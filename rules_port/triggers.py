"""C#-shaped trigger events bridged into the shared metadata trigger engine."""

from __future__ import annotations

import json
import random
import game_engine
from dataclasses import dataclass, field
from typing import Any, Callable, cast


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


def consume_one_shot_trigger(handler, session, game, db, player_uid, ai_uid,
                             battle_state, ability_guid, source_uid) -> bool:
    """Consume a resolved ONE-SHOT (``uses_per_game == 1``) trigger.

    ONE-SHOT is an instance property: the ability triggers once and the card
    then loses it (C# ``Card.PayPerGameCosts`` / ``HasAbility``).  Every
    trigger path therefore reports the resolved trigger here — the mode host
    owns the per-instance write and the CardUpdated projection that drops the
    power from the client's card, so the two authorities cannot drift.
    """
    if source_uid is None:
        return False
    ability = str(ability_guid or "").lower()
    if not ability:
        return False
    consume = getattr(handler, "_remove_one_shot_ability", None)
    if callable(consume):
        try:
            return bool(consume(session, int(source_uid), ability, game,
                                player_uid, ai_uid, battle_state))
        except Exception:
            return False
    # Headless interpreters have no host seam (focused tests, tooling).  Drop
    # the ability from the instance list directly so the invariant still
    # holds outside a live service.
    from pvp_db import (db_ability_activation_metadata, db_card_ability_list,
                        db_set_card_abilities)
    meta = db_ability_activation_metadata(ability, conn=db)
    if not meta or int(meta[1] or 0) != 1:
        return False
    current = [str(value).lower() for value in db_card_ability_list(
        session.session_id, int(source_uid), conn=db)]
    if ability not in current:
        return False
    current.remove(ability)
    db_set_card_abilities(
        session.session_id, int(source_uid), json.dumps(current), conn=db)
    db.commit()
    return True


def _claim_trigger_uses(db, session, battle_state, graph, source_uid) -> bool:
    """Claim authored per-game/per-turn trigger uses before queueing it.

    Triggered abilities bypass manual activation validation, so without this
    claim a ``UsesPerTurn`` trigger can be queued again by events raised while
    its first copy resolves. Persist the claim on the card instance just as a
    manual card ability does.
    """
    costs = getattr(graph, "costs", None)
    game_limit = int(getattr(costs, "uses_per_game", 0) or 0)
    turn_limit = int(getattr(costs, "uses_per_turn", 0) or 0)
    if game_limit <= 0 and turn_limit <= 0:
        return True

    from pvp_db import (db_card_ability_use_counts, db_card_basic,
                        db_record_card_ability_use)
    guid = str(getattr(graph, "guid", "") or "").lower()
    turn_number = int((battle_state or {}).get("turn_number", 1) or 1)
    source_uid = int(source_uid)
    if db_card_basic(session.session_id, source_uid, conn=db):
        game_uses, turn_uses = db_card_ability_use_counts(
            session.session_id, source_uid, guid, turn_number, conn=db)
        if ((game_limit > 0 and game_uses >= game_limit) or
                (turn_limit > 0 and turn_uses >= turn_limit)):
            return False
        db_record_card_ability_use(
            session.session_id, source_uid, guid, turn_number, conn=db,
            uses_per_game=game_limit > 0,
            uses_per_turn=turn_limit > 0)
        db.commit()
        return True

    # Synthetic champions have no game_cards row. Keep their trigger budget
    # in the shared checkpoint so it survives the next event/chain transition.
    key = f"{source_uid}:{guid}"
    uses = (battle_state or {}).setdefault("trigger_ability_uses", {})
    entry = dict(uses.get(key) or {})
    game_uses = int(entry.get("game_uses", 0) or 0)
    entry_turn = int(entry.get("turn_number", -1) or -1)
    turn_uses = (int(entry.get("turn_uses", 0) or 0)
                 if entry_turn == turn_number else 0)
    if ((game_limit > 0 and game_uses >= game_limit) or
            (turn_limit > 0 and turn_uses >= turn_limit)):
        return False
    uses[key] = {
        "game_uses": game_uses + (1 if game_limit > 0 else 0),
        "turn_number": turn_number,
        "turn_uses": turn_uses + (1 if turn_limit > 0 else 0),
    }
    return True


class RecordsTriggerBackend:
    """Execute Records trigger metadata behind the RulesPort boundary."""

    def __init__(self, resolver: Callable | None = None) -> None:
        self.resolver = resolver

    def __call__(self, *, db, handler, game, session, player_uid, ai_uid,
                 battle_state, event: TriggerEvent):
        if ((getattr(session, "_rules_port_session", None) is not None or
             battle_state.get("_rules_port_attached")) and
                not battle_state.get("_rules_port_allow_legacy_backend")):
            raise RuntimeError(
                "RecordsTriggerBackend cannot run in an attached live session; "
                "use NativeTriggerBackend or explicitly enable rollback")
        if self.resolver is None:
            raise RuntimeError(
                "RecordsTriggerBackend requires an explicit resolver")
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
        from pvp_db import (db_card_basic, db_card_location,
                            db_card_owner_id, db_card_state_value)
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
        # AbilityTriggerCardTargetTemplate selects SourceCardId or
        # TargetCardId exactly as carried by the C# TriggerEvent. Do not
        # synthesize a target from the source for event classes that have no
        # TargetCardId; their TriggerTarget picker is empty in the client.
        trigger_target_uid = event.target_card_id

        # A permanent entering play activates its authored creation-replacement
        # markers (IntAttrModifiers such as ShinhareCreationBonus or
        # CreateShinhareMilitiaInsteadOfBattleHopper).  The client reads them
        # from the card context at creation time; the port persists them as
        # permanent data on the entering card.
        if (event_name == "CardEnteredZoneEvent" and
                event.source_card_id is not None and
                str(event.data.get("event_destination_collection")
                    or "").lower() == "warzone"):
            from .creation_effects import activate_creation_replacements
            activate_creation_replacements(
                db, session.session_id, int(event.source_card_id))

        # Keep event-local counters in the port state before condition
        # evaluation, matching the client's event ordering.
        if event_name == "TurnStartedEvent":
            # StartTurnState.UpdateStats clears every card's ThisTurnsData
            # before TurnStartedEvent is processed. These maps are the port's
            # authoritative equivalent for stored target lists at card scope.
            battle_state.pop("stored_targets_by_card_this_turn", None)
            battle_state.pop("stored_targets_this_turn", None)
        elif event_name == "CardDiscardedEvent":
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
            # Champions have no ``game_cards`` row, so resolve them from the
            # handler's per-battle champion identities.  Falling back to the
            # event's player made an OPPOSING champion's ability look like it
            # was owned by that player: the AI's Mentor of the Grave then
            # reacted to cards entering the human's hand from the human's
            # crypt ("when a troop enters YOUR hand from YOUR crypt").
            profile = getattr(handler, "user_profile", None) or {}
            for attr, owner_id in (
                    ("_player_champ_scid",
                     int(profile.get("id", 0) or 0)),
                    ("_ai_champ_scid", 0)):
                champion = getattr(handler, attr, None)
                if champion is None:
                    continue
                try:
                    if int(getattr(getattr(champion, "uid", champion),
                                   "uid64", champion)) == int(uid):
                        return int(owner_id)
                except (TypeError, ValueError):
                    continue
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
            if not location or str(location).lower() == "champion":
                return
            try:
                import game_engine
                scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
                from .runtime_helpers import (card_collection_for_location,
                                              owner_uid)
                collection = card_collection_for_location(location)
                # Skip when this batch already published the card in the same
                # collection (the death transition pushes its own discard
                # CardUpdated); re-publishing duplicated the death update and
                # made the client replay the move animation.
                target_uid64 = int(scid.uid.uid64)
                for prior in reversed(game.events):
                    prior_scid = getattr(prior, "session_card_id", None)
                    prior_uid = getattr(
                        getattr(prior_scid, "uid", prior_scid), "uid64",
                        prior_scid)
                    if prior_uid is None:
                        continue
                    try:
                        prior_uid64 = int(cast(Any, prior_uid))
                    except (TypeError, ValueError):
                        prior_uid64 = None
                    if prior_uid64 != target_uid64:
                        continue
                    if prior.__class__.__name__ != "CardUpdatedSessionEventArgs":
                        break
                    if int(getattr(prior, "collection", -1)) == int(collection):
                        # A hidden hand/deck projection is deliberately sent
                        # first.  A trigger sourced from that card is a
                        # public chain object, however, so its complete card
                        # representation must follow the face-down refresh.
                        # Visible updates are already authoritative and do not
                        # need another copy.
                        if not getattr(prior, "nulling", False):
                            return
                        break
                    break
                tpl, ctype, _name, cost, attack, defense, gems = \
                    handler._card_full_data(game, scid, template_guid)
                game.push_card_updated(
                    scid, owner_uid(owner, player_uid, ai_uid, battle_state),
                    collection, ctype,
                    template_id=tpl, cost=cost, attack=attack,
                    defense=defense, gems=gems,
                    state=int(db_card_state_value(
                        session.session_id, int(uid), conn=db) or 0))
                if str(location).lower() in {"hand", "deck", "choosing"}:
                    # Game.make_network_packet uses this marker to retain the
                    # public chain reveal when the normal hidden-hand/deck
                    # projection is appended later in the same packet.
                    game.events[-1]._chain_reveal = True
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
                # Inspect the lightweight AbilityTemplate before constructing
                # its full target/effect graph. A cold CardEntered/CardCast
                # event otherwise deserializes every manual/static ability in
                # the session before finding the few matching triggers.
                ability_record = DEFAULT_RECORD_STORE.get(
                    "AbilityTemplate", key[1])
                if ability_record is None:
                    continue
                trigger_type = str(
                    getattr(ability_record, "trigger_event_type", "") or "")
                dynamic_source = (
                    event_name == "CardEnteredZoneEvent" and
                    source_uid in {int(uid) for uid in
                                   (getattr(handler,
                                            "_champion_granted_ability_guids",
                                            {}) or {})})
                if event_name == "CardEnteredZoneEvent" and not dynamic_source:
                    from .effect_lifetimes import champion_grants
                    dynamic_source = bool(champion_grants(
                        battle_state, source_uid))
                if (event_name != "GameStartedEvent" and
                        str(trigger_type).rsplit(".", 1)[-1] != event_name and
                        not dynamic_source):
                    continue
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
                # A permanent champion ability whose CardModifier has a card
                # filter is a continuous aura.  Re-evaluate it when a card
                # enters play under that champion so grants made during
                # encounter setup affect troops deployed later as well.
                dynamic = getattr(handler,
                                  "_champion_granted_ability_guids", {}) or {}
                from .effect_lifetimes import champion_grants
                dynamic_source = (source_uid in {int(uid) for uid in dynamic} or
                                  bool(champion_grants(
                                      battle_state, source_uid)))
                continuous_card_aura = bool(
                    event_name == "CardEnteredZoneEvent" and
                    dynamic_source and
                    not graph.trigger_event_type and
                    any(effect.concrete_type == "CardModifierAbilityEffectTemplate" and
                        str(effect.duration).lower() == "permanent"
                        for effect in graph.effects) and
                    any(target.card_filter for target in graph.targets))
                if (str(graph.trigger_event_type or "").rsplit(".", 1)[-1]
                        != event_name and not static_grant and
                        not continuous_card_aura):
                    continue
                # Card.CanTrigger: an opposing champion's
                # OpposingDeathcriesCantTrigger blocks this card's Deathcry.
                if (event_name == "CardEnteredZoneEvent" and
                        _ability_has_deathcry(graph) and
                        _opponents_block_deathcries(
                            battle_state, source_owner)):
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
                # C# treats an unset/None collection mask as unrestricted
                # (``Card.PassesCollectionFlagRequirements`` and
                # ``Session.cs`` skip the zone test for None); only a real
                # mask restricts where the trigger can fire from.
                allowed = {str(value).lower() for value in
                           str(graph.trigger_collection_flags or "").split("|")
                           if value}
                allowed.discard("none")
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
                    event_tac=event.data.get("event_tac"),
                    event_previous_owner_id=event.data.get(
                        "event_previous_owner_id"))
                if not trigger_condition_met(graph.source.to_dict(), context):
                    continue
                entering_uid = event.source_card_id
                entry_event = (event_name == "AsEntersPlayEvent" and
                               entering_uid is not None)
                if (entry_event and entering_uid is not None and
                        not _ability_has_valid_entry_targets(
                            db, session.session_id, source_card_owner,
                            entering_uid, graph, battle_state, champions)):
                    continue
                inspiring = (entry_event and
                             _card_has_inspire(
                                 db, session.session_id, int(source_uid),
                                 battle_state, handler, game))
                if inspiring and entering_uid is not None:
                    from .statistics import add_tac_stat
                    add_tac_stat(
                        battle_state, "cards", entering_uid,
                        "CardStatsWithSpecificDuration", "InspireCount", 1)
                    # C# emits CardInspiredEvent for each valid Inspire
                    # ability before queuing that ability. Preserve the
                    # source/entered-card identities, including self-inspire.
                    inspired = dispatch_native_trigger(
                        db=db, handler=handler, game=game, session=session,
                        player_uid=player_uid, ai_uid=ai_uid,
                        battle_state=battle_state,
                        event_type="CardInspiredEvent",
                        source_card_id=int(source_uid),
                        source_player_id=source_card_owner,
                        target_card_id=entering_uid, data={})
                    if inspired:
                        logs.append(inspired)
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
                    if source_card_owner != 0:
                        prompt = (getattr(handler, "_prompt_optional_trigger", None)
                                  if graph.optional else
                                  getattr(handler, "_prompt_trigger_targets", None))
                        if callable(prompt):
                            prompt(
                                game, player_uid, ai_uid, session, battle_state,
                                source_uid, key[1],
                                tuple(graph.targets[index].guid for index in explicit),
                                candidates, owner_id=source_card_owner,
                                trigger_target_uid=event.target_card_id)
                            logs.append(
                                f"{event_name} {key[1][:8]} -> awaiting "
                                f"{'optional choice' if graph.optional else 'target'}")
                            continue
                    target = candidates[0] if candidates else None
                    if target is None:
                        continue
                elif source_card_owner == 0 and target is None:
                    from .targeting import ai_trigger_target
                    target = ai_trigger_target(
                        db, session, key[1], source_uid, source_card_owner,
                        battle_state, getattr(handler, "_champion_targets", lambda: [])() or [])
                elif (graph.optional and source_card_owner != 0 and
                      callable(getattr(handler, "_prompt_optional_trigger", None))):
                    handler._prompt_optional_trigger(
                        game, player_uid, ai_uid, session, battle_state,
                        source_uid, key[1], (), [], owner_id=source_card_owner,
                        trigger_target_uid=event.target_card_id)
                    logs.append(f"{event_name} {key[1][:8]} -> awaiting optional choice")
                    continue

                if not _claim_trigger_uses(
                        db, session, battle_state, graph, source_uid):
                    continue

                instance_id = int(battle_state.get("_next_instance_id", 1))
                battle_state["_next_instance_id"] = instance_id + 1
                ignores = bool(
                    graph.ignores_chain or force_ignores_chain or
                    event_name == "TurnStartedEvent" or
                    str(location or "").lower() == "underground" or
                    continuous_card_aura)
                if ignores:
                    from .resolution import resolve_port_ability
                    old_source = battle_state.get("resolving_source_uid")
                    old_owner = battle_state.get("resolving_owner_id")
                    old_trigger_target = battle_state.get(
                        "resolving_trigger_target_uid")
                    old_trigger_source = battle_state.get(
                        "resolving_trigger_source_uid")
                    old_event_type = battle_state.get(
                        "resolving_trigger_event_type")
                    old_event_data = battle_state.get(
                        "resolving_trigger_event_data")
                    battle_state["resolving_source_uid"] = source_uid
                    battle_state["resolving_owner_id"] = source_card_owner
                    battle_state["resolving_trigger_target_uid"] = \
                        trigger_target_uid
                    battle_state["resolving_trigger_source_uid"] = \
                        event.source_card_id
                    battle_state["resolving_trigger_event_type"] = event_name
                    battle_state["resolving_trigger_event_data"] = dict(
                        event.data or {})
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
                        if old_trigger_source is None:
                            battle_state.pop("resolving_trigger_source_uid", None)
                        else:
                            battle_state["resolving_trigger_source_uid"] = old_trigger_source
                        if old_event_type is None:
                            battle_state.pop("resolving_trigger_event_type", None)
                        else:
                            battle_state["resolving_trigger_event_type"] = old_event_type
                        if old_event_data is None:
                            battle_state.pop("resolving_trigger_event_data", None)
                        else:
                            battle_state["resolving_trigger_event_data"] = old_event_data
                    logs.append(f"{event_name} {key[1][:8]} -> {result}")
                    if not battle_state.get("resolution_paused"):
                        consume_one_shot_trigger(
                            handler, session, game, db, player_uid, ai_uid,
                            battle_state, key[1], source_uid)
                else:
                    import game_engine
                    from . import chain
                    activation_payload = {}
                    if event_name == "CardActivatedEvent":
                        activation_payload = {
                            field: event.data[field]
                            for field in (
                                "activated_ability_guid",
                                "activated_source_uid",
                                "activated_target_uid",
                                "activated_ability_instance_id",
                                "activated_activation_data")
                            if field in event.data
                        }
                    chain.push(battle_state, {
                        "kind": "trigger", "ability_guid": key[1],
                        "source_uid": source_uid, "target_uid": target,
                        "trigger_source_uid": event.source_card_id,
                        "trigger_target_uid": trigger_target_uid,
                        "trigger_event_type": event_name,
                        "trigger_event_data": dict(event.data or {}),
                        "source_owner_uid": source_card_owner,
                        "instance_id": instance_id,
                        **activation_payload})
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
                            "trigger_target_uid": trigger_target_uid,
                            "trigger_event_type": event_name,
                            "trigger_event_data": dict(event.data or {}),
                            "source_owner_uid": source_card_owner,
                            "instance_id": instance_id,
                            **activation_payload,
                        }, source_card_owner, first_player_id=first_player)
                    logs.append(f"{event_name} {key[1][:8]} -> chain")
        logs_result = "; ".join(logs)
        if (event_name == "CardEnteredZoneEvent" and
                event.source_card_id is not None and
                str(event.data.get("event_destination_collection")
                    or "").lower() == "warzone"):
            # C# raises AsEntersPlayEvent after the entering card's Deploy
            # (CardEnteredZone) triggers.  It carries the entering card as
            # source and event target so a card's own "as this enters play"
            # triggers and every other card's Inspire conditions can resolve.
            enters_logs = dispatch_native_trigger(
                db=db, handler=handler, game=game, session=session,
                player_uid=player_uid, ai_uid=ai_uid,
                battle_state=battle_state, event_type="AsEntersPlayEvent",
                source_card_id=int(event.source_card_id),
                source_player_id=event.source_player_id,
                target_card_id=int(event.source_card_id), data={})
            if enters_logs:
                logs_result = (f"{logs_result}; {enters_logs}"
                               if logs_result else enters_logs)
        return logs_result


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


def dispatch_card_activated(*, db, handler, game, session, player_uid, ai_uid,
                            battle_state, ability_guid, source_card_id,
                            source_player_id, ability_instance_id,
                            activation_data, dispatcher=None):
    """Publish C# ActivateAbility's CardActivatedEvent with its instance."""
    activation = dict(activation_data or {})
    target_map = activation.get("target_map") or {}
    target_uid = None
    for selected in target_map.values() if isinstance(target_map, dict) else ():
        values = selected if isinstance(selected, (tuple, list, set)) else (selected,)
        for value in values:
            try:
                target_uid = int(getattr(value, "uid64", value))
                break
            except (TypeError, ValueError):
                continue
        if target_uid is not None:
            break
    event_data = {
        "activated_ability_guid": str(ability_guid or "").lower(),
        "activated_source_uid": int(source_card_id),
        "activated_target_uid": target_uid,
        "activated_ability_instance_id": int(ability_instance_id),
        "activated_activation_data": activation,
    }
    missing = object()
    previous = {key: battle_state.get(key, missing) for key in event_data}
    battle_state.update(event_data)
    try:
        publish = dispatcher or dispatch_native_trigger
        return publish(
            db=db, handler=handler, game=game, session=session,
            player_uid=player_uid, ai_uid=ai_uid,
            battle_state=battle_state,
            event_type="CardActivatedEvent",
            source_card_id=int(source_card_id),
            source_player_id=int(source_player_id or 0),
            data=event_data)
    finally:
        for key, value in previous.items():
            if value is missing:
                battle_state.pop(key, None)
            else:
                battle_state[key] = value


def _ability_has_deathcry(graph):
    """Whether one authored ability carries the Deathcry TAC keyword."""
    try:
        from .tac import _tac_attr_hash, decode_tac
        data = str(getattr(graph, "serialized_tac", "") or "")
        return bool(data and _tac_attr_hash("Deathcry") in decode_tac(data))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def opponents_have_int_attr(battle_state, owner, attribute) -> bool:
    """Session.CheckOpponentsForIntAttr for one named champion IntAttr."""
    from .static_rules import _champion_int_attrs
    wanted = str(attribute or "").lower()
    for participant in (battle_state.get("champ_map") or {}):
        try:
            opponent = int(participant)
        except (TypeError, ValueError):
            continue
        if opponent == int(owner or 0):
            continue
        for name, value in _champion_int_attrs(battle_state, opponent).items():
            if str(name).lower() == wanted and int(value or 0) > 0:
                return True
    return False


def _opponents_block_deathcries(battle_state, owner):
    """Session.CheckOpponentsForIntAttr(OpposingDeathcriesCantTrigger)."""
    return opponents_have_int_attr(
        battle_state, owner, "OpposingDeathcriesCantTrigger")


def dispatch_native_trigger(*, db, handler, game, session, player_uid, ai_uid,
                            battle_state, event_type, source_card_id,
                            source_player_id=None, target_card_id=None,
                            data=None, force_ignores_chain=False):
    """Dispatch a host-emitted event without constructing a legacy context.

    ``force_ignores_chain`` lets a mode resolve an authored trigger inline
    instead of chaining it.  Merry-Melee-Corinth uses it for the end-of-turn
    ability, which resolves without a priority window in that format.
    """
    event_name = str(event_type).rsplit(".", 1)[-1]
    if event_name == "CardEnteredZoneEvent":
        _record_typed_entry_statistics(
            db, session, battle_state, source_card_id, source_player_id,
            data or {})
    # Duration belongs to each authored effect mapping.  Reconcile zone-bound
    # grants before discovering this event's trigger candidates so a departed
    # source cannot fire its just-expired ability from a stale instance list.
    from .effect_lifetimes import expire_grants, expire_zone_bound_modifiers
    expired = expire_grants(
        db, session.session_id, battle_state, event_type=event_name,
        event_source_uid=source_card_id,
        handler=handler, game=game, player_uid=player_uid, ai_uid=ai_uid)
    expired = list(dict.fromkeys(expired + expire_zone_bound_modifiers(
        db, session.session_id, battle_state)))
    for uid in expired:
        from pvp_db import db_card_source_info
        from .runtime_helpers import card_collection_for_location, owner_uid
        row = db_card_source_info(session.session_id, int(uid), conn=db)
        if not row:
            continue
        template_guid, card_type, location, owner = row
        scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
        try:
            tpl, card_type, _name, cost, attack, defense, gems = \
                handler._card_full_data(game, scid, template_guid)
        except Exception:
            continue
        game.push_card_updated(
            scid, owner_uid(owner or 0, player_uid, ai_uid, battle_state),
            card_collection_for_location(location), card_type,
            template_id=tpl, cost=cost, attack=attack, defense=defense,
            gems=gems, nulling=str(location or "").lower() == "deck")
    previous_event_type = battle_state.get("event_type")
    battle_state["event_type"] = event_name
    try:
        return NativeTriggerBackend()(
            db=db, handler=handler, game=game, session=session,
            player_uid=player_uid, ai_uid=ai_uid, battle_state=battle_state,
            force_ignores_chain=force_ignores_chain,
            # ``data`` must be passed by keyword: the positional slot after
            # ``target_card_id`` is ``target_player_id``.  Passing it
            # positionally dropped every authored collection/state fact
            # (event_source_collection, event_destination_collection,
            # event_previous_state, event_tac, ...) from host-emitted events,
            # which silently made the zone-entry conditions lenient.
            event=TriggerEvent(event_name, source_card_id,
                               source_player_id, target_card_id,
                               data=dict(data or {})))
    finally:
        if previous_event_type is None:
            battle_state.pop("event_type", None)
        else:
            battle_state["event_type"] = previous_event_type


def _record_typed_entry_statistics(db, session, state, card_uid, owner_id,
                                   data):
    """Apply C# entry counters whose authored variable type marks the card.

    SourcePlayerBriarLegionVariable is the sole shipped authored type that
    reads the Briar Legion entry counter. The client increments it for one
    template, identified by its authored ability metadata. Resolve that marker
    from Records rather than branching on the localized card name.
    """
    destination = str(data.get("event_destination_collection") or "").lower()
    previous = str(data.get("event_source_collection") or "").lower()
    if destination != "warzone" or previous == "warzone" or card_uid is None:
        return
    from .statistics import set_tac_stat
    set_tac_stat(state, "cards", int(card_uid),
                 "CardStatsWithSpecificDuration", "InspireCount", 0)
    from pvp_db import db_card_ability_list, db_ability_raw_json
    try:
        ability_guids = db_card_ability_list(
            session.session_id, int(card_uid), conn=db)
    except (TypeError, ValueError):
        ability_guids = ()
    for guid in ability_guids or ():
        try:
            raw = json.loads(db_ability_raw_json(str(guid), conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        variables = raw.get("m_Variables") or []
        if any(str(variable.get("_t", "")).rsplit(".", 1)[-1] ==
               "SourcePlayerBriarLegionVariable" for variable in variables):
            from .statistics import add_champion_card_stat
            add_champion_card_stat(
                state, int(owner_id or 0),
                "BriarLegionsPlayedThisGame", 1)
            return


def _card_has_inspire(db, session_id, card_uid, state, handler, game):
    """Mirror ``Card.HasInspire`` from authored card attributes/context."""
    try:
        attributes = int(__import__("rules_port.static_rules",
                                    fromlist=["effective_attributes"])
                        .effective_attributes(
                            db, session_id, state, int(card_uid)))
        if attributes & int(game_engine.ECardAttributes.Inspire):
            return True
    except (AttributeError, TypeError, ValueError, RuntimeError):
        pass
    try:
        from gamedata import DEFAULT_RECORD_STORE
        from pvp_db import db_card_zone_details
        row = db_card_zone_details(session_id, int(card_uid), conn=db)
        record = (DEFAULT_RECORD_STORE.get("CardTemplate", str(row[0]).lower())
                  if row and row[0] else None)
        serialized = record.field("m_SerializedTAC") if record else None
        data = (serialized.field("data", "")
                if serialized is not None and hasattr(serialized, "field") else
                serialized.get("data", "")
                if isinstance(serialized, dict) else "")
        from .tac import _tac_attr_hash, decode_tac
        if int(decode_tac(data).get(_tac_attr_hash("Inspire"), 0) or 0) > 0:
            return True
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        card_def = getattr(game, "card_defs", {}).get(scid)
        if card_def is None and hasattr(handler, "_card_full_data"):
            from pvp_db import db_card_source_info
            row = db_card_source_info(session_id, int(card_uid), conn=db)
            if row and row[0]:
                handler._card_full_data(game, scid, row[0])
                card_def = getattr(game, "card_defs", {}).get(scid)
        values = getattr(card_def, "int_attrs", {}) if card_def else {}
        if int((values or {}).get("Inspire", 0) or 0) > 0:
            return True
    except (AttributeError, TypeError, ValueError):
        pass
    from pvp_db import db_card_mutation_field
    for column in ("permanent_buffs", "temporary_buffs"):
        try:
            payload = json.loads(db_card_mutation_field(
                session_id, int(card_uid), column, conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        attrs = payload.get("int_attrs", {}) if isinstance(payload, dict) else {}
        try:
            if int(attrs.get("Inspire", 0) or 0) > 0:
                return True
        except (AttributeError, TypeError, ValueError):
            pass
    return False


def _ability_has_valid_entry_targets(db, session_id, owner_id, entering_uid,
                                     graph, state, champions):
    """Port the client's AbilityHasValidTargets check for AsEntersPlay."""
    from .targeting import legal_targets, target_uses_both_players
    targets = tuple(getattr(graph, "targets", ()) or ())
    for effect in tuple(getattr(graph, "effects", ()) or ()):
        try:
            index = int(effect.target_index)
        except (AttributeError, TypeError, ValueError):
            return False
        if index < 0 or index >= len(targets):
            return False
        target = targets[index]
        # The client skips variable minima and best-effort minimum templates
        # in AbilityHasValidTargets; the resolving activation handles them.
        if target.min_variable or target.allow_best_effort_minimum:
            continue
        minimum = int(target.minimum or 0)
        if minimum <= 0:
            continue
        candidates = legal_targets(
            db, session_id, int(owner_id or 0), target.guid,
            int(entering_uid),
            both_players=target_uses_both_players(db, target.guid),
            champions=champions, battle_state=state)
        if len(candidates) < minimum:
            return False
    return True
