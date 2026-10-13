#!/usr/bin/env python3
"""Write ``campaign/campaign_data/encounter_scenes.json``: the campaign editor's full
definition of every encounter scene (the editor, not the Records seed, owns the
server's ``encounter_scenes`` table).

    python3 campaign_editor/import_encounter_scenes.py            # snapshot hconnect.db
    python3 campaign_editor/import_encounter_scenes.py --from-records

``--from-db`` (default) snapshots the table as it stands, so it captures the
authored rewards, actions, mods and personalities exactly. ``--from-records``
rebuilds the file from the Records seed (name, title, gameboard, deck,
champion, mods, default rewards); use it to start over or after a Records
refresh - existing editor edits are re-applied from the old file by guid.

The stored AI personality is copied as-is. These values are precomputed and
belong in the JSON snapshot; this importer does not evaluate decks.

Fields only the editor writes (``EDITOR_KEYS``: battle tags, the player
champion/deck, the personality override, and - with ``--from-records`` - the
gameboard, mods, rewards and actions) are kept from the existing file, so a
re-run reproduces it. ``--dry-run`` reports whether the file would change;
``--out-dir DIR`` writes it into DIR instead (to diff against the live file).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from paths import CAMPAIGN_ROOT, HERE, REPO as ROOT  # noqa: E402  (also sets sys.path)

import campaign_data  # noqa: E402

OUT = campaign_data.DATA_DIR / "encounter_scenes.json"
# Editor-owned fields of an existing row that a snapshot keeps (the DB has no
# scene_tags column; player_* and the override may be blank in the DB).
EDITOR_KEYS = ("scene_tags", "player_champion_guid", "player_deck_guid",
               "ai_deck_personality_override")
# Also kept by --from-records (edited in the editor; the Records value is only
# the starting point).
RECORDS_KEPT_KEYS = ("gameboard", "mods_json", "rewards_json", "actions_json",
                     "ai_deck_personality") + EDITOR_KEYS
COLUMNS = ("guid", "name", "title", "gameboard", "ai_deck_guid", "ai_champion_guid",
           "mods_json", "rewards_json", "actions_json", "ai_deck_personality")


def _loads(value, default):
    if isinstance(value, (list, dict)):
        return value
    try:
        loaded = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return loaded if loaded is not None else default


def _like_old(row: dict, old: dict) -> dict:
    """``row`` in the existing row's key order, reusing its equal values (so an
    unchanged scene is written byte-for-byte as before)."""
    if not old:
        return row
    keys = [k for k in old if k in row] + [k for k in row if k not in old]
    return {k: (old[k] if old.get(k) == row[k] else row[k]) for k in keys}


def rows_from_db(db) -> list[dict]:
    previous = {}
    if OUT.exists():
        previous = {
            row["guid"].lower(): row
            for row in json.loads(OUT.read_text(encoding="utf-8"))["tables"]["encounter_scenes"]
        }
    rows = []
    for r in db.execute(f"SELECT {', '.join(COLUMNS)} FROM encounter_scenes "
                        "ORDER BY name, guid"):
        row = dict(zip(COLUMNS, r))
        guid = str(row["guid"]).lower()
        stored = str(row["ai_deck_personality"] or "").strip()
        old = previous.get(guid, {})
        default = str(old.get("ai_deck_personality") or stored or
                      ("Default" if row["ai_deck_guid"] else "")).strip()
        override = str(old.get("ai_deck_personality_override") or "").strip()
        if not override and stored and stored != default:
            override = stored
        out = {"guid": guid, "name": row["name"] or "",
               "title": row["title"] or "", "gameboard": row["gameboard"] or "",
               "ai_deck_guid": row["ai_deck_guid"] or "",
               "ai_champion_guid": row["ai_champion_guid"] or "",
               "mods_json": _loads(row["mods_json"], []),
               "rewards_json": _loads(row["rewards_json"], {})}
        actions = _loads(row["actions_json"], {})
        if actions or "actions_json" in old:
            out["actions_json"] = actions
        out["ai_deck_personality"] = default
        if override:
            out["ai_deck_personality_override"] = override
        for key in EDITOR_KEYS:
            if key in old:
                out[key] = old[key]
        rows.append(_like_old(out, old))
    return rows
def rows_from_records(db) -> list[dict]:
    from AssetExtraction.gamedata_seed import extract
    previous = {}
    if OUT.exists():
        for r in json.loads(OUT.read_text(encoding="utf-8"))["tables"]["encounter_scenes"]:
            previous[r["guid"].lower()] = r
    rows = []
    for guid, name, title, gameboard, deck, champion, mods, rewards in \
            extract()["tables"]["encounter_scenes"]:
        row = {"guid": str(guid).lower(), "name": name, "title": title,
               "gameboard": gameboard, "ai_deck_guid": deck, "ai_champion_guid": champion,
               "mods_json": _loads(mods, []), "rewards_json": _loads(rewards, {}),
               "ai_deck_personality": "Default" if deck else ""}
        old = previous.get(row["guid"])
        if old:                              # keep what the editor authored
            for key in RECORDS_KEPT_KEYS:
                if key in old:
                    row[key] = old[key]
        if deck and not row["ai_deck_personality"]:
            row["ai_deck_personality"] = "Default"
        rows.append(_like_old(row, old or {}))
    return sorted(rows, key=lambda r: (r["name"], r["guid"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "hconnect.db"))
    parser.add_argument("--from-records", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    db = sqlite3.connect(args.db)
    rows = rows_from_records(db) if args.from_records else rows_from_db(db)
    data = {"schema_version": campaign_data.SCHEMA_VERSION,
            "description": "Encounter scenes (full definitions) - owned by the campaign editor; "
                           "applied to encounter_scenes at every start",
            "tables": {"encounter_scenes": rows}}
    problems = campaign_data.validate(data)
    if problems:
        raise SystemExit("; ".join(problems[:5]))
    text = json.dumps(data, indent=1) + "\n"
    same = OUT.exists() and OUT.read_text(encoding="utf-8") == text
    target = args.out_dir / OUT.name if args.out_dir else OUT
    if not args.dry_run and (args.out_dir or not same):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    with_personality = sum(1 for r in rows if r["ai_deck_personality"])
    print(f"{len(rows)} scenes -> {target} ({with_personality} stored personalities; "
          f"{'unchanged' if same else 'would change' if args.dry_run else 'written'})")


if __name__ == "__main__":
    main()
