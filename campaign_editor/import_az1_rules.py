#!/usr/bin/env python3
"""Generate a first draft of ``campaign/campaign_rules/AZ1.json`` (node state only).

The rules file states when each node is visible / hidden / blocked /
completed / retryable / quest-marked, plus the hooks and quest steps that set
the flags and quest progress those gates read. What a node offers is table
data (see ``offers.py``) and is not written here.

Sources: quest templates and the start roles in ``quest_conversations`` for
quests and quest-start hooks; taming encounters (``void_tamed_troop``
rewards) for completion gates; and the quest-start hooks hard-coded in
``campaign.py``, hand-translated into ``CODE_HOOK_GATES`` with their source
function cited.

Rerunning keeps everything already in the files: the rules file and
``quests.json`` are hand- and editor-maintained, so what is there wins
(``merge_previous`` / ``merge_quests``) and the importer only adds nodes,
gates, hooks, quests and quest steps that are new in the Records. Scene-action
hooks (``scene:`` / ``tamed:``) are rebuilt live by the rules loader and are
never added. ``--dry-run`` reports what would change without writing.

    python3 campaign_editor/import_az1_rules.py [--db hconnect.db] [--output PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths  # noqa: E402,F401  (puts campaign on sys.path)

import catalog  # noqa: E402
import offers  # noqa: E402
import campaign_rules  # noqa: E402

# campaign.py quest-start hooks, expressed as gates on the derived layout.
# A gate is re-evaluated from quest state on every layout, which is what the
# code achieves by replaying hooks on load.
CODE_HOOK_GATES = {
    # _quest_hook_az1_tamed_start / _quest_hook_az1_find_horwich_sea_start
    "Node005": {"blocked": {"all": [
        {"quest": "az01_tamed", "is": "started"},
        {"not": {"quest": "q_seawitch", "is": "started"}}]}},
    # _quest_hook_az1_tamed_start (west Zila bridge) / _az1_unlock_west_bridge
    "Node015": {"hidden": {"all": [
        {"quest": "az01_tamed", "is": "started"},
        {"not": {"quest": "az01_q_cross_the_river", "is": "started",
                 "step_at_least": 1}}]}},
    # _quest_hook_az1_tamed_start / _quest_hook_az1_cross_zodiac_start:
    # hidden until Cross the Zodiac starts; never re-hidden once finished.
    "Node018": {
        "hidden": {"all": [
            {"quest": "az01_tamed", "is": "started"},
            {"not": {"quest": "az01_q_cross_the_river_part2", "is": "started"}}]},
        "visible": {"quest": "az01_q_cross_the_river_part2", "is": "active"},
        "marker": {"quest": "az01_q_cross_the_river_part2", "is": "active",
                   "step_below": 1}},
    # _az1_gate_zodiac_west_bridge: open after Wallace's turn-in (step 3).
    "Node038": {"hidden": {"any": [
        {"all": [{"quest": "az01_tamed", "is": "started"},
                 {"quest": "az01_q_cross_the_river_part2", "is": "not_started"}]},
        {"quest": "az01_q_cross_the_river_part2", "is": "active",
         "step_below": 2}]}},
    # _quest_hook_az1_cross_the_river_start
    "Node013": {"visible": {"quest": "az01_q_cross_the_river", "is": "started"},
                "marker": {"quest": "az01_q_cross_the_river", "is": "active",
                           "step_below": 1}},
    "Node014": {"visible": {"quest": "az01_q_cross_the_river", "is": "started"},
                "marker": {"quest": "az01_q_cross_the_river", "is": "active",
                           "step_below": 1}},
    # _quest_hook_az1_find_cave_in_start / _find_ambling_mesa_start: one
    # faction route is revealed and marked, the other hidden; Lena Grotto is
    # gated until the Masked Army branch (_az1_apply_masked_army_branch,
    # not yet translated: see the review note on Node025).
    "Node017": {
        "visible": {"quest": "az01_uw_find_cave_in", "is": "started"},
        "marker": {"quest": "az01_uw_find_cave_in", "is": "active"},
        "hidden": {"all": [
            {"quest": "az01_ar_find_ambling_mesa", "is": "started"},
            {"not": {"quest": "az01_uw_find_cave_in", "is": "started"}}]}},
    "Node034": {
        "visible": {"quest": "az01_ar_find_ambling_mesa", "is": "started"},
        "marker": {"quest": "az01_ar_find_ambling_mesa", "is": "active"},
        "hidden": {"all": [
            {"quest": "az01_uw_find_cave_in", "is": "started"},
            {"not": {"quest": "az01_ar_find_ambling_mesa", "is": "started"}}]}},
    # _az1_apply_masked_army_branch: Lena Grotto opens once the champion has
    # a faction quest (_AZ1_MASKED_ARMY_QUESTS: Fort Romor / The Usurper),
    # which also switches Vale of Oberon to its faction-quest conversation.
    "Node025": {
        "blocked": {"all": [
            {"any": [{"quest": "az01_uw_find_cave_in", "is": "started"},
                     {"quest": "az01_ar_find_ambling_mesa", "is": "started"}]},
            {"not": {"any": [{"quest": "az01_q_fort_romor", "is": "started"},
                             {"quest": "az01_q_usurper", "is": "started"}]}}]},
        "visible": {"any": [{"quest": "az01_q_fort_romor", "is": "started"},
                            {"quest": "az01_q_usurper", "is": "started"}]},
    },
    # Vale of Oberon stays replayable on the faction branch until its
    # "finished faction quest" conversation is closed (the terminal one).
    "Node00R": {
        "completed": {"any": [
            {"all": [{"flag": "masked_army_met"},
                     {"not": {"any": [
                         {"quest": "az01_q_fort_romor", "is": "started"},
                         {"quest": "az01_q_usurper", "is": "started"}]}}]},
            {"flag": "masked_army_done"}]},
        "retryable": {"all": [
            {"any": [{"quest": "az01_q_fort_romor", "is": "started"},
                     {"quest": "az01_q_usurper", "is": "started"}]},
            {"not": {"flag": "masked_army_done"}}]},
    },
    # _az1_reveal_neighbors: Brink Ridge's blockade hides the northbound
    # destinations until the blockade encounter is won.
    "Node021": {"blocked": {"not": {"flag": "blockade_cleared"}},
                "hidden": {"all": [{"visited": "Node019"},
                                   {"not": {"flag": "blockade_cleared"}}]}},
    "Node023": {"blocked": {"not": {"flag": "blockade_cleared"}},
                "hidden": {"all": [{"visited": "Node019"},
                                   {"not": {"flag": "blockade_cleared"}}]}},
    "Node077": {"blocked": {"not": {"flag": "blockade_cleared"}},
                "hidden": {"all": [{"visited": "Node019"},
                                   {"not": {"flag": "blockade_cleared"}}]}},
}

QUEST_STEP_EVENTS = {"encounter": "encounter_victory",
                     "dungeon": "encounter_victory",
                     "convo": "conversation_end",
                     "conversation": "conversation_end"}


def build(db: sqlite3.Connection) -> dict:
    """Node-state rules for AZ1: gates, the hooks that feed them, quests."""
    data = catalog.server_catalog(db)
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
                step["done_when"] = {"event": event,
                                     "scene": objective["encounter"]}
            elif event == "conversation_end" and objective.get("conversation"):
                step["done_when"] = {"event": event,
                                     "conversation": objective["conversation"]}
            else:
                step["review"] = "objective has no GUID; set done_when"
            steps.append(step)
        quests[quest["script"]] = {"title": quest["title"], "steps": steps}
    # Tamed: winning is not enough, the troop must be tamed.
    for step in (quests.get("az01_tamed") or {}).get("steps") or ():
        if step.get("done_when", {}).get("event") == "encounter_victory":
            step["done_when"]["events"] = "tamed_troop"

    _entries, _starts = offers.build_offers("AZ1", data, quests)
    nodes: dict[str, dict] = {}
    hooks = []
    # Quests are started/completed by their conversations' ending actions
    # (start_quest / complete_quest, see offers.quest_actions), not by hooks.
    # Taming encounters: a win completes the node only when the troop was
    # tamed (the void_tamed_troop reward condition); otherwise it stays
    # retryable.
    for raw_node, encounters in sorted(data["encounters"].items()):
        node_id = offers.canonical_node(raw_node)
        for encounter in encounters:
            if not (encounter["battle"] and offers.tamed_scene(encounter)):
                continue
            # Its tamed:<scene8> hook comes from the scene's check_tamed()
            # victory action (offers.scene_action_hooks below).
            nodes.setdefault(node_id, {})["completed"] = {
                "flag": offers.tamed_flag(encounter["guid"])}
    # Reward entries gated by ``flag(battle:<scene8>:<event>)``: that flag is
    # set when the scene is won with the battle event (e.g. a difficulty
    # chosen in the node's conversation: ``Diff_6``).
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
    # Scene actions (set_flag / clear_flag before the battle and on victory /
    # defeat; check_tamed()) as hooks, so the simulator and the node gates see
    # the same flags. The editor rebuilds these live (catalog.rules).
    hooks += offers.scene_action_hooks(
        e for encounters in data["encounters"].values() for e in encounters)
    for node_id, gates in CODE_HOOK_GATES.items():
        nodes.setdefault(node_id, {}).update(gates)
    # Masked Army conversations are selected by their authored labels, as in
    # campaign.py _az1_masked_army_variant.
    for conversation in (data["node_conversations"].get("NodeR") or
                         data["node_conversations"].get("Node00R") or []):
        label = str(conversation["name"]).lower()
        flag = ("masked_army_done" if "finished faction quest" in label else
                "masked_army_met" if "step 1" in label else "")
        if flag:
            hooks.append({"id": flag,
                          "on": {"event": "conversation_end",
                                 "conversation": conversation["guid"]},
                          "do": [{"set_flag": flag}]})
    # campaign.py _handle_sendevent "gaal_camp_accept": Milosh's reading sets
    # PublicState gaal_fortune (read by flag(gaal_fortune) conversation tags).
    hooks.append({"id": "gaal_fortune",
                  "on": {"event": "script_event", "name": "gaal_camp_accept"},
                  "do": [{"set_flag": "gaal_fortune"}]})
    # campaign.py consume_fortune: the reading is used up when the next
    # campaign battle starts (won or lost).
    for result in ("encounter_victory", "encounter_defeat"):
        hooks.append({"id": f"gaal_fortune_used:{result}", "on": {"event": result},
                      "do": [{"clear_flag": "gaal_fortune"}]})
    # The Brink Ridge blockade flag (blockade_cleared) is set by the Node 19
    # blockade scenes' own victory actions (set_flag(blockade_cleared)), which
    # the rules loader rebuilds as scene: hooks; no Smoldering Dead hook.
    edges = {}
    for edge in data["edges"]:
        left, right = sorted((edge["from_node"], edge["to_node"]))
        edges[(left, right)] = edge["path_name"] or f"Path_{left}_{right}"
    return {
        "schema_version": campaign_rules.SCHEMA_VERSION,
        "campaign": "AZ1",
        "scene": "Howling Plains",
        "generated_by": "campaign_editor/import_az1_rules.py",
        "start": {"node": "Node001", "node_completed": True, "flags": []},
        "map": {"paths": [{"id": name, "from": a, "to": b}
                          for (a, b), name in sorted(edges.items())]},
        "nodes": dict(sorted(nodes.items())),
        # Campaign-wide (campaign/campaign_rules/quests.json); split off by main().
        "quests": quests,
        "hooks": hooks,
    }


# Hooks the rules loader rebuilds from the scenes' actions on every load.
LIVE_HOOK_PREFIXES = ("scene:", "tamed:")
# Top-level keys the importer regenerates; every other key of an existing file
# (start, scene, runtime, edited_states, count_pickers, offer_order, ...) is
# hand- or editor-maintained and kept as it is.
GENERATED_KEYS = ("map", "nodes", "hooks", "quests")


def merge_previous(rules: dict, path: Path) -> dict:
    """Merge a freshly built rules document into the existing file at ``path``.

    The existing file wins: its top-level keys (other than the map), its node
    gates and its hooks are kept unchanged, including gates and hooks that
    were removed by hand (a hand-removed gate is not re-added once the node
    exists in the file). Only nodes the file does not have, and hooks with a
    new id that the loader does not rebuild live, are added.
    """
    if not path.exists():
        rules["hooks"] = [h for h in rules.get("hooks") or ()
                          if not str(h.get("id") or "").startswith(LIVE_HOOK_PREFIXES)]
        return rules
    previous = json.loads(path.read_text(encoding="utf-8"))
    merged = {}
    for key, value in previous.items():
        merged[key] = value if key not in ("map", "quests") else rules.get(key, value)
    for key, value in rules.items():
        if key not in merged and (key in GENERATED_KEYS):
            merged[key] = value
    nodes = dict(previous.get("nodes") or {})
    for node_id, gates in (rules.get("nodes") or {}).items():
        if node_id not in nodes:
            nodes[node_id] = gates
    merged["nodes"] = nodes
    hooks = list(previous.get("hooks") or [])
    known = {str(h.get("id") or "") for h in hooks}
    for hook in rules.get("hooks") or ():
        hook_id = str(hook.get("id") or "")
        if hook_id not in known and not hook_id.startswith(LIVE_HOOK_PREFIXES):
            hooks.append(hook)
            known.add(hook_id)
    merged["hooks"] = hooks
    if "quests" in rules:
        merged["quests"] = rules["quests"]
    return merged


def merge_quests(shared: dict, quests: dict) -> dict:
    """Merge freshly built quests into ``quests.json``'s ``quests``.

    An existing quest keeps its fields and its steps exactly as they are
    (hand-set ``done_when`` blocks, removed ones, cleared ``review`` flags);
    only quests and step ids that are new in the Records are added, a new
    step after the step it follows in the Records.
    """
    out = dict(shared.get("quests") or {})
    for script, built in quests.items():
        current = out.get(script)
        if current is None:
            out[script] = built
            continue
        steps = list(current.get("steps") or [])
        have = {s.get("id") for s in steps}
        for index, step in enumerate(built.get("steps") or ()):
            if step.get("id") in have:
                continue
            before = (built["steps"][index - 1].get("id") if index else None)
            at = next((i + 1 for i, s in enumerate(steps) if s.get("id") == before), len(steps))
            steps.insert(at, step)
            have.add(step.get("id"))
        if steps != (current.get("steps") or []):
            out[script] = dict(current, steps=steps)
    return dict(sorted(out.items()))


def keep_tagged_gates(rules, path: Path) -> None:
    """Carry over node states saved in the editor (``edited_states``)."""
    if not path.exists():
        return
    previous = json.loads(path.read_text(encoding="utf-8"))
    edited = previous.get("edited_states") or {}
    for node_id, gates in edited.items():
        spec = (previous.get("nodes") or {}).get(node_id) or {}
        for gate in gates:
            target = rules["nodes"].setdefault(node_id, {})
            if gate in spec:
                target[gate] = spec[gate]
            else:
                target.pop(gate, None)
    if edited:
        rules["edited_states"] = edited
    if previous.get("runtime"):        # the server runs this zone from rules
        rules["runtime"] = previous["runtime"]
    rules["nodes"] = dict(sorted(rules["nodes"].items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=catalog.REPO / "hconnect.db")
    parser.add_argument("--output", type=Path,
        default=catalog.CAMPAIGN_ROOT / "campaign_rules" / "AZ1.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="report which files would change; write nothing")
    args = parser.parse_args()
    db = catalog.connect(args.db)
    try:
        rules = build(db)
    finally:
        db.close()
    keep_tagged_gates(rules, args.output)
    rules = merge_previous(rules, args.output)
    problems = campaign_rules.validate(
        rules, known_conversations=set(catalog.conversations()))
    quests = rules.pop("quests")
    # Quests are campaign-wide: every zone's rules load them from here.
    quests_path = args.output.parent / "quests.json"
    shared = (json.loads(quests_path.read_text(encoding="utf-8"))
              if quests_path.exists() else {"schema_version": 1})
    shared = dict(shared, quests=merge_quests(shared, quests))
    outputs = {args.output: json.dumps(rules, indent=1) + "\n",
               quests_path: json.dumps(shared, indent=1) + "\n"}
    for path, text in outputs.items():
        same = path.exists() and path.read_text(encoding="utf-8") == text
        print(f"{path}: {'unchanged' if same else 'would change' if args.dry_run else 'written'}")
        if not args.dry_run and not same:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    print(f"{len(rules['nodes'])} gated nodes, {len(rules['hooks'])} hooks; "
          f"{len(quests)} AZ1 quests")
    for problem in problems:
        print(f"  {problem['level']}: {problem['where']}: {problem['text']}")


if __name__ == "__main__":
    main()
