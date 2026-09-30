"""Records-backed fixtures for native RulesPort resolution tests.

The native port resolves every ability from the immutable Records snapshot,
so a focused test cannot drive it from SQLite metadata alone the way the
retired legacy walker could.  These helpers deserialize the same near-JSON
record shapes the client ships and install them into the shared store for the
duration of one test, restoring the store (and its derived graph cache)
afterwards.
"""

from __future__ import annotations

from contextlib import contextmanager

from gamedata import DEFAULT_RECORD_STORE
from gamedata.records import deserialize

_ABILITY_SECTION = "AbilityTemplate"
_EFFECT_SECTION = "AbilityEffectTemplate"
_TARGET_SECTION = "AbilityTargetTemplate"

_TARGET_TYPES = "Game.Shared.Mechanics.Abilities.TargetTemplates"
_EFFECT_TYPES = "Game.Shared.Mechanics.Abilities"

_ZERO_GUID = "00000000-0000-0000-0000-000000000000"


class RecordsInstaller:
    """Install synthetic records into the shared snapshot for one test."""

    def target(self, guid, kind="AbilityTargetTemplate", *, is_auto=1,
               is_random=0, player_filter="Self", collection_flags="Warzone",
               minimum=1, maximum=1, optional=0, explicit=0, card_filter=None,
               game_text=""):
        self._install(_TARGET_SECTION, {
            "_t": f"{_TARGET_TYPES}.{kind}",
            "m_TemplateId": {"m_Guid": guid},
            "m_Name": kind,
            "m_GameText": game_text,
            "m_IsAutoTarget": int(bool(is_auto)),
            "m_IsRandomTarget": int(bool(is_random)),
            "m_PlayerFilter": player_filter,
            "m_CollectionFlags": collection_flags,
            "m_MinTargetCount": int(minimum),
            "m_MaxTargetCount": int(maximum),
            "m_Optional": int(bool(optional)),
            "m_Explicit": int(bool(explicit)),
            "m_CardFilter": card_filter or {},
            "m_AllowBestEffortMinimumTargetCount": 0,
        })
        return guid

    def effect(self, guid, concrete_type, *, name="", text="", **fields):
        record = {
            "_t": f"{_EFFECT_TYPES}.{concrete_type}",
            "m_TemplateId": {"m_Guid": guid},
            "m_Name": name or concrete_type.removesuffix(
                "AbilityEffectTemplate"),
            "m_GameText": text,
        }
        record.update(fields)
        self._install(_EFFECT_SECTION, record)
        return guid

    def ability(self, guid, *, effects=(), targets=(), name="", variables=(),
                trigger_event_type=None, trigger_condition=None,
                ability_condition=None, **fields):
        record = {
            "_t": "Reckoning.Game.AbilityTemplate",
            "m_AbilityTemplateId": {"m_Guid": guid},
            "m_Name": name,
            "m_GameText": "",
            "m_AbilityCondition": ability_condition,
            "m_TriggerEventType": trigger_event_type,
            "m_TriggerCondition": trigger_condition,
            "m_AbilityEffectList": list(effects),
            "m_AbilityTargetTemplateIds": [
                {"m_Guid": target} for target in targets],
            "m_Variables": list(variables),
            "m_ActivationCost": 0,
            "m_ChargePointCost": 0,
            "m_LifeCost": 0,
            "m_Manual": 0,
            "m_IgnoresChain": 0,
            "m_RecalculateAutoTargets": 0,
        }
        record.update(fields)
        self._install(_ABILITY_SECTION, record)
        return guid

    @staticmethod
    def mapping(effect_guid, *, target_index=0, instance=0, group=1,
                condition="", contingent=-1, secondary=-1, duration="Instant",
                recalculate="True"):
        return {
            "m_EffectTemplateId": {"m_Guid": effect_guid},
            "m_TargetTemplateIndex": int(target_index),
            "m_EffectInstanceId": int(instance),
            "m_EffectDuration": duration,
            "m_OutputVariables": {},
            "m_ContingentEffectInstanceId": int(contingent),
            "m_IsOptional": 0,
            "m_ConditionId": {"m_Guid": condition or _ZERO_GUID},
            "m_EffectGroupId": int(group),
            "m_RecalculateTargets": recalculate,
            "m_SecondaryTargetIndex": int(secondary),
        }

    @staticmethod
    def modifier(modifier_type, *, input_variable=None, amount=None,
                 operation=None, **fields):
        modifier = {"_t": f"Game.Shared.Mechanics.Modifiers.{modifier_type}"}
        if input_variable is not None:
            modifier["m_InputValue"] = {
                "_t": "Game.Shared.Mechanics.Abilities.EffectInputVariable",
                "m_InputVariableName": str(input_variable),
            }
        if amount is not None:
            modifier["m_Amount_DEPRECATED"] = int(amount)
        if operation is not None:
            modifier["m_Operation"] = operation
        modifier.update(fields)
        return modifier

    @staticmethod
    def count_variable(name, *, card_filter=None, player_filter="Self",
                       collection_flags="Warzone",
                       different_races_faction="Unknown", default=0):
        return {
            "_t": ("Game.Shared.Mechanics.Abilities."
                   "CardCountAbilityVariable"),
            "m_Name": name,
            "m_DefaultValue": int(default),
            "m_PlayerFilter": player_filter,
            "m_CardFilter": card_filter or {},
            "m_CollectionFlags": collection_flags,
            "m_OnlyIncludeDifferentRacesForFaction": different_races_faction,
            "m_DontRecalculate": 0,
        }

    @staticmethod
    def constant(name, value):
        return {
            "_t": "Game.Shared.Mechanics.Abilities.AbilityConstant",
            "m_Name": name,
            "m_DefaultValue": int(value),
        }

    def _install(self, section, obj):
        record = deserialize(obj)
        store = DEFAULT_RECORD_STORE
        store.load(section)
        store._cache.setdefault(section, []).append(record)
        store._index.setdefault(section, {})[record.guid.lower()] = record
        return record


@contextmanager
def synthetic_records():
    """Run a block with a synthetic-record installer and fully restore after."""
    store = DEFAULT_RECORD_STORE
    saved_cache = {key: list(value) for key, value in store._cache.items()}
    saved_index = {key: dict(value) for key, value in store._index.items()}
    graphs = getattr(store, "_ability_graph_cache", None)
    saved_graphs = dict(graphs) if isinstance(graphs, dict) else None
    try:
        yield RecordsInstaller()
    finally:
        store._cache = saved_cache
        store._index = saved_index
        if saved_graphs is not None:
            store._ability_graph_cache = saved_graphs
        else:
            store.__dict__.pop("_ability_graph_cache", None)
