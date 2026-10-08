"""The editor's only writes: tags, saved to the repo's campaign files.

* Conversation tags -> ``campaign_data/<template>.json`` as a
  ``campaign_node_conversations`` row (``trigger_json.tags``); the server
  applies it to the database at startup (``campaign_data.apply_all``).
* Node state tags -> ``campaign_rules/<zone>.json`` ``nodes.<id>.<state>``.

Records and the database are never written. Every save validates the whole
file first and replaces it atomically, under one lock, so concurrent
requests cannot interleave a read-modify-write.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

import catalog

_WRITE_LOCK = threading.Lock()


def _campaign_data():
    if str(catalog.REPO) not in sys.path:
        sys.path.insert(0, str(catalog.REPO))
    import campaign_data
    return campaign_data


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def clean_tags(tags: Any) -> list:
    """Strip blanks and check every tag parses (``RulesError`` if not).

    A tag list (all must hold) or "or" groups (a list of tag lists); a
    single group collapses to a plain list and empty groups are dropped.
    """
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    engine = catalog._rules_module()
    if tags and all(isinstance(group, list) for group in tags):
        groups = [clean_tags(group) for group in tags]
        groups = [group for group in groups if group]
        return groups[0] if len(groups) == 1 else groups
    if any(isinstance(tag, list) for tag in tags):
        raise ValueError("mixed tags and tag groups")
    cleaned = [str(tag).strip() for tag in tags if str(tag).strip()]
    for tag in cleaned:
        engine.parse_tag(tag)
    return cleaned


def _update_trigger(db: sqlite3.Connection, template: str,
                    conversation_guid: str, change) -> dict[str, Any]:
    """Apply ``change(trigger)`` to one node conversation's trigger and save
    the difference from the seeded row to ``campaign_data/<template>.json``.

    The row may be in the database or only in a data file (rows a data file
    inserts reach the database at the next start).
    """
    row = db.execute(
        "SELECT conversation_guid, trigger_json FROM campaign_node_conversations "
        "WHERE campaign_template=? AND LOWER(conversation_guid)=?",
        (str(template), str(conversation_guid).lower())).fetchone()
    campaign_data = _campaign_data()
    with _WRITE_LOCK:
        files = sorted(catalog.DATA_DIR.glob("*.json"))
        located = None   # (path, data, row) of an existing data-file row
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            for candidate in (data.get("tables") or {}).get(
                    "campaign_node_conversations") or ():
                if (candidate.get("campaign_template") == template and
                        str(candidate.get("conversation_guid")).lower() ==
                        str(conversation_guid).lower()):
                    located = (path, data, candidate)
        if row is None and located is None:
            raise LookupError(f"no {template} node conversation {conversation_guid}")
        if row is not None:
            try:
                base = json.loads(row[1] or "{}")
            except ValueError:
                base = {}
        else:   # inserted by a data file: no seeded row to fall back to
            base = None
        if located is None:
            path = catalog.DATA_DIR / f"{template}.json"
            data = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                    else {"schema_version": campaign_data.SCHEMA_VERSION,
                          "description": f"{template} campaign table edits "
                          "(campaign editor)", "tables": {}})
            existing = {"campaign_template": template, "conversation_guid": row[0]}
            data.setdefault("tables", {}).setdefault(
                "campaign_node_conversations", []).append(existing)
        else:
            path, data, existing = located
        trigger = dict(existing.get("trigger_json") or base or {})
        change(trigger)
        if base is not None and trigger == base:   # back to the seeded row
            existing.pop("trigger_json", None)
            if set(existing) <= {"campaign_template", "conversation_guid"}:
                data["tables"]["campaign_node_conversations"].remove(existing)
        else:
            existing["trigger_json"] = trigger
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
    return trigger


def save_conversation_tags(db: sqlite3.Connection, template: str,
                           conversation_guid: str, tags: Any) -> dict[str, Any]:
    """Set a node conversation's tags; returns its resulting trigger."""
    tags = clean_tags(tags)

    def change(trigger):
        if tags:
            trigger["tags"] = tags
        else:
            trigger.pop("tags", None)
        if trigger.get("source") == "import_crayburn.py" or "_AZ0_" in str(
                trigger.get("source")):
            trigger["edited_tags"] = True   # import_crayburn.py keeps it
    return _update_trigger(db, template, conversation_guid, change)


def save_conversation_actions(db: sqlite3.Connection, template: str,
                              conversation_guid: str, ending: str,
                              actions: Any) -> dict[str, Any]:
    """Set the actions one ending (or ``*``: any ending) runs."""
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")
    engine = catalog._rules_module()
    actions = [str(a).strip() for a in actions if str(a).strip()]
    parsed_actions = [(action, engine.parse_action(action)) for action in actions]
    if any("encounter" in parsed for _action, parsed in parsed_actions):
        node, conversation = _node_conversation(
            db, template, conversation_guid)
        node_conversations = {
            str(row.get("guid") or "").lower()
            for row in [*(node.get("conversations") or []),
                        *(node.get("quest_links") or [])]
            if row.get("guid")
        }
        if str(conversation_guid).lower() not in node_conversations:
            raise ValueError("encounter actions require a conversation on a node")
        allowed_scenes = {
            str(scene.get("guid") or "").lower()
            for scene in node.get("encounters") or ()
            if scene.get("guid") and scene.get("battle") is not False
        }
        # Keep authored action scenes available while editing their action.
        for ending_actions in ((conversation.get("trigger") or {}).get(
                "actions") or {}).values():
            for existing in ending_actions or ():
                match = re.fullmatch(
                    r"\s*encounter\s*\((.+)\)\s*", str(existing), re.I)
                if match:
                    allowed_scenes.add(
                        match.group(1).split(",", 1)[0].strip().lower())
        for action, parsed in parsed_actions:
            encounter = parsed.get("encounter")
            if not encounter:
                continue
            scene = str(encounter.get("scene") or "").lower()
            if scene not in allowed_scenes:
                raise ValueError(
                    f"encounter scene {scene!r} is not on this node or already "
                    "used by this conversation")
            opening_conversation = encounter.get("conversation_id")
            if (opening_conversation and
                    str(opening_conversation).lower() not in node_conversations):
                raise ValueError(
                    f"encounter conversation {opening_conversation!r} is not "
                    "in this node's conversation list")
    for _action, parsed in parsed_actions:
        quest = parsed.get("start_quest") or parsed.get("complete_quest")
        if quest and not db.execute(
                "SELECT 1 FROM quest_templates WHERE script_name=?",
                (quest,)).fetchone():
            raise LookupError(f"unknown quest {quest!r}")
        target = parsed.get("teleport")
        if target:
            catalog.zone(target["zone"])   # LookupError for an unknown zone
    ending = str(ending or "").strip() or "*"

    def change(trigger):
        table = dict(trigger.get("actions") or {})
        if actions:
            table[ending] = actions
        else:
            table.pop(ending, None)
        if table:
            trigger["actions"] = table
        else:
            trigger.pop("actions", None)
        if trigger.get("source") == "import_crayburn.py" or "_AZ0_" in str(
                trigger.get("source")):
            trigger["edited_actions"] = True   # import_crayburn.py keeps it
    return _update_trigger(db, template, conversation_guid, change)


def _node_conversation(db: sqlite3.Connection, template: str,
                       conversation_guid: str
                       ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Find a conversation row and its node in the editor's merged catalog."""
    wanted_template = str(template or "").lower()
    wanted_guid = str(conversation_guid or "").lower()
    for entry in catalog.zones():
        if str(entry.get("server_template") or "").lower() != wanted_template:
            continue
        if entry.get("kind") not in ("overworld", "dungeon"):
            continue
        payload = catalog.area_payload(db, entry["id"])
        for node in payload.get("nodes") or ():
            for row in [*(node.get("conversations") or []),
                        *(node.get("quest_links") or [])]:
                if str(row.get("guid") or "").lower() == wanted_guid:
                    return node, row
    raise LookupError(
        f"no node conversation {conversation_guid} in campaign {template}")


def _update_quest_conditions(db: sqlite3.Connection, template: str,
                             quest_script: str, conversation_guid: str,
                             change) -> dict[str, Any]:
    """Apply ``change(conditions)`` to a quest_conversations row's
    ``conditions_json`` and save it to ``campaign_data/<template>.json``."""
    row = db.execute(
        "SELECT conversation_guid, conditions_json FROM quest_conversations "
        "WHERE campaign_template=? AND quest_script=? AND LOWER(conversation_guid)=?",
        (template, quest_script, str(conversation_guid).lower())).fetchone()
    if row is None:
        raise LookupError(f"no {template} quest row {quest_script} {conversation_guid}")
    guid = row[0]
    try:
        base = json.loads(row[1] or "{}")
    except ValueError:
        base = {}
    campaign_data = _campaign_data()
    path = catalog.DATA_DIR / f"{template}.json"
    with _WRITE_LOCK:
        data = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                else {"schema_version": campaign_data.SCHEMA_VERSION,
                      "description": f"{template} campaign table edits", "tables": {}})
        rows = data.setdefault("tables", {}).setdefault("quest_conversations", [])
        key = (quest_script, guid.lower(), template)
        existing = next((r for r in rows if (r.get("quest_script"),
                         str(r.get("conversation_guid")).lower(),
                         r.get("campaign_template")) == key), None)
        conditions = dict((existing or {}).get("conditions_json") or base)
        change(conditions)
        if existing is None:
            # Carry the row's other columns so a fresh database (where the
            # seed may lack it) can insert it, not only update it.
            full = db.execute(
                "SELECT node_id, npc, role, faction, conversation_name, "
                "start_hook, priority, enabled FROM quest_conversations "
                "WHERE campaign_template=? AND quest_script=? "
                "AND LOWER(conversation_guid)=?",
                (template, quest_script, guid.lower())).fetchone()
            existing = {"quest_script": quest_script, "conversation_guid": guid,
                        "campaign_template": template}
            if full is not None:
                existing.update(dict(zip(
                    ("node_id", "npc", "role", "faction", "conversation_name",
                     "start_hook", "priority", "enabled"), full)))
                existing["enabled"] = bool(existing["enabled"])
            rows.append(existing)
        existing["conditions_json"] = conditions
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
    return conditions


def save_quest_tags(db: sqlite3.Connection, template: str, quest_script: str,
                    conversation_guid: str, tags: Any) -> dict[str, Any]:
    """Set a quest_conversations row's ``conditions_json.tags``."""
    tags = clean_tags(tags)

    def change(conditions):
        if tags:
            conditions["tags"] = tags
        else:
            conditions.pop("tags", None)
    return _update_quest_conditions(
        db, template, quest_script, conversation_guid, change)


def save_quest_actions(db: sqlite3.Connection, template: str, quest_script: str,
                       conversation_guid: str, ending: str,
                       actions: Any) -> dict[str, Any]:
    """Set the actions one ending (or ``*``) of a quest conversation runs.

    Saved as ``conditions_json.actions``; once saved it replaces the
    role-based default (start -> ``start_quest``, complete -> ``complete_quest``),
    so an empty table really means "none".
    """
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")
    engine = catalog._rules_module()
    actions = [str(a).strip() for a in actions if str(a).strip()]
    for action in actions:
        parsed = engine.parse_action(action)
        if "encounter" in parsed:
            raise ValueError("quest conversations cannot start encounters")
        quest = parsed.get("start_quest") or parsed.get("complete_quest")
        if quest and not db.execute(
                "SELECT 1 FROM quest_templates WHERE script_name=?",
                (quest,)).fetchone():
            raise LookupError(f"unknown quest {quest!r}")
        target = parsed.get("teleport")
        if target:
            catalog.zone(target["zone"])
    ending = str(ending or "").strip() or "*"

    def change(conditions):
        table = dict(conditions.get("actions") if isinstance(
            conditions.get("actions"), dict) else _default_quest_table(
                db, template, quest_script, conversation_guid))
        if actions:
            table[ending] = actions
        else:
            table.pop(ending, None)
        conditions["actions"] = table
    return _update_quest_conditions(
        db, template, quest_script, conversation_guid, change)


def _default_quest_table(db, template, quest_script, conversation_guid):
    row = db.execute(
        "SELECT role FROM quest_conversations WHERE campaign_template=? "
        "AND quest_script=? AND LOWER(conversation_guid)=?",
        (template, quest_script, str(conversation_guid).lower())).fetchone()
    return catalog.quest_actions(row[0] if row else "", quest_script, {})[0]


def _new_rules(zone_id: str) -> dict[str, Any]:
    """A rules skeleton for a zone that has none: its prefab paths only."""
    engine = catalog._rules_module()
    entry = catalog.zone(zone_id)
    if entry["kind"] == "panorama":
        raise ValueError("panoramas have no node states")
    layout = json.loads((catalog.GENERATED / zone_id / "map.json")
                        .read_text(encoding="utf-8"))
    ids = [node["id"] for node in layout["nodes"]]
    start = "Node001" if "Node001" in ids else (sorted(ids) or [""])[0]
    return {"schema_version": engine.SCHEMA_VERSION, "campaign": zone_id,
            "scene": entry["title"], "start": {"node": start,
                                               "node_completed": True,
                                               "flags": []},
            "map": {"paths": [{"id": p["id"], "from": p["from"], "to": p["to"]}
                              for p in layout["paths"]]},
            "quests": {}, "nodes": {}, "hooks": []}


def save_node_state(zone_id: str, node_id: str, state: str,
                    tags: Any) -> list[str] | None:
    """Set (tags list) or remove (``None``) one node state's tags."""
    engine = catalog._rules_module()
    if state not in engine.GATES:
        raise ValueError(f"unknown node state {state!r}")
    tags = None if tags is None else clean_tags(tags)
    path = catalog.rules_path(zone_id)
    with _WRITE_LOCK:
        rules = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                 else _new_rules(zone_id))
        nodes = rules.setdefault("nodes", {})
        spec = nodes.setdefault(node_id, {})
        if tags is None:
            spec.pop(state, None)
            if not spec:
                nodes.pop(node_id)
        else:
            spec[state] = tags
        rules["nodes"] = dict(sorted(nodes.items()))
        # Importers regenerate drafts but keep states saved here.
        edited = rules.setdefault("edited_states", {})
        if state not in edited.setdefault(node_id, []):
            edited[node_id].append(state)
        errors = [p for p in engine.validate(rules) if p["level"] == "error"]
        if errors:
            raise ValueError("; ".join(f"{p['where']}: {p['text']}"
                                       for p in errors[:5]))
        _write_json(path, rules)
    return tags


REWARD_LISTS = ("cards", "items")
REWARD_RACE_MAPS = ("chest_guid_by_race", "card_guid_by_race", "item_guid_by_race")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


STARDUST_RARITIES = ("common", "uncommon", "rare", "legendary", "promo")


def clean_stardust(entries: Any) -> list[dict[str, Any]]:
    """``[{rarity, amount, probability}]``; probability is a percentage."""
    if not isinstance(entries, list):
        raise ValueError("stardust must be a list")
    clean = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("stardust entries must be objects")
        rarity = str(entry.get("rarity") or "").lower()
        amount, probability = entry.get("amount"), entry.get("probability", 100)
        if rarity not in STARDUST_RARITIES:
            raise ValueError(f"stardust rarity must be one of {', '.join(STARDUST_RARITIES)}")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise ValueError("stardust amount must be a whole number >= 1")
        if (isinstance(probability, bool) or not isinstance(probability, (int, float))
                or not 0 < probability <= 100):
            raise ValueError("stardust probability must be above 0 and at most 100 (%)")
        clean.append({"rarity": rarity, "amount": amount, "probability": probability})
    return clean


def clean_rewards(rewards: Any) -> dict[str, Any]:
    """Validate a reward object (campaign.py ``_apply_reward_dict`` keys)."""
    if not isinstance(rewards, dict):
        raise ValueError("rewards must be an object")
    known = {"gold", "xp", "quantity", "chest_guid", "card_guid", "item_guid",
             "stardust", *REWARD_LISTS, *REWARD_RACE_MAPS}
    unknown = sorted(set(rewards) - known)
    if unknown:
        raise ValueError(f"unknown reward field(s): {', '.join(unknown)}")
    clean: dict[str, Any] = {}
    for key in ("gold", "xp", "quantity"):
        if key in rewards:
            value = rewards[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} must be a whole number >= 0")
            if value:
                clean[key] = value
    for key in ("chest_guid", "card_guid", "item_guid"):
        if rewards.get(key):
            if not _GUID.match(str(rewards[key])):
                raise ValueError(f"{key} must be a GUID")
            clean[key] = str(rewards[key]).lower()
    for key in REWARD_RACE_MAPS:
        mapping = rewards.get(key)
        if mapping:
            if not isinstance(mapping, dict):
                raise ValueError(f"{key} must map race -> GUID")
            for race, guid in mapping.items():
                if race not in catalog.FACTION_OF_RACE or not _GUID.match(str(guid)):
                    raise ValueError(f"{key}: {race!r} -> {guid!r} is not a race and GUID")
            clean[key] = {race: str(guid).lower() for race, guid in mapping.items()}
    if rewards.get("stardust"):
        clean["stardust"] = clean_stardust(rewards["stardust"])
    for key in REWARD_LISTS:
        entries = rewards.get(key)
        if entries:
            if not isinstance(entries, list):
                raise ValueError(f"{key} must be a list")
            for entry in entries:
                if not isinstance(entry, dict) or not _GUID.match(str(entry.get("guid") or "")):
                    raise ValueError(f"{key} entries need a guid")
            clean[key] = entries
    return clean


def save_quest_rewards(db: sqlite3.Connection, quest_script: str,
                       rewards: Any) -> dict[str, Any]:
    """Set a quest's completion reward in ``campaign_data/quests.json``.

    The server applies it to ``quest_templates.rewards_json`` at startup and
    grants it once per champion when the quest completes.
    """
    campaign_data = _campaign_data()
    rewards = clean_rewards(rewards)
    if not db.execute("SELECT 1 FROM quest_templates WHERE script_name=?",
                      (quest_script,)).fetchone():
        raise LookupError(f"unknown quest {quest_script!r}")
    path = campaign_data.DATA_DIR / "quests.json"
    with _WRITE_LOCK:
        data = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                else {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
        rows = data.setdefault("tables", {}).setdefault("quest_templates", [])
        row = next((r for r in rows if r.get("script_name") == quest_script), None)
        if row is None:
            row = {"script_name": quest_script}
            rows.append(row)
        row["rewards_json"] = rewards
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
    return rewards


SCENE_ENTRY_KEYS = {"gold", "xp", "one_time", "chest_guid", "card_guid", "quantity",
                    "items", "item_guid", "item_guid_by_race", "when", "stardust"}
_FLAG_TAG = re.compile(r"^flag\([^()\s][^()]*\)$")


def clean_scene_rewards(rewards: Any) -> dict[str, Any]:
    """Validate an encounter scene's ``rewards_json``.

    Either one flat entry, or ``end_of_game_rewards`` (a list of entries);
    other top-level keys (``card_choice`` ...) are kept untouched. An entry's
    only condition is ``when``: ``flag(X)`` tags that must all hold.
    """
    if not isinstance(rewards, dict):
        raise ValueError("rewards must be an object")

    def entry(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("each reward entry must be an object")
        unknown = sorted(set(raw) - SCENE_ENTRY_KEYS)
        if unknown:
            raise ValueError(f"unknown scene reward field(s): {', '.join(unknown)}")
        out: dict[str, Any] = {}
        for key in ("gold", "xp", "quantity"):
            if key in raw:
                value = raw[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{key} must be a whole number >= 0")
                if value:
                    out[key] = value
        if "one_time" in raw:
            out["one_time"] = bool(raw["one_time"])
        for key in ("chest_guid", "item_guid"):
            if raw.get(key):
                if not _GUID.match(str(raw[key])):
                    raise ValueError(f"{key} must be a GUID")
                out[key] = str(raw[key]).lower()
        if raw.get("card_guid"):
            out["card_guid"] = str(raw["card_guid"]).strip()   # a GUID or a condition token
        if raw.get("item_guid_by_race"):
            mapping = raw["item_guid_by_race"]
            if not isinstance(mapping, dict):
                raise ValueError("item_guid_by_race must map race -> GUID")
            for race, guid in mapping.items():
                if race not in catalog.FACTION_OF_RACE or not _GUID.match(str(guid)):
                    raise ValueError(f"item_guid_by_race: {race!r} -> {guid!r} is invalid")
            out["item_guid_by_race"] = {r: str(g).lower() for r, g in mapping.items()}
        if raw.get("items"):
            if not isinstance(raw["items"], list) or any(
                    not isinstance(i, dict) or not _GUID.match(str(i.get("guid") or ""))
                    for i in raw["items"]):
                raise ValueError("items entries need a guid")
            out["items"] = raw["items"]
        if raw.get("stardust"):
            out["stardust"] = clean_stardust(raw["stardust"])
        if raw.get("when"):
            tags = raw["when"]
            if not isinstance(tags, list) or any(
                    not _FLAG_TAG.match(str(tag).strip()) for tag in tags):
                raise ValueError("when must be a list of flag(X) tags")
            out["when"] = [str(tag).strip() for tag in tags]
        return out

    clean = {k: v for k, v in rewards.items()
             if k not in SCENE_ENTRY_KEYS | {"end_of_game_rewards", "card_choice"}}
    if rewards.get("card_choice"):
        choices = rewards["card_choice"]
        if not isinstance(choices, list) or any(
                not isinstance(c, dict) or not _GUID.match(str(c.get("guid") or ""))
                for c in choices):
            raise ValueError("card_choice must be a list of {guid} cards")
        clean["card_choice"] = [dict(c, guid=str(c["guid"]).lower())
                                for c in choices]
    if isinstance(rewards.get("end_of_game_rewards"), list):
        clean["end_of_game_rewards"] = [entry(r) for r in rewards["end_of_game_rewards"]]
    else:
        clean.update(entry({k: v for k, v in rewards.items() if k in SCENE_ENTRY_KEYS}))
    return clean


def save_scene_rewards(db: sqlite3.Connection, scene_guid: str,
                       rewards: Any) -> dict[str, Any]:
    """Set an encounter scene's rewards in ``campaign_data/encounter_scenes.json``.

    The server writes it to ``encounter_scenes.rewards_json`` at startup;
    only scenes already in that table can be edited.
    """
    campaign_data = _campaign_data()
    rewards = clean_scene_rewards(rewards)
    row = db.execute("SELECT guid FROM encounter_scenes WHERE LOWER(guid)=?",
                     (str(scene_guid).lower(),)).fetchone()
    if row is None:
        raise LookupError("this scene is not in the server's encounter_scenes table")
    path = campaign_data.DATA_DIR / "encounter_scenes.json"
    with _WRITE_LOCK:
        data = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                else {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
        rows = data.setdefault("tables", {}).setdefault("encounter_scenes", [])
        existing = next((r for r in rows
                         if str(r.get("guid")).lower() == str(scene_guid).lower()), None)
        if existing is None:
            existing = {"guid": row[0]}
            rows.append(existing)
        existing["rewards_json"] = rewards
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
    return rewards


SCENE_TRIGGERS = ("pre", "victory", "defeat")
_SCENE_ACTION = re.compile(r"^(set_flag|clear_flag)\(([^()\s][^()]*)\)$")


def clean_scene_actions(actions: Any) -> dict[str, list[str]]:
    """``{"pre"|"victory"|"defeat": ["set_flag(X)", "clear_flag(Y)"]}``;
    ``victory`` may also run ``check_tamed()`` (sets ``tamed:<scene8>`` when
    the opponent's void holds a tamed troop)."""
    if not isinstance(actions, dict):
        raise ValueError("actions must be an object")
    unknown = sorted(set(actions) - set(SCENE_TRIGGERS))
    if unknown:
        raise ValueError(f"unknown trigger(s): {', '.join(unknown)}")
    clean: dict[str, list[str]] = {}
    for trigger in SCENE_TRIGGERS:
        entries = [str(a).strip() for a in actions.get(trigger) or () if str(a).strip()]
        for action in entries:
            if trigger == "victory" and re.fullmatch(r"check_?tamed\(\s*\)", action, re.I):
                continue
            if not _SCENE_ACTION.match(action):
                raise ValueError(f"{trigger}: {action!r} must be set_flag(X) or "
                                 "clear_flag(X) (or check_tamed() on victory)")
        if entries:
            clean[trigger] = entries
    return clean


def save_scene_actions(db: sqlite3.Connection, scene_guid: str,
                       actions: Any) -> dict[str, list[str]]:
    """Set what a scene does before the battle and on victory / defeat."""
    campaign_data = _campaign_data()
    actions = clean_scene_actions(actions)
    row = db.execute("SELECT guid FROM encounter_scenes WHERE LOWER(guid)=?",
                     (str(scene_guid).lower(),)).fetchone()
    if row is None:
        raise LookupError("this scene is not in the server's encounter_scenes table")
    path = campaign_data.DATA_DIR / "encounter_scenes.json"
    with _WRITE_LOCK:
        data = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                else {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
        rows = data.setdefault("tables", {}).setdefault("encounter_scenes", [])
        existing = next((r for r in rows
                         if str(r.get("guid")).lower() == str(scene_guid).lower()), None)
        if existing is None:
            existing = {"guid": row[0]}
            rows.append(existing)
        existing["actions_json"] = actions
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
    return actions


def save_scene_personality(db: sqlite3.Connection, scene_guid: str,
                           personality: str | None) -> str | None:
    """Set a scene's AI deck personality, or ``None`` to go back to the
    Python deck evaluator's pick.

    Saved to ``campaign_data/encounter_scenes.json`` (``ai_deck_personality``;
    blank means auto), applied at server start.
    """
    campaign_data = _campaign_data()
    if personality is not None and personality not in catalog.DECK_PERSONALITIES:
        raise ValueError(f"personality must be one of {', '.join(catalog.DECK_PERSONALITIES)}")
    row = db.execute("SELECT guid FROM encounter_scenes WHERE LOWER(guid)=?",
                     (str(scene_guid).lower(),)).fetchone()
    if row is None:
        raise LookupError("this scene is not in the server's encounter_scenes table")
    path = campaign_data.DATA_DIR / "encounter_scenes.json"
    with _WRITE_LOCK:
        data = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                else {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
        rows = data.setdefault("tables", {}).setdefault("encounter_scenes", [])
        existing = next((r for r in rows
                         if str(r.get("guid")).lower() == str(scene_guid).lower()), None)
        if existing is None:
            existing = {"guid": row[0]}
            rows.append(existing)
        # Blank = "auto": the deck evaluator's pick (never overwritten at start).
        existing["ai_deck_personality"] = personality or ""
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
    return personality


SCENE_MOD_TARGETS = ("AIPlayer", "Player")


def clean_scene_mods(mods: Any) -> list[dict[str, Any]]:
    """Validate an encounter scene's ``mods_json``.

    Each entry is ``{"title", "description", "mods": [{"guid"}], "target"?,
    "round"?}``: a scene-wide modifier shown to the player, or (with
    ``target``) a card applied to that player at ``round``.

    With ``"choice": true`` the cards are options the player picks one of
    before the battle (the client's card-selector battle panel). Each card
    may carry ``when`` (tags: offered only while they hold) and
    ``pre`` / ``victory`` / ``defeat`` flag actions that run when it was
    the one chosen. Only the chosen card is applied; with no option on
    offer the battle starts as a normal encounter.
    """
    if not isinstance(mods, list):
        raise ValueError("mods must be a list")
    clean = []
    for entry in mods:
        if not isinstance(entry, dict):
            raise ValueError("each mod must be an object")
        item: dict[str, Any] = {}
        for key in ("title", "description"):
            if entry.get(key) not in (None, ""):
                item[key] = str(entry[key])
        choice = bool(entry.get("choice"))
        if choice:
            item["choice"] = True
        if entry.get("target"):
            if entry["target"] not in SCENE_MOD_TARGETS:
                raise ValueError(f"target must be one of {', '.join(SCENE_MOD_TARGETS)}")
            item["target"] = entry["target"]
            rnd = entry.get("round", 0)
            if isinstance(rnd, bool) or not isinstance(rnd, int) or rnd < 0:
                raise ValueError("round must be a whole number >= 0")
            item["round"] = rnd
        guids = []
        for card in entry.get("mods") or ():
            guid = str((card or {}).get("guid") or "").lower()
            if not _GUID.match(guid):
                raise ValueError(f"mod card {guid!r} is not a GUID")
            card_item: dict[str, Any] = {"guid": guid}
            if choice:
                when = clean_tags(card.get("when") or [])
                if when:
                    card_item["when"] = when
                actions = clean_scene_actions(
                    {t: card.get(t) for t in SCENE_TRIGGERS if card.get(t)})
                if any(re.fullmatch(r"check_?tamed\(\s*\)", a, re.I)
                       for a in actions.get("victory") or ()):
                    raise ValueError("check_tamed() belongs on the scene's actions")
                card_item.update(actions)
            guids.append(card_item)
        item["mods"] = guids
        if not (item.get("title") or item.get("description") or guids):
            continue                      # an empty row
        clean.append(item)
    return clean


def save_scene_mods(db: sqlite3.Connection, scene_guid: str,
                    mods: Any) -> list[dict[str, Any]]:
    """Set an encounter scene's modifiers in ``campaign_data/encounter_scenes.json``."""
    campaign_data = _campaign_data()
    mods = clean_scene_mods(mods)
    row = db.execute("SELECT guid FROM encounter_scenes WHERE LOWER(guid)=?",
                     (str(scene_guid).lower(),)).fetchone()
    if row is None:
        raise LookupError("this scene is not in the server's encounter_scenes table")
    path = campaign_data.DATA_DIR / "encounter_scenes.json"
    with _WRITE_LOCK:
        data = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                else {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
        rows = data.setdefault("tables", {}).setdefault("encounter_scenes", [])
        existing = next((r for r in rows
                         if str(r.get("guid")).lower() == str(scene_guid).lower()), None)
        if existing is None:
            existing = {"guid": row[0]}
            rows.append(existing)
        existing["mods_json"] = mods
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
    return mods
