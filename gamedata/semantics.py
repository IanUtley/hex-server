"""Semantic ability graphs built from the typed Records object model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json

from .models import AbilityCost, AbilityEffectMapping, AbilityTemplate, TargetSpec
from .records import RecordObject, RecordStore, reference_guid


@dataclass(frozen=True)
class EffectSpec:
    guid: str
    concrete_type: str
    operation: str
    name: str
    target_index: int
    effect_instance_id: int
    effect_group_id: int
    duration: str
    condition_guid: str
    optional: bool
    recalculate_targets: str
    secondary_target_index: int
    output_variables: Any
    contingent_effect_instance_id: int = -1
    template: RecordObject | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "guid": self.guid,
            "concrete_type": self.concrete_type,
            "operation": self.operation,
            "name": self.name,
            "target_index": self.target_index,
            "effect_instance_id": self.effect_instance_id,
            "effect_group_id": self.effect_group_id,
            "duration": self.duration,
            "condition_guid": self.condition_guid,
            "contingent_effect_instance_id": self.contingent_effect_instance_id,
            "optional": self.optional,
            "recalculate_targets": self.recalculate_targets,
            "secondary_target_index": self.secondary_target_index,
            "output_variables": self.output_variables,
        }


@dataclass(frozen=True)
class AbilityGraph:
    guid: str
    name: str
    game_text: str
    activation_game_text: str
    casting_behavior: str
    manual: bool
    optional: bool
    trigger_event_type: str
    trigger_collection_flags: str
    trigger_condition: Any
    ability_condition: Any
    ability_free_condition: Any
    ignores_chain: bool
    recalculate_auto_targets: bool
    uses_previous_state: bool
    ability_index: int
    options: tuple[Any, ...]
    additional_cost_targets: tuple[tuple[str, str], ...]
    costs: AbilityCost
    targets: tuple[TargetSpec, ...]
    effects: tuple[EffectSpec, ...]
    variables: tuple[RecordObject, ...]
    source: AbilityTemplate

    def as_dict(self) -> dict[str, Any]:
        return {
            "guid": self.guid,
            "name": self.name,
            "game_text": self.game_text,
            "activation_game_text": self.activation_game_text,
            "casting_behavior": self.casting_behavior,
            "manual": self.manual,
            "optional": self.optional,
            "trigger_event_type": self.trigger_event_type,
            "trigger_collection_flags": self.trigger_collection_flags,
            "trigger_condition": self.trigger_condition,
            "ability_condition": self.ability_condition,
            "ability_free_condition": self.ability_free_condition,
            "ignores_chain": self.ignores_chain,
            "recalculate_auto_targets": self.recalculate_auto_targets,
            "uses_previous_state": self.uses_previous_state,
            "ability_index": self.ability_index,
            "options": [_plain(value) for value in self.options],
            "additional_cost_targets": [dict(kind=kind, guid=guid)
                                        for kind, guid in self.additional_cost_targets],
            "costs": self.costs.as_dict(),
            "targets": [target.__dict__ for target in self.targets],
            "effects": [effect.as_dict() for effect in self.effects],
            "variables": [variable.to_dict() if isinstance(variable, RecordObject)
                          else variable for variable in self.variables],
        }


def _type_name(value: Any) -> str:
    if isinstance(value, RecordObject):
        return value.type_name
    if isinstance(value, dict):
        return str(value.get("_t", ""))
    return ""


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, RecordObject):
        return value.field(name, default)
    if isinstance(value, dict):
        return value.get(name, default)
    return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _plain(value: Any) -> Any:
    if isinstance(value, RecordObject):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return {name: _plain(getattr(value, name))
                for name in value.__dataclass_fields__}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _effect_spec(mapping: AbilityEffectMapping,
                 effect: RecordObject | None) -> EffectSpec:
    if effect is None:
        return EffectSpec(
            guid=mapping.effect_guid, concrete_type="", operation="unknown",
            name="", target_index=mapping.target_index,
            effect_instance_id=mapping.effect_instance_id,
            effect_group_id=mapping.effect_group_id, duration="Instant",
            condition_guid=mapping.condition_guid,
            contingent_effect_instance_id=mapping.contingent_effect_instance_id,
            optional=mapping.optional,
            recalculate_targets="UseDefault", secondary_target_index=-1,
            output_variables=mapping.output_variables,
        )
    concrete = effect.short_type
    operation = (effect.operation if hasattr(effect, "operation")
                 else concrete.removesuffix("AbilityEffectTemplate") or concrete)
    return EffectSpec(
        guid=mapping.effect_guid,
        concrete_type=concrete,
        operation=operation,
        name=str(_field(effect, "m_Name", "")),
        target_index=mapping.target_index,
        effect_instance_id=mapping.effect_instance_id,
        effect_group_id=mapping.effect_group_id,
        duration=mapping.effect_duration,
        condition_guid=mapping.condition_guid,
        contingent_effect_instance_id=mapping.contingent_effect_instance_id,
        optional=mapping.optional,
        recalculate_targets=mapping.recalculate_targets,
        secondary_target_index=mapping.secondary_target_index,
        output_variables=mapping.output_variables,
        template=effect,
    )


def ability_graph(store: RecordStore, ability_guid: str) -> AbilityGraph | None:
    """Resolve one AbilityTemplate and all referenced target/effect records."""
    ability = store.get("AbilityTemplate", ability_guid)
    if not isinstance(ability, AbilityTemplate):
        return None
    targets = []
    for target_guid in ability.target_template_guids:
        target = store.get("AbilityTargetTemplate", target_guid)
        if target is not None and hasattr(target, "target_spec"):
            targets.append(target.target_spec)
    effects = []
    for mapping in ability.effect_mappings:
        effect = store.get("AbilityEffectTemplate", mapping.effect_guid)
        effects.append(_effect_spec(mapping, effect))
    return AbilityGraph(
        guid=ability.ability_guid,
        name=ability.name,
        game_text=ability.game_text,
        activation_game_text=ability.activation_game_text,
        casting_behavior=ability.casting_behavior,
        manual=ability.manual,
        optional=ability.optional,
        trigger_event_type=ability.trigger_event_type,
        trigger_collection_flags=str(ability.field("m_TriggerCollectionFlags", "")),
        trigger_condition=ability.trigger_condition,
        ability_condition=ability.ability_condition,
        ability_free_condition=ability.ability_free_condition,
        ignores_chain=ability.ignores_chain,
        recalculate_auto_targets=ability.recalculate_auto_targets,
        uses_previous_state=ability.uses_previous_state,
        ability_index=ability.ability_index,
        options=ability.ability_options,
        additional_cost_targets=ability.additional_cost_targets,
        costs=ability.costs,
        targets=tuple(targets),
        effects=tuple(effects),
        variables=tuple(value for value in ability.variables
                        if isinstance(value, RecordObject)),
        source=ability,
    )


def card_ability_graphs(store: RecordStore, card_guid: str) -> tuple[AbilityGraph, ...]:
    """Return all ability graphs referenced directly by a CardTemplate."""
    card = store.get("CardTemplate", card_guid)
    if card is None or not hasattr(card, "ability_guids"):
        return ()
    return tuple(graph for guid in card.ability_guids
                 if (graph := ability_graph(store, guid)) is not None)


def _nested_guid(value: Any, name: str) -> str:
    return reference_guid(_field(value, name, {})).lower()


def _effect_param(effect: EffectSpec) -> str:
    """Build the small runtime adapter payload still consumed by leaf APIs.

    The rules source is the typed effect record.  The payload is only an
    adapter for the existing leaf-function ABI; it is never used to decide
    whether an effect exists or where it belongs in the graph.
    """
    template = effect.template
    if template is None:
        return ""
    short = effect.concrete_type
    if short in ("ActivateAbilityEffectTemplate",
                 "ActivateTriggeredAbilityEffectTemplate"):
        return _nested_guid(template, "m_AbilityToInvoke")
    if short == "GrantAbilityEffectTemplate":
        return _nested_guid(template, "m_GrantedAbilityTemplateId")
    if short == "TACAbilityEffectTemplate":
        serialized = _field(template, "m_SerializedTAC", {})
        return str(_field(serialized, "data", "") or "")
    if short == "RandomizeVariableEffectTemplate":
        return json.dumps({
            "variable": _field(template, "m_VariableName", "RandomNumber"),
            "min": _field(template, "m_MinValue", 1),
            "max": _field(template, "m_MaxValue", 1),
        }, separators=(",", ":"))
    if short in ("CardModifierAbilityEffectTemplate",
                 "SetCardIntegerVariableEffectTemplate",
                 "SetAbilityVariableEffectEffectTemplate"):
        modifier = _field(template, "m_Modifier", {})
        modifier_type = _type_name(modifier)
        property_name = {
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
        }.get(modifier_type, "")
        payload = {
            "amount": 0,
            "duration": effect.duration,
            "variable": _field(template, "m_VariableName", ""),
            "operation": _field(template, "m_Operation", "Set"),
            "text": _field(template, "m_GameText", ""),
        }
        # Leave this absent when the modifier has a typed class not yet
        # listed here.  The leaf adapter can then fill it from its complete
        # modifier metadata instead of treating an empty value as a rule.
        if property_name:
            payload["property"] = property_name
        return json.dumps(payload, separators=(",", ":"))
    if short == "MoveCardToZoneEffectTemplate":
        return json.dumps({
            "destination": _field(template, "m_DestinationCollection", ""),
            "location": _field(template, "m_DestinationLocation", ""),
            "name": _field(template, "m_Name", ""),
        }, separators=(",", ":"))
    if short == "SummonTokenTroopAbilityEffectTemplate":
        amount_field = _field(template, "m_AmountField", {})
        amount_variable = _field(amount_field, "m_InputVariableName", "")
        payload = {
            "token_guid": _nested_guid(template, "m_CardTemplateId"),
            "amount": _field(template, "m_Amount", 1),
            "collection": _field(template, "m_CardCollection", ""),
            "location": _field(template, "m_CardLocation", "Unknown"),
            "card_filter": _plain(_field(template, "m_CardFilter")),
            "exhausted": _field(template, "m_EntersPlayExhausted", 0),
            "attacking": _field(template, "m_EntersPlayAttacking", 0),
            "copy_gems": _field(template, "m_CopyGems", 0),
        }
        if amount_variable:
            payload["amount_variable"] = amount_variable
        return json.dumps(payload, separators=(",", ":"))
    return ""


def runtime_effects(graph: AbilityGraph) -> tuple[dict[str, Any], ...]:
    """Adapt an authoritative Records graph to the existing leaf ABI."""
    effects = []
    for order, spec in enumerate(graph.effects):
        recalculate = {"True": 1, "False": 0, "UseDefault": -1}.get(
            str(spec.recalculate_targets), -1)
        condition = spec.condition_guid
        if condition.lower() == "00000000-0000-0000-0000-000000000000":
            condition = ""
        effects.append({
            "effect_guid": spec.guid,
            "effect_order": order,
            "effect_type": spec.concrete_type,
            "param": _effect_param(spec),
            "effect_group_id": spec.effect_group_id,
            "condition_id": condition,
            "target_index": spec.target_index,
            "effect_instance_id": spec.effect_instance_id,
            "contingent_effect_instance_id": spec.contingent_effect_instance_id,
            "secondary_target_index": spec.secondary_target_index,
            "recalculate_targets": recalculate,
            "is_optional": int(spec.optional),
            "effect_duration": spec.duration,
            "output_variables": _plain(spec.output_variables),
        })
    return tuple(effects)
