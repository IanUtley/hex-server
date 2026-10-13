"""A discard picker answered during the AI's turn must let the AI continue.

Live symptom (Giant Corpse Fly's Deploy, cast by the AI in its First Main
Phase): the player discarded, the server logged "Discard answered — resuming
AI turn" and then "AI driver stopped: phase 10 was not entered by RulesPort",
and the game hung.  The continuation resolves the chain item directly, which
leaves its ResolveTopOfChainAction on the native action stack; the AI driver
only runs from the native phase window, so the scheduler has to be ticked
back to that window before the AI resumes.
"""

import os
import sys
import types
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()

import game_engine

from tests.tests_combat import (  # noqa: E402
    make_db, add_card, HandlerStub, SessionStub, TPL_GLADIATOR,
)


def test_ai_turn_resumes_from_native_phase_window_after_discard():
    import battle_engine
    import db as dbmod
    import hconnect_server as hcs
    from rules_port import resolution as resolution_mod
    from rules_port.actions import (AbilityResolutionState,
                                    ResolveTopOfChainAction)
    from rules_port.kernel import PriorityWindowAction
    from rules_port.phases import phase_name
    from rules_port.session import AuthoritativeSession

    db = make_db()
    discard_child = "06570445-27e3-fc87-2e17-a7b5e1de693d"  # "Discard a card"
    hand_card = 513
    add_card(db, hand_card, 5, TPL_GLADIATOR, loc="hand")
    db.commit()

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)

    # The AI's First Main Phase window, with the Deploy trigger on the chain
    # waiting for the human's discard choice.
    port = AuthoritativeSession(55, (pl_t, ai_t), seed_z=1, seed_w=2)
    port.active_player_id = ai_t
    port.current_turn_phase = game_engine.ETurnPhases.FirstMainPhase
    assert port.materialize_current_phase()
    resolver_state = {"value": AbilityResolutionState.WAITING_FOR_INPUT}
    port.set_ability_resolver(lambda ability: resolver_state["value"])
    deploy = types.SimpleNamespace(
        instance_id=7, is_triggered=True, ignores_chain=False,
        untargeted_trigger=False, metadata=None, paid=True,
        has_ongoing_effects=False)
    port.chain.push_ability(deploy)
    port.action_stack.push(ResolveTopOfChainAction(deploy))
    port.drive_until_input(max_steps=8)
    resolver_state["value"] = AbilityResolutionState.COMPLETED

    handler = HandlerStub(db)
    handler.client_reck_id = 5
    handler._checkpoint_engine = lambda session: battle_engine
    handler._send_battle_events = lambda *args, **kwargs: True
    handler._push_transaction_ack = lambda *args, **kwargs: None
    for name in ("_resolve_rules_port_discard_continuation",
                 "_completed_rules_port_chain",
                 "_resume_completed_rules_port_chain",
                 "_advance_rules_port_to_priority"):
        setattr(handler, name, types.MethodType(
            getattr(hcs.HCPHandler, name), handler))
    resumed = []

    def project(session, _pl, _ai, _state):
        # The AI driver's gate (ai.run_ai_turn): the top action must be the
        # native phase window for the current phase, owned by the AI.
        top = port.action_stack.peek()
        resumed.append((
            isinstance(top, PriorityWindowAction) and
            top._rules_port_phase == phase_name(port.current_turn_phase),
            hcs._same_uid(top.priority_player_id, ai_t)))
        return False

    handler._project_rules_port_priority = project
    handler._run_ai_turn = mock.Mock()

    session = SessionStub()
    session.session_name = ""
    session._rules_port_session = port
    session.turn_order = {
        "turn_player": "ai",
        "ai_turn_phase_idx": 5,
        "completed_chain_instance_id": 7,
        "pending_discard_continuation": {
            "ability_guid": discard_child, "source_uid": 5121,
            "owner_id": 5, "instance_id": 1, "resume_effect_order": 0,
            "target_map": {},
        },
        "pending_discard_target_template":
            "84e4acf1-1f2e-abac-069d-8c6eb18b2b12",
    }
    transaction = types.SimpleNamespace(payload={
        "activation_data": {"target_map": {0: (hand_card,)}}})

    old_db, old_hcs_db = dbmod._db, hcs._db
    dbmod._db = hcs._db = db
    try:
        with mock.patch.object(resolution_mod, "resolve_port_ability",
                               lambda *args, **kwargs: "ok"), \
                mock.patch.object(
                    resolution_mod, "resume_ability_continuation_parents",
                    lambda *args, **kwargs: "ok"):
            assert handler._resolve_rules_port_discard_continuation(
                session, transaction)
    finally:
        dbmod._db, hcs._db = old_db, old_hcs_db

    assert port.chain.is_empty
    assert resumed == [(True, True)], resumed
    handler._run_ai_turn.assert_not_called()
