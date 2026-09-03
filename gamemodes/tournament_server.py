"""Tournament server — manages tournament lifecycle and pool refilling.

Three tournament types are seeded:
  1. 1v1 Constructed — 2 players, each brings a deck
  2. Limited Sealed — 1 player, 5 games, deck generated from set
  3. Set 1 Draft (AI)  — 1 player, 3 games, deck generated from set

The scheduler keeps a pool of waiting rooms for each type and refills
when rooms fill up.  All state lives in the tournaments DB tables.
"""

import json
import random
import threading
import time
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as _db

# How many waiting rooms of each type to keep available.
POOL_SIZES = {1: 2, 2: 2, 3: 2}  # type_id → target count
REFILL_INTERVAL = 5.0
STALE_CLEANUP_INTERVAL = 60.0
STALE_TOURNAMENT_AGE_DAYS = 1
SEALED_PACK_COUNT = 6   # 6 packs for sealed
DRAFT_PACK_COUNT = 3    # 3 packs for draft
CARDS_PER_PACK = 15     # standard 15-card packs


def seed_pool():
    """Ensure each tournament type has the target number of waiting rooms."""
    types = _db.db_tournament_types()
    for tt in types:
        tid = tt["id"]
        current = _db.db_tournament_count_active_by_type(tid)
        need = POOL_SIZES.get(tid, 1) - current
        for _ in range(need):
            inst_id = _db.db_tournament_next_id()
            _db.db_tournament_create(inst_id, tid)
    print(f"[tournament_server] Pool seeded ({len(types)} types)")


def refill_pool():
    """Check all types and create rooms where needed."""
    closed = _db.db_tournament_close_orphaned_started()
    if closed:
        print(f"[tournament_server] Closed {closed} orphaned started tournament(s)")
    types = _db.db_tournament_types()
    for tt in types:
        tid = tt["id"]
        current = _db.db_tournament_count_active_by_type(tid)
        need = POOL_SIZES.get(tid, 1) - current
        for _ in range(need):
            inst_id = _db.db_tournament_next_id()
            _db.db_tournament_create(inst_id, tid)
    # older IDE compat: placeholder

# ---------------------------------------------------------------------------
# Player actions
# ---------------------------------------------------------------------------

def join_tournament(tid, player_uid, player_name, deck_id=0, entry_group=0,
                    fee_paid=0):
    """Add a player to a waiting room and record the signup (with deck/fee).

    Returns (ok, count, target, type_id).
    """
    room = _db.db_tournament_by_id(tid)
    if not room:
        return False, 0, 0, 0
    if room["status"] != "waiting":
        return False, 0, 0, 0
    players = json.loads(room.get("players_json") or "{}")
    if str(player_uid) in players:
        # Already signed up (e.g. reconnect). Refresh deck/entry-group so the
        # signup stays current, and treat as a successful (idempotent) join.
        _db.db_tournament_signup_add(tid, player_uid, player_name,
                                     deck_id, entry_group, fee_paid)
        return True, len(players), room["max_players"], room["type_id"]
    _db.db_tournament_signup_add(tid, player_uid, player_name,
                                 deck_id, entry_group, fee_paid)
    players[str(player_uid)] = player_name
    _db.db_tournament_update_players(tid, json.dumps(players))
    return True, len(players), room["max_players"], room["type_id"]


def leave_all(player_uid):
    """Withdraw player from all waiting rooms.

    The signup row is kept (status='withdrew') so paid fees are retained.
    """
    for room in _db.db_tournament_list(status="waiting"):
        players = json.loads(room.get("players_json") or "{}")
        if str(player_uid) in players:
            _db.db_tournament_signup_set_status(room["id"], player_uid, "withdrew")
            del players[str(player_uid)]
            _db.db_tournament_update_players(room["id"], json.dumps(players))


def start_tournament(tid, session_id):
    """Mark a room as started."""
    _db.db_tournament_set_status(tid, "started", session_id)


def get_active_rooms():
    """Return all waiting rooms for broadcast to clients."""
    return _db.db_tournament_list(status="waiting")


# ---------------------------------------------------------------------------
# Deck generation for Limited formats
# ---------------------------------------------------------------------------

def _random_cards_from_set(set_id: str, count: int):
    """Pull *count* random card template GUIDs from the given set."""
    rows = _db._db.execute(
        "SELECT guid FROM card_templates WHERE set_id=? AND rarity "
        "IN ('Common','Uncommon','Rare','Legendary') AND is_pve=0 AND no_pvp=0",
        (set_id,)).fetchall()
    if not rows:
        return []
    guids = [r[0] for r in rows]
    return random.sample(guids, min(count, len(guids)))


def generate_sealed_deck(tournament_id, player_uid, set_id):
    """Build a sealed deck from 6 packs of the target set."""
    cards = []
    for _ in range(SEALED_PACK_COUNT):
        cards.extend(_random_cards_from_set(set_id, CARDS_PER_PACK))
    cards_json = json.dumps(cards)
    _db.db_tournament_deck_create(tournament_id, player_uid, cards_json)
    log_line = f"[deck gen] Sealed deck for player {player_uid} in tournament {tournament_id}: {len(cards)} cards"
    print(log_line)
    return cards_json


def generate_draft_deck(tournament_id, player_uid, set_id):
    """Build a draft deck from 3 packs."""
    cards = []
    for _ in range(DRAFT_PACK_COUNT):
        cards.extend(_random_cards_from_set(set_id, CARDS_PER_PACK))
    cards_json = json.dumps(cards)
    _db.db_tournament_deck_create(tournament_id, player_uid, cards_json)
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
    result = _db.db_tournament_cleanup_old(STALE_TOURNAMENT_AGE_DAYS)
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
    closed = _db.db_tournament_close_orphaned_started()
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
