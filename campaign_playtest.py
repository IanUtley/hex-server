"""Campaign editor "Play": a temporary, no-progress battle for one scene.

The editor (a separate process) stores a request under a random token and
shows the player a chat command, ``!playtest <token>``. The player pastes it
into the game client's chat; the command runs on that player's own HConnect
connection (``commands.handle_command``), checks the token belongs to the same
account, and launches the scene the way a campaign battle starts
(``campaign._launch_encounter``).

The battle is a *playtest*: it is a ``camp_<id>`` session so every campaign
battle rule applies (scene AI deck/champion/personality, scene mods, the
scene's player champion/deck override, the campaign opening hand), but its
campaign id is above PLAYTEST_CAMPAIGN_BASE. No ``campaigns`` row has an id this large, so every
campaign read made for it finds nothing and every campaign write is a no-op;
the result path (``campaign.handle_battle_gameend``) hands it to
``finish_battle`` instead of the campaign/rules runtime. Rewards are shown
with the profile's display-only reward popup and are never granted; the game
session and its cards are deleted when the battle ends (the stale-session
sweep removes an abandoned one).

The store lives in ``HEX_PLAYTEST_DIR`` (default ``<tmp>/hex_playtest``):

* ``requests/<token>.json`` - written by the editor, used once by the
  command, expired after ``REQUEST_TTL_SECONDS``;
* ``active/<campaign id>.json`` - the running battle, read by battle setup;
* ``done/<user id>.json`` - the last result, read back by the editor.

Every write is atomic (temporary file + rename). A token is taken by
renaming its file away under a lock, so only one command can use it.

Only the standard library is imported at module level: the editor imports
this module and must not import the server's database modules.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

REQUEST_TTL_SECONDS = 10 * 60   # an unused Play token expires
COMMAND = "!playtest"
ACTIVE_TTL_SECONDS = 6 * 3600   # an abandoned battle's request is forgotten

_LOCK = threading.Lock()
_TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
_last_id = [0]


def spool_dir() -> Path:
    """The spool root shared by the editor and HConnect."""
    return Path(os.environ.get("HEX_PLAYTEST_DIR") or
                os.path.join(tempfile.gettempdir(), "hex_playtest"))


def _path(kind: str, name) -> Path:
    return spool_dir() / kind / f"{name}.json"


def _write(path: Path, value: dict) -> None:
    """Atomically write ``value`` as JSON (temporary file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# Playtest campaign ids sit above 2**62: the client reads campaign ids as
# UInt64 (a negative id fails its CampRequest decode), and real campaign ids
# are small.
PLAYTEST_CAMPAIGN_BASE = 1 << 62


def is_playtest_campaign(camp_id) -> bool:
    """Whether a campaign id names a playtest battle."""
    try:
        return int(camp_id) >= PLAYTEST_CAMPAIGN_BASE
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- editor side

def _expired(request: dict, now: float | None = None) -> bool:
    created = float(request.get("created_at", 0) or 0)
    return (now or time.time()) - created > REQUEST_TTL_SECONDS


def _sweep_requests() -> None:
    """Delete expired request files (and stray temporaries)."""
    now = time.time()
    for path in (spool_dir() / "requests").glob("*.json"):
        try:
            if now - path.stat().st_mtime > REQUEST_TTL_SECONDS:
                path.unlink()
        except OSError:
            pass


def create_request(user_id: int, request: dict) -> dict:
    """Store a Play request for ``user_id`` under a new random token.

    Returns ``{"token", "command", "expires_at"}``; the player pastes the
    command into the game client's chat."""
    token = secrets.token_hex(8)
    now = time.time()
    record = dict(request, user_id=int(user_id), token=token, created_at=now)
    with _LOCK:
        _sweep_requests()
        _write(_path("requests", token), record)
        try:
            _path("done", int(user_id)).unlink()
        except OSError:
            pass
    return {"token": token, "command": f"{COMMAND} {token}",
            "expires_at": now + REQUEST_TTL_SECONDS}


def status(user_id: int) -> dict:
    """``{"state": "waiting"|"playing"|"finished"|"none", ...}`` for a user."""
    user_id = int(user_id)
    for path in sorted((spool_dir() / "active").glob("*.json")):
        active = _read(path)
        if active and int(active.get("user_id", 0)) == user_id:
            return {"state": "playing", "scene": active.get("scene"),
                    "battle": active.get("battle")}
    done = _read(_path("done", user_id))
    if done:
        return dict(done, state="finished")
    now = time.time()
    for path in (spool_dir() / "requests").glob("*.json"):
        request = _read(path)
        if (request and int(request.get("user_id", 0)) == user_id
                and not _expired(request, now)):
            return {"state": "waiting", "scene": request.get("scene"),
                    "token": request.get("token")}
    return {"state": "none"}


# ---------------------------------------------------------------- server side

def _new_campaign_id() -> int:
    """A unique playtest campaign id (microsecond clock, kept monotonic)."""
    with _LOCK:
        value = max(time.time_ns() // 1000, _last_id[0] + 1)
        _last_id[0] = value
    return PLAYTEST_CAMPAIGN_BASE + value


def take_request(token: str, user_id: int) -> tuple[dict | None, str]:
    """Use a Play token once: ``(request, "")`` or ``(None, reason)``.

    Refused when the token is malformed, unknown, already used, expired, or
    belongs to another account (a foreign token is left for its owner)."""
    token = str(token or "").strip().lower()
    if not _TOKEN_RE.match(token):
        return None, "That is not a playtest token."
    source = _path("requests", token)
    with _LOCK:
        request = _read(source)
        if request is None:
            return None, "Unknown or already used playtest token."
        if _expired(request):
            try:
                source.unlink()
            except OSError:
                pass
            return None, "That playtest token has expired; press Play in the editor again."
        if int(request.get("user_id") or 0) != int(user_id):
            return None, ("That playtest token belongs to a different account; "
                          "log in to the editor with this game account.")
        claimed = source.with_name(
            f".used-{token}-{os.getpid()}-{threading.get_ident()}.json")
        try:
            os.replace(source, claimed)     # atomic: one use only
        except OSError:
            return None, "Unknown or already used playtest token."
        try:
            claimed.unlink()
        except OSError:
            pass
    return request, ""


def active(camp_id) -> dict | None:
    """The claimed request of a running playtest battle."""
    if not is_playtest_campaign(camp_id):
        return None
    return _read(_path("active", int(camp_id)))


def _sweep_active() -> None:
    """Forget requests of battles abandoned long ago."""
    now = time.time()
    for path in (spool_dir() / "active").glob("*.json"):
        try:
            if now - path.stat().st_mtime > ACTIVE_TTL_SECONDS:
                path.unlink()
        except OSError:
            pass


def build_battle(db, request: dict, user_id: int) -> dict:
    """Resolve what the Play battle uses: the scene's player champion/deck
    (blank: the account's newest champion and its deck), the scene's
    gameboard, AI champion, AI deck and AI personality.

    Read-only; the campaign battle setup applies the scene overrides itself
    (``campaign.resolve_battle_config``)."""
    import pve_db
    from profile_db import db_latest_champion_for_user
    scene = str(request.get("scene") or "").lower()
    row = pve_db.db_encounter_scene_runtime(scene, conn=db)
    if not row:
        raise LookupError(f"unknown encounter scene {scene}")
    if not row[4]:
        raise ValueError(f"scene {row[1]} has no AI deck (not a battle)")
    champ = db_latest_champion_for_user(user_id, conn=db)
    champion_id = int(champ[0]) if champ else None
    deck_id = int(champ[1]) if champ and champ[1] else None
    if not deck_id:
        deck_id = pve_db.db_first_deck_id_for_user(int(user_id), conn=db)
    player_champion, player_deck = pve_db.db_encounter_scene_player_override(scene, conn=db)
    return {
        "scene": scene, "scene_name": row[1], "title": row[2],
        "gameboard": row[3], "ai_deck_guid": row[4],
        "ai_champion_guid": row[5], "ai_deck_personality": row[6],
        "champion_id": champion_id, "deck_id": deck_id,
        "player_champion_guid": player_champion,
        "player_deck_guid": player_deck,
    }


def start_battle(handler, db, request: dict, user_id: int,
                 service_campaign_uid_type, service_mail_uid) -> dict:
    """Launch a taken Play request on ``handler`` (the player's connection).
    Raises LookupError/ValueError when the scene is not a battle."""
    battle = build_battle(db, request, int(user_id))
    _sweep_active()
    camp_id = _new_campaign_id()
    record = dict(request, battle=battle, camp_id=camp_id, started_at=time.time())
    _write(_path("active", camp_id), record)
    deck_id = battle.get("deck_id") or 0
    deck_uid64 = (int(deck_id) << 8) | 17 if deck_id else 0
    import campaign
    getattr(handler, "_log_req", print)(
        f"    Playtest: user={user_id} scene={battle['scene_name']} camp={camp_id} "
        f"board={battle['gameboard']} ai_deck={battle['ai_deck_guid']}")
    try:
        campaign._launch_encounter(
            handler, db, camp_id, battle.get("champion_id") or 0, battle["scene"],
            deck_uid64, 0, "00000000-0000-0000-0000-000000000000",
            "ServiceCampaign", str(service_campaign_uid_type), 0, service_mail_uid)
    except BaseException:
        try:
            _path("active", camp_id).unlink()
        except OSError:
            pass
        raise
    return battle


def run_command(handler, db, args, service_campaign_uid_type,
                service_mail_uid) -> str:
    """``!playtest <token>`` on the sender's own connection: check the token
    and start its battle. Returns the chat reply."""
    profile = getattr(handler, "user_profile", None) or {}
    user_id = int(profile.get("id") or 0)
    if not user_id or not getattr(handler, "client_reck_id", None):
        return "Playtest: you are not logged in."
    if not args:
        return f"Usage: {COMMAND} <token>  (press Play in the campaign editor)"
    request, reason = take_request(args[0], user_id)
    if request is None:
        return f"Playtest: {reason}"
    try:
        battle = start_battle(handler, db, request, user_id,
                              service_campaign_uid_type, service_mail_uid)
    except (LookupError, ValueError) as exc:
        _write(_path("done", user_id), {"error": str(exc), "scene": request.get("scene")})
        return f"Playtest: {exc}"
    return (f"Playtest: starting {battle.get('title') or battle['scene_name']} "
            "(nothing is saved; rewards are shown, not granted).")


def finish_battle(handler, db, session, camp_id, won) -> bool:
    """End a playtest battle: show (never grant) the scene's rewards, record
    the result for the editor, and delete the temporary game session."""
    from pvp_db import db_delete_game_session
    import campaign
    log = getattr(handler, "_log_req", print)
    with _LOCK:
        path = _path("active", int(camp_id))
        request = _read(path) or {}
        try:
            path.unlink()
        except OSError:
            pass
    battle = request.get("battle") or {}
    user_id = int(request.get("user_id") or
                  (getattr(handler, "user_profile", None) or {}).get("id") or 0)
    preview = {"display": [], "lines": []}
    if won and battle.get("scene"):
        try:
            preview = campaign.preview_encounter_rewards(
                db, session, battle["scene"], user_id, request)
        except Exception as exc:      # a preview must never block the cleanup
            log(f"    Playtest reward preview failed: {exc}")
    if preview["display"] and hasattr(handler, "push_display_rewards"):
        handler.push_display_rewards(preview["display"])
    db_delete_game_session(session.session_id, conn=db)
    db.commit()
    if user_id:
        _write(_path("done", user_id), {
            "scene": battle.get("scene"), "title": battle.get("title"),
            "won": bool(won), "rewards": preview["lines"],
            "finished_at": time.time()})
    log(f"    Playtest {camp_id} finished (won={won}); rewards shown, "
        f"not granted: {preview['lines']}")
    return True
