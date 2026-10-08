#!/usr/bin/env python3
"""Write ``campaign_data/encounter_scenes.json``: the campaign editor's full
definition of every encounter scene (the editor, not the Records seed, owns the
server's ``encounter_scenes`` table).

    python3 tools/campaign_editor/import_encounter_scenes.py            # snapshot hconnect.db
    python3 tools/campaign_editor/import_encounter_scenes.py --from-records

``--from-db`` (default) snapshots the table as it stands, so it captures the
authored rewards, actions, mods and personalities exactly. ``--from-records``
rebuilds the file from the Records seed (name, title, gameboard, deck,
champion, mods, default rewards); use it to start over or after a Records
refresh - existing editor edits are re-applied from the old file by guid.

An AI personality equal to the Python deck evaluator's pick is written blank
("auto"): the server fills blanks at startup, so only deliberate overrides are
stored.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]

import campaign_data  # noqa: E402

OUT = campaign_data.DATA_DIR / "encounter_scenes.json"
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


def _auto_pick(db, deck_guid, cache={}):
    if not deck_guid:
        return ""
    if deck_guid not in cache:
        from ai_deck_strategy import evaluate_deck_strategy
        cards = [(r[0], r[1]) for r in db.execute(
            "SELECT card_guid, quantity FROM encounter_deck_cards WHERE deck_guid=?",
            (deck_guid,))]
        cache[deck_guid] = evaluate_deck_strategy(cards).personality or "Default"
    return cache[deck_guid]


def rows_from_db(db) -> list[dict]:
    rows = []
    for r in db.execute(f"SELECT {', '.join(COLUMNS)} FROM encounter_scenes "
                        "ORDER BY name, guid"):
        row = dict(zip(COLUMNS, r))
        stored = str(row["ai_deck_personality"] or "").strip()
        out = {"guid": str(row["guid"]).lower(), "name": row["name"] or "",
               "title": row["title"] or "", "gameboard": row["gameboard"] or "",
               "ai_deck_guid": row["ai_deck_guid"] or "",
               "ai_champion_guid": row["ai_champion_guid"] or "",
               "mods_json": _loads(row["mods_json"], []),
               "rewards_json": _loads(row["rewards_json"], {})}
        actions = _loads(row["actions_json"], {})
        if actions:
            out["actions_json"] = actions
        auto = _auto_pick(db, row["ai_deck_guid"])
        out["ai_deck_personality"] = "" if (not stored or stored == auto) else stored
        rows.append(out)
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
               "ai_deck_personality": ""}
        old = previous.get(row["guid"])
        if old:                              # keep what the editor authored
            for key in ("mods_json", "rewards_json", "actions_json", "ai_deck_personality"):
                if key in old:
                    row[key] = old[key]
        rows.append(row)
    return sorted(rows, key=lambda r: (r["name"], r["guid"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "hconnect.db"))
    parser.add_argument("--from-records", action="store_true")
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
    OUT.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    overridden = sum(1 for r in rows if r["ai_deck_personality"])
    print(f"{len(rows)} scenes -> {OUT.relative_to(ROOT)} "
          f"({overridden} authored personalities)")


if __name__ == "__main__":
    main()
