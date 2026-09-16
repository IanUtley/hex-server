"""One reversible view between the PvP checkpoint and RulesPort effects."""

from __future__ import annotations


def _thresholds(state, player_id):
    result = {}
    for key, value in (state.get(f"thresh_{player_id}") or {}).items():
        try:
            result[int(key)] = int(value or 0)
        except (TypeError, ValueError):
            continue
    return result


def to_effect_view(state, player_id, opponent_id):
    """Return a side-oriented view while aliasing mutable stack state."""
    if not isinstance(state, dict):
        raise TypeError("PvP state must be a dict")
    return {
        "pvp": True,
        "pids": list(state.get("pids") or []),
        "champ_map": state.get("champ_map") or {},
        "pvp_health_map": {player_id: "player_health",
                           opponent_id: "ai_health"},
        "player_health": int(state.get(f"hp_{player_id}", 20)),
        "ai_health": int(state.get(f"hp_{opponent_id}", 20)),
        "player_max_health": int(state.get(f"hp_{player_id}", 20)),
        "ai_max_health": int(state.get(f"hp_{opponent_id}", 20)),
        "turn_number": int(state.get("turn_number", 1)),
        "damaged_opponent_this_turn": list(
            state.get("damaged_opponent_this_turn") or []),
        "damaged_opponent_turn": int(
            state.get("damaged_opponent_turn", 0) or 0),
        "player_escalation_uses": int(state.get(f"esc_{player_id}", 0)),
        "ai_escalation_uses": int(state.get(f"esc_{opponent_id}", 0)),
        "player_resources": int(state.get(f"res_{player_id}", 0)),
        "ai_resources": int(state.get(f"res_{opponent_id}", 0)),
        "player_total_resources": int(
            state.get(f"res_total_{player_id}", 0)),
        "ai_total_resources": int(
            state.get(f"res_total_{opponent_id}", 0)),
        "player_threshold": _thresholds(state, player_id),
        "ai_threshold": _thresholds(state, opponent_id),
        "player_charges": int(state.get(f"chg_{player_id}", 0)),
        "ai_charges": int(state.get(f"chg_{opponent_id}", 0)),
        "player_spell_points": int(state.get(f"sp_{player_id}", 0)),
        "ai_spell_points": int(state.get(f"sp_{opponent_id}", 0)),
        "briar_legions_entered": int(
            state.get("briar_legions_entered", 0)),
        "champion_counters": state.setdefault("champion_counters", {}),
        "player_visibility": state.setdefault("player_visibility", {}),
        "stack": state.setdefault("stack", []),
        "stack_player_passed": state.get("stack_player_passed", False),
        "stack_ai_passed": state.get("stack_ai_passed", False),
        "_next_instance_id": state.get("_next_instance_id", 1),
    }


def apply_effect_view(state, view, player_id, opponent_id):
    """Copy mutable side-oriented values back into the PvP checkpoint."""
    if not isinstance(state, dict) or not isinstance(view, dict):
        raise TypeError("PvP state and effect view must be dicts")
    for key, view_key in (
            (f"esc_{player_id}", "player_escalation_uses"),
            (f"esc_{opponent_id}", "ai_escalation_uses"),
            (f"res_{player_id}", "player_resources"),
            (f"res_{opponent_id}", "ai_resources"),
            (f"res_total_{player_id}", "player_total_resources"),
            (f"res_total_{opponent_id}", "ai_total_resources"),
            (f"chg_{player_id}", "player_charges"),
            (f"chg_{opponent_id}", "ai_charges"),
            (f"sp_{player_id}", "player_spell_points"),
            (f"sp_{opponent_id}", "ai_spell_points")):
        if view_key in view:
            state[key] = int(view.get(view_key, state.get(key, 0)) or 0)
    if "player_threshold" in view:
        state[f"thresh_{player_id}"] = dict(
            view.get("player_threshold") or {})
    if "ai_threshold" in view:
        state[f"thresh_{opponent_id}"] = dict(
            view.get("ai_threshold") or {})
    for key in ("briar_legions_entered", "damaged_opponent_turn"):
        if key in view:
            state[key] = int(view.get(key, state.get(key, 0)) or 0)
    if "damaged_opponent_this_turn" in view:
        state["damaged_opponent_this_turn"] = list(
            view.get("damaged_opponent_this_turn") or [])
    if "bonus_turn_pid" in view:
        state["bonus_turn_pid"] = int(view.get("bonus_turn_pid") or 0)
    for key in ("champion_counters", "player_visibility"):
        if key in view:
            state[key] = dict(view.get(key) or {})
    if "stack" in view:
        state["stack"] = view["stack"]
    if "_next_instance_id" in view:
        state["_next_instance_id"] = int(view["_next_instance_id"] or 1)
    return state

