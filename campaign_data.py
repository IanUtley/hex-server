"""Campaign table data authored by the campaign editor.

``campaign_data/*.json`` holds rows for the server's campaign tables - what a
node offers (``campaign_node_conversations``, ``quest_conversations``),
encounter rewards and modifiers (``encounter_scenes``), conversation rewards,
quest templates. ``Records/`` is never modified; these rows are applied over
the Records-derived seed on every start, so they win over a seed refresh.

Called from ``static.ensure_schema()`` after the seeds and ``card_fix``.
Applying is idempotent: each row is upserted by its natural key and only the
columns it names are written. A file that fails validation is skipped as a
whole (nothing from it is applied) and reported, so a bad edit cannot stop
the server from starting.

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
DATA_DIR = Path(__file__).resolve().parent / "campaign_data"

# table -> natural key, whether new rows may be inserted, writable columns.
# encounter_scenes is owned by the editor (full definitions in
# encounter_scenes.json); quest_templates is still update-only: the editor
# tunes seeded quests but does not invent them.
TABLES: dict[str, dict[str, Any]] = {
    # The campaign editor owns every encounter scene: full definitions live in
    # campaign_data/encounter_scenes.json and are inserted/updated at start.
    "encounter_scenes": {
        "key": ("guid",), "insert": True,
        "columns": {"name", "title", "gameboard", "ai_deck_guid", "ai_champion_guid",
                    "mods_json", "rewards_json", "actions_json",
                    "ai_deck_personality"},
        # A blank personality means "auto": the deck evaluator fills it at start.
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
            extra = set(row) - set(spec["key"]) - spec["columns"]
            if extra:
                problems.append(f"{table}[{index}]: not editable {sorted(extra)}")
    return problems


def apply_data(db, data: dict[str, Any]) -> dict[str, int]:
    """Upsert one data file's rows; returns per-table changed-row counts.

    The caller owns the transaction (``apply_all`` wraps each file in a
    savepoint).
    """
    counts: dict[str, int] = {}
    for table, rows in (data.get("tables") or {}).items():
        spec = TABLES[table]
        changed = 0
        for row in rows:
            key = {k: row[k] for k in spec["key"]}
            values = {c: _value(c, v) for c, v in row.items()
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
                columns = {**key, **values}
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
    """Apply every ``*.json`` in ``directory``; bad files are skipped."""
    results = {}
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"campaign_data: skipped {path.name}: {exc}")
            continue
        problems = validate(data)
        if problems:
            print(f"campaign_data: skipped {path.name}: {'; '.join(problems[:5])}")
            continue
        db.execute("SAVEPOINT campaign_data")
        try:
            counts = apply_data(db, data)
        except Exception as exc:
            db.execute("ROLLBACK TO SAVEPOINT campaign_data")
            db.execute("RELEASE SAVEPOINT campaign_data")
            print(f"campaign_data: skipped {path.name}: {exc}")
            continue
        db.execute("RELEASE SAVEPOINT campaign_data")
        results[path.name] = counts
        if any(counts.values()):
            print(f"campaign_data: {path.name}: " + ", ".join(
                f"{t}={n}" for t, n in sorted(counts.items()) if n))
    db.commit()
    return results
