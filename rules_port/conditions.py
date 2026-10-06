"""RulesPort condition boundary for Records trigger and effect predicates.

The condition evaluator is stateful only through the supplied typed context.
Unsupported Records condition forms fail closed until they have a native
RulesPort implementation.
"""

from __future__ import annotations

import datetime
from typing import Any, cast


_UNSUPPORTED = object()


def _last(value):
    return str(value or "").rsplit(".", 1)[-1]


def _record_filter_zones(node):
    """Return explicit zone predicates from a Records card-filter tree."""
    if isinstance(node, dict):
        zones = set()
        if _last(node.get("_t")) == "InZone" and node.get("m_Collection"):
            zones.add(str(node["m_Collection"]).lower())
        for child in node.values():
            zones.update(_record_filter_zones(child))
        return zones
    if isinstance(node, list):
        zones = set()
        for child in node:
            zones.update(_record_filter_zones(child))
        return zones
    return set()


def _side(value):
    return "ai" if not value else "player"


def _participant_int(state, owner, raw_prefix, side_suffix, default=0):
    """Read one participant's integer from a PvP checkpoint/view or PvE."""
    from .runtime_helpers import participant_value
    try:
        return int(participant_value(
            state, owner, raw_prefix, side_suffix, default) or 0)
    except (TypeError, ValueError):
        return int(default or 0)


def _participant_thresholds(state, owner):
    from .runtime_helpers import participant_value
    value = participant_value(state, owner, "thresh", "threshold", {})
    return value if isinstance(value, dict) else {}


def _is_active(ctx, owner):
    from .runtime_helpers import is_active_participant
    return is_active_participant(ctx.bstate or {}, owner)


def _player_resources(state, owner, *, temporary=False):
    """Read the C# Player resource pool from either PvP or PvE state."""
    if temporary:
        return _participant_int(state, owner, "res", "resources")
    return _participant_int(state, owner, "res_total", "total_resources")


def _owner_is(ctx, owner, expected) -> bool:
    """Client ownership comparison for trigger control conditions.

    ``_side`` separates the PvE player (nonzero owner) from the AI (owner 0),
    but tournament PvP has two nonzero participant ids, so every comparison
    degenerated to ``"player" == "player"``.  ``TriggerPlayerControlsTarget``
    then matched the *opponent's* cards: Spitfire Elemental's "when you are
    dealt damage" fired on opposing champion damage and chained 1 damage onto
    the enemy champion until the resolver cap.  Compare raw participant ids in
    PvP, matching the client's ``TriggerPlayerControlsTarget.IsValid``
    (``sourceCard.Controller == sessionEvent.TargetPlayerId``).
    """
    if (getattr(ctx, "bstate", None) or {}).get("pvp"):
        try:
            return int(owner or 0) == int(expected or 0)
        except (TypeError, ValueError):
            return False
    return _side(owner) == _side(expected)


def _compare(value, operation, target):
    return {
        "GreaterThanOrEqual": value >= target,
        "LessThanOrEqual": value <= target,
        "GreaterThan": value > target,
        "LessThan": value < target,
        "Equals": value == target,
        "NotEqual": value != target,
        "OneMoreThan": value == target + 1,
        "TwoMoreThan": value == target + 2,
        "OneLessThan": value == target - 1,
    }.get(str(operation or "GreaterThanOrEqual"), True)


def _same_player(ctx, left, right):
    if (getattr(ctx, "bstate", None) or {}).get("pvp"):
        try:
            return int(left or 0) == int(right or 0)
        except (TypeError, ValueError):
            return False
    return _side(left) == _side(right)


def _different_races(card, faction):
    """Yield the C# ERace tool names on a card for one EFactions value."""
    faction = _last(faction).lower()
    races_by_faction = {
        "aria": ("Human", "Elf", "Coyotle", "Orc"),
        "underworld": ("Dwarf", "Shin'hare", "Vennen", "Necrotic"),
    }
    races = races_by_faction.get(faction, ())
    if not races:
        return ()
    tokens = set(str(card.get("subtype") or "").lower().split())
    return tuple(race for race in races if race.lower() in tokens)


def _requires_cards_count(ctx, source, owner, player_filter, zones,
                          same_owner, target_owners, card_filter, node):
    """Count cards with the same filters and distinctness rules as C#.

    ``RequiresCardsControlled.GetFilteredCardCount`` applies its collection,
    player, SameOwner, and distinct-cost/name/race fields both to the primary
    card filter and to ``m_QuantityCardFilter``.
    """
    from rules_port.filters import records_filter_matches

    wanted_player = _last(player_filter).lower()
    count = 0
    different_costs = bool(node.get("m_OnlyIncludeDifferentCosts", 0))
    different_names = bool(node.get("m_OnlyIncludeDifferentNames", 0))
    different_races = node.get("m_OnlyIncludeDifferentRacesForFaction")
    seen_costs = set()
    seen_names = set()
    seen_races = set()
    for card in ctx._cards_in_zones(zones):
        card_owner = card.get("controller_id", card.get("user_id", 0))
        if same_owner and int(card_owner or 0) not in target_owners:
            continue
        if wanted_player in {"self", "you", "controller"}:
            if not _same_player(ctx, card_owner, owner):
                continue
        elif wanted_player in {
                "opposing", "opponent", "opponents", "singleopponent",
                "multipleopponents"}:
            if _same_player(ctx, card_owner, owner):
                continue
        elif wanted_player == "singleplayer":
            # The original switch deliberately counts no player for this enum.
            continue
        if (card_filter is not None and
                not records_filter_matches(
                    card, card_filter, source=source, context=ctx)):
            continue
        if different_costs:
            try:
                value = int(card.get("cost", 0) or 0)
            except (TypeError, ValueError):
                value = 0
            if value in seen_costs:
                continue
            seen_costs.add(value)
            count += 1
        elif different_names:
            value = str(card.get("name") or "")
            if value in seen_names:
                continue
            seen_names.add(value)
            count += 1
        elif _last(different_races).lower() not in {"", "unknown"}:
            for race in _different_races(card, different_races):
                if race not in seen_races:
                    seen_races.add(race)
                    count += 1
        else:
            count += 1
    return count


def _event_tac(ctx):
    """Return the typed event TAC visible to authored condition leaves."""
    actual = dict(ctx.event_tac or ctx.bstate.get("event_tac") or {})
    # The client serializes GainThresholdEvent's colour as an event TAC
    # IntAttr. Native host callers expose the typed colour separately as
    # ``gain_threshold_color``; reconstruct the same event-local TAC here so
    # all resource paths evaluate authored conditions identically.
    if _last(ctx.event_type) != "GainThresholdEvent":
        return actual
    color = (ctx.bstate or {}).get("gain_threshold_color")
    try:
        color = int(color) if color is not None else None
    except (TypeError, ValueError):
        color = None
    if color is None:
        return actual
    import game_engine
    from rules_port.tac import _tac_attr_hash
    for name, flag in game_engine.SHARD_TO_FLAG.items():
        if int(flag) == color:
            actual[_tac_attr_hash(str(name).title())] = 1
            break
    return actual


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
            return False
        selector = _last(node.get("m_TriggerTest") or "TriggerSource").lower()
        if selector == "triggersource":
            trigger_owner = ctx.trigger_owner_id
        elif selector == "triggertarget":
            trigger_owner = ctx.event_target_owner_id
        else:
            # TriggerCondition.GetTriggerPlayer has no TriggerSourcePlayer
            # branch and returns null for that selector; the C# leaf rejects
            # a null trigger player.
            return False
        if trigger_owner is None:
            return False
        return _owner_is(ctx, card.get("user_id"), trigger_owner)
    if kind == "TriggerPlayerControlsCard":
        card = ctx.card(ctx.trigger_uid)
        return (True if card is None else
                _owner_is(ctx, card.get("user_id"),
                          ctx.ability_source_owner_id))
    if kind == "TriggerPlayerControlsTarget":
        # C# compares the ability source controller to the event's
        # TargetPlayerId. The target card itself is not authoritative here:
        # several events carry a target player without a target card, and a
        # card's controller can differ from the event participant.
        target_owner = ctx.event_target_owner_id
        if target_owner is None:
            return False
        return _owner_is(
            ctx, ctx.ability_source_owner_id, target_owner)
    if kind == "TriggerPlayerIsActivePlayer":
        return _is_active(ctx, ctx.ability_source_owner_id)
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
        if _last(ctx.event_type) != "CardDrawnEvent":
            return False
        try:
            drawn_index = int((ctx.event_data or {}).get(
                "nth_card_drawn_this_turn_by_this_player", 0) or 0)
            required_index = int(node.get("m_Nth", 1) or 1)
        except (TypeError, ValueError):
            return False
        return drawn_index == required_index
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
        # C# reads PlayerStatsThisTurn.ActionsCast. The port keeps the count
        # with the turn it was written in; a count from an earlier turn is 0.
        from .runtime_helpers import participant_turn_counter_key
        owner = ctx.ability_source_owner_id
        state = ctx.bstate or {}
        value = int(state.get(participant_turn_counter_key(
            state, owner, "actions_cast_this_turn"), 0) or 0)
        written_turn = state.get(participant_turn_counter_key(
            state, owner, "actions_cast_turn"))
        if (written_turn is not None and
                int(written_turn) != int(state.get("turn_number", 1) or 1)):
            value = 0
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
        try:
            count = int(cast(Any, counter_count(
                card, str(guid or "").lower())))
        except (TypeError, ValueError):
            return _UNSUPPORTED
        return count >= int(node.get("m_RequiredCount", 1) or 1)
    if kind == "TriggerPlayerHealth":
        value = _participant_int(
            ctx.bstate, ctx.ability_source_owner_id, "hp", "health", 20)
        required = int(node.get("m_Health", 0) or 0)
        return _compare(value, node.get("m_ComparisonOp"), required)
    if kind in ("AbilityControllerIsActiveAbilityCondition",
                "AbilityControllerHasPriorityAbilityCondition"):
        return _is_active(ctx, ctx.ability_source_owner_id)
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
        thresholds = _participant_thresholds(ctx.bstate, owner)
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
        required_raw = node.get("m_RequiredCounters")
        required = int(1 if required_raw is None else required_raw)
        return _compare(value, node.get("m_ComparisonOp"), required)
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
            actual = int(_event_tac(ctx).get(
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
        from rules_port.filters import records_filter_matches
        return records_filter_matches(
            target, node.get("m_Filter") or {}, source=source, context=ctx)
    if kind in ("AbilityControllerIsActiveAbilityCondition",
                "AbilityControllerHasPriorityAbilityCondition"):
        return _is_active(ctx, ctx.ability_source_owner_id)
    if kind == "AbilityControllerHasThresholdAbilityCondition":
        import game_engine
        color = str(node.get("m_ColorFlags", "") or "").lower()
        flag = int(game_engine.SHARD_TO_FLAG.get(color, 0) or 0)
        if not flag:
            return True
        owner = int(ctx.ability_source_owner_id or 0)
        thresholds = _participant_thresholds(ctx.bstate, owner)
        return int(thresholds.get(flag, thresholds.get(str(flag), 0)) or 0) >= int(
            node.get("m_RequiredQuantity", 1) or 1)
    if kind == "RequiresChampionHealth":
        owner = int(ctx.ability_source_owner_id or 0)
        health = getattr(ctx, "_champion_health", None)
        if not callable(health):
            value = _participant_int(ctx.bstate, owner, "hp", "health", 20)
        else:
            value = int(cast(Any, health(owner)) or 0)
        required = int(node.get("m_RequiredQuantity", 0) or 0)
        return _compare(value, node.get("m_ComparisonOp"), required)
    if kind == "RequiresChampionCharges":
        return _compare(
            _participant_int(
                ctx.bstate, ctx.ability_source_owner_id, "chg", "charges"),
            node.get("m_ComparisonOp"),
            int(node.get("m_RequiredQuantity", 0) or 0))
    if kind == "RequiresTotalResources":
        state = getattr(ctx, "bstate", {}) or {}
        source_owner = int(ctx.ability_source_owner_id or 0)
        player_filter = _last(node.get("m_PlayerFilter") or "Self").lower()
        participants = list(state.get("pids") or ())
        if not participants:
            participants = list((state.get("champ_map") or {}).keys())
        try:
            participants = list(dict.fromkeys(int(pid) for pid in participants))
        except (TypeError, ValueError):
            participants = []
        if not participants:
            participants = [source_owner,
                            0 if source_owner else 1]
        opponents = [pid for pid in participants if pid != source_owner]
        quantity = int(node.get(
            "m_RequiredQuantity", node.get("m_Value", 0)) or 0)
        temporary = bool(node.get("m_UseTemporaryResources"))
        if node.get("m_QuantityIsHighestOpposingChampionsMatch"):
            quantity = max((_player_resources(
                state, pid, temporary=temporary) for pid in opponents),
                default=0)
        operation = node.get("m_ComparisonOp")
        if player_filter in {"self", "you", "controller"}:
            return _compare(_player_resources(
                state, source_owner, temporary=temporary), operation,
                quantity)
        if player_filter in {"singleopponent", "singleplayer"}:
            return any(_compare(_player_resources(
                state, pid, temporary=temporary), operation, quantity)
                       for pid in opponents)
        if player_filter in {"multipleopponents", "multipleplayers"}:
            return all(_compare(_player_resources(
                state, pid, temporary=temporary), operation, quantity)
                       for pid in opponents)
        return False
    if kind == "RequiresResourceThreshold":
        import game_engine
        owner = int(ctx.ability_source_owner_id or 0)
        thresholds = _participant_thresholds(ctx.bstate, owner)
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
        actual = _event_tac(ctx)
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
        # Zero is an authored quantity, not a missing value.  In particular,
        # conditions such as "you control no Candlekin" serialize as
        # RequiredQuantity=0 and C# compares the filtered count against zero.
        required = int(node.get("m_RequiredQuantity", 0) or 0)
        player_filter = str(
            node.get("m_PlayerFilter") or "Self").rsplit(".", 1)[-1].lower()
        owner = int(ctx.ability_source_owner_id or 0)
        source = ctx.card(ctx.ability_source_uid)
        if source is None and ctx.ability_source_uid is not None:
            source = {"card_uid": int(ctx.ability_source_uid)}
        if source is None:
            return False
        same_owner = bool(node.get("m_SameOwner", 0))
        pregame = (
            str(getattr(ctx, "event_type", None) or "").rsplit(".", 1)[-1]
            == "PreGameEvent" or
            str((ctx.bstate or {}).get("event_type") or "").rsplit(".", 1)[-1]
            == "PreGameEvent")
        quantity_filter = node.get("m_QuantityCardFilter")
        distinct_count = bool(
            node.get("m_OnlyIncludeDifferentCosts", 0) or
            node.get("m_OnlyIncludeDifferentNames", 0) or
            _last(node.get("m_OnlyIncludeDifferentRacesForFaction"))
            .lower() not in {"", "unknown"})
        if (pregame and not same_owner and player_filter in
                ("self", "you", "controller") and
                _record_filter_zones(node.get("m_CardFilter") or {}) ==
                {"deck"} and quantity_filter is None and
                not distinct_count and
                not node.get("m_QuantityIsHighestOpposingChampionsMatch") and
                not node.get("m_QuantityIsLowestOpposingChampionsMatch")):
            snapshots = (ctx.bstate or {}).get(
                "pregame_initial_deck_counts") or {}
            snapshot = snapshots.get(str(owner))
            if snapshot is None:
                snapshot = snapshots.get(owner)
            if snapshot is not None:
                return _compare(
                    int(snapshot), node.get("m_ComparisonOp"), required)
        target_owners = set()
        if same_owner:
            for uid in getattr(ctx, "effect_secondary_target_uids", ()) or ():
                target = ctx.card(uid)
                if target is None:
                    continue
                target_owner = target.get(
                    "controller_id", target.get("user_id"))
                if target_owner is not None:
                    target_owners.add(int(target_owner))
            # C# RequiresCardsControlled returns false when SameOwner has no
            # effect mapping/target to bind. It must not silently count cards
            # from every player in that case.
            if not target_owners:
                return False
        quantity = required
        if (node.get("m_QuantityIsHighestOpposingChampionsMatch") or
                node.get("m_QuantityIsLowestOpposingChampionsMatch")):
            highest = bool(node.get(
                "m_QuantityIsHighestOpposingChampionsMatch"))
            quantity = -2147483648 if highest else 2147483647
            all_zones = ctx._zones(
                "Deck|Hand|Champions|Warzone|Discard|Void|CastSpells|"
                "PlayedResources|Underground|Choosing|Mod|Simulacrum")
            source_owner = source.get(
                "controller_id", source.get("user_id", owner))
            state = getattr(ctx, "bstate", {}) or {}
            participant_ids = list(state.get("pids") or
                                   state.get("player_ids") or ())
            if not participant_ids:
                participant_ids = list((state.get("champ_map") or {}).keys())
            owner_key = (lambda player_id: int(player_id or 0)) if state.get(
                "pvp") else _side
            # C# iterates each opponent's Player object, so an opponent with
            # no matching card rows still contributes a count of zero.
            opposing_counts = {
                owner_key(player_id): 0 for player_id in participant_ids
                if not _same_player(ctx, player_id, source_owner)
            }
            for card in ctx._cards_in_zones(all_zones):
                card_owner = card.get(
                    "controller_id", card.get("user_id", 0))
                if _same_player(ctx, card_owner, source_owner):
                    continue
                from rules_port.filters import records_filter_matches
                card_filter = node.get("m_CardFilter")
                if (card_filter is not None and
                        not records_filter_matches(
                            card, card_filter, source=source, context=ctx)):
                    continue
                key = owner_key(card_owner)
                opposing_counts[key] = opposing_counts.get(key, 0) + 1
            if opposing_counts:
                quantity = (max(opposing_counts.values()) if highest else
                            min(opposing_counts.values()))
        if quantity_filter is not None:
            quantity = _requires_cards_count(
                ctx, source, owner, player_filter, zones, same_owner,
                target_owners, quantity_filter, node)
        count = _requires_cards_count(
            ctx, source, owner, player_filter, zones, same_owner,
            target_owners, node.get("m_CardFilter"), node)
        return _compare(count, node.get("m_ComparisonOp"), quantity)
    if kind == "AbilityVariableCondition":
        variables = getattr(ctx, "ability_variables", {}) or {}
        lhs, rhs = str(node.get("m_Lhs") or ""), str(node.get("m_Rhs") or "")
        state = getattr(ctx, "bstate", {}) or {}
        # List-producing child abilities (for example, burying a card) record
        # their results on the invoking ability instance.  A parent
        # AbilityVariableCondition can name that list directly, but its
        # authored variable default is still zero.  Resolve a matching
        # instance list as its current count before falling back to the
        # scalar variable value.
        resolving_ability = str(state.get("resolving_ability") or "").lower()
        scoped_lists = ((state.get("list_attrs") or {}).get(
            resolving_ability) or {})
        if lhs in scoped_lists:
            values = scoped_lists[lhs]
            if isinstance(values, (list, tuple, set)):
                left_value = len(values)
            else:
                try:
                    left_value = int(values)
                except (TypeError, ValueError):
                    return False
        else:
            if lhs not in variables:
                return False
            try:
                left_value = int(variables[lhs])
            except (TypeError, ValueError):
                return False
        try:
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
        friendly = _same_player(
            ctx, card.get("user_id"), ctx.ability_source_owner_id)
        if int(node.get("m_Your", 0) or 0):
            # C# ``TriggerCardEnteredZone.IsValid``: a "friendly zone" entry
            # requires BOTH the card's current controller and the controller it
            # had before the move to be the ability source's controller.
            # Checking only the current controller made an opposing champion
            # react to a card that came from the other player's zone: Mentor of
            # the Grave's "when a troop enters your hand from your crypt" fired
            # for a troop pulled out of the opponent's crypt, because the
            # control-transferring move had already rewritten the card's owner.
            if not friendly:
                return False
            previous = getattr(ctx, "event_previous_owner_id", None)
            if previous is not None and not _same_player(
                    ctx, previous, ctx.ability_source_owner_id):
                return False
        if int(node.get("m_Opposing", 0) or 0) and friendly:
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
    kind = _last(node.get("_t")) if isinstance(node, dict) else "<invalid>"
    raise RuntimeError("RulesPort condition has no native handler: " + kind)


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
