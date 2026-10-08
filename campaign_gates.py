"""Quest-driven AZ1 map gates, evaluated from ``campaign_rules/AZ1.json``.

The rules file states, per node, when it is ``visible`` (forced), ``hidden``,
``blocked`` or carries a quest ``marker``.  This module evaluates those
conditions against the champion's persisted facts (quest rows, conversation
flags, visited nodes) and writes the result into the ``PublicState`` lists
``campaign._az1_reveal_neighbors`` consumes (``blocked_nodes``,
``quest_hidden_nodes``, ``quest_reveal_nodes`` / ``unlocked_nodes``,
``quest_nodes``).  It replaces the old ``az1_*_start`` hook functions, so a
quest "starting" needs no code: starting it changes the quest row and the
gates follow.

Brink Ridge's blockade nodes stay with ``_az1_reveal_neighbors`` (their gate
is an encounter result it already tracks).
"""

from __future__ import annotations

import threading
from pathlib import Path

import campaign_rules

RULES_DIR = Path(__file__).parent / "campaign_rules"
GATE_KEYS = ("blocked", "hidden", "visible", "marker")
# Nodes whose gates code owns instead of the rules file (none now: Brink
# Ridge's blockade is editor data).
BLOCKADE_NODES: frozenset[str] = frozenset()

_LOCK = threading.Lock()
_RULES: dict[str, tuple[float, dict]] = {}


def rules_for(template: str) -> dict | None:
    path = RULES_DIR / f"{template}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _LOCK:
        cached = _RULES.get(template)
        if cached and cached[0] == mtime:
            return cached[1]
    rules = campaign_rules.load_rules(path)
    with _LOCK:
        _RULES[template] = (mtime, rules)
    return rules


def engine_state(campaign, db, rules, champ_id, state):
    """The engine's progress view, built from the legacy persisted facts."""
    current = str(state.get("LastNode") or state.get("ALoc") or "")
    view = campaign._conversation_tag_state(db, champ_id, state, current, [])
    flags = set(view["flags"])
    quests = {}
    for script in rules.get("quests") or ():
        _qid, quest_state = campaign._quest_state_row(db, champ_id, script)
        if not isinstance(quest_state, dict):
            continue
        try:
            step = int((quest_state.get("Flags") or {}).get(
                "_quest_objective_idx", 0) or 0)
        except (TypeError, ValueError):
            step = 0
        quests[script] = {"status": "finished" if quest_state.get("Finished")
                          else "active", "step": step}
    # Replay the hooks whose triggers legacy records indirectly: finished
    # conversations (``conversation(Name)`` flags) and the encounter step of
    # every quest objective already passed.
    done_scenes = set()
    for script, progress in quests.items():
        for index, step in enumerate((rules["quests"][script]).get("steps") or ()):
            if progress["status"] == "finished" or index < progress["step"]:
                scene = (step.get("done_when") or {}).get("scene")
                if scene:
                    done_scenes.add(str(scene).lower())
    for hook in rules.get("hooks") or ():
        on = hook.get("on") or {}
        fired = False
        if on.get("event") == "conversation_end" and on.get("conversation"):
            name = campaign._conversation_name(db, on["conversation"])
            fired = bool(name) and campaign_rules.conversation_flag(name) in flags
        elif on.get("event") == "encounter_victory" and on.get("scene"):
            fired = str(on["scene"]).lower() in done_scenes
        if fired:
            flags.update(e["set_flag"] for e in hook.get("do") or ()
                         if "set_flag" in e)
    view.update(flags=sorted(flags), quests=quests, current=current)
    return view


def _members(pdata, key):
    return [str(v) for v in (pdata.get(key) or []) if v]


def _set_member(pdata, key, node, on):
    values = [v for v in _members(pdata, key) if v != node]
    if on:
        values.append(node)
    pdata[key] = values


def apply(campaign, db, champ_id, state, template="AZ1") -> None:
    """Rewrite the gated nodes' PublicState lists from the rules.

    Only nodes the rules gate are touched; an absent gate key means "not
    blocked / hidden / forced visible / marked", so stale entries left by the
    old hooks are cleared.  Hidden nodes also lose any recorded visit.
    """
    rules = rules_for(template)
    if not rules or not isinstance(state, dict):
        return
    view = engine_state(campaign, db, rules, champ_id, state)
    pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
    for node_id, spec in (rules.get("nodes") or {}).items():
        if "impassable" in spec:
            # The location's /impassable field: the client draws it red and
            # stops the character token there until it is completed.
            # A let_pass() (bribe / sneak) lets the player leave on this visit.
            on = (campaign_rules.evaluate(spec["impassable"], view, node_id)
                  and str(pdata.get("pass_node") or "") != node_id)
            for loc in state.get("VisLocs") or ():
                data = (loc or {}).get("Data") or {}
                if data.get("node") == node_id:
                    data["impassable"] = bool(on and not data.get("completed"))
        if node_id in BLOCKADE_NODES or not any(k in spec for k in GATE_KEYS):
            continue
        gate = {k: (k in spec and campaign_rules.evaluate(spec[k], view, node_id))
                for k in GATE_KEYS}
        _set_member(pdata, "blocked_nodes", node_id, gate["blocked"])
        _set_member(pdata, "quest_hidden_nodes", node_id, gate["hidden"])
        _set_member(pdata, "quest_reveal_nodes", node_id, gate["visible"])
        _set_member(pdata, "unlocked_nodes", node_id, gate["visible"])
        _set_member(pdata, "quest_nodes", node_id, gate["marker"])
        if gate["hidden"]:
            _forget_visits(state, pdata, node_id)


def _forget_visits(state, pdata, node_id):
    """A hidden node cannot have been visited: drop stale discovery state."""
    names = {node_id}
    for loc in state.get("VisLocs") or ():
        data = (loc or {}).get("Data") or {}
        if data.get("node") == node_id:
            names |= {str(data.get("name") or ""), str(data.get("title") or "")}
    names.discard("")
    pdata["visited_nodes"] = [v for v in (pdata.get("visited_nodes") or [])
                              if str(v) not in names]
    pdata["visited_paths"] = [v for v in (pdata.get("visited_paths") or [])
                              if node_id not in str(v)]
    if isinstance(pdata.get("conversation_visits"), dict):
        pdata["conversation_visits"].pop(node_id, None)
