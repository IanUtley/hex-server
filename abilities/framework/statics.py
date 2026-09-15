"""Continuous static-ability evaluation — the WhileCardInPlay / Permanent layer.

The client's AbilityManager applies automatic abilities with
``m_EffectDuration`` WhileCardInPlay / Permanent continuously: dynamic stat
bonuses ("+2/+2 for each card in your hand"), auras ("Troops you control have
+2/+2"), zone-wide cost modifiers ("Your artifacts in all zones have cost -1")
and combat semantics (prevent damage, double damage, Rage).  This module
computes those deltas on demand from the gamedata BOM + raw_json variables so
display and resolution always reflect the current board — no per-card logic.

Value rule (mirrors the client's ability variables):
  * a CardModifier with amount != 0 → amount x variable (count / health)
  * amount == 0 → the variable value itself (sum / expression result)
"""

import json
import ast
import math
import re
import sqlite3

import game_engine

from .targeting import (
    ZONE_MAP,
    evaluate_card_filter,
    shards_from_threshold,
)
from ._shared import attribute_bits_from_flags, attribute_bits_from_text
from .effects.counters import card_counters_full
from .condition_engine import ConditionContext, evaluate_effect_condition


def _side_of(user_id):
    return "ai" if not user_id else "player"


def _opponent_id(db, session_id, owner):
    from pvp_db import db_session_user_ids
    for (r,) in db_session_user_ids(session_id, conn=db):
        if r != owner:
            return r
    return 0


def _card_dict(row):
    return {
        "card_uid": int(row[0]), "card_type": row[1],
        "location": row[2], "user_id": row[3],
        "state": int(row[4] or 0), "attack": row[5],
        "defense": row[6], "name": row[7] or "",
        "cost": row[8] or 0, "subtype": row[9] or "",
        "shards": shards_from_threshold(row[10]),
        "attributes": int(row[11] or 0) | int(row[12] or 0),
        "damaged_opponent_this_turn": [],
        "src_owner_side": "player" if (row[3] or 0) else "ai",
    }


def _cards_in_zones(db, session_id, user_id, zones, bstate=None,
                    include_champions=False):
    from pvp_db import db_static_card_rows, db_card_mutation_field
    rows = db_static_card_rows(session_id, zones, conn=db)
    if user_id is not None:
        rows = [r for r in rows if r[3] == user_id]
    out = [_card_dict(r) for r in rows]
    # CardThresholdModifier/SubTypeModifier alter the instance context rather
    # than the immutable card template.  Static target filters must see those
    # current values in both the display and combat paths.
    for card in out:
        try:
            buffs_value = db_card_mutation_field(
                session_id, card["card_uid"], "permanent_buffs", conn=db)
            buffs = json.loads(buffs_value or "{}") if buffs_value else {}
            if isinstance(buffs, dict):
                if isinstance(buffs.get("subtype"), str):
                    card["subtype"] = buffs["subtype"]
                if isinstance(buffs.get("thresholds"), list):
                    card["shards"] = [int(value) for value in
                                      buffs["thresholds"]]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if include_champions and bstate is not None:
        players = {}
        from pvp_db import db_session_user_ids
        for (uid,) in db_session_user_ids(session_id, conn=db):
            players[uid] = _opponent_id(db, session_id, uid)
        champs = [
            (0, "ai", bstate.get("ai_health", 20)),
        ]
        human = [uid for uid in players if uid != 0]
        if human:
            champs.append((human[0], "player",
                           bstate.get("player_health", 20)))
        for c_uid, side, hp in champs:
            out.append({
                "card_uid": -1 if side == "ai" else -2,
                "card_type": "Champion",
                "location": "warzone",
                "user_id": c_uid,
                "state": 0, "attack": 0, "defense": hp,
                "name": "Champion", "cost": 0, "subtype": "",
                "shards": [], "attributes": 0,
                "damaged_opponent_this_turn": [],
                "src_owner_side": side,
            })
    return out


def _target_owner(db, session_id, owner, player_filter):
    pf = (player_filter or "Self")
    if pf == "Opposing":
        return _opponent_id(db, session_id, owner)
    return owner


def _card_property_value(db, session_id, card_uid, prop, bstate=None):
    """Read a typed current-card property for an ability variable."""
    from pvp_db import db_card_property_state
    row = db_card_property_state(session_id, int(card_uid), conn=db)
    if not row:
        return None
    if prop == "ResourceCostTrue":
        # A static modifier can use the source card's cost as its variable
        # (for example, ``this gets +ATK/+DEF equal to its cost``).  The
        # normal effective-cost path includes continuous static deltas, which
        # evaluates the same CardPropertyVariable again.  Break only that
        # re-entrant evaluation and retain the authoritative base/instance
        # cost; the outer call still includes all non-recursive cost auras.
        state = bstate if isinstance(bstate, dict) else None
        stack = state.setdefault("_card_property_cost_stack", []) if state is not None else []
        uid = int(card_uid)
        from pvp_db import db_card_cost_location_state
        row_cost = db_card_cost_location_state(session_id, uid, conn=db)
        if uid in stack:
            return max(0, int(row_cost[0] or 0) + int(row_cost[1] or 0)) if row_cost else 0
        stack.append(uid)
        try:
            return effective_cost(db, session_id, state or {}, uid)
        finally:
            stack.pop()
            if state is not None and not stack:
                state.pop("_card_property_cost_stack", None)
    if prop not in ("CurrentAttackValue", "CurrentDefenseValue"):
        return None
    value = int(row[0 if prop == "CurrentAttackValue" else 1] or 0)
    value += int(row[2 if prop == "CurrentAttackValue" else 3] or 0)
    key = "atk" if prop == "CurrentAttackValue" else "def"
    for raw_buffs in row[4:6]:
        try:
            buffs = json.loads(raw_buffs or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        value += int(buffs.get(key, 0) or 0)
    return value


def _evaluate_numeric_expression(expression_text, resolve_name):
    """Evaluate the numeric expression grammar used by the client data.

    The original client uses NCalc for these values.  We intentionally expose
    only numeric literals, named variables, unary +/- and arithmetic operators
    here; expressions are data, not executable Python.  The extracted Records
    currently use +, -, *, and /.

    NCalc returns a numeric value and ``ExpressionAbilityVariable`` converts
    it to an integer, so division is evaluated as a float and truncated toward
    zero at the end, matching the client's cast behavior.
    """
    try:
        tree = ast.parse(str(expression_text or ""), mode="eval")
    except (SyntaxError, ValueError, TypeError):
        return None

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(
                node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            value = resolve_name(node.id)
            if value is None:
                raise ValueError("unknown expression variable")
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(
                node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left = visit(node.left)
            right = visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise ValueError("division by zero")
            return left / right
        raise ValueError("unsupported expression syntax")

    try:
        value = visit(tree)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if not math.isfinite(float(value)):
            return None
        return int(value)
    except (ArithmeticError, TypeError, ValueError, OverflowError):
        return None


def _variable_value(db, session_id, bstate, raw, var_name, owner, source_uid,
                    stat_prop=None, _expression_stack=None):
    """Compute one named ability variable from raw_json m_Variables."""
    if not raw or not var_name:
        return None
    try:
        rec = json.loads(raw)
    except Exception:
        return None
    for var in rec.get("m_Variables") or []:
        if var.get("m_Name") != var_name:
            continue
        t = str(var.get("_t", "")).split(".")[-1]
        if t == "CardCountAbilityVariable":
            return _count_variable(db, session_id, bstate, owner, var,
                                   source_uid)
        if t == "CountListAttrAbilityVariable":
            # Payment targets such as m_ExhaustTarget populate the named
            # list before the BOM resolves. Count that selection directly;
            # looking at card state would include cards tapped earlier.
            ability_lists = (bstate or {}).get("ability_lists") or {}
            # Gamedata distinguishes the variable identifier from the list
            # attribute it counts.  For example, Construction Plans names
            # the variable AForEachTroopExhaustedThisWay but populates the
            # ExhaustedCards list during activation.
            list_name = var.get("m_ListAttrName") or var_name
            values = ability_lists.get(list_name)
            if values is None:
                # Retain compatibility with callers that keyed the list by
                # variable name rather than by its extracted list attribute.
                values = ability_lists.get(var_name)
            if values is not None:
                return len(values)
            return int(var.get("m_DefaultValue", 0) or 0)
        if t == "CounterVariable":
            return _counter_variable(db, session_id, bstate, owner, var,
                                     source_uid)
        if t == "CardPropertyVariable":
            prop = var.get("m_Property") or ""
            value = _card_property_value(
                db, session_id, source_uid, prop, bstate=bstate)
            if value is not None:
                return value
            return int(var.get("m_DefaultValue", 0) or 0)
        if t == "SumVariableInListAttrCardsAbilityVariable":
            return _sum_list_attr_variable(db, session_id, bstate, owner,
                                            var, source_uid)
        if t in ("TriggerTargetPropertyVariable",
                 "TriggerSourcePropertyVariable"):
            target_uid = ((bstate or {}).get("resolving_trigger_target_uid")
                          or (bstate or {}).get("resolving_target_uid"))
            if target_uid is None:
                return int(var.get("m_DefaultValue", 0) or 0)
            prop = var.get("m_Property") or ""
            if prop == "ResourceCostTrue":
                return effective_cost(db, session_id, bstate,
                                      int(target_uid))
            named = (bstate or {}).get("ability_variables") or {}
            return int(named.get(var.get("m_Name"),
                                 var.get("m_DefaultValue", 0)) or 0)
        if t == "CardSumAbilityVariable":
            return _sum_variable(db, session_id, bstate, owner, var,
                                 source_uid, stat_prop)
        if t == "IntAttrAbilityVariable":
            attr_name = str(var.get("m_IntAttrName") or var_name)
            if attr_name == "DamageDealt":
                # DamageModifier records the amount dealt on the active
                # AbilityInstance.  This is the server-side equivalent of
                # the client's AbilityInstance TAC attribute and is scoped
                # by resolve_ability for each activation.
                return int((bstate or {}).get("_ability_damage_dealt", 0) or 0)
            named = (bstate or {}).get("ability_variables") or {}
            return int(named.get(var_name,
                                 var.get("m_DefaultValue", 0)) or 0)
        if t == "SourcePlayerHealthVariable":
            key = "player_health" if owner else "ai_health"
            return int(bstate.get(key, 0) or 0)
        if t == "SourcePlayerThresholdAbilityVariable":
            color = (var.get("m_Threshold") or "").lower()
            flag = game_engine.SHARD_TO_FLAG.get(color, 0)
            key = "player_threshold" if owner else "ai_threshold"
            return int((bstate.get(key) or {}).get(flag, 0) or 0)
        if t == "AbilityPropertyVariable":
            prop = var.get("m_Property") or ""
            if prop == "AbilityResourceXCost":
                # For a variable-cost ability, X is the value selected in the
                # activation dialog. Spell resolution carries it in bstate;
                # without an active activation the current client contract is
                # zero, not the card's effective resource cost.
                return int((bstate or {}).get("x_cost", 0) or 0)
            named = (bstate or {}).get("ability_variables") or {}
            return int(named.get(var.get("m_Name"), 0) or 0)
        if t == "SourcePlayerBriarLegionVariable":
            # The variable is defined by the card's typed metadata, but the
            # match-wide event counter is shared by both controllers.
            return int(bstate.get("briar_legions_entered", 0) or 0)
        if t == "AbilityConstant":
            try:
                return int(var.get("m_DefaultValue", 0) or 0)
            except Exception:
                return 0
        if t == "ExpressionAbilityVariable":
            stack = set(_expression_stack or ())
            if var_name in stack:
                return None
            stack.add(var_name)

            def resolve_name(name):
                value = _variable_value(
                    db, session_id, bstate, raw, name, owner, source_uid,
                    stat_prop, _expression_stack=stack)
                if value is None:
                    value = _constant_value(raw, name)
                if value is None and name == "ESC":
                    # The client's ESC variable is SourceCard.EscalationCount,
                    # which starts at 1 and increments with each Escalate.
                    side = ("ai" if owner == 0 else "player")
                    value = int(bstate.get(f"{side}_escalation_uses", 0)) + 1
                return value

            return _evaluate_numeric_expression(
                var.get("m_ExpressionText"), resolve_name)
    return None


def ability_variable_value(db, session_id, bstate, ability_guid, var_name,
                           owner, source_uid, stat_prop=None):
    """Evaluate one metadata-defined ability variable in the live state.

    Effect leaves normally receive the ability's constant defaults in
    ``bstate['ability_variables']``.  Variables such as ``CounterVariable``
    are expressions over the current game state, however, and must be
    recalculated after earlier effect groups have changed that state.
    """
    if not ability_guid or not var_name:
        return None
    raw = None
    from pvp_db import db_any_ability_raw_json
    raw = db_any_ability_raw_json(ability_guid, conn=db)
    if raw is None:
        from .fields import _raw_ability
        raw = json.dumps(_raw_ability(db, ability_guid))
    return _variable_value(db, session_id, bstate, raw, var_name, owner,
                           source_uid, stat_prop=stat_prop)


def _constant_value(raw, name):
    try:
        rec = json.loads(raw)
    except Exception:
        return None
    for var in rec.get("m_Variables") or []:
        if var.get("m_Name") == name and \
                str(var.get("_t", "")).split(".")[-1] == "AbilityConstant":
            try:
                return int(var.get("m_DefaultValue", 0) or 0)
            except Exception:
                return 0
    return None


def _count_variable(db, session_id, bstate, owner, var, source_uid):
    zones = [ZONE_MAP.get(z, z.lower())
             for z in (var.get("m_CollectionFlags") or "").split("|") if z]
    if not zones:
        return 0
    pf = var.get("m_PlayerFilter") or "Self"
    target = None if pf == "MultiplePlayers" else _target_owner(
        db, session_id, owner, pf)
    f = var.get("m_CardFilter") or {}
    n = 0
    # IsControlledBy compares each candidate's owner to the ability SOURCE's
    # side — the card dicts must carry the source's side, not their own
    # (otherwise the filter is a tautology and "your hand" counts both sides).
    src_side = _side_of(owner)
    for card in _cards_in_zones(db, session_id, target, zones, bstate,
                                include_champions=True):
        card["src_owner_side"] = src_side
        # In PvP both players have non-zero ids.  IsControlledBy must compare
        # the candidate's real owner with the ability source's owner; the
        # player/AI side fallback would incorrectly treat both PvP players as
        # the same side.  MultiplePlayers deliberately gathers both owners,
        # then this source-owner field applies the metadata filter.
        card["src_owner_id"] = owner
        if evaluate_card_filter(card, f, source_uid):
            n += 1
    return n


def _counter_variable(db, session_id, bstate, owner, var, source_uid):
    """Sum a gamedata CounterVariable over its filtered card collection.

    CounterVariable is distinct from CardCountAbilityVariable: the former
    counts counter instances on matching cards, not matching cards.  Counter
    values are persisted with their gamedata counter-template GUID, so this
    remains independent of the ability's display text.
    """
    zones = [ZONE_MAP.get(z, z.lower())
             for z in (var.get("m_CollectionFlags") or "").split("|") if z]
    if not zones:
        return 0
    pf = var.get("m_PlayerFilter") or "Self"
    target = None if pf == "MultiplePlayers" else _target_owner(
        db, session_id, owner, pf)
    f = var.get("m_CardFilter") or {}
    counter_guid = ((var.get("m_CardCounterTemplateId") or {}).get("m_Guid")
                    or "").lower()
    total = 0
    src_side = _side_of(owner)
    for card in _cards_in_zones(db, session_id, target, zones, bstate,
                                include_champions=True):
        card["src_owner_side"] = src_side
        card["src_owner_id"] = owner
        if not evaluate_card_filter(card, f, source_uid):
            continue
        counts, guids = card_counters_full(
            db, session_id, int(card["card_uid"]))
        for name, count in counts.items():
            if counter_guid and str(guids.get(name, "")).lower() != counter_guid:
                continue
            total += int(count or 0)
    return total


def _sum_list_attr_variable(db, session_id, bstate, owner, var, source_uid):
    """Sum a property over cards captured in an AbilityInstance list.

    The client keeps lists such as ``VoidedCards`` and ``DiscardedCards`` on
    the active ability instance.  These are not the same as a query over the
    cards' current zone: the cards may already have moved to Void or Discard
    by the time a later effect evaluates the variable.
    """
    list_name = var.get("m_ListAttrName") or var.get("m_Name")
    ability_lists = (bstate or {}).get("ability_lists") or {}
    values = ability_lists.get(list_name)
    if values is None:
        values = ability_lists.get(var.get("m_Name"))

    prop = str(var.get("m_Property") or "")
    if values is None:
        # Direct/unit-test callers may provide the already resolved aggregate
        # rather than the client-style list.  The production path populates
        # VoidedCards below, but retaining this aggregate keeps the resolver
        # compatible with those callers without using card names or text.
        cached = (bstate or {}).get("champion_voided_stats") or {}
        if prop == "CurrentAttackValue" and "atk" in cached:
            return int(cached.get("atk") or 0)
        if prop == "CurrentDefenseValue" and "def" in cached:
            return int(cached.get("def") or 0)
        return int(var.get("m_DefaultValue", 0) or 0)

    total = 0
    card_filter = var.get("m_CardFilter") or {}
    for value in values if isinstance(values, (list, tuple, set)) else []:
        try:
            card_uid = int(value)
        except (TypeError, ValueError):
            continue
        from pvp_db import db_card_list_stat_row
        row = db_card_list_stat_row(session_id, card_uid, conn=db)
        if not row:
            continue
        card = _card_dict(row[:13])
        if card_filter and not evaluate_card_filter(card, card_filter,
                                                    source_uid):
            continue
        if prop == "CurrentAttackValue":
            value_now = int(row[5] or 0) + int(row[13] or 0)
            buff_columns = (row[15], row[16])
        elif prop == "CurrentDefenseValue":
            value_now = int(row[6] or 0) + int(row[14] or 0)
            buff_columns = (row[15], row[16])
        elif prop == "ResourceCostTrue":
            value_now = effective_cost(db, session_id, bstate, card_uid)
            buff_columns = ()
        else:
            # Unknown typed properties must not be guessed from display text.
            continue
        for raw_buffs in buff_columns:
            try:
                buffs = json.loads(raw_buffs or "{}")
                if prop == "CurrentAttackValue":
                    value_now += int(buffs.get("atk", 0) or 0)
                elif prop == "CurrentDefenseValue":
                    value_now += int(buffs.get("def", 0) or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        total += value_now
    return total


def _sum_variable(db, session_id, bstate, owner, var, source_uid, stat_prop):
    zones = [ZONE_MAP.get(z, z.lower())
             for z in (var.get("m_CollectionFlags") or "").split("|") if z]
    if not zones:
        return 0
    pf = var.get("m_PlayerFilter") or "Self"
    target = None if pf == "MultiplePlayers" else _target_owner(
        db, session_id, owner, pf)
    f = var.get("m_CardFilter") or {}
    prop = stat_prop or "defense"
    total = 0
    src_side = _side_of(owner)
    for card in _cards_in_zones(db, session_id, target, zones, bstate,
                                include_champions=True):
        card["src_owner_side"] = src_side
        if evaluate_card_filter(card, f, source_uid):
            total += int(card.get(prop, 0) or 0)
    return total


def _gate_condition(db, session_id, bstate, condition_id, source_uid, owner):
    if not condition_id:
        return True
    ctx = ConditionContext(db, _SessionStub(session_id), bstate,
                           ability_source_uid=int(source_uid),
                           ability_source_owner_id=owner)
    return evaluate_effect_condition(db, condition_id, ctx)


class _SessionStub:
    """Minimal session object for ConditionContext (only session_id is used
    by the condition engine's DB queries)."""

    def __init__(self, session_id):
        self.session_id = session_id


def _leaf_numeric_value(db, session_id, bstate, param, raw, owner, source_uid,
                        prop):
    """Numeric value of an attack/defense/cardcost leaf."""
    amount = int(param.get("amount") or 0)
    try:
        rec = json.loads(raw) if raw else {}
    except Exception:
        rec = {}
    variables = (rec.get("m_Variables") or []) if isinstance(rec, dict) else []
    seen = set()
    count_names = []
    for var in variables:
        if str(var.get("_t", "")).split(".")[-1] in (
                "CardCountAbilityVariable", "CountListAttrAbilityVariable",
                "CounterVariable",
                "TriggerTargetPropertyVariable",
                "TriggerSourcePropertyVariable",
                "SourcePlayerHealthVariable",
                "SourcePlayerThresholdAbilityVariable",
                "SourcePlayerBriarLegionVariable",
                "AbilityPropertyVariable", "CardPropertyVariable"):
            n = var.get("m_Name")
            if n and n not in seen:
                seen.add(n)
                count_names.append(n)
    if amount == 0:
        # Typed NumericModifier fields carry the authoritative variable name
        # in the effect metadata. Several abilities have more than one
        # AbilityConstant (Strength of the Redwood is P1=1 and P3=3); falling
        # through to the first constant makes every typed stat modifier use
        # the first value, turning +1/+3 into +1/+1.
        input_variable = str(param.get("input_variable") or "")
        if input_variable:
            value = _variable_value(
                db, session_id, bstate, raw, input_variable, owner,
                source_uid, stat_prop=prop)
            if value is not None:
                return value
        # Dynamic: expression / sum / health / count variable directly.
        for var in variables:
            t = str(var.get("_t", "")).split(".")[-1]
            name = var.get("m_Name")
            if t in ("ExpressionAbilityVariable", "CardSumAbilityVariable",
                     "CounterVariable", "TriggerTargetPropertyVariable",
                     "TriggerSourcePropertyVariable",
                     "SourcePlayerHealthVariable",
                     "SourcePlayerThresholdAbilityVariable",
                     "SourcePlayerBriarLegionVariable",
                     "AbilityPropertyVariable", "CardPropertyVariable") and \
                    name not in seen:
                v = _variable_value(db, session_id, bstate, raw, name, owner,
                                    source_uid, stat_prop=prop)
                if v is not None:
                    return v
        for name in count_names:
            v = _variable_value(db, session_id, bstate, raw, name, owner,
                                source_uid, stat_prop=prop)
            if v is not None:
                return v
        # Typed modifiers commonly feed a literal through an AbilityConstant
        # (for example ChargePointsModifier's input variable ``A``).  The
        # extracted parent param is amount=0 in that form, but zero is not the
        # operation's value; it means "read the input variable".
        for var in variables:
            if str(var.get("_t", "")).split(".")[-1] == "AbilityConstant":
                try:
                    return int(var.get("m_DefaultValue", 0) or 0)
                except (TypeError, ValueError):
                    return 0
        return 0
    # Static amount, possibly scaled by a count/health variable.
    for name in count_names:
        v = _variable_value(db, session_id, bstate, raw, name, owner,
                            source_uid, stat_prop=prop)
        if v is not None:
            return amount * v
    return amount


def _flag_from_text(text):
    """Combat/rule flags encoded in an intattr / special leaf's game text."""
    low = (text or "").lower()
    flags = set()
    if "prevent all combat damage" in low:
        flags.add("prevent_combat_damage")
    if "prevent all non-combat damage" in low or \
            ("prevent all damage" in low and "combat" not in low):
        flags.add("prevent_noncombat_damage")
    if "prevent all damage" in low and "combat" in low:
        flags.add("prevent_combat_damage")
        flags.add("prevent_noncombat_damage")
    if "can't gain health" in low or "cannot gain health" in low:
        flags.add("cant_gain_health")
    if "can't lose health" in low or "cannot lose health" in low:
        flags.add("cant_lose_health")
    if "can't play cards" in low or "cannot play cards" in low:
        flags.add("cant_play_cards")
    if "no maximum hand size" in low:
        flags.add("no_max_hand_size")
    if "double damage" in low:
        flags.add("double_damage")
    if "can't be blocked except" in low or "cannot be blocked except" in low:
        flags.add("unblockable_except")
        if "blood" in low:
            flags.add("unblockable_except_blood")
        if "artifact" in low:
            flags.add("unblockable_except_artifact")
    if "create that many +1 instead" in low or "shin'hare" in low:
        flags.add("shinhare_plus_one")
    m = re.search(r'rage\s+(\d+)', low)
    if m:
        flags.add("rage")
    return flags


def _flag_from_typed_intattr(attribute):
    """Map the client IntAttr enum to the internal rule flags.

    ``m_Attribute`` is the rules value; ``m_GameText`` is only its localized
    presentation.  Keep the text parser as a compatibility path for old
    extracted rows which do not contain the typed field.
    """
    name = str(attribute or "").rsplit(".", 1)[-1].lower()
    return {
        "cantgainhealth": {"cant_gain_health"},
        "cantlosehealth": {"cant_lose_health"},
        "cantplaycards": {"cant_play_cards"},
        "unlimitedhandsize": {"no_max_hand_size"},
        "preventcombatdamage": {"prevent_combat_damage"},
        "preventnoncombatdamage": {"prevent_noncombat_damage"},
        "shinharecreationbonus": {"shinhare_plus_one"},
    }.get(name, set())


def _apply_leaf(db, session_id, bstate, param, raw, owner, source_uid,
                deltas):
    """Fold one CardModifier leaf into a deltas dict."""
    prop = param.get("property")
    text = param.get("text") or ""
    if prop in ("attack", "defense"):
        v = _leaf_numeric_value(db, session_id, bstate, param, raw, owner,
                                source_uid, prop)
        if prop == "attack":
            deltas["atk"] += v
        else:
            deltas["def"] += v
    elif prop == "cardcost":
        v = _leaf_numeric_value(db, session_id, bstate, param, raw, owner,
                                source_uid, "cost")
        deltas["cost_mod"] += v
    elif prop == "attribute":
        flags = param.get("attribute_flags")
        bits = (attribute_bits_from_flags(flags)
                if flags is not None else attribute_bits_from_text(text))
        if bits:
            deltas["attrs"] |= bits
    elif prop == "intattr":
        # Data-driven from the gamedata IntAttrModifier fields
        # (m_Attribute/m_Value), not the effect's game text.
        attr = param.get("attribute") or ""
        typed_flags = _flag_from_typed_intattr(attr)
        deltas["flags"] |= (typed_flags if attr else _flag_from_text(text))
        base = int(param.get("amount") or 0)
        if attr == "Rage" and base > 0:
            if "for each" in (text or "").lower() or \
                    "for every" in (text or "").lower():
                v = _leaf_numeric_value(db, session_id, bstate, param, raw,
                                        owner, source_uid, "attack")
                base = int(v) if v else base
            # Rage values are additive: printed Rage 2 plus a granted Rage 2
            # is Rage 4, and multiple independent grants stack likewise.
            deltas["rage"] = deltas.get("rage", 0) + base
            deltas["attrs"] |= game_engine.ECardAttributes.Rage
        else:
            # Fallback for BOM params without the IntAttrModifier fields:
            # parse the text.
            m = re.search(r'rage\s+(\d+)', (text or "").lower())
            if m:
                base = int(m.group(1))
                if "for each" in (text or "").lower() or \
                        "for every" in (text or "").lower():
                    v = _leaf_numeric_value(db, session_id, bstate, param,
                                            raw, owner, source_uid, "attack")
                    base = int(v) if v else base
                deltas["rage"] = deltas.get("rage", 0) + base
                deltas["attrs"] |= game_engine.ECardAttributes.Rage
    elif prop == "damagemultiplier":
        if param.get("combatdamageonly"):
            deltas["flags"].add("double_combat_damage")
        elif param.get("noncombatdamageonly"):
            deltas["flags"].add("double_noncombat_damage")
        else:
            deltas["flags"].add("double_damage")
    elif prop in ("blockimmunityexception", "blockimmunity", "blockrestriction"):
        deltas["flags"] |= _flag_from_text(text)
    elif prop == "damageimmunity":
        deltas["flags"] |= _flag_from_text(text)
    elif prop == "targetingimmunity":
        deltas["flags"].add("targeting_immunity")


def _static_leaves(db, ability_guid):
    """[(param, raw_json)] for a static ability's CardModifier leaves."""
    from pvp_db import db_ability_raw_json, db_ability_effect_rows
    raw = db_ability_raw_json(ability_guid, conn=db) or ""
    out = []
    for effect_guid, etype, param in db_ability_effect_rows(
            ability_guid, conn=db):
        if etype != "CardModifierAbilityEffectTemplate":
            continue
        try:
            pm = json.loads(param or "{}")
        except Exception:
            continue
        if not isinstance(pm, dict):
            continue
        if pm.get("duration") not in ("WhileCardInPlay", "Permanent",
                                      "BeginningOfOwnersTurn"):
            continue
        # Parent params retain duration/target compatibility data. Modifier
        # operation, attribute flags, and counter identity come from the
        # typed child effect template and are authoritative.
        from .fields import modifier_metadata
        typed = modifier_metadata(effect_guid)
        if typed:
            pm = dict(pm)
            if typed.get("property"):
                pm.setdefault("property", typed["property"])
            if typed.get("attribute"):
                pm["attribute"] = typed["attribute"]
            if typed.get("attributeflags"):
                pm["attribute_flags"] = typed["attributeflags"]
            if typed.get("counter_template_guid"):
                pm["counter_template_guid"] = typed[
                    "counter_template_guid"]
            if typed.get("operation"):
                pm["operation"] = typed["operation"]
            if typed.get("input_variable"):
                pm["input_variable"] = typed["input_variable"]
            if typed.get("input_value") and not pm.get("amount"):
                pm["amount"] = typed["input_value"]
            # m_Value is the literal operand for IntAttrModifier and is
            # distinct from m_InputValue (which is usually a variable).
            if "value" in typed:
                pm["amount"] = typed["value"]
            for key in ("cardfilter", "iscombatdamage", "copysourcecard",
                        "setthresholds", "shard", "subtype"):
                if key in typed:
                    pm[key] = typed[key]
            for key in ("combatdamageonly", "noncombatdamageonly",
                        "replaceexistingvalue"):
                if key in typed:
                    pm[key] = typed[key]
            if "value" in typed:
                pm["amount"] = typed["value"]
        out.append((pm, raw))
    return out


def _card_static_abilities(db, session_id, card_uid):
    """Static ability GUIDs + raw_json for one card instance."""
    from pvp_db import db_card_ability_payload, db_ability_static_metadata
    payload = db_card_ability_payload(session_id, int(card_uid), conn=db)
    if not payload:
        return []
    try:
        ags = [g.lower() for g in json.loads(payload)]
    except Exception:
        return []
    out = []
    for ag in ags:
        m = db_ability_static_metadata(ag, conn=db)
        if not m:
            continue
        # Zone-wide statics (socketed gems' "Rage 1 in all zones") are
        # CardCreatedEvent triggers in the gamedata but behave as continuous
        # statics — their m_TriggerCollectionFlags field lists every zone.
        is_zone_static = False
        raw = m[2] or ""
        if raw and "m_TriggerCollectionFlags" in raw:
            flags = ""
            fm = re.search(r'"m_TriggerCollectionFlags"\s*:\s*"([^"]*)"', raw)
            if fm:
                flags = fm.group(1)
            is_zone_static = all(z in flags for z in
                                  ("Deck", "Hand", "Warzone", "Discard"))
        # A trigger collection covering every zone does not make a triggered
        # ability continuous.  Grave Nibbler's one-shot has all-zone
        # collection flags because it listens while Underground; treating it
        # as static applies its +2/+2 once continuously and again when the
        # death trigger resolves.  Only CardCreatedEvent abilities use this
        # all-zone static convention.
        if ((not m[0] and not m[1]) or
                (is_zone_static and "CardCreatedEvent" in str(m[0]))):
            out.append((ag, raw))
    return out


def rule_modifiers(db, session_id, bstate, card_uid):
    """Return typed rule modifiers currently attached to one card.

    Runtime modifiers are kept in the instance buff JSON.  Continuous card
    abilities are folded from the same Records-backed leaves so combat does
    not depend on localized text or on whether a client refresh happened.
    """
    out = []
    for column in ("permanent_buffs", "temporary_buffs"):
        from pvp_db import db_card_mutation_field
        value = db_card_mutation_field(session_id, int(card_uid), column, conn=db)
        try:
            buffs = json.loads(value or "{}") if value else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if isinstance(buffs, dict):
            out.extend(value for value in buffs.get("rule_modifiers", [])
                       if isinstance(value, dict))
    for ability_guid, _raw in _card_static_abilities(
            db, session_id, int(card_uid)):
        for param, _raw_effect in _static_leaves(db, ability_guid):
            if param.get("property") in (
                    "damageimmunity", "targetingimmunity", "attackimmunity",
                    "blockimmunity", "blockimmunityexception",
                    "blockrestriction", "damagemultiplier"):
                out.append(dict(param))
    return out


def self_deltas(db, session_id, bstate, card_uid):
    """Deltas from the card's own static abilities (self-targeting leaves)."""
    from pvp_db import db_card_owner_location_position
    row = db_card_owner_location_position(session_id, int(card_uid), conn=db)
    if not row:
        return {"atk": 0, "def": 0, "cost_mod": 0, "attrs": 0,
                "flags": set(), "rage": 0}
    owner, loc, _position = row
    if loc != "warzone":
        # WhileCardInPlay statics only apply while the card is in play; zone-
        # wide cost reductions come through the aura pass instead.
        return {"atk": 0, "def": 0, "cost_mod": 0, "attrs": 0,
                "flags": set(), "rage": 0}
    deltas = {"atk": 0, "def": 0, "cost_mod": 0, "attrs": 0,
              "flags": set(), "rage": 0}
    for ag, raw in _card_static_abilities(db, session_id, card_uid):
        tpl_ids = _ability_target_templates(db, ag)
        if tpl_ids:
            kinds = [_target_kind(_target_template(db, t)) for t in tpl_ids]
            if not kinds:
                continue
            if all(k == "aura" for k in kinds):
                # Most aura holders are separate cards and are evaluated by
                # ``aura_deltas`` below.  Encounter scene passives are
                # materialized directly onto each qualifying troop, though
                # (for example, Beast Crossing's Wild-troops-have-Crush
                # ability).  In that case the source card is itself in the
                # aura's target set and must receive the modifier too.
                matched = False
                for tid in tpl_ids:
                    tt = _target_template(db, tid)
                    if not tt:
                        continue
                    zones = [ZONE_MAP.get(z, z.lower())
                             for z in (tt["collection_flags"] or "").split("|") if z]
                    if loc not in zones:
                        continue
                    target_owner = _target_owner(db, session_id, owner,
                                                 tt["player_filter"])
                    if target_owner is not None and owner != target_owner:
                        continue
                    card_rows = _cards_in_zones(
                        db, session_id, target_owner, zones)
                    card = next((c for c in card_rows
                                 if c["card_uid"] == int(card_uid)), None)
                    if card and evaluate_card_filter(card, tt["filter_json"],
                                                      card_uid):
                        matched = True
                        break
                if not matched:
                    continue
        for pm, rawj in _static_leaves(db, ag):
            if not _gate_condition(db, session_id, bstate,
                                   pm.get("condition_id"), card_uid, owner):
                continue
            _apply_leaf(db, session_id, bstate, pm, rawj or raw, owner,
                        card_uid, deltas)
    return deltas


def _target_template(db, template_id):
    from pvp_db import db_static_target_template
    row = db_static_target_template(template_id, conn=db)
    if not row:
        return None
    try:
        f = json.loads(row[2] or "{}")
    except Exception:
        f = {}
    return {"collection_flags": row[0] or "",
            "player_filter": row[1] or "Self",
            "filter_json": f,
            "game_text": row[3] or ""}


def _target_kind(tt):
    """Classify a target template: 'self' (this / #SELF# / You / pets),
    'global' (all champions) or 'aura' (troops you control / other X / ...)."""
    if not tt:
        return "self"
    gt = (tt.get("game_text") or "").lower()
    if "this" in gt or "#self#" in gt or "pets" in gt or gt.strip() == "you":
        return "self"
    if "all champions" in gt:
        return "global"
    return "aura"


def _ability_target_templates(db, ability_guid):
    from pvp_db import db_ability_target_template_ids
    payload = db_ability_target_template_ids(ability_guid, conn=db)
    if not payload:
        return []
    try:
        ids = json.loads(payload)
    except Exception:
        return []
    return [i for i in ids if i]


def aura_deltas(db, session_id, bstate, card_uid):
    """Deltas from other cards the controller controls whose static aura
    targets this card (e.g. Soul Armaments' +2/+2 to troops you control)."""
    from pvp_db import db_card_owner_location_position
    row = db_card_owner_location_position(session_id, int(card_uid), conn=db)
    if not row:
        return {"atk": 0, "def": 0, "cost_mod": 0, "attrs": 0,
                "flags": set(), "rage": 0}
    owner, loc, _position = row
    empty = {"atk": 0, "def": 0, "cost_mod": 0, "attrs": 0,
             "flags": set(), "rage": 0}
    if loc != "warzone":
        # Zone-wide auras can still hit cards outside the warzone (e.g.
        # Technical Genius: "Your artifacts in all zones have cost -1").
        pass
    # Cards that can project an aura: the controller's permanents, including
    # underground cards.  CardCreatedEvent abilities whose trigger collection
    # covers every zone are continuous statics in the client; Subterranean
    # Saboteur is the canonical example (its underground ability changes the
    # effective casting speed of matching cards already in hand).
    from pvp_db import db_cards_in_zones_with_abilities
    holders = [row for row in db_cards_in_zones_with_abilities(
        session_id, owner, ("warzone", "underground"), conn=db)
        if int(row[0]) != int(card_uid)]
    total = dict(empty)
    for src_uid, ab_json in holders:
        try:
            ags = [g.lower() for g in json.loads(ab_json or "[]")]
        except Exception:
            continue
        for ag in ags:
            from pvp_db import db_ability_static_metadata
            m = db_ability_static_metadata(ag, conn=db)
            if not m or m[1]:
                continue
            if m[0]:
                raw = m[2] or ""
                # Only CardCreatedEvent abilities authored for all relevant
                # collections are continuous.  Other triggered abilities
                # must still resolve through the trigger dispatcher.
                if "CardCreatedEvent" not in str(m[0]) or not all(
                        zone in raw for zone in
                        ('Deck', 'Hand', 'Warzone', 'Discard')):
                    continue
            tpl_ids = _ability_target_templates(db, ag)
            if not tpl_ids:
                continue  # self-targeting ability — handled by self_deltas
            for tid in tpl_ids:
                tt = _target_template(db, tid)
                if not tt or _target_kind(tt) != "aura":
                    continue
                zones = [ZONE_MAP.get(z, z.lower())
                         for z in (tt["collection_flags"] or "").split("|") if z]
                target_owner = _target_owner(db, session_id, owner,
                                             tt["player_filter"])
                # Stored names recorded by the source card's abilities (Oath of
                # Valor's HasName-with-UseStoredName aura).
                src_stored = []
                try:
                    for ag2 in json.loads(ab_json or "[]"):
                        src_stored.extend(
                            (bstate or {}).get("stored_names", {}).get(
                                ag2.lower(), []))
                except Exception:
                    pass
                pool = [c for c in _cards_in_zones(db, session_id, target_owner,
                                                   zones)
                        if evaluate_card_filter(c, tt["filter_json"], src_uid,
                                                src_stored)]
                if int(card_uid) not in {c["card_uid"] for c in pool}:
                    continue
                # This card is inside the aura's target pool — apply its leaves.
                raw = m[2] or ""
                for pm, rawj in _static_leaves(db, ag):
                    if not _gate_condition(db, session_id, bstate,
                                           pm.get("condition_id"), src_uid,
                                           owner):
                        continue
                    _apply_leaf(db, session_id, bstate, pm, rawj or raw,
                                owner, src_uid, total)
    return total


def effective_deltas(db, session_id, bstate, card_uid):
    """Combined static deltas for a card (own statics + auras)."""
    own = self_deltas(db, session_id, bstate, card_uid)
    aura = aura_deltas(db, session_id, bstate, card_uid)
    return {
        "atk": own["atk"] + aura["atk"],
        "def": own["def"] + aura["def"],
        "cost_mod": own["cost_mod"] + aura["cost_mod"],
        "attrs": own["attrs"] | aura["attrs"],
        "flags": own["flags"] | aura["flags"],
        "rage": max(own["rage"], aura["rage"]),
    }


def effective_stats(db, session_id, bstate, card_uid):
    """(atk, def_, attrs, flags, rage) for a card including base stats,
    instance modifiers and continuous static abilities — used by combat
    resolution so the fought numbers match the displayed card.

    ``flags`` also carries the card template's base Lethal keyword. Lethal is
    an IntAttr in the client card model rather than an ECardAttributes bit, so
    it must be kept alongside the other combat flags for authoritative
    resolution.
    """
    from pvp_db import db_card_combat_state
    row = db_card_combat_state(session_id, int(card_uid), conn=db)
    if not row:
        return 0, 0, 0, set(), 0
    atk = (row[4] or 0) + (row[0] or 0)
    def_ = (row[5] or 0) + (row[1] or 0)
    dmg = row[2] or 0
    attrs = (row[3] or 0) | (row[6] or 0) | (row[9] or 0)
    instance_rage = 0
    instance_damage_flags = set()
    for buff_col in (row[7], row[8]):
        try:
            buffs = json.loads(buff_col or "{}")
            atk += int(buffs.get("atk", 0) or 0)
            def_ += int(buffs.get("def", 0) or 0)
            instance_rage += int(buffs.get("rage", 0) or 0)
            for rule in buffs.get("rule_modifiers", []) or []:
                if not isinstance(rule, dict):
                    continue
                if rule.get("property") != "damagemultiplier":
                    continue
                if int(rule.get("value", 0) or 0) <= 1:
                    continue
                if rule.get("combatdamageonly"):
                    d_flags = {"double_combat_damage"}
                elif rule.get("noncombatdamageonly"):
                    d_flags = {"double_noncombat_damage"}
                else:
                    d_flags = {"double_damage"}
                # ``d`` is initialized below; retain the temporary flags in
                # the local accumulator until the static deltas are merged.
                instance_damage_flags |= d_flags
        except Exception:
            pass
    d = effective_deltas(db, session_id, bstate, card_uid)
    d["flags"] |= instance_damage_flags
    atk += d["atk"]
    def_ += d["def"]
    attrs |= d["attrs"]
    # The card's printed Rage X (gamedata m_RageValue) stacks with granted
    # Rage (e.g. a socketed gem's "Rage 1 in all zones").  Guarded for DBs /
    # fixtures without the column.
    try:
        from pvp_db import db_card_rage_lethal
        rv_all = db_card_rage_lethal(session_id, int(card_uid), conn=db)
        rv = (rv_all[:1] if rv_all else None)
        if rv and rv[0]:
            d["rage"] += int(rv[0])
    except Exception:
        pass
    if instance_rage:
        d["rage"] += instance_rage
    if d["rage"] > 0:
        attrs |= game_engine.ECardAttributes.Rage
    # The Prevent* attributes imply the same combat semantics as the intattr
    # flags ("Prevent all damage" / combat-only / non-combat-only).
    if attrs & game_engine.ECardAttributes.PreventAllDamage:
        d["flags"] |= {"prevent_combat_damage", "prevent_noncombat_damage"}
    elif attrs & game_engine.ECardAttributes.PreventCombatDamage:
        d["flags"].add("prevent_combat_damage")
    elif attrs & game_engine.ECardAttributes.PreventNonCombatDamage:
        d["flags"].add("prevent_noncombat_damage")
    # Base Lethal is stored from the client TAC metadata on card_templates.
    # Keep this query additive so older focused test fixtures without the
    # migrated column continue to resolve as cards without Lethal.
    try:
        from pvp_db import db_card_rage_lethal
        lethal_all = db_card_rage_lethal(session_id, int(card_uid), conn=db)
        lethal_row = (lethal_all[1],) if lethal_all else None
    except sqlite3.OperationalError:
        lethal_row = None
    if lethal_row and lethal_row[0]:
        d["flags"].add("lethal")
    return atk, max(0, def_ - dmg), attrs, d["flags"], d["rage"]


def effective_attributes(db, session_id, bstate, card_uid):
    """Return the current combat keyword bits for one card instance.

    Combat option generation must use the same continuous/static evaluation as
    combat resolution.  In particular, conditional CardModifier abilities
    (Electroid's Dwarf/Robot count is one example) are not persisted in
    ``game_cards.card_attributes``.
    """
    return effective_stats(db, session_id, bstate, card_uid)[2]


def effective_cost(db, session_id, bstate, card_uid):
    """Current play cost of a card instance (template cost + cost modifiers +
    continuous static cost reductions) — the X value for AbilityResourceXCost
    variables and the cost shown/charged for hand cards."""
    from pvp_db import db_card_cost_location_state
    row = db_card_cost_location_state(session_id, int(card_uid), conn=db)
    if not row:
        return 0
    cost = (row[0] or 0) + (row[1] or 0)
    if row[2] and str(row[2]).strip() not in ("[]", "{}", ""):
        try:
            from .cost_mod import cost_mod_delta
            cost += cost_mod_delta(db, session_id, int(card_uid), row[2])
        except Exception:
            pass
    # A zone-wide dynamic reduction is the fallback for cards outside the
    # warzone (they have not received their CardCreatedEvent yet).  Once the
    # card is in the warzone, effective_deltas() evaluates the same metadata
    # through the card's continuous static ability.  Applying both paths
    # double-counts reductions such as Pterobot's, producing a displayed cost
    # of zero when the real cost is positive.
    if ((not row[2] or str(row[2]).strip() in ("[]", "{}", ""))
            and row[3] != "warzone"):
        try:
            from .cost_mod import dynamic_cost_mod_delta
            cost += dynamic_cost_mod_delta(db, session_id, int(card_uid))
        except Exception:
            pass
    try:
        cost += effective_deltas(db, session_id, bstate, int(card_uid))["cost_mod"]
    except Exception:
        pass
    return max(0, cost)


def controller_flags(db, session_id, bstate, owner):
    """Aggregated combat flags from every static ability the controller has in
    play (e.g. Te'talca's "your cards and effects deal double damage")."""
    flags = set()
    from pvp_db import db_warzone_card_uids
    for (uid,) in db_warzone_card_uids(session_id, owner, conn=db):
        d = self_deltas(db, session_id, bstate, uid)
        flags |= d["flags"]
    return flags


def global_flags(db, session_id, bstate):
    """Flags from every player's warzone statics (e.g. Emberspire Witch's
    "Champions can't gain health" applies while she is in play)."""
    flags = set()
    from pvp_db import db_warzone_owner_ids
    for (owner,) in db_warzone_owner_ids(session_id, conn=db):
        flags |= controller_flags(db, session_id, bstate, owner)
    return flags


def health_gain_bonus(db, session_id, bstate, owner):
    """Return the controller's continuous bonus to each health gain.

    The client represents effects such as Lifeweaver Shaman's
    ``LifeGainModifier`` as a static typed ``IntAttrModifier``.  It is not a
    triggered ability, so it must be folded into the amount before the shared
    health-gain event is emitted.  Iterate over card instances so identical
    copies stack independently.
    """
    total = 0
    from pvp_db import db_warzone_card_uids
    rows = db_warzone_card_uids(session_id, owner, conn=db)
    for (card_uid,) in rows:
        for ability_guid, _raw in _card_static_abilities(
                db, session_id, int(card_uid)):
            for param, raw in _static_leaves(db, ability_guid):
                if (param.get("property") != "intattr" or
                        str(param.get("attribute") or "") !=
                        "LifeGainModifier"):
                    continue
                if not _gate_condition(
                        db, session_id, bstate, param.get("condition_id"),
                        int(card_uid), owner):
                    continue
                try:
                    value = int(param.get("amount") or 0)
                except (TypeError, ValueError):
                    value = 0
                operation = str(param.get("operation") or "Add").lower()
                if operation == "add":
                    total += value
    return total


def can_block(db, session_id, bstate, attacker_uid, blocker_uid):
    """Is ``blocker_uid`` allowed to block ``attacker_uid``?  Enforces Flight
    (needs a Flight/SkyGuard blocker) and "can't be blocked except by artifact
    troops and/or blood troops" (Corrupt Harvester, Wailing Banshee), plus the
    client's CanBlock() baseline: a blocker must be an untapped Troop without
    the CantBlock attribute (Inner Peace / Inner Conflict "can't attack or
    block")."""
    a_atk, a_def, a_attrs, a_flags, _ = effective_stats(
        db, session_id, bstate, attacker_uid)
    b_atk, b_def, b_attrs, b_flags, _ = effective_stats(
        db, session_id, bstate, blocker_uid)
    from pvp_db import db_card_mutation_info, db_card_state_value
    brow_info = db_card_mutation_info(session_id, int(blocker_uid), conn=db)
    brow_state = db_card_state_value(session_id, int(blocker_uid), conn=db)
    brow = (brow_info[2], brow_state) if brow_info else None
    if not brow or "Troop" not in (brow[0] or ""):
        return False
    if int(brow[1] or 0) & game_engine.ECardStates.Tapped:
        return False
    if b_attrs & game_engine.ECardAttributes.CantBlock:
        return False
    combat_cards = _cards_in_zones(
        db, session_id, None, ["warzone"], bstate=bstate)
    by_uid = {int(card["card_uid"]): card for card in combat_cards}
    attacker_card = by_uid.get(int(attacker_uid))
    blocker_card = by_uid.get(int(blocker_uid))
    if attacker_card and blocker_card:
        def _matches(candidate, source, rule):
            filter_json = rule.get("filter") or rule.get("cardfilter")
            return bool(filter_json and evaluate_card_filter(
                candidate, filter_json, int(source["card_uid"]),
                source_card=source, card_pool=combat_cards,
                ability_state=bstate, db=db))

        attacker_rules = rule_modifiers(
            db, session_id, bstate, int(attacker_uid))
        block_immunity = [rule for rule in attacker_rules
                          if rule.get("property") == "blockimmunity"]
        if any(_matches(blocker_card, attacker_card, rule)
               for rule in block_immunity):
            return False
        exceptions = [rule for rule in attacker_rules
                      if rule.get("property") == "blockimmunityexception"]
        if exceptions and not any(
                _matches(blocker_card, attacker_card, rule)
                for rule in exceptions):
            return False
        blocker_rules = rule_modifiers(
            db, session_id, bstate, int(blocker_uid))
        if any(_matches(attacker_card, blocker_card, rule)
               for rule in blocker_rules
               if rule.get("property") == "blockrestriction"):
            return False
    # "Unblockable" (CantBeBlocked, e.g. Infiltrator Bot's activated ability):
    # the attacker cannot be blocked at all.
    if a_attrs & game_engine.ECardAttributes.CantBeBlocked:
        return False
    if a_attrs & game_engine.ECardAttributes.Flight and not (
            b_attrs & (game_engine.ECardAttributes.Flight |
                       game_engine.ECardAttributes.SkyGuard)):
        return False
    if "unblockable_except" in a_flags:
        artifact_ok = "unblockable_except_artifact" in a_flags
        blood_ok = "unblockable_except_blood" in a_flags
        if not artifact_ok and not blood_ok:
            return False
        is_artifact = False
        is_blood = False
        from pvp_db import db_card_type_threshold
        row = db_card_type_threshold(session_id, int(blocker_uid), conn=db)
        if row:
            ctype = row[0] or ""
            is_troop = "Troop" in ctype
            is_artifact = "Artifact" in ctype
            is_blood = (game_engine.ECardShards.Blood in
                        shards_from_threshold(row[1]))
            # The client's exception filter is
            # Or(And(IsArtifact, IsTroop), And(IsColor Blood, IsTroop)):
            # artifact TROOPS or blood TROOPS may block.
            if not ((artifact_ok and is_artifact and is_troop) or
                    (blood_ok and is_blood and is_troop)):
                return False
        else:
            return False
    return True


def apply_rage(db, session, handler, game, pl_t, ai_t, bstate, uid):
    """Rage X: when a troop with Rage X attacks, it gets +X ATK permanently.
    Returns the applied rage value (0 when the attacker has no Rage)."""
    _atk, _def, _attrs, _flags, rage = effective_stats(
        db, session.session_id, bstate, uid)
    if rage and rage > 0:
        from .stat_mod import apply_card_stat_mod
        # The client's built-in RageAbility uses a Permanent attack modifier;
        # it is not an end-of-turn combat bonus.
        apply_card_stat_mod(game, session, db, handler, pl_t, ai_t, uid,
                            rage, 0, this_turn=False)
        return rage
    return 0
