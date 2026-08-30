#!/usr/bin/env python3
"""Seed card_counter_templates (counter template GUID -> name) from
Records/CardCounterTemplate.jsonl, data-driven.

The client renders counters from CardUpdated.CounterTemplates (ResourceIds) +
CounterCounts (ints), keyed by the gamedata CardCounterTemplate — e.g.
Incantation of Righteousness' "incantation counter" is template 12a1bb1f
("Incantation").  This table lets the server map a counter name parsed from
ability text to the template GUID the client expects.

Usage:
    python3 AssetExtraction/extract_counter_templates.py
"""

import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static.py")
DB = os.path.join(ROOT, "hconnect.db")
RECORDS = os.path.join(ROOT, "Records", "CardCounterTemplate.jsonl")

BEGIN_MARKER = "CARD_COUNTER_TEMPLATES = ["
END_MARKER = "# === END CARD COUNTER TEMPLATE SEED ==="


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
            cid = str(rec.get("m_CardCounterId", {}).get("m_Guid", "")).lower()
            if cid:
                rows.append((cid, rec.get("m_Name", "") or "",
                             rec.get("m_Description", "") or ""))
    return sorted(rows)


def main():
    rows = load_rows()
    lines = ["# CardCounterTemplate rows: (template_id, name, description). "
             "Generated from Records/CardCounterTemplate.jsonl.",
             "CARD_COUNTER_TEMPLATES = ["]
    for cid, name, desc in rows:
        lines.append(f"    ({cid!r}, {name!r}, {desc!r}),")
    lines.append("]")
    block = "\n".join(lines)

    static_src = open(STATIC).read()
    if BEGIN_MARKER not in static_src:
        # Insert right before the ability_effects list header (match the exact
        # line so "CHAMPION_ABILITY_EFFECTS = [" is never substring-matched).
        m = re.search(r"^ABILITY_EFFECTS = \[$", static_src, re.M)
        if not m:
            raise SystemExit("could not locate ABILITY_EFFECTS = [")
        idx = m.start()
        static_src = (static_src[:idx]
                      + f"{block}\n{END_MARKER}\n\n"
                      + static_src[idx:])
    else:
        begin = static_src.index(BEGIN_MARKER)
        end = static_src.index(END_MARKER)
        end_of_block = static_src.index("\n", end)
        static_src = (static_src[:begin]
                      + f"{block}\n{END_MARKER}"
                      + static_src[end_of_block:])
    open(STATIC, "w").write(static_src)

    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS card_counter_templates (
        template_id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT ''
    )""")
    for cid, name, desc in rows:
        db.execute(
            "INSERT OR REPLACE INTO card_counter_templates "
            "(template_id, name, description) VALUES (?,?,?)",
            (cid, name, desc))
    db.commit()
    db.close()
    print(f"Wrote {len(rows)} counter templates")


if __name__ == "__main__":
    main()
