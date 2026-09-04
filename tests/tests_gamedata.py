"""Checks for the typed client-Records object model."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gamedata import (AbilityCost, AbilityGraph, AbilityInstance,
                      AbilityOptionEntry, AbilityOptionGroup,
                      AbilityTemplate, ActivationData, EffectSpec, PlayPlan,
                      RecordStore, TargetSpec, ability_graph,
                      card_ability_graphs, deserialize, deserialize_line,
                      runtime_effects)


PLAY_CARD_ABILITY = "5a8783b0-e420-4f41-b2a1-96f70b0cd851"
CARD_CHOOSE_BLOOD = "a32092b7-b469-4e2a-84da-f5e3687a93e7"
CONDITION_100_DECK = "eea0c082-9b19-0c74-36c0-64090413f6fc"


def test_deserialize_preserves_polymorphism_and_fields():
    store = RecordStore()
    ability = store.get("AbilityTemplate", PLAY_CARD_ABILITY)
    assert isinstance(ability, AbilityTemplate)
    assert ability.name == "Play Card Ability"
    assert ability.versions["AbilityTemplate"] == 42
    assert ability.effect_mappings[0].target_index == 0
    assert ability.to_dict()["_t"] == "Reckoning.Game.AbilityTemplate"


def test_graph_resolves_costs_targets_and_effect_actions():
    graph = ability_graph(RecordStore(), PLAY_CARD_ABILITY)
    assert graph is not None
    assert graph.costs.is_free
    assert graph.casting_behavior == "QuickAction"
    assert len(graph.targets) == 1
    assert graph.targets[0].name == "Play Card Target"
    assert [effect.operation for effect in graph.effects] == [
        "BuiltInPlayCard", "FinishResolvingCard"
    ]


def test_card_graph_follows_card_ability_references():
    graphs = card_ability_graphs(RecordStore(), CARD_CHOOSE_BLOOD)
    assert len(graphs) == 1
    assert graphs[0].name == "TheRememberedCardSThresholdsBecomeBlood"


def test_condition_keeps_nested_typed_objects():
    condition = RecordStore().get(
        "AbilityEffectConditionTemplate", CONDITION_100_DECK)
    assert condition is not None
    assert condition.name == "YouHave100OrMoreCardsInYourDeck"
    assert condition.condition.short_type == "RequiresCardsControlled"
    card_filter = condition.condition.field("m_CardFilter")
    assert card_filter.short_type == "AndCardFilter"


def test_scalar_text_is_not_parsed_as_json():
    line = json.dumps(json.dumps({
        "_t": "Reckoning.Game.AbilityTemplate",
        "m_AbilityTemplateId": {"m_Guid": PLAY_CARD_ABILITY},
        "m_Name": "[BASIC] Draw a card",
    }))
    record = deserialize_line(line)
    assert record.name == "[BASIC] Draw a card"


def test_play_plan_tracks_card_cost_and_separates_triggered_abilities():
    plan = PlayPlan.from_card(RecordStore(), CARD_CHOOSE_BLOOD)
    assert plan.card.name == "Choose Blood Transform"
    assert plan.cost.resource == 0
    assert len(plan.abilities) == 1
    assert not plan.abilities[0].is_triggered
    assert plan.required_prompts == ()
    assert plan.validate() == ()


def test_play_plan_binds_card_target_to_authored_index():
    plan = PlayPlan.from_card(
        RecordStore(), "16c354dd-50a7-45fb-b4e6-309d27cb6575")
    activations, costs = plan.activation_bundle([987654321])
    activation = activations[
        "ecd8264c-306a-1d07-f685-0c8b2ef3d3bf"]
    # Countermagic has an automatic source target at index 1 and its explicit
    # interrupt target at index 0.  The client selection must retain that
    # authored index rather than being collapsed to a generic target.
    assert costs == {}
    assert activation.target_map == {0: (987654321,)}


def test_play_plan_exposes_authored_card_cost_instance():
    plan = PlayPlan.from_card(
        RecordStore(), "e99983ef-f945-4fd9-9509-641b7dfb0d80")
    assert plan.cost_instances == ({
        "index": 0,
        "kind": "sacrifice",
        "target_guid": "8d8095b3-0d36-719e-4fa1-ab904fa3c3f0",
        "cost_type": 2,
        "minimum": 2,
        "maximum": 2,
        "auto": False,
    },)
    assert plan.validate() == (
        "missing additional-cost input at index 0",
        "card play is awaiting activation data",
    )
    assert plan.validate(cost_target_map={0: [11, 12]}) == ()
    assert "additional cost 0 needs at least 2 selections" in plan.validate(
        cost_target_map={0: [11]})


def test_activation_data_validates_client_style_prompts():
    source = deserialize({
        "_t": "Reckoning.Game.AbilityTemplate",
        "m_AbilityTemplateId": {"m_Guid": "11111111-1111-1111-1111-111111111111"},
    })
    target = TargetSpec(
        guid="22222222-2222-2222-2222-222222222222", name="a target",
        is_auto=False, is_random=False, player_filter="Any",
        collection_flags="Warzone", minimum=1, maximum=1,
        optional=False, explicit=True)
    graph = AbilityGraph(
        guid="11111111-1111-1111-1111-111111111111", name="test",
        game_text="", activation_game_text="", casting_behavior="QuickAction",
        manual=True, optional=False, trigger_event_type="",
        trigger_collection_flags="", trigger_condition=None,
        ability_condition=None, ability_free_condition=None,
        ignores_chain=False, recalculate_auto_targets=False,
        uses_previous_state=False, ability_index=-1,
        options=({"m_Name": "Choose"},),
        additional_cost_targets=(), costs=AbilityCost(variable_activation=1,
                                                       variable_minimum=2),
        targets=(target,), effects=(EffectSpec(
            guid="33333333-3333-3333-3333-333333333333",
            concrete_type="DamageAbilityEffectTemplate", operation="Damage",
            name="damage", target_index=0, effect_instance_id=0,
            effect_group_id=1, duration="Instant", condition_guid="",
            optional=False, recalculate_targets="UseDefault",
            secondary_target_index=-1, output_variables={}),),
        variables=(), source=source)
    instance = AbilityInstance.from_graph(graph)
    assert {prompt.kind for prompt in instance.required_prompts()} == {
        "target", "option", "x_cost"}
    assert not instance.is_complete
    complete = AbilityInstance.from_graph(
        graph, activation=ActivationData.from_values(
            target_map={"0": [99]}, option_map={"0": 1}, x_cost=2))
    assert complete.required_prompts() == ()
    assert complete.validate_activation() == ()


def test_runtime_instance_orders_effect_groups_and_instances():
    instance = AbilityInstance.from_runtime("44444444-4444-4444-4444-444444444444", [
        {"effect_group_id": 2, "effect_instance_id": 4, "effect_order": 0},
        {"effect_group_id": 1, "effect_instance_id": 9, "effect_order": 1},
        {"effect_group_id": 1, "effect_instance_id": 3, "effect_order": 2},
    ], 0)
    assert [effect["effect_instance_id"] for effect in instance.ordered_effects] == [3, 9, 4]
    assert [group for group, _effects in instance.effect_groups] == [1, 2]


def test_real_ability_option_shape_matches_client_option_map_contract():
    group = AbilityOptionGroup.from_value({
        "m_TargetProperty": "ResourceCost",
        "m_Label": "Choose a resource",
        "m_Options": [
            {"m_Value": 8, "m_Label": "[RUBY]"},
            {"m_Value": 16, "m_Label": "[SAPPHIRE]"},
        ],
    })
    assert group.options == (AbilityOptionEntry(8, "[RUBY]"),
                             AbilityOptionEntry(16, "[SAPPHIRE]"))
    assert group.is_valid(1)
    assert not group.is_valid(2)


def test_runtime_effect_adapter_preserves_records_wiring():
    graph = ability_graph(
        RecordStore(), "1026a613-0814-a633-0869-3d35aaa8dd72")
    effects = runtime_effects(graph)
    assert [effect["effect_group_id"] for effect in effects] == [1, 2, 3]
    assert effects[1]["condition_id"]
    assert effects[2]["contingent_effect_instance_id"] == 1


def test_ability_metadata_exposes_trigger_and_additional_costs():
    store = RecordStore()
    ability = store.get(
        "AbilityTemplate", "64fa780a-f241-d943-3e4b-e5debd8d233a")
    assert ability is not None
    assert ability.additional_cost_targets == (
        ("sacrifice", "fcf4ebdd-1e7a-ebd3-a058-101937be154f"),)
    triggered = store.get(
        "AbilityTemplate", "2d2687e4-a72b-4c34-9ef6-fce95c42ab17")
    assert triggered is not None
    assert triggered.is_triggered
    assert triggered.trigger_event_type.endswith("CardAttackedEvent")


def main():
    tests = [
        test_deserialize_preserves_polymorphism_and_fields,
        test_graph_resolves_costs_targets_and_effect_actions,
        test_card_graph_follows_card_ability_references,
        test_condition_keeps_nested_typed_objects,
        test_scalar_text_is_not_parsed_as_json,
        test_play_plan_tracks_card_cost_and_separates_triggered_abilities,
        test_play_plan_binds_card_target_to_authored_index,
        test_play_plan_exposes_authored_card_cost_instance,
        test_activation_data_validates_client_style_prompts,
        test_runtime_instance_orders_effect_groups_and_instances,
        test_real_ability_option_shape_matches_client_option_map_contract,
        test_runtime_effect_adapter_preserves_records_wiring,
        test_ability_metadata_exposes_trigger_and_additional_costs,
    ]
    for test in tests:
        test()
    print(f"PASS {len(tests)} gamedata model tests")


if __name__ == "__main__":
    main()
