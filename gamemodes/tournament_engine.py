"""Tournament game engine — session creation, desc building, rdata pushes.

Extracted from hconnect_server.py.  The ``player_handlers`` dict and
related state live here so the matchmaking service can access them.
"""

import json, gzip, time, threading, struct
from datetime import datetime, timezone
from binascii import unhexlify

from db import _db, log_req  # _db retained for legacy fixture injection
from profile_db import db_get_deck_by_id, db_send_email
from tournament_db import (
    db_tournament_by_id, db_tournament_players_name_map,
    db_tournament_signups_by_tournament, db_tournament_completed_for_player,
    db_tournament_signup_by_player, db_tournament_matches,
    db_tournament_match_start, db_tournament_match_result,
    db_tournament_discard_match,
    db_tournament_set_status, db_tournament_room_for_game,
    db_seed_tournament_game_deck, db_insert_tournament_champion_card,
    db_tournament_pool_replace, db_tournament_pool_delete,
    db_seed_tournament_pool, db_tournament_pool,
    db_tournament_signup_set_async_state, db_tournament_async_ready_players,
    db_tournament_player_score, db_tournament_finalize_player_run)
from pvp_db import db_game_deck_cards, db_delete_game_session
from encoder import encode_objfmt_response, compress_gzip, encode_datawrapper, client_session_guid
import gamemodes.tournament_server as tournament_server
from domain.enums import ESessionFlags, ETournamentFormats
from domain.constants import (
    AUTHORITATIVE_SESSION_UID_TYPE, SERVICE_GAME_SESSION_UID_TYPE,
    SERVICE_PLAYER_UID_TYPE, TOURNAMENT_DECK_CONSTRUCTION_DATA_TYPE,
    TOURNAMENT_GAME_DATA_TYPE, TOURNAMENT_INFO_DATA_TYPE,
    TOURNAMENT_SESSION_START_DATA_TYPE,
)

CORINTH_MERRY_MELEE_MODE = "corinth_merry_melee"
CORINTH_CHAMPION_GUID = "93d8a5ca-d999-461d-84d8-30975ef4dfc1"
CORINTH_RUN_WINS = 5
CORINTH_RUN_LOSSES = 3
# Corinth starts with four of each basic shard in the main deck.  Additional
# cards enter the deck later through Corinth's charge power; they are not a
# sideboard/deck-construction pool.
CORINTH_SHARD_GUIDS = (
    "b253393b-fdde-47c4-9288-4b8efb0698b1",
    "1f897193-72a1-487e-a6bd-f3f6e7897c47",
    "cd41bd00-7585-4762-a721-6163bdaee3c3",
    "8554b2c8-cf48-467d-bf55-ab45e306ce43",
    "6865d8d5-bd2e-43c6-8a68-53d1bde6bc28",
)


def _rollback_shared_db_on_error(func):
    """Release the legacy shared tournament connection on failed requests.

    The normal commit remains at each operation's existing transaction
    boundary.  This guard covers the gap where a caller has written through
    ``_db`` and an exception occurs before that commit is reached.
    """
    def guarded(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except BaseException:
            try:
                _db.rollback()
            except Exception:
                pass
            raise
    guarded.__name__ = getattr(func, "__name__", "guarded")
    guarded.__doc__ = getattr(func, "__doc__", None)
    return guarded


def _is_corinth_room(room):
    try:
        return bool(int(room.get("format") or 0) &
                    int(ETournamentFormats.Iconoclast))
    except (AttributeError, TypeError, ValueError):
        return False


def _is_async_room(room):
    return str(room.get("style", "")).lower() in {"async", "asynchronous"}


def tournament_id_from_session_name(session_name):
    """Extract the event ID from ``tourney-<event>-<match>`` names."""
    try:
        parts = str(session_name or "").split("-")
        if len(parts) < 2 or parts[0] != "tourney":
            return 0
        return int(parts[1])
    except (TypeError, ValueError):
        return 0


def _retire_finished_async_runs(tid, player_uids):
    """Retire players who reached the Merry Melee run limit."""
    retire = []
    for uid in {int(value) for value in player_uids}:
        wins, losses = db_tournament_player_score(tid, uid)
        if wins >= CORINTH_RUN_WINS or losses >= CORINTH_RUN_LOSSES:
            result = db_tournament_finalize_player_run(tid, uid, "completed")
            if result:
                retire.append(uid)
                _email_tournament_rewards(db_tournament_by_id(tid), result)
    return retire


def retire_completed_corinth_runs():
    """Retire Merry Melee runs that reached 5 wins or 3 losses.

    This is deliberately safe to call from the scheduler every minute:
    ``db_tournament_finalize_player_run`` is idempotent once the live run has
    been removed, and reward mail is created only for the successful final
    run returned by that operation.
    """
    room = db_tournament_by_id(40004)
    if not room or not _is_corinth_room(room) or not _is_async_room(room):
        return 0
    candidates = db_tournament_signups_by_tournament(40004, status="active")
    checked = 0
    for signup in candidates:
        pid = int(signup["player_uid"])
        wins, losses = db_tournament_player_score(40004, pid)
        if wins < CORINTH_RUN_WINS and losses < CORINTH_RUN_LOSSES:
            continue
        # A client can reach the run limit while an abandoned setup/session
        # row is still present. Remove that unfinished assignment before
        # retiring the run, otherwise the remaining player is permanently
        # excluded from the async matcher by the live-match guard.
        for match in db_tournament_matches(40004):
            if (match.get("state") == "Complete" or pid not in (
                    int(match.get("player1_uid") or 0),
                    int(match.get("player2_uid") or 0))):
                continue
            db_tournament_discard_match(
                40004, match.get("session_id"), conn=_db)
            db_delete_game_session(match.get("session_id"), conn=_db)
            _db.commit()
            log_req(f"  Corinth run retirement removed unfinished match "
                    f"session={match.get('session_id')}")
        retired = _retire_finished_async_runs(40004, (pid,))
        if retired:
            checked += len(retired)
            log_req(f"  Corinth run retired by scheduler: pid={pid} "
                    f"score={wins}-{losses}")
    return checked

# ── shared tournament state ──────────────────────────────────────────
# Keep these objects alive across ``importlib.reload``.  The main server
# imports them directly, while the reload command re-imports the tournament
# modules; replacing the dictionaries splits the live handler registry and
# causes the initial game packet to work but later PvP pushes (mulligan,
# priority, etc.) to find no handlers.
player_handlers = globals().get("player_handlers", {})   # player_uid → HCPHandler
player_handler_lock = globals().get("player_handler_lock", threading.Lock())
player_decks = globals().get("player_decks", {})         # player_uid → deck_db_id

# ── helpers ──────────────────────────────────────────────────────────

def _encode_enter_tournament_error(comp, session_id, tournament_id, error_name):
    inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.EnterTournamentResponseArgs"],
        [("isWaitingRoom", "bool", True),
         ("TournamentID", "ulong", tournament_id),
         ("Error", "enum1",
          (f"Game.Shared.Network.Tournaments.EEnterTournamentError.{error_name}", 0))])
    body = compress_gzip(inner) if comp else inner
    return encode_datawrapper(0, TOURNAMENT_GAME_DATA_TYPE, body, comp, session_id)


def _make_deck_data(deck_id):
    """Return (did, dname, did_val, champ_did, card_guids) for deckbits encoding."""
    deck = db_get_deck_by_id(int(deck_id) if deck_id else 0)
    if not deck:
        return (f"d{deck_id}", "Unknown Deck", int(deck_id) if deck_id else 0, 0, [])
    return (f"d{deck_id}", deck.get("deck_name") or f"Deck #{deck_id}",
            int(deck_id) if deck_id else 0,
            int(deck.get("pve_champion_id") or 0), [])


def _tournament_format_bitmask(room):
    fmt_raw = str(room.get("format") or "").strip()
    try:
        return int(fmt_raw)
    except ValueError:
        return {"constructed": 0, "sealed": 1, "draft": 2}.get(
            fmt_raw.lower(), 0)


def _tournament_session_flags(room):
    """Return the PvP encounter flags for a tournament room.

    The client treats ``IsStandardPvP`` and ``IsImmortalPvP`` as mutually
    exclusive when selecting valid sets.  In particular, Session checks the
    Standard flag first, so sending both flags makes an Immortal game use the
    Standard card pool.  Preserve the existing DuelingPit flag and select
    exactly one format flag from the tournament format bitmask.
    """
    format_bits = _tournament_format_bitmask(room)
    flags = ESessionFlags.IsDuelingPit
    if format_bits & ETournamentFormats.Iconoclast:
        flags |= ESessionFlags.IsIconoclast
    elif format_bits & ETournamentFormats.Immortal:
        flags |= ESessionFlags.IsImmortalPvP
    else:
        flags |= ESessionFlags.IsStandardPvP
    return flags


def _tournament_style_bitmask(room):
    style_str = (room.get("style") or "sw").lower()
    return {"se": 0, "sw": 1, "async": 2, "asynchronous": 2}.get(
        style_str, 0)


def _tournament_rewards(room):
    """Load and validate the configured native tournament reward JSON."""
    if not room or not room.get("type_id"):
        return {"tournamentRewards": []}
    columns = {row[1] for row in _db.execute(
        "PRAGMA table_info(tournament_types)")}
    if "rewards_json" not in columns:
        return {"tournamentRewards": []}
    row = _db.execute(
        "SELECT rewards_json FROM tournament_types WHERE id=?",
        (int(room["type_id"]),),
    ).fetchone()
    try:
        value = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    rewards = value.get("tournamentRewards")
    return {"tournamentRewards": rewards if isinstance(rewards, list) else []}


def _email_tournament_rewards(room, result):
    """Create one claimable mail item for a reward-eligible run."""
    if not result or int(result.get("wins", 0)) < CORINTH_RUN_WINS:
        return
    attachments = []
    for group in _tournament_rewards(room).get("tournamentRewards", []):
        if (not group.get("basedOnPoints") or
                int(group.get("place", 0)) != int(result["wins"])):
            continue
        for reward in group.get("rewards", []):
            prize = reward.get("prizeResource")
            if isinstance(prize, dict):
                prize = prize.get("m_Guid")
            if int(reward.get("type", -1)) == 3 and prize:
                attachments.append({
                    "type": "CARD",
                    "template": str(prize),
                    "quantity": max(1, int(reward.get("quantity", 1) or 1)),
                })
    if attachments:
        db_send_email(
            int(result["player_uid"]),
            "Rewards for participating in tournament",
            "Thank you for participating in the tournament. Claim the attached "
            "rewards from this message.",
            sender="SYSTEM", attachments=attachments,
        )


_TOURNAMENT_OPPONENT_WIN_FLOOR = 1.0 / 3.0

# Values from Game.Shared.Tournaments.ETournamentPlayerEliminationReason.
# The client uses these fields when rendering an eliminated player in the
# standings; omitted fields default to TPE_NotEliminated and round zero.
_TPE_NOT_ELIMINATED = 0
_TPE_LOST_MATCH_SINGLE_ELIM = 3


def build_tournament_desc_json(room):
    players = db_tournament_players_name_map(room["id"])
    all_signups = db_tournament_signups_by_tournament(room["id"], status=None)
    matches = db_tournament_matches(room["id"])
    async_event = _is_async_room(room)
    # Async events are persistent catalog entries.  Their database
    # max_players=0 means "no concurrent room cap", not zero lobby seats.
    # The client uses maxPlayers for OPEN/FULL rendering, so expose the
    # single-player deck-building entry point here.
    max_p = 1 if async_event else room.get("max_players", 2)
    min_p = 1 if async_event else room.get("min_players", max_p)
    complete = _tournament_is_complete(room, matches)
    if complete:
        tournament_state = "Complete"
    elif async_event:
        # A persistent async event remains open while individual runs are
        # playing.  Match history belongs in the detail view, not in the
        # lobby's registration state.
        tournament_state = "WaitForStart"
    elif str(room.get("status", "")).lower() == "started" or matches:
        tournament_state = "PlayGames"
    else:
        tournament_state = "WaitForStart"
    start_time, end_time, open_time, current_round = _tournament_times(
        room, matches
    )
    return {
        # The client treats waitRoom as an active join queue before it checks
        # TournamentState.  Completed history must therefore be a normal
        # tournament descriptor or double-clicking it opens the entry/fee UI.
        "roomType": "waitRoom" if max_p > 1 and not complete else "",
        "id": room["id"],
        # Async events are singleton catalog entries.  The client uses this
        # field as a localization key/display name and does not append a
        # process suffix for asynchronous styles.
        "name": (room.get("type_name", "") if _is_async_room(room)
                 else f"{room.get('type_name', '')} #{room['id']}"),
        "numPlayers": 0 if async_event else (
            len(all_signups) if all_signups else len(players)),
        "maxPlayers": max_p,
        "minPlayers": min_p,
        "maxRounds": room.get("games_count", 1),
        "style": _tournament_style_bitmask(room),
        "endTime": end_time, "startTime": start_time, "openTime": open_time,
        "lastUpdate": end_time or start_time or open_time,
        "format": _tournament_format_bitmask(room),
        "state": tournament_state,
        "currentRound": current_round,
        "requiredTOS": 0,
        "rewards": _tournament_rewards(room),
        "fees": {},
    }


def uid_instance(inner_bytes, field):
    """Extract a UID-typed request field's instance id from raw ObjFmt bytes."""
    if not isinstance(inner_bytes, bytes):
        return 0
    pos = inner_bytes.find(field.encode("utf-8"))
    if pos < 0:
        return 0
    rest = inner_bytes[pos + len(field):]
    uid_pos = rest.find(b"m_UID64")
    if uid_pos < 0:
        return 0
    parts = rest[uid_pos + 7:].split(b";", 5)
    if len(parts) < 5:
        return 0
    try:
        uid64 = struct.unpack("<Q", unhexlify(parts[4].decode("ascii")))[0]
    except (ValueError, TypeError):
        return 0
    return uid64 >> 8


# ── waiting-room & tournament-info rdata ─────────────────────────────

def build_waiting_room_data(base_room):
    prefix = "tourn:waitingroom-"
    tid = int(base_room[len(prefix):])
    players = db_tournament_players_name_map(tid)
    return {base_room: {"players": list(players.values())}}


def build_tournament_info_data(base_room):
    prefix = "tourn:tournament-"
    tid = int(base_room[len(prefix):])
    room = db_tournament_by_id(tid)
    if not room:
        return {}

    matches = db_tournament_matches(tid)
    signups = db_tournament_signups_by_tournament(tid, status=None)
    complete = _tournament_is_complete(room, matches)

    stats = {
        int(s["player_uid"]): {
            "wins": 0, "losses": 0, "games_won": 0, "games_played": 0,
            "state": "WaitingForTournamentStart", "opponents": set(),
            "elimination_reason": _TPE_NOT_ELIMINATED,
            "elimination_round": 0,
        }
        for s in signups
    }
    for match in matches:
        p1 = int(match["player1_uid"])
        p2 = int(match["player2_uid"])
        p1_live = bool(int(match.get("player1_live", 1) or 0))
        p2_live = bool(int(match.get("player2_live", 1) or 0))
        async_tournament = str(room.get("style", "")).lower() in {
            "async", "asynchronous"}
        stats.setdefault(p1, {
            "wins": 0, "losses": 0, "games_won": 0,
            "games_played": 0, "state": "WaitingForNewRound",
            "opponents": set(),
            "elimination_reason": _TPE_NOT_ELIMINATED,
            "elimination_round": 0,
        })
        stats.setdefault(p2, {
            "wins": 0, "losses": 0, "games_won": 0,
            "games_played": 0, "state": "WaitingForNewRound",
            "opponents": set(),
            "elimination_reason": _TPE_NOT_ELIMINATED,
            "elimination_round": 0,
        })
        if p1 != p2 and p1_live:
            stats[p1]["opponents"].add(p2)
        if p1 != p2 and p2_live:
            stats[p2]["opponents"].add(p1)
        game_wins = {p1: 0, p2: 0}
        for winner_key in ("game1_winner", "game2_winner", "game3_winner"):
            winner = int(match.get(winner_key) or 0)
            if winner not in (p1, p2):
                continue
            loser = p2 if winner == p1 else p1
            game_wins[winner] += 1
            if (winner == p1 and p1_live) or (winner == p2 and p2_live):
                stats[winner]["games_won"] += 1
                stats[winner]["games_played"] += 1
            if (loser == p1 and p1_live) or (loser == p2 and p2_live):
                stats[loser]["games_played"] += 1
        if match.get("state") == "Complete":
            if game_wins[p1] > game_wins[p2]:
                match_winner, match_loser = p1, p2
            elif game_wins[p2] > game_wins[p1]:
                match_winner, match_loser = p2, p1
            else:
                match_winner = int(match.get("game1_winner") or 0)
                match_loser = p2 if match_winner == p1 else p1
                if match_winner not in (p1, p2):
                    match_loser = 0
            if match_loser:
                if ((match_winner == p1 and p1_live) or
                        (match_winner == p2 and p2_live)):
                    stats[match_winner]["wins"] += 1
                if ((match_loser == p1 and p1_live) or
                        (match_loser == p2 and p2_live)):
                    stats[match_loser]["losses"] += 1
                    if not async_tournament:
                        stats[match_loser]["state"] = "Eliminated"
                        stats[match_loser]["elimination_reason"] = (
                            _TPE_LOST_MATCH_SINGLE_ELIM)
                        stats[match_loser]["elimination_round"] = int(
                            match.get("round_id") or 0)

    if matches:
        for player in stats.values():
            if player["state"] == "WaitingForTournamentStart":
                player["state"] = "InGame"
    if complete:
        for player in stats.values():
            if player["state"] != "Eliminated":
                player["state"] = "WaitingForNewRound"

    match_win_rates = {
        uid: (player["wins"] / (player["wins"] + player["losses"])
              if player["wins"] + player["losses"] else 0.0)
        for uid, player in stats.items()
    }
    omw_rates = {}
    for uid, player in stats.items():
        opponent_rates = [
            max(match_win_rates.get(opponent, 0.0),
                _TOURNAMENT_OPPONENT_WIN_FLOOR)
            for opponent in player["opponents"]
        ]
        omw_rates[uid] = (sum(opponent_rates) / len(opponent_rates)
                          if opponent_rates else 0.0)
    oomw_rates = {
        uid: (sum(omw_rates.get(opponent, 0.0)
                  for opponent in player["opponents"])
              / len(player["opponents"])
              if player["opponents"] else 0.0)
        for uid, player in stats.items()
    }
    for uid, player in stats.items():
        games_played = player["games_played"]
        player["gwr"] = (player["games_won"] / games_played
                          if games_played else 0.0)
        player["omwr"] = omw_rates[uid]
        player["oomwr"] = oomw_rates[uid]

    ranked = sorted(
        stats.items(),
        key=lambda item: (-item[1]["wins"], -item[1]["omwr"],
                          -item[1]["gwr"], -item[1]["oomwr"], item[0]),
    )
    ranks = {uid: index for index, (uid, _stats) in enumerate(ranked, 1)}
    players = {}
    for s in signups:
        uid = int(s["player_uid"])
        player_stats = stats.get(uid, {
            "wins": 0, "losses": 0, "games_won": 0, "games_played": 0,
            "state": "WaitingForTournamentStart",
        })
        players[str(uid)] = {
            "id": f"p{s['player_uid']}",
            "uid": uid,
            "name": s["player_name"],
            "state": player_stats["state"],
            "eliminationReason": int(player_stats.get(
                "elimination_reason", _TPE_NOT_ELIMINATED
            )),
            "eliminationRound": int(player_stats.get(
                "elimination_round", 0
            )),
            "deckid": str(s["deck_id"]),
            "points": player_stats["wins"],
            "wins": player_stats["wins"],
            "losses": player_stats["losses"],
            "rank": ranks.get(uid, 1),
            "gwr": player_stats["gwr"],
            "omwr": player_stats["omwr"],
            "oomwr": player_stats["oomwr"],
        }

    match_data = {}
    for match in matches:
        match_data[str(match["id"])] = {
            "state": match["state"],
            "status": match["status"],
            "matchID": int(match["match_id"]),
            "roundID": int(match["round_id"]),
            "player1id": f"p{match['player1_uid']}",
            "player2id": f"p{match['player2_uid']}",
            "startTime": int(match["start_time"] or 0),
            "endTime": int(match["end_time"] or 0),
            "game1Winner": int(match["game1_winner"] or 0),
            "game2Winner": int(match["game2_winner"] or 0),
            "game3Winner": int(match["game3_winner"] or 0),
        }

    tournament_state = "Complete" if complete else (
        "PlayGames" if matches else "WaitForStart")
    info = {
        "id": f"t{room['id']}",
        "name": room.get("type_name", ""),
        "completionType": 1 if complete else 0,
        "players": players,
        "matches": match_data,
        "state": tournament_state,
        "numberOfRounds": room.get("games_count", 1),
        "nextRoundTime": 0,
        "format": _tournament_format_bitmask(room),
        "style": _tournament_style_bitmask(room),
        "description": build_tournament_desc_json(room),
    }
    return {base_room: info}


def _dotnet_ticks_now():
    """Return UTC now in the .NET ticks format used by TournamentDataReceiver."""
    return int((time.time() + 62135596800) * 10000000)


def _dotnet_ticks_from_datetime(value):
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int((parsed.timestamp() + 62135596800) * 10000000)
    except (TypeError, ValueError, OverflowError):
        return 0


def _tournament_times(room, matches):
    """Return start/end/open ticks and current round for lobby metadata."""
    starts = [int(m.get("start_time") or 0) for m in matches
              if int(m.get("start_time") or 0) > 0]
    ends = [int(m.get("end_time") or 0) for m in matches
            if int(m.get("end_time") or 0) > 0]
    open_time = _dotnet_ticks_from_datetime(room.get("created_at"))
    start_time = min(starts) if starts else (open_time or _dotnet_ticks_now())
    complete = _tournament_is_complete(room, matches)
    end_time = max(ends) if complete and ends else 0
    current_round = max((int(m.get("round_id") or 0) for m in matches), default=0)
    return start_time, end_time, open_time or start_time, current_round


def _tournament_is_complete(room, matches):
    """Return whether the room's configured rounds have all completed."""
    if str(room.get("status", "")).lower() in {"complete", "closed"}:
        return True
    # Merry Melee/gauntlet is a persistent event.  Individual player runs
    # retire independently; the tournament itself ends only when expiry or an
    # explicit administrative close changes the room status.
    if str(room.get("style", "")).lower() in {"async", "asynchronous"}:
        return False
    if not matches or any(m.get("state") != "Complete" for m in matches):
        return False
    expected_rounds = max(1, int(room.get("games_count") or 1))
    completed_rounds = {
        int(m.get("round_id") or 0) for m in matches
        if m.get("state") == "Complete"
    }
    return len(completed_rounds) >= expected_rounds


def _push_tournament_status_event(handler, tournament_id, complete):
    """Keep TournamentInfo.GetStatus() in sync with the rdata state."""
    info_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.TournamentInfoEventArgs",
         "Game.Shared.Tournaments.TournamentInfo", "System.UInt64",
         "Game.Shared.Tournaments.ETournamentStatus",
         "Game.Shared.Tournaments.ETournamentCompletionType",
         "System.Int32", "System.Int64", "System.Boolean"],
        [("Info", "struct", ("Game.Shared.Tournaments.TournamentInfo", [
            ("TournamentID", "ulong", int(tournament_id)),
            ("TournamentStatus", "enum1", (
                "Game.Shared.Tournaments.ETournamentStatus",
                7 if complete else 6)),
            ("CompletionType", "enum1", (
                "Game.Shared.Tournaments.ETournamentCompletionType",
                1 if complete else 0)),
            ("ResgistrationOpenTime", "long", 0),
            ("Public", "bool", False),
        ]))],
    )
    info_dw = encode_datawrapper(
        0, TOURNAMENT_INFO_DATA_TYPE, compress_gzip(info_inner), 1,
        client_session_guid(handler))
    handler.scnt += 1
    handler.send({
        "issuer": _SERVICE_MAIL_UID,
        "target": "ServicePlayer",
        "instance": handler.sid or "0",
        "reqid": 0,
        "c": 0,
        "conh": 0,
        "sid": handler.sid,
    }, info_dw)


def _publish_tournament_result(tid, signups, finished, handler_overrides=None):
    """Publish the result/status update to every player still connected."""
    overrides = handler_overrides or {}
    recipients = {int(s["player_uid"]) for s in signups}

    def refresh_snapshot(handler, player_uid):
        """Restore the rich rdata snapshot after the status event is handled.

        TournamentInfoEventArgs is intentionally small here, but the client
        replaces its cached TournamentInfo with that object.  ServicePlayer
        events and chat-room rdata are dispatched by different client paths,
        so the sparse completion event can be processed after the full rdata
        packet even though it was sent first.  In that case the lobby has the
        completed status but an empty Games list (displayed as 0-0).  A final
        rdata push makes the authoritative match result the last state in the
        client's cache.
        """
        try:
            push_tournament_room_data(handler, f"tourn:tournament-{tid}_full", "")
            push_tournament_room_data(
                handler, "tourn:lobby_full", "", include_tournament_id=tid)
        except Exception as exc:
            log_req(f"  WARN: tournament result refresh tid={tid} "
                    f"pid={player_uid}: {exc}")

    for player_uid in recipients:
        handler = overrides.get(player_uid) or player_handlers.get(player_uid)
        if not handler:
            continue
        try:
            _push_tournament_status_event(handler, tid, finished)
            push_tournament_room_data(
                handler, f"tourn:tournament-{tid}_full", "")
            push_tournament_room_data(
                handler, "tourn:lobby_full", "", include_tournament_id=tid)
            # Keep the final snapshot on this request thread.  The shared
            # SQLite connection is deliberately not used from timer threads;
            # the old delayed refresh intermittently raised "bad parameter or
            # other API misuse" immediately after a match, leaving the lobby
            # with the pre-game 0-0 descriptor.  Sending it synchronously also
            # preserves the intended packet order: status, full room, lobby,
            # authoritative final room, authoritative final lobby.
            refresh_snapshot(handler, player_uid)
        except Exception as exc:
            log_req(f"  WARN: tournament result push tid={tid} "
                    f"pid={player_uid}: {exc}")


def _return_async_players_to_deckbuilder(tid, player_uids, retired=()):
    """Open the next Corinth deck build for players with runs remaining."""
    room = db_tournament_by_id(tid)
    if not room or not _is_corinth_room(room) or not _is_async_room(room):
        return
    retired = {int(uid) for uid in retired}
    signups = {
        int(signup["player_uid"]): signup
        for signup in db_tournament_signups_by_tournament(tid, status=None)
    }
    for player_uid in {int(uid) for uid in player_uids}:
        signup = signups.get(player_uid)
        handler = player_handlers.get(player_uid)
        if (not signup or signup.get("status") != "active" or
                player_uid in retired or not handler):
            continue
        pool = db_tournament_pool(tid, player_uid, conn=_db)
        if len(pool) != len(CORINTH_SHARD_GUIDS) * 4:
            db_seed_tournament_pool(
                tid, player_uid, CORINTH_SHARD_GUIDS * 4, conn=_db)
            pool = db_tournament_pool(tid, player_uid, conn=_db)
        _push_corinth_deck_construction(handler, tid, pool)
        log_req(f"  Corinth async result: returned pid={player_uid} "
                f"to deck construction")


def record_tournament_game_result(session, winner_pid, loser_pid):
    """Persist a completed game and advance active async players to deck build."""
    session_name = str(getattr(session, "session_name", "") or "")
    if not session_name.startswith("tourney-"):
        return False
    tid = tournament_id_from_session_name(session_name)
    if not tid:
        return False
    room = db_tournament_by_id(tid)
    if not room:
        return False

    signups = db_tournament_signups_by_tournament(tid, status=None)
    signup_uids = [int(s["player_uid"]) for s in signups]
    ordered = [uid for uid in signup_uids if uid in (int(winner_pid), int(loser_pid))]
    if len(ordered) != 2:
        ordered = [int(winner_pid), int(loser_pid)]
    db_tournament_match_start(
        tid, session.session_id, ordered[0], ordered[1], round_id=1,
        start_time=_dotnet_ticks_now(),
    )
    match_id = db_tournament_match_result(
        tid, session.session_id, int(winner_pid), int(loser_pid),
        end_time=_dotnet_ticks_now(),
    )
    if not match_id:
        return False

    retired = (_retire_finished_async_runs(
        tid, (int(winner_pid), int(loser_pid)))
        if _is_async_room(room) else [])

    matches = db_tournament_matches(tid)
    finished = _tournament_is_complete(room, matches)
    db_tournament_set_status(tid, "complete" if finished else "started")
    if finished:
        db_tournament_pool_delete(tid)
    _publish_tournament_result(tid, signups, finished)
    if not finished:
        _return_async_players_to_deckbuilder(
            tid, (winner_pid, loser_pid), retired=retired)
    log_req(f"  Tournament {tid}: recorded match {match_id}, "
            f"winner={winner_pid}, complete={finished}, retired={retired}")
    return True


def record_tournament_forfeit(tournament_id, loser_pid, handler=None):
    """Close the active match when the client leaves via the forfeit button.

    The tournament UI sends LeaveTournament separately from the in-game
    QuitGameTransaction.  If that is the only request received, the match
    otherwise remains PlayGame with a 0-0 score forever.
    """
    tid = int(tournament_id)
    loser_pid = int(loser_pid)
    room = db_tournament_by_id(tid)
    if not room:
        return False
    matches = db_tournament_matches(tid)
    active = next(
        (match for match in matches
         if match.get("state") != "Complete"
         and loser_pid in (int(match["player1_uid"]),
                           int(match["player2_uid"]))),
        None,
    )
    if not active:
        # A LeaveTournament can race the game-result transaction.  Re-publish
        # an already completed room so the leaving client cannot retain the
        # stale 0-0 lobby descriptor it had cached before the result arrived.
        if str(room.get("status", "")).lower() == "complete":
            signups = db_tournament_signups_by_tournament(tid, status=None)
            _publish_tournament_result(tid, signups, True,
                                       {loser_pid: handler} if handler else None)
            log_req(f"  Tournament {tid}: forfeit was already complete; "
                    "republished final lobby result")
            return True
        return False
    player1 = int(active["player1_uid"])
    player2 = int(active["player2_uid"])
    winner_pid = player2 if loser_pid == player1 else player1
    match_id = db_tournament_match_result(
        tid, active["session_id"], winner_pid, loser_pid,
        end_time=_dotnet_ticks_now(),
    )
    if not match_id:
        return False
    retired = (_retire_finished_async_runs(
        tid, (winner_pid, loser_pid))
        if _is_async_room(room) else [])
    signups = db_tournament_signups_by_tournament(tid, status=None)
    finished = _tournament_is_complete(room, db_tournament_matches(tid))
    db_tournament_set_status(tid, "complete" if finished else "started")
    if finished:
        db_tournament_pool_delete(tid)
    _publish_tournament_result(tid, signups, finished,
                               {loser_pid: handler} if handler else None)
    if not finished:
        # A forfeit is still a completed async game.  Keep the persistent
        # Corinth event in the lobby, but return both eligible players to the
        # next deck-construction screen just like a normally resolved game.
        _return_async_players_to_deckbuilder(
            tid, (winner_pid, loser_pid), retired=retired)
    log_req(f"  Tournament {tid}: recorded forfeit match {match_id}, "
            f"winner={winner_pid}, loser={loser_pid}, complete={finished}, "
            f"retired={retired}")
    return True


def recover_stale_tournament_matches(age_seconds=3600):
    """Resolve old incomplete tournament games before session cleanup.

    A disconnected client can leave a tournament match in ``PlayGame`` while
    the durable PvP checkpoint still identifies the player who held priority.
    That player is the timeout loser; the other player receives the win.  The
    normal forfeit/result path is deliberately reused so match state, scores,
    run-limit retirement, rewards, and the next async deck build stay in sync.
    """
    try:
        seconds = max(1, int(age_seconds))
    except (TypeError, ValueError):
        seconds = 3600
    cutoff_ticks = _dotnet_ticks_now() - seconds * 10_000_000
    rows = _db.execute(
        "SELECT tm.tournament_id, tm.session_id, tm.player1_uid, "
        "tm.player2_uid, tm.start_time, gs.turn_order_json "
        "FROM tournament_matches tm "
        "JOIN game_sessions gs ON CAST(gs.session_id AS TEXT)="
        "CAST(tm.session_id AS TEXT) "
        "WHERE tm.state<>'Complete' AND tm.start_time > 0 "
        "AND tm.start_time <= ?",
        (cutoff_ticks,),
    ).fetchall()
    recovered = 0
    for row in rows:
        tid, session_id, player1, player2, _start_time, state_json = row
        try:
            state = json.loads(state_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            state = {}
        try:
            priority_pid = int(state.get("priority_pid") or 0)
        except (TypeError, ValueError):
            priority_pid = 0
        players = {int(player1), int(player2)}
        if priority_pid not in players:
            log_req(
                f"  Stale tournament match skipped: tid={tid} "
                f"session={session_id} has no valid priority player")
            continue
        room = db_tournament_by_id(tid)
        if not room:
            continue
        if record_tournament_forfeit(int(tid), priority_pid):
            recovered += 1
            log_req(
                f"  Recovered stale tournament match: tid={tid} "
                f"session={session_id} loser={priority_pid} "
                f"winner={next(pid for pid in players if pid != priority_pid)}")
    return recovered


def push_tournament_room_data(handler, room, display_name,
                              include_tournament_id=None):
    """Push rdata to a chat room (sent to its '_full' variant)."""
    base = room[:-5] if room.endswith("_full") else room
    if base.startswith("tourn:waitingroom-"):
        lobby = build_waiting_room_data(base)
    elif base.startswith("tourn:tournament-"):
        lobby = build_tournament_info_data(base)
        if not lobby:  # tournament doesn't exist (e.g., tournament-0)
            return
    else:
        rooms = tournament_server.get_active_rooms()
        room_ids = {int(r["id"]) for r in rooms}
        # The normal lobby snapshot contains waiting rooms for everyone, plus
        # completed history for the player viewing the lobby.  Completed rooms
        # are not joinable, so exposing another player's history would only
        # add stale/non-actionable rows to the Battlegrounds list.
        try:
            player_uid = int(getattr(handler, "client_reck_id", 0) or 0)
        except (TypeError, ValueError):
            player_uid = 0
        if player_uid:
            for completed_room in db_tournament_completed_for_player(player_uid):
                if int(completed_room["id"]) not in room_ids:
                    rooms = list(rooms) + [completed_room]
                    room_ids.add(int(completed_room["id"]))
        if include_tournament_id is not None:
            completed_room = db_tournament_by_id(int(include_tournament_id))
            if completed_room and int(completed_room["id"]) not in room_ids:
                # TournamentManager keeps descriptors that disappear from a
                # full lobby update.  Include the just-completed room once so
                # the client replaces its stale 0/0 joinable descriptor with
                # the authoritative Complete/2-player descriptor.
                rooms = list(rooms) + [completed_room]
                room_ids.add(int(completed_room["id"]))
        lobby = {}
        for r in rooms:
            lobby[f"tournament-{r['id']}"] = build_tournament_desc_json(r)

    # RoomInfo.processUpdate constructs DateTime directly from this value;
    # the client therefore expects .NET ticks, not Unix milliseconds.  Using
    # Unix milliseconds makes the client treat serverTime as year 1 and then
    # add the current year again when converting tournament timestamps.
    payload = [[1, "/", lobby, _dotnet_ticks_now()]]
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_bytes = payload_json.encode("utf-8")
    compressed = gzip.compress(payload_bytes)
    envelope = json.dumps({
        "action": "rdata", "room": room, "rflg": "", "flg": "",
        "user": display_name, "sz": len(compressed),
    }, separators=(",", ":"))
    body = envelope.encode("utf-8") + compressed
    handler.scnt += 1
    handler.send({"issuer": "Session", "target": "chat", "sid": handler.sid},
                 body=body)
    log_req(f"    Pushed rdata to {room} ({len(lobby)} rooms, {len(body)}b)")


# ── game session creation ────────────────────────────────────────────

_SERVICE_MAIL_UID = "0.0.0.0.ServiceTournaments.252"


def _push_corinth_deck_construction(handler, room_id, pool_cards):
    """Put one async Corinth entrant directly into deck construction."""
    # Deck construction is also the start of a new async run. Refresh the
    # rich tournament-info projection first so the client replaces any cached
    # score from the retired run with the new 0-0 live-run state.
    try:
        push_tournament_room_data(
            handler, f"tourn:tournament-{int(room_id)}_full", "")
    except Exception as exc:
        log_req(f"    WARN: Corinth score refresh failed: {exc}")
    pool = [(str(template_guid), "", int(card_uid), 0, 0, 0)
            for card_uid, template_guid, _location in pool_cards]
    inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.DeckConstructionStartedEventArgs",
         "Game.Shared.Tournaments.TournamentInfo",
         "Game.Shared.Domain.deck_bits"],
        [("TournamentID", "ulong", int(room_id)),
         ("TournamentInfo", "struct",
          ("Game.Shared.Tournaments.TournamentInfo",
           [("TournamentID", "ulong", int(room_id))])),
         ("my_Deck", "deckbits", ("corinth", "Corinth", 0, 0,
                                    {"main": pool, "sideboard": []},
                                    CORINTH_CHAMPION_GUID)),
         ("timeForSideboarding", "long", 0),
         ("PlayerID", "ulong", int(handler.client_reck_id or 0))])
    body = compress_gzip(inner)
    dw = encode_datawrapper(
        0, TOURNAMENT_DECK_CONSTRUCTION_DATA_TYPE, body, 1,
        client_session_guid(handler))
    handler.scnt += 1
    handler.send({
        "issuer": _SERVICE_MAIL_UID, "target": "ServicePlayer",
        "instance": handler.sid or "0", "reqid": 0, "c": 0,
        "conh": 0, "sid": handler.sid,
    }, dw)
    log_req(f"    Pushed Corinth deck construction tid={room_id} "
            f"player={handler.client_reck_id}")


def resume_corinth_deck_construction(handler, room_id):
    """Resume a Corinth deck build after a reconnect has authenticated.

    Tournament reconnect requests can arrive before the auth response on a
    freshly recreated client socket.  In that window the handler still has
    its placeholder ReckID, so the reconnect must be retried after auth.
    """
    tid = int(room_id or 0)
    player_uid = int(getattr(handler, "client_reck_id", 0) or 0)
    room = db_tournament_room_for_game(tid, conn=_db) if tid else None
    signup = (db_tournament_signup_by_player(tid, player_uid, conn=_db)
              if tid and player_uid else None)
    live = bool(signup and db_tournament_player_run_live(
        tid, player_uid, conn=_db))
    if (not room or not signup or not _is_corinth_room(room) or
            str(room.get("status", "")).lower() == "closed" or live):
        return False
    needs_commit = False
    try:
        if signup.get("status") != "active":
            from tournament_db import db_tournament_signup_set_status
            db_tournament_signup_set_status(tid, player_uid, "active", conn=_db)
            db_tournament_signup_set_async_state(
                tid, player_uid, deck_ready=False, searching=False, conn=_db)
            needs_commit = True
        pool = db_tournament_pool(tid, player_uid, conn=_db)
        if len(pool) != len(CORINTH_SHARD_GUIDS) * 4:
            db_seed_tournament_pool(tid, player_uid, CORINTH_SHARD_GUIDS * 4,
                                    conn=_db)
            pool = db_tournament_pool(tid, player_uid, conn=_db)
            needs_commit = True
        if needs_commit:
            _db.commit()
    except BaseException:
        _db.rollback()
        raise
    with player_handler_lock:
        player_handlers[player_uid] = handler
    _push_corinth_deck_construction(handler, tid, pool)
    log_req(f">>> Auth reconnect: resumed Corinth tid={tid} "
            f"player={player_uid}")
    return True


@_rollback_shared_db_on_error
def start_waiting_room_game(room_id, handler_overrides=None,
                            match_player_uids=None):
    """Create a game session for a filled room or an async pair.

    ``match_player_uids`` is deliberately separate from the tournament's
    registered player list: persistent async events can contain many entrants
    while every battle session still contains exactly two players.

    ``EnterTournament`` runs on the joining client's request thread.  Keep a
    snapshot of the handlers selected for this room while that request is
    completing; otherwise a reconnect or concurrent join can replace the
    shared registry entry before the start events are pushed.
    """
    import game_session as gs
    import encoder
    room = db_tournament_room_for_game(room_id)
    if not room:
        log_req(f"  Room {room_id}: not found")
        return
    players = json.loads(room.get("players_json", "{}"))
    pids = ([str(int(pid)) for pid in match_player_uids]
            if match_player_uids is not None else list(players.keys()))
    room_handlers = dict(handler_overrides or {})
    with player_handler_lock:
        for puid_str in pids:
            puid = int(puid_str)
            room_handlers.setdefault(puid, player_handlers.get(puid))
    log_req(f"  Room {room_id}: start handlers="
            f"{[(int(pid), bool(room_handlers.get(int(pid)))) for pid in pids]}")
    # Use the HConnect connection for instance allocation as well as the
    # session/match writes below.  Opening a second connection here can block
    # indefinitely while tournament_server is updating the same SQLite meta
    # row, leaving both clients stuck in Finding Player.
    inst = gs._next_instance(conn=_db)
    session_name = f"tourney-{room_id}-{inst}"
    sid_value = encoder.make_uid(AUTHORITATIVE_SESSION_UID_TYPE, inst)
    srv_value = encoder.make_uid(SERVICE_GAME_SESSION_UID_TYPE, inst * 7)
    session = gs.GameSession(sid_value, srv_value, session_name,
                             int(pids[0]) if pids else 0)
    corinth_mode = _is_corinth_room(room)
    if corinth_mode:
        # The mode is carried in the persisted session because the later
        # GameSession handlers are reconstructed from the database after the
        # tournament service hands the battle to HConnect.
        session.encounter_data = {
            "tournament_mode": CORINTH_MERRY_MELEE_MODE,
            "tournament_type_id": int(room.get("type_id") or 0),
            "starting_hand_size": 4,
            "skip_draw_phase": True,
            "end_turn_draw_count": 4,
            "recycle_discard_at_end_turn": True,
        }
    for puid_str in pids:
        session.add_player(
            encoder.make_uid(SERVICE_PLAYER_UID_TYPE, int(puid_str)), 0,
            conn=_db)
    session.state = "starting"
    session._persist(conn=_db)
    if len(pids) >= 2:
        ordered_pids = [int(pid) for pid in pids[:2]]
        db_tournament_match_start(
            room_id, session.session_id, ordered_pids[0], ordered_pids[1],
            round_id=1, start_time=_dotnet_ticks_now(), conn=_db,
        )
    # A persistent async event remains joinable while individual two-player
    # matches are created and retired underneath it.
    if not (corinth_mode and match_player_uids is not None):
        tournament_server.start_tournament(room_id, sid_value)

    # Seed each player's deck into game_cards.
    for puid_str in pids:
        puid = int(puid_str)
        signup = db_tournament_signup_by_player(room_id, puid, conn=_db)
        deck_db_id = signup["deck_id"] if signup else 0
        if not deck_db_id:
            deck_db_id = player_decks.get(puid, 0)
        if not deck_db_id and not corinth_mode:
            log_req(f"    WARN: No deck for player {puid} in room {room_id} — skipping")
            continue
        if corinth_mode:
            if match_player_uids is not None:
                main_cards = [guid for _uid, guid, location in
                              db_tournament_pool(room_id, puid, conn=_db)
                              if int(location) == 0]
                seed = db_seed_tournament_game_deck(
                    session.session_id, puid, 0, card_guids=main_cards,
                    champion_guid=CORINTH_CHAMPION_GUID, conn=_db)
            else:
                seed = db_seed_tournament_game_deck(
                    session.session_id, puid, 0,
                    card_guids=CORINTH_SHARD_GUIDS * 4,
                    champion_guid=CORINTH_CHAMPION_GUID, conn=_db)
        else:
            seed = db_seed_tournament_game_deck(
                session.session_id, puid, deck_db_id, conn=_db)
        log_req(
            f"    Seeded {seed['inserted']} cards "
            f"(skipped: int={seed['skipped_int']} "
            f"invalid={seed['skipped_invalid']} err={seed['skipped_error']}) "
            f"from deck {deck_db_id} for player {puid}")
        if seed["champion_guid"]:
            db_insert_tournament_champion_card(
                session.session_id, puid, seed["champion_guid"], conn=_db)
            log_req(f"    Created champion {seed['champion_guid'][:8]} "
                    f"for player {puid}")
        if corinth_mode and match_player_uids is None:
            db_tournament_pool_replace(
                room_id, puid,
                [(card_uid, template_guid, 1) for card_uid, template_guid in
                 db_game_deck_cards(session.session_id, puid, conn=_db)],
                conn=_db)

    _db.commit()
    log_req(f"  Room {room_id}: game started as {session_name}")

    # Push DeckConstructionStarted (25072) to set CurrentTournament.
    # The client transitions to sideboarding — player clicks Confirm,
    # which sends GameEntrance (25039).  We push TournamentSessionStart
    # then, with CurrentTournament already populated.
    for puid_str in pids:
        puid = int(puid_str)
        h = room_handlers.get(puid)
        if h:
            try:
                signup = db_tournament_signup_by_player(room_id, puid)
                s_deck = signup["deck_id"] if signup else 0
                if not s_deck:
                    s_deck = player_decks.get(puid, 0)

                if corinth_mode and match_player_uids is None:
                    # Corinth's starting cards are the actual main deck. The
                    # client must not receive a sideboard/pool here.
                    pool_cards = [
                        (str(template_guid), "", int(card_uid), 0, 0, 0)
                        for card_uid, template_guid in db_game_deck_cards(
                            session.session_id, puid, conn=_db)
                    ]
                    deck_bits = (
                        "corinth", "Corinth", 0, 0,
                        {"main": pool_cards, "sideboard": []},
                        CORINTH_CHAMPION_GUID)
                else:
                    deck_bits = None

                # 25072 sets CurrentTournament.  A matched Corinth client
                # is already past deck construction, so do not overwrite its
                # saved deck with an empty construction packet.
                if not (corinth_mode and match_player_uids is not None):
                    dcs_inner = encode_objfmt_response(
                        ["Game.Shared.Network.Tournaments.DeckConstructionStartedEventArgs",
                         "Game.Shared.Tournaments.TournamentInfo",
                         "Game.Shared.Domain.deck_bits"],
                        [("TournamentID", "ulong", room_id),
                         ("TournamentInfo", "struct",
                          ("Game.Shared.Tournaments.TournamentInfo",
                           [("TournamentID", "ulong", room_id)])),
                         ("my_Deck", "deckbits", deck_bits or
                          ("", "", 0, 0, [])),
                         ("timeForSideboarding", "long", 0),
                         ("PlayerID", "ulong", int(puid))])
                    dcs_body = compress_gzip(dcs_inner)
                    dcs_dw = encode_datawrapper(
                        0, TOURNAMENT_DECK_CONSTRUCTION_DATA_TYPE,
                        dcs_body, 1, client_session_guid(h))
                    h.scnt += 1
                    h.send({
                        "issuer": _SERVICE_MAIL_UID,
                        "target": "ServicePlayer", "instance": h.sid or "0",
                        "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
                    }, dcs_dw)

                if corinth_mode and match_player_uids is None:
                    # The ordinary two-player start is deferred for async
                    # Corinth; matching sends this transition later.
                    log_req(f"    Deferred 25060/25058 for Corinth tid={room_id} "
                            f"player={puid}")
                    continue

                # 25060 — override sideboarding → Battle
                sid_u64 = int(session.session_id) if isinstance(session.session_id, int) else 0
                enc_flags = _tournament_session_flags(room)
                evt_inner = encode_objfmt_response(
                    ["Game.Shared.Network.Tournaments.TournamentSessionStartEventArgs",
                     "Game.Shared.SessionState",
                     "Game.Shared.SessionStateEncounterData",
                     "Game.Shared.UID"],
                    [("SessionState", "struct",
                      ("Game.Shared.SessionState",
                       [("SessionId", "uid", sid_u64),
                        ("SessionName", "string", session_name),
                        ("MinimumPlayerCount", "int", 2),
                        ("MaximumPlayerCount", "int", 2),
                        ("EncounterData", "struct",
                         ("Game.Shared.SessionStateEncounterData",
                          [("SessionFlags", "int", enc_flags),
                           ("IsVirtualTournament", "bool", True),
                           ("TournamentID", "ulong", room_id),
                           ])),
                        ("JoinInsteadOfReconnect", "bool", True)])),
                     ("DeckId", "uid", (s_deck << 8) | 17),
                     ("Forced", "bool", True)])
                evt_body = compress_gzip(evt_inner)
                evt_dw = encode_datawrapper(0, TOURNAMENT_SESSION_START_DATA_TYPE, evt_body, 1,
                                            client_session_guid(h))
                h.scnt += 1
                h.send({
                    "issuer": _SERVICE_MAIL_UID,
                    "target": "ServicePlayer", "instance": h.sid or "0",
                    "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
                }, evt_dw)

                # 25058 — LAST: fires InfoUpdated_Transition → GoToTargetState()
                ti_inner = encode_objfmt_response(
                    ["Game.Shared.Network.Tournaments.TournamentInfoEventArgs",
                     "Game.Shared.Tournaments.TournamentInfo",
                     "System.UInt64"],
                    [("Info", "struct", ("Game.Shared.Tournaments.TournamentInfo", [
                        ("TournamentID", "ulong", room_id)]))])
                ti_body = compress_gzip(ti_inner)
                ti_dw = encode_datawrapper(0, TOURNAMENT_INFO_DATA_TYPE, ti_body, 1,
                                            client_session_guid(h))
                h.scnt += 1
                h.send({
                    "issuer": _SERVICE_MAIL_UID, "target": "ServicePlayer",
                    "instance": h.sid or "0", "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
                }, ti_dw)
                log_req(f"    Pushed 25072+25060+25058 for tid={room_id}")
            except Exception as e:
                log_req(f"  WARN: push 25072 to {puid} failed: {e}")


_corinth_match_lock = threading.Lock()


def try_start_corinth_match(room_id, handler_override=None):
    """Pair the first two ready Corinth entrants and start their match."""
    room = db_tournament_room_for_game(room_id)
    if not room or not _is_corinth_room(room) or not _is_async_room(room):
        return False
    with _corinth_match_lock:
        ready = db_tournament_async_ready_players(room_id, conn=_db)
        eligible = []
        for pid, name in ready:
            wins, losses = db_tournament_player_score(room_id, pid)
            if wins >= CORINTH_RUN_WINS or losses >= CORINTH_RUN_LOSSES:
                _retire_finished_async_runs(room_id, (pid,))
                log_req(f"  Corinth matcher: pid {pid} reached run limit "
                        f"({wins}-{losses}); excluded")
                continue
            eligible.append((pid, name))
        ready = eligible
        if len(ready) < 2:
            return False
        pids = [int(ready[0][0]), int(ready[1][0])]
        for pid in pids:
            db_tournament_signup_set_async_state(
                room_id, pid, searching=False, conn=_db)
        handlers = {}
        if handler_override is not None:
            handlers[int(getattr(handler_override, "client_reck_id", 0) or 0)] = handler_override
        with player_handler_lock:
            for pid in pids:
                handlers.setdefault(pid, player_handlers.get(pid))
        start_waiting_room_game(
            room_id, handler_overrides=handlers, match_player_uids=pids)
        log_req(f"  Corinth async match started tid={room_id} players={pids}")
        return True


def push_tournament_session_start(handler, room_id, session_id,
                                   session_name, deck_id, room):
    """Push the deferred 25060/25058 transition after deck confirmation."""
    sid_u64 = int(session_id) if isinstance(session_id, int) else 0
    enc_flags = _tournament_session_flags(room)
    evt_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.TournamentSessionStartEventArgs",
         "Game.Shared.SessionState",
         "Game.Shared.SessionStateEncounterData",
         "Game.Shared.UID"],
        [("SessionState", "struct",
          ("Game.Shared.SessionState",
           [("SessionId", "uid", sid_u64),
            ("SessionName", "string", session_name),
            ("MinimumPlayerCount", "int", 2),
            ("MaximumPlayerCount", "int", 2),
            ("EncounterData", "struct",
             ("Game.Shared.SessionStateEncounterData",
              [("SessionFlags", "int", enc_flags),
               ("IsVirtualTournament", "bool", True),
               ("TournamentID", "ulong", int(room_id))])),
            ("JoinInsteadOfReconnect", "bool", True)])),
         ("DeckId", "uid", (int(deck_id) << 8) | 17),
         ("Forced", "bool", True)])
    evt_dw = encode_datawrapper(
        0, TOURNAMENT_SESSION_START_DATA_TYPE,
        compress_gzip(evt_inner), 1, client_session_guid(handler))
    handler.scnt += 1
    handler.send({"issuer": _SERVICE_MAIL_UID, "target": "ServicePlayer",
                  "instance": handler.sid or "0", "reqid": 0, "c": 0,
                  "conh": 0, "sid": handler.sid}, evt_dw)

    ti_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.TournamentInfoEventArgs",
         "Game.Shared.Tournaments.TournamentInfo", "System.UInt64"],
        [("Info", "struct", ("Game.Shared.Tournaments.TournamentInfo",
                               [("TournamentID", "ulong", int(room_id))]))])
    ti_dw = encode_datawrapper(
        0, TOURNAMENT_INFO_DATA_TYPE, compress_gzip(ti_inner), 1,
        client_session_guid(handler))
    handler.scnt += 1
    handler.send({"issuer": _SERVICE_MAIL_UID, "target": "ServicePlayer",
                  "instance": handler.sid or "0", "reqid": 0, "c": 0,
                  "conh": 0, "sid": handler.sid}, ti_dw)
    log_req(f"    Pushed deferred 25060+25058 for Corinth tid={room_id}")
