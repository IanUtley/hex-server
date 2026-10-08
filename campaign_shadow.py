"""Shadow comparison of campaign.py's AZ1 map state against campaign_rules.

Feature flag only: the client is always served the legacy state.  When
enabled, every AREA campaign response is re-derived through the declarative
engine from the facts the legacy code already persists (conversation flags,
quest rows, visited nodes) and any difference in what each node shows
(visible / completed / repeatable / blocked) is logged and kept for
``!campaign_rules diff``.

Enable with ``CAMPAIGN_RULES_SHADOW=1`` or at runtime with
``!campaign_rules on|off``.  The runtime switch writes the environment
variable, so it survives a SIGUSR1 module reload.
"""

from __future__ import annotations

import json
import os
import threading
import time

import campaign_gates
import campaign_rules

ENV_FLAG = "CAMPAIGN_RULES_SHADOW"
MAX_REPORTS = 256

_LOCK = threading.Lock()
_REPORTS: dict[int, dict] = {}          # campaign id -> latest report


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "on", "yes")


def set_enabled(value: bool) -> None:
    os.environ[ENV_FLAG] = "1" if value else "0"


def _rules_for(template: str) -> dict | None:
    return campaign_gates.rules_for(template)


def _diff(rules, layout, state):
    data = (state.get("PublicState") or {}).get("Data") or {}
    blocked = {str(v) for v in data.get("blocked_nodes") or ()}
    diffs = []
    for loc in state.get("VisLocs") or ():
        ld = (loc or {}).get("Data") or {}
        node = str(ld.get("node") or "")
        mine = layout["nodes"].get(node)
        if not mine or node.startswith("Fork"):
            continue
        pairs = (("visible", ld.get("visible", True) is not False, mine["visible"]),
                 ("completed", bool(ld.get("completed")), mine["completed"]),
                 ("repeatable", bool(ld.get("repeatable")), mine["retryable"]),
                 # Legacy lists quest-hidden nodes as blocked; the engine has both.
                 ("blocked", node in blocked, mine["blocked"] or mine["hidden"]))
        diffs.extend({"node": node, "field": field, "legacy": old, "engine": new}
                     for field, old, new in pairs if old != new)
    return diffs


def observe(db, camp_id, state, log=print) -> None:
    """Compare one outgoing campaign state; never raises into the caller."""
    if not enabled() or not isinstance(state, dict):
        return
    try:
        import campaign
        import pve_db
        if str(state.get("TempType") or "").upper() != "AREA":
            return
        template = str(pve_db.db_campaign_template_name(camp_id, conn=db) or "")
        rules = _rules_for(template) if template else None
        champ_id = state.get("ChampID")
        if not rules or champ_id is None:
            return
        view = campaign_gates.engine_state(campaign, db, rules, champ_id, state)
        diffs = _diff(rules, campaign_rules.layout(rules, view), state)
        digest = json.dumps(diffs, sort_keys=True)
        with _LOCK:
            previous = _REPORTS.get(camp_id)
            if len(_REPORTS) >= MAX_REPORTS and camp_id not in _REPORTS:
                _REPORTS.pop(next(iter(_REPORTS)))
            _REPORTS[camp_id] = {"template": template, "champ": champ_id,
                                 "at": time.time(), "diffs": diffs,
                                 "digest": digest}
        if not previous or previous["digest"] != digest:
            log(f"[campaign-shadow] camp={camp_id} {template}: "
                f"{len(diffs)} difference(s)" +
                "".join(f"\n    {d['node']}.{d['field']}: legacy={d['legacy']} "
                        f"engine={d['engine']}" for d in diffs[:20]))
    except Exception as exc:            # shadow mode must never break gameplay
        log(f"[campaign-shadow] error: {exc!r}")


def report_text(limit=12) -> str:
    with _LOCK:
        reports = sorted(_REPORTS.items(), key=lambda kv: -kv[1]["at"])
    lines = [f"campaign_rules shadow: {'ON' if enabled() else 'OFF'}"
             f" ({len(reports)} campaign(s) observed)"]
    for camp_id, report in reports[:5]:
        diffs = report["diffs"]
        lines.append(f"camp {camp_id} {report['template']}: "
                     f"{len(diffs)} difference(s)")
        lines += [f"  {d['node']}.{d['field']} legacy={d['legacy']} "
                  f"engine={d['engine']}" for d in diffs[:limit]]
    return "\n".join(lines)


def reset() -> None:
    with _LOCK:
        _REPORTS.clear()
