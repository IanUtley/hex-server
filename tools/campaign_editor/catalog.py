"""Read-only campaign data for the campaign editor.

Static content comes from ``Records/`` (SceneData, ConversationTemplate,
QuestTemplate, ChampionTemplate); the server's derived catalog tables and a
champion's live campaign state come from the SQLite database, opened
read-only. Nothing here writes to Records or the database.
"""

from __future__ import annotations

import json
import html
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
RECORDS = REPO / "Records"
GENERATED = Path(__file__).resolve().parent / "generated"

AREA_TEMPLATE = "AZ1"
SCENE_NAME = "Howling Plains"
# Counter-backed champion statuses exposed by the client. The editor still
# accepts any name, but these are useful suggestions for keyword(Name).
_CHAMPION_STATUS_KEYWORDS = ("Burning", "Dazed", "Stealth", "Vulnerable")

_LOCK = threading.Lock()
_CONVERSATION_LOCK = threading.Lock()  # separate: builds call records()
_SECTIONS: dict[str, list[dict[str, Any]]] = {}
_CONVERSATIONS: dict[str, dict[str, Any]] | None = None


def _parse_record(raw: str) -> dict[str, Any] | None:
    import sys
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    # Records lines are relaxed JSON (trailing commas, integer object keys);
    # the seed pipeline's parser handles both.
    from AssetExtraction.gamedata_seed import parse_record
    return parse_record(raw)


def records(section: str) -> list[dict[str, Any]]:
    """All parsed records of one Records section, loaded once per process."""
    with _LOCK:
        cached = _SECTIONS.get(section)
        if cached is not None:
            return cached
        parsed = []
        path = RECORDS / f"{section}.jsonl"
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index == 0 or not line.strip():
                    continue  # section marker
                value = json.loads(line)
                record = _parse_record(value if isinstance(value, str) else line)
                if record:
                    parsed.append(record)
        _SECTIONS[section] = parsed
        return parsed


def champion_status_keywords() -> list[str]:
    """Known counter-backed champion statuses, confirmed by game records."""
    available = {str(record.get("m_Name") or "").strip()
                 for record in records("CardCounterTemplate")}
    return [name for name in _CHAMPION_STATUS_KEYWORDS if name in available]


def _guid(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("m_Guid")
    return str(value or "").lower()


def _short_type(value: Any) -> str:
    return str(value or "").rsplit(".", 1)[-1]


def champion_names() -> dict[str, str]:
    return {_guid(r.get("m_Id")): str(r.get("m_Name") or "").strip()
            for r in records("ChampionTemplate") if r.get("m_Name")}


def area_scene() -> dict[str, Any]:
    for record in records("SceneData"):
        if record.get("m_Name") == SCENE_NAME:
            return record
    raise LookupError(f"SceneData {SCENE_NAME!r} not found")


def _qualifiers(items: Any) -> list[dict[str, Any]]:
    """Summarize Challenge restrictions (prerequisites / qualifiers)."""
    result = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        fields = {key[2:]: value for key, value in item.items()
                  if key.startswith("m_") and key != "m_EventOwnerRestriction"}
        result.append({"type": _short_type(item.get("_t")), "fields": fields})
    return result


def _events(items: Any) -> list[dict[str, Any]]:
    result = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        entry = {"type": _short_type(item.get("_t"))}
        for key, value in item.items():
            if key.startswith("m_") and value not in ("", None, [], {}):
                entry[key[2:]] = value
        result.append(entry)
    return result


def _conversation_summary(record: dict[str, Any]) -> dict[str, Any]:
    questions = {}
    for qid, question in (record.get("m_Questions") or {}).items():
        questions[str(qid)] = {
            "text": question.get("m_QuestionText", ""),
            "answers": [str(a) for a in question.get("m_AnswerIds") or ()],
            "speaker_override": question.get("m_OwnerNameOverride") or "",
        }
    answers = {}
    for aid, answer in (record.get("m_Answers") or {}).items():
        answers[str(aid)] = {
            "text": answer.get("m_AnswerText", ""),
            "next": answer.get("m_NextQuestionId"),
            "prerequisites": _qualifiers(answer.get("m_Prerequisites")),
            "events": (_events(answer.get("m_OnSelectedEvents")) +
                       _events(answer.get("m_EventsToFireWhenSelected"))),
        }
    return {
        "guid": _guid(record.get("m_Id")),
        "name": record.get("m_Name", ""),
        "owner": record.get("m_OwnerName", ""),
        "champion": _guid(record.get("m_ChampionId")),
        "appearance": record.get("m_Appearance", ""),
        "start": str(record.get("m_StartingQuestionId", 0)),
        "questions": questions,
        "answers": answers,
    }


def conversations() -> dict[str, dict[str, Any]]:
    global _CONVERSATIONS
    with _CONVERSATION_LOCK:
        if _CONVERSATIONS is None:
            built = {}
            for record in records("ConversationTemplate"):
                summary = _conversation_summary(record)
                if summary["guid"]:
                    built[summary["guid"]] = summary
            _CONVERSATIONS = built
        return _CONVERSATIONS


def ending_id(answer_id: str, answer: dict[str, Any]) -> tuple[str, bool]:
    """``(id, server_visible)`` for an answer that ends the conversation.

    The client reports an answer to the server only through its
    ServerScriptEvent, so that event name is the ending's id; an ending
    without one is ``A<answer id>`` and only the simulator can choose it.
    """
    for event in answer.get("events") or ():
        if event.get("type") == "ServerScriptEvent" and event.get("EventName"):
            return str(event["EventName"]), True
    return f"A{answer_id}", False


def _ends(answer: dict[str, Any]) -> bool:
    try:
        return answer.get("next") is None or int(answer["next"]) < 0
    except (TypeError, ValueError):
        return True


def conversation_endings(found: dict[str, Any]) -> list[dict[str, Any]]:
    question_of = {aid: qid for qid, q in found["questions"].items()
                   for aid in q["answers"]}
    endings = []
    for aid, answer in found["answers"].items():
        if not _ends(answer):
            continue
        ending, visible = ending_id(aid, answer)
        endings.append({"answer": aid, "question": question_of.get(aid, ""),
                        "text": answer["text"], "ending": ending,
                        "event": visible, "server_visible": visible})
    # A single ending is implied by conv_done itself.
    if len(endings) == 1:
        endings[0]["server_visible"] = True
    return endings


def _endings_of(guid: Any) -> list[dict[str, Any]]:
    found = conversations().get(str(guid or "").lower())
    return conversation_endings(found) if found else []


def conversation(guid: str) -> dict[str, Any] | None:
    found = conversations().get(str(guid or "").lower())
    if found is None:
        return None
    names = champion_names()
    return dict(found, champion_name=names.get(found["champion"], ""),
                endings=conversation_endings(found))


def conversation_hooks(guid: str) -> dict[str, list[Any]]:
    """Native script events and prerequisites authored on a conversation."""
    found = conversations().get(str(guid or "").lower()) or {}
    events, prerequisites = [], []
    for answer in (found.get("answers") or {}).values():
        events.extend(answer["events"])
        prerequisites.extend(answer["prerequisites"])
    return {"events": events, "prerequisites": prerequisites}


def quest_templates() -> list[dict[str, Any]]:
    result = []
    for record in records("QuestTemplate"):
        objectives = []
        for objective in record.get("m_Objectives") or ():
            if not isinstance(objective, dict):
                continue
            objectives.append({
                "type": _short_type(objective.get("_t")),
                "location": objective.get("m_QuestLocationId", ""),
                "id": objective.get("m_ID", ""),
                "text": objective.get("m_Title") or "",
                "encounter": _guid(objective.get("m_EncounterId")),
                "dungeon": _guid(objective.get("m_DungeonId")),
            })
        result.append({
            "guid": _guid(record.get("m_Id")),
            "script": record.get("m_ScriptName") or record.get("m_Name", ""),
            "title": record.get("m_Title") or record.get("m_Name", ""),
            "objectives": objectives,
        })
    return result


# --- database (read-only) -------------------------------------------------

def connect(db_path: Path) -> sqlite3.Connection:
    """Open the server database read-only; one connection per request."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                                 timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


_NODE_IN_SCENE = re.compile(r"\bNODE\s*-?\s*([0-9]+|[A-Z])([A-Z])?\b", re.I)


def scene_node_id(scene_name: str) -> str | None:
    """``AZ 1 - NODE 23B - ...`` -> ``Node023`` (variant letter dropped)."""
    match = _NODE_IN_SCENE.search(scene_name or "")
    if not match:
        return None
    token = match.group(1).upper()
    return f"Node{int(token):03d}" if token.isdigit() else f"Node00{token}"


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


_SCENE_KINDS = ("PANORAMA", "DUNGEON", "DIALOG", "REWARD", "CHOICE")


def _scene_kind(name: str, battle: bool) -> str:
    """Scene role from its authored name suffix (``... - PANORAMA``)."""
    upper = str(name or "").upper()
    for kind in _SCENE_KINDS:
        if upper.endswith(kind) or f" {kind} " in f"{upper} ":
            return kind.lower()
    # No opponent and no role suffix: a conversation/lore node.
    return "battle" if battle else "scene"


def zones() -> list[dict[str, Any]]:
    """Every extracted zone (overworlds, dungeons, panoramas)."""
    path = GENERATED / "zones.json"
    if not path.exists():
        raise FileNotFoundError(
            "Run AssetExtraction/extract_campaign_editor_assets.py first")
    return json.loads(path.read_text(encoding="utf-8"))["zones"]


def zone(zone_id: str) -> dict[str, Any]:
    for entry in zones():
        if entry["id"] == zone_id:
            return entry
    raise LookupError(f"unknown zone {zone_id!r}")


def scene_template(name: str) -> dict[str, Any] | None:
    for record in records("SceneData"):
        if record.get("m_Name") == name and record["_t"].endswith("DungeonTemplate"):
            return record
    return None


def encounter_scene_names() -> dict[str, str]:
    return {_guid(r.get("m_Id")): str(r.get("m_Name") or r.get("m_Title") or "")
            for r in records("SceneData") if r["_t"].endswith("EncounterScene")}


DATA_DIR = REPO / "campaign_data"


def data_overrides(table: str) -> list[dict[str, Any]]:
    """Rows the editor saved for ``table`` (applied to the DB at startup)."""
    rows = []
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.extend(r for r in (data.get("tables") or {}).get(table) or ()
                    if isinstance(r, dict))
    return rows


def data_mtime() -> float:
    return max((p.stat().st_mtime for p in DATA_DIR.glob("*.json")), default=0.0)


def _audience(name: str, tags: Any = None, faction: str = "") -> dict[str, str]:
    """Who a row is for: its race/faction from positive tags, else the
    authored name (``... - Ardent - ...``, ``... - Quest End - Dwarf``)."""
    import offers
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


_RECORD_SCENES: list[dict[str, Any]] | None = None
_SCENES_LOCK = threading.Lock()   # separate: building calls records()


def _records_encounters() -> list[dict[str, Any]]:
    """Encounter scenes straight from Records (opponent = the AI deck)."""
    global _RECORD_SCENES
    with _SCENES_LOCK:
        if _RECORD_SCENES is None:
            # Opponent: EncounterDeck -> its champion, else its DeckTemplate's
            # champion, else that deck's name.
            names = champion_names()
            templates = {_guid(r.get("m_Id")): r for r in records("DeckTemplate")}
            decks = {}
            for r in records("EncounterDeck"):
                template = templates.get(_guid(r.get("m_DeckTemplateId"))) or {}
                champion = _guid(r.get("m_ChampionId")).strip("0-") and _guid(r.get("m_ChampionId"))
                champion = champion or _guid(template.get("m_ChampionId"))
                decks[_guid(r.get("m_Id"))] = (names.get(champion) or
                                               str(template.get("m_DeckName") or ""))
            built = []
            for r in records("SceneData"):
                if not r["_t"].endswith("EncounterScene"):
                    continue
                ai = [p for p in r.get("m_EncounterPlayers") or ()
                      if isinstance(p, dict) and p.get("m_IsAI")]
                deck = _guid((ai[0].get("m_EncounterDeck") if ai else None))
                battle = bool(deck.strip("0-"))
                name = str(r.get("m_Name") or "")
                built.append({
                    "guid": _guid(r.get("m_Id")), "name": name,
                    "title": str(r.get("m_Title") or name), "champion": "",
                    "champion_name": decks.get(deck, ""), "deck": deck,
                    "battle": battle, "kind": _scene_kind(name, battle), "race": "",
                    "mods": [{"title": m.get("m_Title", "")} for m in r.get("m_Mods") or ()
                             if isinstance(m, dict)],
                    "rewards": {}, "source": "Records"})
            _RECORD_SCENES = built
        return _RECORD_SCENES


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


def _scene_node(name: str, scene_prefix: str, scene_regex: str | None,
                item_names: dict[str, str]) -> tuple[str | None, str]:
    """``(node, race)`` for an encounter scene name in this zone."""
    if not scene_regex:
        return (scene_node_id(name) if name.startswith(scene_prefix) else None), ""
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
                   scene_match: str | None = None
                   ) -> dict[str, Any]:
    """The server's catalogue rows for one campaign template.

    Encounters are matched to nodes by scene name: ``AZ 1 - NODE 23 ...`` for
    overworlds, or a zone's ``scene_regex`` (named groups ``node`` - a Records
    item name - and ``race``) for per-race dungeons.
    """
    names = champion_names()
    # Saved data-file rows override this read-only DB projection; startup
    # applies those rows to the database as well.
    pending = {(r.get("campaign_template"), str(r.get("conversation_guid") or "").lower()): r
               for r in data_overrides("campaign_node_conversations")}
    node_conversations: dict[str, list[dict[str, Any]]] = {}
    db_rows = [dict(row) for row in db.execute(
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
            "hooks": conversation_hooks(guid), "endings": _endings_of(guid)})
    quest_links: dict[str, list[dict[str, Any]]] = {}
    pending_quests = {(r.get("quest_script"), str(r.get("conversation_guid") or "").lower(),
                       r.get("campaign_template")): r
                      for r in data_overrides("quest_conversations")}
    for row in db.execute(
            "SELECT quest_script, conversation_guid, node_id, npc, role, "
            "faction, conversation_name, start_hook, conditions_json, "
            "priority, enabled FROM quest_conversations "
            "WHERE campaign_template=? ORDER BY node_id, priority", (template,)):
        conditions = _quest_conditions(row, pending_quests.get(
            (row["quest_script"], str(row["conversation_guid"] or "").lower(), template)))
        actions, actions_default = quest_actions(
            row["role"], row["quest_script"], conditions)
        quest_links.setdefault(row["node_id"] or "", []).append({
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
            "endings": _endings_of(row["conversation_guid"])})
    # Saved-but-not-yet-applied scene rewards win (later files override).
    pending_scene_rewards, pending_scene_actions = {}, {}
    pending_scene_mods: dict[str, Any] = {}
    pending_personality: dict[str, str] = {}
    for r in data_overrides("encounter_scenes"):
        if "mods_json" in r:
            value = r["mods_json"]
            pending_scene_mods[str(r.get("guid") or "").lower()] = (
                _json(value, []) if isinstance(value, str) else value)
        if str(r.get("ai_deck_personality") or "").strip():
            pending_personality[str(r.get("guid") or "").lower()] = str(
                r["ai_deck_personality"]).strip()
        for column, target in (("rewards_json", pending_scene_rewards),
                               ("actions_json", pending_scene_actions)):
            if column in r:
                value = r[column]
                target[str(r.get("guid") or "").lower()] = (
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
    for row in db.execute(
            "SELECT guid, name, title, ai_champion_guid, ai_deck_guid, "
            "mods_json, rewards_json, actions_json, ai_deck_personality "
            "FROM encounter_scenes "
            "WHERE name LIKE ? ORDER BY name",
            ("%" if scene_regex or scene_match else f"{scene_prefix}%",)):
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
            encounters.setdefault(node_id, []).append({
                "guid": str(row["guid"] or "").lower(),
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
                    str(row["guid"] or "").lower(),
                    str(row["ai_deck_personality"] or "")),
                "ai_auto": (auto_personality(db, row["ai_deck_guid"])
                            if battle and row["ai_deck_guid"] else None),
                "ai_overridden": str(row["guid"] or "").lower() in pending_personality})
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
    for row in db.execute(
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
    edges = [dict(row) for row in db.execute(
        "SELECT from_node, to_node, path_name FROM campaign_node_edges "
        "WHERE campaign_template=?", (template,))]
    return {"node_conversations": node_conversations,
            "quest_links": quest_links, "encounters": encounters,
            "quests": quests, "edges": edges}


_EMPTY_CATALOG = {"node_conversations": {}, "quest_links": {},
                  "encounters": {}, "quests": [], "edges": []}


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

def _map_node_ids(records_name: str) -> set[str]:
    """The node ids on a zone's extracted map (generated/<zone>/map.json)."""
    for entry in zones():
        if entry.get("records") == records_name:
            path = GENERATED / entry["id"] / "map.json"
            try:
                return {n["id"] for n in json.loads(
                    path.read_text(encoding="utf-8")).get("nodes") or ()}
            except (OSError, ValueError, KeyError):
                return set()
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
    return authored


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


def zone_catalog(db: sqlite3.Connection | None, entry: dict[str, Any],
                 authored: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Table rows for a zone that has a server campaign template."""
    if db is None or not entry.get("server_template"):
        return _EMPTY_CATALOG
    if authored is None and entry.get("records") and entry["kind"] == "dungeon":
        authored = _authored_items(scene_template(entry["records"]))
    return server_catalog(db, entry["server_template"],
                          entry.get("scene_prefix") or "\x00",
                          entry.get("scene_regex"), authored,
                          entry.get("scene_match"))


DECK_PERSONALITIES = ("Default", "Aggressive", "BigThreats", "BuildArmy", "Burn",
                      "HandAdvantage", "Reanimation")
_AUTO_PERSONALITY: dict[str, dict[str, Any]] = {}
_AUTO_LOCK = threading.Lock()


def auto_personality(db: sqlite3.Connection, deck_guid: str) -> dict[str, Any]:
    """The Python deck evaluator's pick for an encounter deck (cached).

    ``{"personality": name|"Default", "scores": {...}}``; this is what the
    server stores for scenes nobody has mapped (``ai_deck_strategy``).
    """
    key = str(deck_guid or "").lower()
    if not key:
        return {"personality": "Default", "scores": {}}
    with _AUTO_LOCK:
        if key in _AUTO_PERSONALITY:
            return _AUTO_PERSONALITY[key]
    try:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from ai_deck_strategy import evaluate_deck_strategy
        cards = [(r[0], r[1]) for r in db.execute(
            "SELECT card_guid, quantity FROM encounter_deck_cards WHERE deck_guid=?",
            (deck_guid,))]
        result = evaluate_deck_strategy(cards)
        value = {"personality": result.personality or "Default",
                 "scores": {k: round(v, 1) for k, v in result.scores.items()}}
    except Exception as exc:                      # the editor still works without it
        value = {"personality": "Default", "scores": {}, "error": str(exc)}
    with _AUTO_LOCK:
        _AUTO_PERSONALITY[key] = value
    return value


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
    return {"chests": [c for c in chests if c["name"]], "cards": cards,
            "art": art, "card_art": card_art, "stardust": stardust,
            "equipment": [e for e in equipment if e["name"]],
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
    encounters = by_node(data["encounters"])
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
    own = entry.get("server_template") and entry.get("race")
    if own:
        data = zone_catalog(db, entry)
    elif db is not None and parent and parent["kind"] == "overworld":
        data = server_catalog(db, parent["server_template"], parent["scene_prefix"])
    node_id = entry.get("parent_node") or ""
    if own:
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

RULES_DIR = REPO / "campaign_rules"
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
    """Rules for evaluation: the zone's rules file plus table-derived offers.

    The file holds node state only; ``offers.build_offers`` merges what each
    node offers from the (startup-overridden) tables. Rebuilt when the file
    changes on disk. ``(None, [])`` when the zone has no rules file yet.
    """
    import copy
    import offers
    path = rules_path(zone_id)
    if not path.exists():
        return None, []
    engine = _rules_module()
    mtime = (path.stat().st_mtime, data_mtime())
    with _RULES_LOCK:
        cached = _RULES_CACHE.get(zone_id)
        if cached is None or cached["mtime"] != mtime:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            merged = copy.deepcopy(loaded)
            entry = zone(zone_id)
            # Starter panoramas (own AZ0 rows) have rules: NPCs are nodes.
            if db is not None and entry.get("server_template") and (
                    entry["kind"] != "panorama" or entry.get("race")):
                data = zone_catalog(db, entry)
                entries, _starts = offers.build_offers(
                    entry["server_template"], data,
                    loaded.get("quests") or {},
                    known_conversations=set(conversations()))
                for node_id, node_entries in entries.items():
                    merged.setdefault("nodes", {}).setdefault(
                        node_id, {})["entries"] = node_entries
                # Scene actions (set_flag / clear_flag / check_tamed()) are
                # live editor data: rebuild their hooks instead of trusting
                # the copies an importer baked into the rules file.
                canon = (offers.canonical_node if entry["kind"] == "overworld"
                         else (lambda n: n))
                merged["hooks"] = [
                    hook for hook in merged.get("hooks") or ()
                    if not str(hook.get("id") or "").startswith(("scene:", "tamed:"))]
                merged["hooks"] += offers.scene_action_hooks(
                    e for listed in data["encounters"].values() for e in listed)
                # Card-selector options ("choice" scene mods), by scene.
                merged["mod_choices"] = {
                    str(e["guid"]).lower(): options
                    for listed in data["encounters"].values() for e in listed
                    if (options := engine.scene_mod_choices(e.get("mods")))}
                for raw_node, listed in data["encounters"].items():
                    for encounter in listed:
                        if encounter.get("battle") and offers.tamed_scene(encounter):
                            # A taming win completes the node only once tamed.
                            merged.setdefault("nodes", {}).setdefault(
                                canon(raw_node), {}).setdefault(
                                "completed", {"flag": offers.tamed_flag(encounter["guid"])})
            # Each node state as an editable tag list (None: needs any/nesting).
            state_tags = {
                node_id: {gate: engine.condition_tags(spec[gate])
                          if not isinstance(spec[gate], list) else spec[gate]
                          for gate in engine.GATES if gate in spec}
                for node_id, spec in (loaded.get("nodes") or {}).items()}
            merged["state_tags"] = state_tags
            # Checked after merging: live scene-action hooks set flags too.
            problems = engine.validate({k: v for k, v in merged.items()
                                        if k != "state_tags"})
            cached = {"mtime": mtime, "rules": merged, "problems": problems}
            _RULES_CACHE[zone_id] = cached
        return cached["rules"], cached["problems"]


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
