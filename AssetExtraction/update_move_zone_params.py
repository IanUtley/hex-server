#!/usr/bin/env python3
"""Backfill MoveCardToZoneAbilityEffectTemplate params from
Records/AbilityEffectTemplate.jsonl (data-driven), so the BOM leaf knows the
destination zone — e.g. Eternal Youth's Escalation "PutThisIntoYourDeck"
(m_DestinationCollection "Deck") puts the played spell into the deck.

Updates ability_effects in the DB and the MoveCardToZone rows in static.py's
BOM blocks (CARD_ABILITY_EFFECTS / CHAMPION_ABILITY_EFFECTS / ABILITY_EFFECTS).

Usage:
    python3 AssetExtraction/update_move_zone_params.py
"""

import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static.py")
DB = os.path.join(ROOT, "hconnect.db")
RECORDS = os.path.join(ROOT, "Records", "AbilityEffectTemplate.jsonl")


def load_move_zone_meta():
    meta = {}
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
            if not str(rec.get("_t", "")).endswith("MoveCardToZoneEffectTemplate"):
                continue
            eg = str(rec.get("m_TemplateId", {}).get("m_Guid", "")).lower()
            if not eg:
                continue
            meta[eg] = json.dumps({
                "destination": rec.get("m_DestinationCollection", "") or "",
                "location": rec.get("m_DestinationLocation", "") or "",
                "name": rec.get("m_Name", "") or "",
                "text": rec.get("m_GameText", "") or "",
            })
    return meta


def update_db(meta):
    db = sqlite3.connect(DB)
    n = 0
    for (eg,) in db.execute(
            "SELECT DISTINCT effect_guid FROM ability_effects "
            "WHERE effect_type='MoveCardToZoneEffectTemplate' AND "
            "(param IS NULL OR param='')"):
        if eg.lower() in meta:
            db.execute(
                "UPDATE ability_effects SET param=? "
                "WHERE effect_guid=? AND effect_type='MoveCardToZoneEffectTemplate'",
                (meta[eg.lower()], eg))
            n += 1
    db.commit()
    db.close()
    return n


def update_static(meta):
    src = open(STATIC).read()
    pattern = re.compile(
        r"(?m)^(\s*)\('([0-9a-fA-F-]+)', '([0-9a-fA-F-]+)', "
        r"(\d+), 'MoveCardToZoneEffectTemplate', ''\),?$")

    def repl(m):
        indent, ag, eg, order = m.groups()
        param = meta.get(eg.lower())
        if param is None:
            return m.group(0)
        return (f"{indent}('{ag}', '{eg}', {order}, "
                f"'MoveCardToZoneEffectTemplate', {param!r}),")

    new_src, count = pattern.subn(repl, src)
    if count:
        open(STATIC, "w").write(new_src)
    return count


if __name__ == "__main__":
    move_meta = load_move_zone_meta()
    db_n = update_db(move_meta)
    static_n = update_static(move_meta)
    print(f"move-zone params: DB rows {db_n}, static rows {static_n}")
