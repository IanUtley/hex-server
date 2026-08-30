#!/usr/bin/env python3
"""Extract PvP champion abilities from Records/*.jsonl and seed champion_abilities table.

Reads ChampionTemplate.jsonl + AbilityTemplate.jsonl from Hex/Records/ and
inserts/updates the champion_abilities table in hconnect.db.

Each row: champion_guid, champion_name, ability_guid, ability_name,
           charge_cost, spell_cost, threshold_colors, game_text.
"""

import json, sqlite3, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "hconnect.db")
RECORDS = os.path.join(ROOT, "Records")

CASTING = {"BasicAction": 8, "QuickAction": 64}


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


# --- Load ability templates --------------------------------------------------

ab_data = {}   # guid -> {name, charge_cost, threshold}
with open(os.path.join(RECORDS, "AbilityTemplate.jsonl")) as f:
    for line in f:
        l = line.strip()
        if len(l) < 20:
            continue
        try:
            data = json.loads(json.loads(l))
        except Exception:
            continue
        ag = data.get("m_AbilityTemplateId", {}).get("m_Guid", "")
        if not ag:
            continue
        cond = data.get("m_AbilityCondition") or {}
        ab_data[ag] = {
            "name": data.get("m_Name", ""),
            "charge_cost": data.get("m_ChargePointCost", 0),
            "spell_cost": data.get("m_ActivationCost", 0),
            "threshold": cond.get("m_ColorFlags", ""),
            "casting_behavior": CASTING.get(data.get("m_CastingBehavior", ""), 0),
            "thresholds": json.dumps(parse_thresholds(cond)),
            "targets": json.dumps([
                t.get("m_Guid") for t in (data.get("m_AbilityTargetTemplateIds") or [])
                if t.get("m_Guid")
            ]),
        }
print(f"Loaded {len(ab_data)} ability templates")

# --- Load champion abilities ------------------------------------------------

champs = []  # [(champ_guid, champ_name, ability_guid, game_text), ...]
with open(os.path.join(RECORDS, "ChampionTemplate.jsonl")) as f:
    for line in f:
        l = line.strip()
        if "PvPChampion" not in l or len(l) < 20:
            continue
        try:
            data = json.loads(json.loads(l))
        except Exception:
            continue
        if data.get("m_ChampionType") != "PvPChampion":
            continue
        cg = data["m_Id"]["m_Guid"]
        name = data.get("m_Name", "?")
        game_text = data.get("m_GameText", "")
        for ab_entry in data.get("m_ChampionAbilities", []):
            ag = ab_entry.get("m_CardAbilityId", {}).get("m_Guid", "")
            if ag:
                champs.append((cg, name, ag, game_text))

print(f"Found {len(champs)} PvP champion abilities")

# --- Seed the DB ------------------------------------------------------------

db = sqlite3.connect(DB)
db.execute("""
    CREATE TABLE IF NOT EXISTS champion_abilities (
        champion_guid TEXT NOT NULL,
        champion_name TEXT NOT NULL,
        ability_guid TEXT NOT NULL,
        ability_name TEXT NOT NULL DEFAULT '',
        charge_cost INTEGER NOT NULL DEFAULT 0,
        spell_cost INTEGER NOT NULL DEFAULT 0,
        threshold_colors TEXT NOT NULL DEFAULT '',
        game_text TEXT NOT NULL DEFAULT '',
        casting_behavior INTEGER NOT NULL DEFAULT 0,
        thresholds_json TEXT NOT NULL DEFAULT '[]',
        target_template_ids TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (champion_guid, ability_guid)
    )
""")
cols = {r[1] for r in db.execute("PRAGMA table_info(champion_abilities)")}
if "thresholds_json" not in cols:
    db.execute("ALTER TABLE champion_abilities ADD COLUMN thresholds_json TEXT DEFAULT '[]'")
if "target_template_ids" not in cols:
    db.execute("ALTER TABLE champion_abilities ADD COLUMN target_template_ids TEXT DEFAULT '[]'")

# Also create a cache table for champion metadata
db.execute("""
    CREATE TABLE IF NOT EXISTS champion_templates_extended (
        guid TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        race TEXT NOT NULL DEFAULT '',
        champion_class TEXT NOT NULL DEFAULT '',
        gender TEXT NOT NULL DEFAULT '',
        is_selectable INTEGER NOT NULL DEFAULT 0,
        starting_health INTEGER NOT NULL DEFAULT 20,
        faction TEXT NOT NULL DEFAULT ''
    )
""")

inserted = 0
for cg, cname, ag, game_text in champs:
    ab = ab_data.get(ag, {})
    db.execute(
        "INSERT OR REPLACE INTO champion_abilities "
        "(champion_guid, champion_name, ability_guid, ability_name, "
        "charge_cost, spell_cost, threshold_colors, game_text, "
        "casting_behavior, thresholds_json, target_template_ids) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cg, cname, ag, ab.get("name", ""),
         ab.get("charge_cost", 0), ab.get("spell_cost", 0),
         ab.get("threshold", ""), game_text,
         ab.get("casting_behavior", 0), ab.get("thresholds", "[]"),
         ab.get("targets", "[]")))
    # Also upsert extended champion template data from the Records
    # (already loaded above, but we need more fields)
    inserted += 1

db.commit()

# Now seed extended champion template rows from ChampionTemplate.jsonl
with open(os.path.join(RECORDS, "ChampionTemplate.jsonl")) as f:
    for line in f:
        l = line.strip()
        if "PvPChampion" not in l or len(l) < 20:
            continue
        try:
            data = json.loads(json.loads(l))
        except Exception:
            continue
        if data.get("m_ChampionType") != "PvPChampion":
            continue
        if not data.get("m_IsPlayerSelectable"):
            continue
        cg = data["m_Id"]["m_Guid"]
        db.execute(
            "INSERT OR REPLACE INTO champion_templates_extended "
            "(guid, name, race, champion_class, gender, is_selectable, "
            "starting_health, faction) VALUES (?,?,?,?,?,1,?,?)",
            (cg, data.get("m_Name", ""),
             data.get("m_Race", ""), data.get("m_Class", ""),
             data.get("m_Gender", ""),
             data.get("m_StartingHealth", 20),
             data.get("m_Faction", "")))

db.commit()
db.close()

print(f"Seeded {inserted} champion abilities and template rows")
