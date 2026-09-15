"""Tournament and bracket persistence API."""

import json
import random

import db as _db_layer
from replay_db import db_get_replay_match, db_get_tournament_signup_names


def db_tournament_cleanup_old(age_days=1, conn=None):
    """Delegate tournament cleanup to the shared persistence layer."""
    # The shared cleanup deliberately owns its isolated write transaction;
    # retain the optional conn argument for the tournament DB API shape.
    return _db_layer.db_tournament_cleanup_old(age_days)


def db_tournament_close_orphaned_started():
    """Close started tournament rows whose game session no longer exists."""
    return _db_layer.db_tournament_close_orphaned_started()


def _tourney_select(base="t.*, tt.name AS type_name, tt.style, tt.format, "
                    "tt.min_players, tt.max_players, tt.games_count, tt.set_id"):
    return ("SELECT " + base + " FROM tournaments t "
            "JOIN tournament_types tt ON t.type_id=tt.id")


_TOURNEY_KEYS = ("id", "type_id", "status", "players_json", "session_id",
                 "created_at", "type_name", "style", "format", "min_players",
                 "max_players", "games_count", "set_id")
_TOURNEY_KEYS_EXPIRY = ("id", "type_id", "status", "players_json",
                        "session_id", "created_at", "expires_at",
                        "type_name", "style", "format", "min_players",
                        "max_players", "games_count", "set_id")


def _tourney_row_to_dict(row):
    if not row:
        return None
    keys = _TOURNEY_KEYS_EXPIRY if len(row) == len(_TOURNEY_KEYS_EXPIRY) \
        else _TOURNEY_KEYS
    return dict(zip(keys, row))


def db_tournament_types(conn=None):
    connection = conn or _db_layer._db
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(tournament_types)")}
    enabled_sql = ", enabled" if "enabled" in columns else ""
    where_sql = " WHERE enabled=1" if "enabled" in columns else ""
    rows = connection.execute(
        "SELECT id, name, style, format, min_players, max_players, "
        "games_count, set_id" + enabled_sql +
        " FROM tournament_types" + where_sql + " ORDER BY id").fetchall()
    keys = ("id", "name", "style", "format", "min_players", "max_players",
            "games_count", "set_id")
    return [dict(zip(keys, row)) for row in rows]


def db_tournament_banned_card_guids(tournament_type_id, conn=None):
    """Return the normalized ban list for one tournament type."""
    connection = conn or _db_layer._db
    rows = connection.execute(
        "SELECT card_guid FROM tournament_type_banned_cards "
        "WHERE tournament_type_id=?", (int(tournament_type_id),)).fetchall()
    return {str(row[0]).lower() for row in rows if row[0]}


def db_tournament_list(status=None, conn=None, enabled_only=False):
    connection = conn or _db_layer._db
    query = _tourney_select() + " ORDER BY t.id"
    params = ()
    if enabled_only:
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(tournament_types)")}
        if "enabled" in columns:
            query = _tourney_select() + " WHERE tt.enabled=1 ORDER BY t.id"
    if status:
        clause = " WHERE t.status=?"
        if enabled_only and "enabled" in columns:
            clause += " AND tt.enabled=1"
        query = _tourney_select() + clause + " ORDER BY t.id"
        params = (status,)
    return [_tourney_row_to_dict(row)
            for row in connection.execute(query, params).fetchall()]


def db_tournament_completed_for_player(player_uid, conn=None):
    query = _tourney_select(
        "DISTINCT t.*, tt.name AS type_name, tt.style, tt.format, "
        "tt.min_players, tt.max_players, tt.games_count, tt.set_id") + \
        " JOIN tournament_signups ts ON ts.tournament_id=t.id " \
        "WHERE LOWER(t.status) IN ('complete', 'closed') " \
        "AND ts.player_uid=? ORDER BY t.id"
    rows = (conn or _db_layer._db).execute(query, (int(player_uid),)).fetchall()
    return [_tourney_row_to_dict(row) for row in rows]


def db_tournament_create(inst_id, type_id, conn=None):
    connection = conn or _db_layer._db
    try:
        connection.execute(
            "INSERT OR IGNORE INTO tournaments (id, type_id) VALUES (?, ?)",
            (inst_id, type_id))
        if conn is None:
            connection.commit()
    except BaseException:
        if conn is None:
            connection.rollback()
        raise
    return inst_id


def db_tournament_close_other_instances(type_id, keep_id, conn=None):
    """Close generated duplicates while retaining a singleton event row."""
    connection = conn or _db_layer._db
    cursor = connection.execute(
        "UPDATE tournaments SET status='closed' "
        "WHERE type_id=? AND id<>? AND status<>'closed'",
        (int(type_id), int(keep_id)))
    if conn is None:
        connection.commit()
    return int(cursor.rowcount or 0)


def db_tournament_set_expiry(tid, expires_at, conn=None):
    connection = conn or _db_layer._db
    connection.execute("UPDATE tournaments SET expires_at=? WHERE id=?",
                       (str(expires_at), int(tid)))
    if conn is None:
        connection.commit()


def db_tournament_expire(tid, conn=None):
    """Close an expired event while retaining its historical result rows."""
    connection = conn or _db_layer._db
    connection.execute("UPDATE tournaments SET status='closed' WHERE id=?",
                       (int(tid),))
    for table in ("tournament_pool", "tournament_matches"):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if exists:
            connection.execute("DELETE FROM %s WHERE tournament_id=?" % table,
                               (int(tid),))
    if conn is None:
        connection.commit()


def db_tournament_reopen(tid, conn=None):
    """Reopen a persistent event without discarding its result history."""
    connection = conn or _db_layer._db
    try:
        connection.execute(
            "UPDATE tournaments SET status='waiting', players_json='{}', "
            "session_id=NULL WHERE id=?", (int(tid),))
        # Active signups belong to the closed run. Keep their rows for
        # history, but make a fresh deck/search lifecycle on re-entry.
        connection.execute(
            "UPDATE tournament_signups SET status='withdrew', "
            "deck_ready=0, searching=0 WHERE tournament_id=? "
            "AND status='active'", (int(tid),))
        if conn is None:
            connection.commit()
    except BaseException:
        if conn is None:
            connection.rollback()
        raise


def db_tournament_update_players(tid, players_json, conn=None):
    connection = conn or _db_layer._db
    try:
        connection.execute("UPDATE tournaments SET players_json=? WHERE id=?",
                           (players_json, tid))
        if conn is None:
            connection.commit()
    except BaseException:
        if conn is None:
            connection.rollback()
        raise
    return len(json.loads(players_json)) if players_json else 0


def db_tournament_set_status(tid, status, session_id=None, conn=None):
    connection = conn or _db_layer._db
    try:
        if session_id:
            connection.execute(
                "UPDATE tournaments SET status=?, session_id=? WHERE id=?",
                (status, str(session_id), tid))
        else:
            connection.execute("UPDATE tournaments SET status=? WHERE id=?",
                               (status, tid))
        if conn is None:
            connection.commit()
    except BaseException:
        if conn is None:
            connection.rollback()
        raise


def db_tournament_count_by_status(status, conn=None):
    row = (conn or _db_layer._db).execute(
        "SELECT COUNT(*) AS count FROM tournaments WHERE status=?", (status,)).fetchone()
    return int(row["count"] if hasattr(row, "keys") else row[0]) if row else 0


def db_tournament_count_active_by_type(type_id, conn=None):
    row = (conn or _db_layer._db).execute(
        "SELECT COUNT(*) AS count FROM tournaments "
        "WHERE type_id=? AND status='waiting'", (type_id,)).fetchone()
    return int(row["count"] if hasattr(row, "keys") else row[0]) if row else 0


def db_tournament_next_id(conn=None):
    row = (conn or _db_layer._db).execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM tournaments").fetchone()
    next_id = row["next_id"] if hasattr(row, "keys") else row[0] if row else 10000
    return max(10000, int(next_id))


def db_tournament_deck_create(tournament_id, player_uid, cards_json, conn=None):
    connection = conn or _db_layer._db
    connection.execute(
        "INSERT INTO tournament_decks (tournament_id, player_uid, cards_json) "
        "VALUES (?, ?, ?)", (tournament_id, player_uid, cards_json))
    if conn is None:
        connection.commit()


def db_tournament_deck_by_player(tournament_id, player_uid, conn=None):
    row = (conn or _db_layer._db).execute(
        "SELECT id, tournament_id, player_uid, cards_json, created_at "
        "FROM tournament_decks WHERE tournament_id=? AND player_uid=?",
        (tournament_id, player_uid)).fetchone()
    return dict(zip(("id", "tournament_id", "player_uid", "cards_json",
                     "created_at"), row)) if row else None


_SIGNUP_KEYS = ("id", "tournament_id", "player_uid", "player_name", "deck_id",
                "entry_group", "fee_paid", "status", "deck_ready",
                "searching", "created_at")
_SIGNUP_SELECT_FIELDS = _SIGNUP_KEYS


def _signup_rows(connection, where_sql, params):
    """Read signup rows by named fields across pre-async databases."""
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(tournament_signups)")}
    selected = [field for field in _SIGNUP_SELECT_FIELDS if field in columns]
    rows = connection.execute(
        "SELECT " + ", ".join(selected) +
        " FROM tournament_signups " + where_sql, params).fetchall()
    result = []
    for row in rows:
        value = dict(zip(selected, row))
        value.setdefault("deck_ready", 0)
        value.setdefault("searching", 0)
        result.append(value)
    return result


def db_tournament_signup_add(tid, player_uid, player_name, deck_id=0,
                             entry_group=0, fee_paid=0, conn=None):
    connection = conn or _db_layer._db
    connection.execute(
        "INSERT INTO tournament_signups "
        "(tournament_id, player_uid, player_name, deck_id, entry_group, fee_paid, "
        "deck_ready, searching) VALUES (?,?,?,?,?,?,0,0) "
        "ON CONFLICT(tournament_id, player_uid) DO UPDATE SET "
        "player_name=excluded.player_name, deck_id=excluded.deck_id, "
        "entry_group=excluded.entry_group, fee_paid=excluded.fee_paid, "
        "status='active'",
        (tid, player_uid, player_name, deck_id, entry_group, fee_paid))
    if conn is None:
        connection.commit()


def db_tournament_signup_set_status(tid, player_uid, status, conn=None):
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE tournament_signups SET status=? "
        "WHERE tournament_id=? AND player_uid=?", (status, tid, player_uid))
    if conn is None:
        connection.commit()


def db_tournament_signup_set_async_state(tid, player_uid, *,
                                         deck_ready=None, searching=None,
                                         conn=None):
    """Update the per-player async-event matchmaking state."""
    connection = conn or _db_layer._db
    assignments = []
    values = []
    if deck_ready is not None:
        assignments.append("deck_ready=?")
        values.append(1 if deck_ready else 0)
    if searching is not None:
        assignments.append("searching=?")
        values.append(1 if searching else 0)
    if not assignments:
        return False
    values.extend((int(tid), int(player_uid)))
    connection.execute(
        "UPDATE tournament_signups SET " + ", ".join(assignments) +
        " WHERE tournament_id=? AND player_uid=?", values)
    if conn is None:
        connection.commit()
    return True


def db_tournament_clear_player_searching(player_uid, conn=None):
    """Stop matchmaking for a disconnected player without retiring its run."""
    connection = conn or _db_layer._db
    cursor = connection.execute(
        "UPDATE tournament_signups SET searching=0 "
        "WHERE player_uid=? AND status='active' AND searching=1 "
        "AND NOT EXISTS (SELECT 1 FROM tournament_matches tm "
        "WHERE tm.tournament_id=tournament_signups.tournament_id "
        "AND tm.state!='Complete' AND "
        "(tm.player1_uid=tournament_signups.player_uid OR "
        "tm.player2_uid=tournament_signups.player_uid))",
        (int(player_uid),))
    if conn is None:
        connection.commit()
    return int(cursor.rowcount or 0)


def db_tournament_clear_orphaned_searches(conn=None):
    """Clear searches left by clients lost before a match was created."""
    connection = conn or _db_layer._db
    cursor = connection.execute(
        "UPDATE tournament_signups SET searching=0 "
        "WHERE status='active' AND searching=1 "
        "AND NOT EXISTS (SELECT 1 FROM tournament_matches tm "
        "WHERE tm.tournament_id=tournament_signups.tournament_id "
        "AND tm.state!='Complete' AND "
        "(tm.player1_uid=tournament_signups.player_uid OR "
        "tm.player2_uid=tournament_signups.player_uid))")
    if conn is None:
        connection.commit()
    return int(cursor.rowcount or 0)


def db_tournament_async_ready_players(tid, conn=None):
    """Return active, deck-complete players currently searching for a match."""
    connection = conn or _db_layer._db
    rows = connection.execute(
        "SELECT ts.player_uid, ts.player_name FROM tournament_signups ts "
        "WHERE ts.tournament_id=? AND ts.status='active' "
        "AND ts.deck_ready=1 AND ts.searching=1 "
        "AND NOT EXISTS (SELECT 1 FROM tournament_matches tm "
        "WHERE tm.tournament_id=ts.tournament_id AND tm.state!='Complete' "
        "AND (tm.player1_uid=ts.player_uid OR tm.player2_uid=ts.player_uid)) "
        "ORDER BY ts.created_at, ts.id", (int(tid),)).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


_TOURNAMENT_MATCH_KEYS = (
    "id", "tournament_id", "round_id", "match_id", "player1_uid",
    "player2_uid", "session_id", "state", "status", "start_time",
    "end_time", "game1_winner", "game2_winner", "game3_winner",
    "player1_live", "player2_live",
)


def db_tournament_matches(tid, conn=None):
    connection = conn or _db_layer._db
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(tournament_matches)")}
    live_select = (", player1_live, player2_live"
                   if {"player1_live", "player2_live"} <= columns else
                   ", 1, 1")
    rows = connection.execute(
        "SELECT id, tournament_id, round_id, match_id, player1_uid, player2_uid, "
        "session_id, state, status, start_time, end_time, game1_winner, "
        "game2_winner, game3_winner" + live_select +
        " FROM tournament_matches WHERE tournament_id=? "
        "ORDER BY round_id DESC, id DESC", (tid,)).fetchall()
    return [dict(zip(_TOURNAMENT_MATCH_KEYS, row)) for row in rows]


def db_tournament_match_start(tid, session_id, player1_uid, player2_uid,
                              round_id=1, start_time=0, conn=None):
    connection = conn or _db_layer._db
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(tournament_matches)")}
    if {"player1_live", "player2_live"} <= columns:
        connection.execute(
            "INSERT OR IGNORE INTO tournament_matches "
            "(tournament_id, round_id, match_id, player1_uid, player2_uid, "
            "session_id, state, status, start_time, player1_live, player2_live) "
            "VALUES (?, ?, ?, ?, ?, ?, 'PlayGame', 'InProgress', ?, 1, 1)",
            (tid, int(round_id), int(round_id), int(player1_uid),
             int(player2_uid), str(session_id), int(start_time)))
    else:
        connection.execute(
            "INSERT OR IGNORE INTO tournament_matches "
            "(tournament_id, round_id, match_id, player1_uid, player2_uid, "
            "session_id, state, status, start_time) VALUES (?, ?, ?, ?, ?, ?, "
            "'PlayGame', 'InProgress', ?)",
            (tid, int(round_id), int(round_id), int(player1_uid),
             int(player2_uid), str(session_id), int(start_time)))
    row = connection.execute(
        "SELECT id FROM tournament_matches WHERE tournament_id=? "
        "AND session_id=? LIMIT 1", (tid, str(session_id))).fetchone()
    if conn is None:
        connection.commit()
    return int(row["id"] if hasattr(row, "keys") else row[0]) if row else 0


def db_tournament_discard_match(tid, session_id, conn=None):
    """Remove an unstarted/stale match without recording a result."""
    connection = conn or _db_layer._db
    cursor = connection.execute(
        "DELETE FROM tournament_matches WHERE tournament_id=? AND session_id=? "
        "AND state!='Complete'", (int(tid), str(session_id)))
    if conn is None:
        connection.commit()
    return int(cursor.rowcount or 0)


def db_tournament_retire_player_run(tid, player_uid, conn=None):
    """Finalize and retire one async run while preserving its result."""
    return db_tournament_finalize_player_run(tid, player_uid, "quit", conn)


def _db_tournament_retire_player_run_state(tid, player_uid, connection):
    """Remove live state after its result has been durably recorded."""
    player_uid = int(player_uid)
    connection.execute(
        "UPDATE tournament_matches SET player1_live=0 "
        "WHERE tournament_id=? AND player1_uid=?",
        (int(tid), player_uid))
    connection.execute(
        "UPDATE tournament_matches SET player2_live=0 "
        "WHERE tournament_id=? AND player2_uid=?",
        (int(tid), player_uid))
    connection.execute(
        "DELETE FROM tournament_matches WHERE tournament_id=? "
        "AND player1_live=0 AND player2_live=0", (int(tid),))
    db_tournament_pool_delete(tid, player_uid, conn=connection)
    connection.execute(
        "UPDATE tournament_signups SET status='withdrew' "
        "WHERE tournament_id=? AND player_uid=?", (int(tid), player_uid))


def db_tournament_player_run_live(tid, player_uid, conn=None):
    """Return whether a player still has a live run in an async event."""
    connection = conn or _db_layer._db
    uid = int(player_uid)
    row = connection.execute(
        "SELECT 1 FROM tournament_matches WHERE tournament_id=? AND "
        "state!='Complete' AND status!='Complete' AND "
        "((player1_uid=? AND player1_live=1) OR "
        "(player2_uid=? AND player2_live=1)) LIMIT 1",
        (int(tid), uid, uid)).fetchone()
    return bool(row)


def db_tournament_player_score(tid, player_uid, conn=None):
    """Return completed live-run match wins/losses for one player."""
    connection = conn or _db_layer._db
    uid = int(player_uid)
    rows = connection.execute(
        "SELECT player1_uid, player2_uid, game1_winner, player1_live, "
        "player2_live, state FROM tournament_matches "
        "WHERE tournament_id=? AND state='Complete' "
        "AND ((player1_uid=? AND player1_live=1) OR "
        "(player2_uid=? AND player2_live=1))",
        (int(tid), uid, uid)).fetchall()
    wins = losses = 0
    for row in rows:
        p1, p2, winner, _p1_live, _p2_live, _state = row
        if int(winner or 0) == uid:
            wins += 1
        elif int(winner or 0) in (int(p1), int(p2)):
            losses += 1
    return wins, losses


def db_tournament_finalize_player_run(tid, player_uid, result="quit",
                                       conn=None):
    """Persist one run's final score, then remove its live state.

    A live-match check makes this idempotent when an automatic threshold
    retirement is followed by the client's LeaveTournament request.
    """
    connection = conn or _db_layer._db
    # ``db_tournament_player_run_live`` means an unfinished match is active;
    # a completed match keeps player*_live=1 until this run finalizer retires
    # it. The scheduler must therefore accept either kind of live state.
    active_match = db_tournament_player_run_live(
        tid, player_uid, conn=connection)
    completed_run = connection.execute(
        "SELECT 1 FROM tournament_matches WHERE tournament_id=? "
        "AND state='Complete' AND "
        "((player1_uid=? AND player1_live=1) OR "
        "(player2_uid=? AND player2_live=1)) LIMIT 1",
        (int(tid), int(player_uid), int(player_uid))).fetchone()
    if not active_match and not completed_run:
        return None
    wins, losses = db_tournament_player_score(tid, player_uid, conn=connection)
    row = connection.execute(
        "SELECT COALESCE(MAX(id), 0) FROM tournament_matches "
        "WHERE tournament_id=? AND ((player1_uid=? AND player1_live=1) OR "
        "(player2_uid=? AND player2_live=1))",
        (int(tid), int(player_uid), int(player_uid))).fetchone()
    final_match_id = int(row[0] or 0) if row else 0
    run_row = connection.execute(
        "SELECT COALESCE(MAX(run_number), 0) + 1 FROM tournament_results "
        "WHERE tournament_id=? AND player_uid=?",
        (int(tid), int(player_uid))).fetchone()
    run_number = int(run_row[0] or 1) if run_row else 1
    connection.execute(
        "INSERT INTO tournament_results "
        "(tournament_id, player_uid, run_number, wins, losses, result, "
        "final_match_id) VALUES (?,?,?,?,?,?,?)",
        (int(tid), int(player_uid), run_number, wins, losses, str(result),
         final_match_id))
    _db_tournament_retire_player_run_state(tid, player_uid, connection)
    if conn is None:
        connection.commit()
    return {
        "tournament_id": int(tid), "player_uid": int(player_uid),
        "run_number": run_number, "wins": wins, "losses": losses,
        "result": str(result), "final_match_id": final_match_id,
    }


def db_tournament_results(tid, player_uid=None, conn=None):
    """Return immutable per-run results retained after match cleanup."""
    connection = conn or _db_layer._db
    query = ("SELECT id, tournament_id, player_uid, run_number, wins, losses, "
             "result, final_match_id, completed_at FROM tournament_results "
             "WHERE tournament_id=?")
    params = [int(tid)]
    if player_uid is not None:
        query += " AND player_uid=?"
        params.append(int(player_uid))
    query += " ORDER BY id"
    rows = connection.execute(query, params).fetchall()
    keys = ("id", "tournament_id", "player_uid", "run_number", "wins",
            "losses", "result", "final_match_id", "completed_at")
    return [dict(zip(keys, row)) for row in rows]


def db_tournament_match_result(tid, session_id, winner_uid, loser_uid,
                               end_time=0, conn=None):
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT id, state FROM tournament_matches WHERE tournament_id=? "
        "AND session_id=? LIMIT 1", (tid, str(session_id))).fetchone()
    if not row:
        return 0
    row_id = row["id"] if hasattr(row, "keys") else row[0]
    state = row["state"] if hasattr(row, "keys") else row[1]
    if state == "Complete":
        return int(row_id)
    connection.execute(
        "UPDATE tournament_matches SET state='Complete', status='Complete', "
        "end_time=?, game1_winner=? WHERE id=?",
        (int(end_time), int(winner_uid), int(row_id)))
    if conn is None:
        connection.commit()
    return int(row_id)


def db_tournament_by_id(tid, conn=None):
    """Return the joined tournament/type projection by ID."""
    try:
        row = (conn or _db_layer._db).execute(
            _tourney_select() + " WHERE t.id=?", (tid,)).fetchone()
    except (OverflowError, TypeError):
        return None
    if not row:
        return None
    return _tourney_row_to_dict(row)


def db_tournament_signup_by_player(tid, player_uid, conn=None):
    """Return one tournament signup projection."""
    rows = _signup_rows(
        conn or _db_layer._db,
        "WHERE tournament_id=? AND player_uid=? LIMIT 1",
        (tid, player_uid))
    return rows[0] if rows else None


def db_tournament_signups_by_tournament(tid, status="active", conn=None):
    """Return tournament signups, optionally restricted by status."""
    connection = conn or _db_layer._db
    if status is None:
        return _signup_rows(
            connection, "WHERE tournament_id=? ORDER BY id", (tid,))
    else:
        return _signup_rows(
            connection, "WHERE tournament_id=? AND status=? ORDER BY id",
            (tid, status))


def db_tournament_players_name_map(tid, conn=None):
    """Return active tournament player IDs mapped to display names."""
    rows = (conn or _db_layer._db).execute(
        "SELECT player_uid, player_name FROM tournament_signups "
        "WHERE tournament_id=? AND status='active'", (tid,)).fetchall()
    return {str(row[0]): row[1] for row in rows}


def db_tournament_room_for_game(tournament_id, conn=None):
    """Return the joined tournament/type row used to start a game."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT t.*, tt.name AS type_name, tt.style, tt.format, "
        "tt.min_players, tt.max_players, tt.games_count, tt.set_id "
        "FROM tournaments t JOIN tournament_types tt ON t.type_id = tt.id "
        "WHERE t.id=? LIMIT 1", (tournament_id,)).fetchone()
    if not row:
        return None
    keys = (_TOURNEY_KEYS_EXPIRY if len(row) == len(_TOURNEY_KEYS_EXPIRY)
            else _TOURNEY_KEYS)
    return dict(zip(keys, row))


def db_tournament_player_name_for_session(session_id, player_uid, conn=None):
    """Return the signup display name for a player in a game session."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT player_name FROM tournament_signups "
        "WHERE tournament_id=(SELECT tournament_id FROM tournament_matches "
        "WHERE session_id=? LIMIT 1) AND player_uid=?",
        (session_id, player_uid),
    ).fetchone()
    return row[0] if row else None


def db_tournament_match_session(tournament_id, player_uid, conn=None):
    """Return an unfinished match session involving a player."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT session_id FROM tournament_matches WHERE tournament_id=? "
        "AND state!='Complete' AND (player1_uid=? OR player2_uid=?) LIMIT 1",
        (int(tournament_id), int(player_uid), int(player_uid))).fetchone()


def db_tournament_pool_replace(tournament_id, player_uid, cards,
                               conn=None):
    """Replace one player's tournament pool with a validated location map.

    ``cards`` is an iterable of ``(card_uid, template_guid, location)``.
    Location 0 is main deck and 1 is sideboard/pool.
    """
    connection = conn or _db_layer._db
    rows = [(int(tournament_id), int(player_uid), int(card_uid),
             str(template_guid).lower(), int(location))
            for card_uid, template_guid, location in cards]
    connection.execute(
        "DELETE FROM tournament_pool WHERE tournament_id=? AND player_uid=?",
        (int(tournament_id), int(player_uid)))
    connection.executemany(
        "INSERT INTO tournament_pool "
        "(tournament_id, player_uid, card_uid, template_guid, location) "
        "VALUES (?,?,?,?,?)", rows)
    if conn is None:
        connection.commit()
    return len(rows)


def db_seed_tournament_pool(tournament_id, player_uid, card_guids,
                            conn=None):
    """Create a persistent limited pool without creating a battle session.

    Async events build their deck before an opponent exists, so the pool is
    owned by the tournament/player pair rather than by ``game_cards`` in a
    two-player session.  The returned card UIDs are only deck-construction
    identities; match materialization later resolves the stored templates.
    """
    connection = conn or _db_layer._db
    next_uid = connection.execute(
        "SELECT COALESCE(MAX(card_uid), 1) + 256 FROM tournament_pool "
        "WHERE tournament_id=?", (int(tournament_id),)).fetchone()[0]
    cards = []
    for template_guid in list(card_guids or []):
        guid = str(template_guid or "").lower()
        if len(guid) != 36:
            continue
        exists = connection.execute(
            "SELECT 1 FROM card_templates WHERE guid=?", (guid,)).fetchone()
        if not exists:
            continue
        cards.append((int(next_uid), guid, 0))
        next_uid += 256
    count = db_tournament_pool_replace(
        tournament_id, player_uid, cards, conn=connection)
    if conn is None:
        connection.commit()
    return count


def db_tournament_pool(tournament_id, player_uid, conn=None):
    connection = conn or _db_layer._db
    rows = connection.execute(
        "SELECT card_uid, template_guid, location FROM tournament_pool "
        "WHERE tournament_id=? AND player_uid=? ORDER BY card_uid",
        (int(tournament_id), int(player_uid))).fetchall()
    return [(int(row[0]), str(row[1]), int(row[2])) for row in rows]


def db_tournament_pool_delete(tournament_id, player_uid=None, conn=None):
    connection = conn or _db_layer._db
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='tournament_pool'"
    ).fetchone()
    if not exists:
        return
    if player_uid is None:
        connection.execute("DELETE FROM tournament_pool WHERE tournament_id=?",
                           (int(tournament_id),))
    else:
        connection.execute(
            "DELETE FROM tournament_pool WHERE tournament_id=? AND player_uid=?",
            (int(tournament_id), int(player_uid)))
    if conn is None:
        connection.commit()


def db_tournament_status(tournament_id, conn=None):
    """Return a tournament's current status."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT status FROM tournaments WHERE id=?", (int(tournament_id),)
    ).fetchone()
    return row[0] if row else None


def db_tournament_game_deck(deck_id, conn=None):
    """Return the deck fields required to materialize tournament cards."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT cards, pvp_champion_guid, user_id, active_gems "
        "FROM decks WHERE id=?", (deck_id,)).fetchone()
    if not row:
        return None
    return {
        "cards": row[0] or "[]", "champion_guid": row[1] or "",
        "owner_user_id": row[2], "active_gems": row[3] or "{}",
    }


def db_seed_tournament_game_deck(session_id, player_uid, deck_id, conn=None,
                                 *, card_guids=None, champion_guid=None):
    """Materialize one tournament deck into authoritative ``game_cards``.

    This keeps deck-instance resolution, socketed gem abilities, template
    backfill, and the card UID sequence in the tournament persistence boundary.
    The caller owns the surrounding game/session orchestration.
    """
    connection = conn or _db_layer._db
    if card_guids is None:
        deck = db_tournament_game_deck(deck_id, connection)
        if not deck:
            return {"inserted": 0, "skipped_int": 0, "skipped_invalid": 0,
                    "skipped_error": 0, "champion_guid": ""}
    else:
        # Mode-owned decks do not depend on a player's profile deck. Keep the
        # same materialization path as ordinary tournaments.
        deck = {"cards": json.dumps(list(card_guids)),
                "champion_guid": champion_guid or "",
                "owner_user_id": int(player_uid), "active_gems": "{}"}
    try:
        active_gems = json.loads(deck["active_gems"] or "{}")
    except (TypeError, ValueError):
        active_gems = {}
    card_guids = json.loads(deck["cards"]) if isinstance(deck["cards"], str) else list(deck["cards"])
    random.shuffle(card_guids)

    gem_ability_by_instance = {}
    for instance_key, gem_type in (active_gems or {}).items():
        try:
            gem_type = int(gem_type)
        except (TypeError, ValueError):
            continue
        if gem_type <= 0:
            continue
        row = connection.execute(
            "SELECT abilities_json FROM gem_templates WHERE gem_type=?",
            (gem_type,)).fetchone()
        if not row or not row[0]:
            continue
        try:
            abilities = json.loads(row[0])
        except (TypeError, ValueError):
            abilities = []
        if abilities:
            gem_ability_by_instance[str(instance_key)] = [
                str(ability).lower() for ability in abilities]

    counts = {"inserted": 0, "skipped_int": 0, "skipped_invalid": 0,
              "skipped_error": 0, "champion_guid": deck["champion_guid"]}
    for position, original_guid in enumerate(card_guids):
        instance_key = None
        template_guid = original_guid
        if isinstance(original_guid, (int, float)):
            instance_key = str(int(original_guid))
            resolved = connection.execute(
                "SELECT template_guid FROM card_instances "
                "WHERE instance_id=? AND user_id=?",
                (int(original_guid), deck["owner_user_id"])).fetchone()
            if resolved:
                template_guid = resolved[0]
            else:
                counts["skipped_int"] += 1
                continue
        if not isinstance(template_guid, str) or len(template_guid) != 36:
            counts["skipped_invalid"] += 1
            continue
        max_uid = connection.execute(
            "SELECT COALESCE(MAX(card_uid), 0) FROM game_cards "
            "WHERE session_id=?", (session_id,)).fetchone()[0]
        card_uid = int(max_uid) + 256 if max_uid else 257
        try:
            connection.execute(
                "INSERT INTO game_cards (session_id, user_id, card_uid, "
                "template_guid, card_template_id, location, position) "
                "VALUES (?, ?, ?, ?, ?, 'deck', ?)",
                (session_id, player_uid, card_uid, template_guid,
                 original_guid, position))
            template = connection.execute(
                "SELECT card_type, abilities_json, attributes "
                "FROM card_templates WHERE guid=?", (template_guid,)).fetchone()
            if template:
                card_type, abilities_json, attributes = template
                try:
                    abilities = json.loads(abilities_json or "[]")
                except (TypeError, ValueError):
                    abilities = []
                for ability in gem_ability_by_instance.get(instance_key, []):
                    if ability not in abilities:
                        abilities.append(ability)
                connection.execute(
                    "UPDATE game_cards SET card_type=?, card_abilities=?, "
                    "card_attributes=?, gems=?, original_template_guid = CASE "
                    "WHEN COALESCE(original_template_guid,'')='' THEN ? "
                    "ELSE original_template_guid END "
                    "WHERE session_id=? AND card_uid=?",
                    (card_type or "Unknown", json.dumps(abilities),
                     int(attributes or 0),
                     int(active_gems.get(instance_key, 0) or 0)
                     if instance_key else 0,
                     template_guid, session_id, card_uid))
            counts["inserted"] += 1
        except Exception:
            counts["skipped_error"] += 1
    connection.commit()
    return counts


def db_insert_tournament_champion_card(session_id, player_uid, champion_guid,
                                        conn=None):
    """Create the session-card row representing a tournament champion."""
    connection = conn or _db_layer._db
    max_uid = connection.execute(
        "SELECT COALESCE(MAX(card_uid), 0) FROM game_cards "
        "WHERE session_id=?", (session_id,)).fetchone()[0]
    card_uid = int(max_uid) + 256 if max_uid else 257
    connection.execute(
        "INSERT INTO game_cards (session_id, user_id, card_uid, "
        "template_guid, card_template_id, card_type, location, position, "
        "is_champion) VALUES (?, ?, ?, ?, ?, 'Champion', 'champion', 0, 1)",
        (session_id, player_uid, card_uid, champion_guid, champion_guid))
    connection.commit()
    return card_uid


def db_random_card_guids_for_set(set_id, count, conn=None):
    """Return up to ``count`` eligible PVP cards from a set."""
    connection = conn or _db_layer._db
    rows = connection.execute(
        "SELECT guid FROM card_templates WHERE set_id=? AND rarity "
        "IN ('Common','Uncommon','Rare','Legendary') AND is_pve=0 AND no_pvp=0",
        (set_id,)).fetchall()
    guids = [row[0] for row in rows]
    random.shuffle(guids)
    return guids[:max(0, min(int(count), len(guids)))]

__all__ = [name for name in globals() if name.startswith("db_")]
