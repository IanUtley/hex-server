"""Extract champion talent + ability data from the Hex gamedata blob.

Reads Data/gamedata (gzip, JSON-like records separated by $$--$$) and
materializes the talent tables directly into ``static.py`` (between the
``### BEGIN TALENT SEED`` / ``### END TALENT SEED`` markers):

    - TALENT_DATA      -> talent_data table
    - TALENT_ABILITIES -> talent_abilities table (cost + phase per granted ability)
    - ABILITY_EFFECTS  -> ability_effects BOM table (top-level ability -> leaf effects)

static.py seeds a fresh database from these lists; the data is self-contained
(no runtime dependency on this script or a generated module).

Run from the repo root:

    python3 AssetExtraction/extract_talents.py

The gamedata path defaults to the standard install location; override with
GAMEDATA=/path/to/gamedata.
"""

import gzip
import json
import os
import re

GAMEDATA = os.environ.get(
    "GAMEDATA",
    "/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Data/gamedata",
)
# static.py is one level up from AssetExtraction/.
STATIC_PY = os.path.join(os.path.dirname(__file__), "..", "static.py")

BEGIN_MARKER = "### BEGIN TALENT SEED"
END_MARKER = "### END TALENT SEED"

# Phase bitmask helpers (bit N = phase N); mirrors ability.py / battle_engine.
PHASE_PRE_GAME = 1 << 2            # 4
PHASE_MAIN = (1 << 10) | (1 << 19)  # 525312  (FirstMain | SecondMain)

# EAbilityCastingBehavior values (Game.Shared.Mechanics).
CASTING_BEHAVIOR = {
    "QuickAction": 64,
    "BasicAction": 8,
}


def _load_gamedata():
    with gzip.open(GAMEDATA, "rb") as f:
        return f.read().decode("utf-8", "replace")


def _section(data, name):
    """Return records of one gamedata section (list of raw record strings)."""
    marker = f"\n{name}\n$$--$$\n"
    start = data.find(marker)
    if start < 0:
        return []
    sec_start = start + len(marker)
    # The section ends at the next section header ($$$---$$$).
    end = data.find("$$$---$$$", sec_start)
    sec = data[sec_start:end] if end >= 0 else data[sec_start:]
    return sec.split("\n$$--$$\n")


def _guid(rec, field):
    """Extract a nested m_Guid: '"m_Guid" : "xxxx-...."'."""
    pat = '"' + re.escape(field) + r'"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"'
    m = re.search(pat, rec)
    return m.group(1).lower() if m else None


def _int_field(rec, field):
    m = re.search(rf'"{re.escape(field)}"\s*:\s*(-?\d+)', rec)
    return int(m.group(1)) if m else 0


def _str_field(rec, field):
    m = re.search(rf'"{re.escape(field)}"\s*:\s*"((?:[^"\\]|\\.)*)"', rec)
    if not m:
        return ""
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\")


def _object_field(rec, field):
    """Extract one nested object from a near-JSON gamedata record."""
    match = re.search(rf'"{re.escape(field)}"\s*:\s*\{{', rec)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(rec)):
        char = rec[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                raw = rec[start:index + 1]
                raw = re.sub(r',\s*([}\]])', r'\1', raw)
                try:
                    value = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return None
                return value if isinstance(value, dict) else None
    return None


def _trigger(rec):
    """Return the m_TriggerEventType internal type, or None for manual."""
    m = re.search(r'"m_TriggerEventType"\s*:\s*\{\s*"m_InternalType"\s*:\s*"([^"]+)"', rec)
    return m.group(1) if m else None


def _casting(rec):
    v = _str_field(rec, "m_CastingBehavior")
    return CASTING_BEHAVIOR.get(v, 0)


def _condition(rec):
    """Derive a compact condition spec from m_TriggerCondition.

    Returns a string the server can dispatch on (ability.py _CONDITIONS), or
    '' for unconditional. Supported patterns:
      - RequiresCardsControlled + IsColor + m_RequiredQuantity=N
        -> 'pregame_shards_in_deck:COLOR,COUNT'   (Shard Attuned / Cosmic Powers)
      - IntAttrFilter You>PermanentData>IsDungeonBoss Equals 1
        -> 'pregame_is_dungeon'                    (Heroism / Fearless / Fortitude)
    """
    i = rec.find('"m_TriggerCondition"')
    if i < 0:
        return ""
    cond = rec[i:rec.find('", "m_ActivationCost"', i)]
    if 'RequiresCardsControlled' in cond:
        color = re.search(r'"m_ColorFlags"\s*:\s*"([^"]+)"', cond)
        qty = re.search(r'"m_RequiredQuantity"\s*:\s*(\d+)', cond)
        if qty:
            if color:
                return f"pregame_shards_in_deck:{color.group(1)},{qty.group(1)}"
            return f"pregame_cards_in_deck:{qty.group(1)}"
    if 'IntAttrFilter' in cond and 'IsDungeonBoss' in cond:
        return "pregame_is_dungeon"
    return ""


def _target_template_ids(rec):
    """Return the ordered target-template GUIDs from an AbilityTemplate."""
    i = rec.find('"m_AbilityTargetTemplateIds"')
    if i < 0:
        return "[]"
    end = rec.find('],', i)
    body = rec[i:end if end >= 0 else len(rec)]
    return json.dumps([
        g.lower() for g in re.findall(
            r'"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', body)
    ])


def _effect_list(rec):
    """Return ordered [(effect_guid, duration), ...] from m_AbilityEffectList entries.

    Each entry carries an m_EffectDuration ("Permanent", "EndOfTurn", ...) that
    the leaf executor uses to decide whether the buff persists or wears off.
    """
    m = re.search(r'"m_AbilityEffectList"\s*:\s*\[(.*?)\]\s*,?\s*"m_AbilityTargetTemplateIds"', rec, re.S)
    body = m.group(1) if m else ""
    pairs = re.findall(
        r'"m_EffectTemplateId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"'
        r'[^{}]*\}.*?"m_EffectDuration"\s*:\s*"([^"]+)"', body, re.S)
    return [(g.lower(), d) for g, d in pairs]


def main():
    data = _load_gamedata()

    # Ability templates indexed by guid (for cost/casting/trigger/effects).
    abilities = {}
    for rec in _section(data, "AbilityTemplate"):
        g = _guid(rec, "m_AbilityTemplateId")
        if g:
            abilities[g] = rec

    # AbilityEffectTemplates indexed by guid: type + key param for execution.
    # For CardModifier we also capture the effect's own game text ("+2[ATK]")
    # and the modifier property (Attack/Defense) so the BOM is data-driven.
    effect_templates = {}
    for rec in _section(data, "AbilityEffectTemplate"):
        g = _guid(rec, "m_TemplateId")
        if not g:
            continue
        m = re.search(r'"_t"\s*:\s*"([^"]+)"', rec)
        ttype = m.group(1).split(".")[-1] if m else "?"
        # Key param: ActivateAbilityEffectTemplate -> m_AbilityToInvoke guid.
        param = _guid(rec, "m_AbilityToInvoke") or ""
        # RevealCards carries recipient visibility on the effect template. Keep
        # it with the BOM row so the runtime can distinguish a private
        # controller "look at" from a public reveal in PvP.
        if ttype == "RevealCardsAbilityEffectTemplate":
            param = json.dumps({
                "player_reveal_targets":
                    _str_field(rec, "m_PlayerRevealTargets") or "Everyone",
            })
        if ttype == "SummonTokenTroopAbilityEffectTemplate":
            values = {"token_guid": _guid(rec, "m_CardTemplateId") or ""}
            card_filter = _object_field(rec, "m_CardFilter")
            if card_filter:
                values["card_filter"] = card_filter
            param = json.dumps(values)
        gtext = _str_field(rec, "m_GameText")
        # CardModifier modifier property: AttackModifier / DefenseModifier.
        prop = ""
        pm = re.search(r'"m_Modifier"\s*:\s*\{\s*"_t"\s*:\s*"[^"]*\.(\w+)"', rec)
        if pm:
            prop = pm.group(1).replace("Modifier", "")
        effect_templates[g] = (ttype, param, gtext, prop)

    talent_data = []
    talent_abilities = []
    ability_effects = []
    seen_effects = set()

    # BOM expansion: we must also emit effect lists for sub-abilities that a
    # top-level ability *invokes* (ActivateAbilityEffectTemplate.m_AbilityToInvoke),
    # so resolve_effect can recurse.  Collect them transitively.
    bom_heads = set()   # abilities whose effect list we must expand
    pending_invocations = set()

    def _variables(arec):
        """Parse m_Variables -> {name: default_value} (AbilityConstant entries)."""
        vars_map = {}
        vm = re.search(r'"m_Variables"\s*:\s*\[(.*?)\]\s*,?\s*"m_GameText"', arec, re.S)
        body = vm.group(1) if vm else ""
        for name, val in re.findall(
                r'"m_Name"\s*:\s*"([^"]+)"\s*,\s*"m_DefaultValue"\s*:\s*(-?\d+)', body):
            vars_map[name] = int(val)
        return vars_map

    def expand_bom(ag):
        """Record the BOM for one ability and queue any invoked sub-abilities."""
        if ag in bom_heads:
            return
        bom_heads.add(ag)
        arec = abilities.get(ag, "")
        var_map = _variables(arec)
        for order, (eg, duration) in enumerate(_effect_list(arec)):
            key = (ag, eg, order)
            if key not in seen_effects:
                seen_effects.add(key)
                ttype, invoke_param, gtext, prop = effect_templates.get(eg, ("?", "", "", ""))
                # For CardModifier, resolve the amount from the effect's own
                # text (data-driven) and the ability's m_Variables for inputs.
                param = invoke_param
                if ttype == "CardModifierAbilityEffectTemplate":
                    amount = 0
                    am = re.search(r'([+-]?\d+)\s*\[(ATK|DEF)\]', gtext or "")
                    if am:
                        amount = int(am.group(1))
                    try:
                        param = json.dumps({
                            "text": gtext,
                            "property": prop.lower(),
                            "amount": amount,
                            "duration": duration,
                        })
                    except Exception:
                        param = gtext
                ability_effects.append((ag, eg, order, ttype, param))
                if ttype == "ActivateAbilityEffectTemplate" and param:
                    pending_invocations.add(param.lower())

    for rec in _section(data, "ChampionTalentData"):
        tg = _guid(rec, "m_Id")
        if not tg:
            continue
        name = _str_field(rec, "m_Name")
        desc = _str_field(rec, "m_Description")
        # m_Abilities[] -> list of m_CardAbilityId guids.
        m = re.search(r'"m_Abilities"\s*:\s*\[(.*?)\]\s*,?\s*"m_GameText"', rec, re.S)
        abody = m.group(1) if m else ""
        card_abilities = re.findall(
            r'"m_CardAbilityId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', abody)
        card_abilities = [g.lower() for g in card_abilities]

        has_ability = 1 if card_abilities else 0
        # talent_data carries the FIRST granted ability (as talent_map did).
        first_ab = card_abilities[0] if card_abilities else None
        acc = asc = 0
        if first_ab and first_ab in abilities:
            acc = _int_field(abilities[first_ab], "m_ChargePointCost")
            asc = _int_field(abilities[first_ab], "m_SpellPointCost")
        talent_data.append((tg, name, first_ab, has_ability, desc, acc, asc))

        # talent_abilities: one row per granted ability, with cost + phases.
        for ag in card_abilities:
            arec = abilities.get(ag, "")
            cc = _int_field(arec, "m_ChargePointCost")
            sc = _int_field(arec, "m_SpellPointCost")
            trig = _trigger(arec)
            phases = PHASE_PRE_GAME if trig and "PreGameEvent" in trig else PHASE_MAIN
            casting = _casting(arec)
            condition = _condition(arec)
            talent_abilities.append((
                tg, ag, cc, sc, phases, casting, condition,
                _target_template_ids(arec)))

            # ability_effects BOM: top-level ability -> its leaf effect templates.
            expand_bom(ag)

    # Transitively expand any sub-abilities invoked by the leaves above.
    while pending_invocations:
        ag = pending_invocations.pop()
        expand_bom(ag)

    # De-dupe: the same talent/ability pair may appear once per record; sort for determinism.
    talent_data = sorted(set(talent_data))
    talent_abilities = sorted(set(talent_abilities))
    ability_effects = sorted(set(ability_effects))

    # Build the generated block: three lists materialized into static.py.
    lines = []
    lines.append(f"# {len(talent_data)} talents, {len(talent_abilities)} granted abilities, "
                 f"{len(ability_effects)} BOM effect links. Auto-generated by "
                 "AssetExtraction/extract_talents.py — do not edit by hand.")
    lines.append("# talent_data rows: (talent_guid, name, ability_guid, has_ability, description, charge_cost, spell_cost)")
    lines.append("TALENT_DATA = [")
    for row in talent_data:
        g, n, ab, ha, d, acc, asc = row
        lines.append(f"    ({g!r}, {n!r}, {ab!r}, {ha}, {d!r}, {acc}, {asc}),")
    lines.append("]")
    lines.append("")
    lines.append("# talent_abilities rows: (talent_guid, ability_guid, charge_cost, spell_cost, activatable_phases, casting_behavior, condition, target_template_ids)")
    lines.append("TALENT_ABILITIES = [")
    for row in talent_abilities:
        g, ag, cc, sc, ph, cb, cond, targets = row
        lines.append(f"    ({g!r}, {ag!r}, {cc}, {sc}, {ph}, {cb}, {cond!r}, {targets!r}),")
    lines.append("]")
    lines.append("")
    lines.append("# ability_effects BOM rows: (ability_guid, effect_guid, effect_order, effect_type, param)")
    lines.append("ABILITY_EFFECTS = [")
    for row in ability_effects:
        ag, eg, order, ttype, param = row
        lines.append(f"    ({ag!r}, {eg!r}, {order}, {ttype!r}, {param!r}),")
    lines.append("]")

    block = "\n".join(lines)

    # Splice into static.py between the markers.
    static_src = open(STATIC_PY).read()
    begin = static_src.find(f"{BEGIN_MARKER}\n")
    end = static_src.find(f"{END_MARKER}")
    if begin < 0 or end < 0:
        raise SystemExit(f"static.py missing {BEGIN_MARKER}/{END_MARKER} markers")
    end_of_block = static_src.find("\n", end)
    new_static = (
        static_src[:begin]
        + f"{BEGIN_MARKER}\n{block}\n{END_MARKER}"
        + static_src[end_of_block:]
    )
    open(STATIC_PY, "w").write(new_static)

    print(f"Wrote {STATIC_PY}: {len(talent_data)} talents, {len(talent_abilities)} granted "
          f"abilities, {len(ability_effects)} BOM links")


if __name__ == "__main__":
    main()
