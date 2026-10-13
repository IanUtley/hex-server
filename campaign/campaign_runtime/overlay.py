"""Legacy per-user campaign overlay helpers.

Older editor versions stored per-user campaign changes in
``campaign_user_data`` and applied them over shared JSON files. The current
editor and live runtime use the shared authored files for every account and do
not load, save, or apply those legacy rows. The merge/storage functions remain
so existing databases can retain their old rows without a destructive schema
migration.

The context, merge, and storage functions remain for compatibility with old
database rows. Active editor and runtime readers use the shared authored files
directly and never activate these stored documents.
"""

from __future__ import annotations

import contextvars
import copy
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

CAMPAIGN_ROOT = Path(__file__).resolve().parents[1]
EDITABLE_DIRS = ("campaign_data", "campaign_rules")
# Row identity per table (later rows override earlier fields).
TABLE_KEYS = {
    "campaign_node_conversations": ("campaign_template", "conversation_guid"),
    "quest_conversations": ("quest_script", "conversation_guid", "campaign_template"),
    "encounter_scenes": ("guid",),
    "quest_templates": ("script_name",),
}
REWARD_FIELDS = ("rewards_json",)
_CURRENT: contextvars.ContextVar = contextvars.ContextVar("campaign_overlay", default=None)


# ---------------------------------------------------------------- context

@contextmanager
def use(docs: dict[str, Any] | None, user_id: int | None = None):
    """Make a player's sparse documents current for this thread."""
    # A player with no edits yet still gets a (mutable) document set, so a
    # save during the request is seen by the reads after it.
    token = _CURRENT.set({"docs": docs if docs is not None else {}, "user": user_id}
                         if (docs or user_id is not None) else None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current() -> dict[str, Any] | None:
    value = _CURRENT.get()
    return value["docs"] if value else None


def key() -> str:
    """Cache key part for the current overlay ('' = the defaults)."""
    docs = current()
    return json.dumps(docs, sort_keys=True) if docs else ""


def relative(path: Path) -> str:
    return Path(path).resolve().relative_to(CAMPAIGN_ROOT).as_posix()


def is_editable(rel: str) -> bool:
    parts = rel.split("/")
    return (len(parts) == 2 and parts[0] in EDITABLE_DIRS and
            parts[1].endswith(".json") and ".." not in rel and
            parts[1] != "zones.json")


def extra_paths(directory: Path) -> list[Path]:
    """Documents only the current player has in ``directory``."""
    docs = current() or {}
    if not docs:
        return []
    try:
        base = relative(directory)
    except ValueError:
        return []
    return [CAMPAIGN_ROOT / rel for rel in sorted(docs)
            if rel.startswith(base + "/") and not (CAMPAIGN_ROOT / rel).exists()]


# ---------------------------------------------------------------- merge

def _row_key(table: str, row: dict) -> tuple | None:
    fields = TABLE_KEYS.get(table)
    if not fields:
        return None
    return tuple(str(row.get(f) or "").lower() for f in fields)


def _merge_data(default: dict, sparse: dict) -> dict:
    out = copy.deepcopy(default) if default else {"schema_version": 1, "tables": {}}
    tables = out.setdefault("tables", {})
    for table, rows in (sparse.get("tables") or {}).items():
        target = tables.setdefault(table, [])
        index = {_row_key(table, r): r for r in target if _row_key(table, r)}
        for row in rows or ():
            found = index.get(_row_key(table, row))
            if found is not None:
                found.update(copy.deepcopy(row))
            else:
                target.append(copy.deepcopy(row))
    return out


def _merge_rules(default: dict, sparse: dict) -> dict:
    out = copy.deepcopy(default) if default else {}
    for name, spec in (sparse.get("nodes") or {}).items():
        node = out.setdefault("nodes", {}).setdefault(name, {})
        for gate, value in (spec or {}).items():
            if value is None:
                node.pop(gate, None)
            else:
                node[gate] = copy.deepcopy(value)
        if not node:
            out["nodes"].pop(name, None)
    for name, gates in (sparse.get("edited_states") or {}).items():
        mine = out.setdefault("edited_states", {}).setdefault(name, [])
        mine.extend(g for g in gates if g not in mine)
    for field in ("quests",):
        for name, value in (sparse.get(field) or {}).items():
            out.setdefault(field, {})[name] = copy.deepcopy(value)
    return out


def merge(rel: str, default: dict | None, sparse: dict | None) -> dict | None:
    if not sparse:
        return default
    if rel.startswith("campaign_data/"):
        return _merge_data(default or {}, sparse)
    return _merge_rules(default or {}, sparse)


def effective(path: Path, default: dict | None) -> dict | None:
    """``default`` (a file's content, or None) as the current player sees it."""
    docs = current()
    if not docs:
        return default
    rel = relative(path)
    return merge(rel, default, docs.get(rel))


# ---------------------------------------------------------------- diff

def _strip_rewards(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in REWARD_FIELDS}


def _diff_data(default: dict, edited: dict) -> dict:
    tables = {}
    base = (default or {}).get("tables") or {}
    for table, rows in (edited.get("tables") or {}).items():
        index = {_row_key(table, r): r for r in base.get(table) or () if _row_key(table, r)}
        changed = []
        for row in rows or ():
            key = _row_key(table, row)
            old = index.get(key) if key else None
            if old == row:
                continue
            delta = {k: v for k, v in row.items() if old is None or old.get(k) != v}
            if key:
                delta.update({f: row.get(f) for f in TABLE_KEYS[table]})
            delta = _strip_rewards(delta)
            if set(delta) - set(TABLE_KEYS.get(table, ())):
                changed.append(delta)
        if changed:
            tables[table] = changed
    return {"tables": tables} if tables else {}


def _diff_rules(default: dict, edited: dict) -> dict:
    out: dict[str, Any] = {}
    base = (default or {}).get("nodes") or {}
    mine = edited.get("nodes") or {}
    nodes = {}
    for name in set(base) | set(mine):
        old, new = base.get(name) or {}, mine.get(name) or {}
        delta = {g: new.get(g) for g in set(old) | set(new) if old.get(g) != new.get(g)}
        if delta:
            nodes[name] = delta
    if nodes:
        out["nodes"] = nodes
        out["edited_states"] = {
            n: g for n, g in (edited.get("edited_states") or {}).items() if n in nodes}
    quests = {q: v for q, v in (edited.get("quests") or {}).items()
              if ((default or {}).get("quests") or {}).get(q) != v}
    if quests:
        out["quests"] = {q: {k: v for k, v in body.items() if k != "rewards"}
                         for q, body in quests.items()}
    return out


def diff(rel: str, default: dict | None, edited: dict) -> dict:
    """The sparse document that turns ``default`` into ``edited``."""
    if rel.startswith("campaign_data/"):
        return _diff_data(default or {}, edited)
    return _diff_rules(default or {}, edited)


# ---------------------------------------------------------------- storage

def load(db, user_id: int) -> dict[str, Any]:
    """``{relative path: sparse document}`` of one player."""
    rows = db.execute(
        "SELECT path, doc_json FROM campaign_user_data WHERE user_id=?",
        (int(user_id),)).fetchall()
    return {path: json.loads(doc or "{}") for path, doc in rows}


def save(db, user_id: int, rel: str, sparse: dict) -> None:
    """Store (or, when empty, remove) one sparse document; caller commits."""
    if not is_editable(rel):
        raise ValueError(f"{rel} is not an editable campaign document")
    if sparse:
        db.execute(
            "INSERT INTO campaign_user_data (user_id, path, doc_json, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) ON CONFLICT(user_id, path) DO UPDATE "
            "SET doc_json=excluded.doc_json, updated_at=excluded.updated_at",
            (int(user_id), rel, json.dumps(sparse, sort_keys=True)))
    else:
        db.execute("DELETE FROM campaign_user_data WHERE user_id=? AND path=?",
                   (int(user_id), rel))


def reset(db, user_id: int) -> int:
    """Back to the default campaign; returns how many documents were dropped."""
    return db.execute("DELETE FROM campaign_user_data WHERE user_id=?",
                      (int(user_id),)).rowcount


def scene_value(db, user_id, scene_guid: str, column: str):
    """A player's personal value of one ``encounter_scenes`` column
    (``mods_json``, ``ai_deck_personality``), or None to use the default.
    Rewards are never personal."""
    if user_id is None or column in REWARD_FIELDS:
        return None
    row = db.execute(
        "SELECT doc_json FROM campaign_user_data WHERE user_id=? AND path=?",
        (int(user_id), "campaign_data/encounter_scenes.json")).fetchone()
    if not row:
        return None
    for scene in (json.loads(row[0] or "{}").get("tables") or {}).get(
            "encounter_scenes") or ():
        if str(scene.get("guid") or "").lower() == str(scene_guid or "").lower():
            return scene.get(column)
    return None


def default_doc(rel: str) -> dict | None:
    path = CAMPAIGN_ROOT / rel
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
