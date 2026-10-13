"""Read-only campaign data for the campaign editor.

Static content comes from ``hex-private/Records/`` (SceneData, ConversationTemplate,
QuestTemplate, ChampionTemplate); the server's derived catalog tables and a
champion's live campaign state come from the SQLite database, opened
read-only. Nothing here writes to Records or the database.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

from paths import CAMPAIGN_ROOT, GENERATED, RECORDS, REPO  # noqa: E402,F401  (also sets sys.path)
_CARD_LOCATIONS = None
_CARD_LOCATIONS_LOCK = threading.Lock()


def card_art_on_demand(art: str) -> Path | None:
    """Decode one card's art from the client install the first time it is shown.

    The client root is ``HEX_CLIENT_ROOT``, else ``/client-data`` (the container
    mount), else the extractor's default. Returns ``None`` when no install has
    that card's art.
    """
    global _CARD_LOCATIONS
    import sys
    extractors = CAMPAIGN_ROOT / "AssetExtraction"
    if str(extractors) not in sys.path:
        sys.path.insert(0, str(extractors))
    import extract_card_art
    with _CARD_LOCATIONS_LOCK:
        if _CARD_LOCATIONS is None:
            root = Path(os.environ.get("HEX_CLIENT_ROOT") or "/client-data")
            if not (root / "AssetBundles").is_dir():
                root = extract_card_art.DEFAULT_CLIENT
            _CARD_LOCATIONS = extract_card_art.harc_locations(root)
        locations = _CARD_LOCATIONS
    return extract_card_art.extract_card(locations, art, out=GENERATED / "cards")

# The data side is shared with the server's campaign runtime.
from campaign_runtime import offers  # noqa: E402
from campaign_runtime.catalog_data import (  # noqa: E402,F401
    _EMPTY_CATALOG, _audience, _authored_items, _json, _quest_conditions,
    data_mtime, data_overrides, dungeon_prefixes, encounter_event_nodes,
    item_title, match_item, quest_actions, scene_node_id, zone, zones,
    DATA_DIR, SCENE_NODE_LINKS)
from campaign_runtime import catalog_data as _catalog_data  # noqa: E402
from campaign_runtime.catalog_data import server_catalog as _server_catalog  # noqa: E402
from campaign_runtime.records import (  # noqa: E402,F401
    _endings_of, _events, _guid, _qualifiers, _records_encounters,
    _scene_kind, _short_type, champion_names, champion_status_keywords,
    conversation_endings, conversation_hooks, conversations, ending_id,
    encounter_scene_names, quest_templates, records, scene_template)
from campaign_runtime import rules_loader  # noqa: E402

AREA_TEMPLATE = "AZ1"
SCENE_NAME = "Howling Plains"


def area_scene() -> dict[str, Any]:
    for record in records("SceneData"):
        if record.get("m_Name") == SCENE_NAME:
            return record
    raise LookupError(f"SceneData {SCENE_NAME!r} not found")


def warm_caches(db_path: Path = REPO / "hconnect.db") -> None:
    """Fill the Records-derived caches the page asks for first, in the background.

    The first request for each one parses a Records section under a shared
    lock, so a page that asks for several at once waited on the parse. Warming
    them at startup means the first page load finds them ready. Timings go to
    stdout (the editor's log).
    """
    import time
    steps = [("conversations", lambda db: conversations()),
             ("rewards catalogue", lambda db: rewards_catalog(db)),
             ("zones", lambda db: zones()),
             ("area " + AREA_TEMPLATE, lambda db: area_payload(db, AREA_TEMPLATE)),
             ("rules " + AREA_TEMPLATE, lambda db: rules(db, AREA_TEMPLATE))]
    try:
        db = connect(db_path)
    except sqlite3.Error as exc:
        print(f"Campaign editor warm-up skipped: {exc}", flush=True)
        db = None
    started = time.time()
    try:
        for name, step in steps:
            began = time.time()
            try:
                step(db)
                print(f"warm-up {name}: {time.time() - began:.1f}s", flush=True)
            except Exception as exc:  # a warm-up failure must not stop the editor
                print(f"warm-up {name} failed: {exc}", flush=True)
    finally:
        if db is not None:
            db.close()
    print(f"warm-up finished in {time.time() - started:.1f}s", flush=True)


def conversation(guid: str) -> dict[str, Any] | None:
    found = conversations().get(str(guid or "").lower())
    if found is None:
        return None
    names = champion_names()
    return dict(found, champion_name=names.get(found["champion"], ""),
                endings=conversation_endings(found))


# --- database (read-only) -------------------------------------------------

def connect(db_path: Path) -> sqlite3.Connection:
    """Open the server database read-only; one connection per request."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                                 timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _zone_campaign_types(entry: dict[str, Any]) -> str:
    return {"overworld": "AREA", "dungeon": "DUNGEON",
            "panorama": "PANORAMA"}[entry["kind"]]


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def area_campaigns(db: sqlite3.Connection, zone_id: str = AREA_TEMPLATE
                   ) -> list[dict[str, Any]]:
    """Live campaigns showing this zone (dungeon rows matched by name)."""
    entry = zone(zone_id)
    rows = db.execute(
        "SELECT id, champion_id, champion_name, template_name, is_started, "
        "created_at FROM campaigns WHERE campaign_type=? ORDER BY id DESC",
        (_zone_campaign_types(entry),)).fetchall()
    wanted = entry.get("server_template")
    keys = {_compact(entry["id"]), _compact(entry.get("records") or "")}
    result = []
    for row in rows:
        name = row["template_name"]
        if entry["kind"] == "panorama":
            # A handoff panorama row names the overworld node it belongs to.
            state = _json(db.execute("SELECT state_json FROM campaigns WHERE id=?",
                                     (row["id"],)).fetchone()[0], {})
            public = (state.get("PublicState") or {}).get("Data") or {}
            if public.get("PanoramaNode") != entry.get("parent_node"):
                continue
        elif wanted is not None:
            if name != wanted:
                continue
        elif not any(key and (key in _compact(name) or _compact(name) in key)
                     for key in keys):
            continue
        result.append(dict(row))
    return result


def campaign_state(db: sqlite3.Connection, campaign_id: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT id, champion_id, champion_name, state_json FROM campaigns "
        "WHERE id=?", (int(campaign_id),)).fetchone()
    if row is None:
        return None
    state = _json(row["state_json"], {})
    nodes = {}
    for loc in state.get("VisLocs") or ():
        data = (loc or {}).get("Data") or {}
        node = data.get("node")
        if node:
            nodes[str(node)] = data
    public = (state.get("PublicState") or {}).get("Data") or {}
    quests = []
    for quest in db.execute(
            "SELECT id, template_name, state_json FROM campaigns "
            "WHERE champion_id=? AND campaign_type='QUEST' ORDER BY id",
            (row["champion_id"],)):
        quest_state = _json(quest["state_json"], {})
        flags = quest_state.get("Flags") or {}
        quests.append({
            "id": quest["id"], "script": quest["template_name"],
            "finished": bool(quest_state.get("Finished")),
            "objective_index": flags.get("_quest_objective_idx", 0)})
    return {
        "id": row["id"], "champion_id": row["champion_id"],
        "champion_name": row["champion_name"],
        "current_node": state.get("ALoc") or state.get("LastNode"),
        "nodes": nodes, "public": public,
        "flags": {key: value for key, value in (state.get("Flags") or {}).items()
                  if not str(key).startswith("_")},
        "quests": quests}


# --- combined payload -----------------------------------------------------


def _node_links(zone_id: str) -> dict[str, dict[str, Any]]:
    """Overworld node -> the dungeon/panorama it leads to (client POI)."""
    links = {}
    for entry in zones():
        if entry.get("parent") == zone_id and entry.get("parent_node"):
            links[entry["parent_node"]] = {"zone": entry["id"],
                                           "kind": entry["kind"],
                                           "title": entry["title"]}
    return links


POI_KINDS = {"POI_ShroomkinHaus": "shroom", "POI_GaalCamp": "camp"}


def _action_scene_names(data: dict[str, Any]) -> dict[str, str]:
    """Names of encounter scenes available in this zone or used by its actions."""
    names = encounter_scene_names()
    found = {}
    for scenes in (data.get("encounters") or {}).values():
        for scene in scenes:
            guid = str(scene.get("guid") or "").strip().lower()
            if guid and scene.get("battle", True):
                found[guid] = str(scene.get("name") or scene.get("title") or
                                  names.get(guid) or guid)
    for rows in data["node_conversations"].values():
        for row in rows:
            for actions in ((row["trigger"] or {}).get("actions") or {}).values():
                for action in actions:
                    match = re.fullmatch(r"\s*encounter\s*\((.+)\)\s*", str(action))
                    if match:
                        # encounter(SceneGuid[,ConversationGuid]): the first
                        # argument is the scene; the second is its opening mod.
                        guid = match.group(1).split(",", 1)[0].strip().lower()
                        found.setdefault(guid, names.get(guid, ""))
    return found


DECK_PERSONALITIES = ("Default", "Aggressive", "BigThreats", "BuildArmy", "Burn",
                      "HandAdvantage", "Reanimation")


def server_catalog(db, *args, **kwargs):
    """``campaign_runtime.catalog_data.server_catalog`` with the editor's
    conversation endings and hooks, including precomputed deck personalities."""
    kwargs.setdefault("ui", True)
    return _server_catalog(db, *args, **kwargs)


def zone_catalog(db, entry, authored=None):
    """Table rows for a zone, with the editor's extras (see server_catalog)."""
    return _catalog_data.zone_catalog(db, entry, authored, ui=True)


def rewards_catalog(db: sqlite3.Connection | None) -> dict[str, Any]:
    """Names and accessible text for the reward editor's pickers."""
    def plain_text(value: Any) -> str:
        text = html.unescape(str(value or ""))
        return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", text)).strip()

    chests = sorted(
        ({"guid": _guid(r.get("m_Id")), "name": str(r.get("m_Name") or "").strip()}
         for r in records("InventoryItemData")
         if str(r.get("_t", "")).endswith("InventoryTreasureChest")),
        key=lambda c: c["name"].lower())
    set_names = {_guid(r.get("m_Id")): str(r.get("m_Name") or "").strip()
                 for r in records("CardSetTemplate")}
    cards = []
    if db is not None:
        # Base cards only: the many "equipment modified" printings of a card
        # are hidden, and alternate-art printings are labelled.
        card_records = records("CardTemplate")
        alternate = {_guid(r.get("m_Id")) for r in card_records
                     if r.get("m_HasAlternateArt")}
        card_info = {_guid(r.get("m_Id")): {
                         "text": plain_text(r.get("m_GameText")),
                         "set_name": set_names.get(_guid(r.get("m_SetId")), "")}
                     for r in card_records}
        cards = [{"guid": row[0], "name": row[1], "rarity": row[2] or "",
                  "pve": bool(row[3]), "alt": row[0].lower() in alternate,
                  "text": card_info.get(row[0].lower(), {}).get("text", ""),
                  "set_name": card_info.get(row[0].lower(), {}).get("set_name", ""),
                  "label": row[1] + (" (alt-art)" if row[0].lower() in alternate else "")}
                 for row in db.execute(
                     "SELECT guid, name, rarity, is_pve FROM card_templates "
                     "WHERE name<>'' AND COALESCE(equipment_modified, 0)=0 "
                     "ORDER BY is_pve DESC, name")]
    equipment = sorted(
        ({"guid": _guid(r.get("m_Id")),
          "name": str(r.get("m_Name") or "").strip(),
          "text": plain_text(r.get("m_Description") or r.get("m_To")),
          "set_name": set_names.get(_guid(r.get("m_SetId")), "")}
         for r in records("InventoryItemData")
         if str(r.get("_t", "")).endswith("InventoryEquipmentData")),
        key=lambda e: e["name"].lower())
    art = {}
    art_path = GENERATED / "items" / "index.json"
    if art_path.exists():
        art = json.loads(art_path.read_text(encoding="utf-8"))
    card_art = {}
    card_art_path = GENERATED / "cards" / "index.json"
    if card_art_path.exists():
        card_art = json.loads(card_art_path.read_text(encoding="utf-8"))
    stardust = {str(r.get("m_Name") or "").split()[0].lower(): _guid(r.get("m_Id"))
                for r in records("InventoryItemData")
                if str(r.get("_t", "")).endswith("InventoryStardust")}
    mercenaries = sorted(
        ({"guid": _guid(r.get("m_Id")), "name": str(r.get("m_Name") or "").strip()}
         for r in records("InventoryItemData")
         if str(r.get("_t", "")).endswith("InventoryMercenaryData")),
        key=lambda m: m["name"].lower())
    sleeves = sorted(
        ({"guid": _guid(r.get("m_Id")), "name": str(r.get("m_Name") or "").strip()}
         for r in records("InventoryItemData")
         if str(r.get("m_Type") or "") == "DeckSleeve"),
        key=lambda m: m["name"].lower())
    return {"chests": [c for c in chests if c["name"]], "cards": cards,
            "art": art, "card_art": card_art, "stardust": stardust,
            "equipment": [e for e in equipment if e["name"]],
            "mercenaries": [m for m in mercenaries if m["name"]],
            "sleeves": [m for m in sleeves if m["name"]],
            "races": list(FACTION_OF_RACE)}


def quest_action_index(db: sqlite3.Connection | None
                       ) -> dict[str, dict[str, list[dict[str, str]]]]:
    """``{quest: {"start"|"complete": [{guid, name, template, node}]}}``
    across every zone: node conversations whose ending actions name the
    quest, and quest conversations (their role's default when no actions are
    saved). Editor saves in ``campaign_data/`` win over the DB rows."""
    rows: dict[tuple, dict[str, Any]] = {}

    def merge(kind, key, row, trigger_key):
        found = rows.setdefault((kind,) + key, {"kind": kind})
        for field in ("campaign_template", "node_id", "conversation_guid",
                      "conversation_name", "quest_script", "role"):
            if row.get(field) not in (None, ""):
                found[field] = row[field]
        if trigger_key in row:
            value = row[trigger_key]
            found["json"] = value if isinstance(value, dict) else _json(value, {})
        if "enabled" in row:
            found["enabled"] = bool(row["enabled"])

    if db is not None:
        for row in db.execute(
                "SELECT campaign_template, node_id, conversation_guid, "
                "conversation_name, trigger_json, enabled "
                "FROM campaign_node_conversations"):
            row = dict(zip(("campaign_template", "node_id", "conversation_guid",
                            "conversation_name", "trigger_json", "enabled"), row))
            merge("node", (row["campaign_template"],
                           str(row["conversation_guid"]).lower()), row, "trigger_json")
        for row in db.execute(
                "SELECT campaign_template, node_id, conversation_guid, "
                "conversation_name, quest_script, role, conditions_json, enabled "
                "FROM quest_conversations"):
            row = dict(zip(("campaign_template", "node_id", "conversation_guid",
                            "conversation_name", "quest_script", "role",
                            "conditions_json", "enabled"), row))
            merge("quest", (row["quest_script"],
                            str(row["conversation_guid"]).lower()), row, "conditions_json")
    for row in data_overrides("campaign_node_conversations"):
        merge("node", (row.get("campaign_template"),
                       str(row.get("conversation_guid") or "").lower()), row,
              "trigger_json")
    for row in data_overrides("quest_conversations"):
        merge("quest", (row.get("quest_script"),
                        str(row.get("conversation_guid") or "").lower()), row,
              "conditions_json")

    pattern = re.compile(r"^(start|complete)_?quest\((.+)\)$", re.I)
    index: dict[str, dict[str, list[dict[str, str]]]] = {}
    for row in rows.values():
        if row.get("enabled") is False:
            continue
        payload = row.get("json") if isinstance(row.get("json"), dict) else {}
        if row["kind"] == "quest":
            actions, _default = quest_actions(
                row.get("role", ""), row.get("quest_script", ""), payload)
        else:
            actions = payload.get("actions") or {}
        for action_list in (actions.values() if isinstance(actions, dict) else ()):
            for action in action_list or ():
                match = pattern.match(re.sub(r"\s", "", str(action)))
                if not match:
                    continue
                bucket = index.setdefault(match.group(2), {}).setdefault(
                    match.group(1).lower(), [])
                item = {"guid": row.get("conversation_guid", ""),
                        "name": row.get("conversation_name", ""),
                        "template": row.get("campaign_template", ""),
                        "node": row.get("node_id", "")}
                if not any(i["guid"].lower() == item["guid"].lower()
                           for i in bucket):
                    bucket.append(item)
    return index


def _default_visit_battles(authored: dict[str, Any],
                           encounters: dict[str, list[dict[str, Any]]]) -> None:
    """Show a node's first battle as its visit battle when the Records give it
    no visit battle of its own. The battle is the node's default (the first
    one in its list); this only changes what the editor displays."""
    for node_id, item in (authored or {}).items():
        visits = item.get("visit_events") or []
        if any("encounter" in str(v.get("type") or "").lower() for v in visits):
            continue
        first = next((e for e in encounters.get(node_id, []) if e.get("battle")), None)
        if first is None:
            continue
        item["visit_events"] = visits + [{
            "type": "UIEventBeginEncounter", "guid": first["guid"],
            "name": first["name"], "qualifiers": [], "default": True}]


def area_payload(db: sqlite3.Connection | None, zone_id: str = AREA_TEMPLATE
                 ) -> dict[str, Any]:
    import offers
    entry = zone(zone_id)
    map_path = GENERATED / zone_id / "map.json"
    if entry["kind"] == "panorama":
        return panorama_payload(db, entry)
    else:
        layout = json.loads(map_path.read_text(encoding="utf-8"))
    template = scene_template(entry["records"]) if entry.get("records") else None
    authored = _authored_items(template)
    data = zone_catalog(db, entry, authored)

    # NodeR -> Node00R is the overworld's spelling only (dungeons use NodeA..).
    canon = offers.canonical_node if entry["kind"] == "overworld" else (lambda n: n)

    def by_node(rows):
        merged: dict[str, list] = {}
        for node_id, items in rows.items():
            merged.setdefault(canon(node_id), []).extend(items)
        return merged

    node_conversations = by_node(data["node_conversations"])
    quest_links = by_node(data["quest_links"])
    # A panorama's NPC content is shown inside the panorama, not on its node.
    claims = offers.panorama_claims(zone_id) if entry["kind"] == "overworld" else {}
    for node_id, claim in claims.items():
        node_conversations[node_id] = [c for c in node_conversations.get(node_id, [])
                                       if not offers.claimed_by_panorama(c, claim)]
        quest_links[node_id] = [q for q in quest_links.get(node_id, [])
                                if not offers.claimed_by_panorama(q, claim)]
    encounters = by_node(data["encounters"])
    _default_visit_battles(authored, encounters)
    links = _node_links(zone_id)
    nodes = []
    for node in layout["nodes"]:
        node_id = node["id"]
        link = links.get(node_id)
        nodes.append(dict(
            node, authored=authored.get(node_id), link=link,
            poi_kind=(link or {}).get("kind") or POI_KINDS.get(node.get("poi"), ""),
            conversations=node_conversations.get(node_id, []),
            quest_links=quest_links.get(node_id, []),
            encounters=encounters.get(node_id, [])))
    return {
        "zone": entry, "zones": zones(),
        "campaign": entry.get("server_template") or entry["title"],
        "title": entry["title"], "scene_guid": _guid((template or {}).get("m_Id")),
        "keyword_names": champion_status_keywords(),
        "nodes": nodes, "paths": layout["paths"], "tiles": layout["tiles"],
        "emitters": layout.get("emitters") or [],
        "quests": data["quests"], "edges": data["edges"],
        "quest_actions": quest_action_index(db),
        "scene_names": _action_scene_names(data),
        "issues": _issues(layout, authored, data, node_conversations)}


def panorama_payload(db: sqlite3.Connection | None, entry: dict[str, Any]
                     ) -> dict[str, Any]:
    """A panorama: its scene file plus each NPC's conversations.

    NPC content comes from the parent overworld node's tables: quest
    conversations by their ``npc`` column, node conversations whose authored
    name mentions the NPC. A race starter panorama (``server_template`` AZ0)
    uses its own rows instead: ``node_id`` is the NPC and the race tag picks
    the panorama.
    """
    import offers
    scene_path = GENERATED / entry["id"] / "panorama.json"
    scene = (json.loads(scene_path.read_text(encoding="utf-8"))
             if scene_path.exists() else {"npcs": []})
    parent = zone(entry["parent"]) if entry.get("parent") else None
    data = _EMPTY_CATALOG
    own = entry.get("server_template") and (entry.get("race") or entry.get("npcs"))
    if own:
        data = zone_catalog(db, entry)
    elif db is not None and parent and parent["kind"] == "overworld":
        data = server_catalog(db, parent["server_template"], parent["scene_prefix"])
    node_id = entry.get("parent_node") or ""
    if own and entry.get("npcs"):
        # An AZ1 panorama: its NPCs are the rows' node ids.
        wanted = set(entry["npcs"])
        quest_rows = [dict(row, npc_key=_compact(raw.split("@")[0]))
                      for raw, rows in data["quest_links"].items() if raw in wanted
                      for row in rows]
        conv_rows = [dict(row, npc_key=_compact(raw.split("@")[0]))
                     for raw, rows in data["node_conversations"].items() if raw in wanted
                     for row in rows]
    elif own:
        quest_rows = []
        conv_rows = [dict(row, npc_key=_compact(raw))
                     for raw, rows in data["node_conversations"].items()
                     for row in rows if row["audience"]["race"] == entry["race"]]
    else:
        quest_rows = [row for raw, rows in data["quest_links"].items()
                      if offers.canonical_node(raw) == node_id for row in rows]
        conv_rows = [row for raw, rows in data["node_conversations"].items()
                     if offers.canonical_node(raw) == node_id for row in rows]
    npcs = []
    claimed = set()
    for npc in scene.get("npcs") or ():
        key = _compact(npc["name"])
        quests = [row for row in quest_rows if _compact(row["npc"]) == key]
        convs = [row for row in conv_rows if key and (
            row.get("npc_key") == key if own else key in _compact(row["name"]))]
        claimed.update(row["guid"] for row in quests + convs)
        npcs.append(dict(npc, quest_links=quests, conversations=convs))
    unassigned = [row for row in quest_rows + conv_rows
                  if row["guid"] not in claimed]
    return {"zone": entry, "zones": zones(), "title": entry["title"],
            "campaign": entry["title"], "nodes": [], "paths": [], "tiles": [],
            "keyword_names": champion_status_keywords(),
            "quests": data["quests"], "edges": [],
            "quest_actions": quest_action_index(db),
            "scene_names": _action_scene_names(data),
            "panorama": {"scene": f"{entry['id']}/panorama.json"
                         if scene_path.exists() else None,
                         "npcs": npcs, "unassigned": unassigned},
            "issues": [{"level": "info", "node": node_id,
                        "text": f"{len(unassigned)} {node_id} conversation(s) "
                        "name no NPC in this scene"}] if unassigned else []}


def _issues(layout, authored, catalog, node_conversations) -> list[dict[str, str]]:
    """Cross-checks between the client prefab, Records and the server DB."""
    issues = []
    layout_nodes = {node["id"] for node in layout["nodes"]}
    forks = {node["id"] for node in layout["nodes"]
             if node.get("fork") or node["id"].startswith("Fork")}
    for node in sorted(layout_nodes - set(authored)):
        if node not in forks:
            issues.append({"level": "warn", "node": node,
                           "text": "prefab node has no SceneData entry"})
    for node in sorted(set(authored) - layout_nodes):
        issues.append({"level": "info", "node": node,
                       "text": "SceneData node is not placed in the prefab"})
    if catalog["edges"]:
        prefab_edges = {frozenset((p["from"], p["to"])) for p in layout["paths"]}
        db_edges = {frozenset((e["from_node"], e["to_node"]))
                    for e in catalog["edges"]}
        for edge in sorted(db_edges - prefab_edges, key=sorted):
            issues.append({"level": "warn", "node": sorted(edge)[0],
                           "text": "server edge %s-%s is not a prefab path"
                           % tuple(sorted(edge))})
        for edge in sorted(prefab_edges - db_edges, key=sorted):
            issues.append({"level": "warn", "node": sorted(edge)[0],
                           "text": "prefab path %s-%s is missing from "
                           "campaign_node_edges" % tuple(sorted(edge))})
    known = set(conversations())
    for node, entries in node_conversations.items():
        if node not in layout_nodes:
            issues.append({"level": "warn", "node": node,
                           "text": "conversations are mapped to a node that "
                           "is not on the map"})
        for entry in entries:
            if entry["guid"] not in known:
                issues.append({"level": "error", "node": node,
                               "text": f"conversation {entry['guid']} is not "
                               "in Records"})
    for node, item in authored.items():
        has_rows = bool(node_conversations.get(node))
        for event in item["visit_events"]:
            if not event["name"]:
                issues.append({
                    "level": "info" if has_rows else "warn", "node": node,
                    "text": f"SceneData visit event {event['type']} "
                    f"{event['guid']} is a stale reference (not in Records)" +
                    (f"; the node's conversations come from its "
                     f"{len(node_conversations[node])} table rows"
                     if has_rows else "")})
    return issues


# --- rules / simulator ----------------------------------------------------

RULES_DIR = CAMPAIGN_ROOT / "campaign_rules"
RACES = {1: "Human", 2: "Elf", 3: "Coyotle", 4: "Orc", 5: "Dwarf",
         6: "Shin'hare", 7: "Vennen", 8: "Necrotic"}
FACTION_OF_RACE = {"Human": "Ardent", "Elf": "Ardent", "Coyotle": "Ardent",
                   "Orc": "Ardent", "Dwarf": "Underworld",
                   "Shin'hare": "Underworld", "Vennen": "Underworld",
                   "Necrotic": "Underworld"}
_RULES_LOCK = threading.Lock()
_RULES_CACHE: dict[str, dict[str, Any]] = {}


def _rules_module():
    import sys
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import campaign_rules
    return campaign_rules


def rules_path(zone_id: str) -> Path:
    return RULES_DIR / f"{zone_id}.json"


def rules(db: sqlite3.Connection | None, zone_id: str = AREA_TEMPLATE
          ) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Rules for evaluation, exactly as the server's campaign runtime builds
    them (``campaign_runtime.rules_loader``), plus ``state_tags``: each node
    state as an editable tag list (None: needs any/nesting).
    """
    built, problems = rules_loader.rules(db, zone_id)
    if built is None:
        return None, []
    engine = _rules_module()
    with _RULES_LOCK:
        cached = _RULES_CACHE.get(zone_id)
        if cached is not None and cached["source"] is built:
            return cached["rules"], problems
    loaded = json.loads(rules_path(zone_id).read_text(encoding="utf-8"))
    merged = dict(built, state_tags={
        node_id: {gate: engine.condition_tags(spec[gate])
                  if not isinstance(spec[gate], list) else spec[gate]
                  for gate in engine.GATES if gate in spec}
        for node_id, spec in (loaded.get("nodes") or {}).items()})
    with _RULES_LOCK:
        _RULES_CACHE[zone_id] = {"source": built, "rules": merged}
    return merged, problems


def _positive_flags(condition):
    """Flags a condition reads positively (not under a ``not``)."""
    if not isinstance(condition, dict):
        return []
    if "flag" in condition:
        return [condition["flag"]]
    found = []
    for key in ("all", "any"):
        for child in condition.get(key) or ():
            found.extend(_positive_flags(child))
    return found


def _infer_completion_flags(progress: dict[str, Any], zone_id: str) -> None:
    """A node the live save completed satisfies its ``completed`` gate.

    The live save has no rules flags, so for each such node set the first
    flag the gate reads that makes it hold (taming flags, Masked Army).
    """
    engine = _rules_module()
    file_rules = json.loads(rules_path(zone_id).read_text(encoding="utf-8"))
    for node_id, spec in (file_rules.get("nodes") or {}).items():
        gate = spec.get("completed")
        if gate is None or not (progress["nodes"].get(node_id) or {}).get("completed"):
            continue
        if engine.evaluate(gate, progress):
            continue
        for flag in _positive_flags(gate):
            trial = dict(progress, flags=sorted(set(progress["flags"]) | {flag}))
            if engine.evaluate(gate, trial):
                progress["flags"] = trial["flags"]
                break


def sim_state_from_campaign(db: sqlite3.Connection, campaign_id: int,
                            zone_id: str = AREA_TEMPLATE) -> dict[str, Any]:
    """Map a champion's live campaign onto the rules progress state.

    Visits, completion, retry and failure come from ``VisLocs`` and
    ``PublicState``; quests from the champion's QUEST rows. The live state's
    gate lists (``blocked_nodes`` ...) are deliberately ignored: in the
    rules model those are derived from quest progress and flags.
    """
    row = db.execute(
        "SELECT c.champion_id, c.state_json, ch.race FROM campaigns c "
        "LEFT JOIN champions ch ON ch.id = c.champion_id WHERE c.id=?",
        (int(campaign_id),)).fetchone()
    if row is None:
        raise LookupError("no such campaign")
    state = _json(row["state_json"], {})
    public = (state.get("PublicState") or {}).get("Data") or {}
    visits = dict(public.get("conversation_visits") or {})
    failed = set(public.get("failed_nodes") or ())
    titles = {}
    nodes = {}
    for loc in state.get("VisLocs") or ():
        data = (loc or {}).get("Data") or {}
        node = data.get("node")
        if not node:
            continue
        titles[str(data.get("title") or data.get("name") or "")] = node
        nodes[node] = {"completed": bool(data.get("completed")),
                       "retryable": bool(data.get("repeatable") and
                                         not data.get("autostart")),
                       "failed": node in failed, "visits": 0}
    for value in public.get("visited_nodes") or ():
        node = titles.get(str(value), str(value))
        if node in nodes:
            nodes[node]["visits"] = max(1, int(visits.get(node, 1) or 1))
    nodes = {k: v for k, v in nodes.items()
             if v["visits"] or v["completed"] or v["failed"]}
    quests = {}
    for quest in db.execute(
            "SELECT template_name, state_json FROM campaigns "
            "WHERE champion_id=? AND campaign_type='QUEST' ORDER BY id",
            (row["champion_id"],)):
        quest_state = _json(quest["state_json"], {})
        flags = quest_state.get("Flags") or {}
        quests[quest["template_name"]] = {
            "status": "finished" if quest_state.get("Finished") else "active",
            "step": int(flags.get("_quest_objective_idx", 0) or 0)}
    race = RACES.get(int(row["race"] or 0), "")
    flags = {"blockade_cleared"} if public.get("blockade_cleared") else set()
    flags |= {str(f) for f in public.get("conversation_flags") or ()}
    # The same flags the server's flag(X) tags read: the area's boolean
    # markers (gaal_fortune, blockade_cleared, ...).
    flags |= {str(k) for k, v in public.items() if v is True}
    file_rules = _json(rules_path(zone_id).read_text(encoding="utf-8"), {})
    start_counters = (file_rules.get("start") or {}).get("counters") or {}
    counters = {str(name): int(value or 0)
                for name, value in start_counters.items()}
    counters.update(public.get("counters") or {})
    engine = _rules_module()
    keywords = engine._keyword_names(
        public.get("keywords") or public.get("champion_keywords"))
    progress = {"flags": sorted(flags), "faction": FACTION_OF_RACE.get(race, ""),
                "race": race, "quests": quests, "nodes": nodes,
                "counters": counters, "keywords": keywords}
    _infer_completion_flags(progress, zone_id)
    current = state.get("ALoc") or state.get("LastNode") or ""
    return {"flags": progress["flags"], "faction": FACTION_OF_RACE.get(race, ""),
            "race": race, "current": titles.get(current, current),
            "quests": quests, "nodes": nodes, "counters": counters,
            "keywords": keywords,
            "visited_paths": list(public.get("visited_paths") or ())}
