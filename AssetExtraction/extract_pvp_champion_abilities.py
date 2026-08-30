"""Backfill PvP champion charge-power abilities from the gamedata records.

The PvE talent extractor (extract_talents.py) only expands abilities referenced
by ChampionTalentData; PvP champion signature charge powers (e.g. Bun'jitsu's
"Void two ready troops you control. Summon an exhausted Abomination...") live
in champion_abilities and were previously hand-seeded with stale BOM rows.

For every ability_guid in champion_abilities this script upserts:

  * card_abilities_meta — game_text, target_template_ids (the ability's own
    m_AbilityTargetTemplateIds plus its m_VoidTarget), raw_json, casting
    behavior, activation cost (m_ChargePointCost);
  * ability_effects — the ordered m_AbilityEffectList with parent-level params
    (SummonToken token_guid/amount/collection/location/exhausted,
    CardModifier text/property/amount/duration/target_index).

Run from the repo root:

    python3 AssetExtraction/extract_pvp_champion_abilities.py
"""

import json
import re
import sqlite3

DB = "hconnect.db"


def _load_jsonl(path):
    out = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except Exception:
                continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _guid_of(rec, field):
    v = (rec.get(field) or {})
    return str(v.get("m_Guid") or "").lower() if isinstance(v, dict) else ""


def _str(rec, field):
    v = rec.get(field)
    return str(v) if v is not None else ""


def _int(rec, field):
    v = rec.get(field)
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _effect_type(rec):
    t = str(rec.get("_t") or "")
    return t.split(".")[-1]


def _card_modifier_prop(rec):
    m = re.search(r'"m_Modifier"\s*:\s*\{\s*"_t"\s*:\s*"[^"]*\.(\w+)"',
                  json.dumps(rec))
    return m.group(1).replace("Modifier", "") if m else ""


def main():
    abilities = {_guid_of(r, "m_AbilityTemplateId"): r
                 for r in _load_jsonl("Records/AbilityTemplate.jsonl")}
    effects = {_guid_of(r, "m_TemplateId"): r
               for r in _load_jsonl("Records/AbilityEffectTemplate.jsonl")}

    db = sqlite3.connect(DB)
    ags = [r[0] for r in db.execute("SELECT ability_guid FROM champion_abilities")]
    updated_meta = 0
    updated_effects = 0
    for ag in ags:
        arec = abilities.get(ag.lower())
        if not arec:
            continue
        tids = [str(t.get("m_Guid") or "").lower()
                for t in (arec.get("m_AbilityTargetTemplateIds") or [])
                if t.get("m_Guid")]
        void_tid = _guid_of(arec, "m_VoidTarget")
        if void_tid and void_tid not in tids:
            tids.append(void_tid)
        raw = json.dumps(arec)
        casting = {"QuickAction": 64, "BasicAction": 8}.get(
            _str(arec, "m_CastingBehavior"), 64)
        trigger = arec.get("m_TriggerEventType") or {}
        trigger_name = (_str(trigger, "m_InternalType")
                        if isinstance(trigger, dict) else str(trigger))
        db.execute(
            "INSERT OR REPLACE INTO card_abilities_meta "
            "(ability_guid, is_triggered, trigger_event_type, game_text, "
            "raw_json, casting_behavior, is_manual, activation_cost, "
            "uses_per_game, uses_per_turn, target_template_ids, exhausts_on_use) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ag.lower(), 1 if trigger_name else 0, trigger_name,
             _str(arec, "m_GameText"), raw, casting,
             _int(arec, "m_Manual"), _int(arec, "m_ChargePointCost"),
             _int(arec, "m_UsesPerGame"), _int(arec, "m_UsesPerTurn"),
             json.dumps(tids), _int(arec, "m_ExhaustsCardOnUse")))
        updated_meta += 1
        # Replace the ability's BOM rows with the gamedata list.
        db.execute("DELETE FROM ability_effects WHERE ability_guid=?",
                   (ag.lower(),))
        for order, entry in enumerate(arec.get("m_AbilityEffectList") or []):
            eg = (entry.get("m_EffectTemplateId") or {}).get("m_Guid", "")
            if not eg:
                continue
            et = effects.get(str(eg).lower())
            if not et:
                continue
            ttype = _effect_type(et)
            param = _guid_of(et, "m_AbilityToInvoke")
            if ttype == "SummonTokenTroopAbilityEffectTemplate":
                param = json.dumps({
                    "token_guid": _guid_of(et, "m_CardTemplateId"),
                    "amount": _int(et, "m_Amount") or 1,
                    "collection": _str(et, "m_CardCollection"),
                    "location": _str(et, "m_CardLocation"),
                    "exhausted": _int(et, "m_EntersPlayExhausted"),
                })
            elif ttype == "CardModifierAbilityEffectTemplate":
                gtext = _str(et, "m_GameText")
                prop = _card_modifier_prop(et).lower()
                amount = 0
                am = re.search(r'([+-]?\d+)\s*\[(ATK|DEF)\]', gtext or "")
                if am:
                    amount = int(am.group(1))
                param = json.dumps({
                    "text": gtext,
                    "property": prop,
                    "amount": amount,
                    "duration": _str(entry, "m_EffectDuration"),
                    "target_index": _int(entry, "m_TargetTemplateIndex"),
                })
            db.execute(
                "INSERT INTO ability_effects (ability_guid, effect_guid, "
                "effect_order, effect_type, param) VALUES (?,?,?,?,?)",
                (ag.lower(), str(eg).lower(), order, ttype, param or ""))
            updated_effects += 1
    db.commit()
    # Restore the group/condition/target mapping columns from raw_json.
    try:
        from db import db_backfill_ability_effect_meta
        n = db_backfill_ability_effect_meta(db)
        print(f"backfilled {n} effect-meta rows")
    except Exception as e:
        print(f"backfill skipped: {e}")
    print(f"PvP champion abilities: {updated_meta} meta rows, "
          f"{updated_effects} BOM links")


if __name__ == "__main__":
    main()
