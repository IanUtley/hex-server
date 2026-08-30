"""Regression tests for persisted chat history retention."""

from datetime import datetime, timedelta, timezone
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import db


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


if __name__ == "__main__":
    test_chat_history_is_limited_to_last_24_hours()
    print("chat tests passed")
