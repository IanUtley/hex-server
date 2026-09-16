"""RulesPort lifecycle rules for the two-human PvP checkpoint.

Tournament PvP retains a wire/database-compatible checkpoint with raw player
IDs (``turn_pid``/``phase``) rather than the ordinary Practice/PvE
``turn_player``/``phase_idx`` shape. These lifecycle decisions belong to the
RulesPort; packet construction and persistence remain in the service adapter.
"""

from __future__ import annotations

import game_engine

from . import lifecycle


def default_pvp_state(turn_pid, goes_first_pid) -> dict:
    """Create the wire-compatible two-human PvP checkpoint."""
    return {
        "pvp": True,
        "pids": [turn_pid, goes_first_pid]
        if turn_pid != goes_first_pid else [turn_pid],
        "turn_pid": turn_pid,
        "goes_first_pid": goes_first_pid,
        "turn_number": 1,
        "phase": 3,
        "passes": [],
        "kept": [],
        "draws_first_pid": 0,
        "priority_elapsed_ticks": {},
        "_priority_clock_pid": 0,
        "_priority_clock_started_ns": 0,
        "_priority_window_pid": 0,
        "_priority_window_started_ns": 0,
    }


def player_auto_passes(state, player_id) -> bool:
    try:
        return int(state.get("autopass_pid", 0)) == int(player_id)
    except (TypeError, ValueError):
        return False


def phase_is_stop(state, phase, turn_pid, opponent_pid) -> bool:
    self_stops = set(lifecycle.SELF_ALWAYS_STOPS)
    self_stops.update(
        state.get(f"stops_self_{turn_pid}") or lifecycle.SELF_DEFAULT_STOPS)
    opponent_stops = set(lifecycle.OPP_ALWAYS_STOPS)
    opponent_stops.update(
        state.get(f"stops_opp_{opponent_pid}") or lifecycle.OPP_DEFAULT_STOPS)
    if player_auto_passes(state, turn_pid):
        self_stops = set(lifecycle.SELF_ALWAYS_STOPS)
    if player_auto_passes(state, opponent_pid):
        opponent_stops = set(lifecycle.OPP_ALWAYS_STOPS)
    return phase in self_stops or phase in opponent_stops


def waiting_player_requires_priority(state, phase, waiting_pid,
                                     has_quick_action=False) -> bool:
    """Whether a passed phase must be handed to the other player."""
    if player_auto_passes(state, waiting_pid):
        return False
    opponent_stops = set(lifecycle.OPP_ALWAYS_STOPS)
    opponent_stops.update(state.get(f"stops_opp_{waiting_pid}") or ())
    return phase in opponent_stops or bool(has_quick_action)


def stack_pass_transition(passed_players, player_id, player_ids) -> dict:
    """Classify one pass on a two-player stack without mutating state."""
    passed = {int(value) for value in (passed_players or ())}
    player_id = int(player_id)
    if player_id in passed:
        return {"action": "duplicate", "passed": sorted(passed),
                "other_player": None}
    passed.add(player_id)
    players = [int(value) for value in player_ids]
    other = next((value for value in players if value != player_id), None)
    action = "resolve" if len(passed) >= len(players) else "handoff"
    return {"action": action, "passed": sorted(passed),
            "other_player": other}


def mulligan_transition(state, player_ids, just_acted_pid) -> dict:
    """Choose the next mulligan player or the post-mulligan transition."""
    players = [int(value) for value in player_ids]
    kept = {int(value) for value in (state.get("kept") or ())}
    if len(kept) >= len(players):
        return {"action": "start_turn", "next_player": None}
    acted = int(just_acted_pid)
    other = next((value for value in players if value != acted), None)
    next_player = other if other not in kept else acted
    return {"action": "prompt", "next_player": next_player}


def advance_turn_state(state, player_ids, *, incoming_player_id=None) -> dict:
    """Apply the mode-neutral two-player turn-boundary state transition.

    ``incoming_player_id`` is used when the native RulesPort has already
    selected the typed next active player.  It keeps the durable PvP cleanup
    (resource-play flags, combat facts, autopass, and temporary turn facts)
    without selecting a second, potentially different cursor.
    """
    players = [int(value) for value in player_ids]
    outgoing = int(state.get("turn_pid") or 0)
    bonus = int(state.pop("bonus_turn_pid", 0) or 0)
    if incoming_player_id is not None:
        incoming = int(incoming_player_id)
        bonus_used = bonus in players and incoming == bonus
    elif bonus in players:
        incoming = bonus
        bonus_used = True
    else:
        incoming = next((value for value in players if value != outgoing),
                        players[0] if players else 0)
        bonus_used = False
    state.pop("autopass_pid", None)
    state.pop("autopass_state", None)
    state.pop("turn_end_trigger_fired", None)
    state["turn_pid"] = incoming
    state["turn_number"] = int(state.get("turn_number", 1)) + 1
    state.pop("damaged_opponent_this_turn", None)
    state.pop("damaged_opponent_turn", None)
    state.pop("attackers", None)
    state.pop("blockers", None)
    state.pop("extra_combats_this_turn", None)
    for player_id in players:
        state[f"res_played_{player_id}"] = 0
    return {"turn_pid": incoming, "bonus_used": bonus_used}


def queue_stack_item(state, item) -> int:
    """Allocate a stable chain instance ID and queue one stack item."""
    if not isinstance(item, dict):
        raise TypeError("stack item must be a dict")
    instance_id = int(item.get("instance_id") or
                      state.get("_next_instance_id", 1) or 1)
    item = dict(item)
    item["instance_id"] = instance_id
    state["_next_instance_id"] = max(
        int(state.get("_next_instance_id", 1) or 1), instance_id + 1)
    lifecycle.stack_push(state, item)
    return instance_id


def turn_phase_list(state, turn_pid, has_ready) -> list:
    phases = list(lifecycle.COMBAT_TURN_PHASES if has_ready
                  else lifecycle.BASE_TURN_PHASES)
    if state.get("skip_draw_phase") or state.get("corinth_mode"):
        phases = [phase for phase in phases
                  if phase != game_engine.ETurnPhases.Draw]
    entries = (state.get("extra_combats_this_turn") or {}).get(
        str(turn_pid), [])
    if not entries:
        return phases
    try:
        second_main = phases.index(game_engine.ETurnPhases.SecondMainPhase)
    except ValueError:
        return phases
    return (phases[:second_main + 1] +
            sum((lifecycle.COMBAT_STEPS +
                 [game_engine.ETurnPhases.SecondMainPhase]
                 for _entry in entries), []) +
            phases[second_main + 1:])


def phase_transition(phase_list, current_phase, after_blockers=None) -> dict:
    """Calculate the next PvP phase without touching session state.

    ``after_blockers`` is supplied only after the final defense-priority
    window, allowing the caller to account for keywords granted during that
    response window.  A wrapped transition is reported to the service so it
    can run end-of-turn processing before committing the next phase.
    """
    phases = list(phase_list or [])
    if not phases:
        raise ValueError("phase_list must not be empty")
    try:
        current_index = phases.index(current_phase)
    except ValueError:
        current_index = 0
    if after_blockers is not None:
        next_index = phases.index(after_blockers)
        return {"current_index": current_index, "next_index": next_index,
                "new_phase": after_blockers, "wrapped": False}
    next_index = current_index + 1
    wrapped = next_index >= len(phases)
    return {"current_index": current_index, "next_index": next_index,
            "new_phase": None if wrapped else phases[next_index],
            "wrapped": wrapped}


def enter_phase(state, phase) -> int:
    """Enter a PvP phase and clear the completed phase's pass interval."""
    if not isinstance(state, dict):
        raise TypeError("PvP phase state must be a dict")
    state["phase"] = int(phase)
    state["passes"] = []
    return int(phase)


def record_phase_pass(state, player_id) -> list:
    """Record one player's pass for the current PvP phase."""
    if not isinstance(state, dict):
        raise TypeError("PvP phase state must be a dict")
    pid = int(player_id)
    passes = list(state.get("passes") or [])
    if pid not in passes:
        passes.append(pid)
    state["passes"] = passes
    return passes


def set_priority(state, player_id) -> int:
    """Assign PvP priority to one player in the current interval."""
    if not isinstance(state, dict):
        raise TypeError("PvP phase state must be a dict")
    state["priority_pid"] = int(player_id)
    return int(player_id)


def reset_priority_interval(state, player_id=None) -> int | None:
    """Clear phase/stack passes and optionally assign the next priority."""
    if not isinstance(state, dict):
        raise TypeError("PvP phase state must be a dict")
    state["passes"] = []
    state["stack_passed"] = []
    if player_id is None:
        state.pop("priority_pid", None)
        return None
    return set_priority(state, player_id)


def phase_after_blockers(has_attackers, has_swiftstrike):
    """Select the combat phase following the blocker response window."""
    if not has_attackers:
        return game_engine.ETurnPhases.SecondMainPhase
    if not has_swiftstrike:
        return game_engine.ETurnPhases.AssignDamage
    return game_engine.ETurnPhases.AssignFirstStrikeDamage


__all__ = ("player_auto_passes", "phase_is_stop", "turn_phase_list",
           "phase_transition", "phase_after_blockers",
           "waiting_player_requires_priority", "stack_pass_transition",
           "advance_turn_state", "queue_stack_item", "default_pvp_state",
           "mulligan_transition")
