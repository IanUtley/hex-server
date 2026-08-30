#!/usr/bin/env python3
"""Extract BOM (ability_effects) rows for PvP champion abilities.

Champion abilities (e.g. Dimmid's "[DIAMOND][DIAMOND]: [BASIC] [2] Target troop
gets Lifedrain this turn") are not talents, so their effect chains were never
seeded into ability_effects and resolve_effect returned nothing.  This script
expands each champion ability's m_AbilityEffectList from
Records/AbilityTemplate.jsonl + AbilityEffectTemplate.jsonl (same data-driven
logic as extract_talents.py) and writes the CHAMPION_ABILITY_EFFECTS block into
static.py plus upserts ability_effects in the DB.

Usage:
    python3 AssetExtraction/extract_champion_boms.py
"""

import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static.py")
DB = os.path.join(ROOT, "hconnect.db")
RECORDS = os.path.join(ROOT, "Records")

BEGIN_MARKER = "### BEGIN CHAMPION ABILITY EFFECT SEED"
END_MARKER = "### END CHAMPION ABILITY EFFECT SEED"
META_BEGIN_MARKER = "### BEGIN CHAMPION ABILITY META SEED"
META_END_MARKER = "### END CHAMPION ABILITY META SEED"


def load_gamedata():
    """Return {section_name: raw_text} from the Records JSONL files."""
    data = {}
    for fname in ("AbilityTemplate.jsonl", "AbilityEffectTemplate.jsonl",
                  "ChampionTemplate.jsonl"):
        with open(os.path.join(RECORDS, fname)) as fh:
            data[fname] = fh.read()
    return data


def section(data, name):
    """Decode JSONL records into raw record strings (unescaped quotes)."""
    out = []
    for line in data[name].splitlines():
        line = line.strip()
        if not line or line.startswith('"$'):
            continue
        try:
            inner = json.loads(line)
        except Exception:
            continue
        if isinstance(inner, str):
            out.append(inner)
    return out


def guid(rec, field):
    m = re.search(r'"%s"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"' % field, rec)
    return m.group(1).lower() if m else ""


def str_field(rec, field):
    m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % field, rec)
    if not m:
        return ""
    return json.loads('"' + m.group(1) + '"')


def effect_list(rec):
    """Return ordered [(effect_guid, duration)] from m_AbilityEffectList."""
    m = re.search(r'"m_AbilityEffectList"\s*:\s*\[(.*?)\]\s*,?\s*"m_AbilityTargetTemplateIds"', rec, re.S)
    body = m.group(1) if m else ""
    out = []
    for eg in re.findall(r'"m_EffectTemplateId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', body):
        out.append((eg.lower(), ""))
    durs = re.findall(r'"m_EffectDuration"\s*:\s*"([^"]+)"', body)
    for i, eg in enumerate([e for e, _ in out]):
        out[i] = (eg, durs[i] if i < len(durs) else "")
    return out


def variables(arec):
    """Parse m_Variables -> {name: default_value} (AbilityConstant entries)."""
    vars_map = {}
    vm = re.search(r'"m_Variables"\s*:\s*\[(.*?)\]\s*,?\s*"m_GameText"', arec, re.S)
    body = vm.group(1) if vm else ""
    for name, val in re.findall(
            r'"m_Name"\s*:\s*"([^"]+)"\s*,\s*"m_DefaultValue"\s*:\s*(-?\d+)', body):
        vars_map[name] = int(val)
    return vars_map


def champion_ability_meta(arec):
    """Return the card-meta-shaped row for a champion ability.

    Manual champion powers need this metadata too.  Their raw AbilityTemplate
    contains the target-template indexes and conditional effect groups that
    distinguish one random stat branch from applying every branch.
    """
    try:
        # Records JSONL contains Unity-exported objects with trailing commas;
        # use the same relaxed normalization as the other extraction paths.
        record = json.loads(re.sub(r",\s*([}\]])", r"\1", arec))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    trigger = record.get("m_TriggerEventType") or {}
    trigger_name = (trigger.get("m_InternalType", "")
                    if isinstance(trigger, dict) else str(trigger))
    target_ids = [
        guid for item in (record.get("m_AbilityTargetTemplateIds") or [])
        if isinstance(item, dict)
        for guid in [str(item.get("m_Guid") or "").lower()]
        if guid
    ]
    casting = {"BasicAction": 8, "QuickAction": 64}.get(
        str(record.get("m_CastingBehavior") or ""), 0)
    return (
        str((record.get("m_AbilityTemplateId") or {}).get("m_Guid") or "").lower(), casting,
        int(record.get("m_Manual", 0) or 0),
        int(record.get("m_ActivationCost", 0) or 0),
        int(record.get("m_UsesPerGame", 0) or 0),
        int(record.get("m_UsesPerTurn", 0) or 0),
        int(record.get("m_Cooldown", 0) or 0),
        int(record.get("m_ExhaustsCardOnUse", 0) or 0),
        1 if trigger_name else 0,
        json.dumps(target_ids), trigger_name,
        str(record.get("m_GameText") or ""),
        json.dumps(record, separators=(",", ":")),
    )


def main():
    data = load_gamedata()

    abilities = {}
    for rec in section(data, "AbilityTemplate.jsonl"):
        g = guid(rec, "m_AbilityTemplateId")
        if g:
            abilities[g] = rec

    effect_templates = {}
    for rec in section(data, "AbilityEffectTemplate.jsonl"):
        g = guid(rec, "m_TemplateId")
        if not g:
            continue
        m = re.search(r'"_t"\s*:\s*"([^"]+)"', rec)
        ttype = m.group(1).split(".")[-1] if m else "?"
        param = guid(rec, "m_AbilityToInvoke") or ""
        gtext = str_field(rec, "m_GameText")
        prop = ""
        pm = re.search(r'"m_Modifier"\s*:\s*\{\s*"_t"\s*:\s*"[^"]*\.(\w+)"', rec)
        if pm:
            prop = pm.group(1).replace("Modifier", "")
        random_param = ""
        if ttype == "RandomizeVariableEffectTemplate":
            variable = str_field(rec, "m_VariableName")
            min_match = re.search(r'"m_MinValue"\s*:\s*(-?\d+)', rec)
            max_match = re.search(r'"m_MaxValue"\s*:\s*(-?\d+)', rec)
            if variable and min_match and max_match:
                random_param = json.dumps({
                    "variable": variable,
                    "min": int(min_match.group(1)),
                    "max": int(max_match.group(1)),
                }, separators=(",", ":"))
        effect_templates[g] = (ttype, param, gtext, prop, random_param)

    # Champion ability GUIDs come from every PvP champion template.  FRA uses
    # non-player-selectable PvP champions too, and secondary/passive abilities
    # are often the triggered part of a champion (for example Dragon Guard
    # Stalwart's ChampionHealedEvent ability), not merely the first charge
    # power.
    heads = []
    for rec in section(data, "ChampionTemplate.jsonl"):
        if "PvPChampion" not in rec:
            continue
        m = re.search(r'"m_ChampionAbilities"\s*:\s*\[(.*?)\]\s*,?\s*"m_ArtNumber"', rec, re.S)
        body = m.group(1) if m else ""
        ags = re.findall(
            r'"m_CardAbilityId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', body)
        heads.extend(ag.lower() for ag in ags)

    ability_effects = []
    seen = set()
    pending = list(heads)
    while pending:
        ag = pending.pop()
        if ag in seen:
            continue
        seen.add(ag)
        arec = abilities.get(ag, "")
        var_map = variables(arec)
        for order, (eg, duration) in enumerate(effect_list(arec)):
            key = (ag, eg, order)
            if key in seen:
                continue
            ttype, invoke_param, gtext, prop, random_param = effect_templates.get(
                eg, ("?", "", "", "", ""))
            param = invoke_param
            if ttype == "RandomizeVariableEffectTemplate" and random_param:
                param = random_param
            if ttype == "CardModifierAbilityEffectTemplate":
                amount = 0
                am = re.search(r'([+-]?\d+)\s*\[(ATK|DEF)\]', gtext or "")
                if am:
                    amount = int(am.group(1))
                param = json.dumps({
                    "text": gtext,
                    "property": prop.lower(),
                    "amount": amount,
                    "duration": duration,
                })
            ability_effects.append((ag, eg, order, ttype, param))
            if ttype == "ActivateAbilityEffectTemplate" and param:
                pending.append(param.lower())

    ability_effects.sort(key=lambda r: (r[0], r[2]))
    champion_meta = []
    for ag in sorted(seen):
        meta = champion_ability_meta(abilities.get(ag, ""))
        if meta:
            champion_meta.append(meta)

    # Write the static.py block.
    lines = ["# Champion ability BOM rows: "
             "(ability_guid, effect_guid, effect_order, effect_type, param)",
             "CHAMPION_ABILITY_EFFECTS = ["]
    for ag, eg, order, ttype, param in ability_effects:
        lines.append(f"    ({ag!r}, {eg!r}, {order}, {ttype!r}, {param!r}),")
    lines.append("]")
    block = "\n".join(lines)
    meta_lines = [
        "# Champion ability metadata rows: (card_abilities_meta columns)",
        "CHAMPION_ABILITY_META = [",
    ]
    for row in champion_meta:
        meta_lines.append(f"    {row!r},")
    meta_lines.append("]")
    meta_block = "\n".join(meta_lines)

    static_src = open(STATIC).read()
    if BEGIN_MARKER not in static_src:
        anchor = "ABILITY_EFFECTS = ["
        idx = static_src.index(anchor)
        static_src = (static_src[:idx]
                      + f"{BEGIN_MARKER}\n{block}\n{END_MARKER}\n\n"
                      + static_src[idx:])
    else:
        begin = static_src.index(BEGIN_MARKER)
        end = static_src.index(END_MARKER)
        end_of_block = static_src.index("\n", end)
        static_src = (static_src[:begin]
                      + f"{BEGIN_MARKER}\n{block}\n{END_MARKER}"
                      + static_src[end_of_block:])
    if META_BEGIN_MARKER in static_src:
        begin = static_src.index(META_BEGIN_MARKER)
        end = static_src.index(META_END_MARKER)
        end_of_block = static_src.index("\n", end)
        static_src = (static_src[:begin]
                      + f"{META_BEGIN_MARKER}\n{meta_block}\n{META_END_MARKER}"
                      + static_src[end_of_block:])
    else:
        insert_at = static_src.index(META_END_MARKER) if META_END_MARKER in static_src else -1
        if insert_at < 0:
            marker_end = static_src.index(END_MARKER) + len(END_MARKER)
            static_src = (static_src[:marker_end]
                          + f"\n\n{META_BEGIN_MARKER}\n{meta_block}\n{META_END_MARKER}"
                          + static_src[marker_end:])
    open(STATIC, "w").write(static_src)

    # Upsert the DB.
    db = sqlite3.connect(DB)
    for ag, eg, order, ttype, param in ability_effects:
        db.execute(
            "INSERT OR REPLACE INTO ability_effects "
            "(ability_guid, effect_guid, effect_order, effect_type, param) "
            "VALUES (?,?,?,?,?)",
            (ag, eg, order, ttype, param))
    db.execute(
        "CREATE TABLE IF NOT EXISTS card_abilities_meta ("
        "ability_guid TEXT PRIMARY KEY, casting_behavior INTEGER DEFAULT 0, "
        "is_manual INTEGER DEFAULT 0, activation_cost INTEGER DEFAULT 0, "
        "uses_per_game INTEGER DEFAULT 0, uses_per_turn INTEGER DEFAULT 0, "
        "cooldown INTEGER DEFAULT 0, exhausts_on_use INTEGER DEFAULT 0, "
        "is_triggered INTEGER DEFAULT 0, target_template_ids TEXT DEFAULT '[]', "
        "trigger_event_type TEXT DEFAULT '', game_text TEXT DEFAULT '', "
        "raw_json TEXT DEFAULT '')")
    for row in champion_meta:
        db.execute(
            "INSERT OR REPLACE INTO card_abilities_meta "
            "(ability_guid, casting_behavior, is_manual, activation_cost, "
            "uses_per_game, uses_per_turn, cooldown, exhausts_on_use, "
            "is_triggered, target_template_ids, trigger_event_type, game_text, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    db.commit()
    db.close()
    print(f"Wrote {len(ability_effects)} champion BOM links "
          f"({len(heads)} champion abilities)")


if __name__ == "__main__":
    main()
