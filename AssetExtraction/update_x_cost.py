#!/usr/bin/env python3
"""Backfill card_templates.variable_cost / variable_cost_minimum from
Records/CardTemplate.jsonl (m_VariableCost / m_VariableCostMinimum).

Variable-X cards ("pay X", e.g. Burn to the Ground "1X" = 1 base + X) are
detected from the gamedata card fields, NOT by scanning ability text.  This
patches the static CARD_TEMPLATES seed (16-tuples become 18-tuples, inserting
the two fields before subtype) and the live hconnect.db.

Usage:
    python3 AssetExtraction/update_x_cost.py
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


def load_x_cost():
    data = {}
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
                data[g] = (int(rec.get("m_VariableCost") or 0),
                           int(rec.get("m_VariableCostMinimum") or 0))
    return data


def main():
    xcost = load_x_cost()
    db = sqlite3.connect(DB)
    cols = {r[1] for r in db.execute("PRAGMA table_info(card_templates)")}
    if "variable_cost" not in cols:
        db.execute("ALTER TABLE card_templates ADD COLUMN variable_cost INTEGER DEFAULT 0")
    if "variable_cost_minimum" not in cols:
        db.execute("ALTER TABLE card_templates ADD COLUMN variable_cost_minimum INTEGER DEFAULT 0")
    n = 0
    for (g,) in db.execute("SELECT guid FROM card_templates"):
        vc, vcm = xcost.get(str(g).lower(), (0, 0))
        db.execute(
            "UPDATE card_templates SET variable_cost=?, variable_cost_minimum=? WHERE guid=?",
            (vc, vcm, g))
        n += 1
    db.commit()
    db.close()

    # Patch the static CARD_TEMPLATES block: 16-tuples (…, sacrifice_target,
    # subtype) become 18-tuples with variable_cost / variable_cost_minimum
    # inserted before subtype, matching extract_cards.py's canonical order.
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
            if len(tup) == 16:
                vc, vcm = xcost.get(str(tup[0]).lower(), (0, 0))
                tup = tuple(tup[:15]) + (vc, vcm, tup[15])
                updated += 1
            out.append("    " + repr(tup) + ",")
        else:
            out.append(line)
    end = start + sum(len(l) + 1 for l in block_lines)
    open(STATIC, "w").write(src[:start] + "\n".join(out) + "\n]\n" + src[end:])
    print(f"DB rows updated: {n}; static rows updated: {updated}")


if __name__ == "__main__":
    main()
