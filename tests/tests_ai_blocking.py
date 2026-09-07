"""Regression tests for the AI's defender block selection."""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import ai
import game_engine

from tests.tests_combat import HandlerStub, SessionStub


SRC = os.environ.get(
    "HEX_TEST_SOURCE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db"),
)


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


if __name__ == "__main__":
    test_incomplete_dogpile_is_not_committed()
    test_lethal_attack_uses_one_lowest_attack_chump()
    print("PASS AI blocking")
