"""Client-style phase state objects over the direct phase-permit graph.

The current battle engine stores a phase cursor.  These objects port the C#
``TurnPhaseState`` lifecycle so a future mode can use state entry hooks and
priority windows without encoding each phase transition in service handlers.
"""

from __future__ import annotations

from dataclasses import dataclass

from .kernel import PriorityWindowAction, TurnPhasePlayers
from .phases import permitted_next_phases, phase_name


@dataclass(frozen=True)
class TurnPhaseTrigger:
    """Portable equivalent of C# ``TurnPhaseEvent`` at state entry."""

    phase: object
    source_player_id: object | None


class TurnPhaseState:
    """Port of the C# base state entry/next-phase contract."""

    def __init__(self, phase, *, priority_players=TurnPhasePlayers.ACTIVE,
                 chain_can_resolve=True) -> None:
        self.phase = phase
        self.priority_players = priority_players
        self._chain_can_resolve = bool(chain_can_resolve)

    def chain_can_resolve(self) -> bool:
        return self._chain_can_resolve

    def get_next_turn_phase(self, session=None):
        permitted = permitted_next_phases(self.phase)
        non_terminal = tuple(value for value in permitted if value != "EndGame")
        if len(non_terminal) != 1:
            raise ValueError(f"{phase_name(self.phase)} must select a next phase")
        return non_terminal[0]

    def on_entry(self, session) -> None:
        session.enqueue_trigger_event(TurnPhaseTrigger(
            self.phase, session.active_player_id))
        # Mode-specific card/resource/event projection is injected here.  The
        # phase graph and action ordering remain owned by RulesPort; adapters
        # must not advance the cursor or create their own priority action.
        session.resolve_turn_phase_entry(self.phase)
        action = PriorityWindowAction(self.priority_players, None)
        action._rules_port_phase = phase_name(self.phase)
        session.push_game_action_behind(action)
        # A mode may have a phase-specific first priority player (for
        # example, the defender at DeclareDefense).  The action is now
        # materialized, so let the mode reorder its native queue.  This is a
        # scheduler hook, not a second phase implementation.
        configure = getattr(session, "configure_phase_priority", None)
        if configure is not None:
            configure(action)

    def on_exit(self, session) -> None:
        pass


class FirstMainPhaseState(TurnPhaseState):
    """C# ``FirstMainPhaseState.DetermineNextTurnPhase`` override."""

    def __init__(self, phase) -> None:
        super().__init__(phase)

    def get_next_turn_phase(self, session=None):
        return ("SecondMainPhase" if getattr(session, "active_player_skips_attack", False)
                else "DeclareCombatPriorityWindow")


class StartTurnState(TurnPhaseState):
    """StartTurn's C# special case: chain resolution is not permitted."""

    def __init__(self, phase) -> None:
        super().__init__(phase, chain_can_resolve=False)

    def on_entry(self, session) -> None:
        super().on_entry(session)
        session.total_turns_taken += 1
        # Card/state mutations belong to the authoritative rules lifecycle,
        # not to a transport handler.  The runtime adapter supplies this
        # callback for the live SQLite-backed game; the kernel remains usable
        # with no callback in focused scheduler tests.
        session.resolve_turn_start()


class PriorityPhaseState(TurnPhaseState):
    """C# ``PriorityWindowState``: every player gets priority in APNAP order."""

    def __init__(self, phase, *, chain_can_resolve=True) -> None:
        super().__init__(phase, priority_players=TurnPhasePlayers.ALL,
                         chain_can_resolve=chain_can_resolve)


class ConditionalPhaseState(TurnPhaseState):
    """C# state-specific next-phase branch over an injected session fact.

    Card legality remains in the runtime card adapter. This state object owns
    the C# control-flow choice and has a deterministic fallback for headless
    or partially migrated sessions.
    """

    def __init__(self, phase, selector, *, priority_players=TurnPhasePlayers.ACTIVE,
                 chain_can_resolve=True) -> None:
        super().__init__(phase, priority_players=priority_players,
                         chain_can_resolve=chain_can_resolve)
        self._selector = selector

    def get_next_turn_phase(self, session=None):
        return self._selector(session)


def default_phase_states(enum) -> dict[str, TurnPhaseState]:
    """Build state objects keyed by C# phase name from ``game_engine.ETurnPhases``."""
    states = {}
    # These are subclasses of the C# PriorityWindowState rather than merely
    # phases whose names happen to mention priority. Their queue includes all
    # players; the other states below intentionally do not.
    for name in ("Ready", "Prep", "Draw", "DeclareCombatPriorityWindow",
                 "DeclareAttackPriorityWindow", "DeclareDefensePriorityWindow",
                 "FirstStrikePriorityWindow", "SecondMainPhase", "EndPhase"):
        states[name] = PriorityPhaseState(
            getattr(enum, name), chain_can_resolve=name != "Draw")
    # The client never offers an input window in setup/terminal/checksum
    # states. Preserve its no-priority action rather than granting the active
    # player a spurious GreenLight.
    for name in ("StartGame", "Checksum", "EndGame"):
        states[name] = TurnPhaseState(
            getattr(enum, name), priority_players=TurnPhasePlayers.NONE)
    for name in ("PickGoesFirst", "Mulligan", "AssignFirstStrikeDamage",
                 "AssignDamage", "EndTurn"):
        states[name] = TurnPhaseState(getattr(enum, name))
    states["DeclareAttack"] = TurnPhaseState(
        enum.DeclareAttack, chain_can_resolve=False)
    states["DeclareDefense"] = TurnPhaseState(
        enum.DeclareDefense, priority_players=TurnPhasePlayers.DEFENDING,
        chain_can_resolve=False)
    states["Discard"] = TurnPhaseState(enum.Discard, chain_can_resolve=False)
    states["PreGame"] = ConditionalPhaseState(
        enum.PreGame,
        lambda session: ("StartGame" if getattr(session, "skip_setup", False)
                         else "PickGoesFirst"),
        priority_players=TurnPhasePlayers.NONE)
    states["Mulligan"] = ConditionalPhaseState(
        enum.Mulligan,
        lambda session: ("StartGame" if getattr(session, "skip_mulligan", False)
                         or getattr(session, "all_players_ready_to_start", False)
                         else "Mulligan"))
    states["Prep"] = ConditionalPhaseState(
        enum.Prep,
        lambda session: ("FirstMainPhase" if getattr(
            session, "active_player_skips_draw", False) else "Draw"),
        priority_players=TurnPhasePlayers.ALL)
    states["DeclareCombatPriorityWindow"] = ConditionalPhaseState(
        enum.DeclareCombatPriorityWindow,
        lambda session: ("DeclareAttack" if getattr(
            session, "has_legal_attackers", False) else "AssignDamage"),
        priority_players=TurnPhasePlayers.ALL)
    states["DeclareAttack"] = ConditionalPhaseState(
        enum.DeclareAttack,
        lambda session: ("DeclareAttackPriorityWindow" if getattr(
            session, "has_combats", False) or getattr(
            session, "has_forced_attackers", False) else "AssignDamage"),
        chain_can_resolve=False)
    states["DeclareAttackPriorityWindow"] = ConditionalPhaseState(
        enum.DeclareAttackPriorityWindow,
        lambda session: ("DeclareDefense" if getattr(session, "has_legal_blockers", False)
                         else ("DeclareDefensePriorityWindow" if getattr(
                             session, "has_combats", False) else "SecondMainPhase")),
        priority_players=TurnPhasePlayers.ALL)
    states["DeclareDefensePriorityWindow"] = ConditionalPhaseState(
        enum.DeclareDefensePriorityWindow,
        lambda session: ("AssignFirstStrikeDamage" if getattr(
            session, "combat_has_first_strike", False) else
            ("AssignDamage" if getattr(session, "combat_has_standard_damage", False)
             else "SecondMainPhase")),
        priority_players=TurnPhasePlayers.ALL)
    states["FirstStrikePriorityWindow"] = ConditionalPhaseState(
        enum.FirstStrikePriorityWindow,
        lambda session: ("AssignDamage" if getattr(
            session, "combat_has_standard_damage", False) else "SecondMainPhase"),
        priority_players=TurnPhasePlayers.ALL)
    states["SecondMainPhase"] = ConditionalPhaseState(
        enum.SecondMainPhase,
        lambda session: ("DeclareCombatPriorityWindow" if getattr(
            session, "has_extra_combats", False) else "EndPhase"),
        # An empty main phase still follows the normal two-pass rule: the
        # active player passes, then the opponent passes, then the phase ends.
        # The active-only window is used by the explicit reconnect fallback in
        # session.py, not by the ordinary SecondMain phase state.
        priority_players=TurnPhasePlayers.ALL)
    states["EndTurn"] = ConditionalPhaseState(
        enum.EndTurn,
        lambda session: ("StartTurn" if len(getattr(session, "player_ids", ()))
                         else "EndGame"))
    states["StartTurn"] = StartTurnState(enum.StartTurn)
    states["FirstMainPhase"] = FirstMainPhaseState(enum.FirstMainPhase)
    return states
