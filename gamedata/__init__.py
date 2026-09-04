"""Typed access to the client-derived Records snapshot.

The Records files contain serialized polymorphic C# objects rather than a
normal JSON schema.  This package keeps that object graph intact and exposes
small semantic views for the ability engine.
"""

from .models import (
    AbilityCost,
    CounterCost,
    AbilityEffectMapping,
    AbilityEffectTemplate,
    AbilityOptionEntry,
    AbilityOptionGroup,
    AbilityTargetTemplate,
    AbilityTemplate,
    CardTemplate,
    ConditionTemplate,
    RecordObject,
    TargetSpec,
)
from .records import RecordIssue, RecordStore, deserialize, deserialize_line
from .semantics import (AbilityGraph, EffectSpec, ability_graph,
                         card_ability_graphs, runtime_effects)
from .play_plan import (AbilityInstance, ActivationData, CardPlayCost,
                        PlayPlan, PromptSpec)

__all__ = [
    "AbilityCost",
    "CounterCost",
    "AbilityEffectMapping",
    "AbilityEffectTemplate",
    "AbilityOptionEntry",
    "AbilityOptionGroup",
    "AbilityTargetTemplate",
    "AbilityTemplate",
    "AbilityGraph",
    "AbilityInstance",
    "ActivationData",
    "CardTemplate",
    "CardPlayCost",
    "ConditionTemplate",
    "EffectSpec",
    "RecordIssue",
    "RecordObject",
    "RecordStore",
    "PlayPlan",
    "PromptSpec",
    "TargetSpec",
    "ability_graph",
    "card_ability_graphs",
    "runtime_effects",
    "deserialize",
    "deserialize_line",
]
