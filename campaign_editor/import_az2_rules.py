#!/usr/bin/env python3
"""Generate ``campaign/campaign_rules/AZ2.json`` (the Alachian Sea overworld).

The same shape as ``import_az1_rules.py``, without AZ1's hand-translated quest
hooks: AZ2 has none in ``campaign.py``, so its node gates come from the
authored data alone - taming and reward flags from the encounters, scene
actions and the quests' own conversations. The map is the ``campaign_node_edges``
seed, which matches the client's path prefabs exactly (AZ1 checked the same way).

Quests are campaign-wide and live in ``campaign_rules/quests.json``, which
already holds every AZ2 quest with hand-set ``done_when`` values. This script
does not rewrite that file; it lists the steps that still have no completion
trigger (``review``), which need a ``done_when`` by hand.

    python3 campaign_editor/import_az2_rules.py [--db hconnect.db] [--write]

Without ``--write`` it only prints what it would change. An existing rules
file wins: its hand-set gates, hooks and other keys are kept and only new
nodes and hooks are added (``import_az1_rules.merge_previous``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths  # noqa: E402,F401  (puts campaign on sys.path)

import catalog  # noqa: E402
import offers  # noqa: E402
import campaign_rules  # noqa: E402
from import_az1_rules import merge_previous  # noqa: E402

ZONE = "AZ2"
# A teleport into the zone lands on its node; this is the node a fresh
# entry starts on when the zone was never visited.
START = "Node099"
QUEST_STEP_EVENTS = {"encounter": "encounter_victory",
                     "dungeon": "encounter_victory",
                     "convo": "conversation_end",
                     "conversation": "conversation_end"}


def build_quests(data: dict) -> dict:
    quests = {}
    for quest in data["quests"]:
        if quest["group"] != "AREA":
            continue
        steps = []
        for objective in quest["objectives"]:
            kind = str(objective.get("type") or "").lower()
            step = {"id": objective.get("id", ""),
                    "title": objective.get("title", ""), "type": kind}
            event = QUEST_STEP_EVENTS.get(kind)
            if kind in ("encounter", "dungeon") and objective.get("encounter"):
                step["done_when"] = {"event": event, "scene": objective["encounter"]}
            elif event == "conversation_end" and objective.get("conversation"):
                step["done_when"] = {"event": event,
                                     "conversation": objective["conversation"]}
            else:
                step["review"] = "objective has no GUID; set done_when"
            steps.append(step)
        quests[quest["script"]] = {"title": quest["title"], "steps": steps}
    return quests


def build(db) -> dict:
    data = catalog.server_catalog(db, ZONE)
    quests = build_quests(data)
    _entries, _starts = offers.build_offers(ZONE, data, quests)
    nodes: dict[str, dict] = {}
    hooks = []
    # A won taming encounter completes its node only with the troop tamed
    # (the void_tamed_troop reward condition); otherwise it stays retryable.
    for raw_node, encounters in sorted(data["encounters"].items()):
        node_id = offers.canonical_node(raw_node)
        for encounter in encounters:
            if not (encounter["battle"] and offers.tamed_scene(encounter)):
                continue
            nodes.setdefault(node_id, {})["completed"] = {
                "flag": offers.tamed_flag(encounter["guid"])}
    # Reward entries gated by flag(battle:<scene8>:<event>) (a difficulty chosen
    # in the node's conversation sets it when the scene is won with that event).
    for encounters in data["encounters"].values():
        for encounter in encounters:
            rewards = encounter.get("rewards") or {}
            for entry in rewards.get("end_of_game_rewards") or [rewards]:
                for tag in (entry or {}).get("when") or ():
                    match = re.fullmatch(r"flag\((battle:([0-9a-f]{8}):(.+))\)", str(tag))
                    if match and encounter["guid"].startswith(match.group(2)):
                        hooks.append({
                            "id": match.group(1),
                            "on": {"event": "encounter_victory",
                                   "scene": encounter["guid"],
                                   "events": match.group(3)},
                            "do": [{"set_flag": match.group(1)}]})
    # Scene actions (set_flag / clear_flag, check_tamed()) as hooks, so the
    # simulator and the node gates see the same flags.
    hooks += offers.scene_action_hooks(
        e for encounters in data["encounters"].values() for e in encounters)
    edges = {}
    for edge in data["edges"]:
        left, right = sorted((edge["from_node"], edge["to_node"]))
        edges[(left, right)] = edge["path_name"] or f"Path_{left}_{right}"
    return {
        "schema_version": campaign_rules.SCHEMA_VERSION,
        "campaign": "AZ2",
        "scene": "The Alachian Sea",
        "generated_by": "campaign_editor/import_az2_rules.py",
        "runtime": "rules",
        "start": {"node": START, "node_completed": False, "flags": []},
        "map": {"paths": [{"id": name, "from": a, "to": b}
                          for (a, b), name in sorted(edges.items())]},
        "nodes": dict(sorted(nodes.items())),
        "hooks": hooks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=catalog.REPO / "hconnect.db")
    parser.add_argument("--output", type=Path,
                        default=catalog.CAMPAIGN_ROOT / "campaign_rules" / "AZ2.json")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    db = catalog.connect(args.db)
    try:
        rules = build(db)
        quests = build_quests(catalog.server_catalog(db, ZONE))
    finally:
        db.close()
    rules = merge_previous(rules, args.output)
    problems = campaign_rules.validate(
        rules, known_conversations=set(catalog.conversations()))
    # The steps of this zone's quests that quests.json still has no
    # completion trigger for (a conversation ending's complete_step action
    # can also finish a step, so this is a list to check, not an error).
    shared_path = args.output.parent / "quests.json"
    shared = (json.loads(shared_path.read_text(encoding="utf-8")).get("quests") or {}
              if shared_path.exists() else {})
    review = [f"{script}.{step['id']}" for script in quests
              for step in (shared.get(script) or quests[script])["steps"]
              if "done_when" not in step]
    print(f"{args.output}: {len(rules['nodes'])} gated nodes, "
          f"{len(rules['map']['paths'])} paths, {len(rules['hooks'])} hooks; "
          f"{len(review)} quest steps without done_when: {review}")
    for problem in problems:
        print(f"  {problem['level']}: {problem['where']}: {problem['text']}")
    text = json.dumps(rules, indent=1) + "\n"
    same = args.output.exists() and args.output.read_text(encoding="utf-8") == text
    print(f"{args.output}: {'unchanged' if same else 'would change'}")
    if not args.write or same:
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
