"""Mechanical port of core ``TransactionRequirement`` validation objects.

The C# classes validate server-owned state before resolving a transaction.
They are deliberately small predicate objects here so each decoded HConnect
transaction can declare its complete validation contract instead of relying on
an endpoint-specific `if` chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Iterable, Protocol

from .phases import phase_name


class EComparisons(IntEnum):
    """Exact numeric order of ``HexClient.Game.Shared.EComparisons``."""
    LessThan = 0
    LessThanOrEqual = 1
    GreaterThan = 2
    GreaterThanOrEqual = 3
    Equals = 4
    NotEqual = 5
    OneMoreThan = 6
    TwoMoreThan = 7
    OneLessThan = 8


class Requirement(Protocol):
    def is_valid(self, session, player_id) -> bool: ...


def _value(item, *names, default=None):
    """Read existing card/player adapters without introducing a second model."""
    for name in names:
        if isinstance(item, dict) and name in item:
            return item[name]
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _card(session, card_id):
    lookup = getattr(session, "get_card", None)
    return lookup(card_id) if callable(lookup) else None


def _player(session, player_id):
    lookup = getattr(session, "get_player", None)
    return lookup(player_id) if callable(lookup) else None


def _bool_call(owner, method: str, *args) -> bool:
    callback = getattr(owner, method, None)
    return bool(callback(*args)) if callable(callback) else False


def _same_player_id(session, left, right) -> bool:
    """Compare participant identities across wire and native UID domains.

    The client serializes player IDs as uint64 values while the native rules
    session normally keeps typed ``UID`` objects.  A reconnect or a projected
    AI action can therefore leave one side of a requirement as the raw value
    even though it names the same participant.
    """
    coerce = getattr(session, "coerce_transaction_player_id", None)
    if callable(coerce):
        left = coerce(left)
        right = coerce(right)
    if left == right:
        return True
    try:
        return int(getattr(left, "uid64", left)) == int(
            getattr(right, "uid64", right))
    except (TypeError, ValueError):
        return False


def _compare(lhs, operation, rhs) -> bool:
    op = getattr(operation, "name", operation)
    op = str(op).replace("_", "").replace(" ", "").lower()
    try:
        if op in {"lessthan", "less", "0"}: return lhs < rhs
        if op in {"lessthanorequal", "lessorequal", "1"}: return lhs <= rhs
        if op in {"greaterthan", "greater", "2"}: return lhs > rhs
        if op in {"greaterthanorequal", "greaterorequal", "3"}: return lhs >= rhs
        if op in {"equals", "equal", "4"}: return lhs == rhs
        if op in {"notequals", "notequal", "5"}: return lhs != rhs
        if op in {"onemorethan", "6"}: return lhs == rhs + 1
        if op in {"twomorethan", "7"}: return lhs == rhs + 2
        if op in {"onelessthan", "8"}: return lhs == rhs - 1
    except TypeError:
        return False
    return False


@dataclass(frozen=True)
class TurnPhaseRequirement:
    required_phase: object

    def is_valid(self, session, player_id) -> bool:
        return (phase_name(self.required_phase) == "Unknown" or
                phase_name(session.current_turn_phase) == phase_name(self.required_phase))


@dataclass(frozen=True)
class PlayerHasPriorityRequirement:
    player_id: object

    def is_valid(self, session, player_id) -> bool:
        return (_same_player_id(session, self.player_id, player_id) and
                _same_player_id(session, player_id,
                                session.action_stack.priority_player_id))


@dataclass(frozen=True)
class PlayerIsActiveRequirement:
    player_id: object

    def is_valid(self, session, player_id) -> bool:
        return self.player_id == player_id == session.active_player_id


@dataclass(frozen=True)
class PlayerIsInactiveRequirement:
    player_id: object

    def is_valid(self, session, player_id) -> bool:
        checker = getattr(session, "is_inactive_player", None)
        return (self.player_id == player_id and bool(checker(self.player_id))
                if callable(checker) else self.player_id != session.active_player_id)


@dataclass(frozen=True)
class PlayerIsStartingPlayerRequirement:
    player_id: object

    def is_valid(self, session, player_id) -> bool:
        starting = getattr(session, "starting_player_id", None)
        if starting is None:
            checker = getattr(session, "get_starting_player_id", None)
            starting = checker() if callable(checker) else None
        return self.player_id == player_id and starting == self.player_id


@dataclass(frozen=True)
class PlayerHasChargePointsRequirement:
    player_id: object
    charge_points: int

    def is_valid(self, session, player_id) -> bool:
        player = _player(session, self.player_id)
        try:
            return self.player_id == player_id and player is not None and int(
                _value(player, "charge_points", default=-1)) >= int(self.charge_points)
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class CardInPlayableLocationRequirement:
    player_id: object
    card_id: object

    def is_valid(self, session, player_id) -> bool:
        if self.player_id != player_id:
            return False
        card = _card(session, self.card_id)
        return card is not None and _bool_call(session, "is_in_playable_location",
                                               card, self.player_id)


@dataclass(frozen=True)
class SpaceInCardCollectionRequirement:
    player_id: object
    collection: object

    def is_valid(self, session, player_id) -> bool:
        if self.player_id != player_id:
            return False
        checker = getattr(session, "has_space_in_collection", None)
        return bool(checker(self.player_id, self.collection)) if callable(checker) else False


@dataclass(frozen=True)
class CardCountRequirement:
    player_id: object
    collection: object
    comparison: object
    quantity: int

    def is_valid(self, session, player_id) -> bool:
        if self.player_id != player_id:
            return False
        checker = getattr(session, "card_collection_count", None)
        if not callable(checker):
            checker = getattr(getattr(session, "runtime_facts", None),
                              "card_collection_count", None)
        if not callable(checker):
            return False
        try:
            return _compare(int(checker(self.player_id, self.collection)),
                            self.comparison, int(self.quantity))
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class TurnCountRequirement:
    comparison: object
    quantity: int

    def is_valid(self, session, player_id) -> bool:
        return _compare(getattr(session, "total_turns_taken", 0),
                        self.comparison, self.quantity)


class EmptyChainRequirement:
    def is_valid(self, session, player_id) -> bool:
        return session.chain.is_empty


@dataclass(frozen=True)
class AttackExistsRequirement:
    combat_id: object

    def is_valid(self, session, player_id) -> bool:
        return bool(getattr(session, "combat_manager", None) and
                    session.combat_manager.contains(self.combat_id))


@dataclass(frozen=True)
class AllDamageAssignedRequirement:
    combat_id: object
    damage_order: tuple[object, ...] | None

    def __init__(self, combat_id, damage_order):
        object.__setattr__(self, "combat_id", combat_id)
        object.__setattr__(self, "damage_order", None if damage_order is None
                           else tuple(damage_order))

    def is_valid(self, session, player_id) -> bool:
        combat = session.combat_manager.get(self.combat_id)
        return (combat is not None and
                ((self.damage_order is None and not combat.blockers) or
                 (self.damage_order is not None and
                  len(self.damage_order) == len(combat.blockers))))


@dataclass(frozen=True)
class AttackerIsValidRequirement:
    """Direct port of C# ``AttackerIsValid`` through card-combat facts."""

    defending_card_id: object
    attacking_card_id: object

    def is_valid(self, session, player_id) -> bool:
        attacker = _card(session, self.attacking_card_id)
        defender = _card(session, self.defending_card_id)
        # Champion targets are represented by SessionCardIds in the wire
        # declaration but are not rows in ``game_cards``.  The runtime facts
        # adapter validates that target as an opponent champion when
        # ``defender`` is None; do not reject it merely because it has no
        # mutable card row.
        return (attacker is not None and
                _bool_call(session, "can_attack", attacker, defender, player_id))


@dataclass(frozen=True)
class DefenseDeclarationsLegalRequirement:
    """The client validates the complete blocker set as one transaction."""

    declarations: tuple[tuple[object, tuple[object, ...]], ...]

    def is_valid(self, session, player_id) -> bool:
        return _bool_call(session, "are_defense_declarations_legal",
                          self.declarations, player_id)


@dataclass(frozen=True)
class BlocksAreValidRequirement:
    combat_ids: tuple[object, ...]
    blocking_card_ids: tuple[object, ...]

    def __init__(self, combat_ids, blocking_card_ids):
        object.__setattr__(self, "combat_ids", tuple(combat_ids or ()))
        object.__setattr__(self, "blocking_card_ids", tuple(blocking_card_ids or ()))

    def is_valid(self, session, player_id) -> bool:
        cards = tuple(_card(session, card_id) for card_id in self.blocking_card_ids)
        if any(card is None for card in cards):
            return False
        return _bool_call(session, "are_blocks_legal", self.combat_ids, cards)


@dataclass(frozen=True)
class CardCanBePlayedRequirement:
    """Direct port of the client requirement; legality stays in the adapter."""

    card_id: object
    playing_for_free: bool = False

    def is_valid(self, session, player_id) -> bool:
        card = _card(session, self.card_id)
        return card is not None and _bool_call(
            session, "can_play_card", card, player_id, self.playing_for_free)


@dataclass(frozen=True)
class CardInCollectionRequirement:
    player_id: object
    collection_flags: int
    card_id: object

    def is_valid(self, session, player_id) -> bool:
        # The C# predicate uses player-id validity as a guard, then tests the
        # card's collection bitmask. Ownership is enforced by CanPlay/card
        # operation adapters where relevant.
        card = _card(session, self.card_id)
        collection = _value(card, "collection", "current_collection",
                            "current_card_collection", default=0)
        try:
            return self.player_id is not None and card is not None and bool(
                int(self.collection_flags) & int(collection))
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class CardTypeRequirement:
    card_id: object
    card_type: int

    def is_valid(self, session, player_id) -> bool:
        card = _card(session, self.card_id)
        has_type = getattr(card, "has_type", None)
        if callable(has_type):
            return bool(has_type(self.card_type))
        try:
            return card is not None and bool(int(_value(card, "card_type", "type", default=0)) &
                                             int(self.card_type))
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class CardTappedRequirement:
    card_id: object

    def is_valid(self, session, player_id) -> bool:
        card = _card(session, self.card_id)
        tapped = getattr(card, "is_tapped", None)
        return bool(tapped()) if callable(tapped) else bool(
            _value(card, "tapped", "is_exhausted", default=False))


@dataclass(frozen=True)
class CardCanUntapRequirement:
    card_id: object

    def is_valid(self, session, player_id) -> bool:
        card = _card(session, self.card_id)
        can_ready = getattr(card, "can_ready_at_start_of_turn", None)
        return bool(can_ready()) if callable(can_ready) else bool(
            _value(card, "can_ready", "can_untap", default=False))


@dataclass(frozen=True)
class CardUntappedRequirement:
    card_id: object

    def is_valid(self, session, player_id) -> bool:
        card = _card(session, self.card_id)
        tapped = getattr(card, "is_tapped", None)
        return card is not None and (not bool(tapped()) if callable(tapped)
                                     else not bool(_value(card, "tapped", "is_exhausted", default=True)))


@dataclass(frozen=True)
class PlayerHasResourceCountRequirement:
    player_id: object
    resource_count: int

    def is_valid(self, session, player_id) -> bool:
        player = _player(session, self.player_id)
        try:
            return self.player_id == player_id and player is not None and int(
                _value(player, "current_resource_pool", "resources", default=-1)) >= int(self.resource_count)
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class SessionCardAttributesRequirement:
    attribute_flags: int

    def is_valid(self, session, player_id) -> bool:
        try:
            return bool(int(getattr(session, "current_attribute_restrictions", 0)) & int(self.attribute_flags))
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class SpellAllowedInTurnPhaseRequirement:
    player_id: object
    card_id: object

    def is_valid(self, session, player_id) -> bool:
        if self.player_id != player_id:
            return False
        card = _card(session, self.card_id)
        if card is None or session.action_stack.priority_player_id != self.player_id:
            return False
        phase = phase_name(session.current_turn_phase)
        quick = _value(card, "is_quick_action", "quick_action", default=False)
        if callable(quick):
            quick = quick()
        if quick:
            return phase.endswith("PriorityWindow")
        spell = _value(card, "is_spell", default=False)
        if callable(spell):
            spell = spell()
        return bool(spell and session.active_player_id == self.player_id and
                    phase in {"FirstMainPhase", "SecondMainPhase",
                               "FirstMainPhasePriorityWindow",
                               "SecondMainPhasePriorityWindow"})


@dataclass(frozen=True)
class CardCastingCostRequirement:
    player_id: object
    card_id: object
    x_cost: int = 0

    def is_valid(self, session, player_id) -> bool:
        player, card = _player(session, self.player_id), _card(session, self.card_id)
        try:
            pool = _value(player, "current_resource_pool", "resources", default=-1)
            cost = _value(card, "casting_cost", "cost", default=0)
            return player is not None and card is not None and int(pool) >= int(cost) + int(self.x_cost)
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class CardResourceThresholdRequirement:
    player_id: object
    card_id: object

    def is_valid(self, session, player_id) -> bool:
        player, card = _player(session, self.player_id), _card(session, self.card_id)
        thresholds = _value(card, "thresholds", "resource_thresholds", default=())
        available = _value(player, "resource_thresholds", "thresholds", default={})
        if player is None or card is None or not isinstance(available, dict):
            return False
        for threshold in thresholds or ():
            color = _value(threshold, "color", "color_flags", "shard")
            needed = _value(threshold, "amount", "requirement",
                            "threshold_color_requirement", default=0)
            try:
                if color not in available or int(available[color]) < int(needed):
                    return False
            except (TypeError, ValueError):
                return False
        return True


@dataclass(frozen=True)
class AbilityCanBeActivatedRequirement:
    source_card_id: object
    ability_template_id: object

    def is_valid(self, session, player_id) -> bool:
        card = _card(session, self.source_card_id)
        if card is not None:
            return _bool_call(session, "can_activate_ability", card, player_id,
                              self.ability_template_id)
        # Champion cards are synthetic SessionCardIds and therefore have no
        # mutable game_cards row.  Delegate their activation gate to the
        # runtime facts bridge, which knows the attached champion IDs and
        # Records-backed ability metadata.
        checker = getattr(getattr(session, "runtime_facts", None),
                          "can_activate_champion_ability", None)
        return bool(checker(self.source_card_id, player_id,
                            self.ability_template_id)) if callable(checker) else False


@dataclass(frozen=True)
class AbilityCostRequirement:
    player_id: object
    ability_template_id: object

    def is_valid(self, session, player_id) -> bool:
        if self.player_id != player_id:
            return False
        checker = getattr(session, "ability_cost_is_paid", None)
        if not callable(checker):
            checker = getattr(getattr(session, "runtime_facts", None),
                              "ability_cost_is_paid", None)
        return bool(checker(self.player_id, self.ability_template_id)) if callable(checker) else False


@dataclass(frozen=True)
class AbilityHasTriggerRequirement:
    """Port of the template trigger predicate used by manual activation."""
    ability_template_id: object

    def is_valid(self, session, player_id) -> bool:
        checker = getattr(session, "ability_has_trigger", None)
        if callable(checker):
            return bool(checker(self.ability_template_id))
        facts = getattr(session, "runtime_facts", None)
        checker = getattr(facts, "ability_has_trigger", None)
        if callable(checker):
            return bool(checker(self.ability_template_id))
        return False


@dataclass(frozen=True)
class AbilityIsManuallyActivatedRequirement:
    """Port of ``AbilityTemplate.IsManual`` through an adapter seam."""
    ability_template_id: object

    def is_valid(self, session, player_id) -> bool:
        checker = getattr(session, "ability_is_manual", None)
        if callable(checker):
            return bool(checker(self.ability_template_id))
        facts = getattr(session, "runtime_facts", None)
        checker = getattr(facts, "ability_is_manual", None)
        if callable(checker):
            return bool(checker(self.ability_template_id))
        # Older metadata projections did not materialize this field. Preserve
        # compatibility while treating explicitly supplied values as authority.
        return True


@dataclass(frozen=True)
class AbilityHasValidTargetsRequirement:
    ability_instance_id: int
    activation_data: Any

    def is_valid(self, session, player_id) -> bool:
        data = self.activation_data
        if not isinstance(data, dict):
            return False
        if data.get("disable") or (data.get("opted") and data.get("target_map") is None):
            return True
        if data.get("target_map") is None:
            return False
        ability = session.ability_manager.get(self.ability_instance_id)
        if ability is None:
            return False
        return _bool_call(session, "validate_ability_targets", ability, data,
                          player_id)


@dataclass(frozen=True)
class PlayerIsAtFrontOfTriggeredAbilityQueueRequirement:
    player_id: object

    def is_valid(self, session, player_id) -> bool:
        checker = getattr(session, "is_at_triggered_ability_queue_front", None)
        return (self.player_id == player_id and
                bool(checker(self.player_id)) if callable(checker) else False)


@dataclass(frozen=True)
class AbilitiesAreTriggeredRequirement:
    player_id: object
    activation_data: tuple[Any, ...]

    def __init__(self, player_id, activation_data):
        object.__setattr__(self, "player_id", player_id)
        object.__setattr__(self, "activation_data", tuple(activation_data or ()))

    def is_valid(self, session, player_id) -> bool:
        checker = getattr(session, "validate_triggered_abilities", None)
        return (self.player_id == player_id and callable(checker) and
                bool(checker(self.player_id, self.activation_data)))


@dataclass(frozen=True)
class XCostRequirement:
    activation_data: Any

    def is_valid(self, session, player_id) -> bool:
        data = self.activation_data
        if not isinstance(data, dict):
            return False
        if data.get("disable") or not data.get("opted", True):
            return True
        return _bool_call(session, "validate_x_cost", player_id, data)


class MainPhaseRequirement:
    def is_valid(self, session, player_id) -> bool:
        return phase_name(session.current_turn_phase) in {
            "FirstMainPhase", "SecondMainPhase"}


@dataclass(frozen=True)
class QuickActionCardRequirement:
    """Allow an instant-speed card at any client priority checkpoint.

    Practice exposes priority while the phase enum is still StartTurn/Ready/
    Prep/Draw when an AI chain item is waiting.  The client can cast a
    QuickAction there, but a plain MainPhase/priority-window enum check rejects
    it before the card's authored speed is considered.
    """
    card_id: object

    def is_valid(self, session, player_id) -> bool:
        card = _card(session, self.card_id)
        if card is None:
            return False
        try:
            card_type = int(_value(card, "card_type", "type", default=0))
            attributes = int(_value(card, "attributes", default=0))
            # ECardTypes.QuickAction is 64.  A troop/artifact can also gain
            # the QuickAction card attribute (for example Electroid while
            # Subterranean Saboteur's effect is active), so either form is
            # sufficient for client-speed legality.
            return bool(card_type & 64 or attributes & 268435456)
        except (TypeError, ValueError):
            return False


class PriorityWindowRequirement:
    def is_valid(self, session, player_id) -> bool:
        return phase_name(session.current_turn_phase).endswith("PriorityWindow")


@dataclass(frozen=True)
class PlayerIsNotEliminatedRequirement:
    player_id: object

    def is_valid(self, session, player_id) -> bool:
        checker = getattr(session, "is_player_eliminated", None)
        return (self.player_id == player_id and
                not bool(checker(self.player_id)) if callable(checker) else
                self.player_id == player_id and self.player_id not in
                getattr(session, "eliminated_player_ids", ()))


@dataclass(frozen=True)
class AbilityExistsRequirement:
    ability_instance_id: int

    def is_valid(self, session, player_id) -> bool:
        return session.ability_manager.get(self.ability_instance_id) is not None


@dataclass(frozen=True)
class PlayerIsResponsibleForAbilityRequirement:
    player_id: object
    ability_instance_id: int

    def is_valid(self, session, player_id) -> bool:
        ability = session.ability_manager.get(self.ability_instance_id)
        return (ability is not None and self.player_id == player_id and
                getattr(ability, "responsible_player_id", None) == self.player_id)


@dataclass(frozen=True)
class AndRequirement:
    requirements: tuple[Requirement, ...]

    def __init__(self, requirements: Iterable[Requirement]):
        object.__setattr__(self, "requirements", tuple(requirements))

    def is_valid(self, session, player_id) -> bool:
        return all(requirement.is_valid(session, player_id)
                   for requirement in self.requirements)


@dataclass(frozen=True)
class OrRequirement:
    requirements: tuple[Requirement, ...]

    def __init__(self, requirements: Iterable[Requirement]):
        object.__setattr__(self, "requirements", tuple(requirements))

    def is_valid(self, session, player_id) -> bool:
        return any(requirement.is_valid(session, player_id)
                   for requirement in self.requirements)


@dataclass(frozen=True)
class NotRequirement:
    requirement: Requirement

    def is_valid(self, session, player_id) -> bool:
        return not self.requirement.is_valid(session, player_id)


_REQUIREMENT_TYPES = {name.lower(): value for name, value in globals().items()
                      if isinstance(value, type) and name.endswith("Requirement")}
_REQUIREMENT_TYPES.update({"cardinplayablelocation": CardInPlayableLocationRequirement,
                           "attackerisvalid": AttackerIsValidRequirement,
                           "blocksarevalid": BlocksAreValidRequirement,
                           "abilityhasvalidtargets": AbilityHasValidTargetsRequirement,
                           "cardcanuntap": CardCanUntapRequirement,
                           "cardtapped": CardTappedRequirement,
                           "carduntapped": CardUntappedRequirement})

def requirement_from_metadata(spec: Any) -> Requirement:
    """Instantiate one C#-shaped requirement from normalized metadata."""
    if not isinstance(spec, dict):
        raise TypeError("requirement metadata must be a mapping")
    kind = str(spec.get("type", spec.get("class", ""))).rsplit(".", 1)[-1]
    key = kind.lower()
    if key in {"and", "andrequirement"}:
        return AndRequirement(tuple(requirement_from_metadata(item)
                                     for item in spec.get("requirements", ())))
    if key in {"or", "orrequirement"}:
        return OrRequirement(tuple(requirement_from_metadata(item)
                                    for item in spec.get("requirements", ())))
    if key in {"not", "notrequirement"}:
        return NotRequirement(requirement_from_metadata(spec["requirement"]))
    cls = _REQUIREMENT_TYPES.get(key)
    if cls is None:
        raise ValueError(f"unsupported requirement type: {kind or '<missing>'}")
    values = {k: v for k, v in spec.items() if k not in {"type", "class"}}
    values = {(k[2:] if str(k).startswith("m_") else k): v
              for k, v in values.items()}
    aliases = {"phase": "required_phase", "requiredphase": "required_phase",
               "card": "card_id",
               "cardid": "card_id", "playerid": "player_id",
               "ability": "ability_instance_id", "activation": "activation_data"}
    try:
        return cls(**{aliases.get(str(k).lower(), str(k).lower()): v
                      for k, v in values.items()})
    except TypeError as exc:
        raise ValueError(f"invalid metadata for {kind}: {exc}") from exc
