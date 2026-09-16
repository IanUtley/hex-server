"""Native queries over the Records-derived ability graph.

These helpers answer structural questions needed by the protocol boundary.
They intentionally do not inspect card text or the legacy BOM tables.
"""

from __future__ import annotations

from gamedata import DEFAULT_RECORD_STORE, ability_graph
from gamedata.records import reference_guid


def modifier_metadata(effect_guid):
    """Return typed CardModifier fields from the Records effect template."""
    effect = DEFAULT_RECORD_STORE.get(
        "AbilityEffectTemplate", str(effect_guid or "").lower())
    if effect is None:
        return {}
    modifier = effect.field("m_Modifier", {})
    if hasattr(modifier, "to_dict"):
        modifier = modifier.to_dict()
    if not isinstance(modifier, dict):
        return {}
    kind = str(modifier.get("_t", "")).rsplit(".", 1)[-1]
    properties = {
        "AttackModifier": "attack", "DefenseModifier": "defense",
        "DamageModifier": "damage", "LoseLifeModifier": "loselife",
        "HealHeroModifier": "healhero", "SetHeroHealthModifier": "setherohealth",
        "CardCostModifier": "cardcost", "ChargePointsModifier": "chargepoints",
        "SpellPointsModifier": "spellpoints", "CurrentResourceModifier": "currentresource",
        "TotalResourceModifier": "totalresource", "ThresholdModifier": "threshold",
        "CardThresholdModifier": "cardthreshold", "AttributeModifier": "attribute",
        "IntAttrModifier": "intattr", "CounterModifier": "counter",
        "DamageMultiplierModifier": "damagemultiplier", "DamageImmunityModifier": "damageimmunity",
        "DamageShieldModifier": "damageshield", "BlockImmunityModifier": "blockimmunity",
        "BlockImmunityExceptionModifier": "blockimmunityexception",
        "BlockRestrictionModifier": "blockrestriction",
        "TargetingImmunityModifier": "targetingimmunity",
        "AttackImmunityModifier": "attackimmunity", "SubTypeModifier": "subtype",
    }
    result = {"property": properties.get(kind, "")}
    for key in ("m_AttributeFlags", "m_Attribute", "m_Operation",
                "m_Value", "m_ThresholdColor", "m_Shard", "m_Subtype",
                "m_CardFilter", "m_SetThresholds", "m_RemoveAllCounters",
                "m_ReplaceExistingValue", "m_IsCombatDamage", "m_CombatDamageOnly",
                "m_NonCombatDamageOnly", "m_OneShot", "m_LastsIndefinitely"):
        if key in modifier:
            result[key[2:].lower()] = modifier[key]
    counter = modifier.get("m_CardCounterTemplateId")
    if isinstance(counter, dict) and counter.get("m_Guid"):
        result["counter_template_guid"] = str(counter["m_Guid"]).lower()
    input_value = modifier.get("m_InputValue")
    if isinstance(input_value, dict):
        result["input_variable"] = str(
            input_value.get("m_InputVariableName") or "")
        result["input_value"] = input_value.get("m_Value", 0)
    value_field = modifier.get("m_ValueField")
    if isinstance(value_field, dict):
        result["input_variable"] = str(
            value_field.get("m_InputVariableName") or "")
    return result


def _walk_effects(ability_guid):
    seen = set()

    def walk(guid):
        guid = str(guid or "").lower()
        if not guid or guid in seen:
            return
        seen.add(guid)
        graph = ability_graph(DEFAULT_RECORD_STORE, guid)
        if graph is None:
            return
        for effect in graph.effects:
            yield guid, graph, effect
            if effect.concrete_type == "ActivateAbilityEffectTemplate":
                template = getattr(effect, "template", None)
                child = reference_guid(
                    template.field("m_AbilityToInvoke")
                    if template is not None else None)
                yield from walk(child)

    yield from walk(ability_guid)


def ability_has_effect(ability_guid, concrete_type) -> bool:
    return any(effect.concrete_type == concrete_type
               for _guid, _graph, effect in _walk_effects(ability_guid))


def ability_effect_prompt(ability_guid, concrete_type):
    """Return ``(leaf ability GUID, target template GUID)`` if present."""
    for guid, graph, effect in _walk_effects(ability_guid):
        if effect.concrete_type != concrete_type:
            continue
        index = int(effect.target_index)
        target = graph.targets[index] if 0 <= index < len(graph.targets) else None
        return guid, target.guid if target is not None else None
    return None


def ability_cost_prompt(ability_guid, cost_kind):
    """Return the authored additional-cost target for an ability, if any."""
    graph = ability_graph(DEFAULT_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        return None
    wanted = str(cost_kind).lower()
    for kind, target_guid in graph.additional_cost_targets or ():
        if str(kind).lower() != wanted:
            continue
        return str(ability_guid).lower(), str(target_guid).lower()
    return None
