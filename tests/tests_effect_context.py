"""Focused tests for the context-style effect and ability-builder adapters."""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from abilities.framework.builder import AbilityBuilder
from abilities.framework.context import EffectContext
from abilities.framework.effects.registry import _LEAFS, effect
from gamedata.models import AbilityCost, TargetSpec
from gamedata.play_plan import (AbilityInstance, ActivationData, CardPlayCost,
                                PlayPlan)
from gamedata.semantics import AbilityGraph, EffectSpec


class _Cursor:
    def fetchone(self):
        return (5, 7)


class _DB:
    def execute(self, _sql, _params):
        return _Cursor()

    def commit(self):
        pass


class _Session:
    session_id = 9


class _DrawHandler:
    def __init__(self):
        self.calls = []

    def _player_draw_card(self, game, session, draw_uid, owner):
        self.calls.append((game, session, draw_uid, owner))


class _Game:
    def __init__(self):
        self.events = []

    def _push(self, event):
        self.events.append(event)


def test_context_decorator_adapts_legacy_leaf_arguments():
    seen = {}

    @effect("TestContextEffect")
    def capture(ctx: EffectContext):
        seen["target"] = ctx.target()
        seen["owner"] = ctx.target_owner()
        seen["value"] = ctx.value("m_InputValue", default=7)
        return "captured"

    result = _LEAFS["TestContextEffect"](
        object(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_target_uid": 123}, "missing-effect", "")

    assert result == "captured"
    assert seen == {"target": 123, "owner": 5, "value": 7}

    # Direct imports retain the historical ABI while the resolver uses the
    # same registered adapter.
    assert capture(
        object(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_target_uid": 123}, "missing-effect", "") == "captured"


def test_battle_state_persistence_omits_runtime_builder():
    import json

    from battle_engine import persistence_state, save_state

    builder = object()
    state = {"turn_player": "player", "_ability_builder": builder}
    persisted = persistence_state(state)
    assert persisted == {"turn_player": "player"}
    assert state["_ability_builder"] is builder


def test_extra_combat_entries_extend_the_current_turn_after_second_main():
    import battle_engine
    import game_engine

    state = {
        "player_has_ready_troop": False,
        "extra_combats_this_turn": {
            "player": [{"ready_your_troops": True}],
        },
    }
    phases = battle_engine.build_turn_phases(state)
    second_main = phases.index(game_engine.ETurnPhases.SecondMainPhase)
    assert phases[second_main + 1:second_main + 1 + len(
        battle_engine.COMBAT_STEPS)] == battle_engine.COMBAT_STEPS
    assert phases[second_main + 1 + len(battle_engine.COMBAT_STEPS)] == \
        game_engine.ETurnPhases.SecondMainPhase


def test_pvp_extra_combat_entries_use_the_active_player_key():
    from services.tournament_game import _pvp_turn_phase_list
    import battle_engine
    import game_engine

    phases = _pvp_turn_phase_list(
        {"extra_combats_this_turn": {"1001": [{}]}}, 1001, False)
    second_main = phases.index(game_engine.ETurnPhases.SecondMainPhase)
    assert phases[second_main + 1:second_main + 1 + len(
        battle_engine.COMBAT_STEPS)] == battle_engine.COMBAT_STEPS

    class Session:
        turn_order = None

        def _persist(self):
            json.dumps(self.turn_order)

    session = Session()
    save_state(session, state)
    assert session.turn_order is state
    assert state["_ability_builder"] is builder


def test_all_registered_effects_use_the_context_adapter():
    """Keep the nine-argument ABI behind one registry boundary."""
    assert _LEAFS
    assert all(hasattr(leaf, "__wrapped__") for leaf in _LEAFS.values())


def test_ability_resolution_entrypoint_accepts_context(monkeypatch):
    from abilities import resolve_ability_context

    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), object(), "player", "ai", {},
        "ability-guid", "")
    seen = {}

    def fake_resolve(handler, game, session, db, player_uid, ai_uid, bstate,
                     ability_guid, source_uid, owner_id, target_map,
                     variables=None, activation_data=None):
        seen.update({
            "handler": handler, "game": game, "ability_guid": ability_guid,
            "source_uid": source_uid, "owner_id": owner_id,
            "target_map": target_map,
        })
        return "resolved"

    monkeypatch.setattr("abilities.framework.resolution.resolve_ability",
                        fake_resolve)
    assert resolve_ability_context(
        context, source_uid=123, owner_id=5, target_map={0: 123}) == \
        "resolved"
    assert seen["ability_guid"] == "ability-guid"
    assert seen["source_uid"] == 123
    assert seen["owner_id"] == 5
    assert seen["target_map"] == {0: 123}


def test_legacy_ability_facade_exports_context_entrypoint():
    import ability

    from abilities import resolve_ability_context

    assert ability.resolve_ability_context is resolve_ability_context


def test_context_draw_preserves_owner_and_handler_boundary():
    handler = _DrawHandler()
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), handler, "player", "ai",
        {"resolving_target_uid": 123}, "draw-effect", "")

    assert context.draw(2) == "draw 2 for owner 5"
    assert len(handler.calls) == 2
    assert all(call[-1] == 5 for call in handler.calls)


def test_tac_decoder_preserves_append_to_list_metadata():
    from abilities.framework.tac import decode_tac_tree, tac_function, tac_string

    # Representative authored TAC from Records.  Keeping the fixture local
    # makes this test independent of the developer's runtime database.
    param = (
        "AgD231pIDEFwcGVuZFRvTGlzdKx3EcQURXh0cmFDb21iYXRzVGhpc1R1cm4d"
        "ssWzDVRoaXNUdXJuc0RhdGEG67dBAQAAAAAAAAA="
    )
    tree = decode_tac_tree(param)
    assert tac_function(param) == "AppendToList"
    assert tac_string(param, "ListName") == "ExtraCombatsThisTurn"
    assert tac_string(param, "Where") == "ThisTurnsData"
    assert tree


def test_context_authored_events_use_typed_event_names():
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_source_uid": 123, "resolving_owner_id": 5},
        "fire-effect", "")
    with mock.patch.object(
            context, "template_value",
            side_effect=lambda name, default=None: {
                "m_TriggerType": "Game.Shared.Mechanics.FateweavedEvent",
                "m_Name": "fireFateweavedEvent",
            }.get(name, default)), mock.patch(
                "abilities.framework.triggers.resolve_triggers",
                return_value="triggered") as resolve:
        assert context.fire_event() == "fired FateweavedEvent: triggered"
    assert resolve.call_args.args[7:9] == ("FateweavedEvent", 123)
    assert resolve.call_args.kwargs["source_owner_uid"] == 5


def test_context_verdict_emits_shared_authored_event():
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_source_uid": 123, "resolving_owner_id": 5},
        "verdict-effect", "")
    with mock.patch(
            "abilities.framework.triggers.resolve_triggers",
            return_value="triggered") as resolve:
        assert context.verdict() == "fired VerdictEvent: triggered"
    assert resolve.call_args.args[7:9] == ("VerdictEvent", 123)
    assert resolve.call_args.kwargs["source_owner_uid"] == 5


def test_context_draw_effect_uses_typed_or_fixture_count():
    handler = _DrawHandler()
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), handler, "player", "ai",
        {"resolving_target_uid": 123}, "missing-draw-effect",
        '{"count": 2}')

    assert context.draw_effect() == "draw 2 for owner 5"
    assert len(handler.calls) == 2


def test_context_randomize_variable_uses_typed_bounds():
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), object(), "player", "ai", {},
        "random-effect", "")
    with mock.patch.object(
            context, "template_value",
            side_effect=lambda name, default=None: {
                "m_VariableName": "RandomNumber",
                "m_MinValue": 2,
                "m_MaxValue": 5,
                "m_MaxValueField": None,
            }.get(name, default)), mock.patch(
                "random.randint", return_value=4) as randint:
        assert context.randomize_variable() == "randomized RandomNumber=4"
    assert context.bstate["ability_variables"]["RandomNumber"] == 4
    randint.assert_called_once_with(2, 5)


def test_context_stat_mod_uses_shared_operation_boundary():
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), object(), "player", "ai", {},
        "stat-effect", "")
    with mock.patch(
            "abilities.framework.stat_mod.apply_card_stat_mod",
            return_value=4) as apply:
        assert context.stat_mod(123, 2, 3, this_turn=True) == 4
    assert apply.call_args.args[6:9] == (123, 2, 3)
    assert apply.call_args.kwargs == {"this_turn": True, "bstate": {}}


def test_context_damage_modifier_resolves_typed_input_and_target():
    context = EffectContext.from_legacy(
        _Game(), _Session(), _DB(), object(), "player", "ai",
        {
            "resolving_ability":
            "af816e98-51dd-9029-2e0a-5f3be27f1689",
            "resolving_target_uid": 123,
            "resolving_owner_id": 1,
        },
        "796ff24c-d468-68c1-8636-82d26a149c8b", "")
    with mock.patch.object(context, "damage", return_value="damaged") as damage:
        assert context.damage_modifier(
            {"text": "Deal 2 damage", "amount": 0},
            {"input_variable": "2"}) == "damaged"
    damage.assert_called_once_with(123, 2)


def test_context_stat_modifier_resolves_typed_input_and_duration():
    context = EffectContext.from_legacy(
        _Game(), _Session(), _DB(), object(), "player", "ai",
        {
            "resolving_ability":
            "af816e98-51dd-9029-2e0a-5f3be27f1689",
            "resolving_target_uid": 123,
            "resolving_owner_id": 1,
        },
        "796ff24c-d468-68c1-8636-82d26a149c8b", "")
    with mock.patch.object(context, "stat_mod", return_value=4) as stat_mod:
        assert context.stat_modifier(
            {"property": "attack", "amount": 0,
             "input_variable": "2", "duration": "EndOfTurn"},
            {"property": "attack", "input_variable": "2"}) == \
            "mod 0x7b +2/+0"
    stat_mod.assert_called_once_with(123, 2, 0, this_turn=True)


def test_context_counter_hides_card_persistence_and_projection():
    context = EffectContext.from_legacy(
        _Game(), _Session(), _DB(), object(), "player", "ai", {},
        "counter-effect", "")
    with mock.patch(
            "abilities.framework.effects.counters.is_champion_target",
            return_value=False), mock.patch(
                "abilities.framework.effects.counters.card_counters",
                return_value={"roar": 3}), mock.patch(
                    "abilities.framework.effects.counters.remove_card_counters"
                ) as remove, mock.patch(
                    "abilities.framework.effects.counters.add_card_counter",
                    return_value=1) as add, mock.patch(
                        "abilities.framework.effects.counters.push_card_counters"
                    ) as push:
        assert context.counter(123, "roar", amount=2,
                               operation="remove") == (3, 1)
    remove.assert_called_once()
    add.assert_called_once()
    push.assert_called_once()


def test_context_counter_uses_champion_state_and_event_projection():
    context = EffectContext.from_legacy(
        _Game(), _Session(), _DB(), object(), "player", "ai", {},
        "counter-effect", "")
    with mock.patch(
            "abilities.framework.effects.counters.is_champion_target",
            return_value=True), mock.patch(
                "abilities.framework.effects.counters.change_champion_counter",
                return_value=(1, 4)) as change, mock.patch(
                    "abilities.framework.effects.counters.push_champion_counter"
                ) as push:
        assert context.counter(123, "roar", "counter-guid", 3) == (1, 4)
    change.assert_called_once()
    push.assert_called_once()


def test_context_remove_from_combat_preserves_state_event_boundary():
    context = EffectContext.from_legacy(
        _Game(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_target_uid": 123}, "combat-effect", "")
    with mock.patch("abilities.framework.bom._push_card_state") as push:
        assert context.remove_from_combat() == "removed 0x7b from combat"
    push.assert_called_once()


def test_context_card_state_owns_projection_and_tap_trigger():
    context = EffectContext.from_legacy(
        _Game(), _Session(), _DB(), object(), "player", "ai", {},
        "tap-effect", "")
    with mock.patch("abilities.framework.bom._push_card_state") as push, \
            mock.patch(
                "abilities.framework.triggers.resolve_triggers",
                return_value=False) as resolve:
        assert context.update_card_state(
            123, add=1, trigger="CardTappedEvent") == 5
    push.assert_called_once()
    assert resolve.call_args.args[7:9] == ("CardTappedEvent", 123)


def test_context_lose_thresholds_preserves_state_and_event_operation():
    game = _Game()
    bstate = {"resolving_owner_id": 5, "player_threshold": {4: 2}}
    context = EffectContext.from_legacy(
        game, _Session(), _DB(), object(), "player", "ai", bstate,
        "threshold-effect", "")

    assert context.lose_thresholds(["Blood"]) == "lost 2 threshold(s)"
    assert bstate["player_threshold"][4] == 0
    assert len(game.events) == 1
    assert game.events[0].operation == 2
    assert game.events[0].delta == 2


def test_builder_reuses_metadata_cost_target_filter_and_ordering():
    first = EffectSpec(
        guid="effect-2", concrete_type="DrawNCardsAbilityEffectTemplate",
        operation="DrawNCards", name="draw", target_index=0,
        effect_instance_id=2, effect_group_id=2, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={})
    second = EffectSpec(
        guid="effect-1", concrete_type="CardModifierAbilityEffectTemplate",
        operation="CardModifier", name="modify", target_index=0,
        effect_instance_id=1, effect_group_id=1, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={})
    graph = AbilityGraph(
        guid="ability-guid", name="test", game_text="", activation_game_text="",
        casting_behavior="QuickAction", manual=True, optional=False,
        trigger_event_type="", trigger_collection_flags="", trigger_condition=None,
        ability_condition=None, ability_free_condition=None, ignores_chain=False,
        recalculate_auto_targets=False, uses_previous_state=False, ability_index=-1,
        options=(), additional_cost_targets=(),
        costs=AbilityCost(activation=2),
        targets=(TargetSpec(
            guid="target-guid", name="target", is_auto=False, is_random=False,
            player_filter="Opposing", collection_flags="Warzone", minimum=1,
            maximum=1, optional=False, explicit=True,
            card_filter={"m_CardType": "Troop"}),),
        effects=(first, second), variables=(), source=None)
    instance = AbilityInstance.from_graph(graph)
    plan = PlayPlan(
        card=object(),
        cost=CardPlayCost(resource=3, variable=False, variable_minimum=0,
                          life=0, threshold=None),
        abilities=(instance,))
    builder = AbilityBuilder.from_plan(plan, "ability-guid")

    assert builder.costs.activation == 2
    assert builder.card_costs.resource == 3
    assert builder.card_cost_instances == ()
    assert builder.filter_target(0) == {"m_CardType": "Troop"}
    assert builder.filter_target("primary") == {"m_CardType": "Troop"}
    assert [effect["effect_instance_id"] for effect in builder.effects] == [1, 2]
    assert builder.effect(0).type_name == "CardModifierAbilityEffectTemplate"
    assert builder.effect(0).condition_guid == ""
    assert [group for group, _ in builder.effect_groups] == [1, 2]
    assert builder.conditions == {"ability": None, "trigger": None, "free": None}
    assert builder.validate().count("missing target input at index 0") == 1
    bound = builder.bind(ActivationData.from_values(target_map={0: [123]}))
    assert bound.validate() == ()
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), object(), "player", "ai",
        {"_ability_builder": builder}, "effect", "")
    assert context.ability is builder


def test_builder_exposes_typed_values_conditions_and_continuations():
    first = EffectSpec(
        guid="effect-1", concrete_type="DrawNCardsAbilityEffectTemplate",
        operation="DrawNCards", name="draw", target_index=0,
        effect_instance_id=1, effect_group_id=1, duration="Instant",
        condition_guid="condition-1", optional=False,
        recalculate_targets="UseDefault", secondary_target_index=-1,
        output_variables={})
    second = EffectSpec(
        guid="effect-2", concrete_type="ActivateAbilityEffectTemplate",
        operation="ActivateAbility", name="continue", target_index=-1,
        effect_instance_id=2, effect_group_id=2, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={})
    graph = AbilityGraph(
        guid="ability-guid", name="test", game_text="", activation_game_text="",
        casting_behavior="QuickAction", manual=True, optional=False,
        trigger_event_type="", trigger_collection_flags="", trigger_condition=None,
        ability_condition=None, ability_free_condition=None, ignores_chain=False,
        recalculate_auto_targets=False, uses_previous_state=False, ability_index=-1,
        options=(), additional_cost_targets=(), costs=AbilityCost(activation=0),
        targets=(), effects=(first, second), variables=(), source=None)
    builder = AbilityBuilder.from_graph(graph)

    assert builder.effect_conditions == {0: "condition-1"}
    assert [ref.type_name for ref in builder.continuations] == [
        "ActivateAbilityEffectTemplate"]
    with mock.patch(
            "abilities.framework.fields.effect_field", return_value=6) as field:
        assert builder.value(_DB(), {"session_id": 9}, "m_InputValue",
                             effect=builder.effect(0)) == 6
    field.assert_called_once()


def test_builder_centralizes_card_cost_candidates():
    first = EffectSpec(
        guid="effect-1", concrete_type="DrawNCardsAbilityEffectTemplate",
        operation="DrawNCards", name="draw", target_index=-1,
        effect_instance_id=1, effect_group_id=1, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={})
    graph = AbilityGraph(
        guid="ability-guid", name="test", game_text="", activation_game_text="",
        casting_behavior="QuickAction", manual=True, optional=False,
        trigger_event_type="", trigger_collection_flags="", trigger_condition=None,
        ability_condition=None, ability_free_condition=None, ignores_chain=False,
        recalculate_auto_targets=False, uses_previous_state=False, ability_index=-1,
        options=(), additional_cost_targets=(),
        costs=AbilityCost(activation=0), targets=(),
        effects=(first,), variables=(), source=None)
    plan = PlayPlan(
        card=object(),
        cost=CardPlayCost(
            resource=0, variable=False, variable_minimum=0, life=0,
            threshold=None,
            additional_cost_targets=(("sacrifice", "cost-guid"),)),
        abilities=(AbilityInstance.from_graph(graph),))
    builder = AbilityBuilder.from_play_plan(plan)

    with mock.patch(
            "abilities.framework.targeting.legal_targets_for",
            return_value=[44, 45]) as legal:
        result = builder.card_cost_candidates(
            _DB(), 9, 1, source_uid=77, battle_state={})

    assert result[0][0]["target_guid"] == "cost-guid"
    assert result[0][1] == (44, 45)
    assert legal.call_args.kwargs == {
        "both_players": False, "champions": None, "battle_state": {}}


def test_builder_target_and_cost_candidates_delegate_to_shared_legality():
    first = EffectSpec(
        guid="effect", concrete_type="DrawNCardsAbilityEffectTemplate",
        operation="DrawNCards", name="draw", target_index=0,
        effect_instance_id=1, effect_group_id=1, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={})
    graph = AbilityGraph(
        guid="ability-guid", name="test", game_text="", activation_game_text="",
        casting_behavior="QuickAction", manual=True, optional=False,
        trigger_event_type="", trigger_collection_flags="", trigger_condition=None,
        ability_condition=None, ability_free_condition=None, ignores_chain=False,
        recalculate_auto_targets=False, uses_previous_state=False, ability_index=-1,
        options=(), additional_cost_targets=(),
        costs=AbilityCost(activation=0),
        targets=(TargetSpec(
            guid="target-guid", name="target", is_auto=False, is_random=False,
            player_filter="Opposing", collection_flags="Warzone", minimum=1,
            maximum=1, optional=False, explicit=True,
            card_filter={"m_CardType": "Troop"}),),
        effects=(first,), variables=(), source=None)
    builder = AbilityBuilder.from_graph(graph)
    with mock.patch(
            "abilities.framework.targeting.legal_targets_for",
            return_value=[123]) as legal:
        assert builder.target_candidates(
            _DB(), 9, 1, builder.target(0), 55) == [123]
    legal.assert_called_once()


if __name__ == "__main__":
    test_context_decorator_adapts_legacy_leaf_arguments()
    test_all_registered_effects_use_the_context_adapter()
    test_context_draw_preserves_owner_and_handler_boundary()
    test_context_authored_events_use_typed_event_names()
    test_context_verdict_emits_shared_authored_event()
    test_context_draw_effect_uses_typed_or_fixture_count()
    test_context_randomize_variable_uses_typed_bounds()
    test_context_stat_mod_uses_shared_operation_boundary()
    test_context_damage_modifier_resolves_typed_input_and_target()
    test_context_stat_modifier_resolves_typed_input_and_duration()
    test_context_counter_hides_card_persistence_and_projection()
    test_context_counter_uses_champion_state_and_event_projection()
    test_context_remove_from_combat_preserves_state_event_boundary()
    test_context_card_state_owns_projection_and_tap_trigger()
    test_context_lose_thresholds_preserves_state_and_event_operation()
    test_builder_reuses_metadata_cost_target_filter_and_ordering()
    test_builder_exposes_typed_values_conditions_and_continuations()
    test_builder_target_and_cost_candidates_delegate_to_shared_legality()
    print("PASS effect context/builder tests")
