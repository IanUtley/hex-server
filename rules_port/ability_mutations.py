"""RulesPort-owned mutations for dynamic card abilities."""

from __future__ import annotations

import json

import game_engine


def _abilities(value):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(guid).lower() for guid in parsed] if isinstance(parsed, list) else []


def revoke_base_abilities(context, target: int) -> str:
    """Remove authored base abilities and project the resulting card state."""
    from pvp_db import db_card_ability_state, db_set_card_abilities
    from .card_projection import push_card_state

    row = db_card_ability_state(context.session.session_id, int(target),
                                conn=context.db)
    if not row:
        return "revoke base abilities: target not found"
    current = _abilities(row[0])
    base = set(_abilities(row[1]))
    remaining = [guid for guid in current if guid not in base]
    db_set_card_abilities(context.session.session_id, int(target),
                          json.dumps(remaining), conn=context.db)
    context.db.commit()
    push_card_state(context.game, context.session, context.db, context.handler,
                    context.player_uid, context.ai_uid, int(target),
                    int(row[2] or 0), context.bstate)
    return f"revoked {len(current) - len(remaining)} base abilities"


def shift_ability(context, source: int, target: int, ability_guid: str) -> str:
    """Move one ability between card instances using only native projections."""
    from pvp_db import (db_card_ability_payload, db_card_mutation_info,
                        db_card_state_value, db_set_card_abilities_and_attributes,
                        db_template_attributes)
    from .card_projection import push_card_state
    from .triggers import dispatch_native_trigger

    source = int(source)
    target = int(target)
    ability_guid = str(ability_guid).lower()
    source_row = db_card_mutation_info(
        context.session.session_id, source, conn=context.db)
    target_row = db_card_mutation_info(
        context.session.session_id, target, conn=context.db)
    if not source_row or not target_row:
        return "shift: source or target card not found"

    source_abilities = _abilities(db_card_ability_payload(
        context.session.session_id, source, conn=context.db))
    target_abilities = _abilities(db_card_ability_payload(
        context.session.session_id, target, conn=context.db))
    source_abilities = [guid for guid in source_abilities if guid != ability_guid]
    if ability_guid not in target_abilities:
        target_abilities.append(ability_guid)

    for row, uid, abilities in (
            (source_row, source, source_abilities),
            (target_row, target, target_abilities)):
        attributes = int(db_template_attributes(row[0], conn=context.db) or 0)
        db_set_card_abilities_and_attributes(
            context.session.session_id, uid, json.dumps(abilities), attributes,
            conn=context.db)
    context.db.commit()

    target_owner = int(target_row[1] or 0)
    dispatch_native_trigger(
        db=context.db, handler=context.handler, game=context.game,
        session=context.session, player_uid=context.player_uid,
        ai_uid=context.ai_uid, battle_state=context.bstate,
        event_type="PowerShiftedEvent", source_card_id=target,
        source_player_id=target_owner, target_card_id=target)

    # Reuse the RulesPort card projection for the wire update. It reads the
    # updated per-instance ability list and preserves the existing card state.
    push_card_state(context.game, context.session, context.db, context.handler,
                    context.player_uid, context.ai_uid, source,
                    int(db_card_state_value(
                        context.session.session_id, source, conn=context.db) or 0),
                    context.bstate)
    push_card_state(context.game, context.session, context.db, context.handler,
                    context.player_uid, context.ai_uid, target,
                    int(db_card_state_value(
                        context.session.session_id, target, conn=context.db) or 0),
                    context.bstate)
    return (f"shift {ability_guid[:8]} {hex(source)} -> {hex(target)}")
