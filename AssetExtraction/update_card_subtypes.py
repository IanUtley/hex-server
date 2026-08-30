#!/usr/bin/env python3
"""Backfill card_templates.subtype from Records/CardTemplate.jsonl
(m_CardSubtype, e.g. "Human Cleric") so IsSubType filters can be evaluated.

Usage:
    python3 AssetExtraction/update_card_subtypes.py
"""

import ast
import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static.py")
DB = os.path.join(ROOT, "hconnect.db")
RECORDS = os.path.join(ROOT, "Records", "CardTemplate.jsonl")

BEGIN_MARKER = "CARD_TEMPLATES = ["


def load_subtypes():
    subs = {}
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
            g = str(rec.get("m_Id", {}).get("m_Guid", "")).lower()
            if g:
                subs[g] = rec.get("m_CardSubtype", "") or ""
    return subs


def main():
    subs = load_subtypes()
    db = sqlite3.connect(DB)
    cols = {r[1] for r in db.execute("PRAGMA table_info(card_templates)")}
    if "subtype" not in cols:
        db.execute("ALTER TABLE card_templates ADD COLUMN subtype TEXT DEFAULT ''")
    n = 0
    for (g,) in db.execute("SELECT guid FROM card_templates"):
        st = subs.get(str(g).lower())
        if st is not None:
            db.execute("UPDATE card_templates SET subtype=? WHERE guid=?", (st, g))
            n += 1
    db.commit()
    db.close()

    # Patch the static CARD_TEMPLATES block: 18-tuples (incl. variable_cost /
    # variable_cost_minimum / rage_value) become 19-tuples with subtype.
    src = open(STATIC).read()
    start = src.index(BEGIN_MARKER)
    lines = src[start:].splitlines()
    end_rel = 1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "]":
            end_rel = i
            break
    block_lines = lines[:end_rel + 1]
    out = [BEGIN_MARKER]
    updated = 0
    for line in block_lines[1:-1]:
        s = line.strip()
        if s.startswith("(") and s.endswith("),"):
            tup = ast.literal_eval(s[:-1])
            if len(tup) == 18:
                tup = tuple(tup) + (subs.get(str(tup[0]).lower(), ""),)
                updated += 1
            out.append("    " + repr(tup) + ",")
        else:
            out.append(line)
    end = start + sum(len(l) + 1 for l in block_lines)
    open(STATIC, "w").write(src[:start] + "\n".join(out) + "\n]\n" + src[end:])
    print(f"DB rows updated: {n}; static rows updated: {updated}")


if __name__ == "__main__":
    main()
