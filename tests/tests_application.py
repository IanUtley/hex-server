"""Focused tests for application transaction boundaries."""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from application import ApplicationCommandDispatcher
from application.commands import (ClaimMailCommand, DeleteMailCommand,
                                  JoinSessionCommand, RemoveSessionCommand,
                                  ServiceRequestCommand,
                                  MarkMailReadCommand,
                                  SetSessionStateCommand,
                                  StartEncounterCommand, StartSessionCommand)
from application.results import SessionRemoved
from application.player_transactions import classify_player_transaction
import db


def _make_db():
    fd, path = tempfile.mkstemp(prefix="hex-application-", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE game_sessions (
            session_id TEXT PRIMARY KEY,
            server_id TEXT,
            session_name TEXT UNIQUE,
            owner_uid TEXT,
            state TEXT,
            encounter_data TEXT,
            players_json TEXT,
            turn_order_json TEXT,
            seed_z INTEGER,
            seed_w INTEGER,
            deck_template_id TEXT,
            created_at TEXT
        )
    """)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER)")
    conn.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            gold INTEGER NOT NULL,
            platinum INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE emails (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            gold_delivered INTEGER,
            platinum_delivered INTEGER,
            read_at TEXT,
            claimed_at TEXT
        )
    """)
    conn.execute("INSERT INTO users VALUES (1, 100, 20)")
    conn.execute("INSERT INTO emails VALUES (10, 1, 25, 3, NULL, NULL)")
    conn.execute(
        "INSERT INTO game_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("1", "2", "session-1", "244", "created", "{}",
         json.dumps([[500, 0]]), "[]", 1, 2, "", "2026-01-01"))
    conn.commit()
    conn.close()
    return path


def test_remove_session_commits_and_publishes_after_commit():
    path = _make_db()
    published = []
    try:
        dispatcher = ApplicationCommandDispatcher(
            event_publisher=published.extend,
            database_path=path,
        )
        result = dispatcher.execute(RemoveSessionCommand(500))

        assert result.value == "session-1"
        assert len(published) == 1
        assert isinstance(published[0], SessionRemoved)

        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM game_sessions").fetchone()[0] == 0
        conn.close()
    finally:
        os.unlink(path)


def test_transaction_rolls_back_on_failure():
    path = _make_db()
    try:
        try:
            with db.transaction(path) as conn:
                conn.execute("DELETE FROM game_sessions")
                raise RuntimeError("simulated command failure")
        except RuntimeError:
            pass

        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM game_sessions").fetchone()[0] == 1
        conn.close()
    finally:
        os.unlink(path)


def test_service_request_dispatch_passes_the_command_envelope():
    command = ServiceRequestCommand(
        target="ServiceGameSession",
        instance="1",
        data_type=3029,
        request_id=7,
        compressed=1,
        session_id="session-1",
        connection_handle="connection-1",
        inner_object={"__type__": "PassPriorityTransaction"},
        inner_bytes=b"payload",
    )
    received = []
    result = ApplicationCommandDispatcher.dispatch_request(
        command, lambda request: received.append(request) or "handled")

    assert result == "handled"
    assert received == [command]


def test_session_lifecycle_commands_share_one_transaction():
    path = _make_db()
    try:
        dispatcher = ApplicationCommandDispatcher(database_path=path)
        started = dispatcher.execute(StartSessionCommand("session-2", 700))
        session = started.value
        assert session.session_name == "session-2"
        assert session.players == [(700, 0)]

        joined = dispatcher.execute(JoinSessionCommand(session.session_id, 800))
        assert joined.value.players == [(700, 0), (800, 0)]

        changed = dispatcher.execute(SetSessionStateCommand(700, "setup"))
        assert changed.value.state == "setup"

        encounter = dispatcher.execute(StartEncounterCommand(
            "session-3", {"encounter": 1}, 900))
        assert encounter.value.encounter_data == {"encounter": 1}
    finally:
        os.unlink(path)


def test_player_transaction_classifier_is_side_effect_free_and_typed():
    raw = (b"PassPriorityTransaction;AcceptStartingHand;"
           b"m_TransactionId;0;0;0;0000000a;"
           b"m_QuitEntireSeries;0;0;0;False;")
    command = classify_player_transaction(raw)

    assert command.is_pass_priority is True
    assert command.is_mulligan_keep is True
    assert command.is_mulligan_redraw is False
    assert command.transaction_id == 10
    assert command.quit_series == "False"


def test_mail_commands_commit_related_mutations_together():
    path = _make_db()
    try:
        dispatcher = ApplicationCommandDispatcher(database_path=path)
        dispatcher.execute(MarkMailReadCommand(1))
        claimed = dispatcher.execute(ClaimMailCommand(1, 10)).value
        assert claimed == {"gold": 25, "platinum": 3}

        conn = sqlite3.connect(path)
        user = conn.execute(
            "SELECT gold, platinum FROM users WHERE id=1").fetchone()
        email = conn.execute(
            "SELECT read_at, claimed_at FROM emails WHERE id=10").fetchone()
        assert user == (125, 23)
        assert email[0] is not None and email[1] is not None

        dispatcher.execute(DeleteMailCommand(1))
        assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0
        conn.close()
    finally:
        os.unlink(path)


def main():
    test_remove_session_commits_and_publishes_after_commit()
    test_transaction_rolls_back_on_failure()
    test_service_request_dispatch_passes_the_command_envelope()
    test_session_lifecycle_commands_share_one_transaction()
    test_player_transaction_classifier_is_side_effect_free_and_typed()
    test_mail_commands_commit_related_mutations_together()
    print("application transaction tests passed")


if __name__ == "__main__":
    main()
