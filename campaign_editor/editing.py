"""Admin campaign edits, saved to the shared campaign files.

* Conversation tags -> ``campaign/campaign_data/<template>.json`` as a
  ``campaign_node_conversations`` row (``trigger_json.tags``); the server
  applies it to the database at startup (``campaign_data.apply_all``).
* Node state tags -> ``campaign/campaign_rules/<zone>.json`` ``nodes.<id>.<state>``.

Records and the database are never written. Every save validates the whole
file first and replaces it atomically, under one lock, so concurrent
requests cannot interleave a read-modify-write.
"""

from __future__ import annotations

import copy
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


def _read_doc(path: Path, fallback: Any = None) -> Any:
    """Read a shared authored campaign document."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return copy.deepcopy(data) if data is not None else fallback


def _campaign_data():
    if str(catalog.CAMPAIGN_ROOT) not in sys.path:
        sys.path.insert(0, str(catalog.CAMPAIGN_ROOT))
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
            data = _read_doc(path, {})
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
            data = _read_doc(path, {"schema_version": campaign_data.SCHEMA_VERSION,
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
        _check_quest_refs(db, parsed)
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
        data = _read_doc(path, {"schema_version": campaign_data.SCHEMA_VERSION,
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


def quest_step_ids(script: str) -> list[str]:
    """The step ids of a campaign-wide quest, in order (quests.json)."""
    doc = _read_doc(catalog.CAMPAIGN_ROOT / "campaign_rules" / "quests.json", {})
    spec = (doc.get("quests") or {}).get(script) or {}
    return [str(step.get("id") or "") for step in spec.get("steps") or ()]


def _check_quest_refs(db: sqlite3.Connection, parsed: dict[str, Any]) -> None:
    """An action's quest must exist; complete_step's step must be on it."""
    quest = parsed.get("start_quest") or parsed.get("complete_quest")
    if quest and not db.execute(
            "SELECT 1 FROM quest_templates WHERE script_name=?",
            (quest,)).fetchone():
        raise LookupError(f"unknown quest {quest!r}")
    if "complete_step" in parsed:
        script, step_id = parsed["complete_step"]
        if not db.execute(
                "SELECT 1 FROM quest_templates WHERE script_name=?",
                (script,)).fetchone():
            raise LookupError(f"unknown quest {script!r}")
        if step_id not in quest_step_ids(script):
            raise LookupError(f"quest {script!r} has no step {step_id!r}")


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
        _check_quest_refs(db, parsed)
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
    # A cleared tag list means "no tags", not an always-true gate: remove it.
    if not tags:
        tags = None
    path = catalog.rules_path(zone_id)
    with _WRITE_LOCK:
        rules = _read_doc(path) or _new_rules(zone_id)
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


def save_offer_order(zone_id: str, node_id: str, order: Any) -> list[str]:
    """Set the authored order of one node's offers (offer ids, first wins).

    Offers keep their conditions; the order only decides which of several
    offers that are all applicable comes first. An empty list removes the
    saved order, so the generated order is used again.
    """
    if not isinstance(order, list):
        raise ValueError("order must be a list of offer ids")
    ids = []
    for value in order:
        offer = str(value or "").strip()
        if not offer or len(offer) > 120 or offer in ids:
            raise ValueError(f"bad offer id {value!r}")
        ids.append(offer)
    path = catalog.rules_path(zone_id)
    with _WRITE_LOCK:
        rules = _read_doc(path) or _new_rules(zone_id)
        saved = rules.setdefault("offer_order", {})
        if ids:
            saved[node_id] = ids
        else:
            saved.pop(node_id, None)
        if not saved:
            rules.pop("offer_order", None)
        _write_json(path, rules)
    return ids


def save_conversation_override(zone_id: str, conversation_guid: str, ending: str,
                               actions: Any) -> list[str]:
    """Set the actions a conversation runs when it ends (``*`` = any ending)
    for a conversation no node offer covers, e.g. a scene's visit conversation.
    Stored in the zone's rules file as ``conversation_actions``."""
    from campaign_rules import RulesError, parse_action
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")
    clean = []
    for value in actions:
        action = str(value or "").strip()
        if not action:
            continue
        try:
            parse_action(action)
        except RulesError as exc:
            raise ValueError(str(exc)) from exc
        clean.append(action)
    guid = str(conversation_guid or "").strip().lower()
    ending = str(ending or "*").strip() or "*"
    if not guid:
        raise ValueError("a conversation guid is needed")
    path = catalog.rules_path(zone_id)
    with _WRITE_LOCK:
        rules = _read_doc(path) or _new_rules(zone_id)
        saved = rules.setdefault("conversation_actions", {})
        endings = saved.setdefault(guid, {})
        if clean:
            endings[ending] = clean
        else:
            endings.pop(ending, None)
        if not endings:
            saved.pop(guid, None)
        if not saved:
            rules.pop("conversation_actions", None)
        _write_json(path, rules)
    return clean


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


def clean_inventory_grants(entries: Any, key: str) -> list[dict[str, Any]]:
    """``[{guid, quantity}]`` of account inventory items (``key`` names the field)."""
    if not isinstance(entries, list):
        raise ValueError(f"{key} must be a list")
    clean = []
    for entry in entries:
        if not isinstance(entry, dict) or not _GUID.match(str(entry.get("guid") or "")):
            raise ValueError(f"{key} entries need a guid")
        # Mercenaries and sleeves are always granted as a single copy.
        clean.append({"guid": str(entry["guid"]).lower(), "quantity": 1})
    return clean


def clean_mercenaries(entries: Any) -> list[dict[str, Any]]:
    """``[{guid, quantity}]``: InventoryMercenaryData GUIDs (a mercenary, not a card)."""
    return clean_inventory_grants(entries, "mercenaries")


def clean_sleeves(entries: Any) -> list[dict[str, Any]]:
    """``[{guid, quantity}]``: DeckSleeve InventoryItemData GUIDs."""
    return clean_inventory_grants(entries, "sleeves")


def clean_rewards(rewards: Any) -> dict[str, Any]:
    """Validate a reward object (campaign.py ``_apply_reward_dict`` keys)."""
    if not isinstance(rewards, dict):
        raise ValueError("rewards must be an object")
    known = {"gold", "xp", "quantity", "chest_guid", "card_guid", "item_guid",
             "stardust", "mercenaries", "sleeves", "deck_guid",
             *REWARD_LISTS, *REWARD_RACE_MAPS}
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
    for key in ("chest_guid", "card_guid", "item_guid", "deck_guid"):
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
    if rewards.get("mercenaries"):
        clean["mercenaries"] = clean_mercenaries(rewards["mercenaries"])
    if rewards.get("sleeves"):
        clean["sleeves"] = clean_sleeves(rewards["sleeves"])
    for key in REWARD_LISTS:
        entries = rewards.get(key)
        if entries:
            if not isinstance(entries, list):
                raise ValueError(f"{key} must be a list")
            for entry in entries:
                if not isinstance(entry, dict) or not _GUID.match(str(entry.get("guid") or "")):
                    raise ValueError(f"{key} entries need a guid")
            # Equipment is one copy per reward entry, never a stack.
            clean[key] = [{**entry, "quantity": 1} if key == "items" else entry
                          for entry in entries]
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
        data = _read_doc(path, {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
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
                    "items", "mercenaries", "sleeves", "item_guid", "item_guid_by_race",
                    "deck_guid", "when", "stardust"}
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
            out["items"] = [{**i, "quantity": 1} for i in raw["items"]]
        if raw.get("mercenaries"):
            out["mercenaries"] = clean_mercenaries(raw["mercenaries"])
        if raw.get("sleeves"):
            out["sleeves"] = clean_sleeves(raw["sleeves"])
        if raw.get("deck_guid"):
            if not _GUID.match(str(raw["deck_guid"])):
                raise ValueError("deck_guid must be a GUID")
            out["deck_guid"] = str(raw["deck_guid"]).lower()
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
        data = _read_doc(path, {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
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
        data = _read_doc(path, {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
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


def clean_battle_tags(tags):
    """Validate battle tags the way conversation tags are: a tag list, or
    "or" groups (a list of tag lists). Outcome tags belong to result
    conversations, so a battle refuses them."""
    import campaign_rules

    def one(tag):
        text = str(tag or "").strip()
        if not text:
            return ""
        try:
            condition = campaign_rules.parse_tag(text)
        except campaign_rules.RulesError as exc:
            raise ValueError(f"unreadable battle tag {text!r}: {exc}") from exc
        if "outcome" in condition:
            raise ValueError("a battle takes condition tags; outcome tags belong to result conversations")
        return text

    if campaign_rules.is_groups(tags):
        groups = [[t for t in (one(x) for x in group) if t] for group in tags]
        return [group for group in groups if group]
    return [t for t in (one(x) for x in (tags or [])) if t]


def save_scene_tags(db: sqlite3.Connection, scene_guid: str,
                    tags) -> list | list[list[str]]:
    """Set the condition tags that make a battle scene offerable (see
    campaign_runtime.offers.battle_offers). An empty list leaves the battle
    untagged, which makes it the node's default battle."""
    campaign_data = _campaign_data()
    cleaned = clean_battle_tags(tags)
    row = db.execute("SELECT guid FROM encounter_scenes WHERE LOWER(guid)=?",
                     (str(scene_guid).lower(),)).fetchone()
    if row is None:
        raise LookupError("this scene is not in the server's encounter_scenes table")
    path = campaign_data.DATA_DIR / "encounter_scenes.json"
    with _WRITE_LOCK:
        data = _read_doc(path, {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
        rows = data.setdefault("tables", {}).setdefault("encounter_scenes", [])
        existing = next((r for r in rows
                         if str(r.get("guid")).lower() == str(scene_guid).lower()), None)
        if existing is None:
            existing = {"guid": row[0]}
            rows.append(existing)
        existing["scene_tags"] = cleaned
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
    return cleaned


def save_scene_personality(db: sqlite3.Connection, scene_guid: str,
                           personality: str | None) -> str | None:
    """Set a scene override, or clear it to use the precomputed JSON value."""
    campaign_data = _campaign_data()
    if personality is not None and personality not in catalog.DECK_PERSONALITIES:
        raise ValueError(f"personality must be one of {', '.join(catalog.DECK_PERSONALITIES)}")
    row = db.execute(
        "SELECT guid, ai_deck_guid, ai_deck_personality FROM encounter_scenes "
        "WHERE LOWER(guid)=?", (str(scene_guid).lower(),)).fetchone()
    if row is None:
        raise LookupError("this scene is not in the server's encounter_scenes table")
    path = campaign_data.DATA_DIR / "encounter_scenes.json"
    with _WRITE_LOCK:
        data = _read_doc(path, {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
        rows = data.setdefault("tables", {}).setdefault("encounter_scenes", [])
        existing = next((r for r in rows
                         if str(r.get("guid")).lower() == str(scene_guid).lower()), None)
        if existing is None:
            existing = {
                "guid": row[0],
                "ai_deck_personality": str(row[2] or ("Default" if row[1] else "")),
            }
            rows.append(existing)
        existing["ai_deck_personality_override"] = personality or ""
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
    return personality

def save_scene_player(db: sqlite3.Connection, scene_guid: str, champion: str | None,
                      deck: str | None) -> dict[str, str]:
    """Set (or clear, when blank) the player champion and deck for one battle scene.

    Champions must be ChampionTemplate records and decks EncounterDeck records
    with cards; blank keeps the player's own champion or deck. Written to the
    scene's campaign_data row and to the database at once.
    """
    from campaign_runtime import catalog_data
    options = catalog_data.player_options(db)
    champion = (champion or "").strip().lower()
    deck = (deck or "").strip().lower()
    if champion and champion not in {c["guid"] for c in options["champions"]}:
        raise ValueError("the player champion must be a ChampionTemplate")
    if deck and deck not in {d["guid"] for d in options["decks"]}:
        raise ValueError("the player deck must be an encounter deck from Records")
    row = db.execute("SELECT guid FROM encounter_scenes WHERE LOWER(guid)=?",
                     (str(scene_guid).lower(),)).fetchone()
    if row is None:
        raise LookupError("this scene is not in the server's encounter_scenes table")
    campaign_data = _campaign_data()
    path = campaign_data.DATA_DIR / "encounter_scenes.json"
    with _WRITE_LOCK:
        data = _read_doc(path, {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
        rows = data.setdefault("tables", {}).setdefault("encounter_scenes", [])
        existing = next((r for r in rows
                         if str(r.get("guid")).lower() == str(scene_guid).lower()), None)
        if existing is None:
            existing = {"guid": row[0]}
            rows.append(existing)
        existing["player_champion_guid"] = champion
        existing["player_deck_guid"] = deck
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
    db.execute("UPDATE encounter_scenes SET player_champion_guid=?, player_deck_guid=? "
               "WHERE LOWER(guid)=?", (champion, deck, str(scene_guid).lower()))
    db.commit()
    return {"player_champion": champion, "player_deck": deck}


# The client's Game.Shared.GameboardEnums (HexClient GameboardEnums.cs); the
# editor's Deck panel offers the same list.
GAMEBOARDS = ("Default", "FrostArena", "Forest", "CastleExterior", "Coastline",
              "CanyonDesert", "Highlands", "Junkyard", "Jungle", "Boat", "Wizard",
              "FrostLock", "Badlands", "DreamForest", "SanctumOfSuffering",
              "GreatMachineGraveyard")


def save_scene_gameboard(db: sqlite3.Connection, scene_guid: str, gameboard: Any) -> str:
    """Set the gameboard one encounter scene is played on.

    The value is a GameboardEnums name (plain letters/digits). Written to the
    scene's campaign_data/encounter_scenes.json row and to the database at once.
    """
    gameboard = str(gameboard or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]+", gameboard):
        raise ValueError("the gameboard must be a non-empty name of letters and digits")
    if gameboard not in GAMEBOARDS:
        raise ValueError(f"gameboard must be one of {', '.join(GAMEBOARDS)}")
    row = db.execute("SELECT guid FROM encounter_scenes WHERE LOWER(guid)=?",
                     (str(scene_guid).lower(),)).fetchone()
    if row is None:
        raise LookupError("this scene is not in the server's encounter_scenes table")
    campaign_data = _campaign_data()
    path = campaign_data.DATA_DIR / "encounter_scenes.json"
    with _WRITE_LOCK:
        data = _read_doc(path, {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
        rows = data.setdefault("tables", {}).setdefault("encounter_scenes", [])
        existing = next((r for r in rows
                         if str(r.get("guid")).lower() == str(scene_guid).lower()), None)
        if existing is None:
            existing = {"guid": row[0]}
            rows.append(existing)
        existing["gameboard"] = gameboard
        problems = campaign_data.validate(data)
        if problems:
            raise ValueError("; ".join(problems[:5]))
        _write_json(path, data)
        db.execute("UPDATE encounter_scenes SET gameboard=? WHERE LOWER(guid)=?",
                   (gameboard, str(scene_guid).lower()))
        db.commit()
    return gameboard


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
        data = _read_doc(path, {"schema_version": campaign_data.SCHEMA_VERSION, "tables": {}})
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
