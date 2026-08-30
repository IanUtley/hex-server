"""
Game session management for Hex TCG private server.

Handles the LoadBalancer service DataTypes (session lifecycle) and
interfaces with the GameSession service for per-session game events.

Architecture:
  LoadBalancer Service (stateless, DataTypes 22001-22045)
    - StartSession, StartEncounter, FindSession, JoinSession
    - ReadyForGameSetup, ReadyForGameEvents, ReadyToStartGame
    - LeaveSession, EndSession

  GameSession Service (stateful, DataTypes 3001-3056)
    - PlayerTransaction, PlayerAdded, PlayerRemoved
    - GameStarted, GameEnded, SessionSyncEvent
    - ReadyForGameSetup, ReadyForGameEvents (session-specific)

Session lifecycle:
  1. Client sends StartEncounter/StartSession -> Server creates session
  2. Client sends JoinSession -> Server adds player
  3. Client sends ReadyForGameSetup -> Server returns opponent info + seeds
  4. Client sends ReadyForGameEvents -> Server acks
  5. Server pushes GameStarted -> Game begins
  6. Game engine processes transactions and pushes SessionSyncEvents
  7. Server pushes GameEnded -> Game ends
  8. Client sends LeaveSession/EndSession -> Cleanup

All session state is stored in the SQLite `game_sessions` table so the
server can run many handler threads concurrently (one thread per client)
without shared in-memory mutable state.
"""

import json
import os
import time
from binascii import hexlify

# UID type codes (must match hconnect_server.py)
UID_TYPE = {
    "ServicePlayer": 244,
    "ServiceMail": 252,
    "ServiceProfile": 245,
    "ServiceGameSession": 246,
    "ServiceMatchmaking": 247,
    "ServiceEscrow": 249,
    "ServiceCampaign": 253,
}

_DB_PATH = os.environ.get(
    "HEX_DB_PATH",
    os.path.join(os.path.dirname(__file__), "hconnect.db"),
)


def make_uid(uid_type, inst):
    """Create a UID ulong from type and instance."""
    return (inst << 8) | uid_type


def uid_to_hex(uid_val):
    """Convert a UID ulong to hex string for ObjFmt."""
    return hexlify(__import__("struct").pack("<Q", uid_val)).decode("ascii")


class GameSession:
    """A single game session, backed by a row in the game_sessions table."""

    def __init__(self, session_id, server_id, session_name, owner_uid):
        self.session_id = session_id
        self.server_id = server_id
        self.session_name = session_name
        self.owner_uid = owner_uid
        self.players = []
        self.encounter_data = {}
        self.state = "created"
        self.turn_order = []
        self.seed_z = 12345
        self.seed_w = 67890
        self.deck_template_id = "00000000-0000-0000-0000-000000000000"

    def add_player(self, player_uid, player_position, conn=None):
        self.players.append((player_uid, player_position))
        self._persist(conn=conn)

    def set_state(self, state, conn=None):
        self.state = state
        self._persist(conn=conn)

    def get_player_state_list(self):
        return [
            {"PlayerId": puid, "PlayerPosition": ppos}
            for puid, ppos in self.players
        ]

    def get_uid_list(self):
        return [puid for puid, _ in self.players]

    # -- persistence -------------------------------------------------------
    def _persist(self, conn=None):
        _save(self, conn=conn)


def _db():
    # Session state is written from HConnect while the separate tournament
    # scheduler may be updating the same WAL database.  Use the same wait
    # policy as db.py so a transient writer collision does not abort a turn.
    import db as db_layer
    return db_layer.connect(_DB_PATH)


def _next_instance(conn=None):
    """Return a monotonically increasing instance number from the DB.

    Stored in a tiny key/value table so concurrent threads never reuse an
    instance number (and thus never collide on session_id/server_id).
    """
    owns_connection = conn is None
    conn = conn or _db()
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='next_session_inst'").fetchone()
        nxt = (row[0] + 1) if row else 1
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('next_session_inst', ?)",
            (nxt,))
        if owns_connection:
            conn.commit()
        return nxt
    finally:
        if owns_connection:
            conn.close()


def create_encounter_session(session_name, encounter_data, player_uid, conn=None):
    """Create a new encounter session and persist it to the DB."""
    import struct
    inst = _next_instance(conn=conn)
    session_id = make_uid(UID_TYPE["ServiceGameSession"], inst)
    server_id = make_uid(UID_TYPE["ServiceGameSession"], inst * 7)
    session = GameSession(session_id, server_id, session_name, player_uid)
    session.encounter_data = encounter_data or {}
    session.add_player(player_uid, 0, conn=conn)
    session.state = "created"
    session._persist(conn=conn)
    return session


def _save(session, conn=None):
    owns_connection = conn is None
    conn = conn or _db()
    try:
        # session_id/server_id/owner_uid are unsigned 64-bit UIDs which can
        # exceed SQLite's signed 64-bit INTEGER range, so store them as TEXT.
        conn.execute(
            "INSERT OR REPLACE INTO game_sessions "
            "(session_id, server_id, session_name, owner_uid, state, "
            " encounter_data, players_json, turn_order_json, seed_z, seed_w, "
            " deck_template_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?, COALESCE(("
            "  SELECT created_at FROM game_sessions WHERE session_id=?), "
            " datetime('now')))",
            (str(session.session_id), str(session.server_id), session.session_name,
             str(session.owner_uid), session.state,
             json.dumps(session.encounter_data),
             json.dumps(session.players),
             json.dumps(session.turn_order),
             session.seed_z, session.seed_w,
             session.deck_template_id,
             str(session.session_id)))
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()


def _load(row):
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return v
    s = GameSession(_int(row["session_id"]), _int(row["server_id"]),
                    row["session_name"], _int(row["owner_uid"]))
    s.state = row["state"]
    try:
        s.encounter_data = json.loads(row["encounter_data"] or "{}")
    except (ValueError, TypeError):
        s.encounter_data = {}
    try:
        s.players = [tuple(p) for p in json.loads(row["players_json"] or "[]")]
    except (ValueError, TypeError):
        s.players = []
    try:
        s.turn_order = json.loads(row["turn_order_json"] or "[]")
    except (ValueError, TypeError):
        s.turn_order = []
    s.seed_z = row["seed_z"]
    s.seed_w = row["seed_w"]
    s.deck_template_id = row["deck_template_id"]
    return s


def get_session(session_name):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM game_sessions WHERE session_name=?",
            (session_name,)).fetchone()
        return _load(row) if row else None
    finally:
        conn.close()


def find_session_by_id(session_id, conn=None):
    owns_connection = conn is None
    conn = conn or _db()
    try:
        row = conn.execute(
            "SELECT * FROM game_sessions WHERE session_id=?",
            (str(session_id),)).fetchone()
        return _load(row) if row else None
    finally:
        if owns_connection:
            conn.close()


def find_session_by_player(player_uid, conn=None):
    """Find the most recent session containing this player.

    Callers use both the raw Reckoning ID and the typed ServicePlayer UID.
    Tournament sessions persist the latter, while reconnect requests carry
    the former, so compare both representations.
    """
    owns_connection = conn is None
    conn = conn or _db()
    try:
        player_uid = int(player_uid)
        raw_player_id = (player_uid >> 8
                         if (player_uid & 0xff) == UID_TYPE["ServicePlayer"]
                         else player_uid)
        player_ids = {raw_player_id,
                      make_uid(UID_TYPE["ServicePlayer"], raw_player_id)}
        rows = conn.execute(
            "SELECT * FROM game_sessions ORDER BY created_at DESC").fetchall()
        for row in rows:
            try:
                players = json.loads(row["players_json"] or "[]")
            except (ValueError, TypeError):
                continue
            for p in players:
                if (isinstance(p, (list, tuple)) and p
                        and int(p[0]) in player_ids):
                    return _load(row)
        return None
    finally:
        if owns_connection:
            conn.close()


def remove_session(session_name, conn=None):
    """Remove a session, using the caller's transaction when supplied."""
    owns_connection = conn is None
    conn = conn or _db()
    try:
        conn.execute("DELETE FROM game_sessions WHERE session_name=?", (session_name,))
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()


def cleanup_ended_sessions():
    """Remove sessions that have ended."""
    conn = _db()
    try:
        conn.execute("DELETE FROM game_sessions WHERE state='ended'")
        conn.commit()
    finally:
        conn.close()
