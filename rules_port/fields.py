"""RulesPort-owned evaluation of typed Records effect fields."""

from __future__ import annotations

import json


def _last_type(value):
    return str(value or "").rsplit(".", 1)[-1]


def _field_value(value, name, default=None):
    if value is None:
        return default
    if hasattr(value, "field"):
        return value.field(name, default)
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _field_name(field):
    return (_field_value(field, "m_InputVariableName", "") or
            _field_value(field, "m_VariableName", "") or
            _field_value(field, "m_Name", ""))


def _as_dict(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    # Live gamedata records are RecordObject instances.  They retain the
    # serialized typed fields in ``raw`` rather than implementing to_dict;
    # expose that representation at the Records adapter boundary.
    raw = getattr(value, "raw", None)
    if isinstance(raw, dict):
        return raw
    return value if isinstance(value, dict) else {}


def resolve_field(field, variables=None, outputs=None, battle_state=None,
                  default=0):
    """Evaluate a scalar Records EffectField without localized text."""
    variables = variables or {}
    outputs = outputs or {}
    battle_state = battle_state or {}
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
    field = _as_dict(field)
    if not field:
        return int(default or 0)
    kind = _last_type(field.get("_t"))
    if kind in {"EffectConstant", "AbilityConstant", "Constant"}:
        return int(field.get("m_Value", field.get("m_DefaultValue", default)) or 0)
    if kind in {"EffectInputVariable", "EffectAbilityVariable",
                "AbilityVariable", "EffectVariable"}:
        name = _field_name(field)
        return int(variables.get(name, battle_state.get(name, default)) or 0)
    if kind in {"EffectOutputVariable", "OutputVariable"}:
        name = _field_name(field) or field.get("m_OutputVariableName", "")
        return int(outputs.get(name, battle_state.get(name, default)) or 0)
    if kind in {"EffectCardIntegerVariable", "CardIntegerVariable"}:
        name = _field_name(field)
        values = battle_state.get("card_integer_variables") or {}
        return int(values.get(name, variables.get(name, default)) or 0)
    return int(default or 0)


def ability_variables(ability):
    graph = getattr(getattr(ability, "metadata", None), "graph", None)
    values = {}
    for variable in getattr(graph, "variables", ()) or ():
        name = _field_value(variable, "m_Name", "") or _field_value(
            variable, "m_VariableName", "")
        if name:
            values[str(name)] = _field_value(
                variable, "m_DefaultValue", _field_value(variable, "m_Value", 0))
    return values


def effect_template(ability, effect_guid):
    for effect in getattr(getattr(ability, "metadata", None), "effects", ()) or ():
        guid = getattr(effect, "guid", None)
        if isinstance(effect, dict):
            guid = effect.get("guid") or effect.get("effect_guid")
        if str(guid or "").lower() != str(effect_guid or "").lower():
            continue
        template = _as_dict(getattr(effect, "template", None))
        if not template and isinstance(effect, dict):
            from gamedata import DEFAULT_RECORD_STORE
            record = DEFAULT_RECORD_STORE.get("AbilityEffectTemplate", guid)
            template = _as_dict(record)
        return template
    return {}


def effect_field(ability, db, battle_state, effect_guid, field_name, default=0):
    template = effect_template(ability, effect_guid)
    variables = ability_variables(ability)
    variables.update((battle_state or {}).get("ability_variables") or {})
    outputs = (battle_state or {}).get("effect_outputs") or {}
    field = template.get(field_name)
    value = resolve_field(field, variables, outputs, battle_state, default)
    variable_name = _field_name(field) if _last_type(
        _as_dict(field).get("_t")) in {
            "EffectInputVariable", "EffectAbilityVariable",
            "AbilityVariable", "EffectVariable"} else ""
    if variable_name:
        from .static_rules import _expression_value
        graph = getattr(getattr(ability, "metadata", None), "graph", None)
        raw = json.dumps(_as_dict(getattr(graph, "source", None)))
        resolved = _expression_value(
            db,
            (battle_state or {}).get("session_id", 0), battle_state,
            (battle_state or {}).get("resolving_source_uid"),
            (battle_state or {}).get("resolving_owner_id", 0), raw,
            variable_name)
        if resolved is not None:
            return int(resolved)
    return value


def effect_template_value(ability, effect_guid, field_name, default=None):
    value = effect_template(ability, effect_guid).get(field_name, default)
    if isinstance(value, dict) and "m_Guid" in value:
        return str(value.get("m_Guid") or "").lower()
    return value
