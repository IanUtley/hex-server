"""DB-backed battle turn/priority engine.

Tracks whose turn it is, the current phase, and the priority state for a
single 1v1 battle (player vs AI).  All state is stored in the
``game_sessions.turn_order_json`` column as a JSON dict so concurrent players
never interfere and a reconnect can resume.

Turn model:

    The per-turn phase cycle is DYNAMIC.  It is built when a turn starts and
    stored in ``state['turn_phases']`` (a list of ETurnPhases values):

        BASE_TURN_PHASES (no combat):
            StartTurn -> Ready -> Prep -> Draw -> FirstMainPhase ->
            SecondMainPhase -> EndPhase -> Discard -> EndTurn

        COMBAT_TURN_PHASES (player has a ready troop in the warzone):
            StartTurn -> Ready -> Prep -> Draw -> FirstMainPhase ->
            DeclareCombatPriorityWindow -> DeclareAttack ->
            DeclareAttackPriorityWindow -> DeclareDefense ->
            DeclareDefensePriorityWindow -> AssignFirstStrikeDamage ->
            FirstStrikePriorityWindow -> AssignDamage ->
            SecondMainPhase -> EndPhase -> Discard -> EndTurn

    The AI never attacks yet, so its turn always uses BASE_TURN_PHASES.
    ``phase_idx`` indexes into the stored list; ``advance_phase`` wraps
    (EndTurn -> StartTurn) and switches the turn player, rebuilding the list
    for the new turn.

Priority model:
    the active (turn) player holds priority at the start of every phase.
    When a player passes, priority goes to the opponent.  When both players
    have passed in a phase, the phase advances.  On EndTurn the turn player
    switches and the new turn starts at StartTurn.

The AI is driven by the server: it draws a card each Draw phase and, during
its own FirstMainPhase, plays a resource card if it can (has one in hand and
hasn't played one this turn).  Every other priority window it passes.
"""

import json

import game_engine


# No-combat per-turn phase cycle (used by the AI, and the player when they
# control no ready troop).
BASE_TURN_PHASES = [
    game_engine.ETurnPhases.StartTurn,
    game_engine.ETurnPhases.Ready,
    game_engine.ETurnPhases.Prep,
    game_engine.ETurnPhases.Draw,
    game_engine.ETurnPhases.FirstMainPhase,
    game_engine.ETurnPhases.SecondMainPhase,
    game_engine.ETurnPhases.EndPhase,
    game_engine.ETurnPhases.Discard,
    game_engine.ETurnPhases.EndTurn,
]

# Combat steps inserted between FirstMainPhase and SecondMainPhase (the order
# mirrors the client's permitted state transitions; MTG-combat renamed).
COMBAT_STEPS = [
    game_engine.ETurnPhases.DeclareCombatPriorityWindow,
    game_engine.ETurnPhases.DeclareAttack,
    game_engine.ETurnPhases.DeclareAttackPriorityWindow,
    game_engine.ETurnPhases.DeclareDefense,
    game_engine.ETurnPhases.DeclareDefensePriorityWindow,
    game_engine.ETurnPhases.AssignFirstStrikeDamage,
    game_engine.ETurnPhases.FirstStrikePriorityWindow,
    game_engine.ETurnPhases.AssignDamage,
]

# Full combat per-turn cycle (player controls a ready troop).
COMBAT_TURN_PHASES = [
    game_engine.ETurnPhases.StartTurn,
    game_engine.ETurnPhases.Ready,
    game_engine.ETurnPhases.Prep,
    game_engine.ETurnPhases.Draw,
    game_engine.ETurnPhases.FirstMainPhase,
] + COMBAT_STEPS + [
    game_engine.ETurnPhases.SecondMainPhase,
    game_engine.ETurnPhases.EndPhase,
    game_engine.ETurnPhases.Discard,
    game_engine.ETurnPhases.EndTurn,
]

# Backwards-compatible alias for callers that index the phase list directly.
TURN_PHASES = BASE_TURN_PHASES

PLAYER = "player"
AI = "ai"

# Phases the client ALWAYS pauses on for a player's own turn / the opponent's
# turn (SetTurnPhases adds these regardless of user selection).
SELF_ALWAYS_STOPS = {
    game_engine.ETurnPhases.PickGoesFirst,
    game_engine.ETurnPhases.Mulligan,
    game_engine.ETurnPhases.StartGame,
    game_engine.ETurnPhases.DeclareAttack,
    game_engine.ETurnPhases.AssignDamage,
    game_engine.ETurnPhases.AssignFirstStrikeDamage,
}
OPP_ALWAYS_STOPS = {
    game_engine.ETurnPhases.DeclareDefense,
}

# Client's SetDefaultTurnPhases (used until the player customises via the
# Phase Stops dialog, which sends a SetTurnPhasesTransaction).
SELF_DEFAULT_STOPS = {
    game_engine.ETurnPhases.FirstMainPhase,
    game_engine.ETurnPhases.SecondMainPhase,
    game_engine.ETurnPhases.DeclareCombatPriorityWindow,
    game_engine.ETurnPhases.DeclareAttackPriorityWindow,
    game_engine.ETurnPhases.DeclareDefensePriorityWindow,
}
OPP_DEFAULT_STOPS = {
    game_engine.ETurnPhases.SecondMainPhase,
    game_engine.ETurnPhases.DeclareAttackPriorityWindow,
    game_engine.ETurnPhases.DeclareDefensePriorityWindow,
}


def build_turn_phases(state):
    """The per-turn phase list: combat phases only when the CURRENT turn player
    controls a ready troop (player_has_ready_troop set at the turn's FirstMain
    pass — for the player in _advance_to_priority, for the AI in ai.run_ai_turn).
    The AI gets combat phases too, so it can attack (aggressive personality).
    """
    if state.get("player_has_ready_troop"):
        return COMBAT_TURN_PHASES
    return BASE_TURN_PHASES


def is_self_stop(state, phase):
    """True if `phase` is a stop (pause) for the human on their own turn."""
    stops = set(SELF_ALWAYS_STOPS)
    stops.update(state.get("player_self_stops") or SELF_DEFAULT_STOPS)
    return phase in stops


def is_opp_stop(state, phase):
    """True if `phase` is a stop (pause) for the human on the AI's turn."""
    stops = set(OPP_ALWAYS_STOPS)
    stops.update(state.get("player_opp_stops") or OPP_DEFAULT_STOPS)
    return phase in stops


def ai_held_phase_context(state):
    """Return ``(phase, index, phases)`` for a paused AI opponent stop.

    ``ai_turn_phase_idx`` is the resume cursor and is written immediately
    before the AI hands priority to the human.  A late session write can leave
    ``phase_idx`` behind that cursor, even though the cursor itself is still
    authoritative.  Prefer the persisted phase list, but recover the combat
    list when an old/base list cannot contain the cursor.
    """
    if not isinstance(state, dict) or state.get("ai_turn_phase_idx") is None:
        return None
    try:
        resume_idx = int(state["ai_turn_phase_idx"])
    except (TypeError, ValueError):
        return None
    candidates = []
    stored = state.get("turn_phases")
    if isinstance(stored, list):
        candidates.append(stored)
    candidates.extend((COMBAT_TURN_PHASES, BASE_TURN_PHASES))
    for phases in candidates:
        if 0 < resume_idx <= len(phases):
            return phases[resume_idx - 1], resume_idx - 1, phases
    return None


def load_state(session):
    """Load the battle state dict for a session (defaults if absent)."""
    try:
        data = session.turn_order
        if isinstance(data, dict) and "turn_player" in data:
            # JSON serializes int keys as strings; convert threshold dicts back.
            for key in ("player_threshold", "ai_threshold"):
                if key in data and isinstance(data[key], dict):
                    data[key] = {int(k): v for k, v in data[key].items()}
            return data
    except (ValueError, TypeError):
        pass
    return default_state()


def save_state(session, state):
    """Persist battle state into the session's turn_order_json column."""
    session.turn_order = state
    session._persist()


# --- Chain / stack ----------------------------------------------------------
# The chain holds pending resolutions for the current phase (troops, spells,
# triggers). It is cleared at the start of each phase; when both players pass
# priority, the top resolves and executes, then priority is re-granted until the
# chain empties (then the phase advances). Each item:
#   {"kind": "troop"|"trigger"|"spell", "source_uid": int, "instance_id": int,
#    "ability_guid": str, "targets": [int]}

def stack_push(state, item):
    state.setdefault("stack", []).append(item)


def stack_pop(state):
    stack = state.get("stack") or []
    return stack.pop() if stack else None


def stack_top(state):
    stack = state.get("stack") or []
    return stack[-1] if stack else None


def stack_empty(state):
    return not (state.get("stack") or [])


def stack_clear(state):
    state["stack"] = []


def stack_set_pass(state, player, passed):
    """Mark whether the player (PLAYER/AI) has passed priority for the current
    chain. When both have passed, the top resolves."""
    key = "stack_player_passed" if player == PLAYER else "stack_ai_passed"
    state[key] = bool(passed)


def stack_both_passed(state):
    return bool(state.get("stack_player_passed")) and bool(state.get("stack_ai_passed"))


def stack_reset_passes(state):
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False


def default_state(turn_player=PLAYER):
    """Fresh battle state for a battle about to begin."""
    state = {
        "turn_player": turn_player,
        "turn_number": 1,
        "phase_idx": 0,
        "player_passed": False,
        "ai_passed": False,
        "player_resources": 0,
        "player_total_resources": 0,
        "player_threshold": {},
        "ai_resources": 0,
        "ai_total_resources": 0,
        "ai_threshold": {},
        "player_resource_played_this_turn": False,
        "ai_resource_played_this_turn": False,
        "player_charges": 0,
        "ai_charges": 0,
        "player_spell_points": 0,
        "ai_spell_points": 0,
        # Spell-power escalation: {ability_guid: times_used}. Each use of a spell
        # power permanently adds +1 to its SP cost (mirrors the client's
        # IncrementSpellPointCostModifier). Player-only for now (AI trainers use
        # charge powers, not spell powers).
        "player_sp_uses": {},
        # Phase stops the human has configured (via SetTurnPhasesTransaction).
        # None/absent means "use client defaults".
        "player_self_stops": None,
        "player_opp_stops": None,
        # Champion health (persisted so a reconnect can resume combat damage).
        "player_health": 20,
        "ai_health": 20,
        # Combat state (persisted for reconnect): which of the player's troops
        # are attacking this turn, and the declared attackers {card_uid: (defender_champ_uid)}.
        "player_attackers": {},
        # Per-turn phase list (built at turn start). Player gets combat phases
        # only when they control a ready troop; the AI always skips combat.
        "player_has_ready_troop": False,
        "turn_phases": BASE_TURN_PHASES,
        # Monotonically increasing counter for warzone arrival ordering.
        # Each card that resolves to the warzone gets current value, then it increments.
        "resolve_counter": 0,
    }
    return state


def turn_phases(state):
    """The phase list for the current turn (builds + caches if missing)."""
    phases = state.get("turn_phases")
    if not phases:
        phases = build_turn_phases(state)
        state["turn_phases"] = phases
    return phases


def current_phase(state):
    phases = turn_phases(state)
    idx = state.get("phase_idx", 0) % len(phases)
    return phases[idx]


def next_turn_player(state):
    """Return the player who owns the next turn and consume a bonus turn.

    The live PvE driver performs some EndTurn handoffs itself instead of
    calling :func:`advance_phase`, so both paths must use the same bonus-turn
    rule.  A GiveBonusTurn effect stores the current side in ``bonus_turn``;
    that side keeps the next turn and the marker is then consumed.
    """
    current = state.get("turn_player")
    if state.get("bonus_turn") == current:
        state.pop("bonus_turn", None)
        return current
    return AI if current == PLAYER else PLAYER


def advance_phase(state):
    """Advance to the next phase; switch turn player when EndTurn passes.

    Wraps within the stored per-turn phase list; when the turn ends it flips
    turn_player, increments the turn number and rebuilds the phase list for
    the new turn (the caller must set player_has_ready_troop for a player
    turn before advancing into it).
    """
    state["player_passed"] = False
    state["ai_passed"] = False
    phases = turn_phases(state)
    idx = state.get("phase_idx", 0) + 1
    if idx >= len(phases):
        # Wrapped past EndTurn.  An extra-turn effect (GiveBonusTurn) keeps the
        # same player going; otherwise switch to the opponent.
        state["turn_player"] = next_turn_player(state)
        state["turn_number"] = state.get("turn_number", 1) + 1
        state["phase_idx"] = 0
        state["turn_phases"] = build_turn_phases(state)
        return current_phase(state)
    state["phase_idx"] = idx
    return current_phase(state)


def skip_to_phase(state, phase):
    """Jump ``phase_idx`` forward to ``phase`` in the current turn's phase
    list (no-op when the phase is not ahead).  Used to skip the remaining
    combat steps when no attackers were declared."""
    phases = turn_phases(state)
    idx = state.get("phase_idx", 0)
    try:
        target = phases.index(phase, idx)
    except ValueError:
        return False
    state["phase_idx"] = target
    return True


def turn_player_uid(state, pl_uid, ai_uid):
    return pl_uid if state.get("turn_player") == PLAYER else ai_uid


def other_player_uid(state, pl_uid, ai_uid):
    return ai_uid if state.get("turn_player") == PLAYER else pl_uid
