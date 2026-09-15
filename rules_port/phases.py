"""Client-derived legal turn-phase graph.

This is a direct transcription of the ``.Permit`` calls in
``HexClient/.../Game/Shared/Mechanics/*State.cs``.  It validates transitions;
phase-entry effects remain the responsibility of a session adapter.
"""

from __future__ import annotations

from typing import Dict, FrozenSet


# State name -> permitted next states.  EndGame is included only where the C#
# state registered it; NotPlaying is the special all-entry reset state.
_PERMITS: Dict[str, FrozenSet[str]] = {
    "NotPlaying": frozenset({"AssignDamage", "AssignFirstStrikeDamage",
        "DeclareAttack", "DeclareAttackPriorityWindow", "DeclareDefense",
        "DeclareDefensePriorityWindow", "DeclareCombatPriorityWindow", "Discard",
        "Draw", "EndGame", "EndPhase", "EndTurn", "FirstMainPhase",
        "FirstStrikePriorityWindow", "NotPlaying", "PickGoesFirst", "PreGame",
        "Prep", "Ready", "SecondMainPhase", "StartGame", "StartTurn"}),
    "PreGame": frozenset({"PickGoesFirst", "StartGame", "EndGame"}),
    "PickGoesFirst": frozenset({"PreGame", "Mulligan", "EndGame"}),
    "Mulligan": frozenset({"StartGame", "Mulligan", "EndGame"}),
    "StartGame": frozenset({"StartTurn", "EndGame"}),
    "StartTurn": frozenset({"Ready", "EndGame"}),
    "Ready": frozenset({"Prep", "EndGame"}),
    "Prep": frozenset({"Draw", "FirstMainPhase", "EndGame"}),
    "Draw": frozenset({"FirstMainPhase", "EndGame"}),
    "FirstMainPhase": frozenset({"DeclareCombatPriorityWindow",
                                   "SecondMainPhase", "EndGame"}),
    "DeclareCombatPriorityWindow": frozenset({"DeclareAttack", "AssignDamage",
                                                 "EndGame"}),
    "DeclareAttack": frozenset({"AssignDamage", "DeclareAttackPriorityWindow",
                                  "EndGame"}),
    "DeclareAttackPriorityWindow": frozenset({"DeclareDefense", "SecondMainPhase",
        "DeclareDefensePriorityWindow", "EndGame"}),
    "DeclareDefense": frozenset({"DeclareDefensePriorityWindow", "EndGame"}),
    "DeclareDefensePriorityWindow": frozenset({"AssignFirstStrikeDamage",
        "AssignDamage", "SecondMainPhase", "EndGame"}),
    "AssignFirstStrikeDamage": frozenset({"FirstStrikePriorityWindow", "EndGame"}),
    "FirstStrikePriorityWindow": frozenset({"AssignDamage", "SecondMainPhase",
                                              "EndGame"}),
    "AssignDamage": frozenset({"SecondMainPhase", "EndGame"}),
    "SecondMainPhase": frozenset({"DeclareAttack", "DeclareCombatPriorityWindow",
        "SecondMainPhase", "EndPhase", "EndGame"}),
    "EndPhase": frozenset({"Discard", "EndGame"}),
    "Discard": frozenset({"EndTurn", "EndGame"}),
    "EndTurn": frozenset({"StartTurn", "Mulligan", "EndGame"}),
    "Checksum": frozenset({"StartTurn", "EndGame"}),
    "EndGame": frozenset({"EndGame", "NotPlaying"}),
}

_PHASE_BY_VALUE = {
    index: name for index, name in enumerate((
        "Unknown", "NotPlaying", "PreGame", "PickGoesFirst", "Mulligan",
        "StartGame", "StartTurn", "Ready", "Prep", "Draw", "FirstMainPhase",
        "DeclareCombatPriorityWindow", "DeclareAttack",
        "DeclareAttackPriorityWindow", "DeclareDefense",
        "DeclareDefensePriorityWindow", "AssignFirstStrikeDamage",
        "FirstStrikePriorityWindow", "AssignDamage", "SecondMainPhase",
        "EndPhase", "Discard", "EndTurn", "Checksum", "EndGame",
    ))
}


def phase_name(phase) -> str:
    """Accept Python enums, C#-style names, or persisted phase values."""
    name = getattr(phase, "name", None)
    if name:
        return name
    try:
        return _PHASE_BY_VALUE[int(phase)]
    except (KeyError, TypeError, ValueError):
        return str(phase)


def permitted_next_phases(phase) -> FrozenSet[str]:
    """Return the exact C# state-machine permit set for ``phase``."""
    try:
        return _PERMITS[phase_name(phase)]
    except KeyError as exc:
        raise ValueError(f"unknown Hex turn phase: {phase!r}") from exc


def transition_is_legal(current, next_phase) -> bool:
    return phase_name(next_phase) in permitted_next_phases(current)


def require_legal_transition(current, next_phase) -> None:
    if not transition_is_legal(current, next_phase):
        raise ValueError(f"illegal Hex phase transition: {phase_name(current)} "
                         f"-> {phase_name(next_phase)}")
