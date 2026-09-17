"""Parity checks for the first direct ``HexClient/Game.Shared`` Python port."""

import os
import sys
import asyncio
import json
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rules_port import (AbilityRegistry, Chain, GameAction, GameActionResult, GameActionStack,
                        MultiplyWithCarryRng, PriorityWindowAction,
                        AuthoritativeSession, GameEngineEventSink, RulesTransaction,
                        TurnPhasePlayers, require_legal_transition,
                        transition_is_legal, AndRequirement,
                        PlayerHasPriorityRequirement, PlayerIsActiveRequirement,
                        CardCanBePlayedRequirement, CardInCollectionRequirement,
                        CardTypeRequirement, CardTappedRequirement,
                        CardCanUntapRequirement, CardCastingCostRequirement,
                        CardResourceThresholdRequirement,
                        AbilityCanBeActivatedRequirement,
                        AttackExistsRequirement, AllDamageAssignedRequirement,
                        AttackerIsValidRequirement,
                        DefenseDeclarationsLegalRequirement,
                        MainPhaseRequirement, PriorityWindowRequirement,
                        PlayerIsNotEliminatedRequirement, XCostRequirement,
                        AbilityFactory, SQLiteRulesSnapshot, ParityCapture,
                        compare_captures, event_record, replay_transactions,
                        MetadataResolutionAdapter, enable_rules_port)
from rules_port import (plan_ability_cost, apply_ability_cost_plan,
                        card_cost_targets)
from rules_port import play_resource
from rules_port.resources import (pay_resource_for_player,
                                  pay_charge_for_player,
                                  pay_spell_points_for_player,
                                  play_resource_for_player, pay_resource,
                                  pay_counter, begin_turn_resources,
                                  begin_turn_resources_for_player)
from rules_port import CardTransactionExecutor, MetadataCardTransactionExecutor
from rules_port.wire import (extract_session_card_uids,
                             normalize_player_transaction,
                             submit_classified_transaction)
from rules_port.runtime_adapter import (PvpRuntimeFacts, RuntimeCard,
                                         SQLiteCardMutationAdapter)
from rules_port.actions import (AbilityResolutionState, PushOntoChainAction,
                                ResolveTopOfChainAction)
from rules_port.triggers import MetadataTriggerAdapter, TriggerEvent
from rules_port.targets import MetadataTargetAdapter, TargetSelection
from rules_port.async_bridge import (AsyncActivationPublisher,
                                     AsyncEventPublisher,
                                     AsyncRulesCoordinator, AsyncUIEventBus)
from rules_port.adapter import (_native_participant_ids, rules_session_for,
                                session_from_persisted_game)
from rules_port.pvp_session import PvpAuthoritativeSession
from rules_port.combat import (Combat, CombatId, CombatManager, CombatPhase,
                               CombatResolver)
from rules_port.combat import CombatDamageBackend
from rules_port.combat_damage import _Combatant
from rules_port.conditions import evaluate_condition
from rules_port.persistence import (current_phase as native_current_phase,
                                    load_state as native_load_state,
                                    persistence_state)
from rules_port.lifecycle import (complete_turn, practice_phase_priority,
                                  practice_priority_players, should_draw_for_turn)
from rules_port.cast_stats import record_card_cast
from rules_port import pvp_lifecycle
import game_engine
from domain.constants import AI_UID_TYPE, PLAYER_UID_TYPE
from domain.events import PlayerWishesToDrawFirstSessionEventArgs
from gamedata.play_plan import AbilityInstance as MetadataAbilityInstance


class RecordingAction(GameAction):
    def __init__(self, result, events, name):
        super().__init__()
        self.result = result
        self.events = events
        self.name = name

    def on_enter(self):
        self.events.append(f"enter:{self.name}")

    def on_exit(self):
        self.events.append(f"exit:{self.name}")

    def update(self):
        self.events.append(f"update:{self.name}")
        return self.result


class SessionStub:
    active_player_id = "a"
    current_turn_phase = "FirstMainPhase"

    def __init__(self):
        self.events = []
        self.top = None

    def handle_game_event(self):
        self.events.append("handle")

    def chain_top(self):
        return self.top

    def player_ids_in_turn_order(self):
        return ("a", "b")

    def player_ids_in_priority_order(self):
        return ("b", "a")

    def defending_player_ids(self):
        return ("b",)

    def can_player_pass_priority(self, player_id):
        return player_id in {"a", "b"}

    def hand_larger_than_maximum(self, player_id):
        return False

    def send_turn_phase_update(self):
        self.events.append("phase-update")


class AbilityStub:
    def __init__(self, instance_id):
        self.instance_id = instance_id


class PersistedSessionStub:
    def __init__(self):
        self.turn_order = {"legacy_phase_idx": 4}
        self.persisted = 0

    def _persist(self, conn=None):
        self.persisted += 1


class CombatCardStub:
    def __init__(self, uid, attack, *, crush=False):
        self.session_card_id = uid
        self.attack = attack
        self.crush = crush
        self.in_warzone = True
        self.is_troop = True


class PromptAbilityStub(AbilityStub):
    def __init__(self, instance_id, responsible_player_id=None):
        super().__init__(instance_id)
        self.responsible_player_id = responsible_player_id
        self.activation_data = None

    def needs_activation_data(self):
        return (() if self.activation_data is not None
                else ({"kind": "target", "minimum": 1},))

    def continuation(self):
        return {"ability_instance_id": self.instance_id, "target_map": {}}

    def bind_activation(self, activation_data):
        self.activation_data = dict(activation_data)
        return True


class RequirementSessionStub:
    current_turn_phase = "FirstMainPhase"
    eliminated_player_ids = {"eliminated"}

    def __init__(self):
        self.cards = {
            9: {"collection": 2, "card_type": 4, "tapped": True,
                "can_ready": True, "casting_cost": 3,
                "thresholds": ({"color": "ruby", "amount": 2},)},
        }
        self.players = {"p": {"current_resource_pool": 5,
                              "resource_thresholds": {"ruby": 2}}}

    def get_card(self, card_id):
        return self.cards.get(card_id)

    def get_player(self, player_id):
        return self.players.get(player_id)

    def can_play_card(self, card, player_id, for_free):
        return player_id == "p" and card is self.cards[9] and not for_free

    def can_activate_ability(self, card, player_id, ability_id):
        return card is self.cards[9] and player_id == "p" and ability_id == "ability"

    def validate_x_cost(self, player_id, activation_data):
        return player_id == "p" and activation_data.get("x_cost") == 2


def test_multiply_with_carry_matches_client_algorithm():
    rng = MultiplyWithCarryRng(12345, 67890)
    assert [rng.next() for _ in range(5)] == [1466010529, 33336022,
                                               165350713, 1005774524,
                                               1003587169]
    rng.set_seed(12345, 67890)
    assert rng.next(7) == 4
    assert rng.next(10, 20) == 12
    assert rng.next_range(4, 4) == 0


def test_action_stack_enters_updates_and_exits_before_next_action():
    session = SessionStub()
    stack = GameActionStack(session)
    events = []
    first = RecordingAction(GameActionResult.COMPLETE, events, "first")
    second = RecordingAction(GameActionResult.WAITING_FOR_INPUT, events, "second")
    stack.push(first)
    stack.push(second)
    assert first.was_interrupted
    assert stack.update() is False
    assert events == ["enter:second", "update:second"]
    second.result = GameActionResult.COMPLETE
    assert stack.update() is True
    assert events[-2:] == ["update:second", "exit:second"]
    assert stack.update() is True
    assert events[-2:] == ["update:first", "exit:first"]


def test_priority_window_is_apnap_and_requires_current_player():
    session = SessionStub()
    stack = GameActionStack(session)
    window = PriorityWindowAction(TurnPhasePlayers.ALL)
    stack.push(window)
    assert window.priority_player_id == "a"
    assert stack.priority_player_id == "a"
    assert window.pass_priority("b") is False
    assert window.pass_priority("a") is True
    assert window.priority_player_id == "b"
    assert window.pass_priority("b") is True
    assert window.priority_player_id is None
    assert session.events == ["phase-update", "phase-update"]


def test_practice_follow_up_auto_pass_only_applies_to_ability_chain():
    """AssignDamage's AI phase window must reach the AI combat driver."""
    from hconnect_server import _is_practice_chain_follow_up

    session = SessionStub()
    stack = GameActionStack(session)
    phase_window = PriorityWindowAction(TurnPhasePlayers.ALL)
    stack.push(phase_window)
    assert phase_window.pass_priority("a")
    assert phase_window.priority_player_id == "b"
    assert not _is_practice_chain_follow_up(phase_window, "a", True)

    phase_window.ability_responding_to = object()
    assert _is_practice_chain_follow_up(phase_window, "a", True)


def test_native_ai_attacker_declaration_is_idempotent_after_phase_entry():
    """The AI loop must not erase attackers tapped by native declaration."""
    import ai

    declarations = {"17153": "257", "17665": "257"}
    state = {"ai_attackers": declarations.copy()}
    port = SimpleNamespace(
        combat_manager=SimpleNamespace(combats=[object(), object()]))
    session = SimpleNamespace(_rules_port_session=port)

    result = ai.ai_declare_attackers(
        SimpleNamespace(), SimpleNamespace(), session,
        "ai", "player", state)

    assert result is state
    assert state["ai_attackers"] == declarations


def test_interrupted_priority_window_restarts_with_active_player():
    session = SessionStub()
    stack = GameActionStack(session)
    window = PriorityWindowAction(TurnPhasePlayers.ALL)
    stack.push(window)
    assert window.pass_priority("a")
    window.on_interrupted()
    window.on_enter()
    assert window.priority_player_id == "a"
    assert session.events[-1] == "phase-update"


def test_chain_uses_ability_instance_ids_and_only_pops_the_top():
    registry = AbilityRegistry()
    chain = Chain(registry)
    first, second = AbilityStub(7), AbilityStub(9)
    chain.push_ability(first)
    chain.push_ability(second)
    assert chain.count == 2
    assert chain.peek_ability() is second
    assert chain.peek_ability(7) is None
    assert chain.pop_ability(7) is None
    assert chain.pop_ability(9) is second
    assert chain.peek_ability() is first
    assert chain.remove_ability(7) is first
    assert chain.is_empty


def test_ability_registry_distinguishes_pending_and_active_chain_abilities():
    registry = AbilityRegistry()
    ability = AbilityStub(7)
    registry.add_chain(7, ability)
    assert registry.is_chain_ability(7)
    assert registry.activate_chain(7) is ability
    assert not registry.is_chain_ability(7)


def test_client_card_and_ability_requirements_delegate_to_authoritative_facts():
    session = RequirementSessionStub()
    assert CardCanBePlayedRequirement(9).is_valid(session, "p")
    assert CardInCollectionRequirement("p", 2, 9).is_valid(session, "p")
    assert CardTypeRequirement(9, 4).is_valid(session, "p")
    assert CardTappedRequirement(9).is_valid(session, "p")
    assert CardCanUntapRequirement(9).is_valid(session, "p")
    assert CardCastingCostRequirement("p", 9, 2).is_valid(session, "p")
    assert CardResourceThresholdRequirement("p", 9).is_valid(session, "p")
    assert AbilityCanBeActivatedRequirement(9, "ability").is_valid(session, "p")
    assert XCostRequirement({"opted": True, "x_cost": 2}).is_valid(session, "p")
    assert MainPhaseRequirement().is_valid(session, "p")
    assert not PriorityWindowRequirement().is_valid(session, "p")
    assert PlayerIsNotEliminatedRequirement("p").is_valid(session, "p")
    assert not PlayerIsNotEliminatedRequirement("eliminated").is_valid(session, "eliminated")


def test_pvp_runtime_facts_reads_existing_card_projection_without_rules_sql():
    row = (9, "template", 1, "Troop", 1, '["ability"]', 0)
    def cards_at_location(session_id, location):
        assert session_id == 55
        return [row] if location == "hand" else []
    api = SimpleNamespace(
        db_game_cards_at_location=cards_at_location,
        db_card_template_field=lambda *_: 3,
        db_card_template_thresholds=lambda *_: (
            '[{"color": "ruby", "quantity": 2}]', "[]", 0),
        db_card_ability_list=lambda *_: ["ability"],
    )
    facts = PvpRuntimeFacts(55, {"player_resources": 3,
                                 "player_threshold": {"ruby": 2}},
                            player_uid=1, ai_uid=0, pvp_api=api)
    card = facts.get_card(9)
    assert card is not None
    assert card.location == "hand"
    assert card.is_tapped()
    assert facts.can_play_card(card, 1)
    assert facts.can_activate_ability(card, 1, "ability")
    port = AuthoritativeSession(55, (1,), seed_z=1, seed_w=2)
    port.set_runtime_facts(facts)
    assert CardCanBePlayedRequirement(9).is_valid(port, 1)


def test_pvp_runtime_facts_uses_effective_instance_cost_for_legality():
    """A cost reduction must affect RulesPort validation, not just payment."""
    row = (17, "template", 1, "Troop", 0, "[]", 0)

    def cards_at_location(session_id, location):
        return [row] if location == "hand" else []

    api = SimpleNamespace(
        db_game_cards_at_location=cards_at_location,
        db_card_template_field=lambda *_: 2,  # printed Wretched Brood cost
        db_game_card_effective_cost=lambda *_: 0,  # Infernal Professor -2
        db_card_template_thresholds=lambda *_: ("[]", "[]", 0),
        db_card_ability_list=lambda *_: [],
    )
    facts = PvpRuntimeFacts(55, {"player_resources": 0,
                                 "player_threshold": {}},
                            player_uid=1, ai_uid=0, pvp_api=api)
    card = facts.get_card(17)
    assert card is not None
    assert card.casting_cost == 0
    assert facts.can_play_card(card, 1)


def test_phase_graph_matches_client_shortcuts_and_rejects_impossible_jumps():
    # Prep may skip Draw, combat may be skipped, but main phase cannot jump
    # directly to damage.  These are exact C# StateMachine permits.
    assert transition_is_legal("Prep", "FirstMainPhase")
    assert transition_is_legal("FirstMainPhase", "SecondMainPhase")
    assert transition_is_legal("DeclareCombatPriorityWindow", "AssignDamage")
    assert not transition_is_legal("FirstMainPhase", "AssignDamage")
    try:
        require_legal_transition("Draw", "EndPhase")
    except ValueError as exc:
        assert "Draw -> EndPhase" in str(exc)
    else:
        raise AssertionError("illegal phase transition was accepted")


def test_authoritative_session_validates_intent_and_emits_existing_events():
    player = game_engine.UID.make(244, 1)
    ai = game_engine.UID.make(3, 2)
    game = game_engine.Game(55, player, ai)
    session = AuthoritativeSession(55, (player, ai), seed_z=1, seed_w=2,
                                   event_sink=GameEngineEventSink(game))
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.action_stack.priority_player_id = player
    session.send_turn_phase_update()
    assert isinstance(game.events[-1], game_engine.TurnPhaseUpdatedSessionEventArgs)
    received = []
    session.register_transaction("pass", lambda tx: received.append(tx) or True)
    valid = RulesTransaction(
        player, "pass", game_engine.ETurnPhases.FirstMainPhase,
        requirements=(AndRequirement((PlayerHasPriorityRequirement(player),
                                      PlayerIsActiveRequirement(player))),))
    stale = RulesTransaction(player, "pass", game_engine.ETurnPhases.Draw)
    assert session.submit_transaction(valid)
    assert not session.submit_transaction(stale)
    assert session.handle_transaction()
    assert received == [valid]


def test_pass_priority_transaction_matches_client_requirements_and_routes_window():
    player, ai = game_engine.UID.make(244, 1), game_engine.UID.make(3, 2)
    session = AuthoritativeSession(55, (player, ai), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    window = PriorityWindowAction(TurnPhasePlayers.ALL)
    session.push_game_action(window)
    assert session.action_stack.update() is False  # enters the window
    transaction = RulesTransaction.pass_priority(
        player, game_engine.ETurnPhases.FirstMainPhase)
    assert session.submit_transaction(transaction)
    assert session.handle_transaction()
    assert window.priority_player_id == ai
    assert not session.submit_transaction(RulesTransaction.pass_priority(
        player, game_engine.ETurnPhases.FirstMainPhase))


def test_pass_priority_accepts_equivalent_raw_wire_priority_identity():
    """Reconnect/AI projections must not strand a typed client pass."""
    player = game_engine.UID.make(244, 101)
    ai = game_engine.UID.make(3, 102)
    session = AuthoritativeSession(551, (player, ai), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    window = PriorityWindowAction(TurnPhasePlayers.ACTIVE)
    session.push_game_action(window)
    # Simulate a persisted/projected raw uint64 priority value.  The native
    # participant remains the typed UID used by the transaction path.
    window._priority_queue.clear()
    window._priority_queue.append(int(player.uid64))
    session.action_stack.priority_player_id = int(player.uid64)
    transaction = RulesTransaction.pass_priority(
        player, game_engine.ETurnPhases.FirstMainPhase)
    assert session.submit_transaction(transaction)
    assert session.handle_transaction()
    assert window.priority_player_id is None


def test_choose_draw_first_reorders_players_like_client_transaction():
    player, opponent = game_engine.UID.make(244, 1), game_engine.UID.make(3, 2)
    game = game_engine.Game(55, player, opponent)
    session = AuthoritativeSession(55, (player, opponent), seed_z=1, seed_w=2,
                                   event_sink=GameEngineEventSink(game))
    session.current_turn_phase = game_engine.ETurnPhases.PickGoesFirst
    session.push_game_action(PriorityWindowAction(TurnPhasePlayers.ACTIVE))
    assert session.action_stack.update() is False
    assert session.submit_transaction(RulesTransaction.choose_draw_first(
        player, game_engine.ETurnPhases.PickGoesFirst))
    assert session.handle_transaction()
    assert session.player_ids == (opponent, player)
    assert session.active_player_id == opponent
    assert isinstance(game.events[-2], PlayerWishesToDrawFirstSessionEventArgs)


def test_authoritative_session_owns_declared_combats_not_transport_state():
    player = game_engine.UID.make(244, 1)
    session = AuthoritativeSession(55, (player,), seed_z=1, seed_w=2)
    defender = object()
    attacker = object()
    combat = session.declare_attack(player, defender, attacker)
    assert combat is not None
    assert combat.attacker is attacker
    assert session.combat_manager.combats == [combat]


def test_repeated_attack_declaration_reuses_native_combat_identity():
    player = game_engine.UID.make(244, 2)
    session = AuthoritativeSession(55, (player,), seed_z=1, seed_w=2)
    defender = object()
    attacker = object()
    first = session.declare_attack(player, defender, attacker)
    second = session.declare_attack(player, defender, attacker)
    assert second is first
    assert len(session.combat_manager.combats) == 1


def test_native_combat_descriptors_rehydrate_after_reconnect():
    player = game_engine.UID.make(244, 3)
    session = AuthoritativeSession(56, (player,), seed_z=1, seed_w=2)
    attacker = SimpleNamespace(session_card_id=1001)
    defender = SimpleNamespace(session_card_id=2001)
    blocker = SimpleNamespace(session_card_id=1002)
    session.runtime_facts = SimpleNamespace(
        get_card=lambda uid: {1001: attacker, 1002: blocker}.get(int(uid)))
    combat = session.declare_attack(player, defender, attacker)
    combat.declare_blockers((blocker,))
    saved = session.snapshot()
    restored = AuthoritativeSession(56, (player,), seed_z=8, seed_w=9)
    restored.runtime_facts = session.runtime_facts
    assert restored.restore_snapshot(saved)
    assert restored.rehydrate_combats()
    recovered = restored.combat_manager.combats[0]
    assert recovered.attacker is attacker
    assert recovered.blockers == [blocker]


def test_ported_ability_instance_preserves_metadata_effect_order_and_identity():
    metadata = MetadataAbilityInstance.from_runtime(
        "ability-guid", [{"effect_group_id": 1, "effect_instance_id": 4},
                         {"effect_group_id": 0, "effect_instance_id": 9}],
        0, source_uid=123, owner_id=7)
    ability = AbilityFactory(90).create(metadata, activating_player_id=7)
    assert ability.instance_id == 90
    assert ability.source_uid == 123
    assert [item["effect_group_id"] for item in ability.ordered_effects] == [0, 1]
    assert ability.continuation()["ability_instance_id"] == 90


def test_sqlite_snapshot_namespaces_port_state_without_replacing_legacy_state():
    stored = PersistedSessionStub()
    snapshot = SQLiteRulesSnapshot(stored)
    snapshot.save({"phase": "Draw", "seed_z": 12})
    assert stored.persisted == 1
    assert stored.turn_order == {
        "legacy_phase_idx": 4,
        "rules_port": {"phase": "Draw", "seed_z": 12},
    }
    assert snapshot.load() == {"phase": "Draw", "seed_z": 12}


def test_successful_rules_transaction_persists_at_mutation_boundary():
    stored = PersistedSessionStub()
    snapshot = SQLiteRulesSnapshot(stored)
    player = game_engine.UID.make(244, 31)
    session = AuthoritativeSession(58, (player,), seed_z=1, seed_w=2,
                                   snapshot=snapshot)
    session.register_transaction("test_mutation", lambda tx: True)
    transaction = RulesTransaction(player, "test_mutation", None)
    assert session.submit_transaction(transaction)
    assert session.handle_transaction()
    assert stored.persisted == 1
    assert stored.turn_order["rules_port"]["version"] == 1


def test_session_records_normalized_accepted_transactions_for_parity_capture():
    player = game_engine.UID.make(244, 35)
    session = AuthoritativeSession(63, (player,), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.action_stack.priority_player_id = player
    transaction = RulesTransaction.pass_priority(
        player, game_engine.ETurnPhases.FirstMainPhase)
    assert session.submit_transaction(transaction)
    assert session.transaction_history == ({
        "player_id": int(player.uid64), "kind": "pass_priority",
        "phase": "FirstMainPhase", "payload": {},
    },)
    assert session.parity_state()["phase"] == "FirstMainPhase"


def test_auto_pass_and_priority_sync_transactions_follow_client_control_flow():
    player = game_engine.UID.make(244, 43)
    game = game_engine.Game(73, player, game_engine.UID.make(3, 2))
    session = AuthoritativeSession(73, (player,), seed_z=1, seed_w=2,
                                   event_sink=GameEngineEventSink(game))
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.push_game_action(PriorityWindowAction(TurnPhasePlayers.ACTIVE))
    auto = RulesTransaction.set_auto_pass(player, True, 2)
    assert session.submit_transaction(auto)
    assert session.handle_transaction()
    assert session.auto_pass_states[player] == 2
    assert session.submit_transaction(RulesTransaction.cancel_auto_pass(player))
    assert session.handle_transaction()
    assert player not in session.auto_pass_states
    session.action_stack.priority_player_id = player
    assert session.submit_transaction(RulesTransaction.request_priority_sync(player))
    assert session.handle_transaction()
    assert isinstance(game.events[-1], game_engine.GreenLightSessionEventArgs)


def test_discard_transaction_uses_client_hand_requirement_and_injected_mutator():
    player = game_engine.UID.make(244, 44)
    card = SimpleNamespace(collection=game_engine.ECardCollections.Hand)
    facts = type("Facts", (), {
        "get_card": lambda self, card_id: card if card_id == 7 else None,
    })()
    session = AuthoritativeSession(74, (player,), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.Discard
    session.set_runtime_facts(facts)
    seen = []
    session.set_discard_transaction_resolver(lambda tx: seen.append(tx.payload) or True)
    tx = RulesTransaction.discard(player, 7)
    assert session.submit_transaction(tx)
    assert session.handle_transaction()
    assert seen == [{"card_id": 7}]


def test_play_champion_transaction_matches_start_game_requirements():
    player = game_engine.UID.make(244, 45)
    card = SimpleNamespace(collection=game_engine.ECardCollections.Hand,
                           card_type=game_engine.ECardTypes.Champion)
    facts = type("Facts", (), {
        "get_card": lambda self, card_id: card if card_id == 8 else None,
    })()
    session = AuthoritativeSession(75, (player,), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.StartGame
    session.set_runtime_facts(facts)
    seen = []
    session.set_card_transaction_resolver(lambda kind, tx: seen.append(kind) or True)
    tx = RulesTransaction.play_champion(player, 8)
    assert session.submit_transaction(tx)
    assert session.handle_transaction()
    assert seen == ["play_champion"]


def test_phase_transition_persists_after_phase_update():
    stored = PersistedSessionStub()
    player = game_engine.UID.make(244, 32)
    session = AuthoritativeSession(60, (player,), seed_z=1, seed_w=2,
                                   snapshot=SQLiteRulesSnapshot(stored))
    session.current_turn_phase = game_engine.ETurnPhases.PreGame
    session.transition_to(game_engine.ETurnPhases.StartGame)
    assert stored.persisted == 1
    assert stored.turn_order["rules_port"]["phase"] == "StartGame"


def test_snapshot_records_pending_action_and_chain_descriptors_for_reconnect():
    player = game_engine.UID.make(244, 33)
    session = AuthoritativeSession(61, (player,), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    action = PriorityWindowAction(TurnPhasePlayers.ACTIVE)
    session.push_game_action(action)
    ability = AbilityStub(71)
    session.ability_manager.add_chain(71, ability)
    session.chain._instance_ids.append(71)
    saved = session.snapshot()
    restored = AuthoritativeSession(61, (player,), seed_z=9, seed_w=9)
    assert restored.restore_snapshot(saved)
    assert restored.restored_action_descriptors[0]["type"] == "PriorityWindowAction"
    assert saved["chain_instance_ids"] == [71]


def test_reconnect_hydration_hooks_restore_factory_actions_without_ui_replay():
    player = game_engine.UID.make(244, 34)
    source = AuthoritativeSession(62, (player,), seed_z=1, seed_w=2)
    source.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    source.push_game_action(PriorityWindowAction(TurnPhasePlayers.ACTIVE))
    restored = AuthoritativeSession(62, (player,), seed_z=1, seed_w=2)
    assert restored.restore_snapshot(source.snapshot())
    assert restored.restore_actions(lambda descriptor: PriorityWindowAction(
        TurnPhasePlayers.ACTIVE))
    assert restored.action_stack.count == 1
    assert restored.action_stack.current_action is None


def test_persisted_game_adapter_uses_session_rng_and_existing_event_sink():
    stored = PersistedSessionStub()
    stored.session_id = 55
    stored.seed_z, stored.seed_w = 101, 202
    player = game_engine.UID.make(244, 1)
    ai = game_engine.UID.make(3, 2)
    stored.players = [(player, 0), (ai, 1)]
    game = game_engine.Game(55, player, ai)
    port = session_from_persisted_game(stored, game)
    assert port.player_ids == (player, ai)
    assert port.random_number_generator.get_seed() == (101, 202)
    port.persist()
    assert stored.turn_order["rules_port"]["seed_w"] == 202


def test_persisted_game_adapter_repairs_raw_practice_participants():
    stored = PersistedSessionStub()
    stored.session_id = 551
    stored.seed_z, stored.seed_w = 1, 2
    player = game_engine.UID.make(PLAYER_UID_TYPE, 123)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    # This is the JSON shape written by older practice sessions: only the
    # human is registered, and some rows contain it twice.
    stored.players = [(123, 0), (123, 0)]
    port = session_from_persisted_game(
        stored, game_engine.Game(551, player, ai))
    assert port.player_ids == (player, ai)
    assert port.coerce_transaction_player_id(player) == player


def test_practice_participants_survive_projection_with_duplicate_human_uids():
    stored = PersistedSessionStub()
    stored.session_name = "Session-practice-participant-test"
    player = game_engine.UID.make(PLAYER_UID_TYPE, 124)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    stored.players = [(int(player.uid64), 0), (int(player.uid64), 0)]
    malformed_projection = game_engine.Game(552, player, player)

    participants = _native_participant_ids(stored, malformed_projection)

    assert participants == (player, ai)


def test_persisted_game_adapter_rehydrates_rules_scheduler_snapshot():
    stored = PersistedSessionStub()
    stored.session_id = 56
    stored.seed_z, stored.seed_w = 1, 2
    player = game_engine.UID.make(244, 11)
    ai = game_engine.UID.make(3, 12)
    stored.players = [(player, 0), (ai, 1)]
    stored.turn_order["rules_port"] = {
        "version": 1,
        "phase": "SecondMainPhase",
        "active_player_id": int(ai.uid64),
        "priority_player_id": int(player.uid64),
        "seed_z": 901,
        "seed_w": 902,
        "total_turns_taken": 7,
        "pending_activation": {"ability_instance_id": 44},
    }
    game = game_engine.Game(56, player, ai)
    port = session_from_persisted_game(stored, game)
    assert port.current_turn_phase == game_engine.ETurnPhases.SecondMainPhase
    assert port.active_player_id == ai
    assert port.action_stack.priority_player_id == player
    assert port.random_number_generator.get_seed() == (901, 902)
    assert port.total_turns_taken == 7
    assert port.pending_activation == {"ability_instance_id": 44}


def test_pvp_projected_chain_round_trips_and_rehydrates_native_window():
    source = PvpAuthoritativeSession(
        57, (game_engine.UID.make(244, 11), game_engine.UID.make(244, 12)),
        seed_z=1, seed_w=2)
    first = game_engine.UID.make(244, 11)
    second = game_engine.UID.make(244, 12)
    source.action_stack.priority_player_id = second
    source.queue_projected_chain(
        {"kind": "spell", "source_uid": 901, "instance_id": 77,
         "ability_guids": []}, 11, first_player_id=second)
    saved = source.snapshot()
    assert saved["projected_chain"][0]["owner_id"] == 11

    restored = PvpAuthoritativeSession(
        57, (first, second), seed_z=9, seed_w=9)
    assert restored.restore_snapshot(saved)
    assert restored.chain._instance_ids == []
    assert restored.rehydrate_projected_chain()
    assert restored.chain._instance_ids == [77]
    assert restored.chain.peek_ability().owner_id == first
    assert isinstance(restored.action_stack.peek(), PriorityWindowAction)
    restored.chain.pop_ability(77)
    assert restored.snapshot()["projected_chain"] == []
    restored.forget_projected_chain(77)
    assert restored.snapshot()["projected_chain"] == []


def test_chain_resolution_keeps_first_main_phase():
    """Resolving the last chain item must return priority to the active
    player in the same main phase; the phase must not advance.

    Playing a card in FirstMainPhase puts it on the chain.  Once both players
    pass and the item resolves, C# re-enters the interrupted phase
    ``PriorityWindowAction``; the turn phase stays FirstMainPhase.
    """
    player = game_engine.UID.make(244, 21)
    ai = game_engine.UID.make(3, 1000)
    session = AuthoritativeSession(70, (player, ai), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    phase_window = PriorityWindowAction(TurnPhasePlayers.ALL)
    phase_window._rules_port_phase = "FirstMainPhase"
    session.push_game_action(phase_window)
    assert session.action_stack.update() is False
    session.set_ability_resolver(
        lambda item: AbilityResolutionState.COMPLETED)
    session.queue_projected_chain(
        {"kind": "spell", "source_uid": 905, "instance_id": 90,
         "ability_guids": []},
        player, first_player_id=player)
    chain_window = session.action_stack.peek()
    assert isinstance(chain_window, PriorityWindowAction)
    assert chain_window.ability_responding_to is not None
    assert session.pass_player_priority(player)
    assert chain_window.priority_player_id is None
    # The response window is now consumed; one tick pops it and leaves only
    # the resolver above the interrupted phase window.
    assert session.tick()
    assert isinstance(session.action_stack.peek(), ResolveTopOfChainAction)
    # A re-attach during the pass (sync_checkpoint calls this) must NOT clear
    # the stack just because the top is the resolver rather than a response
    # window, or the interrupted FirstMainPhase window is lost.
    assert not session.ensure_projected_chain_action()
    assert session.current_turn_phase == game_engine.ETurnPhases.FirstMainPhase
    for _ in range(12):
        if not session.tick():
            break
    assert session.current_turn_phase == game_engine.ETurnPhases.FirstMainPhase
    top = session.action_stack.peek()
    assert isinstance(top, PriorityWindowAction)
    assert top.ability_responding_to is None
    assert top.priority_player_id == player


def test_generic_projected_card_chain_is_owned_by_native_action_stack():
    player = game_engine.UID.make(244, 13)
    ai = game_engine.UID.make(3, 14)
    session = AuthoritativeSession(59, (player, ai), seed_z=1, seed_w=2)
    session.queue_projected_chain(
        {"kind": "troop", "source_uid": 902, "instance_id": 78},
        player, first_player_id=player)
    assert session.chain._instance_ids == [78]
    assert isinstance(session.action_stack.peek(), PriorityWindowAction)
    assert session.action_stack.priority_player_id == player
    assert session.snapshot()["projected_chain"][0]["source_uid"] == 902
    restored = AuthoritativeSession(59, (player, ai), seed_z=8, seed_w=9)
    assert restored.restore_snapshot(session.snapshot())
    assert restored.rehydrate_projected_chain()
    assert restored.chain._instance_ids == [78]


def test_generic_ai_projected_card_can_start_with_human_response():
    player = game_engine.UID.make(PLAYER_UID_TYPE, 14)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    session = AuthoritativeSession(60, (player, ai), seed_z=1, seed_w=2)
    session.active_player_id = ai
    session.queue_projected_chain(
        {"kind": "troop", "source_uid": 903, "instance_id": 79},
        ai, first_player_id=player)

    action = session.action_stack.peek()
    assert isinstance(action, PriorityWindowAction)
    assert action.priority_player_id == player

    restored = AuthoritativeSession(60, (player, ai), seed_z=8, seed_w=9)
    assert restored.restore_snapshot(session.snapshot())
    assert restored.active_player_id == ai
    assert restored.rehydrate_projected_chain()
    restored_action = restored.action_stack.peek()
    assert isinstance(restored_action, PriorityWindowAction)
    assert restored_action.priority_player_id == player
    assert restored.pass_player_priority(player)
    assert restored_action.priority_player_id is None


def test_projected_chain_reconciles_priority_from_durable_snapshot():
    from collections import deque

    player = game_engine.UID.make(PLAYER_UID_TYPE, 15)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    source = AuthoritativeSession(61, (player, ai), seed_z=1, seed_w=2)
    source.active_player_id = ai
    source.queue_projected_chain(
        {"kind": "troop", "source_uid": 904, "instance_id": 80},
        ai, first_player_id=player)
    saved = source.snapshot()

    store = SimpleNamespace(load=lambda: saved)
    restored = AuthoritativeSession(
        61, (player, ai), seed_z=8, seed_w=9, snapshot=store)
    assert restored.restore_snapshot(saved)
    assert restored.rehydrate_projected_chain()
    action = restored.action_stack.peek()
    action._priority_queue = deque([ai])
    restored.action_stack.priority_player_id = ai

    assert restored.reconcile_projected_chain_priority()
    assert action.priority_player_id == player
    assert restored.action_stack.priority_player_id == player


def test_reconnect_restores_normalized_transaction_history_for_parity():
    player = game_engine.UID.make(244, 42)
    source = AuthoritativeSession(71, (player,), seed_z=1, seed_w=2)
    source.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    source.action_stack.priority_player_id = player
    source.submit_transaction(RulesTransaction.pass_priority(
        player, game_engine.ETurnPhases.FirstMainPhase))
    restored = AuthoritativeSession(71, (player,), seed_z=9, seed_w=9)
    assert restored.restore_snapshot(source.snapshot())
    assert restored.transaction_history == source.transaction_history


def test_snapshot_round_trip_supports_string_player_ids():
    source = AuthoritativeSession(72, ("player", "ai"), seed_z=1, seed_w=2)
    source.active_player_id = "ai"
    source.action_stack.priority_player_id = "player"
    restored = AuthoritativeSession(72, ("player", "ai"), seed_z=9, seed_w=9)
    assert restored.restore_snapshot(source.snapshot())
    assert restored.active_player_id == "ai"
    assert restored.action_stack.priority_player_id == "player"


def test_rules_session_for_caches_one_opt_in_host_per_live_session_wrapper():
    stored = PersistedSessionStub()
    stored.session_id = 57
    stored.seed_z, stored.seed_w = 3, 4
    player = game_engine.UID.make(244, 21)
    ai = game_engine.UID.make(3, 22)
    stored.players = [(player, 0), (ai, 1)]
    game = game_engine.Game(57, player, ai)
    first = rules_session_for(stored, game)
    second = rules_session_for(stored, game)
    assert first is second


def test_enable_rules_port_wires_runtime_facts_and_sqlite_mutations():
    stored = PersistedSessionStub()
    stored.session_id = 58
    stored.seed_z, stored.seed_w = 5, 6
    player = game_engine.UID.make(244, 25)
    ai = game_engine.UID.make(3, 26)
    stored.players = [(player, 0), (ai, 1)]
    api = SimpleNamespace()
    port = enable_rules_port(stored, game_engine.Game(58, player, ai),
                             {}, pvp_api=api)
    assert port.runtime_facts is not None
    assert port.event_sink.mutation_adapter._pvp is api


def test_enable_rules_port_upgrades_a_previously_cached_light_host():
    stored = PersistedSessionStub()
    stored.session_id = 59
    stored.seed_z, stored.seed_w = 7, 8
    player = game_engine.UID.make(244, 27)
    ai = game_engine.UID.make(3, 28)
    stored.players = [(player, 0), (ai, 1)]
    game = game_engine.Game(59, player, ai)
    light = rules_session_for(stored, game)
    api = SimpleNamespace()
    upgraded = enable_rules_port(stored, game, {}, pvp_api=api)
    assert upgraded is light
    assert upgraded.runtime_facts is not None
    assert upgraded.event_sink.mutation_adapter._pvp is api


def test_cached_enable_keeps_native_shared_turn_over_stale_projection():
    stored = PersistedSessionStub()
    stored.session_id = 590
    stored.seed_z, stored.seed_w = 7, 8
    player = game_engine.UID.make(PLAYER_UID_TYPE, 27)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    stored.players = [(player, 0), (ai, 1)]
    game = game_engine.Game(590, player, ai)
    shared = {"turn_player": "ai", "phase_idx": 0}
    port = enable_rules_port(stored, game, shared)
    stale = {"turn_player": "player", "phase_idx": 8}

    attached = enable_rules_port(stored, game, stale)

    assert attached is port
    assert stored._rules_port_battle_state is shared
    assert stale["turn_player"] == "ai"
    assert stale["phase_idx"] == 0
    assert attached.runtime_facts.battle_state is shared


def test_enable_rules_port_repairs_cached_legacy_practice_participants():
    stored = PersistedSessionStub()
    stored.session_id = 60
    stored.seed_z, stored.seed_w = 9, 10
    player = game_engine.UID.make(PLAYER_UID_TYPE, 29)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    stored.players = [(29, 0), (29, 0)]
    game = game_engine.Game(60, player, ai)
    cached = rules_session_for(stored, game)
    cached.player_ids = (29,)
    cached.active_player_id = 29
    cached.action_stack.priority_player_id = 29

    repaired = enable_rules_port(stored, game, {})

    assert repaired is cached
    assert repaired.player_ids == (player, ai)
    assert repaired.active_player_id == player
    assert repaired.action_stack.priority_player_id == player


def test_cached_participant_repair_updates_priority_window_queue():
    from collections import deque

    stored = PersistedSessionStub()
    stored.session_id = 61
    stored.seed_z, stored.seed_w = 11, 12
    player = game_engine.UID.make(PLAYER_UID_TYPE, 30)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    stored.players = [(30, 0), (30, 0)]
    game = game_engine.Game(61, player, ai)
    cached = rules_session_for(stored, game)
    cached.player_ids = (30,)
    cached.active_player_id = 30
    action = PriorityWindowAction(TurnPhasePlayers.ALL)
    cached.action_stack.push(action)
    action._priority_queue = deque([30])
    cached.action_stack.priority_player_id = 30

    repaired = enable_rules_port(stored, game, {})

    assert repaired.action_stack.peek() is action
    assert list(action._priority_queue) == [player]
    assert action.priority_player_id == player


def test_persisted_game_factory_accepts_explicit_card_mutation_adapter():
    stored = PersistedSessionStub()
    stored.session_id = 66
    stored.seed_z, stored.seed_w = 1, 2
    player = game_engine.UID.make(244, 23)
    ai = game_engine.UID.make(3, 24)
    stored.players = [(player, 0), (ai, 1)]
    mutation = lambda event: True
    port = session_from_persisted_game(
        stored, game_engine.Game(66, player, ai), mutation_adapter=mutation)
    assert port.event_sink.mutation_adapter is mutation


def test_records_filter_state_history_and_faction():
    """Turn-history predicates derive from card_state; factions compare equal."""
    from rules_port.filters import records_filter_matches
    card = {"card_uid": 1, "state": 8192, "faction": 3}  # CameOutThisTurn
    assert records_filter_matches(card, {"type": "IsPlayedThisTurn"})
    assert not records_filter_matches(card, {"type": "IsDamagedThisTurn"})
    # EFactions is a plain enum: InFaction compares by equality, not bitwise.
    assert records_filter_matches(card, {"type": "InFaction", "m_Faction": 3})
    assert not records_filter_matches(card, {"type": "InFaction", "m_Faction": 1})


def test_records_filters_decode_enum_names_and_card_state():
    """Filters must decode Records enum-name fields and read dynamic state.

    Records serializes attribute/shard flags as display strings and stores
    int-attributes dynamically; the port previously passed the strings to
    ``int()`` (crashing) and never read ``int_attrs``/``shards``.
    """
    from rules_port.filters import (records_filter_from_metadata,
                                    records_filter_matches)

    card = {"card_uid": 1, "int_attrs": {"Trained": 3}, "shards": [16],
            "attributes": 2, "location": "warzone", "cost": 2}
    assert records_filter_from_metadata(
        {"type": "HasAllAttributeFlags", "m_CardAttributeFlags": "Flight"})
    assert records_filter_matches(
        card, {"type": "HasAllAttributeFlags",
               "m_CardAttributeFlags": "Flight"})
    assert not records_filter_matches(
        card, {"type": "HasAllAttributeFlags",
               "m_CardAttributeFlags": "SpellShield"})
    # Static wrappers must be accepted rather than raising ValueError.
    assert records_filter_from_metadata(
        {"type": "StaticAndCardFilter", "m_TargetFilters": []}) is not None
    assert records_filter_matches(
        card, {"type": "IntAttrFilter", "m_Attribute": "Trained",
               "m_ComparisonOp": "GreaterThanOrEqual", "m_Value": 1})
    assert records_filter_matches(
        card, {"type": "IsColor", "m_ColorFlags": "Sapphire"})


def test_commit_attack_recovery_survives_typed_merge():
    """A raw-recovered attack declaration must survive the typed merge.

    ``CommitTroopsToAttack``'s nested AttackDeclaration does not decode through
    the generic ObjFmt walker, so the typed parser produces nothing; assigning
    an empty tuple then clobbered the recovered declaration and the client was
    stuck in Select Attackers.
    """
    from application.player_transactions import (classify_player_transaction,
                                                 typed_payload_from_decoded)
    raw = (b";0;0;2;PlayerId;1;1;1;m_UID64;2;2;0;F490FB3351F3D606;"
           b"Transaction;3;3;3;m_Attacks;4;4;0;1;0;5;5;2;DefendingCardId;6;6;1;"
           b"value;7;1;1;m_UID64;8;2;0;0102000000000000;"
           b"AttackingCardIds;9;7;0;1;0;10;6;1;value;11;1;1;m_UID64;12;2;0;"
           b"0107000000000000;m_PlayerId;13;1;1;m_UID64;14;2;0;"
           b"F490FB3351F3D606;m_TransactionId;15;8;0;32000000;"
           b"Game.Shared.Mechanics.Transactions."
           b"CommitTroopsToAttackTransaction")
    command = classify_player_transaction(raw)
    assert command.is_commit_attack
    payload = typed_payload_from_decoded(command, {"__raw__": raw})
    assert payload is not None
    assert payload.get("declarations") == ((0x0201, (0x0701,)),)


def test_target_spec_resolves_variable_counts():
    """TargetVariable min/max resolve from the ability variable map."""
    from gamedata.models import TargetSpec
    spec = TargetSpec(guid="t", name="n", is_auto=False, is_random=False,
                      player_filter="Self", collection_flags="Warzone",
                      minimum=1, maximum=1, optional=False, explicit=True,
                      min_variable="toAffect", max_variable="toAffect")
    assert spec.resolved_maximum({"toAffect": 3}) == 3
    assert spec.resolved_minimum({"toAffect": 2}) == 2
    assert spec.resolved_maximum({}) == 1


def test_finish_playing_cards_drains_queue():
    player = game_engine.UID.make(244, 93)
    session = AuthoritativeSession(93, (player,), seed_z=1, seed_w=2)
    seen = []
    session.set_card_finisher(
        lambda item: seen.append(item["card_uid"]) or True)
    session.queue_card_ready_to_play(101, player)
    assert session.finish_playing_cards() is True
    assert seen == [101]
    assert session.finish_playing_cards() is False


def test_chain_can_resolve_per_phase():
    """C# ChainCanResolve is false in Draw/DeclareAttack/DeclareDefense/Discard."""
    player = game_engine.UID.make(244, 92)
    session = AuthoritativeSession(92, (player,), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    assert session.chain_can_resolve()
    session.current_turn_phase = game_engine.ETurnPhases.Draw
    assert not session.chain_can_resolve()
    session.current_turn_phase = game_engine.ETurnPhases.DeclareAttack
    assert not session.chain_can_resolve()
    session.current_turn_phase = game_engine.ETurnPhases.Discard
    assert not session.chain_can_resolve()


def test_pick_goes_first_advances_to_mulligan():
    """C# PickGoesFirstState.GetNextTurnPhase always returns Mulligan.

    Modelling it as a plain TurnPhaseState raised "PickGoesFirst must select
    a next phase" because it permits both PreGame and Mulligan.
    """
    player = game_engine.UID.make(244, 91)
    session = AuthoritativeSession(91, (player,), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.PickGoesFirst
    assert session.advance_turn_phase() == game_engine.ETurnPhases.Mulligan


def test_combat_damage_order_requires_every_blocker():
    from rules_port.combat import _has_juggernaut
    combat = Combat("instigator", "defender", CombatId(1, 1))
    combat.declare_blockers([CombatCardStub(20, 1), CombatCardStub(30, 1)])
    assert not combat.assign_damage_order([20])
    assert combat.assign_damage_order([20, 30])
    # Crush (rule flag) must count as Juggernaut even when the attribute is
    # absent; the previous getattr fallback never reached ``crush``.
    assert _has_juggernaut(SimpleNamespace(crush=True))
    assert not _has_juggernaut(SimpleNamespace(crush=False))


def test_legal_targets_excludes_spell_shielded_opponent_permanent():
    """A non-auto target must not offer an opposing Spell-Shielded permanent."""
    from unittest import mock
    import rules_port.targeting as T

    row = (101, "Troop", "warzone", 0, "tpl", 0, 1, 1, "Shielded", 1, "", "{}",
           "[]", "{}", "", 0, 0, "tpl", 128)  # ECardAttributes.SpellShield
    template = {"template_id": "t", "is_auto_target": 0, "is_random_target": 0,
                "optional": 0, "explicit": 0,
                "player_filter": "MultipleOpponents",
                "collection_flags": "Warzone", "min_target_count": 1,
                "max_target_count": 1, "filter_json": "{}",
                "target_kind": "AbilityTargetTemplate"}
    with mock.patch.object(T, "target_template", return_value=template), \
            mock.patch("pvp_db.db_target_candidate_rows", return_value=[row]), \
            mock.patch.object(
                T, "_source_card",
                return_value={"card_uid": 999, "user_id": 5}):
        assert T.legal_targets(
            object(), 1, 5, "t", 999, both_players=True,
            battle_state={"_rules_port_suppress_card_properties": True}) == []


def test_random_target_sample_bounds_the_pool():
    """A random auto-target must resolve to a bounded sample.

    Infernal Professor's "a random non-resource card from your deck" target
    previously resolved to the whole legal pool, moving the entire deck into
    hand.  ``_random_target_sample`` bounds it to the target's maximum.
    """
    from rules_port.resolution import _random_target_sample

    class _Rng:
        def next(self, bound):
            return 0

    pool = [11, 22, 33, 44, 55]
    assert _random_target_sample(pool, 1, {"_rules_rng": _Rng()}) == (11,)
    two = _random_target_sample(pool, 2, {"_rules_rng": _Rng()})
    assert len(two) == 2 and all(value in pool for value in two)
    # count <= 0 is "unlimited": the whole pool is returned (shuffled).
    assert sorted(_random_target_sample(pool, 0, {"_rules_rng": _Rng()})) == sorted(pool)
    # Without a session RNG a plain random sample of the requested size is used.
    assert len(_random_target_sample([7, 8, 9], 1, {})) == 1


def test_records_filter_matches_keeps_card_with_threshold_context():
    """A threshold context must not shadow the filtered card value.

    ``records_filter_matches`` iterates the active player's threshold pool;
    the loop variable previously shadowed the card dict, so every filter
    evaluated with a threshold context compared against an int and returned
    False (Subterranean Spy's ThisIsUnderground reveal silently failed).
    """
    from rules_port.filters import records_filter_matches
    card = {"card_uid": 1, "card_type": "Troop", "location": "underground",
            "user_id": 5, "state": 0}
    context = SimpleNamespace(
        bstate={"resolving_owner_id": 5, "player_threshold": {16: 1}})
    spec = {"_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
            "m_Collection": "Underground"}
    assert records_filter_matches(card, spec, source=card, context=context)


def test_combat_manager_accepts_wire_combat_id():
    """The AI passes ``game_engine.CombatId`` into the port combat manager.

    The wire struct exposes ``attacker``/``serial`` while the port keys on
    ``attacker_id``/``serial_number``; without coercion ``create_attack``
    raised ``AttributeError: 'CombatId' object has no attribute 'is_valid'``
    and aborted the AI attack declaration.
    """
    wire = game_engine.CombatId(game_engine.UID.make(244, 7), 9)
    manager = CombatManager()
    combat = manager.create_attack(wire, "instigator", "defender")
    assert combat.combat_id.attacker_id == int(game_engine.UID.make(244, 7).uid64)
    assert combat.combat_id.serial_number == 9
    assert manager.contains(wire)
    assert manager.get(combat.combat_id) is combat


def test_wire_combat_id_coerces_port_attacker_integer():
    """The wire ``CombatId`` must accept the port's raw-uid64 attacker.

    ``rules_port.combat.CombatId.attacker_id`` is an int; a combat-listing
    projection that crossed the namespaces crashed on serialization with
    ``'int' object has no attribute 'write'``.
    """
    raw = int(game_engine.UID.make(244, 7).uid64)
    cid = game_engine.CombatId(raw, 3)
    assert isinstance(cid.attacker, game_engine.UID)
    assert cid.attacker.uid64 == raw
    assert cid.serial == 3
    # Round-trips through the wire writer without raising.
    from domain.serializer import Serializer
    ser = Serializer()
    ser.begin_write()
    ser.add_combat_id(cid)
    payload = ser.end_write()
    assert payload


def test_combat_port_preserves_blocker_order_and_crush_damage_routing():
    manager = CombatManager()
    attacker = CombatCardStub(10, 7, crush=True)
    first, second = CombatCardStub(20, 1), CombatCardStub(30, 1)
    champion = CombatCardStub(99, 0)
    combat = manager.create_attack(CombatId(), 1, champion)
    combat.declare_attacker(attacker)
    combat.declare_blockers((first, second))
    assert combat.assign_damage_order((30, 20))
    assert combat.blockers == [second, first]
    calls = []
    # The existing session damage adapter would determine lethal; this stub
    # makes each blocker consume one point and exposes source routing.
    def damage(source, target, amount, minimum):
        calls.append((source.session_card_id, target.session_card_id, amount, minimum))
        return min(amount, 1) if target is not champion else amount
    results = CombatResolver.resolve(combat, CombatPhase.STANDARD, damage)
    assert calls[:3] == [(10, 30, 7, True), (10, 20, 6, True),
                         (10, 99, 5, False)]
    assert [result.damage for result in results] == [1, 1, 5, 1, 1]


def test_damage_order_transaction_matches_client_combat_requirements_and_update():
    player = game_engine.UID.make(244, 1)
    session = AuthoritativeSession(55, (player,), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.AssignDamage
    attacker, first, second, champion = (CombatCardStub(10, 7),
                                          CombatCardStub(20, 1),
                                          CombatCardStub(30, 1),
                                          CombatCardStub(99, 0))
    combat = session.combat_manager.create_attack(CombatId(), player, champion)
    combat.declare_attacker(attacker)
    combat.declare_blockers((first, second))
    assert AttackExistsRequirement(combat.combat_id).is_valid(session, player)
    assert AllDamageAssignedRequirement(combat.combat_id, (30, 20)).is_valid(session, player)
    transaction = RulesTransaction.assign_damage_order(
        player, game_engine.ETurnPhases.AssignDamage,
        ((combat.combat_id, (30, 20)),))
    assert session.submit_transaction(transaction)
    assert session.handle_transaction()
    assert [card.session_card_id for card in combat.blockers] == [30, 20]
    assert session.combat_has_standard_damage


def test_combat_damage_backend_is_the_explicit_port_projection_seam():
    calls = []
    backend = CombatDamageBackend(
        lambda session, transaction: calls.append((session, transaction)) or True)
    marker = object()
    assert backend(marker, "damage")
    assert calls == [(marker, "damage")]


def test_native_combatant_preserves_client_combat_phase_properties():
    firststrike = _Combatant(
        10, attack=3, attributes=int(game_engine.ECardAttributes.FirstStrike))
    dualstrike = _Combatant(
        11, attack=3, attributes=int(game_engine.ECardAttributes.DualStrike))
    normal = _Combatant(12, attack=3)

    assert firststrike.cares_about_combat_phase(CombatPhase.FIRST_STRIKE)
    assert not firststrike.cares_about_combat_phase(CombatPhase.STANDARD)
    assert dualstrike.cares_about_combat_phase(CombatPhase.FIRST_STRIKE)
    assert dualstrike.cares_about_combat_phase(CombatPhase.STANDARD)
    assert not normal.cares_about_combat_phase(CombatPhase.FIRST_STRIKE)
    assert normal.cares_about_combat_phase(CombatPhase.STANDARD)


def test_native_conditions_cover_cast_counts_and_keyword_count():
    class Context:
        bstate = {"player_cards_cast_this_turn": 3,
                  "player_nonresource_cards_cast_this_turn": 2,
                  "player_cards_cast": 5}
        ability_source_owner_id = 7
        ability_source_uid = 10

        def card(self, uid):
            return {"card_uid": uid, "attributes": 3,
                    "int_attrs": {"Rage": 1, "Lethal": 1}}

    context = Context()
    assert evaluate_condition({
        "_t": "ChampionCardsCastCondition", "m_ThisTurn": 1,
        "m_NonResource": 1, "m_RequiredQuantity": 2,
        "m_ComparisonOp": "GreaterThanOrEqual"}, context)
    assert evaluate_condition({
        "_t": "ChampionCardsCastCondition", "m_ThisTurn": 0,
        "m_RequiredQuantity": 5, "m_ComparisonOp": "Equals"}, context)
    assert evaluate_condition({
        "_t": "SourceCardHasKeywords", "m_RequiredQuantity": 4,
        "m_ComparisonOp": "GreaterThanOrEqual"}, context)

    state = {"turn_number": 4}
    record_card_cast(state, 7, resource=True)
    assert state["player_resource_cards_cast_this_turn"] == 1


def test_attached_rules_port_does_not_fall_back_for_unknown_conditions():
    class Session:
        _rules_port_session = object()

    context = SimpleNamespace(session=Session(), bstate={})
    try:
        evaluate_condition({"_t": "ConditionNotYetPorted"}, context)
    except RuntimeError as exc:
        assert "ConditionNotYetPorted" in str(exc)
    else:
        raise AssertionError("unknown condition entered the legacy provider")


def test_rules_port_and_battle_engine_share_attached_checkpoint():
    import battle_engine

    class Session:
        turn_order = {"turn_player": "player", "phase_idx": 3}
        _persist_calls = 0

        def _persist(self, conn=None):
            self._persist_calls += 1

    session = Session()
    shared = {"turn_player": "player", "phase_idx": 3}
    session._rules_port_battle_state = shared
    assert battle_engine.load_state(session) is shared
    snapshot = SQLiteRulesSnapshot(session)
    snapshot.save({"phase": "FirstMainPhase"})
    assert battle_engine.load_state(session)["rules_port"]["phase"] == \
        "FirstMainPhase"
    assert session._rules_port_battle_state is shared
    assert session._rules_port_battle_state is battle_engine.load_state(session)


def test_native_checkpoint_helpers_do_not_require_battle_engine():
    class Session:
        turn_order = {"turn_player": "player", "phase_idx": 1,
                      "turn_phases": ["StartTurn", "Ready"]}

    session = Session()
    assert native_load_state(session) is session.turn_order
    assert native_current_phase(session.turn_order) == "Ready"
    state = {"turn_player": "player", "_ability_builder": object()}
    assert persistence_state(state) == {"turn_player": "player"}


def test_ai_events_use_native_trigger_backend_when_port_is_attached():
    import ai
    calls = []

    class Session:
        _rules_port_session = object()

    old = ai._dispatch_triggers
    try:
        # Exercise the actual helper's branch while replacing only its
        # transport-independent native dispatcher.
        import rules_port.triggers as port_triggers
        native = port_triggers.dispatch_native_trigger
        port_triggers.dispatch_native_trigger = lambda **kwargs: calls.append(kwargs) or "native"
        result = old(None, object(), object(), Session(), "p", "a", {},
                     "CardDrawnEvent", 12, source_owner_uid=0,
                     extra_target=13)
    finally:
        port_triggers.dispatch_native_trigger = native
    assert result == "native"
    assert calls[0]["event_type"] == "CardDrawnEvent"
    assert calls[0]["source_player_id"] == 0
    assert calls[0]["target_card_id"] == 13


def test_commit_attack_transaction_validates_then_creates_session_combat():
    attacker = type("Card", (), {"collection": game_engine.ECardCollections.Warzone,
                                  "session_card_id": 10})()
    defender = type("Card", (), {"collection": game_engine.ECardCollections.Champions,
                                  "session_card_id": 99})()
    facts = type("Facts", (), {
        "get_card": lambda self, card_id: {10: attacker, 99: defender}.get(card_id),
        "can_attack": lambda self, source, target, player: (
            source is attacker and target is defender and player == "p"),
    })()
    session = AuthoritativeSession(55, ("p",), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.DeclareAttack
    session.set_runtime_facts(facts)
    assert AttackerIsValidRequirement(99, 10).is_valid(session, "p")
    tx = RulesTransaction.commit_troops_to_attack(
        "p", game_engine.ETurnPhases.DeclareAttack, ((99, (10,)),))
    assert session.submit_transaction(tx)
    assert session.handle_transaction()
    assert len(session.combat_manager.combats) == 1
    assert session.combat_manager.combats[0].attacker is attacker


def test_commit_attack_passes_priority_like_csharp_transaction():
    """C# ``CommitTroopsToAttackTransaction.Resolve`` ends with
    ``session.DoPassPriorityTransaction()``.

    The live host registers an ``attack_transaction`` projection, and the port
    used to consume the ``DeclareAttack`` window only in the no-resolver
    branch.  The window then stayed open, the phase never advanced to
    ``DeclareAttackPriorityWindow``, and the client was stuck in Select
    Attackers.
    """
    attacker = type("Card", (), {"collection": game_engine.ECardCollections.Warzone,
                                  "session_card_id": 10})()
    defender = type("Card", (), {"collection": game_engine.ECardCollections.Champions,
                                  "session_card_id": 99})()
    facts = type("Facts", (), {
        "get_card": lambda self, card_id: {10: attacker, 99: defender}.get(card_id),
        "can_attack": lambda self, source, target, player: (
            source is attacker and target is defender and player == "p"),
    })()
    session = AuthoritativeSession(55, ("p",), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.DeclareAttack
    session.set_runtime_facts(facts)
    window = PriorityWindowAction(TurnPhasePlayers.ACTIVE)
    window._rules_port_phase = "DeclareAttack"
    session.push_game_action(window)
    assert session.action_stack.update() is False
    seen = []
    session.set_attack_transaction_resolver(
        lambda tx: seen.append(tx) or True)
    tx = RulesTransaction.commit_troops_to_attack(
        "p", game_engine.ETurnPhases.DeclareAttack, ((99, (10,)),))
    assert session.submit_transaction(tx)
    assert session.handle_transaction()
    assert seen
    assert window.priority_player_id is None
    for _ in range(8):
        if not session.tick():
            break
    assert session.current_turn_phase == (
        game_engine.ETurnPhases.DeclareAttackPriorityWindow)


def test_commit_defense_transaction_validates_atomically_then_sets_blockers():
    attacker = type("Card", (), {"session_card_id": 10})()
    defender = type("Card", (), {"session_card_id": 99})()
    blocker = type("Card", (), {"session_card_id": 20})()
    facts = type("Facts", (), {
        "get_card": lambda self, card_id: {10: attacker, 99: defender,
                                            20: blocker}.get(card_id),
        "validate_blocks": lambda self, session, declarations, player: (
            player == "d" and declarations == ((10, (20,)),)),
    })()
    session = AuthoritativeSession(55, ("p", "d"), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.DeclareDefense
    session.set_runtime_facts(facts)
    combat = session.combat_manager.create_attack(CombatId(), "p", defender)
    combat.declare_attacker(attacker)
    declaration = ((10, (20,)),)
    assert DefenseDeclarationsLegalRequirement(declaration).is_valid(session, "d")
    tx = RulesTransaction.commit_troops_to_defense(
        "d", game_engine.ETurnPhases.DeclareDefense, declaration)
    assert session.submit_transaction(tx)
    assert session.handle_transaction()
    assert combat.blockers == [blocker]


def test_card_play_and_manual_activation_transactions_use_injected_mutators():
    card = type("Card", (), {})()
    facts = type("Facts", (), {
        "get_card": lambda self, card_id: card if card_id == 7 else None,
        "can_play_card": lambda self, value, player, free: value is card and player == "p",
        "can_activate_ability": lambda self, value, player, ability: (
            value is card and player == "p" and ability == "ability"),
        "validate_x_cost": lambda self, player, data: player == "p",
    })()
    session = AuthoritativeSession(55, ("p",), seed_z=1, seed_w=2)
    session.set_runtime_facts(facts)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.action_stack.priority_player_id = "p"
    resolved = []
    session.set_card_transaction_resolver(
        lambda kind, transaction: resolved.append((kind, transaction.payload)) or True)
    resource = RulesTransaction.play_resource("p", 7)
    assert session.submit_transaction(resource)
    assert session.handle_transaction()
    troop = RulesTransaction.play_troop("p", 7, ({"opted": False},))
    assert session.submit_transaction(troop)
    assert session.handle_transaction()
    ability = RulesTransaction.activate_ability(
        "p", 7, "ability", {"opted": False})
    assert session.submit_transaction(ability)
    assert session.handle_transaction()
    assert [item[0] for item in resolved] == ["play_resource", "play_troop",
                                               "activate_ability"]


def test_card_play_transactions_require_authoritative_phase_and_priority():
    card = object()
    facts = type("Facts", (), {
        "get_card": lambda self, card_id: card,
        "can_play_card": lambda self, value, player, free: value is card,
    })()
    session = AuthoritativeSession(56, ("p", "o"), seed_z=1, seed_w=2)
    session.set_runtime_facts(facts)
    session.current_turn_phase = game_engine.ETurnPhases.DeclareCombatPriorityWindow
    session.action_stack.priority_player_id = "p"
    assert not session.submit_transaction(RulesTransaction.play_resource("p", 7))
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.action_stack.priority_player_id = "o"
    assert not session.submit_transaction(RulesTransaction.play_troop("p", 7))


def test_discard_transaction_uses_rules_port_resolver():
    card = type("Card", (), {"collection": game_engine.ECardCollections.Hand})()
    facts = type("Facts", (), {
        "get_card": lambda self, card_id: card if card_id == 7 else None,
    })()
    session = AuthoritativeSession(57, ("p",), seed_z=1, seed_w=2)
    session.set_runtime_facts(facts)
    session.current_turn_phase = game_engine.ETurnPhases.Discard
    seen = []
    session.set_discard_transaction_resolver(
        lambda tx: seen.append(tx.payload["card_id"]) or True)
    assert session.submit_transaction(RulesTransaction.discard("p", 7))
    assert session.handle_transaction()
    assert seen == [7]


def test_quit_transaction_uses_host_projection_resolver():
    session = AuthoritativeSession(58, ("p", "o"), seed_z=1, seed_w=2)
    seen = []
    session.set_quit_game_resolver(
        lambda tx: seen.append(tx.player_id) or True)
    tx = RulesTransaction.quit_game("p")
    assert session.submit_transaction(tx)
    assert session.handle_transaction()
    assert seen == ["p"]
    assert session.is_player_eliminated("p")


def test_manual_activation_rejects_triggered_or_nonmanual_templates():
    from rules_port import AbilityHasTriggerRequirement, AbilityIsManuallyActivatedRequirement
    session = type("S", (), {})()
    session.ability_has_trigger = lambda template: template == "triggered"
    session.ability_is_manual = lambda template: template == "manual"
    assert AbilityHasTriggerRequirement("triggered").is_valid(session, "p")
    assert AbilityHasTriggerRequirement("manual").is_valid(session, "p") is False
    assert AbilityIsManuallyActivatedRequirement("manual").is_valid(session, "p")
    assert not AbilityIsManuallyActivatedRequirement("triggered").is_valid(session, "p")


def test_triggered_activation_uses_queue_and_batch_validation_adapters():
    data = ({"opted": False},)
    session = AuthoritativeSession(92, ("p",), seed_z=1, seed_w=2)
    session.set_runtime_facts(type("Facts", (), {
        "is_at_triggered_ability_queue_front": lambda self, player: player == "p",
        "validate_triggered_abilities": lambda self, player, values: player == "p" and values == data,
    })())
    tx = RulesTransaction.activate_triggered_abilities("p", data)
    assert tx.validate(session)
    session.set_triggered_ability_transaction_resolver(lambda value: True)
    session.submit_transaction(tx)
    assert session.handle_transaction()


def test_triggered_activation_has_native_chain_lifecycle_without_adapter():
    class TriggeredAbility:
        instance_id = 4
        responsible_player_id = "p"
        paid = False

        def bind_activation(self, value):
            self.activation = value
            return True

        def continuation(self):
            return {}

    session = AuthoritativeSession(93, ("p",), seed_z=1, seed_w=2)
    session.set_runtime_facts(type("Facts", (), {
        "is_at_triggered_ability_queue_front": lambda self, player: True,
        "validate_triggered_abilities": lambda self, player, values: True,
    })())
    ability = TriggeredAbility()
    session.ability_manager.add_chain(ability.instance_id, ability)
    tx = RulesTransaction.activate_triggered_abilities(
        "p", ({"ability_instance_id": 4, "opted": False},))
    assert session.submit_transaction(tx)
    assert session.handle_transaction()
    assert session.chain.contains_ability(4)


def test_additional_csharp_requirement_predicates_use_session_facts():
    from rules_port import (CardUntappedRequirement, PlayerHasResourceCountRequirement,
                            SessionCardAttributesRequirement,
                            SpellAllowedInTurnPhaseRequirement)
    card = type("Card", (), {"tapped": False, "is_spell": True, "quick_action": False})()
    player = type("Player", (), {"current_resource_pool": 4})()
    session = AuthoritativeSession(94, ("p",), seed_z=1, seed_w=2)
    session.set_runtime_facts(type("Facts", (), {
        "get_card": lambda self, value: card if value == 7 else None,
        "get_player": lambda self, value: player if value == "p" else None,
    })())
    session.current_attribute_restrictions = 8
    session.action_stack.priority_player_id = "p"
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    assert CardUntappedRequirement(7).is_valid(session, "p")
    assert PlayerHasResourceCountRequirement("p", 3).is_valid(session, "p")
    assert SessionCardAttributesRequirement(8).is_valid(session, "p")
    assert SpellAllowedInTurnPhaseRequirement("p", 7).is_valid(session, "p")


def test_ability_cost_requirement_delegates_template_costs_to_runtime_facts():
    from rules_port import AbilityCostRequirement
    session = AuthoritativeSession(95, ("p",), seed_z=1, seed_w=2)
    session.set_runtime_facts(type("Facts", (), {
        "ability_cost_is_paid": lambda self, player, template: player == "p" and template == "a",
    })())
    assert AbilityCostRequirement("p", "a").is_valid(session, "p")
    assert not AbilityCostRequirement("p", "b").is_valid(session, "p")


def test_blocks_are_valid_requires_all_blocking_cards_and_delegates_legality():
    from rules_port import BlocksAreValidRequirement
    card = object()
    session = AuthoritativeSession(96, ("p",), seed_z=1, seed_w=2)
    session.set_runtime_facts(type("Facts", (), {
        "get_card": lambda self, value: card if value == 2 else None,
    })())
    session.are_blocks_legal = lambda combats, cards: combats == (4,) and cards == (card,)
    assert BlocksAreValidRequirement((4,), (2,)).is_valid(session, "p")
    assert not BlocksAreValidRequirement((4,), (99,)).is_valid(session, "p")


def test_composable_card_filters_match_client_zone_type_and_controller_rules():
    from rules_port import AndCardFilter, InZone, IsControlledBy, IsTapped, IsType, NotCardFilter
    card = {"collection": 2, "card_type": 4, "tapped": True, "controller_id": "p"}
    assert AndCardFilter((InZone(2), IsType(4), IsTapped(True),
                          IsControlledBy())).matches(card, player="p", source=object())
    assert NotCardFilter(IsTapped()).matches(card, player="p") is False


def test_metadata_filters_cover_attributes_cost_keywords_and_flags():
    from rules_port import (HasAnyAttributeFlags, HasCastingCost, HasKeywordAbility,
                            InCollection, IsQuick, IsResource, IsToken)
    card = {"collection": 4, "attributes": 8, "casting_cost": 3,
            "abilities": ("Swiftstrike",), "is_token": True,
            "is_resource": True, "quick_action": True}
    assert InCollection(4).matches(card)
    assert HasAnyAttributeFlags(8).matches(card)
    assert HasCastingCost(3).matches(card)
    assert HasKeywordAbility("swiftstrike").matches(card)
    assert IsToken().matches(card) and IsResource().matches(card) and IsQuick().matches(card)


def test_name_and_rarity_filters_follow_case_insensitive_client_matching():
    from rules_port import HasName, IsRarity
    source = {"name": "Ruby Dragon"}
    card = {"name": "Ancient Ruby Dragon", "rarity": "Rare"}
    assert HasName("ruby dragon").matches(card)
    assert HasName("<this>").matches(card, source=source)
    assert IsRarity("Rare").matches(card)


def test_inverse_and_turn_history_filters_preserve_client_flags():
    from rules_port import (IsNotControlledBy, IsPlayedThisTurn,
                            IsDamagedThisTurn, IsHealedThisTurn)
    card = {"controller_id": "p", "played_this_turn": True,
            "damaged_this_turn": True, "healed_this_turn": True}
    assert IsNotControlledBy().matches(card, player="q")
    assert IsPlayedThisTurn().matches(card)
    assert IsDamagedThisTurn().matches(card)
    assert IsHealedThisTurn().matches(card)


def test_numeric_stat_filters_use_client_comparisons_and_source_context():
    from rules_port import HasResourceCost, HasAttackValue, HasDefenseValue
    card, source = {"resource_cost": 4, "attack": 3, "defense": 2}, {"attack": 2, "defense": 3}
    assert HasResourceCost(3, "GreaterThan").matches(card)
    assert HasAttackValue(0, "OneMoreThan", True).matches(card, source=source)
    assert HasDefenseValue(0, "OneLessThan", True).matches(card, source=source)


def test_color_and_faction_filters_use_normalized_bit_flags():
    from rules_port import IsColor, InFaction
    card = {"color_flags": 5, "faction_flags": 8}
    assert IsColor(1).matches(card)
    assert not IsColor(2).matches(card)
    assert InFaction(8).matches(card)


def test_type_and_print_variant_filters_use_runtime_flags():
    from rules_port import IsNotType, IsAlternateArt, IsExtendedArt, IsPromo
    card = {"card_type": 2, "alternate_art": True, "extended_art": True, "promo": True}
    assert IsNotType(4).matches(card)
    assert IsAlternateArt().matches(card)
    assert IsExtendedArt().matches(card)
    assert IsPromo().matches(card)


def test_socket_filters_use_socket_counts_and_comparison_metadata():
    from rules_port import IsSocketable, IsSocketed
    card = {"socket_count": 2, "socketed_count": 1}
    assert IsSocketable(2).matches(card)
    assert IsSocketed().matches(card)


def test_tag_subtype_and_threshold_filters_match_client_metadata():
    from rules_port import HasTag, IsSubType, IsMultiThresholdCard
    card = {"tags": ("Construct",), "subtype": "Dwarf Robot",
            "thresholds": ({"color": "ruby"}, {"color": "diamond"})}
    assert HasTag("construct").matches(card)
    assert IsSubType("robot").matches(card)
    assert IsSubType("*").matches(card)
    assert IsMultiThresholdCard().matches(card)


def test_filter_factory_builds_nested_records_metadata_and_rejects_unknown_types():
    from rules_port import filter_from_metadata
    filt = filter_from_metadata({"type": "AndCardFilter", "filters": [
        {"class": "InZone", "collection": 2},
        {"filter_type": "IsTapped", "value": False},
    ]})
    assert filt.matches({"collection": 2, "tapped": False})
    try:
        filter_from_metadata({"type": "UnknownFilter"})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown filter type was silently accepted")


def test_filter_factory_supports_fixed_type_and_combat_aliases():
    from rules_port import filter_from_metadata
    card = {"card_type": 2, "is_attacking": True}
    assert filter_from_metadata({"type": "IsTroop"}).matches(card)
    assert filter_from_metadata({"type": "IsAttacking"}).matches(card)


def test_factories_accept_serialized_m_prefixed_fields():
    from rules_port import filter_from_metadata, requirement_from_metadata
    assert filter_from_metadata({"type": "InZone", "m_Collection": 2}).matches({"collection": 2})
    requirement = requirement_from_metadata({"type": "TurnPhaseRequirement", "m_RequiredPhase": "Ready"})
    session = AuthoritativeSession(103, ("p",), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.Ready
    assert requirement.is_valid(session, "p")
    assert filter_from_metadata({"type": "IsType", "m_CardType": 2}).matches({"card_type": 2})
    assert requirement_from_metadata({"type": "CardUntapped", "card_id": 9})


def test_filtered_target_adapter_reuses_csharp_filter_for_options_and_validation():
    from rules_port import FilteredTargetAdapter, IsType
    troop, artifact = {"id": 1, "card_type": 2}, {"id": 2, "card_type": 4}
    adapter = FilteredTargetAdapter(IsType(2), lambda: (troop, artifact))
    assert adapter.candidates() == (troop,)
    assert adapter.validate((troop, artifact)) == (troop,)


def test_target_factory_builds_filter_backed_target_from_normalized_metadata():
    from rules_port import target_from_metadata
    adapter = target_from_metadata(
        {"filter": {"type": "IsArtifact"}},
        lambda: ({"card_type": 4}, {"card_type": 2}))
    assert len(adapter.candidates()) == 1


def test_requirement_factory_builds_nested_phase_and_priority_contracts():
    from rules_port import requirement_from_metadata
    requirement = requirement_from_metadata({"type": "AndRequirement", "requirements": [
        {"class": "TurnPhaseRequirement", "phase": "Ready"},
        {"type": "PlayerIsActiveRequirement", "player_id": "p"},
    ]})
    session = AuthoritativeSession(101, ("p",), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.Ready
    session.active_player_id = "p"
    assert requirement.is_valid(session, "p")


def test_client_comparison_enum_numeric_order_matches_ecomparisons():
    from rules_port.transactions import _compare
    assert _compare(1, 0, 2)       # LessThan
    assert _compare(2, 1, 2)       # LessThanOrEqual
    assert _compare(3, 2, 2)       # GreaterThan
    assert _compare(2, 3, 2)       # GreaterThanOrEqual
    assert _compare(2, 4, 2)       # Equals
    assert _compare(3, 6, 2)       # OneMoreThan
    assert _compare(4, 7, 2)       # TwoMoreThan
    assert _compare(1, 8, 2)       # OneLessThan


def test_ecomparisons_enum_matches_client_numeric_contract():
    from rules_port import EComparisons
    assert EComparisons.LessThan == 0
    assert EComparisons.Equals == 4
    assert EComparisons.OneLessThan == 8


def test_wire_normalizer_maps_existing_classification_without_reparsing_payloads():
    player = game_engine.UID.make(244, 1)
    command = SimpleNamespace(is_pass_priority=True, pass_turn_phase=None,
                              inner_bytes=b"PassPriorityTransaction")
    tx = normalize_player_transaction(command, player,
                                      current_phase=game_engine.ETurnPhases.Draw)
    assert tx.kind == "pass_priority"
    assert tx.phase == game_engine.ETurnPhases.Draw
    choose = SimpleNamespace(is_choose_pick=True, pass_turn_phase=None,
                             inner_bytes=b"ChooseDrawTransaction")
    assert normalize_player_transaction(choose, player,
        current_phase=game_engine.ETurnPhases.PickGoesFirst).kind == "choose_draw_first"
    incomplete = SimpleNamespace(is_commit_attack=True, pass_turn_phase=None,
                                 inner_bytes=b"CommitTroopsToAttackTransaction")
    assert normalize_player_transaction(incomplete, player) is None
    numeric = SimpleNamespace(is_pass_priority=True, pass_turn_phase=9,
                              inner_bytes=b"PassPriorityTransaction")
    assert normalize_player_transaction(numeric, player).phase == (
        game_engine.ETurnPhases.Draw)
    unknown = SimpleNamespace(is_pass_priority=True, pass_turn_phase=999,
                              inner_bytes=b"PassPriorityTransaction")
    assert normalize_player_transaction(unknown, player).phase == (
        game_engine.ETurnPhases.Unknown)
    sync = SimpleNamespace(is_priority_sync=True, pass_turn_phase=None,
                           inner_bytes=b"RequestPrioritySyncTransaction")
    assert normalize_player_transaction(sync, player).kind == "request_priority_sync"
    auto = SimpleNamespace(is_set_auto_pass=True, pass_turn_phase=None,
                           inner_bytes=b"SetAutoPassTransaction")
    assert normalize_player_transaction(auto, player) is None


def test_wire_normalizer_maps_manual_activate_ability_typed_payload():
    player = game_engine.UID.make(244, 1)
    command = SimpleNamespace(is_ability_activate=True, pass_turn_phase=None,
                              inner_bytes=b"ActivateAbilityTransaction")
    payload = {"source_card_id": 77, "ability_template_id": "ability-guid",
               "activation_data": {"target": 12}, "ability_instance_id": 9}
    tx = normalize_player_transaction(command, player, payload=payload)
    assert tx.kind == "activate_ability"
    assert tx.payload == {"source_card_id": 77,
                          "ability_template_id": "ability-guid",
                          "activation_data": {"target": 12},
                          "ability_instance_id": 9}
    assert normalize_player_transaction(command, player,
                                        payload={"source_card_id": 77}) is None


def test_wire_normalizer_uses_classifier_owned_typed_payload_by_default():
    player = game_engine.UID.make(244, 1)
    command = SimpleNamespace(is_play_resource=True, typed_payload={"card_id": 77},
                              pass_turn_phase=None, inner_bytes=b"")
    tx = normalize_player_transaction(command, player)
    assert tx.kind == "play_resource" and tx.payload["card_id"] == 77


def test_wire_normalizer_maps_triggered_ability_batch_payload():
    player = game_engine.UID.make(244, 1)
    command = SimpleNamespace(is_activate_triggered_abilities=True,
                              pass_turn_phase=None, inner_bytes=b"")
    data = ({"ability_instance_id": 3, "target_map": {"0": [19]}},)
    tx = normalize_player_transaction(command, player,
                                      payload={"activation_data": data})
    assert tx.kind == "activate_triggered_abilities"
    assert tx.payload["activation_data"] == data


def test_wire_normalizer_maps_play_card_transactions_from_typed_payload():
    player = game_engine.UID.make(244, 1)
    command = SimpleNamespace(is_play_troop=True, is_play_resource=False,
                              is_play_artifact=False, is_play_spell=False,
                              is_play_champion=False, pass_turn_phase=None,
                              inner_bytes=b"PlayTroopTransaction")
    tx = normalize_player_transaction(
        command, player,
        payload={"card_id": 41, "ability_data": ({"x_cost": 2},),
                 "playing_for_free": True})
    assert tx.kind == "play_troop"
    assert tx.payload["card_id"] == 41
    assert tx.payload["playing_for_free"] is True


def test_wire_normalizer_accepts_classifier_set_stops_name():
    player = game_engine.UID.make(244, 1)
    command = SimpleNamespace(is_set_stops=True, pass_turn_phase=None,
                              inner_bytes=b"SetTurnPhasesTransaction")
    tx = normalize_player_transaction(
        command, player,
        payload={"self_phases": (1, 2), "opponent_phases": (3,)})
    assert tx.kind == "set_turn_phases"
    assert tx.payload["self_phases"] == (1, 2)


def test_common_client_filter_primitives_are_metadata_constructible():
    from rules_port.filters import filter_from_metadata
    card = {"name": "Alpha Troop", "is_basic": True,
            "is_unique": False, "attacked_this_turn": True}
    assert filter_from_metadata({"type": "AnyCard"}).matches(card)
    assert filter_from_metadata({"type": "IsBasic"}).matches(card)
    assert filter_from_metadata({"type": "IsCardName", "name": "alpha troop"}).matches(card)
    assert filter_from_metadata({"type": "NameContainsFilter", "value": "troop"}).matches(card)
    assert filter_from_metadata({"type": "HasAttackedThisTurn"}).matches(card)


def test_combat_and_source_filter_primitives_use_runtime_relationships():
    from rules_port.filters import filter_from_metadata
    source = {"session_card_id": 1, "card_type": 2}
    card = {"session_card_id": 2, "card_type": 2,
            "is_blocking": True, "is_blocked": True}
    assert filter_from_metadata({"type": "OtherTroops"}).matches(card, source=source)
    assert filter_from_metadata({"type": "BlockingFilter"}).matches(card)
    assert filter_from_metadata({"type": "BeingBlockedByFilter"}).matches(card)
    assert filter_from_metadata({"type": "IsAbilitySource"}).matches(source, source=source)


def test_compare_attack_and_defense_filter_matches_client_direction_flags():
    from rules_port.filters import filter_from_metadata
    card = {"attack": 3, "defense": 2}
    source = {"attack": 5, "defense": 4}
    assert filter_from_metadata({"type": "CompareAttackAndDefenseFilter",
                                "comparison": "GreaterThan"}).matches(card)
    assert filter_from_metadata({"type": "CompareAttackAndDefenseFilter",
                                "comparison": "LessThan",
                                "compare_to_ability_source_defense": True}).matches(card, source=source)


def test_runtime_flag_filters_cover_pve_transformed_equipped_and_stored_cards():
    from rules_port.filters import filter_from_metadata
    card = {"is_pve": True, "is_transformed": True,
            "is_equipped": True, "is_stored": True, "is_mercenary": True,
            "color_flags": 4, "is_prismatic": True}
    for kind in ("IsPvECard", "IsTranformed", "IsEquippedCardFilter",
                 "IsStoredCardFilter", "IsMercenaryFilter"):
        assert filter_from_metadata({"type": kind}).matches(card)
    assert filter_from_metadata({"type": "IsColor_DeckBuilder",
                                "color": 4, "prismatic": True}).matches(card)
    extracted = filter_from_metadata({"type": "IsColor_DeckBuilder",
                                      "m_ColorFlags": 4, "m_Prismatic": True})
    assert extracted.matches(card)


def test_attack_extrema_filters_use_explicit_runtime_card_collection():
    from rules_port.filters import filter_from_metadata
    session = type("S", (), {"cards": ({"attack": 2}, {"attack": 5})})()
    low = filter_from_metadata({"type": "CompareAttackToLowestFilter",
                                "comparison": "Equals"})
    high = filter_from_metadata({"type": "CompareAttackToHighestFilter",
                                 "comparison": "Equals"})
    assert low.matches({"attack": 2}, session=session)
    assert high.matches({"attack": 5}, session=session)


def test_defense_and_champion_health_extrema_filters():
    from rules_port.filters import filter_from_metadata
    session = type("S", (), {"cards": (
        {"card_type": 1, "health": 8, "defense": 8},
        {"card_type": 1, "health": 12, "defense": 12},
    )})()
    assert filter_from_metadata({"type": "CompareDefenseToLowestFilter"}).matches(
        {"defense": 8}, session=session)
    assert filter_from_metadata({"type": "CompareHealthToHighestFilter"}).matches(
        {"card_type": 1, "health": 12}, session=session)


def test_resource_cost_highest_filter_uses_runtime_candidates():
    from rules_port.filters import filter_from_metadata
    session = type("S", (), {"cards": ({"resource_cost": 1}, {"resource_cost": 4})})()
    filt = filter_from_metadata({"type": "CompareResourceCostToHighestFilter"})
    assert filt.matches({"resource_cost": 4}, session=session)


def test_resource_cost_my_highest_filter_scopes_to_source_owner():
    from rules_port.filters import filter_from_metadata
    source = {"owner_id": 1}
    session = type("S", (), {"cards": (
        {"owner_id": 1, "resource_cost": 3},
        {"owner_id": 2, "resource_cost": 9},
    )})()
    filt = filter_from_metadata({"type": "CompareResourceCostToMyHighestFilter"})
    assert filt.matches({"owner_id": 1, "resource_cost": 3},
                        session=session, source=source)


def test_source_relative_filters_compare_runtime_card_metadata():
    from rules_port.filters import filter_from_metadata
    source = {"card_type": 2, "resource_cost": 4, "casting_cost": 5,
              "template_guid": "source"}
    card = {"card_type": 2, "resource_cost": 3, "casting_cost": 5,
            "template_guid": "other"}
    assert filter_from_metadata({"type": "HasSourceTypeFilter"}).matches(card, source=source)
    assert filter_from_metadata({"type": "HasSourceResourceCost",
                                "comparison": "LessThan"}).matches(card, source=source)
    assert filter_from_metadata({"type": "HasSourceCastingCostFilter"}).matches(card, source=source)


def test_shared_source_filters_and_owner_filter():
    from rules_port.filters import filter_from_metadata
    source = {"owner_id": 1, "faction": 3, "rarity": "Rare", "subtypes": ("Elf",)}
    card = {"owner_id": 2, "faction": 1, "rarity": "Rare", "subtypes": ("Elf", "Warrior")}
    assert filter_from_metadata({"type": "DifferentOwners"}).matches(card, source=source)
    # EFactions is a plain enum: sharing is equality, not bitwise overlap.
    assert not filter_from_metadata(
        {"type": "HasASharedFactionWithSourceFilter"}).matches(card, source=source)
    assert filter_from_metadata(
        {"type": "HasASharedFactionWithSourceFilter"}).matches(
            dict(card, faction=3), source=source)
    assert filter_from_metadata({"type": "HasASharedRarityWithSourceFilter"}).matches(card, source=source)
    assert filter_from_metadata({"type": "HasASharedSubtypeWithSourceFilter"}).matches(card, source=source)


def test_source_relationship_and_damage_filters():
    from rules_port.filters import filter_from_metadata
    source = {"session_card_id": 7}
    card = {"moved_by_source_id": 7, "parent_id": 7,
            "stats_this_turn": {"combat_damage_dealt_to_opponent": 2}}
    assert filter_from_metadata({"type": "MovedBySource"}).matches(card, source=source)
    assert filter_from_metadata({"type": "IsChildOfAbilitySource"}).matches(card, source=source)
    assert filter_from_metadata({"type": "DamagedOpponentThisTurn",
                                "only_combat_damage": True}).matches(card)


def test_shared_shard_and_champion_metadata_filters():
    from rules_port.filters import filter_from_metadata
    source = {"shards": 3, "classes": ("Warrior",), "subtypes": ("Elf",)}
    card = {"shards": 1, "classes": ("Warrior",), "subtypes": ("Elf",)}
    assert filter_from_metadata({"type": "HasASharedShardWithSourceFilter"}).matches(card, source=source)
    assert filter_from_metadata({"type": "HasASharedClassWithSourceChampionFilter"}).matches(card, source=source)
    assert filter_from_metadata({"type": "HasASharedSubtypeWithSourceChampionFilter"}).matches(card, source=source)
    session = type("S", (), {"top_of_chain": source})()
    assert filter_from_metadata({"type": "HasASharedShardWithTopOfChainFilter"}).matches(card, session=session)


def test_all_attribute_flags_requires_every_requested_bit():
    from rules_port.filters import filter_from_metadata
    filt = filter_from_metadata({"type": "HasAllAttributeFlags",
                                 "m_AttributeFlags": 0b0110})
    assert filt.matches({"attributes": 0b1110})
    assert not filt.matches({"attributes": 0b0010})


def test_casting_cost_source_counter_filter_uses_named_counter():
    from rules_port.filters import filter_from_metadata
    filt = filter_from_metadata({"type": "CompareCastingCostToSourceCountersFilter",
                                 "comparison": "Equals", "counter_type": "charge"})
    source = {"counters": {"charge": 3}}
    assert filt.matches({"casting_cost": 3}, source=source)
    assert not filt.matches({"casting_cost": 2}, source=source)


def test_has_counters_value_compares_card_counter_amount():
    from rules_port.filters import filter_from_metadata
    filt = filter_from_metadata({"type": "HasCountersValue",
                                 "m_Amount": 2, "m_CounterType": "charge",
                                 "m_ComparisonOp": "GreaterThanOrEqual"})
    assert filt.matches({"counters": {"charge": 3}})
    assert not filt.matches({"counters": {"charge": 1}})


def test_attribute_and_set_filters_read_nested_runtime_metadata():
    from rules_port.filters import filter_from_metadata
    card = {"stats": {"power": 4}, "name": "Alpha", "set_id": "set-a", "set_number": 7}
    assert filter_from_metadata({"type": "IntAttrFilter", "attribute": "stats>power",
                                "value": 4}).matches(card)
    assert filter_from_metadata({"type": "StringAttrFilter", "attribute": "name",
                                "value": "Alpha"}).matches(card)
    assert filter_from_metadata({"type": "SetIdFilter", "set_id": "set-a"}).matches(card)
    assert filter_from_metadata({"type": "SetNumberFilter", "set_number": 7}).matches(card)


def test_target_top_deck_and_tac_filters_use_explicit_context():
    from rules_port.filters import filter_from_metadata
    card = {"name": "Alpha", "is_quick_action": True}
    effect = {"targets": {"0": ({"name": "Alpha"},)}}
    assert filter_from_metadata({"type": "MatchesTargetFilter"}).matches(card, effect=effect)
    session = type("S", (), {"deck_top": (card,)})()
    assert filter_from_metadata({"type": "TopNOfDeck", "amount": 1}).matches(card, session=session)
    assert filter_from_metadata({"type": "TACFilter", "template": "IsQuick"}).matches(card)


def test_threshold_tac_filter_uses_player_thresholds():
    from rules_port.filters import filter_from_metadata
    player = type("P", (), {"resource_thresholds": {"ruby": 2}})()
    card = {"thresholds": ({"color": "ruby", "quantity": 2},)}
    filt = filter_from_metadata({"type": "PlayerMeetsThresholdRequirementsToCast"})
    assert filt.matches(card, player=player)


def test_players_who_control_matching_filter_counts_controller_cards():
    from rules_port.filters import filter_from_metadata
    session = type("S", (), {"cards": (
        {"owner_id": 4, "is_troop": True}, {"owner_id": 4, "is_troop": True},
        {"owner_id": 9, "is_troop": True})})()
    card = {"owner_id": 4, "card_type": 1}
    filt = filter_from_metadata({"type": "PlayersWhoControlMatchingFilter",
                                 "required_quantity": 2,
                                 "comparison": "GreaterThanOrEqual"})
    assert filt.matches(card, session=session)


def test_wire_bridge_submits_classified_intent_against_port_phase():
    player = game_engine.UID.make(244, 1)
    session = AuthoritativeSession(55, (player,), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.action_stack.priority_player_id = player
    command = SimpleNamespace(is_pass_priority=True, pass_turn_phase=None,
                              inner_bytes=b"PassPriorityTransaction")
    assert submit_classified_transaction(session, command, player)
    assert session._transactions[0].kind == "pass_priority"
    assert session.handle_transaction() is False  # no PriorityWindowAction yet


def test_wire_card_uid_decoder_preserves_declared_order_and_filters_uid_type():
    raw = (b"CommitTroopsToAttackTransaction;"
           b"m_UID64;0;0;0;010000000000000A;"
           b"m_UID64;0;0;0;010000000000000B;"
           b"m_UID64;0;0;0;020000000000000C;")
    assert extract_session_card_uids(raw) == (0x0A00000000000001,
                                               0x0B00000000000001)
    assert extract_session_card_uids(raw, exclude=(0x0A00000000000001,)) == (
        0x0B00000000000001,)


def test_wire_card_uid_decoder_accepts_value_wrapped_session_card_id():
    """Client choice answers may wrap the UID scalar in ``value``."""
    raw = (b"m_Targets;1;1;0;Target;1;1;1;value;m_UID64;2;2;0;"
           b"0101000000000000;")
    assert extract_session_card_uids(raw) == (0x101,)


def test_card_move_mutation_adapter_maps_known_collection_through_pvp_api():
    calls = []
    api = SimpleNamespace(db_set_card_location=lambda *args: calls.append(args))
    adapter = SQLiteCardMutationAdapter(77, pvp_api=api)
    event = SimpleNamespace(
        session_card_id=SimpleNamespace(uid=SimpleNamespace(uid64=0x010000000000000A)),
        collection=game_engine.ECardCollections.Hand,
    )
    assert adapter.apply_card_moved(event)
    assert calls == [(77, 0x010000000000000A, "hand")]
    event.collection = game_engine.ECardCollections.Underground
    assert adapter.apply_card_moved(event)
    assert calls[-1] == (77, 0x010000000000000A, "underground")
    event.collection = 999
    assert not adapter.apply_card_moved(event)


def test_event_sink_persists_card_move_before_publishing_client_event():
    player = game_engine.UID.make(244, 1)
    game = game_engine.Game(59, player, game_engine.UID.make(3, 2))
    seen = []
    sink = GameEngineEventSink(game, mutation_adapter=lambda event: seen.append(event) or True)
    card = game_engine.SessionCardId(game_engine.UID(0x010000000000000A))
    assert sink.card_moved(card, player, game_engine.ECardCollections.Hand)
    assert seen == [game.events[-1]]
    assert isinstance(game.events[-1], game_engine.CardMovedSessionEventArgs)


def test_event_sink_observer_receives_events_in_wire_emission_order():
    player = game_engine.UID.make(244, 38)
    game = game_engine.Game(67, player, game_engine.UID.make(3, 2))
    observed = []
    sink = GameEngineEventSink(game, event_observer=observed.append)
    sink.green_light(player)
    sink.player_wishes_to_draw_first(player)
    assert observed == game.events


def test_async_event_publisher_forwards_existing_game_event_records():
    async def scenario():
        player = game_engine.UID.make(244, 39)
        game = game_engine.Game(68, player, game_engine.UID.make(3, 2))
        bus = AsyncUIEventBus()
        received = []
        bus.subscribe("GreenLightSessionEventArgs",
                      lambda event_type, payload: received.append(payload))
        sink = GameEngineEventSink(game, event_observer=AsyncEventPublisher(bus))
        sink.green_light(player)
        await asyncio.sleep(0)
        return received

    received = asyncio.run(scenario())
    assert received[0]["type"] == "GreenLightSessionEventArgs"


def test_phase_state_entry_queues_trigger_and_priority_action():
    player = game_engine.UID.make(244, 1)
    ai = game_engine.UID.make(3, 2)
    session = AuthoritativeSession(55, (player, ai), seed_z=1, seed_w=2)
    session.transition_to(game_engine.ETurnPhases.PreGame)
    assert session._trigger_events
    assert session._trigger_events[0].phase == game_engine.ETurnPhases.PreGame
    assert session.action_stack.count == 1
    session.transition_to(game_engine.ETurnPhases.PickGoesFirst)
    # StartTurn has the C# non-resolving phase-specific counter behavior.
    session.current_turn_phase = game_engine.ETurnPhases.StartGame
    session.transition_to(game_engine.ETurnPhases.StartTurn)
    assert session.total_turns_taken == 1


def test_phase_entry_projection_runs_before_native_priority_window():
    player = game_engine.UID.make(244, 46)
    session = AuthoritativeSession(76, (player,), seed_z=1, seed_w=2)
    seen = []
    session.set_turn_phase_entry_resolver(
        lambda phase: seen.append((phase, session.action_stack.count)))
    session.current_turn_phase = game_engine.ETurnPhases.Prep
    session.transition_to(game_engine.ETurnPhases.FirstMainPhase)
    assert seen == [(game_engine.ETurnPhases.FirstMainPhase, 0)]
    assert isinstance(session.action_stack.peek(), PriorityWindowAction)


def test_first_turn_boundary_leaves_mulligan_for_selected_ai_owner():
    player = game_engine.UID.make(244, 77)
    ai = game_engine.UID.make(3, 78)
    session = AuthoritativeSession(177, (player, ai), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.Mulligan
    session.active_player_id = player
    assert session.materialize_current_phase()
    assert session.action_stack.count == 1

    assert session.begin_first_turn(ai)
    assert session.current_turn_phase == game_engine.ETurnPhases.StartTurn
    assert session.active_player_id == ai
    assert session.total_turns_taken == 1
    assert isinstance(session.action_stack.peek(), PriorityWindowAction)
    assert session.action_stack.priority_player_id == ai


def test_phase_state_priority_populations_match_client_state_subclasses():
    states = AuthoritativeSession(55, ("a", "b"), seed_z=1, seed_w=2).phase_states
    assert states["PreGame"].priority_players is TurnPhasePlayers.NONE
    assert states["Ready"].priority_players is TurnPhasePlayers.ALL
    assert states["FirstMainPhase"].priority_players is TurnPhasePlayers.ALL
    assert states["DeclareAttack"].priority_players is TurnPhasePlayers.ACTIVE
    assert states["DeclareDefense"].priority_players is TurnPhasePlayers.DEFENDING
    assert not states["Draw"].chain_can_resolve()
    assert not states["Discard"].chain_can_resolve()


def test_end_turn_rotation_updates_native_active_player():
    """EndTurnState rotates the RulesPort owner before StartTurn entry."""
    session = AuthoritativeSession(56, ("p", "o"), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.EndTurn
    session.active_player_id = "p"
    session.events = []
    seen = []
    session.set_turn_boundary_resolver(lambda active: seen.append(active))
    session.advance_turn_phase()
    assert session.current_turn_phase == game_engine.ETurnPhases.StartTurn
    assert session.active_player_id == "o"
    assert seen == ["o"]


def test_end_turn_rotation_matches_raw_active_uid_to_typed_participants():
    player = game_engine.UID.make(PLAYER_UID_TYPE, 246)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    session = AuthoritativeSession(57, (player, ai), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.EndTurn
    session.active_player_id = int(player.uid64)

    session.advance_turn_phase()

    assert session.current_turn_phase == game_engine.ETurnPhases.StartTurn
    assert session.active_player_id == ai


def test_practice_end_phase_rotates_duplicate_persisted_player_row_to_ai():
    """The live Practice row can contain the human UID in both player slots."""
    player = game_engine.UID.make(PLAYER_UID_TYPE, 246)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    game = SimpleNamespace(player_uid=player, ai_uid=ai)
    persisted = SimpleNamespace(players=[
        (int(player.uid64), 0),
        (int(player.uid64), 0),
    ])
    participants = _native_participant_ids(persisted, game)
    session = AuthoritativeSession(
        58, participants, seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.EndPhase
    session.active_player_id = int(player.uid64)
    state = {
        "player_self_stops": [],
        "player_opp_stops": [int(game_engine.ETurnPhases.EndPhase)],
    }
    session.set_phase_priority_resolver(
        lambda port, _action: practice_phase_priority(
            state, port.current_turn_phase,
            active_player_id=port.active_player_id,
            player_id=player))

    assert session.materialize_current_phase()
    assert session.pass_player_priority(player)
    assert session.pass_player_priority(ai)
    session.drive_until_input()

    assert session.player_ids == (player, ai)
    assert session.current_turn_phase == game_engine.ETurnPhases.StartTurn
    assert session.active_player_id == ai
    assert session.action_stack.priority_player_id == ai


def test_reentrant_checkpoint_cannot_replace_owner_during_turn_transition():
    player = game_engine.UID.make(PLAYER_UID_TYPE, 247)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    session = AuthoritativeSession(
        581, (player, ai), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.EndTurn
    session.active_player_id = player
    phases = [game_engine.ETurnPhases.StartTurn,
              game_engine.ETurnPhases.FirstMainPhase]

    def stale_nested_attach(_phase):
        session.sync_checkpoint(
            phases=phases, phase_idx=1,
            active_player_id=player, client_player_id=player,
            ensure_current_priority=True)

    session.set_turn_phase_entry_resolver(stale_nested_attach)
    session.advance_turn_phase()

    assert session.current_turn_phase == game_engine.ETurnPhases.StartTurn
    assert session.active_player_id == ai
    assert session.action_stack.priority_player_id == ai


def test_end_turn_boundary_can_override_native_rotation_for_bonus_turn():
    session = AuthoritativeSession(62, ("p", "o"), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.EndTurn
    session.active_player_id = "p"
    session.set_turn_boundary_resolver(lambda _active: "p")
    session.advance_turn_phase()
    assert session.current_turn_phase == game_engine.ETurnPhases.StartTurn
    assert session.active_player_id == "p"


def test_pvp_stop_policy_classifies_native_priority_window():
    from rules_port.pvp_session import PvpAuthoritativeSession
    state = {"stops_self_1": [], "stops_opp_2": []}
    assert PvpAuthoritativeSession.priority_players_for_phase(
        state, game_engine.ETurnPhases.FirstMainPhase, 1, 2
    ) is TurnPhasePlayers.ACTIVE
    assert PvpAuthoritativeSession.priority_players_for_phase(
        state, game_engine.ETurnPhases.SecondMainPhase, 1, 2
    ) is TurnPhasePlayers.ALL
    assert PvpAuthoritativeSession.priority_players_for_phase(
        state, game_engine.ETurnPhases.Discard, 1, 2
    ) is TurnPhasePlayers.NONE
    state["discard_required"] = True
    assert PvpAuthoritativeSession.priority_players_for_phase(
        state, game_engine.ETurnPhases.Discard, 1, 2
    ) is TurnPhasePlayers.ACTIVE


def test_pvp_begin_turn_uses_native_phase_graph_after_mulligan():
    from rules_port.pvp_session import PvpAuthoritativeSession
    state = {"pvp": True, "phase": game_engine.ETurnPhases.Mulligan,
             "turn_pid": 1, "stops_self_1": [], "stops_opp_2": []}
    checkpoint = SimpleNamespace(turn_order=state, _persist=lambda **_kwargs: None)
    player = game_engine.UID.make(244, 1)
    opponent = game_engine.UID.make(244, 2)
    session = PvpAuthoritativeSession(
        60, (player, opponent), seed_z=1, seed_w=2,
        snapshot=SQLiteRulesSnapshot(checkpoint))
    session.sync_from_pvp_state(state)
    session.begin_pvp_turn()
    assert session.current_turn_phase == game_engine.ETurnPhases.FirstMainPhase
    assert session.active_player_id == player
    assert session.action_stack.priority_player_id == player


def test_pvp_native_full_pass_cycle_rotates_before_next_first_main():
    from rules_port.pvp_session import PvpAuthoritativeSession
    state = {"pvp": True, "phase": game_engine.ETurnPhases.Mulligan,
             "turn_pid": 1, "stops_self_1": [], "stops_opp_2": []}
    checkpoint = SimpleNamespace(turn_order=state, _persist=lambda **_kwargs: None)
    player = game_engine.UID.make(244, 1)
    opponent = game_engine.UID.make(244, 2)
    session = PvpAuthoritativeSession(
        61, (player, opponent), seed_z=1, seed_w=2,
        snapshot=SQLiteRulesSnapshot(checkpoint))
    session.sync_from_pvp_state(state)
    session.active_player_skips_attack = True
    session.begin_pvp_turn()
    assert session.current_turn_phase == game_engine.ETurnPhases.FirstMainPhase

    # First Main is active-player priority; Second Main is an ALL-player
    # window and therefore requires the two native passes.
    assert session.pass_priority_and_drive(player)
    assert session.current_turn_phase == game_engine.ETurnPhases.SecondMainPhase
    assert session.pass_priority_and_drive(player)
    assert session.action_stack.priority_player_id == opponent
    assert session.pass_priority_and_drive(opponent)
    # End Phase and ordinary Discard are NONE here, so the native scheduler
    # completes cleanup and EndTurn without a service-side phase-list tick.
    assert session.current_turn_phase == game_engine.ETurnPhases.FirstMainPhase
    assert session.active_player_id == opponent


def test_pvp_rehydrated_all_window_drops_already_passed_player():
    from rules_port.pvp_session import PvpAuthoritativeSession
    state = {"pvp": True, "phase": game_engine.ETurnPhases.SecondMainPhase,
             "turn_pid": 1, "priority_pid": 2, "passes": [1],
             "stops_self_1": [], "stops_opp_2": []}
    checkpoint = SimpleNamespace(turn_order=state,
                                 _persist=lambda **_kwargs: None)
    player = game_engine.UID.make(244, 1)
    opponent = game_engine.UID.make(244, 2)
    session = PvpAuthoritativeSession(
        62, (player, opponent), seed_z=1, seed_w=2,
        snapshot=SQLiteRulesSnapshot(checkpoint))
    session.current_turn_phase = game_engine.ETurnPhases.SecondMainPhase
    session.active_player_id = player
    action = PriorityWindowAction(TurnPhasePlayers.ALL)
    session.action_stack.push(action)
    session.configure_phase_priority(action)
    assert list(action._priority_queue) == [opponent]
    assert action.priority_player_id == opponent


def test_pvp_state_save_keeps_native_snapshot_on_same_state_root():
    import services.tournament_game as tournament_game
    old = {"pvp": True, "phase": game_engine.ETurnPhases.SecondMainPhase,
           "turn_pid": 1,
           "rules_port": {"phase": "SecondMainPhase",
                           "active_player_id": 1}}
    new = {"pvp": True, "phase": game_engine.ETurnPhases.FirstMainPhase,
           "turn_pid": 2}
    session = SimpleNamespace(
        _rules_port_battle_state=old, turn_order=old,
        _persist=lambda **_kwargs: None)
    tournament_game.pvp_save_state(session, new)
    assert session._rules_port_battle_state is new
    snapshot = SQLiteRulesSnapshot(session)
    snapshot.save({"phase": "FirstMainPhase"})
    assert session.turn_order is new
    assert new["rules_port"]["phase"] == "FirstMainPhase"


def test_pvp_pass_drives_native_scheduler_after_queue_empty():
    from rules_port.pvp_session import PvpAuthoritativeSession
    session = PvpAuthoritativeSession(57, ("p", "o"), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.active_player_id = "p"
    session.action_stack.push(PriorityWindowAction(TurnPhasePlayers.ALL))
    action = session.action_stack.peek()
    action.reset_priority_window(start_with_active_player=True)
    assert session.pass_priority_and_drive("p")
    assert action.priority_player_id == "o"


def test_pvp_native_state_projection_uses_scheduler_ownership():
    from rules_port.pvp_session import PvpAuthoritativeSession
    player = game_engine.UID.make(244, 1)
    opponent = game_engine.UID.make(244, 2)
    session = PvpAuthoritativeSession(58, (player, opponent), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.SecondMainPhase
    session.active_player_id = opponent
    session.action_stack.priority_player_id = player
    state = {"pvp": True, "phase": 0, "turn_pid": 0}
    assert session.sync_to_pvp_state(state)
    assert state["phase"] == game_engine.ETurnPhases.SecondMainPhase
    assert state["turn_pid"] == 2
    assert state["priority_pid"] == 1


def test_pvp_native_auto_pass_obeys_stop_policy():
    from rules_port.pvp_session import PvpAuthoritativeSession
    player = game_engine.UID.make(244, 1)
    opponent = game_engine.UID.make(244, 2)
    session = PvpAuthoritativeSession(59, (player, opponent), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.active_player_id = player
    session.action_stack.push(PriorityWindowAction(TurnPhasePlayers.ACTIVE))
    action = session.action_stack.peek()
    action.reset_priority_window(start_with_active_player=True)
    state = {"pvp": True, "phase": game_engine.ETurnPhases.FirstMainPhase,
             "stops_self_1": [],
             "stops_opp_1": [game_engine.ETurnPhases.FirstMainPhase]}
    assert not session.auto_pass_waiting_player(
        state, player, has_quick_action=False)
    assert action.priority_player_id == player


def test_phase_state_branching_uses_client_transition_conditions():
    states = AuthoritativeSession(55, ("a", "b"), seed_z=1, seed_w=2).phase_states
    facts = type("Facts", (), {"skip_setup": False, "active_player_skips_draw": False,
                                "has_legal_attackers": False,
                                "combat_has_first_strike": False})()
    assert states["PreGame"].get_next_turn_phase(facts) == "PickGoesFirst"
    facts.skip_setup = True
    assert states["PreGame"].get_next_turn_phase(facts) == "StartGame"
    facts.active_player_skips_draw = True
    assert states["Prep"].get_next_turn_phase(facts) == "FirstMainPhase"
    facts.has_legal_attackers = True
    assert states["DeclareCombatPriorityWindow"].get_next_turn_phase(facts) == "DeclareAttack"
    facts.has_legal_attackers = False
    facts.combat_has_first_strike = True
    assert states["DeclareDefensePriorityWindow"].get_next_turn_phase(facts) == "AssignFirstStrikeDamage"


def test_empty_action_stack_advances_with_the_client_phase_state_machine():
    player = game_engine.UID.make(244, 1)
    session = AuthoritativeSession(55, (player,), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.PreGame
    session.skip_setup = True
    assert session.tick()
    assert session.current_turn_phase == game_engine.ETurnPhases.StartGame
    # StartGame's no-priority action resolves, then its one legal successor
    # starts on the following scheduler tick.
    assert session.tick()
    assert session.tick()
    assert session.current_turn_phase == game_engine.ETurnPhases.StartTurn


def test_parity_capture_compares_existing_event_type_and_field_order():
    player = game_engine.UID.make(244, 1)
    game = game_engine.Game(55, player, game_engine.UID.make(3, 2))
    GameEngineEventSink(game).green_light(player)
    capture = ParityCapture(1, 2, ({"kind": "pass"},),
                            {"phase": "FirstMainPhase"},
                            (event_record(game.events[-1]),))
    compare_captures(capture, ParityCapture.from_dict(capture.to_dict()))


def test_parity_capture_can_be_built_directly_from_rules_session_and_game_events():
    player = game_engine.UID.make(244, 36)
    game = game_engine.Game(64, player, game_engine.UID.make(3, 2))
    session = AuthoritativeSession(64, (player,), seed_z=7, seed_w=8,
                                   event_sink=GameEngineEventSink(game))
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.action_stack.priority_player_id = player
    assert session.submit_transaction(RulesTransaction.pass_priority(
        player, game_engine.ETurnPhases.FirstMainPhase))
    session.send_green_light(player)
    capture = ParityCapture.from_session(session, game)
    assert capture.seed_z == 7 and capture.seed_w == 8
    assert capture.transactions[0]["kind"] == "pass_priority"
    assert capture.events[0]["type"] == "GreenLightSessionEventArgs"


def test_parity_replay_feeds_captured_transactions_through_fresh_session():
    player = game_engine.UID.make(244, 40)
    source = AuthoritativeSession(69, (player,), seed_z=1, seed_w=2)
    source.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    source.register_transaction("test_mutation", lambda tx: True)
    tx = RulesTransaction(player, "test_mutation", None,
                          payload={"value": 3})
    assert source.submit_transaction(tx)
    assert source.handle_transaction()
    capture = ParityCapture.from_session(source)
    replay = AuthoritativeSession(69, (player,), seed_z=1, seed_w=2)
    replay.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    replay.register_transaction("test_mutation", lambda tx: True)
    replay_transactions(replay, capture.transactions,
                        lambda record: RulesTransaction(
                            player, record["kind"], None,
                            payload=record["payload"]))
    assert replay.transaction_history == capture.transactions


def test_async_ui_event_bus_waits_for_late_client_reply_without_blocking_loop():
    import asyncio

    async def scenario():
        bus = AsyncUIEventBus()
        observed = []

        async def dialog(request_id, payload):
            observed.append((request_id, payload))
            await asyncio.sleep(0)
            bus.respond(request_id, {"target_map": {"0": [42]}})

        bus.subscribe("ability_activation_data_required", dialog)
        result = await bus.request(
            "ability_activation_data_required", "ability:9",
            {"ability_instance_id": 9, "prompts": ("target",)})
        return observed, result

    observed, result = asyncio.run(scenario())
    assert observed[0][0] == "ability:9"
    assert result["target_map"]["0"] == [42]


def test_async_ui_event_bus_request_nowait_bridges_sync_action_callbacks():
    import asyncio

    async def scenario():
        bus = AsyncUIEventBus()
        bus.subscribe("checkpoint", lambda request_id, payload: bus.respond(
            request_id, payload["value"] + 1))
        task = bus.request_nowait("checkpoint", "sync:1", {"value": 4})
        return await task

    assert asyncio.run(scenario()) == 5


def test_async_activation_publisher_keeps_resume_as_protocol_transaction():
    import asyncio

    async def scenario():
        bus = AsyncUIEventBus()
        received = []
        bus.subscribe("ability_activation_data_required",
                       lambda request_id, payload: received.append(payload))
        ability = SimpleNamespace(instance_id=12,
                                  responsible_player_id="player")
        task = AsyncActivationPublisher(bus)(ability, ({"index": 0},))
        # The publisher task waits for the eventual protocol response.
        await asyncio.sleep(0)
        assert received[0]["ability_instance_id"] == 12
        assert not task.done()
        bus.respond("ability:12", {"target_map": {}})
        return await task

    assert asyncio.run(scenario())["target_map"] == {}


def test_async_rules_coordinator_drives_csharp_tick_until_waiting_action():
    import asyncio

    async def scenario():
        player = game_engine.UID.make(244, 37)
        session = AuthoritativeSession(65, (player,), seed_z=1, seed_w=2)
        session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
        session.push_game_action(PriorityWindowAction(TurnPhasePlayers.ACTIVE))
        coordinator = AsyncRulesCoordinator(session, AsyncUIEventBus())
        return await coordinator.drive_until_wait(), session.action_stack.count

    steps, count = asyncio.run(scenario())
    assert steps == 0 and count == 1


def test_async_event_bus_emits_one_way_server_events_without_reply_bookkeeping():
    import asyncio

    async def scenario():
        bus = AsyncUIEventBus()
        seen = []
        bus.subscribe("phase", lambda event_type, payload: seen.append(
            (event_type, payload["phase"])))
        task = bus.emit_nowait("phase", {"phase": "Draw"})
        await task
        return seen, bus._pending

    seen, pending = asyncio.run(scenario())
    assert seen == [("phase", "Draw")] and pending == {}


def test_async_event_bus_normalizes_request_ids_before_duplicate_detection():
    import asyncio

    async def scenario():
        bus = AsyncUIEventBus()
        started = asyncio.Event()

        async def handler(request_id, payload):
            started.set()

        bus.subscribe("checkpoint", handler)
        task = asyncio.create_task(bus.request("checkpoint", 7, {}))
        await started.wait()
        try:
            await bus.request("checkpoint", "7", {})
        except ValueError:
            bus.respond(7, True)
            await task
            return True
        return False

    assert asyncio.run(scenario())


def test_push_and_resolve_chain_actions_keep_csharp_prompt_ordering():
    player = game_engine.UID.make(244, 1)
    session = AuthoritativeSession(55, (player,), seed_z=1, seed_w=2)
    metadata = MetadataAbilityInstance.from_runtime("ability", [], 0,
                                                     source_uid=3, owner_id=player)
    ability = AbilityFactory().create(metadata, activating_player_id=player)
    requested, resolved = [], []
    session.set_activation_requester(lambda item, prompts: requested.append((item, prompts)))
    session.set_ability_resolver(lambda item: resolved.append(item) or
                                 AbilityResolutionState.COMPLETED)
    action = PushOntoChainAction(ability)
    session.push_game_action(action)
    assert session.action_stack.update()  # Push action completes and finishes on exit.
    assert session.chain.peek_ability() is ability
    assert session.action_stack.update()  # ResolveTopOfChainAction resolves.
    assert resolved == [ability]
    assert session.chain.is_empty
    assert session.ability_manager.get(ability.instance_id) is None
    assert requested == []


def test_manual_ability_finish_opens_all_player_chain_priority_window():
    """FinishAbilityOnChain must preserve the client response window."""
    player = game_engine.UID.make(244, 1)
    opponent = game_engine.UID.make(244, 2)
    session = AuthoritativeSession(56, (player, opponent), seed_z=1, seed_w=2)
    ability = SimpleNamespace(
        instance_id=14, metadata=SimpleNamespace(
            graph=SimpleNamespace(manual=True)),
        is_triggered=False, ignores_chain=False,
        untargeted_trigger=False, paid=False)
    session.set_ability_cost_payer(lambda _ability: True)
    assert session.finish_ability_on_chain(ability)
    assert isinstance(session.action_stack.peek(), PriorityWindowAction)
    assert session.action_stack.priority_player_id == player


def test_manual_ability_finish_projects_chain_entry_and_green_light():
    """Native manual activations must enter the existing Unity chain UI."""
    player = game_engine.UID.make(244, 57)
    opponent = game_engine.UID.make(3, 58)
    game = game_engine.Game(57, player, opponent)
    session = AuthoritativeSession(
        57, (player, opponent), seed_z=1, seed_w=2,
        event_sink=GameEngineEventSink(game))
    ability = SimpleNamespace(
        instance_id=19,
        source_uid=game_engine.UID.make(1, 8).uid64,
        ability_template_id="4ba9e978-53fd-a3ed-88ca-d57632f186cb",
        activation=SimpleNamespace(
            target_map={0: (game_engine.UID.make(1, 9).uid64,)}),
        metadata=SimpleNamespace(graph=SimpleNamespace(manual=True)),
        is_triggered=False, ignores_chain=False,
        untargeted_trigger=False, paid=False)
    session.set_ability_cost_payer(lambda _ability: True)

    assert session.finish_ability_on_chain(ability)
    chain_events = [
        event for event in game.events
        if isinstance(event, game_engine.AbilityPushedOnChainSessionEventArgs)]
    green_lights = [
        event for event in game.events
        if isinstance(event, game_engine.GreenLightSessionEventArgs)]
    assert len(chain_events) == 1
    assert chain_events[0].source_card_id.uid.uid64 == 0x801
    assert [item.uid.uid64 for item in chain_events[0].target_card_ids] == [
        0x901]
    assert str(chain_events[0].ability_template_id.guid) == (
        "4ba9e978-53fd-a3ed-88ca-d57632f186cb")
    assert len(green_lights) == 1
    assert green_lights[0].player_id == player


def test_practice_manual_ability_response_window_auto_passes_ai():
    """A Practice opponent must not strand a manual ability on the chain."""
    player = game_engine.UID.make(244, 59)
    opponent = game_engine.UID.make(3, 60)
    session = AuthoritativeSession(
        59, (player, opponent), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.active_player_id = player
    ability = SimpleNamespace(
        instance_id=21, source_uid=game_engine.UID.make(1, 8).uid64,
        ability_template_id="598fe8be-5c04-918c-e0aa-82e88aee3d28",
        metadata=SimpleNamespace(graph=SimpleNamespace(manual=True)),
        is_triggered=False, ignores_chain=False,
        untargeted_trigger=False, paid=False)
    resolved = []
    session.set_ability_cost_payer(lambda _ability: True)
    session.set_ability_resolver(
        lambda item: resolved.append(item) or AbilityResolutionState.COMPLETED)
    assert session.finish_ability_on_chain(ability)
    assert session.action_stack.priority_player_id == player

    assert session.pass_player_priority(player)
    while (session.action_stack.priority_player_id is not None and
           session.action_stack.priority_player_id != player):
        assert session.pass_player_priority(
            session.action_stack.priority_player_id)
    session.drive_until_input()

    assert resolved == [ability]
    assert session.chain.is_empty


def test_live_projected_chain_response_window_survives_reattach_prune():
    """A reattach must not prune a manual ability's live response window.

    The HConnect host runs a per-transaction orphan cleanup before
    ``sync_checkpoint``.  A manual/triggered ability response window has
    ``ability_responding_to`` set, so it is not an ordinary phase window; if
    the cleanup also ignored the live chain it cleared the window, the rebuilt
    phase window gave the human priority, the server-driven AI pass was
    rejected, and the ability stayed on the chain forever (Minion of Yazukan's
    hand Tunneling paid its cost but never resolved).
    """
    player = game_engine.UID.make(244, 61)
    opponent = game_engine.UID.make(3, 62)
    session = AuthoritativeSession(
        62, (player, opponent), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.active_player_id = player
    session.action_stack.clear()

    resolved = []
    session.set_ability_resolver(
        lambda item: resolved.append(item.instance_id) or
        AbilityResolutionState.COMPLETED)
    session.queue_projected_chain(
        {"kind": "ability", "source_uid": 0x0801, "instance_id": 1,
         "ability_guid": "95474d1e-ac9b-6c02-cb95-0305ebec42dc",
         "activation_data": {}},
        player, first_player_id=player)

    # The response window is live; a reattach prune must leave it alone.
    assert session.prune_orphan_actions(None) is False
    assert session.chain._instance_ids == [1]
    assert session.action_stack.priority_player_id == player

    # Human passes; the AI's server-driven response re-runs the reattach
    # (fresh Game projection) before it is asked to pass.
    assert session.pass_player_priority(player)
    assert session.action_stack.priority_player_id == opponent
    assert session.prune_orphan_actions(None) is False
    assert session.action_stack.priority_player_id == opponent

    # The AI's pass is now accepted and the chain resolves.
    assert session.pass_player_priority(opponent)
    assert session.action_stack.priority_player_id is None
    session.drive_until_input()
    assert resolved == [1]
    assert session.chain.is_empty


def test_orphan_phase_action_without_chain_item_is_pruned():
    """The double-click guard still clears a chainless stale action."""
    player = game_engine.UID.make(244, 63)
    opponent = game_engine.UID.make(3, 64)
    session = AuthoritativeSession(
        63, (player, opponent), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.active_player_id = player
    session.action_stack.clear()
    ability = AbilityStub(99)
    session.action_stack.push(ResolveTopOfChainAction(ability))
    assert session.chain.is_empty
    assert session.prune_orphan_actions(None) is True
    assert session.action_stack.count == 0


def test_completed_chain_resolution_persists_effect_boundary():
    stored = PersistedSessionStub()
    player = game_engine.UID.make(244, 41)
    session = AuthoritativeSession(70, (player,), seed_z=1, seed_w=2,
                                   snapshot=SQLiteRulesSnapshot(stored))
    ability = AbilityStub(72)
    session.ability_manager.add_chain(72, ability)
    session.chain._instance_ids.append(72)
    session.set_ability_resolver(lambda item: AbilityResolutionState.COMPLETED)
    assert session.resolve_top_of_chain(72) is AbilityResolutionState.COMPLETED
    assert stored.persisted == 1
    assert session.chain.is_empty


def test_completed_chain_resolution_emits_next_priority_checkpoint():
    player = game_engine.UID.make(244, 71)
    opponent = game_engine.UID.make(3, 72)
    game = game_engine.Game(71, player, opponent)
    session = AuthoritativeSession(
        71, (player, opponent), seed_z=1, seed_w=2,
        event_sink=GameEngineEventSink(game))
    session.action_stack.priority_player_id = player
    ability = AbilityStub(73)
    session.ability_manager.add_chain(73, ability)
    session.chain._instance_ids.append(73)
    session.set_ability_resolver(lambda item: AbilityResolutionState.COMPLETED)
    assert session.resolve_top_of_chain(73) is AbilityResolutionState.COMPLETED
    assert any(event.__class__.__name__ == "GreenLightSessionEventArgs"
               for event in game.events)


def test_prompt_boundary_persists_ability_continuation_before_waiting():
    stored = PersistedSessionStub()
    player = game_engine.UID.make(244, 1)
    session = AuthoritativeSession(55, (player,), seed_z=1, seed_w=2,
                                   snapshot=SQLiteRulesSnapshot(stored))
    ability = PromptAbilityStub(9, player)
    session.push_game_action(PushOntoChainAction(ability))
    assert session.action_stack.update() is False
    assert stored.persisted == 1
    assert stored.turn_order["rules_port"]["pending_activation"] == {
        "ability_instance_id": 9,
        "responsible_player_id": int(player.uid64),
        "continuation": {"ability_instance_id": 9, "target_map": {}},
        "prompts": [{"kind": "target", "minimum": 1}],
    }


def test_prompt_boundary_emits_the_client_class_23_ability_dialog_event():
    player = game_engine.UID.make(244, 1)
    game = game_engine.Game(55, player, game_engine.UID.make(3, 2))
    session = AuthoritativeSession(55, (player,), seed_z=1, seed_w=2,
                                   event_sink=GameEngineEventSink(game))
    ability = PromptAbilityStub(9, player)
    ability.source_uid = player.uid64
    ability.parent_instance_id = 2
    ability.ability_template_id = "00000000-0000-0000-0000-000000000001"
    ability.ordered_effects = ({"effect_group_id": 3,
                                "effect_instance_id": 4,
                                "target_index": -1},)
    ability.is_triggered = False
    session.push_game_action(PushOntoChainAction(ability))
    assert session.action_stack.update() is False
    event = game.events[-1]
    assert isinstance(event, game_engine.AbilityActivationDataRequiredSessionEventArgs)
    assert event.player_id == player
    assert event.ability_instance_id == 9
    assert event.ability_parent_id == 2
    assert event.effect_group_id == 3
    assert event.effect_instance_ids == [4]
    assert event.resolve_chain


def test_activation_reply_binds_the_waiting_instance_then_finishes_push():
    stored = PersistedSessionStub()
    player = game_engine.UID.make(244, 1)
    session = AuthoritativeSession(55, (player,), seed_z=1, seed_w=2,
                                   snapshot=SQLiteRulesSnapshot(stored))
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    ability = PromptAbilityStub(9, player)
    paid = []
    session.set_ability_cost_payer(lambda item: paid.append(item.instance_id) or True)
    session.push_game_action(PushOntoChainAction(ability))
    assert session.action_stack.update() is False
    reply = RulesTransaction.set_ability_activation_data(
        player, 9,
        {"target_map": {"0": [42]}})
    assert session.submit_transaction(reply)
    assert session.handle_transaction()
    assert ability.activation_data == {"target_map": {"0": [42]}}
    assert paid == [9]
    assert stored.persisted == 2
    assert session.pending_activation is None
    assert session.action_stack.update()
    assert paid == [9]
    assert session.chain.peek_ability() is ability


def test_activation_reply_uses_client_ability_and_responsibility_requirements():
    player, opponent = game_engine.UID.make(244, 1), game_engine.UID.make(3, 2)
    session = AuthoritativeSession(55, (player, opponent), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    ability = PromptAbilityStub(9, player)
    session.push_game_action(PushOntoChainAction(ability))
    assert session.action_stack.update() is False
    assert not session.submit_transaction(RulesTransaction.set_ability_activation_data(
        opponent, 9, {"target_map": {}}))
    assert not session.submit_transaction(RulesTransaction.set_ability_activation_data(
        player, 10, {"target_map": {}}))


def test_metadata_resolution_adapter_reuses_existing_activation_data_shape():
    metadata = MetadataAbilityInstance.from_runtime("ability", [], 0,
                                                     source_uid=3, owner_id=7)
    ability = AbilityFactory().create(metadata, activating_player_id=7)
    calls, state = [], {}
    adapter = MetadataResolutionAdapter(
        None, None, type("Session", (), {"session_id": 5})(), None, 7, 0,
        state, resolver=lambda *args, **kwargs: calls.append((args, kwargs)))
    assert adapter(ability) is AbilityResolutionState.COMPLETED
    assert calls[0][0][7:10] == ("ability", 3, 7)
    assert calls[0][1]["activation_data"] == ability.activation.as_dict()


def test_ability_instance_serialization_preserves_csharp_lifecycle_fields():
    metadata = MetadataAbilityInstance.from_runtime("ability", [], 0,
                                                     source_uid=3, owner_id=7)
    ability = AbilityFactory().create(metadata, activating_player_id=7)
    ability.paid, ability.free, ability.resolving = True, True, True
    value = ability.as_dict()
    assert value["ability_instance_id"] == ability.instance_id
    assert value["paid"] and value["free"] and value["resolving"]


def test_ability_instance_serialization_is_json_safe_for_trigger_events():
    metadata = MetadataAbilityInstance.from_runtime("ability", [], 0,
                                                     source_uid=3, owner_id=7)
    ability = AbilityFactory().create(
        metadata,
        activating_player_id=7,
        trigger_event=TriggerEvent("CardMovedEvent", 11, 7, 22, 0,
                                   {"source_zone": "deck", "destination_zone": "hand"}),
    )
    encoded = json.dumps(ability.as_dict(), sort_keys=True)
    assert "CardMovedEvent" in encoded
    assert "destination_zone" in encoded


def test_metadata_trigger_adapter_preserves_csharp_event_source_target_envelope():
    calls = []
    adapter = MetadataTriggerAdapter(
        None, None, type("Session", (), {"session_id": 5})(), None, 7, 0, {},
        resolver=lambda *args, **kwargs: calls.append((args, kwargs)) or "ok")
    event = TriggerEvent("CardDealtDamageEvent", 11, 7, 22, 0,
                         {"CombatDamage": 1})
    assert adapter(event) == "ok"
    assert calls[0][0][7:9] == ("CardDealtDamageEvent", 11)
    assert calls[0][1]["source_owner_uid"] == 7
    assert calls[0][1]["extra_target"] == 22


def test_metadata_target_adapter_uses_one_predicate_for_options_and_validation():
    candidate_calls, validation_calls = [], []
    adapter = MetadataTargetAdapter(
        None, 5, 7, 11,
        candidates=lambda *args, **kwargs: candidate_calls.append((args, kwargs)) or (12, 13),
        validator=lambda *args, **kwargs: validation_calls.append((args, kwargs)) or [13])
    assert adapter.candidates("target-template") == (12, 13)
    selection = TargetSelection.from_values("target-template", [13])
    assert adapter.validate(selection) == (13,)
    assert candidate_calls[0][0][1:5] == (5, 7, "target-template", 11)
    assert validation_calls[0][0][1:6] == (5, 7, "target-template", 11, (13,))


def test_metadata_target_adapter_defaults_to_native_records_targeting():
    from tests.tests_targeting import (EXILE_TARGET, EXILE_TPL, EXILE_DEPLOY,
                                       EXILE_LEAVE, PLAIN_TPL, make_db,
                                       add_card)
    db = make_db()
    try:
        add_card(db, 100, 5, EXILE_TPL, "warzone",
                 f'["{EXILE_DEPLOY}", "{EXILE_LEAVE}"]')
        add_card(db, 101, 5, PLAIN_TPL, "warzone")
        add_card(db, 200, 0, PLAIN_TPL, "warzone")
        adapter = MetadataTargetAdapter(db, 1, 5, 100)
        assert adapter.candidates(EXILE_TARGET) == (101, 200)
    finally:
        db.close()


def test_ready_card_transaction_matches_csharp_requirements():
    card = type("Card", (), {"collection": int(game_engine.ECardCollections.Warzone),
                              "tapped": True, "can_ready": True})()
    session = RequirementSessionStub()
    session.current_turn_phase = game_engine.ETurnPhases.Ready
    session.active_player_id = 1
    session.cards = {9: card}
    tx = RulesTransaction.ready_card(1, (9,))
    assert tx.validate(session)


def test_quit_game_eliminates_player_and_persists_terminal_state():
    player = game_engine.UID.make(244, 1)
    session = AuthoritativeSession(91, (player,), seed_z=1, seed_w=2)
    tx = RulesTransaction.quit_game(player, was_bugged=True, quit_entire_series=True)
    assert tx.validate(session)
    session.submit_transaction(tx)
    assert session.handle_transaction()
    assert session.is_player_eliminated(player)
    assert session.terminated
    assert session.snapshot()["eliminated_player_ids"]


def test_set_turn_phases_persists_player_preferences():
    session = AuthoritativeSession(93, ("p",), seed_z=1, seed_w=2)
    tx = RulesTransaction.set_turn_phases("p", ("Ready", "Draw"), ("Priority",))
    session.submit_transaction(tx)
    assert session.handle_transaction()
    assert session.turn_phase_preferences["p"]["self"] == ("Ready", "Draw")
    assert session.snapshot()["turn_phase_preferences"]["p"]["opponent"] == ["Priority"]


def test_request_player_options_uses_async_ui_adapter_resolver():
    session = AuthoritativeSession(97, ("p",), seed_z=1, seed_w=2)
    requested = []
    session.set_player_options_resolver(lambda tx: requested.append(tx.player_id) or True)
    session.submit_transaction(RulesTransaction.request_player_options("p"))
    assert session.handle_transaction()
    assert requested == ["p"]


def test_state_checksum_transaction_delegates_typed_checksum_payload():
    session = AuthoritativeSession(98, ("p",), seed_z=1, seed_w=2)
    received = []
    session.set_checksum_resolver(lambda tx: received.append(tx.payload["checksum_data"]) or True)
    session.submit_transaction(RulesTransaction.send_state_checksum("p", {"hash": "abc"}))
    assert session.handle_transaction()
    assert received == [{"hash": "abc"}]


def test_state_checksum_is_natively_acknowledged_when_defunct():
    session = AuthoritativeSession(981, ("p",), seed_z=1, seed_w=2)
    session.submit_transaction(RulesTransaction.send_state_checksum(
        "p", {"session_checksum": 17}))
    assert session.handle_transaction()


def test_async_coordinator_submits_and_drives_to_next_checkpoint():
    import asyncio
    from rules_port import AsyncRulesCoordinator, AsyncUIEventBus
    session = AuthoritativeSession(102, ("p",), seed_z=1, seed_w=2)
    bus = AsyncUIEventBus()
    coordinator = AsyncRulesCoordinator(session, bus)
    command = SimpleNamespace(is_tip_window_closed=True, inner_bytes=b"",
                              pass_turn_phase=None)
    assert asyncio.run(coordinator.submit_and_drive(command, "p")) >= 0


def test_transaction_coverage_manifest_is_explicit_and_counts_statuses():
    from rules_port import (TRANSACTION_COVERAGE, coverage_summary,
                            validate_transaction_coverage, validate_filter_coverage,
                            validate_effect_coverage, coverage_report)
    assert TRANSACTION_COVERAGE["ActivateAbilityTransaction"] == "native+projection"
    assert coverage_summary()["native"] > 0
    assert coverage_summary()["legacy-only"] == 2
    assert validate_transaction_coverage() == ((), ())
    missing_filters, extra_filters = validate_filter_coverage()
    assert "HasName" not in missing_filters
    assert not extra_filters
    report = coverage_report()
    assert report["transactions"]["missing"] == []
    assert report["filters"]["native"] >= 30
    assert validate_effect_coverage() == ((), ())
    assert report["effects"]["missing"] == []


def test_native_effect_backend_walks_typed_effects_without_legacy_resolver():
    from gamedata.semantics import EffectSpec
    from rules_port.resolution import NativeEffectBackend
    from tests.tests_combat import make_db, HandlerStub, SessionStub

    effect = EffectSpec(
        guid="native-test-effect", concrete_type="NoOpEffectTemplate",
        operation="NoOp", name="", target_index=-1, effect_instance_id=0,
        effect_group_id=0, duration="Instant", condition_guid="",
        optional=False, recalculate_targets="True",
        secondary_target_index=-1, output_variables={})
    skipped = EffectSpec(
        guid="skipped-effect", concrete_type="NoOpEffectTemplate",
        operation="NoOp", name="", target_index=-1, effect_instance_id=1,
        effect_group_id=9, duration="Instant", condition_guid="",
        optional=False, recalculate_targets="True",
        secondary_target_index=-1, output_variables={})
    ability = SimpleNamespace(
        ability_template_id="native-test-ability", source_uid=100,
        responsible_player_id=5, ordered_effects=(effect, skipped),
        activation=SimpleNamespace(target_map={}))
    db = make_db()
    game = game_engine.Game(
        1, game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000))
    state = {}
    NativeEffectBackend()(
        handler=HandlerStub(db), game=game, session=SessionStub(), db=db,
        player_uid=game.player_uid, ai_uid=game.ai_uid,
        battle_state=state, ability=ability, effect_groups=(0,))
    assert state["applied_effects"] == {0: True}
    assert state["rules_port_effect_results"][0]["result"] == "no-op"


def test_native_effect_backend_reuses_one_auto_target_mapping():
    """A move and its follow-up effects must keep the same random card."""
    from unittest.mock import patch
    from rules_port.resolution import NativeEffectBackend
    from tests.tests_combat import make_db, HandlerStub, SessionStub

    target = SimpleNamespace(
        guid="random-deck-card", target_kind="AbilityTargetTemplate",
        is_auto=True, is_random=True, player_filter="self",
        resolved_maximum=lambda _variables: 1)
    effects = tuple(SimpleNamespace(
        guid=f"effect-{index}", concrete_type="CapturedEffect",
        target_index=0, effect_instance_id=index, effect_group_id=index,
        contingent_effect_instance_id=-1, param="", condition_guid="")
        for index in range(3))
    activation = SimpleNamespace(target_map={}, variables={})
    ability = SimpleNamespace(
        ability_template_id="shared-auto-target", source_uid=100,
        responsible_player_id=5, ordered_effects=effects,
        metadata=SimpleNamespace(targets=(target,)), activation=activation)
    db = make_db()
    game = game_engine.Game(
        1, game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000))
    selected, seen = [], []

    def legal_targets(*_args, **_kwargs):
        selected.append(True)
        return (101,) if len(selected) == 1 else (202,)

    def capture(_effect_type, context, _effect):
        seen.append(context.target())
        return "captured"

    with patch("rules_port.targeting.legal_targets", legal_targets):
        NativeEffectBackend()(
            handler=HandlerStub(db), game=game, session=SessionStub(), db=db,
            player_uid=game.player_uid, ai_uid=game.ai_uid, battle_state={},
            ability=ability, native_effect=capture)

    assert seen == [101, 101, 101]
    assert len(selected) == 1
    assert activation.target_map == {0: (101,)}


def test_native_effect_context_rejects_legacy_helpers():
    """A native context must never silently enter the transitional BOM ABI."""
    from abilities.framework.context import EffectContext

    context = EffectContext.from_rules_port(
        game=SimpleNamespace(), session=SimpleNamespace(), db=None,
        handler=SimpleNamespace(), pl_t=None, ai_t=None, bstate={},
        effect_guid="native", ability=None)
    try:
        context._legacy("should_not_run")
    except RuntimeError as exc:
        assert "legacy helper" in str(exc)
    else:
        raise AssertionError("native context entered a legacy helper")


def test_rules_port_modules_do_not_import_legacy_rule_engines():
    """The native package must not acquire a hidden compatibility dependency."""
    from pathlib import Path

    forbidden = ("import battle_engine", "from battle_engine",
                 "import ability\n", "from ability ")
    violations = []
    for path in Path("rules_port").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                violations.append(f"{path}: {token}")
    assert not violations, "legacy RulesPort imports: " + ", ".join(violations)


def test_tip_window_closed_is_ordered_noop_transaction():
    session = AuthoritativeSession(99, ("p",), seed_z=1, seed_w=2)
    session.submit_transaction(RulesTransaction.tip_window_closed("p"))
    assert session.handle_transaction()
    assert session.transaction_history[-1]["kind"] == "tip_window_closed"


def test_encounter_mod_dialog_delegates_conversation_id_to_resolver():
    session = AuthoritativeSession(100, ("p",), seed_z=1, seed_w=2)
    seen = []
    session.set_encounter_mod_resolver(lambda tx: seen.append(tx.payload["conversation_id"]) or True)
    session.submit_transaction(RulesTransaction.encounter_mod_dialog("p", "conv-1"))
    assert session.handle_transaction()
    assert seen == ["conv-1"]


def test_card_transaction_executor_rejects_untyped_or_unknown_intents():
    calls = []
    executor = CardTransactionExecutor(
        lambda kind, tx: calls.append((kind, tx.payload)) or True)
    assert not executor("not_a_card_transaction",
                        SimpleNamespace(payload={"card_id": 7}))
    assert not executor("play_troop", SimpleNamespace(payload={}))
    assert not executor("activate_ability", SimpleNamespace(payload={
        "source_card_id": 7, "ability_template_id": "", "activation_data": {},
    }))
    assert calls == []


def test_card_transaction_executor_forwards_only_normalized_payload():
    calls = []
    executor = CardTransactionExecutor(
        lambda kind, tx: calls.append((kind, tx.payload)) or True)
    tx = SimpleNamespace(payload={"card_id": 7, "ability_data": ()})
    assert executor("play_troop", tx)
    assert calls == [("play_troop", {"card_id": 7, "ability_data": ()})]


def test_metadata_card_executor_keeps_zone_mutation_as_explicit_adapter():
    calls = []
    executor = MetadataCardTransactionExecutor(
        SimpleNamespace(), graph_loader=lambda _guid: None, owner_id=9,
        compatibility=lambda kind, tx: calls.append((kind, tx.payload)) or True)
    tx = SimpleNamespace(kind="play_resource", payload={"card_id": 7})
    assert executor("play_resource", tx)
    assert calls == [("play_resource", {"card_id": 7})]


def test_metadata_manual_ability_uses_reconnectable_projected_chain():
    """A response pass must not discard a typed ability instance on reattach."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph

    player = game_engine.UID.make(PLAYER_UID_TYPE, 41)
    port = AuthoritativeSession(141, (player,), seed_z=1, seed_w=2)
    executor = MetadataCardTransactionExecutor(
        port,
        graph_loader=lambda guid: ability_graph(DEFAULT_RECORD_STORE, guid),
        owner_id=41)
    tx = SimpleNamespace(
        player_id=player,
        payload={
            "source_card_id": 2561,
            "ability_template_id": (
                "598fe8be-5c04-918c-e0aa-82e88aee3d28"),
            "activation_data": {"target_map": {}},
            "ability_instance_id": 0,
        })

    assert executor("activate_ability", tx)
    assert port.chain._instance_ids == [1]
    saved = port.snapshot()
    assert saved["projected_chain"][0]["kind"] == "ability"
    assert saved["projected_chain"][0]["activation_data"]["target_map"] == {}

    restored = AuthoritativeSession(141, (player,), seed_z=9, seed_w=9)
    assert restored.restore_snapshot(saved)
    assert restored.rehydrate_projected_chain()
    assert restored.chain.peek_ability().source_uid == 2561
    assert restored.chain.peek_ability().ability_template_id == (
        "598fe8be-5c04-918c-e0aa-82e88aee3d28")


def test_projected_chain_replaces_stale_phase_action_before_pass():
    player = game_engine.UID.make(PLAYER_UID_TYPE, 42)
    ai = game_engine.UID.make(3, 1000)
    source = AuthoritativeSession(142, (player, ai), seed_z=1, seed_w=2)
    source.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    source.active_player_id = player
    source.action_stack.priority_player_id = player
    source.queue_projected_chain(
        {"kind": "ability", "source_uid": 2305, "ability_guid": "tunnel",
         "instance_id": 1, "activation_data": {}},
        player, first_player_id=player)
    saved = source.snapshot()

    restored = AuthoritativeSession(142, (player, ai), seed_z=9, seed_w=9)
    assert restored.restore_snapshot(saved)
    restored.rehydrate_projected_chain()
    # Simulate the bad reconnect state observed in the live session: the
    # descriptor/chain survived, but only a normal phase window was rebuilt.
    restored.action_stack.clear()
    normal = PriorityWindowAction(TurnPhasePlayers.ACTIVE)
    restored.action_stack.push(normal)
    restored.action_stack.priority_player_id = player
    assert restored.ensure_projected_chain_action()
    action = restored.action_stack.peek()
    assert isinstance(action, PriorityWindowAction)
    assert action.ability_responding_to is restored.chain.peek_ability()
    assert restored.action_stack.priority_player_id == player


def test_projected_chain_resolves_ignores_chain_ability_without_a_window():
    """An authored IgnoresChain ability must not wait on a priority window.

    The client's PriorityWindowAction.Update completes immediately for an
    IgnoresChain chain top, so the native scheduler has to match or the
    ability (and any picker inside its BOM) never resolves.  Corinth's charge
    power is the canonical case: its BOM creates three Choosing cards and
    invokes a copy-to-hand child.
    """
    from rules_port.session import (
        ProjectedChainAbility, projected_ability_ignores_chain)

    # Explicit descriptor flag wins without a Records lookup.
    assert projected_ability_ignores_chain(
        {"ability_guid": "does-not-exist", "ignores_chain": True})
    # The real authored Corinth charge power is IgnoresChain in Records.
    charge_power = "286f1891-4404-585e-4fb6-bd9f783f222b"
    assert projected_ability_ignores_chain({"ability_guid": charge_power})
    ability = ProjectedChainAbility(
        3, {"kind": "ability", "ability_guid": charge_power}, 7)
    assert ability.ignores_chain
    # An ordinary chain ability still opens a response window.
    assert not projected_ability_ignores_chain({"ability_guid": ""})
    assert not projected_ability_ignores_chain(None)


def test_projected_chain_keeps_rehydrated_response_window_by_instance_id():
    """A reload must not reset an in-progress response window to its opener."""
    from rules_port.session import ProjectedChainAbility
    from collections import deque

    player = game_engine.UID.make(PLAYER_UID_TYPE, 43)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    session = AuthoritativeSession(143, (player, ai), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.active_player_id = player
    ability = session.queue_projected_chain(
        {"kind": "ability", "source_uid": 2305, "ability_guid": "tunnel",
         "instance_id": 2, "activation_data": {}},
        player, first_player_id=player)
    response = session.action_stack.peek()
    # Restore paths can recreate the action's wrapper separately from the
    # Chain wrapper.  It still names the same durable ability instance.
    response.ability_responding_to = ProjectedChainAbility(
        2, ability.descriptor, player)
    response._priority_queue = deque([ai])
    session.action_stack.priority_player_id = ai

    assert not session.ensure_projected_chain_action()
    assert session.action_stack.peek() is response
    assert response.priority_player_id == ai


def test_metadata_card_executor_can_separate_resource_mutation_adapter():
    calls = []
    executor = MetadataCardTransactionExecutor(
        SimpleNamespace(), graph_loader=lambda _guid: None, owner_id=9,
        compatibility=lambda kind, tx: calls.append(("generic", kind)) or True,
        resource_compatibility=lambda tx: calls.append(
            ("resource", tx.payload["card_id"])) or True)
    tx = SimpleNamespace(kind="play_resource", payload={"card_id": 7})
    assert executor("play_resource", tx)
    assert calls == [("resource", 7)]


def test_resource_transaction_executor_rejects_other_kinds():
    from rules_port.card_transactions import ResourceTransactionExecutor
    calls = []
    executor = ResourceTransactionExecutor(
        lambda tx: calls.append(tx.payload["card_id"]) or True)
    assert executor(SimpleNamespace(kind="play_troop", payload={"card_id": 7})) is False
    assert executor(SimpleNamespace(kind="play_resource", payload={})) is False
    assert executor(SimpleNamespace(kind="play_resource", payload={"card_id": 7}))
    assert calls == [7]


def test_combat_transactions_use_authoritative_mutation_resolvers():
    session = AuthoritativeSession(101, ("p", "o"), seed_z=1, seed_w=2)
    seen = []
    session.set_attack_transaction_resolver(lambda tx: seen.append("attack") or True)
    session.set_defense_transaction_resolver(lambda tx: seen.append("defense") or True)
    session.set_damage_transaction_resolver(lambda tx: seen.append("damage") or True)
    attack = RulesTransaction.commit_troops_to_attack("p", session.current_turn_phase, ())
    defense = RulesTransaction.commit_troops_to_defense("o", session.current_turn_phase, ())
    damage = RulesTransaction.assign_damage_order("p", game_engine.ETurnPhases.AssignDamage, ())
    assert session._resolve_commit_troops_to_attack(attack)
    assert session._resolve_commit_troops_to_defense(defense)
    assert session._resolve_assign_damage_order(damage)
    assert seen == ["attack", "defense", "damage"]


def test_live_projection_wiring_is_complete_and_auditable():
    session = AuthoritativeSession(102, ("p", "o"), seed_z=1, seed_w=2)
    missing = session.missing_projection_resolvers()
    assert "card_transaction" in missing
    assert "priority_transaction" in missing
    for name, attribute in session._PROJECTION_RESOLVERS.items():
        session._projection.bind(name, lambda *_args, **_kwargs: True)
    assert session.missing_projection_resolvers() == ()
    session.assert_projection_wiring()


def test_native_control_transactions_use_host_projections_when_bound():
    session = AuthoritativeSession(103, ("p", "o"), seed_z=1, seed_w=2)
    seen = []
    session.set_auto_pass_transaction_resolver(
        lambda tx: seen.append("auto") or True)
    session.set_cancel_auto_pass_transaction_resolver(
        lambda tx: seen.append("cancel") or True)
    session.set_priority_sync_resolver(
        lambda tx: seen.append("sync") or True)
    session.set_turn_phase_resolver(
        lambda tx: seen.append("stops") or True)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.action_stack.priority_player_id = "p"
    session.submit_transaction(RulesTransaction.set_auto_pass(
        "p", True, 2))
    assert session.handle_transaction()
    session.submit_transaction(RulesTransaction.cancel_auto_pass("p"))
    assert session.handle_transaction()
    session.submit_transaction(RulesTransaction.request_priority_sync("p"))
    assert session.handle_transaction()
    session.submit_transaction(RulesTransaction.set_turn_phases(
        "p", (1,), (2,)))
    assert session.handle_transaction()
    assert seen == ["auto", "cancel", "sync", "stops"]


def test_bound_projection_rejection_never_falls_back_to_native_mutation():
    """A host projection is authoritative once it is wired."""
    session = AuthoritativeSession(104, ("p", "o"), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    session.active_player_id = "p"
    session.action_stack.priority_player_id = "p"
    session.set_auto_pass_transaction_resolver(lambda _tx: False)
    tx = RulesTransaction.set_auto_pass("p", True, 2)
    assert session._resolve_set_auto_pass(tx) is False
    assert session.auto_pass_states == {}

    session.set_cancel_auto_pass_transaction_resolver(lambda _tx: False)
    session.auto_pass_states["p"] = 2
    assert session._resolve_cancel_auto_pass(
        RulesTransaction.cancel_auto_pass("p")) is False
    assert session.auto_pass_states == {"p": 2}


def test_rules_port_cost_plan_preserves_temporary_resource_over_maximum():
    """RulesPort may spend a Hideous Conversion-style current-pool grant."""
    costs = type("Costs", (), {
        "activation": 2, "variable_activation": 0,
        "charge_points": 0, "spell_points": 0, "life": 0,
        "is_spell_power": False,
    })()
    activation = type("Activation", (), {"x_cost": 0})()
    plan = plan_ability_cost(
        costs, activation, current_resource=4, charges=0,
        spell_points=0, health=25)
    assert plan is not None
    assert plan.resource == 2

    # The temporary pool can be above its maximum after the authored grant;
    # it remains spendable and is not clamped to total resources here.
    plan = plan_ability_cost(
        costs, activation, current_resource=4, charges=0,
        spell_points=0, health=25)
    assert plan.resource == 2
    payment_state = {"player_resources": 4, "player_charges": 1,
                     "player_spell_points": 3, "player_health": 20}
    payment = apply_ability_cost_plan(payment_state, "player", plan)
    assert payment_state["player_resources"] == 2
    assert payment["resource"]["old"] == 4
    play_plan = SimpleNamespace(
        store=None,
        cost_instances=({"index": 0, "kind": "sacrifice",
                         "target_guid": "target-guid", "cost_type": 2,
                         "minimum": 1, "maximum": -1, "auto": True},))
    targets = card_cost_targets(play_plan, None, 1, 7, 99)
    assert targets[0].is_source_auto_target
    assert targets[0].candidates == (99,)


def test_resource_play_is_one_atomic_native_transition():
    state = {
        "ai_resources": 0, "ai_total_resources": 0, "ai_charges": 0,
        "ai_threshold": {},
    }
    result = play_resource(
        state, "ai", current_amount=1, total_amount=1,
        threshold_color=int(game_engine.ECardShards.Sapphire))
    assert state["ai_resources"] == 1
    assert state["ai_total_resources"] == 1
    assert state["ai_charges"] == 1
    assert state["ai_threshold"][int(game_engine.ECardShards.Sapphire)] == 1
    assert state["ai_resource_played_this_turn"] is True
    assert result.threshold.new_value == 1
    try:
        play_resource(state, "ai", 1, 1)
    except ValueError as exc:
        assert "already played" in str(exc)
    else:
        raise AssertionError("second resource play was not rejected")


def test_pvp_phase_transition_is_native_and_reports_wrap():
    phases = [game_engine.ETurnPhases.FirstMainPhase,
              game_engine.ETurnPhases.DeclareAttack,
              game_engine.ETurnPhases.SecondMainPhase]
    result = pvp_lifecycle.phase_transition(
        phases, game_engine.ETurnPhases.FirstMainPhase)
    assert result == {"current_index": 0, "next_index": 1,
                      "new_phase": game_engine.ETurnPhases.DeclareAttack,
                      "wrapped": False}
    result = pvp_lifecycle.phase_transition(
        phases, game_engine.ETurnPhases.SecondMainPhase)
    assert result["wrapped"] is True
    assert result["new_phase"] is None
    result = pvp_lifecycle.phase_transition(
        phases, game_engine.ETurnPhases.DeclareAttack,
        after_blockers=game_engine.ETurnPhases.SecondMainPhase)
    assert result["new_phase"] == game_engine.ETurnPhases.SecondMainPhase
    assert result["wrapped"] is False
    assert pvp_lifecycle.phase_after_blockers(False, True) == \
        game_engine.ETurnPhases.SecondMainPhase
    phase_state = {"phase": game_engine.ETurnPhases.FirstMainPhase,
                   "passes": [7, 8]}
    assert pvp_lifecycle.enter_phase(
        phase_state, game_engine.ETurnPhases.SecondMainPhase) == \
        game_engine.ETurnPhases.SecondMainPhase
    assert phase_state["passes"] == []
    assert pvp_lifecycle.record_phase_pass(phase_state, 7) == [7]
    assert pvp_lifecycle.record_phase_pass(phase_state, 7) == [7]
    assert pvp_lifecycle.set_priority(phase_state, 8) == 8
    assert phase_state["priority_pid"] == 8
    phase_state["stack_passed"] = [7]
    assert pvp_lifecycle.reset_priority_interval(phase_state, 7) == 7
    assert phase_state["passes"] == phase_state["stack_passed"] == []
    assert pvp_lifecycle.phase_after_blockers(True, False) == \
        game_engine.ETurnPhases.AssignDamage
    assert pvp_lifecycle.phase_after_blockers(True, True) == \
        game_engine.ETurnPhases.AssignFirstStrikeDamage
    assert pvp_lifecycle.waiting_player_requires_priority(
        {}, game_engine.ETurnPhases.DeclareDefense, 2)
    assert not pvp_lifecycle.waiting_player_requires_priority(
        {}, game_engine.ETurnPhases.SecondMainPhase, 2)
    assert pvp_lifecycle.waiting_player_requires_priority(
        {}, game_engine.ETurnPhases.SecondMainPhase, 2,
        has_quick_action=True)
    assert pvp_lifecycle.stack_pass_transition([], 7, [7, 8]) == {
        "action": "handoff", "passed": [7], "other_player": 8}
    assert pvp_lifecycle.stack_pass_transition([7], 7, [7, 8])["action"] == \
        "duplicate"
    assert pvp_lifecycle.stack_pass_transition([7], 8, [7, 8])["action"] == \
        "resolve"
    state = {"turn_pid": 7, "turn_number": 3, "bonus_turn_pid": 7,
             "autopass_pid": 7, "attackers": {1: 2}, "blockers": {1: [3]},
             "extra_combats_this_turn": {"7": [True]},
             "res_played_7": 1, "res_played_8": 1}
    result = pvp_lifecycle.advance_turn_state(state, [7, 8])
    assert result == {"turn_pid": 7, "bonus_used": True}
    assert state["turn_number"] == 4
    assert state["res_played_7"] == state["res_played_8"] == 0
    assert "attackers" not in state and "blockers" not in state
    projected = {"turn_pid": 7, "turn_number": 3,
                 "bonus_turn_pid": 7, "res_played_7": 1,
                 "res_played_8": 1}
    assert pvp_lifecycle.advance_turn_state(
        projected, [7, 8], incoming_player_id=8) == {
            "turn_pid": 8, "bonus_used": False}
    assert projected["turn_pid"] == 8
    assert projected["res_played_7"] == projected["res_played_8"] == 0
    initial = pvp_lifecycle.default_pvp_state(7, 7)
    assert initial["pvp"] and initial["phase"] == 3
    assert pvp_lifecycle.mulligan_transition(
        {"kept": [7]}, [7, 8], 7) == {
            "action": "prompt", "next_player": 8}
    assert pvp_lifecycle.mulligan_transition(
        {"kept": [7, 8]}, [7, 8], 8) == {
            "action": "start_turn", "next_player": None}
    state = {"res_7": 1, "res_total_7": 1, "chg_7": 0,
             "thresh_7": {"8": 1}}
    resource = play_resource_for_player(
        state, 7, 1, 1, threshold_color=8, charge_amount=0)
    assert resource.current.new_value == 2
    assert resource.total.new_value == 2
    assert state["thresh_7"] == {8: 2}
    assert state["res_played_7"] == 1
    payment = pay_resource_for_player(state, 7, 1)
    assert payment.old_value == 2 and payment.new_value == 1
    charge_payment = pay_charge_for_player(state, 7, 0)
    assert charge_payment.new_value == 0
    state["sp_7"] = 3
    spell_payment = pay_spell_points_for_player(state, 7, 2)
    assert spell_payment.old_value == 3 and spell_payment.new_value == 1
    canonical = {"ai_resources": 4, "ai_total_resources": 6,
                 "ai_charges": 2, "ai_spell_points": 3}
    assert pay_resource(canonical, "ai", 1).new_value == 3
    assert pay_counter(canonical, "ai", "chargepoints", 2).new_value == 0
    assert pay_counter(canonical, "ai", "spellpoints", 1).new_value == 2
    canonical["ai_resource_played_this_turn"] = True
    refill = begin_turn_resources(canonical, "ai")
    assert refill.new_value == canonical["ai_total_resources"]
    assert canonical["ai_resource_played_this_turn"] is False
    raw = {"res_7": 1, "res_total_7": 4, "res_played_7": 1}
    raw_refill = begin_turn_resources_for_player(raw, 7)
    assert raw_refill.new_value == 4 and raw["res_played_7"] == 0
    chain_state = {"_next_instance_id": 4, "stack": []}
    assert pvp_lifecycle.queue_stack_item(
        chain_state, {"kind": "spell"}) == 4
    assert chain_state["_next_instance_id"] == 5
    assert chain_state["stack"][0]["instance_id"] == 4

def test_pvp_runtime_facts_use_raw_player_checkpoint_keys():
    """The generic port validator must read the tournament state shape."""
    state = {"pvp": True, "res_7": 2, "res_total_7": 3,
             "chg_7": 1, "sp_7": 4, "thresh_7": {8: 1}}
    player = game_engine.UID.make(244, 7)
    opponent = game_engine.UID.make(244, 8)
    facts = PvpRuntimeFacts(1, state, player_uid=player, ai_uid=opponent)
    current = facts.get_player(player)
    assert current.current_resource_pool == 2
    assert current.resource_thresholds == {8: 1}
    assert current.charge_points == 1
    assert current.spell_points == 4
    card = RuntimeCard(
        101, "template", 7, "hand", game_engine.ECardCollections.Hand,
        int(game_engine.ECardTypes.Troop), 0, 0, 2,
        ({"color": 8, "amount": 1},), ())
    assert facts.can_play_card(card, player)
    assert not facts.can_play_card(card, opponent)


def test_can_attack_resolves_profile_owner_domain():
    """``can_attack`` must map the wire UID onto the ``game_cards`` owner.

    ``game_cards.user_id`` stores the profile id for the human, but the
    CommitTroopsToAttack transaction carries the typed ServicePlayer UID;
    comparing the raw decoded id rejected the owner's own troop and the client
    stayed stuck in Select Attackers.
    """
    profile_id = 6175190558117173535
    reck_id = 1925190388022160
    player = game_engine.UID.make(244, reck_id)
    opponent = game_engine.UID.make(3, 1000)
    facts = PvpRuntimeFacts(1, {}, player_uid=player, ai_uid=opponent)
    facts.player_owner_id = profile_id
    facts.ai_owner_id = 0
    facts.client_player_uid = player
    ready = int(game_engine.ECardStates.StartedATurnOnYourSide)
    attacker = RuntimeCard(
        1793, "template", profile_id, "warzone",
        game_engine.ECardCollections.Warzone,
        int(game_engine.ECardTypes.Troop), ready, 0, 2, (), ())
    assert facts.can_attack(attacker, None, player)
    assert not facts.can_attack(attacker, None, opponent)


def test_pvp_runtime_facts_price_ability_from_metadata_owner():
    """Cached two-human facts must not use the original attaching player."""
    state = {"pvp": True, "res_7": 0, "res_8": 3,
             "chg_7": 0, "chg_8": 0, "sp_7": 0, "sp_8": 0,
             "hp_7": 20, "hp_8": 20}
    player = game_engine.UID.make(244, 7)
    opponent = game_engine.UID.make(244, 8)
    facts = PvpRuntimeFacts(1, state, player_uid=player, ai_uid=opponent)
    ability = SimpleNamespace(
        metadata=SimpleNamespace(
            owner_id=8, source_uid=101,
            ability_template_id="ability",
            costs=SimpleNamespace(activation=2, variable_activation=0,
                                  charge_points=0, spell_points=0, life=0),
            activation=SimpleNamespace(x_cost=0)))
    assert facts.can_pay_ability_cost(ability)
    state["res_8"] = 1
    assert not facts.can_pay_ability_cost(ability)


def test_pvp_authoritative_session_syncs_native_kernel_ownership():
    state = {"pvp": True, "phase": game_engine.ETurnPhases.FirstMainPhase,
             "turn_pid": 7, "priority_pid": 8}
    session = SimpleNamespace(turn_order=state)
    snapshot = SQLiteRulesSnapshot(session)
    player = game_engine.UID.make(244, 7)
    opponent = game_engine.UID.make(244, 8)
    port = PvpAuthoritativeSession(
        1, (player, opponent), seed_z=1, seed_w=2, snapshot=snapshot)
    assert port.sync_from_pvp_state(state)
    assert port.current_turn_phase == game_engine.ETurnPhases.FirstMainPhase
    assert port.active_player_id == player
    assert port.action_stack.priority_player_id == opponent


def test_rules_session_factory_selects_pvp_host_for_pvp_checkpoint():
    from rules_port.adapter import rules_session_for
    player = game_engine.UID.make(244, 7)
    opponent = game_engine.UID.make(244, 8)
    session = SimpleNamespace(
        session_id=1, seed_z=1, seed_w=2,
        players=[(player, 0), (opponent, 0)],
        turn_order={"pvp": True, "phase": 10, "turn_pid": 7,
                    "priority_pid": 7},
        _persist=lambda *args, **kwargs: None)
    game = game_engine.Game(1, player, opponent)
    port = rules_session_for(session, game)
    assert isinstance(port, PvpAuthoritativeSession)
    assert port.current_turn_phase == game_engine.ETurnPhases.FirstMainPhase


def test_pvp_projected_card_uses_native_chain_and_response_action():
    player = game_engine.UID.make(244, 7)
    opponent = game_engine.UID.make(244, 8)
    port = PvpAuthoritativeSession(1, (player, opponent), seed_z=1, seed_w=2)
    port.sync_from_pvp_state({"pvp": True, "phase": 10,
                              "turn_pid": 7, "priority_pid": 8})
    item = port.queue_projected_chain(
        {"kind": "spell", "source_uid": 101, "instance_id": 9},
        7, first_player_id=opponent)
    assert port.chain.peek_ability() is item
    assert isinstance(port.action_stack.peek(), PriorityWindowAction)
    assert port.action_stack.priority_player_id == opponent


def test_pvp_native_pass_drives_projected_chain_after_both_passes():
    player = game_engine.UID.make(244, 7)
    opponent = game_engine.UID.make(244, 8)
    port = PvpAuthoritativeSession(1, (player, opponent), seed_z=1, seed_w=2)
    port.sync_from_pvp_state({"pvp": True, "phase": 10,
                              "turn_pid": 7, "priority_pid": 8})
    item = port.queue_projected_chain(
        {"kind": "ability", "source_uid": 101, "instance_id": 10},
        7, first_player_id=opponent)
    port.set_ability_resolver(
        lambda _ability: AbilityResolutionState.COMPLETED)

    assert port.pass_player_priority(opponent)
    assert port.action_stack.priority_player_id == player
    assert port.pass_player_priority(player)
    # This is the follow-up that the PvP transaction callback must perform:
    # passing the queue is not enough; the native action stack must be ticked
    # so ResolveTopOfChainAction can consume the item.
    for _ in range(8):
        if not port.tick():
            break
    assert port.chain.peek_ability() is None
    # The resolver may immediately enter the next native phase window; the
    # resolved chain item must be gone even when that window is now waiting.
    assert port.current_turn_phase != game_engine.ETurnPhases.FirstMainPhase
    assert port.action_stack.peek() is not None


def test_rules_session_sync_checkpoint_owns_practice_main_priority():
    player = game_engine.UID.make(244, 7)
    ai = game_engine.UID.make(244, 0)
    port = AuthoritativeSession(1, (player, ai), seed_z=1, seed_w=2)
    port.sync_checkpoint(
        phases=(game_engine.ETurnPhases.FirstMainPhase,),
        phase_idx=0, active_player_id=player, client_player_id=player,
        phase_facts={"has_legal_attackers": True,
                     "active_player_skips_attack": False})
    assert port.current_turn_phase == game_engine.ETurnPhases.FirstMainPhase
    assert port.active_player_id == player
    assert port.action_stack.priority_player_id == player
    assert port.has_legal_attackers
    assert not port.active_player_skips_attack
    assert isinstance(port.action_stack.peek(), PriorityWindowAction)
    assert port.pass_player_priority(player)


def test_sync_checkpoint_canonicalizes_raw_priority_for_native_pass():
    player = game_engine.UID.make(244, 73)
    ai = game_engine.UID.make(3, 1000)
    port = AuthoritativeSession(79, (player, ai), seed_z=1, seed_w=2)
    port.current_turn_phase = game_engine.ETurnPhases.SecondMainPhase
    port.active_player_id = ai
    window = PriorityWindowAction(TurnPhasePlayers.ALL)
    window._rules_port_phase = "SecondMainPhase"
    port.action_stack.push(window)
    window._priority_queue.clear()
    window._priority_queue.append(int(player.uid64))
    port.action_stack.priority_player_id = int(player.uid64)

    port.sync_checkpoint(
        phases=(game_engine.ETurnPhases.SecondMainPhase,), phase_idx=0,
        active_player_id=int(ai.uid64),
        client_player_id=int(player.uid64),
        ensure_main_priority=False, ensure_current_priority=False)

    assert port.active_player_id == ai
    assert port.action_stack.priority_player_id == player
    assert port.pass_player_priority(player)


def test_default_stops_match_client_set_default_turn_phases():
    """The default self/opponent stop sets must mirror the client's
    ``Player.SetDefaultTurnPhases``.

    That method does NOT add ``DeclareCombatPriorityWindow`` to either list,
    so the active player must auto-pass the pre-combat window and land in
    ``DeclareAttack`` where the attack UI lives.  The server previously listed
    it as a self stop, which halted the client in the "Declare Combat" window
    with no way to declare attackers.
    """
    from rules_port import lifecycle
    combat_window = game_engine.ETurnPhases.DeclareCombatPriorityWindow
    assert combat_window not in lifecycle.SELF_DEFAULT_STOPS
    assert combat_window not in lifecycle.OPP_DEFAULT_STOPS
    assert not lifecycle.is_self_stop({}, combat_window)
    assert not lifecycle.is_opp_stop({}, combat_window)
    assert practice_priority_players(
        {}, combat_window, active_is_player=True) is TurnPhasePlayers.NONE
    assert game_engine.ETurnPhases.FirstMainPhase in lifecycle.SELF_DEFAULT_STOPS
    assert game_engine.ETurnPhases.SecondMainPhase in lifecycle.OPP_DEFAULT_STOPS


def test_practice_stop_matrix_keeps_both_passes_native():
    player = game_engine.UID.make(244, 71)
    ai = game_engine.UID.make(3, 1000)
    phase = game_engine.ETurnPhases.SecondMainPhase
    first_main = game_engine.ETurnPhases.FirstMainPhase

    both = {"player_self_stops": [phase], "player_opp_stops": [phase]}
    assert practice_priority_players(
        both, phase, active_is_player=True) is TurnPhasePlayers.ALL
    assert practice_priority_players(
        {"player_self_stops": [first_main], "player_opp_stops": []},
        first_main, active_is_player=True) is TurnPhasePlayers.ACTIVE
    assert practice_priority_players(
        {"player_self_stops": [], "player_opp_stops": [first_main]},
        first_main, active_is_player=True) is TurnPhasePlayers.ALL
    assert practice_priority_players(
        {"player_opp_stops": [phase]}, phase,
        active_is_player=False) is TurnPhasePlayers.ALL
    assert practice_priority_players(
        {"player_opp_stops": []}, game_engine.ETurnPhases.FirstMainPhase,
        active_is_player=False) is TurnPhasePlayers.ACTIVE

    session = AuthoritativeSession(78, (player, ai), seed_z=1, seed_w=2)
    session.current_turn_phase = phase
    session.active_player_id = player
    session.action_stack.push(PriorityWindowAction(TurnPhasePlayers.ALL))
    assert session.action_stack.priority_player_id == player
    assert session.pass_player_priority(player)
    assert session.action_stack.priority_player_id == ai
    assert session.pass_player_priority(ai)
    session.drive_until_input()
    assert session.current_turn_phase == game_engine.ETurnPhases.EndPhase


def test_practice_phase_priority_normalizes_raw_and_typed_participants():
    player = game_engine.UID.make(244, 79)
    ai = game_engine.UID.make(3, 1000)
    state = {
        "player_self_stops": [game_engine.ETurnPhases.FirstMainPhase,
                               game_engine.ETurnPhases.SecondMainPhase],
        "player_opp_stops": [],
    }

    assert practice_phase_priority(
        state, game_engine.ETurnPhases.FirstMainPhase,
        active_player_id=int(player.uid64), player_id=player
    ) is TurnPhasePlayers.ACTIVE
    assert practice_phase_priority(
        state, game_engine.ETurnPhases.SecondMainPhase,
        active_player_id=player, player_id=int(player.uid64)
    ) is TurnPhasePlayers.ACTIVE
    assert practice_phase_priority(
        state, game_engine.ETurnPhases.SecondMainPhase,
        active_player_id=int(ai.uid64), player_id=player
    ) is TurnPhasePlayers.ACTIVE
    opponent_stop = dict(state, player_opp_stops=[
        game_engine.ETurnPhases.SecondMainPhase])
    assert practice_phase_priority(
        opponent_stop, game_engine.ETurnPhases.SecondMainPhase,
        active_player_id=int(ai.uid64), player_id=player
    ) is TurnPhasePlayers.ALL


def test_native_active_raw_uid_is_live_in_typed_participants():
    from hconnect_server import _uid_in

    player = game_engine.UID.make(PLAYER_UID_TYPE, 245)
    ai = game_engine.UID.make(AI_UID_TYPE, 1000)
    assert _uid_in(int(player.uid64), (player, ai))
    assert not _uid_in(int(game_engine.UID.make(AI_UID_TYPE, 1001).uid64),
                       (player, ai))


def test_rules_session_rehydrates_non_main_native_phase_window():
    player = game_engine.UID.make(244, 7)
    ai = game_engine.UID.make(3, 1000)
    port = AuthoritativeSession(1, (player, ai), seed_z=1, seed_w=2)
    port.sync_checkpoint(
        phases=(game_engine.ETurnPhases.Ready,), phase_idx=0,
        active_player_id=player, client_player_id=player,
        ensure_main_priority=False, ensure_current_priority=True)
    action = port.action_stack.peek()
    assert isinstance(action, PriorityWindowAction)
    assert action.priority_players is TurnPhasePlayers.ALL
    assert action.priority_player_id == player
    assert getattr(action, "_rules_port_phase") == "Ready"


def test_native_scheduler_drives_phase_entry_until_input():
    player = game_engine.UID.make(244, 47)
    session = AuthoritativeSession(77, (player,), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    entered = []
    session.set_turn_phase_entry_resolver(lambda phase: entered.append(phase))
    steps = session.drive_until_input()
    assert steps == 2
    assert entered == [game_engine.ETurnPhases.DeclareCombatPriorityWindow]
    assert isinstance(session.action_stack.peek(), PriorityWindowAction)


def test_native_draw_lifecycle_honors_first_player_draw_rule():
    assert should_draw_for_turn({"turn_number": 1,
                                 "player_draws_first_turn": True}, "player")
    assert not should_draw_for_turn({"turn_number": 1,
                                     "player_draws_first_turn": True}, "ai")
    assert should_draw_for_turn({"turn_number": 1,
                                 "player_draws_first_turn": False}, "ai")
    assert should_draw_for_turn({"turn_number": 2}, "player")


def test_native_complete_turn_resets_scheduler_state_for_next_owner():
    state = {"turn_player": "ai", "turn_number": 3, "phase_idx": 12,
             "turn_phases": ["old"], "player_passed": True,
             "ai_passed": True, "ai_turn_phase_idx": 8,
             "ai_attackers": {"1": "2"}, "ai_resource_played_this_turn": True,
             "player_resource_played_this_turn": True}
    assert complete_turn(state) == "player"
    assert state["turn_player"] == "player"
    assert state["turn_number"] == 4
    assert state["phase_idx"] == 0
    assert not state["player_passed"] and not state["ai_passed"]
    assert "ai_turn_phase_idx" not in state
    assert "ai_attackers" not in state
    assert not state["player_resource_played_this_turn"]


def test_native_scheduler_rotates_active_player_at_end_turn():
    player = game_engine.UID.make(244, 47)
    opponent = game_engine.UID.make(244, 0)
    session = AuthoritativeSession(
        78, (player, opponent), seed_z=1, seed_w=2)
    session.current_turn_phase = game_engine.ETurnPhases.EndTurn
    session.active_player_id = player
    session.action_stack.clear()
    assert session.advance_turn_phase() == game_engine.ETurnPhases.StartTurn
    assert session.active_player_id == opponent


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"PASS {len(tests)} rules-port kernel tests")
