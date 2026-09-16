"""Read-only capture adapter for persisted client transactions and events.

The runtime stores the exact 3029 payload and the resulting ordered session
events in SQLite.  This module deliberately does not decode or replay bytes;
it exposes stable records for parity tooling and leaves protocol decoding to
``application.player_transactions``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping


def _json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return dict(value) if isinstance(value, Mapping) else {}


def transaction_capture(connection: sqlite3.Connection, session_id: int | str,
                         *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
    """Return persisted transactions in receive order for ``session_id``.

    ``inner_bytes`` is retained as bytes (rather than guessed fields), while
    the server's classification and state hashes are made JSON-friendly.
    """
    sql = (
        "SELECT id, player_uid, received_seq, received_at, data_type, request_id, "
        "compressed, transaction_id, transaction_type, classification_json, "
        "inner_bytes, pre_state_hash, post_state_hash, status, handled, error "
        "FROM session_transactions WHERE session_id = ? ORDER BY id"
    )
    params: list[Any] = [str(session_id)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    records = []
    for row in connection.execute(sql, params):
        (row_id, player_uid, received_seq, received_at, data_type, request_id,
         compressed, transaction_id, transaction_type, classification, inner_bytes,
         pre_hash, post_hash, status, handled, error) = row
        records.append({
            "id": int(row_id), "player_uid": int(player_uid),
            "received_seq": int(received_seq), "received_at": received_at,
            "data_type": int(data_type), "request_id": int(request_id),
            "compressed": bool(compressed), "transaction_id": int(transaction_id),
            "transaction_type": transaction_type or "",
            "classification": _json(classification),
            "inner_bytes": bytes(inner_bytes or b""),
            "pre_state_hash": pre_hash or "", "post_state_hash": post_hash or "",
            "status": status or "", "handled": bool(handled), "error": error or "",
        })
    return tuple(records)


def event_capture(connection: sqlite3.Connection, session_id: int | str,
                  *, target_player_uid: int | str | None = None,
                  limit: int | None = None) -> tuple[dict[str, Any], ...]:
    """Return persisted events in sequence/id order for parity inspection."""
    sql = (
        "SELECT id, target_player_uid, seq, event_class, event_bytes, sent_at "
        "FROM session_events WHERE session_id = ?"
    )
    params: list[Any] = [str(session_id)]
    if target_player_uid is not None:
        sql += " AND target_player_uid = ?"
        params.append(str(target_player_uid))
    sql += " ORDER BY seq, id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return tuple({
        "id": int(row[0]), "target_player_uid": int(row[1]), "seq": int(row[2]),
        "event_class": int(row[3]), "event_bytes": bytes(row[4] or b""),
        "sent_at": row[5],
    } for row in connection.execute(sql, params))


def capture_session(connection: sqlite3.Connection, session_id: int | str,
                    *, transaction_limit: int | None = None,
                    event_limit: int | None = None) -> dict[str, Any]:
    """Build a complete, JSON-serialisable metadata capture (bytes base16)."""
    tx = transaction_capture(connection, session_id, limit=transaction_limit)
    events = event_capture(connection, session_id, limit=event_limit)
    return {
        "session_id": str(session_id),
        "transactions": [{**item, "inner_bytes": item["inner_bytes"].hex()}
                          for item in tx],
        "events": [{**item, "event_bytes": item["event_bytes"].hex()}
                   for item in events],
    }
