"""Mirror the engine's quest progress into the client's quest journal.

The engine keeps ``quests[script] = {"status", "step"}`` campaign-wide; the
client's journal is one QUEST campaign row per quest whose objectives are
revealed one at a time (``campaign._ensure_quest_campaign`` /
``_advance_quest_campaign``). ``sync`` brings the rows level with the engine
and returns what to tell the client: new journal entries (``campspawn``),
progressed ones (``cmpupdate``) and the rewards of quests just finished.
"""

from __future__ import annotations

import json
from typing import Any

import pve_db


def _journal_index(db, champion_id: int, script: str):
    rows = pve_db.db_campaign_state_rows(
        champion_id, "QUEST", db, template_name=script)
    if not rows:
        return None, None
    qid, raw = rows[0]
    state = json.loads(raw or "{}")
    return int((state.get("Flags") or {}).get("_quest_objective_idx", 0) or 0), state


def sync(handler, db, camp_id: int, champion_id: int, client_state: dict,
         before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Bring the journal level with ``after`` (the engine's quests).

    Returns ``{"spawned": [(qid, script, state)], "progressed": [state],
    "applied": AppliedUpdates}``; the caller pushes the notifications after
    its response.
    """
    import campaign
    out = {"spawned": [], "progressed": [], "applied": campaign._empty_applied_updates()}
    for script, quest in (after or {}).items():
        status = quest.get("status")
        if status not in ("active", "finished"):
            continue
        old = (before or {}).get(script) or {}
        if old == quest:
            continue
        index, state = _journal_index(db, champion_id, script)
        if state is None:
            qid = campaign._ensure_quest_campaign(db, champion_id, None, script)
            if not qid:
                continue           # no QuestTemplate: an engine-only quest
            index, state = _journal_index(db, champion_id, script)
            out["spawned"].append((qid, script, state))
        target = int(quest.get("step", 0) or 0)
        objectives = len((state.get("Flags") or {}).get("_quest_objectives") or ())
        if status == "finished":
            target = max(target, objectives)
        progressed = None
        while index is not None and index < target and not state.get("Finished"):
            progressed = campaign._advance_quest_campaign(db, champion_id, script)
            if progressed is None:
                break
            index, state = _journal_index(db, champion_id, script)
        if progressed is not None:
            out["progressed"].append(progressed)
        if status == "finished" and old.get("status") != "finished":
            campaign._merge_applied(out["applied"], campaign._apply_quest_rewards(
                handler, db, camp_id, client_state, script))
    return out
