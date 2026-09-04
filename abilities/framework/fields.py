"""Runtime evaluation of the client's AbilityField/EffectField values.

The client does not read effect quantities from localized text.  It evaluates
typed fields such as ``EffectInputVariable`` and ``EffectOutputVariable``
against the current ``AbilityInstance``.  This module is the small server-side
equivalent used by effect executors; the current Records snapshot is the only
rules-data source.
"""

import json

from gamedata.records import RecordStore


_EFFECT_TEMPLATES = None
_ABILITY_TEMPLATES = None
_RECORD_STORE = RecordStore()


def _last_type(value):
    return str(value or "").rsplit(".", 1)[-1]


def _load_effect_templates():
    """Load effect templates once from the extracted gamedata snapshot."""
    global _EFFECT_TEMPLATES
    if _EFFECT_TEMPLATES is not None:
        return _EFFECT_TEMPLATES
    _EFFECT_TEMPLATES = {
        record.guid.lower(): record.to_dict()
        for record in _RECORD_STORE.load("AbilityEffectTemplate")
        if record.guid
    }
    return _EFFECT_TEMPLATES


def effect_template(effect_guid):
    """Return the typed effect-template record for a BOM effect GUID."""
    if not effect_guid:
        return None
    return _load_effect_templates().get(str(effect_guid).lower())


def _load_ability_templates():
    """Load the current typed AbilityTemplate records from Records.

    SQLite stores generated indexes and runtime state; it is not consulted for
    the ability definition or its typed variables.
    """
    global _ABILITY_TEMPLATES
    if _ABILITY_TEMPLATES is not None:
        return _ABILITY_TEMPLATES
    _ABILITY_TEMPLATES = {
        record.guid.lower(): record.to_dict()
        for record in _RECORD_STORE.load("AbilityTemplate")
        if record.guid
    }
    return _ABILITY_TEMPLATES


def _raw_ability(db, ability_guid):
    if not ability_guid:
        return {}
    # SQLite contains indexes/materialized state. All callers resolve the
    # current typed AbilityTemplate from Records.
    return _load_ability_templates().get(str(ability_guid).lower(), {})


def ability_record(db, ability_guid):
    """Return the authoritative AbilityTemplate record for *ability_guid*.

    Transitive grants are not always materialized in the compact database
    seed, but their source AbilityTemplate is still present in Records.  The
    runtime uses this accessor when resolving such an ability without making
    localized card text part of the rules path.
    """
    return _raw_ability(db, ability_guid)


def ability_variables(db, ability_guid):
    """Return AbilityTemplate constant defaults keyed by variable name."""
    record = _raw_ability(db, ability_guid)
    values = {}
    for field in record.get("m_Variables") or []:
        if not isinstance(field, dict):
            continue
        name = field.get("m_Name") or field.get("m_VariableName")
        if not name:
            continue
        value = field.get("m_DefaultValue", field.get("m_Value", 0))
        try:
            values[str(name)] = int(value or 0)
        except (TypeError, ValueError):
            continue
    return values


def _field_name(field):
    if not isinstance(field, dict):
        return ""
    return (field.get("m_InputVariableName") or field.get("m_VariableName")
            or field.get("m_Name") or "")


def field_variable_name(field):
    """Return the variable referenced by a serialized EffectField.

    ``EffectInputVariable`` is not a literal zero.  The client evaluates it
    against the active AbilityInstance, where names such as ``AForEach...``
    can be computed from the current board.  Keeping this small accessor
    public lets effect leaves use the same typed field contract without
    reading the localized ability text.
    """
    if not isinstance(field, dict):
        return ""
    if _last_type(field.get("_t")) not in (
            "EffectInputVariable", "EffectAbilityVariable",
            "AbilityVariable", "EffectVariable"):
        return ""
    return str(_field_name(field) or "")


def resolve_field(field, variables=None, outputs=None, bstate=None,
                  default=0):
    """Evaluate one client ``EffectField``/``AbilityField`` value.

    Unknown field kinds deliberately return the supplied default.  That is
    safer than treating an unresolved dynamic value as a localized number.
    """
    variables = variables or {}
    outputs = outputs or {}
    bstate = bstate or {}
    if field is None:
        return int(default or 0)
    if isinstance(field, bool):
        return int(field)
    if isinstance(field, (int, float)):
        return int(field)
    if isinstance(field, str):
        try:
            return int(field)
        except (TypeError, ValueError):
            return int(variables.get(field, default) or 0)
    if not isinstance(field, dict):
        return int(default or 0)

    kind = _last_type(field.get("_t"))
    if kind in ("EffectConstant", "AbilityConstant", "Constant"):
        return int(field.get("m_Value", field.get("m_DefaultValue", default)) or 0)
    if kind in ("EffectInputVariable", "EffectAbilityVariable",
                "AbilityVariable", "EffectVariable"):
        name = _field_name(field)
        return int(variables.get(name, bstate.get(name, default)) or 0)
    if kind in ("EffectOutputVariable", "OutputVariable"):
        name = (_field_name(field) or field.get("m_OutputVariableName") or "")
        return int(outputs.get(name, bstate.get(name, default)) or 0)
    if kind in ("EffectCardIntegerVariable", "CardIntegerVariable"):
        name = _field_name(field)
        card_values = bstate.get("card_integer_variables") or {}
        return int(card_values.get(name, variables.get(name, default)) or 0)

    return int(default or 0)


def effect_field(db, bstate, effect_guid, field_name, default=0):
    """Evaluate a field on the currently resolving effect template."""
    template = effect_template(effect_guid)
    if not template:
        return int(default or 0)
    ability_guid = (bstate or {}).get("resolving_ability")
    variables = dict(ability_variables(db, ability_guid))
    variables.update((bstate or {}).get("ability_variables") or {})
    outputs = (bstate or {}).get("effect_outputs") or {}
    # CardIntegerVariable is persistent per card instance.  Populate the
    # resolver cache from the authoritative row on every field evaluation so
    # a later activation sees a value set by an earlier one.
    source_uid = (bstate or {}).get("resolving_source_uid")
    if source_uid is not None:
        try:
            row = db.execute(
                "SELECT permanent_buffs FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                ((bstate or {}).get("session_id", 0), int(source_uid))).fetchone()
            if row and row[0]:
                data = json.loads(row[0] or "{}")
                values = data.get("card_integer_variables") or {}
                if values:
                    bstate["card_integer_variables"] = values
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            pass
    field = template.get(field_name)
    value = resolve_field(field, variables, outputs, bstate, default)
    # Numeric fields in the extracted effect template often contain an
    # EffectInputVariable.  Its default is intentionally zero for dynamic
    # values (for example "draw a card for each Blessing in your hand").
    # Resolve the named variable through the same metadata variable evaluator
    # used by the statics and CardModifier leaves.
    variable_name = field_variable_name(field)
    ability_guid = (bstate or {}).get("resolving_ability")
    if variable_name and ability_guid:
        try:
            from .statics import ability_variable_value
            resolved = ability_variable_value(
                db, (bstate or {}).get("session_id", 0), bstate,
                ability_guid, variable_name,
                (bstate or {}).get("resolving_owner_id", 0),
                (bstate or {}).get("resolving_source_uid"),
                stat_prop=field_name)
            if resolved is not None:
                return int(resolved)
        except (AttributeError, TypeError, ValueError):
            pass
    return value


def effect_template_value(db, bstate, effect_guid, field_name, default=None):
    """Read a non-numeric typed effect-template field."""
    template = effect_template(effect_guid)
    if not template:
        return default
    value = template.get(field_name, default)
    if isinstance(value, dict) and "m_Guid" in value:
        return str(value.get("m_Guid") or "").lower()
    return value


def counter_template_name(counter_guid):
    """Return the current Records name for a card-counter template."""
    record = _RECORD_STORE.get("CardCounterTemplate", str(counter_guid).lower())
    return str(record.field("m_Name", "")) if record is not None else ""


def modifier_metadata(effect_guid):
    """Return rule fields from a CardModifier's typed child template.

    The normalized BOM ``param`` is intentionally compact for compatibility,
    but the extracted effect record contains the authoritative modifier class
    and fields such as attribute flags, counter GUID, operation, and dynamic
    input value.  This adapter keeps leaves independent of localized text.
    """
    modifier = (effect_template(effect_guid) or {}).get("m_Modifier")
    if not isinstance(modifier, dict):
        return {}
    kind = _last_type(modifier.get("_t"))
    properties = {
        "AttackModifier": "attack",
        "DefenseModifier": "defense",
        "DamageModifier": "damage",
        "HealHeroModifier": "healhero",
        "LoseLifeModifier": "damage",
        "SetHeroHealthModifier": "setherohealth",
        "CardCostModifier": "cardcost",
        "ChargePointsModifier": "chargepoints",
        "SpellPointsModifier": "spellpoints",
        "CurrentResourceModifier": "currentresource",
        "TotalResourceModifier": "totalresource",
        "ThresholdModifier": "threshold",
        "CardThresholdModifier": "cardthreshold",
        "AttributeModifier": "attribute",
        "IntAttrModifier": "intattr",
        "CounterModifier": "counter",
        "DamageMultiplierModifier": "damagemultiplier",
        "DamageImmunityModifier": "damageimmunity",
        "BlockImmunityModifier": "blockimmunity",
        "BlockImmunityExceptionModifier": "blockimmunityexception",
        "BlockRestrictionModifier": "blockrestriction",
        "TargetingImmunityModifier": "targetingimmunity",
    }
    out = {"property": properties.get(kind, "")}
    for key in ("m_AttributeFlags", "m_Attribute", "m_Operation",
                "m_Value", "m_ThresholdColor", "m_Shard",
                "m_RemoveAllCounters", "m_RemoveHalfRoundedUp",
                "m_ReplaceExistingValue", "m_IsCombatDamage",
                "m_CombatDamageOnly", "m_NonCombatDamageOnly"):
        if key in modifier:
            out[key[2:].lower()] = modifier[key]
    counter = modifier.get("m_CardCounterTemplateId")
    if isinstance(counter, dict) and counter.get("m_Guid"):
        out["counter_template_guid"] = str(counter["m_Guid"]).lower()
    input_value = modifier.get("m_InputValue")
    if isinstance(input_value, dict):
        out["input_variable"] = field_variable_name(input_value)
        out["input_value"] = resolve_field(input_value, default=0)
    return out
