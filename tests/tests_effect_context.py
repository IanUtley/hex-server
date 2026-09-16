"""Focused tests for the context-style effect and ability-builder adapters."""

import os
import sys
from types import SimpleNamespace
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


class _ConversationHandler:
    def queue(self, game, session, player_uid, ai_uid, bstate, conversation_id):
        bstate["queued_conversation"] = conversation_id
        bstate["resolution_paused"] = True
        return "queued"


def test_native_discard_creates_parent_continuation_for_hand_picker():
    """A native discard target must pause the same typed ability instance."""
    state = {"resolving_effect_order": 3, "resolving_ability": "parent"}

    class Handler:
        def _push_discard_prompt(self, *args, **kwargs):
            return "prompted"

    class Ability:
        instance_id = 7

        @staticmethod
        def continuation(**kwargs):
            return {
                "ability_guid": "parent", "source_uid": 101,
                "owner_id": 5, "target_map": {}, "variables": {}, **kwargs,
            }

    context = EffectContext.from_rules_port(
        SimpleNamespace(events=[]), _Session(), None, Handler(), 1, 2,
        state, "effect", ability=Ability())
    assert context.discard() == "prompted"
    assert state["pending_discard_continuation"]["instance_id"] == 7
    assert state["pending_discard_continuation"]["resume_effect_order"] == 4
    assert state["resolution_paused"] is True


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


def test_tunnel_moves_source_to_underground_and_emits_zone_triggers():
    class TunnelDB:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params):
            self.sql.append((sql, params))
            if sql.startswith("SELECT user_id, location"):
                return type("Cursor", (), {
                    "fetchone": lambda self: (5, "warzone")})()
            return type("Cursor", (), {
                "fetchone": lambda self: None})()

        def commit(self):
            pass

    db = TunnelDB()
    game = _Game()
    context = EffectContext.from_legacy(
        game, _Session(), db, object(), "player", "ai",
        # Legacy target aliases may be left behind by a nested trigger. A
        # source-bound Tunnel effect must continue to use the current typed
        # source instead of moving one of those stale cards.
        {"resolving_source_uid": 123, "resolving_target_uid": None,
         "player_spell_target": 999, "player_mod_target": 998},
        "effect", "")
    with mock.patch("abilities.framework.effects.utility._push_card_in_zone"), \
            mock.patch("abilities.framework.triggers.resolve_triggers") as triggers, \
            mock.patch(
                "abilities.framework.effects.tokens.activate_creation_replacements_for_card"
            ) as replacements:
        assert context.tunnel() == "tunneled 0x7b"
    # Reese's replacement is granted by Underground -> CastSpells, not by
    # entering Underground. It must remain inactive while the card is buried.
    replacements.assert_not_called()
    update = next(sql for sql, _params in db.sql
                  if sql.startswith("UPDATE game_cards"))
    assert "location='underground'" in update
    update_params = next(params for sql, params in db.sql
                         if sql.startswith("UPDATE game_cards"))
    assert int(update_params[-1]) == 123
    assert triggers.call_count == 2
    assert [call.args[7] for call in triggers.call_args_list] == [
        "CardExitedZoneEvent", "CardEnteredZoneEvent"]


def test_current_resource_modifier_persists_before_next_rules_port_cost():
    """A temporary resource grant must update all three state projections.

    Hideous Conversion grants current resource, not maximum resource.  The
    client receives the pool event immediately, so the checkpoint must be
    saved at the same effect boundary or the next RulesPort activation can
    incorrectly charge from the old pool.
    """
    from abilities.framework.bom import _apply_resource_property
    import battle_engine

    class Handler:
        user_profile = {"id": 5}

    game = _Game()
    game.player_uid = "player"
    game.ai_uid = "ai"
    session = _Session()
    state = {
        "player_resources": 1,
        "player_total_resources": 3,
        "ai_resources": 0,
        "ai_total_resources": 0,
    }
    with mock.patch.object(battle_engine, "save_state") as save_state:
        result = _apply_resource_property(
            game, session, _DB(), Handler(), "player", "ai", state,
            {"property": "currentresource", "amount": 1}, None)

    assert result == "player resources 1->2"
    assert state["player_resources"] == 2
    assert game.player_resources == 2
    assert game.events[-1].new_value == 2
    save_state.assert_called_once_with(session, state)


def test_tunneling_metadata_and_underground_visibility_are_data_driven():
    from abilities.framework.effects.counters import counter_is_secret, tunneling_value
    from domain.game import Game
    from domain.types import UID, SessionCardId
    import game_engine

    # Minion of Yazukan's Tunneling value is a CardTemplate TAC IntAttr, not
    # a name/text rule in the server.
    assert tunneling_value(
        None, "4cd28b0f-a50e-4bd4-b07d-cd2265e0a403") == 2
    assert tunneling_value(
        None, "4cd28b0f-a50e-4bd4-b07d-cd2265e0a403",
        {"Tunneling": 1}) == 1

    owner = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    card = SessionCardId(UID(0x6501))
    template = "4cd28b0f-a50e-4bd4-b07d-cd2265e0a403"
    counter = "def75520-0b8b-447f-8705-b34e71043890"
    assert counter_is_secret(counter) is False

    class Packet:
        def __init__(self):
            self.events = []

        def add_event(self, event):
            self.events.append(event)

    def packet_for(viewer):
        game = Game(1, owner, opponent)
        game.push_card_updated(
            card, owner, game_engine.ECardCollections.Underground,
            game_engine.ECardTypes.Troop, template_id=template,
            counters={counter: 1})
        with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
            return game.make_network_packet(viewer).events[0]

    own = packet_for(owner)
    other = packet_for(opponent)
    assert own.nulling is False
    assert other.nulling is True
    assert [str(value.guid) for value in other.counter_templates] == [counter]
    assert list(other.counter_counts) == [1]


def test_subterranean_spy_visibility_reveals_only_the_controller_view():
    from domain.game import Game
    from domain.types import UID, SessionCardId
    import game_engine

    owner = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    opponent_card = SessionCardId(UID(0x6601))

    class Packet:
        def __init__(self):
            self.events = []

        def add_event(self, event):
            self.events.append(event)

    game = Game(1, owner, opponent)
    game._visibility_by_uid[int(owner.uid64)] = {
        "CanSeeOpponentsHand": 1,
    }
    game.push_card_updated(
        opponent_card, opponent, game_engine.ECardCollections.Hand,
        game_engine.ECardTypes.Troop,
        template_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", nulling=True)
    with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
        owner_packet = game.make_network_packet(owner)
    assert owner_packet.events[0].nulling is False

    game = Game(1, owner, opponent)
    game._visibility_by_uid[int(owner.uid64)] = {
        "CanSeeOpponentsHand": 1,
    }
    game.push_card_updated(
        opponent_card, opponent, game_engine.ECardCollections.Hand,
        game_engine.ECardTypes.Troop,
        template_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", nulling=True)
    with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
        opponent_packet = game.make_network_packet(opponent)
    assert opponent_packet.events[0].nulling is True

    # A phase checkpoint can contain both the full Spy projection and the
    # normal opponent-hand placeholder.  The placeholder has an invalid
    # template id; if it is allowed to overwrite the full event, Unity renders
    # a visible but black rectangle.  Keep the complete definition as the
    # only event for this card/viewer.
    game = Game(1, owner, opponent)
    game._visibility_by_uid[int(owner.uid64)] = {
        "CanSeeOpponentsHand": 1,
    }
    game.push_card_updated(
        opponent_card, opponent, game_engine.ECardCollections.Hand,
        game_engine.ECardTypes.Troop,
        template_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", nulling=False)
    game.events[-1]._hand_reveal_viewer_uid = owner
    game.push_card_updated(
        opponent_card, opponent, game_engine.ECardCollections.Hand,
        game_engine.ECardTypes.Troop, nulling=True)
    with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
        projected_packet = game.make_network_packet(owner)
    assert len(projected_packet.events) == 1
    assert projected_packet.events[0].nulling is False
    assert str(projected_packet.events[0].card_id.guid) == \
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_tunneling_surface_queue_is_empty_without_underground_cards():
    from abilities.framework.effects.counters import (
        queue_tunneling_surfaces, surface_source_is_underground)

    class EmptyCursor:
        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class EmptyDB:
        def execute(self, _sql, _params):
            return EmptyCursor()

    class Session:
        session_id = 1

    class Handler:
        pass

    class Game:
        events = []

    bstate = {"stack": []}
    assert not surface_source_is_underground(EmptyDB(), Session(), 123)
    assert queue_tunneling_surfaces(
        EmptyDB(), Session(), Handler(), Game(), "player", "ai", bstate,
        1001) == []
    assert bstate["stack"] == []


def test_resource_choice_ability_is_detected_from_metadata():
    from abilities.framework.resources import printed_resource_choice_ability

    assert printed_resource_choice_ability([
        "8dcbdee1-e1de-1acd-5b17-e8beaec808b4",
        "43461713-afe7-299f-9aba-40002e9105eb",
    ]) == "8dcbdee1-e1de-1acd-5b17-e8beaec808b4"
    assert printed_resource_choice_ability([
        "43461713-afe7-299f-9aba-40002e9105eb",
    ]) is None


def test_match_secondary_reveal_uses_stored_target_owner():
    from abilities.framework.bom import _reveal_owner_for_target

    class OwnerCursor:
        def fetchone(self):
            return (1002,)

    class OwnerDB:
        def execute(self, _sql, _params):
            return OwnerCursor()

    target_champion = 0x6801
    battle_state = {
        "pvp": True,
        "stored_targets": {"withering-touch": [target_champion]},
    }
    owner = _reveal_owner_for_target(
        OwnerDB(), object(), _Session(), battle_state, 1001,
        "MatchSecondaryTargetTemplate", "withering-touch")
    assert owner == 1002


def test_player_updated_carries_subterranean_spy_hand_permission():
    from domain.game import Game
    from domain.types import UID, SessionCardId
    import game_engine

    owner = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    game = Game(1, owner, opponent)
    champion = SessionCardId(UID(0x6701))
    game.player_champion_card_id = champion
    game._visibility_by_uid[int(owner.uid64)] = {
        "CanSeeOpponentsHand": 1,
    }
    game.push_player_updated(owner, champ_id=champion)
    event = game.events[-1]
    assert event.can_see_enemy_hand is True


def test_effect_context_keeps_runtime_builder_out_of_battle_state():
    import json

    builder = object()
    state = {"turn_player": "player"}
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), object(), "player", "ai", state,
        "effect", "", ability=builder)
    assert context.ability is builder
    assert "_ability_builder" not in state
    assert json.dumps(state) == '{"turn_player": "player"}'


def test_resolver_passes_builder_ephemerally_when_effect_persists_state():
    """A leaf may persist state before resolution cleanup runs."""
    import json
    import abilities.framework.resolution as resolution

    effect_row = {
        "effect_guid": "runtime-effect",
        "effect_type": "RuntimeBuilderProbeEffect",
        "param": "",
        "target_index": -1,
        "effect_group_id": 0,
        "effect_order": 0,
        "effect_instance_id": 1,
        "condition_id": "",
        "contingent_effect_instance_id": -1,
        "secondary_target_index": -1,
    }
    seen = {}

    def probe(context):
        seen["builder"] = context.ability
        # Model draw/save_state: this occurs while the leaf is executing,
        # not after resolver cleanup.
        json.dumps(context.bstate)
        return "persisted"

    state = {}
    with mock.patch.object(resolution, "ability_graph", return_value=None), \
            mock.patch.object(resolution, "ability_variables", return_value={}), \
            mock.patch.object(resolution, "_target_template_ids", return_value=[]), \
            mock.patch.object(resolution, "_effect_list", return_value=[effect_row]), \
            mock.patch.object(resolution, "_target_template", return_value=None), \
            mock.patch.dict(resolution._LEAFS,
                            {"RuntimeBuilderProbeEffect": probe}):
        result = resolution.resolve_ability(
            object(), object(), _Session(), _DB(), "player", "ai", state,
            "runtime-ability", 123, 5, {})

    assert result == "persisted"
    assert seen["builder"].guid == "runtime-ability"
    assert "_ability_builder" not in state
    assert json.dumps(state)


def test_continuation_carries_only_serializable_activation_data():
    from abilities.framework.builder import AbilityContinuation

    pending = AbilityContinuation.from_state({
        "resolving_ability": "ABILITY-GUID",
        "resolving_source_uid": 123,
        "resolving_owner_id": 5,
        "resolving_effect_order": 4,
        "ability_target_map": {0: [456]},
        "ability_variables": {"Roll": 2},
    }).to_dict()
    assert pending == {
        "ability_guid": "ability-guid", "source_uid": 123,
        "owner_id": 5, "target_map": {"0": [456]},
        "variables": {"Roll": 2}, "resume_effect_order": 5,
    }
    import json
    assert json.loads(json.dumps(pending)) == pending


def test_context_activate_ability_uses_typed_child_resolver():
    import abilities.framework.resolution as resolution

    state = {
        "resolving_source_uid": 123,
        "resolving_owner_id": 5,
        "ability_variables": {"Roll": 2},
    }
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), object(), "player", "ai", state,
        "effect", "child-guid")
    with mock.patch.object(resolution, "resolve_ability", return_value="child") as run:
        assert context.activate_ability() == "child"
    assert run.call_args.args[7:10] == ("child-guid", 123, 5)
    assert run.call_args.kwargs["target_map"] == {}
    assert run.call_args.kwargs["variables"] == {"Roll": 2}


def test_ability_trace_records_authoritative_zone_and_event_deltas():
    from abilities.framework.trace import _CARD_COLUMNS, begin_effect, end_effect

    class TraceDB:
        def __init__(self):
            self.rows = {
                101: {"user_id": 5, "location": "deck", "position": 0},
                102: {"user_id": 5, "location": "deck", "position": 1},
            }

        def execute(self, _sql, _params):
            return type("Cursor", (), {
                "fetchall": lambda cursor: [
                    (uid,) + tuple(row.get(column) for column in _CARD_COLUMNS)
                    for uid, row in self.rows.items()],
            })()

    db = TraceDB()
    game = _Game()
    state = {"trace_ability_resolution": True}
    effect = {
        "effect_guid": "bury-effect",
        "effect_type": "BuryCardAbilityEffectTemplate",
        "effect_order": 2,
    }
    started = begin_effect(db, _Session(), game, state, effect, 123)
    db.rows[101]["location"] = "discard"
    db.rows[101]["card_cost_mod"] = -2
    db.rows[101]["card_attributes"] = 16
    state["player_health"] = 23
    state["player_charges"] = 4
    state["player_resources"] = 3
    state["player_total_resources"] = 5
    state["player_spell_points"] = 2
    state["player_threshold"] = {4: 2}
    game.events.append(type("CardMovedSessionEventArgs", (), {})())
    end_effect(db, _Session(), game, state, started, result="buried 1")

    trace = state["ability_trace"]
    assert trace[0]["effect_type"] == "BuryCardAbilityEffectTemplate"
    assert trace[0]["events"] == [{
        "type": "CardMovedSessionEventArgs", "fields": {},
    }]
    change = trace[0]["card_changes"]
    assert len(change) == 1 and change[0]["card_uid"] == 101
    assert change[0]["before"]["location"] == "deck"
    assert change[0]["after"]["location"] == "discard"
    assert change[0]["after"]["card_cost_mod"] == -2
    assert change[0]["after"]["card_attributes"] == 16
    assert trace[0]["champion_changes"] == {
        "player_charges": {"before": None, "after": 4},
        "player_health": {"before": None, "after": 23},
        "player_resources": {"before": None, "after": 3},
        "player_spell_points": {"before": None, "after": 2},
        "player_threshold": {"before": None, "after": {"4": 2}},
        "player_total_resources": {"before": None, "after": 5},
    }


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


def test_legacy_ability_facade_exports_turn_phase_trigger_entrypoint():
    import ability

    from abilities import resolve_turn_phase_triggers

    assert ability.resolve_turn_phase_triggers is resolve_turn_phase_triggers


def test_context_draw_preserves_owner_and_handler_boundary():
    handler = _DrawHandler()
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), handler, "player", "ai",
        {"resolving_target_uid": 123}, "draw-effect", "")

    assert context.draw(2) == "draw 2 for owner 5"
    assert len(handler.calls) == 2
    assert all(call[-1] == 5 for call in handler.calls)


def test_context_conversation_uses_typed_id_and_pauses_at_handler_boundary():
    handler = _ConversationHandler()
    context = EffectContext.from_legacy(
        _Game(), _Session(), _DB(), handler, "player", "ai",
        {"resolving_ability": "ability-guid",
         "resolving_source_uid": 123,
         "resolving_effect_order": 4},
        "conversation-effect", "")
    with mock.patch.object(
            handler, "_queue_conversation_prompt", handler.queue, create=True), \
         mock.patch.object(
             context, "template_value",
             return_value="11111111-2222-3333-4444-555555555555"):
        assert context.conversation() == "queued"
    assert context.bstate["queued_conversation"] == \
        "11111111-2222-3333-4444-555555555555"
    assert context.bstate["resolution_paused"] is True


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


def test_native_draw_routes_empty_deck_to_explicit_result_projection():
    from types import SimpleNamespace
    from rules_port.draw_effects import draw_cards

    session = SimpleNamespace(session_id=9)
    game = SimpleNamespace()
    seen = []
    context = SimpleNamespace(
        db=_DB(), session=session, game=game, handler=SimpleNamespace(
            _rules_port_deck_out=lambda *args: seen.append(args)),
        player_uid="player", ai_uid="ai", bstate={},
        target_owner=lambda default=None: 5,
        _emit_trigger=lambda *args, **kwargs: False,
    )
    with mock.patch("pvp_db.db_deck_top_card_details", return_value=None):
        assert draw_cards(context, 1, owner=5) == "draw 0 for owner 5"
    assert len(seen) == 1
    assert seen[0][0:2] == (game, session)
    assert seen[0][2] == 5


def test_context_randomize_variable_uses_typed_bounds():
    context = EffectContext.from_legacy(
        object(), _Session(), _DB(), object(), "player", "ai", {},
        "random-effect", "")
    with mock.patch.object(
            context, "template_value",
            side_effect=lambda name, default=None: {
                "m_VariableName": "RandomNumber",
            }.get(name, default)), mock.patch.object(
                context, "value",
                side_effect=lambda name, default=0: {
                    "m_MinValue": 2,
                    "m_MaxValue": 5,
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


def test_rules_port_counter_projection_reads_persisted_buffs():
    """``_project`` must read ``permanent_buffs`` through the typed accessor.

    ``db_card_source_info`` returns four columns; indexing ``row[4]`` raised
    IndexError during the Tunneling turn-start advance and closed the game
    connection ("stuck at end-phase").
    """
    from rules_port.counter_effects import _project

    class _Game:
        def __init__(self):
            self.updated = []
            self.counters = []

        def push_card_updated(self, *args, **kwargs):
            self.updated.append((args, kwargs))

        def push_card_counters_changed(self, *args, **kwargs):
            self.counters.append((args, kwargs))

    class _Handler:
        def _card_full_data(self, game, scid, template):
            return (template, "Troop", "Spy", 1, 1, 1, 0)

    game = _Game()
    context = SimpleNamespace(
        game=game, session=_Session(), db=_DB(), handler=_Handler(),
        player_uid="player", ai_uid="ai", bstate={})

    with mock.patch(
            "pvp_db.db_card_source_info",
            return_value=("tpl-guid", "Troop", "underground", 0)), \
            mock.patch(
                "pvp_db.db_card_mutation_field",
                return_value='{"counters": {"tunneling": 2}}'), \
            mock.patch(
                "pvp_db.db_counter_template_id",
                return_value="def75520-0b8b-447f-8705-b34e71043890"), \
            mock.patch("pvp_db.db_card_owner_id", return_value=0):
        _project(context, 1281, 0,
                 "def75520-0b8b-447f-8705-b34e71043890", 1, 2)

    assert game.counters, "counter projection must emit its change event"
    assert game.updated, "counter projection must emit a CardUpdated"


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


def test_context_typed_hero_health_and_spell_points_share_owner_projection():
    import game_engine

    game = _Game()
    bstate = {
        "resolving_owner_id": 5,
        "player_health": 20,
        "player_spell_points": 1,
    }
    context = EffectContext.from_legacy(
        game, _Session(), _DB(), object(), "player", "ai", bstate,
        "typed-modifier", "")

    assert context.set_hero_health(123, 25) == "set health 20->25"
    assert context.spell_points(123, 2) == "spell points 1->3"
    assert bstate["player_health"] == 25
    assert bstate["player_spell_points"] == 3
    assert isinstance(game.events[0],
                      game_engine.ChampionHealthChangedSessionEventArgs)
    assert isinstance(game.events[1],
                      game_engine.ChampionSpellPointsChangedSessionEventArgs)


def test_context_typed_card_threshold_and_subtype_preserve_metadata_values():
    context = EffectContext.from_legacy(
        _Game(), _Session(), _DB(), object(), "player", "ai", {},
        "typed-modifier", "")
    with mock.patch.object(context, "_card_thresholds", return_value=[4, 4]), \
            mock.patch.object(context, "_card_buffs", return_value={}), \
            mock.patch.object(context, "_save_card_buffs") as save, \
            mock.patch.object(context, "_push_modifier_card") as push:
        assert context.card_threshold(
            123, {"shard": "Ruby", "setthresholds": False}) == \
            "thresholds 0x7b -> [8, 8]"
        save.assert_called_once()
        push.assert_called_once_with(123, thresholds=[8, 8])

    with mock.patch.object(context, "_card_buffs",
                           return_value={"subtype": "Orc"}), \
            mock.patch.object(context, "_save_card_buffs") as save, \
            mock.patch.object(context, "_push_modifier_card") as push:
        assert context.subtype_modifier(
            123, {"subtype": "Robot", "operation": "Add"}) == \
            "subtype 0x7b -> Orc Robot"
        assert save.call_args.args[1]["subtype"] == "Orc Robot"
        push.assert_called_once_with(123, sub_type="Orc Robot")


def test_context_typed_damage_shield_uses_client_flags():
    context = EffectContext.from_legacy(
        _Game(), _Session(), _DB(), object(), "player", "ai", {},
        "typed-modifier", "")
    with mock.patch.object(context, "_card_buffs", return_value={}) as buffs, \
            mock.patch.object(context, "_save_card_buffs") as save, \
            mock.patch.object(context, "_push_modifier_card") as push:
        assert context.damage_shield(
            123, 3, {"onlycombatdamage": True, "oneshot": True,
                     "lastsindefinitely": False}) == \
            "damage shield 0x7b +3"
        entry = save.call_args.args[1]["damage_shields"][0]
        assert entry == {
            "amount": 3, "only_combat": True, "one_shot": True,
            "lasts_indefinitely": False,
        }
        buffs.assert_called_once_with(123, "temporary_buffs")
        push.assert_called_once_with(123, damage_shield=True)


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
        {}, "effect", "", ability=builder)
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
            "rules_port.targeting.legal_targets_for",
            return_value=[123]) as legal:
        assert builder.target_candidates(
            _DB(), 9, 1, builder.target(0), 55) == [123]
    legal.assert_called_once()


if __name__ == "__main__":
    test_context_decorator_adapts_legacy_leaf_arguments()
    test_tunnel_moves_source_to_underground_and_emits_zone_triggers()
    test_tunneling_metadata_and_underground_visibility_are_data_driven()
    test_tunneling_surface_queue_is_empty_without_underground_cards()
    test_resource_choice_ability_is_detected_from_metadata()
    test_subterranean_spy_visibility_reveals_only_the_controller_view()
    test_match_secondary_reveal_uses_stored_target_owner()
    test_player_updated_carries_subterranean_spy_hand_permission()
    test_all_registered_effects_use_the_context_adapter()
    test_context_draw_preserves_owner_and_handler_boundary()
    test_context_authored_events_use_typed_event_names()
    test_context_verdict_emits_shared_authored_event()
    test_context_draw_effect_uses_typed_or_fixture_count()
    test_native_draw_routes_empty_deck_to_explicit_result_projection()
    test_context_randomize_variable_uses_typed_bounds()
    test_context_stat_mod_uses_shared_operation_boundary()
    test_context_damage_modifier_resolves_typed_input_and_target()
    test_context_stat_modifier_resolves_typed_input_and_duration()
    test_context_counter_hides_card_persistence_and_projection()
    test_context_counter_uses_champion_state_and_event_projection()
    test_rules_port_counter_projection_reads_persisted_buffs()
    test_context_remove_from_combat_preserves_state_event_boundary()
    test_context_card_state_owns_projection_and_tap_trigger()
    test_context_lose_thresholds_preserves_state_and_event_operation()
    test_context_typed_hero_health_and_spell_points_share_owner_projection()
    test_context_typed_card_threshold_and_subtype_preserve_metadata_values()
    test_context_typed_damage_shield_uses_client_flags()
    test_builder_reuses_metadata_cost_target_filter_and_ordering()
    test_builder_exposes_typed_values_conditions_and_continuations()
    test_builder_target_and_cost_candidates_delegate_to_shared_legality()
    print("PASS effect context/builder tests")
