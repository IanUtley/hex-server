"""Native combat legality predicates for RulesPort."""

from __future__ import annotations

import json
import game_engine


def player_has_eligible_attackers(db, session_id, battle_state, player_id):
    """Return whether ``player_id`` controls a troop that may attack.

    This is the phase-construction predicate shared by Practice/PvE and
    tournament PvP.  Keeping it beside the port's card-combat predicates
    prevents a mode from importing the legacy AI decision helper merely to
    decide whether the combat phases exist.
    """
    from pvp_db import db_warzone_troop_attributes
    from .static_rules import effective_attributes
    rows = db_warzone_troop_attributes(
        session_id, int(player_id), conn=db)
    for uid, state, card_attrs, temporary_attrs, template_attrs, _abilities in rows:
        card_state = int(state or 0)
        if card_state & int(game_engine.ECardStates.Tapped):
            continue
        attrs = (int(card_attrs or 0) | int(temporary_attrs or 0) |
                 int(template_attrs or 0))
        attrs |= int(effective_attributes(
            db, session_id, battle_state or {}, int(uid)) or 0)
        if attrs & int(game_engine.ECardAttributes.CantAttack |
                       game_engine.ECardAttributes.Defensive):
            continue
        if (card_state & int(game_engine.ECardStates.StartedATurnOnYourSide)
                or attrs & int(game_engine.ECardAttributes.Speed)):
            return True
    return False


def _combat_card(db, session_id, battle_state, uid):
    from pvp_db import db_card_mutation_info
    info = db_card_mutation_info(session_id, int(uid), conn=db)
    if not info:
        return None, 0, 0
    from .static_rules import effective_stats
    attack, defense, attrs, flags, damage = effective_stats(
        db, session_id, battle_state or {}, int(uid))
    return {"card_uid": int(uid), "card_type": info[2] or "",
            "location": info[1] or "warzone", "user_id": info[0],
            "owner_id": info[0], "controller_id": info[0],
            "attack": attack, "defense": defense,
            "attributes": attrs, "rule_flags": flags}, attrs, int(damage or 0)


def _rules(db, session_id, battle_state, uid):
    from pvp_db import db_card_mutation_field
    values = []
    for column in ("permanent_buffs", "temporary_buffs"):
        try:
            data = json.loads(db_card_mutation_field(
                session_id, int(uid), column, conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            values.extend(rule for rule in data.get("rule_modifiers", [])
                          if isinstance(rule, dict))
    from .static_rules import rule_modifiers
    values.extend(rule_modifiers(
        db, session_id, battle_state or {}, int(uid)))
    return values


def card_int_attr(db, session_id, uid, name):
    """Sum a dynamic int-attribute across a card's permanent/temporary buffs."""
    from pvp_db import db_card_mutation_field
    total = 0
    for column in ("permanent_buffs", "temporary_buffs"):
        try:
            data = json.loads(db_card_mutation_field(
                session_id, int(uid), column, conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            attrs = data.get("int_attrs")
            if isinstance(attrs, dict):
                total += int(attrs.get(name, attrs.get(name.lower(), 0)) or 0)
    return total


def can_block(db, session_id, battle_state, attacker_uid, blocker_uid):
    """Evaluate the client CanBlock baseline and typed block restrictions."""
    attacker, attacker_attrs, _ = _combat_card(
        db, session_id, battle_state, attacker_uid)
    blocker, blocker_attrs, blocker_damage = _combat_card(
        db, session_id, battle_state, blocker_uid)
    if not attacker or not blocker or "Troop" not in str(blocker["card_type"]):
        return False
    from pvp_db import db_card_state_value
    if int(db_card_state_value(session_id, int(blocker_uid), conn=db) or 0) & int(
            game_engine.ECardStates.Tapped):
        return False
    if blocker_attrs & int(game_engine.ECardAttributes.CantBlock):
        return False
    if attacker_attrs & int(game_engine.ECardAttributes.CantBeBlocked):
        return False
    if attacker_attrs & int(game_engine.ECardAttributes.Flight) and not (
            blocker_attrs & int(game_engine.ECardAttributes.Flight | game_engine.ECardAttributes.SkyGuard)):
        return False

    from pvp_db import db_card_mutation_field
    from rules_port.filters import records_filter_matches
    for rule in _rules(db, session_id, battle_state, attacker_uid):
        if rule.get("property") not in {"blockimmunity", "blockimmunityexception"}:
            continue
        spec = rule.get("filter") or rule.get("cardfilter")
        if not spec:
            continue
        matched = records_filter_matches(
            blocker, spec, source=attacker, context=battle_state or {})
        if rule.get("property") == "blockimmunity" and matched:
            return False
        if rule.get("property") == "blockimmunityexception" and not matched:
            return False
    for rule in _rules(db, session_id, battle_state, blocker_uid):
        if rule.get("property") != "blockrestriction":
            continue
        spec = rule.get("filter") or rule.get("cardfilter")
        if spec and records_filter_matches(
                attacker, spec, source=blocker, context=battle_state or {}):
            return False
    return True
