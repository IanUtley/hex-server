"""Regression coverage for the observed AZ1 campaign state machine.

These tests clone the local metadata database so campaign-state transitions can
be exercised without changing the developer's live character progress.
"""

import json
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import campaign


SRC = os.environ.get(
    "HEX_TEST_SOURCE_DB",
    os.path.join(os.path.dirname(__file__), "..", "hconnect.db"),
)
SAVAGE_LORD = "ab77df1e-5f13-471b-80e7-b7b4824ca280"
CORRUPT_DRYAD = "879317e1-8b04-486e-a10a-f2d2f1a080bc"
GNASH_BRIDGES = "3cf073b0-47fd-4911-953a-d86902890459"
HOWLING_PLAINS_PACK = "7b8390fd-7d3d-44d4-b285-1aeae3aef98b"
RAVENOUS_PIRANHA = "9ed0730b-e469-4c3d-ac7f-7b56c64b42ae"


def cloned_db():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    db = sqlite3.connect(path)
    source = sqlite3.connect(SRC)
    try:
        source.backup(db)
    finally:
        source.close()
    return db, path


def area_state(db, champion_id=6):
    raw = db.execute(
        "SELECT id, state_json FROM campaigns "
        "WHERE champion_id=? AND campaign_type='AREA' "
        "ORDER BY id DESC LIMIT 1", (champion_id,)).fetchone()
    assert raw, "the metadata database needs an AZ1 area campaign fixture"
    return raw[0], json.loads(raw[1])


def test_cross_zila_objective_is_linked_to_savage_lord():
    db, path = cloned_db()
    try:
        raw = db.execute(
            "SELECT objectives_json FROM quest_templates "
            "WHERE script_name='az01_q_cross_the_river'"
        ).fetchone()[0]
        objective = next(
            item for item in json.loads(raw) if item.get("id") == "Step1")
        assert objective["type"] == "Encounter"
        assert objective["encounter"] == SAVAGE_LORD
    finally:
        db.close()
        os.unlink(path)


def test_node00r_uses_authored_node_r_conversation_fallback():
    db, path = cloned_db()
    try:
        rows = campaign._az1_node_conversation_rows(db, "NodeR")
        expected = next(
            guid for guid, _trigger, _priority, name in rows
            if "Step 1" in name
        )
        state = {"PublicState": {"Data": {"conversation_visits": {}}}}
        assert campaign._az1_node_conversation(
            db, "Node00R", state, champ_id=6) == expected
    finally:
        db.close()
        os.unlink(path)


def test_cross_zila_and_zodiac_gates_follow_quest_progress():
    db, path = cloned_db()
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name IN (?, ?)",
            (6, "az01_q_cross_the_river", "az01_q_cross_the_river_part2"),
        )
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_cross_the_river")
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_cross_the_river_part2")

        area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node012"],
            "visited_paths": [],
            "blocked_nodes": [],
            "unlocked_nodes": [],
            "quest_nodes": [],
            "quest_reveal_nodes": [],
            "quest_hidden_nodes": [],
            "blockade_cleared": False,
        })
        state["LastNode"] = "Node012"
        state["ALoc"] = "Bridge over the Zila - East"

        # A fresh Tamed quest keeps both later branches hidden.
        tamed = campaign._quest_hook_az1_tamed_start(db, 6, state)
        assert tamed
        for node in ("Node015", "Node018"):
            location = next(x["Data"] for x in state["VisLocs"]
                            if x["Data"].get("node") == node)
            assert location["visible"] is False
            assert location["enabled"] is False

        # Starting Cross the Zila River reveals and marks Razortooth Forest.
        assert campaign._quest_hook_az1_cross_the_river_start(db, 6, state)
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        razortooth = next(x["Data"] for x in state["VisLocs"]
                          if x["Data"].get("node") == "Node014")
        assert razortooth["visible"] is True
        assert "Node014" in pdata["quest_nodes"]

        # Cross the Zodiac is initially allowed to show Shadowgrove, but its
        # west bridge remains gated until Wallace's turn-in advances Step 2.
        db.execute(
            "UPDATE campaigns SET state_json=? WHERE id=?",
            (json.dumps(state), area_id),
        )
        db.commit()
        state["LastNode"] = "Node030"
        state["ALoc"] = "Bridge over the Zodiac - East"
        pdata["visited_nodes"] = ["Node030"]
        assert campaign._quest_hook_az1_cross_zodiac_start(db, 6, state)
        shadowgrove = next(x["Data"] for x in state["VisLocs"]
                           if x["Data"].get("node") == "Node018")
        west_bridge = next(x["Data"] for x in state["VisLocs"]
                           if x["Data"].get("node") == "Node038")
        assert shadowgrove["visible"] is True
        assert west_bridge["visible"] is False

        quest_id, raw = campaign._quest_state_row(
            db, 6, "az01_q_cross_the_river_part2")
        quest_state = json.loads(json.dumps(raw))
        quest_state["Flags"]["_quest_objective_idx"] = 2
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(quest_state), quest_id))
        db.commit()
        campaign._quest_hook_az1_cross_zodiac_start(db, 6, state)
        assert west_bridge["visible"] is True
        assert west_bridge["enabled"] is True

        # The opening Brink Ridge conversation only explains the blockade;
        # its northbound destinations remain hidden until the encounter wins.
        state["LastNode"] = "Node019"
        state["ALoc"] = "Brink Ridge"
        pdata["visited_nodes"] = ["Node019"]
        pdata["blockade_cleared"] = False
        campaign._az1_reveal_neighbors(db, state, "Node019")
        northbound = {
            node for node in campaign._az1_neighbors(db, "Node019")
            if node != "Node016"
        }
        for location in state["VisLocs"]:
            data = location["Data"]
            if data.get("node") in northbound:
                assert data["visible"] is False
    finally:
        db.close()
        os.unlink(path)


def test_savage_lord_advances_cross_zila_and_node_rewards_are_authored():
    db, path = cloned_db()
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name='az01_q_cross_the_river'", (6,))
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_cross_the_river")
        area_id, state = area_state(db)
        state["ActiveEncounterGuid"] = SAVAGE_LORD
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        advanced = campaign._advance_quest_encounter_objectives(
            db, 6, SAVAGE_LORD)
        assert len(advanced) == 1
        _quest_id, progressed = campaign._quest_state_row(
            db, 6, "az01_q_cross_the_river")
        assert progressed["Flags"]["_quest_objective_idx"] == 1
        assert progressed["VisLocs"][-1]["Data"]["node"] == "Step2"

        savage_rewards = json.loads(db.execute(
            "SELECT rewards_json FROM encounter_scenes WHERE guid=?",
            (SAVAGE_LORD,)).fetchone()[0])
        assert savage_rewards["gold"] == 300
        assert savage_rewards["xp"] == 200

        gnash_rewards = json.loads(db.execute(
            "SELECT rewards_json FROM encounter_scenes WHERE guid=?",
            (GNASH_BRIDGES,)).fetchone()[0])
        assert any(
            item.get("card_guid") == RAVENOUS_PIRANHA
            for item in gnash_rewards["end_of_game_rewards"]
        )
        dryad_rewards = json.loads(db.execute(
            "SELECT rewards_json FROM encounter_scenes WHERE guid=?",
            (CORRUPT_DRYAD,)).fetchone()[0])
        assert any(
            item.get("chest_guid") == HOWLING_PLAINS_PACK
            for item in dryad_rewards["end_of_game_rewards"]
        )
        assert any(
            item.get("xp") == 250 and item.get("gold") is None
            for item in dryad_rewards["end_of_game_rewards"]
        )
        assert any(
            item.get("item_guid_by_race", {}).get("Elf") ==
            "63e87fdf-6650-4861-90e5-7c8c840ec292"
            for item in dryad_rewards["end_of_game_rewards"]
        )
        conversation_rewards = dict(db.execute(
            "SELECT conversation_guid, reward_json FROM conversation_rewards "
            "WHERE conversation_guid IN (?, ?, ?)",
            ("5e5ec1cd-c869-43e8-82d4-417021954440",
             "08b9b8ab-2100-4f8d-87b0-18369eb4ecb4",
             "4000c850-c1f7-45e4-b254-3cb1b0e9bf2b"),
        ).fetchall())
        assert json.loads(
            conversation_rewards["5e5ec1cd-c869-43e8-82d4-417021954440"]
        )["chest_guid"] == HOWLING_PLAINS_PACK
        assert json.loads(
            conversation_rewards["08b9b8ab-2100-4f8d-87b0-18369eb4ecb4"]
        )["chest_guid"] == HOWLING_PLAINS_PACK
        assert json.loads(
            conversation_rewards["4000c850-c1f7-45e4-b254-3cb1b0e9bf2b"]
        ) == {}
    finally:
        db.close()
        os.unlink(path)


def test_corrupt_dryad_encounter_grants_pack_and_faction_equipment():
    db, path = cloned_db()
    try:
        area_id, state = area_state(db)
        state["ActiveEncounterGuid"] = CORRUPT_DRYAD
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        class Handler:
            @staticmethod
            def _log_req(_message):
                pass

        result = campaign._apply_encounter_end_rewards(
            Handler(), db, SimpleNamespace(session_name="test", session_id=999),
            area_id, True)
        assert result["gold"] == 0
        assert result["xp"] == 250
        assert result["chests"]
        assert result["items"][0]["template"] == (
            "cd13fdb5-6d6f-4d8e-a413-589176b44935")  # Coyotle
        user_id = db.execute(
            "SELECT user_id FROM champions WHERE id=6").fetchone()[0]
        assert db.execute(
            "SELECT quantity FROM player_inventory "
            "WHERE user_id=? AND template_guid=?",
            (user_id, result["items"][0]["template"]),
        ).fetchone()[0] == 1
    finally:
        db.close()
        os.unlink(path)


def test_az1_scene_lookup_keeps_authored_node_variants_distinct():
    db, path = cloned_db()
    try:
        expected = {
            "Node018": "CORRUPT DRYAD",
            "Node06B": "SEA WITCH - NO FROG",
            "Node19B1": "ZOMBIE BLOCKADE",
            "Node23A_2": "HUMANS NO HELP",
            "Node75A": "DWARVEN DIGGERS",
        }
        for node, suffix in expected.items():
            scene = campaign._az1_scene_for_node(db, node)
            assert scene and suffix in scene[1], (node, scene)
    finally:
        db.close()
        os.unlink(path)


def test_az1_objectives_use_faction_specific_conversations():
    db, path = cloned_db()
    try:
        expected = {
            6: {
                "q_army_of_myth": "91f7eabb-eb17-4771-91b0-a5de3bee0126",
                "q_smoldering_dead": "0b30f528-ad70-480c-8ac4-d10a78b29fbb",
                "q_tranquil_dream": "c11fc1a7-cb27-4420-88dd-5c789c77b563",
                "q_devonshire_keep": "08677dda-e01e-4492-96a5-a36f4f4b6ca7",
            },
            4: {
                "q_army_of_myth": "0050adc1-bef2-45c9-9131-e80fcc80fd1a",
                "q_smoldering_dead": "78fc05bc-6503-4644-a84f-3109b7727ae7",
                "q_tranquil_dream": "0ec4b919-e18c-424a-96b0-310e3452aab3",
                "q_devonshire_keep": "abb502ef-fee9-4e44-b11a-d56d5fae6680",
            },
        }
        for champ_id, scripts in expected.items():
            for script, conversation in scripts.items():
                raw = db.execute(
                    "SELECT objectives_json FROM quest_templates "
                    "WHERE script_name=?", (script,)).fetchone()[0]
                objectives = campaign._materialize_quest_objectives(
                    db, champ_id, script, json.loads(raw))
                final = objectives[-1]
                assert final["conversation"] == conversation
                assert conversation in final["conversation_ids"]

        # The same persisted quest transition must accept the Underworld
        # completion conversation and reject the Ardent-only variant.
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name='q_army_of_myth'", (4,))
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 4, "AREA", "q_army_of_myth")
        quest_id, state = campaign._quest_state_row(db, 4, "q_army_of_myth")
        state["Flags"]["_quest_objective_idx"] = 1
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), quest_id))
        db.commit()
        assert campaign._advance_quest_conversation_objectives(
            db, 4, "0050adc1-bef2-45c9-9131-e80fcc80fd1a")
        _quest_id, state = campaign._quest_state_row(db, 4, "q_army_of_myth")
        assert state["Finished"]
    finally:
        db.close()
        os.unlink(path)


def test_az1_opening_uses_the_underworld_route_and_fog_gates():
    db, path = cloned_db()
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type='AREA' OR template_name IN (?, ?))",
            (4, "az01_tamed", "az01_uw_find_cave_in"),
        )
        db.commit()
        _area_id, state = campaign._activate_az1_area(db, 4)
        spawned, _hooks = campaign._grant_quests_for_conversation(
            db, 4, "AZ1", "40def058-ae19-4fe5-ade8-4cc928d5b226")
        assert {script for _qid, script, _state in spawned} >= {
            "az01_tamed", "az01_uw_find_cave_in"
        }
        state = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE champion_id=? "
            "AND campaign_type='AREA' ORDER BY id DESC LIMIT 1", (4,)
        ).fetchone()[0])
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        assert locations["Node017"]["visible"] is True
        assert locations["Node017"]["enabled"] is True
        assert locations["Node034"]["visible"] is False
        assert locations["Node034"]["enabled"] is False
        assert locations["Node025"]["visible"] is False
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    tests = [
        test_cross_zila_objective_is_linked_to_savage_lord,
        test_node00r_uses_authored_node_r_conversation_fallback,
        test_cross_zila_and_zodiac_gates_follow_quest_progress,
        test_savage_lord_advances_cross_zila_and_node_rewards_are_authored,
        test_corrupt_dryad_encounter_grants_pack_and_faction_equipment,
        test_az1_scene_lookup_keeps_authored_node_variants_distinct,
        test_az1_objectives_use_faction_specific_conversations,
        test_az1_opening_uses_the_underworld_route_and_fog_gates,
    ]
    for test in tests:
        test()
        print("PASS", test.__name__)
