"""Host-side state/event projections used by the RulesPort.

These functions deliberately accept a host object for the small set of
protocol/event builders that still belong to HConnect.  Gameplay ordering is
already decided by the RulesPort; this module owns the resulting mutation and
its complete client-visible event sequence.
"""

from __future__ import annotations

import json
import game_engine
from pvp_db import db_card_zone_details, db_discard_card


def discard_card_to_owner(host, session, pl_t, ai_t, card_uid):
    """Move a card to its owner graveyard and build the complete projection."""
    from db import _db
    row = db_card_zone_details(session.session_id, card_uid)
    if not row:
        return None, None
    template_guid, _instance_template, owner_uid, _location = row
    owner_uid = owner_uid or 0
    db_discard_card(session.session_id, card_uid, owner_user_id=owner_uid)
    owner_player_uid = ai_t if owner_uid == 0 else pl_t
    game = game_engine.Game(session.session_id, pl_t, ai_t)
    scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
    tpl, card_type, _name, cost, attack, defense, gems = host._card_full_data(
        game, scid, template_guid, None)
    game.push_card_discarded(scid, owner_player_uid)
    # CardRepresentation.Update emits an implicit CardMoved when its
    # collection changes. Emit the explicit move first so the subsequent
    # complete CardUpdated refresh does not schedule a duplicate animation.
    game.push_card_moved(
        scid, owner_player_uid, game_engine.ECardCollections.Discard,
        game_engine.ECardLocations.Top, 0)
    game.push_card_updated(
        scid, owner_player_uid, game_engine.ECardCollections.Discard,
        game_engine.card_type_from_db(card_type)
        if card_type else game_engine.ECardTypes.Troop,
        attack=attack, defense=defense, cost=cost,
        template_id=tpl, gems=gems)
    from . import chain
    from .persistence import load_state
    bstate = load_state(session)
    host._current_bstate = bstate
    from .triggers import PortTriggerDispatcher, TriggerEvent
    dispatch = PortTriggerDispatcher(
        host, game, session, _db, pl_t, ai_t, bstate)
    dispatch(TriggerEvent("CardEnteredZoneEvent", int(card_uid), owner_uid))
    dispatch(TriggerEvent(
        "CardDiscardedEvent", int(card_uid), owner_uid,
        data={"event_source_collection": "hand",
              "event_destination_collection": "discard"}))
    return game, owner_player_uid


def project_mulligan_cards(host, game, event_player_uid, old_rows,
                           new_rows, *, new_player_uid=None,
                           reveal_new=True, move_new=True, draw_new=False,
                           update_old_first=False):
    """Project one redraw to a recipient, preserving hidden information."""
    new_player_uid = new_player_uid or event_player_uid
    for row in old_rows or ():
        scid = game_engine.SessionCardId(game_engine.UID(int(row[1])))
        def update():
            game.push_card_updated(scid, event_player_uid,
                                   game_engine.ECardCollections.Deck,
                                   game_engine.ECardTypes.Unknown, nulling=True)
        def move():
            game.push_card_moved(scid, event_player_uid,
                                 game_engine.ECardCollections.Deck,
                                 game_engine.ECardLocations.Top, 0)
        if update_old_first:
            update(); move()
        else:
            move(); update()
    for index, row in enumerate(new_rows or ()):
        card_uid, template_guid = int(row[0]), row[2]
        scid = game_engine.SessionCardId(game_engine.UID(card_uid))
        _tpl, card_type, _name, _cost, _attack, _defense, gems = \
            host._card_full_data(game, scid, template_guid, row[1])
        owner = new_player_uid if reveal_new else event_player_uid
        if draw_new:
            game.push_card_drawn(scid, owner, index + 1)
        if move_new:
            game.push_card_moved(scid, owner, game_engine.ECardCollections.Hand,
                                 game_engine.ECardLocations.Top, index)
        game.push_card_updated(
            scid, owner, game_engine.ECardCollections.Hand, card_type,
            template_id=template_guid, nulling=not reveal_new, gems=gems)


def project_attacking_card(host, game, session_card_id, player_uid,
                           template_guid, state, attributes=None):
    """Emit the complete cache refresh for a newly declared attacker."""
    _tpl, card_type, _name, _cost, _attack, _defense, _gems = \
        host._card_full_data(game, session_card_id, template_guid)
    game.push_card_updated(
        session_card_id, player_uid, game_engine.ECardCollections.Warzone,
        game_engine.ECardTypes.Troop, template_id=template_guid,
        state=int(state or 0),
        **({"attributes": int(attributes)} if attributes is not None else {}))


def apply_attacking_card_state(session, card_uid, state):
    """Persist the accepted attack state through the host mutation boundary."""
    from pvp_db import db_card_set_attacking_state
    db_card_set_attacking_state(session.session_id, int(card_uid), int(state))


def queue_free_played_card(host, game, session, db, player_uid, ai_uid,
                           battle_state, card_uid, owner_id, template_guid,
                           card_type):
    """Queue a free card with its complete typed client projection."""
    from pvp_db import (db_set_card_played_to_zone, db_card_location,
                        db_add_temporary_attributes, db_card_ability_payload)
    from . import chain
    from .runtime_helpers import owner_uid

    card_uid = int(card_uid)
    owner = owner_uid(owner_id, player_uid, ai_uid, battle_state)
    scid = game_engine.SessionCardId(game_engine.UID(card_uid))
    previous_location = db_card_location(
        session.session_id, card_uid, conn=db)
    surfaced = str(previous_location or "").lower() == "underground"
    db_set_card_played_to_zone(session.session_id, card_uid, "CastSpells",
                               conn=db)
    if surfaced:
        # A troop that untunnels has Speed for the turn it surfaces.  Persist
        # that as a temporary instance attribute so targeting, attack-option
        # generation, combat validation, and CardUpdated all agree, and record
        # the EndTurn boundary: Surface resolves at StartTurn, so the Prep that
        # follows in the same turn must not expire the grant.
        db_add_temporary_attributes(
            session.session_id, card_uid, game_engine.ECardAttributes.Speed,
            conn=db, owner_id=owner_id, boundary="end_turn")
        db.commit()
        from .creation_effects import activate_creation_replacements
        activate_creation_replacements(db, session.session_id, card_uid)
    _tpl, card_type_bits, _name, cost, attack, defense, gems = \
        host._card_full_data(game, scid, template_guid)
    if isinstance(card_type_bits, str):
        card_type_bits = game_engine.card_type_from_db(card_type_bits)
    game.push_card_moved(
        scid, owner, game_engine.ECardCollections.CastSpells,
        game_engine.ECardLocations.Top, 0)
    game.push_card_updated(
        scid, owner, game_engine.ECardCollections.CastSpells,
        card_type_bits, template_id=template_guid, cost=cost,
        attack=attack, defense=defense, gems=gems, nulling=False)
    permanent = bool(card_type_bits & (
        game_engine.ECardTypes.Troop | game_engine.ECardTypes.Artifact |
        game_engine.ECardTypes.Constant))
    if permanent:
        if card_type_bits & game_engine.ECardTypes.Artifact:
            game.push_artifact_card_played(scid, owner)
        else:
            game.push_troop_card_played(scid, owner)
        kind = "troop"
    else:
        game.push_spell_card_cast(scid, owner, free=True)
        kind = "spell"
    payload = db_card_ability_payload(session.session_id, card_uid, conn=db)
    try:
        ability_guids = ([str(value).lower() for value in json.loads(payload)
                          if value] if payload else [])
    except (TypeError, ValueError, json.JSONDecodeError):
        ability_guids = []
    instance_id = int((battle_state or {}).get("_next_instance_id", 1))
    battle_state["_next_instance_id"] = instance_id + 1
    descriptor = {
        "kind": kind, "source_uid": card_uid,
        "ability_guids": ability_guids, "target_uid": None,
        "instance_id": instance_id, "x_cost": 0, "free": True}
    chain.push(battle_state, descriptor)
    chain_guid = (ability_guids[0] if ability_guids else
                  game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID)
    game.push_ability_on_chain(
        scid, game_engine.ResourceId.from_str(chain_guid),
        ability_instance_id=instance_id)
    port = getattr(session, "_rules_port_session", None)
    if port is not None and hasattr(port, "queue_projected_chain"):
        # The native scheduler owns the free-play chain and response window;
        # the compatibility descriptor above is only its durable projection.
        responder = ai_uid if int(owner_id or 0) != 0 else player_uid
        port.queue_projected_chain(
            descriptor, int(owner_id or 0), first_player_id=responder)
    from .persistence import save_state
    save_state(session, battle_state)
    return f"queued {kind} {card_uid} for free (chain={instance_id})"


def play_free_resource_card(host, game, session, db, player_uid, ai_uid,
                            battle_state, card_uid):
    """Play a Resource selected by a native free-play effect.

    This is the effect equivalent of the ordinary RulesPort resource
    transaction.  It deliberately owns only the resulting DB/client
    projection; the enclosing native ability owns ordering and continuation.
    """
    from pvp_db import (db_rules_port_card_projection,
                        db_set_card_played_to_zone)
    from .cast_stats import record_card_cast
    from .runtime_helpers import owner_uid
    row = db_rules_port_card_projection(
        session.session_id, int(card_uid), conn=db)
    if not row or str(row[4] or "").lower() != "resource":
        return "play free resource: card not found"
    owner_id = int(row[2] or 0)
    side = "player" if owner_id else "ai"
    db_set_card_played_to_zone(
        session.session_id, int(card_uid), "PlayedResources")
    state = battle_state
    current_grant = int(row[6] or 0)
    total_grant = int(row[7] or 0)
    state[f"{side}_resources"] = max(
        0, int(state.get(f"{side}_resources", 0) or 0) + current_grant)
    state[f"{side}_total_resources"] = max(
        0, int(state.get(f"{side}_total_resources", 0) or 0) + total_grant)
    state[f"{side}_charges"] = int(
        state.get(f"{side}_charges", 0) or 0) + 1
    record_card_cast(state, owner_id, resource=True)
    color_map = {"ruby": 8, "sapphire": 16, "blood": 4,
                 "diamond": 64, "wild": 32}
    color = color_map.get(str(row[3] or "").split()[0].lower())
    if color is not None:
        thresholds = state.setdefault(f"{side}_threshold", {})
        thresholds[color] = int(thresholds.get(color, 0) or 0) + 1
    db.commit()
    from .persistence import save_state
    save_state(session, state)
    owner = owner_uid(owner_id, player_uid, ai_uid, state)
    scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
    game.push_card_updated(
        scid, owner, game_engine.ECardCollections.PlayedResources,
        game_engine.ECardTypes.Resource, template_id=row[0])
    game.push_resource_card_played(scid, owner, free=True)
    for property_name, amount, value in (
            ("currentresource", current_grant, state[f"{side}_resources"]),
            ("totalresource", total_grant, state[f"{side}_total_resources"])):
        if not amount:
            continue
        event_type = (game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs
                      if property_name == "currentresource" else
                      game_engine.PlayerTotalResourcePoolChangedSessionEventArgs)
        event = event_type()
        event.player_id = owner
        event.operation = 1
        event.delta = amount
        event.new_value = value
        game._push(event)
    if color is not None:
        event = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
        event.player_id = owner
        event.color = color
        event.operation = 1
        event.delta = 1
        event.new_value = state[f"{side}_threshold"][color]
        game._push(event)
    return f"played resource {int(card_uid)} for free"
