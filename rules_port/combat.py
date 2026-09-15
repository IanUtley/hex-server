"""Direct Python port of ``Combat``, ``CombatManager`` and ``CombatResolver``.

Damage remains a callback because the existing server already owns SQLite card
state, damage events, lifegain and trigger dispatch.  This module owns the C#
combat declaration, blocker order and damage-allocation semantics only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from typing import Callable, Iterable, Optional


class CombatFlags(IntFlag):
    NONE = 0
    ATTACKERS_DECLARED = 1
    BLOCKERS_DECLARED = 2
    DAMAGE_ASSIGNED = 4
    ATTACK_BLOCKED = 8
    FIRST_STRIKE_RESOLVED = 16
    RESOLVED = 32


class CombatPhase(IntFlag):
    NONE = 0
    STANDARD = 1
    FIRST_STRIKE = 2


@dataclass(frozen=True)
class CombatId:
    attacker_id: int | None = None
    serial_number: int = 0

    @property
    def is_valid(self) -> bool:
        return self.attacker_id is not None and self.serial_number > 0


@dataclass
class CombatResult:
    source: object
    target: object
    damage: int


@dataclass
class Combat:
    instigator: object
    defender: object
    combat_id: CombatId
    attacker: object | None = None
    blockers: list[object] = field(default_factory=list)
    flags: CombatFlags = CombatFlags.NONE
    resolved_phases: CombatPhase = CombatPhase.NONE

    @property
    def is_attack_declared(self) -> bool:
        return bool(self.flags & CombatFlags.ATTACKERS_DECLARED)

    @property
    def is_defense_declared(self) -> bool:
        return bool(self.flags & CombatFlags.BLOCKERS_DECLARED)

    @property
    def is_damage_assigned(self) -> bool:
        return bool(self.flags & CombatFlags.DAMAGE_ASSIGNED)

    @property
    def is_attack_blocked(self) -> bool:
        return bool(self.flags & CombatFlags.ATTACK_BLOCKED)

    def declare_attacker(self, attacker) -> bool:
        self.attacker = attacker
        self.flags |= CombatFlags.ATTACKERS_DECLARED
        return True

    def declare_blockers(self, blockers: Optional[Iterable[object]]) -> bool:
        self.blockers = list(blockers or ())
        self.flags |= CombatFlags.BLOCKERS_DECLARED
        if self.blockers:
            self.flags |= CombatFlags.ATTACK_BLOCKED
        if len(self.blockers) <= 1:
            self.flags |= CombatFlags.DAMAGE_ASSIGNED
        return True

    @staticmethod
    def _card_id(card):
        return getattr(card, "session_card_id", getattr(card, "uid", card))

    def assign_damage_order(self, damage_order: Iterable[object]) -> bool:
        """Match C# swap-in-place behavior and reject foreign/extra blockers."""
        for index, card_id in enumerate(damage_order):
            if index >= len(self.blockers):
                return False
            found = next((position for position, blocker in enumerate(self.blockers)
                          if self._card_id(blocker) == card_id), -1)
            if found < 0:
                return False
            if found != index:
                self.blockers[index], self.blockers[found] = (
                    self.blockers[found], self.blockers[index])
        self.flags |= CombatFlags.DAMAGE_ASSIGNED
        return True

    def eliminate_troop(self, troop) -> bool:
        if self.attacker is troop:
            self.attacker = None
            return True
        try:
            self.blockers.remove(troop)
        except ValueError:
            return False
        return True

    def mark_phase_resolved(self, phase: CombatPhase) -> None:
        self.resolved_phases |= phase


class CombatManager:
    """Port of C# combat identity and lookup ownership."""

    def __init__(self) -> None:
        self.combats: list[Combat] = []
        self._combat_map: dict[CombatId, Combat] = {}
        self._next_serial = 0

    def contains(self, combat_id: CombatId) -> bool:
        return combat_id in self._combat_map

    def get(self, combat_id: CombatId) -> Optional[Combat]:
        return self._combat_map.get(combat_id)

    def create_attack(self, combat_id: CombatId, instigator, defender) -> Combat:
        existing = self.get(combat_id)
        if existing is not None:
            return existing
        if not combat_id.is_valid:
            self._next_serial += 1
            combat_id = CombatId(getattr(instigator, "player_id", instigator),
                                 self._next_serial)
        combat = Combat(instigator, defender, combat_id)
        self.combats.append(combat)
        self._combat_map[combat_id] = combat
        return combat

    def remove_combat(self, combat_id: CombatId) -> None:
        combat = self._combat_map.pop(combat_id, None)
        if combat is not None:
            self.combats.remove(combat)

    def clear(self) -> None:
        self.combats.clear()
        self._combat_map.clear()

    def combats_with_attacker(self, card) -> list[Combat]:
        wanted = _card_key(card)
        return [combat for combat in self.combats
                if _card_key(combat.attacker) == wanted]

    def combat_for_attacker(self, card) -> Optional[Combat]:
        """Return the existing declaration for a card, if any."""
        wanted = _card_key(card)
        return next((combat for combat in self.combats
                     if _card_key(combat.attacker) == wanted), None)

    def combat_cares_about_phase(self, phase: CombatPhase) -> bool:
        """C# ``CombatManager.CombatCaresAboutPhase`` over live combatants."""
        for combat in self.combats:
            combatants = ((combat.attacker,) + tuple(combat.blockers))
            if any(card is not None and _in_warzone(card) and
                   _cares_about_phase(card, phase) for card in combatants):
                return True
        return False


def _in_warzone(card) -> bool:
    return bool(getattr(card, "in_warzone", True))


def _card_key(card):
    """Stable card identity across separately materialized runtime facts."""
    if card is None:
        return None
    value = getattr(card, "session_card_id", getattr(card, "uid", card))
    value = getattr(value, "uid", value)
    try:
        return int(getattr(value, "uid64", value))
    except (TypeError, ValueError):
        return id(card)


def _is_troop(card) -> bool:
    return bool(getattr(card, "is_troop", True))


def _cares_about_phase(card, phase: CombatPhase) -> bool:
    predicate = getattr(card, "cares_about_combat_phase", None)
    return bool(predicate(phase) if callable(predicate) else True)


def _combat_damage(card) -> int:
    value = getattr(card, "combat_damage", getattr(card, "attack", 0))
    return max(0, int(value))


def _has_juggernaut(card) -> bool:
    return bool(getattr(card, "juggernaut", getattr(card, "crush", False)))


class CombatResolver:
    """The source algorithm with session damage delegated through ``damage``.

    ``damage(source, target, amount, only_minimum_to_kill)`` must mutate the
    authoritative state and return actual damage dealt.  That makes simultaneous
    result accumulation and existing trigger/event order testable at the
    adapter boundary.
    """

    @staticmethod
    def accumulate_results(results: Iterable[CombatResult]) -> dict[object, int]:
        damage: dict[object, int] = {}
        for result in results:
            damage[result.target] = damage.get(result.target, 0) + result.damage
        return damage

    @staticmethod
    def resolve(combat: Combat, phase: CombatPhase,
                damage: Callable[[object, object, int, bool], int]) -> list[CombatResult]:
        results: list[CombatResult] = []
        attacker = combat.attacker
        if attacker is None or not _in_warzone(attacker):
            return results
        if _cares_about_phase(attacker, phase):
            remaining = _combat_damage(attacker)
            if combat.blockers:
                last = combat.blockers[-1]
                for blocker in combat.blockers:
                    if _in_warzone(blocker) and _is_troop(blocker):
                        minimum = blocker is not last or _has_juggernaut(attacker)
                        dealt = int(damage(attacker, blocker, remaining, minimum))
                        remaining -= dealt
                        results.append(CombatResult(attacker, blocker, dealt))
            if not combat.is_attack_blocked or _has_juggernaut(attacker):
                multiplier = int(getattr(attacker, "damage_champion_multiplier", 1))
                champion_damage = remaining * multiplier
                # The C# result records the requested champion damage, rather
                # than the out ``damageDealt`` value, so preserve that detail.
                damage(attacker, combat.defender, champion_damage, False)
                results.append(CombatResult(attacker, combat.defender,
                                            champion_damage))
        for blocker in combat.blockers:
            if _in_warzone(blocker) and _cares_about_phase(blocker, phase):
                dealt = int(damage(blocker, attacker, _combat_damage(blocker), False))
                # C# emits CurrentAttackValueClamped, not the out dealt value.
                results.append(CombatResult(blocker, attacker,
                                            _combat_damage(blocker)))
        combat.mark_phase_resolved(phase)
        return results


class CombatDamageBackend:
    """Explicit host projection for the native combat transaction boundary.

    RulesPort owns combat identity, phase, assignment order, damage, and
    death transitions. The mode callback only publishes the accepted result
    into its SQLite/wire representation; it must not resolve combat again.
    """

    def __init__(self, resolve: Callable) -> None:
        if not callable(resolve):
            raise TypeError("combat damage backend must be callable")
        self.resolve = resolve

    def __call__(self, session, transaction) -> bool:
        return bool(self.resolve(session, transaction))
