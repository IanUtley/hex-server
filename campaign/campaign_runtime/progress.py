"""A champion's rules progress: the campaign-wide part plus one part per zone.

Stored in ``campaign_progress`` (``zone='*'`` is the campaign-wide row). Every
read-modify-write of one champion's progress runs under that champion's lock,
so two connections' events for the same champion cannot interleave.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any

import pve_db

CAMPAIGN = "*"
# Kept across a SIGUSR1 module reload: a request still running then must
# hold the same champion lock as the requests after it.
_GUARD = globals().get("_GUARD") or threading.Lock()
_LOCKS: dict[int, threading.RLock] = globals().get("_LOCKS") or {}


@contextmanager
def champion_lock(champion_id: int):
    with _GUARD:
        lock = _LOCKS.setdefault(int(champion_id), threading.RLock())
    with lock:
        yield


def load(db, champion_id: int) -> tuple[dict[str, Any], dict[str, dict]] | None:
    """``(campaign part, {zone: zone part})``, or None before the first save."""
    rows = pve_db.db_campaign_progress_rows(champion_id, conn=db)
    if CAMPAIGN not in rows:
        return None
    parsed = {zone: json.loads(raw or "{}") for zone, raw in rows.items()}
    return parsed.pop(CAMPAIGN), parsed


def fresh(race_id) -> tuple[dict[str, Any], dict[str, dict]]:
    """``(campaign part, {})``: a champion with no progress yet, in no zone."""
    from campaign_runtime import locate
    race_key = int(race_id or 0)
    return ({"flags": [], "quests": {}, "counters": {}, "keywords": [],
             "race": locate.RACE_NAMES.get(race_key, ""),
             "faction": locate.FACTIONS.get(race_key, "")}, {})


def save(db, champion_id: int, campaign: dict[str, Any],
         zones: dict[str, dict[str, Any]]) -> None:
    """Write the campaign part and the given zones (the caller commits)."""
    rows = {CAMPAIGN: json.dumps(campaign, sort_keys=True)}
    rows.update({zone: json.dumps(part, sort_keys=True)
                 for zone, part in zones.items()})
    pve_db.db_campaign_progress_save(champion_id, rows, conn=db)
