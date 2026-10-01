"""RulesPort-owned card-zone event projections.

The port decides the zone transition; this module emits the corresponding
Unity card representation without calling the legacy BOM utility.
"""

from __future__ import annotations

import json

import game_engine


ZONE_EXIT_STATE_FLAGS = (
    game_engine.ECardStates.Tapped |
    game_engine.ECardStates.Blocking |
    game_engine.ECardStates.Attacking |
    game_engine.ECardStates.Damaged |
    game_engine.ECardStates.Healed |
    game_engine.ECardStates.Dead |
    game_engine.ECardStates.HasAttacked |
    game_engine.ECardStates.HasBlocked |
    game_engine.ECardStates.EffectExpired |
    game_engine.ECardStates.ZoneChangeReplacement |
    game_engine.ECardStates.Activated |
    game_engine.ECardStates.CameOutThisTurn |
    game_engine.ECardStates.StartedATurnOnYourSide)


def state_after_zone_exit(state):
    """Clear transient card state as part of a native zone transition."""
    return int(state or 0) & ~int(ZONE_EXIT_STATE_FLAGS)


def move_card_to_zone(db, session_id, card_uid, location, *, owner_id=None,
                      expected_location=None, state=None, position=None):
    """Apply one typed card-zone transition and commit it atomically."""
    from pvp_db import (db_card_location, db_set_card_location,
                        db_move_card_if_in_zone)
    uid = int(card_uid)
    if expected_location is not None:
        moved = db_move_card_if_in_zone(
            session_id, uid, int(owner_id or 0), expected_location,
            location, int(position or 0), state, conn=db)
        if moved <= 0:
            return False
    else:
        if db_card_location(session_id, uid, conn=db) is None:
            return False
        if position is None:
            db_set_card_location(session_id, uid, location, conn=db)
        else:
            db_set_card_location(
                session_id, uid, location, extra_set="position=?",
                extra_params=[int(position)], conn=db)
    db.commit()
    return True


def project_card_runtime(game, session, db, handler, player_uid, ai_uid,
                         battle_state, card_uid: int, location: str) -> bool:
    """Project one already-mutated card in ``location`` to the client."""
    from pvp_db import db_card_source_info
    from .runtime_helpers import card_collection_for_location, owner_uid

    uid = int(card_uid)
    row = db_card_source_info(session.session_id, uid, conn=db)
    if not row:
        return False
    template_guid, card_type, _actual_location, owner_id = row
    scid = game_engine.SessionCardId(game_engine.UID(uid))
    _tpl, card_type, _name, cost, attack, defense, gems = \
        handler._card_full_data(game, scid, template_guid)
    recipient = owner_uid(owner_id, player_uid, ai_uid, battle_state)
    collection = card_collection_for_location(location)
    game.push_card_moved(
        scid, recipient, collection, game_engine.ECardLocations.Top, 0)
    game.push_card_updated(
        scid, recipient, collection, card_type,
        template_id=template_guid, cost=cost, attack=attack,
        defense=defense, gems=gems,
        nulling=str(location).lower() == "deck")
    # Visibility is a client projection concern, not a legacy rules
    # operation. Recompute it after the zone/controller change so a native
    # tunnel or steal cannot leave a stale opponent-hand view.
    from .visibility import refresh_player_visibility
    refresh_player_visibility(
        db, session, handler, game, player_uid, ai_uid, battle_state)
    return True


def project_card(context, card_uid: int, location: str) -> bool:
    return project_card_runtime(
        context.game, context.session, context.db, context.handler,
        context.player_uid, context.ai_uid, context.bstate,
        card_uid, location)


def has_lifebound(db, session_id, card_uid) -> bool:
    """Whether one card's runtime intattrs carry Lifebound."""
    from pvp_db import db_card_mutation_field
    for column in ("permanent_buffs", "temporary_buffs"):
        try:
            data = json.loads(db_card_mutation_field(
                session_id, int(card_uid), column, conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        attrs = data.get("int_attrs") if isinstance(data, dict) else None
        if isinstance(attrs, dict) and any(
                str(name).lower() == "lifebound" and int(value or 0) > 0
                for name, value in attrs.items()):
            return True
    return False


def lifebound(context, owner_id):
    """Session.Lifebound: a player's discard Lifebound cards return to play.

    C# StartTurnState calls this for the active player; the discard cards
    return when one of that player's warzone cards carries the marker.
    """
    import game_engine
    from pvp_db import db_card_zone_details, db_move_card_for_effect
    session_id = context.session.session_id
    owner = int(owner_id or 0)
    warzone = context.db.execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='warzone'", (session_id, owner)).fetchall()
    if not any(has_lifebound(context.db, session_id, uid)
               for (uid,) in warzone):
        return []
    discard = context.db.execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='discard'", (session_id, owner)).fetchall()
    moved = []
    from .static_rules import opposing_enters_play_exhausted
    for (uid,) in discard:
        uid = int(uid)
        if not has_lifebound(context.db, session_id, uid):
            continue
        entering_state = int(game_engine.ECardStates.CameOutThisTurn)
        details = db_card_zone_details(session_id, uid, conn=context.db)
        if details and opposing_enters_play_exhausted(
                context.bstate, owner, details[0]):
            entering_state |= int(game_engine.ECardStates.Tapped)
        db_move_card_for_effect(
            session_id, uid, "warzone", 0, entering_state, clear_dead=True,
            clear_bits=int(game_engine.ECardStates.Dead), conn=context.db)
        context.db.commit()
        project_card(context, uid, "warzone")
        context._emit_trigger(
            "CardEnteredZoneEvent", uid, owner,
            event_source_collection="discard",
            event_destination_collection="warzone",
            event_previous_state=int(game_engine.ECardStates.Dead))
        moved.append(uid)
    return moved
