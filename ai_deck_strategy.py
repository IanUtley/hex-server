"""Infer an AI deck's broad strategy from authored card metadata.

Deck analysis uses typed Records for card types and ability effects. Localized
card text and card names are deliberately not part of the classifier. Burn
scoring includes BasicAction and QuickAction damage effects that can target a
champion.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

from gamedata import DEFAULT_RECORD_STORE, card_ability_graphs
from gamedata.records import RecordObject


_STRATEGIES = (
    "Aggressive", "BigThreats", "BuildArmy", "Burn", "HandAdvantage",
    "Reanimation",
)
_TIE_BREAK_ORDER = (
    "Reanimation", "BuildArmy", "Burn", "HandAdvantage", "BigThreats",
    "Aggressive",
)
_COLORS = ("Blood", "Diamond", "Ruby", "Sapphire", "Wild")
_STRATEGY_COLORS = {
    "Aggressive": frozenset({"Ruby", "Wild", "Diamond"}),
    "BigThreats": frozenset({"Wild"}),
    "BuildArmy": frozenset({"Wild", "Diamond", "Ruby"}),
    "Burn": frozenset({"Ruby"}),
    "HandAdvantage": frozenset({"Sapphire"}),
    "Reanimation": frozenset({"Blood", "Diamond"}),
}
_COLOR_AFFINITY_BONUS = 2.0
_MIN_STRATEGY_SCORE = 3.0
_MIN_STRATEGY_MARGIN = 1.0


@dataclass(frozen=True)
class DeckStrategyEvaluation:
    """The selected EDeckPersonality and the signals behind that choice."""

    personality: str | None
    scores: dict[str, float]
    features: dict[str, float | int]


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, RecordObject):
        return value.field(name, default)
    if isinstance(value, dict):
        return value.get(name, default)
    return default


def _type_name(value: Any) -> str:
    if isinstance(value, RecordObject):
        return value.short_type
    if isinstance(value, dict):
        return str(value.get("_t", "")).rsplit(".", 1)[-1]
    return ""


def _walk(value: Any):
    if isinstance(value, RecordObject):
        yield value
        for child in value.fields.values():
            yield from _walk(child)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)


def _filter_has_zone(card_filter: Any, zone: str) -> bool:
    wanted = zone.casefold()
    for node in _walk(card_filter):
        if _type_name(node) != "InZone":
            continue
        zones = str(_field(node, "m_Collection", "") or "")
        if wanted in {item.strip().casefold() for item in zones.split("|")}:
            return True
    return False


def _filter_is_self(card_filter: Any) -> bool:
    return any(
        _type_name(node) == "IsControlledBy"
        and str(_field(node, "m_TestAgainstActivePlayer", "")) == "0"
        for node in _walk(card_filter)
    )


def _filter_is_troop(card_filter: Any) -> bool:
    for node in _walk(card_filter):
        kind = _type_name(node)
        if kind == "IsTroop":
            return True
        if kind == "IsType":
            card_types = str(_field(node, "m_CardType", "") or "")
            if "Troop" in {part.strip() for part in card_types.split("|")}:
                return True
    return False


def _filter_is_resource(card_filter: Any) -> bool:
    for node in _walk(card_filter):
        kind = _type_name(node)
        if kind == "IsResource":
            return True
        if kind == "IsType":
            card_types = str(_field(node, "m_CardType", "") or "")
            if "Resource" in {part.strip() for part in card_types.split("|")}:
                return True
    return False


def _filter_has_hero(card_filter: Any) -> bool:
    return any(_type_name(node) == "IsHero" for node in _walk(card_filter))


def _filter_can_target_troop(card_filter: Any) -> bool:
    """Treat an untyped hand filter as unrestricted; honor explicit type gates."""
    type_gate_seen = False
    for node in _walk(card_filter):
        kind = _type_name(node)
        if kind == "IsType":
            type_gate_seen = True
            card_types = str(_field(node, "m_CardType", "") or "")
            if "Troop" in {part.strip() for part in card_types.split("|")}:
                return True
        elif kind == "IsTroop":
            return True
        elif kind in {"IsArtifact", "IsBasicAction", "IsQuickAction",
                      "IsResource", "IsConstant", "IsHero"}:
            type_gate_seen = True
        elif kind == "IsNotType":
            card_types = str(_field(node, "m_CardType", "") or "")
            if "Troop" in {part.strip() for part in card_types.split("|")}:
                return False
    return not type_gate_seen


def _card_color_weights(card: Any) -> dict[str, float]:
    """Return normalized color weights from authored card thresholds."""
    requirements: dict[str, float] = {}
    for entry in getattr(card, "threshold", ()) or ():
        color = str(_field(entry, "m_ColorFlags", "") or "").strip()
        if color not in _COLORS:
            continue
        try:
            raw_quantity = _field(
                entry, "m_ThresholdColorRequirement", 1)
            quantity = float(
                1 if raw_quantity is None else raw_quantity)
        except (TypeError, ValueError):
            quantity = 1.0
        requirements[color] = requirements.get(color, 0.0) + max(
            0.0, quantity)
    total = sum(requirements.values())
    if total <= 0:
        return {}
    return {color: quantity / total
            for color, quantity in requirements.items()}


def _target_is_self(target: Any) -> bool:
    return (str(getattr(target, "player_filter", "")) == "Self"
            or _filter_is_self(getattr(target, "card_filter", None)))


def _effect_target(graph: Any, target_index: int):
    if 0 <= target_index < len(graph.targets):
        return graph.targets[target_index]
    return None


@lru_cache(maxsize=8192)
def _card_traits(card_guid: str) -> tuple[bool, ...]:
    """Return static strategy signals for one CardTemplate GUID."""
    card = DEFAULT_RECORD_STORE.get("CardTemplate", card_guid)
    if card is None or not hasattr(card, "card_type"):
        return (False,) * 10

    types = {part.strip() for part in card.card_type.split("|")}
    is_troop = "Troop" in types
    try:
        cost = int(card.resource_cost or 0)
    except (TypeError, ValueError):
        cost = 0
    try:
        attack = int(card.field("m_BaseAttackValue", 0) or 0)
        defense = int(card.field("m_BaseDefenseValue", 0) or 0)
    except (TypeError, ValueError):
        attack = defense = 0

    cheap_troop = is_troop and cost <= 2
    large_troop = is_troop and (cost >= 5 or attack + defense >= 9)
    has_damage = False
    has_direct_damage_action = False
    has_ramp = False
    has_removal = False
    has_token_summon = False
    has_draw = False
    discards_own_card = False
    recovers_own_troop = False

    # Card-level discard and sacrifice costs are authored on CardTemplate and
    # do not necessarily appear as effects in the ability graph.
    try:
        for kind, target_guid in card.additional_cost_targets:
            if kind not in {"discard", "sacrifice"}:
                continue
            target_record = DEFAULT_RECORD_STORE.get(
                "AbilityTargetTemplate", target_guid)
            target = (target_record.target_spec
                      if target_record is not None
                      and hasattr(target_record, "target_spec") else None)
            if target is not None and _target_is_self(target):
                expected_zone = "Hand" if kind == "discard" else "Warzone"
                has_cost_zone = (
                    _filter_has_zone(target.card_filter, expected_zone)
                    or expected_zone in target.collection_flags.split("|"))
                if (has_cost_zone
                        and _filter_can_target_troop(target.card_filter)):
                    discards_own_card = True
    except (TypeError, ValueError):
        pass

    for graph in card_ability_graphs(DEFAULT_RECORD_STORE, card_guid):
        for effect in graph.effects:
            kind = effect.concrete_type.rsplit(".", 1)[-1]
            template = effect.template
            target = _effect_target(graph, effect.target_index)
            target_filter = getattr(target, "card_filter", None)
            self_target = target is not None and _target_is_self(target)

            if kind == "CardModifierAbilityEffectTemplate":
                modifier_type = _type_name(_field(template, "m_Modifier"))
                if modifier_type in {"DamageModifier", "LoseLifeModifier"}:
                    has_damage = True
                    if (types.intersection({"BasicAction", "QuickAction"})
                            and target is not None
                            and getattr(target, "player_filter", "") != "Self"
                            and _filter_has_hero(target_filter)):
                        has_direct_damage_action = True
            elif kind in {"DestroyCardAbilityEffectTemplate",
                          "TransformCardAtRandomAbilityEffectTemplate"}:
                if target is not None and _filter_is_troop(target_filter):
                    has_removal = True
            elif kind == "DrawNCardsAbilityEffectTemplate":
                if target is None or _target_is_self(target):
                    has_draw = True
            elif kind == "DiscardCardAbilityEffectTemplate":
                if (self_target and target is not None
                        and _filter_has_zone(target_filter, "Hand")
                        and _filter_can_target_troop(target_filter)):
                    discards_own_card = True
            elif kind == "BuryCardAbilityEffectTemplate":
                # Bury moves cards from the selected deck into Discard.  Only
                # count the AI's own deck as reanimation setup, not opposing
                # deck-mill effects.
                if (self_target and target is not None
                        and _filter_can_target_troop(
                            _field(template, "m_Filter"))):
                    discards_own_card = True
            elif kind == "PlayCardAbilityEffectTemplate":
                if (target is not None and _target_is_self(target)
                        and _filter_is_resource(target_filter)
                        and (_filter_has_zone(target_filter, "Deck")
                             or _filter_has_zone(target_filter, "Hand"))):
                    has_ramp = True
            elif kind == "MoveCardToZoneEffectTemplate":
                destination = str(_field(
                    template, "m_DestinationCollection", "") or "")
                if (destination == "PlayedResources" and self_target
                        and _filter_is_resource(target_filter)
                        and (_filter_has_zone(target_filter, "Deck")
                             or _filter_has_zone(target_filter, "Hand")
                             or _filter_has_zone(target_filter, "Discard"))):
                    has_ramp = True
                if (destination == "Discard" and self_target and target is not None
                        and (_filter_has_zone(target_filter, "Hand")
                             or _filter_has_zone(target_filter, "Warzone")
                             or _filter_has_zone(target_filter, "Deck"))
                        and _filter_can_target_troop(target_filter)):
                    discards_own_card = True
                if (destination == "Warzone" and self_target
                        and _filter_has_zone(target_filter, "Discard")
                        and _filter_is_troop(target_filter)):
                    recovers_own_troop = True
                if (destination in {"Hand", "Deck", "Discard", "Void"}
                        and target is not None
                        and _filter_is_troop(target_filter)
                        and (not self_target)):
                    has_removal = True
                if (destination == "Hand" and target is not None
                        and _filter_has_zone(target_filter, "Deck")
                        and _target_is_self(target)):
                    has_draw = True
            elif kind == "SummonTokenTroopAbilityEffectTemplate":
                destination = str(_field(
                    template, "m_CardCollection", "") or "")
                token_filter = _field(template, "m_CardFilter")
                token_guid_obj = _field(template, "m_CardTemplateId")
                token_guid = ""
                if isinstance(token_guid_obj, RecordObject):
                    token_guid = token_guid_obj.guid
                elif isinstance(token_guid_obj, dict):
                    token_guid = str(_field(
                        token_guid_obj, "m_Guid", "") or "")
                token_card = (DEFAULT_RECORD_STORE.get("CardTemplate", token_guid)
                              if token_guid else None)
                token_is_resource = (
                    token_card is not None
                    and "Resource" in str(token_card.card_type).split("|"))
                token_is_troop = (
                    token_card is not None
                    and "Troop" in str(token_card.card_type).split("|"))
                if destination == "Hand":
                    has_draw = True
                if destination == "Warzone" and (token_is_troop
                                                   or _filter_is_troop(token_filter)):
                    has_token_summon = True
                if (destination == "PlayedResources" and token_is_resource
                        and (self_target or _filter_is_self(token_filter)
                             or not graph.targets)):
                    has_ramp = True
                if (destination == "Warzone" and _filter_is_troop(token_filter)
                        and _filter_has_zone(token_filter, "Discard")
                        and (self_target or _filter_is_self(token_filter)
                             or not graph.targets)):
                    recovers_own_troop = True

    return (cheap_troop, large_troop, has_damage, has_removal,
            has_token_summon, has_draw, discards_own_card,
            recovers_own_troop, has_direct_damage_action, has_ramp)


def evaluate_deck_strategy(deck_cards: Iterable[tuple[str, int]]) -> DeckStrategyEvaluation:
    """Choose a deck strategy from ``(CardTemplate GUID, quantity)`` rows.

    ``None`` means the deck has no strong archetype signal, so the AI should
    retain EDeckPersonality.Default. The Reanimation choice requires a graveyard
    enabler, a typed recovery path for a troop from Discard to Warzone, and a
    large troop target in the deck.
    """
    from collections import Counter

    totals = Counter()
    quantities = Counter()
    for guid, quantity in deck_cards or ():
        try:
            count = max(0, int(quantity or 0))
        except (TypeError, ValueError):
            continue
        if count:
            quantities[str(guid).lower()] += count

    for guid, quantity in quantities.items():
        card = DEFAULT_RECORD_STORE.get("CardTemplate", guid)
        if card is None or not hasattr(card, "card_type"):
            continue
        card_types = {part.strip() for part in card.card_type.split("|")}
        if "Resource" in card_types:
            continue
        (cheap, large, damage, removal, token, draw, graveyard_setup,
         graveyard_recovery, direct_damage_action, ramp) = _card_traits(guid)
        totals["nonresource"] += quantity
        for color, weight in _card_color_weights(card).items():
            totals[f"color_{color.casefold()}"] += quantity * weight
        if "Troop" in card_types:
            totals["troops"] += quantity
            try:
                cost = int(card.resource_cost or 0)
            except (TypeError, ValueError):
                cost = 0
            totals["troop_cost"] += cost * quantity
        if cheap:
            totals["cheap_troops"] += quantity
        if large:
            totals["large_troops"] += quantity
        if damage:
            totals["damage_cards"] += quantity
        if direct_damage_action:
            totals["direct_damage_actions"] += quantity
        if ramp:
            totals["ramp_cards"] += quantity
        if removal:
            totals["removal_cards"] += quantity
            try:
                if int(card.resource_cost or 0) <= 3:
                    totals["cheap_removal"] += quantity
            except (TypeError, ValueError):
                pass
        if token:
            totals["token_generators"] += quantity
        if draw:
            totals["draw_cards"] += quantity
        if graveyard_setup:
            totals["graveyard_enablers"] += quantity
        if graveyard_recovery:
            totals["graveyard_recovery"] += quantity

    count = totals["nonresource"]
    troop_count = totals["troops"]
    if count <= 0:
        return DeckStrategyEvaluation(None, {name: 0.0 for name in _STRATEGIES},
                                      dict(totals))

    cheap_troop_share = totals["cheap_troops"] / count
    large_troop_share = totals["large_troops"] / count
    ramp_share = totals["ramp_cards"] / count
    damage_share = totals["damage_cards"] / count
    direct_damage_action_share = totals["direct_damage_actions"] / count
    removal_share = totals["removal_cards"] / count
    token_share = totals["token_generators"] / count
    draw_share = totals["draw_cards"] / count
    cheap_removal_share = totals["cheap_removal"] / count
    avg_troop_cost = (totals["troop_cost"] / troop_count
                      if troop_count else 0.0)
    reanimation_ready = (totals["graveyard_enablers"] > 0
                         and totals["graveyard_recovery"] > 0
                         and totals["large_troops"] > 0)

    # Most decks only need a small reanimation package, so count the setup and
    # recovery cards directly instead of diluting them by the full deck size.
    # BigThreats also depends on a handful of large troops and ramp cards; the
    # bonus for having both captures that ramp is meant to accelerate threats.
    # Average troop costs from 3 through 5 remain neutral for BigThreats.
    big_threats = min(5.0, float(totals["large_troops"]))
    if totals["large_troops"] or avg_troop_cost >= 4.5:
        big_threats += 1.75 * min(3, totals["ramp_cards"])
    if totals["large_troops"] and totals["ramp_cards"]:
        big_threats += 0.75
    big_threats += 1.5 * max(0.0, avg_troop_cost - 5.0)

    color_affinity_shares = {
        strategy: round(sum(
            totals[f"color_{color.casefold()}"]
            for color in colors) / count, 4)
        for strategy, colors in _STRATEGY_COLORS.items()
    }

    # Most other categories are scored from their share of the non-resource
    # deck, since their identity is carried by repeated low-cost or action
    # cards rather than a few high-impact cards.
    scores = {
        "Aggressive": min(
            10.0, 20.0 * cheap_troop_share
            + 12.0 * min(cheap_troop_share, cheap_removal_share)
            + _COLOR_AFFINITY_BONUS * color_affinity_shares["Aggressive"]),
        "BigThreats": min(
            10.0, big_threats
            + _COLOR_AFFINITY_BONUS * color_affinity_shares["BigThreats"]),
        "BuildArmy": min(10.0, 30.0 * token_share
                         + 10.0 * cheap_troop_share
                         + _COLOR_AFFINITY_BONUS
                         * color_affinity_shares["BuildArmy"]),
        "Burn": min(10.0, 40.0 * direct_damage_action_share
                    + 8.0 * damage_share
                    + _COLOR_AFFINITY_BONUS * color_affinity_shares["Burn"]),
        "HandAdvantage": min(
            10.0, 35.0 * draw_share
            + _COLOR_AFFINITY_BONUS
            * color_affinity_shares["HandAdvantage"]),
        "Reanimation": (min(
            10.0, 6.0 + 1.5 * min(totals["graveyard_enablers"], 2)
            + 1.5 * min(totals["graveyard_recovery"], 2)
            + 0.25 * min(totals["large_troops"], 4)
            + _COLOR_AFFINITY_BONUS
            * color_affinity_shares["Reanimation"])
            if reanimation_ready else 0.0),
    }
    scores = {name: round(max(0.0, score), 2)
              for name, score in scores.items()}
    features = {
        **dict(totals),
        "cheap_troop_share": round(cheap_troop_share, 4),
        "large_troop_share": round(large_troop_share, 4),
        "ramp_share": round(ramp_share, 4),
        "damage_share": round(damage_share, 4),
        "direct_damage_action_share": round(direct_damage_action_share, 4),
        "removal_share": round(removal_share, 4),
        "token_generator_share": round(token_share, 4),
        "draw_share": round(draw_share, 4),
        **{f"{strategy.casefold()}_color_affinity": share
           for strategy, share in color_affinity_shares.items()},
        "average_troop_cost": round(avg_troop_cost, 2),
    }

    winner = max(_TIE_BREAK_ORDER, key=lambda name: scores[name])
    runner_up = max(score for name, score in scores.items() if name != winner)
    score_gap = round(scores[winner] - runner_up, 2)
    features["top_strategy_score"] = scores[winner]
    features["strategy_score_gap"] = score_gap
    selected = (winner if scores[winner] >= _MIN_STRATEGY_SCORE
                and score_gap >= _MIN_STRATEGY_MARGIN else None)
    return DeckStrategyEvaluation(
        selected, scores,
        features,
    )
