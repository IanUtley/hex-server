"""Replay index and durable event-stream persistence API."""

import hashlib
import json
import struct
import time

import db as _db_layer


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


def db_session_state_hash(session_id):
    capture_db, owns_connection = _db_layer._session_capture_connection()
    try:
        session_row = capture_db.execute(
            "SELECT state, players_json, turn_order_json, seed_z, seed_w, "
            "deck_template_id FROM game_sessions WHERE session_id=?",
            (str(session_id),)).fetchone()
        if session_row is None:
            return ""
        card_columns = [row[1] for row in capture_db.execute(
            "PRAGMA table_info(game_cards)")]
        card_rows = capture_db.execute(
            "SELECT * FROM game_cards WHERE session_id=? ORDER BY id",
            (str(session_id),)).fetchall()
        snapshot = {"session": list(session_row),
                    "cards": [dict(zip(card_columns, row)) for row in card_rows]}
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                             default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    finally:
        if owns_connection:
            capture_db.close()


def db_record_session_transaction(session_id, player_uid, request_id,
                                  data_type, compressed, transaction_id,
                                  transaction_type, classification, inner_bytes,
                                  pre_state_hash):
    payload = inner_bytes if isinstance(inner_bytes, bytes) else b""
    capture_db, owns_connection = _db_layer._session_capture_connection()
    try:
        cursor = capture_db.execute(
            "INSERT INTO session_transactions "
            "(session_id, player_uid, received_seq, data_type, request_id, "
            "compressed, transaction_id, transaction_type, classification_json, "
            "inner_bytes, pre_state_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(session_id), str(player_uid), time.time_ns(), int(data_type),
             int(request_id or 0), int(compressed or 0), int(transaction_id),
             str(transaction_type or ""),
             json.dumps(classification or {}, sort_keys=True, default=str),
             payload, str(pre_state_hash or "")))
        capture_db.commit()
        return int(cursor.lastrowid)
    except BaseException:
        capture_db.rollback()
        raise
    finally:
        if owns_connection:
            capture_db.close()


def db_complete_session_transaction(row_id, post_state_hash, handled, error=""):
    capture_db, owns_connection = _db_layer._session_capture_connection()
    try:
        capture_db.execute(
            "UPDATE session_transactions SET post_state_hash=?, status=?, "
            "handled=?, completed_at=datetime('now'), error=? WHERE id=?",
            (str(post_state_hash or ""), "completed" if not error else "error",
             1 if handled else 0, str(error or ""), int(row_id)))
        capture_db.commit()
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
        if not _is_replayable_pvp_session(sid):
            return
        for raw in event_byte_list:
            event_class = struct.unpack("<i", raw[:4])[0] if len(raw) >= 4 else 0
            _db_layer._db.execute(
                "INSERT INTO session_events "
                "(session_id, target_player_uid, seq, event_class, event_bytes) "
                "VALUES (?,?,?,?,?)",
                (str(sid), str(tid), int(time.time() * 1000), event_class, raw))
        _db_layer._db.commit()
    except Exception:
        pass


__all__ = [name for name in globals() if name.startswith("db_")]
