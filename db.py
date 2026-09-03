"""
Shared SQLite access layer for the Hex TCG private server.

Owns the single module-level connection (``_db``) and the reusable read/write
helper functions used by ``hconnect_server`` and friends.  All schema (DDL)
lives in ``static.py`` (``static.ensure_schema``) — this module only performs
DML.  Import the connection and helpers directly::

    from db import _db, db_get_inventory, db_get_or_create_user

The connection is ``check_same_thread=False`` with WAL so concurrent handler
threads can share it (SQLite serializes writes internally).
"""

import os
import sqlite3
import threading
import time
import struct
import json
import hashlib
import re
from collections import Counter
from contextlib import contextmanager
from binascii import hexlify
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "HEX_DB_PATH",
    os.path.join(os.path.dirname(__file__), "hconnect.db"),
)

REQUEST_LOG = "/tmp/hconnect_requests.log"
_log_req_file = open(REQUEST_LOG, "a", buffering=1)

_SQLITE_RETRY_DELAYS = (0.05, 0.10, 0.25, 0.50, 1.00)
_SQLITE_LOCK_CODES = {
    getattr(sqlite3, "SQLITE_BUSY", 5),
    getattr(sqlite3, "SQLITE_LOCKED", 6),
    getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517),
    getattr(sqlite3, "SQLITE_LOCKED_SHAREDCACHE", 262),
}
_sqlite_retry_stats = Counter()
_sqlite_retry_stats_lock = threading.Lock()
_SQLITE_RETRY_LOG_MILESTONES = {1, 2, 3, 5, 10, 25, 50, 100}


def _sqlite_sql_shape(sql):
    """Return a compact, parameter-free SQL signature for retry metrics."""
    text = re.sub(r"\s+", " ", str(sql or "")).strip()
    text = re.sub(r"'(?:''|[^'])*'", "'?'", text)
    text = re.sub(r"\b\d+\b", "?", text)
    return text[:240]


def _record_sqlite_retry(label):
    with _sqlite_retry_stats_lock:
        _sqlite_retry_stats[label] += 1
        count = _sqlite_retry_stats[label]
    message = f"[sqlite-retry] count={count} statement={label}"
    # Keep every retry in the request log so a run can be ranked afterward;
    # only milestone counts go to stdout to avoid flooding the server console.
    _log_req_file.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
    if count in _SQLITE_RETRY_LOG_MILESTONES:
        print(message, flush=True)


def sqlite_retry_stats():
    """Return retry counts, highest first, for live lock diagnosis."""
    with _sqlite_retry_stats_lock:
        return sorted(_sqlite_retry_stats.items(),
                      key=lambda item: (-item[1], item[0]))


def _is_sqlite_lock_error(exc):
    """Whether *exc* is a transient SQLite writer/schema lock."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    message = str(exc).lower()
    return code in _SQLITE_LOCK_CODES or any(
        text in message for text in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
        )
    )


class RetryingConnection(sqlite3.Connection):
    """SQLite connection that retries only transient lock operations.

    Retrying individual statements/commits is safe for this application:
    retrying a whole helper could duplicate an INSERT or other side effect.
    The connection-level busy timeout handles normal writer contention; the
    short retry loop also covers SQLITE_LOCKED variants that do not honor the
    busy handler.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._retry_lock = threading.RLock()

    def _with_retry(self, label, operation, *args, **kwargs):
        for attempt, delay in enumerate((0.0,) + _SQLITE_RETRY_DELAYS):
            try:
                return operation(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if (not _is_sqlite_lock_error(exc)
                        or attempt >= len(_SQLITE_RETRY_DELAYS)):
                    raise
                _record_sqlite_retry(label)
                if delay:
                    time.sleep(delay)

    def execute(self, sql, parameters=()):
        with self._retry_lock:
            self._last_sql_shape = _sqlite_sql_shape(sql)
            return self._with_retry(
                f"execute {_sqlite_sql_shape(sql)}",
                super().execute, sql, parameters)

    def executemany(self, sql, parameters):
        with self._retry_lock:
            self._last_sql_shape = _sqlite_sql_shape(sql)
            return self._with_retry(
                f"executemany {_sqlite_sql_shape(sql)}",
                super().executemany, sql, parameters)

    def executescript(self, sql_script):
        with self._retry_lock:
            self._last_sql_shape = _sqlite_sql_shape(sql_script)
            return self._with_retry(
                f"executescript {_sqlite_sql_shape(sql_script)}",
                super().executescript, sql_script)

    def commit(self):
        with self._retry_lock:
            return self._with_retry(
                f"commit after {getattr(self, '_last_sql_shape', '(unknown)')}",
                super().commit)

    def rollback(self):
        with self._retry_lock:
            return super().rollback()


def connect(database_path=None, *, check_same_thread=True):
    """Open a short-lived application connection.

    The legacy module-level ``_db`` connection remains available while the
    server is migrated. New application commands should use this factory so
    their transaction is owned by the command boundary rather than by an
    individual SQL helper.
    """
    conn = sqlite3.connect(
        database_path or DB_PATH,
        timeout=30.0,
        factory=RetryingConnection,
        check_same_thread=check_same_thread,
    )
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(database_path=None, immediate=True):
    """Run one application operation in an explicit SQLite transaction.

    Transactions are intentionally short-lived. Callers must not wait for
    client input or publish network events while inside this context.
    ``BEGIN IMMEDIATE`` serializes competing state-changing commands early,
    which is important for game state and priority transitions.
    """
    conn = connect(database_path)
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def hexdump(data, max_bytes=256):
    if not data:
        return "(empty)"
    s = hexlify(data[:max_bytes]).decode("ascii")
    if len(data) > max_bytes:
        s += f"... ({len(data)}b total)"
    return s


# --- Per-instance card helpers (game_cards) --------------------------------
# Reusable reads/writes for a card instance's ability/state/stat data. These
# back the handler-level wrappers so ability.py and the server share one source
# of truth for the game_cards row layout.

def db_template_by_guid(template_guid):
    """Return (guid, card_type, name, cost, attack, defense) or None."""
    row = _db.execute(
        "SELECT guid, card_type, name, cost, attack, defense "
        "FROM card_templates WHERE guid=?", (template_guid,)).fetchone()
    if not row:
        return None
    return row[0], row[1], row[2], row[3] or 0, row[4] or 0, row[5] or 0


def db_backfill_ability_effect_meta(db=None):
    """Restore the gamedata effect structure onto ability_effects rows:
    effect_group_id / condition_id / target_index from each ability's
    m_AbilityEffectList (card_abilities_meta.raw_json).  The client's engine
    walks effects by group and gates them on conditions; without these columns
    the flat BOM walk cannot reproduce it."""
    import json as _j
    import re as _re
    con = db or _db
    # Ensure the mapping columns exist (fresh DBs get them from static.DDL;
    # older DBs need ALTER TABLE before the backfill can write them).
    try:
        ecols = {r[1] for r in con.execute("PRAGMA table_info(ability_effects)")}
        for col, ddl in (
                ("effect_instance_id", "ALTER TABLE ability_effects ADD COLUMN effect_instance_id INTEGER DEFAULT -1"),
                ("contingent_effect_instance_id", "ALTER TABLE ability_effects ADD COLUMN contingent_effect_instance_id INTEGER DEFAULT -1"),
                ("secondary_target_index", "ALTER TABLE ability_effects ADD COLUMN secondary_target_index INTEGER DEFAULT -1"),
                ("recalculate_targets", "ALTER TABLE ability_effects ADD COLUMN recalculate_targets INTEGER DEFAULT -1"),
                ("is_optional", "ALTER TABLE ability_effects ADD COLUMN is_optional INTEGER DEFAULT 0"),
                ("effect_duration", "ALTER TABLE ability_effects ADD COLUMN effect_duration TEXT DEFAULT 'Instant'"),
                ("output_variables", "ALTER TABLE ability_effects ADD COLUMN output_variables TEXT DEFAULT '{}'"),
        ):
            if col not in ecols:
                con.execute(ddl)
        con.commit()
    except Exception:
        pass
    _recalc_map = {"True": 1, "False": 0, "UseDefault": -1, None: -1}
    rows = con.execute(
        "SELECT ability_guid, raw_json FROM card_abilities_meta "
        "WHERE raw_json IS NOT NULL AND raw_json != ''").fetchall()
    updated = 0
    for ag, raw in rows:
        try:
            rec = _j.loads(raw)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        lst = rec.get("m_AbilityEffectList") or []
        for order, entry in enumerate(lst):
            eg = (entry.get("m_EffectTemplateId") or {}).get("m_Guid", "")
            if not eg:
                continue
            cid = (entry.get("m_ConditionId") or {}).get("m_Guid", "") or ""
            if cid.lower() == "00000000-0000-0000-0000-000000000000":
                cid = ""
            # m_TargetTemplateIndex is a real index: 0 must survive (the old
            # `int(x or -1)` collapsed index 0 to -1, breaking every first
            # target).  Same care for the other index fields.
            def _idx(v):
                if v is None:
                    return -1
                try:
                    return int(v)
                except (ValueError, TypeError):
                    return -1
            recalc = entry.get("m_RecalculateTargets")
            outvars = entry.get("m_OutputVariables") or {}
            if not isinstance(outvars, dict):
                outvars = {}
            con.execute(
                "UPDATE ability_effects SET effect_group_id=?, condition_id=?, "
                "target_index=?, effect_instance_id=?, "
                "contingent_effect_instance_id=?, secondary_target_index=?, "
                "recalculate_targets=?, is_optional=?, effect_duration=?, "
                "output_variables=? WHERE ability_guid=? AND effect_guid=? "
                "AND effect_order=?",
                (_idx(entry.get("m_EffectGroupId")),
                 cid.lower() if cid else "",
                 _idx(entry.get("m_TargetTemplateIndex")),
                 _idx(entry.get("m_EffectInstanceId")),
                 _idx(entry.get("m_ContingentEffectInstanceId")),
                 _idx(entry.get("m_SecondaryTargetIndex")),
                 _recalc_map.get(str(recalc), -1),
                 _idx(entry.get("m_IsOptional")),
                 str(entry.get("m_EffectDuration") or "Instant"),
                 _j.dumps(outvars),
                 ag.lower(), eg.lower(), order))
            updated += 1

    con.commit()
    return updated


def db_ensure_resource_grants(db=None):
    """Populate card_templates.current_resources_granted /
    max_resources_granted from the gamedata template fields
    (m_CurrentResourcesGranted / m_MaxResourcesGranted).  Basic shards grant
    1/1; Shards of Fate grants 0/1 — it increases MAX mana only.  The fields
    are not part of the CARD_TEMPLATES seed tuple, so the live DB backfills
    them here; resources missing from Records default to 1/1 like a shard."""
    import json as _json
    import os as _os
    con = db or _db
    try:
        ecols = {r[1] for r in con.execute("PRAGMA table_info(card_templates)")}
        if "current_resources_granted" not in ecols:
            con.execute("ALTER TABLE card_templates "
                        "ADD COLUMN current_resources_granted INTEGER DEFAULT 0")
        if "max_resources_granted" not in ecols:
            con.execute("ALTER TABLE card_templates "
                        "ADD COLUMN max_resources_granted INTEGER DEFAULT 0")
        con.commit()
    except Exception:
        pass
    try:
        _rec_path = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)),
            "Records", "CardTemplate.jsonl")
        if _os.path.exists(_rec_path):
            _grants = {}
            with open(_rec_path) as _fh:
                for _line in _fh:
                    _line = _line.rstrip("\n")
                    if not _line or _line.startswith('"$'):
                        continue
                    try:
                        _inner = _json.loads(_line)
                    except Exception:
                        continue
                    if not isinstance(_inner, str):
                        continue
                    try:
                        _rec = _json.loads(_inner)
                    except Exception:
                        continue
                    if not isinstance(_rec, dict):
                        continue
                    _g = ((_rec.get("m_Id") or {}).get("m_Guid") or "").lower()
                    if not _g:
                        continue
                    _grants[_g] = (
                        int(_rec.get("m_CurrentResourcesGranted") or 0),
                        int(_rec.get("m_MaxResourcesGranted") or 0))
            for _g, (_cur, _max) in _grants.items():
                con.execute(
                    "UPDATE card_templates SET current_resources_granted=?, "
                    "max_resources_granted=? WHERE guid=?",
                    (_cur, _max, _g))
            con.commit()
    except Exception:
        pass
    # Insurance for resource templates missing from Records: behave like a
    # basic shard (+1/+1).
    con.execute(
        "UPDATE card_templates SET current_resources_granted=1, "
        "max_resources_granted=1 WHERE card_type='Resource' "
        "AND current_resources_granted=0 AND max_resources_granted=0")
    con.commit()
    # Shards of Fate ("Choose a Standard resource in your deck. Gain the
    # thresholds it provides.") increases MAX resources only — the gamedata
    # snapshot reports it as current+max.  Detect it the same data-driven way
    # the play path does (an ability chain whose target templates filter a
    # Standard RESOURCE in the DECK) and override to max-only (0/1).
    try:
        _snext = []
        _scols = {r[1] for r in con.execute("PRAGMA table_info(card_templates)")}
        if "abilities_json" not in _scols:
            raise RuntimeError("no abilities_json")
        _rows = con.execute(
            "SELECT guid, abilities_json FROM card_templates "
            "WHERE card_type='Resource'").fetchall()
        for _guid, _aj in _rows:
            try:
                _ags = _json.loads(_aj or "[]")
            except Exception:
                _ags = []
            _look = list(_ags)
            _seen = set()
            _sift = None
            while _look:
                _ag = str(_look.pop()).lower()
                if _ag in _seen:
                    continue
                _seen.add(_ag)
                _trow = con.execute(
                    "SELECT target_template_ids FROM card_abilities_meta "
                    "WHERE ability_guid=?", (_ag,)).fetchone()
                if _trow and _trow[0]:
                    try:
                        _tids = _json.loads(_trow[0])
                    except Exception:
                        _tids = []
                    for _tid in (_tids or []):
                        _tt = con.execute(
                            "SELECT filter_json FROM target_templates "
                            "WHERE template_id=?", (str(_tid),)).fetchone()
                        _fj = (_tt[0] if _tt else "") or ""
                        if ("IsSubType" in _fj and '"Standard"' in _fj
                                and "IsResource" in _fj
                                and "InZone" in _fj and '"Deck"' in _fj):
                            _sift = _ag
                            break
                    if _sift:
                        break
                for _e in con.execute(
                        "SELECT param FROM ability_effects "
                        "WHERE ability_guid=? "
                        "AND effect_type='ActivateAbilityEffectTemplate'",
                        (_ag,)).fetchall():
                    if _e and _e[0]:
                        _look.append(_e[0].lower())
            if _sift:
                con.execute(
                    "UPDATE card_templates SET "
                    "current_resources_granted=0, max_resources_granted=1 "
                    "WHERE guid=?", (_guid,))
        con.commit()
    except Exception:
        pass


def db_card_ability_list(session_id, card_uid):
    """Current ability GUID list for a card instance (game_cards.card_abilities)."""
    row = _db.execute(
        "SELECT card_abilities FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row or not row[0]:
        return []
    try:
        return [g.lower() for g in json.loads(row[0])]
    except Exception:
        return []


def db_card_uses(session_id, card_uid):
    """Per-instance ability usage counts {ability_guid: uses} (card_uses)."""
    row = _db.execute(
        "SELECT card_uses FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return dict(json.loads(row[0]))
    except Exception:
        return {}


def db_bump_card_use(session_id, card_uid, ability_guid):
    """Increment an instance's usage of an ability (UsesPerGame/Turn limits)."""
    uses = db_card_uses(session_id, card_uid)
    uses[ability_guid] = int(uses.get(ability_guid, 0)) + 1
    _db.execute(
        "UPDATE game_cards SET card_uses=? WHERE session_id=? AND card_uid=?",
        (json.dumps(uses), session_id, int(card_uid)))
    _db.commit()
    return uses[ability_guid]


def db_card_state(session_id, card_uid):
    """Current card_state bitmask for an instance (0 if absent)."""
    row = _db.execute(
        "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return int(row[0]) if row and row[0] else 0


def db_warzone_troop_count(session_id, user_id):
    """Count of a player's warzone Troop cards (user_id 0 = the AI)."""
    row = _db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='warzone' AND card_type LIKE '%Troop%'",
        (session_id, user_id)).fetchone()
    return int(row[0]) if row else 0


def db_card_stat_mods(session_id, card_uid):
    """(card_attack_mod, card_defense_mod) for an instance (0s if absent)."""
    row = _db.execute(
        "SELECT card_attack_mod, card_defense_mod FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row:
        return 0, 0
    return (row[0] or 0), (row[1] or 0)



def log_req(msg: str):
    log(msg)
    _log_req_file.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


log_lock = threading.Lock()


def log(msg):
    with log_lock:
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


# === SQLite connection ===

# The HConnect process and the tournament scheduler are separate processes
# sharing this WAL database.  A short write collision is normal when a game
# session is persisted while the scheduler refills its room pool; the default
# sqlite3 timeout (5 seconds) can turn that collision into a request failure.
# Wait longer for the writer to finish instead.
_db = connect(DB_PATH, check_same_thread=False)
_db.row_factory = None
_db.execute("PRAGMA busy_timeout=30000")
_db.execute("PRAGMA journal_mode=WAL")
_db.execute("PRAGMA foreign_keys=ON")

# Ensure the static schema (incl. game_cards, session_events) exists and apply
# any column migrations for databases created before columns were added.
import static
static.ensure_schema(_db)


# === Identity helpers ===

def player_id_from_name(name):
    """Derive a stable numeric player ID from the full identity string.

    The identity is "Display#Discriminator" (e.g. "Neverness#1234"). The
    player ID is a 63-bit hash of that FULL string so different players with
    the same display name still get distinct IDs, and the same player always
    maps to the same ID across sessions.
    """
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2 ** 63)


def player_id_from_steam(steam_id):
    """Derive a player ID from a Steam account ID (steamId).

    SteamID64 values (17-digit, ~7.6e16 max) fit comfortably in a 63-bit
    signed int, so we use the raw Steam ID as the player ID.  This lets the
    auth proxy's Steam ID become the authoritative account key even though
    the in-game display name stays the fixed "TestPlayer".
    """
    try:
        return int(steam_id) % (2 ** 63)
    except (TypeError, ValueError):
        return player_id_from_name(str(steam_id))


def display_name_from_identity(name):
    """Strip the hidden '#discriminator' suffix for display (e.g. 'Neverness')."""
    if name and "#" in name:
        return name.rsplit("#", 1)[0]
    return name or ""


# === Users / profile ===

def db_get_or_create_user(name, steam_id=None):
    import new_player
    # If the auth proxy supplied a Steam ID (via the login token), that is the
    # authoritative player key; fall back to hashing the identity otherwise.
    uid = player_id_from_steam(steam_id) if steam_id else player_id_from_name(name)
    row = _db.execute("SELECT id, name, gold, platinum, experience, level, flags, last_login FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        row = _db.execute("SELECT id, name, gold, platinum, experience, level, flags, last_login FROM users WHERE name=?", (name,)).fetchone()
    if row:
        # The auth proxy may have created this user row before we saw the
        # login (proxy's /steam/login -> db_set_user_flags).  In that case
        # the new-player grant never ran, so grant it here idempotently:
        # only if the account has no collection cards yet.
        has_cards = _db.execute(
            "SELECT COUNT(*) FROM collections WHERE user_id=?", (row[0],)).fetchone()[0]
        if not has_cards:
            try:
                new_player.grant_new_player(_db, row[0])
                log(f"  WARN: user '{name}' existed without grants; applied new-player grant")
            except Exception as e:
                log(f"    WARN: catch-up new-player grant failed: {e}")
        old_last_login = row[7] if len(row) > 7 else None
        daily_bonus_xp = 0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if old_last_login:
            try:
                old_date = old_last_login[:10]  # Extract YYYY-MM-DD from ISO string
                if old_date != today:
                    daily_bonus_xp = 100
                    new_xp = (row[4] or 0) + 100
                    _db.execute("UPDATE users SET experience=?, last_login=datetime('now'), last_ip=? WHERE id=?", (new_xp, "127.0.0.1", row[0]))
                else:
                    _db.execute("UPDATE users SET last_login=datetime('now'), last_ip=? WHERE id=?", ("127.0.0.1", row[0]))
            except (ValueError, IndexError):
                _db.execute("UPDATE users SET last_login=datetime('now'), last_ip=? WHERE id=?", ("127.0.0.1", row[0]))
        else:
            _db.execute("UPDATE users SET last_login=datetime('now'), last_ip=? WHERE id=?", ("127.0.0.1", row[0]))
        _db.commit()
        flags = row[6] if len(row) > 6 else "{}"
        result = {"id": row[0], "name": row[1], "gold": row[2], "platinum": row[3],
                  "experience": row[4] if not daily_bonus_xp else new_xp,
                  "level": row[5], "flags": flags, "daily_bonus_xp": daily_bonus_xp}
        return result
    _db.execute("INSERT OR IGNORE INTO users (id, name, last_login, flags) VALUES (?, ?, datetime('now'), '{}')", (uid, name))
    _db.commit()
    # Seed stardust for new users
    for rarity in ("common", "uncommon", "rare", "legendary", "promo"):
        _db.execute("INSERT OR IGNORE INTO stardust (user_id, rarity, quantity) VALUES (?, ?, 100)", (uid, rarity))
    # Grant new-player starting currency + shards + booster packs
    # (all per-new-player init lives in new_player.py).
    try:
        new_player.grant_new_player(_db, uid)
    except Exception as e:
        log(f"    WARN: new-player grant failed: {e}")
    _db.commit()
    return {"id": uid, "name": name, "gold": new_player.STARTING_GOLD,
            "platinum": new_player.STARTING_PLATINUM, "experience": 0, "level": 1,
            "flags": "{}"}


def _user_profile_from_row(row):
    """Build the profile shape used by the protocol handlers."""
    if not row:
        return None
    return {"id": row[0], "name": row[1], "gold": row[2],
            "platinum": row[3], "experience": row[4], "level": row[5],
            "flags": row[6]}


def db_get_user(user_id):
    """Load an existing user without changing login/account state."""
    row = _db.execute(
        "SELECT id, name, gold, platinum, experience, level, flags "
        "FROM users WHERE id=?", (int(user_id),)).fetchone()
    return _user_profile_from_row(row)


def db_get_user_by_client_auth_id(auth_id):
    """Recover a user from the stable client SAuthID used after reconnect.

    The game client can send profile/deck updates immediately after creating a
    new HConnect session, before sending the auth request again.  SAuthID is
    derived from the user's low 48-bit ID as ``base * 10 + 45``.
    """
    try:
        auth_id = int(auth_id)
        if auth_id < 45 or (auth_id - 45) % 10:
            return None
        base_id = (auth_id - 45) // 10
    except (TypeError, ValueError):
        return None
    row = _db.execute(
        "SELECT id, name, gold, platinum, experience, level, flags "
        "FROM users WHERE (id & 281474976710655)=? LIMIT 1",
        (base_id,)).fetchone()
    return _user_profile_from_row(row)


def db_get_stardust(user_id):
    rows = _db.execute("SELECT rarity, quantity FROM stardust WHERE user_id=?", (user_id,)).fetchall()
    return {r[0]: r[1] for r in rows}


# Stardust template GUIDs
STARDUST_TEMPLATES = {
    "common": "ab4a63a8-c378-4693-8b5a-97e423d3d47b",
    "uncommon": "3f2af3a4-8780-4095-ae81-d32b618f0595",
    "rare": "a2a6129b-978a-40ce-9673-73588e6a40c3",
    "legendary": "259a55a1-9e42-4a41-b8a1-ba33d32e62ed",
    "promo": "4844eb99-457a-457f-a246-449954ff305a",
}
CHEST_TEMPLATE = "a9ae9af2-e27a-48e0-9cd2-490d252fffe4"


def db_update_resources(user_id, gold=None, platinum=None, conn=None):
    connection = conn or _db
    if gold is not None:
        connection.execute("UPDATE users SET gold=? WHERE id=?", (gold, user_id))
    if platinum is not None:
        connection.execute("UPDATE users SET platinum=? WHERE id=?", (platinum, user_id))
    if conn is None:
        connection.commit()


def db_add_card(user_id, template_id):
    existing = _db.execute("SELECT id, quantity FROM collections WHERE user_id=? AND card_template_id=?", (user_id, template_id)).fetchone()
    if existing:
        _db.execute("UPDATE collections SET quantity=quantity+1 WHERE id=?", (existing[0],))
    else:
        _db.execute("INSERT INTO collections (user_id, card_template_id, quantity) VALUES (?, ?, 1)", (user_id, template_id))
    _db.commit()


def db_record_purchase(user_id, item_name, template_id, price, currency, conn=None):
    connection = conn or _db
    connection.execute("INSERT INTO store_purchases (user_id, item_name, item_template_id, price, currency) VALUES (?, ?, ?, ?, ?)",
                       (user_id, item_name, template_id, price, currency))
    if conn is None:
        connection.commit()


# === Inventory ===

def db_add_inventory(user_id, template_guid, quantity=1, conn=None):
    connection = conn or _db
    existing = connection.execute("SELECT id, quantity FROM player_inventory WHERE user_id=? AND template_guid=?", (user_id, template_guid)).fetchone()
    if existing:
        connection.execute("UPDATE player_inventory SET quantity=quantity+? WHERE id=?", (quantity, existing[0]))
    else:
        connection.execute("INSERT INTO player_inventory (user_id, template_guid, quantity) VALUES (?, ?, ?)", (user_id, template_guid, quantity))
    if conn is None:
        connection.commit()


def db_get_inventory(user_id):
    """Return list of (template_guid, quantity) for profile push."""
    rows = _db.execute("SELECT template_guid, quantity FROM player_inventory WHERE user_id=?", (user_id,)).fetchall()
    return [(r[0], r[1]) for r in rows]


# === Arena (Frost Ring Arena) ===

def db_get_arena_state(user_id, initialize=True):
    """Get arena state for user, initializing if needed.

    Card serialization can ask for the current socketed gem while a game
    transaction is being advanced.  That read must not create or commit an
    ``arena_state`` row, because doing so can contend with the game's writer
    (or the tournament scheduler) and stall the client-facing turn packet.
    Callers that only need an existing state can pass ``initialize=False``.
    """
    if initialize:
        _db.execute("INSERT OR IGNORE INTO arena_state (user_id) VALUES (?)", (user_id,))
        _db.commit()
    row = _db.execute("SELECT deck_id, wins, losses, challenger_index, fight_history, gold_earned, chests_earned, sacks_earned FROM arena_state WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return {"deck_id": 0, "wins": 0, "losses": 0,
                "challenger_index": 0, "fight_history": "[]",
                "gold_earned": 0, "chests_earned": 0, "sacks_earned": 0}
    return {"deck_id": row[0] or 0, "wins": row[1] or 0, "losses": row[2] or 0, "challenger_index": row[3] or 0,
            "fight_history": row[4] or "[]", "gold_earned": row[5] or 0, "chests_earned": row[6] or 0, "sacks_earned": row[7] or 0}


def db_get_arena_fight_history(user_id):
    """Return the arena's fixed twenty fight slots for lobby rendering.

    The client expects a slot for every opponent.  Missing slots are not the
    same as an empty history: without them it replaces the received history
    with a local placeholder list and loses completed-fight markers.
    """
    arena = db_get_arena_state(user_id)
    try:
        raw = json.loads(arena.get("fight_history", "[]") or "[]")
    except (TypeError, ValueError):
        raw = []
    history = []
    for index in range(20):
        item = raw[index] if index < len(raw) and isinstance(raw[index], dict) else {}
        result = str(item.get("result", "NONE") or "NONE").upper()
        if result == "LOSS":
            result = "LOSE"
        history.append({
            "fight_id": int(item.get("fight_id", index + 1) or index + 1),
            "fight_tier": int(item.get("fight_tier", index // 5 + 1) or index // 5 + 1),
            "fight_order": int(item.get("fight_order", index + 1) or index + 1),
            "challenger_instance": int(item.get("challenger_instance", index + 1) or index + 1),
            "result": result,
            "is_boss": (
                bool(item.get("is_boss"))
                if isinstance(item.get("is_boss"), (bool, int))
                else (str(item.get("is_boss")).lower() == "true"
                      if item.get("is_boss") is not None else None)
            ),
            "round_challenge": str(item.get("round_challenge", "00000000-0000-0000-0000-000000000000") or "00000000-0000-0000-0000-000000000000"),
            "challenge_response": str(item.get("challenge_response", "NONE") or "NONE").upper(),
            "active_challenges": [
                str(guid) for guid in item.get("active_challenges", [])
                if guid and str(guid) != "00000000-0000-0000-0000-000000000000"
            ] if isinstance(item.get("active_challenges", []), list) else [],
        })
    return history


def db_get_fra_challenge(conversation_guid=None, challenge_key=None):
    """Return one enabled extracted FRA challenge by GUID or stable key."""
    if conversation_guid is not None:
        row = _db.execute(
            """
            SELECT conversation_guid, challenge_key, challenge_name,
                   challenge_order, probability_percent, dialogue_text,
                   answer_text, objective_heading, objective_text,
                   modifications_json, metadata_json
            FROM fra_challenges
            WHERE conversation_guid=? AND enabled=1
            """,
            (str(conversation_guid),),
        ).fetchone()
    elif challenge_key is not None:
        row = _db.execute(
            """
            SELECT conversation_guid, challenge_key, challenge_name,
                   challenge_order, probability_percent, dialogue_text,
                   answer_text, objective_heading, objective_text,
                   modifications_json, metadata_json
            FROM fra_challenges
            WHERE challenge_key=? AND enabled=1
            """,
            (str(challenge_key),),
        ).fetchone()
    else:
        return None
    if not row:
        return None
    return {
        "conversation_guid": row[0], "challenge_key": row[1],
        "challenge_name": row[2], "challenge_order": row[3],
        "probability_percent": row[4], "dialogue_text": row[5],
        "answer_text": row[6], "objective_heading": row[7],
        "objective_text": row[8], "modifications_json": row[9],
        "metadata_json": row[10],
    }


def db_get_active_fra_challenges(user_id):
    """Return challenge definitions active for the current FRA run.

    Active challenge GUIDs live in the run JSON.  The first fight's
    ``round_challenge`` remains a backward-compatible fallback for runs saved
    before the explicit active list was added.
    """
    history = db_get_arena_fight_history(user_id)
    if not history:
        return []
    guids = list(history[0].get("active_challenges", []))
    if not guids:
        guid = history[0].get("round_challenge", "")
        if guid and guid != "00000000-0000-0000-0000-000000000000":
            guids = [guid]
    return [challenge for guid in guids
            if (challenge := db_get_fra_challenge(conversation_guid=guid))]


def db_roll_fra_start_challenge(user_id, rng=None):
    """Select the optional challenge for a newly started, full FRA run.

    The challenge is stored in fight zero so reconnects and the battle-mod
    request see the same result.  The probability is read from the extracted
    challenge metadata; the current Starting Health 15 row is five percent.
    """
    import random

    arena = db_get_arena_state(user_id)
    if int(arena.get("challenger_index", 0) or 0) != 0:
        return None

    history = db_get_arena_fight_history(user_id)
    existing_guid = history[0].get("round_challenge", "")
    if existing_guid and existing_guid != "00000000-0000-0000-0000-000000000000":
        return db_get_fra_challenge(conversation_guid=existing_guid)

    challenge = db_get_fra_challenge(challenge_key="starting_health_15")
    if not challenge:
        return None
    probability = max(0, min(100, int(challenge.get("probability_percent", 5) or 0)))
    roller = rng or random.SystemRandom()
    if roller.randrange(100) >= probability:
        return None

    history[0]["round_challenge"] = challenge["conversation_guid"]
    history[0]["challenge_response"] = "NONE"
    history[0]["active_challenges"] = [challenge["conversation_guid"]]
    db_update_arena_state(user_id, fight_history=json.dumps(history))
    return challenge


def db_record_arena_fight(user_id, won):
    """Record the current FRA opponent and advance to the next roster slot."""
    arena = db_get_arena_state(user_id)
    challengers = db_get_fra_challengers(user_id)
    index = int(arena.get("challenger_index", 0) or 0)
    if index >= len(challengers):
        return False
    history = db_get_arena_fight_history(user_id)
    result = "WIN" if won else "LOSE"
    if history[index]["result"] not in ("WIN", "LOSE"):
        history[index]["result"] = result
        history[index]["challenger_instance"] = challengers[index]["id"]
        history[index]["fight_id"] = challengers[index]["id"]
        history[index]["fight_tier"] = index // 5 + 1
        history[index]["fight_order"] = index + 1
        is_boss = str(challengers[index].get("boss", "False")).lower() == "true"
        history[index]["is_boss"] = is_boss
        gold_earned = int(arena.get("gold_earned", 0) or 0)
        chests_earned = int(arena.get("chests_earned", 0) or 0)
        if won:
            if is_boss:
                chests_earned += 1
            else:
                gold_earned += 1
        db_update_arena_state(
            user_id,
            wins=int(arena.get("wins", 0) or 0) + (1 if won else 0),
            losses=int(arena.get("losses", 0) or 0) + (0 if won else 1),
            challenger_index=index + 1,
            fight_history=json.dumps(history),
            gold_earned=gold_earned,
            chests_earned=chests_earned,
        )
    return True


def db_update_arena_state(user_id, **kwargs):
    """Update arena state fields."""
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [user_id]
    _db.execute(f"UPDATE arena_state SET {sets} WHERE user_id=?", vals)
    _db.commit()


def db_clear_fra_challengers(user_id):
    """Remove the saved opponent roster for one player's arena run."""
    _db.execute("DELETE FROM fra_challengers WHERE user_id=?", (user_id,))
    _db.commit()


def db_create_fra_challengers(user_id, rng=None):
    """Create and save the 20 opponents for a new Frost Ring Arena run.

    Encounter rank ranges are inclusive. Ranks 10, 15, and 20 always use a
    known boss-version encounter. Positions 9, 12, 14, 17, and 19 always use
    the eligible elite version of a normal deck family. Elite encounters are
    not bosses unless they belong to a known boss family.

    ``rng`` is injectable so the selection rules can be tested without
    changing the production random source.
    """
    rows = _db.execute(
        """
        SELECT deck_guid, name, champion_guid,
               COALESCE(is_boss, 0), COALESCE(is_elite, 0),
               base_deck_name, COALESCE(min_rank, 6),
               COALESCE(max_rank, 19)
        FROM fra_encounters
        """
    ).fetchall()
    encounters = [
        {
            "deck": row[0], "name": row[1], "champion": row[2],
            "is_boss": bool(row[3]), "is_elite": bool(row[4]),
            "base": row[5], "min_rank": row[6], "max_rank": row[7],
        }
        for row in rows
    ]

    from gamemodes.arena import is_boss_encounter, select_fra_roster

    selected = [(
            user_id,
            rank - 1,
            chosen["name"],
            chosen["champion"],
            chosen["deck"],
            int(is_boss_encounter(chosen)),
        ) for rank, chosen in select_fra_roster(encounters, rng=rng)]

    _db.execute("DELETE FROM fra_challengers WHERE user_id=?", (user_id,))
    _db.executemany(
        """
        INSERT INTO fra_challengers
            (user_id, challenger_index, name, champion_guid,
             encounter_deck_guid, is_boss)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        selected,
    )
    _db.commit()
    return db_get_fra_challengers(user_id)


def db_get_fra_challengers(user_id):
    """Return the saved FRA opponent roster for one player."""
    rows = _db.execute(
        """
        SELECT challenger_index, name, champion_guid,
               encounter_deck_guid, is_boss
        FROM fra_challengers
        WHERE user_id=?
        ORDER BY challenger_index
        """,
        (user_id,),
    ).fetchall()
    return [
        {
            "id": r[0] + 1,
            "name": r[1],
            "champion_guid": r[2],
            "deck": r[3],
            "boss": "True" if r[4] else "False",
        }
        for r in rows
    ]


def db_get_fra_public_base_encounter(deck_guid):
    """Return the normal encounter behind an elite FRA deck, if any.

    The saved challenger roster must retain the selected elite deck. This
    lookup is only for the pre-fight lobby projection, where the client must
    not learn that a future opponent was upgraded.
    """
    row = _db.execute(
        """
        SELECT base.deck_guid, base.name, base.champion_guid
        FROM fra_encounters AS selected
        JOIN fra_encounters AS base
          ON base.base_deck_name = selected.base_deck_name
         AND COALESCE(base.is_elite, 0) = 0
        WHERE selected.deck_guid=?
          AND COALESCE(selected.is_elite, 0) = 1
        ORDER BY base.deck_guid
        LIMIT 1
        """,
        (deck_guid,),
    ).fetchone()
    if not row:
        return None
    return {"deck": row[0], "name": row[1], "champion_guid": row[2]}


def db_get_player_champion_guid(deck_db_id):
    """Get the champion GUID for a player's deck."""
    row = _db.execute("SELECT pvp_champion_guid FROM decks WHERE id=?", (deck_db_id,)).fetchone()
    if row and row[0] and row[0] != "00000000-0000-0000-0000-000000000000":
        return row[0]
    return None  # Will use default


# === Redeem codes / store ===

def db_redeem_code(code, conn=None):
    connection = conn or _db
    row = connection.execute("SELECT id, gold_delta, platinum_delta, uses, max_uses FROM redeem_codes WHERE code=?", (code,)).fetchone()
    if not row:
        return None
    if row[3] >= row[4]:
        return None  # max uses reached
    connection.execute("UPDATE redeem_codes SET uses=uses+1 WHERE id=?", (row[0],))
    if conn is None:
        connection.commit()
    return {"gold": row[1], "platinum": row[2]}


def db_get_store_items():
    rows = _db.execute("SELECT template_guid, name, short_desc, price, currency, store_tab FROM store_items ORDER BY id").fetchall()
    return [{"n": r[1], "s": r[2] or "", "price": r[3], "currency": r[4],
             "template_guid": r[0], "t": r[5]} for r in rows]


def db_primal_pack_for(pack_guid):
    """Return the Primal pack GUID for the same set as *pack_guid*, or None.

    Data-driven via pack_set_map: a normal booster (is_full_set=0, is_primal=0)
    maps to its set, and the set's Primal pack (is_primal=1) is returned when
    one exists (e.g. core Sets 1-4).  Non-pack items / sets without a Primal
    version return None so no upgrade applies.
    """
    row = _db.execute(
        "SELECT set_guid, is_full_set, is_primal FROM pack_set_map "
        "WHERE pack_guid=?", (pack_guid,)).fetchone()
    if not row or row[1] or row[2]:
        return None
    p = _db.execute(
        "SELECT pack_guid FROM pack_set_map "
        "WHERE set_guid=? AND is_primal=1 LIMIT 1", (row[0],)).fetchone()
    return p[0] if p else None


# === Emails ===

def db_find_mail_recipient(name):
    """Find a user by full identity or its display-name portion."""
    if not name:
        return None
    name = str(name).strip()
    row = _db.execute(
        "SELECT id, name FROM users WHERE LOWER(name)=LOWER(?) "
        "OR LOWER(CASE WHEN instr(name, '#') > 0 "
        "THEN substr(name, 1, instr(name, '#') - 1) ELSE name END)=LOWER(?) "
        "LIMIT 1", (name, name)).fetchone()
    return {"id": row[0], "name": row[1]} if row else None

def db_send_email(user_id, subject, body, sender="SYSTEM", gold_delivered=0, platinum_delivered=0, conn=None):
    connection = conn or _db
    connection.execute("INSERT INTO emails (user_id, sender, subject, body, gold_delivered, platinum_delivered) VALUES (?, ?, ?, ?, ?, ?)",
                       (user_id, sender, subject, body, gold_delivered, platinum_delivered))
    if conn is None:
        connection.commit()


# === Decks ===

def db_save_deck(user_id, deck_name, cards_json="[]", pve_champion_id=None, pvp_champion_guid=None, active_gems_json="{}", gem_abilities_json="{}", deck_sleeve_guid=None, gameboard_guid=None, coin_guid=None, conn=None):
    connection = conn or _db
    connection.execute("INSERT INTO decks (user_id, deck_name, cards, pve_champion_id, pvp_champion_guid, active_gems, gem_abilities, deck_sleeve_guid, gameboard_guid, coin_guid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (user_id, deck_name, cards_json, pve_champion_id, pvp_champion_guid, active_gems_json, gem_abilities_json, deck_sleeve_guid, gameboard_guid, coin_guid))
    if conn is None:
        connection.commit()
    return connection.execute("SELECT last_insert_rowid()").fetchone()[0]


def db_update_deck(deck_id, user_id, deck_name=None, cards_json=None, pve_champion_id=None, pvp_champion_guid=None, active_gems_json=None, gem_abilities_json=None, deck_sleeve_guid=None, gameboard_guid=None, coin_guid=None):
    updates = []
    params = []
    if deck_name is not None:
        updates.append("deck_name=?")
        params.append(deck_name)
    if cards_json is not None:
        updates.append("cards=?")
        params.append(cards_json)
    if pve_champion_id is not None:
        updates.append("pve_champion_id=?")
        params.append(pve_champion_id)
    if pvp_champion_guid is not None:
        updates.append("pvp_champion_guid=?")
        params.append(pvp_champion_guid)
    if active_gems_json is not None:
        updates.append("active_gems=?")
        params.append(active_gems_json)
    if gem_abilities_json is not None:
        updates.append("gem_abilities=?")
        params.append(gem_abilities_json)
    if deck_sleeve_guid is not None:
        updates.append("deck_sleeve_guid=?")
        params.append(deck_sleeve_guid)
    if gameboard_guid is not None:
        updates.append("gameboard_guid=?")
        params.append(gameboard_guid)
    if coin_guid is not None:
        updates.append("coin_guid=?")
        params.append(coin_guid)
    if updates:
        updates.append("last_saved=datetime('now')")
        params.extend([deck_id, user_id])
        _db.execute(f"UPDATE decks SET {', '.join(updates)} WHERE id=? AND user_id=?", params)
        _db.commit()
        return True
    return False


def db_get_deck_by_id(deck_id):
    """Return a deck dict by its primary key, or None."""
    row = _db.execute(
        "SELECT id, deck_name, cards, pve_champion_id, pvp_champion_guid, "
        "active_gems, deck_sleeve_guid, gameboard_guid, coin_guid "
        "FROM decks WHERE id=?", (deck_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "deck_name": row[1], "cards": row[2],
        "pve_champion_id": row[3], "pvp_champion_guid": row[4],
        "active_gems": row[5], "deck_sleeve_guid": row[6],
        "gameboard_guid": row[7], "coin_guid": row[8]
    }


def db_get_decks(user_id):
    rows = _db.execute("SELECT id, deck_name, cards, pve_champion_id, pvp_champion_guid, active_gems, deck_sleeve_guid, gameboard_guid, coin_guid FROM decks WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    return [{"id": r[0], "name": r[1], "cards": r[2], "pve_champion_id": r[3], "pvp_champion_guid": r[4], "active_gems": r[5], "deck_sleeve_guid": r[6], "gameboard_guid": r[7], "coin_guid": r[8]} for r in rows]


# === Sessions (reconnect) ===

def db_save_session(sid, user_id, username, auth_id, reck_id, uid, addr):
    _db.execute("INSERT OR REPLACE INTO sessions (sid, user_id, username, client_auth_id, client_reck_id, client_uid, addr) VALUES (?,?,?,?,?,?,?)",
                (sid, user_id, username, auth_id, reck_id, uid, addr))
    _db.commit()


def db_find_session(addr):
    row = _db.execute("SELECT sid, username, client_auth_id, client_reck_id FROM sessions WHERE addr=? ORDER BY created_at DESC LIMIT 1", (addr,)).fetchone()
    if row:
        return {"username": row[1], "auth_id": row[2], "reck_id": row[3]}
    return None


# === Chat ===

CHAT_HISTORY_HOURS = 24

def db_store_chat(user_id, sender, room, message, icon="", flags=""):
    _db.execute("INSERT INTO chat_messages (user_id, sender, room, message, icon, flags) VALUES (?,?,?,?,?,?)",
                (user_id, sender, room, message, icon, flags))
    _db.commit()
    # Keep only last 500 messages per room
    _db.execute("DELETE FROM chat_messages WHERE room=? AND id NOT IN (SELECT id FROM chat_messages WHERE room=? ORDER BY id DESC LIMIT 500)",
                (room, room))
    _db.commit()


def db_get_recent_chat(room, limit=30):
    rows = _db.execute(
        "SELECT sender, message, icon, flags, created_at "
        "FROM chat_messages "
        "WHERE room=? AND created_at >= datetime('now', ?) "
        "ORDER BY id DESC LIMIT ?",
        (room, f"-{CHAT_HISTORY_HOURS} hours", limit)).fetchall()
    return [{"user": r[0], "msg": r[1], "icon": r[2], "flags": r[3], "time": r[4]} for r in reversed(rows)]


# === Inbound transaction capture -------------------------------------------

def db_session_state_hash(session_id):
    """Return a deterministic digest of the authoritative session state.

    The digest includes persisted battle state and every card instance,
    including hidden-zone cards. It is for replay comparison, not client
    visibility.
    """
    session_row = _db.execute(
        "SELECT state, players_json, turn_order_json, seed_z, seed_w, "
        "deck_template_id FROM game_sessions WHERE session_id=?",
        (str(session_id),)).fetchone()
    if session_row is None:
        return ""

    card_columns = [row[1] for row in _db.execute(
        "PRAGMA table_info(game_cards)")]
    card_rows = _db.execute(
        "SELECT * FROM game_cards WHERE session_id=? ORDER BY id",
        (str(session_id),)).fetchall()
    snapshot = {
        "session": list(session_row),
        "cards": [dict(zip(card_columns, row)) for row in card_rows],
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                         default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def db_record_session_transaction(session_id, player_uid, request_id,
                                  data_type, compressed, transaction_id,
                                  transaction_type, classification, inner_bytes,
                                  pre_state_hash):
    """Persist one raw inbound transaction before rules resolution."""
    payload = inner_bytes if isinstance(inner_bytes, bytes) else b""
    cursor = _db.execute(
        "INSERT INTO session_transactions "
        "(session_id, player_uid, received_seq, data_type, request_id, "
        "compressed, transaction_id, transaction_type, classification_json, "
        "inner_bytes, pre_state_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (str(session_id), str(player_uid), time.time_ns(), int(data_type),
         int(request_id or 0), int(compressed or 0), int(transaction_id),
         str(transaction_type or ""),
         json.dumps(classification or {}, sort_keys=True, default=str),
         payload, str(pre_state_hash or "")))
    _db.commit()
    return int(cursor.lastrowid)


def db_complete_session_transaction(row_id, post_state_hash, handled,
                                    error=""):
    """Mark a captured transaction after its handler has returned."""
    _db.execute(
        "UPDATE session_transactions SET post_state_hash=?, status=?, "
        "handled=?, completed_at=datetime('now'), error=? WHERE id=?",
        (str(post_state_hash or ""), "completed" if not error else "error",
         1 if handled else 0, str(error or ""), int(row_id)))
    _db.commit()


# === Replay event log ===

# Only real player-versus-player sessions are candidates for the public replay
# browser.  Practice uses ``Session-*`` and Campaign uses ``camp_*``; neither
# should retain its event stream.  ``Challenge_*`` covers direct PvP invites,
# while ``pvp-`` is reserved for other direct/matchmaking PvP sessions.
_REPLAYABLE_PVP_SESSION_PREFIXES = ("tourney-", "pvp-", "Challenge_")


def _is_replayable_pvp_session(session_id):
    """Return whether *session_id* belongs to a PvP replayable game."""
    row = _db.execute(
        "SELECT session_name FROM game_sessions WHERE session_id=?",
        (str(session_id),),
    ).fetchone()
    return bool(row and (row[0] or "").startswith(
        _REPLAYABLE_PVP_SESSION_PREFIXES))

def _record_session_events(session_id, target_player_uid, event_byte_list):
    """Persist an event batch for replay (installed as event_logger hook)."""
    try:
        # Normalize UID objects to their uint64 TEXT form (matches
        # game_sessions.session_id so replays can be looked up by session).
        sid = session_id.to_uint64() if hasattr(session_id, 'to_uint64') else session_id
        tid = target_player_uid.to_uint64() if hasattr(target_player_uid, 'to_uint64') else target_player_uid
        if not _is_replayable_pvp_session(sid):
            return
        for raw in event_byte_list:
            # event_class = int32 LE at start of each SessionEventArgs payload.
            cls = struct.unpack('<i', raw[:4])[0] if len(raw) >= 4 else 0
            _db.execute(
                "INSERT INTO session_events (session_id, target_player_uid, seq, event_class, event_bytes) "
                "VALUES (?,?,?,?,?)",
                (str(sid), str(tid),
                 int(time.time() * 1000), cls, raw))
        _db.commit()
    except Exception:
        pass


# --- Tournaments ---------------------------------------------------------------

_TOURNEY_LIST_COLS = (
    "t.id", "t.type_id", "t.status", "t.players_json", "t.session_id",
    "t.created_at", "tt.name AS type_name", "tt.style", "tt.format",
    "tt.min_players", "tt.max_players", "tt.games_count", "tt.set_id")

def _tourney_row_to_dict(row):
    """Convert a raw tuple to a dict keyed by _TOURNEY_LIST_COLS aliases."""
    if not row:
        return None
    keys = [c.split(" AS ")[-1] if " AS " in c else c.split(".")[-1]
            for c in _TOURNEY_LIST_COLS]
    return dict(zip(keys, row))


def _tourney_select(base="t.*, tt.name AS type_name, tt.style, tt.format, "
                        "tt.min_players, tt.max_players, tt.games_count, "
                        "tt.set_id"):
    return (f"SELECT {base} FROM tournaments t "
            "JOIN tournament_types tt ON t.type_id = tt.id")


def db_tournament_types():
    rows = _db.execute(
        "SELECT id, name, style, format, min_players, max_players, "
        "games_count, set_id FROM tournament_types ORDER BY id").fetchall()
    keys = ("id", "name", "style", "format", "min_players", "max_players",
            "games_count", "set_id")
    return [dict(zip(keys, r)) for r in rows]


def db_tournament_list(status=None):
    if status:
        rows = _db.execute(
            _tourney_select() + " WHERE t.status=? ORDER BY t.id",
            (status,)).fetchall()
    else:
        rows = _db.execute(
            _tourney_select() + " ORDER BY t.id").fetchall()
    return [_tourney_row_to_dict(r) for r in rows]


def db_tournament_completed_for_player(player_uid):
    """Return completed tournaments in which *player_uid* signed up.

    Signup rows are retained after a withdrawal, so this also preserves a
    player's completed history when their final status is no longer active.
    """
    rows = _db.execute(
        _tourney_select(
            "DISTINCT t.*, tt.name AS type_name, tt.style, tt.format, "
            "tt.min_players, tt.max_players, tt.games_count, tt.set_id") +
        " JOIN tournament_signups ts ON ts.tournament_id=t.id "
        "WHERE LOWER(t.status) IN ('complete', 'closed') "
        "AND ts.player_uid=? ORDER BY t.id",
        (int(player_uid),)).fetchall()
    return [_tourney_row_to_dict(r) for r in rows]


def db_tournament_by_id(tid):
    try:
        row = _db.execute(
            _tourney_select() + " WHERE t.id=?", (tid,)).fetchone()
    except (OverflowError, sqlite3.InterfaceError):
        return None
    return _tourney_row_to_dict(row)


def db_tournament_create(inst_id, type_id):
    _db.execute(
        "INSERT OR IGNORE INTO tournaments (id, type_id) VALUES (?, ?)",
        (inst_id, type_id))
    _db.commit()
    return inst_id


def db_tournament_update_players(tid, players_json):
    _db.execute(
        "UPDATE tournaments SET players_json=? WHERE id=?", (players_json, tid))
    _db.commit()
    return len(json.loads(players_json)) if players_json else 0


def db_tournament_set_status(tid, status, session_id=None):
    if session_id:
        _db.execute(
            "UPDATE tournaments SET status=?, session_id=? WHERE id=?",
            (status, str(session_id), tid))
    else:
        _db.execute(
            "UPDATE tournaments SET status=? WHERE id=?", (status, tid))
    _db.commit()


def db_tournament_count_by_status(status):
    row = _db.execute(
        "SELECT COUNT(*) FROM tournaments WHERE status=?", (status,)).fetchone()
    return row[0] if row else 0


def db_tournament_close_orphaned_started():
    """Close started tournaments whose persisted game session is gone.

    ``started`` is an active match state, while ``waiting`` is deliberately
    kept open by the tournament pool scheduler.  A started room with no
    corresponding game session cannot be resumed and otherwise remains
    permanently open in the tournament database, so mark only those rows
    closed.  Live sessions, including disconnected games that can be
    rejoined, are left untouched.
    """
    cursor = _db.execute(
        "UPDATE tournaments SET status='closed' "
        "WHERE status='started' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM game_sessions gs "
        "  WHERE CAST(gs.session_id AS TEXT)=CAST(tournaments.session_id AS TEXT)"
        ")"
    )
    _db.commit()
    return int(cursor.rowcount or 0)


def db_tournament_count_active_by_type(type_id):
    row = _db.execute(
        "SELECT COUNT(*) FROM tournaments WHERE type_id=? AND status='waiting'",
        (type_id,)).fetchone()
    return row[0] if row else 0


def db_tournament_next_id():
    row = _db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM tournaments").fetchone()
    return max(10000, int(row[0])) if row else 10000


def db_tournament_deck_create(tournament_id, player_uid, cards_json):
    _db.execute(
        "INSERT INTO tournament_decks (tournament_id, player_uid, cards_json) "
        "VALUES (?, ?, ?)",
        (tournament_id, player_uid, cards_json))
    _db.commit()


def db_tournament_deck_by_player(tournament_id, player_uid):
    row = _db.execute(
        "SELECT * FROM tournament_decks "
        "WHERE tournament_id=? AND player_uid=?",
        (tournament_id, player_uid)).fetchone()
    if not row:
        return None
    keys = ("id", "tournament_id", "player_uid", "cards_json", "created_at")
    return dict(zip(keys, row))


_SIGNUP_KEYS = ("id", "tournament_id", "player_uid", "player_name", "deck_id",
                "entry_group", "fee_paid", "status", "created_at")


def db_tournament_signup_add(tid, player_uid, player_name, deck_id=0,
                             entry_group=0, fee_paid=0):
    """Upsert a signup for a player. Rejoining reactivates the row."""
    _db.execute(
        "INSERT INTO tournament_signups "
        "(tournament_id, player_uid, player_name, deck_id, entry_group, fee_paid) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(tournament_id, player_uid) DO UPDATE SET "
        "player_name=excluded.player_name, deck_id=excluded.deck_id, "
        "entry_group=excluded.entry_group, fee_paid=excluded.fee_paid, "
        "status='active'",
        (tid, player_uid, player_name, deck_id, entry_group, fee_paid))
    _db.commit()


def db_tournament_signup_by_player(tid, player_uid):
    row = _db.execute(
        "SELECT * FROM tournament_signups "
        "WHERE tournament_id=? AND player_uid=?",
        (tid, player_uid)).fetchone()
    return dict(zip(_SIGNUP_KEYS, row)) if row else None


def db_tournament_signups_by_tournament(tid, status="active"):
    if status is None:
        rows = _db.execute(
            "SELECT * FROM tournament_signups "
            "WHERE tournament_id=? ORDER BY id", (tid,)).fetchall()
    else:
        rows = _db.execute(
            "SELECT * FROM tournament_signups "
            "WHERE tournament_id=? AND status=? ORDER BY id",
            (tid, status)).fetchall()
    return [dict(zip(_SIGNUP_KEYS, r)) for r in rows]


def db_tournament_signup_set_status(tid, player_uid, status):
    _db.execute(
        "UPDATE tournament_signups SET status=? "
        "WHERE tournament_id=? AND player_uid=?",
        (status, tid, player_uid))
    _db.commit()


def db_tournament_players_name_map(tid):
    """Return {str(player_uid): name} for active signups."""
    rows = _db.execute(
        "SELECT player_uid, player_name FROM tournament_signups "
        "WHERE tournament_id=? AND status='active'",
        (tid,)).fetchall()
    return {str(r[0]): r[1] for r in rows}


_TOURNAMENT_MATCH_KEYS = (
    "id", "tournament_id", "round_id", "match_id", "player1_uid",
    "player2_uid", "session_id", "state", "status", "start_time",
    "end_time", "game1_winner", "game2_winner", "game3_winner",
)


def db_tournament_matches(tid):
    """Return tournament matches in display order (latest round first)."""
    rows = _db.execute(
        "SELECT * FROM tournament_matches "
        "WHERE tournament_id=? ORDER BY round_id DESC, id DESC", (tid,)
    ).fetchall()
    return [dict(zip(_TOURNAMENT_MATCH_KEYS, r)) for r in rows]


def db_tournament_match_start(tid, session_id, player1_uid, player2_uid,
                              round_id=1, start_time=0):
    """Create the match row for a game session, returning its database id."""
    _db.execute(
        "INSERT OR IGNORE INTO tournament_matches "
        "(tournament_id, round_id, match_id, player1_uid, player2_uid, "
        "session_id, state, status, start_time) VALUES (?, ?, ?, ?, ?, ?, "
        "'PlayGame', 'InProgress', ?)",
        (tid, int(round_id), int(round_id), int(player1_uid), int(player2_uid),
         str(session_id), int(start_time)),
    )
    _db.commit()
    row = _db.execute(
        "SELECT id FROM tournament_matches WHERE tournament_id=? "
        "AND session_id=? LIMIT 1", (tid, str(session_id))).fetchone()
    return int(row[0]) if row else 0


def db_tournament_match_result(tid, session_id, winner_uid, loser_uid,
                               end_time=0):
    """Close a match once, recording the winner in game 1."""
    row = _db.execute(
        "SELECT id, state FROM tournament_matches WHERE tournament_id=? "
        "AND session_id=? LIMIT 1", (tid, str(session_id))).fetchone()
    if not row:
        return 0
    if row[1] == "Complete":
        return int(row[0])
    _db.execute(
        "UPDATE tournament_matches SET state='Complete', status='Complete', "
        "end_time=?, game1_winner=? WHERE id=?",
        (int(end_time), int(winner_uid), int(row[0])),
    )
    _db.commit()
    return int(row[0])


# ---------------------------------------------------------------------------
# Game cards helpers — reusable self-contained queries.
# ---------------------------------------------------------------------------

def db_game_session_pids(session_id):
    """Return the player ids participating in a game session.

    Tournament sessions can contain extra ``game_cards`` rows created by an
    effect (or left behind by a reconnect).  Those rows are card ownership,
    not evidence that the owner is a player in the match.  Prefer the
    tournament signup rows whenever the session belongs to a tournament, and
    retain the card-owner fallback for practice/campaign sessions.
    """
    tournament_rows = _db.execute(
        "SELECT DISTINCT ts.player_uid "
        "FROM tournaments t "
        "JOIN tournament_signups ts ON ts.tournament_id=t.id "
        "WHERE t.session_id=? ORDER BY ts.player_uid",
        (session_id,)).fetchall()
    if len(tournament_rows) >= 2:
        return [r[0] for r in tournament_rows]
    rows = _db.execute(
        "SELECT DISTINCT user_id FROM game_cards WHERE session_id=?",
        (session_id,)).fetchall()
    return [r[0] for r in rows]


def db_game_champion(session_id, user_id):
    """Return (card_uid, template_guid) for a champion, or None."""
    row = _db.execute(
        "SELECT card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND is_champion=1 LIMIT 1",
        (session_id, user_id)).fetchone()
    return row


def db_game_deck_cards(session_id, user_id):
    """Return list of (card_uid, template_guid) for deck cards in position order."""
    rows = _db.execute(
        "SELECT card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck' ORDER BY position",
        (session_id, user_id)).fetchall()
    return rows


def db_game_draw_cards(session_id, user_id, count=7):
    """Move the first *count* deck cards to hand. Returns list of (card_uid, template_guid)."""
    rows = _db.execute(
        "SELECT card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck' "
        "ORDER BY position LIMIT ?",
        (session_id, user_id, count)).fetchall()
    for cu, _ in rows:
        _db.execute(
            "UPDATE game_cards SET location='hand' WHERE card_uid=? AND session_id=?",
            (int(cu), session_id))
    _db.commit()
    return rows


def db_game_get_hand(session_id, user_id):
    """Return list of (card_uid, template_guid) for cards in hand, ordered by position."""
    return _db.execute(
        "SELECT card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='hand' ORDER BY position",
        (session_id, user_id)).fetchall()


def db_game_card_type(template_guid):
    """Return ECardTypes value for a template GUID, or 'Troop'."""
    if not template_guid:
        return "Troop"
    row = _db.execute(
        "SELECT card_type FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else "Troop"


def db_game_shuffle_deck(session_id, user_id):
    """Shuffle deck positions using random offsets."""
    import random as _shuf_rnd
    rows = _db.execute(
        "SELECT card_uid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck'",
        (session_id, user_id)).fetchall()
    deck = [r[0] for r in rows]
    _shuf_rnd.shuffle(deck)
    for pos, cu in enumerate(deck):
        _db.execute(
            "UPDATE game_cards SET position=? WHERE card_uid=? AND session_id=?",
            (pos, int(cu), session_id))
    _db.commit()


def db_randomly_insert_deck_cards(session_id, user_id, card_uids,
                                  connection=None):
    """Randomly reinsert selected cards into a deck.

    The selected cards are removed from the ordered sequence, randomized, and
    inserted into random slots.  The relative order of every non-selected
    card is preserved; this is not a full deck shuffle.

    Return the selected card UIDs in their new deck order.
    """
    import random as _shuf_rnd

    conn = connection or _db
    wanted = {int(uid) for uid in (card_uids or [])}
    if not wanted:
        return []
    rows = conn.execute(
        "SELECT card_uid, position FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck' "
        "ORDER BY position",
        (session_id, user_id)).fetchall()
    if not rows:
        return []

    selected = [int(card_uid) for card_uid, _position in rows
                if int(card_uid) in wanted]
    if not selected:
        return []

    _shuf_rnd.shuffle(selected)
    slots = list(range(len(rows)))
    _shuf_rnd.shuffle(slots)
    selected_slots = sorted(slots[:len(selected)])
    selected_by_slot = dict(zip(selected_slots, selected))
    # Remove the selected cards from their old positions first.  Otherwise
    # inserting them into new slots duplicates them and silently drops the
    # cards displaced from those slots, corrupting the deck permutation.
    remaining = [int(card_uid) for card_uid, _position in rows
                 if int(card_uid) not in set(selected)]
    remaining_iter = iter(remaining)
    deck = []
    for rank in range(len(rows)):
        if rank in selected_by_slot:
            deck.append(selected_by_slot[rank])
        else:
            deck.append(next(remaining_iter))

    # Use temporary positions so this remains safe if a future schema adds a
    # uniqueness constraint on (session_id, position).
    offset = len(rows) + 1
    conn.executemany(
        "UPDATE game_cards SET position=position+? "
        "WHERE session_id=? AND user_id=? AND location='deck'",
        [(offset, session_id, user_id)])
    conn.executemany(
        "UPDATE game_cards SET position=? "
        "WHERE session_id=? AND card_uid=?",
        [(position, session_id, uid) for position, uid in enumerate(deck)])
    conn.commit()

    ordered = conn.execute(
        "SELECT card_uid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck' "
        "AND card_uid IN ({}) ORDER BY position".format(
            ",".join("?" * len(selected))),
        (session_id, user_id, *selected)).fetchall()
    return [int(row[0]) for row in ordered]


# --- Friend system helpers -------------------------------------------------

def db_get_friends(user_id):
    """Return [(friend_user_id, name, is_online), ...] for *user_id*."""
    rows = _db.execute(
        "SELECT f.friend_user_id, u.name "
        "FROM friends f JOIN users u ON u.id = f.friend_user_id "
        "WHERE f.user_id=?", (user_id,)).fetchall()
    return [(r[0], r[1], False) for r in rows]


def db_get_pending_friend_requests(user_id):
    """Return [sender_name, ...] for incoming friend requests."""
    rows = _db.execute(
        "SELECT u.name FROM friend_requests fr "
        "JOIN users u ON u.id = fr.from_user_id "
        "WHERE fr.to_user_id=?", (user_id,)).fetchall()
    return [r[0] for r in rows]


def db_get_ignored_list(user_id):
    """Return {ignored_user_id: name, ...} dict."""
    rows = _db.execute(
        "SELECT ip.ignored_user_id, u.name "
        "FROM ignored_players ip JOIN users u ON u.id = ip.ignored_user_id "
        "WHERE ip.user_id=?", (user_id,)).fetchall()
    return {r[0]: r[1] for r in rows}


def db_send_friend_request(from_user_id, to_user_name, conn=None):
    """Send a friend request. Returns (success, response_code_str, to_user_id|None)."""
    connection = conn or _db
    to_user = connection.execute("SELECT id FROM users WHERE LOWER(name) = LOWER(?)", (to_user_name,)).fetchone()
    if not to_user:
        return False, "UserDoesNotExist", None
    to_user_id = to_user[0]
    if from_user_id == to_user_id:
        return False, "SelfAdd", None
    # Check already sent
    existing = connection.execute(
        "SELECT 1 FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
        (from_user_id, to_user_id)).fetchone()
    if existing:
        return False, "RequestAlreadySent", to_user_id
    # Check already received (reverse direction)
    existing = connection.execute(
        "SELECT 1 FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
        (to_user_id, from_user_id)).fetchone()
    if existing:
        return False, "RequestAlreadyReceived", to_user_id
    # Check already friends
    existing = connection.execute(
        "SELECT 1 FROM friends WHERE user_id=? AND friend_user_id=?",
        (from_user_id, to_user_id)).fetchone()
    if existing:
        return False, "RequestAlreadySent", to_user_id  # already friends, treat as sent
    connection.execute("INSERT OR IGNORE INTO friend_requests (from_user_id, to_user_id) VALUES (?,?)",
                       (from_user_id, to_user_id))
    if conn is None:
        connection.commit()
    return True, "Success", to_user_id


def db_accept_friend_request(from_user_id, to_user_name, conn=None):
    """Accept a friend request. Returns (success, to_user_id|None)."""
    connection = conn or _db
    to_user = connection.execute("SELECT id FROM users WHERE LOWER(name) = LOWER(?)", (to_user_name,)).fetchone()
    if not to_user:
        return False, None
    to_user_id = to_user[0]
    # Find the request
    req = connection.execute(
        "SELECT 1 FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
        (to_user_id, from_user_id)).fetchone()
    if not req:
        return False, to_user_id
    connection.execute("DELETE FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
                       (to_user_id, from_user_id))
    connection.execute("INSERT OR IGNORE INTO friends (user_id, friend_user_id) VALUES (?,?)",
                       (from_user_id, to_user_id))
    connection.execute("INSERT OR IGNORE INTO friends (user_id, friend_user_id) VALUES (?,?)",
                       (to_user_id, from_user_id))
    if conn is None:
        connection.commit()
    return True, to_user_id


def db_ignore_friend_request(user_id, from_user_name, conn=None):
    """Ignore/decline a friend request from *from_user_name*."""
    connection = conn or _db
    from_user = connection.execute("SELECT id FROM users WHERE LOWER(name) = LOWER(?)", (from_user_name,)).fetchone()
    if not from_user:
        return False, None
    from_user_id = from_user[0]
    connection.execute("DELETE FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
                       (from_user_id, user_id))
    if conn is None:
        connection.commit()
    return True, from_user_id


def db_remove_friend(user_id, friend_name, conn=None):
    """Remove a friend. Returns (success, friend_user_id|None)."""
    connection = conn or _db
    friend = connection.execute("SELECT id FROM users WHERE LOWER(name) = LOWER(?)", (friend_name,)).fetchone()
    if not friend:
        return False, None
    friend_id = friend[0]
    connection.execute("DELETE FROM friends WHERE (user_id=? AND friend_user_id=?) OR (user_id=? AND friend_user_id=?)",
                       (user_id, friend_id, friend_id, user_id))
    if conn is None:
        connection.commit()
    return True, friend_id


def db_ignore_player(user_id, player_name, conn=None):
    """Add a player to the ignore list. Returns (success, ignored_user_id|None, code)."""
    connection = conn or _db
    player = connection.execute("SELECT id FROM users WHERE LOWER(name) = LOWER(?)", (player_name,)).fetchone()
    if not player:
        return False, None, "CouldNotIgnore"
    ignored_id = player[0]
    if ignored_id == user_id:
        return False, None, "CouldNotIgnore"
    existing = connection.execute(
        "SELECT 1 FROM ignored_players WHERE user_id=? AND ignored_user_id=?",
        (user_id, ignored_id)).fetchone()
    if existing:
        return False, ignored_id, "AlreadyIgnored"
    connection.execute("INSERT OR IGNORE INTO ignored_players (user_id, ignored_user_id) VALUES (?,?)",
                       (user_id, ignored_id))
    if conn is None:
        connection.commit()
    return True, ignored_id, "Success"


def db_unignore_player(user_id, player_name, conn=None):
    """Remove a player from the ignore list. Returns (success, unignored_user_id|None, code)."""
    connection = conn or _db
    player = connection.execute("SELECT id FROM users WHERE LOWER(name) = LOWER(?)", (player_name,)).fetchone()
    if not player:
        return False, None, "CouldNotUnignore"
    unignored_id = player[0]
    existing = connection.execute(
        "SELECT 1 FROM ignored_players WHERE user_id=? AND ignored_user_id=?",
        (user_id, unignored_id)).fetchone()
    if not existing:
        return False, unignored_id, "AlreadyUnignored"
    connection.execute("DELETE FROM ignored_players WHERE user_id=? AND ignored_user_id=?",
                       (user_id, unignored_id))
    if conn is None:
        connection.commit()
    return True, unignored_id, "Success"


def db_insert_game_card(session_id, user_id, card_uid, template_guid, location,
                        card_type="Troop", position=0, abilities_json=None,
                        attributes=0, is_champion=0, resolved_at=0):
    """Insert a row into game_cards with all required fields set correctly.
    
    Always sets owner_user_id = user_id so discards return to the correct graveyard.
    Returns the new row id.
    """
    owner = user_id or 0
    _db.execute(
        "INSERT INTO game_cards (session_id, user_id, card_uid, template_guid, "
        "card_template_id, location, position, card_type, card_abilities, "
        "card_attributes, owner_user_id, is_champion, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, user_id, card_uid, template_guid, template_guid,
         location, position, card_type,
         abilities_json or "[]", attributes or 0,
         owner, is_champion, resolved_at))
    _db.commit()
    return _db.execute("SELECT last_insert_rowid()").fetchone()[0]


def db_set_card_resolved_at(session_id, card_uid, resolved_at):
    """Stamp a card's resolved_at counter when it enters the warzone."""
    _db.execute(
        "UPDATE game_cards SET resolved_at=? WHERE session_id=? AND card_uid=?",
        (resolved_at, session_id, int(card_uid)))
    _db.commit()


def db_warzone_by_resolved_at(session_id, user_id=None):
    """Return warzone card_uids ordered by resolved_at (oldest first).
    Used for trigger resolution ordering (FIFO: first in, first resolved)."""
    sql = ("SELECT card_uid FROM game_cards "
           "WHERE session_id=? AND location='warzone'")
    params = [session_id]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    sql += " ORDER BY resolved_at ASC"
    rows = _db.execute(sql, params).fetchall()
    return [r[0] for r in rows]


def db_get_deck(deck_id, user_id):
    """Return a deck row: (id, cards, pvp_champion_guid, pve_champion_id)."""
    return _db.execute(
        "SELECT id, cards, pvp_champion_guid, pve_champion_id FROM decks "
        "WHERE id=? AND user_id=?", (deck_id, user_id)).fetchone()


def db_get_last_deck(user_id):
    """Return the player's last-saved deck."""
    return _db.execute(
        "SELECT id, cards, pvp_champion_guid, pve_champion_id FROM decks "
        "WHERE user_id=? ORDER BY last_saved DESC LIMIT 1", (user_id,)).fetchone()


def db_get_champion_guid(pve_champion_id):
    """Resolve a champion template GUID from a pve_champion_id."""
    row = _db.execute(
        "SELECT ct.guid FROM champion_templates ct "
        "JOIN champions c ON ct.race=c.race AND ct.champion_class=c.champion_class "
        "AND ct.gender=c.gender AND ct.is_player=1 "
        "WHERE c.id=?", (pve_champion_id,)).fetchone()
    return row[0] if row else None


def db_get_charge_power(champion_guid):
    """Get a champion's charge power ability GUID."""
    row = _db.execute(
        "SELECT charge_power FROM champion_templates WHERE guid=?",
        (champion_guid,)).fetchone()
    return row[0] if row and row[0] else None


def db_get_champion_ability_guids(champion_guid):
    """Get all ability GUIDs for a champion."""
    return [r[0] for r in _db.execute(
        "SELECT ability_guid FROM champion_abilities WHERE champion_guid=?",
        (champion_guid,)).fetchall()]


def db_get_card_abilities(template_guid):
    """Return (abilities_json, attributes) for a card template."""
    row = _db.execute(
        "SELECT abilities_json, attributes FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    if row:
        return (row[0] or "[]", int(row[1] or 0))
    return ("[]", 0)


def db_get_card_template_for_instance(instance_id, user_id):
    """Resolve a card_instances row to (template_guid, card_type)."""
    return _db.execute(
        "SELECT ci.template_guid, ct.card_type FROM card_instances ci "
        "JOIN card_templates ct ON ci.template_guid=ct.guid "
        "WHERE ci.instance_id=? AND ci.user_id=?",
        (instance_id, user_id)).fetchone()


def db_clear_session_cards(session_id):
    """Delete all game_cards rows for a session."""
    _db.execute("DELETE FROM game_cards WHERE session_id=?", (session_id,))
    _db.commit()


def db_delete_game_session(session_id):
    """Remove a completed game and every DB-owned row for that session."""
    sid = str(session_id)
    _db.execute("DELETE FROM game_cards WHERE session_id=?", (sid,))
    _db.execute("DELETE FROM session_events WHERE session_id=?", (sid,))
    _db.execute("DELETE FROM session_transactions WHERE session_id=?", (sid,))
    _db.execute("DELETE FROM game_sessions WHERE session_id=?", (sid,))
    _db.commit()


def db_move_cards_to_hand(session_id, card_uids):
    """Bulk-update card locations to 'hand'."""
    for uid in card_uids:
        _db.execute(
            "UPDATE game_cards SET location='hand', position=100 "
            "WHERE card_uid=? AND session_id=?",
            (uid, session_id))
    _db.commit()


def db_get_card_type(template_guid):
    """Return the card_type string for a template (or 'Troop')."""
    row = _db.execute(
        "SELECT card_type FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else "Troop"


# === Battle query helpers (read-only) ========================================

def db_target_template_text(template_id):
    """Return the game_text of a target template, or None."""
    row = _db.execute(
        "SELECT game_text FROM target_templates WHERE template_id=?",
        (str(template_id),)).fetchone()
    return (row[0] or "") if row else ""


def db_ability_meta_targets(ability_guid):
    """Return (target_template_ids_json, trigger_event_type, game_text, casting_behavior,
    is_manual, activation_cost, uses_per_game, uses_per_turn) for an ability GUID, or None."""
    row = _db.execute(
        "SELECT target_template_ids, trigger_event_type, game_text, "
        "casting_behavior, is_manual, activation_cost, "
        "uses_per_game, uses_per_turn FROM card_abilities_meta WHERE ability_guid=?",
        (str(ability_guid),)).fetchone()
    return row


def db_ability_effects(ability_guid):
    """Return list of effect_guid values for an ability's BOM."""
    rows = _db.execute(
        "SELECT effect_guid FROM ability_effects WHERE ability_guid=?",
        (ability_guid,)).fetchall()
    return [r[0] for r in rows]


def db_talent_ability_costs(ability_guid):
    """Return (charge_cost, spell_cost, activatable_phases, casting_behavior) or None."""
    row = _db.execute(
        "SELECT charge_cost, spell_cost, activatable_phases, casting_behavior "
        "FROM talent_abilities WHERE ability_guid=? LIMIT 1",
        (str(ability_guid),)).fetchone()
    return row


def db_champion_ability_guids(champion_guid):
    """Return list of ability_guid for a champion."""
    rows = _db.execute(
        "SELECT ability_guid FROM champion_abilities WHERE champion_guid=?",
        (champion_guid,)).fetchall()
    return [r[0] for r in rows]


def db_champion_ability_costs(ability_guid):
    """Return (charge_cost, spell_cost, activatable_phases, casting_behavior)
    for a champion's signature charge power (champion_abilities), or None.

    Champion charge powers are seeded from gamedata into champion_abilities,
    while talent_abilities only covers talents — so this is the fallback cost
    source for abilities like Dimmid's Lifedrain charge power."""
    row = _db.execute(
        "SELECT charge_cost, spell_cost, casting_behavior FROM champion_abilities "
        "WHERE ability_guid=? LIMIT 1",
        (str(ability_guid),)).fetchone()
    if not row:
        return None
    casting = row[2] or 0
    if casting == 64:
        # QuickAction: any priority window.
        return (row[0] or 0, row[1] or 0, 0, 64)
    if casting == 8:
        # BasicAction: the player's own main phases only (ETurnPhases
        # FirstMainPhase=10, SecondMainPhase=19) — mirrors the client's
        # CanActivateAbilityBase main-phase gate.
        return (row[0] or 0, row[1] or 0,
                (1 << 10) | (1 << 19), 8)
    # Unknown casting: no phase restriction.
    return (row[0] or 0, row[1] or 0, 0, 64)


def db_champion_ability_thresholds(ability_guid):
    """Return [(color_flag_name, required_quantity), ...] for a champion's
    charge power (e.g. Dimmid -> [("Diamond", 2)]), or [] when none."""
    row = _db.execute(
        "SELECT thresholds_json FROM champion_abilities WHERE ability_guid=? LIMIT 1",
        (str(ability_guid),)).fetchone()
    if not row or not row[0]:
        return []
    try:
        data = json.loads(row[0])
    except Exception:
        return []
    reqs = []
    for d in data if isinstance(data, list) else []:
        if isinstance(d, dict):
            reqs.append((str(d.get("color", "")),
                         int(d.get("quantity", 0) or 0)))
    return reqs


def db_champion_template_health(guid):
    """Return starting_health for a champion template GUID, or 20."""
    row = _db.execute(
        "SELECT starting_health FROM champion_template_data WHERE guid=?",
        (guid,)).fetchone()
    return row[0] if row else 20


def db_champion_template_health_by_class(race_name, cls_name):
    """Return starting_health from champion_class_data, or 20."""
    row = _db.execute(
        "SELECT starting_health FROM champion_class_data WHERE race=? AND champion_class=?",
        (race_name, cls_name)).fetchone()
    return row[0] if row else 20


def db_game_cards_at_location(session_id, location, card_type=None, user_id=None):
    """Return list of (card_uid, template_guid, ...) for cards in a zone."""
    sql = ("SELECT card_uid, template_guid, user_id, card_type, card_state, "
           "card_abilities, card_attributes "
           "FROM game_cards WHERE session_id=? AND location=?")
    params = [session_id, location]
    if card_type is not None:
        sql += " AND card_type=?"
        params.append(card_type)
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    sql += " ORDER BY position, id"
    return _db.execute(sql, params).fetchall()


def db_game_cards_at_location_scalar(session_id, location, user_id=None):
    """Return list of card_uid values for cards in a zone (lightweight)."""
    sql = "SELECT card_uid FROM game_cards WHERE session_id=? AND location=?"
    params = [session_id, location]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    sql += " ORDER BY position, id"
    rows = _db.execute(sql, params).fetchall()
    return [r[0] for r in rows]


def db_hand_card_count(session_id, user_id):
    """Return count of cards in a player's hand."""
    row = _db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? AND location='hand'",
        (session_id, user_id)).fetchone()
    return row[0] if row else 0


def db_warzone_troops_with_state(session_id, user_id=None):
    """Return list of (card_uid, template_guid, card_state, user_id) for warzone troops."""
    sql = ("SELECT card_uid, template_guid, card_state, user_id "
           "FROM game_cards WHERE session_id=? AND location='warzone' "
           "AND card_type LIKE '%Troop%'")
    params = [session_id]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    return _db.execute(sql, params).fetchall()


def db_hand_cards_with_templates(session_id, user_id):
    """Return list of (card_uid, cost, card_type, threshold_json, abilities_json)
    for cards in hand, joined with card_templates."""
    return _db.execute(
        "SELECT gc.card_uid, ct.cost, ct.card_type, ct.threshold_json, ct.abilities_json "
        "FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' ORDER BY gc.position",
        (session_id, user_id)).fetchall()


def db_hand_quick_actions(session_id, user_id):
    """Return list of (card_uid, cost, card_type, threshold_json, abilities_json)
    for QuickAction cards in hand."""
    return _db.execute(
        "SELECT gc.card_uid, ct.cost, ct.card_type, ct.threshold_json, ct.abilities_json "
        "FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' "
        "AND ct.card_type LIKE '%QuickAction%' ORDER BY gc.position",
        (session_id, user_id)).fetchall()


def db_hand_cards_raw(session_id, user_id):
    """Return list of (card_uid, card_template_id, template_guid)
    for cards in hand, ordered by position."""
    return _db.execute(
        "SELECT card_uid, card_template_id, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='hand' ORDER BY position",
        (session_id, user_id)).fetchall()


def db_hand_cards_full(session_id, user_id):
    """Return list of (card_uid, card_type, template_guid) for hand cards
    with db-backed card_type (game_cards + card_templates join)."""
    return _db.execute(
        "SELECT gc.card_uid, gc.card_type, gc.template_guid FROM game_cards gc "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' ORDER BY gc.position",
        (session_id, user_id)).fetchall()


def db_warzone_card_uids(session_id, user_id=None):
    """Return list of card_uid values in a player's warzone."""
    sql = ("SELECT card_uid FROM game_cards "
           "WHERE session_id=? AND location='warzone'")
    params = [session_id]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    rows = _db.execute(sql, params).fetchall()
    return [r[0] for r in rows]


def db_warzone_troops_basic(session_id, user_id=None):
    """Return list of (card_uid, card_type, card_state, combined_attributes)
    for warzone troops (template attributes OR instance-granted attributes)."""
    sql = ("SELECT gc.card_uid, gc.card_type, gc.card_state, "
           "(ct.attributes | gc.card_attributes) "
           "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
           "WHERE gc.session_id=? AND gc.location='warzone' "
           "AND gc.card_type LIKE '%Troop%'")
    params = [session_id]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    return _db.execute(sql, params).fetchall()


def db_warzone_blockers(session_id, user_id):
    """Return list of (card_uid, combined_attributes) for untapped warzone troops
    that could block (not Tapped)."""
    import game_engine
    return _db.execute(
        "SELECT card_uid, (ct.attributes | gc.card_attributes) FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND user_id=? AND location='warzone' "
        "AND gc.card_type LIKE '%Troop%' AND (gc.card_state & ?) = 0",
        (session_id, user_id, game_engine.ECardStates.Tapped)).fetchall()


def db_card_with_template(session_id, card_uid):
    """Return (template_guid, card_type, card_state, user_id, card_attributes,
    card_template_id, card_abilities, card_attack_mod, card_defense_mod,
    card_damage, original_template_guid) for a game card joined with its template,
    or None."""
    row = _db.execute(
        "SELECT gc.template_guid, ct.card_type, gc.card_state, gc.user_id, "
        "gc.card_attributes, gc.card_template_id, gc.card_abilities, "
        "gc.card_attack_mod, gc.card_defense_mod, gc.card_damage, "
        "gc.original_template_guid "
        "FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row


def db_card_instance_full(session_id, card_uid):
    """Return (card_abilities, card_attack_mod, card_defense_mod, card_damage,
    original_template_guid, permanent_buffs, temporary_buffs, card_cost_mod,
    cost_mod_json, card_attributes, temporary_attributes) for a game card
    instance, or None."""
    row = _db.execute(
        "SELECT card_abilities, card_attack_mod, card_defense_mod, card_damage, "
        "original_template_guid, permanent_buffs, temporary_buffs, card_cost_mod, "
        "cost_mod_json, card_attributes, temporary_attributes FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row


def db_card_owner(session_id, card_uid):
    """Return (id, owner_user_id, template_guid) for a card, or None."""
    row = _db.execute(
        "SELECT id, owner_user_id, template_guid FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row


def db_card_basic(session_id, card_uid):
    """Return (template_guid, user_id) for a game card."""
    return _db.execute(
        "SELECT template_guid, user_id FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_state_raw(session_id, card_uid):
    """Return the current card_state integer for a card, or 0."""
    row = _db.execute(
        "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row[0] if row and row[0] else 0


def db_card_template_thresholds(template_guid):
    """Return (threshold_json, abilities_json, attributes) for a card template, or None."""
    row = _db.execute(
        "SELECT threshold_json, abilities_json, attributes FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row


def db_card_info_joined(session_id, card_uid):
    """Return (template_guid, card_type, name, cost, attack, defense) for a card
    joined with its template, or None. Resolves instances through card_instances."""
    row = _db.execute(
        "SELECT ci.template_guid, ct.card_type, ct.name, ct.cost, ct.attack, ct.defense "
        "FROM game_cards gc "
        "LEFT JOIN card_instances ci ON ci.instance_id = gc.card_template_id "
        "LEFT JOIN card_templates ct ON ct.guid = ci.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row


def db_card_template_field(template_guid, field):
    """Return a single column value from card_templates by guid, or None."""
    valid = {"abilities_json", "attributes", "card_type", "sacrifice_target",
             "cost", "attack", "defense", "name", "threshold_json"}
    if field not in valid:
        return None
    row = _db.execute(
        f"SELECT {field} FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else None


def db_card_template_attrs_joined(session_id, card_uid):
    """Return (template_guid, attributes_from_ct, card_attributes_from_gc)
    for a game card joined with its template."""
    row = _db.execute(
        "SELECT gc.template_guid, ct.attributes, gc.card_attributes FROM game_cards gc "
        "LEFT JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row


# === Battle mutation helpers (read/write) ====================================

def db_set_card_location(session_id, card_uid, location, extra_set=None, extra_params=None):
    """Move a card to a new zone, with optional extra SET clauses."""
    sql = f"UPDATE game_cards SET location=?"
    params = [location]
    if extra_set:
        sql += f", {extra_set}"
    params.extend(extra_params or [])
    params.extend([session_id, int(card_uid)])
    _db.execute(f"{sql} WHERE session_id=? AND card_uid=?", params)
    _db.commit()


def db_discard_card(session_id, card_uid, owner_user_id=None,
                    extra_set=None, extra_params=None, connection=None):
    """Move one card to its owner's discard pile at the next position.

    ``position`` is an append order within one session/player discard pile.
    The optional ``extra_set``/``extra_params`` are for discard paths that
    also clear combat state or reset damage.  The SQL fragments are supplied
    only by trusted server code, never by a client request.

    Return the user id that owns the resulting discard pile, or ``None`` if
    the card was not found.
    """
    connection = connection or _db
    row = connection.execute(
        "SELECT user_id FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row:
        return None
    owner = int(row[0] if owner_user_id is None else owner_user_id)
    position = connection.execute(
        "SELECT COALESCE(MAX(position) + 1, 1) "
        "FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='discard'",
        (session_id, owner)).fetchone()[0]

    sql = "UPDATE game_cards SET user_id=?, location='discard', position=?"
    params = [owner, int(position or 1)]
    if extra_set:
        sql += ", " + extra_set
        params.extend(extra_params or [])
    sql += " WHERE session_id=? AND card_uid=?"
    params.extend([session_id, int(card_uid)])
    connection.execute(sql, params)
    connection.commit()
    return owner


def db_set_card_owner_and_discard(session_id, card_uid, owner_user_id):
    """Discard a card to its owner's graveyard (restore user_id + location=discard)."""
    return db_discard_card(session_id, card_uid, owner_user_id=owner_user_id)


def db_set_card_state_or(session_id, card_uid, state_bits):
    """OR in state_bits to a card's card_state. Persisted and committed."""
    _db.execute(
        "UPDATE game_cards SET card_state = (card_state | ?) "
        "WHERE session_id=? AND card_uid=?",
        (state_bits, session_id, int(card_uid)))
    _db.commit()


def db_set_card_state_replace(session_id, card_uid, clear_bits, set_bits):
    """Clear mask bits then set new bits on a card's state. Persisted and committed."""
    _db.execute(
        "UPDATE game_cards SET card_state = (card_state & ~?) | ? "
        "WHERE session_id=? AND card_uid=?",
        (clear_bits, set_bits, session_id, int(card_uid)))
    _db.commit()


def db_clear_combat_states(session_id, card_uid):
    """Clear Attacking, HasBlocked states from a warzone troop (Prep reset)."""
    import game_engine
    _db.execute(
        "UPDATE game_cards SET card_state = (card_state & ~?) "
        "WHERE session_id=? AND card_uid=?",
        (game_engine.ECardStates.Attacking | game_engine.ECardStates.HasBlocked,
         session_id, int(card_uid)))
    _db.commit()


def db_set_card_played_to_zone(session_id, card_uid, location):
    """Update a card's location and set position to sentinel value."""
    _db.execute(
        "UPDATE game_cards SET location=?, position=9999 "
        "WHERE session_id=? AND card_uid=?",
        (location, session_id, int(card_uid)))
    _db.commit()


def db_card_set_warzone_arrival(session_id, card_uid):
    """Clear StartedATurnOnYourSide and set CameOutThisTurn — troop just resolved."""
    import game_engine
    _db.execute(
        "UPDATE game_cards SET location='warzone', "
        "card_state = (card_state & ~?) | ? "
        "WHERE session_id=? AND card_uid=?",
        (game_engine.ECardStates.StartedATurnOnYourSide,
         game_engine.ECardStates.CameOutThisTurn,
         session_id, int(card_uid)))
    _db.commit()


def db_card_discard_spell(session_id, card_uid):
    """Discard a spell (BasicAction/QuickAction) after resolution."""
    return db_discard_card(session_id, card_uid)


def db_card_set_sacrifice_state(session_id, card_uid):
    """Clear Attacking, HasAttacked, Tapped, StartedATurnOnYourSide on a
    sacrificed troop and move to discard."""
    import game_engine
    return db_discard_card(
        session_id, card_uid,
        extra_set="card_state = (card_state & ~?)",
        extra_params=(game_engine.ECardStates.Attacking |
                      game_engine.ECardStates.HasAttacked |
                      game_engine.ECardStates.Tapped |
                      game_engine.ECardStates.StartedATurnOnYourSide,))


def db_card_save_player_stops(user_id, self_stops_json, opp_stops_json):
    """Persist a player's phase-stop preferences."""
    _db.execute(
        "INSERT INTO user_prefs (user_id, self_stops, opp_stops) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "self_stops=excluded.self_stops, opp_stops=excluded.opp_stops",
        (user_id, self_stops_json, opp_stops_json))
    _db.commit()


def db_card_load_player_stops(user_id):
    """Return (self_stops_json, opp_stops_json) or (None, None)."""
    row = _db.execute(
        "SELECT self_stops, opp_stops FROM user_prefs WHERE user_id=?",
        (user_id,)).fetchone()
    if not row or not row[0]:
        return None, None
    return row[0], row[1] if row[1] else None


def db_card_revert_to_template(session_id, card_uid, attributes, abilities_json,
                               template_guid, card_type):
    """Reversion: reset a card instance to its original template data."""
    _db.execute(
        "UPDATE game_cards SET card_attributes=?, card_abilities=?, "
        "card_template_id=?, template_guid=?, card_type=?, "
        "card_attack_mod=0, card_defense_mod=0, card_uses='{}', "
        "original_template_guid=?, position=100 "
        "WHERE session_id=? AND card_uid=?",
        (attributes, abilities_json, template_guid, template_guid, card_type,
         template_guid, session_id, int(card_uid)))
    _db.commit()


def db_card_sync_abilities(session_id, card_uid, abilities_json, attributes,
                           template_guid, commit=True):
    """Populate a card instance's ability/attribute/uses from its template."""
    _db.execute(
        "UPDATE game_cards SET card_abilities=?, card_attributes=?, card_uses='{}', "
        "original_template_guid = CASE WHEN original_template_guid IS NULL "
        "OR original_template_guid='' THEN ? ELSE original_template_guid END "
        "WHERE session_id=? AND card_uid=?",
        (abilities_json, attributes, template_guid, session_id, int(card_uid)))
    if commit:
        _db.commit()


def db_bulk_blocker_state(session_id, blocker_pairs):
    """Blocking state for a list of (card_uid) blockers. Uses executemany."""
    if blocker_pairs:
        import game_engine
        _db.executemany(
            "UPDATE game_cards SET card_state = (card_state | ?) "
            "WHERE session_id=? AND card_uid=?",
            [(game_engine.ECardStates.Blocking, session_id, u)
             for u in blocker_pairs])
        _db.commit()


def db_card_set_attacking_state(session_id, card_uid, state_bits):
    """Set attacking/tapped state on a warzone troop."""
    _db.execute(
        "UPDATE game_cards SET card_state = (card_state | ?) "
        "WHERE session_id=? AND card_uid=?",
        (state_bits, session_id, int(card_uid)))
    _db.commit()


# === Service query helpers ===================================================

def db_get_unread_mail_count(user_id):
    """Return count of unread emails for a user."""
    row = _db.execute(
        "SELECT COUNT(*) FROM emails WHERE user_id=? AND read_at IS NULL",
        (user_id,)).fetchone()
    return row[0] if row else 0


def db_get_mail_list(user_id):
    """Return list of (id, sender, subject, body, sent_at, gold_delivered,
    platinum_delivered, claimed_at) for a user's emails, newest first."""
    return _db.execute(
        "SELECT id, sender, subject, body, sent_at, gold_delivered, "
        "platinum_delivered, claimed_at FROM emails WHERE user_id=? "
        "ORDER BY id DESC",
        (user_id,)).fetchall()


def db_get_sent_mail_list(sender):
    """Return mail sent by *sender*, with recipient names, newest first."""
    return _db.execute(
        "SELECT e.id, e.sender, u.name, e.subject, e.body, e.sent_at, "
        "e.gold_delivered, e.platinum_delivered, e.claimed_at "
        "FROM emails e LEFT JOIN users u ON u.id=e.user_id "
        "WHERE LOWER(e.sender)=LOWER(?) ORDER BY e.id DESC",
        (sender,)).fetchall()


def db_delete_sent_mail(sender, email_ids):
    """Delete selected mail rows owned by *sender* and return the count."""
    ids = sorted({int(email_id) for email_id in email_ids if int(email_id) > 0})
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = _db.execute(
        f"DELETE FROM emails WHERE LOWER(sender)=LOWER(?) "
        f"AND id IN ({placeholders})",
        [sender, *ids])
    _db.commit()
    return cursor.rowcount


def db_mark_all_mail_read(user_id):
    """Mark all unread emails as read."""
    _db.execute(
        "UPDATE emails SET read_at=datetime('now') WHERE user_id=? AND read_at IS NULL",
        (user_id,))
    _db.commit()


def db_delete_all_mail(user_id):
    """Delete all emails for a user."""
    _db.execute("DELETE FROM emails WHERE user_id=?", (user_id,))
    _db.commit()


def db_get_mail_by_id(eid, user_id):
    """Return (id, gold_delivered, platinum_delivered, claimed_at) for a mail."""
    return _db.execute(
        "SELECT id, gold_delivered, platinum_delivered, claimed_at "
        "FROM emails WHERE id=? AND user_id=?",
        (eid, user_id)).fetchone()


def db_claim_mail(eid):
    """Mark a mail as claimed."""
    _db.execute("UPDATE emails SET claimed_at=datetime('now') WHERE id=?", (eid,))
    _db.commit()


def db_get_chest_by_id(chest_db_id, user_id):
    """Return chest details, including its inventory template GUID."""
    return _db.execute(
        "SELECT id, set_guid, chest_rarity, opened, template_guid FROM treasure_chests "
        "WHERE id=? AND user_id=? AND opened=0",
        (chest_db_id, user_id)).fetchone()


def db_next_card_instance_id():
    """Return the next free card_instances.instance_id."""
    row = _db.execute(
        "SELECT COALESCE(MAX(instance_id), 5000) FROM card_instances").fetchone()
    return row[0] + 1 if row else 5001


def db_create_card_instance(user_id, instance_id, template_guid):
    """Insert a card_instances row."""
    _db.execute(
        "INSERT OR IGNORE INTO card_instances (user_id, instance_id, template_guid) "
        "VALUES (?,?,?)", (user_id, instance_id, template_guid))
    _db.commit()


def db_open_chest(chest_db_id):
    """Mark a treasure chest as opened."""
    _db.execute("UPDATE treasure_chests SET opened=1 WHERE id=?", (chest_db_id,))
    _db.commit()


def db_get_unopened_chests(user_id):
    """Return (id, template_guid) for unopened chests owned by user_id."""
    return _db.execute(
        "SELECT id, template_guid FROM treasure_chests WHERE user_id=? AND opened=0",
        (user_id,)).fetchall()


def db_get_user_champions(user_id):
    """Return list of champion rows for a user, ordered by id."""
    return _db.execute(
        "SELECT id, champion_name, race, champion_class, gender, level, xp, "
        "last_deck_id, last_campaign_id, talents, pet_name FROM champions "
        "WHERE user_id=? AND is_deleted=0", (user_id,)).fetchall()


def db_get_champion_deck_match(user_id):
    """Return list of (id, champion_name) for all active champions of user_id.
    The caller filters by deck_name prefix downstream."""
    return _db.execute(
        "SELECT id, champion_name FROM champions WHERE user_id=? AND is_deleted=0",
        (user_id,)).fetchall()


def db_set_inventory_client_uid(user_id, template_guid, item_id):
    """Assign a client_item_uid to an inventory row that doesn't have one yet."""
    _db.execute(
        "UPDATE player_inventory SET client_item_uid=? "
        "WHERE user_id=? AND template_guid=? AND client_item_uid=0",
        (item_id, user_id, template_guid))
    _db.commit()


# Champion template lookup — tries extended table first, then standard.
def db_is_champion_template(template_guid):
    """True if the GUID exists in champion_templates or champion_templates_extended."""
    row = _db.execute(
        "SELECT guid FROM champion_templates_extended WHERE guid=? "
        "UNION ALL SELECT guid FROM champion_templates WHERE guid=? LIMIT 1",
        (template_guid, template_guid)).fetchone()
    return row is not None


# Card instance resolve (for player collection cards): template_guid + card_type.
def db_instance_resolve(instance_id):
    """Return (template_guid, card_type, name, cost, attack, defense) for a
    card_instances row by instance_id, or None."""
    row = _db.execute(
        "SELECT ci.template_guid, ct.card_type, ct.name, ct.cost, ct.attack, "
        "ct.defense FROM card_instances ci JOIN card_templates ct "
        "ON ci.template_guid=ct.guid WHERE ci.instance_id=?",
        (instance_id,)).fetchone()
    return row


def db_power_shift_triggers(ability_guids):
    """Return list of (ability_guid, game_text) for PowerShifted trigger abilities."""
    if not ability_guids:
        return []
    ph = ",".join("?" * len(ability_guids))
    rows = _db.execute(
        f"SELECT ability_guid, game_text FROM card_abilities_meta "
        f"WHERE trigger_event_type=? AND ability_guid IN ({ph})",
        ("Game.Shared.Mechanics.PowerShiftedEvent",) + tuple(ability_guids)).fetchall()
    return rows


def db_card_original_template(session_id, card_uid):
    """Return the original_template_guid for a card instance, or None."""
    row = _db.execute(
        "SELECT original_template_guid FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return (row[0] or None) if row else None


# Ability-grant lookup: for a troop ability activation, find the source card
# whose card_abilities JSON contains the given ability_guid.
def db_warzone_card_with_ability(session_id, user_id, ability_guid):
    """Return (card_uid, card_uses) for a warzone card of user_id that has
    ability_guid in its card_abilities JSON, or None."""
    row = _db.execute(
        "SELECT card_uid, card_uses FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='warzone' "
        "AND card_abilities LIKE ?",
        (session_id, user_id, f'%"{ability_guid}"%')).fetchone()
    return row


def db_is_champion_ability(ability_guid):
    """True if the ability is a champion talent ability (in talent_abilities)."""
    row = _db.execute(
        "SELECT 1 FROM talent_abilities WHERE ability_guid=? LIMIT 1",
        (ability_guid,)).fetchone()
    return row is not None


# --- card_template full read for card_instances resolution ---
def db_card_template_full(template_guid):
    """Return (card_type, cost, attack, defense, threshold_json, abilities_json,
    attributes) for a template GUID, or None."""
    return _db.execute(
        "SELECT card_type, cost, attack, defense, threshold_json, "
        "abilities_json, attributes FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()


log(f"DB initialized at {DB_PATH}")
