"""Damage resolution for troops and champions."""

import game_engine

from .._shared import owner_uid


def deal_damage(game, session, db, handler, pl_t, ai_t, bstate, uid, amount):
    """Deal damage to a troop or champion and run replacement/death rules."""
    uid_i = int(uid)
    p = getattr(handler, "_player_champ_scid", None)
    a = getattr(handler, "_ai_champ_scid", None)
    cmap = (bstate or {}).get("champ_map") or {}
    hmap = (bstate or {}).get("pvp_health_map") or {}
    is_champ = False
    champ_owner = None
    if cmap and (bstate or {}).get("pvp"):
        for _k, _v in cmap.items():
            try:
                if int(_v) == uid_i:
                    champ_owner = int(_k)
                    is_champ = True
                    break
            except (TypeError, ValueError):
                continue
    if not is_champ and ((p is not None and uid_i == int(p.uid.uid64)) or
                         (a is not None and uid_i == int(a.uid.uid64))):
        is_champ = True
        champ_owner = (0 if a is not None and uid_i == int(a.uid.uid64)
                       else (handler.user_profile["id"]
                             if handler.user_profile else 0))
    if not is_champ:
        for _k, _v in cmap.items():
            try:
                if int(_v) == uid_i:
                    _hpk = hmap.get(int(_k))
                    champ_owner = 0 if _hpk == "ai_health" else int(_k)
                    is_champ = True
                    break
            except Exception:
                pass
    if is_champ:
        row = ("Champion", champ_owner)
    else:
        row = db.execute(
            "SELECT card_type, user_id FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, uid_i)).fetchone()
        if row is None:
            return "no card"

    from ..triggers import resolve_triggers
    if resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                        "CardWouldBeDamagedEvent", uid_i,
                        source_owner_uid=row[1] if row else 0):
        return "replaced"

    from ..statics import controller_flags, effective_stats
    if row[0] != "Champion":
        _atk, _def, _attrs, flags, _rage = effective_stats(
            db, session.session_id, bstate, uid_i)
        if "prevent_noncombat_damage" in flags:
            return "prevented"
    dealer = (bstate or {}).get("resolving_source_uid")
    if dealer is not None:
        drow = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(dealer))).fetchone()
        if drow:
            _atk2, _def2, _attrs2, flags, _rage = effective_stats(
                db, session.session_id, bstate, int(dealer))
            if ("double_damage" in flags or
                    "double_damage" in controller_flags(
                        db, session.session_id, bstate, drow[0])):
                amount *= 2
    if row[0] == "Champion":
        owner = row[1]
        if (bstate or {}).get("pvp"):
            key = hmap.get(int(owner), "player_health")
        else:
            key = "ai_health" if not owner else "player_health"
        cur = int(bstate.get(key, 20))
        bstate[key] = max(0, cur - amount)
        if (bstate or {}).get("resolving_ability"):
            bstate["_ability_damage_dealt"] = int(
                bstate.get("_ability_damage_dealt", 0) or 0) + max(0, amount)
        setattr(game, key, bstate[key])
        dealer = (bstate or {}).get("resolving_source_uid")
        if dealer is not None:
            turn = int(bstate.get("turn_number", 1))
            if bstate.get("damaged_opponent_turn") != turn:
                bstate["damaged_opponent_this_turn"] = []
                bstate["damaged_opponent_turn"] = turn
            damaged = bstate.setdefault("damaged_opponent_this_turn", [])
            if int(dealer) not in damaged:
                damaged.append(int(dealer))
        ev = game_engine.ChampionHealthChangedSessionEventArgs()
        ev.player_id = owner_uid(owner, pl_t, ai_t, bstate)
        ev.old_damage_value = cur
        ev.new_damage_value = bstate[key]
        game._push(ev)
        return f"champion {cur}->{bstate[key]}"

    from ..kill_troop import kill_troop
    # Use the same effective stat calculation as the card display and state-
    # based death pass.  Permanent/temporary buffs and continuous statics are
    # not stored in card_defense_mod, so reading only the printed defense here
    # makes a buffed troop die as though it were still its base size.
    _atk, remaining_defense, _attrs, _flags, _rage = effective_stats(
        db, session.session_id, bstate, uid_i)
    db.execute(
        "UPDATE game_cards SET card_damage = card_damage + ? "
        "WHERE session_id=? AND card_uid=?",
        (amount, session.session_id, uid_i))
    db.commit()
    if (bstate or {}).get("resolving_ability"):
        bstate["_ability_damage_dealt"] = int(
            bstate.get("_ability_damage_dealt", 0) or 0) + max(0, amount)
    crow = db.execute(
        "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, uid_i)).fetchone()
    from ..bom import _push_card_state
    _push_card_state(game, session, db, handler, pl_t, ai_t, uid_i,
                     int(crow[0]) if crow else 0, bstate=bstate)
    if remaining_defense - amount <= 0:
        kill_troop(game, session, db, handler, pl_t, ai_t, uid_i, bstate,
                   cause="damage")
        return "killed"
    return "survives"
