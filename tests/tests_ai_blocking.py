"""Regression tests for the AI's defender block selection."""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

# Bind this process's test database before any runtime import
# opens ``db``; the live ``hconnect.db`` is never opened.
SRC = fresh_database()

import ai
import game_engine

from tests.tests_combat import HandlerStub, SessionStub




def _database_copy():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    source = sqlite3.connect(SRC)
    target = sqlite3.connect(path)
    source.backup(target)
    source.close()
    return target, path


def _add_card(db, uid, owner, template_guid):
    row = db.execute(
        "SELECT card_type, attributes FROM card_templates WHERE guid=?",
        (template_guid,),
    ).fetchone()
    assert row, template_guid
    db.execute(
        "INSERT INTO game_cards "
        "(user_id, session_id, card_uid, card_template_id, location, "
        "position, card_type, template_guid, card_state, card_attributes, "
        "card_abilities, card_uses) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (owner, 1, uid, template_guid, "warzone", uid, row[0],
         template_guid, 0, row[1] or 0, "[]", "{}"),
    )


def _template_with_stats(db, attack, defense):
    row = db.execute(
        "SELECT guid FROM card_templates WHERE card_type='Troop' "
        "AND attack=? AND defense=? "
        "AND (COALESCE(attributes, 0) & 1024) = 0 LIMIT 1",
        (attack, defense),
    ).fetchone()
    assert row, (attack, defense)
    return row[0]


def _run_defense(db, attacker_attack, attacker_defense, blocker_stats,
                 ai_health=20):
    attacker_tpl = _template_with_stats(db, attacker_attack, attacker_defense)
    _add_card(db, 100, 5, attacker_tpl)
    for index, (attack, defense) in enumerate(blocker_stats, start=1):
        _add_card(db, 200 + index, 0,
                  _template_with_stats(db, attack, defense))
    db.commit()

    session = SessionStub()
    session.session_id = 1
    handler = HandlerStub(db)
    game = game_engine.Game(
        1, game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000))
    bstate = {
        "player_attackers": {"100": "0"},
        "ai_health": ai_health,
    }
    old_db = ai._db
    ai._db = db
    try:
        ai.ai_pass_declare_defense(
            handler, session, game.player_uid, game.ai_uid, bstate, game)
    finally:
        ai._db = old_db
    return bstate.get("ai_blockers") or {}


def test_incomplete_dogpile_is_not_committed():
    db, path = _database_copy()
    try:
        # Two 0/1 blockers cannot kill or meaningfully trade with a 3/3.
        assert _run_defense(db, 3, 3, [(0, 1), (0, 1)]) == {}
    finally:
        db.close()
        os.unlink(path)


def test_blocker_is_never_assigned_to_two_attackers():
    """A troop blocks at most one attacker per combat.

    ``Card.CanBlock`` returns ``InCombat`` while ``ECardStates.Blocking`` is
    set and ``Session.AssignBlocker`` refuses an already blocking blocker, so
    the old AI MULTIBLOCK branch (reusing a blocker that could survive the
    second hit) let a single wall like Cavern Guard or Tribunal Magistrate
    block the whole attacking team.  Players reported exactly that.
    """
    db, path = _database_copy()
    try:
        attacker = _template_with_stats(db, 1, 1)
        _add_card(db, 100, 5, attacker)
        _add_card(db, 101, 5, attacker)
        _add_card(db, 201, 0, _template_with_stats(db, 0, 4))
        db.commit()

        session = SessionStub()
        session.session_id = 1
        handler = HandlerStub(db)
        game = game_engine.Game(
            1, game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000))
        bstate = {"player_attackers": {"100": "0", "101": "0"},
                  "ai_health": 2}
        old_db = ai._db
        ai._db = db
        try:
            ai.ai_pass_declare_defense(
                handler, session, game.player_uid, game.ai_uid, bstate, game)
        finally:
            ai._db = old_db
        blocks = bstate.get("ai_blockers") or {}
        blocker_uses = [b for blockers in blocks.values() for b in blockers]
        assert len(blocker_uses) == len(set(blocker_uses)), blocks
        assert blocks == {"100": ["201"]}, blocks
    finally:
        db.close()
        os.unlink(path)


def test_lethal_attack_uses_one_lowest_attack_chump():
    db, path = _database_copy()
    try:
        # The attack is lethal, but the blockers cannot combine to kill the
        # attacker.  Only the 0-attack troop should be sacrificed.
        blocks = _run_defense(db, 3, 3, [(0, 1), (2, 1)], ai_health=2)
        assert blocks == {"100": ["201"]}, blocks
    finally:
        db.close()
        os.unlink(path)


def test_ai_block_declaration_queues_the_blocked_attackers_trigger():
    """Block events fire with the declaration, not at the damage step.

    ``Session.EmitBlockerEvents`` names the blocker as the source and the
    blocked attacker as the target, so the attacker's "When this becomes
    blocked" ability (Nameless Citizen) queues before combat damage resolves.
    """
    import json
    db, path = _database_copy()
    try:
        citizen = "a0ed3464-ea1e-4f79-b206-d2675a965ceb"
        ability = "6c48c325-2fa0-11e3-d47e-c229d2b35c72"
        _add_card(db, 100, 5, citizen)
        db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=100",
                   (json.dumps([ability]),))
        _add_card(db, 201, 0, _template_with_stats(db, 4, 4))
        db.commit()
        session = SessionStub()
        session.session_id = 1
        handler = HandlerStub(db)
        game = game_engine.Game(
            1, game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000))
        bstate = {"player_attackers": {"100": "0"}, "ai_health": 20}
        old_db = ai._db
        ai._db = db
        try:
            ai.ai_pass_declare_defense(handler, session, game.player_uid,
                                       game.ai_uid, bstate, game)
        finally:
            ai._db = old_db
        assert bstate["ai_blockers"], "the AI should block a 4/2 attacker"
        items = bstate.get("stack") or []
        assert items, "the blocked attacker's trigger must be queued"
        assert items[0]["ability_guid"] == ability
        assert int(items[0]["target_uid"]) == 100
    finally:
        db.close()
        os.unlink(path)


def test_ai_turn_pauses_while_a_client_prompt_is_open():
    """A combat-death prompt owns the client's UI.

    The AI phase loop must not push its phase packet while one is open —
    TurnPhaseUpdated/PlayerOptionList would close the picker right after it
    opens, which is how a Bloatcap Deathcry discard never reached the player.
    """
    import ai

    assert ai._ai_turn_prompt_pending({"pending_deck_search": {"kind": "x"}})
    assert ai._ai_turn_prompt_pending(
        {"pending_discard_ability": "06570445-27e3-fc87-2e17-a7b5e1de693d"})
    assert not ai._ai_turn_prompt_pending({"pending_choice": None})
    assert not ai._ai_turn_prompt_pending(None)


def test_native_attack_declaration_survives_the_next_projection():
    """The AI's declaration must reach the packet Unity renders.

    With RulesPort attached the attackers are chosen during the native
    ``DeclareAttack`` phase entry, which publishes the class-27
    ``AttackDeclaredSessionEventArgs`` onto the port's current projection Game.
    Practice builds a fresh Game per packet, so the same phase walk replaces
    that projection (blocker options, then the AI loop) before anything is
    serialized.  Carrying the unsent queue keeps the declaration in the next
    packet; dropping it left Unity with no line from the troop to the champion
    (``UIBattle.OnAttackDeclared`` -> ``ELineConnectorType.Attack``), which is
    why blocking and unblocking redrew it from the client's own combat state.
    """
    from rules_port.session import GameEngineEventSink

    attacker = 0x4001
    defender = 0x101
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    # The transaction's projection: the native phase entry declares here.
    phase_game = game_engine.Game(1, pl_t, ai_t)
    phase_game.push_attack_declared(
        game_engine.CombatId(ai_t, attacker & 0xFFFF), ai_t,
        game_engine.SessionCardId(game_engine.UID(defender)),
        game_engine.SessionCardId(game_engine.UID(attacker)))
    # A later call in the same phase walk builds the next packet's Game.
    next_game = game_engine.Game(1, pl_t, ai_t)
    next_game.push_turn_phase(game_engine.ETurnPhases.DeclareDefense, ai_t,
                              pl_t)
    sink = GameEngineEventSink(phase_game)
    sink.game = next_game
    # Serializing the new projection drains the unpublished declaration.
    sink.drain_into(next_game)
    assert not phase_game.events, phase_game.events
    packet = next_game.make_network_packet(pl_t)
    declared = game_engine.AttackDeclaredSessionEventArgs.CLASS_ID
    assert declared in packet.event_ids, packet.event_ids
    # Publication order survives the move: the declaration precedes the events
    # the new projection queued for itself.
    assert packet.event_ids.index(declared) == 0, packet.event_ids
    # The carried event is delivered exactly once.
    later = next_game.make_network_packet(pl_t)
    assert declared not in later.event_ids, later.event_ids


if __name__ == "__main__":
    test_incomplete_dogpile_is_not_committed()
    test_blocker_is_never_assigned_to_two_attackers()
    test_lethal_attack_uses_one_lowest_attack_chump()
    test_ai_block_declaration_queues_the_blocked_attackers_trigger()
    test_ai_turn_pauses_while_a_client_prompt_is_open()
    test_native_attack_declaration_survives_the_next_projection()
    print("PASS AI blocking")
