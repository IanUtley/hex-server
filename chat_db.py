"""Chat persistence API."""

import db as _db_layer

from profile_db import display_name_from_identity


CHAT_HISTORY_HOURS = 24
DEFAULT_CHAT_HISTORY_LIMIT = 30
MAX_CHAT_MESSAGES_PER_ROOM = 500


def db_store_chat(user_id, sender, room, message, icon="", flags=""):
    connection = _db_layer._db
    connection.execute(
        "INSERT INTO chat_messages "
        "(user_id, sender, room, message, icon, flags) VALUES (?,?,?,?,?,?)",
        (user_id, sender, room, message, icon, flags))
    connection.commit()
    connection.execute(
        "DELETE FROM chat_messages WHERE room=? AND id NOT IN "
        "(SELECT id FROM chat_messages WHERE room=? ORDER BY id DESC LIMIT ?)",
        (room, room, MAX_CHAT_MESSAGES_PER_ROOM))
    connection.commit()


def db_get_recent_chat(room, limit=DEFAULT_CHAT_HISTORY_LIMIT):
    rows = _db_layer._db.execute(
        "SELECT sender, message, icon, flags, created_at FROM chat_messages "
        "WHERE room=? AND created_at >= datetime('now', ?) "
        "ORDER BY id DESC LIMIT ?",
        (room, f"-{CHAT_HISTORY_HOURS} hours", limit)).fetchall()
    return [{"user": row[0], "msg": row[1], "icon": row[2],
             "flags": row[3], "time": row[4]} for row in reversed(rows)]

__all__ = [
    "db_get_recent_chat", "db_store_chat", "display_name_from_identity",
]
