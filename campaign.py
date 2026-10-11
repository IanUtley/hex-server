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
from profile_db import (db_add_stardust,
                        db_inventory_item, db_next_inventory_client_uid,
                        db_upsert_inventory_item, db_reward_card_template,
                        db_grant_card_instance,
                        db_adjust_user_currency, db_champion_reward_profile,
                        db_update_champion_xp, db_create_treasure_chest)

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
    """Find (or create) a champion's starter panorama row on the rules runtime."""
    from campaign_runtime import locate
    if campaign_type != "PANORAMA":
        raise ValueError(f"only starter panoramas are created here, not {campaign_type}")
    row = pve_db.db_campaign_for_champion(champion_id, campaign_type, db)
    if row:
        pve_db.db_campaign_set_last_campaign(champion_id, row[0], db)
        db.commit()
        return (row[0], row[1], row[2], json.loads(row[4] or "{}"))
    cid = _new_camp_id(db)
    inst_id = _generate_inst_id()
    champ = _get_champion(db, champion_id)
    zone_id = locate.starter_zone(champ[2] if champ else None)
    if not zone_id:
        raise ValueError(f"no starter panorama for champion {champion_id}")
    state = _initial_zone_state(cid, champion_id, zone_id)
    pve_db.db_create_campaign(
        cid, inst_id, champion_id, pve_db.db_campaign_user_id(champion_id, db),
        champ[3] if champ else "", "AZ1", "PANORAMA", json.dumps(state), db)
    db.commit()
    # Keep the champion's LastCampaignID in sync so the client can jump
    # straight into this campaign after selecting the champion on the globe.
    pve_db.db_campaign_set_last_campaign(champion_id, cid, db)
    db.commit()
    return (cid, inst_id, 0, state)


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


def _conversation_name(db, conversation_guid):
    row = pve_db.db_conversation_name(conversation_guid, db)
    return str(row or "").strip()




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


_AZ1_MASKED_ARMY_QUESTS = (
    ("az01_q_fort_romor", "Underworld"),
    ("az01_q_usurper", "Ardent"),
)






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


# Named quest-start content hooks (``quest_templates.start_hook``).  Map
# gates come from hex-private/campaign/campaign_rules via campaign_gates and quest content from
# the editor data; none are registered now.
_QUEST_START_HOOKS = {}










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








def _resolve_node(state, key):
    """Map a VisLoc name or node id back to the canonical node id."""
    if key in _CASTLE_CHAIN:
        return key
    for loc in state.get("VisLocs", []):
        d = loc.get("Data", {})
        if d.get("name") == key:
            return d.get("node") or key
    return key












def _race_name_for_campaign(db, camp_id):
    race = pve_db.db_campaign_champion_race(camp_id, conn=db)
    if race is None:
        return "Necrotic"
    return _RACE_NAMES.get(race, "Necrotic")









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










def repair_user_campaigns(db, user_id, camp_id=None):
    """Repair pass for ``!campaign-cleanup``.

    Every campaign row is now a rules zone or a quest journal, and the rules
    runtime owns the state of a zone, so there is no legacy state to repair.
    ``camp_id`` is still checked against the profile, so a chat command cannot
    touch another player's campaign: None means it is not theirs.
    """
    if camp_id is not None:
        owner = pve_db.db_campaign_owner_user_id(int(camp_id), conn=db)
        if owner is None or owner != int(user_id):
            return None
    return []




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




_CRAYBURN_ENCOUNTER_NODES = ("CastleGate", "TowerGate", "PenworthTower")





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
    # Otherwise the champion's starter panorama is the current campaign.
    cid, _inst_id, _started, state = _find_campaign_for_champion(
        db, champ_id, "PANORAMA")
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

    # Preserve the exact protocol fallback order while keeping SQL in pve_db.
    rows = pve_db.db_active_campaign_candidates(
        champ_id, ctype, template, conn=db)
    templates = [_build_template_info(row[0], row[1], row[2] or "AREA", row[3] or "AZ1") for row in rows]
    return _send_response(handler, json.dumps(templates), comp, session_id,
                          reqid, target, instance, conh, uid)


def _no_zone_reply(handler, camp_id, comp, session_id, reqid, target,
                    instance, conh, uid):
    """Error reply for a campaign row that no rules zone runs."""
    resp = _build_input_response(camp_id, None, success=False)
    resp["Errors"] = ["Campaign not found"]
    return _send_response(handler, json.dumps(resp), comp, session_id,
                          reqid, target, instance, conh, uid)


def _initial_zone_state(camp_id, champ_id, zone_id):
    """Minimal client state for a new rules-driven row; campaign_runtime
    projects the rest from the zone's rules on the first request."""
    from campaign_runtime import catalog_data, locate
    row_type = locate.ROW_TYPES[catalog_data.zone(zone_id)["kind"]]
    return {
        "CampID": camp_id, "ChampID": champ_id,
        "TempType": row_type, "RulesZone": zone_id,
        "PayGroups": [], "CSlide": None, "ALoc": None,
        "VisLocs": [], "LocNodes": [], "Encounters": [],
        "Champions": [], "CurState": "EXPLORE", "LastNode": "",
        "PublicState": {"Data": {"CampaignGroup": row_type}},
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
                    state = _initial_zone_state(
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
                state = _initial_zone_state(
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
    # A quest campaign is not a zone: its journal row is its state.
    row = pve_db.db_campaign_query_row(camp_id, conn=db)
    if not row or str(row[3] or "").upper() != "QUEST":
        return _no_zone_reply(handler, camp_id, comp, session_id, reqid,
                              target, instance, conh, uid)
    state = json.loads(row[2] or "{}")
    resp = _build_input_response(camp_id, state, success=True)
    return _send_response(handler, json.dumps(resp), comp, session_id,
                          reqid, target, instance, conh, uid)


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
    if not row or str(row[2] or "").upper() != "QUEST":
        return _no_zone_reply(handler, camp_id, comp, session_id, reqid,
                              target, instance, conh, uid)
    champ_id, state_json, ctype, template_name = row
    state = json.loads(state_json or "{}")
    state["Started"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["CurState"] = "EXPLORE"
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
    return _no_zone_reply(handler, camp_id, comp, session_id, reqid,
                          target, instance, conh, uid)






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
    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
    if not row:
        return "    Campaign not found for gameend"
    state = json.loads(row[1] or "{}")

    champ_id = state.get("ChampID", 0)
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


_SCENE_ACTION = re.compile(r"^\s*(set_flag|clear_flag)\s*\(\s*([^()\s][^()]*?)\s*\)\s*$")
_SCENE_CHECK_TAMED = re.compile(r"^\s*check_?tamed\s*\(\s*\)\s*$", re.I)




# PublicState path the ModChoiceEncounter location's ``modlistId`` names:
# the client's card-selector panel lists these card GUIDs.
_MOD_CHOICE_LIST = "mod_choice_list"






def _battle_events(db, session, player_user_id):
    """Named events a finished battle produced (``tamed_troop`` ...)."""
    events = []
    if _evaluate_encounter_condition(
            db, session.session_id, player_user_id,
            {"type": "void_tamed_troop", "owner": "opponent"}):
        events.append("tamed_troop")
    return events




def _apply_encounter_end_rewards(handler, db, session, camp_id, won):
    """Apply authored encounter rewards and return campaign AppliedUpdates.

    Campaign clients build their loot window from ``gameendnotify.Applied``;
    generic profile reward events are not sufficient here. The engine has
    already run the scene's flag actions; its flags are mirrored into the
    state the ``when`` tags read.
    """
    result = {"applied": _empty_applied_updates(), "cards": [],
              "items": [], "chests": [], "gold": 0, "xp": 0,
              "condition_met": False, "scene_guid": None}
    if not won:
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
    from pvp_db import db_delete_game_session
    zone = runtime.handle_battle_end(handler, db, zone_id, session, camp_id, won)
    reward_result = _apply_encounter_end_rewards(
        handler, db, session, camp_id, won)
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
    from pvp_db import db_delete_game_session
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
        db_delete_game_session(session.session_id)
        getattr(handler, "_log_req", print)(
            f"    Campaign {camp_id}: no rules zone; session cleaned")
        return False
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
    # A scene may fix the player's champion and deck (blank: the player's own).
    player_champ_override, player_deck_override = (
        pve_db.db_encounter_scene_player_override(scene_guid, conn=db)
        if scene_guid else (None, None))
    player_deck_cards_json = None
    if player_deck_override:
        player_deck_cards_json = json.dumps([
            str(card_guid).lower()
            for card_guid, quantity, _gems in pvp_db.db_encounter_deck_cards(
                player_deck_override, conn=db)
            for _copy in range(int(quantity or 0))])

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
        "player_champ_guid": player_champ_override or player_champ_guid,
        "player_deck_cards_json": player_deck_cards_json,
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
    return _no_zone_reply(handler, camp_id, comp, session_id, reqid,
                          target, instance, conh, uid)


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
    return _no_zone_reply(handler, camp_id, comp, session_id, reqid,
                          target, instance, conh, uid)


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
    state = json.loads(state_json or "{}")

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
    else:
        results.append({"Cheat": "unknown", "Success": False,
                        "Summary": "Unsupported cheat"})

    return _send_response(handler, json.dumps(results), comp, session_id,
                          reqid, target, instance, conh, uid)
