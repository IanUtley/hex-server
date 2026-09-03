"""Focused tests for in-game debug chat commands."""

import sqlite3
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import commands
import game_engine


class SessionStub:
    session_id = 7
    session_name = "practice-test"


class HandlerStub:
    user_profile = {"id": 5}
    client_reck_id = 5

    def _card_full_data(self, _game, _scid, template_guid, _instance_id=None):
        return template_guid, "Troop", "Chosen Card", 1, 2, 3, 0


def test_top_moves_named_hand_card_to_deck_position_zero():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE card_templates (
            guid TEXT PRIMARY KEY, name TEXT, card_type TEXT);
        CREATE TABLE game_cards (
            session_id INTEGER, user_id INTEGER, card_uid INTEGER,
            template_guid TEXT, card_template_id TEXT, location TEXT,
            position INTEGER, card_state INTEGER);
    """)
    db.executemany("INSERT INTO card_templates VALUES (?, ?, ?)", [
        ("00000000-0000-0000-0000-000000000101", "Chosen Card", "Troop"),
        ("00000000-0000-0000-0000-000000000102", "Deck A", "Troop"),
        ("00000000-0000-0000-0000-000000000103", "Deck B", "Troop"),
    ])
    db.executemany("INSERT INTO game_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (7, 5, 101, "00000000-0000-0000-0000-000000000101", "00000000-0000-0000-0000-000000000101", "hand", 0, 0),
        (7, 5, 102, "00000000-0000-0000-0000-000000000102", "00000000-0000-0000-0000-000000000102", "deck", 0, 0),
        (7, 5, 103, "00000000-0000-0000-0000-000000000103", "00000000-0000-0000-0000-000000000103", "deck", 1, 0),
    ])
    db.commit()
    old_db = commands.hconnect_server._db
    old_send = commands._send_game_events
    commands.hconnect_server._db = db
    commands._send_game_events = lambda *_args: None
    try:
        result = commands._dispatch(
            HandlerStub(), "top", ["Chosen"], SessionStub(),
            game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000),
            "", "")
        assert result == "Put Chosen Card on top of deck", result
        rows = db.execute(
            "SELECT card_uid, location, position FROM game_cards "
            "WHERE session_id=7 AND user_id=5 ORDER BY position"
        ).fetchall()
        assert rows == [
            (101, "deck", 0), (102, "deck", 1), (103, "deck", 2)
        ], rows
    finally:
        commands.hconnect_server._db = old_db
        commands._send_game_events = old_send
        db.close()


if __name__ == "__main__":
    test_top_moves_named_hand_card_to_deck_position_zero()
    print("PASS !top command")
