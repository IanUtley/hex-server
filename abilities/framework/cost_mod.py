"""Dynamic cost modifiers, data-driven from gamedata ability variables.

When a CardCreatedEvent trigger applies a permanent cost reduction whose size
depends on a CardCountAbilityVariable (e.g. Pterobot: "cost -1 for each Dwarf
and/or Robot you control"), the static leaf amount is 0 and the real value
comes from the ability's m_Variables tree:

    CardCountAbilityVariable  — count cards in m_CollectionFlags matching
                                m_CardFilter (controlled by the source)
    ExpressionAbilityVariable — "DwarfAndOrRobotYouControl * -1" → multiplier

We store the parsed formula on the card's game_cards.cost_mod_json and
evaluate it on demand, so the displayed cost and the charged cost stay
current as the board changes (matching the client's recalculated variables).
"""

import json
import re

from .targeting import (
    ZONE_MAP,
    evaluate_card_filter,
    shards_from_threshold,
)


def formula_from_raw(raw_json):
    """Parse a CardModifier cardcost ability's m_Variables into a formula
    entry dict, or None when the modifier is static (no count variable)."""
    if not raw_json:
        return None
    try:
        rec = json.loads(raw_json)
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None
    count_var = None
    for var in rec.get("m_Variables") or []:
        if str(var.get("_t", "")).split(".")[-1] == "CardCountAbilityVariable":
            count_var = var
            break
    if not count_var:
        return None
    count_name = count_var.get("m_Name", "")
    expr_text = None
    for var in rec.get("m_Variables") or []:
        if str(var.get("_t", "")).split(".")[-1] == "ExpressionAbilityVariable":
            et = var.get("m_ExpressionText") or ""
            if count_name and count_name in et:
                expr_text = et
                break
    mult = 1
    if expr_text:
        flat = expr_text.replace(" ", "")
        m = re.search(r'([A-Za-z_]\w*)\s*\*\s*(-?\d+)', flat)
        if m and m.group(1) == count_name:
            mult = int(m.group(2))
    return {
        "zones": [z for z in
                  (count_var.get("m_CollectionFlags") or "").split("|") if z],
        "filter": count_var.get("m_CardFilter") or {},
        "multiplier": mult,
    }


def _card_dict(row):
    return {
        "card_uid": int(row[0]), "card_type": row[1],
        "location": row[2], "user_id": row[3],
        "state": int(row[4] or 0), "attack": row[5],
        "defense": row[6], "name": row[7] or "",
        "cost": row[8] or 0, "subtype": row[9] or "",
        "shards": shards_from_threshold(row[10]),
        "damaged_opponent_this_turn": [],
        "src_owner_side": "player" if (row[3] or 0) else "ai",
    }


def _cards_in_zones(db, session_id, owner_uid, zones):
    from pvp_db import db_condition_cards_in_zones
    rows = db_condition_cards_in_zones(
        session_id, zones, user_id=owner_uid, conn=db)
    return [_card_dict(r) for r in rows]


def cost_mod_delta(db, session_id, card_uid, cost_mod_json):
    """Evaluate the card's stored formula entries against the current board."""
    try:
        entries = json.loads(cost_mod_json or "[]")
    except Exception:
        return 0
    if not entries:
        return 0
    from pvp_db import db_card_owner_id
    owner = db_card_owner_id(session_id, int(card_uid), conn=db) or 0
    total = 0
    for entry in entries:
        zones = [ZONE_MAP.get(z, z.lower()) for z in entry.get("zones") or []]
        if not zones:
            continue
        count = 0
        for card in _cards_in_zones(db, session_id, owner, zones):
            card["src_owner_id"] = owner
            if evaluate_card_filter(card, entry.get("filter") or {},
                                    int(card_uid)):
                count += 1
        total += count * int(entry.get("multiplier", 1))
    return total


def dynamic_cost_mod_delta(db, session_id, card_uid):
    """Evaluate dynamic card-cost abilities that apply in every zone.

    Pterobot's metadata uses CardCreatedEvent plus a CardCountAbilityVariable,
    but its text says the cost reduction applies in all zones.  Existing cards
    in a deck or hand do not receive a CardCreatedEvent during the match, so
    derive the formula from the card's own current ability list when the
    displayed/charged cost is requested.
    """
    from pvp_db import (db_card_source_info, db_card_ability_payload,
                        db_template_ability_payload, db_any_ability_raw_json,
                        db_ability_effect_type_params)
    source = db_card_source_info(session_id, int(card_uid), conn=db)
    payload = db_card_ability_payload(session_id, int(card_uid), conn=db)
    row = (source[3], payload, source[0]) if source else None
    if not row:
        return 0
    owner, abilities_json, template_guid = row
    try:
        abilities = json.loads(abilities_json or "[]")
    except Exception:
        abilities = []
    if not abilities:
        try:
            abilities = json.loads(
                db_template_ability_payload(template_guid, conn=db) or "[]")
        except Exception:
            abilities = []
    entries = []
    for ability_guid in abilities:
        raw_json = db_any_ability_raw_json(ability_guid, conn=db)
        if not raw_json:
            continue
        formula = formula_from_raw(raw_json)
        if not formula:
            continue
        effect = db_ability_effect_type_params(ability_guid, conn=db)
        for effect_type, param in effect:
            if effect_type != "CardModifierAbilityEffectTemplate":
                continue
            try:
                modifier = json.loads(param or "{}")
            except Exception:
                modifier = {}
            if modifier.get("property") == "cardcost" and not int(
                    modifier.get("amount") or 0):
                entries.append(formula)
                break
    if not entries:
        return 0
    return cost_mod_delta(db, session_id, int(card_uid), json.dumps(entries))
