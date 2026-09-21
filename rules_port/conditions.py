"""RulesPort condition boundary for Records trigger and effect predicates.

The condition evaluator is stateful only through the supplied typed context.
This module is the port-owned API used by trigger dispatch; its compatibility
provider is loaded lazily so old non-battle callers can continue to use the
historical module while live RulesPort sessions have one condition boundary.
"""

from __future__ import annotations

import datetime


_UNSUPPORTED = object()


def _last(value):
    return str(value or "").rsplit(".", 1)[-1]


def _side(value):
    return "ai" if not value else "player"


def _compare(value, operation, target):
    return {
        "GreaterThanOrEqual": value >= target,
        "LessThanOrEqual": value <= target,
        "GreaterThan": value > target,
        "LessThan": value < target,
        "Equals": value == target,
    }.get(str(operation or "GreaterThanOrEqual"), True)


def _native_condition(node, ctx):
    """Evaluate condition forms already represented by RulesPort types.

    Returning ``_UNSUPPORTED`` is deliberate: it is an auditable migration
    signal, not a truthy default.  The compatibility provider is consulted
    only for condition families that have not yet been represented here.
    """
    if not isinstance(node, dict):
        return True
    kind = _last(node.get("_t"))
    if kind in ("AndTriggerCondition", "AndEffectCondition",
                "AndAbilityCondition"):
        values = [_native_condition(child, ctx)
                  for child in (node.get("m_Conditions") or [])]
        if any(value is _UNSUPPORTED for value in values):
            return _UNSUPPORTED
        return all(values)
    if kind in ("OrTriggerCondition", "OrEffectCondition",
                "OrAbilityCondition"):
        values = [_native_condition(child, ctx)
                  for child in (node.get("m_Conditions") or [])]
        if any(value is _UNSUPPORTED for value in values):
            return _UNSUPPORTED
        return any(values)
    if kind in ("NotTriggerCondition", "NotEffectCondition", "NotAbilityCondition"):
        child = node.get("m_Condition")
        if child is None:
            children = node.get("m_Conditions") or []
            child = children[0] if children else None
        value = _native_condition(child, ctx)
        return _UNSUPPORTED if value is _UNSUPPORTED else not value

    if kind == "TriggerCardIsAbilitySource":
        return (ctx.trigger_uid is not None and
                int(ctx.trigger_uid) == int(ctx.ability_source_uid or 0))
    if kind == "TriggerPlayerControlsAbilitySource":
        card = ctx.card(ctx.ability_source_uid)
        if card is None:
            return True
        trigger_owner = (ctx.trigger_owner_id
                         if ctx.trigger_owner_id is not None
                         else ctx.ability_source_owner_id)
        if ctx.bstate.get("pvp"):
            return int(card.get("user_id", 0) or 0) == int(trigger_owner or 0)
        return _side(card.get("user_id")) == _side(trigger_owner)
    if kind == "TriggerPlayerControlsCard":
        card = ctx.card(ctx.trigger_uid)
        return (True if card is None else
                _side(card.get("user_id")) ==
                _side(ctx.ability_source_owner_id))
    if kind == "TriggerPlayerControlsTarget":
        card = ctx.card(ctx.extra_target)
        return (True if card is None else
                _side(card.get("user_id")) ==
                _side(ctx.ability_source_owner_id))
    if kind == "TriggerPlayerIsActivePlayer":
        return ctx.bstate.get("turn_player") == _side(
            ctx.ability_source_owner_id)
    if kind == "TriggerEventIsCombatDamage":
        event = _last(ctx.event_type)
        return event == "CardDealtDamageEvent" or (
            event == "CardWouldDealDamageEvent" and
            bool((ctx.event_tac or {}).get("is_combat_damage")))
    if kind == "TriggerEventIntAttribute":
        return (_last(ctx.event_type) == "CardGainedIntAttrEvent" and
                str(node.get("m_Attribute") or "") == str(
                    ctx.event_int_attribute or ""))
    if kind == "TriggerAbilityIsChargePower":
        activated = ctx.bstate.get("activated_ability_guid")
        if not activated:
            return False
        from pvp_db import db_charge_ability_cost
        return int(db_charge_ability_cost(activated, conn=ctx.db) or 0) > 0
    if kind == "TriggerAbilityIsSpellPower":
        activated = ctx.bstate.get("activated_ability_guid")
        if not activated:
            return False
        from pvp_db import db_champion_ability_costs
        costs = db_champion_ability_costs(activated, conn=ctx.db)
        return bool(costs and int(costs[1] or 0) > 0)
    if kind == "TriggerAbilityHasSpellPointCostModifier":
        return int(ctx.bstate.get("spell_points_cost_modifier", 0) or 0) != 0
    if kind == "TriggerAbilityHasUsesPerGameValue":
        used = int((ctx.bstate.get("ability_uses") or {}).get(
            str(ctx.bstate.get("activated_ability_guid") or ""), 0) or 0)
        return used < int(node.get("m_UsesPerGame", node.get("m_Value", 1)) or 1)
    if kind == "TriggerCardIsNthCardDrawnThisTurnByThisPlayer":
        side = _side(ctx.ability_source_owner_id)
        return int(ctx.bstate.get(f"{side}_draws_this_turn", 0) or 0) == int(
            node.get("m_Nth", 1) or 1)
    if kind == "TurnPhaseCondition":
        wanted = str(node.get("m_TurnPhase") or "")
        current = ctx.bstate.get("phase")
        if current is None:
            from .persistence import current_phase
            current = current_phase(ctx.bstate)
        return str(current) == wanted
    if kind == "CardsDiscardedThisTurn":
        owner = int(ctx.ability_source_owner_id or 0)
        key = (f"cards_discarded_this_turn_{owner}"
               if ctx.bstate.get("pvp") else
               f"{_side(owner)}_cards_discarded_this_turn")
        value = int(ctx.bstate.get(key, 0) or 0)
        required = int(node.get("m_RequiredQuantity", node.get(
            "m_Amount", node.get("m_Value", 1))) or 1)
        return _compare(value, node.get("m_ComparisonOp"), required)
    if kind == "ChampionActionsCastThisTurn":
        side = _side(ctx.ability_source_owner_id)
        value = int(ctx.bstate.get(f"{side}_actions_cast_this_turn", 0) or 0)
        required = int(node.get("m_RequiredQuantity", 1) or 1)
        return _compare(value, node.get("m_ComparisonOp"), required)
    if kind == "TriggerCardCounter":
        card = ctx.card(ctx.ability_source_uid)
        if card is None:
            return True
        guid = node.get("m_CardCounterTemplateId")
        guid = guid.get("m_Guid") if isinstance(guid, dict) else guid
        counter_count = getattr(ctx, "_counter_count", None)
        if not callable(counter_count):
            return _UNSUPPORTED
        return counter_count(card, str(guid or "").lower()) >= int(
            node.get("m_RequiredCount", 1) or 1)
    if kind == "TriggerPlayerHealth":
        side = _side(ctx.ability_source_owner_id)
        value = int(ctx.bstate.get(f"{side}_health", 20) or 0)
        required = int(node.get("m_Health", 0) or 0)
        return _compare(value, node.get("m_ComparisonOp"), required)
    if kind in ("AbilityControllerIsActiveAbilityCondition",
                "AbilityControllerHasPriorityAbilityCondition"):
        return ctx.bstate.get("turn_player") == _side(
            ctx.ability_source_owner_id)
    if kind == "AbilityControllerIsStartingPlayerAbilityCondition":
        return ctx.bstate.get("starting_player") == _side(
            ctx.ability_source_owner_id)
    if kind == "RequiresEncounterGameboard":
        return bool(ctx.bstate.get("encounter_gameboard", True))
    if kind == "AbilityControllerHasThresholdAbilityCondition":
        color = str(node.get("m_ColorFlags", "") or "").lower()
        required = int(node.get("m_RequiredQuantity", 1) or 1)
        import game_engine
        flag = int(game_engine.SHARD_TO_FLAG.get(color, 0) or 0)
        if not flag:
            return True
        owner = int(ctx.ability_source_owner_id or 0)
        side = _side(owner)
        thresholds = (ctx.bstate.get(f"thresh_{owner}", {}) or {}
                      if ctx.bstate.get("pvp") else
                      ctx.bstate.get(f"{side}_threshold", {}) or {})
        return int(thresholds.get(flag, thresholds.get(str(flag), 0)) or 0) >= required
    if kind == "SourceCardHasCounters":
        card = ctx.card(ctx.ability_source_uid)
        if card is None:
            return False
        guid = node.get("m_CardCounterTemplateId")
        guid = guid.get("m_Guid") if isinstance(guid, dict) else guid
        counter_count = getattr(ctx, "_counter_count", None)
        if not callable(counter_count):
            return _UNSUPPORTED
        value = counter_count(card, str(guid or "").lower())
        return _compare(value, node.get("m_ComparisonOp"), int(
            node.get("m_RequiredCounters", 1) or 1))
    if kind == "TriggerCardIsStoredTargetOfAbilitySource":
        if ctx.trigger_uid is None:
            return False
        stored = [uid for values in (ctx.bstate.get("stored_targets") or {}).values()
                  for uid in values]
        return int(ctx.trigger_uid) in {int(uid) for uid in stored}
    if kind == "TriggerCardSameNameInZone":
        card = ctx.card(ctx.ability_source_uid)
        if card is None or not card.get("template_guid"):
            return True
        zones = ctx._zones(node.get("m_Collection", "") or
                           node.get("m_CollectionFlags", ""))
        if not zones:
            return True
        from pvp_db import db_template_in_zones
        return db_template_in_zones(
            ctx.session.session_id, card["template_guid"], zones, conn=ctx.db)
    if kind == "TriggerCardMatchesFilter":
        test = node.get("m_TriggerTest") or "TriggerSource"
        uid = (ctx.extra_target if test == "TriggerTarget" and
               ctx.extra_target is not None else ctx.trigger_uid)
        card = ctx.card(uid)
        if card is None:
            return True
        from rules_port.filters import records_filter_matches
        return records_filter_matches(
            card, node.get("m_CardFilter") or {},
            source=ctx.card(ctx.ability_source_uid), context=ctx)
    if kind == "IntAttrFilter":
        attr = str(node.get("m_Attribute") or "")
        if attr.startswith("AbilityTAC>"):
            from rules_port.tac import _tac_attr_hash
            actual = int((ctx.event_tac or {}).get(
                _tac_attr_hash(attr.split(">", 1)[1]), 0) or 0)
            return _compare(actual, node.get("m_ComparisonOp"), int(
                node.get("m_Value", 0) or 0))
        card = ctx.card(ctx.trigger_uid) or ctx.card(ctx.ability_source_uid)
        if card is None:
            return True
        from rules_port.filters import records_filter_matches
        return records_filter_matches(card, node, context=ctx)
    if kind == "CardFilterAbilityCondition":
        source = ctx.card(ctx.ability_source_uid)
        if source is None:
            return True
        from rules_port.filters import records_filter_matches
        return records_filter_matches(
            source, node.get("m_CardFilter") or {}, source=source,
            context=ctx)
    if kind == "RequiresSourcePassesFilterCondition":
        source = ctx.card(ctx.ability_source_uid)
        if source is None:
            return True
        from rules_port.filters import records_filter_matches
        return records_filter_matches(source, node.get("m_Filter") or {},
                                      source=source, context=ctx)
    if kind == "RequiresTargetPassesFilterCondition":
        target_uid = ctx.extra_target
        if target_uid is None:
            stored = ((ctx.bstate.get("stored_targets") or {}).get(
                ctx.bstate.get("resolving_ability")) or [])
            target_uid = stored[-1] if stored else None
        target = ctx.card(target_uid) if target_uid is not None else None
        if target is None:
            return False
        source = ctx.card(ctx.ability_source_uid)
        return records_filter_matches(
            target, node.get("m_Filter") or {}, source=source, context=ctx)
    if kind in ("AbilityControllerIsActiveAbilityCondition",
                "AbilityControllerHasPriorityAbilityCondition"):
        return ctx.bstate.get("turn_player") == _side(
            ctx.ability_source_owner_id)
    if kind == "AbilityControllerHasThresholdAbilityCondition":
        import game_engine
        color = str(node.get("m_ColorFlags", "") or "").lower()
        flag = int(game_engine.SHARD_TO_FLAG.get(color, 0) or 0)
        if not flag:
            return True
        owner = int(ctx.ability_source_owner_id or 0)
        side = _side(owner)
        thresholds = (ctx.bstate.get(f"thresh_{owner}", {}) or {}
                      if ctx.bstate.get("pvp") else
                      ctx.bstate.get(f"{side}_threshold", {}) or {})
        return int(thresholds.get(flag, thresholds.get(str(flag), 0)) or 0) >= int(
            node.get("m_RequiredQuantity", 1) or 1)
    if kind == "RequiresChampionHealth":
        owner = int(ctx.ability_source_owner_id or 0)
        health = getattr(ctx, "_champion_health", None)
        if not callable(health):
            side = _side(owner)
            value = int(ctx.bstate.get(f"{side}_health", 20) or 0)
        else:
            value = int(health(owner))
        required = int(node.get("m_RequiredQuantity", 0) or 0)
        return _compare(value, node.get("m_ComparisonOp"), required)
    if kind == "RequiresChampionCharges":
        side = _side(ctx.ability_source_owner_id)
        return _compare(
            int(ctx.bstate.get(f"{side}_charges", 0) or 0),
            node.get("m_ComparisonOp"),
            int(node.get("m_RequiredQuantity", 0) or 0))
    if kind == "RequiresTotalResources":
        side = _side(ctx.ability_source_owner_id)
        return _compare(
            int(ctx.bstate.get(f"{side}_total_resources", 0) or 0),
            node.get("m_ComparisonOp"),
            int(node.get("m_RequiredQuantity", node.get("m_Value", 0)) or 0))
    if kind == "RequiresResourceThreshold":
        import game_engine
        owner = int(ctx.ability_source_owner_id or 0)
        side = _side(owner)
        thresholds = (ctx.bstate.get(f"thresh_{owner}", {}) or {}
                      if ctx.bstate.get("pvp") else
                      ctx.bstate.get(f"{side}_threshold", {}) or {})
        colors = node.get("m_ColorFlags", node.get("m_Color", ""))
        if isinstance(colors, str):
            colors = [colors]
        values = []
        for color in colors or ():
            flag = game_engine.SHARD_TO_FLAG.get(
                str(color).rsplit(".", 1)[-1].lower(), 0)
            if flag:
                values.append(int(thresholds.get(flag,
                    thresholds.get(str(flag), 0)) or 0))
        required = int(node.get("m_RequiredQuantity", 1) or 1)
        return all(value >= required for value in values) if values else True
    if kind == "RequiresDateTime":
        now = datetime.datetime.now()
        values = {"m_Year": now.year, "m_Month": now.month,
                  "m_Day": now.day, "m_DayOfWeek": (now.weekday() + 1) % 7,
                  "m_DayOfYear": now.timetuple().tm_yday,
                  "m_Hour": now.hour, "m_Minute": now.minute,
                  "m_Second": now.second}
        op = node.get("m_ComparisonOp")
        for field, actual in values.items():
            try:
                expected = int(node.get(field, -1))
            except (TypeError, ValueError):
                expected = -1
            if expected >= 0 and not _compare(actual, op, expected):
                return False
        return True
    if kind == "TACTriggerCondition":
        serialized = node.get("m_Conditions") or {}
        data = serialized.get("data") if isinstance(serialized, dict) else None
        if not data:
            return True
        from rules_port.tac import decode_tac_tree, _tac_attr_hash
        try:
            required = decode_tac_tree(data)
        except (TypeError, ValueError):
            return True
        actual = dict(ctx.event_tac or ctx.bstate.get("event_tac") or {})
        conditions = required.get(_tac_attr_hash("Conditions")) or [required]
        minimum = _tac_attr_hash("MinimumValues")
        subset = _tac_attr_hash("HasAsSubset")
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            for key, value in (condition.get(minimum) or {}).items():
                if int(actual.get(key, 0) or 0) < int(value or 0):
                    return False
            for key, value in (condition.get(subset) or {}).items():
                if isinstance(value, dict):
                    nested = actual.get(key)
                    if not isinstance(nested, dict) or any(
                            nested.get(k) != v for k, v in value.items()):
                        return False
                elif actual.get(key) != value:
                    return False
        return True
    if kind == "RequiresCardsControlled":
        zones = ctx._zones(node.get("m_CardCollection", ""))
        if not zones:
            return True
        required = int(node.get("m_RequiredQuantity", 1) or 1)
        player_filter = node.get("m_PlayerFilter") or "Self"
        owner = int(ctx.ability_source_owner_id or 0)
        source_side = _side(owner)
        count = 0
        from rules_port.filters import records_filter_matches
        for card in ctx._cards_in_zones(zones):
            card_owner = int(card.get("user_id", 0) or 0)
            if player_filter in ("Self", "You", "Controller"):
                if ((ctx.bstate.get("pvp") and card_owner != owner) or
                        (not ctx.bstate.get("pvp") and
                         _side(card_owner) != source_side)):
                    continue
            elif player_filter in ("Opposing", "Opponents", "MultipleOpponents"):
                if ((ctx.bstate.get("pvp") and card_owner == owner) or
                        (not ctx.bstate.get("pvp") and
                         _side(card_owner) == source_side)):
                    continue
            if records_filter_matches(card, node.get("m_CardFilter") or {},
                                      context=ctx):
                count += 1
        return _compare(count, node.get("m_ComparisonOp"), required)
    if kind == "AbilityVariableCondition":
        variables = getattr(ctx, "ability_variables", {}) or {}
        lhs, rhs = str(node.get("m_Lhs") or ""), str(node.get("m_Rhs") or "")
        if lhs not in variables:
            return False
        try:
            left_value = int(variables[lhs])
            right_value = int(rhs)
        except (TypeError, ValueError):
            if rhs not in variables:
                return False
            try:
                right_value = int(variables[rhs])
            except (TypeError, ValueError):
                return False
        return _compare(left_value, node.get("m_ComparisonOp"), right_value)
    if kind in ("NotContingentAbilityCondition", "NotContingentEffectCondition"):
        index = node.get("m_EffectIndex")
        if index is None:
            return True
        try:
            return not bool((getattr(ctx, "applied_effects", {}) or {}).get(
                int(index), False))
        except (TypeError, ValueError):
            return True
    if kind == "TriggerCardEnteredZone":
        card = ctx.card(ctx.trigger_uid)
        if card is None:
            return True
        destination = ctx.event_destination_collection or card.get("location")
        source = ctx.event_source_collection
        zones = ctx._zones(node.get("m_DestinationCollection", ""))
        if zones and destination not in zones:
            return False
        source_zones = ctx._zones(node.get("m_SourceCollection", ""))
        if source_zones and source is not None and source not in source_zones:
            return False
        owner = _side(card.get("user_id"))
        source_owner = _side(ctx.ability_source_owner_id)
        if int(node.get("m_Your", 0) or 0):
            # C# ``TriggerCardEnteredZone.IsValid``: a "friendly zone" entry
            # requires BOTH the card's current controller and the controller it
            # had before the move to be the ability source's controller.
            # Checking only the current controller made an opposing champion
            # react to a card that came from the other player's zone: Mentor of
            # the Grave's "when a troop enters your hand from your crypt" fired
            # for a troop pulled out of the opponent's crypt, because the
            # control-transferring move had already rewritten the card's owner.
            if owner != source_owner:
                return False
            previous = getattr(ctx, "event_previous_owner_id", None)
            if previous is not None and _side(previous) != source_owner:
                return False
        if int(node.get("m_Opposing", 0) or 0) and owner == source_owner:
            return False
        return True

    if kind == "ChampionCardsCastCondition":
        # Game.Shared reads PlayerStatsThisTurn or PlayerGameStats from the
        # controlling champion.  The port keeps the same three typed counters
        # in the battle checkpoint, keyed by owner in PvP.
        from .cast_stats import _side as cast_stats_side
        owner = int(ctx.ability_source_owner_id or 0)
        side = cast_stats_side(owner, ctx.bstate)
        this_turn = bool(node.get("m_ThisTurn", 0))
        suffix = "_this_turn" if this_turn else ""
        if node.get("m_Resource"):
            field = f"{side}_resource_cards_cast{suffix}"
        elif node.get("m_NonResource"):
            field = f"{side}_nonresource_cards_cast{suffix}"
        else:
            field = f"{side}_cards_cast{suffix}"
        actual = int(ctx.bstate.get(field, 0) or 0)
        return _compare(actual, node.get("m_ComparisonOp"), int(
            node.get("m_RequiredQuantity", 0) or 0))

    if kind == "SourceCardHasKeywords":
        card = ctx.card(ctx.ability_source_uid)
        if card is None:
            return True
        attributes = int(card.get("attributes", 0) or 0)
        keyword_count = attributes.bit_count()
        int_attrs = card.get("int_attrs") or {}
        # CardRepresentation exposes Rage and Lethal through its dynamic
        # context in addition to the ordinary attribute bit field.
        keyword_count += int(int_attrs.get("Rage", int_attrs.get("rage", 0)) or 0) > 0
        keyword_count += bool(int_attrs.get("Lethal", int_attrs.get("lethal", 0)))
        return _compare(keyword_count, node.get("m_ComparisonOp"), int(
            node.get("m_RequiredQuantity", 0) or 0))

    return _UNSUPPORTED


def _provider():
    from . import condition_context
    return condition_context


class PortConditionContext:
    """RulesPort name for the Records-backed typed condition context."""

    def __new__(cls, *args, **kwargs):
        return _provider().ConditionContext(*args, **kwargs)


def evaluate_condition(node, context):
    result = _native_condition(node, context)
    if result is not _UNSUPPORTED:
        return result
    # Native condition evaluation is the default for every mode.  A legacy
    # evaluator is an explicit rollback seam only; otherwise Practice/PvE
    # could silently diverge from PvP merely because attachment metadata was
    # missing from a reconstructed session wrapper.
    if not (getattr(context, "bstate", None) or {}).get(
            "_rules_port_allow_legacy_backend"):
        kind = _last(node.get("_t")) if isinstance(node, dict) else "<invalid>"
        raise RuntimeError(
            "RulesPort condition has no native handler: " + kind)
    return _provider().evaluate_condition(node, context)


def evaluate_effect_condition(db, condition_id, context):
    if not condition_id:
        return True
    try:
        from pvp_db import db_effect_condition_json
        import json
        raw = db_effect_condition_json(condition_id, conn=context.db)
        node = json.loads(raw) if raw else None
    except Exception:
        node = None
    if not node:
        return True
    return evaluate_condition(node, context)


def trigger_condition_met(raw_json, context):
    if not raw_json:
        return True
    if isinstance(raw_json, dict):
        record = raw_json
    else:
        try:
            import json
            record = json.loads(raw_json)
        except Exception:
            return True
    if not isinstance(record, dict):
        return True
    for key in ("m_AbilityCondition", "m_TriggerCondition"):
        condition = record.get(key)
        if isinstance(condition, dict) and not evaluate_condition(
                condition, context):
            return False
    return True


# The short name makes migration call sites read like the C# contract while
# keeping the concrete context constructor compatible with existing adapters.
ConditionContext = PortConditionContext

__all__ = ["ConditionContext", "PortConditionContext",
           "evaluate_condition", "evaluate_effect_condition",
           "trigger_condition_met"]
