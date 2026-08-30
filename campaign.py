"""Campaign service handler for Hex TCG private server.
Handles ServiceCampaign requests (data_type 110000, UID type 253).

Protocol: CampSysGeneral transports JSON-serialized requests/responses
via a byte[] Envelope field, wrapped in an ObjFmt Response/Request.

Request types:
  qcur4champ  - QueryCurrentForChampion (globe -> dungeon transition)
  getactive   - QueryActiveStatus (check active campaigns)
  createcamp  - Create a new campaign
  getcampstate - QueryCampState (get full GameplayState)
  startcamp   - StartCamp (begin the campaign)
  getcampsum  - GetCampaignSummary (template/asset info)
  sendevent   - SendEvent (to campaign logic engine)
  locaction   - StartLoc / FinishLoc
  forfeit     - Forfeit campaign
  cheat       - Cheat commands
"""

import json
import time
import uuid
import re
import random
from pathlib import Path

import game_engine

try:
    import yaml
except ImportError:  # pragma: no cover - deployment fallback
    yaml = None

from encoder import encode_objfmt_response, encode_datawrapper, compress_gzip

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_camp_uid(lo, hi=0):
    """Build a compound UID from lo/hi parts (simulates UID.GetInstanceId())."""
    return {"lo": lo, "hi": hi}


def _new_camp_id(db):
    """Generate a unique campaign UID for our DB."""
    row = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM campaigns").fetchone()
    cid = row[0]
    # Return as JSON-friendly ulong (just an int, since Python ints are arbitrary-precision
    # and Newtonsoft.Json on the client can parse them from JSON numbers)
    return cid


def _generate_inst_id():
    """Generate a UID instance ID (pseudo-random ulong)."""
    return abs(hash(uuid.uuid4())) % (2**63)


def _get_champion(db, champion_id):
    """Get champion info from the DB."""
    return db.execute(
        "SELECT id, user_id, race, champion_name, level FROM champions WHERE id=?",
        (champion_id,)
    ).fetchone()


def _find_campaign_for_champion(db, champion_id, campaign_type="PANORAMA"):
    """Find (or create) a campaign for a champion."""
    row = db.execute(
        "SELECT id, camp_uid_lo, camp_uid_hi, is_started, state_json "
        "FROM campaigns WHERE champion_id=? AND campaign_type=?",
        (champion_id, campaign_type)
    ).fetchone()
    if not row:
        cid = _new_camp_id(db)
        inst_id = _generate_inst_id()
        champ = _get_champion(db, champion_id)
        race = champ[2] if champ else None
        champ_name = champ[3] if champ else ""
        state = _build_initial_gameplay_state(cid, champion_id, campaign_type, race)
        template_name = "Crayburn Castle" if campaign_type == "DUNGEON" else "AZ1"
        db.execute(
            "INSERT INTO campaigns (id, camp_uid_lo, camp_uid_hi, champion_id, user_id, "
            "champion_name, template_name, campaign_type, is_started, state_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, inst_id, 0, champion_id,
             db.execute("SELECT user_id FROM champions WHERE id=?", (champion_id,)).fetchone()[0],
             champ_name, template_name, campaign_type, 0, json.dumps(state))
        )
        db.commit()
        # Keep the champion's LastCampaignID in sync so the client can jump
        # straight into this campaign after selecting the champion on the globe.
        db.execute("UPDATE champions SET last_campaign_id=? WHERE id=?", (cid, champion_id))
        db.commit()
        # A DUNGEON campaign always drives a journal quest (the client's
        # QuestMgr queries getactive with CampType=QUEST and resolves the quest
        # template by the campaign's TemplateName = quest script name).
        if campaign_type == "DUNGEON":
            _ensure_quest_campaign(db, champion_id, "DUNGEON")
        return (cid, inst_id, 0, state)
    # Campaign already exists: keep LastCampaignID synced and return its data.
    # Older development runs could leave a one-node dungeon state attached to
    # a PANORAMA row (for example after using the dungeon debug path).  The
    # client trusts the row type from getcampsum but renders the locations from
    # this state, so that mismatch produces a panorama with no NPCs at all.
    existing_state = row[4] and json.loads(row[4]) or None
    if campaign_type == "PANORAMA" and existing_state:
        public_data = (existing_state.get("PublicState", {}) or {}).get("Data", {}) or {}
        stale_panorama = (
            existing_state.get("TempType") != "PANORAMA"
            or public_data.get("CampaignGroup") != "PANORAMA"
            or public_data.get("IsStarterDungeon")
        )
        if stale_panorama:
            champ = _get_champion(db, champion_id)
            race = champ[2] if champ else None
            existing_state = _build_initial_gameplay_state(
                row[0], champion_id, "PANORAMA", race)
            db.execute(
                "UPDATE campaigns SET template_name='AZ1', is_started=0, state_json=? WHERE id=?",
                (json.dumps(existing_state), row[0]))
            db.commit()
        champ = _get_champion(db, champion_id)
        cfg = _az0_config(champ[2]) if champ else None
        if cfg and _normalize_starter_panorama_state(existing_state, cfg):
            db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                       (json.dumps(existing_state), row[0]))
            db.commit()
    db.execute("UPDATE champions SET last_campaign_id=? WHERE id=?", (row[0], champion_id))
    db.commit()
    if campaign_type == "DUNGEON":
        _ensure_quest_campaign(db, champion_id, "DUNGEON")
    return (row[0], row[1], row[2], existing_state)


def _get_existing_campaign_for_champion(db, champion_id, campaign_type):
    """Return the newest stored campaign of *campaign_type*, without creating one."""
    row = db.execute(
        "SELECT id, camp_uid_lo, camp_uid_hi, is_started, state_json "
        "FROM campaigns WHERE champion_id=? AND campaign_type=? "
        "ORDER BY id DESC LIMIT 1",
        (champion_id, campaign_type)
    ).fetchone()
    if not row:
        return None
    return (row[0], row[1], row[2], row[3],
            json.loads(row[4]) if row[4] else None)


# ---------------------------------------------------------------------------
# Quest templates (data-driven from gamedata QuestTemplate records)
# ---------------------------------------------------------------------------
# The client's QuestMgr queries getactive with CampType=QUEST; it resolves the
# QuestTemplate by regex-matching our TemplateName against the template's
# m_ScriptName. The quest journal then iterates the QUEST campaign's VisLocs,
# matching each Loc.Name against the objective's m_QuestLocationId. So each
# quest campaign row carries template_name = script name, and its state has one
# VisLoc per objective keyed by that location id.
def _quest_template_from_row(row):
    """Decode one Records-derived QuestTemplate row."""
    if not row:
        return None
    try:
        objectives = json.loads(row[2] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        objectives = []
    if not isinstance(objectives, list):
        objectives = []
    return {
        "script_name": row[0],
        "title": row[1] or row[0],
        "objectives": objectives,
        "campaign_group": row[3] or "AREA",
        "start_hook": row[4] or "",
    }


def _quest_template(db, quest_script=None, campaign_group=None):
    """Select a QuestTemplate from the server-owned metadata table."""
    sql = ("SELECT script_name, title, objectives_json, campaign_group, "
           "start_hook FROM quest_templates WHERE enabled=1")
    params = []
    if quest_script:
        sql += " AND script_name=?"
        params.append(quest_script)
    if campaign_group:
        sql += " AND campaign_group=?"
        params.append(campaign_group)
    sql += " ORDER BY script_name LIMIT 1"
    try:
        row = db.execute(sql, params).fetchone()
    except Exception:
        # Small protocol fixtures may predate quest metadata.  Real databases
        # are seeded by static.ensure_schema before campaign requests arrive.
        return None
    return _quest_template_from_row(row)


def _ensure_quest_campaign(db, champ_id, campaign_group, quest_script=None):
    """Create the QUEST campaign row that drives the zone's quest journal.

    The client's QuestMgr caches the quest by the getactive TemplateName
    (QuestScriptName), resolves the QuestTemplate by that name, then renders
    the objectives from the QUEST campaign's GameplayState VisLocs (matched by
    objective m_QuestLocationId). Idempotent.
    """
    candidate = _quest_template(db, quest_script, campaign_group)
    quest = ((candidate["script_name"], candidate)
             if candidate else None)
    if not quest:
        return None
    script_name, q = quest
    row = db.execute(
        "SELECT id FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
        "AND template_name=?", (champ_id, script_name)).fetchone()
    if row:
        return row[0]
    cid = _new_camp_id(db)
    inst_id = _generate_inst_id()
    champ = _get_champion(db, champ_id)
    champ_name = champ[3] if champ else ""
    # Reveal objectives progressively: only the FIRST objective is visible
    # initially. The client's journal iterates all quest-state VisLocs, so a
    # not-yet-reached objective (e.g. "Report your success") must NOT be in the
    # VisLocs yet — it is appended by _advance_quest_campaign when the previous
    # objective completes. The full ordered list is kept in Flags.
    vislocs = []
    locnodes = []
    for i, obj in enumerate(q["objectives"]):
        if i > 0:
            break  # only the active (first) objective for now
        # Preserve the authored objective type.  In particular, AZ1 taming
        # objectives are Encounter locations; collapsing every non-Dungeon
        # objective to Convo leaves the quest journal pointing at a synthetic
        # conversation instead of the battle.
        otype = obj.get("type") or "Convo"
        vislocs.append({
            "Data": {
                "name": obj["id"], "node": obj["id"], "type": otype,
                "autostart": False, "autopan": False, "autotrigger": False,
                "battle": None, "completed": False, "enabled": True, "visible": True,
                "repeatable": False, "givequest": False, "turninquest": False,
                "impassable": False, "unknown": False,
                "encounter": obj.get("encounter"), "encounter_desc": None, "allow_cancel": False,
                "conversationId": obj.get("conversation"),
            }
        })
        locnodes.append({"Name": obj["id"], "Data": {"id": obj["id"], "type": "DEFAULT"}})
    state = {
        "CampID": cid,
        "ChampID": champ_id,
        "TempType": "QUEST",
        "PayGroups": [],
        "CSlide": None,
        "ALoc": q["objectives"][0]["id"] if q["objectives"] else "",
        "VisLocs": vislocs,
        "LocNodes": locnodes,
        # The client resolves Location.Encounter through this catalog before
        # it can open a battle panel. Build it from the encounter_scenes rows
        # referenced by the authored objectives.
        "Encounters": [
            {"Name": guid, "Data": {"encscene": guid}}
            for guid in dict.fromkeys(
                obj.get("encounter") for obj in q["objectives"]
                if obj.get("encounter") and db.execute(
                    "SELECT 1 FROM encounter_scenes WHERE guid=?", (obj["encounter"],)
                ).fetchone()
            )
        ],
        "Champions": [],
        "CurState": "EXPLORE",
        "LastNode": q["objectives"][0]["id"] if q["objectives"] else "",
        "PublicState": {"Data": {"CampaignGroup": campaign_group}},
        "Started": None, "Finished": None, "FinishReason": None,
        "Wins": 0, "Losses": 0, "Score": 0, "HealthAdj": 0, "DungeonLifeAdj": 0,
        "Flags": {"_quest_objective_idx": 0, "_quest_objectives": q["objectives"]},
    }
    db.execute(
        "INSERT INTO campaigns (id, camp_uid_lo, camp_uid_hi, champion_id, user_id, "
        "champion_name, template_name, campaign_type, is_started, state_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, inst_id, 0, champ_id,
         db.execute("SELECT user_id FROM champions WHERE id=?", (champ_id,)).fetchone()[0],
         champ_name, script_name, "QUEST", 1, json.dumps(state)))
    db.commit()
    return cid


def _advance_quest_campaign(db, champ_id, quest_script=None, scene_guid=None):
    """Complete the current quest objective and reveal the next one.

    The client's journal iterates all quest-state VisLocs, so objectives that
    are not yet reachable must not be present. This marks the active objective
    completed and appends the next objective's VisLoc when one exists. Returns
    the updated GameplayState or None.
    """
    if not quest_script:
        template = _quest_template(db, campaign_group="DUNGEON")
        quest_script = template["script_name"] if template else None
    if not quest_script:
        return None
    row = db.execute(
        "SELECT id, state_json FROM campaigns "
        "WHERE champion_id=? AND campaign_type='QUEST' "
        "AND template_name=?",
        (champ_id, quest_script)).fetchone()
    if not row:
        return None
    qid, state_json = row
    state = json.loads(state_json) if state_json else None
    if not state:
        return None
    objectives = state.get("Flags", {}).get("_quest_objectives") or []
    idx = int(state.get("Flags", {}).get("_quest_objective_idx", 0))
    if scene_guid and idx < len(objectives):
        expected = objectives[idx].get("encounter")
        if expected and str(expected) != str(scene_guid):
            return None
    # Mark the current objective completed.
    cur_name = objectives[idx]["id"] if idx < len(objectives) else None
    for loc in state.get("VisLocs", []):
        d = loc.get("Data", {})
        if cur_name and (d.get("node") == cur_name or d.get("name") == cur_name):
            d["completed"] = True
    # Reveal the next objective if any.
    nxt = idx + 1
    if nxt < len(objectives):
        obj = objectives[nxt]
        otype = obj.get("type") or "Convo"
        # The post-dungeon journal objective is a real conversation.  Resolve
        # its GUID from the race-specific Crayburn quest-end conversation
        # instead of emitting a null conversationId that makes the client
        # hang in campaign loading. The final encounter's victory scene is
        # shown at the castle; the quest objective is the later report to the
        # faction NPC in the AZ1 panorama.
        conversation_id = None
        quest_template = _quest_template(db, quest_script)
        if (otype.lower() in {"convo", "conversation"} and
                quest_template and
                quest_template.get("campaign_group") == "DUNGEON"):
            race = _race_name_for_campaign(db, champ_id)
            conversation_id = (_CRAYBURN_CASTLE.get("races", {})
                               .get(race, {}).get("quest_end"))
        elif otype.lower() in {"convo", "conversation"}:
            conversation_id = obj.get("conversation")
        state.setdefault("VisLocs", []).append({
            "Data": {
                "name": obj["id"], "node": obj["id"], "type": otype,
                "autostart": False, "autopan": False, "autotrigger": False,
                "battle": None, "completed": False, "enabled": True, "visible": True,
                "repeatable": False, "givequest": False, "turninquest": False,
                "impassable": False, "unknown": False,
                "encounter": None, "encounter_desc": None, "allow_cancel": False,
                "conversationId": conversation_id,
            }
        })
        state.setdefault("LocNodes", []).append(
            {"Name": obj["id"], "Data": {"id": obj["id"], "type": "DEFAULT"}})
        state["Flags"]["_quest_objective_idx"] = nxt
        state["ALoc"] = obj["id"]
        state["LastNode"] = obj["id"]
    db.execute("UPDATE campaigns SET state_json=? WHERE id=?", (json.dumps(state), qid))
    db.commit()
    return state


def _advance_quest_encounter_objectives(db, champ_id, scene_guid):
    """Advance any active non-taming quest whose current objective is *scene_guid*."""
    if not scene_guid:
        return []
    rows = db.execute(
        "SELECT template_name FROM campaigns WHERE champion_id=? "
        "AND campaign_type='QUEST' AND template_name<>? AND state_json IS NOT NULL",
        (champ_id, "az01_tamed"),
    ).fetchall()
    advanced = []
    for (script,) in rows:
        _qid, state = _quest_state_row(db, champ_id, script)
        if not state or state.get("Finished"):
            continue
        flags = state.get("Flags") or {}
        objectives = flags.get("_quest_objectives") or []
        try:
            index = int(flags.get("_quest_objective_idx", 0))
        except (TypeError, ValueError):
            index = 0
        if index >= len(objectives):
            continue
        expected = objectives[index].get("encounter")
        if expected and str(expected) == str(scene_guid):
            updated = _advance_quest_campaign(db, champ_id, script, scene_guid)
            if updated:
                advanced.append(updated)
    return advanced


# Per-race AZ0 starter campaign config.
# Race IDs map to ERace: 1=Human 2=Elf 3=Coyotle 4=Orc 5=Dwarf
#                         6=ShinHare 7=Vennen 8=Necrotic
# Each race has its own panorama prefab, intro/trainer NPC GameObjects,
# and AZ0 conversation GUIDs (extracted from gamedata/localization.db).
# Race id -> race display name (used to key into the Crayburn Castle dungeon
# conversation map, whose conversation names are "Crayburn Castle - <Race> - ...").
_RACE_NAMES = {
    1: "Human", 2: "Elf", 3: "Coyotle", 4: "Orc",
    5: "Dwarf", 6: "Shin'hare", 7: "Vennen", 8: "Necrotic",
}
_RACE_FACTIONS = {1: "Ardent", 2: "Ardent", 3: "Ardent", 4: "Ardent",
                  5: "Underworld", 6: "Underworld", 7: "Underworld", 8: "Underworld"}
_AZ0_RACE_CONFIG = {
    1: {  # Human — Wren's Citadel
        "bundle": "adventurezone01/p_hmm_wrenscastle",
        "prefab": "p_hmm_wrenscastle",
        "intro_npc": "CaptainCedric",
        "trainer_npc": "GarethKay",
        "quest_npc": "ColonelSterling",
        "training_node": "TrainingWithGareth",
        "intro_conv": "a31d27d0-0c2b-4b8b-90ec-25abae6c5987",
        "battle_conv": "d41a4a46-fd3d-4c98-bafb-a87c30aad05c",
        "training_success_conv": "bcadcd54-f196-443a-aeea-e77e6aba87b3",
        "training_fail_conv": "3b42dc53-51cf-411d-b43c-a1841f69272b",
        "quest_conv": "abdcca36-20fa-4af7-8c24-8b5e0fcb61d3",
        "transition_conv": "c92ff657-ea66-4fd5-9b90-1f8161ffd964",
        "training_encounter": "5227b1b0-193a-45e8-a1a3-8215f20ac95b",
        "ai_champion_guid": "a633844d-bb26-4776-9351-aea16b4c71ba",
        "gameboard": "CastleExterior",
        "ai_personality": "Comfortable",
    },
    2: {  # Elf — Satyr's Roost
        "bundle": "adventurezone01/p_elf_satyrsroost",
        "prefab": "p_elf_satyrsroost",
        "intro_npc": "Emilia",
        "trainer_npc": "Nerissa",
        "quest_npc": "Balthasar",
        "training_node": "TrainingWithNerissa",
        "intro_conv": "e33c74c8-8b90-4549-a8f2-25c06c35a7a4",
        "battle_conv": "ed3b4737-b824-48e6-8a53-cc6269f46b37",
        "training_success_conv": "557dcfdc-f26c-4b36-94b9-182d1f8c6be3",
        "training_fail_conv": "7756b814-2c9b-49f1-bb9a-651d2a4f0e43",
        "quest_conv": "18b84529-d187-46ad-bb98-9a591d5287ea",
        "transition_conv": "366a9029-5545-4967-95d4-b10f54279b9c",
        "training_encounter": "e5cd349e-a77d-4175-8cfc-3565395c2eb4",
        "ai_champion_guid": "11b18575-fab3-49d1-a1ab-2ef6447c99d1",
        "gameboard": "Forest",
        "ai_personality": "Defensive",
    },
    3: {  # Coyotle — Thunderfield
        "bundle": "adventurezone01/p_cytl_thunderfield",
        "prefab": "p_cytl_thunderfield",
        "intro_npc": "ShortBuffalo",
        "trainer_npc": "WhisperingBreeze",
        "quest_npc": "DuskDaughter",
        "training_node": "TrainingWithWhisperingBreeze",
        "intro_conv": "04829c2c-b544-4232-bd47-cbd6cb860d91",
        "battle_conv": "081487f9-c107-4c2f-a085-643aa3f19018",
        "training_success_conv": "ee681f28-72a9-4e0b-bb25-fb8f45dd3da3",
        "training_fail_conv": "1d2dac36-6e91-4ed1-b4cc-a0eaf66d55b1",
        "quest_conv": "0934ceae-03be-436e-b8e5-4f8922e02a0b",
        "transition_conv": "c5f75a23-4044-47d7-b80f-0615cfd2546c",
        "training_encounter": "064d32cd-9171-4052-82a2-869eb0017b2a",
        "ai_champion_guid": "b41ca664-789c-4958-8386-8e7b339825c8",
        "gameboard": "CanyonDesert",
        "ai_personality": "Aggressive",
    },
    4: {  # Orc — Xamahuac
        "bundle": "adventurezone01/p_orc_xamahuac",
        "prefab": "p_orc_xamahuac",
        "intro_npc": "Xolotl",
        "trainer_npc": "Moqui",
        "quest_npc": "Montecuma",
        "training_node": "TrainingWithMoqui",
        "intro_conv": "f048e5f8-399c-4fa8-88c2-ceeddd6e6d7e",
        "battle_conv": "49272f71-1cff-451e-a311-5a6c31c57bf0",
        "training_success_conv": "ac38b34a-4f16-44fb-ac6f-6950bb20f33b",
        "training_fail_conv": "875ae8cd-7ed2-4f8a-ad8a-472df8e6f824",
        "quest_conv": "578dd9ea-c5ab-49a0-93c5-44ad235cf0a9",
        "transition_conv": "0445cf14-1725-4488-ad78-79642b90e519",
        "training_encounter": "84cdd061-93ec-4126-a2f6-530ac5217c96",
        "ai_champion_guid": "cb2e56cd-5502-4e4c-88e0-a9955715569b",
        "gameboard": "CastleExterior",
        "ai_personality": "Aggressive",
    },
    5: {  # Dwarf — The Quarry (NPCs use generic NodeA-D names)
        "bundle": "adventurezone01/p_dwrf_thequarry",
        "prefab": "p_dwrf_thequarry",
        "intro_npc": "NodeA",
        "trainer_npc": "NodeB",
        "quest_npc": "NodeC",
        "training_node": "TrainingWithGwendower",
        "intro_conv": "2a606713-1fa8-4253-ade3-c05d1a0bdd3a",
        "battle_conv": "3e72f03f-150a-4788-9024-08020711cacf",
        "training_success_conv": "d40ba708-26d2-43cb-a28d-386dcc829bac",
        "training_fail_conv": "84df3448-eb70-41f0-96d3-d088c8344fb9",
        "quest_conv": "88fe73d5-a42a-408d-97c2-ca5651affc1b",
        "transition_conv": "d4a78dc8-2505-484e-8088-245447fa3f9d",
        "training_encounter": "de641801-55a2-4df8-b298-d2e91c21718d",
        "ai_champion_guid": "b43e98a6-040e-4447-a576-e8e5bdd3df11",
        "gameboard": "CastleExterior",
        "ai_personality": "Comfortable",
    },
    6: {  # Shin'hare — Jinguru
        "bundle": "adventurezone01/p_shnhr_jinguru",
        "prefab": "p_shnhr_jinguru",
        "intro_npc": "Mitsuo",
        "trainer_npc": "Sora",
        "quest_npc": "Uyuki",
        "training_node": "TrainingWithSora",
        "intro_conv": "d6fb2c8c-51a3-4a9d-8761-bb9275f53fd6",
        "battle_conv": "9c508f59-0c71-4ca6-8cf1-1c5647f3cde4",
        "training_success_conv": "645e5c27-7ed2-485b-babd-f8bc7856a0b1",
        "training_fail_conv": "a95cf2a9-a860-4caa-9022-070c24cbc626",
        "quest_conv": "d89040c9-75c1-4f10-a647-f35679bcff72",
        "transition_conv": "beb123ef-c255-43c0-bebc-06cd79565f66",
        "training_encounter": "510ed850-5a3e-437b-875e-7834cacd1865",
        "ai_champion_guid": "ddc235eb-c6dc-4384-b567-70eb7498b729",
        "gameboard": "Forest",
    },
    7: {  # Vennen — The Hatchery
        "bundle": "adventurezone01/p_vnnn_thehatchery",
        "prefab": "p_vnnn_thehatchery",
        "intro_npc": "Orzh",
        "trainer_npc": "Zilth",
        "quest_npc": "Xarlot",
        "training_node": "TrainingWithZilth",
        "intro_conv": "c1d936df-518b-4dfa-bdfe-8ce60e6aa278",
        "battle_conv": "4198e540-3301-42de-a394-a8e1f80b8b64",
        "training_success_conv": "3ab83907-6db4-4c29-ab0d-5076393dd744",
        "training_fail_conv": "25b66849-845a-41e9-b0d5-ee11d79879a0",
        "quest_conv": "782369c4-61c2-4a3e-97ce-660f2d062be7",
        "transition_conv": "8f27b71d-42ce-4120-8e6f-380a31f273d2",
        "training_encounter": "f56ba80f-af31-4d75-a4bc-640f899ddd32",
        "ai_champion_guid": "e7539c3a-0bac-486c-9792-0293db113268",
        "gameboard": "CastleExterior",
    },
    8: {  # Necrotic — The Necropolis
        "bundle": "adventurezone01/p_ncrtc_necropolis",
        "prefab": "p_ncrtc_necropolis",
        "intro_npc": "Drokkord",
        "trainer_npc": "Iddi",
        "quest_npc": "Margugram",
        "training_node": "TrainingWithIddi",
        "intro_conv": "827e9c9b-7d79-4a01-81ca-1c11907ab24d",
        "battle_conv": "5ca75696-f381-4d4f-abd9-a6428dfb3cd9",
        "training_success_conv": "f526e886-6577-4088-8bcc-9dfff3615ada",
        "training_fail_conv": "6374b9a7-7ead-48b1-9752-e57024d4dec3",
        "quest_conv": "4cf66046-086b-40f8-8237-4e4d13e64a42",
        "transition_conv": "263dbbaa-d710-4e4f-b763-e638816bbcfa",
        "training_encounter": "7b9a5a0d-727f-4df8-923f-1e5a5b24c522",
        "ai_champion_guid": "b173e3d2-bdd3-44cb-b554-85d1c56b0cd2",
        "ai_charge_power": "0f72c46b-9fb6-a0e6-9fec-b9e297c3a75c",
        "gameboard": "CastleExterior",
    },
}


def _az0_config(champion_race):
    """Return the AZ0 race config for a champion race id, or None."""
    return _AZ0_RACE_CONFIG.get(champion_race)


def _convo_location(name, conversation_id, *, givequest=False,
                    turninquest=False):
    return {
        "Data": {
            "name": name, "node": name, "type": "Convo",
            "autostart": False, "autopan": False, "autotrigger": False,
            "battle": None, "completed": False, "enabled": True, "visible": True,
            "repeatable": False, "givequest": bool(givequest),
            "turninquest": bool(turninquest),
            "impassable": False, "unknown": False, "encounter": None,
            "encounter_desc": None, "allow_cancel": False,
            "conversationId": conversation_id,
        }
    }


def _activate_az1_transition(db, champ_id, cfg):
    """Expose the race NPC's authored Feralroot travel conversation.

    The Crayburn report is spoken to a faction NPC, while the AZ0->AZ1
    transition is a separate conversation with the original race NPC.
    """
    if not cfg or not cfg.get("transition_conv"):
        return None
    row = db.execute(
        "SELECT id, state_json FROM campaigns "
        "WHERE champion_id=? AND campaign_type='PANORAMA' "
        "ORDER BY id DESC LIMIT 1", (champ_id,)).fetchone()
    if not row:
        return None
    pano_id, state_json = row
    state = json.loads(state_json) if state_json else None
    if not state:
        return None
    intro_npc = cfg["intro_npc"]
    report_npc = cfg["quest_npc"]
    # The report has been consumed; replace it with the original NPC's
    # Feralroot travel conversation and leave the player in explore mode.
    for loc in state.setdefault("VisLocs", []):
        data = loc.setdefault("Data", {})
        node = data.get("node") or data.get("name")
        if node == report_npc:
            data.update({"visible": False, "enabled": False, "completed": True})
        elif node == intro_npc:
            data.update({"type": "Convo", "conversationId": cfg["transition_conv"],
                         "visible": True, "enabled": True, "completed": False,
                         "givequest": True})
    if not any((loc.get("Data", {}).get("node") or loc.get("Data", {}).get("name")) == intro_npc
               for loc in state["VisLocs"]):
        state["VisLocs"].append(_convo_location(intro_npc, cfg["transition_conv"],
                                                 givequest=True))
    state["PostCrayburnReport"] = True
    state["ALoc"] = None
    state["CurState"] = "EXPLORE"
    db.execute("UPDATE campaigns SET is_started=1, state_json=? WHERE id=?",
               (json.dumps(state), pano_id))
    db.commit()
    return pano_id, state


def _prepare_post_crayburn_report(db, champ_id, state):
    """Expose the authored report conversation in a completed dungeon handoff.

    The report is hosted by the race panorama, but it is only valid after the
    Crayburn dungeon has advanced its linked journal quest.  Keep this helper
    limited to shaping the panorama state; the conversation handler applies
    the reward and completes the objective when the client sends ``conv_done``.
    """
    champ = _get_champion(db, champ_id)
    cfg = _az0_config(champ[2]) if champ else None
    if not cfg:
        return False
    report_conv = (_CRAYBURN_CASTLE.get("races", {})
                   .get(_RACE_NAMES.get(champ[2], ""), {})
                   .get("quest_end"))
    if not report_conv:
        return False

    report_npc = cfg["quest_npc"]
    report_loc = None
    for loc in state.setdefault("VisLocs", []):
        data = loc.setdefault("Data", {})
        if data.get("node") == report_npc or data.get("name") == report_npc:
            report_loc = data
            break
    if report_loc is None:
        state["VisLocs"].append(
            _convo_location(report_npc, report_conv, turninquest=True))
        changed = True
    else:
        desired = {
            "type": "Convo", "conversationId": report_conv,
            "visible": True, "enabled": True, "completed": False,
            "givequest": False, "turninquest": True,
        }
        changed = any(report_loc.get(key) != value
                      for key, value in desired.items())
        report_loc.update(desired)
    if state.get("PostCrayburnReport") is not True:
        state["PostCrayburnReport"] = True
        changed = True
    if state.get("ALoc") is not None or state.get("CurState") != "EXPLORE":
        state["ALoc"] = None
        state["CurState"] = "EXPLORE"
        changed = True
    return changed


def _activate_az1_area(db, champ_id):
    """Create/activate the AZ1 overworld campaign at Into The Woods."""
    row = db.execute(
        "SELECT id, camp_uid_lo, camp_uid_hi, state_json FROM campaigns "
        "WHERE champion_id=? AND campaign_type='AREA' AND template_name='AZ1' "
        "ORDER BY id DESC LIMIT 1", (champ_id,)).fetchone()
    if row:
        cid, lo, hi, raw = row
        state = json.loads(raw) if raw else {}
    else:
        cid, lo, hi = _new_camp_id(db), _generate_inst_id(), 0
        state = {}
        champ = _get_champion(db, champ_id)
        user_id = champ[1] if champ else 0
        champ_name = champ[3] if champ else ""
        db.execute("INSERT INTO campaigns (id,camp_uid_lo,camp_uid_hi,champion_id,user_id,"
                   "champion_name,template_name,campaign_type,is_started,state_json) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (cid,lo,hi,champ_id,user_id,champ_name,"AZ1","AREA",1,"{}"))
    champ = _get_champion(db, champ_id)
    area_locs, area_nodes = _az1_area_scene_state(champ[2] if champ else None)
    # Resolve every authored AZ1 encounter by its NODE number.  The map node
    # owns the encounter reference; scene metadata supplies the GUID,
    # rewards, and scene type (including all Shroom Haus locations).
    scenes = db.execute(
        "SELECT guid, name, rewards_json FROM encounter_scenes "
        "WHERE name LIKE 'AZ 1 - NODE %'"
    ).fetchall()
    _hydrate_az1_area_scene_metadata(db, area_locs, champ_id=champ_id,
                                     state={"VisLocs": area_locs,
                                            "PublicState": {"Data": {}}})
    # Location.Encounter is a name/reference; the client then looks it up in
    # GameplayState.Encounters to obtain the scene GUID. Keep this catalog
    # derived from the authored node references and encounter_scenes table.
    area_encounters = []
    for loc in area_locs:
        guid = (loc.get("Data") or {}).get("encounter")
        if guid and db.execute(
                "SELECT 1 FROM encounter_scenes WHERE guid=?", (guid,)
        ).fetchone():
            if guid not in {x["Name"] for x in area_encounters}:
                area_encounters.append({"Name": guid, "Data": {"encscene": guid}})
    state.update({"CampID": cid, "ChampID": champ_id, "TempType": "AREA",
                  # ALoc is reserved for an active location.  Keeping it
                  # empty at map entry lets the client select/move from the
                  # starting node; LastNode identifies the starting tile.
                  "ALoc": None, "LastNode": "Node001",
                  "CurState": "EXPLORE", "Finished": None,
                  "PublicState": {"Data": {"CampaignGroup": "AREA",
                                             "visited_nodes": ["Node001"],
                                             "visited_paths": [],
                                             # Filled from active quest
                                             # objectives below.  Do not bake
                                             # a node number into the area
                                             # campaign; quest encounter GUIDs
                                             # are the source of truth.
                                             "quest_nodes": []}},
                  "PayGroups": [], "CSlide": None,
                  "Encounters": area_encounters,
                  "Champions": [],
                  "Started": _now_utc(), "FinishReason": None, "Wins": 0, "Losses": 0,
                  "Score": 0, "HealthAdj": 0, "DungeonLifeAdj": 0, "Flags": {},
                  "VisLocs": area_locs, "LocNodes": area_nodes})
    _sync_az1_quest_gates(db, champ_id, state)
    _apply_az1_quest_markers(db, champ_id, state)
    _az1_reveal_neighbors(db, state, state.get("LastNode") or "Node001")
    db.execute("UPDATE campaigns SET is_started=1,state_json=? WHERE id=?",
               (json.dumps(state), cid)); db.commit()
    return cid, state


def _az1_scene_for_node(db, node):
    """Resolve the authored AZ1 scene for a map node.

    Scene names are authored as ``AZ 1 - NODE NN - ...``.  The map state uses
    the same node identifier, so this lookup is shared by newly-created and
    older persisted area campaigns.
    """
    match = re.search(r"NODE[_ ]?0*(\d+)", str(node or ""), re.I)
    if not match:
        return None
    number = int(match.group(1))
    rows = db.execute(
        "SELECT guid, name, rewards_json FROM encounter_scenes "
        "WHERE name LIKE 'AZ 1 - NODE %'"
    ).fetchall()
    return next((row for row in rows
                 if re.search(r"NODE[_ ]?0*%d\b" % number,
                              row[1] or "", re.I)), None)


def _az1_node_conversation_rows(db, node):
    """Return authored conversations for an AZ1 node with decoded triggers."""
    try:
        rows = db.execute(
            "SELECT conversation_guid, trigger_json, priority "
            "FROM campaign_node_conversations "
            "WHERE campaign_template='AZ1' AND node_id=? AND enabled=1 "
            "ORDER BY priority, conversation_guid", (str(node),)).fetchall()
    except Exception:
        return []
    result = []
    for guid, raw_trigger, priority in rows:
        try:
            trigger = json.loads(raw_trigger or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            trigger = {}
        result.append((guid, trigger if isinstance(trigger, dict) else {}, priority))
    return result


def _az1_node_conversation(db, node, state=None):
    """Select the authored conversation for the node's current visit.

    SceneData can provide a first-visit conversation, a repeat conversation,
    and a state-specific variant (for example Milosh's "already has fortune"
    text). The selection is based on persisted area state, not a node-specific
    GUID in campaign.py.
    """
    rows = _az1_node_conversation_rows(db, node)
    if not rows:
        return None
    data = ((state or {}).get("PublicState", {}) or {}).get("Data", {}) or {}
    visits = data.get("conversation_visits") or {}
    try:
        visit_count = int(visits.get(str(node), 0) or 0)
    except (TypeError, ValueError):
        visit_count = 0
    has_fortune = bool(data.get("gaal_fortune"))
    # A state-qualified variant must win over an ordinary first/repeat
    # conversation.  Some authored records carry both predicates (for
    # example "First encounter - Player already has fortune"), while others
    # use only ``state=fortune`` for the post-reading branch.  Match the visit
    # qualifier when present, but allow an unqualified fortune variant to be
    # the fallback for either visit count.
    if has_fortune:
        for guid, trigger, _priority in rows:
            if str(trigger.get("state") or "").lower() != "fortune":
                continue
            visit = str(trigger.get("visit") or "").lower()
            if ((visit_count <= 0 and visit == "first") or
                    (visit_count > 0 and visit == "repeat")):
                return guid
        for guid, trigger, _priority in rows:
            if (str(trigger.get("state") or "").lower() == "fortune" and
                    not str(trigger.get("visit") or "").strip()):
                return guid
    if visit_count <= 0:
        for guid, trigger, _priority in rows:
            if (str(trigger.get("visit") or "").lower() == "first" and
                    str(trigger.get("state") or "").strip() == ""):
                return guid
    if visit_count > 0:
        for guid, trigger, _priority in rows:
            if str(trigger.get("visit") or "").lower() == "repeat":
                return guid
    fallback = None
    for guid, trigger, _priority in rows:
        if fallback is None:
            fallback = guid
    return fallback


def _az1_node_is_repeatable(db, node):
    """Whether authored node conversations include a repeat/state variant."""
    return any(
        str(trigger.get("visit") or "").lower() == "repeat" or
        bool(str(trigger.get("state") or "").strip())
        for _guid, trigger, _priority in _az1_node_conversation_rows(db, node)
    )


def _az1_pre_encounter_conversation(db, node, champ_id=None):
    """Select an authored pre-battle conversation for an AZ1 node.

    Encounter scenes and their opening narration are authored separately in
    the client data.  A scene name does not always contain ``DIALOG`` (for
    example Node009's Cockatwice scene), so use the conversation catalog to
    identify a first-visit prelude instead of maintaining a node allow-list.
    Outcome and quest-turn-in conversations are deliberately excluded; those
    are selected by their respective result/quest flows.
    """
    rows = _az1_node_conversation_rows(db, node)
    candidates = []
    for guid, trigger, _priority in rows:
        name = str(trigger.get("label") or "").lower()
        if trigger.get("outcome"):
            continue
        if any(term in name for term in (
                "quest start", "quest end", "quest not complete",
                "quest completed", "completed repeating", "repeat encounter",
                "repeating until", "success", "fail")):
            continue
        if str(trigger.get("visit") or "").lower() == "repeat":
            continue
        candidates.append((guid, trigger, name))
    if not candidates:
        return None

    # Prefer an explicitly authored first/step-one conversation.  For
    # faction branches, prefer the branch matching the champion's faction.
    faction = _quest_faction_for_champion(db, champ_id) if champ_id else None
    scored = []
    for guid, trigger, name in candidates:
        score = 0
        visit = str(trigger.get("visit") or "").lower()
        if visit == "first":
            score += 100
        step = re.search(r"\bstep\s*([0-9]+)", name)
        if step:
            score += max(1, 20 - int(step.group(1)))
        if "first encounter" in name or "first visit" in name:
            score += 20
        if any(term in name for term in (
                "revert", "player has", "player did", "installed",
                "no gnomes", "wormoid queen", "surfaces", "tunnels")):
            score -= 40
        if "ardent" in name or "underworld" in name:
            if faction and str(faction).lower() in name:
                score += 25
            else:
                score -= 25
        scored.append((score, guid))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][1]


def _hydrate_az1_area_scene_metadata(db, locations, champ_id=None, state=None):
    """Apply authored scene type/conversation data to AZ1 map locations.

    Older area campaigns were created before encounter scenes were seeded and
    therefore retained ``Empty`` locations with no scene GUID.  Hydrating on
    every response repairs those states and, importantly, makes a DIALOG scene
    (such as Node007's Gaal Camp) a conversation instead of a Shroom Haus.
    """
    for loc in locations or []:
        data = loc.get("Data") or {}
        node = data.get("node") or ""
        scene = _az1_scene_for_node(db, node)
        if not scene:
            continue
        guid, name, rewards_json = scene
        data["encounter"] = guid
        upper_name = str(name or "").upper()
        if "SHROOM HAUS" in upper_name:
            data["type"] = "ShroomHaus"
            try:
                choices = [x["guid"] for x in
                           (json.loads(rewards_json or "{}").get("card_choice") or [])
                           if isinstance(x, dict) and x.get("guid")]
                if choices:
                    data["choices"] = choices
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        elif "DIALOG" in upper_name:
            data["type"] = "Convo"
            data["conversationId"] = (data.get("conversationId") or
                                       _az1_node_conversation(db, node))
            data["repeatable"] = _az1_node_is_repeatable(db, node)
            # The map token starts a conversation when it reaches an
            # unfinished node. Without AutoStart the client only records the
            # path and leaves the player at the prior location. Repeatable
            # conversations intentionally remain unfinished so they can be
            # entered again. After a completed visit, StartLoc re-enables the
            # flag when the player deliberately returns to this node; keeping
            # it off here prevents the client from reopening the conversation
            # immediately after conv_done (or after a reconnect).
            visits = (((state or {}).get("PublicState", {}) or {}).get("Data", {})
                      or {}).get("conversation_visits", {})
            try:
                visit_count = int(visits.get(str(node), 0) or 0)
            except (TypeError, ValueError):
                visit_count = 0
            data["autostart"] = (not bool(data.get("completed")) and
                                 visit_count <= 0)
        elif str(data.get("type") or "").lower() in {"", "empty", "encounter"}:
            # Some battle scenes have an authored opening conversation but do
            # not carry ``DIALOG`` in their scene name (for example Node009's
            # Cockatwice prelude). Bind that conversation before exposing the
            # battle. The marker lets conv_done promote the same location back
            # to Encounter without a node-specific special case.
            prelude = (None if "PANORAMA" in upper_name else
                       _az1_pre_encounter_conversation(db, node, champ_id))
            if (prelude and not data.get("pre_encounter_completed") and
                    not data.get("completed")):
                data.update({
                    "type": "Convo",
                    "conversationId": prelude,
                    "pre_encounter": True,
                    "repeatable": False,
                    "autostart": (not bool(data.get("completed"))),
                })
            else:
                # Battles and authored dungeon/panorama scenes are actionable
                # map encounters. Preserve explicit Convo/ShroomHaus overrides.
                data["type"] = "Encounter"
                if data.get("completed"):
                    data["autostart"] = False
    if champ_id is not None and state is not None:
        _sync_az1_quest_gates(db, champ_id, state)
        _apply_az1_quest_markers(db, champ_id, state)


def _az1_area_scene_state(champion_race=None, rewards_json=None):
    """Build client locations from the authored Howling Plains SceneData."""
    try:
        raw = Path(__file__).parent.joinpath("Records", "SceneData.jsonl").read_text()
        record = yaml.safe_load(json.loads(raw.splitlines()[4])) if yaml else None
        items = (record or {}).get("m_ItemData", [])
    except Exception:
        items = []
    if not items:
        items = [{"m_MapNodeId": "Node001", "m_Name": "001 - Into the Woods",
                  "m_Title": "Into The Woods", "m_Descriptions": [],
                  "m_TotemType": "None"}]
    locs, nodes = [], []
    for i, item in enumerate(items):
        node = item.get("m_MapNodeId") or f"Node{i+1:03d}"
        title = item.get("m_Title") or item.get("m_Name") or node
        desc = next((d.get("m_Description", "") for d in item.get("m_Descriptions", [])
                     if d.get("m_Type") == "SingleClick"), "")
        # Keep the authored starting node and its immediate map neighbors
        # available; the map prefab supplies the connecting lines.
        visible = i < 2
        locs.append({"Data": {"name": title, "node": node, "type": "Empty",
            "autostart": False, "autopan": False, "autotrigger": False,
            "battle": None, "visible": visible, "enabled": visible,
            "completed": (node == "Node001"), "repeatable": False, "givequest": False,
            "turninquest": False, "impassable": False, "unknown": False,
            "encounter": None, "encounter_desc": desc, "description": desc,
            "title": title, "conversationId": None}})
        nodes.append({"Name": node, "Data": {"id": node, "type": "DEFAULT"}})
    for loc in locs:
        if loc["Data"]["node"] == "Node002":
            loc["Data"].update({"type": "Convo", "conversationId": None,
                                 "givequest": True})
        elif loc["Data"]["node"] == "Node004":
            loc["Data"].update({"type": "ShroomHaus",
                                 # Card choices are filled from the matching
                                 # encounter_scenes.rewards_json record when
                                 # the area state is activated.  Do not bake
                                 # a particular Haus's cards into campaign
                                 # logic; AZ1 has several distinct locations.
                                 "choices": [],
                                 "selection-type": "Shroomkin_Haus",
                                 "selection-desc": "Shroomkin_Instructions"})
    return locs, nodes


def _az1_neighbors(db, node):
    """Return AZ1 node IDs directly connected to *node* in the map graph."""
    if not node:
        return set()
    rows = db.execute(
        "SELECT to_node FROM campaign_node_edges "
        "WHERE campaign_template='AZ1' AND from_node=?",
        (str(node),),
    ).fetchall()
    return {str(row[0]) for row in rows if row and row[0]}


def _az1_is_adjacent(db, start_node, end_node):
    """Whether two AZ1 nodes share an authored map path."""
    if not start_node or not end_node or start_node == end_node:
        return start_node == end_node
    return bool(db.execute(
        "SELECT 1 FROM campaign_node_edges "
        "WHERE campaign_template='AZ1' AND from_node=? AND to_node=?",
        (str(start_node), str(end_node)),
    ).fetchone())


def _az1_reveal_neighbors(db, state, current_node=None):
    """Reveal the current AZ1 node and its graph neighbours.

    The client owns the static NodesPrefab (positions, paths, and FOW
    visuals), while this state controls which of those nodes are known. Keep
    every visited/completed node visible and reveal only the current node's
    actual adjacent locations for new exploration.
    """
    if not isinstance(state, dict):
        return
    current = _resolve_node(
        state, current_node or state.get("ALoc") or state.get("LastNode"))
    pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
    visited = {
        _resolve_node(state, str(node))
        for node in (pdata.get("visited_nodes") or [])
        if node
    }
    if current:
        visited.add(current)
    # Revealed map space is cumulative.  Keep the neighbours of every node
    # the champion has visited, not only the node occupied on the latest
    # response; otherwise travelling from Dunnwood to a side location would
    # hide the still-discovered Node007 branch again.
    reveal = set(visited)
    for node in visited:
        reveal.update(_az1_neighbors(db, node))
    if current:
        reveal.update(_az1_neighbors(db, current))
    blocked = {
        _resolve_node(state, str(node))
        for node in (pdata.get("blocked_nodes") or [])
        if node
    }
    for loc in state.get("VisLocs", []):
        data = loc.get("Data") or {}
        node = data.get("node")
        if not node:
            continue
        # A quest gate can hide an authored neighbour even when the static
        # map graph says it is adjacent.  A previously visited node remains
        # visible so reconnects never erase discovered map space.
        known = ((node in reveal and node not in blocked) or
                 node in visited or bool(data.get("completed")))
        data["visible"] = known
        data["enabled"] = known


def _az1_set_node_gate(state, node, blocked):
    """Add/remove a quest-controlled AZ1 map gate idempotently."""
    pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
    values = [str(value) for value in (pdata.get("blocked_nodes") or []) if value]
    unlocked = [str(value) for value in (pdata.get("unlocked_nodes") or []) if value]
    node = str(node)
    if blocked and node not in values:
        if node not in unlocked:
            values.append(node)
    elif not blocked:
        values = [value for value in values if value != node]
        if node not in unlocked:
            unlocked.append(node)
    pdata["blocked_nodes"] = values
    pdata["unlocked_nodes"] = unlocked


def _quest_state_row(db, champ_id, quest_script):
    row = db.execute(
        "SELECT id, state_json FROM campaigns "
        "WHERE champion_id=? AND campaign_type='QUEST' AND template_name=? "
        "ORDER BY id DESC LIMIT 1", (champ_id, quest_script)).fetchone()
    if not row:
        return None, None
    try:
        return row[0], json.loads(row[1] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return row[0], {}


def _quest_is_turnin_ready(state):
    """Whether a quest's current objective is its final conversation."""
    if not isinstance(state, dict) or state.get("Finished"):
        return False
    flags = state.get("Flags") or {}
    objectives = flags.get("_quest_objectives") or []
    try:
        index = int(flags.get("_quest_objective_idx", 0))
    except (TypeError, ValueError):
        index = 0
    if index >= len(objectives):
        return False
    return str(objectives[index].get("type") or "").lower() in {
        "conversation", "convo"
    } and index == len(objectives) - 1


def _quest_faction_for_champion(db, champ_id):
    champ = _get_champion(db, champ_id)
    return _RACE_FACTIONS.get(champ[2], "Ardent") if champ else "Ardent"


def _quest_row_matches_faction(row_faction, champion_faction):
    return not row_faction or str(row_faction).lower() in {
        str(champion_faction).lower(), "all"
    }


def _quest_hook_az1_tamed_start(db, champ_id, state):
    """Tamed quest: keep the Fonferek branch closed until unlocked."""
    before = json.dumps(state, sort_keys=True)
    _az1_set_node_gate(state, "Node005", True)
    _az1_reveal_neighbors(db, state, state.get("LastNode"))
    return json.dumps(state, sort_keys=True) != before


def _quest_hook_az1_find_horwich_sea_start(db, champ_id, state):
    """Find Horwich Sea: open the previously blocked Fonferek branch."""
    before = json.dumps(state, sort_keys=True)
    _az1_set_node_gate(state, "Node005", False)
    _az1_reveal_neighbors(db, state, state.get("LastNode"))
    return json.dumps(state, sort_keys=True) != before


_QUEST_START_HOOKS = {
    "az1_tamed_start": _quest_hook_az1_tamed_start,
    "az1_find_horwich_sea_start": _quest_hook_az1_find_horwich_sea_start,
    # Backwards-compatible alias for databases seeded by the earlier schema.
    "az1_sea_witch_start": _quest_hook_az1_find_horwich_sea_start,
}


def _run_quest_start_hook(db, champ_id, hook_name):
    """Run a named, whitelisted metadata hook against the champion's AZ1 area."""
    hook = _QUEST_START_HOOKS.get(str(hook_name or ""))
    if hook is None:
        return False
    row = db.execute(
        "SELECT id, state_json FROM campaigns WHERE champion_id=? "
        "AND campaign_type='AREA' AND template_name='AZ1' "
        "ORDER BY id DESC LIMIT 1", (champ_id,)).fetchone()
    if not row:
        return False
    try:
        state = json.loads(row[1] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    changed = bool(hook(db, champ_id, state))
    if changed:
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), row[0]))
        db.commit()
    return changed


def _sync_az1_quest_gates(db, champ_id, state):
    """Reapply active quest start hooks after reconnects or state rebuilds.

    The quest script is deliberately not interpreted here.  Each active quest
    contributes the named function stored in ``quest_templates.start_hook``;
    hooks are replayed in grant order so a later unlock hook can override an
    earlier blocking hook while the persisted ``unlocked_nodes`` flag keeps an
    unlocked path open on future loads.
    """
    if not isinstance(state, dict):
        return False
    before = json.dumps(state, sort_keys=True)
    hooks = db.execute(
        "SELECT c.id, qt.start_hook FROM campaigns c "
        "JOIN quest_templates qt ON qt.script_name=c.template_name "
        "WHERE c.champion_id=? AND c.campaign_type='QUEST' "
        "AND c.state_json IS NOT NULL AND c.is_started=1 "
        "AND qt.enabled=1 AND qt.start_hook<>'' ORDER BY c.id",
        (champ_id,)).fetchall()
    for _quest_id, hook_name in hooks:
        try:
            hook_state = json.loads(
                db.execute("SELECT state_json FROM campaigns WHERE id=?",
                           (_quest_id,)).fetchone()[0] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if hook_state.get("Finished"):
            continue
        hook = _QUEST_START_HOOKS.get(str(hook_name or ""))
        if hook:
            hook(db, champ_id, state)
    _az1_reveal_neighbors(db, state, state.get("LastNode") or state.get("ALoc"))
    return json.dumps(state, sort_keys=True) != before


def _grant_quests_for_conversation(db, champ_id, campaign_template,
                                   conversation_guid):
    """Grant all eligible quest-start rows for a completed conversation."""
    if not conversation_guid:
        return [], []
    faction = _quest_faction_for_champion(db, champ_id)
    rows = db.execute(
        "SELECT qc.quest_script, COALESCE(NULLIF(qt.start_hook, ''), "
        "NULLIF(qc.start_hook, ''), ''), qc.faction, "
        "COALESCE(qt.campaign_group, 'AREA') FROM quest_conversations qc "
        "LEFT JOIN quest_templates qt ON qt.script_name=qc.quest_script "
        "WHERE qc.conversation_guid=? AND qc.campaign_template=? "
        "AND qc.role='start' AND qc.enabled=1 ORDER BY qc.priority, qc.quest_script",
        (str(conversation_guid), str(campaign_template))).fetchall()
    spawned, hooks = [], []
    for script, hook, row_faction, campaign_group in rows:
        if not _quest_row_matches_faction(row_faction, faction):
            continue
        existing_id, _existing_state = _quest_state_row(db, champ_id, script)
        quest_id = _ensure_quest_campaign(db, champ_id, campaign_group, script)
        if not quest_id:
            continue
        if existing_id is None:
            qrow = db.execute(
                "SELECT state_json FROM campaigns WHERE id=?", (quest_id,)
            ).fetchone()
            if qrow and qrow[0]:
                spawned.append((quest_id, script, json.loads(qrow[0])))
            if hook:
                hooks.append(hook)
    for hook in hooks:
        _run_quest_start_hook(db, champ_id, hook)
    return spawned, hooks


def _apply_az1_quest_markers(db, champ_id, state):
    """Apply authored quest flags and objective markers to an AZ1 area.

    The client expects ``PublicState.Data.quest_nodes`` to contain map node
    IDs.  Quest metadata, however, identifies encounter objectives by scene
    GUID (and conversation objectives by conversation GUID), so resolve those
    references against the hydrated area locations at runtime.  This keeps
    quest markers correct for every authored quest and avoids node-specific
    campaign code.  All incomplete objectives are marked; undiscovered ones
    remain hidden by the normal fog-of-war visibility state and acquire their
    marker when the node is revealed.
    """
    if not isinstance(state, dict):
        return False
    faction = _quest_faction_for_champion(db, champ_id)
    changed = False

    # Resolve authored objective references to actual AZ1 map nodes.  The
    # area location owns the encounter/conversation GUID after hydration.
    encounter_nodes = {}
    conversation_nodes = {}
    for loc in state.get("VisLocs", []):
        data = loc.setdefault("Data", {})
        node = data.get("node") or data.get("name")
        if not node or not str(node).lower().startswith("node"):
            continue
        encounter = data.get("encounter")
        if encounter:
            encounter_nodes.setdefault(str(encounter), set()).add(str(node))
        conversation = data.get("conversationId")
        if conversation:
            conversation_nodes.setdefault(str(conversation), set()).add(str(node))

    quest_nodes = set()
    quest_rows = db.execute(
        "SELECT template_name, state_json FROM campaigns "
        "WHERE champion_id=? AND campaign_type='QUEST' "
        "AND is_started=1 AND state_json IS NOT NULL",
        (champ_id,),
    ).fetchall()
    for quest_script, raw_state in quest_rows:
        try:
            quest_state = json.loads(raw_state or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(quest_state, dict) or quest_state.get("Finished"):
            continue
        flags = quest_state.get("Flags") or {}
        objectives = flags.get("_quest_objectives") or []
        completed = {
            str((loc.get("Data") or {}).get("node") or
                (loc.get("Data") or {}).get("name"))
            for loc in quest_state.get("VisLocs", [])
            if (loc.get("Data") or {}).get("completed")
        }
        for objective in objectives:
            if not isinstance(objective, dict):
                continue
            objective_id = str(objective.get("id") or "")
            if objective_id and objective_id in completed:
                continue
            encounter = objective.get("encounter")
            if encounter:
                quest_nodes.update(encounter_nodes.get(str(encounter), set()))
            for conversation in objective.get("conversation_ids") or []:
                quest_nodes.update(conversation_nodes.get(str(conversation), set()))
            conversation = objective.get("conversation")
            if conversation:
                quest_nodes.update(conversation_nodes.get(str(conversation), set()))

    pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
    # Preserve map order so the payload remains stable for clients and tests.
    ordered_quest_nodes = [
        str((loc.get("Data") or {}).get("node"))
        for loc in state.get("VisLocs", [])
        if str((loc.get("Data") or {}).get("node")) in quest_nodes
    ]
    if pdata.get("quest_nodes") != ordered_quest_nodes:
        pdata["quest_nodes"] = ordered_quest_nodes
        changed = True

    for loc in state.get("VisLocs", []):
        data = loc.setdefault("Data", {})
        node = data.get("node") or data.get("name")
        if not node or str(node).lower().startswith("node") is False:
            continue
        rows = db.execute(
            "SELECT quest_script, conversation_guid, role, faction "
            "FROM quest_conversations WHERE campaign_template='AZ1' "
            "AND node_id=? AND enabled=1 ORDER BY priority, conversation_guid",
            (str(node),)).fetchall()
        give = False
        turnin = False
        selected = None
        fallback = None
        for script, guid, role, row_faction in rows:
            if not _quest_row_matches_faction(row_faction, faction):
                continue
            if fallback is None:
                fallback = guid
            _qid, qstate = _quest_state_row(db, champ_id, script)
            if role == "start" and qstate is None:
                give = True
                selected = selected or guid
            elif role == "complete" and _quest_is_turnin_ready(qstate):
                turnin = True
                selected = guid
            elif role == "not_complete" and qstate and not qstate.get("Finished"):
                selected = selected or guid
        if selected is None:
            selected = fallback
        if selected and data.get("conversationId") != selected:
            data["conversationId"] = selected
            changed = True
        if bool(data.get("givequest")) != give:
            data["givequest"] = give
            changed = True
        if bool(data.get("turninquest")) != turnin:
            data["turninquest"] = turnin
            changed = True
    return changed


def _normalize_starter_panorama_state(state, cfg):
    """Repair client-facing NPC metadata in an existing starter panorama.

    Early campaign states did not include the trainer's champion template ID
    and marked actionable NPCs as not giving a quest.  The panorama client
    uses the former for the profile portrait (especially once a conversation
    has become an encounter) and the latter for the quest marker, so repair
    those fields whenever an older state is returned.
    """
    # A completed dungeon hands off to the panorama with a report NPC.  Do
    # not rebuild that intentionally post-dungeon state as a fresh tutorial.
    if state.get("PostCrayburnReport"):
        return False
    changed = False
    trainer_npc = cfg["trainer_npc"]
    quest_npc = cfg["quest_npc"]
    trainer_guid = cfg.get("ai_champion_guid")
    intro_completed = False
    trainer_data = None
    quest_data = None

    for node in state.setdefault("LocNodes", []):
        data = node.setdefault("Data", {})
        node_id = data.get("id") or node.get("Name")
        if node_id == trainer_npc and trainer_guid and data.get("championId") != trainer_guid:
            data["championId"] = trainer_guid
            changed = True

    for loc in state.get("VisLocs", []):
        data = loc.setdefault("Data", {})
        node_id = data.get("node") or data.get("name")
        if node_id == cfg["intro_npc"]:
            intro_completed = bool(data.get("completed"))
            if intro_completed and data.get("autostart"):
                data["autostart"] = False
                changed = True
        elif node_id == trainer_npc:
            trainer_data = data
        elif node_id == quest_npc:
            quest_data = data
        if node_id in (trainer_npc, quest_npc) and not data.get("completed"):
            if not data.get("givequest"):
                data["givequest"] = True
                changed = True

    # Recover a starter panorama after a rejected NPC StartLoc.  The old
    # template-name-only AZ1 movement gate could leave the intro completed
    # while hiding the trainer again; restore the next authored conversation
    # when no later quest-giver or victory transition exists.
    if (intro_completed and trainer_npc and not quest_data and
            not state.get("TrainingVictoryPending")):
        if trainer_data is None:
            state.setdefault("VisLocs", []).append(
                _convo_location(trainer_npc, cfg["battle_conv"], givequest=True))
        else:
            trainer_data.update({
                "type": "Convo", "conversationId": cfg["battle_conv"],
                "encounter": None, "completed": False,
                "enabled": True, "visible": True, "givequest": True,
                "autostart": False,
            })
        changed = True
    return changed


def _training_location(cfg):
    return {
        "Data": {
            "name": cfg["training_node"], "node": cfg["training_node"], "type": "Encounter",
            "autostart": False, "autopan": False, "autotrigger": False,
            "battle": None, "completed": False, "enabled": True, "visible": True,
            "repeatable": False, "givequest": False, "turninquest": False,
            "impassable": False, "unknown": False,
            "encounter": cfg["training_encounter"], "encounter_desc": None,
            "allow_cancel": False, "conversationId": None,
        }
    }


def _mark_location_completed(state, name_or_node):
    """Mark a VisLoc as completed by matching node or name."""
    for loc in state.get("VisLocs", []):
        data = loc.get("Data", {})
        if data.get("node") == name_or_node or data.get("name") == name_or_node:
            data["completed"] = True
            return True
    return False


def _transition_to_dungeon(state, cfg):
    """Switch the AZ0 panorama campaign to the Castle Crayburn dungeon.

    Called when the quest-giver's quest-start conversation ends. Replaces the
    current GameplayState in-place with the Crayburn Castle state (7 nodes with
    real encounter GUIDs + the race's per-node conversations). The campaign
    row's type is switched by the caller so getcampsum/getactive return the
    matching TypeInfo.
    """
    # The campaign presents as a DUNGEON (Castle Crayburn map). (A PANORAMA
    # experiment to dodge the client's missing 'DungeonMapNode' script didn't
    # render, so we stay DUNGEON.)
    dungeon = _build_initial_gameplay_state(
        state.get("CampID"), state.get("ChampID"), "DUNGEON",
        _race_for_cfg(cfg))
    state.clear()
    state.update(dungeon)
    # Preserve started-ness so the client treats this as in-progress.
    state["Started"] = state.get("Started") or _now_utc()


def _now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Dungeon-template name resolution (fuzzy-matches typos like "Cragburn")
# ---------------------------------------------------------------------------
# Lowercase name fragments that identify a campaign as a dungeon.  The
# client's camp.dungeon console command passes whatever the user typed, and
# typos ("cragburn" for "crayburn") are common.  `_is_known_dungeon` does a
# simple substring-overlap match so one-char errors still resolve to DUNGEON.
_DUNGEON_NAME_HINTS = {"crayburn", "castle", "cragburn", "crayborn"}

def _is_known_dungeon(template_name):
    """Fuzzy-match the template against known dungeon name hints."""
    tname = (template_name or "").lower()
    if not tname:
        return False
    # Literal hits.
    overlap = len(set(tname.split()) & _DUNGEON_NAME_HINTS)
    if overlap > 0:
        return True
    # Substring match (e.g. "cragburn" ≈ "crayburn" — share 6/8 chars).
    for hint in _DUNGEON_NAME_HINTS:
        if len(hint) <= 3:
            continue
        # Count shared unique characters.
        shared = len(set(hint) & set(tname))
        if shared >= len(set(hint)) * 0.6:
            return True
    return False


# ---------------------------------------------------------------------------
# Castle Crayburn dungeon — server-driven chain
# ---------------------------------------------------------------------------
# Ordered castle nodes (must match _build_initial_gameplay_state).
_CASTLE_CHAIN = ["Entrance", "WatchTower", "Drawbridge", "CastleGate",
                 "InnerBailey", "TowerGate", "PenworthTower"]
_CASTLE_NODE_CONV = {
    "Entrance": None,
    "WatchTower": "The Watchtower",
    "Drawbridge": "The Drawbridge",
    "CastleGate": "Castle Gatehouse",
    "InnerBailey": "Inner Bailey",
    "TowerGate": "Tower Gatehouse",
    "PenworthTower": "Tower of Penworth",
}

def _crayburn_node_data(race_name, node):
    """Return the per-node conversation/encounter dict from gamedata, or {}."""
    conv_name = _CASTLE_NODE_CONV.get(node)
    if not conv_name:
        return {}
    return _CRAYBURN_CASTLE["races"].get(race_name or "Necrotic", {}) \
        .get("nodes", {}).get(conv_name, {})


def _crayburn_node_is_encounter(race_name, node):
    """True if the castle node fires a battle (race data has success+fail
    conversations — the data-driven discriminator)."""
    nd = _crayburn_node_data(race_name, node)
    if not nd:
        return False
    return "success" in nd and "fail" in nd


def _crayburn_node_is_conversation(race_name, node):
    """True if the node has a conversation but NOT a battle encounter."""
    nd = _crayburn_node_data(race_name, node)
    return bool(nd.get("conv")) and not _crayburn_node_is_encounter(race_name, node)

def _is_location_completed(state, node):
    for loc in state.get("VisLocs", []):
        d = loc.get("Data", {})
        if d.get("node") == node or d.get("name") == node:
            return bool(d.get("completed", False))
    return False

def _node_name(state, node_id):
    """Return the VisLoc `name` (display name) for a node id, or the id."""
    for loc in state.get("VisLocs", []):
        d = loc.get("Data", {})
        if d.get("node") == node_id:
            return d.get("name", node_id)
    return node_id

def _resolve_node(state, key):
    """Map a VisLoc name or node id back to the canonical node id."""
    if key in _CASTLE_CHAIN:
        return key
    for loc in state.get("VisLocs", []):
        d = loc.get("Data", {})
        if d.get("name") == key:
            return d.get("node") or key
    return key

def _note_visited(state, node):
    pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
    visited = pdata.setdefault("visited_nodes", [])
    if node not in visited:
        visited.append(node)

def _set_crayburn_autostart(state, node):
    """Mark a VisLoc for auto-trigger (no force-move warp — the client's
    dungeon VM auto-plays conversations when ALoc changes to a Convo node)."""
    for loc in state.get("VisLocs", []):
        d = loc.get("Data", {})
        if d.get("node") == node:
            d["autostart"] = True

def _reveal_crayburn_node(state, node):
    """Uncover a castle node + its immediate next neighbour on the map."""
    chain = _CASTLE_CHAIN
    try:
        idx = chain.index(node)
    except ValueError:
        return
    for n in chain[idx:idx + 2]:
        for loc in state.get("VisLocs", []):
            d = loc.get("Data", {})
            if d.get("node") == n:
                d["visible"] = True
                d["enabled"] = True

def _advance_crayburn(state, race_name, from_node, won):
    """Advance the Castle Crayburn dungeon chain from `from_node`.

    Returns the next step for the caller to act on:
      ("conv", node)      — a conversation-only node: ALoc is set to the
                            node so the client launches the conversation from
                            the cmpupdate state.
      ("encounter", guid) — an ENCOUNTER node: ALoc is set; launch the battle.
      ("arrival_conv", node) — StartLoc received at a conversation node;
                               ALoc is set so the client auto-plays the conv.
      None                — the castle is beaten.

    Pass-through nodes (e.g. the Entrance, no conversation and no encounter)
    are auto-completed. On `won` the just-finished `from_node` is marked
    completed first; on a loss it stays uncompleted so the same step retries.
    """
    chain = _CASTLE_CHAIN
    if from_node:
        from_node = _resolve_node(state, from_node)
    try:
        start = chain.index(from_node) if from_node else 0
    except ValueError:
        start = 0
    if won and from_node in chain:
        _mark_location_completed(state, from_node)
        _note_visited(state, from_node)
        state["ALoc"] = ""  # clear ALoc so client doesn't chase completed node
    for node in chain[start:]:
        if _is_location_completed(state, node):
            continue
        if _crayburn_node_is_encounter(race_name, node):
            state["ALoc"] = node
            state["LastNode"] = node
            state["CurState"] = "EXPLORE"
            _reveal_crayburn_node(state, node)
            _set_crayburn_autostart(state, node)
            return ("encounter", _crayburn_scene_for_node(race_name, node))
        nd = _crayburn_node_data(race_name, node)
        if nd.get("conv"):
            # Activate the conversation in the state sent to the client.  The
            # dungeon VM only launches a conversation when ALoc matches the
            # current location; a private pending marker is not consumed by
            # the client during a campaign transition.
            state.pop("_pending_travel", None)
            state["ALoc"] = node
            state["LastNode"] = node
            state["CurState"] = "EXPLORE"
            _reveal_crayburn_node(state, node)
            _set_crayburn_autostart(state, node)
            return ("conv", node)
        # Pass-through node (no conversation, no encounter) — auto-complete.
        _mark_location_completed(state, node)
        _note_visited(state, node)
    # Reached the end of the chain — the castle is beaten.
    state["Finished"] = _now_utc()
    state["FinishReason"] = "Complete"
    state["ALoc"] = ""
    state["CurState"] = "EXPLORE"
    return None

def _race_name_for_campaign(db, camp_id):
    row = db.execute(
        "SELECT ch.race FROM campaigns c JOIN champions ch ON c.champion_id=ch.id "
        "WHERE c.id=?", (camp_id,)).fetchone()
    if not row:
        return "Necrotic"
    return _RACE_NAMES.get(row[0], "Necrotic")

def advance_crayburn_step(handler, db, camp_id, won, comp, session_id,
                          target, instance, conh, uid, auto_activate=True):
    """Advance the Castle Crayburn dungeon by one step.

    Resolves everything from the campaign row, persists the updated state, then
    acts on the returned step: a ("conv") step pushes a cmpupdate so the client
    shows the node's conversation; an ("encounter") step pushes a 'gamestarted'
    launching the battle. Returns the step tuple, or None when the dungeon is
    finished / not a DUNGEON campaign. On `won=False` the current node is kept
    uncompleted so an encounter loss retries the same battle.
    """
    log = getattr(handler, "_log_req", print)
    row = db.execute(
        "SELECT state_json, campaign_type FROM campaigns WHERE id=?",
        (camp_id,)).fetchone()
    if not row:
        return None
    state_json, ctype = row
    if (ctype or "").upper() != "DUNGEON":
        return None
    state = json.loads(state_json) if state_json else None
    if state is None:
        champ_row = db.execute(
            "SELECT champion_id FROM campaigns WHERE id=?", (camp_id,)).fetchone()
        state = _build_initial_gameplay_state(
            camp_id, champ_row[0] if champ_row else 0, "DUNGEON",
            _race_name_for_campaign(db, camp_id))
    # Rebuild stale state (built before the Crayburn seed had race-conversation
    # data) so VisLocs carry correct types and conversationIds.
    state = _prepare_dungeon_state(state, _race_name_for_campaign(db, camp_id))

    race_name = _race_name_for_campaign(db, camp_id)

    # Once the player has completed a node (conversation or battle), return
    # them to the castle map.  The avatar must physically travel to the next
    # node; setting ALoc to a later encounter here silently bypasses the map
    # movement and can start (for example) Tower Gatehouse early.
    if not auto_activate:
        current = state.get("ALoc") or state.get("LastNode") or "Entrance"
        current = _resolve_node(state, current)
        if won and current in _CASTLE_CHAIN and not state.get("_pending_encounter_success"):
            success_conv = (_crayburn_node_data(race_name, current) or {}).get("success")
            if success_conv:
                for loc in state.setdefault("VisLocs", []):
                    data = loc.get("Data", {})
                    if data.get("node") == current or data.get("name") == current:
                        data.update({
                            "type": "Convo", "conversationId": success_conv,
                            "encounter": None, "completed": False,
                            "enabled": True, "visible": True,
                        })
                        break
                state["_pending_encounter_success"] = current
                state["ALoc"] = current
                state["LastNode"] = current
                state["CurState"] = "EXPLORE"
                db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                           (json.dumps(state), camp_id))
                db.commit()
                push_campupdate(handler, db, camp_id,
                                state.get("ChampID") or 0,
                                "crayburn_victory", "DUNGEON", False,
                                state, comp, session_id, target, instance,
                                conh, uid)
                return ("success_conv", current)
        if not won:
            # Encounter nodes carry authored defeat conversations in the
            # campaign metadata. Keep the node retryable, but convert it to a
            # conversation and point ALoc at it so the client enqueues the
            # defeat scene before the player retries the battle.
            fail_conv = (_crayburn_node_data(race_name, current) or {}).get("fail")
            if fail_conv:
                for loc in state.setdefault("VisLocs", []):
                    data = loc.get("Data", {})
                    if data.get("node") == current or data.get("name") == current:
                        data.update({
                            "type": "Convo", "conversationId": fail_conv,
                            "encounter": None, "completed": False,
                            "enabled": True, "visible": True,
                        })
                        break
                state["ALoc"] = current
                state["LastNode"] = current
                state["CurState"] = "EXPLORE"
                db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                           (json.dumps(state), camp_id))
                db.commit()
                push_campupdate(handler, db, camp_id,
                                state.get("ChampID") or 0,
                                "crayburn_defeat", "DUNGEON", False,
                                state, comp, session_id, target, instance,
                                conh, uid)
                return ("fail_conv", current)
        if won and current in _CASTLE_CHAIN:
            state.pop("_pending_encounter_success", None)
            _mark_location_completed(state, current)
            _note_visited(state, current)
        state["ALoc"] = ""
        state["CurState"] = "EXPLORE"
        next_node = next((n for n in _CASTLE_CHAIN[1:]
                          if not _is_location_completed(state, n)), None)
        quest_state = None
        panorama_state = None
        panorama_id = None
        if next_node is None:
            # The final encounter completed the dungeon.  Keep the player on
            # the map, but also finish the dungeon and advance the linked
            # journal quest so its follow-up conversation becomes visible.
            state["Finished"] = _now_utc()
            state["FinishReason"] = "Complete"
            quest_state = _advance_quest_campaign(db, state.get("ChampID") or 0)
            # Completing the final castle conversation transitions the client
            # back to the race panorama.  A non-transitioning dungeon update
            # alone leaves the player looking at the completed castle map.
            champ_id = state.get("ChampID") or 0
            panorama_id, _pi, _ps, panorama_state = \
                _find_campaign_for_champion(db, champ_id, "PANORAMA")
            if panorama_state is not None:
                _prepare_post_crayburn_report(db, champ_id, panorama_state)
                db.execute("UPDATE campaigns SET is_started=1, state_json=? WHERE id=?",
                           (json.dumps(panorama_state), panorama_id))
                db.commit()
        if next_node:
            _reveal_crayburn_node(state, next_node)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), camp_id))
        db.commit()
        champ_id = state.get("ChampID") or 0
        push_campupdate(handler, db, camp_id, champ_id, "crayburn_travel",
                        "DUNGEON", False, state, comp, session_id, target,
                        instance, conh, uid)
        if quest_state:
            push_campupdate(handler, db, quest_state.get("CampID") or 0,
                            quest_state.get("ChampID") or champ_id,
                            "quest_complete", "QUEST", False, quest_state,
                            comp, session_id, target, instance, conh, uid)
        if panorama_state is not None:
            push_campupdate(handler, db, panorama_id,
                            panorama_state.get("ChampID") or champ_id,
                            "dungeon_complete", "PANORAMA", True,
                            panorama_state, comp, session_id, target,
                            instance, conh, uid)
        return ("await_travel", next_node) if next_node else None

    from_node = state.get("ALoc") or state.get("LastNode") or "Entrance"
    step = _advance_crayburn(state, race_name, from_node, won)

    db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
               (json.dumps(state), camp_id))
    db.commit()

    if not step:
        log(f"    Crayburn castle beaten (camp={camp_id})")
        # The fallen shard has been found — reveal the next quest objective
        # ("Report your success") in the journal.
        try:
            _advance_quest_campaign(db, state.get("ChampID") or 0)
        except Exception as e:
            log(f"    quest advance failed: {e}")
        return None

    kind, value = step
    if kind == "conv":
        log(f"    Crayburn: conversation pending at node={value} (camp={camp_id})")
        champ_id = state.get("ChampID") or 0
        push_campupdate(handler, db, camp_id, champ_id, "crayburn_travel",
                        "DUNGEON", False, state, comp, session_id, target,
                        instance, conh, uid)
        return step

    champ = db.execute("SELECT id, last_deck_id FROM champions WHERE id=?",
                       (state.get("ChampID") or 0,)).fetchone()
    deck_db_id = champ[1] if champ and champ[1] else None
    deck_uid64 = (deck_db_id << 8) | 17 if deck_db_id else 0
    champ_id = champ[0] if champ else (state.get("ChampID") or 0)
    log(f"    Crayburn: launching encounter {value} (camp={camp_id})")
    _launch_encounter(handler, db, camp_id, champ_id, value, deck_uid64,
                      comp, session_id, target, instance, conh, uid)
    return step


def _race_for_cfg(cfg):
    for race, c in _AZ0_RACE_CONFIG.items():
        if c is cfg or c.get("prefab") == cfg.get("prefab"):
            return _RACE_NAMES.get(race)
    return None


def _build_starter_panorama_state(cid, champion_id, cfg):
    """Build the AZ0 starter panorama for a race.

    Only the intro NPC is visible initially. The trainer and training
    encounter are added to VisLocs as the intro conversation chain advances
    (see _handle_sendevent).
    """
    intro_npc = cfg["intro_npc"]
    trainer_npc = cfg["trainer_npc"]
    quest_npc = cfg["quest_npc"]
    training_node = cfg["training_node"]
    nodes = [intro_npc, trainer_npc, quest_npc, training_node]
    locnodes = []
    for node in nodes:
        data = {"id": node, "type": "DEFAULT"}
        # Panorama portraits are resolved from Node.championId when a
        # conversation has already become an encounter and no longer has a
        # ConversationID.  The training opponent's champion template is the
        # authoritative profile for the trainer NPC.
        if node == trainer_npc and cfg.get("ai_champion_guid"):
            data["championId"] = cfg["ai_champion_guid"]
        locnodes.append({"Name": node, "Data": data})
    return {
        "CampID": cid,
        "ChampID": champion_id,
        "TempType": "PANORAMA",
        "PayGroups": [],
        "CSlide": None,
        "ALoc": intro_npc,
        # Keep the trainer, quest-giver and training node hidden until
        # their conversations authorize them.
        "VisLocs": [_convo_location(intro_npc, cfg["intro_conv"])],
        "LocNodes": locnodes,
        "Encounters": [{"Name": cfg["training_encounter"],
                        "Data": {"encscene": cfg["training_encounter"]}}],
        "Champions": [],
        "CurState": "EXPLORE",
        "LastNode": intro_npc,
        "PublicState": {"Data": {
            "CampaignGroup": "PANORAMA",
            "IsStarterPano": True,
            "HideQuickNavigation": False,
            "RaceTutorialBattleUnlocked": False,
        }},
        # TutorialDone: while False the training battle is treated as the
        # tutorial (player always wins the coin toss and goes first). It is
        # set True by _apply_gameend once the training battle is won, after
        # which subsequent battles randomize the turn player.
        "TutorialDone": False,
        "Started": None, "Finished": None, "FinishReason": None,
        "Wins": 0, "Losses": 0, "Score": 0, "HealthAdj": 0, "DungeonLifeAdj": 0,
        "Flags": {},
    }


def _build_initial_gameplay_state(cid, champion_id, campaign_type, champion_race=None):
    """Build a minimal GameplayState for the campaign."""
    # champion_race may be an int (race id from DB) or a string (race name).
    # The AZ0 config is keyed by numeric race ID, while the Crayburn data is
    # keyed by display name, so retain both forms during construction.
    race_id = champion_race
    if isinstance(champion_race, str):
        race_id = next(
            (rid for rid, name in _RACE_NAMES.items()
             if name.lower() == champion_race.lower()),
            None)
    race_name = _RACE_NAMES.get(race_id, champion_race)
    if campaign_type == "PANORAMA":
        cfg = _az0_config(race_id)
        if cfg:
            return _build_starter_panorama_state(cid, champion_id, cfg)

    castle_nodes = ["Entrance", "WatchTower", "Drawbridge", "CastleGate", "InnerBailey", "TowerGate", "PenworthTower"]
    castle_names = ["001 - Entrance", "002 - Outer Watchtower", "003 - The Drawbridge",
                    "004 - Castle Gatehouse", "005 - Inner Bailey", "006 - Tower Gatehouse",
                    "007 - Tower of Penworth"]
    # Campaign node -> the matching Crayburn Castle conversation node name.
    castle_conv_node = {
        "Entrance": None,
        "WatchTower": "The Watchtower",
        "Drawbridge": "The Drawbridge",
        "CastleGate": "Castle Gatehouse",
        "InnerBailey": "Inner Bailey",
        "TowerGate": "Tower Gatehouse",
        "PenworthTower": "Tower of Penworth",
    }
    if campaign_type == "DUNGEON":
        nodes = castle_nodes
        node_names = castle_names
        # Encounter GUIDs are shared across races; the per-node CONVERSATIONS
        # are race-specific (gamedata names "Crayburn Castle - <Race> - <Node>").
        race_data = _CRAYBURN_CASTLE["races"].get(race_name or "Necrotic", {})
        castle_convs = race_data.get("nodes", {})
        encounter_guids = _crayburn_scene_guids(race_name)
        vislocs = []
        for i, (nid, name) in enumerate(zip(nodes, node_names)):
            conv_name = castle_conv_node.get(nid)
            conv_id = castle_convs.get(conv_name, {}).get("conv") if conv_name else None
            is_enc = _crayburn_node_is_encounter(race_name or "Necrotic", nid)
            # Conv-only nodes are type "Convo" so the client's dungeon VM
            # auto-plays the conversation when ALoc matches their Name (the
            # panorama-intro mechanism). Encounter nodes stay "Encounter".
            # Only the Entrance and its immediate neighbour start visible;
            # the rest are hidden until the player reaches them.
            loc_visible = (i == 0 or i == 1)
            loc = {
                "Data": {
                    "name": nid, "node": nid,
                    "type": "Convo" if conv_id and not is_enc else ("Encounter" if i > 0 else "Dungeon"),
                    "autostart": False, "autopan": False, "autotrigger": False,
                    "battle": None, "completed": False, "enabled": loc_visible,
                    "visible": loc_visible,
                    "repeatable": False, "givequest": False, "turninquest": False,
                    "impassable": False, "unknown": False,
                    "encounter": _crayburn_scene_for_node(race_name, nid)
                    if is_enc else None,
                    "encounter_desc": None, "allow_cancel": False,
                    "conversationId": conv_id,
                }
            }
            vislocs.append(loc)
        locnodes = [{"Name": n, "Data": {"id": n, "type": "DEFAULT"}}
                    for i, n in enumerate(nodes)]
        encounter_list = [{"Name": g, "Data": {"encscene": g}} for g in encounter_guids]
        # ALoc = the Entrance node id so the player spawns at the castle
        # entrance. The server-driven chain (advance_crayburn_step) then moves
        # ALoc to the first conversation node and pushes a cmpupdate, which is
        # when the client auto-plays it (the client only launches conversations
        # from a state change while not mid-transition, so the initial load
        # must not point at a convo node).
        aloc = "Entrance" if len(nodes) > 1 else nodes[0]
    else:
        nodes = ["Node001"]
        node_names = ["The Bridge"]
        encounters = ["Encounter1"]
        vislocs = [{"Data": {"name": node_names[0], "node": nodes[0], "type": "Dungeon",
            "autostart": False, "autopan": False, "autotrigger": False, "battle": None,
            "completed": False, "enabled": True, "visible": True, "repeatable": False,
            "givequest": False, "turninquest": False, "impassable": False, "unknown": False,
            "encounter": encounters[0], "encounter_desc": None, "allow_cancel": False, "conversationId": None}}]
        locnodes = [{"Name": nodes[0], "Data": {"id": nodes[0], "type": "Dungeon"}}]
        encounter_list = [{"Name": encounters[0], "Data": {"encscene": ""}}]
        aloc = nodes[0]
    return {
        "CampID": cid,
        "ChampID": champion_id,
        "TempType": campaign_type,
        "PayGroups": [],
        "CSlide": None,
        "ALoc": aloc,
        "VisLocs": vislocs,
        "LocNodes": locnodes,
        "Encounters": encounter_list,
        "Champions": [],
        "CurState": "EXPLORE",
        "LastNode": nodes[0],
        "PublicState": {"Data": {
            "CampaignGroup": "DUNGEON",
            "visited_nodes": ["Entrance"],
            "IsStarterDungeon": True,
        }},
        "Started": None, "Finished": None, "FinishReason": None,
        "Wins": 0, "Losses": 0, "Score": 0, "HealthAdj": 0, "DungeonLifeAdj": 0, "Flags": {},
    }


def _build_input_response(cid, state, success=True, applied=None):
    """Build an InputResponse JSON dict."""
    return {
        "Cid": cid,
        "Success": success,
        "Errors": [],
        "CurState": state,
        "Applied": applied or _empty_applied_updates(),
    }


def _prepare_dungeon_state(state, race_name=None):
    """Normalize a fresh Castle Crayburn DUNGEON state for delivery to the
    client. If the stored state is stale (built before the per-node
    conversations were seeded), rebuild it from _build_initial_gameplay_state
    so every node carries its race conversationId. A started dungeon with no
    completed node beyond the entrance is also repaired to the first
    conversation node; this recovers runs saved by the old deferred-travel
    implementation."""
    if not state or state.get("TempType") != "DUNGEON":
        return state
    # Detect stale states (pre-Crayburn-seed): no node has a conversationId.
    stale = all(
        not (l.get("Data", {}) or {}).get("conversationId")
        for l in state.get("VisLocs", []))
    if stale:
        cid = state.get("CampID", 0)
        champ_id = state.get("ChampID", 0)
        state = _build_initial_gameplay_state(cid, champ_id, "DUNGEON",
                                              race_name or "Necrotic")
    _normalize_crayburn_encounters(state, race_name)
    completed = [l.get("Data", {}).get("node") for l in state.get("VisLocs", [])
                 if l.get("Data", {}).get("completed")]
    non_entrance_done = [n for n in completed if n and n != "Entrance"]
    if non_entrance_done:
        return state  # mid-run dungeon — leave ALoc as-is
    # A deferred marker can be left behind by an interrupted transition.  If
    # the run has started, expose that node directly so rejoining the dungeon
    # resumes at the conversation rather than returning to the map with no
    # active location.
    pending = _resolve_node(state, state.pop("_pending_travel", ""))
    first_pending = next(
        (node for node in _CASTLE_CHAIN[1:]
         if not _is_location_completed(state, node)), None)
    if state.get("Started") and first_pending:
        node = pending if pending and not _is_location_completed(state, pending) \
            else first_pending
        state["ALoc"] = node
        state["LastNode"] = node
        state["CurState"] = "EXPLORE"
        _reveal_crayburn_node(state, node)
        _set_crayburn_autostart(state, node)
    elif state.get("ALoc") in (None, ""):
        # Before StartCamp, keep the player at the physical entrance.  The
        # startcamp handler advances to the first conversation itself.
        state["ALoc"] = "Entrance"
        if state.get("LastNode") in (None, ""):
            state["LastNode"] = "Entrance"
    return state


def _empty_applied_updates():
    return {
        # This mirrors Game.Shared.Campaign.Messages.AppliedUpdates.  The
        # campaign client uses Completed as the trigger for its loot window;
        # the older Stardust/Experience shape was silently ignored.
        "Pending": [], "Completed": [], "Accounts": [], "Champions": [],
        "Decks": [], "Items": [], "Cards": [], "MercInfos": [],
        "AccountFlags": [],
    }


# Race → dungeon bundle mapping
_RACE_BUNDLE_MAP = {
    1: ("adventurezone01/p_hmm_wrenscastle", "p_hmm_wrenscastle"),      # Human → Wren's Castle
    2: ("adventurezone01/p_elf_aryndelpalace", "p_elf_aryndelpalace"),  # Elf
    3: ("adventurezone01/p_cytl_amblingmesa", "p_cytl_amblingmesa"),    # Coyotle
    4: ("adventurezone01/p_orc_xamahuac", "p_orc_xamahuac"),            # Orc
    5: ("adventurezone01/p_dwrf_cavein", "p_dwrf_cavein"),              # Dwarf
    6: ("adventurezone01/p_shnhr_jinguru", "p_shnhr_jinguru"),          # Shin'hare
    7: ("adventurezone01/p_ncrtc_necropolis", "p_ncrtc_necropolis"),    # Necrotic
    8: ("adventurezone01/p_vnnn_thehatchery", "p_vnnn_thehatchery"),    # Vennen
}

# When _launch_encounter pushes a gamestarted, the scene GUID is stored
# here keyed by session_name so the battle session setup can resolve the
# correct AI deck/name (resolve_encounter uses this to override
# the race's training encounter with the actual launched scene).
_last_encounter_scene = {}

# Race-specific Castle Crayburn EncounterScene IDs from the client data.
# The shared dungeon template supplies the map, but these scenes supply the
# client-side pre-battle opponent preview and the matching EncounterDeck.
# Order is Castle Gatehouse, Tower Gatehouse, Tower of Penworth.
_CRAYBURN_RACE_SCENE_IDS = {
    "Coyotle": (
        "f3c0ac5b-ff09-488c-ad63-f11ff15acdcd",
        "2ba61b7b-6864-4582-a634-f9124fb2fdee",
        "5f222319-7b4e-4ba4-b0dc-f9678c000d8b",
    ),
    "Dwarf": (
        "96225626-52ef-4d18-8972-60200d2042e5",
        "8188e093-047c-40d5-ae06-fb99d54530d4",
        "d796541f-c6c8-4e4f-8313-10ba37f1814b",
    ),
    "Elf": (
        "f25f20b1-3cb3-4066-ae91-1c557f0928c2",
        "31d6b2aa-bdf1-47ea-a0a5-a62bacdb75fa",
        "a5b0cfd3-c85e-4c8f-a4ae-172179ae1450",
    ),
    "Human": (
        "5919c63a-38f6-487a-9d66-3b13cb12a520",
        "be2c5dd9-6cb5-4cf8-a1ef-f050079b1130",
        "61ae67b4-1691-4b1f-903c-687c396256a9",
    ),
    "Necrotic": (
        "69c451b9-4e02-4c66-8943-f0b4769d90d4",
        "a2c5cef6-f76f-46a8-afa0-82558e6ebf46",
        "607b221d-dbe7-4fe7-8d4f-c7b9774fb187",
    ),
    "Orc": (
        "e288f879-5860-4d29-9c2d-7977ff03f0f6",
        "b07c8713-b294-46cd-93e4-d067e36f51a7",
        "6355ca3d-1bab-4251-896c-b82e8877a2f4",
    ),
    "Shin'hare": (
        "ed4ff0de-ce9a-4a50-b256-4cdca572e792",
        "c5cbbc95-a4ba-461e-9d42-1c592f120b1a",
        "1f29a2cc-ec2d-438b-9c46-296d8d8bf9ec",
    ),
    "Vennen": (
        "98158279-f641-48c1-8439-f1688ef09a9d",
        "8afdb8ac-d155-4c63-9a73-623f16075ab8",
        "3c1d4212-f5df-4efa-bfa7-b6b1e51e68b7",
    ),
}


def _crayburn_scene_guids(race_name):
    """Return the client-known encounter scenes for a race route."""
    return _CRAYBURN_RACE_SCENE_IDS.get(
        race_name, tuple(_CRAYBURN_CASTLE.get("encounters", ())))


_CRAYBURN_ENCOUNTER_NODES = ("CastleGate", "TowerGate", "PenworthTower")


def _crayburn_scene_for_node(race_name, node):
    """Return the client-known scene for a race's battle node."""
    try:
        return _crayburn_scene_guids(race_name)[
            _CRAYBURN_ENCOUNTER_NODES.index(node)]
    except (ValueError, IndexError):
        return None


def _normalize_crayburn_encounters(state, race_name):
    """Replace legacy shared scene IDs with the route-specific client IDs."""
    if not state or race_name not in _CRAYBURN_RACE_SCENE_IDS:
        return False
    changed = False
    scene_guids = _crayburn_scene_guids(race_name)
    for loc in state.get("VisLocs", []):
        data = loc.get("Data", {}) or {}
        expected = _crayburn_scene_for_node(race_name, data.get("node"))
        if expected and data.get("encounter") != expected:
            data["encounter"] = expected
            changed = True
    expected_list = [{"Name": guid, "Data": {"encscene": guid}}
                     for guid in scene_guids]
    if state.get("Encounters") != expected_list:
        state["Encounters"] = expected_list
        changed = True
    return changed

def _build_camp_summary(cid, inst_id, campaign_type="PANORAMA", template_name="AZ1", champion_race=None):
    """Build a CampSummary JSON dict."""
    bundle, prefab = "", ""
    cfg = _az0_config(champion_race)
    if cfg:
        bundle, prefab = cfg["bundle"], cfg["prefab"]
    elif champion_race and champion_race in _RACE_BUNDLE_MAP:
        bundle, prefab = _RACE_BUNDLE_MAP[champion_race]

    is_panorama = (campaign_type or "PANORAMA").upper() == "PANORAMA"
    if is_panorama:
        # Race-specific starter panorama: no dungeon nodes, tutorial bg.
        background_prefab = "campaign/tutorial/prefabs/background1"
        nodes_prefab = ""
        campaign_template_id = "2f59b729-7cdf-4fb4-abc3-5654a644df65"
    elif (campaign_type or "PANORAMA").upper() == "AREA":
        background_prefab = "campaign/azmap/azmap"
        nodes_prefab = "campaign/az01/nodes"
        campaign_template_id = "742c6fc1-77e5-4029-bf9e-4e8af387857a"
        bundle, prefab = "", ""
    else:
        # Castle Crayburn dungeon. The background/nodes prefabs are the real
        # castle assets (resolve via Resources.Load); the AssetBundle+LevelPrefab
        # are deliberately left empty so OnStartUp_GetSummary skips the async
        # bundle-level load (UICampaignZoneVMBase.cs:635 -> 642 else) and calls
        # OnPrefabLoaded() directly — the p_* panorama level prefab is not a
        # dungeon scene and its load NREs in UIDungeonZoneViewModel.
        background_prefab = "campaign/az01/crayburncastle/prefabs/background"
        nodes_prefab = "campaign/az01/crayburncastle/prefabs/nodes"
        campaign_template_id = "5bcba43a-95c7-44b4-ba09-a3555a5edf05"
        bundle, prefab = "", ""

    return {
        "CampID": cid,
        "ReckID": {"lo": 0, "hi": 0},
        "CampType": _campaign_type_int(campaign_type or "PANORAMA"),
        "TypeInfo": {
            "Name": template_name,
            "Type": campaign_type or "PANORAMA",
            "Doc": None,
            "NameExclusive": False,
            "TypeExclusive": False,
            "AssetBundle": bundle,
            "LevelPrefab": prefab,
            "BackgroundPrefab": background_prefab,
            "NodesPrefab": nodes_prefab,
            "CampaignTemplateId": campaign_template_id,
        },
        "TemplateName": template_name,
        "IsDeckEditable": True,
        "Version": 1,
        "ClosedOn": None,
    }


def _build_template_info(cid, inst_id, campaign_type="STRONGHOLD", template_name="AZ1"):
    """Build a TemplateInfo JSON dict (for getactive response)."""
    return {
        "CampID": cid,
        "CampType": _campaign_type_int(campaign_type),
        "ReckID": {"lo": 0, "hi": 0},
        "TemplateName": template_name,
        "Version": 1,
        "ClosedOn": None,
    }


def _campaign_type_int(ct_str):
    """Map campaign type string to int enum value."""
    mapping = {
        "ANY": 0, "DUNGEON": 1, "AREA": 2, "WORLD": 3,
        "QUEST": 4, "ACHIEVE": 5, "PANORAMA": 6, "STRONGHOLD": 7, "TEST": 8,
    }
    return mapping.get(ct_str.upper(), 2)


# ---------------------------------------------------------------------------
# Response sending
# ---------------------------------------------------------------------------

CAMP_SYS_RESPONSE_TYPE = "Game.Shared.Campaign.Messages.CampSysGeneral+Response"
CAMP_SYS_REQUEST_TYPE = "Game.Shared.Campaign.Messages.CampSysGeneral+Request"

def _send_response(handler, response_json_str, comp, session_id, reqid,
                   target, instance, conh, service_mail_uid):
    """Serialize the campaign response envelope, wrap in ObjFmt, compress, send."""
    resp_envelope = response_json_str.encode("utf-8")
    resp_inner = encode_objfmt_response(
        [CAMP_SYS_RESPONSE_TYPE, "System.Byte[]"],
        [("Envelope", "bytes", resp_envelope)]
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 110000, resp_body, comp, session_id)

    issuer_str = (
        f"0.0.0.0.ServiceCampaign.{service_mail_uid}."
        f"ServicePlayer.{handler.client_uid}.{resp_reqid}"
    )
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str,
        "target": target,
        "instance": instance,
        "reqid": resp_reqid,
        "c": comp,
        "conh": conh,
        "sid": handler.sid,
    }, dw_bytes)
    return f"    Sent Campaign response for reqid={reqid} ({len(dw_bytes)}b)"


def _push_campaign_notify(handler, env_json, comp, session_id, target, instance,
                          conh, service_mail_uid):
    """Push a Campaign notification (RequestType "gamestarted"/"gameendnotify"
    etc.) to the client.

    The client's ClientCampaignManager dispatches incoming campaign messages by
    their RequestType (gamestarted / gameendnotify / campspawn / cmpupdate).
    The envelope must decode to CampSysGeneral.Request for the routing
    handler (CustomNetworkMessage.Incomming) to fire — an unsolicited push has
    no pending reply to match, so it is matched only via `obj is Request`.
    """
    resp_envelope = json.dumps(env_json).encode("utf-8")
    resp_inner = encode_objfmt_response(
        [CAMP_SYS_REQUEST_TYPE, "System.Byte[]"],
        [("Envelope", "bytes", resp_envelope)]
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(0, 110000, resp_body, comp, session_id)

    issuer_str = (
        f"0.0.0.0.ServiceCampaign.{service_mail_uid}."
        f"ServicePlayer.{handler.client_uid}.{handler.scnt}"
    )
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str,
        "target": target,
        "instance": instance,
        "reqid": 0,
        "c": comp,
        "conh": conh,
        "sid": handler.sid,
    }, dw_bytes)
    return f"    Sent Campaign notify {env_json.get('RequestType','?')} ({len(dw_bytes)}b)"


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

def handle_campaign_request(handler, _db, inner_obj, comp, session_id, reqid,
                             target, instance, conh, service_mail_uid):
    """Entry point for ServiceCampaign (dt=110000) requests.

    inner_obj: parsed dict from the outer ObjFmt message.
               Has an "Envelope" key with the JSON request bytes.
    """
    uid = service_mail_uid  # pass to all handlers
    envelope = inner_obj.get("Envelope", b"{}")
    if isinstance(envelope, bytes):
        try:
            env_json = json.loads(envelope.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            env_json = {}
    elif isinstance(envelope, str):
        try:
            env_json = json.loads(envelope)
        except json.JSONDecodeError:
            env_json = {}
    else:
        env_json = envelope if isinstance(envelope, dict) else {}

    req_type = env_json.get("RequestType", "")
    log = getattr(handler, "_log_req", print)
    log(f"    Campaign dt=110000 req={req_type}")

    if req_type == "qcur4champ":
        return _handle_qcur4champ(handler, _db, env_json, comp, session_id,
                                   reqid, target, instance, conh, uid)
    elif req_type == "getactive":
        return _handle_getactive(handler, _db, env_json, comp, session_id,
                                  reqid, target, instance, conh, uid)
    elif req_type == "createcamp":
        return _handle_createcamp(handler, _db, env_json, comp, session_id,
                                   reqid, target, instance, conh, uid)
    elif req_type == "getcampstate":
        return _handle_getcampstate(handler, _db, env_json, comp, session_id,
                                     reqid, target, instance, conh, uid)
    elif req_type == "startcamp":
        return _handle_startcamp(handler, _db, env_json, comp, session_id,
                                  reqid, target, instance, conh, uid)
    elif req_type == "getcampsum":
        return _handle_getcampsum(handler, _db, env_json, comp, session_id,
                                   reqid, target, instance, conh, uid)
    elif req_type == "sendevent":
        return _handle_sendevent(handler, _db, env_json, comp, session_id,
                                  reqid, target, instance, conh, uid)
    elif req_type == "gameend":
        return _handle_gameend(handler, _db, env_json, comp, session_id,
                                reqid, target, instance, conh, uid)
    elif req_type == "locaction":
        return _handle_locaction(handler, _db, env_json, comp, session_id,
                                  reqid, target, instance, conh, uid)
    elif req_type == "forfeit":
        return _handle_forfeit(handler, _db, env_json, comp, session_id,
                                reqid, target, instance, conh, uid)
    elif req_type == "cheat":
        return _handle_cheat(handler, _db, env_json, comp, session_id,
                              reqid, target, instance, conh, uid)
    else:
        log(f"    Unhandled campaign request: {req_type}")
        return


def _handle_qcur4champ(handler, db, env_json, comp, session_id,
                        reqid, target, instance, conh, uid):
    """QueryCurrentForChampion: returns the champion's current campaign state."""
    champ_id = env_json.get("ChampID", 0)
    # A champion can retain its completed Panorama after accepting the
    # Crayburn quest.  Prefer an active dungeon on reconnect; otherwise this
    # query always recreated/selected Panorama and silently took the player
    # out of the dungeon.
    dungeon = _get_existing_campaign_for_champion(db, champ_id, "DUNGEON")
    if dungeon:
        d_cid, d_inst_id, d_inst_hi, d_started, d_state = dungeon
        if d_state and (d_started or d_state.get("Started")) \
                and not d_state.get("Finished"):
            original = json.dumps(d_state, sort_keys=True)
            d_state = _prepare_dungeon_state(
                d_state, _race_name_for_campaign(db, d_cid))
            if json.dumps(d_state, sort_keys=True) != original:
                db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                           (json.dumps(d_state), d_cid))
                db.commit()
            resp = _build_input_response(d_cid, d_state, success=True)
            return _send_response(handler, json.dumps(resp), comp, session_id,
                                  reqid, target, instance, conh, uid)

    # Repair saves created before the post-dungeon report marker was added.
    # Such saves have a completed Crayburn dungeon and an active next journal
    # objective, but the panorama would otherwise be treated as the old quest
    # giver state when the player reconnects.
    if (dungeon and d_state and d_state.get("Finished") and
            d_state.get("FinishReason") == "Complete"):
        pending_quest = _get_existing_campaign_for_champion(db, champ_id, "QUEST")
        if pending_quest:
            q_cid, _qi, _qh, _qs, q_state = pending_quest
            qrow = db.execute(
                "SELECT template_name FROM campaigns WHERE id=?", (q_cid,)
            ).fetchone()
            q_metadata = _quest_template(db, qrow[0]) if qrow else None
            q_flags = (q_state or {}).get("Flags", {})
            q_idx = q_flags.get("_quest_objective_idx")
            q_objectives = q_flags.get("_quest_objectives") or []
            try:
                q_idx = int(q_idx)
            except (TypeError, ValueError):
                q_idx = -1
            if (q_state and not q_state.get("Finished") and q_metadata and
                    q_metadata.get("campaign_group") == "DUNGEON" and
                    0 < q_idx < len(q_objectives)):
                panorama = _get_existing_campaign_for_champion(
                    db, champ_id, "PANORAMA")
                if panorama:
                    p_cid, _pi, _ph, _ps, p_state = panorama
                    if p_state and _prepare_post_crayburn_report(
                            db, champ_id, p_state):
                        db.execute(
                            "UPDATE campaigns SET is_started=1, state_json=? "
                            "WHERE id=?", (json.dumps(p_state), p_cid))
                        db.commit()
                    if p_state:
                        resp = _build_input_response(p_cid, p_state, success=True)
                        return _send_response(
                            handler, json.dumps(resp), comp, session_id,
                            reqid, target, instance, conh, uid)

    # Once the dungeon is complete, the linked quest campaign owns the next
    # player-facing step (for example Step2: the report-success conversation).
    # Return that campaign before the persistent panorama so reconnect/current
    # campaign queries do not silently strand the player in AZ1.
    quest = _get_existing_campaign_for_champion(db, champ_id, "QUEST")
    if quest:
        q_cid, _q_inst_id, _q_inst_hi, _q_started, q_state = quest
        q_template = db.execute(
            "SELECT template_name FROM campaigns WHERE id=?", (q_cid,)
        ).fetchone()
        if q_state and not q_state.get("Finished"):
            q_aloc = q_state.get("ALoc")
            q_idx = q_state.get("Flags", {}).get("_quest_objective_idx")
            # Once Crayburn is complete, the report objective is represented
            # by the race panorama NPC rather than a synthetic QUEST map.
            # Only the Crayburn journal's report objective is hosted by the
            # dungeon renderer.  AREA quests (such as az01_tamed) stay in
            # the quest journal and must never replace the player's current
            # overworld campaign in QueryCurrentForChampion.
            q_metadata = _quest_template(db, q_template[0]) if q_template else None
            if (q_metadata and q_metadata.get("campaign_group") == "DUNGEON" and
                    (q_aloc or q_idx is not None) and int(q_idx or 0) < 1):
                # The client has no QUEST campaign game-state transition: it
                # only knows how to enter DUNGEON/AREA/PANORAMA/STRONGHOLD.
                # Quest objectives are rendered by the dungeon zone (including
                # conversation locations), so expose the supported UI type
                # without changing the persisted campaign_type.
                q_state = dict(q_state)
                q_state["TempType"] = "DUNGEON"
                resp = _build_input_response(q_cid, q_state, success=True)
                return _send_response(handler, json.dumps(resp), comp, session_id,
                                      reqid, target, instance, conh, uid)

    area = _get_existing_campaign_for_champion(db, champ_id, "AREA")
    if area:
        a_cid, _a_inst_id, _a_inst_hi, a_started, a_state = area
        if a_state and (a_started or a_state.get("Started")) and not a_state.get("Finished"):
            if a_state.get("TempType") == "AREA":
                before = json.dumps(a_state, sort_keys=True)
                _hydrate_az1_area_scene_metadata(
                    db, a_state.get("VisLocs", []), champ_id=champ_id,
                    state=a_state)
                _az1_reveal_neighbors(db, a_state,
                                       a_state.get("LastNode") or a_state.get("ALoc"))
                if json.dumps(a_state, sort_keys=True) != before:
                    db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                               (json.dumps(a_state), a_cid))
                    db.commit()
            resp = _build_input_response(a_cid, a_state, success=True)
            return _send_response(handler, json.dumps(resp), comp, session_id,
                                  reqid, target, instance, conh, uid)

    cid, inst_id, is_started, state = _find_campaign_for_champion(
        db, champ_id, "PANORAMA")
    if state is None:
        champ = _get_champion(db, champ_id)
        state = _build_initial_gameplay_state(cid, champ_id, "PANORAMA", champ[2] if champ else None)
    resp = _build_input_response(cid, state, success=True)
    return _send_response(handler, json.dumps(resp), comp, session_id,
                          reqid, target, instance, conh, uid)


def _handle_getactive(handler, db, env_json, comp, session_id,
                       reqid, target, instance, conh, uid):
    """QueryActiveStatus: return list of TemplateInfo for matching campaigns."""
    champ_id = env_json.get("ChampID", 0)
    # The client sends CampType as an int enum (1=DUNGEON, 6=PANORAMA). Our
    # old "_campType" string key never matched, so we always fell through to
    # "all campaigns" and the client picked the wrong (PANORAMA) one.
    ctype_int = env_json.get("CampType", env_json.get("_campType", 0))
    if isinstance(ctype_int, str):
        ctype = ctype_int.upper()
    else:
        ctype = {1: "DUNGEON", 2: "AREA", 3: "WORLD", 4: "QUEST",
                 5: "ACHIEVE", 6: "PANORAMA", 7: "STRONGHOLD"}.get(ctype_int, "AREA")
    template = (env_json.get("Template", "") or "")
    tname = template.lower()

    # Find matching campaigns - try exact match then by template name
    rows = []
    if template:
        rows = db.execute(
            "SELECT id, camp_uid_lo, campaign_type, template_name FROM campaigns "
            "WHERE champion_id=? AND campaign_type=? AND lower(template_name)=lower(?)",
            (champ_id, ctype, template)).fetchall()
    if not rows and template:
        rows = db.execute(
            "SELECT id, camp_uid_lo, campaign_type, template_name FROM campaigns "
            "WHERE champion_id=? AND lower(template_name)=lower(?)",
            (champ_id, template)).fetchall()
    if not rows:
        rows = db.execute(
            "SELECT id, camp_uid_lo, campaign_type, template_name FROM campaigns "
            "WHERE champion_id=? AND campaign_type=?",
            (champ_id, ctype)).fetchall()
    if not rows and (not ctype or ctype == "ANY"):
        rows = db.execute(
            "SELECT id, camp_uid_lo, campaign_type, template_name FROM campaigns "
            "WHERE champion_id=?", (champ_id,)).fetchall()
    # getactive is an active-status query. Completed campaign rows are kept
    # for history, but returning them here makes the client emit stale
    # "Quest Complete" notifications while selecting another campaign.
    active_rows = []
    for row in rows:
        finished = db.execute(
            "SELECT json_extract(state_json, '$.Finished') FROM campaigns WHERE id=?",
            (row[0],)).fetchone()
        if not finished or finished[0] is None:
            active_rows.append(row)
    rows = active_rows
    templates = [_build_template_info(row[0], row[1], row[2] or "AREA", row[3] or "AZ1") for row in rows]
    return _send_response(handler, json.dumps(templates), comp, session_id,
                          reqid, target, instance, conh, uid)


def _handle_createcamp(handler, db, env_json, comp, session_id,
                        reqid, target, instance, conh, uid):
    """CreateCamp: create a new campaign instance."""
    champ_id = env_json.get("ChampID", 0)
    template_name = env_json.get("Template", "Crayburn Castle")
    # Determine campaign type from template name. The client's camp.dungeon
    # console command sends whatever the user typed (typos like "cragburn" are
    # common) — fuzzy-match against known dungeon names.
    tname = (template_name or "").lower()
    if _is_known_dungeon(tname):
        campaign_type = "DUNGEON"
        # Normalize typos to the canonical name so quest resolution etc. works.
        template_name = "Crayburn Castle"
    elif "panorama" in tname:
        campaign_type = "PANORAMA"
    else:
        campaign_type = "PANORAMA"

    champ = _get_champion(db, champ_id)
    if not champ:
        resp = _build_input_response(0, None, success=False)
        resp["Errors"] = ["Champion not found"]
        return _send_response(handler, json.dumps(resp), comp, session_id,
                              reqid, target, instance, conh, uid)

    cid, inst_id, is_started, state = _find_campaign_for_champion(db, champ_id, campaign_type)
    if campaign_type == "DUNGEON":
        state = _prepare_dungeon_state(state, _race_name_for_campaign(db, cid))
    resp = _build_input_response(cid, state, success=True)
    return _send_response(handler, json.dumps(resp), comp, session_id,
                          reqid, target, instance, conh, uid)


def _handle_getcampstate(handler, db, env_json, comp, session_id,
                          reqid, target, instance, conh, uid):
    """QueryCampState: return full GameplayState for a campaign."""
    camp_id = env_json.get("CampID", 0)
    row = db.execute(
        "SELECT champion_id, is_started, state_json, campaign_type, template_name "
        "FROM campaigns WHERE id=?",
        (camp_id,)
    ).fetchone()
    if not row:
        resp = _build_input_response(camp_id, None, success=False)
        resp["Errors"] = ["Campaign not found"]
        return _send_response(handler, json.dumps(resp), comp, session_id,
                              reqid, target, instance, conh, uid)

    champ_id, is_started, state_json, ctype, template_name = row
    if state_json:
        state = json.loads(state_json)
    else:
        state = _build_initial_gameplay_state(camp_id, champ_id, ctype or "AREA")
    if (ctype or "").upper() == "PANORAMA":
        champ = _get_champion(db, champ_id)
        cfg = _az0_config(champ[2]) if champ else None
        if cfg and _normalize_starter_panorama_state(state, cfg):
            db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                       (json.dumps(state), camp_id))
            db.commit()
    if ((ctype or "").upper() == "AREA" and
            str(template_name or "").upper() == "AZ1"):
        before = json.dumps(state, sort_keys=True)
        _hydrate_az1_area_scene_metadata(
            db, state.get("VisLocs", []), champ_id=champ_id, state=state)
        _az1_reveal_neighbors(db, state,
                               state.get("LastNode") or state.get("ALoc"))
        if json.dumps(state, sort_keys=True) != before:
            db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                       (json.dumps(state), camp_id))
            db.commit()
    if (ctype or "").upper() == "DUNGEON":
        state = _prepare_dungeon_state(state, _race_name_for_campaign(db, camp_id))
    resp = _build_input_response(camp_id, state, success=True)
    return _send_response(handler, json.dumps(resp), comp, session_id,
                          reqid, target, instance, conh, uid)


def _handle_startcamp(handler, db, env_json, comp, session_id,
                       reqid, target, instance, conh, uid):
    """StartCamp: mark a campaign as started, return updated state."""
    camp_id = env_json.get("CampID", 0)
    row = db.execute(
        "SELECT champion_id, state_json, campaign_type, template_name "
        "FROM campaigns WHERE id=?",
        (camp_id,)
    ).fetchone()
    if not row:
        resp = _build_input_response(camp_id, None, success=False)
        resp["Errors"] = ["Campaign not found"]
        return _send_response(handler, json.dumps(resp), comp, session_id,
                              reqid, target, instance, conh, uid)

    champ_id, state_json, ctype, template_name = row
    if state_json:
        state = json.loads(state_json)
    else:
        state = _build_initial_gameplay_state(camp_id, champ_id, ctype or "AREA")

    # Mark as started
    state["Started"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["CurState"] = "EXPLORE"

    if (ctype or "").upper() == "DUNGEON":
        state = _prepare_dungeon_state(state, _race_name_for_campaign(db, camp_id))
        # Advance the castle chain from the Entrance so the startcamp response
        # itself carries ALoc pointing at the first conversation node.  That
        # way the client auto-plays it once on zone load (no separate cmpupdate
        # needed, which would re-trigger and loop).
        _advance_crayburn(state, _race_name_for_campaign(db, camp_id),
                          "Entrance", False)
    elif ((ctype or "").upper() == "AREA" and
          str(template_name or "").upper() == "AZ1"):
        _hydrate_az1_area_scene_metadata(
            db, state.get("VisLocs", []), champ_id=champ_id, state=state)
        _az1_reveal_neighbors(db, state,
                              state.get("LastNode") or state.get("ALoc"))

    db.execute(
        "UPDATE campaigns SET is_started=1, state_json=? WHERE id=?",
        (json.dumps(state), camp_id)
    )
    db.commit()
    resp = _build_input_response(camp_id, state, success=True)
    return _send_response(handler, json.dumps(resp), comp, session_id,
                          reqid, target, instance, conh, uid)


def _handle_getcampsum(handler, db, env_json, comp, session_id,
                        reqid, target, instance, conh, uid):
    """GetCampaignSummary: return CampSummary[] for requested camp IDs."""
    camp_ids = env_json.get("CampIDs", [env_json.get("CampID", 0)])
    if not isinstance(camp_ids, list):
        camp_ids = [camp_ids]

    summaries = []
    for cid in camp_ids:
        row = db.execute(
            "SELECT c.camp_uid_lo, c.campaign_type, c.template_name, ch.race "
            "FROM campaigns c JOIN champions ch ON c.champion_id = ch.id WHERE c.id=?",
            (cid,)
        ).fetchone()
        if row:
            summaries.append(_build_camp_summary(cid, row[0], row[1] or "DUNGEON", row[2] or "Crayburn Castle", row[3]))

    return _send_response(handler, json.dumps(summaries), comp, session_id,
                          reqid, target, instance, conh, uid)


def _handle_sendevent(handler, db, env_json, comp, session_id,
                       reqid, target, instance, conh, uid):
    """SendEvent: handle events sent to the campaign logic engine."""
    camp_id = env_json.get("CampID", 0)
    event_name = env_json.get("Event", "")
    o_params = env_json.get("OParms", [])

    log = getattr(handler, "_log_req", print)
    log(f"    Campaign SendEvent: camp={camp_id} event={event_name} params={o_params}")

    row = db.execute(
        "SELECT champion_id, state_json, campaign_type, template_name "
        "FROM campaigns WHERE id=?",
        (camp_id,)
    ).fetchone()
    if not row:
        resp = _build_input_response(camp_id, None, success=False)
        resp["Errors"] = ["Campaign not found"]
        return _send_response(handler, json.dumps(resp), comp, session_id,
                              reqid, target, instance, conh, uid)

    champ_id, state_json, ctype, template_name = row
    if state_json:
        state = json.loads(state_json)
    else:
        state = _build_initial_gameplay_state(camp_id, champ_id, ctype or "AREA")

    # Determine the AZ0 race config from the champion's race so the
    # conversation chain uses the correct NPC names/GUIDs.
    cfg = None
    champ = _get_champion(db, champ_id)
    if champ:
        cfg = _az0_config(champ[2])
    if cfg is None:
        cfg = _az0_config(1)  # fall back to Human layout

    intro_npc = cfg["intro_npc"]
    trainer_npc = cfg["trainer_npc"]
    quest_npc = cfg["quest_npc"]
    training_node = cfg["training_node"]

    def _append_location(loc):
        node = loc.get("Data", {}).get("node")
        if not any(l.get("Data", {}).get("node") == node
                   for l in state.setdefault("VisLocs", [])):
            state["VisLocs"].append(loc)

    pending_dungeon = None
    pending_area = None
    pending_quest_spawns = []

    if event_name == "choice_battle_yes":
        state.setdefault("PublicState", {}).setdefault("Data", {})[
            "RaceTutorialBattleUnlocked"
        ] = True
    elif event_name == "choice_battle_no":
        state.setdefault("PublicState", {}).setdefault("Data", {})[
            "RaceTutorialBattleUnlocked"
        ] = False
    elif event_name == "visit_path":
        # The map client reports the authored path identifier separately from
        # StartLoc. Persist it so travelled paths remain lit after refresh.
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        paths = pdata.setdefault("visited_paths", [])
        values = o_params[0] if o_params and isinstance(o_params[0], list) else o_params
        for path in values:
            path = str(path)
            if path and path not in paths:
                paths.append(path)
    elif event_name == "visit_node":
        # UIDungeonZoneViewModel reports the node when the token reaches its
        # destination, immediately before issuing StartLoc.  Synchronize the
        # server-side position here so an older save cannot reject a valid
        # authored move (for example Node003 -> Node007 while LastNode still
        # points at a completed Node004 side location).
        if ((ctype or "").upper() == "AREA" and
                str(template_name or "").upper() == "AZ1"):
            requested_node = _resolve_node(
                state, str(o_params[0]) if o_params else "")
            location_data = next(
                ((loc.get("Data") or {}) for loc in state.get("VisLocs", [])
                 if (loc.get("Data") or {}).get("node") == requested_node),
                None,
            )
            pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
            visited_nodes = {
                _resolve_node(state, str(value))
                for value in (pdata.get("visited_nodes") or []) if value
            }
            blocked_nodes = {
                _resolve_node(state, str(value))
                for value in (pdata.get("blocked_nodes") or []) if value
            }
            # The client only emits visit_node for a rendered map node. Do
            # not let a direct event reveal a hidden quest-gated location;
            # already visited nodes remain valid for repeatable locations.
            if (location_data and
                    (location_data.get("visible", True) or
                     requested_node in visited_nodes) and
                    requested_node not in blocked_nodes):
                state["LastNode"] = requested_node
                _note_visited(state, requested_node)
                _az1_reveal_neighbors(db, state, requested_node)
    elif event_name in ("choice_go", "choice_stay"):
        # The authored AZ0 transition conversation is already hosted by the
        # AZ1 panorama.  The choice is recorded for the client conversation
        # flow; selecting either answer returns to the map without relaunching
        # the starter tutorial.
        if state.get("PostCrayburnReport"):
            state["ALoc"] = None
            state["CurState"] = "EXPLORE"
            if event_name == "choice_go":
                pending_area = _activate_az1_area(db, champ_id)
    elif event_name == "gaal_camp_accept":
        # Authored Milosh conversations emit this server-script event after
        # the player pays for a reading.  Persist the state marker used by the
        # node conversation selector so the next visit can use the
        # "already has fortune" variant instead of charging again.
        state.setdefault("PublicState", {}).setdefault("Data", {})[
            "gaal_fortune"
        ] = True
    elif (event_name == "conv_done" and
          (ctype or "").upper() == "DUNGEON"):
        # Server-driven Crayburn dungeon: a conversation-only node's
        # conversation finished → mark it done and advance to the next node.
        advance_crayburn_step(handler, db, camp_id, True, comp, session_id,
                              target, instance, conh, uid,
                              auto_activate=False)
        # advance_crayburn_step already persisted the updated state; reload
        # it so we return the post-advance state, not the stale pre-advance
        # copy that would otherwise be saved back and undo the advance.
        row2 = db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (camp_id,)).fetchone()
        state = json.loads(row2[0]) if row2 and row2[0] else state
        resp = _build_input_response(camp_id, state, success=True)
        return _send_response(handler, json.dumps(resp), comp, session_id,
                              reqid, target, instance, conh, uid)
    elif event_name == "shroom_choice":
        if (ctype or "").upper() == "AREA":
            choice = str(o_params[0]) if o_params else ""
            current = state.get("ALoc")
            current_data = next(
                ((loc.get("Data") or {}) for loc in state.get("VisLocs", [])
                 if current in (loc.get("Data", {}).get("node"),
                                loc.get("Data", {}).get("name"),
                                loc.get("Data", {}).get("title"))),
                {})
            scene_guid = _area_scene_guid(db, state)
            # Older AZ1 states were created before the authored NODE 04
            # encounter scene was seeded, so they have no Data.encounter even
            # though the map location already carries its metadata choices.
            # Prefer the encounter_scenes reward list, but accept that
            # persisted scene-derived list as a migration-safe fallback.
            choices = _scene_card_choices(db, scene_guid)
            if not choices:
                choices = [str(value) for value in
                           (current_data.get("choices") or []) if value]
            # A completed Haus is one-time; do not award a second copy if a
            # client retries the selection or manually reopens the location.
            if choice in choices and not current_data.get("completed"):
                user_row = db.execute(
                    "SELECT user_id FROM champions WHERE id=?", (champ_id,)
                ).fetchone()
                granted = _grant_card_reward(
                    handler, db, user_row[0] if user_row else None, choice)
                if not granted:
                    log(f"    Shroom Haus reward card not found: {choice}")
                if current and granted:
                    _mark_location_completed(state, current)
                    _note_visited(state, current)
                    state["ALoc"] = None
                    state["CurState"] = "EXPLORE"
            else:
                log(f"    Shroom Haus choice rejected: choice={choice} "
                    f"scene={scene_guid} choices={choices} active={current}")
    elif event_name in ("empty_done", "conv_done"):
        # Empty AREA nodes (such as AZ1's Shroom House) still require an
        # explicit completion event from the client after Explore.  Clear the
        # active location so the map can select the next revealed node.
        if (ctype or "").upper() == "AREA" and event_name == "empty_done":
            current = state.get("ALoc")
            if current:
                _mark_location_completed(state, current)
                _note_visited(state, current)
            state["ALoc"] = None
            state["CurState"] = "EXPLORE"
        if (ctype or "").upper() == "QUEST":
            # The journal quest's final report conversation is the terminal
            # objective. Persist completion so reconnect/current-campaign
            # queries fall through to the champion's panorama instead of
            # returning an empty Step2 quest map.
            current = state.get("ALoc")
            if current:
                _mark_location_completed(state, current)
            all_done = all(
                (loc.get("Data", {}) or {}).get("completed")
                for loc in state.get("VisLocs", []))
            if all_done or current == "Step2":
                state["Finished"] = _now_utc()
                state["FinishReason"] = "Complete"
                state["ALoc"] = None
                state["CurState"] = "EXPLORE"
                db.execute(
                    "UPDATE campaigns SET state_json=? WHERE id=?",
                    (json.dumps(state), camp_id))
                db.commit()
            resp = _build_input_response(camp_id, state, success=True)
            return _send_response(handler, json.dumps(resp), comp, session_id,
                                  reqid, target, instance, conh, uid)
        if (ctype or "").upper() == "AREA":
            # Conversation nodes complete when their authored conversation
            # closes. Repeatable authored conversations remain actionable so
            # returning to the node selects their repeat/state variant.
            current = state.get("ALoc")
            if current:
                current_data = next(
                    ((loc.get("Data") or {}) for loc in state.get("VisLocs", [])
                     if current in ((loc.get("Data") or {}).get("node"),
                                    (loc.get("Data") or {}).get("name"))),
                    {})
                repeatable_convo = (
                    str(template_name or "").upper() == "AZ1" and
                    current_data.get("type") == "Convo" and
                    bool(current_data.get("repeatable")))
                pre_encounter_convo = bool(current_data.get("pre_encounter"))
                if not repeatable_convo and not pre_encounter_convo:
                    _mark_location_completed(state, current)
                else:
                    # A repeatable node must not auto-trigger again while the
                    # client is still parked on it after the conversation
                    # closes. StartLoc sets this back to true on re-entry.
                    current_data["autostart"] = False
                _note_visited(state, current)
                if (str(template_name or "").upper() == "AZ1" and
                        current_data.get("type") == "Convo"):
                    pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
                    visits = pdata.setdefault("conversation_visits", {})
                    node_key = str(current_data.get("node") or current)
                    try:
                        visits[node_key] = int(visits.get(node_key, 0) or 0) + 1
                    except (TypeError, ValueError):
                        visits[node_key] = 1
                # Quest assignment is driven by the extracted conversation
                # catalog.  One conversation may grant several quests (for
                # example Tamed plus the faction Find quest).
                if str(template_name or "").upper() in {"AZ1", "AZ2"}:
                    spawned, _hooks = _grant_quests_for_conversation(
                        db, champ_id, str(template_name).upper(),
                        current_data.get("conversationId"))
                    for quest_id, quest_script, quest_state in spawned:
                        pending_quest_spawns.append(
                            (quest_id, quest_script, quest_state))
                    _sync_az1_quest_gates(db, champ_id, state)
                    _apply_az1_quest_markers(db, champ_id, state)
                # A pre-encounter conversation promotes its location back to
                # the authored battle scene. Other conversations simply clear
                # the active location after closing.
                if current_data.get("pre_encounter"):
                    current_data.update({
                        "type": "Encounter",
                        "conversationId": None,
                        "completed": False,
                        "pre_encounter_completed": True,
                        "autostart": False,
                    })
                state["ALoc"] = None
            state["CurState"] = "EXPLORE"
            db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                       (json.dumps(state), camp_id))
            db.commit()
            resp = _build_input_response(camp_id, state, success=True)
            ret = _send_response(handler, json.dumps(resp), comp, session_id,
                                 reqid, target, instance, conh, uid)
            for quest_id, quest_script, quest_state in pending_quest_spawns:
                push_campspawn(handler, quest_id, champ_id, quest_script,
                               quest_state, camp_id, "AZ1", comp, session_id,
                               target, instance, conh, uid)
            return ret
        current = state.get("ALoc")
        # After Crayburn completion the report conversation is shown in the
        # race panorama.  Its completion must not restart the castle; instead
        # expose the original race NPC with the authored Feralroot travel
        # conversation and finish the linked journal objective.
        if ((ctype or "").upper() == "PANORAMA" and
                current == quest_npc and state.get("PostCrayburnReport")):
            report_conv = next(
                ((loc.get("Data") or {}).get("conversationId")
                 for loc in state.get("VisLocs", [])
                 if (loc.get("Data") or {}).get("node") == current),
                None,
            )
            report_applied = _apply_conversation_rewards(
                handler, db, camp_id, state, report_conv)
            _mark_location_completed(state, current)
            transition = _activate_az1_transition(db, champ_id, cfg)
            if transition:
                _pano_id, state = transition
            qrow = db.execute(
                "SELECT id, state_json FROM campaigns "
                "WHERE champion_id=? AND campaign_type='QUEST' "
                "ORDER BY id DESC LIMIT 1", (champ_id,)).fetchone()
            if qrow and qrow[1]:
                qstate = json.loads(qrow[1])
                qcurrent = qstate.get("ALoc")
                if qcurrent:
                    _mark_location_completed(qstate, qcurrent)
                qstate["Finished"] = _now_utc()
                qstate["FinishReason"] = "Complete"
                qstate["ALoc"] = None
                qstate["CurState"] = "EXPLORE"
                db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                           (json.dumps(qstate), qrow[0]))
                db.commit()
            resp = _build_input_response(camp_id, state, success=True,
                                         applied=report_applied)
            return _send_response(handler, json.dumps(resp), comp, session_id,
                                  reqid, target, instance, conh, uid)
        if (current == intro_npc and state.get("PostCrayburnReport")):
            _mark_location_completed(state, current)
            state["ALoc"] = None
        elif current == intro_npc:
            _mark_location_completed(state, current)
            _append_location(_convo_location(
                trainer_npc, cfg["battle_conv"], givequest=True))
            # The player must find the trainer on the panorama; do NOT
            # auto-launch their conversation. Clear ALoc so ProcessStateChange
            # returns to explore mode and the panorama hint guides the player.
            state["ALoc"] = None
        elif current == trainer_npc:
            if state.pop("TrainingVictoryPending", False):
                _mark_location_completed(state, current)
                for loc in state.get("VisLocs", []):
                    data = loc.get("Data", {})
                    if data.get("node") == trainer_npc or data.get("name") == trainer_npc:
                        data.update({"visible": False, "completed": True,
                                     "enabled": False})
                _append_location(_convo_location(
                    quest_npc, cfg["quest_conv"], givequest=True))
                state["ALoc"] = None
            elif state.get("PublicState", {}).get("Data", {}).get("RaceTutorialBattleUnlocked"):
                # Convert the trainer's own location into the training
                # encounter. The client's ProcessStateChange only finds a
                # location to auto-trigger when it is bound to an NPC
                # GameObject (ALoc must equal the NPC's location Name), so we
                # must keep this on the trainer NPC node rather than creating a
                # separate unbound node.
                for loc in state.get("VisLocs", []):
                    data = loc.get("Data", {})
                    if data.get("node") == trainer_npc or data.get("name") == trainer_npc:
                        data["type"] = "Encounter"
                        data["encounter"] = cfg["training_encounter"]
                        data["conversationId"] = None
                        data["completed"] = False
                        break
                state["ALoc"] = trainer_npc
            else:
                # Declined the spar. Return to the panorama with the trainer
                # still visible/talkable. Clear ALoc so the client doesn't
                # auto-relaunch the conversation (ProcessStateChange relaunches
                # when ALoc matches a Convo location's Name).
                state["ALoc"] = None
        elif current == quest_npc:
            # Quest conversation finished. The quest-giver (e.g. Colonel
            # Sterling / Margugram) handed off the Crayburn Castle quest;
            # transition the campaign to the castle. We reuse this same
            # campaign row by switching its type and rebuilding the
            # GameplayState (currently a PANORAMA so the client uses its
            # working panorama renderer — the DUNGEON map renderer hits a
            # missing 'DungeonMapNode' script).
            _mark_location_completed(state, current)
            state["ALoc"] = None
            dungeon_id, _dungeon_uid, _dungeon_started, dungeon_state = \
                _find_campaign_for_champion(db, champ_id, "DUNGEON")
            if not dungeon_state or dungeon_state.get("TempType") != "DUNGEON":
                dungeon_state = _build_initial_gameplay_state(
                    dungeon_id, champ_id, "DUNGEON", _race_for_cfg(cfg))
            dungeon_state["CampID"] = dungeon_id
            dungeon_state["ChampID"] = champ_id
            dungeon_state["Started"] = dungeon_state.get("Started") or _now_utc()
            dungeon_state["CurState"] = "EXPLORE"
            db.execute(
                "UPDATE campaigns SET campaign_type='DUNGEON', "
                "template_name='Crayburn Castle', is_started=1, state_json=? "
                "WHERE id=?",
                (json.dumps(dungeon_state), dungeon_id)
            )
            # Ensure the dungeon's journal quest campaign exists (the client's
            # QuestMgr queries getactive with CampType=QUEST and resolves the
            # quest template by this campaign's TemplateName = script name).
            _ensure_quest_campaign(db, champ_id, "DUNGEON")
            # Send the transition only after the panorama response has been
            # delivered, and use the distinct child campaign ID so the
            # client's OnTransitionRequested actually loads the dungeon.
            pending_dungeon = (dungeon_id, dungeon_state)
    elif event_name == "enc_cancel":
        # The player clicked "Cancel" on the training battle panel.  Return to
        # the panorama with the trainer still available as an Encounter node.
        # Clear ALoc so ProcessStateChange doesn't auto-relaunch anything and
        # the panorama redisplays.
        state["ALoc"] = None
        state["CurState"] = "EXPLORE"
        for loc in state.get("VisLocs", []):
            data = loc.get("Data", {})
            if data.get("node") == trainer_npc or data.get("name") == trainer_npc:
                data["type"] = "Encounter"
                data["encounter"] = cfg["training_encounter"]
                data["conversationId"] = None
                data["completed"] = False
                data["enabled"] = True
                break
    elif event_name == "start":
        # The player pressed "Let's start" on the training encounter (or a
        # dungeon). Launch the battle by pushing a 'gamestarted' notification
        # with a SessionState pointing at the encounter scene.  The client's
        # handleNofityGameStarted transitions to EGameState.Battle and then
        # drives the LoadBalancer session flow (FindSession -> StartEncounter
        # -> Join -> battle events).
        _handle_campaign_start(handler, db, env_json, champ_id, cfg,
                               comp, session_id, target, instance, conh, uid)
        # _handle_campaign_start persists ActiveEncounterGuid so the later
        # battle result can resolve the authored scene rewards.  Reload the
        # state before the common response write below; otherwise this handler
        # would overwrite that field with its stale pre-launch snapshot.
        saved = db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (camp_id,)
        ).fetchone()
        if saved and saved[0]:
            try:
                state = json.loads(saved[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

    resp = _build_input_response(camp_id, state, success=True)
    db.execute(
        "UPDATE campaigns SET state_json=? WHERE id=?",
        (json.dumps(state), camp_id)
    )
    db.commit()
    ret = _send_response(handler, json.dumps(resp), comp, session_id,
                         reqid, target, instance, conh, uid)
    # Server-driven dungeon: after the quest-giver's conv_done response has
    # been delivered, start the castle chain by advancing one step from the
    # Entrance — which shows the first node's conversation (Watchtower). The
    # client processes the response then the cmpupdate/gamestarted in order,
    # so no pacing delay is needed.
    if pending_dungeon:
        dungeon_id, dungeon_state = pending_dungeon
        push_campupdate(handler, db, dungeon_id, champ_id, "quest_complete",
                        "DUNGEON", True, dungeon_state, comp, session_id,
                        target, instance, conh, uid)
        advance_crayburn_step(handler, db, dungeon_id, False, comp, session_id,
                              target, instance, conh, uid)
    if pending_area:
        area_id, area_state = pending_area
        push_campupdate(handler, db, area_id, champ_id, "feralroot_travel",
                        "AREA", True, area_state, comp, session_id, target,
                        instance, conh, uid)
    return ret


def _mark_quest_objective_retryable(db, champion_id, scene_guid):
    """Keep a quest encounter available when its optional condition failed."""
    if not champion_id or not scene_guid:
        return
    rows = db.execute(
        "SELECT id, state_json FROM campaigns "
        "WHERE champion_id=? AND campaign_type='QUEST' "
        "AND template_name='az01_tamed'", (champion_id,)).fetchall()
    for quest_id, raw in rows:
        try:
            state = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        changed = False
        for loc in state.get("VisLocs", []):
            data = loc.get("Data") or {}
            if str(data.get("encounter") or "") == str(scene_guid):
                data["completed"] = False
                data["repeatable"] = True
                data["enabled"] = True
                data["visible"] = True
                changed = True
        if changed:
            db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                       (json.dumps(state), quest_id))
    db.commit()


def _apply_gameend(db, camp_id, won):
    """Apply a campaign game-end result to the DB, return (camp_id, state).

    On a win of the training encounter, reveals the quest-giver NPC on the
    panorama. Returns (camp_id, GameplayState) or (None, None) if not found.
    """
    row = db.execute(
        "SELECT champion_id, state_json, campaign_type FROM campaigns WHERE id=?",
        (camp_id,)
    ).fetchone()
    if not row:
        return None, None

    champ_id, state_json, ctype = row
    if state_json:
        state = json.loads(state_json)
    else:
        state = _build_initial_gameplay_state(camp_id, champ_id, ctype or "AREA")

    cfg = None
    champ = _get_champion(db, champ_id)
    if champ:
        cfg = _az0_config(champ[2])
    if cfg is None:
        cfg = _az0_config(1)

    is_dungeon = (ctype or "").upper() == "DUNGEON"

    if won:
        quest_npc = cfg["quest_npc"]
        trainer_npc = cfg["trainer_npc"]
        success_conv = cfg.get("training_success_conv")
        # Queue the authored post-training victory conversation first.  The
        # conversation-complete handler performs the normal trainer hide and
        # quest-giver reveal after the player advances through it.
        if success_conv and trainer_npc and not is_dungeon:
            trainer_loc = None
            for loc in state.setdefault("VisLocs", []):
                data = loc.get("Data", {})
                if data.get("node") == trainer_npc or data.get("name") == trainer_npc:
                    trainer_loc = data
                    break
            if trainer_loc is None:
                state["VisLocs"].append(
                    _convo_location(trainer_npc, success_conv))
            else:
                trainer_loc.update({
                    "type": "Convo", "conversationId": success_conv,
                    "encounter": None, "completed": False,
                    "enabled": True, "visible": True,
                })
            state["TrainingVictoryPending"] = True
            state["ALoc"] = trainer_npc
        # Hide the trainer node (Iddi) — the training battle is done.
        if not state.get("TrainingVictoryPending"):
            for loc in state.get("VisLocs", []):
                data = loc.get("Data", {})
                if data.get("node") == trainer_npc or data.get("name") == trainer_npc:
                    data["visible"] = False
                    data["completed"] = True
        if not is_dungeon:
            # Only the panorama reveals the quest-giver; the dungeon has its
            # own node list and must not gain a stray panorama NPC location.
            if not any(l.get("Data", {}).get("node") == quest_npc
                       for l in state.setdefault("VisLocs", [])):
                state["VisLocs"].append(_convo_location(
                    quest_npc, cfg["quest_conv"], givequest=True))
        state["Wins"] = state.get("Wins", 0) + 1
        # The training battle has been won: stop treating the campaign as the
        # tutorial so later battles randomize the turn player instead of always
        # giving the player first turn.
        state["TutorialDone"] = True
    else:
        state["Losses"] = state.get("Losses", 0) + 1
        # Training encounters have an authored defeat conversation.  Return
        # to the trainer's panorama node with that conversation queued so the
        # client can play it immediately; the encounter remains retryable.
        fail_conv = cfg.get("training_fail_conv")
        trainer_npc = cfg.get("trainer_npc")
        if fail_conv and trainer_npc and not is_dungeon:
            trainer_loc = None
            for loc in state.setdefault("VisLocs", []):
                data = loc.get("Data", {})
                if data.get("node") == trainer_npc or data.get("name") == trainer_npc:
                    trainer_loc = data
                    break
            if trainer_loc is None:
                state["VisLocs"].append(
                    _convo_location(trainer_npc, fail_conv))
            else:
                trainer_loc.update({
                    "type": "Convo", "conversationId": fail_conv,
                    "encounter": None, "completed": False,
                    "enabled": True, "visible": True,
                })
            state["ALoc"] = trainer_npc

    # AREA encounters are attached directly to map locations. Conditional
    # quest encounters remain retryable until their condition succeeds; a
    # normal victory completes the location and reveals the next map node.
    if (ctype or "").upper() == "AREA":
        active_scene = str(state.get("ActiveEncounterGuid") or "")
        active_node = state.get("ALoc")
        condition_met = bool(state.pop("_last_encounter_condition_met", False))
        conditional = any(
            record.get("end_of_game_condition")
            for record in _scene_reward_records(db, active_scene))
        retryable = conditional and not condition_met
        matched_index = None
        for idx, loc in enumerate(state.get("VisLocs", [])):
            data = loc.get("Data") or {}
            if ((active_scene and str(data.get("encounter") or "") == active_scene)
                    or (active_node and active_node in (
                        data.get("node"), data.get("name"), data.get("title")))):
                data["completed"] = bool(won and not retryable)
                data["repeatable"] = bool(retryable)
                data["enabled"] = True
                data["visible"] = True
                matched_index = idx
                node_name = data.get("node") or data.get("name")
                if node_name:
                    state["LastNode"] = node_name
                    visited = state.setdefault("PublicState", {}).setdefault(
                        "Data", {}).setdefault("visited_nodes", [])
                    if node_name not in visited:
                        visited.append(node_name)
                break
        if retryable:
            _mark_quest_objective_retryable(db, champ_id, active_scene)
        if matched_index is not None:
            for loc in state.get("VisLocs", [])[matched_index + 1:]:
                data = loc.get("Data") or {}
                if not data.get("completed"):
                    data["visible"] = True
                    data["enabled"] = True
                    break
        state.pop("ActiveEncounterGuid", None)

    # Clear ALoc so the panorama doesn't auto-pop the encounter dialog when
    # the client returns from the battle. Use an empty string (not None) — the
    # client's ProcessStateChange won't match it to any node GameObject, and
    # portrait rendering resolves correctly because it's not a null ALoc.
    if not is_dungeon:
        state["ALoc"] = ""

    db.execute(
        "UPDATE campaigns SET state_json=? WHERE id=?",
        (json.dumps(state), camp_id)
    )
    db.commit()
    return camp_id, state


def push_campupdate(handler, db, camp_id, champ_id, reason, temptype, transition,
                    state, comp, session_id, target, instance, conh, uid):
    """Push a 'cmpupdate' Campaign notification to force a panorama/dungeon rebuild.

    The client's HandleOnNotifyCampUpdate processes cmpupdate; when Transition
    is true it calls OnTransitionRequested which triggers the dungeon load.
    """
    uid_int = uid
    if hasattr(uid, 'to_uint64'):
        uid_int = uid.to_uint64()
    reck_id = getattr(handler, "client_reck_id", "0") or "0"
    env_json = {
        "ReckID": int(reck_id),
        "CampID": camp_id,
        "ChampID": champ_id,
        "Reason": reason,
        "TemplateType": temptype,
        "Transition": transition,
        "State": state,
        "Applied": _empty_applied_updates(),
        "RequestType": "cmpupdate",
    }
    return _push_campaign_notify(handler, env_json, comp, session_id,
                                 target, instance, conh, uid_int)


def push_campspawn(handler, camp_id, champ_id, template_name, state,
                   spawned_by, spawned_by_template, comp, session_id,
                   target, instance, conh, uid):
    """Push a quest-spawn notification so the client creates a new journal entry.

    ``cmpupdate`` only updates quests already cached by ClientQuestManager;
    newly granted quests require the CampSpawnNotify/campspawn message.
    """
    uid_int = uid
    if hasattr(uid, 'to_uint64'):
        uid_int = uid.to_uint64()
    reck_id = getattr(handler, "client_reck_id", "0") or "0"
    env_json = {
        "ReckID": int(reck_id),
        "CampID": camp_id,
        "ChampID": champ_id,
        "TemplateType": "QUEST",
        "TemplateName": template_name,
        "SpawnedBy": spawned_by,
        "SpawnedByTemplate": spawned_by_template,
        "Transition": False,
        "ChildState": state,
        "RequestType": "campspawn",
    }
    return _push_campaign_notify(handler, env_json, comp, session_id,
                                 target, instance, conh, uid_int)


def push_gameendnotify(handler, db, camp_id, won, comp, session_id,
                       target, instance, conh, service_mail_uid, applied=None):
    """Push a 'gameendnotify' Campaign notification to the client.

    Used by the !game_end debug command and by the real gameend flow to
    deliver the updated GameplayState after a campaign battle.
    """
    cid, state = _apply_gameend(db, camp_id, won)
    if state is None:
        return "    Campaign not found for gameend"

    champ_id = state.get("ChampID", 0)
    champ = _get_champion(db, champ_id)
    env_json = {
        "ReckID": 0,
        "CampID": camp_id,
        "RequestType": "gameendnotify",
        "ChampID": champ_id,
        "GameState": state,
        "Applied": applied or _empty_applied_updates(),
        "WinLose": bool(won),
    }
    return _push_campaign_notify(handler, env_json, comp, session_id,
                                 target, instance, conh, service_mail_uid)


def _scene_reward_records(db, scene_guid):
    """Return authored end-of-game reward records for one encounter scene.

    ``rewards_json`` is intentionally data shaped.  The evaluator below only
    knows condition types; it does not know encounter or card names.
    """
    if not scene_guid:
        return []
    row = db.execute("SELECT rewards_json FROM encounter_scenes WHERE guid=?",
                     (str(scene_guid),)).fetchone()
    if not row or not row[0]:
        return []
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    # Support both the compact single-record shape used by authored scenes
    # and a list for scenes which eventually have several conditional rewards.
    records = data.get("end_of_game_rewards")
    if records is None:
        records = data.get("end_of_game")
    if records is None and (data.get("end_of_game_condition")
                            or data.get("card_guid")
                            or data.get("gold") is not None
                            or data.get("xp") is not None):
        records = [data]
    if isinstance(records, dict):
        records = [records]
    return [record for record in (records or []) if isinstance(record, dict)]


def _scene_reward_metadata(db, scene_guid):
    """Return the complete authored rewards_json object for a scene."""
    if not scene_guid:
        return {}
    row = db.execute("SELECT rewards_json FROM encounter_scenes WHERE guid=?",
                     (str(scene_guid),)).fetchone()
    if not row or not row[0]:
        return {}
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _evaluate_encounter_condition(db, session_id, player_user_id, condition):
    """Evaluate one metadata condition against the finished battle state.

    The returned mapping is the condition context used by reward templates.
    ``void_tamed_troop`` deliberately checks the persisted IntAttr marker,
    not card text or a particular creature name.
    """
    if not isinstance(condition, dict):
        return None
    ctype = str(condition.get("type") or "").strip().lower()
    if ctype != "void_tamed_troop":
        return None
    owner = str(condition.get("owner") or "opponent").strip().lower()
    owner_sql = "gc.user_id<>?"
    if owner in ("player", "self", "champion"):
        owner_sql = "gc.user_id=?"
    rows = db.execute(
        "SELECT gc.template_guid, gc.permanent_buffs "
        "FROM game_cards gc "
        "WHERE gc.session_id=? AND LOWER(COALESCE(gc.location,''))='void' "
        "AND LOWER(COALESCE(gc.card_type,'')) LIKE '%troop%' "
        "AND " + owner_sql + " ORDER BY gc.card_uid",
        (int(session_id), int(player_user_id))).fetchall()
    for template_guid, permanent_buffs in rows:
        try:
            buffs = json.loads(permanent_buffs or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        int_attrs = buffs.get("int_attrs", {}) if isinstance(buffs, dict) else {}
        try:
            tamed = int(int_attrs.get("Tamed", 0) or 0)
        except (TypeError, ValueError):
            tamed = 0
        if tamed > 0:
            return {"template_guid": template_guid, "owner": owner}
    return None


def _resolve_reward_template(template, condition_context):
    """Resolve a metadata reward template, including ``$condition.*``."""
    if not isinstance(template, str):
        return template
    if not template.startswith("$condition."):
        return template
    key = template[len("$condition."):]
    return (condition_context or {}).get(key)


def _grant_card_reward(handler, db, user_id, template_guid, quantity=1,
                       emit=True):
    """Add card copies to a collection and optionally emit client rewards.

    Encounter rewards and card-choice locations must use the same collection,
    card-instance, card-cache, and reward-popup path.  Keeping that work here
    also ensures a card choice is not merely recorded in the database without
    becoming visible to the client.
    """
    if user_id is None or not template_guid:
        return []
    try:
        quantity = max(1, int(quantity or 1))
    except (TypeError, ValueError):
        quantity = 1
    template_guid = str(template_guid)
    template = db.execute(
        "SELECT name, cost, attack, defense FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    if not template:
        return []
    granted = []
    for _ in range(quantity):
        existing = db.execute(
            "SELECT id FROM collections "
            "WHERE user_id=? AND card_template_id=?",
            (user_id, template_guid)).fetchone()
        if existing:
            db.execute("UPDATE collections SET quantity=quantity+1 WHERE id=?",
                       (existing[0],))
        else:
            db.execute(
                "INSERT INTO collections (user_id, card_template_id, quantity) "
                "VALUES (?,?,1)", (user_id, template_guid))
        max_row = db.execute(
            "SELECT COALESCE(MAX(instance_id), 5000) FROM card_instances "
            "WHERE user_id=?", (user_id,)).fetchone()
        instance_id = int(max_row[0] or 5000) + 1
        db.execute(
            "INSERT OR IGNORE INTO card_instances "
            "(user_id, instance_id, template_guid) VALUES (?,?,?)",
            (user_id, instance_id, template_guid))
        granted.append({
            "guid": template_guid, "name": template[0] or "Card",
            "cost": template[1] or 0, "attack": template[2] or 0,
            "defense": template[3] or 0, "instance_id": instance_id,
        })
    db.commit()
    if emit and granted:
        cards = [(x["guid"], x["name"], x["cost"], x["attack"],
                  x["defense"], x["instance_id"], 0) for x in granted]
        if hasattr(handler, "push_opened_cards_via_generic"):
            handler.push_opened_cards_via_generic(cards)
        if hasattr(handler, "push_display_rewards"):
            handler.push_display_rewards([
                {"id": str(x["instance_id"]), "template": x["guid"],
                 "quantity": 1, "type": "CARD", "ledger_id": 0,
                 "boa": False}
                for x in granted])
    return granted


def _apply_conversation_rewards(handler, db, camp_id, state,
                                conversation_guid):
    """Apply a conversation's authored reward and return AppliedUpdates.

    Conversation completion is a campaign request, so its rewards must be
    returned in the InputResponse's ``Applied`` payload.  This is the same
    payload used by the campaign loot window after a battle.  Claims live in
    campaign state so reconnects or a repeated ``conv_done`` cannot duplicate
    a one-time reward.
    """
    applied = _empty_applied_updates()
    if not conversation_guid:
        return applied
    row = db.execute(
        "SELECT reward_json, one_time, enabled FROM conversation_rewards "
        "WHERE conversation_guid=?", (str(conversation_guid),)
    ).fetchone()
    if not row or not row[2]:
        return applied
    reward_raw, one_time, _enabled = row
    try:
        reward = json.loads(reward_raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        reward = {}
    if not isinstance(reward, dict):
        reward = {}
    claims = state.setdefault("_conversation_reward_claims", {})
    if not isinstance(claims, dict):
        claims = {}
        state["_conversation_reward_claims"] = claims
    champ_id = int(state.get("ChampID") or 0)
    champ = _get_champion(db, champ_id)
    if not champ:
        return applied
    user_id = champ[1]
    # A campaign template can be replayed by several champions.  One-time
    # conversation rewards therefore belong to the champion, not the shared
    # conversation/campaign template.  Accept the legacy unscoped key so a
    # reward already claimed by an older server build is not duplicated.
    claim_key = f"{champ_id}:{conversation_guid}"
    if bool(one_time) and (claims.get(claim_key) or claims.get(str(conversation_guid))):
        return applied
    try:
        gold = max(0, int(reward.get("gold", 0) or 0))
    except (TypeError, ValueError):
        gold = 0
    try:
        xp = max(0, int(reward.get("xp", 0) or 0))
    except (TypeError, ValueError):
        xp = 0

    if gold:
        urow = db.execute(
            "SELECT gold, platinum FROM users WHERE id=?", (user_id,)
        ).fetchone()
        old_gold = int(urow[0] or 0) if urow else 0
        platinum = int(urow[1] or 0) if urow else 0
        new_gold = old_gold + gold
        db.execute("UPDATE users SET gold=? WHERE id=?", (new_gold, user_id))
        applied["Accounts"].append({"Account": {
            "Gold": new_gold, "Platinum": platinum,
        }})
        applied["Completed"].append({
            "ItemKind": "GOLD", "ItemQuantity": gold,
            "ItemAction": "GRANT", "ItemTemplate": "", "RCode": "GOLD",
        })

    if xp:
        crow = db.execute(
            "SELECT id, champion_name, level, xp, champion_class, race, gender, "
            "last_campaign_id, last_deck_id, is_deleted, pet_name "
            "FROM champions WHERE id=?", (champ_id,)
        ).fetchone()
        if crow:
            new_xp = int(crow[3] or 0) + xp
            thresholds = (0, 1000, 2800, 5000, 8000, 12000, 17500,
                          25000, 40000, 50000, 60000, 70000, 80000,
                          92000, 110000)
            new_level = max(1, min(len(thresholds),
                                   1 + sum(new_xp >= n for n in thresholds[1:])))
            db.execute("UPDATE champions SET xp=?, level=? WHERE id=?",
                       (new_xp, new_level, champ_id))
            applied["Champions"].append({"Champ": {
                "Id": int(crow[0]), "Name": crow[1] or "",
                "Level": new_level, "CurrentXP": new_xp,
                "ChampionClass": int(crow[4] or 0), "Race": int(crow[5] or 0),
                "Gender": int(crow[6] or 0),
                "LastCampaignID": int(crow[7] or 0),
                "LastDeckID": int(crow[8] or 0), "IsDeleted": bool(crow[9]),
                "LastRespec": 0, "FreeRespec": 0, "PetName": crow[10] or "",
            }})
            applied["Completed"].append({
                "ItemKind": "XP", "ItemQuantity": xp,
                "ItemAction": "GRANT", "ItemTemplate": "", "RCode": "XP",
            })

    card_specs = reward.get("cards") or []
    if reward.get("card_guid"):
        card_specs = list(card_specs) + [{
            "guid": reward.get("card_guid"),
            "quantity": reward.get("quantity", 1),
        }]
    if isinstance(card_specs, dict):
        card_specs = [card_specs]
    for spec in card_specs:
        if isinstance(spec, str):
            spec = {"guid": spec}
        if not isinstance(spec, dict):
            continue
        template_guid = spec.get("guid") or spec.get("template") \
            or spec.get("card_guid")
        try:
            quantity = max(1, int(spec.get("quantity", 1) or 1))
        except (TypeError, ValueError):
            quantity = 1
        for card in _grant_card_reward(handler, db, user_id, template_guid,
                                       quantity, emit=False):
            applied["Cards"].append({"Card": {
                "Id": int(card["instance_id"]), "TemplateID": card["guid"],
                "CardStats": {}, "IsFoil": False, "IsExtended": False,
                "SocketedGems": 0, "IsNotTradeable": False,
                "EscrowStatus": "Clean",
            }})
            applied["Completed"].append({
                "ItemKind": "BOACARD", "ItemQuantity": 1,
                "ItemAction": "GRANT", "ItemTemplate": card["guid"],
                "RCode": "CARD",
            })

    # Promotional Crayburn rewards are real treasure chests, not merely a
    # display-only loot entry.  Persist the chest so it survives reconnects,
    # and return inventory_bits in Applied.Items so the campaign client adds
    # it immediately to InventoryChests and the pack-opening screen.
    chest_guid = (reward.get("chest_guid") or reward.get("chest_template")
                  or reward.get("pack_guid"))
    if chest_guid:
        chest_guid = str(chest_guid)
        chest_row = db.execute(
            "INSERT INTO treasure_chests "
            "(user_id, set_guid, chest_rarity, opened, template_guid) "
            "VALUES (?, ?, 'Promo', 0, ?)",
            (user_id, "00000000-0000-0000-0000-000000000000", chest_guid),
        )
        chest_id = int(chest_row.lastrowid)
        inventory_id = 9000 + chest_id
        applied["Items"].append({"Item": {
            "Id": inventory_id,
            "TemplateID": chest_guid,
            "BoundToProfile": True,
            "ItemQuantity": 1,
            "ClaimDate": "0001-01-01T00:00:00",
            "EscrowStatus": "Clean",
        }})
        applied["Completed"].append({
            "ItemKind": "BOAITEM", "ItemQuantity": 1,
            "ItemAction": "GRANT", "ItemTemplate": chest_guid,
            "RCode": "CHEST",
        })

    if bool(one_time):
        claims[claim_key] = True
    db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
               (json.dumps(state), camp_id))
    db.commit()
    getattr(handler, "_log_req", print)(
        f"    Conversation reward: conversation={conversation_guid} "
        f"gold={gold} xp={xp} cards={len(applied['Cards'])} "
        f"items={len(applied['Items'])}")
    return applied


def _scene_card_choices(db, scene_guid):
    """Return card GUIDs offered by a scene's authored card_choice reward."""
    if not scene_guid:
        return []
    row = db.execute(
        "SELECT rewards_json FROM encounter_scenes WHERE guid=?",
        (str(scene_guid),)).fetchone()
    if not row or not row[0]:
        return []
    try:
        rewards = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(rewards, dict):
        return []
    choices = rewards.get("card_choice") or []
    return [str(item["guid"]) for item in choices
            if isinstance(item, dict) and item.get("guid")]


def _area_scene_guid(db, state):
    """Resolve the encounter scene attached to the active AREA location."""
    current = state.get("ALoc")
    if not current:
        return None
    for loc in state.get("VisLocs", []):
        data = loc.get("Data") or {}
        if current in (data.get("node"), data.get("name"), data.get("title")):
            if data.get("encounter"):
                return str(data["encounter"])
            node = data.get("node") or current
            match = re.search(r"NODE[_ ]?0*(\d+)", str(node), re.I)
            if match:
                rows = db.execute(
                    "SELECT guid, name FROM encounter_scenes "
                    "WHERE name LIKE 'AZ 1 - NODE %'"
                ).fetchall()
                for guid, name in rows:
                    if re.search(r"NODE[_ ]?0*%s\b" % int(match.group(1)),
                                 name or "", re.I):
                        return str(guid)
            break
    return None


def _apply_encounter_end_rewards_legacy(handler, db, session, camp_id, won):
    """Apply authored conditional encounter rewards before session cleanup.

    One-time records are claimed once per campaign/scene; repeatable records
    are keyed by battle session so later runs can award a new capture while
    duplicate end-game notifications cannot grant the same capture twice.
    """
    if not won:
        return []
    row = db.execute(
        "SELECT champion_id, state_json FROM campaigns WHERE id=?",
        (camp_id,)).fetchone()
    if not row:
        return []
    champion_id, state_json = row
    try:
        state = json.loads(state_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        state = {}
    scene_guid = state.get("ActiveEncounterGuid")
    if not scene_guid:
        scene_guid = _last_encounter_scene.get(session.session_name or "")
    records = _scene_reward_records(db, scene_guid)
    if not records:
        return []
    champ = _get_champion(db, champion_id)
    player_user_id = champ[1] if champ else None
    if player_user_id is None:
        return []

    claims = state.setdefault("_encounter_reward_claims", {})
    if not isinstance(claims, dict):
        claims = {}
        state["_encounter_reward_claims"] = claims
    granted = []
    for record_index, record in enumerate(records):
        reward_obj = record.get("reward")
        one_time = record.get("one_time")
        if one_time is None and isinstance(reward_obj, dict):
            one_time = reward_obj.get("one_time")
        one_time = True if one_time is None else bool(one_time)
        # One-time rewards are claimed once per campaign/scene.  Repeatable
        # rewards are still deduplicated for a single battle session, while a
        # later repeatable encounter receives its own session-scoped claim.
        claim_key = (f"{scene_guid}:{record_index}" if one_time else
                     f"{scene_guid}:{record_index}:{session.session_id}")
        if claims.get(claim_key):
            continue
        condition_context = _evaluate_encounter_condition(
            db, session.session_id, player_user_id,
            record.get("end_of_game_condition"))
        if not condition_context:
            continue
        reward_type = str(record.get("reward_type") or
                          (reward_obj.get("type") if isinstance(reward_obj, dict) else None) or
                          "CARD").upper()
        if reward_type != "CARD":
            # Keep this evaluator extensible without silently treating an
            # unknown reward as a card.  Gold/plat can be added as additional
            # RewardResult types without changing condition handling.
            continue
        reward_obj = reward_obj if isinstance(reward_obj, dict) else record
        template_guid = _resolve_reward_template(
            reward_obj.get("card_guid") or reward_obj.get("template"),
            condition_context)
        if not template_guid:
            continue
        try:
            quantity = max(1, int(reward_obj.get("quantity", 1) or 1))
        except (TypeError, ValueError):
            quantity = 1
        new_cards = _grant_card_reward(
            handler, db, player_user_id, template_guid, quantity, emit=False)
        if new_cards:
            granted.extend(new_cards)
            claims[claim_key] = True
    if not granted:
        return []
    db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
               (json.dumps(state), camp_id))
    db.commit()
    cards = [(x["guid"], x["name"], x["cost"], x["attack"], x["defense"],
              x["instance_id"], 0) for x in granted]
    if hasattr(handler, "push_opened_cards_via_generic"):
        handler.push_opened_cards_via_generic(cards)
    if hasattr(handler, "push_display_rewards"):
        handler.push_display_rewards([
            {"id": str(x["instance_id"]), "template": x["guid"],
             "quantity": 1, "type": "CARD", "ledger_id": 0, "boa": False}
            for x in granted])
    getattr(handler, "_log_req", print)(
        f"    Encounter reward: granted {len(granted)} captured card(s) "
        f"from scene {scene_guid}")
    return granted


def _apply_encounter_end_rewards(handler, db, session, camp_id, won):
    """Apply authored encounter rewards and return campaign AppliedUpdates.

    Campaign clients build their loot window from ``gameendnotify.Applied``;
    generic profile reward events are not sufficient here.
    """
    result = {"applied": _empty_applied_updates(), "cards": [],
              "gold": 0, "xp": 0, "condition_met": False,
              "scene_guid": None}
    if not won:
        return result
    row = db.execute("SELECT champion_id, state_json FROM campaigns WHERE id=?",
                     (camp_id,)).fetchone()
    if not row:
        return result
    champion_id, state_json = row
    try:
        state = json.loads(state_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        state = {}
    scene_guid = state.get("ActiveEncounterGuid") or _last_encounter_scene.get(
        session.session_name or "")
    records = _scene_reward_records(db, scene_guid)
    if not records:
        return result
    result["scene_guid"] = str(scene_guid) if scene_guid else None
    champ = _get_champion(db, champion_id)
    player_user_id = champ[1] if champ else None
    if player_user_id is None:
        return result
    claims = state.setdefault("_encounter_reward_claims", {})
    if not isinstance(claims, dict):
        claims = {}
        state["_encounter_reward_claims"] = claims
    granted, total_gold, total_xp = [], 0, 0
    for record_index, record in enumerate(records):
        reward_obj = record.get("reward")
        reward_obj = reward_obj if isinstance(reward_obj, dict) else record
        one_time = record.get("one_time")
        if one_time is None:
            one_time = reward_obj.get("one_time", True)
        one_time = bool(one_time)
        claim_key = (f"{scene_guid}:{record_index}" if one_time else
                     f"{scene_guid}:{record_index}:{session.session_id}")
        if claims.get(claim_key):
            continue
        condition = record.get("end_of_game_condition")
        context = ({"owner": "player"} if not condition else
                   _evaluate_encounter_condition(
                       db, session.session_id, player_user_id, condition))
        if condition and context:
            result["condition_met"] = True
        try:
            gold = max(0, int(reward_obj.get("gold", 0) or 0))
        except (TypeError, ValueError):
            gold = 0
        try:
            xp = max(0, int(reward_obj.get("xp", 0) or 0))
        except (TypeError, ValueError):
            xp = 0
        # A scene can have unconditional completion currency alongside a
        # conditional capture card (Wild Cub is authored this way). Only the
        # card template is gated by end_of_game_condition.
        template = reward_obj.get("card_guid") or reward_obj.get("template")
        template_guid = (_resolve_reward_template(template, context)
                         if (not condition or context) else None)
        cards = []
        if template_guid:
            try:
                quantity = max(1, int(reward_obj.get("quantity", 1) or 1))
            except (TypeError, ValueError):
                quantity = 1
            cards = _grant_card_reward(handler, db, player_user_id,
                                       template_guid, quantity, emit=False)
        if not cards and not gold and not xp:
            continue
        granted.extend(cards)
        total_gold += gold
        total_xp += xp
        claims[claim_key] = True

    account = None
    if total_gold:
        urow = db.execute("SELECT gold, platinum FROM users WHERE id=?",
                          (player_user_id,)).fetchone()
        old_gold = int(urow[0] or 0) if urow else 0
        platinum = int(urow[1] or 0) if urow else 0
        new_gold = old_gold + total_gold
        db.execute("UPDATE users SET gold=? WHERE id=?",
                   (new_gold, player_user_id))
        account = {"Gold": new_gold, "Platinum": platinum}

    champion_bits = None
    if total_xp:
        crow = db.execute(
            "SELECT id, champion_name, level, xp, champion_class, race, gender, "
            "last_campaign_id, last_deck_id, is_deleted, pet_name "
            "FROM champions WHERE id=?",
            (champion_id,)).fetchone()
        if crow:
            new_xp = int(crow[3] or 0) + total_xp
            thresholds = (0, 1000, 2800, 5000, 8000, 12000, 17500,
                          25000, 40000, 50000, 60000, 70000, 80000,
                          92000, 110000)
            new_level = max(1, min(len(thresholds),
                                   1 + sum(new_xp >= n for n in thresholds[1:])))
            db.execute("UPDATE champions SET xp=?, level=? WHERE id=?",
                       (new_xp, new_level, champion_id))
            champion_bits = {
                "Id": int(crow[0]), "Name": crow[1] or "",
                "Level": new_level, "CurrentXP": new_xp,
                "ChampionClass": int(crow[4] or 0), "Race": int(crow[5] or 0),
                "Gender": int(crow[6] or 0),
                "LastCampaignID": int(crow[7] or 0),
                "LastDeckID": int(crow[8] or 0), "IsDeleted": bool(crow[9]),
                "LastRespec": 0, "FreeRespec": 0,
                "PetName": crow[10] or "",
            }

    applied = result["applied"]
    if account:
        applied["Accounts"].append({"Account": account})
    if champion_bits:
        applied["Champions"].append({"Champ": champion_bits})
    if total_gold:
        applied["Completed"].append({"ItemKind": "GOLD", "ItemQuantity": total_gold,
                                      "ItemAction": "GRANT", "ItemTemplate": "",
                                      "RCode": "GOLD"})
    if total_xp:
        applied["Completed"].append({"ItemKind": "XP", "ItemQuantity": total_xp,
                                      "ItemAction": "GRANT", "ItemTemplate": "",
                                      "RCode": "XP"})
    for card in granted:
        applied["Cards"].append({"Card": {
            "Id": int(card["instance_id"]), "TemplateID": card["guid"],
            "CardStats": {}, "IsFoil": False, "IsExtended": False,
            "SocketedGems": 0, "IsNotTradeable": False,
            "EscrowStatus": "Clean",
        }})
        applied["Completed"].append({
            "ItemKind": "BOACARD", "ItemQuantity": 1,
            "ItemAction": "GRANT", "ItemTemplate": card["guid"],
            "RCode": "CARD",
        })
    if not granted and not total_gold and not total_xp:
        state["_last_encounter_condition_met"] = bool(result["condition_met"])
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), camp_id))
        db.commit()
        return result
    state["_last_encounter_condition_met"] = bool(result["condition_met"])
    db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
               (json.dumps(state), camp_id))
    db.commit()
    result.update({"cards": granted, "gold": total_gold, "xp": total_xp})
    getattr(handler, "_log_req", print)(
        f"    Encounter reward: scene={scene_guid} cards={len(granted)} "
        f"gold={total_gold} xp={total_xp}")
    return result


def handle_battle_gameend(handler, db, session, won, service_mail_uid,
                          service_campaign_uid_type=253):
    """Apply a finished PvE/FRA battle and clean up its game session.

    This is intentionally kept in the campaign module because the battle
    protocol only reports the result; campaign progression, notifications, and
    session lifecycle are service concerns.  FRA persistence remains here as
    the same result path is shared by practice and campaign battles.
    """
    try:
        session_name = session.session_name or ""
        from db import db_record_arena_fight, db_delete_game_session
        if not session_name.startswith("camp_"):
            profile = getattr(handler, "user_profile", None)
            if not session_name.startswith("tourney-") and profile:
                db_record_arena_fight(profile["id"], won)
                db_delete_game_session(session.session_id)
                getattr(handler, "_log_req", print)(
                    f"    FRA result recorded (won={won}); session cleaned")
                return True
            return False
        camp_id = int(session_name[5:])
        reward_result = _apply_encounter_end_rewards(
            handler, db, session, camp_id, won)
        advanced_quest_states = []
        camp_row = db.execute(
            "SELECT champion_id FROM campaigns WHERE id=?", (camp_id,)
        ).fetchone()
        if reward_result.get("condition_met"):
            # Taming objectives are separate QUEST campaign entries linked by
            # encounter GUID. Advance that objective only after the finished
            # battle actually contains a captured troop.
            if camp_row:
                _advance_quest_campaign(
                    db, camp_row[0], "az01_tamed", reward_result.get("scene_guid"))
        if won:
            advanced_quest_states = _advance_quest_encounter_objectives(
                db, camp_row[0] if camp_row else 0,
                reward_result.get("scene_guid"))
        push_gameendnotify(
            handler, db, camp_id, won, 0,
            "00000000-0000-0000-0000-000000000000",
            "ServiceCampaign", str(service_campaign_uid_type), 0,
            service_mail_uid, applied=reward_result.get("applied"))
        for quest_state in advanced_quest_states:
            push_campupdate(
                handler, db, quest_state.get("CampID") or 0,
                quest_state.get("ChampID") or (camp_row[0] if camp_row else 0),
                "quest_progress", "QUEST", False, quest_state, 0,
                "00000000-0000-0000-0000-000000000000", "ServiceCampaign",
                str(service_campaign_uid_type), 0, service_mail_uid)
        getattr(handler, "_log_req", print)(
            f"    Campaign {camp_id}: gameendnotify pushed (won={won})")
        try:
            advance_crayburn_step(
                handler, db, camp_id, won, 0,
                "00000000-0000-0000-0000-000000000000",
                "ServiceCampaign", str(service_campaign_uid_type), 0,
                service_mail_uid, auto_activate=False)
        except Exception as exc:
            getattr(handler, "_log_req", print)(
                f"    Crayburn advance failed: {exc}")
        db_delete_game_session(session.session_id)
        getattr(handler, "_log_req", print)(
            "    PvE game session and game cards cleaned")
        return True
    except Exception as exc:
        getattr(handler, "_log_req", print)(
            f"    gameendnotify failed: {exc}")
        return False


def _launch_encounter(handler, db, camp_id, champ_id, encounter_guid,
                      deck_uid64, comp, session_id, target, instance, conh, uid):
    """Push a 'gamestarted' notification launching an encounter battle.

    Shared by the campaign 'start' SendEvent and the 'camp.encounter' cheat.
    Builds a SessionState whose EncounterData.SceneTemplateId is the encounter
    scene and pushes it; the client transitions to EGameState.Battle and then
    drives the LoadBalancer session flow (FindSession -> StartEncounter ->
    Join -> battle events).

    The battle session itself is NOT created here — StartEncounter (22017)
    allocates the authoritative DB-backed session. The scene GUID is also
     recorded so resolve_encounter can resolve the correct AI deck/name
    when the battle session is later set up.
    """
    from encoder import encode_campaign_session_state
    import base64

    log = getattr(handler, "_log_req", print)
    scene_row = db.execute(
        "SELECT guid, name, title, gameboard, ai_deck_guid FROM encounter_scenes WHERE guid=?",
        (encounter_guid,)).fetchone()
    if scene_row:
        log(f"    Launch encounter: {encounter_guid} scene={scene_row[1]} board={scene_row[3]}")
    else:
        log(f"    Launch encounter: {encounter_guid} not in encounter_scenes")

    reck_id = getattr(handler, "client_reck_id", "0") or "0"
    session_name = f"camp_{camp_id}"
    _last_encounter_scene[session_name] = encounter_guid
    # Persist the scene on the campaign row as well.  The LoadBalancer later
    # consumes the in-memory launch hint while resolving battle setup, but the
    # end-game reward evaluator needs the authored scene after that point.
    camp_row = db.execute(
        "SELECT state_json FROM campaigns WHERE id=?", (camp_id,)).fetchone()
    if camp_row:
        try:
            camp_state = json.loads(camp_row[0] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            camp_state = {}
        camp_state["ActiveEncounterGuid"] = encounter_guid
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(camp_state), camp_id))
        db.commit()
    gs_bytes = encode_campaign_session_state(
        0, session_name, encounter_guid,
        session_flags=1 | 4)  # IsEncounter | IsPvE
    env_json = {
        "ReckID": int(reck_id),
        "CampID": camp_id,
        "RequestType": "gamestarted",
        "GameSession": base64.b64encode(gs_bytes).decode("ascii"),
        "DeckID": {"m_UID64": deck_uid64},
    }
    return _push_campaign_notify(handler, env_json, comp, session_id,
                                 target, instance, conh, uid)


_RACE_DECK_MAP = {
    1: "Human", 2: "Elf", 3: "Coyotle", 4: "Orc",
    5: "Dwarf", 6: "ShinHare", 7: "Vennen", 8: "Necrotic",
}


def resolve_battle_config(handler, db, camp_id, session_name):
    """Resolve all campaign-specific battle setup data.

    The game-session protocol still belongs to ``hconnect_server``; campaign
    identity, encounter selection, champion/deck lookup, and tutorial state do
    not.  Returning a plain mapping keeps the shared battle setup independent
    of campaign tables while allowing the campaign service to own this logic.
    """
    scene_override = _last_encounter_scene.pop(session_name, None)
    scene_guid, ai_deck_guid, ai_champ_guid, ai_name, ai_charge_power, \
        ai_personality, ai_deck_personality = resolve_encounter(
            db, camp_id, scene_override)

    deck_db_id = None
    player_champ_name = None
    race_num = cls_num = gnd_num = None
    player_talents_json = "[]"
    player_starting_health = 20
    is_tutorial = False
    if camp_id:
        row = db.execute(
            "SELECT c.champion_name, ch.last_deck_id, ch.race, "
            "ch.champion_class, ch.gender, c.state_json, ch.talents "
            "FROM campaigns c JOIN champions ch ON ch.id=c.champion_id "
            "WHERE c.id=?", (camp_id,)).fetchone()
        if row:
            player_champ_name, deck_db_id = row[0], row[1]
            race_num, cls_num, gnd_num = row[2], row[3], row[4]
            player_talents_json = row[6] or "[]"
            try:
                is_tutorial = not bool(json.loads(row[5] or "{}").get(
                    "TutorialDone", False))
            except Exception:
                is_tutorial = True
    profile = getattr(handler, "user_profile", None) or {}
    if not deck_db_id and profile:
        row = db.execute(
            "SELECT id FROM decks WHERE user_id=? ORDER BY id LIMIT 1",
            (profile.get("id"),)).fetchone()
        deck_db_id = row[0] if row else None

    race_name = _RACE_DECK_MAP.get(race_num)
    cls_name = {1: "Mage", 2: "Warrior", 3: "Cleric", 4: "Rogue",
                5: "Warlock", 6: "Ranger", 7: "Boat"}.get(cls_num)
    gnd_name = {1: "Male", 2: "Female"}.get(gnd_num, "")
    player_champ_guid = None
    if race_name and cls_name:
        row = db.execute(
            "SELECT guid FROM champion_templates WHERE race=? "
            "AND champion_class=? AND gender=? AND is_player=1 LIMIT 1",
            (race_name, cls_name, gnd_name)).fetchone()
        player_champ_guid = row[0] if row else None
    player_champ_guid = player_champ_guid or \
        "1d462ffb-0744-4996-804c-ba61b2c5c2f1"
    if db.execute(
            "SELECT 1 FROM champion_template_data WHERE guid=?",
            (player_champ_guid,)).fetchone():
        player_starting_health = handler._champion_health_by_guid(
            player_champ_guid)
    else:
        player_starting_health = handler._champion_starting_health(
            race_name, cls_name)
    # Passive talents are not included in the champion ability list sent to
    # the client.  Apply their metadata-defined starting-health modifiers at
    # the authoritative battle boundary (e.g. Weight's +5).
    try:
        from abilities.framework.conditions import \
            passive_talent_starting_health_modifier
        talent_guids = json.loads(player_talents_json or "[]")
        player_starting_health += passive_talent_starting_health_modifier(
            db, talent_guids)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return {
        "scene_guid": scene_guid,
        "ai_deck_guid": ai_deck_guid,
        "ai_champ_guid": ai_champ_guid or
            "f8f86969-2e47-4901-8c9e-7fbf8d859e22",
        "ai_name": ai_name or "Trainer",
        "ai_charge_power": ai_charge_power,
        "ai_personality": ai_personality or "Aggressive",
        "ai_deck_personality": ai_deck_personality,
        "deck_db_id": deck_db_id,
        "player_champ_name": player_champ_name or "Player",
        "race_num": race_num,
        "cls_num": cls_num,
        "gnd_num": gnd_num,
        "race_name": race_name,
        "cls_name": cls_name,
        "gnd_name": gnd_name,
        "player_champ_guid": player_champ_guid,
        "player_talents_json": player_talents_json,
        "player_starting_health": player_starting_health,
        "is_tutorial": is_tutorial,
    }


def player_cannot_choose_play_first(db, camp_id):
    """Return whether a campaign champion has a no-Play-first talent.

    The rule is authored in the talent metadata rather than in the champion
    name or a card-specific battle branch.  Weight is currently a passive
    talent without an ability row, so its description is the authoritative
    source for this restriction.
    """
    if not camp_id:
        return False
    row = db.execute(
        "SELECT ch.talents FROM campaigns c "
        "JOIN champions ch ON ch.id=c.champion_id WHERE c.id=?",
        (camp_id,)).fetchone()
    if not row:
        return False
    try:
        talent_guids = json.loads(row[0] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(talent_guids, list) or not talent_guids:
        return False
    placeholders = ",".join("?" for _ in talent_guids)
    descriptions = db.execute(
        "SELECT description FROM talent_data WHERE talent_guid IN ("
        + placeholders + ")", tuple(talent_guids)).fetchall()
    for (description,) in descriptions:
        text = str(description or "").casefold().replace("’", "'")
        if ("can't choose to go first" in text or
                "cannot choose to go first" in text):
            return True
    return False


def resolve_opening_hand_config(db, session, player_id, race_name, cls_name,
                                ability_guids):
    """Resolve campaign opening-hand rules from class and talent metadata.

    Battle setup owns the common hand/deck protocol, while this helper owns
    the campaign-specific class baseline and typed pre-game talent effects.
    ``ability_guids`` may be ResourceId objects or strings; the metadata
    condition evaluator consumes their canonical GUID text.
    """
    class_row = db.execute(
        "SELECT starting_hand_size FROM champion_class_data "
        "WHERE race=? AND champion_class=?",
        (race_name, cls_name)).fetchone()
    base_hand = (int(class_row[0]) if class_row and class_row[0] is not None
                 else 7)
    from abilities.framework.conditions import pregame_modifiers
    guids = []
    for ability in ability_guids or []:
        guid = getattr(ability, "guid", ability)
        guids.append(str(guid))
    mods = pregame_modifiers(db, session, player_id, guids)
    return {
        "starting_hand_size": max(0, base_hand + int(mods["starting_hand"])),
        "maximum_hand_size": max(0, 7 + int(mods["maximum_hand"])),
        "starting_hand_effects": list(mods["starting_hand_effects"]),
    }


def apply_starting_hand_talents(handler, db, session, game, pl_t, effects):
    """Apply campaign opening-hand effects from typed talent metadata."""
    if not effects:
        return False
    changed = False
    profile = getattr(handler, "user_profile", None) or {}
    owner_id = profile.get("id")
    for effect in effects:
        rage = int(effect.get("rage", 0) or 0)
        cost_mod = int(effect.get("card_cost_mod", 0) or 0)
        card_types = effect.get("card_types") or []
        if rage <= 0 and not cost_mod:
            continue
        if cost_mod and card_types:
            placeholders = ",".join("?" for _ in card_types)
            candidates = db.execute(
                "SELECT card_uid, template_guid, permanent_buffs "
                "FROM game_cards WHERE session_id=? AND user_id=? "
                "AND location='hand' AND card_type IN (" + placeholders + ")",
                (session.session_id, owner_id, *card_types)).fetchall()
        else:
            candidates = db.execute(
                "SELECT card_uid, template_guid, permanent_buffs FROM game_cards "
                "WHERE session_id=? AND user_id=? AND location='hand' "
                "AND card_type LIKE '%Troop%'",
                (session.session_id, owner_id)).fetchall()
        if not candidates:
            continue
        card_uid, template_guid, raw_buffs = random.choice(candidates)
        try:
            buffs = json.loads(raw_buffs or "{}")
        except (TypeError, ValueError):
            buffs = {}
        if not isinstance(buffs, dict):
            buffs = {}
        buffs["atk"] = int(buffs.get("atk", 0) or 0)
        buffs["def"] = int(buffs.get("def", 0) or 0)
        if rage > 0:
            buffs["rage"] = int(buffs.get("rage", 0) or 0) + rage
        if cost_mod:
            db.execute(
                "UPDATE game_cards SET card_cost_mod=COALESCE(card_cost_mod, 0) + ? "
                "WHERE session_id=? AND card_uid=?",
                (cost_mod, session.session_id, int(card_uid)))
        if rage > 0:
            db.execute(
                "UPDATE game_cards SET permanent_buffs=? WHERE session_id=? "
                "AND card_uid=?", (json.dumps(buffs), session.session_id,
                                      int(card_uid)))
        db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        _tpl, ct, _name, cost, atk, defense, gem = handler._card_full_data(
            game, scid, template_guid)
        game.push_card_updated(
            scid, pl_t, game_engine.ECardCollections.Hand, ct,
            template_id=template_guid, cost=cost, attack=atk,
            defense=defense, gems=gem)
        getattr(handler, "_log_req", print)(
            f"    Starting hand talent: {card_uid} gains Rage {rage}" if rage > 0
            else f"    Starting hand talent: {card_uid} cost changes by {cost_mod}")
        changed = True
    return changed


def _handle_campaign_start(handler, db, env_json, champ_id, cfg,
                           comp, session_id, target, instance, conh, uid):
    """Handle the campaign 'start' SendEvent and launch its active encounter.

    The client sends the same event for the AZ0 training encounter and for a
    dungeon encounter.  A dungeon must use the encounter attached to its
    active node; falling back to the race training scene here launches the
    player against the trainer again even though the client is showing a
    Crayburn Castle opponent.
    """
    log = getattr(handler, "_log_req", print)
    camp_id = env_json.get("CampID", 0)

    # Player's deck for this champion (the auto-created race starter deck).
    champ = db.execute("SELECT last_deck_id FROM champions WHERE id=?",
                       (champ_id,)).fetchone()
    deck_db_id = champ[0] if champ and champ[0] else None
    deck_uid64 = (deck_db_id << 8) | 17 if deck_db_id else 0

    row = db.execute(
        "SELECT state_json, campaign_type FROM campaigns WHERE id=?",
        (camp_id,)).fetchone()
    campaign_type = (row[1] or "").upper() if row else ""
    encounter_guid = None

    if campaign_type in ("DUNGEON", "AREA"):
        state = json.loads(row[0]) if row and row[0] else {}
        race_name = _race_name_for_campaign(db, camp_id)
        active_node = _resolve_node(
            state, state.get("ALoc") or state.get("LastNode") or "")
        if campaign_type == "DUNGEON" and _normalize_crayburn_encounters(state, race_name):
            db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                       (json.dumps(state), camp_id))
            db.commit()
        for loc in state.get("VisLocs", []):
            data = loc.get("Data", {}) or {}
            if data.get("node") == active_node or data.get("name") == active_node:
                if (campaign_type == "AREA" and data.get("completed")
                        and not data.get("repeatable")):
                    log(f"    Campaign start ignored: completed AREA node "
                        f"(camp={camp_id}, node={active_node})")
                    return None
                encounter_guid = data.get("encounter")
                break

        # Recover older states whose VisLoc did not retain the encounter GUID.
        if not encounter_guid:
            if _crayburn_node_is_encounter(race_name, active_node):
                try:
                    encounter_guid = _crayburn_scene_for_node(
                        race_name, active_node)
                except (KeyError, ValueError, IndexError):
                    encounter_guid = None

        if not encounter_guid:
            log(f"    Campaign start ignored: no active dungeon encounter "
                f"(camp={camp_id}, node={active_node})")
            return None
        log(f"    {campaign_type.title()} start: camp={camp_id} node={active_node} "
            f"encounter={encounter_guid}")
    else:
        # The training encounter scene for this race.
        encounter_guid = cfg.get("training_encounter")

    return _launch_encounter(handler, db, camp_id, champ_id, encounter_guid,
                             deck_uid64, comp, session_id, target, instance,
                             conh, uid)


def resolve_encounter(db, camp_id, scene_guid=None):
    """Resolve a campaign's encounter data.

    If scene_guid is a Crayburn Castle encounter, the AI deck + champion
    are resolved from the per-race ``_CRAYBURN_CASTLE`` seed; otherwise
    ``encounter_scenes`` + ``_az0_config`` is used. Returns (scene_guid,
    ai_deck_guid, ai_champ_guid, ai_name, ai_charge_power, ai_personality,
    ai_deck_personality) or (None,)*7 if unresolved.
    """
    row = db.execute(
        "SELECT champion_id FROM campaigns WHERE id=?", (camp_id,)).fetchone()
    if not row:
        return None, None, None, None, None, None, None
    champ = db.execute(
        "SELECT race FROM champions WHERE id=?", (row[0],)).fetchone()
    if not champ:
        return None, None, None, None, None, None, None
    cfg = _az0_config(champ[0])
    if not cfg:
        return None, None, None, None, None, None, None
    if scene_guid:
        race_name = _RACE_NAMES.get(champ[0], "Necrotic")
        race_data = _CRAYBURN_CASTLE.get("races", {}).get(race_name, {})
        ai_decks = race_data.get("ai_decks", {})
        enc_node_names = ["Castle Gatehouse", "Tower Gatehouse",
                          "Tower of Penworth"]

        def _route_ai_deck_guid(node_name, fallback):
            """Return the EncounterDeck GUID for a Crayburn route node.

            The campaign mapping stores the node's DeckTemplate GUID because
            that is what identifies the opponent deck/champion.  The battle
            card seed is keyed by the separate EncounterDeck GUID carried by
            SceneData, however.  Resolve that indirection from the client
            data so setup receives real cards instead of the placeholder
            fallback deck.
            """
            try:
                route_nodes = {
                    "Castle Gatehouse": "CastleGate",
                    "Tower Gatehouse": "TowerGate",
                    "Tower of Penworth": "PenworthTower",
                }
                route_scene = _crayburn_scene_for_node(
                    race_name, route_nodes.get(node_name, node_name))
                row = db.execute(
                    "SELECT ai_deck_guid FROM encounter_scenes "
                    "WHERE guid=?", (route_scene,)).fetchone()
                if row and row[0]:
                    return row[0]
            except (KeyError, TypeError, ValueError):
                pass
            return fallback

        def _scene_deck_personality(scene_id):
            try:
                row = db.execute(
                    "SELECT ai_deck_personality FROM encounter_scenes "
                    "WHERE guid=?", (scene_id,)).fetchone()
                return row[0] if row else None
            except Exception:
                return None

        # New campaign state uses the race-specific scene that the client
        # already knows. This keeps the pre-battle preview and actual battle
        # on the same opponent/deck.
        race_scene_guids = _crayburn_scene_guids(race_name)
        if scene_guid in race_scene_guids:
            node_name = enc_node_names[race_scene_guids.index(scene_guid)]
            deck_data = ai_decks.get(node_name, {})
            return (scene_guid, _route_ai_deck_guid(
                node_name, deck_data.get("deck_guid")),
                deck_data.get("champion_guid"),
                deck_data.get("champion_name", cfg.get("trainer_npc")),
                cfg.get("ai_charge_power"), cfg.get("ai_personality"),
                deck_data.get("ai_deck_personality") or
                _scene_deck_personality(scene_guid))

        # Legacy shared Crayburn scenes can still arrive from a battle that
        # was started before its saved dungeon state was normalized.
        castle_encs = _CRAYBURN_CASTLE.get("encounters", [])
        if scene_guid in castle_encs:
            # Map encounter GUID index to encounter node name.
            enc_idx = castle_encs.index(scene_guid)
            race_enc_guids = [
                castle_encs[2],   # Castle Gatehouse (index 2)
                castle_encs[4],   # Tower Gatehouse (index 4)
                castle_encs[5],   # Tower of Penworth (index 5)
            ]
            node_name = None
            if enc_idx in (2, 4, 5):
                node_name = enc_node_names[race_enc_guids.index(scene_guid)]
            deck_data = ai_decks.get(node_name, {}) if node_name else {}
            return (scene_guid, _route_ai_deck_guid(
                node_name, deck_data.get("deck_guid")) if node_name else None,
                deck_data.get("champion_guid"),
                deck_data.get("champion_name", cfg.get("trainer_npc")),
                cfg.get("ai_charge_power"), cfg.get("ai_personality"),
                deck_data.get("ai_deck_personality") or
                _scene_deck_personality(scene_guid))
        # Fallback: encounter_scenes lookup.
        scene = db.execute(
            "SELECT ai_deck_guid, name, title, ai_champion_guid, "
            "ai_deck_personality FROM encounter_scenes WHERE guid=?",
            (scene_guid,)).fetchone()
        ai_deck_guid = scene[0] if scene else None
        # The training scene's internal name is AZ0_Orc, but the campaign
        # encounter is presented by the configured trainer NPC, Moqui.  Keep
        # the scene's deck while using the campaign-facing name.
        if scene_guid == cfg.get("training_encounter"):
            ai_name = cfg.get("trainer_npc") or (scene[1] if scene else None)
            ai_champion_guid = cfg.get("ai_champion_guid")
        else:
            # AZ1/AZ2 encounters are not the race tutorial and must not reuse
            # the trainer's portrait/name.  Use the authored scene title for
            # the banner; the battle setup supplies its generic AI champion
            # fallback when no champion is authored on the scene.
            ai_name = (scene[2] or scene[1]) if scene else "AI Opponent"
            ai_champion_guid = scene[3] if scene and len(scene) > 3 else None
            if ai_champion_guid:
                name_row = db.execute(
                    "SELECT name FROM card_templates WHERE guid=?",
                    (ai_champion_guid,)).fetchone()
                if name_row and name_row[0]:
                    ai_name = name_row[0]
        return scene_guid, ai_deck_guid, ai_champion_guid, \
            ai_name, cfg.get("ai_charge_power"), cfg.get("ai_personality"), \
            scene[4] if scene and len(scene) > 4 else None
    scene_guid = cfg.get("training_encounter")
    scene = db.execute(
        "SELECT ai_deck_guid, name, ai_deck_personality "
        "FROM encounter_scenes WHERE guid=?",
        (scene_guid,)).fetchone() if scene_guid else None
    ai_deck_guid = scene[0] if scene else None
    ai_name = cfg.get("trainer_npc") or (scene[1] if scene else None)
    return scene_guid, ai_deck_guid, cfg.get("ai_champion_guid"), ai_name, \
        cfg.get("ai_charge_power"), cfg.get("ai_personality"), \
        scene[2] if scene and len(scene) > 2 else None


def _handle_gameend(handler, db, env_json, comp, session_id,
                     reqid, target, instance, conh, uid):
    """Handle a campaign game-end notification (RequestType "gameend").

    The client sends this after a campaign battle finishes, with the session
    name and winner/loser UID lists. After the training battle is won, reveal
    the quest-giver NPC so the player can take the Crayburn Castle quest.
    """
    camp_id = env_json.get("CampID", 0)
    session = env_json.get("Session", "")
    winners = env_json.get("Winners", [])
    losers = env_json.get("Losers", [])

    log = getattr(handler, "_log_req", print)
    log(f"    Campaign GameEnd: camp={camp_id} session={session} winners={winners} losers={losers}")

    cid, state = _apply_gameend(db, camp_id, bool(winners))
    if state is None:
        resp = _build_input_response(camp_id, None, success=False)
        resp["Errors"] = ["Campaign not found"]
        return _send_response(handler, json.dumps(resp), comp, session_id,
                              reqid, target, instance, conh, uid)

    resp = _build_input_response(camp_id, state, success=True)
    return _send_response(handler, json.dumps(resp), comp, session_id,
                          reqid, target, instance, conh, uid)


def _handle_locaction(handler, db, env_json, comp, session_id,
                       reqid, target, instance, conh, uid):
    """StartLoc / FinishLoc: start or finish a location."""
    camp_id = env_json.get("CampID", 0)
    ract = env_json.get("RAct", 0)  # 0=Start, 1=FinishLoc
    location_name = env_json.get("Loc", "")
    params = env_json.get("Params", [])

    log = getattr(handler, "_log_req", print)
    action = "StartLoc" if ract == 0 else "FinishLoc"
    log(f"    Campaign {action}: camp={camp_id} loc={location_name}")

    row = db.execute(
        "SELECT champion_id, state_json, campaign_type, template_name "
        "FROM campaigns WHERE id=?",
        (camp_id,)
    ).fetchone()
    if not row:
        resp = _build_input_response(camp_id, None, success=False)
        resp["Errors"] = ["Campaign not found"]
        return _send_response(handler, json.dumps(resp), comp, session_id,
                              reqid, target, instance, conh, uid)

    champ_id, state_json, ctype, template_name = row
    if state_json:
        state = json.loads(state_json)
    else:
        state = _build_initial_gameplay_state(camp_id, champ_id, ctype or "AREA")

    if ract == 0:
        if (ctype or "").upper() == "DUNGEON":
            requested_node = _resolve_node(state, location_name)
            # Recover old states where the client clicked a later map marker
            # while the first conversation was still pending.  Keep the chain
            # ordered and activate the next uncompleted node instead of
            # clearing ALoc and leaving the player at the drawbridge.
            expected_node = next(
                (node for node in _CASTLE_CHAIN[1:]
                 if not _is_location_completed(state, node)), None)
            node = expected_node or requested_node
            if node and _is_location_completed(state, node):
                node = requested_node
            state.pop("_pending_travel", None)
            state["ALoc"] = node
            state["LastNode"] = node
            state["CurState"] = "EXPLORE"
            if node:
                _reveal_crayburn_node(state, node)
                _set_crayburn_autostart(state, node)
        else:
            requested = next((loc.get("Data", {}) for loc in state.get("VisLocs", [])
                              if loc.get("Data", {}).get("node") == location_name or
                              loc.get("Data", {}).get("name") == location_name), {})
            node = requested.get("node") or location_name
            # AREA maps use the authored linear scene order. Keep the current
            # node visited/completed and reveal only the next adjacent node;
            # the client uses these flags to render fog of war and Completed.
            previous = state.get("ALoc") or state.get("LastNode")
            previous_node = _resolve_node(state, previous) if previous else None
            requested_node = _resolve_node(state, node) if node else None
            # A completed battle node is no longer startable, even if a stale
            # client sends StartLoc for it after reconnecting.
            blocked_encounter = bool(
                requested.get("type") == "Encounter"
                and requested.get("completed")
                and not requested.get("repeatable"))
            movement_rejected = False
            if not blocked_encounter and previous and previous != node:
                previous_data = next(
                    ((l.get("Data") or {}) for l in state.get("VisLocs", [])
                     if (l.get("Data") or {}).get("node") == previous), {})
                # Do not silently complete an encounter merely because the
                # client attempted to travel away from it. Battles complete
                # only through the game-end flow.
                if (previous_data.get("type") not in ("", "Empty", None) and
                        not previous_data.get("completed")):
                    node = previous
                    requested = previous_data
                    blocked_encounter = True
                elif ((ctype or "").upper() == "AREA" and
                      str(template_name or "").upper() == "AZ1" and
                      previous_node and requested_node and
                      not _az1_is_adjacent(db, previous_node, requested_node)):
                    # The map prefab contains the real path graph; reject
                    # arbitrary jumps even if a stale client sends a hidden
                    # location name directly.
                    log(f"    Campaign movement rejected: {previous_node} -> "
                        f"{requested_node} is not adjacent")
                    node = previous
                    requested = previous_data
                    movement_rejected = True
            if (not blocked_encounter and not movement_rejected and
                    (ctype or "").upper() == "AREA" and
                    str(template_name or "").upper() == "AZ1" and
                    requested_node and requested_node != previous_node):
                pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
                visited_nodes = {
                    _resolve_node(state, str(value))
                    for value in (pdata.get("visited_nodes") or []) if value
                }
                blocked_nodes = {
                    _resolve_node(state, str(value))
                    for value in (pdata.get("blocked_nodes") or []) if value
                }
                requested_visible = bool(requested.get("visible", True))
                if (requested_node in blocked_nodes and
                        requested_node not in visited_nodes) or not requested_visible:
                    log(f"    Campaign movement rejected: {previous_node} -> "
                        f"{requested_node} is hidden or quest-gated")
                    node = previous
                    requested = previous_data
                    movement_rejected = True
            _note_visited(state, node)
            # The client identifies an active AREA Location by its authored
            # Name, not its internal map-node ID.
            state["ALoc"] = (None if (blocked_encounter or movement_rejected)
                              else (requested.get("name") or node))
            state["LastNode"] = node
            for idx, loc in enumerate(state.get("VisLocs", [])):
                data = loc.get("Data", {})
                if data.get("node") == node:
                    data["visible"] = True
                    data["enabled"] = True
                    if (str(template_name or "").upper() != "AZ1" and
                            idx + 1 < len(state["VisLocs"])):
                        nxt = state["VisLocs"][idx + 1].get("Data", {})
                        nxt["visible"] = True
                        nxt["enabled"] = True
                    if data.get("type") == "Convo":
                        data["autostart"] = True
                    break
            if ((ctype or "").upper() == "AREA" and
                    str(template_name or "").upper() == "AZ1"):
                _hydrate_az1_area_scene_metadata(
                    db, state.get("VisLocs", []), champ_id=champ_id, state=state)
                # Select the authored first/repeat/state conversation at the
                # moment the node is entered.  Hydration provides a safe
                # default for old saves; this pass uses the persisted visit
                # count so repeatable nodes (such as Milosh's camp) receive
                # their repeat conversation on subsequent visits.
                for loc in state.get("VisLocs", []):
                    data = loc.get("Data", {}) or {}
                    if data.get("node") == node and data.get("type") == "Convo":
                        data["conversationId"] = _az1_node_conversation(
                            db, node, state)
                        data["repeatable"] = _az1_node_is_repeatable(db, node)
                        data["autostart"] = True
                        break
                _az1_reveal_neighbors(db, state, node)
        state["CurState"] = "EXPLORE"
    else:
        state["CurState"] = "EXPLORE"
        for loc in state.get("VisLocs", []):
            ld = loc.get("Data", {})
            if ld.get("node") == location_name or ld.get("name") == location_name:
                ld["completed"] = True
                _note_visited(state, ld.get("node") or location_name)
                break

    db.execute(
        "UPDATE campaigns SET state_json=? WHERE id=?",
        (json.dumps(state), camp_id)
    )
    db.commit()

    resp = _build_input_response(camp_id, state, success=True)
    return _send_response(handler, json.dumps(resp), comp, session_id,
                          reqid, target, instance, conh, uid)


def _handle_forfeit(handler, db, env_json, comp, session_id,
                     reqid, target, instance, conh, uid):
    """Forfeit: forfeit a campaign."""
    camp_id = env_json.get("CampID", 0)
    row = db.execute(
        "SELECT champion_id, state_json, campaign_type FROM campaigns WHERE id=?",
        (camp_id,)
    ).fetchone()
    if not row:
        resp = _build_input_response(camp_id, None, success=False)
        resp["Errors"] = ["Campaign not found"]
        return _send_response(handler, json.dumps(resp), comp, session_id,
                              reqid, target, instance, conh, uid)

    champ_id, state_json, ctype = row
    if state_json:
        state = json.loads(state_json)
    else:
        state = _build_initial_gameplay_state(camp_id, champ_id, ctype or "AREA")

    state["Finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["FinishReason"] = "Forfeit"
    state["CurState"] = "FINISHED"

    db.execute(
        "UPDATE campaigns SET state_json=? WHERE id=?",
        (json.dumps(state), camp_id)
    )
    db.commit()

    resp = _build_input_response(camp_id, state, success=True)
    return _send_response(handler, json.dumps(resp), comp, session_id,
                          reqid, target, instance, conh, uid)


def _handle_cheat(handler, db, env_json, comp, session_id,
                   reqid, target, instance, conh, uid):
    """Handle cheat commands from the campaign debug UI.

    Supports the client's 'camp.encounter' console command, which sends
    NameValues=["encounter", "<encounter scene guid>"] and expects a
    List<CampCheatRequest.Result> response.  We launch the encounter (push
    gamestarted) and report success.
    """
    champ_id = env_json.get("ChampionID", 0)
    namevals = env_json.get("NameValues", [])
    log = getattr(handler, "_log_req", print)
    log(f"    Campaign Cheat: champ={champ_id} nv={namevals}")

    results = []
    if len(namevals) >= 2 and namevals[0].lower() == "encounter":
        encounter_guid = namevals[1]
        # Player's deck for this champion.
        champ = db.execute("SELECT last_deck_id FROM champions WHERE id=?",
                           (champ_id,)).fetchone()
        deck_db_id = champ[0] if champ and champ[0] else None
        deck_uid64 = (deck_db_id << 8) | 17 if deck_db_id else 0
        # Find the campaign for this champion to get its CampID.
        row = db.execute(
            "SELECT id FROM campaigns WHERE champion_id=? ORDER BY id DESC LIMIT 1",
            (champ_id,)).fetchone()
        camp_id = row[0] if row else 0
        try:
            _launch_encounter(handler, db, camp_id, champ_id, encounter_guid,
                              deck_uid64, comp, session_id, target, instance,
                              conh, uid)
            results.append({"Cheat": "encounter", "Success": True,
                            "Summary": f"Launched encounter {encounter_guid}"})
        except Exception as e:
            results.append({"Cheat": "encounter", "Success": False,
                            "Summary": str(e)})
    elif len(namevals) >= 1 and namevals[0].lower() == "dungeon":
        # 'camp.dungeon <name>' — enter a dungeon as if the quest-giver's
        # conversation had just finished: switch the campaign row to DUNGEON,
        # transition to the scene, then advance the castle chain one step from
        # the Entrance (which shows the first node's conversation).
        try:
            row = db.execute(
                "SELECT id, champion_id, state_json FROM campaigns "
                "WHERE champion_id=? ORDER BY id DESC LIMIT 1",
                (champ_id,)).fetchone()
            if not row:
                champ_row = db.execute(
                    "SELECT id FROM champions WHERE id=?", (champ_id,)).fetchone()
                if not champ_row:
                    raise ValueError("no champion for dungeon cheat")
                camp_id, _inst, _started, _st = _find_campaign_for_champion(
                    db, champ_id, "DUNGEON")
                camp_champ_id = champ_id
            else:
                camp_id, camp_champ_id, _ = row
            # Always rebuild a fresh dungeon state — stale _pending_travel /
            # completed-node data from a previous run skips conversations.
            db.execute("UPDATE campaigns SET state_json=NULL WHERE id=?",
                       (camp_id,))
            db.commit()
            champ = _get_champion(db, camp_champ_id)
            cfg = _az0_config(champ[2]) if champ else _az0_config(1)
            if cfg is None:
                cfg = _az0_config(1)
            state = _build_initial_gameplay_state(camp_id, camp_champ_id,
                                                  "DUNGEON", _race_for_cfg(cfg))
            _transition_to_dungeon(state, cfg)
            db.execute(
                "UPDATE campaigns SET campaign_type='DUNGEON', template_name='Crayburn Castle', state_json=? WHERE id=?",
                (json.dumps(state), camp_id))
            db.commit()
            # Ensure the dungeon's journal quest campaign exists.
            _ensure_quest_campaign(db, camp_champ_id, "DUNGEON")
            # Transition to the dungeon scene...
            push_campupdate(handler, db, camp_id, state.get("ChampID") or camp_champ_id,
                            "dungeon_enter", "DUNGEON", True, state, comp,
                            session_id, target, instance, conh, uid)
            # ...then start the chain at the first conversation.
            advance_crayburn_step(handler, db, camp_id, False, comp, session_id,
                                  target, instance, conh, uid)
            results.append({"Cheat": "dungeon", "Success": True,
                            "Summary": f"Entered Crayburn Castle (camp={camp_id})"})
        except Exception as e:
            results.append({"Cheat": "dungeon", "Success": False,
                            "Summary": str(e)})
    else:
        results.append({"Cheat": "unknown", "Success": False,
                        "Summary": "Unsupported cheat"})

    return _send_response(handler, json.dumps(results), comp, session_id,
                          reqid, target, instance, conh, uid)


### BEGIN CRAYBURN CASTLE SEED
_CRAYBURN_CASTLE = {
 "encounters": [
  "1e65d03d-f3d8-41e9-b3a1-3600e1756378",
  "cae9b735-ca90-400f-81bf-a0a763fa3dc3",
  "f3c0ac5b-ff09-488c-ad63-f11ff15acdcd",
  "df073679-4fd2-4434-8aff-6c044d759f91",
  "2ba61b7b-6864-4582-a634-f9124fb2fdee",
  "5f222319-7b4e-4ba4-b0dc-f9678c000d8b"
 ],
 "races": {
  "Coyotle": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "891cf0c5-3581-4d4e-910f-6cef50840d0e",
     "champion_name": "Snarling Ambusher",
     "champion_race": "Coyotle",
     "deck_guid": "48e8d6a4-7b58-474e-b3d3-540597916e6d"
    },
    "Tower Gatehouse": {
     "champion_guid": "aa99a0b5-b2b8-40ef-b180-0c0fb5ae4ca6",
     "champion_name": "Wind Whisperer",
     "champion_race": "Coyotle",
     "deck_guid": "49e9ab78-7e97-40e4-881e-c5194be4ce61"
    },
    "Tower of Penworth": {
     "champion_guid": "f1f76801-83be-452b-90e0-85731decd72e",
     "champion_name": "Whispering Breeze",
     "champion_race": "Coyotle",
     "deck_guid": "6e6d3a29-64fe-47fc-a7e8-a7a6e1aecf49"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "4084b46c-3d26-4159-8e01-ec86c1d0e7b6",
     "fail": "50bd1063-7597-424c-9885-1e3adc36dea4",
     "success": "8a39b557-c62d-4f44-bfd4-87e0579cccbe"
    },
    "Inner Bailey": {
     "conv": "c3146d14-b7d1-4737-8c14-d1c4cac4be9f"
    },
    "The Drawbridge": {
     "conv": "1de6dac7-b539-4603-95fb-18b16e6a95e9"
    },
    "The Watchtower": {
     "conv": "a74f5fbb-fc87-488d-921e-cd1cc7a856d8"
    },
    "Tower Gatehouse": {
     "conv": "e1a7a3cc-2ad9-44fc-bde1-16db12eeb014",
     "fail": "b22b0125-6f0a-4d4b-8e5d-c23369ddd9c4",
     "success": "ae96f501-9acf-4c72-a72d-c71a3b96142a"
    },
    "Tower of Penworth": {
     "conv": "e6c52582-14b5-439a-949d-f2911ff03ac7",
     "fail": "d842028a-75da-422c-9f0f-41c33c1d71b0",
     "success": "d68f7078-8ea3-41b1-85a1-01af5ee0cfc8"
    }
   },
   "quest_end": "9c139a1c-40a4-4ed7-b6ff-378c6f6bc1ea",
   "quest_start": "0934ceae-03be-436e-b8e5-4f8922e02a0b"
  },
  "Dwarf": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "14241860-ad15-41bd-a63b-27477044d5b0",
     "champion_name": "Scrap Welder",
     "champion_race": "Dwarf",
     "deck_guid": "7062ed10-73f6-449a-8b11-3faee5442355"
    },
    "Tower Gatehouse": {
     "champion_guid": "519737b5-6ec0-4355-9e40-7a0ef0fbb40b",
     "champion_name": "Elite Battle Tech",
     "champion_race": "Dwarf",
     "deck_guid": "7b600dfb-9f2a-4122-8157-ae8bd484ee11"
    },
    "Tower of Penworth": {
     "champion_guid": "259f0138-5a98-424c-ac83-da081d702ec4",
     "champion_name": "Glendower",
     "champion_race": "Dwarf",
     "deck_guid": "6042bf6c-e0a3-401f-8156-144d2b4d9c5d"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "d6afd6b2-2d82-4e08-b099-281037116261",
     "fail": "bc8ee2ca-0453-4274-a56f-1dee95d24944",
     "success": "1a251fae-0933-4560-bfd5-a9b2181031c5"
    },
    "Inner Bailey": {
     "conv": "a9c78e63-478e-48a0-9223-56d5aff54882"
    },
    "The Drawbridge": {
     "conv": "10238e3b-6a07-4fab-a632-64ecaaf13671"
    },
    "The Watchtower": {
     "conv": "fea06901-f852-4221-a740-d801087e7846"
    },
    "Tower Gatehouse": {
     "conv": "155a5947-2cdf-4cda-b396-3e6ffaf9f362",
     "fail": "76031387-ccb3-4fc1-993e-283395b5fe2b",
     "success": "e35a165c-a306-466f-8e02-bb298d3b6197"
    },
    "Tower of Penworth": {
     "conv": "76b62ae6-ebc3-4801-82f2-83290957446a",
     "fail": "ce4d8cba-ac4f-4dcf-8276-48a4bf55984d",
     "success": "e2c269f1-7d82-4e48-a029-ab65d8e122cd"
    }
   },
   "quest_end": "21c62741-49c8-4da5-8b8d-440247911027",
   "quest_start": "88fe73d5-a42a-408d-97c2-ca5651affc1b"
  },
  "Elf": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "9efbd1c3-1724-4e74-8cc3-06f2c1fc5f22",
     "champion_name": "Wild Child",
     "champion_race": "Elf",
     "deck_guid": "10069a59-a090-4850-98a7-8734d532d150"
    },
    "Tower Gatehouse": {
     "champion_guid": "b90abda1-dc37-48b6-8ed2-a9c86b5bfa2d",
     "champion_name": "Ashwood Blademaster",
     "champion_race": "Elf",
     "deck_guid": "35d90990-3a6a-495c-9092-8e07ff595a30"
    },
    "Tower of Penworth": {
     "champion_guid": "ce9e4694-0a43-44ba-9e33-3b035aea86c6",
     "champion_name": "Nerissa",
     "champion_race": "Elf",
     "deck_guid": "59487597-64ab-4755-8884-d936653353d2"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "260f9a04-28f9-443a-8741-67e6264e090d",
     "fail": "36725ee7-4476-4650-ad02-43c4b7f9a6ee",
     "success": "4fe768f9-4f82-445c-a178-db3730035dfd"
    },
    "Inner Bailey": {
     "conv": "43f26b07-6693-439a-847c-893b9c6b884d"
    },
    "The Drawbridge": {
     "conv": "bfc57f7f-7832-480c-b0cd-2713562aff9a"
    },
    "The Watchtower": {
     "conv": "7f3d1858-a4ca-4166-b6c6-a911b9b60a01"
    },
    "Tower Gatehouse": {
     "conv": "28a7724d-264c-41b2-86a4-5cdc1a16afb2",
     "fail": "f86bb43a-de74-4ac0-a730-cdd1479a90e2",
     "success": "9790dab2-2198-4d29-8544-79c8515fa0d3"
    },
    "Tower of Penworth": {
     "conv": "4ab9607d-b45b-4638-ad4b-f2331c38cde3",
     "fail": "5719984b-06c2-41f0-8304-87dd9817aa37",
     "success": "f6ffc170-f15d-4c34-90db-07e321fd3067"
    }
   },
   "quest_end": "bbc4460d-8452-4d49-a6e9-c64216f483b3",
   "quest_start": "18b84529-d187-46ad-bb98-9a591d5287ea"
  },
  "Human": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "9b3ecd51-da74-41a4-adc9-f44b4bfb412b",
     "champion_name": "Chimera Guard Outrider",
     "champion_race": "Human",
     "deck_guid": "582f1eff-360a-4129-9288-ad938999f6cc"
    },
    "Tower Gatehouse": {
     "champion_guid": "50ab780f-e546-468f-ba79-06a49fb68469",
     "champion_name": "Buccaneer",
     "champion_race": "Human",
     "deck_guid": "146ff24f-f208-494a-b77e-894a3448a665"
    },
    "Tower of Penworth": {
     "champion_guid": "a0a5ef26-6676-4602-9fa2-e1608120d210",
     "champion_name": "Gareth Kay",
     "champion_race": "Human",
     "deck_guid": "326dd554-808b-4d1b-aacd-49fdf717a941"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "60a7d90d-c16f-4922-9c43-8e59bbc416e3",
     "fail": "3c635381-acb3-4912-a3ce-501c53dbb691",
     "success": "e83ffcda-1d7f-4ccf-8dd2-2e5cbdc0dc1d"
    },
    "Inner Bailey": {
     "conv": "52cdef5d-fa56-45f9-8699-16b835bc129c"
    },
    "The Drawbridge": {
     "conv": "29ad6621-9b02-4380-b4ff-3dfe6d8a4b81"
    },
    "The Watchtower": {
     "conv": "ba9fb1a9-406b-49b0-aa46-18044a9ac296"
    },
    "Tower Gatehouse": {
     "conv": "1f9acf0f-970c-4e68-a6ac-558550482550",
     "fail": "cb4b5e85-dfda-45e8-9b96-92973d6eadf5",
     "success": "cc4080df-4ec8-46d9-aaf8-0d67537bfe86"
    },
    "Tower of Penworth": {
     "conv": "94076e42-9435-490d-82a4-a32a592cf3fa",
     "fail": "7c4e314c-4d6c-432c-a097-8d702a44cdc2",
     "success": "10b3d849-3cda-41a8-bbaf-0d43819a0b5a"
    }
   },
   "quest_end": "0427b61f-251c-47b5-b89d-a0f2e2f42b1a",
   "quest_start": "abdcca36-20fa-4af7-8c24-8b5e0fcb61d3"
  },
  "Necrotic": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "05499eb5-f6cc-4d82-96ad-54a9071c055e",
     "champion_name": "Duskwing Outrider",
     "champion_race": "Necrotic",
     "deck_guid": "398df9c8-69fa-43b0-aa8c-44fed10f2e09"
    },
    "Tower Gatehouse": {
     "champion_guid": "c0c7e1b1-1163-4cc9-96d2-4b15570197c8",
     "champion_name": "Warlock of Aettir",
     "champion_race": "Necrotic",
     "deck_guid": "71a650cc-ef4a-4d99-8e23-a7e0d4da883d"
    },
    "Tower of Penworth": {
     "champion_guid": "3658be25-2160-4a23-a609-e40d531832d0",
     "champion_name": "Iddi",
     "champion_race": "Necrotic",
     "deck_guid": "b4b67816-2cc4-4f9e-9788-be0326e139d9"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "83ffe2e5-bd78-47c1-8b85-849ef5e184bf",
     "fail": "df455c81-06e7-4634-9165-9f2b039d14b2",
     "success": "e7d6489d-dff8-446c-8a10-87136d598b15"
    },
    "Inner Bailey": {
     "conv": "2ba5b572-a454-4af2-b1d7-0d96f08d6729"
    },
    "The Drawbridge": {
     "conv": "49da653a-012f-42c4-843d-5eaf14b6731e"
    },
    "The Watchtower": {
     "conv": "8bdde70c-06bf-460d-bd37-ce9029cc01c0"
    },
    "Tower Gatehouse": {
     "conv": "40a89f9a-a41c-465c-a3ef-6c9ab5a07c8d",
     "fail": "a43f9a3a-19cf-4c95-8124-1122fbcae655",
     "success": "1fd4765f-78c5-4514-a7bf-074ca9b75a85"
    },
    "Tower of Penworth": {
     "conv": "e0674a7b-81a0-480a-9c55-e18409378690",
     "fail": "23f92d62-c827-44e8-aa9f-f1c4d98fab47",
     "success": "34d48257-b16e-443c-940c-d322b47d00b9"
    }
   },
   "quest_end": "922aa66e-e41c-41ad-94a2-adb84f356430",
   "quest_start": "4cf66046-086b-40f8-8237-4e4d13e64a42"
  },
  "Orc": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "5e3982aa-9f7e-45f5-8d8f-ef611365053f",
     "champion_name": "Ridge Raider",
     "champion_race": "Orc",
     "deck_guid": "4b78d914-c7a8-4b10-b787-5844ebb5d7c0"
    },
    "Tower Gatehouse": {
     "champion_guid": "ef67a187-286d-4e4d-94c9-e92159617f3a",
     "champion_name": "Bloodsoaked Brawler",
     "champion_race": "Orc",
     "deck_guid": "65407eee-6308-4958-a80f-6d9dc7bff23a"
    },
    "Tower of Penworth": {
     "champion_guid": "0520c385-7e2d-4dc5-9fd0-61c85df69250",
     "champion_name": "Moqui",
     "champion_race": "Orc",
     "deck_guid": "93acc9f3-2fe9-459f-a653-f0ffd2a48db2"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "51bba89c-d583-4d34-a47f-a58bc84ccab8",
     "fail": "9087e9ec-e219-4cec-b181-439d95374f0e",
     "success": "8ae2c43e-e695-44df-adf8-f61d34d2c74b"
    },
    "Inner Bailey": {
     "conv": "fe7818f3-df41-466f-8a1a-162ef7267493"
    },
    "The Drawbridge": {
     "conv": "fd9b329c-2341-43b3-932e-6bea48ed4faf"
    },
    "The Watchtower": {
     "conv": "3c0ee3b4-0453-43c4-bce9-d7882c9ea4e9"
    },
    "Tower Gatehouse": {
     "conv": "7a31ca71-cc29-45c8-a698-fc5f9c65529b",
     "fail": "c16ba7a4-9d23-4de9-ae84-b6074d3fc36e",
     "success": "aa2da90c-9325-44fc-ad8b-abae6f6dba6d"
    },
    "Tower of Penworth": {
     "conv": "a6a20599-fc46-48f4-8a29-134c34548936",
     "fail": "1317c9bb-e1bc-4294-9806-52bf4f8fe953",
     "success": "7b4fb960-9150-4175-8fee-444a29266c03"
    }
   },
   "quest_end": "463a5ea3-847f-4102-83e4-512eb0ca97ab",
   "quest_start": "578dd9ea-c5ab-49a0-93c5-44ad235cf0a9"
  },
  "Shin'hare": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "ddada3b2-7a69-4040-b1f5-fd5ca2ca4bd6",
     "champion_name": "Rune Ear Elite",
     "champion_race": "ShinHare",
     "deck_guid": "c2c0c313-ae14-407a-a7db-b889ee292db0"
    },
    "Tower Gatehouse": {
     "champion_guid": "9d0cdd24-3672-414e-8c49-07da6f97e784",
     "champion_name": "Blood Cauldron Ritualist",
     "champion_race": "ShinHare",
     "deck_guid": "52adf921-5244-41eb-987a-7d78dd7b2943"
    },
    "Tower of Penworth": {
     "champion_guid": "1d756624-3c6a-445e-8fd1-acba6c9e4ce1",
     "champion_name": "Sora",
     "champion_race": "ShinHare",
     "deck_guid": "8400016d-9eb9-4565-b05f-28b5e41d51e3"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "78c4a18a-c212-4005-9619-aa08aa453a85",
     "fail": "3afa146d-1c07-4abb-a0b2-f17889bda00f",
     "success": "97acd02e-1821-4730-a8e2-2b8dd5e5091d"
    },
    "Inner Bailey": {
     "conv": "5c735a25-ab19-46db-a281-3d895680f940"
    },
    "The Drawbridge": {
     "conv": "c437e9e4-946f-4738-92a2-cb0ad621b349"
    },
    "The Watchtower": {
     "conv": "f2e95564-a78e-4ae2-8ba9-be8b72b260f6"
    },
    "Tower Gatehouse": {
     "conv": "263472e5-e4fa-43e2-b58b-0a30424320c4",
     "fail": "8a3aac87-6501-41b8-9054-7abb69e07e05",
     "success": "84cf84b9-ac29-4296-b9b9-96baaa48fc97"
    },
    "Tower of Penworth": {
     "conv": "37191606-9653-48f8-8cb7-b72e54badc7b",
     "fail": "97c516e9-8622-4b8d-849f-afc9164977ec",
     "success": "b15bb1da-cffa-4f8e-9287-15bcee3706aa"
    }
   },
   "quest_end": "5f119ac9-1018-4161-9b87-2cc23dba9c71",
   "quest_start": "d89040c9-75c1-4f10-a647-f35679bcff72"
  },
  "Vennen": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "9a62d370-5029-4356-ad3f-7873792984c6",
     "champion_name": "Vilefang Eremite",
     "champion_race": "Vennen",
     "deck_guid": "6d5144a4-3cf5-4c1c-8c50-2cd17a87ebc1"
    },
    "Tower Gatehouse": {
     "champion_guid": "b6fe99d4-be07-4741-b072-eae38b6b7247",
     "champion_name": "Nazhk Webguard",
     "champion_race": "Vennen",
     "deck_guid": "1aacb82b-fea5-4653-b190-41eae593233c"
    },
    "Tower of Penworth": {
     "champion_guid": "22e3a199-4028-43b5-9290-7c646535360c",
     "champion_name": "Zilth",
     "champion_race": "Vennen",
     "deck_guid": "306b9a87-29cc-4393-a547-5d090b3e1855"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "4c4e84a4-dbdd-481d-a8f6-efd3362c1416",
     "fail": "02d251fd-0f9a-435d-8586-2197aeb97ea2",
     "success": "c16d9605-b12f-41ff-baf2-4d48f439f3f2"
    },
    "Inner Bailey": {
     "conv": "6c08b7be-cd2e-40bc-9942-6d7328daad0f"
    },
    "The Drawbridge": {
     "conv": "509c4971-4d29-450d-acd5-88050919df49"
    },
    "The Watchtower": {
     "conv": "25bc2cf2-3ab5-418e-8301-ef34a74bd60e"
    },
    "Tower Gatehouse": {
     "conv": "26c93c43-b10d-4387-b394-a373fc46bdc6",
     "fail": "8e537c2d-edab-4b21-9ab8-09ef54f1ecb8",
     "success": "5121dde6-9b5f-4895-b0bd-24675a2e3afa"
    },
    "Tower of Penworth": {
     "conv": "9f7aa210-a89f-4b49-942e-44e0a9ea6eb1",
     "fail": "087bb91d-add5-4ed1-a17e-31c6d10c843d",
     "success": "9c20be5e-3e19-4a33-9fd5-30c3e74a9eae"
    }
   },
   "quest_end": "49eaafb9-6645-4c04-9966-5190c4e8ca3d",
   "quest_start": "782369c4-61c2-4a3e-97ce-660f2d062be7"
  }
 }
}



### BEGIN CRAYBURN CASTLE SEED
_CRAYBURN_CASTLE = {
 "encounters": [
  "1e65d03d-f3d8-41e9-b3a1-3600e1756378",
  "cae9b735-ca90-400f-81bf-a0a763fa3dc3",
  "f3c0ac5b-ff09-488c-ad63-f11ff15acdcd",
  "df073679-4fd2-4434-8aff-6c044d759f91",
  "2ba61b7b-6864-4582-a634-f9124fb2fdee",
  "5f222319-7b4e-4ba4-b0dc-f9678c000d8b"
 ],
 "races": {
  "Coyotle": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "ddada3b2-7a69-4040-b1f5-fd5ca2ca4bd6",
     "champion_name": "Rune Ear Elite",
     "champion_race": "ShinHare",
     "deck_guid": "c2c0c313-ae14-407a-a7db-b889ee292db0"
    },
    "Tower Gatehouse": {
     "champion_guid": "9d0cdd24-3672-414e-8c49-07da6f97e784",
     "champion_name": "Blood Cauldron Ritualist",
     "champion_race": "ShinHare",
     "deck_guid": "52adf921-5244-41eb-987a-7d78dd7b2943"
    },
    "Tower of Penworth": {
     "champion_guid": "1d756624-3c6a-445e-8fd1-acba6c9e4ce1",
     "champion_name": "Sora",
     "champion_race": "ShinHare",
     "deck_guid": "8400016d-9eb9-4565-b05f-28b5e41d51e3"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "4084b46c-3d26-4159-8e01-ec86c1d0e7b6",
     "fail": "50bd1063-7597-424c-9885-1e3adc36dea4",
     "success": "8a39b557-c62d-4f44-bfd4-87e0579cccbe"
    },
    "Inner Bailey": {
     "conv": "c3146d14-b7d1-4737-8c14-d1c4cac4be9f"
    },
    "The Drawbridge": {
     "conv": "1de6dac7-b539-4603-95fb-18b16e6a95e9"
    },
    "The Watchtower": {
     "conv": "a74f5fbb-fc87-488d-921e-cd1cc7a856d8"
    },
    "Tower Gatehouse": {
     "conv": "e1a7a3cc-2ad9-44fc-bde1-16db12eeb014",
     "fail": "b22b0125-6f0a-4d4b-8e5d-c23369ddd9c4",
     "success": "ae96f501-9acf-4c72-a72d-c71a3b96142a"
    },
    "Tower of Penworth": {
     "conv": "e6c52582-14b5-439a-949d-f2911ff03ac7",
     "fail": "d842028a-75da-422c-9f0f-41c33c1d71b0",
     "success": "d68f7078-8ea3-41b1-85a1-01af5ee0cfc8"
    }
   },
   "quest_end": "9c139a1c-40a4-4ed7-b6ff-378c6f6bc1ea",
   "quest_start": "0934ceae-03be-436e-b8e5-4f8922e02a0b"
  },
  "Dwarf": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "9efbd1c3-1724-4e74-8cc3-06f2c1fc5f22",
     "champion_name": "Wild Child",
     "champion_race": "Elf",
     "deck_guid": "10069a59-a090-4850-98a7-8734d532d150"
    },
    "Tower Gatehouse": {
     "champion_guid": "b90abda1-dc37-48b6-8ed2-a9c86b5bfa2d",
     "champion_name": "Ashwood Blademaster",
     "champion_race": "Elf",
     "deck_guid": "35d90990-3a6a-495c-9092-8e07ff595a30"
    },
    "Tower of Penworth": {
     "champion_guid": "ce9e4694-0a43-44ba-9e33-3b035aea86c6",
     "champion_name": "Nerissa",
     "champion_race": "Elf",
     "deck_guid": "59487597-64ab-4755-8884-d936653353d2"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "d6afd6b2-2d82-4e08-b099-281037116261",
     "fail": "bc8ee2ca-0453-4274-a56f-1dee95d24944",
     "success": "1a251fae-0933-4560-bfd5-a9b2181031c5"
    },
    "Inner Bailey": {
     "conv": "a9c78e63-478e-48a0-9223-56d5aff54882"
    },
    "The Drawbridge": {
     "conv": "10238e3b-6a07-4fab-a632-64ecaaf13671"
    },
    "The Watchtower": {
     "conv": "fea06901-f852-4221-a740-d801087e7846"
    },
    "Tower Gatehouse": {
     "conv": "155a5947-2cdf-4cda-b396-3e6ffaf9f362",
     "fail": "76031387-ccb3-4fc1-993e-283395b5fe2b",
     "success": "e35a165c-a306-466f-8e02-bb298d3b6197"
    },
    "Tower of Penworth": {
     "conv": "76b62ae6-ebc3-4801-82f2-83290957446a",
     "fail": "ce4d8cba-ac4f-4dcf-8276-48a4bf55984d",
     "success": "e2c269f1-7d82-4e48-a029-ab65d8e122cd"
    }
   },
   "quest_end": "21c62741-49c8-4da5-8b8d-440247911027",
   "quest_start": "88fe73d5-a42a-408d-97c2-ca5651affc1b"
  },
  "Elf": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "14241860-ad15-41bd-a63b-27477044d5b0",
     "champion_name": "Scrap Welder",
     "champion_race": "Dwarf",
     "deck_guid": "7062ed10-73f6-449a-8b11-3faee5442355"
    },
    "Tower Gatehouse": {
     "champion_guid": "519737b5-6ec0-4355-9e40-7a0ef0fbb40b",
     "champion_name": "Elite Battle Tech",
     "champion_race": "Dwarf",
     "deck_guid": "7b600dfb-9f2a-4122-8157-ae8bd484ee11"
    },
    "Tower of Penworth": {
     "champion_guid": "259f0138-5a98-424c-ac83-da081d702ec4",
     "champion_name": "Glendower",
     "champion_race": "Dwarf",
     "deck_guid": "6042bf6c-e0a3-401f-8156-144d2b4d9c5d"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "260f9a04-28f9-443a-8741-67e6264e090d",
     "fail": "36725ee7-4476-4650-ad02-43c4b7f9a6ee",
     "success": "4fe768f9-4f82-445c-a178-db3730035dfd"
    },
    "Inner Bailey": {
     "conv": "43f26b07-6693-439a-847c-893b9c6b884d"
    },
    "The Drawbridge": {
     "conv": "bfc57f7f-7832-480c-b0cd-2713562aff9a"
    },
    "The Watchtower": {
     "conv": "7f3d1858-a4ca-4166-b6c6-a911b9b60a01"
    },
    "Tower Gatehouse": {
     "conv": "28a7724d-264c-41b2-86a4-5cdc1a16afb2",
     "fail": "f86bb43a-de74-4ac0-a730-cdd1479a90e2",
     "success": "9790dab2-2198-4d29-8544-79c8515fa0d3"
    },
    "Tower of Penworth": {
     "conv": "4ab9607d-b45b-4638-ad4b-f2331c38cde3",
     "fail": "5719984b-06c2-41f0-8304-87dd9817aa37",
     "success": "f6ffc170-f15d-4c34-90db-07e321fd3067"
    }
   },
   "quest_end": "bbc4460d-8452-4d49-a6e9-c64216f483b3",
   "quest_start": "18b84529-d187-46ad-bb98-9a591d5287ea"
  },
  "Human": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "05499eb5-f6cc-4d82-96ad-54a9071c055e",
     "champion_name": "Duskwing Outrider",
     "champion_race": "Necrotic",
     "deck_guid": "398df9c8-69fa-43b0-aa8c-44fed10f2e09"
    },
    "Tower Gatehouse": {
     "champion_guid": "c0c7e1b1-1163-4cc9-96d2-4b15570197c8",
     "champion_name": "Warlock of Aettir",
     "champion_race": "Necrotic",
     "deck_guid": "71a650cc-ef4a-4d99-8e23-a7e0d4da883d"
    },
    "Tower of Penworth": {
     "champion_guid": "3658be25-2160-4a23-a609-e40d531832d0",
     "champion_name": "Iddi",
     "champion_race": "Necrotic",
     "deck_guid": "b4b67816-2cc4-4f9e-9788-be0326e139d9"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "60a7d90d-c16f-4922-9c43-8e59bbc416e3",
     "fail": "3c635381-acb3-4912-a3ce-501c53dbb691",
     "success": "e83ffcda-1d7f-4ccf-8dd2-2e5cbdc0dc1d"
    },
    "Inner Bailey": {
     "conv": "52cdef5d-fa56-45f9-8699-16b835bc129c"
    },
    "The Drawbridge": {
     "conv": "29ad6621-9b02-4380-b4ff-3dfe6d8a4b81"
    },
    "The Watchtower": {
     "conv": "ba9fb1a9-406b-49b0-aa46-18044a9ac296"
    },
    "Tower Gatehouse": {
     "conv": "1f9acf0f-970c-4e68-a6ac-558550482550",
     "fail": "cb4b5e85-dfda-45e8-9b96-92973d6eadf5",
     "success": "cc4080df-4ec8-46d9-aaf8-0d67537bfe86"
    },
    "Tower of Penworth": {
     "conv": "94076e42-9435-490d-82a4-a32a592cf3fa",
     "fail": "7c4e314c-4d6c-432c-a097-8d702a44cdc2",
     "success": "10b3d849-3cda-41a8-bbaf-0d43819a0b5a"
    }
   },
   "quest_end": "0427b61f-251c-47b5-b89d-a0f2e2f42b1a",
   "quest_start": "abdcca36-20fa-4af7-8c24-8b5e0fcb61d3"
  },
  "Necrotic": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "9b3ecd51-da74-41a4-adc9-f44b4bfb412b",
     "champion_name": "Chimera Guard Outrider",
     "champion_race": "Human",
     "deck_guid": "582f1eff-360a-4129-9288-ad938999f6cc"
    },
    "Tower Gatehouse": {
     "champion_guid": "50ab780f-e546-468f-ba79-06a49fb68469",
     "champion_name": "Buccaneer",
     "champion_race": "Human",
     "deck_guid": "146ff24f-f208-494a-b77e-894a3448a665"
    },
    "Tower of Penworth": {
     "champion_guid": "a0a5ef26-6676-4602-9fa2-e1608120d210",
     "champion_name": "Gareth Kay",
     "champion_race": "Human",
     "deck_guid": "326dd554-808b-4d1b-aacd-49fdf717a941"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "83ffe2e5-bd78-47c1-8b85-849ef5e184bf",
     "fail": "df455c81-06e7-4634-9165-9f2b039d14b2",
     "success": "e7d6489d-dff8-446c-8a10-87136d598b15"
    },
    "Inner Bailey": {
     "conv": "2ba5b572-a454-4af2-b1d7-0d96f08d6729"
    },
    "The Drawbridge": {
     "conv": "49da653a-012f-42c4-843d-5eaf14b6731e"
    },
    "The Watchtower": {
     "conv": "8bdde70c-06bf-460d-bd37-ce9029cc01c0"
    },
    "Tower Gatehouse": {
     "conv": "40a89f9a-a41c-465c-a3ef-6c9ab5a07c8d",
     "fail": "a43f9a3a-19cf-4c95-8124-1122fbcae655",
     "success": "1fd4765f-78c5-4514-a7bf-074ca9b75a85"
    },
    "Tower of Penworth": {
     "conv": "e0674a7b-81a0-480a-9c55-e18409378690",
     "fail": "23f92d62-c827-44e8-aa9f-f1c4d98fab47",
     "success": "34d48257-b16e-443c-940c-d322b47d00b9"
    }
   },
   "quest_end": "922aa66e-e41c-41ad-94a2-adb84f356430",
   "quest_start": "4cf66046-086b-40f8-8237-4e4d13e64a42"
  },
  "Orc": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "9a62d370-5029-4356-ad3f-7873792984c6",
     "champion_name": "Vilefang Eremite",
     "champion_race": "Vennen",
     "deck_guid": "6d5144a4-3cf5-4c1c-8c50-2cd17a87ebc1"
    },
    "Tower Gatehouse": {
     "champion_guid": "b6fe99d4-be07-4741-b072-eae38b6b7247",
     "champion_name": "Nazhk Webguard",
     "champion_race": "Vennen",
     "deck_guid": "1aacb82b-fea5-4653-b190-41eae593233c"
    },
    "Tower of Penworth": {
     "champion_guid": "22e3a199-4028-43b5-9290-7c646535360c",
     "champion_name": "Zilth",
     "champion_race": "Vennen",
     "deck_guid": "306b9a87-29cc-4393-a547-5d090b3e1855"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "51bba89c-d583-4d34-a47f-a58bc84ccab8",
     "fail": "9087e9ec-e219-4cec-b181-439d95374f0e",
     "success": "8ae2c43e-e695-44df-adf8-f61d34d2c74b"
    },
    "Inner Bailey": {
     "conv": "fe7818f3-df41-466f-8a1a-162ef7267493"
    },
    "The Drawbridge": {
     "conv": "fd9b329c-2341-43b3-932e-6bea48ed4faf"
    },
    "The Watchtower": {
     "conv": "3c0ee3b4-0453-43c4-bce9-d7882c9ea4e9"
    },
    "Tower Gatehouse": {
     "conv": "7a31ca71-cc29-45c8-a698-fc5f9c65529b",
     "fail": "c16ba7a4-9d23-4de9-ae84-b6074d3fc36e",
     "success": "aa2da90c-9325-44fc-ad8b-abae6f6dba6d"
    },
    "Tower of Penworth": {
     "conv": "a6a20599-fc46-48f4-8a29-134c34548936",
     "fail": "1317c9bb-e1bc-4294-9806-52bf4f8fe953",
     "success": "7b4fb960-9150-4175-8fee-444a29266c03"
    }
   },
   "quest_end": "463a5ea3-847f-4102-83e4-512eb0ca97ab",
   "quest_start": "578dd9ea-c5ab-49a0-93c5-44ad235cf0a9"
  },
  "Shin'hare": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "891cf0c5-3581-4d4e-910f-6cef50840d0e",
     "champion_name": "Snarling Ambusher",
     "champion_race": "Coyotle",
     "deck_guid": "48e8d6a4-7b58-474e-b3d3-540597916e6d"
    },
    "Tower Gatehouse": {
     "champion_guid": "aa99a0b5-b2b8-40ef-b180-0c0fb5ae4ca6",
     "champion_name": "Wind Whisperer",
     "champion_race": "Coyotle",
     "deck_guid": "49e9ab78-7e97-40e4-881e-c5194be4ce61"
    },
    "Tower of Penworth": {
     "champion_guid": "f1f76801-83be-452b-90e0-85731decd72e",
     "champion_name": "Whispering Breeze",
     "champion_race": "Coyotle",
     "deck_guid": "6e6d3a29-64fe-47fc-a7e8-a7a6e1aecf49"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "78c4a18a-c212-4005-9619-aa08aa453a85",
     "fail": "3afa146d-1c07-4abb-a0b2-f17889bda00f",
     "success": "97acd02e-1821-4730-a8e2-2b8dd5e5091d"
    },
    "Inner Bailey": {
     "conv": "5c735a25-ab19-46db-a281-3d895680f940"
    },
    "The Drawbridge": {
     "conv": "c437e9e4-946f-4738-92a2-cb0ad621b349"
    },
    "The Watchtower": {
     "conv": "f2e95564-a78e-4ae2-8ba9-be8b72b260f6"
    },
    "Tower Gatehouse": {
     "conv": "263472e5-e4fa-43e2-b58b-0a30424320c4",
     "fail": "8a3aac87-6501-41b8-9054-7abb69e07e05",
     "success": "84cf84b9-ac29-4296-b9b9-96baaa48fc97"
    },
    "Tower of Penworth": {
     "conv": "37191606-9653-48f8-8cb7-b72e54badc7b",
     "fail": "97c516e9-8622-4b8d-849f-afc9164977ec",
     "success": "b15bb1da-cffa-4f8e-9287-15bcee3706aa"
    }
   },
   "quest_end": "5f119ac9-1018-4161-9b87-2cc23dba9c71",
   "quest_start": "d89040c9-75c1-4f10-a647-f35679bcff72"
  },
  "Vennen": {
   "ai_decks": {
    "Castle Gatehouse": {
     "champion_guid": "5e3982aa-9f7e-45f5-8d8f-ef611365053f",
     "champion_name": "Ridge Raider",
     "champion_race": "Orc",
     "deck_guid": "4b78d914-c7a8-4b10-b787-5844ebb5d7c0"
    },
    "Tower Gatehouse": {
     "champion_guid": "ef67a187-286d-4e4d-94c9-e92159617f3a",
     "champion_name": "Bloodsoaked Brawler",
     "champion_race": "Orc",
     "deck_guid": "65407eee-6308-4958-a80f-6d9dc7bff23a"
    },
    "Tower of Penworth": {
     "champion_guid": "0520c385-7e2d-4dc5-9fd0-61c85df69250",
     "champion_name": "Moqui",
     "champion_race": "Orc",
     "deck_guid": "93acc9f3-2fe9-459f-a653-f0ffd2a48db2"
    }
   },
   "nodes": {
    "Castle Gatehouse": {
     "conv": "4c4e84a4-dbdd-481d-a8f6-efd3362c1416",
     "fail": "02d251fd-0f9a-435d-8586-2197aeb97ea2",
     "success": "c16d9605-b12f-41ff-baf2-4d48f439f3f2"
    },
    "Inner Bailey": {
     "conv": "6c08b7be-cd2e-40bc-9942-6d7328daad0f"
    },
    "The Drawbridge": {
     "conv": "509c4971-4d29-450d-acd5-88050919df49"
    },
    "The Watchtower": {
     "conv": "25bc2cf2-3ab5-418e-8301-ef34a74bd60e"
    },
    "Tower Gatehouse": {
     "conv": "26c93c43-b10d-4387-b394-a373fc46bdc6",
     "fail": "8e537c2d-edab-4b21-9ab8-09ef54f1ecb8",
     "success": "5121dde6-9b5f-4895-b0bd-24675a2e3afa"
    },
    "Tower of Penworth": {
     "conv": "9f7aa210-a89f-4b49-942e-44e0a9ea6eb1",
     "fail": "087bb91d-add5-4ed1-a17e-31c6d10c843d",
     "success": "9c20be5e-3e19-4a33-9fd5-30c3e74a9eae"
    }
   },
   "quest_end": "49eaafb9-6645-4c04-9966-5190c4e8ca3d",
   "quest_start": "782369c4-61c2-4a3e-97ce-660f2d062be7"
  }
 }
}
### END CRAYBURN CASTLE SEED
