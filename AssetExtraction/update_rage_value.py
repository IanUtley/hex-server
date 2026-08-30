#!/usr/bin/env python3
"""Backfill card_templates.rage_value from Records/CardTemplate.jsonl
(m_RageValue) — the card's printed Rage X, used by the statics layer for the
"when this attacks it gets +X ATK" combat bonus.

Patches the static CARD_TEMPLATES seed (18-tuples become 19-tuples, inserting
rage_value before subtype) and the live hconnect.db.

Usage:
    python3 AssetExtraction/update_rage_value.py
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


def load_rage():
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
                value = int(rec.get("m_RageValue") or 0)
                # Keep the backfill consistent with extract_cards.py: legacy
                # templates can carry a stale value without declaring Rage.
                if not value:
                    rage_match = re.search(
                        r"\brage\s+(\d+)\b",
                        str(rec.get("m_GameText") or ""), re.IGNORECASE)
                    value = int(rage_match.group(1)) if rage_match else 0
                if value and not re.search(
                        r"\brage\b", str(rec.get("m_GameText") or ""),
                        re.IGNORECASE):
                    value = 0
                data[g] = value
    return data


def main():
    rage = load_rage()
    db = sqlite3.connect(DB)
    cols = {r[1] for r in db.execute("PRAGMA table_info(card_templates)")}
    if "rage_value" not in cols:
        db.execute("ALTER TABLE card_templates ADD COLUMN rage_value INTEGER DEFAULT 0")
    n = 0
    for (g,) in db.execute("SELECT guid FROM card_templates"):
        db.execute(
            "UPDATE card_templates SET rage_value=? WHERE guid=?",
            (rage.get(str(g).lower(), 0), g))
        n += 1
    db.commit()
    db.close()

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
                rv = rage.get(str(tup[0]).lower(), 0)
                tup = tuple(tup[:17]) + (rv, tup[17])
                updated += 1
            elif len(tup) == 19:
                rv = rage.get(str(tup[0]).lower(), 0)
                tup = tuple(tup[:17]) + (rv,) + tuple(tup[18:])
                updated += 1
            out.append("    " + repr(tup) + ",")
        else:
            out.append(line)
    end = start + sum(len(l) + 1 for l in block_lines)
    open(STATIC, "w").write(src[:start] + "\n".join(out) + "\n]\n" + src[end:])
    print(f"DB rows updated: {n}; static rows updated: {updated}")


if __name__ == "__main__":
    main()
