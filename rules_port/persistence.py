"""RulesPort-owned persistence for the shared battle checkpoint.

The checkpoint is still stored by the existing session/database adapter, but
native rules code must not call the compatibility battle engine to load or
save it.  This module is the narrow runtime boundary for that storage detail.
"""

from __future__ import annotations


_RUNTIME_STATE_KEYS = frozenset({"_ability_builder"})


def persistence_state(state):
    """Return the JSON-safe checkpoint view used by the session adapter."""
    if not isinstance(state, dict):
        return state
    return {key: value for key, value in state.items()
            if key not in _RUNTIME_STATE_KEYS}


def load_state(session, *, default=None):
    """Load the already-authoritative checkpoint without legacy imports."""
    shared = getattr(session, "_rules_port_battle_state", None)
    if isinstance(shared, dict) and (
            "turn_player" in shared or shared.get("pvp")):
        _restore_threshold_keys(shared)
        return shared
    data = getattr(session, "turn_order", None)
    if isinstance(data, dict) and (
            "turn_player" in data or data.get("pvp")):
        _restore_threshold_keys(data)
        return data
    return {} if default is None else default()


def load_pvp_state(session):
    """Load the tournament-compatible two-human checkpoint."""
    try:
        data = session.turn_order
        if isinstance(data, dict) and data.get("pvp"):
            return data
    except (ValueError, TypeError):
        pass
    return None


def save_pvp_state(session, state, *, flush_clock=None) -> None:
    """Persist the PvP checkpoint through the shared session adapter."""
    if callable(flush_clock):
        flush_clock(state)
    session.turn_order = persistence_state(state)
    try:
        session._persist()
    finally:
        session.turn_order = state


def save_state(session, state) -> None:
    """Persist a native checkpoint through the session's DB adapter."""
    if isinstance(getattr(session, "_rules_port_battle_state", None), dict):
        session._rules_port_battle_state = state
    session.turn_order = persistence_state(state)
    try:
        import db as db_layer
        try:
            session._persist(conn=db_layer._db)
        except TypeError:
            session._persist()
        db_layer._db.commit()
    finally:
        # The active native resolver continues to use its mutable dictionary.
        session.turn_order = state


def current_phase(state):
    """Read the phase at the persisted cursor from the native checkpoint."""
    phases = state.get("turn_phases") if isinstance(state, dict) else None
    if not phases:
        return state.get("phase") if isinstance(state, dict) else None
    try:
        index = int(state.get("phase_idx", 0) or 0) % len(phases)
    except (TypeError, ValueError):
        index = 0
    return phases[index]


def _restore_threshold_keys(state) -> None:
    for key in ("player_threshold", "ai_threshold"):
        value = state.get(key)
        if isinstance(value, dict):
            state[key] = {int(k): item for k, item in value.items()}
