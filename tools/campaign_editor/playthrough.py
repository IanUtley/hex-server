#!/usr/bin/env python3
"""Auto-play a race's starter run (AZ0 -> Crayburn Castle -> AZ1) in the engine.

Chains the zones through ``teleport()`` exactly as the editor's "Follow
teleport" does, always winning battles and taking each conversation's first
non-default ending, and prints the route. Use it to check the whole starter
path still reaches AZ1 after editing tags, actions or rules.

    python3 tools/campaign_editor/playthrough.py [Race]     (default Human)
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(Path(__file__).parent)]

import campaign_rules as engine   # noqa: E402
import catalog                    # noqa: E402

MAX_STEPS = 120


def _zone_for(race: str) -> str:
    return "az0-" + race.lower().replace("'", "")


def playthrough(race: str = "Human", db=None) -> tuple[bool, list[str]]:
    """``(reached_AZ1, route)`` for ``race``'s starter run."""
    db = db or sqlite3.connect(REPO / "hconnect.db")
    db.row_factory = sqlite3.Row
    faction = catalog.FACTION_OF_RACE[race]
    zone = _zone_for(race)
    rules = catalog.rules(db, zone)[0]
    state = engine.initial_state(rules, race=race, faction=faction)
    route: list[str] = []
    for _ in range(MAX_STEPS):
        if state.get("encounter"):
            enc = state["encounter"]
            scene = enc.get("scene") if isinstance(enc, dict) else enc
            route.append(f"{zone}: battle {scene} won")
            state, _ = engine.apply(rules, state, {
                "type": "encounter_result", "node": state["current"],
                "scene": scene, "victory": True, "events": []})
            state, _ = engine.apply(rules, state, {"type": "clear_encounter"})
            continue
        tele = state.get("teleport")
        if tele:
            route.append(f"teleport {zone} -> {tele['zone']} {tele.get('node') or ''}".rstrip())
            if tele["zone"] == "AZ1":
                return True, route
            if tele["zone"].lower() == zone.lower():     # same zone: just move
                state.pop("teleport", None)
                if tele.get("node"):
                    state, _ = engine.apply(rules, state, {
                        "type": "visit", "node": tele["node"]})
                continue
            zone = tele["zone"]
            rules = catalog.rules(db, zone)[0]
            nxt = engine.initial_state(rules, race=race, faction=faction)
            nxt["flags"] = sorted(set(nxt["flags"]) | set(state["flags"]))
            nxt["quests"] = dict(state["quests"])
            node = tele.get("node") or ""
            if node and node != nxt["current"]:
                nxt["current"] = node
                nxt["nodes"] = {node: {"visits": 1, "completed": False}}
            state = nxt
            continue
        layout = engine.layout(rules, state)
        pick = next(((nid, n) for nid, n in layout["nodes"].items()
                     if n["visible"] and n["enabled"] and n["entry"]
                     and (not n["completed"] or n["retryable"])), None)
        if not pick:
            route.append(f"STUCK in {zone} at {state.get('current')}")
            return False, route
        node_id, info = pick
        entry = next(e for e in rules["nodes"][node_id]["entries"]
                     if e["id"] == info["entry"])
        state, _ = engine.apply(rules, state, {"type": "visit", "node": node_id})
        if entry["kind"] == "encounter":
            state, _ = engine.apply(rules, state, {
                "type": "encounter_result", "node": node_id,
                "scene": entry.get("scene", ""), "victory": True, "events": []})
            route.append(f"{zone}:{node_id} battle won")
        elif entry["kind"] == "conversation":
            ending = next((k for k in (entry.get("actions") or {}) if k != "*"), "")
            state, _ = engine.apply(rules, state, {
                "type": "conversation_end", "node": node_id,
                "conversation": entry.get("conversation"),
                "name": entry.get("label", ""), "ending": ending,
                "events": [ending] if ending else []})
            route.append(f"{zone}:{node_id} {entry.get('label', '')}"
                         + (f" [{ending}]" if ending else ""))
    route.append("gave up: too many steps")
    return False, route


if __name__ == "__main__":
    race = sys.argv[1] if len(sys.argv) > 1 else "Human"
    ok, route = playthrough(race)
    print("\n".join(route))
    print("REACHED AZ1" if ok else "DID NOT REACH AZ1")
    sys.exit(0 if ok else 1)
