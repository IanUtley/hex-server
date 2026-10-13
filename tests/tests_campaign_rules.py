"""Focused checks for the declarative campaign rules engine."""

import copy
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CAMPAIGN_ROOT = os.path.join(ROOT, "hex-private", "campaign")
sys.path[:0] = [ROOT, CAMPAIGN_ROOT]

import campaign_rules as cr

SCENE = "11111111-1111-1111-1111-111111111111"
GIVER = "22222222-2222-2222-2222-222222222222"

RULES = {
    "schema_version": 1,
    "start": {"node": "A", "flags": []},
    "map": {"paths": [
        {"id": "Path_A_B", "from": "A", "to": "B"},
        {"id": "Path_B_C", "from": "B", "to": "C"},
        {"id": "Path_B_D", "from": "B", "to": "D"},
        {"id": "Path_C_E", "from": "C", "to": "E"},
    ]},
    "quests": {"tame": {"steps": [
        {"id": "Tame1", "done_when": {"event": "encounter_victory",
                                      "scene": SCENE, "events": "tamed_troop"}},
        {"id": "TurnIn", "done_when": {"event": "conversation_end",
                                       "conversation": GIVER}},
    ]}},
    "nodes": {
        "B": {"entries": [
            {"id": "turn-in", "kind": "conversation", "conversation": GIVER,
             "when": {"quest": "tame", "is": "active", "step_at_least": 1}},
            {"id": "give", "kind": "conversation", "conversation": GIVER,
             "when": {"quest": "tame", "is": "not_started"}},
        ]},
        # Offers ("entries") are merged from table data at load time.
        "C": {"completed": {"flag": "tamed_c"},
              "entries": [{"id": "battle", "kind": "encounter", "scene": SCENE}]},
        "D": {"blocked": {"quest": "tame", "is": "started"}},
        "E": {"visible": {"flag": "e_open"},
              "marker": {"quest": "tame", "is": "active"}},
    },
    "hooks": [
        {"id": "tame", "on": {"event": "encounter_victory", "scene": SCENE,
                              "events": "tamed_troop"},
         "do": [{"set_flag": "tamed_c"}]},
        {"id": "grant", "on": {"event": "conversation_end", "conversation": GIVER},
         "when": {"quest": "tame", "is": "not_started"},
         "do": [{"start_quest": "tame"}]},
        {"id": "open-e", "on": {"event": "quest_complete", "quest": "tame"},
         "do": [{"set_flag": "e_open"}]},
    ],
}


def _visible(state):
    return {n for n, v in cr.layout(RULES, state)["nodes"].items() if v["visible"]}


def test_conditional_encounter_stays_retryable_until_its_event():
    """Winning without taming leaves the node replayable; taming sets the flag
    its ``completed`` gate reads and advances the quest (the Tamed contract)."""
    state = cr.initial_state(RULES)
    state, _ = cr.apply(RULES, state, {"type": "visit", "node": "B"})
    state, _ = cr.apply(RULES, state, {"type": "conversation_end", "node": "B",
                                       "conversation": GIVER})
    assert state["quests"]["tame"] == {"status": "active", "step": 0}
    state, _ = cr.apply(RULES, state, {"type": "visit", "node": "C"})
    won, _ = cr.apply(RULES, state, {"type": "encounter_result", "node": "C",
                                     "victory": True, "events": []})
    won_c = cr.layout(RULES, won)["nodes"]["C"]
    assert won_c["retryable"] and not won_c["completed"]
    assert won["quests"]["tame"]["step"] == 0
    tamed, _ = cr.apply(RULES, state, {"type": "encounter_result", "node": "C",
                                       "victory": True, "events": ["tamed_troop"]})
    tamed_c = cr.layout(RULES, tamed)["nodes"]["C"]
    assert tamed_c["completed"] and not tamed_c["retryable"]
    assert tamed["quests"]["tame"] == {"status": "active", "step": 1}
    lost, _ = cr.apply(RULES, state, {"type": "encounter_result", "node": "C",
                                      "victory": False})
    assert lost["nodes"]["C"]["failed"] and not lost["nodes"]["C"]["retryable"]
    assert not cr.layout(RULES, lost)["nodes"]["C"]["completed"]
    relost, _ = cr.apply(RULES, won, {"type": "encounter_result", "node": "C",
                                      "victory": False})
    assert relost["nodes"]["C"]["failed"] and relost["nodes"]["C"]["retryable"]


def test_same_node_offers_state_dependent_content_and_gates_follow_quests():
    state = cr.initial_state(RULES)
    assert _visible(state) == {"A", "B"}
    state, _ = cr.apply(RULES, state, {"type": "visit", "node": "B"})
    assert cr.active_entry(RULES, state, "B")["id"] == "give"
    # Arrival at an unresolved conversation does not open its paths.
    assert "C" not in _visible(state)
    state, _ = cr.apply(RULES, state, {"type": "conversation_end", "node": "B",
                                       "conversation": GIVER})
    assert _visible(state) == {"A", "B", "C"}          # D is quest-blocked
    assert cr.layout(RULES, state)["nodes"]["E"]["marker"]
    state, _ = cr.apply(RULES, state, {"type": "visit", "node": "C"})
    state, _ = cr.apply(RULES, state, {"type": "encounter_result", "node": "C",
                                       "victory": True, "events": ["tamed_troop"]})
    assert cr.active_entry(RULES, state, "B")["id"] == "turn-in"
    state, log = cr.apply(RULES, state, {"type": "conversation_end", "node": "B",
                                         "conversation": GIVER})
    # Turn-in finishes the quest; quest_complete cascades into a flag that
    # reveals E even though it is not adjacent to a visited node.
    assert state["quests"]["tame"]["status"] == "finished"
    assert "e_open" in state["flags"] and "E" in _visible(state)
    assert {"hook": "open-e"} in log


def test_apply_is_pure_and_cascades_are_bounded():
    state = cr.initial_state(RULES)
    before = copy.deepcopy(state)
    cr.apply(RULES, state, {"type": "visit", "node": "B"})
    assert state == before
    looping = dict(RULES, hooks=[
        {"on": {"event": "script_event", "name": "loop"},
         "do": [{"set_flag": "x"}]},
        {"on": {"event": "quest_start", "quest": "tame"},
         "do": [{"complete_quest": "tame"}]},
        {"on": {"event": "quest_complete", "quest": "tame"},
         "do": [{"set_flag": "done"}]},
    ])
    state, _ = cr.apply(looping, cr.initial_state(looping),
                        {"type": "set_quest", "quest": "tame", "status": "active"})
    assert state["quests"]["tame"]["status"] == "finished"
    assert "done" in state["flags"]


def test_validate_reports_unknown_conditions_and_dangling_flags():
    broken = copy.deepcopy(RULES)
    broken["nodes"]["E"]["visible"] = {"flag": "never_set"}
    broken["nodes"]["B"]["entries"].append({"id": "x", "kind": "bogus",
                                            "when": {"weather": "rain"}})
    broken["nodes"]["D"]["reveal"] = {"flag": "x"}   # retired key name
    levels = {(p["level"], p["text"]) for p in cr.validate(broken)}
    assert any(level == "error" and "unknown node key 'reveal'" in text
               for level, text in levels)
    assert ("warn", "flag 'never_set' is read but never set") in levels
    assert any(level == "error" and "unknown entry kind" in text
               for level, text in levels)
    assert any(level == "error" and "unknown condition" in text
               for level, text in levels)


def test_generated_az1_rules_reproduce_the_opening():
    """The importer's AZ1 draft: Belarius grants Tamed and the Ardent route."""
    path = os.path.join(CAMPAIGN_ROOT, "campaign_rules", "AZ1.json")
    if not os.path.exists(path):
        print("SKIP generated AZ1 rules not present")
        return
    rules = cr.load_rules(path)
    state = cr.initial_state(rules, faction="Ardent", race="Elf")
    # Offers and their ending actions come from table data (quest_conversations
    # rows: start role -> start_quest); merge Belarius's the way the editor does.
    belarius = "4b4c73b2-a28a-471e-91e3-8c10bf7fb799"
    rules = copy.deepcopy(rules)
    rules["nodes"].setdefault("Node002", {})["entries"] = [{
        "id": "belarius", "kind": "conversation", "conversation": belarius,
        "actions": {"*": ["start_quest(az01_tamed)",
                          "start_quest(az01_ar_find_ambling_mesa)"]}}]
    state, _ = cr.apply(rules, state, {"type": "visit", "node": "Node002"})
    state, _ = cr.apply(rules, state, {"type": "conversation_end", "node": "Node002",
                                       "conversation": belarius})
    assert state["quests"]["az01_tamed"]["status"] == "active"
    nodes = cr.layout(rules, state)["nodes"]
    assert nodes["Node034"]["visible"] and nodes["Node034"]["marker"]
    assert nodes["Node005"]["blocked"] and not nodes["Node017"]["visible"]


def test_conversation_end_runs_every_matching_entry_and_sets_flags():
    """Entries sharing a conversation all run (several quests from one
    conversation); ``set_flag(X)`` ending actions set ``flag(X)``."""
    rules = copy.deepcopy(RULES)
    rules["quests"]["other"] = {"steps": [{"id": "S1"}]}
    rules["nodes"]["B"]["entries"] = [
        {"id": "a", "kind": "conversation", "conversation": GIVER,
         "when": ["!questStarted(tame)"], "actions": {"*": ["start_quest(tame)"]}},
        {"id": "b", "kind": "conversation", "conversation": GIVER,
         "when": ["!questStarted(other)"],
         "actions": {"*": ["startQuest(other)"], "go": ["set_flag(gate_open)"]}},
        {"id": "c", "kind": "conversation", "conversation": GIVER,
         "when": ["faction(Underworld)"], "actions": {"*": ["set_flag(wrong)"]}},
    ]
    state = cr.initial_state(rules, faction="Ardent")
    state, _ = cr.apply(rules, state, {"type": "visit", "node": "B"})
    state, _ = cr.apply(rules, state, {"type": "conversation_end", "node": "B",
                                       "conversation": GIVER, "ending": "go"})
    assert state["quests"]["tame"]["status"] == "active"
    assert state["quests"]["other"]["status"] == "active"
    assert "gate_open" in state["flags"] and "wrong" not in state["flags"]
    assert cr.parse_action("setFlag(x)") == {"set_flag": "x"}


def test_impassable_node_is_red_until_completed_and_holds_the_road():
    """``impassable`` (the client's red, token-stopping state) follows its
    tags, never shows on a completed node, and keeps the node's paths shut."""
    rules = copy.deepcopy(RULES)
    rules["nodes"]["C"]["impassable"] = ["visited(C)", "!flag(cleared)"]
    state = cr.initial_state(rules)
    for node in ("B", "C"):
        state, _ = cr.apply(rules, state, {"type": "visit", "node": node})
    c = cr.layout(rules, state)["nodes"]
    assert c["C"]["impassable"] and not c["E"]["visible"]
    state["flags"].append("cleared")
    assert not cr.layout(rules, state)["nodes"]["C"]["impassable"]


def test_campaign_table_data_upserts_idempotently_and_skips_bad_files():
    """Editor table data: upsert by key (encounter scenes are inserted - the editor owns them), no-op on
    re-apply, and an invalid file is skipped whole."""
    import json
    import sqlite3
    import tempfile
    from pathlib import Path
    import campaign_data
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE encounter_scenes (guid TEXT, name TEXT, title TEXT, "
               "ai_deck_guid TEXT, ai_champion_guid TEXT, mods_json TEXT, "
               "rewards_json TEXT, ai_deck_personality TEXT)")
    db.execute("CREATE TABLE quest_conversations (quest_script TEXT, "
               "conversation_guid TEXT, campaign_template TEXT, node_id TEXT, "
               "npc TEXT, role TEXT, faction TEXT, conversation_name TEXT, "
               "start_hook TEXT, conditions_json TEXT, priority INTEGER, "
               "enabled INTEGER)")
    db.execute("INSERT INTO encounter_scenes (guid, rewards_json) VALUES ('s1', '{}')")
    good = {"schema_version": 1, "tables": {
        "encounter_scenes": [{"guid": "s1", "rewards_json": {"gold": 50}},
                             {"guid": "unseeded", "rewards_json": {}}],
        "quest_conversations": [{"quest_script": "q", "conversation_guid": "c",
                                 "campaign_template": "AZ1", "node_id": "Node00R",
                                 "role": "start", "enabled": True}]}}
    bad = {"schema_version": 1, "tables": {
        "encounter_scenes": [{"guid": "s1", "bogus": "renamed"}]}}
    with tempfile.TemporaryDirectory() as folder:
        Path(folder, "a.json").write_text(json.dumps(good))
        Path(folder, "b.json").write_text(json.dumps(bad))
        first = campaign_data.apply_all(db, Path(folder))
        second = campaign_data.apply_all(db, Path(folder))
    assert first["a.json"] == {"encounter_scenes": 2, "quest_conversations": 1}
    assert second["a.json"] == {"encounter_scenes": 0, "quest_conversations": 0}
    assert "b.json" not in first                      # an unknown column skips the file
    assert json.loads(db.execute("SELECT rewards_json FROM encounter_scenes "
                                 "WHERE guid='s1'").fetchone()[0]) == {"gold": 50}
    assert db.execute("SELECT COUNT(*) FROM encounter_scenes").fetchone()[0] == 2   # editor-owned: rows are inserted
    assert db.execute("SELECT node_id, enabled FROM quest_conversations"
                      ).fetchone() == ("Node00R", 1)


def test_tags_select_states_and_conversations_and_endings_set_flags():
    """Editor tags: node states and offers are tag lists (all must hold);
    ``outcome`` reads the node's last battle; a finished conversation sets
    ``conversation(Name)`` and ``conversation(Name,Ending)``."""
    rules = copy.deepcopy(RULES)
    rules["nodes"]["D"] = {"blocked": ["!completedQuest(tame,1)", "faction(Ardent)"]}
    rules["nodes"]["C"]["entries"].insert(0, {
        "id": "after-win", "kind": "conversation", "conversation": GIVER,
        "when": ["outcome:success", "!ChampionIsRace(Elf)",
                 "conversation(Giver,choice_yes)"]})
    assert cr.validate(rules) == [p for p in cr.validate(rules) if p["level"] != "error"]
    assert cr.parse_tag("not faction(Ardent)") == {"not": {"faction": "Ardent"}}
    assert cr.condition_tags(cr.tags_condition(
        ["questActive(tame)", "!completedQuest(tame,2)"])) == \
        ["questActive(tame)", "!completedQuest(tame,2)"]
    state = cr.initial_state(rules, faction="Ardent", race="Human")
    assert cr.layout(rules, state)["nodes"]["D"]["blocked"]
    state, _ = cr.apply(rules, state, {"type": "visit", "node": "B"})
    state, _ = cr.apply(rules, state, {"type": "conversation_end", "node": "B",
                                       "conversation": GIVER, "name": "Giver",
                                       "ending": "choice_yes"})
    assert {"conversation(Giver)", "conversation(Giver,choice_yes)"} <= set(state["flags"])
    state, _ = cr.apply(rules, state, {"type": "visit", "node": "C"})
    assert cr.active_entry(rules, state, "C")["id"] == "battle"
    lost, _ = cr.apply(rules, state, {"type": "encounter_result", "node": "C",
                                      "victory": False})
    assert cr.active_entry(rules, lost, "C")["id"] == "battle"
    won, _ = cr.apply(rules, state, {"type": "encounter_result", "node": "C",
                                     "victory": True, "events": ["tamed_troop"]})
    assert cr.active_entry(rules, won, "C")["id"] == "after-win"
    assert not cr.layout(rules, won)["nodes"]["D"]["blocked"]   # step 1 done
    elf = dict(won, race="Elf")
    assert cr.active_entry(rules, elf, "C")["id"] == "battle"
    # Ending actions: only the chosen ending's (plus "*") run.
    rules["nodes"]["C"]["entries"][0]["actions"] = {
        "choice_go": ["teleport(AZ1,Node001)"], "choice_stay": []}
    stay, _ = cr.apply(rules, won, {"type": "conversation_end", "node": "C",
                                    "conversation": GIVER, "ending": "choice_stay"})
    go, log = cr.apply(rules, won, {"type": "conversation_end", "node": "C",
                                    "conversation": GIVER, "ending": "choice_go"})
    assert "teleport" not in stay
    assert go["teleport"] == {"zone": "AZ1", "node": "Node001"}
    assert {"action": "teleport(AZ1,Node001)"} in log
    # encounter(scene): a conversation starts a battle at its node; the
    # result records the outcome there for outcome: tags.
    rules["nodes"]["B"]["entries"].insert(0, {
        "id": "spar", "kind": "conversation", "conversation": "spar",
        "actions": {"choice_battle_yes": [f"encounter({SCENE})"]}})
    asked, _ = cr.apply(rules, won, {"type": "conversation_end", "node": "B",
                                     "conversation": "spar", "ending": "choice_battle_yes"})
    assert asked["encounter"] == {"scene": SCENE, "node": "B"}
    lost, _ = cr.apply(rules, asked, {"type": "encounter_result", "node": "B",
                                      "victory": False})
    assert "encounter" not in lost and cr.evaluate(["outcome:fail"], lost, "B")
    # talkedAt: conversations finished at a node (first vs repeat visits).
    assert cr.evaluate(["!talkedAt(C)"], won) and not cr.evaluate(["talkedAt(C)"], won)
    talked, _ = cr.apply(rules, won, {"type": "conversation_end", "node": "C",
                                      "conversation": GIVER})
    assert cr.evaluate(["talkedAt(C)", "!talkedAt(C,2)"], talked)
    assert cr.condition_tags({"talked": ["C", 2]}) == ["talkedAt(C,2)"]
    try:
        cr.parse_tag("weather(rain)")
        raise AssertionError("unknown tag accepted")
    except cr.RulesError:
        pass


def test_editor_saves_tags_to_campaign_files():
    """Conversation tags land in campaign_data (dropped again when cleared);
    node-state tags land in the rules file and invalid tags are refused."""
    import json
    import sqlite3
    import tempfile
    from pathlib import Path
    sys.path.insert(0, os.path.join(ROOT, "campaign_editor"))
    import catalog
    import editing
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE campaign_node_conversations (campaign_template TEXT, "
               "conversation_guid TEXT, trigger_json TEXT)")
    db.execute("INSERT INTO campaign_node_conversations VALUES "
               "('AZ1', 'ABC', '{\"label\": \"Hag\"}')")
    saved = (catalog.DATA_DIR, catalog.RULES_DIR)
    with tempfile.TemporaryDirectory() as folder:
        catalog.DATA_DIR = Path(folder, "data")
        catalog.RULES_DIR = Path(folder, "rules")
        try:
            trigger = editing.save_conversation_tags(
                db, "AZ1", "abc", ["outcome:fail", "faction(Ardent)"])
            data = json.loads(Path(folder, "data", "AZ1.json").read_text())
            row, = data["tables"]["campaign_node_conversations"]
            assert row["conversation_guid"] == "ABC" and trigger == row["trigger_json"]
            assert trigger == {"label": "Hag", "tags": ["outcome:fail", "faction(Ardent)"]}
            editing.save_conversation_tags(db, "AZ1", "abc", [])
            data = json.loads(Path(folder, "data", "AZ1.json").read_text())
            assert data["tables"]["campaign_node_conversations"] == []
            catalog.RULES_DIR.mkdir()
            Path(catalog.RULES_DIR, "Z.json").write_text(json.dumps(RULES))
            editing.save_node_state("Z", "E", "hidden", ["completedQuest(tame)"])
            assert json.loads(Path(catalog.RULES_DIR, "Z.json").read_text()
                              )["nodes"]["E"]["hidden"] == ["completedQuest(tame)"]
            try:
                editing.save_node_state("Z", "E", "hidden", ["bogus(x)"])
                raise AssertionError("invalid tag saved")
            except ValueError:
                pass
            editing.save_node_state("Z", "E", "hidden", None)
            assert "hidden" not in json.loads(Path(catalog.RULES_DIR, "Z.json")
                                              .read_text())["nodes"]["E"]
        finally:
            catalog.DATA_DIR, catalog.RULES_DIR = saved


def test_or_groups_and_generated_crayburn_rules():
    """"Or" groups hold when any group holds and round-trip from ``any``
    conditions; the generated Crayburn chain offers each race its own
    content and keeps the next node blocked after a lost battle."""
    groups = [["questStarted(tame)", "!flag(done)"], ["faction(Underworld)"]]
    state = cr.initial_state(RULES, faction="Ardent")
    assert not cr.evaluate(groups, state)
    assert cr.evaluate(groups, dict(state, faction="Underworld"))
    condition = {"any": [{"all": [{"quest": "tame", "is": "started"},
                                  {"not": {"flag": "done"}}]},
                         {"faction": "Underworld"}]}
    assert cr.condition_tags(condition) == groups
    assert cr.condition_tags({"not": {"all": [{"flag": "a"}, {"flag": "b"}]}}) == \
        [["!flag(a)"], ["!flag(b)"]]
    assert cr.split_outcome([["outcome:fail", "faction(Ardent)"],
                             ["outcome:fail", "faction(Underworld)"]]) == \
        ("fail", [["faction(Ardent)"], ["faction(Underworld)"]])
    path = os.path.join(CAMPAIGN_ROOT, "campaign_rules", "crayburn.json")
    if not os.path.exists(path):
        print("SKIP generated Crayburn rules not present")
        return
    rules = copy.deepcopy(cr.load_rules(path))
    rules["nodes"]["CastleGate"]["entries"] = [
        {"id": race, "kind": "encounter", "scene": race, "when": [f"ChampionIsRace({race})"]}
        for race in ("Human", "Necrotic")]
    for node in ("WatchTower", "Drawbridge"):
        rules["nodes"].setdefault(node, {})["entries"] = [
            {"id": "talk", "kind": "conversation", "conversation": node}]
    state = cr.initial_state(rules, race="Necrotic")
    for node in ("WatchTower", "Drawbridge"):
        state, _ = cr.apply(rules, state, {"type": "visit", "node": node})
        state, _ = cr.apply(rules, state, {"type": "conversation_end", "node": node})
    state, _ = cr.apply(rules, state, {"type": "visit", "node": "CastleGate"})
    assert cr.active_entry(rules, state, "CastleGate")["id"] == "Necrotic"
    lost, _ = cr.apply(rules, state, {"type": "encounter_result", "node": "CastleGate",
                                      "victory": False})
    assert not cr.layout(rules, lost)["nodes"]["InnerBailey"]["visible"]
    won, _ = cr.apply(rules, lost, {"type": "encounter_result", "node": "CastleGate",
                                    "victory": True})
    assert cr.layout(rules, won)["nodes"]["InnerBailey"]["visible"]


def test_deck_evaluator_fills_blank_scene_personalities_without_overwriting():
    """An authored per-scene AI personality survives the startup evaluator; a
    blank scene sharing that deck gets the evaluator's pick (Default here)."""
    import sqlite3
    from AssetExtraction.evaluate_fra_deck_personalities import (
        update_fra_deck_personalities)
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE fra_encounters (deck_guid TEXT, deck_name TEXT, ai_deck_personality TEXT);
        CREATE TABLE fra_challengers (encounter_deck_guid TEXT, ai_deck_personality TEXT);
        CREATE TABLE encounter_deck_cards (deck_guid TEXT, card_guid TEXT, quantity INTEGER);
        CREATE TABLE encounter_scenes (guid TEXT, name TEXT, ai_deck_guid TEXT, ai_deck_personality TEXT);
        INSERT INTO encounter_scenes VALUES ('a', 'A', 'deck1', 'Burn'), ('b', 'B', 'deck1', NULL);
    """)
    update_fra_deck_personalities(db)
    got = dict(db.execute("SELECT guid, ai_deck_personality FROM encounter_scenes"))
    assert got["a"] == "Burn", got
    assert got["b"] and got["b"] != "Burn", got


def test_card_selector_offers_flagged_cards_and_clears_the_won_one():
    tower, walls = "a" * 8 + "-0000-0000-0000-" + "0" * 12, "b" * 8 + "-0000-0000-0000-" + "0" * 12
    mods = [{"title": "Plain", "mods": [{"guid": "c" * 8}]},
            {"title": "Defences", "choice": True, "mods": [
                {"guid": tower, "when": ["flag(tower)"], "victory": ["clear_flag(tower)"]},
                {"guid": walls, "when": ["flag(walls)"], "victory": ["clear_flag(walls)"]}]}]
    rules = copy.deepcopy(RULES)
    rules["start"]["flags"] = ["tower", "walls"]
    rules["mod_choices"] = {SCENE: cr.scene_mod_choices(mods)}
    state = cr.initial_state(rules)
    assert cr.layout(rules, state)["nodes"]["C"]["mod_choices"] == [tower, walls]
    try:
        cr.apply(rules, state, {"type": "encounter_result", "node": "C", "scene": SCENE,
                                "victory": True, "mod": "f" * 8})
        raise AssertionError("an option that was not offered was accepted")
    except cr.RulesError:
        pass
    state, _ = cr.apply(rules, state, {"type": "encounter_result", "node": "C",
                                       "scene": SCENE, "victory": True, "mod": tower})
    assert "tower" not in state["flags"] and "walls" in state["flags"], state["flags"]
    state["nodes"]["C"]["completed"] = False
    assert cr.layout(rules, state)["nodes"]["C"]["mod_choices"] == [walls]
    state["flags"] = []      # nothing left on offer: a normal encounter
    assert cr.layout(rules, state)["nodes"]["C"]["mod_choices"] == []
    # Battle setup keeps plain mods and applies only the chosen option.
    assert cr.chosen_scene_mods(mods, walls) == [
        mods[0], {"title": "Defences", "mods": [{"guid": walls}]}]
    assert cr.chosen_scene_mods(mods, None) == [mods[0]]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
