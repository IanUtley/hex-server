"""RulesPort-owned card stat projection.

The mutable instance/template fields are authoritative runtime facts.  The
continuous-ability scan uses the native typed modifier evaluator; unsupported
metadata is reported as an explicit failure rather than delegated to the old
evaluator, so an attached session cannot silently become hybrid.
"""

from __future__ import annotations

import ast
import json
import math
import threading
from contextlib import contextmanager

import game_engine


def _empty_deltas():
    return {"atk": 0, "def": 0, "cost_mod": 0, "attrs": 0,
            "flags": set(), "rage": 0, "rules": [],
            "card_properties": {}}


_EMPTY_TARGET_SET = frozenset()
_MISSING = object()
# Continuous leaf properties that change a card's projected view (thresholds
# and subtype) rather than its combat numbers.
_CARD_PROPERTY_LEAVES = frozenset({"cardthreshold", "subtype"})


class _ProjectionCache:
    """Memo of the target-independent inputs of one static projection.

    A single ``effective_stats``/``effective_cost``/``effective_attributes``
    call re-derives the same continuous modifiers many times: every projected
    target card asks every source card for its authored leaves, every aura
    leaf asks its ability for the authored target template, and every
    candidate card in that template's pool projects its own threshold/subtype
    view.  Those inputs cannot change while one synchronous projection runs,
    so they are memoized here for the lifetime of the outermost scan.
    """

    __slots__ = ("source_cards", "source_abilities", "ability_leaves",
                 "ability_targets", "target_sets", "card_property_sources",
                 "card_rows")

    def __init__(self):
        self.source_cards = {}
        self.source_abilities = {}
        self.ability_leaves = {}
        self.ability_targets = {}
        self.target_sets = {}
        self.card_property_sources = {}
        self.card_rows = {}


_projection_local = threading.local()


@contextmanager
def _projection_cache():
    """Share one :class:`_ProjectionCache` with every nested scan."""
    cache = getattr(_projection_local, "cache", None)
    if cache is not None:
        yield cache
        return
    cache = _ProjectionCache()
    _projection_local.cache = cache
    try:
        yield cache
    finally:
        _projection_local.cache = None


def _static_abilities(db, session_id, card_uid, cache=None):
    """Return the continuous abilities authored on one materialized card."""
    from pvp_db import db_card_ability_payload, db_ability_static_metadata
    key = (int(session_id or 0), int(card_uid))
    if cache is not None:
        cached = cache.source_abilities.get(key)
        if cached is not None:
            return cached
    payload = db_card_ability_payload(session_id, int(card_uid), conn=db)
    try:
        values = json.loads(payload or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    result = []
    for value in values:
        guid = str(value).lower()
        metadata = db_ability_static_metadata(guid, conn=db)
        if not metadata:
            continue
        if not metadata[0] and not metadata[1]:
            result.append(guid)
            continue
        raw = str(metadata[2] or "")
        if ("CardCreatedEvent" in str(metadata[0] or "") and
                all(zone in raw for zone in ("Deck", "Hand", "Warzone", "Discard"))):
            result.append(guid)
    result = tuple(result)
    if cache is not None:
        cache.source_abilities[key] = result
    return result


def _static_leaves(db, ability_guid, cache=None):
    """Return the continuous CardModifier leaves authored on one ability."""
    from .metadata import modifier_metadata
    from pvp_db import db_ability_effect_rows
    cache_key = str(ability_guid or "").lower()
    if cache is not None:
        cached = cache.ability_leaves.get(cache_key)
        if cached is not None:
            return cached
    leaves = []
    for effect_guid, effect_type, raw_param in db_ability_effect_rows(
            ability_guid, conn=db):
        if effect_type != "CardModifierAbilityEffectTemplate":
            continue
        try:
            param = json.loads(raw_param or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(param, dict) or param.get("duration") not in (
                "WhileCardInPlay", "Permanent", "BeginningOfOwnersTurn"):
            continue
        typed = modifier_metadata(effect_guid)
        if typed:
            param = dict(param)
            for key, value in typed.items():
                if value not in (None, ""):
                    if key == "value" and not param.get("amount"):
                        param["amount"] = value
                    else:
                        param.setdefault(key, value)
        from pvp_db import db_ability_raw_json
        leaves.append((param, db_ability_raw_json(ability_guid, conn=db) or ""))
    leaves = tuple(leaves)
    if cache is not None:
        cache.ability_leaves[cache_key] = leaves
    return leaves


def _literal_leaf(param):
    """Return a native literal leaf, or ``None`` for a dynamic leaf."""
    prop = str(param.get("property") or "").lower()
    if prop not in {"attack", "defense", "cardcost", "attribute", "intattr"}:
        return None
    if param.get("input_variable"):
        return None
    value = param.get("amount")
    if value in (None, ""):
        value = param.get("input_value", param.get("value", 0))
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return None
    return prop, value


def _count_variable(db, session_id, battle_state, source_uid, owner, raw,
                    variable_name):
    """Evaluate a typed CardCountAbilityVariable using native filters."""
    try:
        record = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    variable = next((item for item in record.get("m_Variables", [])
                     if item.get("m_Name") == variable_name), None)
    if not variable or str(variable.get("_t", "")).rsplit(".", 1)[-1] != \
            "CardCountAbilityVariable":
        return None
    from pvp_db import db_target_candidate_rows
    from .targeting import _card, _source_card
    from .filters import records_filter_matches
    zone_map = {"Deck": "deck", "Hand": "hand", "Warzone": "warzone",
                "Crypt": "discard", "Discard": "discard",
                "Void": "void", "CastSpells": "CastSpells",
                "PlayedResources": "PlayedResources", "Choosing": "choosing",
                "Underground": "underground"}
    zones = [zone_map.get(value, str(value).lower()) for value in
             str(variable.get("m_CollectionFlags") or "").split("|") if value]
    if not zones:
        return 0
    player_filter = str(variable.get("m_PlayerFilter") or "Self").lower()
    both_players = player_filter in {"multipleplayers", "allplayers",
                                    "multipleopponents"}
    candidates = db_target_candidate_rows(
        session_id, zones, controller_uid=int(owner),
        both_players=both_players, conn=db)
    source = _source_card(db, session_id, int(source_uid), int(owner))
    spec = variable.get("m_CardFilter") or {}
    cards = [_card(row) for row in candidates]
    return sum(1 for card in cards if records_filter_matches(
        card, spec, source=source, context=dict(battle_state or {}, cards=cards)))


def _sum_variable(db, session_id, battle_state, source_uid, owner, raw,
                  variable_name):
    """Evaluate a typed CardSumAbilityVariable using native card facts."""
    try:
        record = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    variable = next((item for item in record.get("m_Variables", [])
                     if item.get("m_Name") == variable_name), None)
    if not variable or str(variable.get("_t", "")).rsplit(".", 1)[-1] != \
            "CardSumAbilityVariable":
        return None
    from pvp_db import db_target_candidate_rows
    from .targeting import _card, _source_card
    from .filters import records_filter_matches
    zone_map = {"Deck": "deck", "Hand": "hand", "Warzone": "warzone",
                "Crypt": "discard", "Discard": "discard",
                "Void": "void", "CastSpells": "CastSpells",
                "PlayedResources": "PlayedResources", "Choosing": "choosing",
                "Underground": "underground"}
    zones = [zone_map.get(value, str(value).lower()) for value in
             str(variable.get("m_CollectionFlags") or "").split("|") if value]
    if not zones:
        return 0
    player_filter = str(variable.get("m_PlayerFilter") or "Self").lower()
    both_players = player_filter in {"multipleplayers", "allplayers",
                                    "multipleopponents"}
    cards = [_card(row) for row in db_target_candidate_rows(
        session_id, zones, controller_uid=int(owner),
        both_players=both_players, conn=db)]
    source = _source_card(db, session_id, int(source_uid), int(owner))
    spec = variable.get("m_CardFilter") or {}
    cards = [card for card in cards if records_filter_matches(
        card, spec, source=source, context=dict(battle_state or {}, cards=cards))]
    prop = str(variable.get("m_Property") or "").lower()
    if prop in {"currentattackvalue", "attack", "cardattack"}:
        return sum(int(card.get("attack", 0) or 0) for card in cards)
    if prop in {"currentdefensevalue", "defense", "carddefense"}:
        return sum(int(card.get("defense", 0) or 0) for card in cards)
    if prop in {"resourcecosttrue", "cost", "cardcost"}:
        return sum(effective_cost(
            db, session_id, battle_state, int(card["card_uid"]))
                   for card in cards)
    return None


def _counter_variable(db, session_id, battle_state, source_uid, owner, raw,
                      variable_name):
    """Evaluate a typed CounterVariable from native persisted counters."""
    try:
        record = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    variable = next((item for item in record.get("m_Variables", [])
                     if item.get("m_Name") == variable_name), None)
    if not variable or str(variable.get("_t", "")).rsplit(".", 1)[-1] != \
            "CounterVariable":
        return None
    from pvp_db import db_target_candidate_rows
    from .targeting import _card, _source_card
    from .filters import records_filter_matches
    zone_map = {"Deck": "deck", "Hand": "hand", "Warzone": "warzone",
                "Crypt": "discard", "Discard": "discard",
                "Void": "void", "CastSpells": "CastSpells",
                "PlayedResources": "PlayedResources", "Choosing": "choosing",
                "Underground": "underground"}
    zones = [zone_map.get(value, str(value).lower()) for value in
             str(variable.get("m_CollectionFlags") or "").split("|") if value]
    if not zones:
        return 0
    player_filter = str(variable.get("m_PlayerFilter") or "Self").lower()
    both_players = player_filter in {"multipleplayers", "allplayers",
                                    "multipleopponents"}
    cards = [_card(row) for row in db_target_candidate_rows(
        session_id, zones, controller_uid=int(owner),
        both_players=both_players, conn=db)]
    source = _source_card(db, session_id, int(source_uid), int(owner))
    spec = variable.get("m_CardFilter") or {}
    wanted = str((variable.get("m_CardCounterTemplateId") or {}).get(
        "m_Guid") or "").lower()
    total = 0
    for card in cards:
        if not records_filter_matches(
                card, spec, source=source,
                context=dict(battle_state or {}, cards=cards)):
            continue
        for name, value in (card.get("counters") or {}).items():
            guid = str((card.get("counter_guids") or {}).get(name, "")).lower()
            if wanted and guid != wanted:
                continue
            total += int(value or 0)
    return total


def _list_sum_variable(db, session_id, battle_state, raw, variable):
    """Sum a typed property over the cards captured by an ability list."""
    list_name = variable.get("m_ListAttrName") or variable.get("m_Name")
    values = (battle_state or {}).get("ability_lists", {}).get(list_name)
    if values is None:
        return 0
    prop = str(variable.get("m_Property") or "").lower()
    total = 0
    from pvp_db import db_card_static_row, db_card_cost_location_state
    for value in values if isinstance(values, (list, tuple, set)) else []:
        try:
            uid = int(value)
        except (TypeError, ValueError):
            continue
        if prop in {"currentattackvalue", "attack", "cardattack",
                    "currentdefensevalue", "defense", "carddefense"}:
            try:
                row = db_card_static_row(session_id, uid, conn=db)
            except Exception:
                row = None
            if not row:
                continue
            index, modifier = ((4, 0) if prop in {
                "currentattackvalue", "attack", "cardattack"} else (5, 1))
            value_now = int(row[index] or 0) + int(row[modifier] or 0)
            for serialized in row[7:9]:
                try:
                    value_now += int((json.loads(serialized or "{}") or {}).get(
                        "atk" if index == 4 else "def", 0) or 0)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            total += value_now
        elif prop in {"resourcecosttrue", "cost", "cardcost"}:
            row = db_card_cost_location_state(session_id, uid, conn=db)
            if row:
                total += max(0, int(row[0] or 0) + int(row[1] or 0))
    return total


def _source_player_shards(db, session_id, owner, variable):
    """Count distinct authored shard bits in the variable's card zone."""
    from pvp_db import db_target_candidate_rows
    from .targeting import _card
    zone_map = {"Deck": "deck", "Hand": "hand", "Warzone": "warzone",
                "Crypt": "discard", "Discard": "discard", "Void": "void",
                "CastSpells": "CastSpells", "PlayedResources": "PlayedResources",
                "Choosing": "choosing", "Underground": "underground"}
    zones = [zone_map.get(value, str(value).lower()) for value in
             str(variable.get("m_Zone") or "Warzone").split("|") if value]
    bits = set()
    for row in db_target_candidate_rows(
            session_id, zones, controller_uid=int(owner),
            both_players=False, conn=db):
        card = _card(row)
        bits.update(int(value) for value in card.get("shards", ()) if value)
    return len(bits) if variable.get("m_DifferentThresholds") else 0


def _expression_value(db, session_id, battle_state, source_uid, owner, raw,
                      variable_name, stack=None):
    """Resolve one native Records variable, including safe expressions."""
    stack = set(stack or ())
    if variable_name in stack:
        return None
    stack.add(variable_name)
    try:
        record = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    variable = next((item for item in record.get("m_Variables", [])
                     if item.get("m_Name") == variable_name), None)
    if not variable:
        return None
    kind = str(variable.get("_t", "")).rsplit(".", 1)[-1]
    if kind == "AbilityConstant":
        try:
            return int(variable.get("m_DefaultValue", 0) or 0)
        except (TypeError, ValueError):
            return 0
    if kind in {"CardCountAbilityVariable", "CardSumAbilityVariable",
                "CounterVariable"}:
        fn = {"CardCountAbilityVariable": _count_variable,
              "CardSumAbilityVariable": _sum_variable,
              "CounterVariable": _counter_variable}[kind]
        return fn(db, session_id, battle_state, source_uid, owner, raw,
                  variable_name)
    if kind == "SourcePlayerHealthVariable":
        key = "player_health" if owner else "ai_health"
        return int((battle_state or {}).get(key, 0) or 0)
    if kind == "SourcePlayerThresholdAbilityVariable":
        color = str(variable.get("m_Threshold") or "").lower()
        flag = game_engine.SHARD_TO_FLAG.get(color, 0)
        key = "player_threshold" if owner else "ai_threshold"
        return int(((battle_state or {}).get(key) or {}).get(flag, 0) or 0)
    if kind == "IntAttrAbilityVariable":
        values = (battle_state or {}).get("ability_variables") or {}
        return int(values.get(variable_name,
                              variable.get("m_DefaultValue", 0)) or 0)
    if kind in {"AbilityVariable", "CardIntegerVariable"}:
        if kind == "CardIntegerVariable":
            values = (battle_state or {}).get("card_integer_variables") or {}
            if variable_name in values:
                return int(values[variable_name] or 0)
            from pvp_db import db_card_mutation_field
            try:
                raw_buffs = db_card_mutation_field(
                    session_id, int(source_uid), "permanent_buffs", conn=db)
                saved = json.loads(raw_buffs or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                saved = {}
            values = saved.get("card_integer_variables", {}) \
                if isinstance(saved, dict) else {}
            return int(values.get(variable_name,
                                  variable.get("m_DefaultValue", 0)) or 0)
        values = (battle_state or {}).get("ability_variables") or {}
        return int(values.get(variable_name,
                              variable.get("m_DefaultValue", 0)) or 0)
    if kind == "SourcePlayerResourceAbilityVariable":
        key = ("player_resources" if owner else "ai_resources") \
            if variable.get("m_LookUpTemporaryResources") else \
            ("player_total_resources" if owner else "ai_total_resources")
        return int((battle_state or {}).get(
            key, variable.get("m_DefaultValue", 0)) or 0)
    if kind == "SourcePlayerShardAbilityVariable":
        return _source_player_shards(db, session_id, owner, variable)
    if kind == "CountListAttrAbilityVariable":
        lists = (battle_state or {}).get("ability_lists") or {}
        values = lists.get(variable.get("m_ListAttrName") or variable_name)
        return (len(values) if values is not None else
                int(variable.get("m_DefaultValue", 0) or 0))
    if kind == "AbilityPropertyVariable":
        if str(variable.get("m_Property") or "") == "AbilityResourceXCost":
            return int((battle_state or {}).get("x_cost", 0) or 0)
        values = (battle_state or {}).get("ability_variables") or {}
        return int(values.get(variable_name, 0) or 0)
    if kind == "CardPropertyVariable":
        prop = str(variable.get("m_Property") or "")
        from pvp_db import db_card_static_row, db_card_cost_location_state
        if prop in {"CurrentAttackValue", "CurrentDefenseValue"}:
            row = db_card_static_row(session_id, int(source_uid), conn=db)
            index = 4 if prop == "CurrentAttackValue" else 5
            modifier = 0 if prop == "CurrentAttackValue" else 1
            value = int(row[index] or 0) + int(row[modifier] or 0) if row else 0
            if row:
                key = "atk" if prop == "CurrentAttackValue" else "def"
                for serialized in row[7:9]:
                    try:
                        value += int((json.loads(serialized or "{}") or {}).get(key, 0) or 0)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
            return value
        if prop == "ResourceCostTrue":
            row = db_card_cost_location_state(
                session_id, int(source_uid), conn=db)
            return max(0, int(row[0] or 0) + int(row[1] or 0)) if row else 0
        return None
    if kind in {"TriggerTargetPropertyVariable",
                "TriggerSourcePropertyVariable"}:
        target = ((battle_state or {}).get("resolving_trigger_target_uid")
                  or (battle_state or {}).get("resolving_target_uid"))
        if target is None:
            return int(variable.get("m_DefaultValue", 0) or 0)
        prop = str(variable.get("m_Property") or "")
        if prop == "ResourceCostTrue":
            return effective_cost(db, session_id, battle_state, int(target))
        if prop in {"CurrentAttackValue", "CurrentDefenseValue"}:
            values = effective_stats(
                db, session_id, battle_state, int(target))
            return int(values[0] if prop == "CurrentAttackValue" else values[1])
        return int(variable.get("m_DefaultValue", 0) or 0)
    if kind == "SourcePlayerBriarLegionVariable":
        return int((battle_state or {}).get("briar_legions_entered", 0) or 0)
    if kind == "SumVariableInListAttrCardsAbilityVariable":
        return _list_sum_variable(
            db, session_id, battle_state, raw, variable)
    if kind != "ExpressionAbilityVariable":
        return None
    try:
        tree = ast.parse(str(variable.get("m_ExpressionText") or ""),
                         mode="eval")
    except (SyntaxError, ValueError, TypeError):
        return None

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(
                node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            if node.id == "ESC":
                side = "ai" if not owner else "player"
                return int((battle_state or {}).get(f"{side}_escalation_uses", 0) or 0) + 1
            return _expression_value(
                db, session_id, battle_state, source_uid, owner, raw,
                node.id, stack)
        if isinstance(node, ast.UnaryOp) and isinstance(
                node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = visit(node.left), visit(node.right)
            if left is None or right is None:
                raise ValueError("unknown expression variable")
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
        return int(value) if math.isfinite(float(value)) else None
    except (ArithmeticError, TypeError, ValueError, OverflowError):
        return None


def _native_leaf_value(db, session_id, battle_state, source_uid, owner, param,
                       raw):
    if param.get("input_variable"):
        value = _count_variable(
            db, session_id, battle_state, source_uid, owner, raw,
            str(param["input_variable"]))
        if value is None:
            value = _sum_variable(
                db, session_id, battle_state, source_uid, owner, raw,
                str(param["input_variable"]))
        if value is None:
            value = _counter_variable(
                db, session_id, battle_state, source_uid, owner, raw,
                str(param["input_variable"]))
        if value is None:
            value = _expression_value(
                db, session_id, battle_state, source_uid, owner, raw,
                str(param["input_variable"]))
        if value is None:
            return None
        try:
            amount = int(param.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount:
            value = amount * int(value)
        return str(param.get("property") or "").lower(), int(value)
    literal = _literal_leaf(param)
    if literal is not None and literal[1] != 0:
        return literal
    # A zero CardModifier operand means “evaluate the authored variable” in
    # the client data.  Prefer typed metadata over a localized text guess.
    try:
        record = json.loads(raw or "{}")
        variables = record.get("m_Variables") or []
    except (TypeError, ValueError, json.JSONDecodeError):
        variables = []
    for variable in variables:
        name = variable.get("m_Name")
        if not name:
            continue
        value = _expression_value(
            db, session_id, battle_state, source_uid, owner, raw, name)
        if value is not None:
            prop = str(param.get("property") or "").lower()
            if prop in {"attack", "defense", "cardcost", "intattr"}:
                return prop, int(value)
    # Attribute grants commonly encode their operand in typed
    # ``attribute_flags`` while leaving CardModifier.amount at zero (for
    # example Emberleaf Duelist's Swiftstrike). Preserve the leaf so the
    # native static evaluator can project the keyword when its condition is
    # met.
    if str(param.get("property") or "").lower() == "attribute" and (
            param.get("attribute_flags") or param.get("attributeflags") or
            param.get("value")):
        return "attribute", 0
    return literal


def _target_matches(db, session_id, source_uid, source_owner, target_uid,
                    ability_guid, battle_state, cache=None):
    """Whether one continuous leaf's authored target set contains ``target``."""
    if cache is None:
        cache = _ProjectionCache()
    entries = _ability_target_entries(db, ability_guid, cache)
    if entries is None:
        # An ability with no authored target template modifies its own source.
        return int(source_uid) == int(target_uid)
    for template_id, self_only, both in entries:
        if self_only:
            if int(source_uid) == int(target_uid):
                return True
            continue
        if int(target_uid) in _template_target_set(
                db, session_id, source_owner, template_id, source_uid,
                both, battle_state, cache):
            return True
    return False


def _ability_target_entries(db, ability_guid, cache):
    """Return ``(template_id, self_only, both_players)`` per authored target.

    The template list and its game-text classification are authored data, so
    they are resolved once per projection instead of once per projected card.
    ``None`` means the ability authors no target template at all.
    """
    key = str(ability_guid or "").lower()
    cached = cache.ability_targets.get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    from pvp_db import db_ability_target_template_ids, db_static_target_template
    payload = db_ability_target_template_ids(ability_guid, conn=db)
    if not payload:
        cache.ability_targets[key] = None
        return None
    try:
        template_ids = json.loads(payload or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        template_ids = []
    entries = []
    for template_id in template_ids:
        row = db_static_target_template(template_id, conn=db)
        if not row:
            continue
        # target_templates stores game_text in column 1; column 3 is the
        # random-target flag. Reading the latter made #SELF# continuous
        # abilities (including Emberleaf Duelist's attack-time Swiftstrike)
        # fail their target match and silently disappear from combat stats.
        text = str(row[1] or "").lower()
        entries.append((template_id,
                        "this" in text or "#self#" in text or
                        text.strip() == "you",
                        text in {"multipleplayers", "allplayers"}))
    entries = tuple(entries)
    cache.ability_targets[key] = entries
    return entries


def _template_target_set(db, session_id, controller_uid, template_id,
                         source_uid, both_players, battle_state, cache):
    """Return the candidate uids matching one authored target template.

    An aura only needs to know whether a single projected card is a legal
    target.  Materializing the candidate pool once per projection keeps that
    question linear in the board size instead of re-scanning (and re-projecting
    every candidate's threshold/subtype view) for every leaf and card.
    """
    state = battle_state if isinstance(battle_state, dict) else {}
    key = (int(session_id or 0), str(template_id).lower(),
           int(controller_uid or 0), int(source_uid or 0), bool(both_players),
           bool(state.get("_rules_port_suppress_card_properties")))
    cached = cache.target_sets.get(key)
    if cached is not None:
        return cached
    from .targeting import legal_targets
    try:
        candidates = legal_targets(
            db, session_id, int(controller_uid), template_id, int(source_uid),
            both_players=bool(both_players), battle_state=battle_state)
    except Exception:
        # A failing template resolves no targets, exactly as the per-leaf
        # probe did; do not memoize it so a transient failure can retry.
        return _EMPTY_TARGET_SET
    result = frozenset(int(value) for value in candidates)
    cache.target_sets[key] = result
    return result


class _StaticSession:
    def __init__(self, session_id):
        self.session_id = session_id


def _static_condition_matches(db, session_id, battle_state, param, source_uid,
                              source_owner, target_uid):
    condition_id = str(param.get("condition_id") or "")
    if not condition_id or condition_id == "0" * 36:
        return True
    from .condition_context import ConditionContext
    from .conditions import evaluate_effect_condition
    context = ConditionContext(
        db, _StaticSession(session_id), battle_state,
        event_type="AbilityEffectEvent",
        ability_source_uid=int(source_uid),
        ability_source_owner_id=int(source_owner),
        trigger_uid=int(target_uid))
    return bool(evaluate_effect_condition(db, condition_id, context))


def _native_static_deltas(db, session_id, battle_state, card_uid):
    """Evaluate the supported literal continuous Records leaves natively."""
    with _projection_cache() as cache:
        return _scan_static_deltas(
            db, session_id, battle_state, card_uid, cache)


def _owner_static_sources(db, session_id, owner, cache):
    """Return the uids whose continuous abilities can project onto ``owner``."""
    from pvp_db import db_cards_in_zones_with_abilities
    key = (int(session_id or 0), int(owner or 0))
    cached = cache.source_cards.get(key)
    if cached is None:
        cached = tuple(int(uid) for uid, _abilities in
                       db_cards_in_zones_with_abilities(
                           session_id, int(owner),
                           ("warzone", "underground"), conn=db))
        cache.source_cards[key] = cached
    return cached


def _card_location_row(db, session_id, card_uid, cache):
    """Return ``(owner, location, position)`` for one card, memoized per scan."""
    from pvp_db import db_card_owner_location_position
    key = (int(session_id or 0), int(card_uid))
    cached = cache.card_rows.get(key, _MISSING)
    if cached is _MISSING:
        cached = db_card_owner_location_position(
            session_id, int(card_uid), conn=db)
        cache.card_rows[key] = cached
    return cached


def _owner_projects_card_properties(db, session_id, owner, cache):
    """Whether any source of ``owner`` can change a card's projected view.

    Only ``CardThreshold``/``SubType`` leaves feed the threshold/subtype
    projection.  When the owner has none — the common case — projecting a
    candidate's view is a no-op, so the pool scan skips an aura evaluation per
    candidate card.
    """
    key = (int(session_id or 0), int(owner or 0))
    cached = cache.card_property_sources.get(key)
    if cached is not None:
        return cached
    cached = any(
        str(param.get("property") or "").lower() in _CARD_PROPERTY_LEAVES
        for source_uid in _owner_static_sources(db, session_id, owner, cache)
        for ability_guid in _static_abilities(db, session_id, source_uid, cache)
        for param, _raw in _static_leaves(db, ability_guid, cache))
    cache.card_property_sources[key] = cached
    return cached


def _scan_static_deltas(db, session_id, battle_state, card_uid, cache):
    row = _card_location_row(db, session_id, card_uid, cache)
    if not row:
        return _empty_deltas(), False
    owner, location, _position = row
    total = _empty_deltas()
    for source_uid in _owner_static_sources(db, session_id, owner, cache):
        for ability_guid in _static_abilities(db, session_id, source_uid, cache):
            for param, raw in _static_leaves(db, ability_guid, cache):
                literal = _native_leaf_value(
                    db, session_id, battle_state, source_uid, owner, param, raw)
                property_name = str(param.get("property") or "").lower()
                # These modifiers change the target card's typed view rather
                # than its combat numbers.  Keep them in the same native
                # projection so a permanent aura cannot force the whole stat
                # calculation back through the historical static evaluator.
                if property_name in {"cardthreshold", "subtype"}:
                    if not _target_matches(
                            db, session_id, source_uid, int(owner),
                            int(card_uid), ability_guid, battle_state, cache):
                        continue
                    if not _static_condition_matches(
                            db, session_id, battle_state, param, source_uid,
                            int(owner), int(card_uid)):
                        continue
                    props = total["card_properties"].setdefault(
                        int(card_uid), {"thresholds": None,
                                        "subtype": None,
                                        "subtype_add": []})
                    if property_name == "cardthreshold":
                        shard = str(param.get("shard") or "").rsplit(
                            ".", 1)[-1].lower()
                        if shard in {"", "unknown", "invalid"}:
                            props["thresholds"] = []
                        else:
                            flag = int(game_engine.SHARD_TO_FLAG.get(shard, 0))
                            props["thresholds"] = [flag] if flag else []
                    else:
                        subtype = str(param.get("subtype") or "").strip()
                        operation = str(param.get("operation") or "set").lower()
                        if subtype:
                            if operation == "add":
                                props["subtype_add"].append(subtype)
                        else:
                            props["subtype"] = subtype
                    continue
                # Resource modifiers are activated effects, not card combat
                # deltas.  They can appear on a Permanent leaf in authored
                # data, but the resource lifecycle owns their application;
                # stat/cost projection must not delegate merely because it
                # encountered one while scanning a source ability.
                if property_name in {"currentresource", "totalresource",
                                      "chargepoints"}:
                    continue
                if property_name in {
                        "damagemultiplier", "damageimmunity", "attackimmunity",
                        "targetingimmunity", "blockimmunity",
                        "blockimmunityexception", "blockrestriction"}:
                    if _static_condition_matches(
                            db, session_id, battle_state, param, source_uid,
                            int(owner), int(card_uid)):
                        total["rules"].append(dict(param))
                    continue
                if literal is None:
                    return total, True
                if not _target_matches(
                        db, session_id, source_uid, int(owner), int(card_uid),
                        ability_guid, battle_state, cache):
                    continue
                try:
                    if not _static_condition_matches(
                            db, session_id, battle_state, param, source_uid,
                            int(owner), int(card_uid)):
                        continue
                except RuntimeError:
                    return total, True
                prop, value = literal
                if prop == "attack":
                    total["atk"] += value
                elif prop == "defense":
                    total["def"] += value
                elif prop == "cardcost":
                    total["cost_mod"] += value
                elif prop == "attribute":
                    from .attribute_effects import attribute_bits_from_flags
                    total["attrs"] |= attribute_bits_from_flags(
                        param.get("attribute_flags",
                                  param.get("attributeflags",
                                           param.get("value", 0))))
                elif prop == "intattr":
                    attribute = str(param.get("attribute") or "").lower()
                    if attribute == "rage":
                        total["rage"] += value
                        total["attrs"] |= int(game_engine.ECardAttributes.Rage)
                    elif attribute in {"preventcombatdamage",
                                       "preventnoncombatdamage"}:
                        total["flags"].add(
                            "prevent_combat_damage" if attribute ==
                            "preventcombatdamage" else
                            "prevent_noncombat_damage")
                    elif attribute in {"cantgainhealth", "cantlosehealth",
                                       "cantplaycards", "unlimitedhandsize"}:
                        total["flags"].add({
                            "cantgainhealth": "cant_gain_health",
                            "cantlosehealth": "cant_lose_health",
                            "cantplaycards": "cant_play_cards",
                            "unlimitedhandsize": "no_max_hand_size",
                        }[attribute])
                    elif attribute in {"lethal", "crush"}:
                        total["flags"].add(attribute)
                    else:
                        # IntAttr is also the client's storage for many
                        # ability-local markers (Tamed, Prophesied, and
                        # similar).  Those are not continuous combat stats;
                        # the old static layer ignores them, so do the same
                        # natively instead of delegating the whole projection.
                        continue
    return total, False


def rule_modifiers(db, session_id, battle_state, card_uid):
    """Return native typed rule modifiers affecting one card."""
    native, unsupported = _native_static_deltas(
        db, session_id, battle_state or {}, int(card_uid))
    from pvp_db import db_card_mutation_field
    result = list(native["rules"])
    for column in ("permanent_buffs", "temporary_buffs"):
        try:
            data = json.loads(db_card_mutation_field(
                session_id, int(card_uid), column, conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            result.extend(value for value in data.get("rule_modifiers", [])
                         if isinstance(value, dict))
    return result


def effective_card_properties(db, session_id, battle_state, card_uid):
    """Return the native current threshold/subtype view of a card.

    CardThresholdModifier and SubTypeModifier are not combat deltas, but
    target filters and casting requirements must observe them.  The guard
    prevents the static aura's own target query from recursively asking for
    the same projected view.
    """
    state = battle_state if isinstance(battle_state, dict) else {}
    marker = "_rules_port_suppress_card_properties"
    previous = state.get(marker)
    state[marker] = True
    try:
        props = {}
        with _projection_cache() as cache:
            row = _card_location_row(db, session_id, int(card_uid), cache)
            if row and _owner_projects_card_properties(
                    db, session_id, row[0], cache):
                native, _unsupported = _scan_static_deltas(
                    db, session_id, state, int(card_uid), cache)
                props = native.get("card_properties", {}).get(
                    int(card_uid), {}) or {}
    finally:
        if previous is None:
            state.pop(marker, None)
        else:
            state[marker] = previous
    from pvp_db import (db_card_mutation_field,
                        db_card_template_threshold_subtype)
    row = db_card_template_threshold_subtype(
        session_id, int(card_uid), conn=db)
    thresholds = []
    subtype = ""
    if row:
        from .targeting import _shards
        thresholds = list(_shards(row[0]))
        subtype = str(row[1] or "")
    try:
        buffs = json.loads(db_card_mutation_field(
            session_id, int(card_uid), "permanent_buffs", conn=db) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if isinstance(buffs, dict):
        if isinstance(buffs.get("thresholds"), list):
            thresholds = [int(value) for value in buffs["thresholds"]]
        if isinstance(buffs.get("subtype"), str):
            subtype = buffs["subtype"]
    if props.get("thresholds") is not None:
        thresholds = list(props["thresholds"])
    if props.get("subtype") is not None:
        subtype = str(props["subtype"])
    for value in props.get("subtype_add", ()):
        if value.lower() not in subtype.lower().split():
            subtype = (subtype + " " + value).strip()
    return thresholds, subtype


def _has_continuous_static(db, session_id, card_uid):
    from pvp_db import db_card_ability_payload, db_ability_static_metadata
    payload = db_card_ability_payload(session_id, int(card_uid), conn=db)
    try:
        abilities = json.loads(payload or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    for guid in abilities:
        metadata = db_ability_static_metadata(str(guid).lower(), conn=db)
        if not metadata:
            continue
        if not metadata[0] and not metadata[1]:
            return True
        # Only the authored all-zone CardCreatedEvent convention is
        # continuous.  Triggered abilities with broad collection flags (for
        # example Grave Nibbler) must not be treated as static modifiers.
        raw = str(metadata[2] or "")
        if ("CardCreatedEvent" in str(metadata[0] or "") and
                all(zone in raw for zone in ("Deck", "Hand", "Warzone", "Discard"))):
            return True
    return False


def _instance_buffs(row):
    attack = defense = rage = 0
    attrs = int(row[3] or 0) | int(row[6] or 0) | int(row[9] or 0)
    flags = set()
    for raw in (row[7], row[8]):
        try:
            buffs = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            continue
        attack += int(buffs.get("atk", 0) or 0)
        defense += int(buffs.get("def", 0) or 0)
        rage += int(buffs.get("rage", 0) or 0)
        attrs |= int(buffs.get("attributes", 0) or 0)
        for rule in buffs.get("rule_modifiers", []) or []:
            if not isinstance(rule, dict) or rule.get("property") != "damagemultiplier":
                continue
            if int(rule.get("value", 0) or 0) <= 1:
                continue
            if rule.get("combatdamageonly"):
                flags.add("double_combat_damage")
            elif rule.get("noncombatdamageonly"):
                flags.add("double_noncombat_damage")
            else:
                flags.add("double_damage")
    return attack, defense, attrs, flags, rage


def effective_stats(db, session_id, battle_state, card_uid):
    """Return current (attack, defense, attributes, flags, rage)."""
    static, unsupported = _native_static_deltas(
        db, session_id, battle_state or {}, int(card_uid))
    if unsupported:
        raise RuntimeError(
            f"RulesPort static stats have no native handler for card {card_uid}")
    return _stats_from_deltas(db, session_id, card_uid, static)


def _stats_from_deltas(db, session_id, card_uid, static):
    """Project combat stats from an already-computed native delta view."""
    from pvp_db import db_card_static_row, db_card_combat_state
    try:
        row = db_card_static_row(session_id, int(card_uid), conn=db)
    except Exception:
        # Focused/older schemas predate the optional printed Rage/Lethal
        # columns.  The combat projection contains all baseline fields.
        row = db_card_combat_state(session_id, int(card_uid), conn=db)
        if row is not None:
            row = tuple(row) + (0, 0)
    if not row:
        return 0, 0, 0, set(), 0
    atk, defense, attrs, flags, rage = _instance_buffs(row)
    atk += int(row[0] or 0) + int(row[4] or 0)
    defense += int(row[1] or 0) + int(row[5] or 0) - int(row[2] or 0)
    attrs |= int(row[3] or 0) | int(row[6] or 0)
    atk += int(static["atk"])
    defense += int(static["def"])
    attrs |= int(static["attrs"])
    flags |= set(static["flags"])
    rage += int(static["rage"])
    rage += int(row[10] or 0)
    if row[11]:
        flags.add("lethal")
    return atk, max(0, defense), attrs, flags, rage


def effective_attributes(db, session_id, battle_state, card_uid):
    return effective_stats(db, session_id, battle_state, card_uid)[2]


def effective_deltas(db, session_id, battle_state, card_uid):
    native, unsupported = _native_static_deltas(
        db, session_id, battle_state or {}, int(card_uid))
    if unsupported:
        raise RuntimeError(
            f"RulesPort static deltas have no native handler for card {card_uid}")
    return native


def effective_cost(db, session_id, battle_state, card_uid):
    native, unsupported = _native_static_deltas(
        db, session_id, battle_state or {}, int(card_uid))
    if unsupported:
        raise RuntimeError(
            f"RulesPort static cost has no native handler for card {card_uid}")
    return _cost_from_deltas(db, session_id, card_uid, native)


def _cost_from_deltas(db, session_id, card_uid, native):
    """Project an effective cost from an already-computed native delta view."""
    from pvp_db import db_card_cost_location_state
    row = db_card_cost_location_state(session_id, int(card_uid), conn=db)
    if not row:
        return 0
    cost = int(row[0] or 0) + int(row[1] or 0)
    if row[2]:
        try:
            value = json.loads(row[2])
            cost += int(value.get("cost_mod", 0) or 0) if isinstance(value, dict) else 0
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return max(0, cost + int(native["cost_mod"]))


def effective_option_projection(db, session_id, battle_state, card_uid):
    """Return ``(attributes, cost)`` for one hand card from a single scan.

    The returned values are the same projections as :func:`effective_attributes`
    and :func:`effective_cost`; they share this evaluator rather than a parallel
    cost/attribute source.

    Play-option refreshes need both values for every card in hand.  Calling
    :func:`effective_attributes` and :func:`effective_cost` separately repeated
    the native static scan — including its target-filter evaluation — twice per
    card, which dominated the opponent-turn priority window.
    """
    native, unsupported = _native_static_deltas(
        db, session_id, battle_state or {}, int(card_uid))
    if unsupported:
        raise RuntimeError(
            "RulesPort static projection has no native handler for card "
            f"{card_uid}")
    attributes = _stats_from_deltas(db, session_id, card_uid, native)[2]
    return attributes, _cost_from_deltas(db, session_id, card_uid, native)


def controller_flags(db, session_id, battle_state, owner):
    """Return continuous combat flags granted by this controller's troops."""
    from pvp_db import db_warzone_card_uids
    flags = set()
    for (uid,) in db_warzone_card_uids(session_id, owner, conn=db):
        flags |= effective_stats(db, session_id, battle_state, int(uid))[3]
    return flags


def global_flags(db, session_id, battle_state):
    from pvp_db import db_warzone_owner_ids
    flags = set()
    for (owner,) in db_warzone_owner_ids(session_id, conn=db):
        flags |= controller_flags(db, session_id, battle_state, int(owner))
    return flags
