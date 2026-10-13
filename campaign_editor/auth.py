"""Editor log-in: the game's own accounts (``users``) and passwords.

An account name and password are checked exactly as the game's non-Steam
log-in does (``proxy.py`` ``auth/hexlogin``: the id is a hash of the name,
with the old ``Name#1234`` id as a fallback; ``encryption.verify_password``).
An account with no password (Steam-only) cannot log in to the editor.

A successful log-in gets a random session token in an HttpOnly cookie;
sessions live in memory (a restart logs everyone out) behind one lock.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from typing import Any

import catalog

COOKIE = "hex_editor_session"
IDLE_SECONDS = 12 * 3600
_LOCK = threading.Lock()
_SESSIONS: dict[str, dict[str, Any]] = {}


def _player_id(identity: str) -> int:
    """``db.player_id_from_name``: a 63-bit hash of the full identity."""
    return int(hashlib.md5(identity.encode("utf-8")).hexdigest()[:16], 16) % (2 ** 63)


def _legacy_id(name: str) -> int:
    """Old ``Name#1234`` accounts (``proxy._legacy_nonsteam_uid``)."""
    disc = str(int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16) % 10000).zfill(4)
    return _player_id(f"{name}#{disc}")


def _is_admin(flags_json: str | None) -> bool:
    """Editor write access: a moderator (``mod``) or an admin. Everyone else
    gets the campaign read-only."""
    try:
        flags = json.loads(flags_json or "{}") or {}
    except (TypeError, ValueError):
        return False
    return flags.get("mod") == "true" or flags.get("admin") == "true"


def login(db: sqlite3.Connection, name: str, password: str) -> dict[str, Any] | None:
    """A new session for a valid account name and password, else None."""
    if str(catalog.REPO) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(catalog.REPO))
    import encryption
    name = str(name or "").strip()
    if not name or not password:
        return None
    for user_id in (_player_id(name), _legacy_id(name)):
        row = db.execute("SELECT id, name, password_hash, flags FROM users WHERE id=?",
                         (user_id,)).fetchone()
        if row:
            break
    else:
        row = db.execute("SELECT id, name, password_hash, flags FROM users WHERE name=?",
                         (name,)).fetchone()
    if not row or not row[2] or not encryption.verify_password(password, row[2]):
        return None
    token = secrets.token_urlsafe(32)
    session = {"token": token, "user_id": int(row[0]), "name": row[1],
               "admin": _is_admin(row[3]), "seen": time.time()}
    with _LOCK:
        _SESSIONS[token] = session
    return session


def session_for(headers) -> dict[str, Any] | None:
    """The live session named by the request's cookie, if any."""
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("Cookie") or "")
    except Exception:
        return None
    morsel = cookie.get(COOKIE)
    if not morsel:
        return None
    now = time.time()
    with _LOCK:
        session = _SESSIONS.get(morsel.value)
        if not session or now - session["seen"] > IDLE_SECONDS:
            _SESSIONS.pop(morsel.value, None)
            return None
        session["seen"] = now
        return dict(session)


def logout(token: str) -> None:
    with _LOCK:
        _SESSIONS.pop(token, None)


def set_cookie(token: str, *, clear: bool = False) -> str:
    """The Set-Cookie header value for a session (or its removal)."""
    value = "" if clear else token
    extra = "; Max-Age=0" if clear else ""
    return f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict{extra}"
