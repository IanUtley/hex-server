"""Reconnect decline tracking for FRA/practice sessions."""

import time


def _reconnect_declined_for_pid(state, player_id):
    """Return whether this player dismissed reconnect for this game session."""
    if not isinstance(state, dict):
        return False
    try:
        player_id = int(player_id)
    except (TypeError, ValueError):
        return False
    if player_id <= 0:
        return False
    declined = state.get("reconnect_declined_pids")
    if isinstance(declined, dict):
        if str(player_id) in declined or player_id in declined:
            return True
    elif isinstance(declined, (list, tuple, set)):
        try:
            if player_id in {int(value) for value in declined}:
                return True
        except (TypeError, ValueError):
            pass
    # Read the older single-player marker written before declines were
    # tracked independently for each participant.
    try:
        return int(state.get("reconnect_declined_pid") or 0) == player_id
    except (TypeError, ValueError):
        return False


def _record_reconnect_decline(state, player_id):
    """Persist a per-player dismiss marker in this session's checkpoint."""
    if not isinstance(state, dict):
        return False
    try:
        player_id = int(player_id)
    except (TypeError, ValueError):
        return False
    if player_id <= 0:
        return False
    declined = state.get("reconnect_declined_pids")
    if isinstance(declined, dict):
        declined = dict(declined)
    elif isinstance(declined, (list, tuple, set)):
        declined = {str(value): 0 for value in declined}
    else:
        declined = {}
    declined_at = int(time.time())
    declined[str(player_id)] = declined_at
    state["reconnect_declined_pids"] = declined
    # Keep writing the legacy fields so checkpoints remain readable by older
    # server builds; the per-player mapping above is authoritative.
    state["reconnect_declined_pid"] = player_id
    state["reconnect_declined_at"] = declined_at
    return True
