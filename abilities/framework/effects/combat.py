"""Combat-specific metadata effects."""

import game_engine

from .registry import leaf_register
from ..statics import can_block


def _champion_id_for_owner(handler, bstate, owner_id):
    """Return the displayed champion UID for a player when available."""
    if (bstate or {}).get("pvp"):
        for pid, uid in ((bstate or {}).get("champ_map") or {}).items():
            try:
                if int(pid) == int(owner_id):
                    return int(uid)
            except (TypeError, ValueError):
                continue
    profile = getattr(handler, "user_profile", None) or {}
    attr = "_player_champ_scid" if int(owner_id or 0) == int(
        profile.get("id", 0) or 0) else "_ai_champ_scid"
    champion = getattr(handler, attr, None)
    return int(champion.uid.uid64) if champion is not None else None


def _owner_for_champion(handler, bstate, champion_uid):
    """Resolve a combat defender champion UID to its player ID."""
    if (bstate or {}).get("pvp"):
        for pid, uid in ((bstate or {}).get("champ_map") or {}).items():
            try:
                if int(uid) == int(champion_uid):
                    return int(pid)
            except (TypeError, ValueError):
                continue
    profile = getattr(handler, "user_profile", None) or {}
    for owner_id in (0, profile.get("id", 0)):
        if _champion_id_for_owner(handler, bstate, owner_id) == int(champion_uid):
            return owner_id
    return None


def _active_combat(bstate, attacker_uid):
    """Return the active attacker map and the map used to store its blockers."""
    state = bstate or {}
    if state.get("pvp"):
        attackers = state.get("attackers") or {}
        return attackers, "blockers"
    for key in ("ai_attackers", "player_attackers"):
        attackers = state.get(key) or {}
        if any(int(uid) == int(attacker_uid) for uid in attackers):
            # PvE's shared combat resolver uses ai_blockers for either side.
            return attackers, "ai_blockers"
    return {}, "ai_blockers"


def _combat_defender_id(handler, bstate, attackers, attacker_uid):
    for uid, defender in attackers.items():
        try:
            if int(uid) == int(attacker_uid):
                return _owner_for_champion(handler, bstate, int(defender))
        except (TypeError, ValueError):
            continue
    return None


@leaf_register("BlockEffectTemplate")
def _leaf_block(game, session, db, handler, pl_t, ai_t, bstate,
                effect_guid, param):
    """Assign the metadata-selected created troop as a combat blocker.

    The original client receives the attacking troop as this effect's target
    and takes the blocker from ``SecondaryTargetIndex`` (normally the troop
    created by the preceding Conscript/PutIntoPlay effects). No card text or
    card-name special case is needed here.
    """
    attacker_uid = (bstate or {}).get("player_spell_target")
    blocker_uid = (bstate or {}).get("resolving_secondary_target_uid")
    if attacker_uid is None or blocker_uid is None:
        return "block: missing attacker or blocker"
    attacker_uid = int(attacker_uid)
    blocker_uid = int(blocker_uid)

    attackers, blocker_key = _active_combat(bstate, attacker_uid)
    defender_id = _combat_defender_id(
        handler, bstate, attackers, attacker_uid)
    blocker_row = db.execute(
        "SELECT user_id, location, card_type, card_state "
        "FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, blocker_uid)).fetchone()
    attacker_row = db.execute(
        "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, attacker_uid)).fetchone()
    if not attackers or not attacker_row or attacker_row[0] != "warzone":
        return "block: attacker is not active"
    if not blocker_row or blocker_row[1] != "warzone":
        return "block: blocker is not in play"
    if "Troop" not in (blocker_row[2] or ""):
        return "block: blocker is not a troop"
    if defender_id is not None and int(blocker_row[0]) != int(defender_id):
        return "block: blocker controls the wrong side"
    existing = (bstate or {}).get(blocker_key) or {}
    if any(blocker_uid in [int(value) for value in (values or [])]
           for values in existing.values()):
        return "block: blocker already assigned"
    if not can_block(db, session.session_id, bstate,
                     attacker_uid, blocker_uid):
        return "block: illegal combat assignment"

    assigned = list(existing.get(str(attacker_uid), []) or [])
    if blocker_uid in [int(value) for value in assigned]:
        return "block: blocker already assigned"
    assigned.append(blocker_uid)
    existing[str(attacker_uid)] = [str(value) for value in assigned]
    bstate[blocker_key] = existing
    db.execute(
        "UPDATE game_cards SET card_state = card_state | ? "
        "WHERE session_id=? AND card_uid=?",
        (game_engine.ECardStates.Blocking | game_engine.ECardStates.HasBlocked,
         session.session_id, blocker_uid))
    db.commit()

    defender_uid = None
    for uid, value in attackers.items():
        if int(uid) == attacker_uid:
            defender_uid = int(value)
            break
    if defender_uid is None:
        return "block: defender missing"
    if (bstate or {}).get("pvp"):
        defender_owner = _owner_for_champion(handler, bstate, defender_uid)
        attacker_owner = next(
            (int(pid) for pid in (bstate.get("champ_map") or {})
             if int(pid) != int(defender_owner or -1)), 0)
        combat_owner = game_engine.UID.make(244, attacker_owner)
    else:
        combat_owner = ai_t if (bstate.get("ai_attackers") and
                                str(attacker_uid) in bstate["ai_attackers"]) else pl_t
    combat_id = game_engine.CombatId(combat_owner, attacker_uid & 0xFFFF)
    blocker_scids = [game_engine.SessionCardId(game_engine.UID(value))
                     for value in assigned]
    game.push_blockers_assigned(
        combat_id,
        game_engine.SessionCardId(game_engine.UID(attacker_uid)),
        game_engine.SessionCardId(game_engine.UID(defender_uid)),
        blocker_scids)

    # AssignBlocker queues these client trigger events. Resolve them through
    # the same shared trigger dispatcher after the visible combat event.
    from ..triggers import resolve_triggers
    blocker_owner = int(blocker_row[0])
    resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                     "CardBlockedEvent", blocker_uid, blocker_owner,
                     extra_target=attacker_uid)
    attacker_owner = _owner_for_champion(handler, bstate, defender_uid)
    if attacker_owner is not None:
        resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                         "CardWasBlockedEvent", attacker_uid,
                         attacker_owner)
    return f"blocked {hex(attacker_uid)} with {hex(blocker_uid)}"
