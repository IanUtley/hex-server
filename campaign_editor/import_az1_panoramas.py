#!/usr/bin/env python3
"""Give each AZ1 panorama (Cave-In, Ambling Mesa, Thunderfield) its own rules.

Until now a panorama's NPC conversations were rows of its parent overworld
node (``Node034``), told apart by name matching. This moves them into the
panorama: each assigned row's ``node_id`` becomes the NPC's name, the
panorama gets ``campaign/campaign_rules/<zone>.json`` (its NPCs
are its nodes) and ``campaign/campaign_rules/zones.json`` records
its NPCs and scene.

Rows the scene has no NPC for (Ambling Mesa's Vincent and Kian) stay where
they are and are listed; the client shows no NPC without an anchor in the
scene either, so nothing changes for the player.

    python3 campaign_editor/import_az1_panoramas.py [--db hconnect.db] [--dry-run]

A conversation that also has a ``quest_conversations`` row is placed by that
quest row (its NPC anchor); its ``campaign_node_conversations`` row is left
where it is (usually the parent node), so the NPC does not get a duplicate
entry. An existing panorama rules file is kept as it is (hand-set gates, node
order, start, metadata); only NPCs it does not list yet are added.

    --dry-run       report which files would change; write nothing
    --out-dir DIR   write the files into DIR instead (to diff against the live ones)

Idempotent: run again after adding rows to a panorama's parent node.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from paths import CAMPAIGN_ROOT, HERE, REPO  # noqa: E402  (also sets sys.path)

import catalog  # noqa: E402
import campaign_rules  # noqa: E402

# Panoramas per overworld template (their parent overworld is that template).
PANORAMAS_BY_TEMPLATE = {
    "AZ1": ("cavein", "amblingmesa", "thunderfield"),
    "AZ2": ("necropolis", "wakuna", "naagaan"),
}


def _scene_guid(db, template: str, node: str) -> str:
    """The encounter scene of an overworld map node (``... - PANORAMA``)."""
    for row in db.execute("SELECT guid, name FROM encounter_scenes "
                          "WHERE name LIKE ? ORDER BY name",
                          (f"{template[:2]} {template[2:]} - NODE %",)):
        if (catalog.scene_node_id(row["name"]) == node and
                "PANORAMA" in row["name"].upper()):
            return str(row["guid"]).lower()
    return ""


def _upsert(rows: list[dict], key: dict, **values) -> None:
    found = next((r for r in rows if all(str(r.get(k)).lower() == str(v).lower()
                                         for k, v in key.items())), None)
    if found is None:
        found = dict(key)
        rows.append(found)
    found.update(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO / "hconnect.db")
    parser.add_argument("--template", default="AZ1", choices=sorted(PANORAMAS_BY_TEMPLATE))
    parser.add_argument("--dry-run", action="store_true",
                        help="report which files would change; write nothing")
    parser.add_argument("--out-dir", type=Path,
                        help="write the files into this directory instead (for a diff)")
    args = parser.parse_args()
    outputs: dict[Path, str] = {}
    TEMPLATE = args.template
    PANORAMAS = PANORAMAS_BY_TEMPLATE[TEMPLATE]
    db = catalog.connect(args.db)

    data_path = catalog.DATA_DIR / f"{TEMPLATE}.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    tables = data.setdefault("tables", {})
    node_rows = tables.setdefault("campaign_node_conversations", [])
    quest_rows = tables.setdefault("quest_conversations", [])
    zones_path = CAMPAIGN_ROOT / "campaign_rules" / "zones.json"
    zones = json.loads(zones_path.read_text(encoding="utf-8"))
    by_id = {z["id"]: z for z in zones["zones"]}

    # An NPC in two panoramas (Myaa) needs a node id per zone; the client
    # still sees the plain name (the part before "@").
    scene_names = {}
    for zone_id in PANORAMAS:
        parent_view = dict(catalog.zone(zone_id), npcs=None, server_template=None)
        scene_names[zone_id] = [n["name"] for n in
                                catalog.panorama_payload(db, parent_view)["panorama"]["npcs"]]
    counts = {}
    for names in scene_names.values():
        for name in names:
            counts[name] = counts.get(name, 0) + 1

    def node_id(zone_id, name):
        return f"{name}@{zone_id}" if counts[name] > 1 else name

    for zone_id in PANORAMAS:
        entry = catalog.zone(zone_id)
        # Rows still at the parent node (before the move) or already moved.
        parent_view = dict(entry, npcs=None, server_template=None)
        payload = catalog.panorama_payload(db, parent_view)["panorama"]
        npcs = [node_id(zone_id, n["name"]) for n in payload["npcs"]]
        moved = unplaced = 0
        # Conversations a quest row places: their quest row is the NPC anchor.
        quest_guids = {str(r.get("conversation_guid") or "").lower() for r in quest_rows
                       if str(r.get("campaign_template") or "").upper() == TEMPLATE}
        for npc in payload["npcs"]:
            for row in npc["conversations"]:
                if str(row["guid"]).lower() in quest_guids:
                    continue
                _upsert(node_rows, {"campaign_template": TEMPLATE,
                                    "conversation_guid": row["guid"]},
                        node_id=node_id(zone_id, npc["name"]))
                moved += 1
            for row in npc["quest_links"]:
                _upsert(quest_rows, {"quest_script": row["quest"],
                                     "conversation_guid": row["guid"],
                                     "campaign_template": TEMPLATE},
                        node_id=node_id(zone_id, npc["name"]))
                moved += 1
        unplaced = len(payload["unassigned"])
        rules = {
            "schema_version": campaign_rules.SCHEMA_VERSION,
            "campaign": zone_id, "scene": entry["title"],
            "generated_by": "campaign_editor/import_az1_panoramas.py",
            "start": {"node": npcs[0] if npcs else "", "node_completed": False,
                      "flags": []},
            "map": {"paths": []},
            "nodes": {npc: {} for npc in npcs},
            "hooks": [],
        }
        rules_path = catalog.rules_path(zone_id)
        if rules_path.exists():       # the file is hand-maintained: add new NPCs only
            rules = json.loads(rules_path.read_text(encoding="utf-8"))
            nodes = rules.setdefault("nodes", {})
            for npc in npcs:
                nodes.setdefault(npc, {})
        zone = by_id[zone_id]
        zone.update(server_template=TEMPLATE, npcs=npcs,
                    scene_guid=_scene_guid(db, TEMPLATE, entry["parent_node"]),
                    campaign_group="AREA")
        print(f"{zone_id}: {len(npcs)} NPCs, {moved} rows moved, "
              f"{unplaced} left at {entry['parent_node']} (no NPC in the scene)")
        outputs[rules_path] = json.dumps(rules, indent=1) + "\n"

    problems = __import__("campaign_data").validate(data)
    if problems:
        raise SystemExit(f"campaign/campaign_data/{TEMPLATE}.json: {problems[:3]}")
    outputs[data_path] = json.dumps(data, indent=1) + "\n"
    outputs[zones_path] = json.dumps(zones, indent=1) + "\n"
    changed = False
    for path, text in outputs.items():
        same = path.read_text(encoding="utf-8") == text if path.exists() else False
        changed |= not same
        target = args.out_dir / path.name if args.out_dir else path
        print(f"{path.name}: {'unchanged' if same else 'changes'}")
        if args.dry_run or (same and not args.out_dir):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if changed and not args.dry_run and not args.out_dir:
        print("written; restart the server so the table rows are applied")


if __name__ == "__main__":
    main()
