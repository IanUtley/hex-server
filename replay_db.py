"""Replay index and durable event-stream persistence API."""

import hashlib
import json
import hashlib
import os
import struct
import time
from contextlib import contextmanager
from contextvars import ContextVar

import db as _db_layer


_session_transaction_trace = ContextVar(
    "hex_session_transaction_trace", default=None)


@contextmanager
def session_transaction_trace(request_id, session_id=None):
    """Correlate synchronous 3055 projections with their inbound 3029."""
    token = _session_transaction_trace.set({
        "request_id": int(request_id or 0),
        "session_id": str(session_id) if session_id is not None else None,
        "transaction_id": -1,
        "transaction_row_id": None,
    })
    try:
        yield
    except Exception as exc:
        # A failing transaction must remain inspectable even when control
        # unwinds before HConnect reaches its normal post-state capture. Keep
        # this diagnostic path best-effort so it never masks the gameplay
        # exception that caused the failure.
        try:
            db_fail_session_transaction_trace(exc)
        except Exception:
            pass
        raise
    finally:
        _session_transaction_trace.reset(token)


def set_session_transaction_trace_ids(*, transaction_id=None,
                                      transaction_row_id=None,
                                      session_id=None):
    """Add decoded identifiers to the current request context."""
    context = _session_transaction_trace.get()
    if context is None:
        return
    updated = dict(context)
    if transaction_id is not None:
        updated["transaction_id"] = int(transaction_id)
    if transaction_row_id is not None:
        updated["transaction_row_id"] = int(transaction_row_id)
    if session_id is not None:
        updated["session_id"] = str(session_id)
    _session_transaction_trace.set(updated)


def session_transaction_trace_state():
    """Return a copy of the active request correlation, if any."""
    context = _session_transaction_trace.get()
    return dict(context) if context is not None else None


def db_get_replay_candidates(conn=None):
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gs.session_id, gs.session_name, gs.server_id, gs.state, "
        "gs.players_json, gs.created_at FROM game_sessions gs "
        "JOIN session_events se ON se.session_id=gs.session_id "
        "WHERE (gs.session_name LIKE 'tourney-%' OR gs.session_name LIKE 'pvp-%' "
        "OR gs.session_name LIKE 'Challenge_%') "
        "AND (gs.state='ended' OR se.event_class=2) GROUP BY gs.session_id "
        "UNION SELECT gr.session_id, gr.session_name, gr.server_id, 'ended', "
        "gr.players_json, gr.start_time FROM game_replays gr "
        "JOIN session_events se ON se.session_id=gr.session_id "
        "WHERE gr.status IN ('stale', 'error') AND "
        "(gr.session_name LIKE 'tourney-%' OR gr.session_name LIKE 'pvp-%' "
        "OR gr.session_name LIKE 'Challenge_%') GROUP BY gr.session_id"
    ).fetchall()


def db_get_replay_events(session_id, conn=None):
    return (conn or _db_layer._db).execute(
        "SELECT id, target_player_uid, seq, event_class, event_bytes "
        "FROM session_events WHERE session_id=? ORDER BY seq, id", (session_id,)
    ).fetchall()


def db_get_replay_source(session_id, conn=None):
    return (conn or _db_layer._db).execute(
        "SELECT source_event_max_id, status FROM game_replays WHERE session_id=?",
        (session_id,)).fetchone()


def db_get_replay_match(session_id, conn=None):
    return (conn or _db_layer._db).execute(
        "SELECT tournament_id, round_id, player1_uid, player2_uid, game1_winner "
        "FROM tournament_matches WHERE session_id=? LIMIT 1", (session_id,)
    ).fetchone()


def db_get_tournament_signup_names(tournament_id, conn=None):
    return (conn or _db_layer._db).execute(
        "SELECT player_uid, player_name FROM tournament_signups "
        "WHERE tournament_id=?", (tournament_id,)).fetchall()


def db_upsert_replay(values, conn=None):
    (conn or _db_layer._db).execute(
        "INSERT INTO game_replays "
        "(session_id,session_name,server_id,session_flags,start_time,end_time,"
        "tournament_round,is_public,series_format,series_points,series_template,"
        "players_json,winners_json,replay_path,generation_count,event_count,"
        "source_event_max_id,status,error,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(session_id) DO UPDATE SET session_name=excluded.session_name,"
        "server_id=excluded.server_id,session_flags=excluded.session_flags,"
        "start_time=excluded.start_time,end_time=excluded.end_time,"
        "tournament_round=excluded.tournament_round,is_public=excluded.is_public,"
        "series_format=excluded.series_format,series_points=excluded.series_points,"
        "series_template=excluded.series_template,players_json=excluded.players_json,"
        "winners_json=excluded.winners_json,replay_path=excluded.replay_path,"
        "generation_count=excluded.generation_count,event_count=excluded.event_count,"
        "source_event_max_id=excluded.source_event_max_id,status=excluded.status,"
        "error=excluded.error,updated_at=datetime('now')", values)


def db_get_replay_list_rows(filters, conn=None):
    return (conn or _db_layer._db).execute(
        "SELECT session_name,server_id,start_time,end_time,series_format,"
        "series_points,series_template,is_public,players_json,tournament_round "
        "FROM game_replays WHERE status='ready' AND session_name LIKE ? "
        "AND series_format LIKE ? AND series_template LIKE ? "
        "ORDER BY end_time DESC LIMIT ? OFFSET ?", filters).fetchall()


def db_get_replay_path(session_name, conn=None):
    return (conn or _db_layer._db).execute(
        "SELECT replay_path FROM game_replays "
        "WHERE session_name=? AND status='ready'", (session_name,)).fetchone()


def db_cleanup_old_replays(age_days=30, conn=None):
    """Remove expired replay indexes and their durable source streams."""
    try:
        days = max(1, int(age_days))
    except (TypeError, ValueError):
        days = 30
    connection = conn or _db_layer.connect()
    owns = conn is None
    paths = []
    removed_events = 0
    removed_transactions = 0
    try:
        rows = connection.execute(
            "SELECT session_id, replay_path FROM game_replays "
            "WHERE status='ready' AND end_time IS NOT NULL "
            "AND datetime(end_time) <= datetime('now', ?)",
            (f"-{days} days",)).fetchall()
        for session_id, path in rows:
            if path:
                paths.append(str(path))
            cursor = connection.execute(
                "DELETE FROM session_events WHERE CAST(session_id AS TEXT)=?",
                (str(session_id),))
            removed_events += int(cursor.rowcount or 0)
            connection.execute(
                "DELETE FROM session_packets WHERE CAST(session_id AS TEXT)=?",
                (str(session_id),))
            cursor = connection.execute(
                "DELETE FROM session_transactions WHERE CAST(session_id AS TEXT)=?",
                (str(session_id),))
            removed_transactions += int(cursor.rowcount or 0)
            connection.execute(
                "DELETE FROM game_replays WHERE CAST(session_id AS TEXT)=?",
                (str(session_id),))
        if owns:
            connection.commit()
        return {"replays_removed": len(rows),
                "session_events_removed": removed_events,
                "session_transactions_removed": removed_transactions,
                "paths": paths}
    except BaseException:
        if owns:
            connection.rollback()
        raise
    finally:
        if owns:
            connection.close()


def db_session_state_snapshot(session_id):
    """Serialize authoritative SQLite session state for a diagnostic trace."""
    capture_db, owns_connection = _db_layer._session_capture_connection()
    try:
        session_columns = [row[1] for row in capture_db.execute(
            "PRAGMA table_info(game_sessions)")]
        session_row = capture_db.execute(
            "SELECT * FROM game_sessions WHERE session_id=?",
            (str(session_id),)).fetchone()
        if session_row is None or not session_columns:
            return ""
        card_columns = [row[1] for row in capture_db.execute(
            "PRAGMA table_info(game_cards)")]
        card_rows = capture_db.execute(
            "SELECT * FROM game_cards WHERE session_id=? ORDER BY id",
            (str(session_id),)).fetchall()
        snapshot = {"session": dict(zip(session_columns, session_row)),
                    "cards": [dict(zip(card_columns, row)) for row in card_rows]}
        return json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                          default=str)
    finally:
        if owns_connection:
            capture_db.close()


def db_session_state_hash(session_id):
    snapshot = db_session_state_snapshot(session_id)
    if not snapshot:
        return ""
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


def db_rules_port_state_snapshot(session):
    """Serialize the in-memory RulesPort state attached to a live session."""
    port = getattr(session, "_rules_port_session", None)
    if port is None:
        return ""
    snapshot = getattr(port, "parity_state", None)
    if not callable(snapshot):
        snapshot = getattr(port, "snapshot", None)
    if not callable(snapshot):
        return ""
    try:
        return json.dumps(snapshot(), sort_keys=True, separators=(",", ":"),
                          default=str)
    except Exception:
        return ""


def db_record_session_transaction(session_id, player_uid, request_id,
                                  data_type, compressed, transaction_id,
                                  transaction_type, classification, inner_bytes,
                                  pre_state_hash, *, decoded_intent=None,
                                  raw_data_wrapper_bytes=b"",
                                  pre_state_json="",
                                  pre_rules_port_json=""):
    payload = inner_bytes if isinstance(inner_bytes, bytes) else b""
    raw_body = (raw_data_wrapper_bytes
                if isinstance(raw_data_wrapper_bytes, bytes)
                else b"")
    capture_db, owns_connection = _db_layer._session_capture_connection()
    try:
        cursor = capture_db.execute(
            "INSERT INTO session_transactions "
            "(session_id, player_uid, received_seq, data_type, request_id, "
            "compressed, transaction_id, transaction_type, classification_json, "
            "decoded_intent_json, raw_request_body, inner_bytes, "
            "pre_state_hash, pre_state_json, pre_rules_port_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(session_id), str(player_uid), time.time_ns(), int(data_type),
             int(request_id or 0), int(compressed or 0), int(transaction_id),
             str(transaction_type or ""),
             json.dumps(classification or {}, sort_keys=True, default=str),
             json.dumps(decoded_intent or {}, sort_keys=True, default=str),
             raw_body, payload, str(pre_state_hash or ""),
             str(pre_state_json or ""),
             str(pre_rules_port_json or "")))
        capture_db.commit()
        return int(cursor.lastrowid or 0)
    except BaseException:
        capture_db.rollback()
        raise
    finally:
        if owns_connection:
            capture_db.close()


def db_complete_session_transaction(row_id, post_state_hash, handled, error="",
                                    post_state_json="",
                                    post_rules_port_json=""):
    capture_db, owns_connection = _db_layer._session_capture_connection()
    try:
        capture_db.execute(
            "UPDATE session_transactions SET post_state_hash=?, "
            "post_state_json=?, post_rules_port_json=?, status=?, handled=?, "
            "completed_at=datetime('now'), error=? WHERE id=?",
            (str(post_state_hash or ""), str(post_state_json or ""),
             str(post_rules_port_json or ""),
             "completed" if not error else "error",
             1 if handled else 0, str(error or ""), int(row_id)))
        capture_db.commit()
    except BaseException:
        capture_db.rollback()
        raise
    finally:
        if owns_connection:
            capture_db.close()


def db_fail_session_transaction_trace(error):
    """Finalize the active captured transaction after an uncaught error.

    Normal 3029 handling records a richer RulesPort post-state at its exit.
    This fallback stores the persisted state and exception when that path
    unwinds early, preserving the original exception for HConnect's logger.
    """
    trace = _session_transaction_trace.get()
    row_id = trace.get("transaction_row_id") if trace else None
    if row_id is None:
        return False
    session_id = trace.get("session_id")
    post_state_json = (db_session_state_snapshot(session_id)
                       if session_id is not None else "")
    post_state_hash = (hashlib.sha256(post_state_json.encode("utf-8")).hexdigest()
                       if post_state_json else "")
    error_text = f"{type(error).__name__}: {error}"
    db_complete_session_transaction(
        row_id, post_state_hash, False, error=error_text,
        post_state_json=post_state_json)
    return True


def db_mark_session_packet_sent(session_id, target_player_uid, body):
    """Record successful delivery of the active transaction's 3055 packet.

    Called only after ``socket.sendall`` returns. The trace association is
    request-local and packet projections are matched in build order for the
    same session and recipient. A 3055 sent without a matching projection gets
    a send-only row so acknowledgement counts remain observable. Capture
    failures are handled by the caller so they cannot affect gameplay.
    """
    trace = _session_transaction_trace.get()
    if not trace or trace.get("transaction_row_id") is None:
        return False
    capture_db, owns_connection = _db_layer._session_capture_connection()
    try:
        cursor = capture_db.execute(
            "UPDATE session_packets SET send_count=send_count+1, sent_body=?, "
            "sent_at=datetime('now') WHERE id=(SELECT id FROM session_packets "
            "WHERE session_id=? AND target_player_uid=? AND data_type=3055 "
            "AND transaction_row_id=? AND send_count=0 "
            "ORDER BY seq, id LIMIT 1)",
            (bytes(body), str(session_id), str(target_player_uid),
             int(trace["transaction_row_id"])))
        recorded = cursor.rowcount > 0
        if not recorded:
            capture_db.execute(
                "INSERT INTO session_packets "
                "(session_id, target_player_uid, seq, data_type, request_id, "
                "transaction_id, transaction_row_id, event_count, "
                "event_batch_json, projection_recorded, send_count, sent_body, "
                "sent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (str(session_id), str(target_player_uid), time.time_ns(), 3055,
                 int(trace.get("request_id", 0) or 0),
                 int(trace.get("transaction_id", -1) or -1),
                 int(trace["transaction_row_id"]), 0, "[]", 0, 1,
                 bytes(body)))
            recorded = True
        capture_db.commit()
        return recorded
    except BaseException:
        capture_db.rollback()
        raise
    finally:
        if owns_connection:
            capture_db.close()


_REPLAYABLE_PVP_SESSION_PREFIXES = ("tourney-", "pvp-", "Challenge_")


def _is_replayable_pvp_session(session_id):
    row = _db_layer._db.execute(
        "SELECT session_name FROM game_sessions WHERE session_id=?",
        (str(session_id),)).fetchone()
    return bool(row and (row[0] or "").startswith(_REPLAYABLE_PVP_SESSION_PREFIXES))


def _record_session_events(session_id, target_player_uid, event_byte_list):
    try:
        sid = session_id.to_uint64() if hasattr(session_id, "to_uint64") else session_id
        tid = (target_player_uid.to_uint64()
               if hasattr(target_player_uid, "to_uint64") else target_player_uid)
        replayable = _is_replayable_pvp_session(sid)
        capture_any_session = os.environ.get(
            "HEX_CAPTURE_SESSION_TRACES", "").strip().lower() in {
                "1", "true", "yes", "on"}
        if not replayable and not capture_any_session:
            return

        encoded_events = []
        for raw in event_byte_list:
            raw = bytes(raw)
            event_class = (struct.unpack("<i", raw[:4])[0]
                           if len(raw) >= 4 else 0)
            encoded_events.append({"event_class": event_class,
                                   "event_bytes": raw.hex()})

        # Keep packet boundaries and empty 3055 acknowledgements. The legacy
        # replay table below intentionally remains one row per non-empty event.
        trace = _session_transaction_trace.get() or {}
        transaction_row_id = trace.get("transaction_row_id")
        request_id = int(trace.get("request_id", 0) or 0)
        transaction_id = int(trace.get("transaction_id", -1) or -1)
        capture_db, owns_connection = _db_layer._session_capture_connection()
        try:
            if transaction_row_id is None and request_id:
                match = capture_db.execute(
                    "SELECT id FROM session_transactions WHERE session_id=? "
                    "AND request_id=? AND transaction_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (str(sid), request_id, transaction_id)).fetchone()
                transaction_row_id = int(match[0]) if match else None
            capture_db.execute(
                "INSERT INTO session_packets "
                "(session_id, target_player_uid, seq, data_type, request_id, "
                "transaction_id, transaction_row_id, event_count, "
                "event_batch_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(sid), str(tid), time.time_ns(), 3055, request_id,
                 transaction_id, transaction_row_id, len(encoded_events),
                 json.dumps(encoded_events, separators=(",", ":"))))
            capture_db.commit()
        except Exception:
            capture_db.rollback()
        finally:
            if owns_connection:
                capture_db.close()

        if replayable:
            for raw in event_byte_list:
                raw = bytes(raw)
                event_class = (struct.unpack("<i", raw[:4])[0]
                               if len(raw) >= 4 else 0)
                _db_layer._db.execute(
                    "INSERT INTO session_events "
                    "(session_id, target_player_uid, seq, event_class, event_bytes) "
                    "VALUES (?,?,?,?,?)",
                    (str(sid), str(tid), int(time.time() * 1000),
                     event_class, raw))
            _db_layer._db.commit()
    except Exception:
        pass


__all__ = [name for name in globals() if name.startswith("db_")]
