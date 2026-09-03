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
import re

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
    for (r,) in db.execute(
            "SELECT DISTINCT user_id FROM game_cards WHERE session_id=?",
            (session_id,)):
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
    placeholders = ",".join("?" * len(zones))
    rows = db.execute(
        "SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
        "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
        "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
        "gc.card_attributes, ct.attributes "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.location IN (%s)"
        % placeholders,
        [session_id] + list(zones)).fetchall()
    if user_id is not None:
        rows = [r for r in rows if r[3] == user_id]
    out = [_card_dict(r) for r in rows]
    if include_champions and bstate is not None:
        players = {}
        for (uid,) in db.execute(
                "SELECT DISTINCT user_id FROM game_cards WHERE session_id=?",
                (session_id,)):
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


def _variable_value(db, session_id, bstate, raw, var_name, owner, source_uid,
                    stat_prop=None):
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
        if t == "SumVariableInListAttrCardsAbilityVariable":
            return _sum_list_attr_variable(db, session_id, bstate, owner,
                                            var, source_uid)
        if t == "TriggerTargetPropertyVariable":
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
                # activation dialog.  Spell resolution carries it in bstate;
                # retain the effective-cost fallback for older callers that
                # use this variable outside an active X-cost resolution.
                if "x_cost" in (bstate or {}):
                    return int((bstate or {}).get("x_cost", 0) or 0)
                return effective_cost(db, session_id, bstate, source_uid)
            named = (bstate or {}).get("ability_variables") or {}
            return int(named.get(var.get("m_Name"), 0) or 0)
        if t == "SourcePlayerBriarLegionVariable":
            side = "player" if owner else "ai"
            return int(bstate.get(f"{side}_briar_legions_entered", 0))
        if t == "AbilityConstant":
            try:
                return int(var.get("m_DefaultValue", 0) or 0)
            except Exception:
                return 0
        if t == "CardPropertyVariable":
            # Reads a current stat off the ability's source card (e.g. the
            # Outrider's SelfsDefenseValue -> CurrentDefenseValue drives
            # "+[ATK] equal to this troop's [DEF]").  Base template stat plus
            # the live mod/buff layers, matching the trigger/attribute view.
            prop = str(var.get("m_Property") or "")
            if source_uid is None:
                return int(var.get("m_DefaultValue", 0) or 0)
            row = db.execute(
                "SELECT ct.attack, ct.defense, ct.cost, gc.card_attack_mod, "
                "gc.card_defense_mod, gc.permanent_buffs, gc.temporary_buffs "
                "FROM game_cards gc JOIN card_templates ct "
                "ON ct.guid=gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (session_id, int(source_uid))).fetchone()
            if not row:
                return int(var.get("m_DefaultValue", 0) or 0)
            atk = int(row[0] or 0) + int(row[3] or 0)
            defense = int(row[1] or 0) + int(row[4] or 0)
            for raw_buffs in (row[5], row[6]):
                try:
                    buffs = json.loads(raw_buffs or "{}")
                    atk += int(buffs.get("atk", 0) or 0)
                    defense += int(buffs.get("def", 0) or 0)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            if prop == "CurrentDefenseValue":
                return defense
            if prop == "CurrentAttackValue":
                return atk
            if prop == "CurrentCostValue":
                return int(row[2] or 0)
            return int(var.get("m_DefaultValue", 0) or 0)
        if t == "ExpressionAbilityVariable":
            flat = str(var.get("m_ExpressionText") or "").replace(" ", "")
            # Extracted expressions commonly add a fixed constant to a
            # computed variable (for example, voided troop stats + 3), while
            # a few older records use multiplication (ESC * 4).  Both are
            # AbilityVariable expressions, not localized card-text values.
            m = re.fullmatch(
                r'([A-Za-z_]\w*)(?:\*(-?\d+)|([+-])(\d+))?', flat)
            if m:
                base = _variable_value(db, session_id, bstate, raw, m.group(1),
                                       owner, source_uid, stat_prop)
                if base is None:
                    base = _constant_value(raw, m.group(1))
                if base is None and m.group(1) == "ESC":
                    # The client's ESC variable is SourceCard.EscalationCount,
                    # which starts at 1 and increments with each Escalate —
                    # so the first cast of "ESC * 4" buries 4, not 0.
                    side = ("ai" if owner == 0 else "player")
                    base = int(bstate.get(f"{side}_escalation_uses", 0)) + 1
                if base is None:
                    return None
                if m.group(2) is not None:
                    return base * int(m.group(2))
                if m.group(3) is not None:
                    delta = int(m.group(4))
                    return base + delta if m.group(3) == "+" else base - delta
                return base
            return None
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
    for table in ("card_abilities_meta", "champion_abilities"):
        try:
            row = db.execute(
                "SELECT raw_json FROM %s WHERE ability_guid=? LIMIT 1" % table,
                (str(ability_guid).lower(),)).fetchone()
        except Exception:
            row = None
        if row and row[0]:
            raw = row[0]
            break
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
        row = db.execute(
            "SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
            "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
            "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
            "gc.card_attributes, ct.attributes, gc.card_attack_mod, "
            "gc.card_defense_mod, gc.permanent_buffs, gc.temporary_buffs "
            "FROM game_cards gc JOIN card_templates ct "
            "ON ct.guid=gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session_id, card_uid)).fetchone()
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
                "SourcePlayerHealthVariable",
                "SourcePlayerThresholdAbilityVariable"):
            n = var.get("m_Name")
            if n and n not in seen:
                seen.add(n)
                count_names.append(n)
    if amount == 0:
        # Dynamic: expression / sum / health / count variable directly.
        for var in variables:
            t = str(var.get("_t", "")).split(".")[-1]
            name = var.get("m_Name")
            if t in ("ExpressionAbilityVariable", "CardSumAbilityVariable",
                     "CounterVariable", "TriggerTargetPropertyVariable",
                     "SourcePlayerHealthVariable",
                     "SourcePlayerThresholdAbilityVariable") and name not in seen:
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
        deltas["flags"] |= _flag_from_text(text)
        # Data-driven from the gamedata IntAttrModifier fields
        # (m_Attribute/m_Value), not the effect's game text.
        attr = param.get("attribute") or ""
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
        deltas["flags"].add("double_damage")
    elif prop in ("blockimmunityexception", "blockimmunity", "blockrestriction"):
        deltas["flags"] |= _flag_from_text(text)
    elif prop == "damageimmunity":
        deltas["flags"] |= _flag_from_text(text)
    elif prop == "targetingimmunity":
        deltas["flags"].add("targeting_immunity")


def _static_leaves(db, ability_guid):
    """[(param, raw_json)] for a static ability's CardModifier leaves."""
    row = db.execute(
        "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
        (ability_guid,)).fetchone()
    raw = row[0] if row else ""
    out = []
    for effect_guid, etype, param in db.execute(
            "SELECT effect_guid, effect_type, param FROM ability_effects "
            "WHERE ability_guid=? AND effect_type='CardModifierAbilityEffectTemplate'",
            (ability_guid,)):
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
            if "value" in typed:
                pm["amount"] = typed["value"]
        out.append((pm, raw))
    return out


def _card_static_abilities(db, session_id, card_uid):
    """Static ability GUIDs + raw_json for one card instance."""
    row = db.execute(
        "SELECT card_abilities FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row or not row[0]:
        return []
    try:
        ags = [g.lower() for g in json.loads(row[0])]
    except Exception:
        return []
    out = []
    for ag in ags:
        m = db.execute(
            "SELECT trigger_event_type, is_manual, raw_json "
            "FROM card_abilities_meta WHERE ability_guid=?", (ag,)).fetchone()
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
        if (not m[0] and not m[1]) or is_zone_static:
            out.append((ag, raw))
    return out


def self_deltas(db, session_id, bstate, card_uid):
    """Deltas from the card's own static abilities (self-targeting leaves)."""
    row = db.execute(
        "SELECT user_id, location FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row:
        return {"atk": 0, "def": 0, "cost_mod": 0, "attrs": 0,
                "flags": set(), "rage": 0}
    owner, loc = row
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
    row = db.execute(
        "SELECT collection_flags, player_filter, filter_json, game_text "
        "FROM target_templates WHERE template_id=?", (template_id,)).fetchone()
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
    row = db.execute(
        "SELECT target_template_ids FROM card_abilities_meta "
        "WHERE ability_guid=?", (ability_guid,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        ids = json.loads(row[0])
    except Exception:
        return []
    return [i for i in ids if i]


def aura_deltas(db, session_id, bstate, card_uid):
    """Deltas from other cards the controller controls whose static aura
    targets this card (e.g. Soul Armaments' +2/+2 to troops you control)."""
    row = db.execute(
        "SELECT user_id, location FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row:
        return {"atk": 0, "def": 0, "cost_mod": 0, "attrs": 0,
                "flags": set(), "rage": 0}
    owner, loc = row
    empty = {"atk": 0, "def": 0, "cost_mod": 0, "attrs": 0,
             "flags": set(), "rage": 0}
    if loc != "warzone":
        # Zone-wide auras can still hit cards outside the warzone (e.g.
        # Technical Genius: "Your artifacts in all zones have cost -1").
        pass
    # Cards that can project an aura: the controller's warzone cards.
    holders = db.execute(
        "SELECT card_uid, card_abilities FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='warzone' "
        "AND card_uid!=? AND card_abilities!=''",
        (session_id, owner, int(card_uid))).fetchall()
    total = dict(empty)
    for src_uid, ab_json in holders:
        try:
            ags = [g.lower() for g in json.loads(ab_json or "[]")]
        except Exception:
            continue
        for ag in ags:
            m = db.execute(
                "SELECT trigger_event_type, is_manual, raw_json "
                "FROM card_abilities_meta WHERE ability_guid=?", (ag,)).fetchone()
            if not m or m[0] or m[1]:
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
    resolution so the fought numbers match the displayed card."""
    row = db.execute(
        "SELECT gc.card_attack_mod, gc.card_defense_mod, gc.card_damage, "
        "gc.card_attributes, ct.attack, ct.defense, ct.attributes, "
        "gc.permanent_buffs, gc.temporary_buffs, gc.temporary_attributes "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row:
        return 0, 0, 0, set(), 0
    atk = (row[4] or 0) + (row[0] or 0)
    def_ = (row[5] or 0) + (row[1] or 0)
    dmg = row[2] or 0
    attrs = (row[3] or 0) | (row[6] or 0) | (row[9] or 0)
    instance_rage = 0
    for buff_col in (row[7], row[8]):
        try:
            buffs = json.loads(buff_col or "{}")
            atk += int(buffs.get("atk", 0) or 0)
            def_ += int(buffs.get("def", 0) or 0)
            instance_rage += int(buffs.get("rage", 0) or 0)
        except Exception:
            pass
    d = effective_deltas(db, session_id, bstate, card_uid)
    atk += d["atk"]
    def_ += d["def"]
    attrs |= d["attrs"]
    # The card's printed Rage X (gamedata m_RageValue) stacks with granted
    # Rage (e.g. a socketed gem's "Rage 1 in all zones").  Guarded for DBs /
    # fixtures without the column.
    try:
        rv = db.execute(
            "SELECT ct.rage_value FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid=gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session_id, int(card_uid))).fetchone()
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
    return atk, max(0, def_ - dmg), attrs, d["flags"], d["rage"]


def effective_cost(db, session_id, bstate, card_uid):
    """Current play cost of a card instance (template cost + cost modifiers +
    continuous static cost reductions) — the X value for AbilityResourceXCost
    variables and the cost shown/charged for hand cards."""
    row = db.execute(
        "SELECT ct.cost, gc.card_cost_mod, gc.cost_mod_json, gc.location "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()
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
    for (uid,) in db.execute(
            "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
            "AND location='warzone'", (session_id, owner)):
        d = self_deltas(db, session_id, bstate, uid)
        flags |= d["flags"]
    return flags


def global_flags(db, session_id, bstate):
    """Flags from every player's warzone statics (e.g. Emberspire Witch's
    "Champions can't gain health" applies while she is in play)."""
    flags = set()
    for (owner,) in db.execute(
            "SELECT DISTINCT user_id FROM game_cards "
            "WHERE session_id=? AND location='warzone'", (session_id,)):
        flags |= controller_flags(db, session_id, bstate, owner)
    return flags


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
    brow = db.execute(
        "SELECT card_type, card_state FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(blocker_uid))).fetchone()
    if not brow or "Troop" not in (brow[0] or ""):
        return False
    if int(brow[1] or 0) & game_engine.ECardStates.Tapped:
        return False
    if b_attrs & game_engine.ECardAttributes.CantBlock:
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
        row = db.execute(
            "SELECT ct.card_type, ct.threshold_json FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid=gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session_id, int(blocker_uid))).fetchone()
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
