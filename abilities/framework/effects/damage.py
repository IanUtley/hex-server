"""Damage resolution for troops and champions."""

import json

import game_engine

from .._shared import owner_uid


def _consume_damage_shields(db, session, bstate, target_uid, dealer_uid,
                            amount, is_combat):
    """Apply persisted DamageShieldModifier entries and return damage left."""
    from pvp_db import (db_card_damage_shield_fields,
                        db_set_card_mutation_field)
    stores = []
    row = (db_card_damage_shield_fields(
        session.session_id, int(target_uid), conn=db) if db is not None else None)
    if row:
        for index, raw in enumerate(row):
            try:
                buffs = __import__("json").loads(raw or "{}")
            except (TypeError, ValueError):
                buffs = {}
            if isinstance(buffs, dict) and isinstance(
                    buffs.get("damage_shields"), list):
                stores.append(("card", index, buffs))
    else:
        shields = (bstate or {}).get("damage_shields") or {}
        if isinstance(shields, dict) and isinstance(
                shields.get(str(int(target_uid))), list):
            stores.append(("state", str(int(target_uid)), shields))

    remaining = max(0, int(amount or 0))
    changed_cards = []
    for store_kind, key, container in stores:
        shields = container.get("damage_shields", []) if store_kind == "card" \
            else container.get(key, [])
        store_changed = False
        kept = []
        for shield in shields:
            if remaining <= 0:
                kept.append(shield)
                continue
            if not isinstance(shield, dict):
                continue
            shield_amount = max(0, int(shield.get("amount", 0) or 0))
            if not shield_amount or (shield.get("only_combat") and
                                     not is_combat):
                if shield_amount:
                    kept.append(shield)
                continue
            shield_dealer = shield.get("dealer")
            if (shield_dealer is not None and dealer_uid is not None and
                    int(shield_dealer) != int(dealer_uid)):
                kept.append(shield)
                continue
            if shield_dealer is not None and dealer_uid is None:
                kept.append(shield)
                continue
            blocked = min(remaining, shield_amount)
            remaining -= blocked
            store_changed = True
            if shield.get("one_shot") or blocked >= shield_amount:
                shield_amount = 0
            else:
                shield_amount -= blocked
            shield["amount"] = shield_amount
            if shield_amount:
                kept.append(shield)
        if store_kind == "card":
            if store_changed or len(kept) != len(shields):
                container["damage_shields"] = kept
                changed_cards.append((key, container))
        else:
            container[key] = kept
    if changed_cards:
        import json
        for index, buffs in changed_cards:
            if db is None:
                continue
            db_set_card_mutation_field(
                session.session_id, int(target_uid),
                "permanent_buffs" if index == 0 else "temporary_buffs",
                json.dumps(buffs), conn=db)
        db.commit()
    return remaining


def _card_view(db, session_id, card_uid, bstate=None):
    """Build the small CardFilter view used by typed immunities."""
    if db is None:
        return {
            "card_uid": int(card_uid), "card_type": "Champion",
            "location": "warzone", "user_id": 0, "state": 0,
            "attack": 0, "defense": 0, "name": "Champion", "cost": 0,
            "subtype": "", "shards": [], "card_abilities": [],
        }
    from pvp_db import db_card_filter_view
    row = db_card_filter_view(session_id, int(card_uid), conn=db)
    if not row:
        return {
            "card_uid": int(card_uid), "card_type": "Champion",
            "location": "warzone", "user_id": 0, "state": 0,
            "attack": 0, "defense": 0, "name": "Champion", "cost": 0,
            "subtype": "", "shards": [], "card_abilities": [],
        }
    from ..targeting import shards_from_threshold
    view = {
        "card_uid": int(row[0]), "card_type": row[1] or "",
        "location": row[2] or "", "user_id": row[3],
        "template_guid": row[4] or "", "state": int(row[5] or 0),
        "attack": int(row[6] or 0), "defense": int(row[7] or 0),
        "name": row[8] or "", "cost": int(row[9] or 0),
        "subtype": row[10] or "",
        "shards": shards_from_threshold(row[11]),
        "card_abilities": json.loads(row[12] or "[]")
        if isinstance(row[12], str) else (row[12] or []),
    }
    try:
        buffs = json.loads(row[13] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if isinstance(buffs, dict):
        if isinstance(buffs.get("subtype"), str):
            view["subtype"] = buffs["subtype"]
        if isinstance(buffs.get("thresholds"), list):
            view["shards"] = [int(value) for value in buffs["thresholds"]]
    return view


def _rule_prevents_damage(db, session, bstate, target_uid, dealer_uid,
                          is_combat):
    if db is None or dealer_uid is None:
        return False
    from ..statics import rule_modifiers
    from ..targeting import evaluate_card_filter

    target = _card_view(db, session.session_id, target_uid, bstate)
    dealer = _card_view(db, session.session_id, dealer_uid, bstate)
    dealer["src_owner_id"] = target.get("user_id")
    source = dict(target)
    source["src_owner_id"] = target.get("user_id")
    for rule in rule_modifiers(
            db, session.session_id, bstate, int(target_uid)):
        if rule.get("property") != "damageimmunity":
            continue
        if bool(rule.get("iscombatdamage")) != bool(is_combat):
            continue
        filter_json = rule.get("filter") or rule.get("cardfilter")
        if filter_json and evaluate_card_filter(
                dealer, filter_json, target_uid, source_card=source,
                card_pool=[dealer, target], ability_state=bstate, db=db):
            return True
    return False


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
        from pvp_db import db_card_sacrifice_info
        card = db_card_sacrifice_info(session.session_id, uid_i, conn=db)
        row = (card[2], card[0]) if card else None
        if row is None:
            return "no card"

    from ..triggers import resolve_triggers

    # The client checks the dealer's replacement abilities before it checks
    # the recipient's CardWouldBeDamagedEvent.  Ability damage and combat
    # damage both enter through this shared path, so this is the common
    # metadata-driven hook for cards such as Blasphemous Horror and Brood
    # Count. Champions are not game_cards rows, so recover their controller
    # from the live handler/PvP champion map.
    dealer = (bstate or {}).get("resolving_source_uid")
    if dealer is not None:
        dealer = int(dealer)
        from pvp_db import db_card_owner_id
        dealer_owner = db_card_owner_id(session.session_id, dealer, conn=db)
        if dealer_owner is None and (bstate or {}).get("pvp"):
            for pid, cuid in ((bstate or {}).get("champ_map") or {}).items():
                if int(cuid) == dealer:
                    dealer_owner = int(pid)
                    break
        if dealer_owner is None:
            pchamp = getattr(handler, "_player_champ_scid", None)
            achamp = getattr(handler, "_ai_champ_scid", None)
            if achamp is not None and int(achamp.uid.uid64) == dealer:
                dealer_owner = 0
            elif pchamp is not None and int(pchamp.uid.uid64) == dealer:
                dealer_owner = (handler.user_profile["id"]
                                if handler.user_profile else 0)
        if not (bstate or {}).get("_resolving_would_deal"):
            bstate["_resolving_would_deal"] = True
            try:
                replaced = resolve_triggers(
                    db, handler, game, session, pl_t, ai_t, bstate,
                    "CardWouldDealDamageEvent", dealer,
                    source_owner_uid=int(dealer_owner or 0),
                    extra_target=uid_i,
                    event_tac={"damage": int(amount),
                               "is_combat_damage": int(
                                   bool((bstate or {}).get("combat_damage")))})
            finally:
                bstate.pop("_resolving_would_deal", None)
            if replaced:
                return "replaced"

    if resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                        "CardWouldBeDamagedEvent", uid_i,
                        source_owner_uid=row[1] if row else 0):
        return "replaced"

    dealer_uid = (bstate or {}).get("resolving_source_uid")
    amount = _consume_damage_shields(
        db, session, bstate, uid_i,
        int(dealer_uid) if dealer_uid is not None else None,
        amount, bool((bstate or {}).get("combat_damage")))
    if amount <= 0:
        return "prevented by damage shield"
    if _rule_prevents_damage(
            db, session, bstate, uid_i,
            int(dealer) if dealer is not None else None,
            bool((bstate or {}).get("combat_damage"))):
        return "prevented by damage immunity"

    from ..statics import controller_flags, effective_stats
    if row[0] != "Champion":
        _atk, _def, _attrs, flags, _rage = effective_stats(
            db, session.session_id, bstate, uid_i)
        if "prevent_noncombat_damage" in flags:
            return "prevented"
    dealer = (bstate or {}).get("resolving_source_uid")
    if dealer is not None:
        from pvp_db import db_card_owner_id
        dealer_owner = db_card_owner_id(session.session_id, int(dealer), conn=db)
        if dealer_owner is not None:
            _atk2, _def2, _attrs2, flags, _rage = effective_stats(
                db, session.session_id, bstate, int(dealer))
            controller = controller_flags(
                db, session.session_id, bstate, dealer_owner)
            is_combat = bool((bstate or {}).get("combat_damage"))
            if ("double_damage" in flags or
                    (is_combat and "double_combat_damage" in flags) or
                    (not is_combat and "double_noncombat_damage" in flags) or
                    "double_damage" in controller or
                    (is_combat and "double_combat_damage" in controller) or
                    (not is_combat and "double_noncombat_damage" in controller)):
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
    from pvp_db import db_add_card_damage, db_card_state_value
    db_add_card_damage(session.session_id, uid_i, amount, conn=db)
    db.commit()
    if (bstate or {}).get("resolving_ability"):
        bstate["_ability_damage_dealt"] = int(
            bstate.get("_ability_damage_dealt", 0) or 0) + max(0, amount)
    crow = db_card_state_value(session.session_id, uid_i, conn=db)
    from ..bom import _push_card_state
    _push_card_state(game, session, db, handler, pl_t, ai_t, uid_i,
                     int(crow or 0), bstate=bstate)
    if remaining_defense - amount <= 0:
        kill_troop(game, session, db, handler, pl_t, ai_t, uid_i, bstate,
                   cause="damage")
        return "killed"
    return "survives"
