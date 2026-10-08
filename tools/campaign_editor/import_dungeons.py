#!/usr/bin/env python3
"""Link dungeon nodes to their content by name and authored event links.

The dungeon SceneData templates mostly leave ``m_EventsToFireWhenVisited``
empty (Devonshire Keep, Brutecrown Bluff, Ruins of Kukatan ...) or wire
placeholders (The Tranquil Dream's Turtle node starts the *Bear* battle).
The content itself is named after its node: "Ruins of Kukatan - Stagnant
Pool" (+ " Success" / " Fail"), scene "Ruins of Kukatan A - Stagnant Pool",
"Tranquil Dream Skarn Easy/Medium/Hard". This writes
``campaign_data/dungeons.json`` - one ``campaign_node_conversations`` row per
name-matched conversation (template = the dungeon's Records name), tagged
``outcome:success|fail`` and ``faction(...)`` from the name - and reports
nodes with no content and Records events that name another node.
Encounter scenes are matched by name at load time (zones.json
``scene_match: items``); Fort Romor uses ``items_with_event_fallback`` so its
authored ``UIEventBeginEncounter`` links can place opponent-named battles. The server does
not run these dungeons (only Castle Crayburn); the rows are editor data until
it does.

    python3 tools/campaign_editor/import_dungeons.py
"""

from __future__ import annotations

import json
import re
import sys

import catalog

_POINT_DIFFICULTIES = {
    "one_point_answer": "easy",
    "two_point_answer": "medium",
    "three_point_answer": "hard",
}

# Explicit links handle conversation names that do not identify their map node.
_CONVERSATION_NODE_LINKS = {
    # Fort Romor's entrance conversation is attached to the Gate item even
    # though its name ends in "Entrance".
    ("Fort Romor", "f688a1b3-ccd3-41d2-bd1e-cc2d40079ed3"):
        ("NodeA", "Gate - Entrance"),
    # These result conversations describe the aftermath of Devonshire Keep A
    # (NodeA / Gate), but Records names them for the courtyard and does not link
    # their GUIDs from the dungeon's SceneData.
    ("Devonshire Keep", "2547fcee-3fe3-4be4-b990-00a0e24ec48f"):
        ("NodeA", "Courtyard Fail"),
    ("Devonshire Keep", "b2f0c9ac-8045-4b55-a2c2-ca47389177ab"):
        ("NodeA", "Courtyard Success - Hidden Path Unlocked"),
    ("Devonshire Keep", "1830bd1d-2f4f-422c-9aea-1ba27efc65d0"):
        ("NodeA", "Courtyard Success - Hidden Path NOT Unlocked"),
}


def point_battle_actions(records_name: str, node: str, conversation: dict,
                         items: dict[str, str], prefixes: list[str],
                         scenes: dict[str, str]) -> dict[str, list[str]]:
    """Map Tranquil Dream's scored conversation outcomes to scene variants.

    The outcome event names and the Easy/Medium/Hard encounter variants are
    authored separately in Records. Join both by this node's authored name;
    require all three answer events and exactly one scene per difficulty.
    """
    if records_name != "The Tranquil Dream":
        return {}

    point_events = set()
    for answer in (conversation.get("answers") or {}).values():
        for event in answer.get("events") or ():
            if event.get("type") == "ServerScriptEvent":
                name = str(event.get("EventName") or "")
                if name in _POINT_DIFFICULTIES:
                    point_events.add(name)
    if point_events != set(_POINT_DIFFICULTIES):
        return {}

    scenes_by_difficulty: dict[str, list[str]] = {}
    for guid, name in scenes.items():
        scene_node, variant = catalog.match_item(name, items, prefixes)
        difficulty = str(variant or "").lower()
        if scene_node == node and difficulty in set(_POINT_DIFFICULTIES.values()):
            scenes_by_difficulty.setdefault(difficulty, []).append(guid)

    actions = {}
    for event, difficulty in _POINT_DIFFICULTIES.items():
        matches = scenes_by_difficulty.get(difficulty, [])
        if len(matches) != 1:
            return {}
        actions[event] = [f"encounter({matches[0]})"]
    return actions


def faction_tag(rest: str) -> list[str]:
    for side in ("Ardent", "Underworld"):
        if re.search(rf"\b{side.lower()}\b", rest):
            return [f"faction({side})"]
    return []


def main():
    sys.path.insert(0, str(catalog.REPO))
    import campaign_data
    conversations = catalog.conversations()
    scenes = {catalog._guid(r.get("m_Id")): r.get("m_Name", "")
              for r in catalog.records("SceneData") if r["_t"].endswith("EncounterScene")}
    rows, report = [], []
    for zone in catalog.zones():
        if zone["kind"] != "dungeon" or zone["id"] == "crayburn" or not zone.get("records"):
            continue
        template = catalog.scene_template(zone["records"]) or {}
        prefixes = catalog.dungeon_prefixes(zone["records"])
        # Keyed by map node (catalog._authored_items places items whose
        # m_MapNodeId is not a map node, e.g. Fort Romor's "Stables").
        authored = catalog._authored_items(template)
        items = {node: item["name"] for node, item in authored.items()}
        item_node = {i.get("name", ""): node for node, i in authored.items()}
        linked_encounter_nodes = (catalog.encounter_event_nodes(authored)
                                  if zone.get("scene_match") ==
                                  "items_with_event_fallback" else {})
        found: dict[str, list] = {}
        for guid, conv in sorted(conversations.items(), key=lambda x: x[1]["name"]):
            node, rest = _CONVERSATION_NODE_LINKS.get(
                (zone["records"], guid), (None, ""))
            if not node:
                node, rest = catalog.match_item(conv["name"], items, prefixes)
            if not node:
                continue
            tags = []
            if re.search(r"\bsuccess\b", rest, re.I):
                tags.append("outcome:success")
            elif re.search(r"\bfail(ed)?\b", rest, re.I):
                tags.append("outcome:fail")
            tags += faction_tag(rest)
            found.setdefault(node, []).append(conv["name"])
            trigger = {"label": catalog.item_title(items[node]) + (f" - {rest}" if rest else ""),
                       "source": "import_dungeons.py"}
            if tags:
                trigger["tags"] = tags
            actions = point_battle_actions(
                zone["records"], node, conv, items, prefixes, scenes)
            if actions:
                trigger["actions"] = actions
            rows.append({"campaign_template": zone["records"], "conversation_guid": guid,
                         "node_id": node, "conversation_name": conv["name"],
                         "priority": len(found[node]), "enabled": True,
                         "trigger_json": trigger})
        battles = {}
        for guid, name in scenes.items():
            node, rest = catalog.match_item(name, items, prefixes)
            nodes = [node] if node else linked_encounter_nodes.get(guid, [])
            for node_id in nodes:
                battles.setdefault(node_id, []).append(rest or "-")
        # Records events naming another node's content.
        miswired = []
        for item in template.get("m_ItemData") or ():
            node = item_node.get(item.get("m_Name", ""), item.get("m_MapNodeId"))
            for event in item.get("m_EventsToFireWhenVisited") or ():
                guid = catalog._guid(event.get("m_ConversationId") or event.get("m_EncounterId"))
                name = (conversations.get(guid) or {}).get("name") or scenes.get(guid, "")
                explicit = _CONVERSATION_NODE_LINKS.get((zone["records"], guid))
                if explicit:
                    other, _rest = explicit
                else:
                    other, _rest = (catalog.match_item(name, items, prefixes)
                                    if name else (None, ""))
                if other and other != node:
                    miswired.append(f"{catalog.item_title(item.get('m_Name'))} -> "
                                    f"{name} (named for {catalog.item_title(items[other])})")
        empty = [catalog.item_title(t) for n, t in items.items()
                 if n not in found and n not in battles]
        report.append((zone["title"], len(items), len(found), len(battles), empty, miswired))
    # Keep tags / actions already authored in the editor on existing rows.
    path = catalog.DATA_DIR / "dungeons.json"
    try:
        previous = {(r["campaign_template"], str(r["conversation_guid"]).lower()): r
                    for r in json.loads(path.read_text(encoding="utf-8"))
                    ["tables"]["campaign_node_conversations"]}
    except (OSError, ValueError, KeyError):
        previous = {}
    for row in rows:
        old = previous.get((row["campaign_template"],
                            str(row["conversation_guid"]).lower()))
        if old:
            for key in ("tags", "actions"):
                if key in (old.get("trigger_json") or {}):
                    row["trigger_json"][key] = old["trigger_json"][key]
    data = {"schema_version": 1,
            "description": "Dungeon node conversations matched by name and explicit links "
                           "(generated by tools/campaign_editor/import_dungeons.py)",
            "tables": {"campaign_node_conversations": rows}}
    bad = campaign_data.validate(data)
    if bad:
        raise SystemExit(bad[:3])
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"{path}: {len(rows)} conversations")
    for title, total, with_conv, with_battle, empty, miswired in report:
        print(f"{title}: {total} nodes, {with_conv} with conversations, "
              f"{with_battle} with battles")
        if empty:
            print(f"  no named content: {', '.join(empty)}")
        for line in miswired:
            print(f"  Records event mis-wired: {line}")


if __name__ == "__main__":
    main()
