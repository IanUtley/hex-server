"""Native chain-item resolution shared by every mode host.

RulesPort owns chain ordering, the resolver lifecycle, and the
picker-continuation marker.  A mode host owns only its packet projection:
which ``Game`` buffer the authored effects write into, how those events reach
each client, its own warzone bookkeeping, and its post-resolution priority or
end-of-game rules.

Practice/PvE (``hconnect_server``) and tournament PvP
(``services.tournament_game``) each used to keep a copy of this lifecycle.
They drifted: the PvP copy never produced ``completed_chain_instance_id``, so
a Deathcry deck search re-ran its BOM once per remaining candidate (Darkspire
Priestess asked four times for one death), and PvP had grown three
overlapping per-kind resolvers.  Both hosts now resolve through
:func:`resolve_chain_item` and supply the hooks below.

Host interface (duck-typed; no base class is required):

``chain_load(session) -> dict``
    The authoritative checkpoint for this mode.
``chain_save(session, state) -> None``
``chain_new_game(session, state, player_uid, ai_uid) -> Game``
    A fresh packet buffer carrying the current battle projection.
``chain_send(session, game, player_uid, ai_uid) -> None``
    Publish the buffer (Practice: the one client; PvP: both players).
``chain_card_data(game, scid, template_guid) -> tuple``
    ``(template_guid, card_type, name, cost, attack, defense, gems)``.
``chain_dispatch(session, game, state, player_uid, ai_uid, event_type,
source_uid, owner_id, **event_data) -> None``
    The mode's native trigger boundary.
``chain_mark_warzone_entry(session, source_uid) -> None``
    Optional permanent bookkeeping (arrival stamp / resolve counter).
``chain_push_empty(port, state, item, pending) -> bool``
    Whether this item's completion empties the client chain.
``chain_state_based(session, state, game, player_uid, ai_uid) -> bool``
    Apply state-based actions before this item can return priority.  The hook
    receives the same packet buffer as the item so lethal cleanup and any
    resulting Deathcry events are published in order.
``chain_finalize(session, state, game, item, pending, player_uid, ai_uid)``
    Optional in-packet priority projection before the buffer is sent.
``chain_after_item(session, state, game, item, player_uid, ai_uid)``
    Optional post-send step (queue the next item, check the game end).
``chain_ability_owner(state, item, owner_id, player_uid, ai_uid) -> int``
    Optional owner normalization for a manual/champion ability item.

A hook that is absent falls back to the host's existing projection seam
(``_card_full_data`` / ``_dispatch_game_trigger``) or to the documented
default above, so a focused test double only has to provide the piece under
test.
"""

from __future__ import annotations

from typing import Any, cast

import game_engine

from .actions import AbilityResolutionState

# Continuation markers that hold a chain item open until the client answers.
PENDING_INPUT_KEYS = (
    "pending_choice",
    "pending_trigger",
    "pending_deck_search",
    "pending_conversation",
    "pending_discard_ability",
)


def pending_input(state) -> bool:
    """True while a client prompt owns the paused chain item."""
    return any(state.get(key) for key in PENDING_INPUT_KEYS)


def _move_prompt_events_last(game, pending):
    """Leave a triggered-ability picker as the final UI transition.

    Trigger prompts are discovered while the current chain item is applying
    an effect.  The prompt events are therefore initially appended before the
    item's resolved/removed events.  Unity processes one packet in order; if
    the prompt comes first, the later chain events immediately pop the dialog
    again.  Keep the authoritative gameplay events in their existing order,
    but send the interactive prompt after them so it owns the client's UI.
    """
    if not pending:
        return
    prompt_names = {
        "PlayerOptionListSessionEventArgs",
        "TriggeredAbilityActivationDataRequiredSessionEventArgs",
        "AbilityActivationDataRequiredSessionEventArgs",
        "GreenLightSessionEventArgs",
    }
    prompt_events = [
        event for event in game.events
        if event.__class__.__name__ in prompt_names
    ]
    if not prompt_events:
        return
    game.events = [
        event for event in game.events
        if event.__class__.__name__ not in prompt_names
    ] + prompt_events


def _hook(host, name):
    hook = getattr(host, name, None)
    return hook if callable(hook) else None


def card_data(host, game, scid, template_guid):
    """Card definition for a re-projected card (gems included when authored)."""
    hook = _hook(host, "chain_card_data")
    if hook is not None:
        return hook(game, scid, template_guid)
    return host._card_full_data(game, scid, template_guid)


def dispatch(host, session, game, state, player_uid, ai_uid, event_type,
             source_uid, owner_id, **event_data):
    hook = _hook(host, "chain_dispatch")
    if hook is not None:
        return hook(session, game, state, player_uid, ai_uid, event_type,
                    source_uid, owner_id, **event_data)
    legacy = _hook(host, "_dispatch_game_trigger")
    if legacy is not None:
        return legacy(
            game, session, player_uid, ai_uid, state, event_type, source_uid,
            owner_id, **event_data)
    # Small AI/test hosts do not need to duplicate HCPHandler's private
    # forwarding method.  Keep this shared boundary usable for them by
    # dispatching directly through the native trigger backend instead of
    # requiring an otherwise unrelated host adapter method.
    from .triggers import dispatch_native_trigger
    connection = getattr(host, "_db", None)
    if connection is None:
        import db as db_layer
        connection = db_layer._db
    target_card_id = event_data.pop("target_card_id", None)
    return dispatch_native_trigger(
        db=connection, handler=host, game=game, session=session,
        player_uid=player_uid, ai_uid=ai_uid, battle_state=state,
        event_type=event_type, source_card_id=source_uid,
        source_player_id=owner_id, target_card_id=target_card_id,
        data=event_data)


def dispatch_card_cast(host, session, game, state, player_uid, ai_uid,
                       card_uid, owner_id):
    """Dispatch the one CardCastEvent emitted when a card is played.

    This runs while the card is entering CastSpells, before a permanent gains
    Warzone trigger registration. Do not dispatch it again when the chain item
    resolves.
    """
    from .runtime_helpers import champion_uid_for_owner
    state["card_cast_copy_target"] = int(card_uid)
    try:
        dispatch(
            host, session, game, state, player_uid, ai_uid,
            "CardCastEvent",
            champion_uid_for_owner(host, state, owner_id), owner_id,
            target_card_id=int(card_uid))
    finally:
        state.pop("card_cast_copy_target", None)
    # HandleGameRulesTriggers: after the authored listeners, a resource cast
    # activates the built-in Momentum ability on the caster's warzone troops.
    return _activate_momentum(host, session, game, state, player_uid, ai_uid,
                              card_uid, owner_id)


def _activate_momentum(host, session, game, state, player_uid, ai_uid,
                       card_uid, owner_id):
    """Session.HandleGameRulesTriggers' Momentum pass for one cast."""
    import json

    import game_engine
    from pvp_db import (db_card_mutation_field, db_card_source_info,
                        db_set_card_mutation_field,
                        db_warzone_blocker_uids)
    from .effect_lifetimes import record_temporary_stat
    from .runtime_helpers import owner_uid
    from .static_rules import card_has_int_attr

    session_id = session.session_id
    import db as db_layer
    conn = getattr(db_layer, "_db", None)
    info = db_card_source_info(session_id, int(card_uid), conn=conn)
    if not info or "Resource" not in str(info[1] or ""):
        return []
    granted = []
    for (uid,) in db_warzone_blocker_uids(
            session_id, int(owner_id or 0), 0, conn=conn):
        uid = int(uid)
        if not card_has_int_attr(conn, session_id, uid, "Momentum"):
            continue
        try:
            buffs = json.loads(db_card_mutation_field(
                session_id, uid, "permanent_buffs", conn=conn) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            buffs = {}
        buffs["atk"] = int(buffs.get("atk", 0) or 0) + 1
        buffs["def"] = int(buffs.get("def", 0) or 0) + 1
        db_set_card_mutation_field(
            session_id, uid, "permanent_buffs",
            json.dumps(buffs, separators=(",", ":"), sort_keys=True),
            conn=conn)
        record_temporary_stat(
            state, uid, 1, 1, duration="BeginningOfOwnersTurn",
            owner_id=owner_id, source_uid=card_uid)
        granted.append(uid)
    if not granted:
        return []
    conn.commit()
    row_owner = int(info[3] or 0)
    for uid in granted:
        scid = game_engine.SessionCardId(game_engine.UID(uid))
        row = db_card_source_info(session_id, uid, conn=conn)
        if not row:
            continue
        _tpl, card_type, _name, cost, attack, defense, gems = \
            host._card_full_data(game, scid, row[0])
        game.push_card_updated(
            scid, owner_uid(row_owner, player_uid, ai_uid, state),
            game_engine.ECardCollections.Warzone, card_type,
            template_id=row[0], cost=cost, attack=attack, defense=defense,
            gems=gems)
    return granted


def dispatch_card_zone_transition(host, session, game, state, player_uid,
                                  ai_uid, card_uid, owner_id,
                                  source_collection, destination_collection,
                                  previous_state=0, *, hidden=True):
    """Publish the C# MoveCard trigger sequence at a play transition."""
    names = {
        "hand": "Hand", "deck": "Deck", "champions": "Champions",
        "warzone": "Warzone", "discard": "Discard", "crypt": "Discard",
        "void": "Void", "playedresources": "PlayedResources",
        "castspells": "CastSpells", "underground": "Underground",
        "choosing": "Choosing", "mod": "Mod", "simulacrum": "Simulacrum",
    }
    raw_source = str(source_collection or "").rsplit(".", 1)[-1]
    raw_destination = str(destination_collection or "").rsplit(".", 1)[-1]
    source = names.get(raw_source.lower(), raw_source)
    destination = names.get(raw_destination.lower(), raw_destination)
    payload = {
        "event_source_collection": source,
        "event_destination_collection": destination,
        "event_previous_state": int(previous_state or 0),
    }
    dispatch(host, session, game, state, player_uid, ai_uid,
             "CardExitedZoneEvent", int(card_uid), owner_id, **payload)
    dispatch(host, session, game, state, player_uid, ai_uid,
             "CardEnteredZoneEvent", int(card_uid), owner_id, **payload)
    if hidden:
        dispatch(host, session, game, state, player_uid, ai_uid,
                 "HiddenCardEnteredZoneEvent", int(card_uid), owner_id,
                 **payload)


def resolve_chain_item(host, port, session, db, ability, player_uid, ai_uid):
    """Resolve one projected chain item through its mode's projection.

    Returns the port resolution state so the caller can decide whether the
    item stays on the chain.
    """
    descriptor = dict(ability.descriptor)
    instance_id = int(ability.instance_id)
    state = host.chain_load(session) or {}
    # A paused resolution owns this chain item until its continuation answers.
    # Re-entering the BOM while the picker is still open re-issued the same
    # prompt (Scheme asked for its deck card twice).
    if (int(state.get("paused_chain_instance_id", 0)) == instance_id and
            pending_input(state)):
        return AbilityResolutionState.WAITING_FOR_INPUT
    # The native action stack selected this typed descriptor; the persisted
    # stack is only the reconnect/wire mirror.  Drop the mirror by identity so
    # a resolved item cannot be resolved twice through the compatibility
    # walker or hold ``stack_empty`` false for the rest of the game.
    state["stack"] = [
        entry for entry in (state.get("stack") or [])
        if int(entry.get("instance_id", -1)) != instance_id]
    # A picker continuation may already have resolved this item's whole BOM.
    # Consume the marker once, here, and never re-run the authored effects: the
    # finish pass owns only the zone/event projection.
    bom_completed = (int(state.pop("completed_chain_instance_id", 0) or 0)
                     == instance_id)
    game = host.chain_new_game(session, state, player_uid, ai_uid)
    kind = descriptor.get("kind")
    if kind in ("troop", "spell"):
        resolve_card_item(host, session, db, game, state, descriptor,
                          player_uid, ai_uid, bom_completed)
    elif kind == "trigger":
        resolve_trigger_item(host, session, db, game, state, descriptor,
                             player_uid, ai_uid, bom_completed)
    elif kind == "ability":
        resolve_ability_item(host, session, db, game, state, descriptor,
                             player_uid, ai_uid, bom_completed)
    else:
        raise RuntimeError(
            "RulesPort chain has no native card resolver for kind "
            f"{kind!r}")
    if state.get("resolution_paused"):
        state["paused_chain_instance_id"] = instance_id
        state.setdefault("stack", []).append(descriptor)
        host.chain_save(session, state)
        host.chain_send(session, game, player_uid, ai_uid)
        return AbilityResolutionState.WAITING_FOR_INPUT
    # C# performs state-based actions after a resolution and before the next
    # player can receive priority.  Keep this before chain removal/finalization
    # so a lethal troop cannot survive the priority packet, and so a Deathcry
    # can add a real native chain item before ChainEmpty is projected.
    state_based = _hook(host, "chain_state_based")
    if state_based is not None:
        state_based(session, state, game, player_uid, ai_uid)
    pending = pending_input(state)
    if not bool(getattr(ability, "ignores_chain", False)):
        # Ability resolution itself removes the native chain item, but the
        # projected chain animation needs the matching resolved/removed
        # events.  An IgnoresChain ability is never added to the client's
        # chain at all (UIBattle.OnAbilityPushedOnChain plays a card event
        # instead), so emitting them would tear down the wrong UI.
        game.push_top_of_chain_resolved(instance_id)
        game.push_removed_top_of_chain(instance_id)
        push_empty = _hook(host, "chain_push_empty")
        if (push_empty(port, state, descriptor, pending) if push_empty
                else (not pending and not state.get("stack"))):
            # RulesPort may still own further native chain items (for example
            # Nerissa's two troop summons followed by the champion ability).
            # Only tell Unity the chain is empty for the final native item.
            game.push_chain_empty()
    _move_prompt_events_last(game, pending)
    finalize = _hook(host, "chain_finalize")
    if finalize is not None:
        finalize(session, state, game, descriptor, pending,
                 player_uid, ai_uid)
    host.chain_save(session, state)
    host.chain_send(session, game, player_uid, ai_uid)
    after_item = _hook(host, "chain_after_item")
    if after_item is not None:
        after_item(session, state, game, descriptor, player_uid, ai_uid)
    return AbilityResolutionState.COMPLETED


def resolve_card_item(host, session, db, game, state, item, player_uid,
                      ai_uid, bom_completed=False):
    """Resolve a troop/artifact/constant or spell chain item."""
    from pvp_db import (db_card_chain_info, db_card_discard_spell,
                        db_card_location, db_card_owner_zone_state,
                        db_set_card_location)

    kind = str(item.get("kind") or "")
    source_uid = int(item.get("source_uid") or 0)
    if not source_uid:
        return
    details = db_card_owner_zone_state(session.session_id, source_uid, conn=db)
    owner_id = int(details[0]) if details else 0
    loc = details[1] if details else "discard"
    owner_sid = player_uid if owner_id else ai_uid
    scid = game_engine.SessionCardId(game_engine.UID(source_uid))
    row = db_card_chain_info(session.session_id, source_uid, conn=db)
    if not row:
        return

    if kind == "troop":
        if loc != "CastSpells":
            return
        entering_state = game_engine.ECardStates.CameOutThisTurn
        from .static_rules import opposing_enters_play_exhausted
        if opposing_enters_play_exhausted(state, owner_id, row[0]):
            entering_state |= game_engine.ECardStates.Tapped
        db_set_card_location(
            session.session_id, source_uid, "warzone",
            extra_set="position=?, card_state=(card_state | ?)",
            extra_params=[0, entering_state],
            conn=db)
        mark_entry = _hook(host, "chain_mark_warzone_entry")
        if mark_entry is not None:
            mark_entry(session, source_uid)
        _tpl, ctype, _name, cost, attack, defense, gems = cast(
            Any, card_data(host, game, scid, row[0]))
        game.push_card_updated(
            scid, owner_sid, game_engine.ECardCollections.Warzone, ctype,
            template_id=row[0], cost=cost, attack=attack, defense=defense,
            gems=gems)
        game.push_card_moved(
            scid, owner_sid, game_engine.ECardCollections.Warzone,
            game_engine.ECardLocations.Top, 0)
        if ctype & game_engine.ECardTypes.Artifact:
            game.push_artifact_card_played(scid, owner_sid)
        else:
            game.push_troop_card_played(scid, owner_sid)
        # CardEnteredZoneEvent is the native trigger boundary for a permanent
        # reaching the warzone. CardCastEvent was emitted when the play was
        # queued, before this permanent gained Warzone trigger registration.
        dispatch(
            host,
            session, game, state, player_uid, ai_uid,
            "CardEnteredZoneEvent", source_uid, owner_id,
            event_source_collection="CastSpells",
            event_destination_collection="warzone")
        return

    if loc != "CastSpells" and not bom_completed:
        return
    if not bom_completed:
        game.push_spell_card_played(scid, owner_sid)
        turn_now = int(state.get("turn_number", 1))
        side = "ai" if owner_id == 0 else "player"
        turn_key = f"{side}_actions_cast_turn"
        count_key = f"{side}_actions_cast_this_turn"
        if state.get(turn_key) != turn_now:
            state[count_key] = 0
            state[turn_key] = turn_now
        state[count_key] = int(state.get(count_key, 0)) + 1
        state["player_spell_target"] = item.get("target_uid")
        state["resolving_source_uid"] = source_uid
        state["resolving_owner_id"] = owner_id
        state["x_cost"] = int(item.get("x_cost") or 0)
        from .resolution import resolve_port_played_spell
        resolve_port_played_spell(
            game, session, db, host, player_uid, ai_uid, state,
            item.get("ability_guids", ()),
            activations=item.get("activations"),
            played_from_hand=bool(item.get("played_from_hand")))
        if state.get("resolution_paused"):
            return
    state.pop("player_spell_target", None)
    state.pop("resolving_source_uid", None)
    state.pop("resolving_owner_id", None)
    state.pop("x_cost", None)
    current_location = str(db_card_location(
        session.session_id, source_uid, conn=db) or "").lower()
    if current_location == "castspells":
        db_card_discard_spell(session.session_id, source_uid, conn=db)
        dispatch(
            host,
            session, game, state, player_uid, ai_uid,
            "CardEnteredZoneEvent", source_uid, owner_id,
            event_source_collection="CastSpells",
            event_destination_collection="discard")
        card_data(host, game, scid, row[0])
        game.push_card_moved(
            scid, owner_sid, game_engine.ECardCollections.Discard,
            game_engine.ECardLocations.Top, 0)
        game.push_card_updated(
            scid, owner_sid, game_engine.ECardCollections.Discard,
            game_engine.card_type_from_db(row[1]), template_id=row[0])
    state.pop("_spell_played_from_hand", None)


def resolve_trigger_item(host, session, db, game, state, item, player_uid,
                         ai_uid, bom_completed=False):
    """Resolve a chain-queued triggered ability.

    ``bom_completed`` marks a picker continuation that already applied the
    trigger's effects.  Only the one-shot bookkeeping that pass skipped is
    still owed: re-entering the Records resolver re-opened the picker and
    applied the effect once per remaining candidate (Darkspire Priestess
    asked for a deck troop four times).
    """
    if bom_completed:
        from .triggers import consume_one_shot_trigger
        consume_one_shot_trigger(
            host, session, game, db, player_uid, ai_uid, state,
            str(item.get("ability_guid") or ""), item.get("source_uid"))
        return
    from .resolution import resolve_port_trigger
    resolve_port_trigger(
        host, game, session, db, player_uid, ai_uid, state, item)


def resolve_ability_item(host, session, db, game, state, item, player_uid,
                         ai_uid, bom_completed=False):
    """Resolve a manual/champion ability chain item into the game buffer."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph

    source_uid = int(item.get("source_uid") or 0)
    owner_id = item.get("owner_id")
    if owner_id is None and source_uid:
        from pvp_db import db_card_owner_id
        owner_id = int(db_card_owner_id(
            session.session_id, source_uid, conn=db) or 0)
    owner_id = int(owner_id or 0)
    normalizer = _hook(host, "chain_ability_owner")
    if normalizer is not None:
        owner_id = int(cast(Any, normalizer(
            state, item, owner_id, player_uid, ai_uid)) or 0)
    ability_guid = str(item.get("ability_guid") or "").lower()
    if bom_completed:
        # A picker continuation already applied this ability's effects; only
        # the chain projection is left.
        return
    if ability_guid == "f2d6797b-1a24-4c3d-9239-a27a2e0de0ff":
        from .context import EffectContext
        from .tunneling import resolve_surface
        resolve_surface(EffectContext.from_rules_port(
            game, session, db, host, player_uid, ai_uid, state,
            ability_guid, ability=None), source_uid)
        return
    graph = ability_graph(DEFAULT_RECORD_STORE, ability_guid)
    activation_data = item.get("activation_data") or {}
    target_map = dict(activation_data.get("target_map") or {}) \
        if isinstance(activation_data, dict) else {}
    target_uid = item.get("target_uid")
    if graph is not None and not target_map and target_uid is not None:
        for index, target_spec in enumerate(graph.targets):
            if getattr(target_spec, "requires_input", False):
                target_map[index] = int(target_uid)
                break
    state["resolving_source_uid"] = source_uid
    state["resolving_owner_id"] = owner_id
    if target_uid is not None:
        state["player_mod_target"] = int(target_uid)
    from .resolution import resolve_port_ability
    resolve_port_ability(
        host, game, session, db, player_uid, ai_uid, state, ability_guid,
        source_uid, owner_id, target_map=target_map,
        variables=(activation_data.get("variables") or {}
                   if isinstance(activation_data, dict) else {}),
        instance_id=int(item.get("instance_id", 1) or 1))
