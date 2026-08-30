#!/usr/bin/env python3
"""Add casting_behavior + thresholds_json to the PvP champion ability seed.

Regenerates static.py's CHAMPION_ABILITIES block (appending the two
gamedata-derived fields to every ability row, while leaving the adjacent
CHAMPION_TEMPLATES_EXTENDED rows untouched) and upserts champion_abilities in
the DB.  All values come from Records/AbilityTemplate.jsonl
(m_CastingBehavior and the m_AbilityCondition threshold requirements) — never
hardcoded per champion.

Usage:
    python3 AssetExtraction/update_champion_seed.py
"""

import ast
import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static.py")
DB = os.path.join(ROOT, "hconnect.db")
ABILITIES_JSONL = os.path.join(ROOT, "Records", "AbilityTemplate.jsonl")

CASTING = {"BasicAction": 8, "QuickAction": 64}
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}$")


def parse_thresholds(cond):
    """Flatten AbilityControllerHasThresholdAbilityCondition (and
    AndAbilityCondition wrappers) into [{"color", "quantity"}, ...]."""
    reqs = []
    if not isinstance(cond, dict):
        return reqs
    t = str(cond.get("_t", ""))
    if t.endswith("AbilityControllerHasThresholdAbilityCondition"):
        color = cond.get("m_ColorFlags", "")
        qty = int(cond.get("m_RequiredQuantity", 0) or 0)
        if color:
            reqs.append({"color": color, "quantity": qty})
    elif t.endswith("AndAbilityCondition"):
        for c in (cond.get("m_Conditions") or []):
            if (isinstance(c, dict) and str(c.get("_t", ""))
                    .endswith("AbilityControllerHasThresholdAbilityCondition")):
                color = c.get("m_ColorFlags", "")
                qty = int(c.get("m_RequiredQuantity", 0) or 0)
                if color:
                    reqs.append({"color": color, "quantity": qty})
    return reqs


def load_ability_meta():
    """Return {ability_guid_lower: (casting_behavior, thresholds_json)}."""
    meta = {}
    with open(ABILITIES_JSONL) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith('"$') or len(line) < 20:
                continue
            try:
                inner = json.loads(line)
                rec = json.loads(inner) if isinstance(inner, str) else inner
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            ag = str(rec.get("m_AbilityTemplateId", {}).get("m_Guid", ""))
            if ag:
                cb = rec.get("m_CastingBehavior", "") or ""
                meta[ag.lower()] = (
                    CASTING.get(cb, 0),
                    json.dumps(parse_thresholds(rec.get("m_AbilityCondition"))),
                )
    return meta


def regenerate_static(meta):
    """Append casting_behavior + thresholds_json to CHAMPION_ABILITIES rows in
    static.py; leave CHAMPION_TEMPLATES_EXTENDED rows at their 8 fields."""
    with open(STATIC) as fh:
        src = fh.read()
    start = src.index("CHAMPION_ABILITIES = [")
    end = src.index("# === END CHAMPION ABILITY SEED ===")
    block = src[start:end]
    out_lines = []
    changed = 0
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("(") and s.endswith("),"):
            tup = ast.literal_eval(s[:-1])
            if len(tup) >= 3 and UUID_RE.match(str(tup[2])):
                cb, th = meta.get(str(tup[2]).lower(), (0, "[]"))
                out_lines.append("    " + repr(tuple(tup[:8]) + (cb, th)) + ",")
                changed += 1
            else:
                # champion_templates_extended row — keep the original 8 fields.
                out_lines.append("    " + repr(tuple(tup[:8])) + ",")
        else:
            out_lines.append(line)
    with open(STATIC, "w") as fh:
        fh.write(src[:start] + "\n".join(out_lines) + "\n" + src[end:])
    return changed


def update_db(meta):
    """Add the columns to champion_abilities (if missing) and backfill every
    row's casting_behavior / thresholds_json from the Records-derived map."""
    db = sqlite3.connect(DB)
    cols = {r[1] for r in db.execute("PRAGMA table_info(champion_abilities)")}
    if "casting_behavior" not in cols:
        db.execute(
            "ALTER TABLE champion_abilities ADD COLUMN casting_behavior "
            "INTEGER NOT NULL DEFAULT 0")
    if "thresholds_json" not in cols:
        db.execute(
            "ALTER TABLE champion_abilities ADD COLUMN thresholds_json "
            "TEXT NOT NULL DEFAULT '[]'")
    n = 0
    for (ag,) in db.execute("SELECT ability_guid FROM champion_abilities"):
        cb, th = meta.get(str(ag).lower(), (0, "[]"))
        db.execute(
            "UPDATE champion_abilities SET casting_behavior=?, thresholds_json=? "
            "WHERE ability_guid=?",
            (cb, th, ag))
        n += 1
    db.commit()
    db.close()
    return n


if __name__ == "__main__":
    ability_meta = load_ability_meta()
    rows = regenerate_static(ability_meta)
    updated = update_db(ability_meta)
    print(f"static rows updated: {rows}; DB abilities backfilled: {updated}")
