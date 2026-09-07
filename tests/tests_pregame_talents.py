"""Regression tests for metadata-driven PreGame deck insertions."""

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

SRC = os.environ.get(
    "HEX_TEST_SOURCE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db"),
)
GREAT_SPORE_ABILITY = "9f000616-e866-ef3c-efa6-8b85b6079e80"
ZODIAC_ABILITY = "11483a8a-a568-ce6b-0d03-8d14ae49a373"
ZODIAC_BY_MONTH = {
    1: "Zodiac Dream", 2: "Zodiac Dream",
    3: "Zodiac Plainsrunner", 4: "Zodiac Plainsrunner",
    5: "Zodiac Thunderbird", 6: "Zodiac Thunderbird",
    7: "Zodiac Sands", 8: "Zodiac Sands",
    9: "Zodiac Observer", 10: "Zodiac Observer",
    11: "Zodiac Sister Midnight", 12: "Zodiac Sister Midnight",
}

EXPENDABLE_ABILITY = "280285bb-378b-3c88-9d85-11dc25ae8ad7"
EXPENDABLE_GRANT = "89285cf9-97ba-5b40-3a91-ba14ecfccd2a"


def _database_copy():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    source = sqlite3.connect(SRC)
    target = sqlite3.connect(path)
    source.backup(target)
    source.close()
    return target, path


def test_skylak_uses_original_deck_size_for_both_talents():
    db, path = _database_copy()
    try:
        expected_zodiac = ZODIAC_BY_MONTH[datetime.now().month]
        from tests.tests_combat import HandlerStub, SessionStub
        from abilities.framework.conditions import apply_pregame_abilities
        from db import db_backfill_ability_effect_meta
        import game_engine

        template_guid, card_type = db.execute(
            "SELECT guid, card_type FROM card_templates "
            "WHERE name='Wild Shard' LIMIT 1").fetchone()
        handler = HandlerStub(db)
        # The checked-in fixture predates the complete parent-level effect
        # wiring. Exercise the same repair that startup applies to an
        # existing database before resolving the talent.
        db_backfill_ability_effect_meta(db)
        player_uid = game_engine.UID.make(244, 5)
        ai_uid = game_engine.UID.make(3, 1000)
        abilities = [GREAT_SPORE_ABILITY, ZODIAC_ABILITY]

        for session_id, initial_count, expected_count in (
                (1, 99, 1), (2, 100, 2)):
            db.execute("DELETE FROM game_cards WHERE session_id=?", (session_id,))
            for index in range(initial_count):
                db.execute(
                    "INSERT INTO game_cards "
                    "(id,user_id,session_id,card_uid,card_template_id,location,"
                    "position,card_type,template_guid,card_state,card_attributes,"
                    "card_abilities) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id * 10000 + index, 5, session_id,
                     session_id * 100000 + index, template_guid, "deck", index,
                     card_type, template_guid, 0, 0, "[]"))
            db.commit()
            session = SessionStub()
            session.session_id = session_id
            game = game_engine.Game(session_id, player_uid, ai_uid)
            apply_pregame_abilities(
                game, session, db, handler, player_uid, 5, abilities,
                "player_health")
            rows = db.execute(
                "SELECT ct.name, COUNT(*) FROM game_cards gc "
                "JOIN card_templates ct ON ct.guid=gc.template_guid "
                "WHERE gc.session_id=? AND gc.user_id=5 AND gc.location='deck' "
                "AND ct.name IN ('Great Spore Beast','Zodiac Sands',"
                "'Zodiac Dream','Zodiac Plainsrunner','Zodiac Thunderbird',"
                "'Zodiac Observer','Zodiac Sister Midnight') GROUP BY ct.name",
                (session_id,)).fetchall()
            assert dict(rows).get("Great Spore Beast") == expected_count, rows
            assert dict(rows).get(expected_zodiac) == expected_count, rows
            assert sum(count for _, count in rows) == expected_count * 2, rows
            assert all(name in ("Great Spore Beast", expected_zodiac)
                       for name, _ in rows), rows
    finally:
        db.close()
        os.unlink(path)


def test_expendable_lives_grants_and_resolves_one_shot_deathcry():
    """Shin'hare's Expendable Lives resolves through one GameStarted path."""
    db, path = _database_copy()
    try:
        from abilities.framework.triggers import (
            resolve_stack_trigger, resolve_triggers)
        from abilities.framework.kill_troop import kill_troop
        from tests.tests_combat import HandlerStub, SessionStub
        import game_engine

        db.execute("DELETE FROM game_cards WHERE session_id=1")
        template_guid, card_type = db.execute(
            "SELECT guid, card_type FROM card_templates "
            "WHERE card_type='Troop' LIMIT 1").fetchone()
        db.execute(
            "INSERT INTO game_cards "
            "(user_id,session_id,card_uid,card_template_id,location,position,"
            "card_type,template_guid,card_state,card_abilities,card_attributes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (5, 1, 10001, template_guid, "hand", 0, card_type,
             template_guid, 0, "[]", 0))
        db.commit()

        session = SessionStub()
        handler = HandlerStub(db)
        handler._player_champ_abilities = [EXPENDABLE_ABILITY]
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        game = game_engine.Game(1, pl_t, ai_t)
        bstate = {"player_health": 20, "ai_health": 20,
                  "_next_instance_id": 1}
        handler._current_bstate = bstate

        # The typed GrantAbility is discovered from the champion's configured
        # ability list and its authored random-hand target.  It must be
        # pushed exactly once by the normal GameStarted dispatcher.  The
        # random target is resolved when this chain item resolves, matching
        # the client's target-instance timing.
        resolve_triggers(
            db, handler, game, session, pl_t, ai_t, bstate,
            "GameStartedEvent", None, 5, zones=("hand", "warzone"))
        stack = list(bstate.get("stack") or [])
        assert len(stack) == 1, stack
        resolve_stack_trigger(
            handler, game, session, db, pl_t, ai_t, bstate, stack[0])
        current = db.execute(
            "SELECT card_abilities FROM game_cards WHERE card_uid=10001"
        ).fetchone()[0]
        assert EXPENDABLE_GRANT in json.loads(current), current

        kill_troop(
            game, session, db, handler, pl_t, ai_t, 10001, bstate,
            cause="damage")
        location, current = db.execute(
            "SELECT location, card_abilities FROM game_cards "
            "WHERE card_uid=10001").fetchone()
        assert location == "warzone", location
        assert EXPENDABLE_GRANT not in json.loads(current), current
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    test_skylak_uses_original_deck_size_for_both_talents()
    test_expendable_lives_grants_and_resolves_one_shot_deathcry()
    print("PASS Skylak PreGame deck insertions")
