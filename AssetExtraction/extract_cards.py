"""Extract card templates + card-ability BOM into static.py.

Primary source is the gamedata ``CardTemplate`` section (7215 records — the
complete card pool).  We also read gamedata's ``AbilityTemplate`` /
``AbilityEffectTemplate`` sections to build the card-ability bill of
materials, exactly like ``extract_talents.py`` does for champion abilities.

Materializes two seed lists directly into ``static.py`` (between the
``### BEGIN CARD SEED`` / ``### END CARD SEED`` markers):

    - CARD_TEMPLATES — (guid, set_guid, name, rarity, cost, attack, defense,
      card_type, socket_count, no_pvp, is_pve, threshold_json,
      abilities_json, attributes)
    - CARD_ABILITY_EFFECTS — complete ``ability_effects`` rows for the
      transitive leaf-effect chain for EVERY card ability, merged with the
      talent BOM in the ``ability_effects`` table so champion AND card
      abilities resolve through the same walker.

Per card:

    - attributes: a bitmask of ``ECardAttributes`` (Flight=2, Speed=4,
      FirstStrike=16384 "Swift Strike", Steadfast=32, Juggernaught=16, ...)
      from ``m_AttributeFlags`` (pipe-separated names, e.g.
      "Flight|Juggernaught").  These drive combat rules.
    - abilities_json: the FULL ``m_CardAbilities`` ability GUID list (earlier
      imports stored only the first ability — this keeps them all).
    - threshold_json: ``{"values": [c,b,r,s,w,d], "list": [color indices]}``
      from ``m_Threshold``.

Run from the repo root:

    python3 AssetExtraction/extract_cards.py

Paths default to the standard install location; override with GAMEDATA=.
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

BEGIN_MARKER = "### BEGIN CARD SEED"
END_MARKER = "### END CARD SEED"

# ECardAttributes names -> bit values (Game.Shared.Mechanics.ECardAttributes).
# m_AttributeFlags uses the C# enum member names verbatim.
ATTRIBUTE_BITS = {
    "Unknown": 0,
    "SpiritDrain": 1,            # Life Drain
    "Flight": 2,
    "Speed": 4,
    "SkyGuard": 8,               # Sky Guard
    "Juggernaught": 16,          # Crush
    "Steadfast": 32,
    "Immortal": 64,              # Invincible
    "SpellShield": 128,          # Spell Shield
    "Unique": 256,
    "CantAttack": 512,           # Can't Attack
    "CantBlock": 1024,           # Can't Block
    "Defensive": 2048,
    "ForceAttack": 4096,         # Must Attack
    "CantReadyAutomatically": 8192,  # No Auto-Ready
    "FirstStrike": 16384,        # Swift Strike
    "Rage": 32768,
    "MustBlock": 65536,          # Must Block
    "CantBeBlocked": 131072,     # Can't be Blocked
    "PreventCombatDamage": 262144,
    "PreventNonCombatDamage": 524288,
    "PreventAllDamage": 786432,
    "DualStrike": 1048576,
    "CantInflictCombatDamage": 2097152,
    "CantInflictNonCombatDamage": 4194304,
    "CantInflictAnyDamage": 6291456,
    "EntersPlayExhausted": 8388608,
    "Inspire": 16777216,
    "Escalation": 33554432,
    "DoesntReadyNextReadyStep": 67108864,
    "VoidsDamagedTroops": 134217728,
    "QuickAction": 268435456,
    "AllowYardInspire": 536870912,
    "MustBeBlocked": 1073741824,
    "Boon": -2147483648,
}

# m_Threshold m_ColorFlags -> threshold index (0=Colorless,1=Blood,2=Ruby,
# 3=Sapphire,4=Wild,5=Diamond) — the "list" the server converts to ECardShards
# via {0:0, 1:4, 2:8, 3:16, 4:32, 5:64}.
THRESHOLD_COLOR_INDEX = {
    "Colorless": 0, "Blood": 1, "Ruby": 2, "Sapphire": 3, "Wild": 4, "Diamond": 5,
}


# --- gamedata section access -------------------------------------------------

def _gamedata_section(data, name):
    marker = f"\n{name}\n$$--$$\n"
    start = data.find(marker)
    if start < 0:
        return []
    sec_start = start + len(marker)
    end = data.find("$$$---$$$", sec_start)
    sec = data[sec_start:end] if end >= 0 else data[sec_start:]
    return sec.split("\n$$--$$\n")


def _guid(rec, field):
    pat = '"' + re.escape(field) + r'"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"'
    m = re.search(pat, rec)
    return m.group(1).lower() if m else None


def _str_field(rec, field):
    m = re.search(rf'"{re.escape(field)}"\s*:\s*"((?:[^"\\]|\\.)*)"', rec)
    if not m:
        return ""
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\")


def _int_field(rec, field):
    m = re.search(rf'"{re.escape(field)}"\s*:\s*(-?\d+)', rec)
    return int(m.group(1)) if m else 0


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


# --- per-card conversion -----------------------------------------------------

def _attributes_to_int(flags):
    """'Flight|Juggernaught' -> 2|16 = 18. 'Unknown' -> 0."""
    value = 0
    for name in str(flags or "Unknown").split("|"):
        value |= ATTRIBUTE_BITS.get(name.strip(), 0)
    return value


def _threshold_to_json(rec):
    """Extract m_Threshold from a card record and convert
    [{"m_ColorFlags": "Wild", "m_ThresholdColorRequirement": 2}] ->
    {"values": [0,0,0,0,2,0], "list": [4,4]}.  Gamedata embeds it as a JSON
    array (or null) after 'm_Threshold'."""
    m = re.search(r'"m_Threshold"\s*:\s*(\[[^\]]*\]|null)', rec)
    raw = m.group(1) if m else None
    if raw is None or raw == "null":
        return '{"values": [0, 0, 0, 0, 0, 0], "list": []}'
    try:
        reqs = json.loads(raw)
    except Exception:
        reqs = []
    values = [0, 0, 0, 0, 0, 0]
    lst = []
    for req in (reqs or []):
        color = THRESHOLD_COLOR_INDEX.get(str(req.get("m_ColorFlags")), 0)
        count = int(req.get("m_ThresholdColorRequirement") or 1)
        values[color] += count
        lst.extend([color] * count)
    return json.dumps({"values": values, "list": lst})


def _abilities_to_json(rec):
    """Extract the FULL m_CardAbilities GUID list from a card record."""
    m = re.search(r'"m_CardAbilities"\s*:\s*\[(.*?)\]\s*,?\s*"m_VariableCost"', rec, re.S)
    body = m.group(1) if m else ""
    guids = [g.lower() for g in re.findall(
        r'"m_CardAbilityId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', body)]
    return json.dumps(guids)


# --- card ability BOM (mirrors extract_talents.py) ---------------------------

def _effect_list(rec):
    """Return per-child wiring from the parent ability's m_AbilityEffectList.

    A child effect's parameters (duration, target index, condition) live in the
    PARENT ability record — not in the child's own AbilityEffectTemplate — so we
    capture them here to store at parent level. Returns a list of dicts:
    {guid, duration, target_index, group_id, instance_id, condition_id,
     contingent_instance_id, secondary_target_index, recalculate_targets,
     is_optional, output_variables}.
    """
    m = re.search(r'"m_AbilityEffectList"\s*:\s*\[(.*?)\]\s*,?\s*"m_AbilityTargetTemplateIds"', rec, re.S)
    body = m.group(1) if m else ""
    entries = []
    depth = 0
    start = None
    for i, ch in enumerate(body):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                entries.append(body[start:i + 1])
                start = None
    out = []
    for obj in entries:
        g = re.search(r'"m_EffectTemplateId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', obj)
        if not g:
            continue
        dur = re.search(r'"m_EffectDuration"\s*:\s*"([^"]+)"', obj)
        ti = re.search(r'"m_TargetTemplateIndex"\s*:\s*(-?\d+)', obj)
        gi = re.search(r'"m_EffectGroupId"\s*:\s*(-?\d+)', obj)
        ii = re.search(r'"m_EffectInstanceId"\s*:\s*(-?\d+)', obj)
        ci = re.search(r'"m_ConditionId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', obj)
        cond = ci.group(1).lower() if ci else ""
        if cond == "00000000-0000-0000-0000-000000000000":
            cond = ""
        def scalar(name, default):
            match = re.search(rf'"{name}"\s*:\s*(-?\d+)', obj)
            return int(match.group(1)) if match else default
        recalc = re.search(r'"m_RecalculateTargets"\s*:\s*"([^"]+)"', obj)
        recalc = {"True": 1, "False": 0, "UseDefault": -1}.get(
            recalc.group(1) if recalc else "", -1)
        output_variables = _object_field(obj, "m_OutputVariables") or {}
        out.append({
            "guid": g.group(1).lower(),
            "duration": dur.group(1) if dur else "",
            "target_index": int(ti.group(1)) if ti else -1,
            "group_id": int(gi.group(1)) if gi else 0,
            "instance_id": int(ii.group(1)) if ii else 0,
            "condition_id": cond,
            "contingent_instance_id": scalar("m_ContingentEffectInstanceId", -1),
            "secondary_target_index": scalar("m_SecondaryTargetIndex", -1),
            "recalculate_targets": recalc,
            "is_optional": scalar("m_IsOptional", 0),
            "output_variables": output_variables,
        })
    return out


def _variables(arec):
    """Parse an ability's m_Variables -> {name: default_value} (AbilityConstant)."""
    vars_map = {}
    vm = re.search(r'"m_Variables"\s*:\s*\[(.*?)\]\s*,?\s*"m_GameText"', arec, re.S)
    body = vm.group(1) if vm else ""
    for name, val in re.findall(
            r'"m_Name"\s*:\s*"([^"]+)"\s*,\s*"m_DefaultValue"\s*:\s*(-?\d+)', body):
        vars_map[name] = int(val)
    return vars_map


def _card_ability_bom(data, ability_guids):
    """Return complete ``ability_effects`` rows for every card ability.

    Parent-level wiring (groups, conditions, targets, and contingencies) is
    kept in the seed instead of being reconstructed by the runtime.
    """
    abilities = {}
    for rec in _gamedata_section(data, "AbilityTemplate"):
        g = _guid(rec, "m_AbilityTemplateId")
        if g:
            abilities[g] = rec

    effect_templates = {}
    for rec in _gamedata_section(data, "AbilityEffectTemplate"):
        g = _guid(rec, "m_TemplateId")
        if not g:
            continue
        m = re.search(r'"_t"\s*:\s*"([^"]+)"', rec)
        ttype = m.group(1).split(".")[-1] if m else "?"
        param = _guid(rec, "m_AbilityToInvoke") or ""
        # RandomizeVariable / SetAbilityVariable: the effect data (variable
        # name, min/max or fixed value) drives "choose one at random" and
        # set-variable branches in the authoritative resolver.
        if ttype == "RandomizeVariableEffectTemplate":
            param = json.dumps({
                "variable": _str_field(rec, "m_VariableName"),
                "min": _int_field(rec, "m_MinValue"),
                "max": _int_field(rec, "m_MaxValue"),
            })
        if ttype == "SetAbilityVariableEffectEffectTemplate":
            param = json.dumps({
                "variable": _str_field(rec, "m_VariableName"),
                "value": _int_field(rec, "m_Value") or 0,
            })
        # TACAbilityEffectTemplate: the param carries the serialized TAC
        # (m_SerializedTAC.data) so the resolver can decode the operation +
        # referenced ability GUID at runtime (never hardcoded).
        if ttype == "TACAbilityEffectTemplate":
            m2 = re.search(r'"m_SerializedTAC"\s*:\s*\{\s*"data"\s*:\s*"([^"]+)"', rec)
            param = m2.group(1) if m2 else ""
        # GrantAbility: the granted ability GUID comes from m_GrantedAbilityTemplateId,
        # not m_AbilityToInvoke.  Extract it so the BOM leaf executor knows which
        # ability to add to the target card's card_abilities list.
        if ttype == "GrantAbilityEffectTemplate":
            param = _guid(rec, "m_GrantedAbilityTemplateId") or ""
        # RevealCards carries recipient visibility on the effect template, not
        # on the ability or target template.  Preserve it in the BOM param so
        # PvP can keep "look at" effects private while still broadcasting true
        # public reveals.
        if ttype == "RevealCardsAbilityEffectTemplate":
            param = json.dumps({
                "player_reveal_targets":
                    _str_field(rec, "m_PlayerRevealTargets") or "Everyone",
            })
        # SummonToken: pack m_CardTemplateId + m_Amount into param JSON so the
        # BOM leaf executor can resolve the token GUID without parsing game_text.
        if ttype == "SummonTokenTroopAbilityEffectTemplate":
            token_guid = _guid(rec, "m_CardTemplateId") or ""
            amount = _int_field(rec, "m_Amount") or 1
            collection = _str_field(rec, "m_CardCollection") or "Warzone"
            location = _str_field(rec, "m_CardLocation") or "Unknown"
            # m_AmountField: variable name controlling the real count (e.g. "Two")
            amt_var = ""
            am = re.search(r'"m_AmountField"\s*:\s*\{[^}]*"m_InputVariableName"\s*:\s*"([^"]+)"', rec)
            if am:
                amt_var = am.group(1)
            values = {"token_guid": token_guid, "amount": amount,
                      "collection": collection, "location": location,
                      "amount_variable": amt_var}
            card_filter = _object_field(rec, "m_CardFilter")
            if card_filter:
                values["card_filter"] = card_filter
            param = json.dumps(values)
        # CardModifier: capture the modifier property (Attack/Defense/...) and
        # the EffectInputVariable name so the BOM can resolve the amount from
        # the PARENT ability's m_Variables — the child template holds only a
        # reference (e.g. "P2" -> 2), never the literal.
        gtext = _str_field(rec, "m_GameText")
        prop = ""
        pm = re.search(r'"m_Modifier"\s*:\s*\{\s*"_t"\s*:\s*"[^"]*\.(\w+)"', rec)
        if pm:
            prop = pm.group(1).replace("Modifier", "")
        ivar = ""
        iv = re.search(
            r'"m_InputValue"\s*:\s*\{\s*"_t"\s*:\s*"[^"]*EffectInputVariable"\s*,\s*'
            r'"m_InputVariableName"\s*:\s*"([^"]+)"', rec)
        if iv:
            ivar = iv.group(1)
        effect_templates[g] = (ttype, param, gtext, prop, ivar)

    rows = []
    all_discovered = set()
    seen = set()
    pending = [g for g in ability_guids if g]

    def _card_modifier_param(gtext, prop, ivar, var_map, e):
        """Parent-level CardModifier params for one child effect.

        property comes from the child's m_Modifier class; the amount resolves
        through the PARENT ability's m_Variables via the effect's
        EffectInputVariable reference (fallback: regex the effect text); and
        duration / target / condition come from the parent's m_AbilityEffectList
        entry.
        """
        amount = var_map.get(ivar, 0) if ivar else 0
        if not amount:
            am = re.search(r'([+-]?\d+)\s*\[(ATK|DEF)\]', gtext or "")
            if am:
                amount = int(am.group(1))
        return json.dumps({
            "text": gtext,
            "property": prop.lower(),
            "amount": amount,
            "duration": e["duration"],
            "target_index": e["target_index"],
            "condition_id": e["condition_id"],
        })

    def expand(ag):
        if ag in seen:
            return
        seen.add(ag)
        all_discovered.add(ag.lower())
        arec = abilities.get(ag, "")
        var_map = _variables(arec)
        for order, e in enumerate(_effect_list(arec)):
            eg = e["guid"]
            key = (ag, eg, order)
            if key in seen:
                continue
            seen.add(key)
            ttype, invoke_param, gtext, prop, ivar = effect_templates.get(
                eg, ("?", "", "", "", ""))
            param = invoke_param
            if ttype == "CardModifierAbilityEffectTemplate":
                param = _card_modifier_param(gtext, prop, ivar, var_map, e)
            rows.append((
                ag, eg, order, ttype, param,
                e["group_id"], e["condition_id"], e["target_index"],
                e["instance_id"], e["contingent_instance_id"],
                e["secondary_target_index"], e["recalculate_targets"],
                e["is_optional"], e["duration"],
                json.dumps(e["output_variables"], separators=(",", ":")),
            ))
            if ttype == "ActivateAbilityEffectTemplate" and param:
                pending.append(param.lower())
            if ttype == "GrantAbilityEffectTemplate" and param:
                pending.append(param.lower())

    while pending:
        ag = pending.pop()
        expand(ag)

    return sorted(set(rows)), all_discovered


def _ability_meta(data, ability_guids):
    """Per-ability activation metadata for every card ability.

    Returns (ability_guid, casting_behavior, is_manual, activation_cost,
    uses_per_game, uses_per_turn, cooldown, exhausts_on_use, is_triggered,
    target_template_ids).

    Mirrors the champion talent_abilities columns (casting_behavior:
    QuickAction=64, BasicAction=8) plus the fields that gate whether a manual
    ability can be activated on a card instance:
      - is_manual: 1 when m_Manual (no trigger, player-activated).
      - activation_cost: m_ActivationCost (resource cost to activate).
      - uses_per_game / uses_per_turn / cooldown / exhausts_on_use: activation
        limits (default 0 = unlimited).
      - is_triggered: 1 when m_TriggerEventType is present (auto, not manual).
      - target_template_ids: JSON list of m_AbilityTargetTemplateIds GUIDs.
        The client keys its target picker on these (GetTargetsFor matches a
        TargetInstance whose TemplateId equals the ability's target template id
        for index i), so a targeting option MUST carry the ability's real
        template id — not Invalid — or the picker shows zero valid targets.
    """
    abilities = {}
    for rec in _gamedata_section(data, "AbilityTemplate"):
        g = _guid(rec, "m_AbilityTemplateId")
        if g:
            abilities[g] = rec

    def _int(rec, field):
        m = re.search(rf'"{field}"\s*:\s*(-?\d+)', rec)
        return int(m.group(1)) if m else 0

    def _str(rec, field):
        m = re.search(rf'"{field}"\s*:\s*"([^"]*)"', rec)
        return m.group(1) if m else ""

    def _target_templates(rec):
        m = re.search(r'"m_AbilityTargetTemplateIds"\s*:\s*\[(.*?)\]\s*,\s*"m_VariableActivationCost"',
                      rec, re.S)
        if not m:
            return "[]"
        return json.dumps([g.lower() for g in re.findall(
            r'"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', m.group(1))])

    def _trigger_event_type(rec):
        m = re.search(r'"m_TriggerEventType"\s*:\s*\{\s*"m_InternalType"\s*:\s*"([^"]+)"', rec)
        return m.group(1) if m else ""

    def _raw_record(rec):
        """Store the FULL ability record as cleaned JSON so BOM walkers can
        data-drive per-effect values/durations (m_AbilityEffectList entries,
        m_EffectDuration, m_Variables) at runtime instead of regex-parsing."""
        import json as _json
        # Records are near-JSON: strip trailing commas before } / ].
        cleaned = re.sub(r',\s*([}\]])', r'\1', rec)
        cleaned = re.sub(r',\s*$', '', cleaned)
        try:
            return _json.dumps(_json.loads(cleaned))
        except Exception:
            return ""

    rows = []
    for g in ability_guids:
        if not g or g not in abilities:
            continue
        rec = abilities[g]
        casting = _str(rec, "m_CastingBehavior")
        casting_val = 64 if casting == "QuickAction" else (8 if casting == "BasicAction" else 0)
        trig = _trigger_event_type(rec)
        is_triggered = 1 if trig else 0
        rows.append((
            g,
            casting_val,
            1 if _int(rec, "m_Manual") else 0,
            _int(rec, "m_ActivationCost"),
            _int(rec, "m_UsesPerGame"),
            _int(rec, "m_UsesPerTurn"),
            _int(rec, "m_Cooldown"),
            1 if _int(rec, "m_ExhaustsCardOnUse") else 0,
            is_triggered,
            _target_templates(rec),
            trig,
            _str(rec, "m_GameText"),
            _raw_record(rec),
        ))
    return sorted(set(rows))


def _target_templates(data):
    """Extract (template_id, game_text) for every AbilityTargetTemplate.

    Used to decide card playability: a hand card that targets a troop (e.g.
    "Destroy target troop", "target troop you control", "target opposing troop")
    must not be offered when the relevant player has no warzone troops.
    """
    sec = data[data.find("AbilityTargetTemplate"):]
    rows = []
    for rec in sec.split("$$--$$"):
        m = re.search(r'"m_TemplateId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', rec)
        if not m:
            continue
        g = m.group(1).lower()
        gt = re.search(r'"m_GameText"\s*:\s*"([^"]*)"', rec)
        rows.append((g, gt.group(1) if gt else ""))
    return sorted(set(rows))


def main():
    with gzip.open(GAMEDATA, "rb") as f:
        data = f.read().decode("utf-8", "replace")

    card_rows = []
    all_card_abilities = set()
    for rec in _gamedata_section(data, "CardTemplate"):
        guid = _guid(rec, "m_Id")
        if not guid or len(guid) != 36:
            continue
        set_guid = _guid(rec, "m_SetId") or ""
        name = _str_field(rec, "m_Name")
        rarity = _str_field(rec, "m_CardRarity") or "Common"
        cost = _int_field(rec, "m_ResourceCost")
        attack = _int_field(rec, "m_BaseAttackValue")
        defense = _int_field(rec, "m_BaseDefenseValue")
        card_type = _str_field(rec, "m_CardType")
        socket_count = _int_field(rec, "m_SocketCount")
        no_pvp = 1 if _int_field(rec, "m_IneligibleForPvPRandomTemplates") else 0
        is_pve = 1 if _int_field(rec, "m_IsPvE") else 0
        threshold_json = _threshold_to_json(rec)
        abilities_json = _abilities_to_json(rec)
        attributes = _attributes_to_int(_str_field(rec, "m_AttributeFlags"))
        # m_SacrificeTarget = an additional-cost troop the caster must sacrifice
        # (e.g. Abominate). The GUID is an AbilityTargetTemplate id; an
        # all-zeros GUID means "no sacrifice".
        sm = re.search(r'"m_SacrificeTarget"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', rec)
        sacrifice_target = ""
        if sm:
            g = sm.group(1).lower()
            if g != "00000000-0000-0000-0000-000000000000":
                sacrifice_target = g
        # Variable X cost: m_VariableCost marks a "pay X" card (e.g. Burn to
        # the Ground "1X" = 1 base + X extra); m_VariableCostMinimum is the
        # smallest X the player may choose.
        variable_cost = _int_field(rec, "m_VariableCost")
        variable_cost_minimum = _int_field(rec, "m_VariableCostMinimum")
        # Some legacy templates carry a stale m_RageValue even though the
        # card text has no Rage keyword.  Only materialize Rage when the
        # template actually declares it in its game text.
        rage_value = _int_field(rec, "m_RageValue")
        if not rage_value:
            rage_match = re.search(r"\brage\s+(\d+)\b",
                                   _str_field(rec, "m_GameText"),
                                   re.IGNORECASE)
            rage_value = int(rage_match.group(1)) if rage_match else 0
        if rage_value and not re.search(
                r"\brage\b", _str_field(rec, "m_GameText"), re.IGNORECASE):
            rage_value = 0
        card_rows.append((guid, set_guid, name, rarity, cost, attack, defense,
                          card_type, socket_count, no_pvp, is_pve,
                          threshold_json, abilities_json, attributes,
                          sacrifice_target, variable_cost, variable_cost_minimum,
                          rage_value))
        for g in re.findall(
                r'"m_CardAbilityId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"',
                rec):
            all_card_abilities.add(g.lower())

    # Dedupe by guid (last wins) and sort for determinism.
    by_guid = {}
    for row in card_rows:
        by_guid[row[0]] = row
    card_rows = sorted(by_guid.values())

    bom_rows, all_discovered = _card_ability_bom(data, all_card_abilities)
    all_card_abilities |= all_discovered
    meta_rows = _ability_meta(data, all_card_abilities)
    target_rows = _target_templates(data)

    lines = []
    lines.append(f"# {len(card_rows)} cards. Auto-generated by "
                 "AssetExtraction/extract_cards.py — do not edit by hand.")
    lines.append("# CARD_TEMPLATES rows: (guid, set_guid, name, rarity, cost, attack, defense, "
                 "card_type, socket_count, no_pvp, is_pve, threshold_json, abilities_json, "
                 "attributes, sacrifice_target, variable_cost, variable_cost_minimum, rage_value)")
    lines.append("CARD_TEMPLATES = [")
    for (guid, set_guid, name, rarity, cost, attack, defense, card_type,
         socket_count, no_pvp, is_pve, threshold_json, abilities_json, attributes,
         sacrifice_target, variable_cost, variable_cost_minimum,
         rage_value) in card_rows:
        lines.append(
            f"    ({guid!r}, {set_guid!r}, {name!r}, {rarity!r}, {cost}, {attack}, {defense}, "
            f"{card_type!r}, {socket_count}, {no_pvp}, {is_pve}, {threshold_json!r}, "
            f"{abilities_json!r}, {attributes}, {sacrifice_target!r}, "
            f"{variable_cost}, {variable_cost_minimum}, {rage_value}),")
    lines.append("]")
    lines.append("")
    lines.append("# card ability BOM rows: (ability_guid, effect_guid, effect_order, effect_type, param, effect_group_id, condition_id, target_index, effect_instance_id, contingent_effect_instance_id, secondary_target_index, recalculate_targets, is_optional, effect_duration, output_variables)")
    lines.append("CARD_ABILITY_EFFECTS = [")
    for row in bom_rows:
        lines.append("    (" + ", ".join(repr(value) for value in row) + "),")
    lines.append("]")
    lines.append("")
    lines.append("# card ability activation meta rows: (ability_guid, casting_behavior, is_manual, "
                 "activation_cost, uses_per_game, uses_per_turn, cooldown, exhausts_on_use, "
                 "is_triggered, target_template_ids, trigger_event_type, game_text, raw_json)")
    lines.append("CARD_ABILITY_META = [")
    for (ag, casting, manual, cost, upg, upt, cd, exh, trig, tpls, evt, txt, rawj) in meta_rows:
        lines.append(f"    ({ag!r}, {casting}, {manual}, {cost}, {upg}, {upt}, {cd}, {exh}, {trig}, {tpls!r}, {evt!r}, {txt!r}, {rawj!r}),")
    lines.append("]")
    lines.append("")
    lines.append("# AbilityTargetTemplate rows: (template_id, game_text, is_auto_target, "
                 "is_random_target, optional, explicit, player_filter, collection_flags, "
                 "min_target_count, max_target_count, filter_json). The flag/filter "
                 "columns are backfilled from AbilityTargetTemplate.jsonl by "
                 "AssetExtraction/update_target_templates.py.")
    lines.append("TARGET_TEMPLATES = [")
    for tid, gtext in sorted(target_rows):
        lines.append(f"    ({tid!r}, {gtext!r}, 0, 0, 0, 0, '', '', 1, 1, '{{}}'),")
    lines.append("]")
    block = "\n".join(lines)

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

    print(f"Wrote {STATIC_PY}: {len(card_rows)} cards, {len(bom_rows)} card-ability BOM links, "
          f"{len(target_rows)} target templates")


if __name__ == "__main__":
    main()
