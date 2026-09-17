"""C#-shaped trigger events bridged into the shared metadata trigger engine."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class TriggerEvent:
    """Python counterpart of ``Game.Shared.Mechanics.TriggerEvent``.

    Concrete client event classes add fields, so this representation retains a
    typed common source/target envelope and a metadata dictionary for those
    additions. It is safe to persist as a continuation payload.
    """

    event_type: str
    source_card_id: int | None = None
    source_player_id: int | None = None
    target_card_id: int | None = None
    target_player_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


def trigger_collection_allows(trigger_flags, card_location):
    """Mirror ``Card.PassesCollectionFlagRequirements`` from the client."""
    if not trigger_flags:
        return True
    flags = str(trigger_flags)
    if not flags or flags.strip().lower() in ("none", "null"):
        return True
    allowed = {value.strip().lower() for value in flags.split("|")
               if value.strip()}
    location = str(card_location or "").lower()
    return not location or location in allowed


class RecordsTriggerBackend:
    """Execute Records trigger metadata behind the RulesPort boundary."""

    def __init__(self, resolver: Callable | None = None) -> None:
        if resolver is None:
            from abilities.framework.triggers import resolve_triggers
            resolver = resolve_triggers
        self.resolver = resolver

    def __call__(self, *, db, handler, game, session, player_uid, ai_uid,
                 battle_state, event: TriggerEvent):
        if ((getattr(session, "_rules_port_session", None) is not None or
             battle_state.get("_rules_port_attached")) and
                not battle_state.get("_rules_port_allow_legacy_backend")):
            raise RuntimeError(
                "RecordsTriggerBackend cannot run in an attached live session; "
                "use NativeTriggerBackend or explicitly enable rollback")
        previous = battle_state.get("_rules_port_native_effect")
        battle_state["_rules_port_native_effect"] = True
        try:
            return self.resolver(
                db, handler, game, session, player_uid, ai_uid, battle_state,
                event.event_type, event.source_card_id,
                source_owner_uid=event.source_player_id,
                extra_target=event.target_card_id,
                zones=event.data.get("zones"),
                event_source_collection=event.data.get(
                    "event_source_collection"),
                event_destination_collection=event.data.get(
                    "event_destination_collection"),
                event_previous_state=event.data.get("event_previous_state"),
                event_int_attribute=event.data.get("event_int_attribute"),
                event_tac=dict(event.data.get("event_tac") or {}),
            )
        finally:
            if previous is None:
                battle_state.pop("_rules_port_native_effect", None)
            else:
                battle_state["_rules_port_native_effect"] = previous


class NativeTriggerBackend:
    """Dispatch Records triggers without the historical trigger scanner."""

    def __call__(self, *, db, handler, game, session, player_uid, ai_uid,
                 battle_state, event: TriggerEvent,
                 force_ignores_chain: bool = False):
        from gamedata import ability_graph, DEFAULT_RECORD_STORE
        from pvp_db import db_card_basic, db_card_location, db_card_owner_id
        from rules_port.counter_effects import TUNNELING_ABILITY_GUID
        from rules_port.conditions import ConditionContext, trigger_condition_met
        from rules_port.trigger_discovery import RecordsTriggerDiscovery

        def chance_to_happen(graph):
            """Read the client-authored ChanceToHappen TAC value."""
            try:
                from .tac import _tac_attr_hash, decode_tac
                serialized = graph.source.field("m_SerializedTAC")
                data = (serialized.field("data", "")
                        if hasattr(serialized, "field") else
                        serialized.get("data", "")
                        if isinstance(serialized, dict) else "")
                value = decode_tac(data).get(
                    _tac_attr_hash("ChanceToHappen"))
                return (100 if value is None else
                        max(0, min(100, int(value))))
            except (AttributeError, TypeError, ValueError,
                    json.JSONDecodeError):
                return 100

        event_name = str(event.event_type).rsplit(".", 1)[-1]
        if event_name != event.event_type:
            event = TriggerEvent(
                event_name, event.source_card_id, event.source_player_id,
                event.target_card_id, event.target_player_id,
                dict(event.data or {}))

        # Keep event-local counters in the port state before condition
        # evaluation, matching the client's event ordering.
        if event.event_type == "CardDiscardedEvent":
            owner = int(event.source_player_id or 0)
            key = (f"cards_discarded_this_turn_{owner}"
                   if battle_state.get("pvp") else
                   ("player_cards_discarded_this_turn" if owner
                    else "ai_cards_discarded_this_turn"))
            battle_state[key] = int(battle_state.get(key, 0) or 0) + 1
        elif event.event_type == "TurnStartedEvent":
            owner = int(event.source_player_id or 0)
            key = (f"cards_discarded_this_turn_{owner}"
                   if battle_state.get("pvp") else
                   ("player_cards_discarded_this_turn" if owner
                    else "ai_cards_discarded_this_turn"))
            battle_state[key] = 0
        elif event.event_type == "GainThresholdEvent":
            if "gain_threshold_color" in event.data:
                battle_state["gain_threshold_color"] = event.data[
                    "gain_threshold_color"]

        discovered = RecordsTriggerDiscovery(
            db, handler, session, player_uid, ai_uid, battle_state).discover(
                event.event_type, event.source_card_id,
                event.source_player_id, event.target_card_id,
                event.data.get("zones"))
        logs = []
        seen = set()
        champions = []
        champion_fn = getattr(handler, "_champion_targets", None)
        if callable(champion_fn):
            try:
                champions = champion_fn() or []
            except Exception:
                champions = []

        def owner_of(uid, fallback):
            if uid is None:
                return fallback
            owner = db_card_owner_id(session.session_id, int(uid), conn=db)
            if owner is not None:
                return int(owner)
            for pid, champion in (battle_state.get("champ_map") or {}).items():
                if int(champion) == int(uid):
                    return int(pid)
            pchamp = getattr(handler, "_player_champ_scid", None)
            if pchamp is not None and int(pchamp.uid.uid64) == int(uid):
                return int((getattr(handler, "user_profile", None) or {}).get("id", 0))
            return fallback

        def chain_targets(uid):
            if uid is None:
                return []
            import game_engine
            return [game_engine.SessionCardId(game_engine.UID(int(uid)))]

        def push_source(uid, owner):
            # The normal projection path may already have published this card;
            # only repair an absent source representation when the host offers
            # the same complete card-data seam.
            if not hasattr(handler, "_card_full_data") or uid is None:
                return
            basic = db_card_basic(session.session_id, int(uid), conn=db)
            if not basic:
                return
            template_guid = basic[0]
            location = db_card_location(session.session_id, int(uid), conn=db)
            # ``db_card_location`` returns the ZONE, not a template.  Passing
            # it as the template GUID made ``_card_full_data`` fall back to the
            # all-zero template, and ``card_collection_for_location`` defaults
            # any unknown zone to Warzone — so an end-of-turn champion trigger
            # re-published Corinth as an empty warzone card.  Champions are
            # already represented through PlayerUpdated.ChampionId, so never
            # project them as a collection card.
            if not location or str(location).lower() in {
                    "hand", "deck", "void", "choosing", "champion"}:
                return
            try:
                import game_engine
                scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
                tpl, ctype, _name, cost, attack, defense, gems = \
                    handler._card_full_data(game, scid, template_guid)
                from .runtime_helpers import (card_collection_for_location,
                                              owner_uid)
                game.push_card_updated(
                    scid, owner_uid(owner, player_uid, ai_uid, battle_state),
                    card_collection_for_location(location), ctype,
                    template_id=tpl, cost=cost, attack=attack,
                    defense=defense, gems=gems)
            except Exception:
                pass

        for candidate in discovered:
            source_uid = int(candidate.source_uid)
            source_owner = owner_of(source_uid, event.source_player_id or 0)
            for ability_guid in candidate.ability_guids:
                key = (source_uid, str(ability_guid).lower())
                if key in seen:
                    continue
                seen.add(key)
                graph = ability_graph(DEFAULT_RECORD_STORE, key[1])
                if graph is None:
                    continue
                if (event_name == "TurnStartedEvent" and
                        key[1] == TUNNELING_ABILITY_GUID):
                    continue
                location = "champions" if source_uid in {
                    int(value) for value in (battle_state.get("champ_map") or {}).values()
                } else db_card_location(session.session_id, source_uid, conn=db)
                # Encounter setup cards in the hidden ``mod`` collection can
                # carry a direct, non-triggered start-of-game BOM (or a
                # permanent GrantAbility).  The client resolves these during
                # GameStarted before the opening priority window.  They are
                # not ordinary triggered abilities, so admit them explicitly
                # from their authored metadata and hidden setup location.
                static_grant = False
                if event_name == "GameStartedEvent" and not graph.trigger_event_type:
                    static_grant = any(
                        effect.concrete_type == "GrantAbilityEffectTemplate" and
                        str(effect.duration).lower() == "permanent"
                        for effect in graph.effects)
                    if str(location or "").lower() == "mod" and not graph.manual:
                        static_grant = True
                if (str(graph.trigger_event_type or "").rsplit(".", 1)[-1]
                        != event_name and not static_grant):
                    continue
                trigger_location = (
                    "warzone" if str(location or "").lower() == "mod"
                    else str(location or ""))
                # The DB row has already moved when a self-enter event is
                # dispatched.  Match the client's destination/previous-zone
                # collection semantics instead of the post-move row alone.
                if (event_name == "CardEnteredZoneEvent" and
                        event.source_card_id is not None and
                        int(event.source_card_id) == source_uid and
                        event.data.get("event_destination_collection")):
                    trigger_location = (
                        event.data.get("event_source_collection")
                        if graph.uses_previous_state and
                        event.data.get("event_source_collection") else
                        event.data.get("event_destination_collection"))
                allowed = {str(value).lower() for value in
                            str(graph.trigger_collection_flags or "").split("|") if value}
                if (trigger_location and allowed and
                        str(trigger_location).lower() not in allowed):
                    continue
                source_card_owner = source_owner
                context = ConditionContext(
                    db, session, battle_state, event_type=event_name,
                    ability_source_uid=source_uid,
                    ability_source_owner_id=source_card_owner,
                    trigger_uid=event.source_card_id,
                    pl_t=player_uid, ai_t=ai_uid,
                    extra_target=event.target_card_id,
                    champions=champions,
                    trigger_owner_id=event.source_player_id,
                    event_source_collection=event.data.get("event_source_collection"),
                    event_destination_collection=event.data.get("event_destination_collection"),
                    event_previous_state=event.data.get("event_previous_state"),
                    uses_previous_state=graph.uses_previous_state,
                    event_int_attribute=event.data.get("event_int_attribute"),
                    event_tac=event.data.get("event_tac"))
                if not trigger_condition_met(graph.source.to_dict(), context):
                    continue
                chance = chance_to_happen(graph)
                if chance < 100 and random.randrange(100) >= chance:
                    logs.append(f"{event_name} {key[1][:8]} -> chance failed ({chance}%)")
                    continue
                target = event.target_card_id or event.source_card_id
                explicit = [index for index, spec in enumerate(graph.targets)
                            if spec.requires_input and spec.explicit]
                # Only a player-input target consumes the trigger target.  An
                # ability whose targets are all auto (Corinth's end-of-turn
                # "your hand"/"your crypt") must NOT receive the event source
                # as target 0, or the effect resolves against the champion.
                requires_input_index = next(
                    (index for index, spec in enumerate(graph.targets)
                     if spec.requires_input), None)
                if explicit:
                    from .targeting import legal_targets
                    template = graph.targets[explicit[0]].guid
                    candidates = legal_targets(
                        db, session.session_id, source_card_owner, template,
                        source_uid, both_players=True,
                        champions=(getattr(handler, "_champion_targets", lambda: [])() or []),
                        battle_state=battle_state)
                    if source_card_owner != 0 and hasattr(handler, "_prompt_trigger_targets"):
                        handler._prompt_trigger_targets(
                            game, player_uid, ai_uid, session, battle_state,
                            source_uid, key[1],
                            tuple(graph.targets[index].guid for index in explicit),
                            candidates)
                        logs.append(f"{event_name} {key[1][:8]} -> awaiting target")
                        continue
                    target = candidates[0] if candidates else None
                    if target is None:
                        continue
                elif source_card_owner == 0 and target is None:
                    from .targeting import ai_trigger_target
                    target = ai_trigger_target(
                        db, session, key[1], source_uid, source_card_owner,
                        battle_state, getattr(handler, "_champion_targets", lambda: [])() or [])

                instance_id = int(battle_state.get("_next_instance_id", 1))
                battle_state["_next_instance_id"] = instance_id + 1
                ignores = bool(
                    graph.ignores_chain or force_ignores_chain or
                    event_name == "TurnStartedEvent" or
                    str(location or "").lower() == "underground")
                if ignores:
                    from .resolution import resolve_port_ability
                    old_source = battle_state.get("resolving_source_uid")
                    old_owner = battle_state.get("resolving_owner_id")
                    old_trigger_target = battle_state.get(
                        "resolving_trigger_target_uid")
                    battle_state["resolving_source_uid"] = source_uid
                    battle_state["resolving_owner_id"] = source_card_owner
                    battle_state["resolving_trigger_target_uid"] = \
                        event.source_card_id
                    try:
                        result = resolve_port_ability(
                            handler, game, session, db, player_uid, ai_uid,
                            battle_state, key[1], source_uid, source_card_owner,
                            target_map=({requires_input_index: int(target)}
                                        if (requires_input_index is not None
                                            and target is not None) else {}),
                            instance_id=instance_id)
                    finally:
                        battle_state["resolving_source_uid"] = old_source
                        battle_state["resolving_owner_id"] = old_owner
                        if old_trigger_target is None:
                            battle_state.pop("resolving_trigger_target_uid", None)
                        else:
                            battle_state["resolving_trigger_target_uid"] = old_trigger_target
                    logs.append(f"{event_name} {key[1][:8]} -> {result}")
                else:
                    import game_engine
                    from . import chain
                    chain.push(battle_state, {
                        "kind": "trigger", "ability_guid": key[1],
                        "source_uid": source_uid, "target_uid": target,
                        "trigger_target_uid": event.source_card_id,
                        "source_owner_uid": source_card_owner,
                        "instance_id": instance_id})
                    push_source(source_uid, source_card_owner)
                    game.push_ability_on_chain(
                        game_engine.SessionCardId(game_engine.UID(source_uid)),
                        game_engine.ResourceId.from_str(key[1]),
                        ability_instance_id=instance_id,
                        target_card_ids=chain_targets(target), ignores_chain=False)
                    # In tournament PvP the mode adapter owns a typed native
                    # chain as well as the wire-compatible checkpoint. Keep
                    # the two projections aligned at creation time. Without
                    # this, a trigger created during a native manual ability
                    # existed only in ``state['stack']`` and could not receive
                    # a RulesPort response window until a later card resolver
                    # happened to reify it.
                    port = getattr(session, "_rules_port_session", None)
                    queue_projected = getattr(
                        port, "queue_projected_chain", None)
                    if callable(queue_projected):
                        owner_player = getattr(
                            port, "_uid_for_raw_player", lambda value: None)(
                                source_card_owner)
                        if owner_player is None:
                            owner_player = player_uid
                        # C# WaitForTriggeredAbilitiesAction.OnEnter calls
                        # UpdatePriorityPlayer(GetActivePlayer()): the active
                        # player responds first to a triggered ability, not
                        # the non-owner. Using the opponent stranded the
                        # server actor with priority and left the trigger on
                        # the chain forever.
                        first_player = getattr(
                            port, "active_player_id", None) or owner_player
                        queue_projected({
                            "kind": "trigger",
                            "ability_guid": key[1],
                            "source_uid": source_uid,
                            "target_uid": target,
                            "trigger_target_uid": event.source_card_id,
                            "source_owner_uid": source_card_owner,
                            "instance_id": instance_id,
                        }, source_card_owner, first_player_id=first_player)
                    logs.append(f"{event_name} {key[1][:8]} -> chain")
        return "; ".join(logs)


class PortTriggerDispatcher:
    """Dispatch typed trigger events through the port-owned trigger seam."""

    def __init__(self, handler, game, game_session, db, player_uid, ai_uid,
                 battle_state: dict) -> None:
        self.handler = handler
        self.game = game
        self.game_session = game_session
        self.db = db
        self.player_uid = player_uid
        self.ai_uid = ai_uid
        self.battle_state = battle_state
        self.backend = NativeTriggerBackend()

    def __call__(self, event: TriggerEvent):
        return self.backend(
            db=self.db, handler=self.handler, game=self.game,
            session=self.game_session, player_uid=self.player_uid,
            ai_uid=self.ai_uid, battle_state=self.battle_state, event=event,
        )


class MetadataTriggerAdapter(PortTriggerDispatcher):
    """Deprecated compatibility adapter; live dispatch is native-only."""

    def __init__(self, handler, game, game_session, db, player_uid, ai_uid,
                 battle_state: dict, *, resolver=None, backend=None) -> None:
        if resolver is None and backend is None:
            super().__init__(handler, game, game_session, db, player_uid, ai_uid,
                             battle_state)
            return
        self.handler, self.game = handler, game
        self.game_session, self.db = game_session, db
        self.player_uid, self.ai_uid = player_uid, ai_uid
        self.battle_state = battle_state
        self.backend = backend or RecordsTriggerBackend(resolver)


def dispatch_trigger(context, event_type, source_card_id, source_player_id=None,
                     target_card_id=None, *, data=None):
    """Emit a trigger through the native dispatcher for an effect context."""
    dispatcher = PortTriggerDispatcher(
        context.handler, context.game, context.session, context.db,
        context.player_uid, context.ai_uid, context.bstate)
    return dispatcher(TriggerEvent(
        event_type, source_card_id, source_player_id, target_card_id,
        data=dict(data or {})))


def dispatch_native_trigger(*, db, handler, game, session, player_uid, ai_uid,
                            battle_state, event_type, source_card_id,
                            source_player_id=None, target_card_id=None,
                            data=None, force_ignores_chain=False):
    """Dispatch a host-emitted event without constructing a legacy context.

    ``force_ignores_chain`` lets a mode resolve an authored trigger inline
    instead of chaining it.  Merry-Melee-Corinth uses it for the end-of-turn
    ability, which resolves without a priority window in that format.
    """
    return NativeTriggerBackend()(
        db=db, handler=handler, game=game, session=session,
        player_uid=player_uid, ai_uid=ai_uid, battle_state=battle_state,
        force_ignores_chain=force_ignores_chain,
        event=TriggerEvent(
            str(event_type).rsplit(".", 1)[-1], source_card_id,
            source_player_id, target_card_id, dict(data or {})))
