"""The campaign's table data per zone (shared by the server's campaign
runtime and the campaign editor).

Rows come from the server catalogue tables, overridden by the shared authored
``campaign/campaign_data/*.json`` files, and dungeon scenes from
``Records``. Zone metadata is
``campaign/campaign_rules/zones.json``.
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from campaign_runtime import offers
from campaign_runtime.records import (
    CAMPAIGN_ROOT, _endings_of, _guid, _qualifiers, _records_encounters, _scene_kind,
    _short_type, champion_names, conversation_hooks, conversations,
    encounter_scene_names, quest_templates, records, scene_template)

AREA_TEMPLATE = "AZ1"
RULES_DIR = CAMPAIGN_ROOT / "campaign_rules"
ZONES_PATH = RULES_DIR / "zones.json"
_ZONES_LOCK = threading.Lock()
_ZONES: tuple[float, list[dict[str, Any]]] | None = None


def _query(db, sql, params=()):
    """Rows addressable by column name on any connection (the server's
    shared connection returns plain tuples)."""
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, params)


def zones() -> list[dict[str, Any]]:
    """Every campaign zone (overworlds, dungeons, panoramas): id, kind,
    parent/parent_node, server_template and scene matching."""
    global _ZONES
    mtime = ZONES_PATH.stat().st_mtime
    with _ZONES_LOCK:
        if _ZONES is None or _ZONES[0] != mtime:
            _ZONES = (mtime, json.loads(ZONES_PATH.read_text(encoding="utf-8"))["zones"])
        return _ZONES[1]


def zone(zone_id: str) -> dict[str, Any]:
    for entry in zones():
        if entry["id"] == zone_id:
            return entry
    raise LookupError(f"unknown zone {zone_id!r}")


def resolve_zone_name(name: str, *, kind: str | None = None
                      ) -> dict[str, Any] | None:
    """Resolve a debug-console name to its authored zone entry.

    The client accepts campaign titles, while server rows store
    ``server_template``. Also accept the stable zone id and Records name so
    ``camp.dungeon NAME`` can use any of those names without a card/dungeon
    specific branch.
    """
    wanted = _norm(name)
    if not wanted:
        return None

    def forms(value: Any) -> set[str]:
        normalized = _norm(value)
        if not normalized:
            return set()
        return ({normalized[4:]} if normalized.startswith("the ") else
                {normalized, f"the {normalized}"})

    wanted_forms = forms(wanted)
    matches = []
    for entry in zones():
        if kind and str(entry.get("kind") or "").casefold() != kind.casefold():
            continue
        names = (entry.get("id"), entry.get("title"), entry.get("records"),
                 entry.get("server_template"))
        if any(wanted_forms & forms(value) for value in names if value):
            matches.append(entry)
    return matches[0] if len(matches) == 1 else None


_NODE_IN_SCENE = re.compile(r"\bNODE\s*-?\s*([A-Z]?[0-9]+|[A-Z])([A-Z])?\b", re.I)


def scene_node_id(scene_name: str) -> str | None:
    """``AZ 1 - NODE 23B - ...`` -> ``Node023`` (variant letter dropped);
    ``AZ 2 - NODE B18 - ...`` -> ``NodeB18`` (lettered sea nodes)."""
    match = _NODE_IN_SCENE.search(scene_name or "")
    if not match:
        return None
    token = match.group(1).upper()
    if token.isdigit():
        return f"Node{int(token):03d}"
    if token[1:].isdigit():
        return f"Node{token[0]}{int(token[1:]):02d}"
    return f"Node00{token}"


def _battle_tags_of(value):
    """Scene tags as saved: a tag list, or "or" groups (a list of tag lists)."""
    from campaign_rules import is_groups
    if is_groups(value):
        return [[str(t).strip() for t in g if str(t).strip()] for g in value
                if any(str(t).strip() for t in g)]
    return [str(t).strip() for t in (value or ()) if str(t).strip()]


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


DATA_DIR = CAMPAIGN_ROOT / "campaign_data"


def data_overrides(table: str) -> list[dict[str, Any]]:
    """Rows authored in the shared campaign files for ``table``."""
    rows = []
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data = data or {}
        rows.extend(r for r in (data.get("tables") or {}).get(table) or ()
                    if isinstance(r, dict))
    return rows


def data_mtime() -> float:
    return max((p.stat().st_mtime for p in DATA_DIR.glob("*.json")), default=0.0)


def _audience(name: str, tags: Any = None, faction: str = "") -> dict[str, str]:
    """Who a row is for: its race/faction from positive tags, else the
    authored name (``... - Ardent - ...``, ``... - Quest End - Dwarf``)."""
    race, side = "", faction or ""
    groups = tags if tags and all(isinstance(g, list) for g in tags) else [tags or []]
    if len(groups) == 1:
        for tag in groups[0]:
            match = re.fullmatch(r"\s*(ChampionIsRace|faction)\s*\((.+)\)\s*",
                                 str(tag), re.I)
            if match and match.group(1).lower() == "faction":
                side = match.group(2).strip()
            elif match:
                race = match.group(2).strip()
    race = race or offers.race_of(name)
    side = side or offers.faction_of(name)
    return {"race": race, "faction": side}


def _quest_conditions(row, saved: dict[str, Any] | None) -> dict[str, Any]:
    """A quest row's conditions, with saved-but-not-applied editor tags."""
    conditions = _json(row["conditions_json"], {})
    if saved and "conditions_json" in saved:
        value = saved["conditions_json"]
        conditions = value if isinstance(value, dict) else _json(value, {})
    return conditions if isinstance(conditions, dict) else {}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", str(text or "").lower())).strip()


def item_title(item_name: str) -> str:
    """A dungeon item's name without its ``NNN - `` / ``F - `` prefix."""
    return re.sub(r"^(\d+|[A-Z])\s*-\s*", "", str(item_name or "")).strip()


def dungeon_prefixes(records_name: str) -> list[str]:
    """Names a dungeon's conversations/scenes start with: ``The Usurper`` ->
    ``The Usurper``, ``Usurper``; ``The Kraken`` also ``Kraken's Gold``."""
    name = str(records_name or "")
    bare = re.sub(r"^The\s+", "", name)
    return [p for p in dict.fromkeys([name, bare]) if p]


def match_item(name: str, items: dict[str, str], prefixes: list[str]
               ) -> tuple[str | None, str]:
    """``(node, rest)``: the dungeon node a conversation/scene is named after.

    The name must start with one of the dungeon's ``prefixes``; the node is
    the longest node title it contains ("Entrance to the Burial Vault" ->
    Burial Vault); ``rest`` is what follows it ("Success", "Easy", ...).
    """
    lowered = _norm(name)
    if not any(lowered.startswith(_norm(p)) for p in prefixes):
        return None, ""
    # Lettered nodes ("F - East Shore") are also named by letter in scene
    # names ("Kraken's Gold F - Crustac Ambush"). Some dungeons use only the
    # letter ("Devonshire Keep I") or append a form number ("Devonshire Keep
    # H1" / "H2").
    for prefix in prefixes:
        rest = lowered[len(_norm(prefix)):] if lowered.startswith(_norm(prefix)) else ""
        lettered = re.match(
            r"^(?:'s [a-z' ]+?)?\s*\b([a-z])(\d+)?(?:\s+(.*))?$", rest)
        if lettered:
            letter = lettered.group(1)
            suffix = " ".join(part for part in lettered.group(2, 3) if part)
            for node, title in items.items():
                if re.match(rf"^{letter}\s*-", str(title or ""), re.I):
                    return node, suffix.strip(" -:()")
            # Some maps number item names while keeping alphabetic node IDs
            # (e.g. NodeI -> "010 - Minstrel's Gallery").
            node_id = next((node for node in items
                            if str(node).lower() == f"node{letter}"), None)
            if node_id is not None:
                return node_id, suffix.strip(" -:()")
    best = None
    for node, title in items.items():
        key = _norm(item_title(title))
        if len(key) < 3 or key in ("node cut",):
            continue
        # Conversation/scene names sometimes omit a leading article from a
        # node title ("Defense of Fort Romor" for "The Defense of Fort Romor").
        variants = [key[4:]] if key.startswith("the ") else []
        for candidate in [key, *variants]:
            at = lowered.find(candidate)
            if at >= 0 and (best is None or len(candidate) > len(best[1])):
                best = (node, candidate, at)
    if best is None:
        return None, ""
    node, key, at = best
    return node, lowered[at + len(key):].strip(" -:()")


def _prefix_re(scene_prefix: str) -> re.Pattern[str]:
    """Matches a zone's scene prefix with any spacing: ``AZ 2 - NODE`` also
    matches the ``AZ2 - NODE 34A`` spelling some Records scenes use."""
    return re.compile(r"^\s*" + r"\s*".join(
        re.escape(part) for part in scene_prefix.split()), re.I)


def _scene_node(name: str, scene_prefix: str, scene_regex: str | None,
                item_names: dict[str, str]) -> tuple[str | None, str]:
    """``(node, race)`` for an encounter scene name in this zone."""
    if not scene_regex:
        return (scene_node_id(name) if _prefix_re(scene_prefix).match(name or "")
                else None), ""
    match = re.match(scene_regex, name or "")
    if not match:
        return None, ""
    groups = match.groupdict()
    title = str(groups.get("node") or "").strip().lower()
    return item_names.get(title), str(groups.get("race") or "")


def quest_actions(role: str, quest: str, conditions: dict[str, Any]
                  ) -> tuple[dict[str, list[str]], bool]:
    """``(actions, is_default)`` a quest conversation runs when it ends.

    Saved ``conditions_json.actions`` win (an empty table means none);
    otherwise a start conversation starts its quest and a completing one
    completes it.
    """
    saved = (conditions or {}).get("actions")
    if isinstance(saved, dict):
        return saved, False
    default = {"start": f"start_quest({quest})",
               "complete": f"complete_quest({quest})"}.get(role)
    return ({"*": [default]} if default else {}), True


def server_catalog(db: sqlite3.Connection, template: str = AREA_TEMPLATE,
                   scene_prefix: str = "AZ 1 - NODE",
                   scene_regex: str | None = None,
                   authored: dict[str, dict[str, Any]] | None = None,
                   scene_match: str | None = None, *, ui: bool = False,
                   personality=None) -> dict[str, Any]:
    """The server's catalogue rows for one campaign template.

    Encounters are matched to nodes by scene name: ``AZ 1 - NODE 23 ...`` for
    overworlds, or a zone's ``scene_regex`` (named groups ``node`` - a Records
    item name - and ``race``) for per-race dungeons. ``ui`` adds what only the
    editor shows (conversation endings/hooks); ``personality(db, deck)`` the
    deck evaluator's AI personality pick.
    """
    names = champion_names()
    # Saved data-file rows override this read-only DB projection; startup
    # applies those rows to the database as well.
    pending = {(r.get("campaign_template"), str(r.get("conversation_guid") or "").lower()): r
               for r in data_overrides("campaign_node_conversations")}
    node_conversations: dict[str, list[dict[str, Any]]] = {}
    db_rows = [dict(row) for row in _query(db,
        "SELECT node_id, conversation_guid, conversation_name, "
        "trigger_json, priority, enabled FROM campaign_node_conversations "
        "WHERE campaign_template=? ORDER BY node_id, priority, "
        "conversation_name", (template,))]
    seen = {str(r["conversation_guid"] or "").lower() for r in db_rows}
    # Rows a data file inserts (e.g. import_crayburn.py) appear at startup.
    db_rows += [dict(r, trigger_json=r.get("trigger_json") or {}, pending=True)
                for (t, guid), r in pending.items()
                if t == template and guid not in seen and r.get("node_id")]
    for row in db_rows:   # saved campaign rows win until the DB is refreshed
        saved = pending.get((template, str(row["conversation_guid"] or "").lower())) or {}
        for column in ("node_id", "conversation_name", "priority", "enabled"):
            if column in saved:
                row[column] = saved[column]
    for row in sorted(db_rows, key=lambda r: (str(r["node_id"]), r.get("priority") or 0,
                                               str(r.get("conversation_guid") or ""))):
        guid = str(row["conversation_guid"] or "").lower()
        override = pending.get((template, guid)) or {}
        trigger = override.get("trigger_json", row["trigger_json"])
        trigger = trigger if isinstance(trigger, dict) else _json(trigger, {})
        name = row.get("conversation_name") or (conversations().get(guid) or {}).get("name", "")
        node_conversations.setdefault(row["node_id"], []).append({
            "guid": guid, "name": name,
            "template": template, "edited": "trigger_json" in override,
            "pending": bool(row.get("pending")),
            "trigger": trigger, "audience": _audience(name, trigger.get("tags")),
            "priority": row.get("priority"), "enabled": bool(row.get("enabled", True)),
            "hooks": conversation_hooks(guid) if ui else {},
            "endings": _endings_of(guid) if ui else []})
    quest_links: dict[str, list[dict[str, Any]]] = {}
    pending_quests = {(r.get("quest_script"), str(r.get("conversation_guid") or "").lower(),
                       r.get("campaign_template")): r
                      for r in data_overrides("quest_conversations")}
    for row in _query(db,
            "SELECT quest_script, conversation_guid, node_id, npc, role, "
            "faction, conversation_name, start_hook, conditions_json, "
            "priority, enabled FROM quest_conversations "
            "WHERE campaign_template=? ORDER BY node_id, priority", (template,)):
        conditions = _quest_conditions(row, pending_quests.get(
            (row["quest_script"], str(row["conversation_guid"] or "").lower(), template)))
        actions, actions_default = quest_actions(
            row["role"], row["quest_script"], conditions)
        saved_row = pending_quests.get(
            (row["quest_script"], str(row["conversation_guid"] or "").lower(), template)) or {}
        node_of_row = saved_row.get("node_id") or row["node_id"] or ""
        quest_links.setdefault(node_of_row, []).append({
            "quest": row["quest_script"],
            "trigger": dict({"actions": actions},
                            **({"tags": conditions["tags"]} if conditions.get("tags") else {})),
            "actions_default": actions_default,
            "guid": str(row["conversation_guid"] or "").lower(),
            "name": row["conversation_name"], "npc": row["npc"],
            "role": row["role"], "faction": row["faction"],
            "start_hook": row["start_hook"] or "",
            "conditions": conditions,
            "template": template,
            "enabled": bool(row["enabled"]),
            "audience": _audience(row["conversation_name"], None, row["faction"]),
            "endings": _endings_of(row["conversation_guid"]) if ui else []})
    # Saved-but-not-yet-applied scene rewards win (later files override).
    pending_scene_rewards, pending_scene_actions = {}, {}
    pending_scene_mods: dict[str, Any] = {}
    pending_personality: dict[str, str] = {}
    # Editor tags that make a battle conditional (see offers.battle_offers):
    # the same condition grammar as conversation tags, e.g. "flag(x)".
    pending_scene_tags: dict[str, list[str]] = {}
    pending_personality_default: dict[str, str] = {}
    pending_personality_overrides: set[str] = set()
    for r in data_overrides("encounter_scenes"):
        guid = str(r.get("guid") or "").lower()
        if "mods_json" in r:
            value = r["mods_json"]
            pending_scene_mods[guid] = (
                _json(value, []) if isinstance(value, str) else value)
        default_personality = str(r.get("ai_deck_personality") or "").strip()
        override = str(r.get("ai_deck_personality_override") or "").strip()
        if default_personality:
            pending_personality_default[guid] = default_personality
            pending_personality[guid] = default_personality
        if override:
            pending_personality[guid] = override
            pending_personality_overrides.add(guid)
        if "scene_tags" in r:
            pending_scene_tags[guid] = _battle_tags_of(r["scene_tags"])
        for column, target in (("rewards_json", pending_scene_rewards),
                               ("actions_json", pending_scene_actions)):
            if column in r:
                value = r[column]
                target[guid] = (
                    _json(value, {}) if isinstance(value, str) else value)
    encounters: dict[str, list[dict[str, Any]]] = {}
    item_scene_match = scene_match in ("items", "items_with_event_fallback")
    linked_encounter_nodes = (encounter_event_nodes(authored)
                              if scene_match == "items_with_event_fallback" else {})
    if item_scene_match:
        # Dungeon battles are not in the server's encounter_scenes table (it
        # runs only Crayburn): read the Records scenes named after each node.
        items = {n: i["name"] for n, i in (authored or {}).items()}
        prefixes = dungeon_prefixes(template)
        for scene in _records_encounters():
            node, variant = match_item(scene["name"], items, prefixes)
            nodes = [node] if node else linked_encounter_nodes.get(scene["guid"], [])
            for node_id in nodes:
                encounters.setdefault(node_id, []).append(dict(scene, variant=variant))
        scene_regex, scene_prefix = None, "\x00"     # skip the table below
    item_names = {re.sub(r"^\d+\s*-\s*", "", item["name"]).strip().lower(): node
                  for node, item in (authored or {}).items()}
    for row in _query(db,
            "SELECT guid, name, title, ai_champion_guid, ai_deck_guid, "
            "mods_json, rewards_json, actions_json, ai_deck_personality, "
            "player_champion_guid, player_deck_guid, gameboard "
            "FROM encounter_scenes "
            "WHERE name LIKE ? ORDER BY name",
            ("%",)):    # the prefix is matched in _scene_node (any spacing)
        variant = ""
        if item_scene_match:
            # Dungeon scenes are named after their node (+ a difficulty/form).
            node, variant = match_item(
                row["name"], {n: i["name"] for n, i in (authored or {}).items()},
                dungeon_prefixes(template))
            race = ""
            nodes = ([node] if node else
                     linked_encounter_nodes.get(str(row["guid"] or "").lower(), []))
        else:
            node, race = _scene_node(row["name"], scene_prefix, scene_regex, item_names)
            nodes = [node] if node else []
        champion = str(row["ai_champion_guid"] or "").lower()
        battle = bool(champion.strip("0-"))
        for node_id in nodes:
            guid = str(row["guid"] or "").lower()
            default_personality = pending_personality_default.get(
                guid, str(row["ai_deck_personality"] or ""))
            encounters.setdefault(node_id, []).append({
                "guid": guid,
                "name": row["name"], "title": row["title"], "champion": champion,
                "champion_name": names.get(champion, ""),
                # DIALOG scenes share the table but have no opponent.
                "battle": battle, "kind": _scene_kind(row["name"], battle),
                "race": race, "variant": variant,
                "deck": row["ai_deck_guid"], "mods": pending_scene_mods.get(
                    str(row["guid"] or "").lower(), _json(row["mods_json"], [])),
                "rewards": pending_scene_rewards.get(
                    str(row["guid"] or "").lower(), _json(row["rewards_json"], {})),
                "actions": pending_scene_actions.get(
                    str(row["guid"] or "").lower(), _json(row["actions_json"], {})),
                "ai_deck": row["ai_deck_guid"] or "",
                "ai_personality": pending_personality.get(
                    guid, default_personality),
                "ai_personality_default": default_personality,
                "ai_auto": (personality(db, row["ai_deck_guid"])
                            if personality and battle and row["ai_deck_guid"]
                            else None),
                "ai_overridden": guid in pending_personality_overrides,
                "player_champion": str(row["player_champion_guid"] or ""),
                "player_deck": str(row["player_deck_guid"] or ""),
                "gameboard": str(row["gameboard"] or ""),
                "tags": pending_scene_tags.get(guid, [])})
    for scenes in encounters.values():
        for scene in scenes:
            scene.setdefault("tags", pending_scene_tags.get(
                str(scene.get("guid") or "").lower(), []))
    # A dungeon scene can come from both the Records and the server table;
    # keep the table row (it carries the rewards, actions and AI personality).
    for node_id, scenes in encounters.items():
        by_guid: dict[str, dict[str, Any]] = {}
        for scene in scenes:
            by_guid[str(scene.get("guid") or id(scene)).lower()] = scene
        encounters[node_id] = list(by_guid.values())
    quests = []
    records_by_script = {q["script"]: q for q in quest_templates()}
    # Saved-but-not-yet-applied quest rewards win, like the conversation rows.
    pending_rewards = {r.get("script_name"): r.get("rewards_json")
                       for r in data_overrides("quest_templates")
                       if "rewards_json" in r}
    for row in _query(db,
            "SELECT script_name, name, title, objectives_json, "
            "campaign_group, enabled, start_hook, rewards_json "
            "FROM quest_templates ORDER BY script_name"):
        rewards = pending_rewards.get(row["script_name"])
        if rewards is None:
            rewards = _json(row["rewards_json"], {})
        elif isinstance(rewards, str):
            rewards = _json(rewards, {})
        quests.append({
            "rewards": rewards,
            "script": row["script_name"], "title": row["title"],
            "group": row["campaign_group"], "enabled": bool(row["enabled"]),
            "start_hook": row["start_hook"] or "",
            "objectives": _json(row["objectives_json"], []),
            "in_records": row["script_name"] in records_by_script})
    edges = [dict(row) for row in _query(db,
        "SELECT from_node, to_node, path_name FROM campaign_node_edges "
        "WHERE campaign_template=?", (template,))]
    return {"node_conversations": node_conversations,
            "quest_links": quest_links, "encounters": encounters,
            "quests": quests, "edges": edges}


_EMPTY_CATALOG = {"node_conversations": {}, "quest_links": {},
                  "encounters": {}, "quests": [], "edges": []}


def _map_node_ids(records_name: str) -> set[str]:
    """The node ids on a zone's map: its rules file's ``map.paths`` ends."""
    for entry in zones():
        if entry.get("records") == records_name:
            path = RULES_DIR / f"{entry['id']}.json"
            try:
                paths = (json.loads(path.read_text(encoding="utf-8"))
                         .get("map") or {}).get("paths") or ()
            except (OSError, ValueError):
                return set()
            return {p[end] for p in paths for end in ("from", "to") if p.get(end)}
    return set()


def _item_map_node(item: dict[str, Any], map_nodes: set[str],
                   claimed: set[str]) -> str:
    """The map node a SceneData item belongs to.

    Usually its ``m_MapNodeId``. Some dungeons (Fort Romor: ``Stables``,
    ``CmdPost``) name the item instead of the map node; their numbered item
    names follow the map's order as in every other dungeon - ``001`` is
    Node001, ``002`` NodeA, ``003`` NodeB ... - so place it by that number
    when the map has that node and no item names it explicitly."""
    node = str(item.get("m_MapNodeId") or "")
    if not map_nodes or node in map_nodes:
        return node
    match = re.match(r"\s*(\d+)\s*-", str(item.get("m_Name") or ""))
    if match:
        number = int(match.group(1))
        guess = "Node001" if number == 1 else (
            f"Node{chr(ord('A') + number - 2)}" if 2 <= number <= 27 else "")
        if guess in map_nodes and guess not in claimed:
            return guess
    return node


def _authored_items(template: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    conversation_names = {g: c["name"] for g, c in conversations().items()}
    scene_names = encounter_scene_names()
    authored = {}
    items = (template or {}).get("m_ItemData") or ()
    map_nodes = _map_node_ids(str((template or {}).get("m_Name") or ""))
    claimed = {str(i.get("m_MapNodeId") or "") for i in items} & map_nodes
    for item in items:
        node = _item_map_node(item, map_nodes, claimed)
        if not node:
            continue
        visits = []
        for event in item.get("m_EventsToFireWhenVisited") or ():
            kind = _short_type(event.get("_t"))
            guid = _guid(event.get("m_ConversationId") or event.get("m_EncounterId"))
            visits.append({
                "type": kind, "guid": guid,
                "name": (conversation_names.get(guid) if "Conversation" in kind
                         else scene_names.get(guid)) or "",
                "qualifiers": _qualifiers(event.get("m_Qualifiers"))})
        authored[node] = {
            "name": item.get("m_Name", ""), "title": item.get("m_Title", ""),
            "terrain": item.get("m_TerrainType", ""),
            "totem": item.get("m_TotemType", ""),
            "art": item.get("m_NodeScapeArtNumber", ""),
            "visit_events": visits,
            "descriptions": [{
                "type": d.get("m_Type", ""), "text": d.get("m_Description", ""),
                "qualifiers": _qualifiers(d.get("m_Qualifiers"))}
                for d in item.get("m_Descriptions") or ()]}
    # A battle that SCENE_NODE_LINKS places at another node was recorded on the
    # wrong item's visit events: move it, so each node shows its own battle.
    for node, item in list(authored.items()):
        kept = []
        for event in item["visit_events"]:
            target = SCENE_NODE_LINKS.get(str(event.get("guid") or "").lower())
            if ("encounter" in str(event.get("type") or "").lower() and target
                    and target != node and target in authored):
                authored[target]["visit_events"].append(event)
            else:
                kept.append(event)
        item["visit_events"] = kept
    return authored


@functools.lru_cache(maxsize=32)
def _visit_conversations(template_name: str) -> dict[str, str]:
    """Map node -> the conversation its visit events begin (first one per node)."""
    out: dict[str, str] = {}
    for node, item in _authored_items(scene_template(template_name)).items():
        for event in item.get("visit_events") or ():
            if "conversation" in str(event.get("type") or "").lower() and event.get("guid"):
                out.setdefault(node, str(event["guid"]).lower())
    return out


def entrance_visit_conversation(entry: dict[str, Any], node: str) -> str | None:
    """The conversation a dungeon's map node plays when first visited (Records
    ``m_EventsToFireWhenVisited``), or None."""
    if entry.get("kind") != "dungeon" or not entry.get("records") or not node:
        return None
    return _visit_conversations(str(entry["records"])).get(str(node))

# Records visit-event battle links that are mis-wired, corrected from the
# Underworld playthrough (playlist PL5KcPcNiQlz7KYBS72s7qYx2vxvMoDyVn, videos
# 028-030): Fort Romor's Stables / Barracks / Command Post items start each
# other's battles. Scene GUID -> the node its battle is fought at.
SCENE_NODE_LINKS = {
    # The Command Post (NodeD): "...walk brashly into the command center"
    "728ab36c-5738-463b-b90a-604c7e06cf1e": "NodeD",   # Commander Hawthorne
    # The Barracks (NodeC, the tower): "The barracks is the largest building"
    "f129c290-db71-42b2-bcaa-5ff16f5e3a14": "NodeC",   # Rhonwen
    # The Stables (NodeB, the east shed)
    "baaec0e7-b30d-4a3d-a968-8cddbe5eba18": "NodeB",   # Legionnaire of Gawaine
    # The Tranquil Dream: each map item's visit event named the next item's
    # battle. The monster's own conversation (The Dream Turtle, Bear, Skarn,
    # Spider) starts it at the right node.
    "04c13b89-2b48-43e3-83b2-c359982a1c7a": "NodeB",   # Dream Turtle Easy
    "7c84df55-538e-47c4-bedd-4c70b7c833e3": "NodeE",   # Dream Bear Easy
    "50f31ead-c117-43d3-9dfe-fe5361a9b213": "NodeC",   # Dream Skarn Easy
    "9563dc8b-fb88-4d4f-8e85-cfdf3248ecd5": "NodeD",   # Dream Spider Easy
    # The Usurper: the same off-by-one visit events. Each item's battle is
    # the scene of its own name.
    "89c6998a-a656-4f6f-b5fd-8b929a8cdd2f": "NodeB",   # Grimskull Hall (003)
    "36ae1fb2-7268-43d2-80f1-2d6f1c8fa57c": "NodeD",   # Chamber of Awakening (005)
    "405f4f7a-4ccd-4c3e-8f0b-15c848d3ac68": "NodeE",   # Shepherd's Rest, Both Dead (006)
    "299d7998-e04b-4916-b196-91429d623a9b": "NodeF",   # Last Rites (007)
}


def encounter_event_nodes(authored: dict[str, dict[str, Any]] | None
                          ) -> dict[str, list[str]]:
    """Records Encounter scene GUIDs -> nodes that start them when visited
    (``SCENE_NODE_LINKS`` corrects the mis-wired ones)."""
    linked: dict[str, list[str]] = {}
    for node, item in (authored or {}).items():
        for event in item.get("visit_events") or ():
            guid = str(event.get("guid") or "").lower()
            if not guid or "encounter" not in str(event.get("type") or "").lower():
                continue
            if guid in SCENE_NODE_LINKS:
                node = SCENE_NODE_LINKS[guid] if SCENE_NODE_LINKS[guid] in authored else node
            nodes = linked.setdefault(guid, [])
            if node not in nodes:
                nodes.append(node)
    return linked


def zone_catalog(db: sqlite3.Connection | None, entry: dict[str, Any],
                 authored: dict[str, dict[str, Any]] | None = None, *,
                 ui: bool = False, personality=None) -> dict[str, Any]:
    """Table rows for a zone that has a server campaign template."""
    if db is None or not entry.get("server_template"):
        return _EMPTY_CATALOG
    if authored is None and entry.get("records") and entry["kind"] == "dungeon":
        authored = _authored_items(scene_template(entry["records"]))
    return server_catalog(db, entry["server_template"],
                          entry.get("scene_prefix") or "\x00",
                          entry.get("scene_regex"), authored,
                          entry.get("scene_match"), ui=ui, personality=personality)


def player_options(db) -> dict[str, Any]:
    """Choices for a scene's player champion and deck (blank = the player's own).

    Champions: every ChampionTemplate record. Decks: every EncounterDeck record
    that has cards in the database, labelled with the scene(s) using it.
    """
    names = champion_names()
    champions = sorted(({"guid": g, "name": n} for g, n in names.items() if n),
                       key=lambda c: c["name"].casefold())
    cards = {str(row[0]).lower() for row in _query(db, "SELECT DISTINCT deck_guid FROM encounter_deck_cards")}
    scene_names: dict[str, list[str]] = {}
    for scene_name, ai_deck, player_deck in _query(
            db, "SELECT name, ai_deck_guid, player_deck_guid FROM encounter_scenes"):
        for deck in (ai_deck, player_deck):
            if deck:
                scene_names.setdefault(str(deck).lower(), []).append(str(scene_name))
    decks = []
    for record in records("EncounterDeck"):
        guid = str(((record.get("m_Id") or {}).get("m_Guid")) or "").lower()
        if not guid or guid not in cards:
            continue
        used = sorted(set(scene_names.get(guid, [])))
        label = " / ".join(used) if used else f"Deck {guid[:8]}"
        decks.append({"guid": guid, "name": label})
    decks.sort(key=lambda d: d["name"].casefold())
    return {"champions": champions, "decks": decks}
