"""Typed views over the client Mechanics records.

These classes are intentionally thin.  They model the serialized contract and
leave execution to the game-state interpreter, so new client fields remain
available even before a dedicated semantic property is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .records import RecordObject, reference_guid, register_type


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _value(value: Any, default: int = 0) -> int:
    if isinstance(value, RecordObject):
        value = value.field("m_Value", default)
    elif isinstance(value, Mapping):
        value = value.get("m_Value", default)
    return _int(value, default)


_ZERO_GUID = "00000000-0000-0000-0000-000000000000"


def _guid(value: Any) -> str:
    return reference_guid(value).lower()


def _nonzero_guid(value: Any) -> str:
    guid = _guid(value)
    return "" if guid == _ZERO_GUID else guid


@dataclass(frozen=True)
class AbilityOptionEntry:
    value: int
    label: str


@dataclass(frozen=True)
class AbilityOptionGroup:
    target_property: str
    label: str
    options: tuple[AbilityOptionEntry, ...]

    @classmethod
    def from_value(cls, value: Any) -> "AbilityOptionGroup":
        if isinstance(value, cls):
            return value
        target_property = str(
            value.field("m_TargetProperty", "") if isinstance(value, RecordObject)
            else value.get("m_TargetProperty", "") if isinstance(value, Mapping)
            else "")
        label = str(value.field("m_Label", "") if isinstance(value, RecordObject)
                    else value.get("m_Label", "") if isinstance(value, Mapping)
                    else "")
        raw_options = (value.field("m_Options") if isinstance(value, RecordObject)
                       else value.get("m_Options") if isinstance(value, Mapping)
                       else ()) or ()
        options = []
        for option in raw_options:
            options.append(AbilityOptionEntry(
                value=_int(option.field("m_Value", 0)
                           if isinstance(option, RecordObject)
                           else option.get("m_Value", 0)
                           if isinstance(option, Mapping) else 0),
                label=str(option.field("m_Label", "")
                          if isinstance(option, RecordObject)
                          else option.get("m_Label", "")
                          if isinstance(option, Mapping) else ""),
            ))
        return cls(target_property, label, tuple(options))

    def is_valid(self, index: int) -> bool:
        return 0 <= index < len(self.options)


@dataclass(frozen=True)
class CounterCost:
    """One authored counter payment on an ability activation."""

    counter_guid: str
    amount: int

    def as_dict(self) -> dict[str, Any]:
        return {"counter_guid": self.counter_guid, "amount": self.amount}


@dataclass(frozen=True)
class AbilityCost:
    activation: int = 0
    charge_points: int = 0
    spell_points: int = 0
    life: int = 0
    variable_activation: int = 0
    variable_minimum: int = 0
    cooldown: int = 0
    uses_per_turn: int = 0
    uses_per_game: int = 0
    exhausts_card_on_use: bool = False
    is_charge_power: bool = False
    is_spell_power: bool = False
    counter_costs: tuple[CounterCost, ...] = ()
    target_costs: tuple[tuple[str, str], ...] = ()

    @property
    def is_free(self) -> bool:
        return not any((self.activation, self.charge_points, self.spell_points,
                        self.life, self.variable_activation,
                        self.counter_costs, self.target_costs))

    def as_dict(self) -> dict[str, Any]:
        return {
            "activation": self.activation,
            "charge_points": self.charge_points,
            "spell_points": self.spell_points,
            "life": self.life,
            "variable_activation": self.variable_activation,
            "variable_minimum": self.variable_minimum,
            "cooldown": self.cooldown,
            "uses_per_turn": self.uses_per_turn,
            "uses_per_game": self.uses_per_game,
            "exhausts_card_on_use": self.exhausts_card_on_use,
            "is_charge_power": self.is_charge_power,
            "is_spell_power": self.is_spell_power,
            "counter_costs": [cost.as_dict() for cost in self.counter_costs],
            "target_costs": [dict(kind=kind, guid=guid)
                             for kind, guid in self.target_costs],
        }


@register_type("Reckoning.Game.AbilityTemplate", "AbilityTemplate")
class AbilityTemplate(RecordObject):
    @property
    def ability_guid(self) -> str:
        return reference_guid(self.field("m_AbilityTemplateId")) or self.guid

    @property
    def name(self) -> str:
        return str(self.field("m_Name", ""))

    @property
    def game_text(self) -> str:
        return str(self.field("m_GameText", ""))

    @property
    def activation_game_text(self) -> str:
        return str(self.field("m_ActivationGameText", ""))

    @property
    def trigger_event_type(self) -> str:
        value = self.field("m_TriggerEventType")
        if isinstance(value, RecordObject):
            return str(value.field("m_InternalType", ""))
        if isinstance(value, Mapping):
            return str(value.get("m_InternalType", ""))
        return str(value or "")

    @property
    def is_triggered(self) -> bool:
        return bool(self.trigger_event_type or self.field("m_TriggerCondition"))

    @property
    def trigger_condition(self) -> Any:
        return self.field("m_TriggerCondition")

    @property
    def ability_condition(self) -> Any:
        return self.field("m_AbilityCondition")

    @property
    def ability_options(self) -> tuple[AbilityOptionGroup, ...]:
        return tuple(AbilityOptionGroup.from_value(value)
                     for value in (self.field("m_AbilityOptions") or ()))

    @property
    def ignores_chain(self) -> bool:
        return bool(_int(self.field("m_IgnoresChain")))

    @property
    def recalculate_auto_targets(self) -> bool:
        return bool(_int(self.field("m_RecalculateAutoTargets")))

    @property
    def casting_behavior(self) -> str:
        return str(self.field("m_CastingBehavior", ""))

    @property
    def manual(self) -> bool:
        return bool(_int(self.field("m_Manual")))

    @property
    def optional(self) -> bool:
        return bool(_int(self.field("m_Optional")))

    @property
    def uses_previous_state(self) -> bool:
        return bool(_int(self.field("m_UsesPreviousState")))

    @property
    def ability_index(self) -> int:
        return _int(self.field("m_AbilityIndex"), -1)

    @property
    def ability_free_condition(self) -> Any:
        return self.field("m_AbilityFreeCondition")

    @property
    def additional_cost_targets(self) -> tuple[tuple[str, str], ...]:
        """Typed additional-cost target references from AbilityTemplate.

        The client exposes singular and plural forms.  Preserve their kind so
        the activation UI can request the correct target before paying costs.
        """
        fields = (
            ("sacrifice", "m_SacrificeTarget"),
            ("exhaust", "m_ExhaustTarget"),
            ("discard", "m_DiscardTarget"),
            ("void", "m_VoidTarget"),
            ("put_into_deck", "m_PutIntoDeckTarget"),
            ("put_into_deck", "m_PutIntoDeckTarget2"),
            ("put_into_hand", "m_PutIntoHandTarget"),
            ("shuffle_into_deck", "m_ShuffleIntoDeckTarget"),
            ("reveal", "m_RevealTarget"),
        )
        result = []
        for kind, name in fields:
            guid = _nonzero_guid(self.field(name))
            if guid:
                result.append((kind, guid))
        plural = (
            ("discard", "m_DiscardTargets"),
            ("exhaust", "m_ExhaustTargets"),
        )
        for kind, name in plural:
            for value in self.field(name) or ():
                guid = _nonzero_guid(value)
                if guid:
                    result.append((kind, guid))
        return tuple(result)

    @property
    def target_template_guids(self) -> tuple[str, ...]:
        return tuple(
            guid for guid in (
                reference_guid(value)
                for value in (self.field("m_AbilityTargetTemplateIds") or [])
            ) if guid
        )

    @property
    def effect_mappings(self) -> tuple["AbilityEffectMapping", ...]:
        return tuple(AbilityEffectMapping.from_value(value)
                     for value in (self.field("m_AbilityEffectList") or []))

    @property
    def variables(self) -> tuple[Any, ...]:
        return tuple(self.field("m_Variables") or ())

    @property
    def costs(self) -> AbilityCost:
        counter_costs = []
        for value in self.field("m_CounterCosts") or ():
            guid = _nonzero_guid(
                value.field("m_CounterType") if isinstance(value, RecordObject)
                else value.get("m_CounterType") if isinstance(value, Mapping)
                else None)
            amount = _int(
                value.field("m_CounterAmount", 0) if isinstance(value, RecordObject)
                else value.get("m_CounterAmount", 0) if isinstance(value, Mapping)
                else 0)
            if guid:
                counter_costs.append(CounterCost(guid, amount))
        return AbilityCost(
            activation=_int(self.field("m_ActivationCost")),
            charge_points=_int(self.field("m_ChargePointCost")),
            spell_points=_int(self.field("m_SpellPointCost")),
            life=_int(self.field("m_LifeCost")),
            variable_activation=_int(self.field("m_VariableActivationCost")),
            variable_minimum=_int(self.field("m_VariableActivationCostMinimum")),
            cooldown=_int(self.field("m_Cooldown")),
            uses_per_turn=_int(self.field("m_UsesPerTurn")),
            uses_per_game=_int(self.field("m_UsesPerGame")),
            exhausts_card_on_use=bool(
                _int(self.field("m_ExhaustsCardOnUse"))),
            is_charge_power=bool(_int(self.field("m_IsChargePower"))),
            is_spell_power=bool(_int(self.field("m_IsSpellPower"))),
            counter_costs=tuple(counter_costs),
            target_costs=self.additional_cost_targets,
        )


@register_type("AbilityEffectTargetMapping")
class AbilityEffectMapping(RecordObject):
    """One entry in AbilityTemplate.m_AbilityEffectList."""

    @classmethod
    def from_value(cls, value: Any) -> "AbilityEffectMapping":
        if isinstance(value, cls):
            return value
        if isinstance(value, RecordObject):
            return cls(value.type_name or "AbilityEffectTargetMapping",
                       value.fields, value.raw)
        if isinstance(value, Mapping):
            return cls("AbilityEffectTargetMapping", dict(value), dict(value))
        return cls("AbilityEffectTargetMapping", {}, {})

    @property
    def effect_guid(self) -> str:
        return reference_guid(self.field("m_EffectTemplateId"))

    @property
    def target_index(self) -> int:
        return _int(self.field("m_TargetTemplateIndex"), -1)

    @property
    def effect_instance_id(self) -> int:
        return _int(self.field("m_EffectInstanceId"), -1)

    @property
    def effect_group_id(self) -> int:
        return _int(self.field("m_EffectGroupId"), 0)

    @property
    def condition_guid(self) -> str:
        return reference_guid(self.field("m_ConditionId"))

    @property
    def contingent_effect_instance_id(self) -> int:
        return _int(self.field("m_ContingentEffectInstanceId"), -1)

    @property
    def secondary_target_index(self) -> int:
        return _int(self.field("m_SecondaryTargetIndex"), -1)

    @property
    def recalculate_targets(self) -> str:
        return str(self.field("m_RecalculateTargets", "UseDefault"))

    @property
    def optional(self) -> bool:
        return bool(_int(self.field("m_IsOptional")))

    @property
    def effect_duration(self) -> str:
        return str(self.field("m_EffectDuration", "Instant"))

    @property
    def output_variables(self) -> Any:
        return self.field("m_OutputVariables", {})


@register_type("Game.Shared.Mechanics.Abilities.AbilityEffectTemplate",
              "AbilityEffectTemplate")
class AbilityEffectTemplate(RecordObject):
    @property
    def template_guid(self) -> str:
        return reference_guid(self.field("m_TemplateId")) or self.guid

    @property
    def name(self) -> str:
        return str(self.field("m_Name", ""))

    @property
    def operation(self) -> str:
        suffix = self.short_type
        return suffix.removesuffix("AbilityEffectTemplate") or suffix


@register_type("Game.Shared.Mechanics.Abilities.TargetTemplates.AbilityTargetTemplate",
              "AbilityTargetTemplate", "TargetTemplate")
class AbilityTargetTemplate(RecordObject):
    @property
    def template_guid(self) -> str:
        return reference_guid(self.field("m_TemplateId")) or self.guid

    @property
    def name(self) -> str:
        return str(self.field("m_Name", ""))

    @property
    def player_filter(self) -> str:
        return str(self.field("m_PlayerFilter", "Unknown"))

    @property
    def collection_flags(self) -> str:
        return str(self.field("m_CollectionFlags", "None"))

    @property
    def minimum(self) -> int:
        # The current client contract migrated the old fields into the typed
        # AbilityField members.  This server supports that one contract only;
        # an absent current field means the authored default (zero).
        return _value(self.field("m_MinTargetCount"), 0)

    @property
    def maximum(self) -> int:
        return _value(self.field("m_MaxTargetCount"), 0)

    @property
    def allow_best_effort_minimum(self) -> bool:
        return bool(_int(self.field("m_AllowBestEffortMinimumTargetCount")))

    @property
    def target_kind(self) -> str:
        return self.short_type

    @property
    def target_spec(self) -> "TargetSpec":
        return TargetSpec(
            guid=self.template_guid,
            name=self.name,
            is_auto=bool(_int(self.field("m_IsAutoTarget"))),
            is_random=bool(_int(self.field("m_IsRandomTarget"))),
            player_filter=self.player_filter,
            collection_flags=self.collection_flags,
            minimum=self.minimum,
            maximum=self.maximum,
            optional=bool(_int(self.field("m_Optional"))),
            explicit=bool(_int(self.field("m_Explicit"))),
            card_filter=self.field("m_CardFilter"),
            allow_best_effort_minimum=self.allow_best_effort_minimum,
            target_kind=self.target_kind,
        )


@register_type("Game.Shared.Mechanics.Abilities.AbilityEffectConditionTemplate",
              "AbilityEffectConditionTemplate")
class ConditionTemplate(RecordObject):
    @property
    def condition_guid(self) -> str:
        return reference_guid(self.field("m_TemplateId")) or self.guid

    @property
    def name(self) -> str:
        return str(self.field("m_Name", ""))

    @property
    def condition(self) -> Any:
        return self.field("m_Condition")


@register_type("Reckoning.Game.CardTemplate", "Game.Shared.CardTemplate",
              "CardTemplate")
class CardTemplate(RecordObject):
    @property
    def card_guid(self) -> str:
        return (reference_guid(self.field("m_CardTemplateId"))
                or reference_guid(self.field("m_Id")) or self.guid)

    @property
    def name(self) -> str:
        return str(self.field("m_Name", ""))

    @property
    def card_type(self) -> str:
        return str(self.field("m_CardType", ""))

    @property
    def resource_cost(self) -> int:
        return _int(self.field("m_ResourceCost"))

    @property
    def variable_cost(self) -> bool:
        return bool(_int(self.field("m_VariableCost")))

    @property
    def variable_cost_minimum(self) -> int:
        return _int(self.field("m_VariableCostMinimum"))

    @property
    def life_cost(self) -> int:
        return _int(self.field("m_LifeCost"))

    @property
    def threshold(self) -> Any:
        return self.field("m_Threshold")

    @property
    def play_condition(self) -> Any:
        return self.field("m_PlayCondition")

    @property
    def additional_cost_targets(self) -> tuple[tuple[str, str], ...]:
        fields = (
            ("sacrifice", "m_SacrificeTarget"),
            ("exhaust", "m_ExhaustTarget"),
            ("discard", "m_DiscardTarget"),
            ("void", "m_VoidTarget"),
            ("put_into_deck", "m_PutIntoDeckTarget"),
            ("put_into_deck", "m_PutIntoDeckTarget2"),
            ("put_into_hand", "m_PutIntoHandTarget"),
            ("shuffle_into_deck", "m_ShuffleIntoDeckTarget"),
            ("reveal", "m_RevealTarget"),
        )
        result = []
        for kind, name in fields:
            guid = _nonzero_guid(self.field(name))
            if guid:
                result.append((kind, guid))
        plural = (
            ("discard", "m_DiscardTargets"),
            ("exhaust", "m_ExhaustTargets"),
        )
        for kind, name in plural:
            for value in self.field(name) or ():
                guid = _nonzero_guid(value)
                if guid:
                    result.append((kind, guid))
        return tuple(result)

    @property
    def ability_guids(self) -> tuple[str, ...]:
        values = (self.field("m_Abilities")
                  or self.field("m_AbilityTemplateIds")
                  or self.field("m_AbilityIds")
                  or self.field("m_CardAbilities") or [])
        guids = []
        for value in values:
            guid = reference_guid(value)
            if not guid and isinstance(value, Mapping):
                guid = reference_guid(value.get("m_CardAbilityId"))
            if guid:
                guids.append(guid)
        return tuple(guids)


@dataclass(frozen=True)
class TargetSpec:
    guid: str
    name: str
    is_auto: bool
    is_random: bool
    player_filter: str
    collection_flags: str
    minimum: int
    maximum: int
    optional: bool
    explicit: bool
    card_filter: Any = None
    allow_best_effort_minimum: bool = False
    target_kind: str = "AbilityTargetTemplate"

    @property
    def requires_input(self) -> bool:
        # These specialized client target classes resolve from the source,
        # trigger, or a previously created/voided card rather than opening the
        # ordinary player card picker.  SourceRevealed remains input-bearing:
        # the reveal presentation is followed by a selection from its legal
        # cards.
        auto_kinds = {
            "PlayerTargetTemplate",
            "AbilitySourceCardTargetTemplate",
            "AbilityTriggerCardTargetTemplate",
            "SourceDrawnTargetTemplate",
            "SourceBuriedTargetTemplate",
            "SourceStoredTargetTemplate",
            "VoidedTargetTemplate",
            "AbilityCreatedTargetTemplate",
        }
        if self.is_random or self.is_auto or self.target_kind in auto_kinds:
            return False
        return self.explicit or not self.is_auto
