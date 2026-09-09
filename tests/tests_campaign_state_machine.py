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
from campaign_fixtures import seed_campaign_fixtures


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
    seed_campaign_fixtures(db)
    return db, path


def area_state(db, champion_id=6):
    raw = db.execute(
        "SELECT id, state_json FROM campaigns "
        "WHERE champion_id=? AND campaign_type='AREA' "
        "ORDER BY id DESC LIMIT 1", (champion_id,)).fetchone()
    assert raw, "the metadata database needs an AZ1 area campaign fixture"
    return raw[0], json.loads(raw[1])


def dungeon_fixture(db, champion_id=7):
    """Create a disposable Crayburn dungeon row and return its campaign ID."""
    camp_id = campaign._new_camp_id(db)
    state = campaign._build_initial_gameplay_state(
        camp_id, champion_id, "DUNGEON", "Shin'hare")
    db.execute(
        "INSERT INTO campaigns "
        "(id,camp_uid_lo,camp_uid_hi,champion_id,user_id,champion_name,"
        "template_name,campaign_type,is_started,state_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (camp_id, campaign._generate_inst_id(), 0, champion_id, 1,
         "test-shinhare", "Crayburn Castle", "DUNGEON", 1,
         json.dumps(state)),
    )
    db.commit()
    return camp_id


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


def test_node00r_faction_quest_branch_opens_lena_grotto_and_stays_retryable():
    db, path = cloned_db()
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name IN (?, ?)",
            (6, "az01_q_fort_romor", "az01_q_usurper"),
        )
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_usurper")
        _area_id, state = area_state(db)
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node00R"].update({
            "completed": True, "repeatable": False,
            "visible": True, "enabled": True,
        })
        locations["Node025"].update({
            "completed": False, "visible": False, "enabled": False,
        })
        state["LastNode"] = "Node00R"
        state["ALoc"] = "Vale of Oberon"
        pdata = state["PublicState"]["Data"]
        pdata["quest_hidden_nodes"] = ["Node025"]
        pdata["blocked_nodes"] = ["Node025"]

        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)

        assert campaign._az1_node_conversation(
            db, "Node00R", state, champ_id=6) == \
            "8433f72e-bd47-4978-bc02-360e34ddce57"
        assert locations["Node00R"]["completed"] is False
        assert locations["Node00R"]["repeatable"] is True
        assert locations["Node025"]["visible"] is True
        assert locations["Node025"]["enabled"] is True
        assert "Node025" not in pdata["blocked_nodes"]
    finally:
        db.close()
        os.unlink(path)


def test_node00r_finished_faction_quest_uses_terminal_conversation():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campspawn = campaign.push_campspawn
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name IN (?, ?)",
            (6, "az01_q_fort_romor", "az01_q_usurper"),
        )
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_usurper")
        quest_id, quest_state = campaign._quest_state_row(
            db, 6, "az01_q_usurper")
        quest_state["Finished"] = "2026-09-09T00:00:00Z"
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(quest_state), quest_id))
        db.commit()

        area_id, state = area_state(db)
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node00R"].update({
            "type": "Convo", "completed": True, "repeatable": False,
            "visible": True, "enabled": True,
        })
        state["LastNode"] = "Node00R"
        state["ALoc"] = "Vale of Oberon"
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        assert locations["Node00R"]["conversationId"] == \
            "617819c9-bc9b-4788-a469-8117af28a613"
        assert locations["Node00R"]["completed"] is False
        assert locations["Node00R"]["repeatable"] is True
        assert locations["Node00R"]["quest_variant_terminal"] is True
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campspawn = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        after = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        after_node = next(
            item["Data"] for item in after["VisLocs"]
            if item["Data"].get("node") == "Node00R"
        )
        assert after_node["completed"] is True
        assert not after_node.get("quest_variant_terminal")
    finally:
        campaign._send_response = original_send_response
        campaign.push_campspawn = original_push_campspawn
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
        campaign._az1_reveal_neighbors(db, state, "Node012")
        assert "Path_Fork001_Node012" not in pdata["locked_paths"]
        assert "Path_Fork001_Node013" not in pdata["locked_paths"]
        assert "Path_Fork001_Node014" not in pdata["locked_paths"]

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


def test_az1_unfinished_encounter_does_not_reveal_outgoing_paths():
    db, path = cloned_db()
    try:
        area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node001", "Node002", "Node003"],
            "visited_paths": ["Path_Node001_Node002", "Path_Node002_Node003"],
            "blocked_nodes": [],
            "quest_reveal_nodes": [],
            "quest_hidden_nodes": [],
            "failed_nodes": [],
        })
        state["LastNode"] = "Node003"
        state["ALoc"] = "Dunnwood"
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node002"].update({
            "completed": True, "autostart": False,
        })
        locations["Node003"].update({
            "type": "Encounter", "completed": False,
            "repeatable": False, "autostart": True,
            "pre_encounter_completed": True,
        })
        locations["Node004"].update({"visible": False, "enabled": False})
        locations["Node007"].update({"visible": False, "enabled": False})

        campaign._az1_reveal_neighbors(db, state, "Node003")
        assert locations["Node003"]["visible"] is True
        assert locations["Node004"]["visible"] is False
        assert locations["Node007"]["visible"] is False
        assert "Path_Node003_Node004" in pdata["locked_paths"]
        assert "Path_Node003_Node007" in pdata["locked_paths"]

        # A destination revealed by another route must not make the
        # unfinished node's own incoming path appear.
        pdata["unlocked_nodes"] = ["Node004"]
        campaign._az1_reveal_neighbors(db, state, "Node003")
        assert locations["Node004"]["visible"] is True
        assert "Path_Node003_Node004" in pdata["locked_paths"]

        # A normal victory opens the same outgoing paths.
        locations["Node003"].update({
            "completed": True, "autostart": False,
        })
        campaign._az1_reveal_neighbors(db, state, "Node003")
        assert locations["Node004"]["visible"] is True
        assert locations["Node007"]["visible"] is True
        assert "Path_Node003_Node004" not in pdata["locked_paths"]
        assert "Path_Node003_Node007" not in pdata["locked_paths"]

        # A failed/conditional result is retryable and also opens the paths.
        locations["Node003"].update({
            "completed": False, "repeatable": True, "autostart": False,
        })
        pdata["failed_nodes"] = ["Node003"]
        campaign._az1_reveal_neighbors(db, state, "Node003")
        assert locations["Node004"]["visible"] is True
        assert locations["Node007"]["visible"] is True
        assert "Path_Node003_Node004" not in pdata["locked_paths"]
        assert "Path_Node003_Node007" not in pdata["locked_paths"]
    finally:
        db.close()
        os.unlink(path)


def test_az1_unvisited_repeatable_conversations_are_not_blue():
    db, path = cloned_db()
    try:
        _area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata["conversation_visits"] = {}
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        for node in ("Node012", "Node016"):
            locations[node].update({
                "completed": False, "repeatable": True,
                "autostart": True, "visible": True, "enabled": True,
            })

        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        assert locations["Node012"]["repeatable"] is False
        assert locations["Node016"]["repeatable"] is False

        # Once the authored conversation has actually closed, the same
        # repeatable content becomes the blue retryable state.
        pdata["conversation_visits"]["Node012"] = 1
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        assert locations["Node012"]["repeatable"] is True
    finally:
        db.close()
        os.unlink(path)


def test_az1_direct_node00r_route_is_not_replaced_by_fork_path():
    db, path = cloned_db()
    try:
        _area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node00R"],
            "visited_paths": ["Path_Node007_Node00R"],
            "blocked_nodes": [],
            "quest_reveal_nodes": [],
            "quest_hidden_nodes": [],
            "failed_nodes": [],
        })
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node00R"].update({
            "completed": True, "repeatable": False,
            "autostart": False, "visible": True, "enabled": True,
        })
        locations["Node012"].update({
            "completed": False, "repeatable": False,
            "autostart": True, "visible": False, "enabled": False,
        })
        locations["Fork001"].update({
            "visible": True, "enabled": False,
        })

        campaign._az1_reveal_neighbors(db, state, "Node00R")
        assert locations["Node012"]["visible"] is True
        assert "Path_Node00R_Node012" not in pdata["locked_paths"]
        assert "Path_Fork001_Node012" in pdata["locked_paths"]
    finally:
        db.close()
        os.unlink(path)


def test_az1_alternate_reveal_does_not_unlock_incomplete_path():
    db, path = cloned_db()
    try:
        _area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node009", "Node00R"],
            "visited_paths": [],
            "blocked_nodes": [],
            "quest_reveal_nodes": [],
            "quest_hidden_nodes": [],
            "unlocked_nodes": [],
            "failed_nodes": [],
        })
        state["LastNode"] = "Node009"
        state["ALoc"] = "Node009"
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node009"].update({
            "completed": False, "repeatable": False, "autostart": True,
        })
        locations["Node00R"].update({
            "completed": True, "repeatable": False, "autostart": False,
        })
        locations["Node012"].update({"visible": False, "enabled": False})

        # Node012 is revealed from completed Node00R, but the direct
        # Node009 -> Node012 path still belongs to the unfinished Node009
        # branch and must remain locked.
        campaign._az1_reveal_neighbors(db, state, "Node009")
        assert locations["Node012"]["visible"] is True
        assert "Path_Node009_Node012" in pdata["locked_paths"]
    finally:
        db.close()
        os.unlink(path)


def test_az1_conv_done_recomputes_repeatable_node_paths():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campspawn = campaign.push_campspawn
    try:
        area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node001", "Node002", "Node003", "Node007"],
            "visited_paths": [
                "Path_Node001_Node002", "Path_Node002_Node003",
                "Path_Node003_Node007",
            ],
            "locked_paths": [
                "Path_Node007_Node009", "Path_Node007_Node00R",
            ],
            "blocked_nodes": [],
            "quest_reveal_nodes": [],
            "quest_hidden_nodes": [],
            "unlocked_nodes": [],
            "failed_nodes": [],
        })
        state["LastNode"] = "Node007"
        state["ALoc"] = "The Road of Oaks"
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node003"].update({
            "completed": True, "repeatable": False, "autostart": False,
        })
        locations["Node007"].update({
            "type": "Convo", "completed": False, "repeatable": True,
            "autostart": True, "visible": True, "enabled": True,
        })
        locations["Node009"].update({"visible": False, "enabled": False})
        locations["Node00R"].update({"visible": False, "enabled": False})
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campspawn = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        after = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        after_data = after["PublicState"]["Data"]
        after_locations = {
            item["Data"].get("node"): item["Data"]
            for item in after["VisLocs"]
        }
        assert after_locations["Node009"]["visible"] is True
        assert after_locations["Node00R"]["visible"] is True
        assert "Path_Node007_Node009" not in after_data["locked_paths"]
        assert "Path_Node007_Node00R" not in after_data["locked_paths"]
    finally:
        campaign._send_response = original_send_response
        campaign.push_campspawn = original_push_campspawn
        db.close()
        os.unlink(path)


def test_cross_zila_turnin_refreshes_bridge_path_and_weston_conversation():
    """A Winston turn-in must immediately open and activate Weston."""
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    original_push_campspawn = campaign.push_campspawn
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name='az01_q_cross_the_river'", (6,))
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_cross_the_river")
        area_id, state = area_state(db)
        state["LastNode"] = "Node012"
        state["ALoc"] = "Bridge over the Zila - East"
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node012"],
            "visited_paths": [],
            "blocked_nodes": [],
            "unlocked_nodes": ["Node015"],
            "quest_reveal_nodes": ["Node013", "Node014"],
            "quest_hidden_nodes": [],
            "failed_nodes": [],
            "conversation_visits": {"Node012": 1},
        })
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node012"].update({
            "type": "Convo", "completed": False, "repeatable": True,
            "autostart": True, "conversationId":
            "ca7be3ef-66e8-4b08-bad2-9e1f36751573",
            "visible": True, "enabled": True,
        })
        locations["Node015"].update({
            "type": "Convo", "completed": False, "visible": True,
            "enabled": True,
        })

        # Put Cross the River at its Winston turn-in objective (Step 2).
        state["ActiveEncounterGuid"] = SAVAGE_LORD
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()
        assert campaign._advance_quest_encounter_objectives(
            db, 6, SAVAGE_LORD)
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campupdate = lambda *args, **kwargs: None
        campaign.push_campspawn = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        after = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        after_data = after["PublicState"]["Data"]
        after_locations = {
            item["Data"].get("node"): item["Data"]
            for item in after["VisLocs"]
        }
        _quest_id, quest_state = campaign._quest_state_row(
            db, 6, "az01_q_cross_the_river")
        assert quest_state["Flags"]["_quest_objective_idx"] == 2
        assert "Path_Node012_Node015" not in after_data["locked_paths"]
        assert after_locations["Node015"]["visible"] is True
        assert after_locations["Node015"]["enabled"] is True
        assert after_locations["Node015"]["conversationId"] == \
            "5e5ec1cd-c869-43e8-82d4-417021954440"
        assert after_locations["Node015"]["turninquest"] is True

        # Entering the newly opened destination must preserve the active
        # quest conversation instead of replacing it with Weston Step 1b.
        campaign._handle_locaction(
            handler, db,
            {"CampID": area_id, "RAct": 0,
             "Loc": "Bridge over the Zila - West", "Params": []},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        entered = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        entered_weston = next(
            item["Data"] for item in entered["VisLocs"]
            if item["Data"].get("node") == "Node015")
        assert entered_weston["conversationId"] == \
            "5e5ec1cd-c869-43e8-82d4-417021954440"
        assert entered_weston["turninquest"] is True
        assert entered_weston["autostart"] is True
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        campaign.push_campspawn = original_push_campspawn
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
            "WHERE conversation_guid IN (?, ?, ?, ?, ?)",
            ("5e5ec1cd-c869-43e8-82d4-417021954440",
             "08b9b8ab-2100-4f8d-87b0-18369eb4ecb4",
             "4000c850-c1f7-45e4-b254-3cb1b0e9bf2b",
             "02977c4c-803a-465d-ae0c-b5896c3d4012",
             "880690b0-0c54-4ab8-a4e2-911b8bf14f64"),
        ).fetchall())
        assert json.loads(
            conversation_rewards["5e5ec1cd-c869-43e8-82d4-417021954440"]
        )["chest_guid"] == HOWLING_PLAINS_PACK
        assert json.loads(
            conversation_rewards["08b9b8ab-2100-4f8d-87b0-18369eb4ecb4"]
        )["chest_guid"] == HOWLING_PLAINS_PACK
        for find_guid in (
            "02977c4c-803a-465d-ae0c-b5896c3d4012",
            "880690b0-0c54-4ab8-a4e2-911b8bf14f64",
        ):
            assert json.loads(conversation_rewards[find_guid])["chest_guid"] \
                == HOWLING_PLAINS_PACK
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


def test_az1_panorama_scene_transitions_out_of_area_on_startloc():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA', 'QUEST') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, state = campaign._activate_az1_area(db, 4)

        # Put the player at the authored predecessor and expose Node017 as a
        # reachable destination, matching the map state immediately before
        # the client sends StartLoc for Cave-In.
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("node") in {"Node016", "Node017"}:
                data.update({"visible": True, "enabled": True})
            if data.get("node") == "Node016":
                data["completed"] = True
        state.update({"LastNode": "Node016", "ALoc": None})
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=4, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_locaction(
            handler, db,
            {"CampID": area_id, "RAct": 0,
             "Loc": "Cave-In", "Params": []},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        area_state = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        assert area_state["ALoc"] is None

        panorama_push = next(
            args for args in pushes if args[4] == "area_panorama")
        panorama_id, panorama_state = panorama_push[2], panorama_push[7]
        assert panorama_push[5] == "PANORAMA"
        assert panorama_push[6] is True
        assert panorama_state["PanoramaSceneGuid"] == \
            "c99b4f8d-e24d-4896-9342-6ac2562a2364"
        assert panorama_state["PublicState"]["Data"]["CampaignGroup"] == "AREA"
        assert panorama_state["PublicState"]["Data"]["HideQuickNavigation"] is False
        panorama_npcs = {
            item["Data"]["name"] for item in panorama_state["VisLocs"]
        }
        assert {"Ennis", "Takumi"}.issubset(panorama_npcs)
        assert not ({"Fahrny", "Myaa", "Vincent", "Wyatt", "Rizzix"}
                    & panorama_npcs)
        assert panorama_state["ALoc"] is None
        ennis = next(
            item["Data"] for item in panorama_state["VisLocs"]
            if item["Data"]["name"] == "Ennis")
        assert ennis["conversationId"] == \
            "4210aef3-52b5-40a1-a29a-b2e866579dcf"
        assert db.execute(
            "SELECT campaign_type FROM campaigns WHERE id=?",
            (panorama_id,)).fetchone()[0] == "PANORAMA"
        summary = campaign._build_camp_summary(
            panorama_id, 0, "PANORAMA", "AZ1", 6,
            panorama_state["PanoramaSceneGuid"], panorama_state["PanoramaNode"])
        assert summary["TypeInfo"]["AssetBundle"] == \
            "adventurezone01/p_dwrf_cavein"
        assert summary["TypeInfo"]["LevelPrefab"] == "p_dwrf_cavein"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_az1_panorama_uses_authored_repeat_hint_when_taming_is_in_progress():
    db, path = cloned_db()
    try:
        row = db.execute(
            "SELECT id, state_json FROM campaigns "
            "WHERE champion_id=7 AND campaign_type='AREA' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        assert row
        panorama = campaign._build_az1_panorama_state(
            db, 14, 7,
            "c99b4f8d-e24d-4896-9342-6ac2562a2364", "Node017",
            json.loads(row[1]))
        locations = {
            item["Data"]["name"]: item["Data"]
            for item in panorama["VisLocs"]
        }
        assert locations["Takumi"]["conversationId"] == \
            "5b385410-6ca5-41ca-b4bb-63efc8f0a57d"
        assert locations["Takumi"]["turninquest"] is False
        assert locations["Takumi"]["repeatable"] is True
        assert locations["Ennis"]["conversationId"] == \
            "4210aef3-52b5-40a1-a29a-b2e866579dcf"

        campaign._ensure_quest_campaign(db, 6, "AREA", "az01_tamed")
        mesa = campaign._build_az1_panorama_state(
            db, 15, 6,
            "11e30dc4-542e-45a9-8b9d-2fddc48700b9", "Node034",
            json.loads(row[1]))
        mesa_locations = {
            item["Data"]["name"]: item["Data"]
            for item in mesa["VisLocs"]
        }
        assert mesa_locations["Belarius"]["conversationId"] == \
            "b8f1b954-1461-4374-b41a-77401f0a4eb6"
        assert mesa_locations["Kian"]["conversationId"] == \
            "aae342a7-3840-4b59-9291-6720ca9bc782"
    finally:
        db.close()
        os.unlink(path)


def test_az1_panorama_conversations_stay_visible_and_grant_new_quests():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campspawn = campaign.push_campspawn
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name IN ('az01_tamed', 'az01_uw_find_cave_in', "
            "'q_seawitch'))", (4,))
        db.commit()
        campaign._ensure_quest_campaign(db, 4, "AREA", "az01_tamed")
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, area_state = campaign._activate_az1_area(db, 4)
        panorama_id, panorama_state = campaign._activate_az1_panorama(
            db, 4, "c99b4f8d-e24d-4896-9342-6ac2562a2364", "Node017",
            area_state)

        # Closing the authored repeat/hint conversation must not make the
        # client hide Takumi.
        panorama_state["ALoc"] = "Takumi"
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(panorama_state), panorama_id))
        db.commit()
        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campspawn = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": panorama_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        after_takumi = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (panorama_id,)
        ).fetchone()[0])
        takumi = next(
            item["Data"] for item in after_takumi["VisLocs"]
            if item["Data"].get("name") == "Takumi")
        assert takumi["completed"] is False
        assert takumi["repeatable"] is True

        # Ennis is an explicit quest-start row. Closing it must create the
        # quest and immediately replace the start conversation with its
        # not-complete conversation instead of hiding the NPC.
        after_takumi["ALoc"] = "Ennis"
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(after_takumi), panorama_id))
        db.commit()
        campaign._handle_sendevent(
            handler, db,
            {"CampID": panorama_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        _quest_id, sea_state = campaign._quest_state_row(db, 4, "q_seawitch")
        assert sea_state is not None
        after_ennis = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (panorama_id,)
        ).fetchone()[0])
        ennis = next(
            item["Data"] for item in after_ennis["VisLocs"]
            if item["Data"].get("name") == "Ennis")
        assert ennis["completed"] is False
        assert ennis["repeatable"] is True
        assert ennis["conversationId"] == \
            "a2eeb695-1ae8-49ce-b57a-3b7198a1ef92"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campspawn = original_push_campspawn
        db.close()
        os.unlink(path)


def test_az1_panorama_handoff_retries_on_arrived_node_without_encounter_field():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, state = campaign._activate_az1_area(db, 4)
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("node") == "Node017":
                data.update({
                    "visible": True,
                    "enabled": True,
                    "completed": True,
                    "encounter": None,
                })
        state.update({"LastNode": "Node017", "ALoc": None})
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=4, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_locaction(
            handler, db,
            {"CampID": area_id, "RAct": 0,
             "Loc": "Cave-In", "Params": []},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        panorama_push = next(
            args for args in pushes if args[4] == "area_panorama")
        assert panorama_push[5] == "PANORAMA"
        assert panorama_push[6] is True
        assert panorama_push[7]["PanoramaSceneGuid"] == \
            "c99b4f8d-e24d-4896-9342-6ac2562a2364"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_az1_ambling_mesa_uses_its_authored_panorama_asset():
    assert campaign._az1_panorama_assets("Node034", 6) == (
        "adventurezone01/p_cytl_amblingmesa", "p_cytl_amblingmesa")


def test_az1_panorama_return_parent_resumes_area_progress():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, area_state = campaign._activate_az1_area(db, 4)
        area_state["LastNode"] = "Node016"
        area_state["ALoc"] = None
        area_state["PublicState"]["Data"].update({
            "visited_nodes": ["Node001", "Node016"],
            "visited_paths": ["Path_Node015_Node016"],
        })
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(area_state), area_id))
        db.commit()
        panorama_id, _panorama_state = campaign._activate_az1_panorama(
            db, 4, "c99b4f8d-e24d-4896-9342-6ac2562a2364", "Node017",
            area_state)

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": panorama_id, "Event": "return_parent",
             "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        area_push = next(
            args for args in pushes if args[4] == "feralroot_travel")
        assert area_push[5] == "AREA"
        assert area_push[6] is True
        assert area_push[7]["LastNode"] == "Node016"
        assert area_push[7]["PublicState"]["Data"]["visited_paths"] == \
            ["Path_Node015_Node016"]
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_az1_travel_button_transitions_completed_cave_in_on_visit_node():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, state = campaign._activate_az1_area(db, 4)
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("node") in {"Node016", "Node017"}:
                data.update({"visible": True, "enabled": True})
            if data.get("node") in {"Node016", "Node017"}:
                data["completed"] = True
        state.update({"LastNode": "Node016", "ALoc": None})
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=4, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "visit_node",
             "OParms": ["Node017"]},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        panorama_push = next(
            args for args in pushes if args[4] == "area_panorama")
        assert panorama_push[5] == "PANORAMA"
        assert panorama_push[6] is True
        assert panorama_push[7]["PanoramaNode"] == "Node017"
        assert panorama_push[7]["PanoramaSceneGuid"] == \
            "c99b4f8d-e24d-4896-9342-6ac2562a2364"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_az1_multi_edge_travel_path_transitions_completed_cave_in():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, state = campaign._activate_az1_area(db, 4)
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("node") in {"Node015", "Node016", "Node017"}:
                data.update({"visible": True, "enabled": True})
            if data.get("node") == "Node017":
                data["completed"] = True
        state.update({"LastNode": "Node015", "ALoc": None})
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=4, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "visit_path",
             "OParms": [["Path_Node015_Node016",
                         "Path_Node016_Node017"]]},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        panorama_push = next(
            args for args in pushes if args[4] == "area_panorama")
        assert panorama_push[7]["PanoramaNode"] == "Node017"
        assert panorama_push[7]["PanoramaSceneGuid"] == \
            "c99b4f8d-e24d-4896-9342-6ac2562a2364"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_az1_repeated_path_from_cave_in_does_not_reopen_panorama():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, state = campaign._activate_az1_area(db, 4)
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("node") in {"Node016", "Node017"}:
                data.update({
                    "visible": True, "enabled": True, "completed": True,
                })
        state.update({"LastNode": "Node017", "ALoc": "Cave-In"})
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.setdefault("visited_paths", []).append("Path_Node016_Node017")
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=4, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "visit_path",
             "OParms": [["Path_Node016_Node017"]]},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        assert not any(args[4] == "area_panorama" for args in pushes)
        after = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        assert after["LastNode"] == "Node016"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_crayburn_defeat_conversation_keeps_encounter_retryable():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        camp_id = dungeon_fixture(db)
        state = campaign._build_initial_gameplay_state(
            camp_id, 7, "DUNGEON", "Shin'hare")
        state.update({
            "Started": "2026-09-07T00:00:00Z",
            "ALoc": "TowerGate",
            "LastNode": "TowerGate",
        })
        for node in campaign._CASTLE_CHAIN[1:5]:
            campaign._mark_location_completed(state, node)
        tower_gate = next(
            item["Data"] for item in state["VisLocs"]
            if item["Data"].get("node") == "TowerGate")
        tower_gate_fail = campaign._crayburn_node_data(
            "Shin'hare", "TowerGate")["fail"]
        tower_gate.update({
            "type": "Convo",
            "conversationId": tower_gate_fail,
            "encounter": None,
            "completed": False,
            "autostart": True,
        })
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), camp_id))
        db.commit()

        # This handler path normally writes a protocol response and pushes a
        # campaign update.  The state transition is all this regression test
        # needs to observe.
        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campupdate = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        event = {"CampID": camp_id, "Event": "conv_done", "OParms": None}
        campaign._handle_sendevent(
            handler, db, event, 0, "", 0, "ServiceCampaign", "253", 0, 0)

        raw = db.execute(
            "SELECT state_json FROM campaigns WHERE id=?",
            (camp_id,)).fetchone()[0]
        after = json.loads(raw)
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in after["VisLocs"]
        }
        assert locations["TowerGate"]["completed"] is False
        assert locations["TowerGate"]["type"] == "Encounter"
        assert locations["TowerGate"]["conversationId"] is None
        assert locations["TowerGate"]["encounter"] == \
            "c5cbbc95-a4ba-461e-9d42-1c592f120b1a"
        assert locations["PenworthTower"]["completed"] is False
        assert locations["PenworthTower"]["visible"] is False
        assert locations["PenworthTower"]["enabled"] is False
        assert after["ALoc"] == ""
        assert after["LastNode"] == "InnerBailey"

        # A duplicate/stale acknowledgement must not use LastNode to turn the
        # failed encounter into a win.
        campaign._handle_sendevent(
            handler, db, event, 0, "", 0, "ServiceCampaign", "253", 0, 0)
        raw = db.execute(
            "SELECT state_json FROM campaigns WHERE id=?",
            (camp_id,)).fetchone()[0]
        duplicate = json.loads(raw)
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in duplicate["VisLocs"]
        }
        assert locations["TowerGate"]["completed"] is False
        assert locations["PenworthTower"]["completed"] is False
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_crayburn_locked_future_node_does_not_replace_tower_gate_start():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_launch_encounter = campaign._launch_encounter
    try:
        camp_id = dungeon_fixture(db)
        state = campaign._build_initial_gameplay_state(
            camp_id, 7, "DUNGEON", "Shin'hare")
        state.update({
            "Started": "2026-09-07T00:00:00Z",
            "ALoc": "",
            "LastNode": "InnerBailey",
        })
        for node in campaign._CASTLE_CHAIN[1:5]:
            campaign._mark_location_completed(state, node)
        # Simulate an old save that leaked the future marker to the client.
        penworth = next(
            item["Data"] for item in state["VisLocs"]
            if item["Data"].get("node") == "PenworthTower")
        penworth.update({"visible": True, "enabled": True})
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), camp_id))
        db.commit()

        campaign._send_response = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_locaction(
            handler, db,
            {"CampID": camp_id, "RAct": 0, "Loc": "PenworthTower"},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        after_reject = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?",
            (camp_id,)).fetchone()[0])
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in after_reject["VisLocs"]
        }
        assert after_reject["ALoc"] == ""
        assert after_reject["LastNode"] == "InnerBailey"
        assert locations["PenworthTower"]["visible"] is False
        assert locations["PenworthTower"]["enabled"] is False

        # The legal destination remains Tower Gatehouse, and its authored
        # encounter is the one launched by the subsequent start event.
        campaign._handle_locaction(
            handler, db,
            {"CampID": camp_id, "RAct": 0, "Loc": "TowerGate"},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        active = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?",
            (camp_id,)).fetchone()[0])
        assert active["ALoc"] == "TowerGate"

        launched = []
        campaign._launch_encounter = lambda *args, **kwargs: launched.append(args[4])
        campaign._handle_sendevent(
            handler, db,
            {"CampID": camp_id, "Event": "start", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        assert launched == ["c5cbbc95-a4ba-461e-9d42-1c592f120b1a"]
    finally:
        campaign._send_response = original_send_response
        campaign._launch_encounter = original_launch_encounter
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    tests = [
        test_cross_zila_objective_is_linked_to_savage_lord,
        test_node00r_uses_authored_node_r_conversation_fallback,
        test_node00r_faction_quest_branch_opens_lena_grotto_and_stays_retryable,
        test_node00r_finished_faction_quest_uses_terminal_conversation,
        test_cross_zila_and_zodiac_gates_follow_quest_progress,
        test_az1_unfinished_encounter_does_not_reveal_outgoing_paths,
        test_az1_unvisited_repeatable_conversations_are_not_blue,
        test_az1_direct_node00r_route_is_not_replaced_by_fork_path,
        test_az1_alternate_reveal_does_not_unlock_incomplete_path,
        test_az1_conv_done_recomputes_repeatable_node_paths,
        test_cross_zila_turnin_refreshes_bridge_path_and_weston_conversation,
        test_savage_lord_advances_cross_zila_and_node_rewards_are_authored,
        test_corrupt_dryad_encounter_grants_pack_and_faction_equipment,
        test_az1_scene_lookup_keeps_authored_node_variants_distinct,
        test_az1_objectives_use_faction_specific_conversations,
        test_az1_opening_uses_the_underworld_route_and_fog_gates,
        test_az1_panorama_scene_transitions_out_of_area_on_startloc,
        test_az1_panorama_uses_authored_repeat_hint_when_taming_is_in_progress,
        test_az1_panorama_conversations_stay_visible_and_grant_new_quests,
        test_az1_panorama_handoff_retries_on_arrived_node_without_encounter_field,
        test_az1_panorama_return_parent_resumes_area_progress,
        test_az1_travel_button_transitions_completed_cave_in_on_visit_node,
        test_az1_multi_edge_travel_path_transitions_completed_cave_in,
        test_az1_repeated_path_from_cave_in_does_not_reopen_panorama,
        test_crayburn_defeat_conversation_keeps_encounter_retryable,
        test_crayburn_locked_future_node_does_not_replace_tower_gate_start,
    ]
    for test in tests:
        test()
        print("PASS", test.__name__)
