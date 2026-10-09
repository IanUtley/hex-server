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

from domain.enums import EGemTypesNew
import json
import time
import threading
import uuid
import re
import random

from campaign_source import ensure_campaign_path

CAMPAIGN_ROOT = ensure_campaign_path()

import game_engine

try:
    import yaml
except ImportError:  # pragma: no cover - deployment fallback
    yaml = None

from encoder import encode_objfmt_response, encode_datawrapper, compress_gzip
import campaign_rules
import pve_db
import pvp_db
from profile_db import (db_add_stardust, db_add_collection, db_add_inventory,
                        db_inventory_item, db_next_inventory_client_uid,
                        db_upsert_inventory_item, db_reward_card_template,
                        db_grant_card_instance,
                        db_adjust_user_currency, db_champion_reward_profile,
                        db_update_champion_xp, db_create_treasure_chest,
                        db_spend_user_gold)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



def _new_camp_id(db):
    """Generate a unique campaign UID for our DB."""
    cid = pve_db.db_next_campaign_id(db)
    # Return as JSON-friendly ulong (just an int, since Python ints are arbitrary-precision
    # and Newtonsoft.Json on the client can parse them from JSON numbers)
    return cid


def _generate_inst_id():
    """Generate a UID instance ID (pseudo-random ulong)."""
    return abs(hash(uuid.uuid4())) % (2**63)


def _save_campaign_state(db, campaign_id, state):
    """Persist campaign state through the PVE domain boundary."""
    pve_db.db_campaign_update_state(campaign_id, json.dumps(state), db)


def _get_champion(db, champion_id):
    """Get champion info from the DB."""
    return pve_db.db_campaign_champion(champion_id, db)


def _find_campaign_for_champion(db, champion_id, campaign_type="PANORAMA"):
    """Find (or create) a campaign for a champion."""
    row = pve_db.db_campaign_for_champion(champion_id, campaign_type, db)
    if not row:
        cid = _new_camp_id(db)
        inst_id = _generate_inst_id()
        champ = _get_champion(db, champion_id)
        race = champ[2] if champ else None
        champ_name = champ[3] if champ else ""
        state = _build_initial_gameplay_state(cid, champion_id, campaign_type, race)
        template_name = "Crayburn Castle" if campaign_type == "DUNGEON" else "AZ1"
        pve_db.db_create_campaign(
            cid, inst_id, champion_id, pve_db.db_campaign_user_id(champion_id, db),
            champ_name, template_name, campaign_type, json.dumps(state), db)
        db.commit()
        # Keep the champion's LastCampaignID in sync so the client can jump
        # straight into this campaign after selecting the champion on the globe.
        pve_db.db_campaign_set_last_campaign(champion_id, cid, db)
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
    if (campaign_type == "PANORAMA" and existing_state and
            not existing_state.get("RulesZone")):
        public_data = (existing_state.get("PublicState", {}) or {}).get("Data", {}) or {}
        stale_panorama = (
            existing_state.get("TempType") != "PANORAMA"
            or (public_data.get("CampaignGroup") != "PANORAMA" and
                not existing_state.get("PanoramaSceneGuid"))
            or public_data.get("IsStarterDungeon")
        )
        if stale_panorama:
            champ = _get_champion(db, champion_id)
            race = champ[2] if champ else None
            existing_state = _build_initial_gameplay_state(
                row[0], champion_id, "PANORAMA", race)
            pve_db.db_campaign_update_state(
                row[0], json.dumps(existing_state), db, started=False,
                template_name="AZ1")
            db.commit()
        champ = _get_champion(db, champion_id)
        cfg = _az0_config(champ[2]) if champ else None
        if cfg and _normalize_starter_panorama_state(existing_state, cfg):
            pve_db.db_campaign_update_state(row[0], json.dumps(existing_state), db)
            db.commit()
    pve_db.db_campaign_set_last_campaign(champion_id, row[0], db)
    db.commit()
    if campaign_type == "DUNGEON":
        _ensure_quest_campaign(db, champion_id, "DUNGEON")
    return (row[0], row[1], row[2], existing_state)


def restart_champion_campaigns(db, champion_id):
    """Wipe a champion's campaigns and recreate the AZ0 starter panorama.

    The server's normal transitions then carry the champion through the
    starter panorama and Castle Crayburn into AZ1.  Returns ``(deleted,
    new_campaign_id)``.
    """
    deleted = pve_db.db_campaign_delete_for_champion(champion_id, conn=db)
    pve_db.db_campaign_progress_delete(champion_id, conn=db)
    db.commit()
    cid = _find_campaign_for_champion(db, champion_id, "PANORAMA")[0]
    return deleted, cid


def _get_existing_campaign_for_champion(db, champion_id, campaign_type):
    """Return the newest stored campaign of *campaign_type*, without creating one."""
    row = pve_db.db_latest_campaign_for_champion(champion_id, campaign_type, db)
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


def _quest_objective_conversation_guids(db, champ_id, quest_script,
                                        objective):
    """Return the valid completion conversations for one quest objective.

    A number of shared AZ1 QuestTemplate records contain the Ardent
    conversation ID even for Underworld champions.  The authored
    ``quest_conversations`` catalog has the faction-specific completion rows,
    so use those as the authoritative fallback while retaining any
    conversation IDs that are faction-neutral.
    """
    if not isinstance(objective, dict):
        return []
    explicit = []
    for value in objective.get("conversation_ids") or []:
        if value and str(value) not in explicit:
            explicit.append(str(value))
    if objective.get("conversation") and str(objective["conversation"]) not in explicit:
        explicit.append(str(objective["conversation"]))
    try:
        faction = _quest_faction_for_champion(db, champ_id)
    except Exception:
        faction = "Ardent"
    champion = _get_champion(db, champ_id)
    race = _RACE_NAMES.get(champion[2]) if champion else None
    race_key = re.sub(r"[^a-z0-9]", "", str(race or "").lower())
    rows = pve_db.db_quest_conversations(quest_script, db)
    faction_neutral = {str(guid) for guid, _role, row_faction, _name in rows
                       if not row_faction}
    matching_complete = [
        row for row in rows
        if row[1] == "complete" and _quest_row_matches_faction(row[2], faction)
    ]
    # A few AZ1 faction quests have one completion conversation per race.
    # Prefer the exact authored race variant whenever one exists; otherwise
    # use the faction-level completion record.
    race_rows = [
        row for row in matching_complete
        if race_key and race_key in re.sub(
            r"[^a-z0-9]", "", str(row[3] or "").lower())
    ]
    selected_complete = race_rows or matching_complete
    faction_rows = {str(row[0]) for row in selected_complete}
    # Preserve authored neutral IDs. For explicit faction-specific IDs,
    # replace a mismatching faction with the matching completion variant.
    valid_explicit = set(explicit) & faction_neutral
    for guid in explicit:
        matching_rows = [row for row in rows if str(row[0]) == guid]
        if not matching_rows:
            valid_explicit.add(guid)
            continue
        if any(not row[2] for row in matching_rows):
            valid_explicit.add(guid)
            continue
        if any(_quest_row_matches_faction(row[2], faction)
               for row in matching_rows):
            # Do not retain an explicit faction ID for the wrong race when
            # this quest has authored race-specific completion variants.
            if not race_rows or any(row[0] == guid for row in race_rows):
                valid_explicit.add(guid)
    result = sorted(valid_explicit | faction_rows)
    return result or explicit


def _quest_objective_scene_guid(db, quest_script, objective):
    """Resolve an AZ1 encounter/dungeon objective to its authored scene.

    Older QuestTemplate data stores a dungeon template GUID or an empty
    encounter GUID for several AZ1 objectives.  The quest script/title and
    the AZ1 encounter scene name are the remaining authored link.  Resolve it
    once when a quest state is materialized so normal encounter progression
    remains GUID-based.
    """
    if not isinstance(objective, dict):
        return None
    explicit = str(objective.get("encounter") or "").strip()
    if explicit and explicit != "00000000-0000-0000-0000-000000000000":
        if pve_db.db_encounter_scene_exists(explicit, db):
            return explicit
    objective_type = str(objective.get("type") or "").lower()
    if objective_type not in {"encounter", "dungeon"}:
        return None
    rows = pve_db.db_az1_scene_rows(db)
    if not rows:
        return None
    script_tokens = set(re.findall(r"[a-z0-9]+", str(quest_script or "").lower()))
    title_tokens = set(re.findall(
        r"[a-z0-9]+", str(objective.get("title") or "").lower()))
    ignored = {
        "az", "area", "quest", "step", "find", "go", "to", "the",
        "and", "of", "in", "from", "at", "return", "defeat", "kill",
        "win", "match", "with", "investigate", "source", "get", "all",
        "one", "your", "back", "for", "on", "a",
    }
    script_tokens -= ignored
    title_tokens -= ignored
    best = None
    for guid, name, title in rows:
        scene_text = f"{name or ''} {title or ''}".lower()
        scene_tokens = set(re.findall(r"[a-z0-9]+", scene_text)) - ignored
        overlap = len(script_tokens & scene_tokens)
        title_overlap = len(title_tokens & scene_tokens)
        score = overlap * 12 + title_overlap * 4
        normalized_title = re.sub(r"[^a-z0-9]+", "", str(title or "").lower())
        normalized_objective = re.sub(
            r"[^a-z0-9]+", "", str(objective.get("title") or "").lower())
        if normalized_title and normalized_title in normalized_objective:
            score += 100
        if objective_type == "dungeon" and "dungeon" in str(name or "").lower():
            score += 8
        if score <= 0:
            continue
        candidate = (score, int("dungeon" in str(name or "").lower()), guid)
        if best is None or candidate > best[0]:
            best = (candidate, guid)
    return best[1] if best else None


def _materialize_quest_objectives(db, champ_id, quest_script, objectives):
    """Add faction-correct, runtime-resolvable links to quest objectives."""
    try:
        materialized = json.loads(json.dumps(objectives or []))
    except (TypeError, ValueError, json.JSONDecodeError):
        materialized = []
    for objective in materialized:
        if not isinstance(objective, dict):
            continue
        resolved_scene = _quest_objective_scene_guid(
            db, quest_script, objective)
        if resolved_scene:
            objective["encounter"] = resolved_scene
        if str(objective.get("type") or "").lower() in {
                "conversation", "convo"}:
            conversations = _quest_objective_conversation_guids(
                db, champ_id, quest_script, objective)
            if conversations:
                objective["conversation_ids"] = conversations
                objective["conversation"] = conversations[0]
    return materialized


def _quest_template(db, quest_script=None, campaign_group=None):
    """Select a QuestTemplate from the server-owned metadata table."""
    try:
        row = pve_db.db_quest_template(quest_script, campaign_group, db)
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
    # QuestTemplate is shared by the two AZ1 factions. Materialize the
    # faction-correct completion conversation and resolve legacy empty
    # encounter links before copying objectives into persistent quest state.
    q["objectives"] = _materialize_quest_objectives(
        db, champ_id, script_name, q.get("objectives"))
    existing_id = pve_db.db_quest_campaign_id(champ_id, script_name, db)
    if existing_id:
        return existing_id
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
                if obj.get("encounter") and pve_db.db_encounter_scene_exists(
                    obj["encounter"], db)
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
    pve_db.db_create_campaign(
        cid, inst_id, champ_id, pve_db.db_campaign_user_id(champ_id, db),
        champ_name, script_name, "QUEST", json.dumps(state), db, is_started=True)
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
    rows = pve_db.db_campaign_state_rows(
        champ_id, "QUEST", db, template_name=quest_script)
    if not rows:
        return None
    qid, state_json = rows[0]
    state = json.loads(state_json) if state_json else None
    if not state:
        return None
    flags = state.setdefault("Flags", {})
    objectives = flags.get("_quest_objectives") or []
    materialized = _materialize_quest_objectives(
        db, champ_id, quest_script, objectives)
    if materialized != objectives:
        objectives = materialized
        flags["_quest_objectives"] = objectives
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
            conversation_id = _crayburn_quest_end_conv(
                db, _race_name_for_campaign(db, champ_id))
        elif otype.lower() in {"convo", "conversation"}:
            conversation_id = obj.get("conversation")
        state.setdefault("VisLocs", []).append({
            "Data": {
                "name": obj["id"], "node": obj["id"], "type": otype,
                "autostart": False, "autopan": False, "autotrigger": False,
                "battle": None, "completed": False, "enabled": True, "visible": True,
                "repeatable": False, "givequest": False, "turninquest": False,
                "impassable": False, "unknown": False,
                # Encounter objectives keep their scene link (Tamed's next
                # capture); the retry/complete bookkeeping matches on it.
                "encounter": (obj.get("encounter")
                              if otype.lower() == "encounter" else None),
                "encounter_desc": None, "allow_cancel": False,
                "conversationId": conversation_id,
            }
        })
        state.setdefault("LocNodes", []).append(
            {"Name": obj["id"], "Data": {"id": obj["id"], "type": "DEFAULT"}})
        state["Flags"]["_quest_objective_idx"] = nxt
        state["ALoc"] = obj["id"]
        state["LastNode"] = obj["id"]
    else:
        # The conversation/encounter just completed the final objective. Keep
        # the completed location in the journal, but close the QUEST campaign
        # so the client no longer advertises it as active.
        state["Flags"]["_quest_objective_idx"] = nxt
        state["Finished"] = _now_utc()
        state["FinishReason"] = "Complete"
        state["ALoc"] = None
        state["CurState"] = "EXPLORE"
    _save_campaign_state(db, qid, state)
    db.commit()
    return state


def _active_area_encounter_guid(db, champ_id):
    """Return the scene currently being resolved by the champion's area map.

    A few authored encounter objectives contain the all-zero encounter GUID.
    The area campaign still persists the concrete scene in
    ``ActiveEncounterGuid`` while the battle is resolving, so use that
    authoritative active encounter as the compatibility link.
    """
    rows = [(row[1],) for row in pve_db.db_campaign_state_rows(
        champ_id, "AREA", db, newest_first=True)]
    for (raw_state,) in rows:
        try:
            state = json.loads(raw_state or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        scene_guid = state.get("ActiveEncounterGuid")
        if scene_guid:
            return str(scene_guid)
    return None


def _advance_quest_encounter_objectives(db, champ_id, scene_guid):
    """Advance active non-taming quests whose encounter just succeeded.

    Normally the objective carries its authored encounter GUID.  When the
    source data omitted that GUID, only an encounter objective on the
    champion's currently active area battle may use the finished scene as its
    link; ordinary victories cannot advance unrelated journal objectives.
    """
    if not scene_guid:
        return []
    active_area_scene = _active_area_encounter_guid(db, champ_id)
    rows = [(pve_db.db_campaign_template_name(row[0], conn=db),)
            for row in pve_db.db_campaign_state_rows(
                champ_id, "QUEST", db, exclude_template="az01_tamed")]
    advanced = []
    for (script,) in rows:
        _qid, state = _quest_state_row(db, champ_id, script)
        if not state or state.get("Finished"):
            continue
        flags = state.get("Flags") or {}
        objectives = _materialize_quest_objectives(
            db, champ_id, script, flags.get("_quest_objectives") or [])
        if objectives != (flags.get("_quest_objectives") or []):
            flags["_quest_objectives"] = objectives
            _save_campaign_state(db, _qid, state)
            db.commit()
        try:
            index = int(flags.get("_quest_objective_idx", 0))
        except (TypeError, ValueError):
            index = 0
        if index >= len(objectives):
            continue
        objective = objectives[index]
        expected = objective.get("encounter")
        if not expected:
            expected = _quest_objective_scene_guid(db, script, objective)
        if (not expected and
                str(objective.get("type") or "").lower() == "encounter" and
                active_area_scene == str(scene_guid)):
            expected = active_area_scene
        if expected and str(expected) == str(scene_guid):
            updated = _advance_quest_campaign(db, champ_id, script, scene_guid)
            if updated:
                advanced.append(updated)
    return advanced


def _consume_pending_quest_progress(db, champ_id):
    """Return one queued quest state that still needs a client refresh.

    A repair performed outside the live request that completed the encounter
    can fix SQLite without producing the normal ``quest_progress`` notify.
    Keep that notify pending in the quest state until the champion next asks
    for campaign state, then consume it after persisting the corrected state.
    """
    rows = pve_db.db_campaign_state_rows(champ_id, "QUEST", db)
    for quest_id, raw_state in rows:
        try:
            state = json.loads(raw_state or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not state.pop("_quest_progress_notify_pending", False):
            continue
        _save_campaign_state(db, quest_id, state)
        db.commit()
        state.setdefault("CampID", quest_id)
        state.setdefault("ChampID", champ_id)
        return state
    return None


def _advance_quest_conversation_objectives(db, champ_id, conversation_guid):
    """Advance active quest objectives completed by a conversation.

    QuestTemplate conversation IDs are the authoritative link.  A few older
    extracts used a champion GUID for Warren's final conversation; the static
    seed migration normalizes that value, while this matcher also accepts the
    authored conversation as a compatibility fallback for existing saves.
    """
    if not champ_id or not conversation_guid:
        return []
    guid = str(conversation_guid)
    rows = [(pve_db.db_campaign_template_name(row[0], conn=db),)
            for row in pve_db.db_campaign_state_rows(champ_id, "QUEST", db)]
    advanced = []
    for (script,) in rows:
        _qid, state = _quest_state_row(db, champ_id, script)
        if not state or state.get("Finished"):
            continue
        flags = state.get("Flags") or {}
        objectives = _materialize_quest_objectives(
            db, champ_id, script, flags.get("_quest_objectives") or [])
        if objectives != (flags.get("_quest_objectives") or []):
            flags["_quest_objectives"] = objectives
            _save_campaign_state(db, _qid, state)
            db.commit()
        try:
            index = int(flags.get("_quest_objective_idx", 0) or 0)
        except (TypeError, ValueError):
            index = 0
        if not (0 <= index < len(objectives)):
            continue
        objective = objectives[index]
        if str(objective.get("type") or "").lower() not in {
                "conversation", "convo"}:
            continue
        expected = set(_quest_objective_conversation_guids(
            db, champ_id, script, objective))
        # Compatibility for the original Cross Zodiac River extract, whose
        # final objective accidentally contained Warren's champion GUID.
        if (str(script) == "az01_q_cross_the_river_part2" and
                "de519b67-1e3c-4fd1-96a1-909d2b6b3c67" in expected):
            expected.add("08b9b8ab-2100-4f8d-87b0-18369eb4ecb4")
        if guid not in expected:
            continue
        updated = _advance_quest_campaign(db, champ_id, script)
        if updated:
            advanced.append(updated)
    return advanced


def _quest_encounter_is_pending(db, champ_id, scene_guid):
    """Return whether an unfinished quest currently points at this scene.

    Quest objectives are authored with encounter GUIDs.  Looking at the
    current objective rather than a location name keeps ordinary encounters
    completable while quest encounters remain available until their objective
    has actually been satisfied.
    """
    if not champ_id or not scene_guid:
        return False
    rows = [(row[1],) for row in pve_db.db_campaign_state_rows(
        champ_id, "QUEST", db)]
    for (raw_state,) in rows:
        try:
            state = json.loads(raw_state or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if state.get("Finished"):
            continue
        flags = state.get("Flags") or {}
        objectives = flags.get("_quest_objectives") or []
        try:
            index = int(flags.get("_quest_objective_idx", 0) or 0)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(objectives):
            expected = objectives[index].get("encounter")
            if expected and str(expected) == str(scene_guid):
                return True
    return False


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
# Race id -> starter panorama config (bundle, NPCs, conversation GUIDs).
_AZ0_RACE_CONFIG = {
    int(k): v for k, v in json.loads(
        (CAMPAIGN_ROOT / "campaign_runtime" / "az0_races.json")
        .read_text(encoding="utf-8")).items()}



def _crayburn_quest_end_conv(db, race_name):
    """GUID of the race's "Crayburn Castle - <Race> - Quest End" report
    conversation (authored in hex-private/campaign/campaign_data), or None."""
    row = db.execute(
        "SELECT conversation_guid FROM campaign_node_conversations "
        "WHERE conversation_name=? LIMIT 1",
        (f"Crayburn Castle - {race_name} - Quest End",)).fetchone()
    return row[0] if row else None


def _az0_config(champion_race):
    """Return the AZ0 race config for a champion race id, or None."""
    return _AZ0_RACE_CONFIG.get(champion_race)


def _convo_location(name, conversation_id, *, givequest=False,
                    turninquest=False, repeatable=False):
    return {
        "Data": {
            "name": name, "node": name, "type": "Convo",
            "autostart": False, "autopan": False, "autotrigger": False,
            "battle": None, "completed": False, "enabled": True, "visible": True,
            "repeatable": bool(repeatable), "givequest": bool(givequest),
            "turninquest": bool(turninquest),
            "impassable": False, "unknown": False, "encounter": None,
            "encounter_desc": None, "allow_cancel": False,
            "conversationId": conversation_id,
        }
    }












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
    report_conv = _crayburn_quest_end_conv(db, _RACE_NAMES.get(champ[2], ""))
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




















# --- editor tags on node conversations -------------------------------------
# ``trigger_json.tags`` (campaign editor, applied by
# hex-private/campaign/campaign_data.py) select
# a conversation only while every tag holds; see campaign_rules.parse_tag.
# Rows without tags keep the label/visit heuristics below.

def _conversation_tag_state(db, champ_id, state, node, tags, outcome=None):
    """The campaign_rules progress view that ``tags`` read."""
    data = ((state or {}).get("PublicState", {}) or {}).get("Data", {}) or {}
    champ = _get_champion(db, champ_id) if champ_id is not None else None
    flags = {str(f) for f in data.get("conversation_flags") or ()}
    # ``flag(X)`` also reads the area's own boolean markers (gaal_fortune ...).
    flags |= {str(k) for k, v in data.items() if v is True}
    quests = {}
    condition = campaign_rules.tags_condition(tags, str(node or ""))
    for part in campaign_rules._conditions(condition):
        script = part.get("quest")
        if not script or script in quests or champ_id is None:
            continue
        _quest_id, quest_state = _quest_state_row(db, champ_id, script)
        if isinstance(quest_state, dict):
            quest_flags = quest_state.get("Flags") or {}
            try:
                step = int(quest_flags.get("_quest_objective_idx", 0) or 0)
            except (TypeError, ValueError):
                step = 0
            quests[script] = {"status": "finished" if quest_state.get(
                "Finished") else "active", "step": step}
    visited = {str(v) for v in data.get("visited_nodes") or ()}
    nodes = {}
    for loc in (state or {}).get("VisLocs") or ():
        loc_data = (loc or {}).get("Data") or {}
        loc_node = loc_data.get("node")
        if not loc_node:
            continue
        names = {str(loc_node), str(loc_data.get("name") or ""),
                 str(loc_data.get("title") or "")}
        nodes[loc_node] = {"visits": 1 if names & visited else 0,
                           "completed": bool(loc_data.get("completed"))}
    # talkedAt(Node): conversations finished at the node (NodeR == Node00R).
    for key, count in (data.get("conversation_visits") or {}).items():
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 0
        short = re.fullmatch(r"Node00([A-Z])", str(key))
        long_ = re.fullmatch(r"Node([A-Z])", str(key))
        for alias in {str(key), f"Node{short.group(1)}" if short else "",
                      f"Node00{long_.group(1)}" if long_ else ""} - {""}:
            entry = nodes.setdefault(alias, {})
            entry["talks"] = max(int(entry.get("talks", 0)), count)
    if outcome:
        nodes.setdefault(str(node), {})["outcome"] = outcome
    counters = data.get("counters") or {}
    return {"flags": sorted(flags), "quests": quests, "nodes": nodes,
            "counters": counters,
            # cameFrom(Node) / a let_pass() for this visit (campaign_rules).
            "came_from": str(data.get("came_from") or ""),
            "pass": str(data.get("pass_node") or ""),
            "keywords": campaign_rules._keyword_names(
                data.get("keywords") or data.get("champion_keywords")),
            "faction": _RACE_FACTIONS.get(champ[2], "") if champ else "",
            "race": _RACE_NAMES.get(champ[2], "") if champ else "",
            "current": str(node or "")}


def _conversation_tags_hold(db, champ_id, state, node, tags, outcome=None):
    if not tags:
        return True
    try:
        view = _conversation_tag_state(db, champ_id, state, node, tags, outcome)
        return campaign_rules.evaluate(tags, view, str(node or ""))
    except campaign_rules.RulesError as exc:
        print(f"[campaign] ignoring conversation tags {tags}: {exc}")
        return False


def _quest_row_tags(raw_conditions):
    """Editor tags on a quest_conversations row (``conditions_json.tags``)."""
    try:
        conditions = json.loads(raw_conditions or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return campaign_rules.trigger_tags(conditions if isinstance(conditions, dict) else {})


def _conversation_tags_faction_holds(db, champ_id, tags):
    """Only the faction/race tags of a row: the marker selector's faction
    filter (its fallback considers every row of the champion's faction)."""
    flat = [t for group in tags for t in group] if campaign_rules.is_groups(tags) else tags
    audience = [t for t in flat if re.match(r"!?\s*(faction|ChampionIsRace)\(", t)]
    return _conversation_tags_hold(db, champ_id, None, "", audience) if audience else True


def _conversation_outcome_and_tags(trigger):
    """``(outcome, selection tags)`` for one node-conversation trigger."""
    try:
        outcome, tags = campaign_rules.split_outcome(
            campaign_rules.trigger_tags(trigger))
    except campaign_rules.RulesError:
        return str(trigger.get("outcome") or "").lower(), ["!flag(_bad_tags)"]
    return outcome or str(trigger.get("outcome") or "").lower(), tags


def _select_tagged_rows(db, champ_id, state, node, rows, outcome=None):
    """``(first tagged match, untagged rows)`` for an ordered row list."""
    untagged = []
    for row in rows:
        _row_outcome, tags = _conversation_outcome_and_tags(row[1])
        if not tags:
            untagged.append(row)
        elif _conversation_tags_hold(db, champ_id, state, node, tags, outcome):
            return row, untagged
    return None, untagged


# Client-protocol SendEvent names; anything else is an answer's
# ServerScriptEvent (the only way the client reports a conversation choice).
_CAMPAIGN_PROTOCOL_EVENTS = frozenset({
    "accept", "boat_fail_shown", "cancel", "conv_done", "count_choice",
    "display_done", "empty_done", "enc_cancel", "force_move", "pass_node",
    "path_reveal_complete", "return_parent", "shroom_choice", "start",
    "visit_node", "visit_path", "warp_move",
})
_conversation_event_lock = threading.RLock()
_TRANQUIL_DREAM_ANSWER_POINTS = {
    "one_point_answer": 1,
    "two_point_answer": 2,
    "three_point_answer": 3,
}
_DYNAMIC_SCORE_FLAG = re.compile(r"^score_(\d+)$")


def _record_dungeon_script_score(state, template_name, script_name):
    """Record Tranquil Dream's accumulated answer score as ``score_N``.

    The campaign row is one dungeon run, so keeping this boolean in its
    PublicState makes the score local to that dungeon. Replacing the previous
    score flag leaves one unambiguous value for ``flag(score_N)`` conditions.
    """
    if str(template_name or "").strip().casefold() != "the tranquil dream":
        return None
    points = _TRANQUIL_DREAM_ANSWER_POINTS.get(str(script_name or ""))
    if points is None:
        return None

    data = state.setdefault("PublicState", {}).setdefault("Data", {})
    score_flags = []
    current_score = 0
    for key, value in data.items():
        match = _DYNAMIC_SCORE_FLAG.fullmatch(str(key))
        if match:
            score_flags.append(key)
            if value:
                current_score = max(current_score, int(match.group(1)))
    for key in score_flags:
        data.pop(key, None)
    total = current_score + points
    data[f"score_{total}"] = True
    return total


def conversation_event(conversation_guid, scriptEventName):
    """Extension hook for a conversation's ServerScriptEvent.

    Campaign integrations can replace this no-op with their event handling.
    Calls are serialized and receive the active conversation GUID and the
    authored event name.
    """
    return None


def _active_conversation_guid(state, ctype):
    """Find the conversation attached to the campaign's active location."""
    if (state.get("ActiveEncounterGuid") and
            state.get("ActiveEncounterConversationGuid")):
        return state["ActiveEncounterConversationGuid"]
    locations = state.get("VisLocs")
    if not isinstance(locations, list):
        return None

    active_node = state.get("ALoc") or state.get("LastNode")
    if not active_node:
        return None
    if str(ctype or "").upper() == "DUNGEON":
        active_node = _resolve_node(state, active_node)

    for location in locations:
        data = location.get("Data") or {}
        if active_node in (data.get("node"), data.get("name"),
                           data.get("title")):
            return data.get("conversationId")
    return None


def _note_conversation_choice(state, event_name):
    """Remember the open conversation's latest ServerScriptEvent."""
    if event_name and event_name not in _CAMPAIGN_PROTOCOL_EVENTS:
        state.setdefault("PublicState", {}).setdefault("Data", {})[
            "conversation_choice"] = str(event_name)


def _conversation_name(db, conversation_guid):
    row = pve_db.db_conversation_name(conversation_guid, db)
    return str(row or "").strip()


def _conversation_ending_actions(db, conversation_guid, ending, holds=None):
    """Parsed editor ending actions (``set_flag`` / ``start_quest`` ...) that
    ``ending`` (or any ending: ``*``) of a conversation runs.

    A conditional action (``... if <tags>``) is kept only when ``holds(tags)``
    says its tags hold for the champion (no ``holds``: never)."""
    if not conversation_guid:
        return []
    parsed, seen = [], set()
    for table in pve_db.db_conversation_action_tables(conversation_guid, conn=db):
        for action in campaign_rules.ending_actions(table, str(ending or "")):
            if action in seen:
                continue
            seen.add(action)
            try:
                item = campaign_rules.parse_action(action)
            except campaign_rules.RulesError as exc:
                print(f"[campaign] ignoring ending action {action!r}: {exc}")
                continue
            if item.get("if") and not (holds and holds(item["if"])):
                continue
            parsed.append(item)
    return parsed


def _ending_holds(db, champ_id, state):
    """``holds(tags)`` for conditional ending actions, read at the active node."""
    node = _resolve_node(state, str(state.get("ALoc") or state.get("LastNode") or ""))
    return lambda tags: _conversation_tags_hold(db, champ_id, state, node, tags)


def _spend_conversation_gold(db, champ_id, conversation_guid, ending, holds=None):
    """Take the gold an ending's ``spend_gold(N)`` actions charge (a bribe, a
    fortune reading); returns the AppliedUpdates that refresh the client's
    gold. The answer's HasCurrency prerequisite already required the gold;
    a balance that fell short since is not taken below zero."""
    applied = _empty_applied_updates()
    cost = sum(action["spend_gold"] for action in
               _conversation_ending_actions(db, conversation_guid, ending, holds)
               if action.get("spend_gold"))
    champ = _get_champion(db, champ_id) if cost else None
    if not champ:
        return applied
    new_gold, platinum = db_spend_user_gold(champ[1], cost, conn=db)
    applied["Accounts"].append({"Account": {
        "Gold": new_gold, "Platinum": platinum}})
    return applied


def _record_conversation_flags(db, champ_id, state, ctype, conversation_guid):
    """Set ``conversation(Name)`` / ``conversation(Name,Ending)`` on conv_done,
    plus the flags its ``set_flag(X)`` ending actions name.

    The ending is the ServerScriptEvent its final answer fired; the flags
    live on the champion's AREA campaign (``conversation_flags``) so area,
    panorama and dungeon conversations share them. Returns the ending.
    """
    data = state.setdefault("PublicState", {}).setdefault("Data", {})
    ending = data.pop("conversation_choice", "") or ""
    name = _conversation_name(db, conversation_guid) if conversation_guid else ""
    if re.fullmatch(r"Diff_\d+", str(ending or "")):
        # A difficulty chosen in a repeat-encounter conversation (Army of
        # Myth ...): the next won battle reports it as a battle event.
        data["pending_battle_events"] = [str(ending)]
    flags = []
    if name:
        flags.append(campaign_rules.conversation_flag(name))
        if ending:
            flags.append(campaign_rules.conversation_flag(name, ending))
    actions = _conversation_ending_actions(
        db, conversation_guid, ending, _ending_holds(db, champ_id, state))
    flags += [action["set_flag"] for action in actions if action.get("set_flag")]
    clears = [action["clear_flag"] for action in actions if action.get("clear_flag")]
    if not flags and not clears:
        return ending
    if str(ctype or "").upper() == "AREA":
        target = data
    else:
        area = _get_existing_campaign_for_champion(db, champ_id, "AREA")
        if not area or not isinstance(area[4], dict):
            return ending
        area_state = area[4]
        target = area_state.setdefault("PublicState", {}).setdefault("Data", {})
    current = list(target.get("conversation_flags") or ())
    added = [flag for flag in dict.fromkeys(flags) if flag not in current]
    removed = _clear_campaign_flags(target, clears)
    if not added and not removed:
        return ending
    target["conversation_flags"] = (
        list(target.get("conversation_flags") or ()) + added)
    if target is not data:
        _save_campaign_state(db, area[0], area_state)
    return ending


def _clear_campaign_flags(data, patterns):
    """``clear_flag(X)`` / ``clear_flag(prefix*)``: drop matching flags from a
    campaign's public data (``conversation_flags`` and boolean markers such
    as ``tamed:...``). Returns whether anything was removed."""
    removed = False
    for pattern in patterns or ():
        flags = list(data.get("conversation_flags") or ())
        kept = [f for f in flags if not campaign_rules.flag_matches(f, pattern)]
        if len(kept) != len(flags):
            data["conversation_flags"] = kept
            removed = True
        for key in [k for k, v in data.items()
                    if v is True and campaign_rules.flag_matches(k, pattern)]:
            data.pop(key)
            removed = True
    return removed


def _conversation_matches_faction(conversation_name, faction):
    """Match explicit authored Ardent/Underworld conversation labels."""
    if not faction:
        return True
    label = str(conversation_name or "").lower()
    explicit = None
    if "underworld" in label:
        explicit = "underworld"
    elif "ardent" in label or "aria" in label:
        explicit = "ardent"
    return explicit is None or explicit == str(faction).lower()


def _has_faction_variants(rows):
    """Whether authored rows contain explicit faction-labelled variants."""
    return any(
        "ardent" in str(row[3] or "").lower() or
        "aria" in str(row[3] or "").lower() or
        "underworld" in str(row[3] or "").lower()
        for row in rows
    )


_AZ1_MASKED_ARMY_QUESTS = (
    ("az01_q_fort_romor", "Underworld"),
    ("az01_q_usurper", "Ardent"),
)






def _is_repeat_conversation(trigger):
    """A repeat-visit conversation: ``visit: repeat`` or its tag form
    (selected by a positive ``talkedAt(...)``)."""
    if str(trigger.get("visit") or "").lower() == "repeat":
        return True
    tags = campaign_rules.trigger_tags(trigger)
    flat = [t for group in tags for t in group] if campaign_rules.is_groups(tags) else tags
    return any(t.startswith("talkedAt(") for t in flat)




















def _apply_ending_movement(db, champ_id, state, data, conversation_guid,
                           ending):
    """What a finished map conversation does to the player's position.

    ``encounter(Scene)`` ending actions turn the location into that battle
    and start it; ``let_pass()`` lets the player leave an impassable node on
    this visit. Otherwise a conversation that ends on an impassable node
    (the client cannot leave it) walks the player back to where they came
    from through ``force_move_dest``.
    """
    pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
    node = str(data.get("node") or "")
    actions = _conversation_ending_actions(
        db, conversation_guid, ending, _ending_holds(db, champ_id, state))
    battle = next((a["encounter"] for a in actions if a.get("encounter")), None)
    if battle:
        data.update({
            "type": "Encounter", "encounter": battle["scene"],
            "action_encounter": battle["scene"], "conversationId": None,
            "completed": False, "outcome_conversation": False,
            "autostart": True, "enabled": True, "visible": True})
        _offer_mod_choice(db, champ_id, state, data)
        if battle.get("conversation_id"):
            state["ActiveEncounterConversationGuid"] = battle["conversation_id"]
        state["ALoc"] = data.get("name") or node
        return
    if any(a.get("let_pass") for a in actions) and node:
        pdata["pass_node"] = node
        data["impassable"] = False
        data["autostart"] = False
        return
    if data.get("impassable") and not data.get("completed"):
        back = str(pdata.get("came_from") or "")
        if back and back != node:
            pdata["force_move_dest"] = back
            data["autostart"] = False






















def _quest_state_row(db, champ_id, quest_script):
    rows = pve_db.db_campaign_state_rows(
        champ_id, "QUEST", db, template_name=quest_script,
        newest_first=True)
    row = rows[0] if rows else None
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


# Later AZ1 quest starts share the Cave-In/Mesa panorama with the first
# faction quests, but are unlocked by the preceding authored quest chain.
# Keep the prerequisite in campaign metadata logic so a missing QUEST row does
# not accidentally turn every catalogued conversation into a quest giver.
_AZ1_QUEST_START_PREREQUISITES = {
    "q_smoldering_dead": ("q_army_of_myth",),
}


def _quest_start_prerequisites_met(db, champ_id, quest_script):
    for prerequisite in _AZ1_QUEST_START_PREREQUISITES.get(
            str(quest_script or ""), ()):
        _qid, state = _quest_state_row(db, champ_id, prerequisite)
        if not state or not state.get("Finished"):
            return False
    return True






# Named quest-start content hooks (``quest_templates.start_hook``).  Map
# gates come from hex-private/campaign/campaign_rules via campaign_gates and quest content from
# the editor data; none are registered now.
_QUEST_START_HOOKS = {}


def _run_quest_start_hook(db, champ_id, hook_name):
    """Run a named, whitelisted metadata hook against the champion's AZ1 area."""
    hook = _QUEST_START_HOOKS.get(str(hook_name or ""))
    if hook is None:
        return False
    row = pve_db.db_active_area_campaign(champ_id, conn=db)
    if not row:
        return False
    try:
        state = json.loads(row[1] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    changed = bool(hook(db, champ_id, state))
    if changed:
        _save_campaign_state(db, row[0], state)
        db.commit()
    return changed





def _grant_quests_for_conversation(db, champ_id, campaign_template,
                                   conversation_guid, ending="", holds=None):
    """Grant all eligible quest-start rows for a completed conversation,
    plus every quest its ``start_quest(X)`` ending actions name."""
    if not conversation_guid:
        return [], []
    faction = _quest_faction_for_champion(db, champ_id)
    rows = list(pve_db.db_quest_start_rows(
        conversation_guid, campaign_template, conn=db))
    listed = {row[0] for row in rows}
    for action in _conversation_ending_actions(db, conversation_guid, ending,
                                               holds):
        script = action.get("start_quest")
        if not script or script in listed:
            continue
        template = pve_db.db_quest_template_start(script, conn=db)
        if template is None:
            print(f"[campaign] start_quest: unknown quest {script!r}")
            continue
        listed.add(script)
        rows.append((script, template[0], "", template[1]))
    spawned, hooks = [], []
    for script, hook, row_faction, campaign_group in rows:
        if not _quest_row_matches_faction(row_faction, faction):
            continue
        existing_id, _existing_state = _quest_state_row(db, champ_id, script)
        quest_id = _ensure_quest_campaign(db, champ_id, campaign_group, script)
        if not quest_id:
            continue
        if existing_id is None:
            qstate = pve_db.db_campaign_state(quest_id, conn=db)
            if qstate:
                spawned.append((quest_id, script, json.loads(qstate)))
            if hook:
                hooks.append(hook)
    for hook in hooks:
        _run_quest_start_hook(db, champ_id, hook)
    return spawned, hooks




def _normalize_starter_panorama_state(state, cfg):
    """Repair client-facing NPC metadata in an existing starter panorama.

    Early campaign states did not include the trainer's champion template ID
    and marked actionable NPCs as not giving a quest.  The panorama client
    uses the former for the profile portrait (especially once a conversation
    has become an encounter) and the latter for the quest marker, so repair
    those fields whenever an older state is returned.
    """
    # A non-starter panorama is authored by an AREA panorama scene.  Its
    # locations must not be repaired as though they were the AZ0 tutorial
    # NPCs; the scene-specific state was deliberately built by the area
    # transition helper.
    if state.get("PanoramaSceneGuid") or state.get("RulesZone"):
        return False   # an AZ1 panorama, or run by campaign_runtime
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
    """The node's conversation (and, for battles, success/fail) GUIDs from
    the ``Crayburn Castle - <Race> - <Node>`` rows of
    hex-private/campaign/campaign_data, or {}."""
    conv_name = _CASTLE_NODE_CONV.get(node)
    if not conv_name:
        return {}
    import db as _shared_db
    base = f"Crayburn Castle - {race_name or 'Necrotic'} - {conv_name}"
    found = {}
    for key, name in (("conv", base), ("success", base + " Success"),
                      ("fail", base + " Fail")):
        row = _shared_db._db.execute(
            "SELECT conversation_guid FROM campaign_node_conversations "
            "WHERE conversation_name=? LIMIT 1", (name,)).fetchone()
        if row:
            found[key] = row[0]
    return found


def _crayburn_node_is_encounter(race_name, node):
    """True if the castle node fires a battle (race data has success+fail
    conversations — the data-driven discriminator)."""
    nd = _crayburn_node_data(race_name, node)
    if not nd:
        return False
    return "success" in nd and "fail" in nd



def _is_location_completed(state, node):
    for loc in state.get("VisLocs", []):
        d = loc.get("Data", {})
        if d.get("node") == node or d.get("name") == node:
            return bool(d.get("completed", False))
    return False


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


def _crayburn_first_incomplete(state):
    """Return the first unfinished authored castle node, if any."""
    return next(
        (node for node in _CASTLE_CHAIN[1:]
         if not _is_location_completed(state, node)),
        None,
    )


def _crayburn_return_node(state, node):
    """Return the latest completed map node before an encounter."""
    try:
        idx = _CASTLE_CHAIN.index(node)
    except ValueError:
        return "Entrance"
    for candidate in reversed(_CASTLE_CHAIN[:idx]):
        if _is_location_completed(state, candidate):
            return candidate
    return _CASTLE_CHAIN[max(0, idx - 1)]


def _reveal_crayburn_node(state, node):
    """Synchronize castle fog-of-war with the first unfinished node.

    The client renders an enabled/visible node as a legal destination.  Keep
    the next node hidden until the current encounter or conversation has been
    completed, including after a failed encounter is restored for retry.
    """
    first_incomplete = _crayburn_first_incomplete(state)
    frontier = (len(_CASTLE_CHAIN) - 1 if first_incomplete is None else
                _CASTLE_CHAIN.index(first_incomplete))
    for idx, n in enumerate(_CASTLE_CHAIN):
        for loc in state.get("VisLocs", []):
            d = loc.get("Data", {})
            if d.get("node") == n:
                allowed = idx <= frontier
                d["visible"] = allowed
                d["enabled"] = allowed
                if not allowed:
                    d["autostart"] = False
                break

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
    race = pve_db.db_campaign_champion_race(camp_id, conn=db)
    if race is None:
        return "Necrotic"
    return _RACE_NAMES.get(race, "Necrotic")

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
    row = pve_db.db_campaign_runtime_row(camp_id, conn=db)
    if not row:
        return None
    state_json, ctype = row
    if (ctype or "").upper() != "DUNGEON":
        return None
    state = json.loads(state_json) if state_json else None
    if state is None:
        champ_row = pve_db.db_campaign_identity_state(camp_id, conn=db)
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
                _save_campaign_state(db, camp_id, state)
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
                _save_campaign_state(db, camp_id, state)
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
                pve_db.db_campaign_update_state(
                    panorama_id, json.dumps(panorama_state), db, started=True)
                db.commit()
        if next_node:
            _reveal_crayburn_node(state, next_node)
        _save_campaign_state(db, camp_id, state)
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

    _save_campaign_state(db, camp_id, state)
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

    champ_id = state.get("ChampID") or 0
    deck_db_id = pve_db.db_champion_last_deck_id(champ_id, conn=db)
    deck_uid64 = (deck_db_id << 8) | 17 if deck_db_id else 0
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
    race_key = int(race_id) if isinstance(race_id, int) else 0
    race_name = _RACE_NAMES.get(race_key, champion_race)
    if campaign_type == "PANORAMA":
        cfg = _az0_config(race_key)
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
        castle_convs = {name: _crayburn_node_data(race_name, nid)
                        for nid, name in castle_conv_node.items() if name}
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


# ---------------------------------------------------------------------------
# Broken-state diagnostics
#
# A campaign state can be structurally valid JSON yet leave the player with
# nothing to click: a panorama whose NPC conversations are all completed, a
# location whose node name has no matching LocNode, etc.  The client renders
# the scene and silently waits, which reads as a soft-lock in bug reports.
# These helpers surface that condition in the session log (captured by
# ``!issue``) and through the ``!campaign`` developer command.
# ---------------------------------------------------------------------------

_CAMPAIGN_DIAG_LOCK = threading.Lock()
_CAMPAIGN_DIAG_WARNED = {}
_CAMPAIGN_DIAG_WARN_LIMIT = 256


def campaign_state_options(state):
    """Return ``[(kind, node, detail), ...]`` for the client's clickable options.

    Mirrors what the campaign UI enables: a visible location offering an
    unfinished (or repeating) conversation, or an unfinished encounter battle.
    """
    options = []
    for loc in (state or {}).get("VisLocs") or []:
        data = loc.get("Data") or {}
        if data.get("visible") is False or data.get("enabled") is False:
            continue
        node = str(data.get("node") or data.get("name") or "")
        conversation = str(data.get("conversationId") or "").strip()
        if conversation and (not data.get("completed") or data.get("repeatable")):
            options.append(("conversation", node, conversation))
            continue
        if (str(data.get("type") or "").lower() in {"encounter", "battle"}
                and not data.get("completed")):
            options.append(("encounter", node, str(
                data.get("encounter") or data.get("battle") or "")))
    return options


def campaign_state_diagnostic(state):
    """Return ``''`` when the state offers an action, else a reason string.

    Only campaign types with a known interaction contract are judged; an
    unfinished AREA map legitimately has no *location* option (its movement is
    path based), so it is only checked for the node-name contract.
    """
    if (not isinstance(state, dict) or not state.get("Started")
            or state.get("Finished")):
        return ""
    ctype = str(state.get("TempType") or "").upper()
    vis = state.get("VisLocs") or []
    node_names = set()
    for node in state.get("LocNodes") or []:
        name = str((node.get("Data") or {}).get("id") or node.get("Name") or "")
        if name:
            node_names.add(name.lower())
    problems = []
    for loc in vis:
        data = loc.get("Data") or {}
        if data.get("visible") is False:
            continue
        name = str(data.get("node") or data.get("name") or "").lower()
        if node_names and name and name not in node_names:
            problems.append(f"location {name!r} has no matching LocNode")
    if ctype in {"PANORAMA", "QUEST", "DUNGEON"} and not campaign_state_options(state):
        # AREA maps move along authored paths, so only the interactive scene
        # types are judged on a clickable location.
        problems.append("no conversation or encounter option")
    if not problems:
        return ""
    return ("no actionable campaign option: "
            + "; ".join(sorted(set(problems)))
            + f" [type={ctype or '-'}"
            f" node={state.get('PanoramaNode') or state.get('LastNode') or state.get('ALoc') or '-'}"
            f" locs={len(vis)} nodes={len(state.get('LocNodes') or [])}]")


def log_campaign_state_diagnostic(handler, camp_id, champion_id, state):
    """Log one broken-state warning per distinct reason (``!issue`` captures it)."""
    reason = campaign_state_diagnostic(state)
    if not reason:
        return ""
    fingerprint = (int(camp_id or 0), reason)
    with _CAMPAIGN_DIAG_LOCK:
        if fingerprint in _CAMPAIGN_DIAG_WARNED:
            return reason
        if len(_CAMPAIGN_DIAG_WARNED) >= _CAMPAIGN_DIAG_WARN_LIMIT:
            _CAMPAIGN_DIAG_WARNED.clear()
        _CAMPAIGN_DIAG_WARNED[fingerprint] = True
    log = getattr(handler, "_log_req", print)
    log(f"    Campaign broken state: {reason} "
        f"camp={camp_id} champion={champion_id}")
    return reason


def _repair_quest_active_objective(db, champ_id, state, template_name):
    """Re-materialize the active journal objective for an inert QUEST state."""
    template = _quest_template(db, template_name)
    if not template:
        return False
    flags = state.setdefault("Flags", {})
    objectives = _materialize_quest_objectives(
        db, champ_id, template_name, flags.get("_quest_objectives") or [])
    if not objectives:
        return False
    changed = objectives != flags.get("_quest_objectives")
    flags["_quest_objectives"] = objectives
    try:
        index = int(flags.get("_quest_objective_idx", 0) or 0)
    except (TypeError, ValueError):
        index = 0
    if index < 0 or index >= len(objectives):
        return changed
    obj = objectives[index]
    otype = str(obj.get("type") or "Convo")
    conversation_id = obj.get("conversation")
    if otype.lower() in {"convo", "conversation"}:
        if str(template.get("campaign_group") or "").upper() == "DUNGEON":
            conversation_id = _crayburn_quest_end_conv(
                db, _race_name_for_campaign(db, champ_id))
    target = str(obj.get("id") or "")
    seen = False
    for loc in state.setdefault("VisLocs", []):
        data = loc.setdefault("Data", {})
        if str(data.get("node") or data.get("name") or "") != target:
            continue
        seen = True
        repairs = {
            "type": otype, "enabled": True, "visible": True,
            "completed": False, "conversationId": conversation_id,
            "encounter": obj.get("encounter") or data.get("encounter"),
        }
        for key, value in repairs.items():
            if data.get(key) != value:
                data[key] = value
                changed = True
    if not seen:
        state["VisLocs"].append({
            "Data": {
                "name": target, "node": target, "type": otype,
                "autostart": False, "autopan": False, "autotrigger": False,
                "battle": None, "completed": False, "enabled": True,
                "visible": True, "repeatable": False, "givequest": False,
                "turninquest": False, "impassable": False, "unknown": False,
                "encounter": obj.get("encounter"), "encounter_desc": None,
                "allow_cancel": False, "conversationId": conversation_id,
            }
        })
        state.setdefault("LocNodes", []).append(
            {"Name": target, "Data": {"id": target, "type": "DEFAULT"}})
        changed = True
    state["ALoc"] = state.get("ALoc") or target
    state["LastNode"] = state.get("LastNode") or target
    return changed


def repair_broken_campaign_state(db, camp_id):
    """Repair one campaign row and return a structured report.

    Runs the same repair passes ``getcampstate`` uses, plus recovery fallbacks,
    then re-runs the diagnostic.  No client notification is sent here; the
    caller (the ``!campaign-cleanup`` command) owns the refresh push.
    """
    info = pve_db.db_campaign_query_row(camp_id, conn=db)
    if not info:
        return None
    champ_id, is_started, state_json, ctype, template_name = info
    upper = str(ctype or "").upper()
    try:
        state = json.loads(state_json or "{}")
    except (TypeError, ValueError):
        state = {}
    if not isinstance(state, dict) or not state:
        state = _build_initial_gameplay_state(camp_id, champ_id, ctype or "AREA")
    if state.get("RulesZone"):
        # campaign_runtime projects this row from the rules on every request.
        diagnostic = campaign_state_diagnostic(state)
        options = len(campaign_state_options(state))
        return {"camp_id": camp_id, "champ_id": champ_id,
                "campaign_type": upper, "template_name": template_name,
                "repairs": [], "before": diagnostic, "after": diagnostic,
                "before_options": options, "after_options": options,
                "state": state}
    original_state = json.dumps(state, sort_keys=True)
    before = campaign_state_diagnostic(state)
    before_options = len(campaign_state_options(state))
    repairs = []

    if upper == "PANORAMA":
        if not state.get("PanoramaSceneGuid"):
            champ = _get_champion(db, champ_id)
            cfg = _az0_config(champ[2]) if champ else None
            if cfg and _normalize_starter_panorama_state(state, cfg):
                repairs.append("restored starter panorama NPCs")
            if cfg and not campaign_state_options(state):
                # Last resort: rebuild the authored tutorial panorama, keeping
                # the client-facing start/progress bookkeeping that is valid.
                fresh = _build_starter_panorama_state(camp_id, champ_id, cfg)
                for key in ("Started", "Wins", "Losses", "Flags",
                            "TrainingVictoryPending", "PostCrayburnReport"):
                    if key in state:
                        fresh[key] = state[key]
                state = fresh
                repairs.append("rebuilt starter panorama")
    elif upper == "DUNGEON":
        rebuilt = _prepare_dungeon_state(
            state, _race_name_for_campaign(db, camp_id))
        if rebuilt is not state:
            state = rebuilt
            repairs.append("normalized dungeon state")
    elif upper == "QUEST":
        if _repair_quest_active_objective(
                db, champ_id, state, template_name):
            repairs.append("restored active quest objective")

    after = campaign_state_diagnostic(state)
    if repairs or json.dumps(state, sort_keys=True) != original_state:
        _save_campaign_state(db, camp_id, state)
        db.commit()
    return {
        "camp_id": camp_id,
        "champ_id": champ_id,
        "campaign_type": upper,
        "template_name": template_name,
        "repairs": repairs,
        "before": before,
        "after": after,
        "before_options": before_options,
        "after_options": len(campaign_state_options(state)),
        "state": state,
    }


def repair_user_campaigns(db, user_id, camp_id=None):
    """Repair every broken campaign for a profile, or one explicit campaign.

    ``camp_id`` is validated against the profile so a chat command cannot
    touch another player's campaign.  With no id, only states whose diagnostic
    currently fires are repaired; healthy campaigns are never rewritten.
    """
    if camp_id is not None:
        owner = pve_db.db_campaign_owner_user_id(int(camp_id), conn=db)
        if owner is None or owner != int(user_id):
            return None
        candidates = [(int(camp_id),)]
    else:
        candidates = []
        for row in pve_db.db_user_campaign_states(int(user_id), conn=db):
            cid = int(row[0])
            try:
                state = json.loads(row[5] or "{}")
            except (TypeError, ValueError):
                state = {}
            if not isinstance(state, dict) or campaign_state_diagnostic(state):
                candidates.append((cid,))
    reports = []
    for (cid,) in candidates:
        report = repair_broken_campaign_state(db, cid)
        if report:
            reports.append(report)
    return reports




def _prepare_dungeon_state(state, race_name=None):
    """Normalize a fresh Castle Crayburn DUNGEON state for delivery to the
    client. If the stored state is stale (built before the per-node
    conversations were seeded), rebuild it from _build_initial_gameplay_state
    so every node carries its race conversationId. A started dungeon with no
    completed node beyond the entrance is also repaired to the first
    conversation node; this recovers runs saved by the old deferred-travel
    implementation."""
    if not state or state.get("TempType") != "DUNGEON" or state.get("RulesZone"):
        return state       # (a RulesZone state is run by campaign_runtime)
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
        # A failed encounter returns to the preceding map tile with ALoc
        # empty.  Preserve that position while still repairing stale fog of
        # war from older states that exposed a later node.
        first_incomplete = _crayburn_first_incomplete(state)
        last_node = _resolve_node(state, state.get("LastNode") or "")
        if (state.get("ALoc") in (None, "") and
                last_node == first_incomplete and
                _crayburn_node_is_encounter(race_name, first_incomplete)):
            # Older saves recorded a failed encounter's restored node as the
            # physical position.  Convert that shape to the current map
            # contract so the player is returned to the completed predecessor.
            state["LastNode"] = _crayburn_return_node(state, first_incomplete)
        _reveal_crayburn_node(
            state, state.get("ALoc") or state.get("LastNode") or "Entrance")
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
        last_node = _resolve_node(state, state.get("LastNode") or "")
        # A failed encounter has already returned the token to a completed
        # predecessor.  Do not reactivate the pending encounter on reconnect;
        # the player must explicitly travel back to it from the map.
        returned_to_map = (
            state.get("ALoc") in (None, "") and
            last_node in _CASTLE_CHAIN[1:] and
            _CASTLE_CHAIN.index(last_node) <
            _CASTLE_CHAIN.index(first_pending))
        if returned_to_map:
            _reveal_crayburn_node(state, last_node)
        else:
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

# AZ1 AREA handoffs select their destination by authored map node, not by the
# race of the champion travelling there. A Shin'hare can reach Cave-In, for
# example, but that node must still load the Dwarf Cave-In panorama.
_AZ1_PANORAMA_BUNDLE_MAP = {
    "Node017": ("adventurezone01/p_dwrf_cavein", "p_dwrf_cavein"),
    "Node034": ("adventurezone01/p_cytl_amblingmesa", "p_cytl_amblingmesa"),
    "Node049": ("adventurezone01/p_cytl_thunderfield", "p_cytl_thunderfield"),
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
    return _CRAYBURN_RACE_SCENE_IDS.get(race_name, ())


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
        # A dungeon. The background/nodes prefabs are the client's own
        # (Resources.Load), found from the zone data; the AssetBundle and
        # LevelPrefab are deliberately left empty so OnStartUp_GetSummary
        # skips the async bundle load (UICampaignZoneVMBase.cs:635 -> 642
        # else) and calls OnPrefabLoaded() directly - a p_* panorama level
        # prefab is not a dungeon scene and its load NREs in
        # UIDungeonZoneViewModel.
        from campaign_runtime import catalog_data
        zone = next((z for z in catalog_data.zones()
                     if z.get("kind") == "dungeon" and z.get("prefab_root") and
                     z.get("server_template") == template_name), None)
        root = (zone or {}).get("prefab_root") or "campaign/az01/crayburncastle"
        background_prefab = f"{root}/prefabs/background"
        nodes_prefab = f"{root}/prefabs/nodes"
        campaign_template_id = ((zone or {}).get("campaign_template_id")
                                or "5bcba43a-95c7-44b4-ba09-a3555a5edf05")
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


def _push_fra_buff_conversation(handler, conversation_guid, service_mail_uid):
    """Queue an authored FRA reward conversation in the Arena client."""
    body = encode_objfmt_response(
        ["Game.Shared.Network.Campaign.BuffConversationEventArgs",
         "Game.Shared.ResourceId", "System.Guid"],
        [("ConversationID", "struct", ("Game.Shared.ResourceId", [
            ("m_Guid", "guid", str(conversation_guid)),
        ]))],
    )
    body = compress_gzip(body)
    data = encode_datawrapper(
        0, 10047, body, 1, "00000000-0000-0000-0000-000000000000")
    reqid = int(getattr(handler, "scnt", 0) or 0)
    issuer = (f"0.0.0.0.ServiceCampaign.{service_mail_uid}."
              f"ServicePlayer.{handler.client_uid}.{reqid}")
    handler.scnt = reqid + 1
    handler.send({
        "issuer": issuer, "target": "ServiceCampaign", "instance": "Shared",
        "reqid": 0, "c": 1, "conh": 0, "sid": handler.sid,
    }, data)
    return f"    Sent FRA reward conversation {conversation_guid}"


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
    # A champion whose last campaign is run from the rules goes back there.
    last_campaign = pve_db.db_champion_last_campaign_id(champ_id, conn=db)
    zone_id = _rules_zone(db, last_campaign) if last_campaign else None
    if zone_id:
        from campaign_runtime import runtime
        return runtime.handle_query(handler, db, zone_id,
                                    dict(env_json, CampID=last_campaign), comp,
                                    session_id, reqid, target, instance, conh, uid)
    # A champion can retain its completed Panorama after accepting the
    # Crayburn quest.  Prefer an active dungeon on reconnect; otherwise this
    # query always recreated/selected Panorama and silently took the player
    # out of the dungeon.
    dungeon = _get_existing_campaign_for_champion(db, champ_id, "DUNGEON")
    d_state = dungeon[4] if dungeon else None
    if dungeon:
        d_cid, d_inst_id, d_inst_hi, d_started, d_state = dungeon
        if d_state and (d_started or d_state.get("Started")) \
                and not d_state.get("Finished"):
            original = json.dumps(d_state, sort_keys=True)
            d_state = _prepare_dungeon_state(
                d_state, _race_name_for_campaign(db, d_cid))
            if json.dumps(d_state, sort_keys=True) != original:
                _save_campaign_state(db, d_cid, d_state)
                db.commit()
            resp = _build_input_response(d_cid, d_state, success=True)
            return _send_response(handler, json.dumps(resp), comp, session_id,
                                  reqid, target, instance, conh, uid)

    # Authored AZ1 panoramas are the active campaign while the player is in
    # Cave-In/Ambling Mesa/etc.  The AREA row remains persisted underneath so
    # return_parent can resume it, but reconnect must follow LastCampaignID
    # back into the panorama rather than reopening the map.
    # Repair saves created before the post-dungeon report marker was added.
    # Such saves have a completed Crayburn dungeon and an active next journal
    # objective, but the panorama would otherwise be treated as the old quest
    # giver state when the player reconnects.
    if (dungeon and d_state and d_state.get("Finished") and
            d_state.get("FinishReason") == "Complete"):
        pending_quest = _get_existing_campaign_for_champion(db, champ_id, "QUEST")
        if pending_quest:
            q_cid, _qi, _qh, _qs, q_state = pending_quest
            q_template = pve_db.db_campaign_template_name(q_cid, conn=db)
            q_metadata = _quest_template(db, q_template) if q_template else None
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
                        pve_db.db_campaign_update_state(
                            p_cid, json.dumps(p_state), conn=db, started=True)
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
        q_template = pve_db.db_campaign_template_name(q_cid, conn=db)
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
            pending_quest_progress = _consume_pending_quest_progress(db, champ_id)
            resp = _build_input_response(a_cid, a_state, success=True)
            ret = _send_response(handler, json.dumps(resp), comp, session_id,
                                 reqid, target, instance, conh, uid)
            if pending_quest_progress:
                push_campupdate(
                    handler, db, pending_quest_progress.get("CampID") or 0,
                    pending_quest_progress.get("ChampID") or champ_id,
                    "quest_progress", "QUEST", False, pending_quest_progress,
                    comp, session_id, target, instance, conh, uid)
            return ret

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

    # Preserve the exact protocol fallback order while keeping SQL in pve_db.
    rows = pve_db.db_active_campaign_candidates(
        champ_id, ctype, template, conn=db)
    templates = [_build_template_info(row[0], row[1], row[2] or "AREA", row[3] or "AZ1") for row in rows]
    return _send_response(handler, json.dumps(templates), comp, session_id,
                          reqid, target, instance, conh, uid)


def _initial_rules_dungeon_state(camp_id, champ_id, zone_id):
    """Minimal client state; campaign_runtime projects the authored map."""
    return {
        "CampID": camp_id, "ChampID": champ_id,
        "TempType": "DUNGEON", "RulesZone": zone_id,
        "PayGroups": [], "CSlide": None, "ALoc": None,
        "VisLocs": [], "LocNodes": [], "Encounters": [],
        "Champions": [], "CurState": "EXPLORE", "LastNode": "",
        "PublicState": {"Data": {"CampaignGroup": "DUNGEON"}},
        "Started": None, "Finished": None, "FinishReason": None,
        "Wins": 0, "Losses": 0, "Score": 0,
        "HealthAdj": 0, "DungeonLifeAdj": 0, "Flags": {},
    }


def _handle_createcamp(handler, db, env_json, comp, session_id,
                        reqid, target, instance, conh, uid):
    """CreateCamp: create a new campaign instance."""
    champ_id = env_json.get("ChampID", 0)
    requested_name = str(env_json.get("Template") or "Crayburn Castle").strip()
    template_name = requested_name
    from campaign_runtime import catalog_data, rules_loader

    # camp.Dungeon passes its name through GetActiveStatus/CreateCamp. Resolve
    # it against the authored zone catalogue so each named dungeon gets its own
    # template and rules runtime instead of silently reusing Castle Crayburn.
    dungeon_zone = catalog_data.resolve_zone_name(requested_name, kind="dungeon")
    if dungeon_zone is None and _is_known_dungeon(requested_name):
        # Keep the old forgiving spelling behaviour for Castle Crayburn.
        dungeon_zone = catalog_data.resolve_zone_name(
            "Castle Crayburn", kind="dungeon")
    if dungeon_zone:
        campaign_type = "DUNGEON"
        template_name = dungeon_zone.get("server_template") or dungeon_zone["title"]
        try:
            runtime_available = rules_loader.runtime_enabled(dungeon_zone["id"])
        except Exception as exc:
            runtime_available = False
            print(f"[campaign] dungeon runtime unavailable for "
                  f"{dungeon_zone['id']}: {exc}")
        if not runtime_available:
            resp = _build_input_response(0, None, success=False)
            resp["Errors"] = [
                f"Dungeon {dungeon_zone.get('title') or template_name!r} "
                "does not have a server campaign runtime"
            ]
            return _send_response(handler, json.dumps(resp), comp, session_id,
                                  reqid, target, instance, conh, uid)
    elif "panorama" in requested_name.lower():
        campaign_type = "PANORAMA"
    else:
        campaign_type = "PANORAMA"

    champ = _get_champion(db, champ_id)
    if not champ:
        resp = _build_input_response(0, None, success=False)
        resp["Errors"] = ["Champion not found"]
        return _send_response(handler, json.dumps(resp), comp, session_id,
                              reqid, target, instance, conh, uid)

    if dungeon_zone:
        # Dungeons are separate campaign templates. A type-only lookup here
        # returned the champion's existing Crayburn row for every debug name.
        from campaign_runtime import progress

        with progress.champion_lock(champ_id):
            row = pve_db.db_campaign_for_champion_template(
                champ_id, template_name, conn=db)
            if row:
                cid, inst_id, is_started, state_json, stored_type = row
                try:
                    state = json.loads(state_json) if state_json else {}
                except (TypeError, ValueError):
                    state = {}
                old_public = state.get("PublicState") if isinstance(state, dict) else None
                old_data = (old_public.get("Data")
                            if isinstance(old_public, dict) else None)
                old_group = (old_data.get("CampaignGroup")
                             if isinstance(old_data, dict) else None)
                if (str(stored_type or "").upper() != "DUNGEON" or not state or
                        not isinstance(state, dict) or
                        str(state.get("TempType") or "").upper() not in
                        ("", "DUNGEON") or
                        old_group not in (None, "DUNGEON") or
                        state.get("RulesZone") not in (None, dungeon_zone["id"])):
                    state = _initial_rules_dungeon_state(
                        cid, champ_id, dungeon_zone["id"])
                else:
                    state["CampID"] = cid
                    state["ChampID"] = champ_id
                    state["TempType"] = "DUNGEON"
                    state["RulesZone"] = dungeon_zone["id"]
                    public = state.get("PublicState")
                    if not isinstance(public, dict):
                        public = {}
                    data = public.get("Data")
                    if not isinstance(data, dict):
                        data = {}
                    data.setdefault("CampaignGroup", "DUNGEON")
                    public["Data"] = data
                    state["PublicState"] = public
                pve_db.db_campaign_set_zone(
                    cid, "DUNGEON", template_name, json.dumps(state), conn=db,
                    is_started=is_started)
            else:
                cid = _new_camp_id(db)
                inst_id = _generate_inst_id()
                state = _initial_rules_dungeon_state(
                    cid, champ_id, dungeon_zone["id"])
                pve_db.db_create_campaign(
                    cid, inst_id, champ_id, champ[1], champ[3], template_name,
                    "DUNGEON", json.dumps(state), db)
            pve_db.db_campaign_set_last_campaign(champ_id, cid, db)
            if dungeon_zone["id"] == "crayburn":
                # Preserve Crayburn's existing journal setup; other named
                # dungeons are launched directly for debugging.
                _ensure_quest_campaign(db, champ_id, "DUNGEON")
            db.commit()
    else:
        cid, inst_id, is_started, state = _find_campaign_for_champion(
            db, champ_id, campaign_type)
    if campaign_type == "DUNGEON" and not dungeon_zone:
        state = _prepare_dungeon_state(state, _race_name_for_campaign(db, cid))
    resp = _build_input_response(cid, state, success=True)
    return _send_response(handler, json.dumps(resp), comp, session_id,
                          reqid, target, instance, conh, uid)


def _rules_zone(db, camp_id):
    """The rules-driven zone a campaign row shows (``campaign_runtime``
    runs it), or None when the legacy code below still does."""
    from campaign_runtime import runtime
    try:
        return runtime.owns(db, camp_id)
    except Exception as exc:          # a broken rules file must not lock play
        print(f"[campaign] rules runtime unavailable for camp {camp_id}: {exc}")
        return None


def _handle_getcampstate(handler, db, env_json, comp, session_id,
                          reqid, target, instance, conh, uid):
    """QueryCampState: return full GameplayState for a campaign."""
    camp_id = env_json.get("CampID", 0)
    zone_id = _rules_zone(db, camp_id)
    if zone_id:
        from campaign_runtime import runtime
        return runtime.handle_query(handler, db, zone_id, env_json, comp,
                                    session_id, reqid, target, instance, conh, uid)
    row = pve_db.db_campaign_query_row(camp_id, conn=db)
    if not row:
        resp = _build_input_response(camp_id, None, success=False)
        resp["Errors"] = ["Campaign not found"]
        return _send_response(handler, json.dumps(resp), comp, session_id,
                              reqid, target, instance, conh, uid)

    champ_id, is_started, state_json, ctype, template_name = row
    pending_quest_progress = None
    if (ctype or "").upper() == "AREA":
        pending_quest_progress = _consume_pending_quest_progress(db, champ_id)
    if state_json:
        state = json.loads(state_json)
    else:
        state = _build_initial_gameplay_state(camp_id, champ_id, ctype or "AREA")
    if (ctype or "").upper() == "PANORAMA":
        champ = _get_champion(db, champ_id)
        cfg = _az0_config(champ[2]) if champ else None
        if cfg and _normalize_starter_panorama_state(state, cfg):
            _save_campaign_state(db, camp_id, state)
            db.commit()
    if (ctype or "").upper() == "DUNGEON":
        before = json.dumps(state, sort_keys=True)
        state = _prepare_dungeon_state(state, _race_name_for_campaign(db, camp_id))
        if json.dumps(state, sort_keys=True) != before:
            _save_campaign_state(db, camp_id, state)
            db.commit()
    log_campaign_state_diagnostic(handler, camp_id, champ_id, state)
    resp = _build_input_response(camp_id, state, success=True)
    ret = _send_response(handler, json.dumps(resp), comp, session_id,
                         reqid, target, instance, conh, uid)
    if pending_quest_progress:
        push_campupdate(
            handler, db, pending_quest_progress.get("CampID") or 0,
            pending_quest_progress.get("ChampID") or champ_id,
            "quest_progress", "QUEST", False, pending_quest_progress,
            comp, session_id, target, instance, conh, uid)
    return ret


def _handle_startcamp(handler, db, env_json, comp, session_id,
                       reqid, target, instance, conh, uid):
    """StartCamp: mark a campaign as started, return updated state."""
    camp_id = env_json.get("CampID", 0)
    zone_id = _rules_zone(db, camp_id)
    if zone_id:
        from campaign_runtime import runtime
        return runtime.handle_query(handler, db, zone_id, env_json, comp,
                                    session_id, reqid, target, instance, conh,
                                    uid, start=True)
    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
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

    pve_db.db_campaign_update_state(
        camp_id, json.dumps(state), conn=db, started=True)
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
        row = pve_db.db_campaign_summary_row(cid, conn=db)
        if row:
            summaries.append(_build_camp_summary(
                cid, row[0], row[1] or "DUNGEON",
                row[2] or "Crayburn Castle", row[3]))

    return _send_response(handler, json.dumps(summaries), comp, session_id,
                          reqid, target, instance, conh, uid)


def _handle_sendevent(handler, db, env_json, comp, session_id,
                       reqid, target, instance, conh, uid):
    """SendEvent: handle events sent to the campaign logic engine."""
    camp_id = env_json.get("CampID", 0)
    zone_id = _rules_zone(db, camp_id)
    if zone_id:
        from campaign_runtime import runtime
        return runtime.handle_sendevent(handler, db, zone_id, env_json, comp,
                                        session_id, reqid, target, instance,
                                        conh, uid)
    event_name = env_json.get("Event", "")
    o_params = env_json.get("OParms", [])

    log = getattr(handler, "_log_req", print)
    log(f"    Campaign SendEvent: camp={camp_id} event={event_name} params={o_params}")

    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
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
    if cfg is None:
        raise RuntimeError("AZ0 campaign configuration is unavailable")

    intro_npc = cfg["intro_npc"]
    trainer_npc = cfg["trainer_npc"]
    quest_npc = cfg["quest_npc"]
    training_node = cfg["training_node"]

    def _append_location(loc):
        node = loc.get("Data", {}).get("node")
        if not any(l.get("Data", {}).get("node") == node
                   for l in state.setdefault("VisLocs", [])):
            state["VisLocs"].append(loc)

    _note_conversation_choice(state, event_name)
    if event_name and event_name not in _CAMPAIGN_PROTOCOL_EVENTS:
        conversation_guid = _active_conversation_guid(state, ctype)
        if conversation_guid:
            _record_dungeon_script_score(state, template_name, event_name)
            try:
                with _conversation_event_lock:
                    conversation_event(conversation_guid, str(event_name))
            except Exception as exc:
                log("    Campaign conversation_event failed: "
                    f"conversation={conversation_guid} event={event_name!r} "
                    f"error={exc}")

    pending_dungeon = None
    pending_area = None
    pending_quest_progress = []
    conversation_applied = _empty_applied_updates()
    conversation_ending = ""

    # Conversation rewards are delivered in the same AppliedUpdates payload
    # as battle/quest loot. Resolve the active map location before the normal
    # conv_done state transition clears ALoc, and advance any matching journal
    # objective while the conversation GUID is still available.
    if (event_name == "conv_done" and
            (ctype or "").upper() == "AREA"):
        active_node = state.get("ALoc")
        active_data = next(
            ((loc.get("Data") or {}) for loc in state.get("VisLocs", [])
             if active_node in ((loc.get("Data") or {}).get("node"),
                                (loc.get("Data") or {}).get("name"),
                                (loc.get("Data") or {}).get("title"))),
            {},
        )
        active_conversation = active_data.get("conversationId")
        conversation_ending = _record_conversation_flags(
            db, champ_id, state, ctype, active_conversation)
        conversation_applied = _apply_conversation_rewards(
            handler, db, camp_id, state, active_conversation)
        _merge_applied(conversation_applied, _spend_conversation_gold(
            db, champ_id, active_conversation, conversation_ending,
            _ending_holds(db, champ_id, state)))
        pending_quest_progress = _advance_quest_conversation_objectives(
            db, champ_id, active_conversation)
        _grant_finished_quest_rewards(handler, db, camp_id, state,
                                      pending_quest_progress,
                                      conversation_applied)

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
        if (ctype or "").upper() == "DUNGEON":
            state = _prepare_dungeon_state(
                state, _race_name_for_campaign(db, camp_id))
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        paths = pdata.setdefault("visited_paths", [])
        values = (o_params[0] if o_params and
                  isinstance(o_params[0], list) else o_params)
        if not isinstance(values, (list, tuple)):
            values = [values]
        new_values = []
        for path in values:
            path = str(path)
            if path and path not in paths:
                paths.append(path)
                new_values.append(path)
    elif (event_name == "conv_done" and
          (ctype or "").upper() == "DUNGEON"):
        # Server-driven Crayburn dungeon: ordinary conversations and authored
        # victory conversations advance the chain.  A defeat conversation is
        # different: it is the result of a lost encounter and must restore the
        # same encounter as retryable.  Do not infer victory merely because a
        # conversation closed.
        current = _resolve_node(
            state, state.get("ALoc") or state.get("LastNode") or "Entrance")
        current_data = next(
            ((loc.get("Data") or {}) for loc in state.get("VisLocs", [])
             if current in ((loc.get("Data") or {}).get("node"),
                            (loc.get("Data") or {}).get("name"))),
            {},
        )
        node_data = _crayburn_node_data(
            _race_name_for_campaign(db, camp_id), current)
        active_conversation = current_data.get("conversationId")
        defeat_conversation = node_data.get("fail")
        pending_success = (
            state.get("_pending_encounter_success") == current)
        active_conversation_node = (
            current_data.get("type") == "Convo" and
            bool(active_conversation))

        if active_conversation_node and active_conversation == defeat_conversation:
            # The defeat dialogue is acknowledgement of a loss, not a node
            # completion.  Restore the authored encounter so a later StartLoc
            # can retry it, and clear ALoc so the client returns to the map
            # instead of immediately reopening either conversation or battle.
            return_node = _crayburn_return_node(state, current)
            current_data.update({
                "type": "Encounter",
                "conversationId": None,
                "encounter": _crayburn_scene_for_node(
                    _race_name_for_campaign(db, camp_id), current),
                "completed": False,
                "enabled": True,
                "visible": True,
                "autostart": False,
            })
            state.pop("_pending_encounter_success", None)
            state["ALoc"] = ""
            state["LastNode"] = return_node
            state["CurState"] = "EXPLORE"
            _reveal_crayburn_node(state, return_node)
            _save_campaign_state(db, camp_id, state)
            db.commit()
        elif active_conversation_node or pending_success:
            # Ordinary dungeon conversations and the explicitly pending
            # victory conversation are the only conversation completions that
            # advance the Crayburn chain.
            advance_crayburn_step(handler, db, camp_id, True, comp, session_id,
                                  target, instance, conh, uid,
                                  auto_activate=False)
        else:
            # Ignore a stale/duplicate conv_done after a defeat conversation
            # has already restored the encounter.  In particular, do not use
            # LastNode as evidence that an encounter was won.
            state["CurState"] = "EXPLORE"
            _save_campaign_state(db, camp_id, state)
            db.commit()
        # advance_crayburn_step already persisted the updated state; reload
        # it so we return the post-advance state, not the stale pre-advance
        # copy that would otherwise be saved back and undo the advance.
        saved_state = pve_db.db_campaign_state(camp_id, conn=db)
        state = json.loads(saved_state) if saved_state else state
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
                user_id = pve_db.db_campaign_user_id(champ_id, conn=db)
                granted = _grant_card_reward(
                    handler, db, user_id, choice)
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
                _save_campaign_state(db, camp_id, state)
                db.commit()
            resp = _build_input_response(camp_id, state, success=True)
            return _send_response(handler, json.dumps(resp), comp, session_id,
                                  reqid, target, instance, conh, uid)
        current = state.get("ALoc")
        # After Crayburn completion the report conversation is shown in the
        # race panorama.  Its completion must not restart the castle; instead
        # expose the original race NPC with the authored Feralroot travel
        # conversation and finish the linked journal objective.
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
            pve_db.db_promote_campaign_to_dungeon(
                dungeon_id, json.dumps(dungeon_state), conn=db)
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
        saved_state = pve_db.db_campaign_state(camp_id, conn=db)
        if saved_state:
            try:
                state = json.loads(saved_state)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

    resp = _build_input_response(camp_id, state, success=True,
                                 applied=conversation_applied)
    _save_campaign_state(db, camp_id, state)
    db.commit()
    ret = _send_response(handler, json.dumps(resp), comp, session_id,
                         reqid, target, instance, conh, uid)
    for quest_state in pending_quest_progress:
        push_campupdate(
            handler, db, quest_state.get("CampID") or 0,
            quest_state.get("ChampID") or champ_id, "quest_progress", "QUEST",
            False, quest_state, comp, session_id, target, instance, conh, uid)
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
    rows = pve_db.db_campaign_state_rows(
        champion_id, "QUEST", conn=db, template_name="az01_tamed")
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
            _save_campaign_state(db, quest_id, state)
    db.commit()


def _apply_gameend(db, camp_id, won):
    """Apply a campaign game-end result to the DB, return (camp_id, state).

    On a win of the training encounter, reveals the quest-giver NPC on the
    panorama. Returns (camp_id, GameplayState) or (None, None) if not found.
    """
    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
    if not row:
        return None, None

    champ_id, state_json, ctype, template_name = row
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
    if cfg is None:
        raise RuntimeError("AZ0 campaign configuration is unavailable")

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
        if is_dungeon:
            # Consecutive dungeon wins.  The "if you won the last encounter in
            # this dungeon" PreGame talents read this streak rather than the
            # cumulative ``Wins`` counter, which also counts non-dungeon
            # battles and therefore fired on the first dungeon encounter.
            state["dungeon_win_count"] = int(
                state.get("dungeon_win_count", 0) or 0) + 1
        # The training battle has been won: stop treating the campaign as the
        # tutorial so later battles randomize the turn player instead of always
        # giving the player first turn.
        state["TutorialDone"] = True
    else:
        state["Losses"] = state.get("Losses", 0) + 1
        if is_dungeon:
            # A loss breaks the dungeon streak; the next encounter must not
            # grant the previous-win PreGame bonuses.
            state["dungeon_win_count"] = 0
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
    outcome_pending = False
    if (ctype or "").upper() == "AREA":
        active_scene = str(state.get("ActiveEncounterGuid") or "")
        active_node = state.get("ALoc")
        condition_met = bool(state.pop("_last_encounter_condition_met", False))
        quest_pending = _quest_encounter_is_pending(db, champ_id, active_scene)
        # Tamed encounters remain available after an uncaptured win even if
        # the journal has not reached (or has not yet recorded) that specific
        # objective. The authored condition identifies these scenes without
        # naming individual creatures; a captured win closes the node and
        # advances the matching quest objective when one is active.
        tamed_scene = any(
            record.get("when") or str(
                (record.get("end_of_game_condition") or {}).get("type") or ""
            ).strip().lower() == "void_tamed_troop"
            for record in _scene_reward_records(db, active_scene)
        )
        # An authored conditional encounter remains retryable only after a
        # win that did not satisfy its condition.  A loss or concede leaves
        # the encounter incomplete, so it cannot use the retryable map state
        # that exposes outgoing paths.
        retryable = bool(won and (quest_pending or tamed_scene) and
                         not condition_met)
        matched_index = None
        for idx, loc in enumerate(state.get("VisLocs", [])):
            data = loc.get("Data") or {}
            if ((active_scene and str(data.get("encounter") or "") == active_scene)
                    or (active_node and active_node in (
                        data.get("node"), data.get("name"), data.get("title")))):
                data["completed"] = bool(won and not retryable)
                data["repeatable"] = bool(retryable)
                data.pop("action_encounter", None)    # its result is handled
                data["enabled"] = True
                data["visible"] = True
                # A result has been handled.  Even a retryable defeat must
                # return to the map without immediately launching the same
                # encounter again; deliberate re-entry can set this again.
                data["autostart"] = False
                matched_index = idx
                node_name = data.get("node") or data.get("name")
                if node_name:
                    state["LastNode"] = node_name
                    visited = state.setdefault("PublicState", {}).setdefault(
                        "Data", {}).setdefault("visited_nodes", [])
                    if node_name not in visited:
                        visited.append(node_name)
                    pdata = state.setdefault("PublicState", {}).setdefault(
                        "Data", {})
                    failed_nodes = [str(value) for value in
                                    (pdata.get("failed_nodes") or []) if value]
                    if not won and node_name not in failed_nodes:
                        failed_nodes.append(node_name)
                    elif won:
                        failed_nodes = [value for value in failed_nodes
                                        if value != str(node_name)]
                    pdata["failed_nodes"] = failed_nodes
                break
        if retryable:
            _mark_quest_objective_retryable(db, champ_id, active_scene)
        # A successful completion can expose the next authored location. A
        # loss leaves the player at the failed node; its unvisited neighbours
        # stay hidden until a retry succeeds.
        if won and matched_index is not None:
            for loc in state.get("VisLocs", [])[matched_index + 1:]:
                data = loc.get("Data") or {}
                if not data.get("completed"):
                    data["visible"] = True
                    data["enabled"] = True
                    break
        state.pop("ActiveEncounterGuid", None)
        state.pop("ActiveEncounterConversationGuid", None)

    # Clear ALoc so the panorama doesn't auto-pop the encounter dialog when
    # the client returns from the battle. Use an empty string (not None) — the
    # client's ProcessStateChange won't match it to any node GameObject, and
    # portrait rendering resolves correctly because it's not a null ALoc.
    if not is_dungeon and not outcome_pending:
        state["ALoc"] = ""

    _save_campaign_state(db, camp_id, state)
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
    rewards_json = pve_db.db_encounter_scene_rewards(scene_guid, db)
    if not rewards_json:
        return []
    try:
        data = json.loads(rewards_json)
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
    rows = pve_db.db_void_tamed_troop_rows(
        session_id, player_user_id, owner, db)
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
    template = db_reward_card_template(template_guid, conn=db)
    if not template:
        return []
    granted = []
    for _ in range(quantity):
        instance_id = db_grant_card_instance(
            user_id, template_guid, conn=db)
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
    if not conversation_guid:
        return _empty_applied_updates()
    row = pve_db.db_conversation_reward(conversation_guid, conn=db)
    if not row or not row[2]:
        return _empty_applied_updates()
    reward_raw, one_time, _enabled = row
    try:
        reward = json.loads(reward_raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        reward = {}
    return _apply_reward_dict(
        handler, db, camp_id, state, reward if isinstance(reward, dict) else {},
        bool(one_time), str(conversation_guid),
        f"conversation={conversation_guid}")


def _apply_quest_rewards(handler, db, camp_id, state, quest_script):
    """Grant a quest's authored completion reward (``quest_templates.rewards_json``).

    Called where a quest is marked finished; one-time per champion, keyed
    ``quest:<script>`` in the same claim table conversations use.
    """
    row = pve_db.db_quest_template_reward(quest_script, conn=db)
    if not row or not row[1]:
        return _empty_applied_updates()
    try:
        reward = json.loads(row[0] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        reward = {}
    return _apply_reward_dict(
        handler, db, camp_id, state, reward if isinstance(reward, dict) else {},
        True, f"quest:{quest_script}", f"quest={quest_script}")


def _merge_applied(base, extra):
    """Append ``extra``'s AppliedUpdates lists onto ``base`` (in place)."""
    for key, values in (extra or {}).items():
        if isinstance(values, list):
            base.setdefault(key, []).extend(values)
    return base


def _stardust_item_specs(reward):
    """Item specs for a reward's ``stardust`` entries (rarity/amount/probability).

    Each entry ``{"rarity": "common", "amount": 25, "probability": 50}`` is
    rolled independently; ``probability`` is a percentage (default 100).  The
    returned specs carry ``stardust`` so the grant also updates the ledger.
    """
    from db import STARDUST_TEMPLATES
    specs = []
    for entry in (reward or {}).get("stardust") or ():
        if not isinstance(entry, dict):
            continue
        guid = STARDUST_TEMPLATES.get(str(entry.get("rarity") or "").lower())
        try:
            amount = int(entry.get("amount", 0) or 0)
            probability = float(entry.get("probability", 100))
        except (TypeError, ValueError):
            continue
        if guid and amount > 0 and random.random() * 100 < probability:
            specs.append({"guid": guid, "quantity": amount,
                          "stardust": str(entry["rarity"]).lower()})
    return specs


def _grant_finished_quest_rewards(handler, db, camp_id, state, quest_states,
                                  applied):
    """Grant ``rewards_json`` for each quest a conversation just finished.

    ``quest_states`` are the QUEST campaign states returned by
    ``_advance_quest_conversation_objectives``; ``camp_id``/``state`` are the
    campaign the conversation ran in (it keeps the one-time claims).
    """
    for quest_state in quest_states or ():
        if not isinstance(quest_state, dict) or not quest_state.get("Finished"):
            continue
        script = pve_db.db_campaign_template_name(
            quest_state.get("CampID"), conn=db)
        if script:
            _merge_applied(applied, _apply_quest_rewards(
                handler, db, camp_id, state, script))
    return applied


def _apply_reward_dict(handler, db, camp_id, state, reward, one_time,
                       claim_name, label):
    """Grant ``reward`` (gold/xp/cards/items/chest) to the campaign's champion."""
    applied = _empty_applied_updates()
    if not reward:
        return applied
    claims = state.setdefault("_conversation_reward_claims", {})
    if not isinstance(claims, dict):
        claims = {}
        state["_conversation_reward_claims"] = claims
    champ_id = int(state.get("ChampID") or 0)
    champ = _get_champion(db, champ_id)
    if not champ:
        return applied
    user_id = champ[1]
    race_name = _RACE_NAMES.get(champ[2], "")
    # A campaign template can be replayed by several champions.  One-time
    # conversation rewards therefore belong to the champion, not the shared
    # conversation/campaign template.  Accept the legacy unscoped key so a
    # reward already claimed by an older server build is not duplicated.
    claim_key = f"{champ_id}:{claim_name}"
    if bool(one_time) and (claims.get(claim_key) or claims.get(str(claim_name))):
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
        new_gold, platinum = db_adjust_user_currency(
            user_id, gold_delta=gold, conn=db)
        applied["Accounts"].append({"Account": {
            "Gold": new_gold, "Platinum": platinum,
        }})
        applied["Completed"].append({
            "ItemKind": "GOLD", "ItemQuantity": gold,
            "ItemAction": "GRANT", "ItemTemplate": "", "RCode": "GOLD",
        })

    if xp:
        crow = db_champion_reward_profile(champ_id, conn=db)
        if crow:
            new_xp = int(crow[3] or 0) + xp
            thresholds = (0, 1000, 2800, 5000, 8000, 12000, 17500,
                          25000, 40000, 50000, 60000, 70000, 80000,
                          92000, 110000)
            new_level = max(1, min(len(thresholds),
                                   1 + sum(new_xp >= n for n in thresholds[1:])))
            db_update_champion_xp(champ_id, new_xp, new_level, conn=db)
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
    # A single authored conversation can serve all eight race/faction
    # variants.  Keep that mapping in the reward seed and resolve it from the
    # champion's race at claim time rather than duplicating conversation rows.
    race_card_guid = (reward.get("card_guid_by_race") or {}).get(race_name)
    if race_card_guid:
        card_specs = list(card_specs) + [{
            "guid": race_card_guid, "quantity": reward.get("quantity", 1),
        }]
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
                "SocketedGems": EGemTypesNew.Unknown, "IsNotTradeable": False,
                "EscrowStatus": "Clean",
            }})
            applied["Completed"].append({
                "ItemKind": "BOACARD", "ItemQuantity": 1,
                "ItemAction": "GRANT", "ItemTemplate": card["guid"],
                "RCode": "CARD",
            })

    # Equipment and other account-bound inventory rewards are represented by
    # InventoryItemData GUIDs, not CardTemplate GUIDs.  Persist them in the
    # shared player_inventory table and include the resulting quantity in the
    # campaign Applied.Items payload so the client collection updates without
    # requiring a reconnect.
    item_specs = reward.get("items") or []
    race_item_guid = (reward.get("item_guid_by_race") or {}).get(race_name)
    if race_item_guid:
        item_specs = list(item_specs) + [{
            "guid": race_item_guid, "quantity": reward.get("quantity", 1),
        }]
    if reward.get("item_guid"):
        item_specs = list(item_specs) + [{
            "guid": reward.get("item_guid"),
            "quantity": reward.get("quantity", 1),
        }]
    if isinstance(item_specs, dict):
        item_specs = [item_specs]
    item_specs = list(item_specs) + _stardust_item_specs(reward)
    for spec in item_specs:
        if isinstance(spec, str):
            spec = {"guid": spec}
        if not isinstance(spec, dict):
            continue
        template_guid = (spec.get("guid") or spec.get("template") or
                         spec.get("item_guid"))
        if not template_guid:
            continue
        try:
            quantity = max(1, int(spec.get("quantity", 1) or 1))
        except (TypeError, ValueError):
            quantity = 1
        if spec.get("stardust"):
            db_add_stardust(user_id, spec["stardust"], quantity, conn=db)
        existing = db_inventory_item(user_id, str(template_guid), conn=db)
        if existing:
            item_row_id, old_quantity, item_uid = existing
            new_quantity = int(old_quantity or 0) + quantity
            if not item_uid:
                item_uid = db_next_inventory_client_uid(user_id, conn=db)
            db_upsert_inventory_item(
                user_id, str(template_guid), quantity,
                client_item_uid=item_uid, conn=db)
        else:
            item_uid = db_next_inventory_client_uid(user_id, conn=db)
            new_quantity = quantity
            db_upsert_inventory_item(
                user_id, str(template_guid), new_quantity,
                client_item_uid=item_uid, conn=db)
        applied["Items"].append({"Item": {
            "Id": item_uid,
            "TemplateID": str(template_guid),
            "BoundToProfile": True,
            "ItemQuantity": new_quantity,
            "ClaimDate": "0001-01-01T00:00:00",
            "EscrowStatus": "Clean",
        }})
        applied["Completed"].append({
            "ItemKind": "BOAITEM", "ItemQuantity": quantity,
            "ItemAction": "GRANT", "ItemTemplate": str(template_guid),
            "RCode": "ITEM",
        })

    # Promotional Crayburn rewards are real treasure chests, not merely a
    # display-only loot entry.  Persist the chest so it survives reconnects,
    # and return inventory_bits in Applied.Items so the campaign client adds
    # it immediately to InventoryChests and the pack-opening screen.
    chest_guid = (reward.get("chest_guid") or reward.get("chest_template")
                  or reward.get("pack_guid")
                  or (reward.get("chest_guid_by_race") or {}).get(race_name))
    if chest_guid:
        chest_guid = str(chest_guid)
        chest_id = db_create_treasure_chest(
            user_id, "00000000-0000-0000-0000-000000000000", "Promo", db,
            template_guid=chest_guid)
        if chest_id is None:
            raise RuntimeError("Could not persist the campaign reward chest")
        chest_id = int(chest_id)
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
    _save_campaign_state(db, camp_id, state)
    db.commit()
    getattr(handler, "_log_req", print)(
        f"    Reward: {label} "
        f"gold={gold} xp={xp} cards={len(applied['Cards'])} "
        f"items={len(applied['Items'])}")
    return applied


def _scene_card_choices(db, scene_guid):
    """Return card GUIDs offered by a scene's authored card_choice reward."""
    if not scene_guid:
        return []
    rewards_json = pve_db.db_encounter_scene_rewards(scene_guid, db)
    if not rewards_json:
        return []
    try:
        rewards = json.loads(rewards_json)
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
                rows = [(row[0], row[1])
                        for row in pve_db.db_az1_scene_rows(conn=db)]
                for guid, name in rows:
                    if re.search(r"NODE[_ ]?0*%s\b" % int(match.group(1)),
                                 name or "", re.I):
                        return str(guid)
            break
    return None




_SCENE_ACTION = re.compile(r"^\s*(set_flag|clear_flag)\s*\(\s*([^()\s][^()]*?)\s*\)\s*$")
_SCENE_CHECK_TAMED = re.compile(r"^\s*check_?tamed\s*\(\s*\)\s*$", re.I)


def _run_scene_actions(db, state, scene_guid, trigger, battle=None):
    """Run a scene's ``pre`` / ``victory`` / ``defeat`` actions on ``state``.

    Actions are ``set_flag(X)`` / ``clear_flag(X)``; the flags live in the
    campaign's public data, where ``flag(X)`` tags and the rules read them.
    ``check_tamed()`` (victory; ``battle`` is ``(session_id, player_user_id)``)
    sets ``tamed:<first 8 of the scene GUID>`` when a tamed troop is in the
    opponent's void. Returns whether anything changed.
    """
    if not scene_guid:
        return False
    try:
        table = json.loads(pve_db.db_encounter_scene_actions(
            scene_guid, conn=db) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    data = state.setdefault("PublicState", {}).setdefault("Data", {})
    before = json.dumps(data, sort_keys=True)
    actions = list((table or {}).get(trigger) or ())
    # The card picked on the selector panel runs its own flag actions.
    option = _active_mod_choice_option(db, state, scene_guid)
    if option:
        actions += list(option.get(trigger) or ())
    for action in actions:
        if _SCENE_CHECK_TAMED.match(str(action)):
            if (trigger == "victory" and battle and _evaluate_encounter_condition(
                    db, battle[0], battle[1],
                    {"type": "void_tamed_troop", "owner": "opponent"})):
                data[f"tamed:{str(scene_guid).lower()[:8]}"] = True
            continue
        match = _SCENE_ACTION.match(str(action))
        if not match:
            continue
        kind, flag = match.group(1), match.group(2)
        if kind == "set_flag":
            data[flag] = True
        else:
            _clear_campaign_flags(data, [flag])
    return json.dumps(data, sort_keys=True) != before


# PublicState path the ModChoiceEncounter location's ``modlistId`` names:
# the client's card-selector panel lists these card GUIDs.
_MOD_CHOICE_LIST = "mod_choice_list"


def _scene_mod_options(db, scene_guid):
    """``campaign_rules.scene_mod_choices`` for a scene's ``mods_json``."""
    if not scene_guid:
        return []
    try:
        mods = json.loads(pvp_db.db_scene_mods(str(scene_guid), conn=db) or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return campaign_rules.scene_mod_choices(mods)


def _active_mod_choice_option(db, state, scene_guid):
    """The option the player chose for the active battle, if it is this scene's."""
    chosen = str(state.get("ActiveEncounterModChoice") or "").lower()
    if not chosen or not scene_guid:
        return None
    return next((o for o in _scene_mod_options(db, scene_guid)
                 if o["guid"] == chosen), None)


def _offer_mod_choice(db, champ_id, state, data):
    """Turn an encounter location into the client's card-selector battle.

    A scene with ``"choice": true`` mods offers the cards whose ``when``
    tags hold: the location becomes ``ModChoiceEncounter`` and its
    ``modlistId`` points at the offered GUIDs in PublicState (the client's
    UIDungeonZoneViewModel builds the selector from that list). With none
    on offer it stays a normal ``Encounter``.
    """
    pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
    options = _scene_mod_options(db, data.get("encounter"))
    offered = campaign_rules.offered_mod_choices(
        options, _ending_holds(db, champ_id, state)) if options else []
    if offered:
        data["type"] = "ModChoiceEncounter"
        data["modlistId"] = f"/{_MOD_CHOICE_LIST}"
        pdata[_MOD_CHOICE_LIST] = offered
        return True
    if data.get("type") == "ModChoiceEncounter":
        data["type"] = "Encounter"
    data.pop("modlistId", None)
    return False


def _chosen_mod_card(db, state, active_node, o_params):
    """The card the selector panel sent with ``start`` (``OParms[0]``),
    if it is one the active location offered; ``None`` otherwise."""
    location = next((
        (loc.get("Data") or {}) for loc in state.get("VisLocs") or ()
        if (loc.get("Data") or {}).get("node") == active_node or
        (loc.get("Data") or {}).get("name") == active_node), {})
    if location.get("type") != "ModChoiceEncounter":
        return None
    chosen = str((o_params or [""])[0] or "").strip().lower()
    offered = ((state.get("PublicState") or {}).get("Data") or {}).get(
        _MOD_CHOICE_LIST) or ()
    return chosen if chosen in offered else None


def _run_defeat_actions(db, session, camp_id):
    """A lost / conceded battle: run the scene's ``defeat`` actions and save."""
    row = pve_db.db_campaign_identity_state(camp_id, db)
    if not row:
        return
    try:
        state = json.loads(row[1] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return
    scene_guid = state.get("ActiveEncounterGuid") or _last_encounter_scene.get(
        session.session_name or "")
    if _run_scene_actions(db, state, scene_guid, "defeat"):
        _save_campaign_state(db, camp_id, state)
        db.commit()


def _battle_events(db, session, player_user_id):
    """Named events a finished battle produced (``tamed_troop`` ...)."""
    events = []
    if _evaluate_encounter_condition(
            db, session.session_id, player_user_id,
            {"type": "void_tamed_troop", "owner": "opponent"}):
        events.append("tamed_troop")
    return events


def _record_battle_flags(db, session, player_user_id, state, scene_guid):
    """Set the flags the AZ1 rules hooks attach to this battle's result.

    A hook ``{"on": {"event": "encounter_victory", "scene": S, "events":
    "tamed_troop"}, "do": [{"set_flag": F}]}`` sets ``F`` (stored as a
    boolean in the campaign's public data, where ``flag(F)`` tags read it)
    when scene ``S`` is won and the battle produced that event.
    """
    import campaign_gates
    rules = campaign_gates.rules_for("AZ1")
    if not rules or not scene_guid:
        return
    events = _battle_events(db, session, player_user_id)
    data = state.setdefault("PublicState", {}).setdefault("Data", {})
    events += [str(e) for e in data.pop("pending_battle_events", None) or ()]
    for hook in rules.get("hooks") or ():
        on = hook.get("on") or {}
        if on.get("event") != "encounter_victory":
            continue
        if str(on.get("scene") or "").lower() != str(scene_guid).lower():
            continue
        if on.get("events") and on["events"] not in events:
            continue
        for effect in hook.get("do") or ():
            if effect.get("set_flag"):
                data[effect["set_flag"]] = True


def _apply_encounter_end_rewards(handler, db, session, camp_id, won,
                                 rules_runtime=False):
    """Apply authored encounter rewards and return campaign AppliedUpdates.

    Campaign clients build their loot window from ``gameendnotify.Applied``;
    generic profile reward events are not sufficient here. In a rules-driven
    zone (``rules_runtime``) the engine has already run the scene's flag
    actions; its flags are mirrored into the state the ``when`` tags read.
    """
    result = {"applied": _empty_applied_updates(), "cards": [],
              "items": [], "chests": [], "gold": 0, "xp": 0,
              "condition_met": False, "scene_guid": None}
    if not won:
        if not rules_runtime:
            _run_defeat_actions(db, session, camp_id)
        return result
    row = pve_db.db_campaign_identity_state(camp_id, db)
    if not row:
        return result
    champion_id, state_json = row
    try:
        state = json.loads(state_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        state = {}
    scene_guid = state.get("ActiveEncounterGuid") or _last_encounter_scene.get(
        session.session_name or "")
    champ = _get_champion(db, champion_id)
    player_user_id = champ[1] if champ else None
    if not rules_runtime and _run_scene_actions(
            db, state, scene_guid, "victory",
            battle=(session.session_id, player_user_id)
            if player_user_id is not None else None):
        _save_campaign_state(db, camp_id, state)
        db.commit()
    records = _scene_reward_records(db, scene_guid)
    if not records:
        return result
    result["scene_guid"] = str(scene_guid) if scene_guid else None
    if player_user_id is None:
        return result
    claims = state.setdefault("_encounter_reward_claims", {})
    if not isinstance(claims, dict):
        claims = {}
        state["_encounter_reward_claims"] = claims
    success_counts = state.setdefault("_encounter_successes", {})
    if not isinstance(success_counts, dict):
        success_counts = {}
        state["_encounter_successes"] = success_counts
    try:
        prior_successes = int(success_counts.get(str(scene_guid), 0) or 0)
    except (TypeError, ValueError):
        prior_successes = 0
    # Older campaign states did not have _encounter_successes. Existing
    # reward claims still prove that this scene has already been won once.
    if not prior_successes:
        claim_prefix = f"{scene_guid}:"
        if any(str(key).startswith(claim_prefix) for key in claims):
            prior_successes = 1
    quest_pending = _quest_encounter_is_pending(
        db, champion_id, str(scene_guid) if scene_guid else None)
    retry_without_currency = bool(quest_pending and prior_successes)
    if not rules_runtime:
        _record_battle_flags(db, session, player_user_id, state, scene_guid)
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
        # ``when``: flag tags that must hold (set by the battle's events via
        # the rules hooks, see _record_battle_flags).
        when = record.get("when")
        if when:
            if not _conversation_tags_hold(db, champion_id, state, "", when):
                continue
            result["condition_met"] = True
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
        if retry_without_currency:
            gold = 0
            xp = 0
        # Tamed scenes have unconditional completion currency alongside a
        # conditional capture card. Only the card template is gated by
        # end_of_game_condition, so an uncaptured win can be retried without
        # awarding the captured card prematurely.
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
        chest_guid = (reward_obj.get("chest_guid") or
                      reward_obj.get("chest_template") or
                      reward_obj.get("pack_guid"))
        chest = None
        if chest_guid and (not condition or context):
            chest_guid = str(chest_guid)
            chest_id = db_create_treasure_chest(
                player_user_id,
                "00000000-0000-0000-0000-000000000000", "Promo", db,
                template_guid=chest_guid)
            if chest_id is None:
                raise RuntimeError("Could not persist the campaign reward chest")
            chest = {"id": int(chest_id),
                     "template": chest_guid}
        items = []
        item_specs = reward_obj.get("items") or []
        champ_race = _RACE_NAMES.get(champ[2], "") if champ else ""
        race_item_guid = (reward_obj.get("item_guid_by_race") or {}).get(champ_race)
        if race_item_guid:
            item_specs = list(item_specs) + [{
                "guid": race_item_guid, "quantity": reward_obj.get("quantity", 1),
            }]
        if reward_obj.get("item_guid"):
            item_specs = list(item_specs) + [{
                "guid": reward_obj.get("item_guid"),
                "quantity": reward_obj.get("quantity", 1),
            }]
        if isinstance(item_specs, dict):
            item_specs = [item_specs]
        if not condition or context:
            item_specs = list(item_specs) + _stardust_item_specs(reward_obj)
            for spec in item_specs:
                if isinstance(spec, str):
                    spec = {"guid": spec}
                if not isinstance(spec, dict):
                    continue
                template_guid = (spec.get("guid") or spec.get("template") or
                                 spec.get("item_guid"))
                if not template_guid:
                    continue
                try:
                    quantity = max(1, int(spec.get("quantity", 1) or 1))
                except (TypeError, ValueError):
                    quantity = 1
                if spec.get("stardust"):
                    db_add_stardust(player_user_id, spec["stardust"], quantity,
                                    conn=db)
                existing = db_inventory_item(
                    player_user_id, str(template_guid), conn=db)
                if existing:
                    _item_row_id, old_quantity, item_uid = existing
                    new_quantity = int(old_quantity or 0) + quantity
                    if not item_uid:
                        item_uid = db_next_inventory_client_uid(
                            player_user_id, conn=db)
                    db_upsert_inventory_item(
                        player_user_id, str(template_guid), quantity,
                        client_item_uid=item_uid, conn=db)
                else:
                    item_uid = db_next_inventory_client_uid(
                        player_user_id, conn=db)
                    new_quantity = quantity
                    db_upsert_inventory_item(
                        player_user_id, str(template_guid), new_quantity,
                        client_item_uid=item_uid, conn=db)
                items.append({"id": item_uid, "template": str(template_guid),
                              "quantity": new_quantity, "granted": quantity})
        if not cards and not gold and not xp and not chest and not items:
            continue
        granted.extend(cards)
        total_gold += gold
        total_xp += xp
        result["items"].extend(items)
        if chest:
            result["chests"].append(chest)
        claims[claim_key] = True

    success_counts[str(scene_guid)] = prior_successes + 1

    account = None
    if total_gold:
        new_gold, platinum = db_adjust_user_currency(
            player_user_id, gold_delta=total_gold, conn=db)
        account = {"Gold": new_gold, "Platinum": platinum}

    champion_bits = None
    if total_xp:
        crow = db_champion_reward_profile(champion_id, conn=db)
        if crow:
            new_xp = int(crow[3] or 0) + total_xp
            thresholds = (0, 1000, 2800, 5000, 8000, 12000, 17500,
                          25000, 40000, 50000, 60000, 70000, 80000,
                          92000, 110000)
            new_level = max(1, min(len(thresholds),
                                   1 + sum(new_xp >= n for n in thresholds[1:])))
            db_update_champion_xp(champion_id, new_xp, new_level, conn=db)
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
            "SocketedGems": EGemTypesNew.Unknown, "IsNotTradeable": False,
            "EscrowStatus": "Clean",
        }})
        applied["Completed"].append({
            "ItemKind": "BOACARD", "ItemQuantity": 1,
            "ItemAction": "GRANT", "ItemTemplate": card["guid"],
            "RCode": "CARD",
        })
    for chest in result["chests"]:
        applied["Items"].append({"Item": {
            "Id": 9000 + int(chest["id"]),
            "TemplateID": chest["template"],
            "BoundToProfile": True,
            "ItemQuantity": 1,
            "ClaimDate": "0001-01-01T00:00:00",
            "EscrowStatus": "Clean",
        }})
        applied["Completed"].append({
            "ItemKind": "BOAITEM", "ItemQuantity": 1,
            "ItemAction": "GRANT", "ItemTemplate": chest["template"],
            "RCode": "CHEST",
        })
    for item in result["items"]:
        applied["Items"].append({"Item": {
            "Id": int(item["id"]),
            "TemplateID": item["template"],
            "BoundToProfile": True,
            "ItemQuantity": int(item["quantity"]),
            "ClaimDate": "0001-01-01T00:00:00",
            "EscrowStatus": "Clean",
        }})
        applied["Completed"].append({
            "ItemKind": "BOAITEM", "ItemQuantity": int(item["granted"]),
            "ItemAction": "GRANT", "ItemTemplate": item["template"],
            "RCode": "ITEM",
        })
    if (not granted and not total_gold and not total_xp and
            not result["chests"] and not result["items"]):
        state["_last_encounter_condition_met"] = bool(result["condition_met"])
        _save_campaign_state(db, camp_id, state)
        db.commit()
        return result
    state["_last_encounter_condition_met"] = bool(result["condition_met"])
    _save_campaign_state(db, camp_id, state)
    db.commit()
    result.update({"cards": granted, "gold": total_gold, "xp": total_xp})
    getattr(handler, "_log_req", print)(
        f"    Encounter reward: scene={scene_guid} cards={len(granted)} "
        f"chests={len(result['chests'])} gold={total_gold} xp={total_xp}")
    return result


def prepare_fra_battle_gameend(handler, db, session, won):
    """Persist and clean up a non-campaign FRA/PvE battle result.

    The client leaves the battle as soon as it receives ``GameEnded`` and
    immediately joins the Arena lobby again.  Callers that publish that event
    themselves must therefore run this step first, otherwise the join can read
    the previous ``challenger_index`` and show the same opponent again.

    ``None`` means the session belongs to the separate campaign/tournament
    path.  A dict means this was the FRA/PvE path, including a failed attempt
    whose error is already logged and represented by ``handled=False``.
    """
    try:
        session_name = session.session_name or ""
        if (session_name.startswith("camp_") or
                session_name.startswith("tourney-")):
            return None
        profile = getattr(handler, "user_profile", None)
        if not profile:
            return None
        from pve_db import db_record_arena_fight
        from pvp_db import db_delete_game_session
        result = db_record_arena_fight(
            profile["id"], won, return_details=True,
            session_id=session.session_id)
        db_delete_game_session(session.session_id)
        return {"handled": True, "result": result}
    except Exception as exc:
        getattr(handler, "_log_req", print)(
            f"    FRA result preparation failed: {exc}")
        return {"handled": False, "result": None}


def publish_fra_battle_gameend(handler, prepared, service_mail_uid):
    """Publish deferred FRA flags and reward conversations after GameEnded."""
    if not prepared or not prepared.get("handled"):
        return False
    result = prepared.get("result") or {}
    reward_guid = result.get("reward_conversation_guid", "")
    if result.get("tier_one_perfect_flag_awarded"):
        try:
            handler.push_reckoning_flags_updated()
        except Exception as exc:
            getattr(handler, "_log_req", print)(
                "    Could not publish ARENA_TIER1_PERFECT: "
                f"{exc}")
    if result.get("recorded") and reward_guid:
        try:
            _push_fra_buff_conversation(handler, reward_guid, service_mail_uid)
        except Exception as exc:
            getattr(handler, "_log_req", print)(
                f"    Could not send FRA reward conversation: {exc}")
    return True


def _rules_battle_gameend(handler, db, session, camp_id, won, zone_id,
                          service_mail_uid, service_campaign_uid_type):
    """``handle_battle_gameend`` for a rules-driven zone: the engine applies
    the result (scene / card-choice flag actions, quest steps, result
    conversation), then the scene's rewards are paid against its flags."""
    from campaign_runtime import runtime
    zone = runtime.handle_battle_end(handler, db, zone_id, session, camp_id, won)
    reward_result = _apply_encounter_end_rewards(
        handler, db, session, camp_id, won, rules_runtime=True)
    applied = _merge_applied(reward_result.get("applied") or
                             _empty_applied_updates(), zone.applied)
    service = ("00000000-0000-0000-0000-000000000000", "ServiceCampaign",
               str(service_campaign_uid_type), 0, service_mail_uid)
    push_gameendnotify(handler, db, camp_id, won, 0, *service, applied=applied)
    net = {"handler": handler, "args": (0,) + service}
    for push in zone.after:
        push(net)
    db_delete_game_session(session.session_id)
    getattr(handler, "_log_req", print)(
        f"    Campaign {camp_id} [{zone_id}]: rules result applied (won={won})")
    return True


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
        if not session_name.startswith("camp_"):
            prepared = prepare_fra_battle_gameend(handler, db, session, won)
            if prepared is not None:
                publish_fra_battle_gameend(
                    handler, prepared, service_mail_uid)
                getattr(handler, "_log_req", print)(
                    f"    FRA result recorded (won={won}); session cleaned")
                return bool(prepared.get("handled"))
            return False
        camp_id = int(session_name[5:])
        zone_id = _rules_zone(db, camp_id)
        if zone_id:
            return _rules_battle_gameend(handler, db, session, camp_id, won,
                                         zone_id, service_mail_uid,
                                         service_campaign_uid_type)
        reward_result = _apply_encounter_end_rewards(
            handler, db, session, camp_id, won)
        advanced_quest_states = []
        camp_row = pve_db.db_campaign_identity_state(camp_id, conn=db)
        if reward_result.get("condition_met"):
            # Taming objectives are separate QUEST campaign entries linked by
            # encounter GUID. Advance that objective only after the finished
            # battle actually contains a captured troop.
            if camp_row:
                tamed_state = _advance_quest_campaign(
                    db, camp_row[0], "az01_tamed", reward_result.get("scene_guid"))
                # The advance was saved but never pushed, so the journal kept
                # showing the old objective until a reload.
                if tamed_state:
                    advanced_quest_states.append(tamed_state)
        if won:
            advanced_quest_states.extend(_advance_quest_encounter_objectives(
                db, camp_row[0] if camp_row else 0,
                reward_result.get("scene_guid")))
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
                      deck_uid64, comp, session_id, target, instance, conh, uid,
                      encounter_mod_conversation_guid=None, mod_choice=None):
    """Push a 'gamestarted' notification launching an encounter battle.

    Shared by the campaign 'start' SendEvent and the 'camp.encounter' cheat.
    Builds a SessionState whose EncounterData.SceneTemplateId is the encounter
    scene and pushes it; the client transitions to EGameState.Battle and then
    drives the LoadBalancer session flow (FindSession -> StartEncounter ->
    Join -> battle events).

    ``encounter_guid`` may also be the rules engine's encounter descriptor:
    ``{"scene": ..., "conversation_id": ...}``. In that form, the optional
    conversation is queued as an encounter mod when battle setup begins.

    ``mod_choice`` is the card picked on a ModChoiceEncounter's selector
    panel; battle setup applies it in place of the scene's choice mod, and
    its option's flag actions run with the scene's.

    The battle session itself is NOT created here — StartEncounter (22017)
    allocates the authoritative DB-backed session. The scene GUID is also
     recorded so resolve_encounter can resolve the correct AI deck/name
    when the battle session is later set up.
    """
    if isinstance(encounter_guid, dict):
        encounter_action = encounter_guid
        encounter_guid = encounter_action.get("scene")
        if encounter_mod_conversation_guid is None:
            encounter_mod_conversation_guid = encounter_action.get(
                "conversation_id")
    if not encounter_guid:
        raise ValueError("encounter action needs a scene")
    if encounter_mod_conversation_guid:
        encounter_mod_conversation_guid = str(
            encounter_mod_conversation_guid).strip().lower()
    else:
        encounter_mod_conversation_guid = None

    from encoder import encode_campaign_session_state
    import base64

    log = getattr(handler, "_log_req", print)
    scene_row = pve_db.db_encounter_scene_runtime(encounter_guid, conn=db)
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
    saved_state = pve_db.db_campaign_state(camp_id, conn=db)
    camp_row = (saved_state,) if saved_state is not None else None
    if camp_row:
        try:
            camp_state = json.loads(camp_row[0] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            camp_state = {}
        camp_state["ActiveEncounterGuid"] = encounter_guid
        if encounter_mod_conversation_guid:
            camp_state["ActiveEncounterConversationGuid"] = (
                encounter_mod_conversation_guid)
        else:
            camp_state.pop("ActiveEncounterConversationGuid", None)
        if mod_choice:
            camp_state["ActiveEncounterModChoice"] = str(mod_choice).lower()
        else:
            camp_state.pop("ActiveEncounterModChoice", None)
        _run_scene_actions(db, camp_state, encounter_guid, "pre")
        _save_campaign_state(db, camp_id, camp_state)
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

# Discover the random-reading pool from the authored AZ1 effect set.
_AZ1_FORTUNE_SET_GUID = "ccde3b6a-3425-4403-b366-dba0e2358fae"


def _fortune_card_guids(db):
    """Return the authored AZ1 Fortune card templates in stable order."""
    rows = pve_db.db_fortune_card_guids(_AZ1_FORTUNE_SET_GUID, conn=db)
    return [str(row[0]).lower() for row in rows if row[0]]


def _fortune_ability_guid(db, card_guid):
    """Return the first ability authored on a Fortune card."""
    raw_abilities = pvp_db.db_template_ability_payload(
        str(card_guid).lower(), conn=db)
    if raw_abilities is None:
        return None
    try:
        abilities = json.loads(raw_abilities or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        abilities = []
    for ability in abilities if isinstance(abilities, list) else []:
        guid = str(ability or "").lower()
        if guid and pvp_db.db_ability_metadata(guid, conn=db):
            return guid
    return None


def _fortune_starting_hand_bonus(db, ability_guid):
    """Read a Fortune's typed starting-hand modifier from its BOM."""
    if not ability_guid:
        return 0
    from rules_port.pregame import ability_starting_hand_size_modifier
    return ability_starting_hand_size_modifier(db, ability_guid)


def consume_fortune(db, camp_id, fortune_guid):
    """Consume the stored reading once its next campaign battle starts."""
    if not camp_id or not fortune_guid:
        return False
    raw_state = pve_db.db_campaign_state(camp_id, conn=db)
    if raw_state is None:
        return False
    try:
        state = json.loads(raw_state or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    data = ((state.get("PublicState") or {}).get("Data") or {})
    if str(data.get("gaal_fortune_guid") or "").lower() != \
            str(fortune_guid).lower():
        return False
    data.pop("gaal_fortune", None)
    data.pop("gaal_fortune_guid", None)
    state.setdefault("PublicState", {})["Data"] = data
    _save_campaign_state(db, camp_id, state)
    db.commit()
    return True


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
    fortune_guid = None
    fortune_ability_guid = None
    fortune_starting_hand_bonus = 0
    encounter_mod_conversation_guid = None
    encounter_mod_choice_guid = None
    if camp_id:
        row = pve_db.db_campaign_battle_profile(camp_id, conn=db)
        if row:
            player_champ_name, deck_db_id = row[0], row[1]
            race_num, cls_num, gnd_num = row[2], row[3], row[4]
            player_talents_json = row[6] or "[]"
            try:
                campaign_state = json.loads(row[5] or "{}")
                is_tutorial = not bool(campaign_state.get(
                    "TutorialDone", False))
                encounter_mod_conversation_guid = str(
                    campaign_state.get("ActiveEncounterConversationGuid") or ""
                ).strip().lower() or None
                encounter_mod_choice_guid = str(
                    campaign_state.get("ActiveEncounterModChoice") or ""
                ).strip().lower() or None
                data = ((campaign_state.get("PublicState") or {}).get(
                    "Data") or {})
                fortune_guid = str(data.get("gaal_fortune_guid") or "").lower()
                if fortune_guid:
                    fortune_ability_guid = _fortune_ability_guid(db, fortune_guid)
                    fortune_starting_hand_bonus = _fortune_starting_hand_bonus(
                        db, fortune_ability_guid)
            except Exception:
                is_tutorial = True
    profile = getattr(handler, "user_profile", None) or {}
    if not deck_db_id and profile:
        profile_id = profile.get("id")
        if profile_id is not None:
            deck_db_id = pve_db.db_first_deck_id_for_user(
                int(profile_id), conn=db)

    try:
        race_key = int(race_num) if race_num is not None else -1
    except (TypeError, ValueError):
        race_key = -1
    race_name = _RACE_DECK_MAP.get(race_key)
    cls_name = {1: "Mage", 2: "Warrior", 3: "Cleric", 4: "Rogue",
                5: "Warlock", 6: "Ranger", 7: "Boat"}.get(
                    int(cls_num) if cls_num is not None else -1)
    gnd_name = {1: "Male", 2: "Female"}.get(
        int(gnd_num) if gnd_num is not None else -1, "")
    player_champ_guid = None
    if race_name and cls_name:
        player_champ_guid = pve_db.db_player_champion_template(
            race_name, cls_name, gnd_name, conn=db)
    player_champ_guid = player_champ_guid or \
        "1d462ffb-0744-4996-804c-ba61b2c5c2f1"
    if pve_db.db_champion_template_data_exists(
            player_champ_guid, conn=db):
        player_starting_health = handler._champion_health_by_guid(
            player_champ_guid)
    else:
        player_starting_health = handler._champion_starting_health(
            race_name, cls_name)
    # Passive talents are not included in the champion ability list sent to
    # the client.  Apply their metadata-defined starting-health modifiers at
    # the authoritative battle boundary (e.g. Weight's +5).
    try:
        from rules_port.pregame import \
            passive_talent_starting_health_modifier
        talent_guids = json.loads(player_talents_json or "[]")
        player_starting_health += passive_talent_starting_health_modifier(
            db, talent_guids)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    # The scene's AI personality comes from shared authored campaign data.
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
        "fortune_guid": fortune_guid,
        "fortune_ability_guid": fortune_ability_guid,
        "fortune_starting_hand_bonus": fortune_starting_hand_bonus,
        "encounter_mod_conversation_guid": encounter_mod_conversation_guid,
        # The card picked on a ModChoiceEncounter's selector panel.
        "encounter_mod_choice_guid": encounter_mod_choice_guid,
    }


def player_cannot_choose_play_first(db, camp_id):
    """Return whether a campaign champion has a no-Play-first talent.

    ChampionTalentData serializes the rule into its TAC tree as the
    ``CantGoFirst`` attribute, including for passive talents with no ability.
    """
    if not camp_id:
        return False
    raw_talents = pve_db.db_campaign_talents(camp_id, conn=db)
    if raw_talents is None:
        return False
    try:
        talent_guids = json.loads(raw_talents or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(talent_guids, list) or not talent_guids:
        return False
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.tac import decode_tac_tree, _tac_attr_hash
    attr = _tac_attr_hash("CantGoFirst")
    for talent_guid in talent_guids:
        record = DEFAULT_RECORD_STORE.get(
            "ChampionTalentData", str(talent_guid or "").lower())
        serialized = record.field("m_SerializedTAC") or {} if record else {}
        data = serialized.get("data") if isinstance(serialized, dict) else None
        if not data:
            continue
        try:
            value = decode_tac_tree(data).get(attr, 0)
            if int(value or 0):
                return True
        except (TypeError, ValueError):
            continue
    return False


def resolve_opening_hand_config(db, session, player_id, race_name, cls_name,
                                ability_guids):
    """Resolve campaign opening-hand rules from class and talent metadata.

    Battle setup owns the common hand/deck protocol, while this helper owns
    the campaign-specific class baseline and typed pre-game talent effects.
    ``ability_guids`` may be ResourceId objects or strings; the metadata
    condition evaluator consumes their canonical GUID text.
    """
    class_hand_size = pve_db.db_starting_hand_size(
        race_name, cls_name, conn=db)
    base_hand = (int(class_hand_size) if class_hand_size is not None
                 else 7)
    from rules_port.pregame import pregame_modifiers
    guids = []
    for ability in ability_guids or []:
        guid = getattr(ability, "guid", ability)
        guids.append(str(guid))
    mods = pregame_modifiers(db, session, player_id, guids)
    return {
        "starting_hand_size": max(0, base_hand + int(mods["starting_hand"])),
        # Player.MaximumHandSize: PvE and PvE Arena use ten, PvP seven.
        "maximum_hand_size": max(0, 10 + int(mods["maximum_hand"])),
        "starting_hand_effects": list(mods["starting_hand_effects"]),
    }


def apply_starting_hand_talents(handler, db, session, game, pl_t, effects):
    """Apply campaign opening-hand stat effects from talent metadata.

    Triggered GrantAbility talents are resolved by the normal
    ``GameStartedEvent`` dispatcher.  This helper is intentionally limited to
    modifiers that have no event/BOM target, so a talent cannot be applied in
    both this setup pass and the trigger dispatcher.
    """
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
        candidates = pvp_db.db_starting_hand_candidates(
            session.session_id, owner_id,
            card_types if card_types else None, conn=db)
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
        pvp_db.db_apply_starting_hand_effect(
            session.session_id, int(card_uid), cost_mod=cost_mod,
            permanent_buffs=json.dumps(buffs) if rage > 0 else None, conn=db)
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
    deck_db_id = pve_db.db_champion_last_deck_id(champ_id, conn=db)
    deck_uid64 = (deck_db_id << 8) | 17 if deck_db_id else 0

    campaign_row = pve_db.db_campaign_runtime_row(camp_id, conn=db)
    row = campaign_row
    campaign_type = (row[1] or "").upper() if row else ""
    encounter_guid = None
    mod_choice = None

    if campaign_type in ("DUNGEON", "AREA"):
        state = json.loads(row[0]) if row and row[0] else {}
        race_name = _race_name_for_campaign(db, camp_id)
        active_node = _resolve_node(
            state, state.get("ALoc") or state.get("LastNode") or "")
        # A card-selector battle sends the chosen card as OParms[0].
        mod_choice = _chosen_mod_card(db, state, active_node,
                                      env_json.get("OParms"))
        if campaign_type == "DUNGEON" and _normalize_crayburn_encounters(state, race_name):
            _save_campaign_state(db, camp_id, state)
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
            f"encounter={encounter_guid}"
            + (f" mod_choice={mod_choice}" if mod_choice else ""))
    else:
        # The training encounter scene for this race.
        encounter_guid = cfg.get("training_encounter")

    return _launch_encounter(handler, db, camp_id, champ_id, encounter_guid,
                             deck_uid64, comp, session_id, target, instance,
                             conh, uid, mod_choice=mod_choice)


def resolve_encounter(db, camp_id, scene_guid=None):
    """Resolve a campaign's encounter data.

    The AI deck, champion and personality come from the scene's
    ``encounter_scenes`` row; ``_az0_config`` supplies the tutorial's. Returns (scene_guid,
    ai_deck_guid, ai_champ_guid, ai_name, ai_charge_power, ai_personality,
    ai_deck_personality) or (None,)*7 if unresolved.
    """
    champion_id = pve_db.db_campaign_identity_state(camp_id, conn=db)
    if not champion_id:
        return None, None, None, None, None, None, None
    race = pve_db.db_campaign_champion_race(camp_id, conn=db)
    if race is None:
        return None, None, None, None, None, None, None
    cfg = _az0_config(race)
    if not cfg:
        return None, None, None, None, None, None, None
    if scene_guid:
        scene = pve_db.db_encounter_scene_runtime(scene_guid, conn=db)
        ai_deck_guid = scene[4] if scene else None
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
            ai_champion_guid = scene[5] if scene else None
            if ai_champion_guid:
                template_name = pvp_db.db_template_name(
                    ai_champion_guid, conn=db)
                if not template_name:
                    champ_row = db.execute(
                        "SELECT name FROM champion_templates_extended WHERE guid=?",
                        (ai_champion_guid,)).fetchone()
                    template_name = champ_row[0] if champ_row else None
                if template_name:
                    ai_name = template_name
        return scene_guid, ai_deck_guid, ai_champion_guid, \
            ai_name, cfg.get("ai_charge_power"), cfg.get("ai_personality"), \
            scene[6] if scene else None
    scene_guid = cfg.get("training_encounter")
    scene = (pve_db.db_encounter_scene_runtime(scene_guid, conn=db)
             if scene_guid else None)
    ai_deck_guid = scene[4] if scene else None
    ai_name = cfg.get("trainer_npc") or (scene[1] if scene else None)
    return scene_guid, ai_deck_guid, cfg.get("ai_champion_guid"), ai_name, \
        cfg.get("ai_charge_power"), cfg.get("ai_personality"), \
        scene[6] if scene else None


def _handle_gameend(handler, db, env_json, comp, session_id,
                     reqid, target, instance, conh, uid):
    """Handle a campaign game-end notification (RequestType "gameend").

    The client sends this after a campaign battle finishes, with the session
    name and winner/loser UID lists. After the training battle is won, reveal
    the quest-giver NPC so the player can take the Crayburn Castle quest.
    """
    camp_id = env_json.get("CampID", 0)
    zone_id = _rules_zone(db, camp_id)
    if zone_id:
        from campaign_runtime import runtime
        return runtime.handle_gameend(handler, db, zone_id, env_json, comp,
                                      session_id, reqid, target, instance,
                                      conh, uid)
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
    zone_id = _rules_zone(db, camp_id)
    if zone_id:
        from campaign_runtime import runtime
        return runtime.handle_locaction(handler, db, zone_id, env_json, comp,
                                        session_id, reqid, target, instance,
                                        conh, uid)
    ract = env_json.get("RAct", 0)  # 0=Start, 1=FinishLoc
    location_name = env_json.get("Loc", "")
    params = env_json.get("Params", [])

    log = getattr(handler, "_log_req", print)
    action = "StartLoc" if ract == 0 else "FinishLoc"
    log(f"    Campaign {action}: camp={camp_id} loc={location_name}")

    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
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
            state = _prepare_dungeon_state(
                state, _race_name_for_campaign(db, camp_id))
            requested_node = _resolve_node(state, location_name)
            expected_node = _crayburn_first_incomplete(state)
            requested_idx = (_CASTLE_CHAIN.index(requested_node)
                             if requested_node in _CASTLE_CHAIN else None)
            expected_idx = (_CASTLE_CHAIN.index(expected_node)
                            if expected_node else None)
            movement_rejected = bool(
                requested_idx is None or
                (expected_idx is not None and requested_idx > expected_idx))
            if movement_rejected:
                # Do not silently turn a click on a future marker into
                # StartLoc for the current encounter.  That made Tower of
                # Penworth appear reachable while Tower Gatehouse was still
                # unfinished, and could launch the wrong battle on `start`.
                node = _crayburn_return_node(
                    state, expected_node or requested_node or "Entrance")
                log(f"    Campaign movement rejected: {requested_node} is "
                    f"locked; expected {expected_node}")
                state["ALoc"] = ""
                state["LastNode"] = node
                state["CurState"] = "EXPLORE"
                _reveal_crayburn_node(state, node)
            else:
                node = requested_node
                state.pop("_pending_travel", None)
                state["ALoc"] = node
                state["LastNode"] = node
                state["CurState"] = "EXPLORE"
                _reveal_crayburn_node(state, node)
                _set_crayburn_autostart(state, node)
        state["CurState"] = "EXPLORE"
    else:
        state["CurState"] = "EXPLORE"
        for loc in state.get("VisLocs", []):
            ld = loc.get("Data", {})
            if ld.get("node") == location_name or ld.get("name") == location_name:
                ld["completed"] = True
                _note_visited(state, ld.get("node") or location_name)
                break

    _save_campaign_state(db, camp_id, state)
    db.commit()

    resp = _build_input_response(camp_id, state, success=True)
    ret = _send_response(handler, json.dumps(resp), comp, session_id,
                         reqid, target, instance, conh, uid)
    return ret


def _handle_forfeit(handler, db, env_json, comp, session_id,
                     reqid, target, instance, conh, uid):
    """Forfeit: forfeit a campaign."""
    camp_id = env_json.get("CampID", 0)
    zone_id = _rules_zone(db, camp_id)
    if zone_id:
        from campaign_runtime import runtime
        return runtime.handle_forfeit(handler, db, zone_id, env_json, comp,
                                      session_id, reqid, target, instance,
                                      conh, uid)
    row = pve_db.db_campaign_forfeit_row(camp_id, conn=db)
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

    _save_campaign_state(db, camp_id, state)
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
        deck_db_id = pve_db.db_champion_last_deck_id(champ_id, conn=db)
        deck_uid64 = (deck_db_id << 8) | 17 if deck_db_id else 0
        # Find the campaign for this champion to get its CampID.
        latest = pve_db.db_latest_campaign_any(champ_id, conn=db)
        camp_id = latest[0] if latest else 0
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
            row = pve_db.db_latest_campaign_any(champ_id, conn=db)
            if not row:
                if not pve_db.db_campaign_champion(champ_id, conn=db):
                    raise ValueError("no champion for dungeon cheat")
                camp_id, _inst, _started, _st = _find_campaign_for_champion(
                    db, champ_id, "DUNGEON")
                camp_champ_id = champ_id
            else:
                camp_id, camp_champ_id, _ = row
            # Always rebuild a fresh dungeon state — stale _pending_travel /
            # completed-node data from a previous run skips conversations.
            pve_db.db_campaign_clear_state(camp_id, db)
            db.commit()
            champ = _get_champion(db, camp_champ_id)
            cfg = _az0_config(champ[2]) if champ else _az0_config(1)
            if cfg is None:
                cfg = _az0_config(1)
            if cfg is None:
                raise RuntimeError("AZ0 campaign configuration is unavailable")
            state = _build_initial_gameplay_state(camp_id, camp_champ_id,
                                                  "DUNGEON", _race_for_cfg(cfg))
            _transition_to_dungeon(state, cfg)
            pve_db.db_promote_campaign_to_dungeon(
                camp_id, json.dumps(state), conn=db)
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
