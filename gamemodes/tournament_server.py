"""Tournament server — manages tournament lifecycle and pool refilling.

Two tournament types are offered:
  1. 1v1 Immortal — a two-player waiting-room match
  2. Corinth Merry Melee — a persistent one-player-entry async event whose
     individual matches are paired separately

The scheduler keeps a pool of waiting rooms for each type and refills
when rooms fill up.  All state lives in the tournaments DB tables.
"""

import json
import threading
import time
import calendar
from datetime import datetime, timezone
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tournament_db as _tournament_db
from tournament_db import db_random_card_guids_for_set

# Corinth is one persistent event, not a stream of generated rooms. Keep its
# instance ID stable so pools, run results, and re-entry share one event.
CORINTH_TYPE_ID = 4
CORINTH_TOURNAMENT_ID = 40004

# How many waiting rooms of each ordinary type to keep available.
POOL_SIZES = {1: 2}  # type_id → target count
REFILL_INTERVAL = 5.0
STALE_CLEANUP_INTERVAL = 60.0
# Event round clock: an incomplete tournament match is forfeited after one
# hour.  This is separate from the one-day retention threshold for deleting
# old session/database state below.
STALE_MATCH_AGE_SECONDS = 60 * 60
STALE_TOURNAMENT_AGE_DAYS = 1
SEALED_PACK_COUNT = 6   # 6 packs for sealed
DRAFT_PACK_COUNT = 3    # 3 packs for draft
CARDS_PER_PACK = 15     # standard 15-card packs


def _current_month_expiry():
    now = datetime.now(timezone.utc)
    last_day = calendar.monthrange(now.year, now.month)[1]
    return now.replace(day=last_day, hour=23, minute=59, second=59,
                      microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_corinth_event():
    _tournament_db.db_tournament_create(
        CORINTH_TOURNAMENT_ID, CORINTH_TYPE_ID)
    room = _tournament_db.db_tournament_by_id(CORINTH_TOURNAMENT_ID)
    if not room:
        return False
    expiry = room.get("expires_at")
    if not expiry:
        expiry = _current_month_expiry()
        _tournament_db.db_tournament_set_expiry(
            CORINTH_TOURNAMENT_ID, expiry)
    try:
        expired = datetime.strptime(str(expiry), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    except ValueError:
        expired = False
    if expired and str(room.get("status", "")).lower() != "closed":
        _tournament_db.db_tournament_expire(CORINTH_TOURNAMENT_ID)
        return False
    return str(room.get("status", "")).lower() != "closed"


def seed_pool():
    """Ensure ordinary rooms and the singleton Corinth event exist."""
    _ensure_corinth_event()
    _tournament_db.db_tournament_close_other_instances(
        CORINTH_TYPE_ID, CORINTH_TOURNAMENT_ID)
    types = _tournament_db.db_tournament_types()
    for tt in types:
        tid = tt["id"]
        if tid == CORINTH_TYPE_ID:
            continue
        current = _tournament_db.db_tournament_count_active_by_type(tid)
        need = POOL_SIZES.get(tid, 1) - current
        for _ in range(need):
            inst_id = _tournament_db.db_tournament_next_id()
            _tournament_db.db_tournament_create(inst_id, tid)
    print(f"[tournament_server] Pool seeded ({len(types)} types)")


def refill_pool():
    """Check all types and create rooms where needed."""
    _ensure_corinth_event()
    closed = _tournament_db.db_tournament_close_orphaned_started()
    if closed:
        print(f"[tournament_server] Closed {closed} orphaned started tournament(s)")
    types = _tournament_db.db_tournament_types()
    for tt in types:
        tid = tt["id"]
        if tid == CORINTH_TYPE_ID:
            continue
        current = _tournament_db.db_tournament_count_active_by_type(tid)
        need = POOL_SIZES.get(tid, 1) - current
        for _ in range(need):
            inst_id = _tournament_db.db_tournament_next_id()
            _tournament_db.db_tournament_create(inst_id, tid)
    # older IDE compat: placeholder

# ---------------------------------------------------------------------------
# Player actions
# ---------------------------------------------------------------------------

def join_tournament(tid, player_uid, player_name, deck_id=0, entry_group=0,
                    fee_paid=0):
    """Add a player to a waiting room and record the signup (with deck/fee).

    Returns (ok, count, target, type_id).
    """
    room = _tournament_db.db_tournament_by_id(tid)
    if not room:
        return False, 0, 0, 0
    players = json.loads(room.get("players_json") or "{}")
    async_event = str(room.get("style", "")).lower() in {
        "async", "asynchronous"}
    persistent_reentry = async_event and room["status"] == "started"
    if room["status"] != "waiting" and not persistent_reentry:
        return False, 0, 0, 0
    if str(player_uid) in players:
        # Already signed up (e.g. reconnect). Refresh deck/entry-group so the
        # signup stays current, and treat as a successful (idempotent) join.
        _tournament_db.db_tournament_signup_add(tid, player_uid, player_name,
                                                deck_id, entry_group, fee_paid)
        if async_event and not _tournament_db.db_tournament_player_run_live(
                tid, int(player_uid)):
            _tournament_db.db_tournament_signup_set_async_state(
                tid, player_uid, deck_ready=False, searching=False)
        return True, len(players), room["max_players"], room["type_id"]
    if persistent_reentry:
        # A started async tournament is a persistent event, while its two
        # player slots represent the current match.  Reuse a slot only after
        # its previous run has been retired; the old match rows remain for
        # the other player's score until both live flags are zero.
        replacement = next(
            (old_uid for old_uid in players
             if not _tournament_db.db_tournament_player_run_live(
                 tid, int(old_uid))), None)
        if replacement is None:
            return False, len(players), room["max_players"], room["type_id"]
        # Keep the historical signup/match rows for scoring, but do not leave
        # the replaced identity as an active entrant.  Active signups feed the
        # lobby and async matcher and would otherwise retain a stale player.
        _tournament_db.db_tournament_signup_set_status(
            tid, int(replacement), "withdrew")
        del players[replacement]
    _tournament_db.db_tournament_signup_add(tid, player_uid, player_name,
                                            deck_id, entry_group, fee_paid)
    if async_event:
        # A re-entry after a retired run keeps its historical signup row, but
        # starts a fresh deck/search lifecycle for the new run.
        _tournament_db.db_tournament_signup_set_async_state(
            tid, player_uid, deck_ready=False, searching=False)
    players[str(player_uid)] = player_name
    _tournament_db.db_tournament_update_players(tid, json.dumps(players))
    return True, len(players), room["max_players"], room["type_id"]


def leave_all(player_uid):
    """Withdraw player from all waiting rooms.

    The signup row is kept (status='withdrew') so paid fees are retained.
    """
    for room in _tournament_db.db_tournament_list(status="waiting"):
        # Async events are persistent registrations, not mutually exclusive
        # waiting rooms.  Entering another tournament must not remove every
        # existing Corinth signup (or the event would lose its matchmaking
        # queue whenever a second player entered).
        if str(room.get("style", "")).lower() in {"async", "asynchronous"}:
            continue
        players = json.loads(room.get("players_json") or "{}")
        if str(player_uid) in players:
            _tournament_db.db_tournament_signup_set_status(room["id"], player_uid, "withdrew")
            del players[str(player_uid)]
            _tournament_db.db_tournament_update_players(room["id"], json.dumps(players))


def start_tournament(tid, session_id):
    """Mark a room as started."""
    _tournament_db.db_tournament_set_status(tid, "started", session_id)


def get_active_rooms():
    """Return joinable rooms, including the persistent Corinth catalog entry."""
    rooms = _tournament_db.db_tournament_list(
        status="waiting", enabled_only=True)
    corinth = _tournament_db.db_tournament_by_id(CORINTH_TOURNAMENT_ID)
    if (corinth and str(corinth.get("status", "")).lower() != "closed"
            and not any(int(room["id"]) == CORINTH_TOURNAMENT_ID
                        for room in rooms)):
        rooms.append(corinth)
    return rooms


# ---------------------------------------------------------------------------
# Deck generation for Limited formats
# ---------------------------------------------------------------------------

def _random_cards_from_set(set_id: str, count: int):
    """Pull *count* random card template GUIDs from the given set."""
    return db_random_card_guids_for_set(set_id, count)


def generate_sealed_deck(tournament_id, player_uid, set_id):
    """Build a sealed deck from 6 packs of the target set."""
    cards = []
    for _ in range(SEALED_PACK_COUNT):
        cards.extend(_random_cards_from_set(set_id, CARDS_PER_PACK))
    cards_json = json.dumps(cards)
    _tournament_db.db_tournament_deck_create(tournament_id, player_uid, cards_json)
    log_line = f"[deck gen] Sealed deck for player {player_uid} in tournament {tournament_id}: {len(cards)} cards"
    print(log_line)
    return cards_json


def generate_draft_deck(tournament_id, player_uid, set_id):
    """Build a draft deck from 3 packs."""
    cards = []
    for _ in range(DRAFT_PACK_COUNT):
        cards.extend(_random_cards_from_set(set_id, CARDS_PER_PACK))
    cards_json = json.dumps(cards)
    _tournament_db.db_tournament_deck_create(tournament_id, player_uid, cards_json)
    log_line = f"[deck gen] Draft deck for player {player_uid} in tournament {tournament_id}: {len(cards)} cards"
    print(log_line)
    return cards_json


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

_scheduler_running = False
_last_stale_cleanup = 0.0


def _cleanup_old_state(force=False):
    """Close old tournament rows and remove their game state periodically."""
    global _last_stale_cleanup
    now = time.monotonic()
    if not force and now - _last_stale_cleanup < STALE_CLEANUP_INTERVAL:
        return
    # Resolve incomplete async matches while their persisted priority owner is
    # still available.  The generic DB cleanup below removes the old session.
    try:
        from gamemodes.tournament_engine import (
            recover_stale_tournament_matches,
            retire_completed_corinth_runs,
        )
        recovered = recover_stale_tournament_matches(STALE_MATCH_AGE_SECONDS)
        if recovered:
            print(f"[tournament_server] Recovered {recovered} stale match(es)")
        retired = retire_completed_corinth_runs()
        if retired:
            print(f"[tournament_server] Retired {retired} completed Corinth run(s)")
    except Exception as exc:
        print(f"[tournament_server] Stale match recovery error: {exc}")
    result = _tournament_db.db_tournament_cleanup_old(STALE_TOURNAMENT_AGE_DAYS)
    _last_stale_cleanup = now
    if any(result.values()):
        print(
            "[tournament_server] Old-state cleanup: "
            f"closed={result['tournaments_closed']} "
            f"sessions={result['game_sessions_removed']} "
            f"cards={result['game_cards_removed']}"
        )


def _scheduler_loop():
    while _scheduler_running:
        try:
            _cleanup_old_state()
            refill_pool()
        except Exception as e:
            print(f"[tournament_server] Scheduler error: {e}")
        time.sleep(REFILL_INTERVAL)


def start():
    global _scheduler_running
    _cleanup_old_state(force=True)
    cleared = _tournament_db.db_tournament_clear_orphaned_searches()
    if cleared:
        print(f"[tournament_server] Cleared {cleared} orphaned searches")
    closed = _tournament_db.db_tournament_close_orphaned_started()
    if closed:
        print(f"[tournament_server] Closed {closed} orphaned started tournament(s)")
    seed_pool()
    _scheduler_running = True
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    print("[tournament_server] Scheduler started")


def stop():
    global _scheduler_running
    _scheduler_running = False


if __name__ == "__main__":
    start()
    try:
        while _scheduler_running:
            time.sleep(1)
    except KeyboardInterrupt:
        stop()
