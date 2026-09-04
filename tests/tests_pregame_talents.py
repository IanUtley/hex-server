"""Regression tests for metadata-driven PreGame deck insertions."""

import datetime
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db")
GREAT_SPORE_ABILITY = "9f000616-e866-ef3c-efa6-8b85b6079e80"
ZODIAC_ABILITY = "11483a8a-a568-ce6b-0d03-8d14ae49a373"


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
        os.environ["HEX_DB_PATH"] = path
        from tests.tests_combat import HandlerStub, SessionStub
        from abilities.framework.conditions import apply_pregame_abilities
        from abilities.framework import condition_engine
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

        # Zodiac Sands is authored for July/August (effect index 3).  The
        # month-scoped condition reads the real calendar, so pin the clock to a
        # deterministic July 1 to keep the focused test order-independent.
        condition_engine._FAKE_NOW = datetime.datetime(2026, 7, 1, 12, 0, 0)

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
            assert dict(rows).get("Zodiac Sands") == expected_count, rows
            assert sum(count for _, count in rows) == expected_count * 2, rows
            assert all(name in ("Great Spore Beast", "Zodiac Sands")
                       for name, _ in rows), rows
    finally:
        condition_engine._FAKE_NOW = None
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    test_skylak_uses_original_deck_size_for_both_talents()
    print("PASS Skylak PreGame deck insertions")
