#!/usr/bin/env python3
"""Extract socketed-gem data from the extracted Records:

    - Records/InventoryItemData.jsonl  -> GEM_TEMPLATES (gem type -> abilities)
    - Records/AbilityTemplate.jsonl    -> GEM_ABILITY_META (ability rows)
    - Records/AbilityEffectTemplate.jsonl -> GEM_ABILITY_EFFECTS (BOM rows
      carrying the IntAttrModifier gamedata fields, e.g. Rage 1)

The gem's numeric key is the EGemTypesNew enum value (sequential from 1:
Wild_Minor_1=1 ... Blood_Minor_1=5 ...), which is what decks.active_gems and
CardUpdated.Gems carry.

Usage:
    python3 AssetExtraction/extract_gems.py
"""

import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static.py")
DB = os.path.join(ROOT, "hconnect.db")
RECORDS = os.path.join(ROOT, "Records")

BEGIN_MARKER = "### BEGIN GEM SEED"
END_MARKER = "### END GEM SEED"

# EGemTypesNew enum values (sequential from 1, Game.Shared.Mechanics).  The
# client can socket gems from later chapters too; include the complete enum so
# a valid later gem cannot leave a stale cached ability on the deck.
GEM_TYPE_VALUES = [
    "Wild_Minor_1", "Wild_Minor_2", "Wild_Major_1", "Wild_Major_2",
    "Blood_Minor_1", "Blood_Minor_2", "Blood_Major_1", "Blood_Major_2",
    "Ruby_Minor_1", "Ruby_Minor_2", "Ruby_Major_1", "Ruby_Major_2",
    "Diamond_Minor_1", "Diamond_Minor_2", "Diamond_Major_1", "Diamond_Major_2",
    "Sapphire_Minor_1", "Sapphire_Minor_2", "Sapphire_Major_1", "Sapphire_Major_2",
    "Warrior_Minor", "Warrior_Major", "Ranger_Minor", "Ranger_Major",
    "Cleric_Minor", "Cleric_Major", "Mage_Minor", "Mage_Major",
    "Herofall_Wild_Minor_1", "Herofall_Wild_Minor_2",
    "Herofall_Wild_Major_1", "Herofall_Wild_Major_2",
    "Herofall_Blood_Minor_1", "Herofall_Blood_Minor_2",
    "Herofall_Blood_Major_1", "Herofall_Blood_Major_2",
    "Herofall_Ruby_Minor_1", "Herofall_Ruby_Minor_2",
    "Herofall_Ruby_Major_1", "Herofall_Ruby_Major_2",
    "Herofall_Diamond_Minor_1", "Herofall_Diamond_Minor_2",
    "Herofall_Diamond_Major_1", "Herofall_Diamond_Major_2",
    "Herofall_Sapphire_Minor_1", "Herofall_Sapphire_Minor_2",
    "Herofall_Sapphire_Major_1", "Herofall_Sapphire_Major_2",
    "Frostheart_Wild_Minor_1", "Frostheart_Wild_Major_1",
    "Frostheart_Blood_Minor_1", "Frostheart_Blood_Major_1",
    "Frostheart_Ruby_Minor_1", "Frostheart_Ruby_Major_1",
    "Frostheart_Diamond_Minor_1", "Frostheart_Diamond_Major_1",
    "Frostheart_Sapphire_Minor_1", "Frostheart_Sapphire_Major_1",
    "Doombringer_Gem_Blood_Major_1", "Doombringer_Gem_Blood_Minor_1",
    "Doombringer_Gem_Blood_Minor_2",
    "Doombringer_Gem_Diamond_Major_1", "Doombringer_Gem_Diamond_Minor_1",
    "Doombringer_Gem_Diamond_Minor_2",
    "Doombringer_Gem_Ruby_Major_1", "Doombringer_Gem_Ruby_Minor_1",
    "Doombringer_Gem_Ruby_Minor_2",
    "Doombringer_Gem_Sapphire_Major_1", "Doombringer_Gem_Sapphire_Minor_1",
    "Doombringer_Gem_Sapphire_Minor_2",
    "Doombringer_Gem_Wild_Major_1", "Doombringer_Gem_Wild_Minor_1",
    "Doombringer_Gem_Wild_Minor_2",
]
GEM_TYPE_ID = {name: i + 1 for i, name in enumerate(GEM_TYPE_VALUES)}


def _records(name):
    out = []
    path = os.path.join(RECORDS, name)
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('"$'):
                continue
            try:
                v = json.loads(json.loads(line))
            except Exception:
                continue
            if isinstance(v, dict):
                out.append(v)
    return out


def main():
    gem_rows = []
    seen_gems = set()
    abilities_by_guid = {}
    for rec in _records("AbilityTemplate.jsonl"):
        g = (rec.get("m_AbilityTemplateId") or {}).get("m_Guid", "").lower()
        if g:
            abilities_by_guid[g] = rec
    effects_by_guid = {}
    for rec in _records("AbilityEffectTemplate.jsonl"):
        g = (rec.get("m_TemplateId") or {}).get("m_Guid", "").lower()
        if g:
            effects_by_guid[g] = rec

    wanted_abilities = set()
    for rec in _records("InventoryItemData.jsonl"):
        s = json.dumps(rec)
        if "InventoryGemData" not in s or "m_GemTypeNew" not in s:
            continue
        gname = (rec.get("m_GemTypeNew") or "")
        gid = GEM_TYPE_ID.get(gname)
        if gid is None:
            continue
        name = rec.get("m_Name", "")
        abilities = []
        for cont in (rec.get("m_Abilities") or []):
            ag = (cont.get("m_CardAbilityId") or {}).get("m_Guid", "").lower()
            if ag:
                abilities.append(ag)
        if gid in seen_gems:
            continue
        seen_gems.add(gid)
        gem_rows.append((gid, gname, name, json.dumps(abilities)))
        wanted_abilities.update(abilities)
    gem_rows.sort()

    ability_rows = []
    effect_rows = []
    seen_effects = set()
    for ag in sorted(wanted_abilities):
        rec = abilities_by_guid.get(ag)
        if not rec:
            continue
        s = json.dumps(rec)
        trig = (rec.get("m_TriggerEventType") or {}).get(
            "m_InternalType", "")
        tids = [t.get("m_Guid", "").lower()
                for t in (rec.get("m_AbilityTargetTemplateIds") or []) if t]
        ability_rows.append(
            (ag, 1, trig, rec.get("m_GameText", "") or "", s, 64, 0, 0,
             0, 0, json.dumps(tids), 0))
        # BOM rows: each effect's IntAttrModifier gamedata fields (attribute /
        # operation / value) drive the statics layer — no game-text parsing.
        for order, entry in enumerate(rec.get("m_AbilityEffectList") or []):
            eg = (entry.get("m_EffectTemplateId") or {}).get("m_Guid", "").lower()
            eff = effects_by_guid.get(eg)
            if not eff:
                continue
            etype = str(eff.get("_t", "")).split(".")[-1]
            mod = eff.get("m_Modifier") or {}
            param = ""
            if mod and isinstance(mod, dict):
                param = json.dumps({
                    "property": "intattr",
                    "attribute": mod.get("m_Attribute", ""),
                    "operation": mod.get("m_Operation", "Add"),
                    "amount": int(mod.get("m_Value") or 0),
                    "duration": entry.get("m_EffectDuration", "Permanent"),
                    "text": eff.get("m_GameText", "") or "",
                })
            elif etype == "ActivateAbilityEffectTemplate":
                param = (eff.get("m_AbilityToInvoke") or {}).get(
                    "m_Guid", "").lower()
            key = (ag, eg, order)
            if key in seen_effects:
                continue
            seen_effects.add(key)
            effect_rows.append((ag, eg, order, etype, param))
    ability_rows.sort()
    effect_rows.sort()

    # Live DB upsert.
    db = sqlite3.connect(DB)
    cols = {r[1] for r in db.execute("PRAGMA table_info(gem_templates)")}
    if not cols:
        db.execute(
            "CREATE TABLE gem_templates ("
            "gem_type INTEGER PRIMARY KEY, gem_type_name TEXT, "
            "name TEXT, abilities_json TEXT DEFAULT '[]')")
    db.executemany(
        "INSERT OR REPLACE INTO gem_templates "
        "(gem_type, gem_type_name, name, abilities_json) VALUES (?,?,?,?)",
        gem_rows)
    if ability_rows:
        db.executemany(
            "INSERT OR REPLACE INTO card_abilities_meta "
            "(ability_guid, is_triggered, trigger_event_type, game_text, "
            "raw_json, casting_behavior, is_manual, activation_cost, "
            "uses_per_game, uses_per_turn, target_template_ids, "
            "exhausts_on_use) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ability_rows)
    if effect_rows:
        db.executemany(
            "INSERT OR REPLACE INTO ability_effects "
            "(ability_guid, effect_guid, effect_order, effect_type, param) "
            "VALUES (?,?,?,?,?)",
            effect_rows)
    db.commit()
    db.close()

    # Splice into static.py between the markers.
    lines = [f"# {len(gem_rows)} socketed gems. Auto-generated by "
             "AssetExtraction/extract_gems.py — do not edit by hand.",
             "GEM_TEMPLATES = ["]
    for row in gem_rows:
        lines.append("    " + repr(row) + ",")
    lines.append("]")
    lines.append("")
    lines.append("GEM_ABILITY_META = [")
    for row in ability_rows:
        lines.append("    " + repr(row) + ",")
    lines.append("]")
    lines.append("")
    lines.append("GEM_ABILITY_EFFECTS = [")
    for row in effect_rows:
        lines.append("    " + repr(row) + ",")
    lines.append("]")
    block = "\n".join(lines)
    src = open(STATIC).read()
    begin = src.find(f"{BEGIN_MARKER}\n")
    end = src.find(f"{END_MARKER}")
    if begin < 0 or end < 0:
        raise SystemExit("static.py missing GEM SEED markers")
    end_of_block = src.find("\n", end)
    open(STATIC, "w").write(
        src[:begin] + f"{BEGIN_MARKER}\n{block}\n{END_MARKER}"
        + src[end_of_block:])
    print(f"Wrote {len(gem_rows)} gems, {len(ability_rows)} abilities, "
          f"{len(effect_rows)} BOM links")


if __name__ == "__main__":
    main()
