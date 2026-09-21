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


if __name__ == "__main__":
    test_incomplete_dogpile_is_not_committed()
    test_lethal_attack_uses_one_lowest_attack_chump()
    test_ai_block_declaration_queues_the_blocked_attackers_trigger()
    test_ai_turn_pauses_while_a_client_prompt_is_open()
    print("PASS AI blocking")
