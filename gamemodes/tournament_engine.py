"""Tournament game engine — session creation, desc building, rdata pushes.

Extracted from hconnect_server.py.  The ``player_handlers`` dict and
related state live here so the matchmaking service can access them.
"""

import json, gzip, time, threading, struct
from datetime import datetime, timezone
from binascii import unhexlify

from db import _db, log_req
from db import (db_tournament_by_id, db_tournament_players_name_map,
                db_tournament_signups_by_tournament,
                db_tournament_completed_for_player,
                db_tournament_signup_by_player, db_tournament_matches,
                db_tournament_match_start, db_tournament_match_result,
                db_tournament_set_status)
from encoder import encode_objfmt_response, compress_gzip, encode_datawrapper, client_session_guid
import gamemodes.tournament_server as tournament_server

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
    return encode_datawrapper(0, 25029, body, comp, session_id)


def _make_deck_data(deck_id):
    """Return (did, dname, did_val, champ_did, card_guids) for deckbits encoding."""
    row = _db.execute(
        "SELECT cards, pvp_champion_guid FROM decks WHERE id=?",
        (int(deck_id) if deck_id else 0,)).fetchone()
    if not row:
        return (f"d{deck_id}", "Unknown Deck", int(deck_id) if deck_id else 0, 0, [])
    cards_json, champ_guid = row
    dname = f"Deck #{deck_id}"
    # Look up a real deck name
    name_row = _db.execute(
        "SELECT d.deck_name, d.pve_champion_id FROM decks d WHERE d.id=?",
        (int(deck_id) if deck_id else 0,)).fetchone()
    if name_row:
        dname = name_row[0] or dname
        champ_did = name_row[1] or 0
    else:
        champ_did = 0
    return (f"d{deck_id}", dname, int(deck_id) if deck_id else 0, int(champ_did), [])


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
    flags = 8192  # Game.Shared.ESessionFlags.IsDuelingPit
    if format_bits & 16:  # Game.Shared.Tournaments.ETournamentFormats.Immortal
        flags |= 1024  # Game.Shared.ESessionFlags.IsImmortalPvP
    else:
        flags |= 4096  # Game.Shared.ESessionFlags.IsStandardPvP
    return flags


def _tournament_style_bitmask(room):
    style_str = (room.get("style") or "sw").lower()
    return {"se": 0, "sw": 1}.get(style_str, 0)


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
    max_p = room.get("max_players", 2)
    min_p = room.get("min_players", max_p)
    complete = _tournament_is_complete(room, matches)
    if complete:
        tournament_state = "Complete"
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
        "name": f"{room.get('type_name', '')} #{room['id']}",
        "numPlayers": len(all_signups) if all_signups else len(players),
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
        "rewards": {"tournamentRewards": []},
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
        if p1 != p2:
            stats[p1]["opponents"].add(p2)
            stats[p2]["opponents"].add(p1)
        game_wins = {p1: 0, p2: 0}
        for winner_key in ("game1_winner", "game2_winner", "game3_winner"):
            winner = int(match.get(winner_key) or 0)
            if winner not in (p1, p2):
                continue
            loser = p2 if winner == p1 else p1
            game_wins[winner] += 1
            stats[winner]["games_won"] += 1
            stats[winner]["games_played"] += 1
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
                stats[match_winner]["wins"] += 1
                stats[match_loser]["losses"] += 1
                stats[match_loser]["state"] = "Eliminated"
                stats[match_loser]["elimination_reason"] = (
                    _TPE_LOST_MATCH_SINGLE_ELIM
                )
                stats[match_loser]["elimination_round"] = int(
                    match.get("round_id") or 0
                )

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
        0, 25058, compress_gzip(info_inner), 1, client_session_guid(handler))
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


def record_tournament_game_result(session, winner_pid, loser_pid):
    """Persist a completed tournament PvP game and publish the lobby update."""
    session_name = str(getattr(session, "session_name", "") or "")
    if not session_name.startswith("tourney-"):
        return False
    try:
        tid = int(session_name[len("tourney-"):])
    except ValueError:
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

    matches = db_tournament_matches(tid)
    finished = _tournament_is_complete(room, matches)
    db_tournament_set_status(tid, "complete" if finished else "started")
    _publish_tournament_result(tid, signups, finished)
    log_req(f"  Tournament {tid}: recorded match {match_id}, "
            f"winner={winner_pid}, complete={finished}")
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
    signups = db_tournament_signups_by_tournament(tid, status=None)
    finished = _tournament_is_complete(room, db_tournament_matches(tid))
    db_tournament_set_status(tid, "complete" if finished else "started")
    _publish_tournament_result(tid, signups, finished,
                               {loser_pid: handler} if handler else None)
    log_req(f"  Tournament {tid}: recorded forfeit match {match_id}, "
            f"winner={winner_pid}, loser={loser_pid}, complete={finished}")
    return True


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


def start_waiting_room_game(room_id, handler_overrides=None):
    """Create a game session when a room fills up.

    ``EnterTournament`` runs on the joining client's request thread.  Keep a
    snapshot of the handlers selected for this room while that request is
    completing; otherwise a reconnect or concurrent join can replace the
    shared registry entry before the start events are pushed.
    """
    import game_session as gs
    import encoder
    import random as _random

    row = _db.execute(
        "SELECT t.*, tt.name AS type_name, tt.style, tt.format, "
        "tt.min_players, tt.max_players, tt.games_count, tt.set_id "
        "FROM tournaments t JOIN tournament_types tt ON t.type_id = tt.id "
        "WHERE t.id=? LIMIT 1", (room_id,)).fetchone()
    if not row:
        log_req(f"  Room {room_id}: not found")
        return
    col_names = ["id", "type_id", "status", "players_json", "session_id",
                 "created_at", "type_name", "style", "format",
                 "min_players", "max_players", "games_count", "set_id"]
    room = dict(zip(col_names, row))
    players = json.loads(room.get("players_json", "{}"))
    pids = list(players.keys())
    room_handlers = dict(handler_overrides or {})
    with player_handler_lock:
        for puid_str in pids:
            puid = int(puid_str)
            room_handlers.setdefault(puid, player_handlers.get(puid))
    log_req(f"  Room {room_id}: start handlers="
            f"{[(int(pid), bool(room_handlers.get(int(pid)))) for pid in pids]}")
    session_name = f"tourney-{room_id}"
    inst = gs._next_instance()
    sid_value = encoder.make_uid(13, inst)          # AuthoritativeSession (matches live game)
    srv_value = encoder.make_uid(246, inst * 7)
    session = gs.GameSession(sid_value, srv_value, session_name,
                             int(pids[0]) if pids else 0)
    for puid_str in pids:
        session.add_player(encoder.make_uid(244, int(puid_str)), 0)  # ServicePlayer
    session.state = "starting"
    session._persist()
    if len(pids) >= 2:
        ordered_pids = [int(pid) for pid in pids[:2]]
        db_tournament_match_start(
            room_id, session.session_id, ordered_pids[0], ordered_pids[1],
            round_id=1, start_time=_dotnet_ticks_now(),
        )
    tournament_server.start_tournament(room_id, sid_value)

    # Seed each player's deck into game_cards.
    for puid_str in pids:
        puid = int(puid_str)
        signup = db_tournament_signup_by_player(room_id, puid)
        deck_db_id = signup["deck_id"] if signup else 0
        if not deck_db_id:
            deck_db_id = player_decks.get(puid, 0)
        if not deck_db_id:
            log_req(f"    WARN: No deck for player {puid} in room {room_id} — skipping")
            continue
        deck_row = _db.execute(
            "SELECT cards, pvp_champion_guid, user_id, active_gems FROM decks WHERE id=?",
            (deck_db_id,)).fetchone()
        if not deck_row:
            continue
        cards_json = deck_row[0] or "[]"
        champ_guid = deck_row[1] or ""
        deck_owner_uid = deck_row[2]
        active_gems = {}
        try:
            active_gems = json.loads(deck_row[3]) if deck_row[3] else {}
        except Exception:
            active_gems = {}
        card_guids = json.loads(cards_json) if isinstance(cards_json, str) else cards_json
        _random.shuffle(card_guids)
        # Resolve the deck's socketed-gem abilities keyed by instance id -> [guids]
        # (e.g. Shamed Gladiator's Minor Blood Orb -> Rage), so those gem abilities
        # bake into the card's card_abilities and show on the drawn/played card.
        gem_ability_by_inst = {}
        for _inst_str, _gem in (active_gems or {}).items():
            try:
                _gem_i = int(_gem)
            except (TypeError, ValueError):
                continue
            if _gem_i <= 0:
                continue
            _grow = _db.execute(
                "SELECT abilities_json FROM gem_templates WHERE gem_type=?",
                (_gem_i,)).fetchone()
            if _grow and _grow[0]:
                try:
                    _gabs = json.loads(_grow[0])
                except Exception:
                    _gabs = []
                if _gabs:
                    gem_ability_by_inst[str(_inst_str)] = [str(a).lower() for a in _gabs]
        inserted = 0; skipped_int = 0; skipped_invalid = 0; skipped_other = 0
        for pos, tguid in enumerate(card_guids):
            orig_tguid = tguid
            inst_id_str = None
            # Resolve integer instance IDs to template GUIDs (uses deck owner's user_id)
            if isinstance(tguid, (int, float)):
                inst_id_str = str(int(tguid))
                resolved = _db.execute(
                    "SELECT template_guid FROM card_instances "
                    "WHERE instance_id=? AND user_id=?",
                    (int(tguid), deck_owner_uid)).fetchone()
                if resolved:
                    tguid = resolved[0]
                else:
                    skipped_int += 1
                    continue
            if not isinstance(tguid, str) or len(str(tguid)) != 36:
                skipped_invalid += 1
                continue
            max_cuid = _db.execute(
                "SELECT COALESCE(MAX(card_uid), 0) FROM game_cards "
                "WHERE session_id=?", (session.session_id,)).fetchone()[0]
            # Store as proper UID: (instance << 8) | 1 (type=1, Card)
            card_uid = (max_cuid + 256) if max_cuid > 0 else 257
            try:
                _db.execute(
                    "INSERT INTO game_cards (session_id, user_id, card_uid, "
                    "template_guid, card_template_id, location, position) "
                    "VALUES (?, ?, ?, ?, ?, 'deck', ?)",
                    # active_gems is keyed by the original FRA instance id;
                    # retain it while using template_guid for card lookups.
                    (session.session_id, puid, card_uid, tguid, orig_tguid, pos))
                # Backfill per-instance data from the card template so the
                # card is immediately valid (card_type, abilities, attributes,
                # original_template_guid).  Otherwise cards have card_type="Unknown"
                # and can't be offered for attack / ability activation.
                trow = _db.execute(
                    "SELECT card_type, abilities_json, attributes FROM card_templates WHERE guid=?",
                    (tguid,)).fetchone()
                if trow:
                    ct = trow[0] or "Unknown"
                    ab = trow[1] or "[]"
                    attrs = int(trow[2] or 0)
                    try:
                        ab_list = json.loads(ab) if ab else []
                    except Exception:
                        ab_list = []
                    # Append the deck's socketed-gem abilities for this instance
                    # so the card shows its gem power (e.g. Shamed Gladiator Rage).
                    if inst_id_str and inst_id_str in gem_ability_by_inst:
                        for g_a in gem_ability_by_inst[inst_id_str]:
                            if g_a not in ab_list:
                                ab_list.append(g_a)
                        ab = json.dumps(ab_list)
                    _db.execute(
                        "UPDATE game_cards SET card_type=?, card_abilities=?, "
                        "card_attributes=?, gems=?, original_template_guid = CASE "
                        "WHEN COALESCE(original_template_guid,'')='' THEN ? "
                        "ELSE original_template_guid END "
                        "WHERE session_id=? AND card_uid=?",
                        (ct, ab, attrs,
                         int(active_gems.get(inst_id_str, 0) or 0)
                         if inst_id_str else 0,
                         tguid, session.session_id, card_uid))
                inserted += 1
            except Exception as e:
                log_req(f"    INSERT failed for card pos={pos} orig={orig_tguid!r} guid={tguid!r}: {e}")
                skipped_other += 1
        _db.commit()
        log_req(f"    Seeded {inserted} cards (skipped: int={skipped_int} invalid={skipped_invalid} err={skipped_other}) from deck {deck_db_id} for player {puid}")
        if champ_guid:
            max_cuid = _db.execute(
                "SELECT COALESCE(MAX(card_uid), 0) FROM game_cards "
                "WHERE session_id=?", (session.session_id,)).fetchone()[0]
            card_uid = (max_cuid + 256) if max_cuid > 0 else 257
            _db.execute(
                "INSERT INTO game_cards (session_id, user_id, card_uid, "
                "template_guid, card_template_id, card_type, location, "
                "position, is_champion) "
                "VALUES (?, ?, ?, ?, ?, 'Champion', 'champion', 0, 1)",
                (session.session_id, puid, card_uid, champ_guid, champ_guid))
            _db.commit()
            log_req(f"    Created champion {champ_guid[:8]} for player {puid}")

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

                # 25072 — sets CurrentTournament
                dcs_inner = encode_objfmt_response(
                    ["Game.Shared.Network.Tournaments.DeckConstructionStartedEventArgs",
                     "Game.Shared.Tournaments.TournamentInfo",
                     "Game.Shared.Domain.deck_bits"],
                    [("TournamentID", "ulong", room_id),
                     ("TournamentInfo", "struct",
                      ("Game.Shared.Tournaments.TournamentInfo",
                       [("TournamentID", "ulong", room_id)])),
                     ("my_Deck", "class", "Game.Shared.Domain.deck_bits"),
                     ("timeForSideboarding", "long", 0),
                     ("PlayerID", "ulong", int(puid))])
                dcs_body = compress_gzip(dcs_inner)
                dcs_dw = encode_datawrapper(0, 25072, dcs_body, 1,
                                            client_session_guid(h))
                h.scnt += 1
                h.send({
                    "issuer": _SERVICE_MAIL_UID,
                    "target": "ServicePlayer", "instance": h.sid or "0",
                    "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
                }, dcs_dw)

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
                evt_dw = encode_datawrapper(0, 25060, evt_body, 1,
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
                ti_dw = encode_datawrapper(0, 25058, ti_body, 1,
                                            client_session_guid(h))
                h.scnt += 1
                h.send({
                    "issuer": _SERVICE_MAIL_UID, "target": "ServicePlayer",
                    "instance": h.sid or "0", "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
                }, ti_dw)
                log_req(f"    Pushed 25072+25060+25058 for tid={room_id}")
            except Exception as e:
                log_req(f"  WARN: push 25072 to {puid} failed: {e}")
