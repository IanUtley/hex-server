"""Campaign table data authored by the campaign editor.

``campaign/campaign_data/*.json`` holds rows for the server's campaign tables - what a
node offers (``campaign_node_conversations``, ``quest_conversations``),
encounter rewards and modifiers (``encounter_scenes``), conversation rewards,
quest templates. ``hex-private/Records/`` is never modified; these rows are applied over
the Records-derived seed on every start, so they win over a seed refresh.

Called from ``static.ensure_schema()`` after the seeds and ``card_fix``.
Applying is idempotent: each row is upserted by its natural key and only the
columns it names are written. A file that fails validation is skipped as a
whole (nothing from it is applied) and reported, so a bad edit cannot stop
the server from starting.

The files are the source of truth for the rows they create. Each insertable
table has an ``origin`` column: ``'seed'`` for rows the seed code created
(the column default) and ``'data'`` for rows a data file inserted. After all
files are applied, ``apply_all`` deletes the ``'data'`` rows whose key no
file lists any more. ``'seed'`` rows are never deleted (a file entry for a
seed row only overrides columns), and nothing is deleted when any file was
skipped, since its rows cannot be told apart from removed ones.

File format::

    {"schema_version": 1,
     "tables": {"encounter_scenes": [{"guid": "...", "rewards_json": {...}}],
                "quest_conversations": [{...}]}}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ORIGIN_SEED = "seed"
ORIGIN_DATA = "data"
DATA_DIR = Path(__file__).resolve().parent / "campaign_data"

# table -> natural key, whether new rows may be inserted, writable columns.
# encounter_scenes is owned by the editor (full definitions in
# encounter_scenes.json); quest_templates is still update-only: the editor
# tunes seeded quests but does not invent them.
TABLES: dict[str, dict[str, Any]] = {
    # The campaign editor owns every encounter scene: full definitions live in
    # campaign/campaign_data/encounter_scenes.json and are inserted/updated at start.
    "encounter_scenes": {
        "key": ("guid",), "insert": True,
        "columns": {"name", "title", "gameboard", "ai_deck_guid", "ai_champion_guid",
                    "mods_json", "rewards_json", "actions_json",
                    "ai_deck_personality", "player_champion_guid",
                    "player_deck_guid"},
        "file_columns": {"ai_deck_personality_override", "scene_tags"},
        # The JSON keeps the precomputed default separately from an optional
        # authored override; SQLite stores their effective value.
        "skip_blank": {"ai_deck_personality"}},
    "quest_templates": {
        "key": ("script_name",), "insert": False,
        "columns": {"title", "objectives_json", "campaign_group", "enabled",
                    "start_hook", "rewards_json"}},
    "campaign_node_conversations": {
        "key": ("campaign_template", "conversation_guid"), "insert": True,
        "columns": {"node_id", "conversation_name", "trigger_json",
                    "priority", "enabled"}},
    "quest_conversations": {
        "key": ("quest_script", "conversation_guid", "campaign_template"),
        "insert": True,
        "columns": {"node_id", "npc", "role", "faction", "conversation_name",
                    "start_hook", "conditions_json", "priority", "enabled"}},
    "conversation_rewards": {
        "key": ("conversation_guid",), "insert": True,
        "columns": {"reward_json", "one_time", "enabled"}},
}


def _value(column: str, value: Any) -> Any:
    if column.endswith("_json") and not isinstance(value, str):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, bool):
        return int(value)
    return value


def validate(data: dict[str, Any]) -> list[str]:
    """Problems that would make a data file unsafe to apply."""
    problems = []
    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    for table, rows in (data.get("tables") or {}).items():
        spec = TABLES.get(table)
        if spec is None:
            problems.append(f"{table}: not an editable campaign table")
            continue
        if not isinstance(rows, list):
            problems.append(f"{table}: rows must be a list")
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                problems.append(f"{table}[{index}]: row must be an object")
                continue
            missing = [k for k in spec["key"] if not row.get(k)]
            if missing:
                problems.append(f"{table}[{index}]: missing key {missing}")
            extra = (set(row) - set(spec["key"]) - spec["columns"]
                     - spec.get("file_columns", set()))
            if extra:
                problems.append(f"{table}[{index}]: not editable {sorted(extra)}")
    return problems


def _has_origin(db, table: str) -> bool:
    return any(row[1] == "origin"
               for row in db.execute(f"PRAGMA table_info({table})"))


def _key_of(values) -> tuple:
    """Comparable natural key: GUIDs are matched case-insensitively."""
    return tuple(str(v).lower() for v in values)


def file_keys(data: dict[str, Any]) -> dict[str, set[tuple]]:
    """The natural keys of every row a data file lists, per table."""
    keys: dict[str, set[tuple]] = {}
    for table, rows in (data.get("tables") or {}).items():
        spec = TABLES[table]
        keys.setdefault(table, set()).update(
            _key_of(row[k] for k in spec["key"]) for row in rows)
    return keys


def prune_data_rows(db, owned: dict[str, set[tuple]]) -> dict[str, list[tuple]]:
    """Delete ``origin='data'`` rows whose key is not in ``owned``.

    ``owned`` is every key the data files list (``file_keys`` merged over all
    files). Only tables a file may insert into are considered; ``'seed'`` rows
    and tables without an ``origin`` column are left alone. Returns the
    deleted keys per table. The caller owns the transaction.
    """
    deleted: dict[str, list[tuple]] = {}
    for table, spec in TABLES.items():
        if not spec["insert"] or not _has_origin(db, table):
            continue
        keep = owned.get(table, set())
        columns = ", ".join(spec["key"])
        gone = [row for row in db.execute(
                    f"SELECT rowid, {columns} FROM {table} WHERE origin=?",
                    (ORIGIN_DATA,)).fetchall()
                if _key_of(row[1:]) not in keep]
        for row in gone:
            db.execute(f"DELETE FROM {table} WHERE rowid=? AND origin=?",
                       (row[0], ORIGIN_DATA))
        if gone:
            deleted[table] = [tuple(row[1:]) for row in gone]
    return deleted


def apply_data(db, data: dict[str, Any]) -> dict[str, int]:
    """Upsert one data file's rows; returns per-table changed-row counts.

    The caller owns the transaction (``apply_all`` wraps each file in a
    savepoint).
    """
    counts: dict[str, int] = {}
    for table, rows in (data.get("tables") or {}).items():
        spec = TABLES[table]
        changed = 0
        origin = {"origin": ORIGIN_DATA} if (
            spec["insert"] and rows and _has_origin(db, table)) else {}
        for row in rows:
            key = {k: row[k] for k in spec["key"]}
            row_values = row
            if table == "encounter_scenes":
                row_values = {
                    **row,
                    "ai_deck_personality": (
                        str(row.get("ai_deck_personality_override") or "").strip()
                        or str(row.get("ai_deck_personality") or "").strip()),
                }
            values = {c: _value(c, v) for c, v in row_values.items()
                      if c in spec["columns"]
                      and not (c in spec.get("skip_blank", ()) and v in ("", None))}
            where = " AND ".join(f"{k}=?" for k in key)
            exists = db.execute(f"SELECT 1 FROM {table} WHERE {where}",
                                tuple(key.values())).fetchone()
            if exists:
                if values:
                    assignments = ", ".join(f"{c}=?" for c in values)
                    differs = " OR ".join(f"{c} IS NOT ?" for c in values)
                    cursor = db.execute(
                        f"UPDATE {table} SET {assignments} "
                        f"WHERE {where} AND ({differs})",
                        (*values.values(), *key.values(), *values.values()))
                    changed += max(cursor.rowcount, 0)
            elif spec["insert"]:
                columns = {**key, **values, **origin}
                db.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    tuple(columns.values()))
                changed += 1
        counts[table] = changed
    return counts


def apply_encounter_scenes(db, directory: Path = DATA_DIR) -> int:
    """Insert/update ``encounter_scenes`` from ``encounter_scenes.json`` alone.

    ``ensure_schema`` needs the scenes before the rest of the data files are
    applied (the deck evaluator reads them); applying again later is a no-op.
    """
    path = Path(directory) / "encounter_scenes.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = validate(data)
    if problems:
        print(f"campaign_data: skipped {path.name}: {'; '.join(problems[:5])}")
        return 0
    return apply_data(db, data).get("encounter_scenes", 0)


def apply_all(db, directory: Path = DATA_DIR) -> dict[str, dict[str, int]]:
    """Apply every ``*.json`` in ``directory``; bad files are skipped.

    Then delete the ``origin='data'`` rows no file lists (``prune_data_rows``)
    unless a file was skipped. ``results["deleted"]`` holds the deleted keys
    per table (empty when nothing was deleted or pruning was skipped).
    """
    results: dict[str, Any] = {}
    owned: dict[str, set[tuple]] = {}
    skipped = False
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"campaign_data: skipped {path.name}: {exc}")
            skipped = True
            continue
        problems = validate(data)
        if problems:
            print(f"campaign_data: skipped {path.name}: {'; '.join(problems[:5])}")
            skipped = True
            continue
        db.execute("SAVEPOINT campaign_data")
        try:
            counts = apply_data(db, data)
        except Exception as exc:
            db.execute("ROLLBACK TO SAVEPOINT campaign_data")
            db.execute("RELEASE SAVEPOINT campaign_data")
            print(f"campaign_data: skipped {path.name}: {exc}")
            skipped = True
            continue
        db.execute("RELEASE SAVEPOINT campaign_data")
        results[path.name] = counts
        for table, keys in file_keys(data).items():
            owned.setdefault(table, set()).update(keys)
        if any(counts.values()):
            print(f"campaign_data: {path.name}: " + ", ".join(
                f"{t}={n}" for t, n in sorted(counts.items()) if n))
    deleted: dict[str, list[tuple]] = {}
    if skipped or not owned:
        print("campaign_data: a data file was skipped or none was found; "
              "not deleting rows removed from the data files")
    else:
        deleted = prune_data_rows(db, owned)
        for table, keys in sorted(deleted.items()):
            print(f"campaign_data: deleted {len(keys)} {table} row(s) no data "
                  f"file lists: {keys}")
    results["deleted"] = deleted
    db.commit()
    return results
