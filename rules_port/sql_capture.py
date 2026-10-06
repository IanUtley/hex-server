"""Read-only capture adapter for persisted client transactions and packets.

The runtime stores exact 3029 payloads, typed intents, before/after SQLite
snapshots, and ordered 3055 packet batches in SQLite. This module deliberately
does not decode or replay bytes; it exposes stable records for parity tooling
and leaves protocol decoding to ``application.player_transactions``.
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
        "decoded_intent_json, raw_request_body, inner_bytes, pre_state_hash, pre_state_json, "
        "pre_rules_port_json, post_state_hash, post_state_json, "
        "post_rules_port_json, status, handled, error "
        "FROM session_transactions WHERE session_id = ? ORDER BY id"
    )
    params: list[Any] = [str(session_id)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    records = []
    for row in connection.execute(sql, params):
        (row_id, player_uid, received_seq, received_at, data_type, request_id,
         compressed, transaction_id, transaction_type, classification,
         decoded_intent, raw_request_body, inner_bytes, pre_hash, pre_snapshot,
         pre_rules_snapshot, post_hash, post_snapshot, post_rules_snapshot,
         status, handled, error) = row
        records.append({
            "id": int(row_id), "player_uid": int(player_uid),
            "received_seq": int(received_seq), "received_at": received_at,
            "data_type": int(data_type), "request_id": int(request_id),
            "compressed": bool(compressed), "transaction_id": int(transaction_id),
            "transaction_type": transaction_type or "",
            "classification": _json(classification),
            "decoded_intent": _json(decoded_intent),
            "raw_request_body": bytes(raw_request_body or b""),
            "inner_bytes": bytes(inner_bytes or b""),
            "pre_state_hash": pre_hash or "", "post_state_hash": post_hash or "",
            "pre_state": _json(pre_snapshot), "post_state": _json(post_snapshot),
            "pre_rules_port_state": _json(pre_rules_snapshot),
            "post_rules_port_state": _json(post_rules_snapshot),
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


def packet_capture(connection: sqlite3.Connection, session_id: int | str,
                   *, transaction_row_id: int | None = None,
                   limit: int | None = None) -> tuple[dict[str, Any], ...]:
    """Return packet batches, retaining empty acknowledgements and grouping."""
    sql = (
        "SELECT id, target_player_uid, seq, data_type, request_id, "
        "transaction_id, transaction_row_id, event_count, event_batch_json, "
        "projection_recorded, send_count, sent_body, sent_at, captured_at "
        "FROM session_packets WHERE session_id = ?"
    )
    params: list[Any] = [str(session_id)]
    if transaction_row_id is not None:
        sql += " AND transaction_row_id = ?"
        params.append(int(transaction_row_id))
    sql += " ORDER BY seq, id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    records = []
    for row in connection.execute(sql, params):
        (row_id, target_player_uid, seq, data_type, request_id, transaction_id,
         transaction_row_id, event_count, event_batch, projection_recorded,
         send_count, sent_body, sent_at, captured_at) = row
        records.append({
            "id": int(row_id), "target_player_uid": int(target_player_uid),
            "seq": int(seq), "data_type": int(data_type),
            "request_id": int(request_id), "transaction_id": int(transaction_id),
            "transaction_row_id": (int(transaction_row_id)
                                   if transaction_row_id is not None else None),
            "event_count": int(event_count), "events": json.loads(event_batch or "[]"),
            "projection_recorded": bool(projection_recorded),
            "send_count": int(send_count),
            "sent_body": bytes(sent_body).hex() if sent_body is not None else None,
            "sent_at": sent_at,
            "captured_at": captured_at,
        })
    return tuple(records)


def capture_session(connection: sqlite3.Connection, session_id: int | str,
                    *, transaction_limit: int | None = None,
                    event_limit: int | None = None) -> dict[str, Any]:
    """Build a complete, JSON-serialisable metadata capture (bytes base16)."""
    tx = transaction_capture(connection, session_id, limit=transaction_limit)
    events = event_capture(connection, session_id, limit=event_limit)
    packets = packet_capture(connection, session_id, limit=event_limit)
    return {
        "session_id": str(session_id),
        "transactions": [{**item,
                          "raw_request_body": item["raw_request_body"].hex(),
                          "inner_bytes": item["inner_bytes"].hex()}
                          for item in tx],
        "events": [{**item, "event_bytes": item["event_bytes"].hex()}
                   for item in events],
        "packets": list(packets),
    }
