"""Regression tests for persisted chat history retention."""

from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

import db
from services import chat


def test_chat_history_is_limited_to_last_24_hours():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE chat_messages ("
        "id INTEGER PRIMARY KEY, user_id INTEGER, sender TEXT, room TEXT, "
        "message TEXT, icon TEXT, flags TEXT, created_at TEXT)"
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = [
        (1, 1, "Recent", "global", "inside", "", "",
         (now - timedelta(hours=23, minutes=59)).strftime(
             "%Y-%m-%d %H:%M:%S")),
        (2, 1, "Old", "global", "outside", "", "",
         (now - timedelta(hours=24, minutes=1)).strftime(
             "%Y-%m-%d %H:%M:%S")),
        (3, 1, "Other", "trade", "wrong room", "", "",
         (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")),
    ]
    connection.executemany(
        "INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?)", rows)
    connection.commit()

    previous = db._db
    db._db = connection
    try:
        history = db.db_get_recent_chat("global", limit=30)
    finally:
        db._db = previous
        connection.close()

    assert [message["msg"] for message in history] == ["inside"]


def test_multiline_command_response_is_sent_as_separate_chat_messages():
    sent = []
    peer_sent = []

    class Handler:
        user_profile = {"name": "Tester", "id": 1}
        sid = "test-session"
        scnt = 0

        def _handle_chat_command(self, _command, _room, _username):
            return "=== Commands ===\n!one\n!two"

        def send(self, _headers, body=b""):
            sent.append(json.loads(body.decode("utf-8")))

    class Peer:
        authenticated = True
        _chat_rooms = {"global"}
        sid = "peer-session"
        scnt = 0

        def send(self, _headers, body=b""):
            peer_sent.append(json.loads(body.decode("utf-8")))

    handler = Handler()
    peer = Peer()
    fake_server = type("FakeServer", (), {
        "_active_clients": {1: [(handler, 0)], 2: [(peer, 0)]},
    })
    previous_server = sys.modules.get("hconnect_server")
    sys.modules["hconnect_server"] = fake_server
    try:
        chat._handle_rchat(handler, "global", {"msg": "!commands"})
    finally:
        if previous_server is None:
            sys.modules.pop("hconnect_server", None)
        else:
            sys.modules["hconnect_server"] = previous_server

    assert [message["msg"] for message in sent] == [
        "=== Commands ===", "!one", "!two"
    ]
    # The command branch sends only to the requesting handler; it must not
    # enter the ordinary room broadcast path.
    assert len(sent) == 3
    assert peer_sent == []


if __name__ == "__main__":
    test_chat_history_is_limited_to_last_24_hours()
    test_multiline_command_response_is_sent_as_separate_chat_messages()
    print("chat tests passed")
