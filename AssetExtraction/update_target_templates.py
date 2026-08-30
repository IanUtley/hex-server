#!/usr/bin/env python3
"""Add targeting metadata to the TARGET_TEMPLATES seed and target_templates DB
table, extracted data-driven from Records/AbilityTargetTemplate.jsonl.

Each target template gains: is_auto_target, is_random_target, optional,
explicit, player_filter, collection_flags, min/max target count and the raw
card-filter JSON — the fields the client's AbilityTargetTemplate classes read
to build the target picker (e.g. Solitary Exile's Deploy "another target card"
is explicit, 1 target, Warzone, any card type except the ability source).

Usage:
    python3 AssetExtraction/update_target_templates.py
"""

import ast
import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static.py")
DB = os.path.join(ROOT, "hconnect.db")
RECORDS = os.path.join(ROOT, "Records", "AbilityTargetTemplate.jsonl")

BEGIN_MARKER = "TARGET_TEMPLATES = ["


def count_value(field):
    """Extract an int from a TargetConstant {m_Value: N}, else default 1."""
    if isinstance(field, dict):
        try:
            return int(field.get("m_Value", 1) or 1)
        except (TypeError, ValueError):
            return 1
    m = re.search(r'"m_Value"\s*:\s*(-?\d+)', field or "")
    return int(m.group(1)) if m else 1


def load_target_meta():
    """Return {template_id: 10-tuple metadata} from AbilityTargetTemplate.jsonl."""
    meta = {}
    with open(RECORDS) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith('"$') or len(line) < 20:
                continue
            try:
                inner = json.loads(line)
                if isinstance(inner, str):
                    # Records are near-JSON: tolerate trailing commas.
                    inner = re.sub(r',\s*([}\]])', r'\1', inner)
                    rec = json.loads(inner)
                else:
                    rec = inner
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            tid = str(rec.get("m_TemplateId", {}).get("m_Guid", "")).lower()
            if not tid:
                continue
            meta[tid] = (
                int(rec.get("m_IsAutoTarget", 0) or 0),
                int(rec.get("m_IsRandomTarget", 0) or 0),
                int(rec.get("m_Optional", 0) or 0),
                int(rec.get("m_Explicit", 0) or 0),
                rec.get("m_PlayerFilter", "") or "",
                rec.get("m_CollectionFlags", "") or "",
                count_value(rec.get("m_MinTargetCount")),
                count_value(rec.get("m_MaxTargetCount")),
                json.dumps(rec.get("m_CardFilter") or {}),
                str(rec.get("_t", "")).split(".")[-1],
            )
    return meta


def regenerate_static(meta):
    """Rewrite the TARGET_TEMPLATES block: rows become 10-tuples
    (template_id, game_text, ...metadata); unknown rows keep defaults."""
    with open(STATIC) as fh:
        src = fh.read()
    start = src.index(BEGIN_MARKER)
    lines = src[start:].splitlines()
    end_rel = 1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "]":
            end_rel = i
            break
    block_lines = lines[:end_rel + 1]
    out = [BEGIN_MARKER]
    changed = 0
    # block_lines[-1] is the closing ']' — re-emitted explicitly below.
    for line in block_lines[1:-1]:
        s = line.strip()
        if s.startswith("(") and s.endswith("),"):
            tup = ast.literal_eval(s[:-1])
            tid = str(tup[0]).lower()
            gt = tup[1] if len(tup) > 1 else ""
            m = meta.get(tid)
            if m is None:
                m = (0, 0, 0, 0, "", "", 1, 1, "{}", "")
            else:
                changed += 1
            out.append("    " + repr((tup[0], gt) + tuple(m)) + ",")
        else:
            out.append(line)
    end = start + sum(len(l) + 1 for l in block_lines)
    with open(STATIC, "w") as fh:
        fh.write(src[:start] + "\n".join(out) + "\n]\n" + src[end:])
    return changed


def update_db(meta):
    """Add the metadata columns to target_templates (if missing) and backfill."""
    db = sqlite3.connect(DB)
    cols = {r[1] for r in db.execute("PRAGMA table_info(target_templates)")}
    for col, decl in (
            ("is_auto_target", "INTEGER NOT NULL DEFAULT 0"),
            ("is_random_target", "INTEGER NOT NULL DEFAULT 0"),
            ("optional", "INTEGER NOT NULL DEFAULT 0"),
            ("explicit", "INTEGER NOT NULL DEFAULT 0"),
            ("player_filter", "TEXT NOT NULL DEFAULT ''"),
            ("collection_flags", "TEXT NOT NULL DEFAULT ''"),
            ("min_target_count", "INTEGER NOT NULL DEFAULT 1"),
            ("max_target_count", "INTEGER NOT NULL DEFAULT 1"),
            ("filter_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("target_kind", "TEXT NOT NULL DEFAULT ''")):
        if col not in cols:
            db.execute(f"ALTER TABLE target_templates ADD COLUMN {col} {decl}")
    n = 0
    for (tid,) in db.execute("SELECT template_id FROM target_templates"):
        m = meta.get(str(tid).lower())
        if not m:
            continue
        db.execute(
            "UPDATE target_templates SET is_auto_target=?, is_random_target=?, "
            "optional=?, explicit=?, player_filter=?, collection_flags=?, "
            "min_target_count=?, max_target_count=?, filter_json=?, target_kind=? "
            "WHERE template_id=?",
            (m[0], m[1], m[2], m[3], m[4], m[5], m[6], m[7], m[8], m[9], tid))
        n += 1
    db.commit()
    db.close()
    return n


if __name__ == "__main__":
    ability_target_meta = load_target_meta()
    rows = regenerate_static(ability_target_meta)
    updated = update_db(ability_target_meta)
    print(f"static rows updated: {rows}; DB templates backfilled: {updated}")
