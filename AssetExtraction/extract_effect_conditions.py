#!/usr/bin/env python3
"""Seed ability_effect_conditions (condition_id -> condition JSON) from
Records/AbilityEffectConditionTemplate.jsonl, data-driven.

Effect conditions gate individual BOM leaves (e.g. Incantation of Righteousness'
"if there are five or more incantation counters" = SourceCardHasCounters) and
are referenced by ability_effects param condition_id.

Usage:
    python3 AssetExtraction/extract_effect_conditions.py
"""

import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static.py")
DB = os.environ.get("HEX_DB_PATH", os.path.join(ROOT, "hconnect.db"))
RECORDS = os.path.join(ROOT, "Records", "AbilityEffectConditionTemplate.jsonl")

BEGIN_MARKER = "ABILITY_EFFECT_CONDITIONS = ["
END_MARKER = "# === END ABILITY EFFECT CONDITION SEED ==="


def load_rows():
    rows = []
    with open(RECORDS) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith('"$') or len(line) < 20:
                continue
            try:
                inner = json.loads(line)
                if isinstance(inner, str):
                    inner = re.sub(r",\s*([}\]])", r"\1", inner)
                    rec = json.loads(inner)
                else:
                    rec = inner
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            cid = str(rec.get("m_TemplateId", {}).get("m_Guid", "")).lower()
            if cid:
                rows.append((cid, rec.get("m_Name", "") or "",
                             json.dumps(rec.get("m_Condition") or {})))
    return sorted(rows)


def main():
    rows = load_rows()
    lines = ["# AbilityEffectCondition rows: (condition_id, name, condition_json). "
             "Generated from Records/AbilityEffectConditionTemplate.jsonl.",
             "ABILITY_EFFECT_CONDITIONS = ["]
    for cid, name, cond in rows:
        lines.append(f"    ({cid!r}, {name!r}, {cond!r}),")
    lines.append("]")
    block = "\n".join(lines)

    static_src = open(STATIC).read()
    if BEGIN_MARKER not in static_src:
        anchor = "CARD_COUNTER_TEMPLATES = ["
        m = re.search(rf"^{re.escape(anchor)}$", static_src, re.M)
        if not m:
            raise SystemExit("could not locate CARD_COUNTER_TEMPLATES = [")
        # block already carries its own header/rows/closing bracket; just
        # prepend the END marker after it.
        static_src = (static_src[:m.start()]
                      + f"{block}\n{END_MARKER}\n\n"
                      + static_src[m.start():])
    else:
        begin = static_src.index(BEGIN_MARKER)
        end = static_src.index(END_MARKER)
        end_of_block = static_src.index("\n", end)
        static_src = (static_src[:begin]
                      + f"{BEGIN_MARKER}\n{block}\n{END_MARKER}"
                      + static_src[end_of_block:])
    open(STATIC, "w").write(static_src)

    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS ability_effect_conditions (
        condition_id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        condition_json TEXT NOT NULL DEFAULT '{}'
    )""")
    for cid, name, cond in rows:
        db.execute(
            "INSERT OR REPLACE INTO ability_effect_conditions "
            "(condition_id, name, condition_json) VALUES (?,?,?)",
            (cid, name, cond))
    db.commit()
    db.close()
    print(f"Wrote {len(rows)} effect conditions")


if __name__ == "__main__":
    main()
