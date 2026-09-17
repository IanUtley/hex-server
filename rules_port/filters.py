"""Composable ports of the client ``CardFilter`` primitives.

Filters receive the same context shape as the C# engine but use duck-typed
runtime cards, keeping Records/metadata outside the rules kernel.
"""
from __future__ import annotations
from dataclasses import dataclass
import inspect
from typing import Any, Protocol, Sequence

def _v(card, *names, default=None):
    for name in names:
        if isinstance(card, dict) and name in card: return card[name]
        if hasattr(card, name): return getattr(card, name)
    return default


def _attribute_bits(value):
    """Convert an ``ECardAttributes`` bitmask or '|'-joined enum names to bits.

    Records serializes attribute flags as a display string such as
    ``"Flight"`` or ``"Flight|SpellShield"``.  The client's
    ``HasAll/AnyAttributeFlags`` compare the decoded bitmask, so the adapter
    must decode the names rather than pass the string to ``int()``.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return int(value)
    import game_engine
    total = 0
    for name in str(value).split("|"):
        name = name.strip()
        if not name:
            continue
        total |= int(getattr(game_engine.ECardAttributes, name, 0) or 0)
    return total


def _shard_bits(value):
    """Convert an ``ECardShards`` bitmask or '|'-joined enum names to bits."""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return int(value)
    import game_engine
    total = 0
    for name in str(value).split("|"):
        name = name.strip()
        if not name:
            continue
        total |= int(getattr(game_engine.ECardShards, name, 0) or 0)
    return total


def _cmp(lhs, operation, rhs):
    op = str(getattr(operation, "name", operation)).replace("_", "").replace(" ", "").lower()
    return {"lessthan": lhs < rhs, "lessthanorequal": lhs <= rhs,
            "greaterthan": lhs > rhs, "greaterthanorequal": lhs >= rhs,
            "equals": lhs == rhs, "equal": lhs == rhs,
            "notequal": lhs != rhs, "notequals": lhs != rhs,
            "onemorethan": lhs == rhs + 1, "twomorethan": lhs == rhs + 2,
            "onelessthan": lhs == rhs - 1,
            "0": lhs < rhs, "1": lhs <= rhs, "2": lhs > rhs,
            "3": lhs >= rhs, "4": lhs == rhs, "5": lhs != rhs,
            "6": lhs == rhs + 1, "7": lhs == rhs + 2,
            "8": lhs == rhs - 1}.get(op, False)

def _candidate_cards(session=None, cards=None):
    values = cards
    if values is None and session is not None:
        values = _v(session, "cards", "all_cards", default=None)
        if values is None:
            iterator = getattr(session, "iter_cards", None)
            values = iterator() if callable(iterator) else None
    if isinstance(values, dict):
        values = values.values()
    return tuple(values or ())

class CardFilter(Protocol):
    def matches(self, card, *, source=None, player=None, session=None, effect=None) -> bool: ...

@dataclass(frozen=True)
class InZone:
    collection: int
    def matches(self, card, **kwargs):
        try: return card is not None and bool(int(_v(card, "collection", "current_collection", default=0)) & int(self.collection))
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class IsTapped:
    value: bool = True
    def matches(self, card, **kwargs):
        tapped = _v(card, "is_tapped", "tapped", "is_exhausted", default=False)
        return bool(tapped() if callable(tapped) else tapped) is self.value

@dataclass(frozen=True)
class IsType:
    card_type: int
    def matches(self, card, **kwargs):
        try: return bool(int(_v(card, "card_type", "type", default=0)) & int(self.card_type))
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class IsNotType(IsType):
    def matches(self, card, **kwargs): return not IsType(self.card_type).matches(card, **kwargs)

@dataclass(frozen=True)
class InCollection:
    collection: int
    def matches(self, card, **kwargs): return InZone(self.collection).matches(card)

@dataclass(frozen=True)
class HasAnyAttributeFlags:
    attribute_flags: int = 0
    def matches(self, card, **kwargs):
        try: return bool(int(_v(card, "attributes", "attribute_flags", default=0)) & _attribute_bits(self.attribute_flags))
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class HasAllAttributeFlags:
    attribute_flags: int = 0
    def matches(self, card, **kwargs):
        try:
            flags = _attribute_bits(self.attribute_flags)
            if not flags:
                return True
            return (int(_v(card, "attributes", "attribute_flags", default=0)) & flags) == flags
        except (TypeError, ValueError):
            return False

@dataclass(frozen=True)
class HasCastingCost:
    cost: int
    comparison: object = "Equals"
    def matches(self, card, **kwargs):
        try:
            value = int(_v(card, "casting_cost", "cost", default=-1)); target = int(self.cost)
            op = str(getattr(self.comparison, "name", self.comparison)).lower().replace(" ", "")
            return _cmp(value, self.comparison, target)
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class HasKeywordAbility:
    keyword: str
    def matches(self, card, **kwargs):
        abilities = _v(card, "abilities", "keywords", default=()) or ()
        return self.keyword.lower() in {str(value).lower() for value in abilities}

@dataclass(frozen=True)
class HasTag:
    tag: str
    def matches(self, card, **kwargs):
        tags = _v(card, "tags", "tag", default=())
        if isinstance(tags, str): tags = (tags,)
        return self.tag.lower() in {str(value).lower() for value in (tags or ())}

@dataclass(frozen=True)
class IsSubType:
    subtype: str
    def matches(self, card, **kwargs):
        value = str(_v(card, "subtype", "subtypes", default=""))
        return self.subtype == "*" or self.subtype.lower() in value.lower()

class IsMultiThresholdCard:
    def matches(self, card, **kwargs):
        values = _v(card, "thresholds", "resource_thresholds", default=())
        return len(values or ()) > 1

@dataclass(frozen=True)
class HasResourceCost:
    cost: int = 0
    comparison: object = "Equals"
    add_x: bool = False
    add_attack: bool = False
    add_defense: bool = False
    add_card_integer_variable: str = ""
    add_variable: str = ""
    def matches(self, card, *, source=None, **kwargs):
        # C# HasResourceCost compares the card's ResourceCost + paid X against
        # the authored ResourceCost plus the Add* modifiers.  Only the base
        # value was ported, so cost-scaling cards (313 AddCardIntegerVariable
        # uses) always compared the wrong number.
        if card is None:
            return False
        lhs = int(_v(card, "resource_cost", "cost", default=0) or 0)
        lhs += int(_v(card, "resource_x_cost_paid", "x_cost", default=0) or 0)
        rhs = int(self.cost or 0)
        if self.add_attack:
            rhs += int(_v(source, "attack", "attack_value", default=0) or 0)
        if self.add_defense:
            rhs += int(_v(source, "defense", "defense_value", default=0) or 0)
        if self.add_card_integer_variable and source is not None:
            attrs = _v(source, "int_attrs", default={}) or {}
            if isinstance(attrs, dict):
                rhs += int(attrs.get(self.add_card_integer_variable, 0) or 0)
        return _cmp(lhs, self.comparison, rhs)

@dataclass(frozen=True)
class HasAttackValue:
    value: int
    comparison: object = "Equals"
    compare_to_source: bool = False
    def matches(self, card, *, source=None, **kwargs):
        try:
            rhs = int(_v(source, "attack", "attack_value", default=self.value)) if self.compare_to_source and source is not None else int(self.value)
            return _cmp(int(_v(card, "attack", "attack_value", default=-1)), self.comparison, rhs)
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class HasDefenseValue:
    value: int
    comparison: object = "Equals"
    compare_to_source: bool = False
    def matches(self, card, *, source=None, **kwargs):
        try:
            rhs = int(_v(source, "defense", "defense_value", default=self.value)) if self.compare_to_source and source is not None else int(self.value)
            return _cmp(int(_v(card, "defense", "defense_value", default=-1)), self.comparison, rhs)
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class IsSocketable:
    socket_value: int
    comparison: object = "Equals"
    def matches(self, card, **kwargs):
        try: return _cmp(int(_v(card, "socket_count", default=0)), self.comparison, int(self.socket_value))
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class IsSocketed:
    socketed_value: int = 1
    comparison: object = "GreaterThanOrEqual"
    def matches(self, card, **kwargs):
        try:
            value = _v(card, "socketed_count", "active_gem_count", default=None)
            if value is None: value = 1 if _v(card, "is_socketed", "socketed", default=False) else 0
            return _cmp(int(value), self.comparison, int(self.socketed_value))
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class HasName:
    name: str
    use_stored_name: bool = False
    def matches(self, card, *, source=None, **kwargs):
        needle = self.name
        if needle.lower() == "<this>" and source is not None:
            needle = _v(source, "name", "template_name", default="")
        value = _v(card, "name", "template_name", default="")
        return str(needle).lower() in str(value).lower()

@dataclass(frozen=True)
class IsRarity:
    rarity: object
    def matches(self, card, **kwargs): return _v(card, "rarity", "card_rarity") == self.rarity

@dataclass(frozen=True)
class IsColor:
    color: object
    def matches(self, card, **kwargs):
        wanted = _shard_bits(self.color)
        shards = _v(card, "shards", "card_shards", default=None)
        if isinstance(shards, (list, tuple, set)):
            actual = 0
            for item in shards:
                actual |= _shard_bits(item)
        else:
            actual = _shard_bits(_v(
                card, "color", "color_flags", "shard", "shard_bits", default=0))
        return bool(actual & wanted)

@dataclass(frozen=True)
class InFaction:
    faction: object
    def matches(self, card, **kwargs):
        value = _v(card, "faction", "faction_flags", default=None)
        # C# EFactions is a plain enum, compared with equality (not flags).
        try:
            return int(value) == int(self.faction)
        except (TypeError, ValueError):
            return value == self.faction

class IsToken:
    def matches(self, card, **kwargs): return bool(_v(card, "is_token", "token", default=False))

class IsResource:
    def matches(self, card, **kwargs): return bool(_v(card, "is_resource", default=False))

class IsQuick:
    def matches(self, card, **kwargs): return bool(_v(card, "is_quick_action", "quick_action", default=False))

class IsTroop(IsType):
    def __init__(self): super().__init__(2)

class IsArtifact(IsType):
    def __init__(self): super().__init__(4)

class IsChampion(IsType):
    def __init__(self): super().__init__(1)

class IsHero:
    def matches(self, card, **kwargs): return bool(_v(card, "is_hero", "hero", default=False))

class IsAlternateArt:
    def matches(self, card, **kwargs): return bool(_v(card, "alternate_art", "is_alternate_art", default=False))

class IsExtendedArt:
    def matches(self, card, **kwargs): return bool(_v(card, "extended_art", "is_extended_art", default=False))

class IsPromo:
    def matches(self, card, **kwargs): return bool(_v(card, "promo", "is_promo", default=False))

class IsDamaged:
    def matches(self, card, **kwargs):
        try: return int(_v(card, "damage", "damage_taken", default=0)) > 0
        except (TypeError, ValueError): return False

class IsAttacking:
    def matches(self, card, **kwargs): return bool(_v(card, "is_attacking", "attacking", default=False))

class IsBlocking:
    def matches(self, card, **kwargs): return bool(_v(card, "is_blocking", "blocking", default=False))

@dataclass(frozen=True)
class IsControlledBy:
    test_against_active_player: bool = False
    def matches(self, card, *, player=None, session=None, source=None, **kwargs):
        controller = _v(card, "controller_id", "controlling_player", "owner_id")
        expected = getattr(session, "active_player_id", None) if self.test_against_active_player else player
        return (controller == expected if self.test_against_active_player
                else source is not None and controller == expected)

@dataclass(frozen=True)
class IsNotControlledBy:
    fail_uncontrolled_cards: bool = False
    def matches(self, card, *, player=None, **kwargs):
        controller = _v(card, "controller_id", "controlling_player", "owner_id")
        return ((not self.fail_uncontrolled_cards or controller is not None) and
                controller != player)

class IsPlayedThisTurn:
    def matches(self, card, **kwargs): return bool(_v(card, "played_this_turn", "came_out_this_turn", default=False))

class IsDamagedThisTurn:
    def matches(self, card, **kwargs): return bool(_v(card, "damaged_this_turn", "damaged", default=False))

class IsHealedThisTurn:
    def matches(self, card, **kwargs): return bool(_v(card, "healed_this_turn", "healed", default=False))

class AnyCard:
    """Client's permissive card filter; null cards are still excluded."""
    def matches(self, card, **kwargs): return card is not None

class IsBasic:
    def matches(self, card, **kwargs): return bool(_v(card, "is_basic", "basic", default=False))

class IsUniqueCard:
    def matches(self, card, **kwargs): return bool(_v(card, "is_unique", "unique", default=False))

@dataclass(frozen=True)
class IsCardName:
    name: str
    def matches(self, card, **kwargs):
        return str(_v(card, "name", "template_name", default="")).lower() == self.name.lower()

@dataclass(frozen=True)
class NameContainsFilter:
    value: str
    def matches(self, card, **kwargs):
        return self.value.lower() in str(_v(card, "name", "template_name", default="")).lower()

class HasAttackedThisTurn:
    def matches(self, card, **kwargs): return bool(_v(card, "attacked_this_turn", "has_attacked_this_turn", default=False))

@dataclass(frozen=True)
class CompareAttackAndDefenseFilter:
    comparison: object = "Equals"
    compare_to_ability_source_defense: bool = False
    compare_to_ability_source_attack: bool = False
    def matches(self, card, *, source=None, **kwargs):
        attack = int(_v(card, "attack", "attack_value", default=0) or 0)
        defense = int(_v(card, "defense", "defense_value", default=0) or 0)
        if self.compare_to_ability_source_defense:
            rhs = int(_v(source, "defense", "defense_value", default=0) or 0)
            return _cmp(attack, self.comparison, rhs)
        if self.compare_to_ability_source_attack:
            rhs = int(_v(source, "attack", "attack_value", default=0) or 0)
            return _cmp(defense, self.comparison, rhs)
        return _cmp(attack, self.comparison, defense)

@dataclass(frozen=True)
class CompareAttackToLowestFilter:
    comparison: object = "Equals"
    card_filter: CardFilter | None = None
    collection: object = None
    def matches(self, card, *, session=None, source=None, cards=None, **kwargs):
        if self.card_filter is not None and not self.card_filter.matches(card, source=source, session=session, **kwargs):
            return False
        candidates = _candidate_cards(session, cards)
        if self.collection is not None:
            candidates = tuple(c for c in candidates if InZone(self.collection).matches(c))
        if self.card_filter is not None:
            candidates = tuple(c for c in candidates if self.card_filter.matches(c, source=source, session=session, **kwargs))
        values = [int(_v(c, "attack", "attack_value", default=0) or 0) for c in candidates]
        if not values:
            return False
        return _cmp(int(_v(card, "attack", "attack_value", default=0) or 0), self.comparison, min(values))

@dataclass(frozen=True)
class CompareAttackToHighestFilter(CompareAttackToLowestFilter):
    def matches(self, card, *, session=None, source=None, cards=None, **kwargs):
        if self.card_filter is not None and not self.card_filter.matches(card, source=source, session=session, **kwargs):
            return False
        candidates = _candidate_cards(session, cards)
        if self.collection is not None:
            candidates = tuple(c for c in candidates if InZone(self.collection).matches(c))
        if self.card_filter is not None:
            candidates = tuple(c for c in candidates if self.card_filter.matches(c, source=source, session=session, **kwargs))
        values = [int(_v(c, "attack", "attack_value", default=0) or 0) for c in candidates]
        return bool(values) and _cmp(int(_v(card, "attack", "attack_value", default=0) or 0), self.comparison, max(values))

@dataclass(frozen=True)
class CompareDefenseToLowestFilter:
    comparison: object = "Equals"
    def matches(self, card, *, session=None, cards=None, **kwargs):
        values = [int(_v(c, "defense", "defense_value", default=0) or 0)
                  for c in _candidate_cards(session, cards)]
        return bool(values) and _cmp(
            int(_v(card, "defense", "defense_value", default=0) or 0),
            self.comparison, min(values))

@dataclass(frozen=True)
class CompareHealthToLowestFilter:
    comparison: object = "Equals"
    def matches(self, card, *, session=None, cards=None, **kwargs):
        if not IsChampion().matches(card):
            return False
        values = [int(_v(c, "health", "current_health", "defense", default=0) or 0)
                  for c in _candidate_cards(session, cards)
                  if IsChampion().matches(c)]
        return bool(values) and _cmp(
            int(_v(card, "health", "current_health", "defense", default=0) or 0),
            self.comparison, min(values))

@dataclass(frozen=True)
class CompareHealthToHighestFilter(CompareHealthToLowestFilter):
    def matches(self, card, *, session=None, cards=None, **kwargs):
        if not IsChampion().matches(card):
            return False
        values = [int(_v(c, "health", "current_health", "defense", default=0) or 0)
                  for c in _candidate_cards(session, cards)
                  if IsChampion().matches(c)]
        return bool(values) and _cmp(
            int(_v(card, "health", "current_health", "defense", default=0) or 0),
            self.comparison, max(values))

@dataclass(frozen=True)
class CompareResourceCostToHighestFilter:
    comparison: object = "Equals"
    card_filter: CardFilter | None = None
    collection: object = None
    def matches(self, card, *, session=None, source=None, cards=None, **kwargs):
        if self.card_filter is not None and not self.card_filter.matches(
                card, source=source, session=session, **kwargs):
            return False
        candidates = _candidate_cards(session, cards)
        if self.collection is not None:
            candidates = tuple(c for c in candidates if InZone(self.collection).matches(c))
        if self.card_filter is not None:
            candidates = tuple(c for c in candidates if self.card_filter.matches(
                c, source=source, session=session, **kwargs))
        values = [int(_v(c, "resource_cost", "cost", default=0) or 0)
                  for c in candidates]
        return bool(values) and _cmp(
            int(_v(card, "resource_cost", "cost", default=0) or 0),
            self.comparison, max(values))

@dataclass(frozen=True)
class CompareResourceCostToMyHighestFilter(CompareResourceCostToHighestFilter):
    def matches(self, card, *, session=None, source=None, cards=None, **kwargs):
        owner = _v(source, "owner_id", "controller_id", "controlling_player", default=None)
        candidates = tuple(c for c in _candidate_cards(session, cards)
                           if owner is None or _v(c, "owner_id", "controller_id", "controlling_player", default=None) == owner)
        return super().matches(card, session=session, source=source,
                               cards=candidates, **kwargs)

@dataclass(frozen=True)
class HasSourceTypeFilter:
    dont_exactly_match_original: bool = False
    def matches(self, card, *, source=None, **kwargs):
        if card is None or source is None:
            return False
        if self.dont_exactly_match_original:
            a = _v(card, "template_guid", "template_id", default=None)
            b = _v(source, "template_guid", "template_id", default=None)
            if a is not None and a == b:
                return False
        return _v(card, "card_type", "type", default=0) == _v(source, "card_type", "type", default=0)

class DifferentOwners:
    def matches(self, card, *, source=None, player=None, **kwargs):
        if card is None:
            return False
        # C# DifferentOwners: card.m_ControllingPlayer != responsiblePlayer.
        # It does not require a source card.
        expected = player
        if expected is None and source is not None:
            expected = _v(source, "controller_id", "owner_id", default=None)
        controller = _v(card, "controller_id", "owner_id", default=None)
        if expected is None:
            return False
        return controller != expected

@dataclass(frozen=True)
class HasASharedFactionWithSourceFilter:
    def matches(self, card, *, source=None, **kwargs):
        # C# Card.SharesFaction: faction equality (EFactions is not flags).
        try:
            return int(_v(card, "faction", "faction_flags", default=0)) == int(
                _v(source, "faction", "faction_flags", default=0))
        except (TypeError, ValueError):
            return False

@dataclass(frozen=True)
class HasASharedRarityWithSourceFilter:
    def matches(self, card, *, source=None, **kwargs):
        return _v(card, "rarity", "card_rarity", default=None) == _v(source, "rarity", "card_rarity", default=None)

@dataclass(frozen=True)
class HasASharedSubtypeWithSourceFilter:
    def matches(self, card, *, source=None, **kwargs):
        a = _v(card, "subtypes", "subtype", default=())
        b = _v(source, "subtypes", "subtype", default=())
        if isinstance(a, str): a = (a,)
        if isinstance(b, str): b = (b,)
        return bool(set(a or ()) & set(b or ()))

class HasASharedClassWithSourceChampionFilter:
    def matches(self, card, *, source=None, **kwargs):
        a = _v(card, "classes", "champion_classes", "class_flags", default=())
        b = _v(source, "classes", "champion_classes", "class_flags", default=())
        if isinstance(a, (str, int)): a = (a,)
        if isinstance(b, (str, int)): b = (b,)
        return bool(set(a or ()) & set(b or ()))

class HasASharedSubtypeWithSourceChampionFilter(HasASharedSubtypeWithSourceFilter):
    pass

class HasASharedShardWithSourceFilter:
    def matches(self, card, *, source=None, **kwargs):
        try: return bool(int(_v(card, "shards", "shard", "color_flags", default=0)) & int(_v(source, "shards", "shard", "color_flags", default=0)))
        except (TypeError, ValueError): return False

class HasASharedShardWithTopOfChainFilter(HasASharedShardWithSourceFilter):
    def matches(self, card, *, session=None, **kwargs):
        top = _v(session, "top_of_chain", "chain_top", default=None)
        return super().matches(card, source=top, session=session, **kwargs)

class MovedBySource:
    def matches(self, card, *, source=None, **kwargs):
        return card is not None and source is not None and _v(
            card, "moved_by_source_id", "last_moved_by", default=None) == _v(
                source, "session_card_id", "card_uid", "id", default=None)

class IsChildOfAbilitySource:
    def matches(self, card, *, source=None, **kwargs):
        return card is not None and source is not None and (
            _v(card, "parent_link", "parent_id", default=None) in (source, _v(source, "session_card_id", "card_uid", "id", default=None)))

class IsParentOfAbilitySourceFilter:
    def matches(self, card, *, source=None, **kwargs):
        return source is not None and card is not None and _v(
            source, "parent_link", "parent_id", default=None) in (
                card, _v(card, "session_card_id", "card_uid", "id", default=None))

class InCombatWithSourceFilter:
    def matches(self, card, *, source=None, session=None, **kwargs):
        if card is None or source is None:
            return False
        combat = getattr(card, "in_combat_with", None)
        if callable(combat):
            return bool(combat(source, session))
        related = _v(card, "combat_partner_id", "in_combat_with_id", default=None)
        return related in (source, _v(source, "session_card_id", "card_uid", "id", default=None))

@dataclass(frozen=True)
class DamagedOpponentThisTurn:
    only_combat_damage: bool = False
    only_non_combat_damage: bool = False
    def matches(self, card, **kwargs):
        if self.only_combat_damage:
            key = "combat_damage_dealt_to_opponent"
        elif self.only_non_combat_damage:
            key = "non_combat_damage_dealt_to_opponent"
        else:
            key = "damage_dealt_to_opponent"
        stats = _v(card, "stats_this_turn", "card_stats_this_turn", default={}) or {}
        return int(stats.get(key, _v(card, key, default=0)) or 0) > 0

@dataclass(frozen=True)
class HasSourceResourceCost:
    comparison: object = "Equals"
    def matches(self, card, *, source=None, **kwargs):
        if card is None or source is None:
            return False
        return _cmp(int(_v(card, "resource_cost", "cost", default=0) or 0), self.comparison,
                    int(_v(source, "resource_cost", "cost", default=0) or 0))

@dataclass(frozen=True)
class HasSourceCastingCostFilter:
    comparison: object = "Equals"
    add_value: int = 0
    use_source: bool = False
    def matches(self, card, *, source=None, **kwargs):
        if card is None or source is None:
            return False
        rhs = int(_v(source, "casting_cost", "cost", default=0) or 0) + int(self.add_value or 0)
        return _cmp(int(_v(card, "casting_cost", "cost", default=0) or 0), self.comparison, rhs)

@dataclass(frozen=True)
class CompareCastingCostToSourceCountersFilter:
    comparison: object = "Equals"
    counter_type: object = None
    def matches(self, card, *, source=None, **kwargs):
        if card is None or source is None:
            return False
        counters = _v(source, "counters", "card_counters", default={}) or {}
        count = counters.get(self.counter_type, 0) if isinstance(counters, dict) else 0
        return _cmp(int(_v(card, "casting_cost", "cost", default=0) or 0),
                    self.comparison, int(count or 0))

@dataclass(frozen=True)
class HasCountersValue:
    amount: int = 0
    comparison: object = "Equals"
    counter_type: object = None
    def matches(self, card, **kwargs):
        if card is None:
            return False
        counters = _v(card, "counters", "card_counters", default={}) or {}
        value = counters.get(self.counter_type, 0) if isinstance(counters, dict) else 0
        return _cmp(int(value or 0), self.comparison, int(self.amount))

def _attr_path(card, path, default=None):
    value = card
    for part in str(path or "").split(">"):
        if part.lower() == "card":
            continue
        value = _v(value, part, part.lower(), default=default)
        if value is default:
            break
    return value

@dataclass(frozen=True)
class IntAttrFilter:
    attribute: str = ""
    comparison: object = "Equals"
    value: int = 0
    compare_to_cost: bool = False
    def matches(self, card, **kwargs):
        attr = str(self.attribute or "")
        values = _v(card, "int_attrs", "intattrs", default=None)
        if isinstance(values, dict) and values:
            lhs = values.get(
                attr, values.get(attr.lower(),
                                 values.get(attr.capitalize(),
                                            _attr_path(card, attr, 0))))
        else:
            lhs = _attr_path(card, attr, 0)
        try:
            rhs = (int(_v(card, "cost", "casting_cost", default=0) or 0)
                   if self.compare_to_cost else int(self.value))
            return _cmp(int(lhs or 0), self.comparison, rhs)
        except (TypeError, ValueError):
            return False

@dataclass(frozen=True)
class StringAttrFilter:
    attribute: str = ""
    comparison: object = "Equals"
    value: str = ""
    def matches(self, card, **kwargs):
        lhs = str(_attr_path(card, self.attribute, "") or "")
        rhs = str(self.value)
        op = str(getattr(self.comparison, "name", self.comparison)).lower().replace(" ", "")
        return {"equals": lhs == rhs, "equal": lhs == rhs,
                "notequal": lhs != rhs, "notequals": lhs != rhs}.get(op, lhs == rhs)

@dataclass(frozen=True)
class SetIdFilter:
    set_id: object = None
    def matches(self, card, **kwargs): return _v(card, "set_id", "card_set_id", default=None) == self.set_id

@dataclass(frozen=True)
class SetNumberFilter:
    set_number: int = -1
    def matches(self, card, **kwargs):
        return self.set_number <= 0 or int(_v(card, "set_number", default=-1) or -1) == self.set_number

@dataclass(frozen=True)
class MatchesTargetFilter:
    target_index: int = 0
    match_name: bool = True
    def matches(self, card, *, effect=None, **kwargs):
        targets = _v(effect, "target_map", "targets", default={}) or {}
        selected = targets.get(self.target_index, targets.get(str(self.target_index), ())) if isinstance(targets, dict) else ()
        if isinstance(selected, dict): selected = selected.get("cards", selected.get("session_card_ids", ()))
        name = _v(card, "name", "template_name", default=None)
        return any((name == _v(item, "name", "template_name", default=item)) for item in (selected or ()))

@dataclass(frozen=True)
class TopNOfDeck:
    amount: int = 0
    filter: CardFilter | None = None
    def matches(self, card, *, session=None, cards=None, **kwargs):
        deck = _v(session, "deck_top", "top_deck", default=None)
        if deck is None: deck = cards
        values = tuple(deck or ())[:max(0, int(self.amount))]
        if card not in values:
            return False
        return self.filter is None or self.filter.matches(card, session=session, **kwargs)

@dataclass(frozen=True)
class TACFilter:
    template: object = None
    def matches(self, card, **kwargs):
        name = self.template if isinstance(self.template, str) else _v(self.template, "name", "template_name", default=None)
        if name == "IsQuick": return IsQuick().matches(card, **kwargs)
        if name == "IsBasic": return IsBasic().matches(card, **kwargs)
        if name == "PlayerMeetsThresholdRequirementsToCast":
            player = kwargs.get("player")
            if player is None:
                return False
            thresholds = _v(card, "thresholds", "resource_thresholds", default=()) or ()
            available = _v(player, "resource_thresholds", "thresholds", default={}) or {}
            available = {str(key): int(value or 0)
                         for key, value in available.items()}
            return all(int(available.get(str(
                           t.get("color", t.get("color_flags"))), 0)) >=
                       int(t.get("quantity", t.get("amount", 0)))
                       for t in thresholds if isinstance(t, dict))
        return False

class PlayerMeetsThresholdRequirementsToCast:
    def matches(self, card, *, player=None, **kwargs):
        return TACFilter("PlayerMeetsThresholdRequirementsToCast").matches(card, player=player, **kwargs)

@dataclass(frozen=True)
class PlayersWhoControlMatchingFilter:
    target_filter: CardFilter | None = None
    card_collection: object = None
    required_quantity: int = 0
    comparison: object = "Equals"
    def matches(self, card, *, session=None, cards=None, **kwargs):
        if not IsChampion().matches(card):
            return False
        owner = _v(card, "owner_id", "controller_id", default=None)
        candidates = [c for c in _candidate_cards(session, cards)
                      if _v(c, "owner_id", "controller_id", default=None) == owner]
        if self.card_collection is not None:
            candidates = [c for c in candidates if InZone(self.card_collection).matches(c)]
        if self.target_filter is not None:
            candidates = [c for c in candidates if self.target_filter.matches(c, **kwargs)]
        return _cmp(len(candidates), self.comparison, int(self.required_quantity))

class BlockingFilter:
    def matches(self, card, **kwargs):
        return bool(_v(card, "is_blocking", "blocking", default=False))

class BeingBlockedByFilter:
    def matches(self, card, **kwargs):
        return bool(_v(card, "is_blocked", "being_blocked", "blocked", default=False))

class OtherTroops:
    def matches(self, card, *, source=None, **kwargs):
        if card is None or card is source:
            return False
        return IsTroop().matches(card)

class IsAbilitySource:
    def matches(self, card, *, source=None, effect=None, **kwargs):
        source_id = _v(source, "session_card_id", "card_uid", "id", default=None)
        card_id = _v(card, "session_card_id", "card_uid", "id", default=None)
        effect_id = _v(effect, "ability_source_uid", "source_card_id", default=None)
        return card is source or (effect_id is not None and card_id == effect_id) or (
            source_id is not None and card_id == source_id)

class IsTopCard:
    def matches(self, card, *, session=None, **kwargs):
        top = _v(session, "top_card", "top_card_id", default=None)
        card_id = _v(card, "session_card_id", "card_uid", "id", default=None)
        return card is top or (top is not None and card_id == top)

class IsTranformed:
    def matches(self, card, **kwargs):
        return bool(_v(card, "is_transformed", "transformed", "tranformed", default=False))

class IsPvECard:
    def matches(self, card, **kwargs):
        return bool(_v(card, "is_pve", "pve", "is_pve_card", default=False))

class IsMercenaryFilter:
    def matches(self, card, **kwargs):
        return bool(_v(card, "is_mercenary", "mercenary", default=False))

class IsEquippedCardFilter:
    def matches(self, card, **kwargs):
        return bool(_v(card, "is_equipped", "equipped", default=False))

class IsStoredCardFilter:
    def matches(self, card, **kwargs):
        return bool(_v(card, "is_stored", "stored", default=False))

@dataclass(frozen=True)
class IsColorDeckBuilder:
    color: object = 0
    prismatic: bool = False
    def matches(self, card, **kwargs):
        if self.prismatic and not bool(_v(card, "is_prismatic", "prismatic", default=False)):
            return False
        return IsColor(self.color).matches(card, **kwargs)

@dataclass(frozen=True)
class AndCardFilter:
    filters: Sequence[CardFilter]
    def matches(self, card, **kwargs): return all(f.matches(card, **kwargs) for f in self.filters)

@dataclass(frozen=True)
class OrCardFilter:
    filters: Sequence[CardFilter]
    def matches(self, card, **kwargs): return any(f.matches(card, **kwargs) for f in self.filters)

@dataclass(frozen=True)
class NotCardFilter:
    filter: CardFilter
    def matches(self, card, **kwargs): return not self.filter.matches(card, **kwargs)

_FILTER_TYPES = {"inzone": InZone, "incollection": InCollection,
                 "istapped": IsTapped, "istype": IsType,
                 "isnottype": IsNotType,
                 "iscontrolledby": IsControlledBy,
                 "isnotcontrolledby": IsNotControlledBy,
                 "hasanyattributeflags": HasAnyAttributeFlags,
                 "hasallattributeflags": HasAllAttributeFlags,
                 "hascastingcost": HasCastingCost,
                 "hasresourcecost": HasResourceCost, "hasattackvalue": HasAttackValue,
                 "hasdefensevalue": HasDefenseValue,
                 "issocketable": IsSocketable, "issocketed": IsSocketed,
                 "haskeywordability": HasKeywordAbility,
                 "hastag": HasTag, "issubtype": IsSubType,
                 "ismultithresholdcard": IsMultiThresholdCard,
                 "hasname": HasName, "israrity": IsRarity,
                 "iscolor": IsColor, "infaction": InFaction,
                 "istoken": IsToken, "isresource": IsResource, "isquick": IsQuick,
                 "istroop": IsTroop, "isartifact": IsArtifact,
                 "ischampion": IsChampion, "ishero": IsHero,
                 "isalternateart": IsAlternateArt, "isextendedart": IsExtendedArt,
                 "ispromo": IsPromo,
                 "isdamaged": IsDamaged,
                 "isplayedthisturn": IsPlayedThisTurn,
                 "isdamagedthisturn": IsDamagedThisTurn,
                 "ishealedthisturn": IsHealedThisTurn,
                 "isattacking": IsAttacking, "isblocking": IsBlocking,
                 "anycard": AnyCard, "isbasic": IsBasic,
                 "isuniquecard": IsUniqueCard, "iscardname": IsCardName,
                 "namecontains": NameContainsFilter,
                 "namecontainsfilter": NameContainsFilter,
                 "hasattackedthisturn": HasAttackedThisTurn,
                 "compareattackanddefense": CompareAttackAndDefenseFilter,
                 "compareattacktolowest": CompareAttackToLowestFilter,
                 "compareattacktohighest": CompareAttackToHighestFilter,
                 "comparedefensetolowest": CompareDefenseToLowestFilter,
                 "comparehealthtolowest": CompareHealthToLowestFilter,
                 "comparehealthtohighest": CompareHealthToHighestFilter,
                 "compareresourcecosttohighest": CompareResourceCostToHighestFilter,
                 "compareresourcecosttomyhighest": CompareResourceCostToMyHighestFilter,
                 "hassourcetype": HasSourceTypeFilter,
                 "differentowners": DifferentOwners,
                 "hasasharedfactionwithsource": HasASharedFactionWithSourceFilter,
                 "hasasharedraritywithsource": HasASharedRarityWithSourceFilter,
                 "hasasharedsubtypewithsource": HasASharedSubtypeWithSourceFilter,
                 "hasasharedclasswithsourcechampion": HasASharedClassWithSourceChampionFilter,
                 "hasasharedsubtypewithsourcechampion": HasASharedSubtypeWithSourceChampionFilter,
                 "hasasharedshardwithsource": HasASharedShardWithSourceFilter,
                 "hasasharedshardwithtopofchain": HasASharedShardWithTopOfChainFilter,
                 "movedbysource": MovedBySource,
                 "ischildofabilitysource": IsChildOfAbilitySource,
                 "isparentofabilitysource": IsParentOfAbilitySourceFilter,
                 "isparentofabilitysourcefilter": IsParentOfAbilitySourceFilter,
                 "incombatwithsource": InCombatWithSourceFilter,
                 "damagedopponentthisturn": DamagedOpponentThisTurn,
                 "hassourceresourcecost": HasSourceResourceCost,
                 "hassourcecastingcost": HasSourceCastingCostFilter,
                 "comparecastingcosttosourcecounters": CompareCastingCostToSourceCountersFilter,
                 "hascountersvalue": HasCountersValue,
                 "intattr": IntAttrFilter, "stringattr": StringAttrFilter,
                 "setid": SetIdFilter, "setnumber": SetNumberFilter,
                 "matchestarget": MatchesTargetFilter, "topnofdeck": TopNOfDeck,
                 "tac": TACFilter,
                 "playermeetsthresholdrequirementstocast": PlayerMeetsThresholdRequirementsToCast,
                 "playerswhocontrolmatching": PlayersWhoControlMatchingFilter,
                 "blocking": BlockingFilter,
                 "beingblockedby": BeingBlockedByFilter,
                 "othertroops": OtherTroops,
                 "isabilitysource": IsAbilitySource,
                 "istopcard": IsTopCard,
                 "istranformed": IsTranformed,
                 "ispvecard": IsPvECard,
                 "ismercenary": IsMercenaryFilter,
                 "isequippedcard": IsEquippedCardFilter,
                 "isstoredcard": IsStoredCardFilter,
                 "iscolor_deckbuilder": IsColorDeckBuilder,
                 "iscolordeckbuilder": IsColorDeckBuilder}


def _records_filter_spec(spec):
    """Normalize a serialized Records CardFilter into the port shape."""
    # Live Records-backed effects expose RecordObject instances while some
    # persisted/test paths provide the already-unwrapped raw dictionaries.
    # Keep the adapter boundary tolerant of both representations.
    raw = getattr(spec, "raw", None)
    if isinstance(raw, dict):
        spec = raw
    if not isinstance(spec, dict):
        return spec
    kind = str(spec.get("_t", spec.get("type", ""))).rsplit(".", 1)[-1]
    if kind in ("AndCardFilter", "OrCardFilter", "StaticAndCardFilter",
                "StaticOrCardFilter"):
        return {"type": kind, "filters": [
            _records_filter_spec(value)
            for value in spec.get("m_TargetFilters", spec.get("filters", ()))
        ]}
    if kind in ("NotCardFilter", "StaticNotCardFilter"):
        child = spec.get("m_TargetFilter", spec.get("filter", {}))
        return {"type": kind, "filter": _records_filter_spec(child)}
    result = {"type": kind}
    for key, value in spec.items():
        if key == "_t":
            continue
        result[key] = value
    return result


def records_filter_from_metadata(spec: Any) -> CardFilter:
    """Build a RulesPort filter from the client's serialized Records tree."""
    if not spec:
        return AnyCard()
    normalized = _records_filter_spec(spec)
    if not isinstance(normalized, dict):
        return AnyCard()
    kind = str(normalized.get("type", "")).rsplit(".", 1)[-1].lower()
    if not kind:
        return AnyCard()
    if kind in ("andcardfilter", "orcardfilter",
                "staticandcardfilter", "staticorcardfilter"):
        children = tuple(records_filter_from_metadata(item)
                         for item in normalized.get("filters", ()))
        is_or = "or" in kind
        return OrCardFilter(children) if is_or else AndCardFilter(children)
    if kind in ("notcardfilter", "staticnotcardfilter"):
        return NotCardFilter(records_filter_from_metadata(normalized.get("filter", {})))
    if kind == "tacfilter":
        # TACFilter stores its operation in the client's compact binary blob;
        # it is not present as a normal ``template`` field in Records JSON.
        # Decode it at the Records adapter boundary so the kernel receives
        # the same semantic operation as the original client.
        serialized = normalized.get("m_SerializedTAC", {})
        data = serialized.get("data", "") if isinstance(serialized, dict) else serialized
        from .tac import tac_string
        normalized["template"] = tac_string(data, "Name")
    if kind == "istype":
        from domain.enums import card_type_from_db
        normalized["card_type"] = card_type_from_db(
            normalized.get("m_CardType", normalized.get("card_type", "")))
        normalized.pop("m_CardType", None)
    if kind == "isartifact":
        # The generic factory historically exposes the compact test/filter
        # bit used by the port tests, while serialized Records metadata uses
        # the authoritative ECardTypes.Artifact bit.  Keep that distinction
        # at the adapter boundary instead of weakening IsType globally.
        from domain.enums import ECardTypes
        return IsType(ECardTypes.Artifact)
    if kind in ("inzone", "incollection"):
        from domain.enums import ECardCollections
        collections = {name.lower(): value for name, value in vars(
            ECardCollections).items() if not name.startswith("_")}
        # Records uses both the UI name "Crypt" and the runtime name
        # "Discard" for the same collection.  Keep this translation in the
        # Records adapter so the kernel only sees canonical collection bits.
        collections.update({
            "crypt": ECardCollections.Discard,
            "crypts": ECardCollections.Discard,
            "graveyard": ECardCollections.Discard,
            "graveyards": ECardCollections.Discard,
        })
        raw = str(normalized.get("m_Collection", normalized.get(
            "collection", ""))).rsplit(".", 1)[-1].lower()
        normalized["collection"] = collections.get(raw, 0)
        normalized.pop("m_Collection", None)
    return filter_from_metadata(normalized)


def records_filter_matches(card, spec, *, source=None, context=None,
                            player=None):
    """Evaluate one Records filter using the RulesPort card predicate set."""
    from domain.enums import ECardStates, card_type_from_db

    if not spec:
        # An empty/absent filter (for example TopNOfDeck with ``m_Filter``
        # null, used by Nerissa's reveal power) matches every candidate.
        return True

    def normalize_runtime_flags(value):
        # ``game_cards.card_state`` is the authoritative mutable card state;
        # the Records filter layer should expose the same derived predicates
        # as CardRepresentation instead of requiring every DB adapter to
        # duplicate them as booleans.
        if "state" in value:
            state = int(value.get("state") or 0)
            value.setdefault("is_attacking", bool(
                state & int(ECardStates.Attacking)))
            value.setdefault("is_blocking", bool(
                state & int(ECardStates.Blocking)))
            value.setdefault("tapped", bool(
                state & int(ECardStates.Tapped)))
            value.setdefault("is_tapped", value["tapped"])
            # C# derives these turn-history predicates from the same
            # authoritative bitmask (Card.CameOutThisTurn / Damaged / Healed /
            # HasAttacked).  Without them the Is*ThisTurn filters always
            # matched nothing.
            value.setdefault("played_this_turn", bool(
                state & int(ECardStates.CameOutThisTurn)))
            value.setdefault("came_out_this_turn", value["played_this_turn"])
            value.setdefault("damaged_this_turn", bool(
                state & int(ECardStates.Damaged)))
            value.setdefault("healed_this_turn", bool(
                state & int(ECardStates.Healed)))
            value.setdefault("attacked_this_turn", bool(
                state & int(ECardStates.HasAttacked)))
            value.setdefault("has_attacked_this_turn",
                             value["attacked_this_turn"])
            if (value.get("template_guid") and
                    value.get("original_template_guid")):
                value.setdefault(
                    "is_transformed",
                    value["original_template_guid"] != value["template_guid"])
            if isinstance(value.get("shards"), (list, tuple, set)):
                value.setdefault("thresholds", list(value["shards"]))
        return value

    value = dict(card or {})
    normalize_runtime_flags(value)
    raw_type = value.get("card_type")
    if isinstance(raw_type, str):
        value["_card_type_name"] = raw_type
        value["card_type"] = card_type_from_db(raw_type)
        value["is_hero"] = "Champion" in raw_type.split("|")
        value["is_resource"] = "Resource" in raw_type.split("|")
    if "user_id" in value:
        value.setdefault("owner_id", value["user_id"])
        value.setdefault("controller_id", value["user_id"])
    location = str(value.get("location", "")).lower()
    if location:
        from domain.enums import ECardCollections
        value["collection"] = {name.lower(): number for name, number in vars(
            ECardCollections).items() if not name.startswith("_")}.get(location, 0)
    source_value = None
    if source is not None:
        source_value = dict(source)
        normalize_runtime_flags(source_value)
        source_type = source_value.get("card_type")
        if isinstance(source_type, str):
            source_value["_card_type_name"] = source_type
            source_value["card_type"] = card_type_from_db(source_type)
            source_value["is_hero"] = "Champion" in source_type.split("|")
            source_value["is_resource"] = "Resource" in source_type.split("|")
        if "user_id" in source_value:
            source_value.setdefault("owner_id", source_value["user_id"])
            source_value.setdefault("controller_id", source_value["user_id"])
    source_owner = (source_value or {}).get("controller_id")
    # A typed TAC filter needs the active player's threshold pool, not the
    # numeric owner ID.  The Records adapter receives the execution context
    # so it can expose the persisted RulesPort state without teaching the
    # generic filter classes about PvP storage keys.
    # Keep an explicitly supplied player ID intact.  Ownership predicates
    # (notably IsControlledBy) compare against this value.  Threshold-aware
    # callers that omit ``player`` still receive the client-shaped threshold
    # pool below; replacing an explicit ID with that pool makes every
    # contextual self-controlled target appear uncontrolled.
    explicit_player = player is not None
    player = player if explicit_player else source_owner
    if context is not None and player == source_owner:
        state = getattr(context, "bstate", {}) or {}
        # TAC is evaluated for the player resolving the ability.  The source
        # card's controller is normally the same player, but generated and
        # champion projections can omit or temporarily carry a different
        # controller representation.
        active_owner = state.get("resolving_owner_id", source_owner)
        thresholds = (state.get(f"thresh_{active_owner}")
                      if active_owner is not None else None)
        # PvP effect execution uses the side-oriented RulesPort view rather
        # than the raw checkpoint.  Its active/opponent pools are named
        # player_threshold and ai_threshold respectively.
        if thresholds is None and active_owner is not None and int(active_owner) == int(
                state.get("resolving_owner_id", -1) or -1):
            thresholds = state.get("player_threshold")
        if thresholds is None:
            thresholds = state.get("ai_threshold")
        normalized_thresholds = {}
        # NOTE: the loop variable must not shadow ``value`` (the card dict
        # being filtered).  It previously did, so every Records filter
        # evaluated with a threshold context compared against an int and
        # returned False — e.g. Subterranean Spy's "while underground" gate
        # (ThisIsUnderground) always failed and the hand was never revealed.
        for threshold_key, threshold_value in (thresholds or {}).items():
            try:
                threshold_key = int(threshold_key)
            except (TypeError, ValueError):
                pass
            normalized_thresholds[threshold_key] = int(threshold_value or 0)
        if not explicit_player:
            player = {"resource_thresholds": normalized_thresholds}
    return records_filter_from_metadata(spec).matches(
        value, source=source_value, player=player, session=context)

def filter_from_metadata(spec: Any) -> CardFilter:
    if not isinstance(spec, dict):
        raise TypeError("filter metadata must be a mapping")
    kind = str(spec.get("type", spec.get("class", spec.get("filter_type", ""))))
    kind = kind.rsplit(".", 1)[-1].replace("Filter", "").lower()
    if kind in {"and", "andcard", "staticand", "staticandcard"}:
        return AndCardFilter(tuple(filter_from_metadata(item) for item in spec.get("filters", ())))
    if kind in {"or", "orcard", "staticor", "staticorcard"}:
        return OrCardFilter(tuple(filter_from_metadata(item) for item in spec.get("filters", ())))
    if kind in {"not", "notcard", "staticnot", "staticnotcard"}:
        return NotCardFilter(filter_from_metadata(spec["filter"]))
    cls = _FILTER_TYPES.get(kind)
    if cls is None:
        raise ValueError(f"unsupported card filter type: {kind or '<missing>'}")
    values = {key: value for key, value in spec.items()
              if key not in {"type", "class", "filter_type"}}
    values = {(key[2:] if str(key).startswith("m_") else key): value
              for key, value in values.items()}
    aliases = {"card_collection": "collection", "cardcollection": "collection",
               "card_type": "card_type", "cardtype": "card_type",
               "card_name": "name", "cardname": "name",
               "contains_string": "value", "containsstring": "value",
               "attack_value": "value", "attackvalue": "value",
               "defense_value": "value", "defensevalue": "value",
               "resource_cost": "cost", "resourcecost": "cost",
               "attribute_flags": "attribute_flags", "attributeflags": "attribute_flags",
               "casting_cost": "cost", "castingcost": "cost",
               "comparison_op": "comparison", "comparisonop": "comparison",
               "counter_type": "counter_type", "countertype": "counter_type",
               "socket_value": "socket_value", "socketvalue": "socket_value",
               "color_flags": "color", "colorflags": "color",
               "compare_to_ability_source_defense": "compare_to_ability_source_defense",
               "comparetoabilitysourcedefense": "compare_to_ability_source_defense",
               "compare_to_ability_source_attack": "compare_to_ability_source_attack",
               "comparetoabilitysourceattack": "compare_to_ability_source_attack",
               "comparetoabilitysource": "compare_to_source",
               "comparetocost": "compare_to_cost",
               "addx": "add_x",
               "addattack": "add_attack",
               "adddefense": "add_defense",
               "addcardintegervariable": "add_card_integer_variable",
               "addvariable": "add_variable",
               "cardattributeflags": "attribute_flags",
               "usestoredname": "use_stored_name",
               "socketedvalue": "socketed_value",
               "testagainstactiveplayer": "test_against_active_player",
               "dontexactlymatchoriginal": "dont_exactly_match_original",
               "onlycombatdamage": "only_combat_damage",
               "onlynoncombatdamage": "only_non_combat_damage",
               "usesource": "use_source",
               "addvalue": "add_value",
               "targetindex": "target_index",
               "matchname": "match_name",
               "includeresources": "include_resources",
               "exactmatch": "exact_match",
               "storedshard": "stored_shard",
               "failuncontrolledcards": "fail_uncontrolled_cards"}
    kwargs = {aliases.get(str(key).lower(), str(key).lower()): value
              for key, value in values.items()}
    for key in ("counter_type", "counter_template_guid"):
        value = kwargs.get(key)
        if isinstance(value, dict):
            kwargs[key] = value.get("m_Guid", value.get("guid", ""))
    # Several client filter classes are marker types with no constructor
    # fields.  Records retain unrelated serialized fields on those objects;
    # discard only fields not accepted by the port class.
    parameters = set(inspect.signature(cls).parameters)
    kwargs = {key: value for key, value in kwargs.items() if key in parameters}
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ValueError(f"invalid metadata for {kind}: {exc}") from exc
