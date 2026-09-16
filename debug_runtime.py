"""Opt-in local debugging helpers for the live HConnect process.

The game server normally has no debugger dependency and never waits for a
debugger.  Set ``HEX_DEBUGPY=1`` to open a debugpy listener, and optionally set
``HEX_DEBUGPY_WAIT=1`` when starting a deliberately paused local session.
"""

from __future__ import annotations

import os


def _enabled(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def enable_debugpy(log) -> bool:
    """Start an opt-in debugpy listener without changing normal startup."""
    if (not _enabled(os.environ.get("HEX_DEBUGPY")) or
            _enabled(os.environ.get("HEX_DEBUGPY_LAUNCHED"))):
        return False
    try:
        import debugpy
    except ImportError:
        log("[debugpy] requested but package is not installed")
        return False

    host = os.environ.get("HEX_DEBUGPY_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("HEX_DEBUGPY_PORT", "5678"))
    except (TypeError, ValueError):
        port = 5678
    try:
        debugpy.listen((host, port))
    except Exception as exc:
        log(f"[debugpy] listener failed on {host}:{port}: {exc}")
        return False
    log(f"[debugpy] listening on {host}:{port}")
    if _enabled(os.environ.get("HEX_DEBUGPY_WAIT")):
        log("[debugpy] waiting for debugger client")
        debugpy.wait_for_client()
        log("[debugpy] debugger client attached")
    return True


def trace_rules_port(log, label, port, battle_state=None) -> None:
    """Write one compact native/compatibility state snapshot when enabled."""
    if not _enabled(os.environ.get("HEX_RULES_PORT_TRACE")) or port is None:
        return
    action = None
    stack = getattr(port, "action_stack", None)
    if stack is not None:
        action = stack.peek()
    phase = getattr(port, "current_turn_phase", None)
    action_name = type(action).__name__ if action is not None else None
    priority = getattr(action, "priority_player_id", None)
    priority_players = getattr(action, "priority_players", None)
    legacy = battle_state or {}
    log(
        "[rules-trace] "
        f"{label} "
        f"native_phase={phase!r} active={getattr(port, 'active_player_id', None)!r} "
        f"action={action_name!r} priority={priority!r} "
        f"queue={priority_players!r} turns={getattr(port, 'total_turns_taken', None)!r} "
        f"legacy_turn={legacy.get('turn_player')!r} "
        f"legacy_phase_idx={legacy.get('phase_idx')!r} "
        f"legacy_turn_number={legacy.get('turn_number')!r} "
        f"legacy_stack={len(legacy.get('stack') or ())}"
    )
