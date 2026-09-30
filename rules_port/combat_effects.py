"""Native combat keyword mutations owned by RulesPort."""

from __future__ import annotations

import json


def apply_rage(context, card_uid):
    """Apply authored Rage to an attacker as a permanent attack modifier."""
    from pvp_db import db_card_mutation_field, db_set_card_mutation_field
    from .static_rules import effective_stats

    uid = int(card_uid)
    rage = int(effective_stats(
        context.db, context.session.session_id, context.bstate, uid)[4] or 0)
    if rage <= 0:
        return 0
    raw = db_card_mutation_field(
        context.session.session_id, uid, "permanent_buffs", conn=context.db)
    try:
        buffs = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if not isinstance(buffs, dict):
        buffs = {}
    buffs["atk"] = int(buffs.get("atk", 0) or 0) + rage
    db_set_card_mutation_field(
        context.session.session_id, uid, "permanent_buffs",
        json.dumps(buffs, separators=(",", ":"), sort_keys=True), conn=context.db)
    context.db.commit()
    context._push_modifier_card(uid)
    return rage


def _active_combat(battle_state, attacker_uid):
    """Return the active attacker map and the map that stores its blockers."""
    state = battle_state or {}
    if state.get("pvp"):
        return state.get("attackers") or {}, "blockers"
    for key in ("ai_attackers", "player_attackers"):
        attackers = state.get(key) or {}
        if any(int(uid) == int(attacker_uid) for uid in attackers):
            # PvE's shared combat resolver uses ai_blockers for either side.
            return attackers, "ai_blockers"
    return {}, "ai_blockers"


def block(context):
    """Port of ``BlockEffectTemplate`` (``AssignBlocker``).

    The client receives the attacking troop as this effect's target and takes
    the blocker from ``m_SecondaryTargetIndex`` (normally the troop created by
    the preceding Conscript/PutIntoPlay effects).  No card text or card-name
    special case is needed: the metadata supplies both cards.
    """
    import game_engine
    from pvp_db import (db_card_owner_zone_state, db_card_sacrifice_info,
                        db_update_card_state)

    from .combat_rules import can_block
    from .runtime_helpers import champion_owner_id

    battle_state = context.bstate or {}
    attacker_uid = battle_state.get("player_spell_target")
    if attacker_uid is None:
        attacker_uid = context.resolved_target(source_fallback=False)
    blocker_uid = battle_state.get("resolving_secondary_target_uid")
    if attacker_uid is None or blocker_uid is None:
        return "block: missing attacker or blocker"
    attacker_uid = int(attacker_uid)
    blocker_uid = int(blocker_uid)

    attackers, blocker_key = _active_combat(battle_state, attacker_uid)
    defender_id = None
    for uid, defender in attackers.items():
        try:
            if int(uid) == attacker_uid:
                defender_id = champion_owner_id(
                    context.handler, battle_state, int(defender))
                break
        except (TypeError, ValueError):
            continue
    session_id = context.session.session_id
    blocker_state = db_card_owner_zone_state(
        session_id, blocker_uid, conn=context.db)
    blocker_type = db_card_sacrifice_info(
        session_id, blocker_uid, conn=context.db)
    blocker_row = ((blocker_state[0], blocker_state[1], blocker_type[2],
                    blocker_state[2])
                   if blocker_state and blocker_type else None)
    attacker_state = db_card_owner_zone_state(
        session_id, attacker_uid, conn=context.db)
    attacker_row = (attacker_state[1],) if attacker_state else None
    if not attackers or not attacker_row or attacker_row[0] != "warzone":
        return "block: attacker is not active"
    if not blocker_row or blocker_row[1] != "warzone":
        return "block: blocker is not in play"
    if "Troop" not in (blocker_row[2] or ""):
        return "block: blocker is not a troop"
    if defender_id is not None and int(blocker_row[0]) != int(defender_id):
        return "block: blocker controls the wrong side"
    existing = battle_state.get(blocker_key) or {}
    if any(blocker_uid in [int(value) for value in (values or [])]
           for values in existing.values()):
        return "block: blocker already assigned"
    if not can_block(context.db, session_id, battle_state,
                     attacker_uid, blocker_uid):
        return "block: illegal combat assignment"

    assigned = list(existing.get(str(attacker_uid), []) or [])
    if blocker_uid in [int(value) for value in assigned]:
        return "block: blocker already assigned"
    assigned.append(blocker_uid)
    existing[str(attacker_uid)] = [str(value) for value in assigned]
    battle_state[blocker_key] = existing
    db_update_card_state(
        session_id, blocker_uid,
        set_bits=game_engine.ECardStates.Blocking |
        game_engine.ECardStates.HasBlocked, conn=context.db)
    context.db.commit()

    defender_uid = None
    for uid, value in attackers.items():
        if int(uid) == attacker_uid:
            defender_uid = int(value)
            break
    if defender_uid is None:
        return "block: defender missing"
    if battle_state.get("pvp"):
        defender_owner = champion_owner_id(
            context.handler, battle_state, defender_uid)
        attacker_owner = next(
            (int(pid) for pid in (battle_state.get("champ_map") or {})
             if int(pid) != int(defender_owner or -1)), 0)
        combat_owner = game_engine.UID.make(244, attacker_owner)
    else:
        combat_owner = (context.ai_uid if (
            battle_state.get("ai_attackers") and
            str(attacker_uid) in battle_state["ai_attackers"])
            else context.player_uid)
    combat_id = game_engine.CombatId(combat_owner, attacker_uid & 0xFFFF)
    blocker_scids = [game_engine.SessionCardId(game_engine.UID(value))
                     for value in assigned]
    context.game.push_blockers_assigned(
        combat_id,
        game_engine.SessionCardId(game_engine.UID(attacker_uid)),
        game_engine.SessionCardId(game_engine.UID(defender_uid)),
        blocker_scids)

    # AssignBlocker queues these client trigger events; resolve them through
    # the shared native trigger dispatcher after the visible combat event.
    from .triggers import dispatch_native_trigger
    blocker_owner = int(blocker_row[0])
    dispatch_native_trigger(
        db=context.db, handler=context.handler, game=context.game,
        session=context.session, player_uid=context.player_uid,
        ai_uid=context.ai_uid, battle_state=battle_state,
        event_type="CardBlockedEvent", source_card_id=blocker_uid,
        source_player_id=blocker_owner, target_card_id=attacker_uid)
    attacker_owner = champion_owner_id(
        context.handler, battle_state, defender_uid)
    if attacker_owner is not None:
        dispatch_native_trigger(
            db=context.db, handler=context.handler, game=context.game,
            session=context.session, player_uid=context.player_uid,
            ai_uid=context.ai_uid, battle_state=battle_state,
            event_type="CardWasBlockedEvent", source_card_id=attacker_uid,
            source_player_id=attacker_owner)
    return f"blocked {hex(attacker_uid)} with {hex(blocker_uid)}"
