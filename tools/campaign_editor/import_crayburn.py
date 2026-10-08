#!/usr/bin/env python3
"""Reverse-implement Castle Crayburn and the AZ0 starter panoramas as data.

campaign.py drives both in code: ``_AZ0_RACE_CONFIG`` (per-race panorama
NPCs and conversations), ``_CASTLE_CHAIN`` / ``_CASTLE_NODE_CONV`` and the
generated ``_CRAYBURN_CASTLE`` seed (per-race castle conversations) plus
``_reveal_crayburn_node`` (fog of war: nothing past the first unfinished
node). This writes the same content in the editor's model:

* ``campaign_data/crayburn.json`` - one ``campaign_node_conversations`` row
  per race and castle node (template ``Crayburn Castle``), tagged
  ``ChampionIsRace(<race>)`` and ``outcome:success|fail`` for battle results.
  Conversations are found by their Records names (``Crayburn Castle - <Race>
  - <Node>[ Success| Fail]``); battles need no rows - the race scenes are
  matched by name (``<Race> Tutorial <Node>``, zones.json ``scene_regex``).
* ``campaign_data/AZ0.json`` - each race's starter-panorama conversations
  (template ``AZ0``, ``node_id`` = the NPC), from ``_AZ0_RACE_CONFIG``.
* ``campaign_rules/crayburn.json`` - node states: each castle node is
  ``blocked`` until the previous one is completed.

campaign.py is read with ``ast`` (never imported: importing it opens the
server database). Every generated row is cross-checked against the seed and
mismatches are reported. The server does not read these rows yet.

    python3 tools/campaign_editor/import_crayburn.py
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import catalog

CAMPAIGN_PY = catalog.REPO / "campaign.py"
DUNGEON_TEMPLATE = "Crayburn Castle"
RACE_IDS = {1: "Human", 2: "Elf", 3: "Coyotle", 4: "Orc", 5: "Dwarf",
            6: "Shin'hare", 7: "Vennen", 8: "Necrotic"}
# _AZ0_RACE_CONFIG conversation key -> (NPC key, label, outcome tag).
# _AZ0_RACE_CONFIG conversation key -> (NPC key, label, outcome tag).
# NPCs follow campaign.py: the intro NPC opens and later closes the starter
# panorama (transition), the trainer offers the training battle, the quest
# NPC sends the player to Crayburn and takes the report.
AZ0_ROLES = (
    ("transition_repeat", "intro_npc", "Leave for AZ1 (repeat)", ""),
    ("transition_conv", "intro_npc", "Leave for AZ1", ""),
    ("intro_conv", "intro_npc", "Intro", ""),
    ("training_success_conv", "trainer_npc", "Training success", "outcome:success"),
    ("training_fail_conv", "trainer_npc", "Training fail", "outcome:fail"),
    ("battle_conv", "trainer_npc", "Training battle offer", ""),
    ("_report", "quest_npc", "Crayburn report", ""),
    ("quest_conv", "quest_npc", "Crayburn quest start", ""),
)


# Ending actions for the starter flow (campaign.py _handle_sendevent):
# finishing the quest-giver's Crayburn conversation turns the panorama into
# the dungeon; after the report, "ready" (choice_go) activates AZ1 at
# Node001 (_activate_az1_area).
CRAYBURN_QUEST = "az01_q_dwarf_crayburn"
AZ0_ACTIONS = {
    # Reporting back finishes the Crayburn quest; its reward is the quest's.
    "_report": {"*": [f"complete_quest({CRAYBURN_QUEST})"]},
    "quest_conv": {"*": ["teleport(crayburn)"]},
    "transition_conv": {"choice_go": ["teleport(AZ1,Node001)"]},
    "transition_repeat": {"choice_go": ["teleport(AZ1,Node001)"]},
}


def az0_flow_tags(key: str, names: dict[str, str]) -> list[str]:
    """When each starter conversation is offered (campaign.py
    _handle_sendevent conv_done / _apply_gameend), as tags over the
    conversation flags; ``names`` maps a role to its conversation name."""
    done = lambda role, ending="": "conversation({}{})".format(  # noqa: E731
        names[role], f",{ending}" if ending else "")
    return {
        "intro_conv": [f"!{done('intro_conv')}"],
        "battle_conv": [done("intro_conv")],
        "training_success_conv": ["!" + done("training_success_conv")],
        "training_fail_conv": [],
        "quest_conv": ["!flag(crayburn_complete)"],
        "_report": ["flag(crayburn_complete)", "!" + done("_report")],
        "transition_conv": [done("_report")],
        "transition_repeat": [done("transition_conv", "choice_stay")],
    }[key]


def _keep_editor_edits(path: Path, rows: list[dict]) -> None:
    """Tags/actions saved in the editor win over regenerated ones."""
    if not path.exists():
        return
    previous = json.loads(path.read_text(encoding="utf-8"))
    saved = {(r.get("campaign_template"), str(r.get("conversation_guid")).lower()):
             r.get("trigger_json") or {}
             for r in (previous.get("tables") or {}).get("campaign_node_conversations") or ()}
    for row in rows:
        old = saved.get((row["campaign_template"], row["conversation_guid"].lower()))
        for key in ("tags", "actions"):
            if old and old.get("edited_" + key):
                row["trigger_json"][key] = old.get(key)
                row["trigger_json"]["edited_" + key] = True


def code_constants(path: Path = CAMPAIGN_PY) -> dict:
    """Module-level literals from campaign.py (the last assignment wins, as
    at import time)."""
    wanted = {"_AZ0_RACE_CONFIG", "_CASTLE_CHAIN", "_CASTLE_NODE_CONV",
              "_CRAYBURN_CASTLE", "_CRAYBURN_RACE_SCENE_IDS"}
    found = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", "")
            if name in wanted:
                found[name] = ast.literal_eval(node.value)
    missing = wanted - set(found)
    if missing:
        raise SystemExit(f"campaign.py is missing {sorted(missing)}")
    return found


def _conversations_by_name() -> dict[str, str]:
    return {c["name"]: guid for guid, c in catalog.conversations().items()}


def _zone_id(race: str) -> str:
    return "az0-" + re.sub(r"[^a-z]", "", race.lower())


def crayburn_rows(code: dict, problems: list[str]) -> list[dict]:
    by_name = _conversations_by_name()
    conversations = catalog.conversations()
    rows = []
    last_battle = code["_CASTLE_CHAIN"][-1]
    for race in RACE_IDS.values():
        seed = (code["_CRAYBURN_CASTLE"]["races"].get(race) or {}).get("nodes") or {}
        for order, node in enumerate(code["_CASTLE_CHAIN"]):
            title = code["_CASTLE_NODE_CONV"].get(node)
            if not title:
                continue          # the Entrance: pass-through, no content
            for suffix, outcome, key in (("", "", "conv"),
                                         (" Success", "success", "success"),
                                         (" Fail", "fail", "fail")):
                name = f"Crayburn Castle - {race} - {title}{suffix}"
                guid = by_name.get(name)
                expected = (seed.get(title) or {}).get(key)
                if guid is None:
                    if expected:
                        problems.append(f"{name}: in the seed but not in Records")
                    continue
                if expected and expected.lower() != guid.lower():
                    problems.append(f"{name}: Records {guid} != seed {expected}")
                if expected is None:
                    problems.append(f"{name}: in Records but not in the seed")
                tags = [f"ChampionIsRace({race})"]
                if outcome:
                    tags.insert(0, f"outcome:{outcome}")
                found = conversations.get(guid.lower()) or {}
                trigger = {"label": f"{title}{suffix}",
                           "source": "import_crayburn.py", "tags": tags,
                           # The race's trainer NPC goes through the castle.
                           "speaker": found.get("owner") or
                           catalog.champion_names().get(found.get("champion"), "")}
                if node == last_battle and outcome == "success":
                    # advance_crayburn_step: the castle is beaten; the player
                    # returns to the starter panorama for the report.
                    trigger["actions"] = {"*": [f"teleport({_zone_id(race)})"]}
                rows.append({
                    "campaign_template": DUNGEON_TEMPLATE,
                    "conversation_guid": guid, "node_id": node,
                    "conversation_name": name, "priority": order,
                    "enabled": True, "trigger_json": trigger})
    return rows


def az0_rows(code: dict, problems: list[str]) -> list[dict]:
    names = {guid: c["name"] for guid, c in catalog.conversations().items()}
    by_name = {name: guid for guid, name in names.items()}
    seed_races = code["_CRAYBURN_CASTLE"]["races"]
    scenes = _training_scenes(code)
    rows = []
    for race_id, config in sorted(code["_AZ0_RACE_CONFIG"].items()):
        race = RACE_IDS[race_id]
        guids = {key: str(config.get(key) or "").lower() for key, *_ in AZ0_ROLES}
        guids["_report"] = str((seed_races.get(race) or {}).get("quest_end") or "").lower()
        # The "choice_stay" follow-up has no config key; Records names it.
        guids["transition_repeat"] = by_name.get(
            names.get(guids["transition_conv"], "") + " - Repeat", "")
        role_names = {key: names.get(guid, key) for key, guid in guids.items()}
        for order, (key, npc_key, label, outcome) in enumerate(AZ0_ROLES):
            guid = guids.get(key)
            if not guid:
                problems.append(f"AZ0 {race} {key}: no conversation")
                continue
            if guid not in names:
                problems.append(f"AZ0 {race} {key}: {guid} is not in Records")
            tags = ([outcome] if outcome else []) + [f"ChampionIsRace({race})"] + \
                az0_flow_tags(key, role_names)
            trigger = {"label": label, "source": "campaign.py _AZ0_RACE_CONFIG",
                       "tags": tags}
            actions = dict(AZ0_ACTIONS.get(key) or {})
            scene = scenes.get(race)
            if key == "battle_conv" and scene:      # RaceTutorialBattleUnlocked
                actions["choice_battle_yes"] = [f"encounter({scene})"]
            if key == "training_fail_conv" and scene:
                actions["retry_battle"] = [f"encounter({scene})"]
            if actions:
                trigger["actions"] = actions
            rows.append({
                "campaign_template": "AZ0", "conversation_guid": guid,
                "node_id": config[npc_key],
                "conversation_name": names.get(guid, f"AZ0 {race} {label}"),
                "priority": order, "enabled": True, "trigger_json": trigger})
        if (seed_races.get(race) or {}).get("quest_start", "").lower() != \
                str(config.get("quest_conv", "")).lower():
            problems.append(f"AZ0 {race}: quest_conv differs from the "
                            "Crayburn seed's quest_start")
    return rows


def _training_scenes(code: dict) -> dict[str, str]:
    return {RACE_IDS[race_id]: str(config.get("training_encounter") or "").lower()
            for race_id, config in code["_AZ0_RACE_CONFIG"].items()}


def az0_rules(code: dict, rows: list[dict]) -> dict[str, dict]:
    """Per race: each panorama NPC's states, as campaign.py drives them.

    The client (UIPanoramaZoneViewModel.ProcessStateChange) shows an NPC
    with a visible, not completed location, and draws "!" for ``givequest``
    and "?" for ``turninquest``. campaign.py starts on the intro NPC
    (``ALoc``: it auto-plays), completes it after the intro; adds the
    trainer with a ``givequest`` sparring offer, completes it after the
    training success; adds the quest NPC with the ``givequest`` Crayburn
    quest (completed when leaving for the castle); after Crayburn shows the
    ``turninquest`` report there, then the intro NPC's ``givequest``
    transition (_activate_az1_transition).
    """
    engine = catalog._rules_module()
    names = {(r["node_id"], r["trigger_json"]["label"]): r["conversation_name"]
             for r in rows}
    result = {}
    for race_id, config in sorted(code["_AZ0_RACE_CONFIG"].items()):
        race = RACE_IDS[race_id]
        intro, trainer, quest = (config["intro_npc"], config["trainer_npc"],
                                 config["quest_npc"])

        def done(npc, label, ending=""):
            return "conversation({}{})".format(
                names[(npc, label)], f",{ending}" if ending else "")
        intro_done = done(intro, "Intro")
        success_done = done(trainer, "Training success")
        quest_done = done(quest, "Crayburn quest start")
        report_done = done(quest, "Crayburn report")
        back = "flag(crayburn_complete)"
        result[_zone_id(race)] = {
            "schema_version": engine.SCHEMA_VERSION, "campaign": _zone_id(race),
            "scene": f"AZ0 {race}",
            "generated_by": "tools/campaign_editor/import_crayburn.py",
            "start": {"node": intro, "node_completed": False, "flags": []},
            "map": {"paths": []},
            "quests": {},
            "nodes": {
                intro: {
                    "visible": [],
                    "autostart": [f"!{intro_done}"],
                    "completed": [intro_done, f"!{report_done}"],
                    "givequest": [report_done],
                },
                trainer: {
                    "visible": [intro_done],
                    "completed": [success_done],
                    "givequest": [f"!{success_done}"],
                    # The result conversations play on returning from battle.
                    "autostart": [["outcome:success", f"!{success_done}"],
                                  ["outcome:fail"]],
                    "retryable": ["outcome:fail"],
                },
                quest: {
                    "visible": [success_done],
                    "completed": [[quest_done, f"!{back}"], [report_done]],
                    "givequest": [f"!{back}"],
                    "turninquest": [back, f"!{report_done}"],
                },
            },
            "hooks": [],
        }
    return result


def crayburn_rules(code: dict) -> dict:
    engine = catalog._rules_module()
    layout = json.loads((catalog.GENERATED / "crayburn" / "map.json")
                        .read_text(encoding="utf-8"))
    on_map = {node["id"] for node in layout["nodes"]}
    chain = [n for n in code["_CASTLE_CHAIN"] if n in on_map]
    nodes = {}
    # _reveal_crayburn_node: nothing past the first unfinished castle node.
    for previous, node in zip(chain, chain[1:]):
        nodes[node] = {"blocked": [f"!completed({previous})"]}
    return {
        "schema_version": engine.SCHEMA_VERSION, "campaign": "crayburn",
        "scene": "Castle Crayburn",
        "generated_by": "tools/campaign_editor/import_crayburn.py",
        # The Entrance is a pass-through that completes on entry.
        "start": {"node": chain[0], "node_completed": False, "flags": []},
        "map": {"paths": [{"id": p["id"], "from": p["from"], "to": p["to"]}
                          for p in layout["paths"]]},
        "quests": {},
        "nodes": nodes,
        "hooks": [{"id": "crayburn_complete",
                   "on": {"event": "encounter_victory", "node": chain[-1]},
                   "do": [{"set_flag": "crayburn_complete"}]}],
    }


def _data_file(template: str, description: str, rows: list[dict]) -> dict:
    return {"schema_version": 1, "description": description,
            "tables": {"campaign_node_conversations": rows}}


def main():
    sys.path.insert(0, str(catalog.REPO))
    import campaign_data
    code = code_constants()
    problems: list[str] = []
    files = {
        catalog.DATA_DIR / "crayburn.json": _data_file(
            DUNGEON_TEMPLATE, "Castle Crayburn per-race conversations "
            "(generated by tools/campaign_editor/import_crayburn.py)",
            crayburn_rows(code, problems)),
        catalog.DATA_DIR / "AZ0.json": _data_file(
            "AZ0", "AZ0 starter panorama conversations per race "
            "(generated by tools/campaign_editor/import_crayburn.py)",
            az0_rows(code, problems)),
    }
    for path, data in files.items():
        _keep_editor_edits(path, data["tables"]["campaign_node_conversations"])
        bad = campaign_data.validate(data)
        if bad:
            raise SystemExit(f"{path.name}: {bad[:3]}")
        path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        print(f"{path}: {len(data['tables']['campaign_node_conversations'])} rows")
    for zone_id, zone_rules in az0_rules(
            code, files[catalog.DATA_DIR / "AZ0.json"]["tables"][
                "campaign_node_conversations"]).items():
        write_rules(zone_id, zone_rules)
    write_rules("crayburn", crayburn_rules(code))
    for problem in problems:
        print(f"  check: {problem}")
    if not problems:
        print("  all rows match campaign.py's seed")


def write_rules(zone_id: str, rules: dict) -> None:
    path = catalog.rules_path(zone_id)
    engine = catalog._rules_module()
    if path.exists():   # node states saved in the editor win over the draft
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
    errors = [p for p in engine.validate(rules) if p["level"] == "error"]
    if errors:
        raise SystemExit(f"{zone_id} rules: {errors[:3]}")
    path.write_text(json.dumps(rules, indent=1) + "\n", encoding="utf-8")
    print(f"{path}: {len(rules['nodes'])} gated nodes")


if __name__ == "__main__":
    main()
