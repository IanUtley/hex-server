"""ServiceTournaments requests (entry, reconnection, ladder records).

Moved out of hconnect_server.HCPHandler._handle_service_request_legacy.
"""

from application.response_encoding import compress_gzip
from application.response_encoding import encode_datawrapper
from application.response_encoding import encode_objfmt_response
from db import _db
from db import log_req
from domain.enums import ETournamentFormats
from gamemodes.tournament_engine import CORINTH_SHARD_GUIDS
from gamemodes.tournament_engine import _encode_enter_tournament_error
from gamemodes.tournament_engine import _is_corinth_room
from gamemodes.tournament_engine import _make_deck_data
from gamemodes.tournament_engine import _push_corinth_deck_construction
from gamemodes.tournament_engine import db_tournament_discard_match
from gamemodes.tournament_engine import player_decks
from gamemodes.tournament_engine import player_handler_lock
from gamemodes.tournament_engine import player_handlers
from gamemodes.tournament_engine import push_tournament_room_data
from gamemodes.tournament_engine import push_tournament_session_start
from gamemodes.tournament_engine import record_tournament_forfeit
from gamemodes.tournament_engine import start_waiting_room_game
from gamemodes.tournament_engine import try_start_corinth_match
from gamemodes.tournament_engine import uid_instance
from pvp_db import db_apply_tournament_main_deck
from pvp_db import db_delete_game_session
from pvp_db import db_game_session_pids
from tournament_db import db_seed_tournament_pool
from tournament_db import db_tournament_by_id
from tournament_db import db_tournament_match_session
from tournament_db import db_tournament_player_run_live
from tournament_db import db_tournament_pool
from tournament_db import db_tournament_pool_replace
from tournament_db import db_tournament_retire_player_run
from tournament_db import db_tournament_room_for_game
from tournament_db import db_tournament_signup_by_player
from tournament_db import db_tournament_signup_set_async_state
from tournament_db import db_tournament_signup_set_status
from tournament_db import db_tournament_signups_by_tournament
from tournament_db import db_tournament_status
import gamemodes.tournament_server as tournament_server


# ServiceTournaments — TryReconnection (25021)
def handle_tryreconnection_25021(handler, target, instance, data_type, reqid, comp, session_id,
                                 conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    # The client sends this while restoring an asynchronous tournament
    # lobby after a reconnect.  An empty response leaves TournamentManager
    # in its TournamentLobby recovery loop, so an active Corinth signup
    # must be resumed by replaying the deck-construction event.
    reconnect_tid = int(inner_obj.get("forceReconnectToAsynch", 0) or 0)
    # On a fresh socket the client may restore the tournament UI
    # before sending auth.  Do not query the database with the
    # handler's placeholder ReckID; retry once auth binds the real
    # account identity.
    if not handler.authenticated:
        handler._pending_tournament_reconnect = reconnect_tid
        reconnect_ok = bool(reconnect_tid)
        log_req(f">>> TryReconnection: deferred until auth "
                f"tid={reconnect_tid}")
        resp_inner = encode_objfmt_response(
            ["Game.Shared.Network.Tournaments.TryReconnectionToDisconnectedTournamentResponseArgs"],
            [("Error", "enum1", (
                "Game.Shared.Network.Tournaments.ETryReconnectionToDisconnectedTournamentError",
                0 if reconnect_ok else -1)),
             ("ErrorMessage", "string", "")])
        resp_body = compress_gzip(resp_inner) if comp else resp_inner
        resp_reqid = reqid | 1
        dw_bytes = encode_datawrapper(
            resp_reqid, data_type, resp_body, comp, session_id)
        handler.scnt += 1
        handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}",
                   "target": target, "instance": instance,
                   "reqid": resp_reqid, "c": comp, "conh": conh,
                   "sid": handler.sid}, dw_bytes)
        return
    reconnect_room = db_tournament_room_for_game(
        reconnect_tid, conn=_db) if reconnect_tid else None
    reconnect_uid = int(handler.client_reck_id) if handler.client_reck_id else 0
    reconnect_signup = (db_tournament_signup_by_player(
        reconnect_tid, reconnect_uid, conn=_db)
        if reconnect_tid and reconnect_uid else None)
    # A persistent Corinth event can outlive an account identity
    # migration (for example, the old Name#discriminator identity to
    # the canonical non-Steam name).  In that case the client sends
    # reconnect rather than EnterTournament.  Re-enter through the
    # normal async slot logic when a previous run is no longer live.
    if (not reconnect_signup and reconnect_room and
            _is_corinth_room(reconnect_room) and
            str(reconnect_room.get("status", "")).lower() != "closed"):
        reconnect_name = (handler.user_profile.get("name", "Unknown")
                          if handler.user_profile else "Unknown")
        join_ok, _count, _target, _type_id = tournament_server.join_tournament(
            reconnect_tid, reconnect_uid, reconnect_name, deck_id=0,
            entry_group=0)
        if join_ok:
            reconnect_signup = db_tournament_signup_by_player(
                reconnect_tid, reconnect_uid, conn=_db)
            log_req(f">>> TryReconnection: re-entered Corinth tid={reconnect_tid} "
                    f"player={reconnect_uid}")
    reconnect_live = bool(
        reconnect_signup and
        db_tournament_player_run_live(
            reconnect_tid, reconnect_uid, conn=_db))
    reconnect_ok = bool(
        reconnect_room and reconnect_signup and
        str(reconnect_room.get("status", "")).lower() != "closed" and
        _is_corinth_room(reconnect_room) and
        (reconnect_signup.get("status") == "active" or
         not reconnect_live))
    if reconnect_ok:
        # A disconnected client can keep requesting recovery after a
        # previous LeaveTournament marked its signup withdrawn.  A
        # persistent Corinth event is explicitly re-enterable once
        # that player's old run is no longer live.
        if reconnect_signup.get("status") != "active":
            db_tournament_signup_set_status(
                reconnect_tid, reconnect_uid, "active", conn=_db)
            db_tournament_signup_set_async_state(
                reconnect_tid, reconnect_uid,
                deck_ready=False, searching=False, conn=_db)
            _db.commit()
        with player_handler_lock:
            player_handlers[reconnect_uid] = handler
        reconnect_pool = db_tournament_pool(
            reconnect_tid, reconnect_uid, conn=_db)
        if len(reconnect_pool) != len(CORINTH_SHARD_GUIDS) * 4:
            db_seed_tournament_pool(
                reconnect_tid, reconnect_uid,
                CORINTH_SHARD_GUIDS * 4, conn=_db)
            reconnect_pool = db_tournament_pool(
                reconnect_tid, reconnect_uid, conn=_db)
        _push_corinth_deck_construction(
            handler, reconnect_tid, reconnect_pool)
        log_req(f">>> TryReconnection: resumed Corinth tid={reconnect_tid} "
                f"player={reconnect_uid}")
    else:
        log_req(f">>> TryReconnection: no active tournament "
                f"tid={reconnect_tid} player={reconnect_uid}")
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.TryReconnectionToDisconnectedTournamentResponseArgs"],
        [("Error", "enum1", (
            "Game.Shared.Network.Tournaments.ETryReconnectionToDisconnectedTournamentError",
            0 if reconnect_ok else -1)),
         ("ErrorMessage", "string", "" if reconnect_ok else
          "No active tournament")]
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str,
        "target": target,
        "instance": instance,
        "reqid": resp_reqid,
        "c": comp,
        "conh": conh,
        "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent Tournament reconnect response ({len(dw_bytes)}b)")


# ServiceTournaments — LadderRecord (70023)
def handle_ladderrecord_70023(handler, target, instance, data_type, reqid, comp, session_id,
                              conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> Respond LadderRecord — empty ladder")
    resp_fields = [
        ("constructedIronStars", "int", 0), ("constructedStars", "int", 0),
        ("constructedMaxStars", "int", 0), ("constructedStreak", "int", 0),
        ("CosmicRank", "int", 0), ("limitedStars", "int", 0),
        ("limitedMaxStars", "int", 0), ("limitedStreak", "int", 0),
        ("limitedCosmicRank", "int", 0),
        ("limitedSleeves", "coll", ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)),
        ("constructedSleeves", "coll", ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)),
        ("includesDebugData", "bool", False), ("MMR", "int", 0), ("LimitedMMR", "int", 0)]
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Tournaments.Messages.Tournament+LadderRecord+Response"], resp_fields)
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent LadderRecord response ({len(dw_bytes)}b)")


def handle_request_70027(handler, target, instance, data_type, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> Respond LadderTierRewardsList — empty")
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Tournaments.Messages.Tournament+LadderTierRewardsList+Response"],
        [("tier_rewards", "coll", ("System.Collections.Generic.Dictionary`2#System.UInt32!Game.Shared.Tournaments.TournamentRewardInfo", 0))])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent LadderTierRewardsList response ({len(dw_bytes)}b)")


def handle_request_70028(handler, target, instance, data_type, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> Respond LadderRankingList — empty")
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Tournaments.Messages.Tournament+LadderRankingList+Response"],
        [("ConstructedLadderRanking", "coll", ("System.Collections.Generic.List`1#System.String", 0)),
         ("LimitedLadderRanking", "coll", ("System.Collections.Generic.List`1#System.String", 0))])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent LadderRankingList response ({len(dw_bytes)}b)")


def handle_request_70015(handler, target, instance, data_type, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> PooledMsg reqflush — acking")
    resp_inner = encode_objfmt_response(
        ["Game.Shared.PooledMessaging.PooledMessagingRequestsInterface+Response"], [])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)


def handle_request_25019(handler, target, instance, data_type, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    fmt = inner_obj.get("format", "constructed")
    room_id = _hc._next_waiting_room_id()
    with _hc._waiting_room_lock:
        _hc._waiting_rooms[room_id] = {"id": room_id, "style": inner_obj.get("style", "se"),
            "format": fmt, "playerCount": int(inner_obj.get("playerCount", 2)),
            "players": {}, "status": "waiting"}
    log_req(f">>> CreateWaitingRoom: id={room_id}")
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.CreateWaitingRoomResponseArgs"], [])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent CreateWaitingRoom response ({len(dw_bytes)}b)")


# ServiceTournaments — TestTournamentEntry (25027)
def handle_testtournamententry_25027(handler, target, instance, data_type, reqid, comp, session_id,
                                     conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    tournament_id = int(inner_obj.get("TournamentID", 0) or 0)
    is_wr = bool(inner_obj.get("IsWaitingRoom", False))
    log_req(f">>> TestTournamentEntry: tid={tournament_id} wr={is_wr}")
    room = db_tournament_by_id(tournament_id)
    # Corinth is a persistent async catalog entry.  It remains in
    # ``started`` status while individual runs are matched, but its
    # registration is still open and has no entry fee.
    room_status = str(room.get("status", "")).lower() if room else ""
    room_style = str(room.get("style", "")).lower() if room else ""
    success = bool(
        room and room_status != "closed" and
        (room_status == "waiting" or
         room_style in {"async", "asynchronous"}))
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.TestTournamentEntryResponseArgs"],
        [("success", "bool", success),
         ("qualifyingEntryGroups", "intlist", ("System.Collections.Generic.List`1#System.Int32", 0, [])),
         ("Error", "enum1", ("Game.Shared.Network.Tournaments.ETestTournamentEntryError", 0)),
         ("ErrorMessage", "string", "")])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent TestTournamentEntry ({len(dw_bytes)}b)")


# ServiceTournaments — EnterTournament (25029)
def handle_entertournament_25029(handler, target, instance, data_type, reqid, comp, session_id,
                                 conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    tournament_id = int(inner_obj.get("TournamentID", 0) or 0)
    log_req(f">>> EnterTournament: tid={tournament_id}")
    player_uid = int(handler.client_reck_id) if handler.client_reck_id else 0
    player_name = (handler.user_profile.get("name", "Unknown") if handler.user_profile else "Unknown")
    deck_id = uid_instance(inner_bytes, "DeckId")
    entry_group = int(inner_obj.get("EntryGroup", 0) or 0)
    entry_room = db_tournament_room_for_game(
        tournament_id, conn=_db)
    if not _is_corinth_room(entry_room or {}):
        tournament_server.leave_all(player_uid)
    ok, count, target_p, type_id = tournament_server.join_tournament(
        tournament_id, player_uid, player_name, deck_id=deck_id, entry_group=entry_group)
    if not ok:
        signup = db_tournament_signup_by_player(tournament_id, player_uid)
        if signup:
            log_req(f"    Player already signed up")
            is_waiting_room = False
        else:
            dw_bytes = _encode_enter_tournament_error(comp, session_id, tournament_id, "InvalidTouranmentError")
            handler.scnt += 1
            handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
            return
    else:
        is_waiting_room = target_p > 1
        log_req(f"    Player {player_name} joined room {tournament_id} ({count}/{target_p}) type={type_id}")
    with player_handler_lock:
        player_handlers[player_uid] = handler
    if deck_id:
        player_decks[player_uid] = deck_id
        log_req(f"    Deck selected: {deck_id}")
    log_req(f"    EntryGroup: {entry_group}")
    for stype_id in (2, 3):
        if type_id == stype_id:
            row = db_tournament_room_for_game(tournament_id, conn=_db)
            if row:
                set_id = row.get("set_id") or "set01"
                if stype_id == 2:
                    tournament_server.generate_sealed_deck(tournament_id, player_uid, set_id)
                else:
                    tournament_server.generate_draft_deck(tournament_id, player_uid, set_id)
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.EnterTournamentResponseArgs",
         "Game.Shared.Domain.deck_bits", "System.UInt64", "System.String",
         "Game.Shared.ResourceId", "System.Guid",
         "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
         "Game.Shared.Mechanics.EDeckLock", "Game.Shared.Mechanics.EDeckPersonality", "System.Int32"],
        [("isWaitingRoom", "bool", is_waiting_room),
         ("TournamentID", "ulong", tournament_id),
         ("TournamentDeckInfo", "deckbits", _make_deck_data(deck_id if deck_id else 0)),
         ("Error", "enum1", ("Game.Shared.Network.Tournaments.EEnterTournamentError", 0))])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent EnterTournament response ({len(dw_bytes)}b)")
    for s in db_tournament_signups_by_tournament(tournament_id):
        handler = player_handlers.get(s["player_uid"])
        if handler and handler is not handler and tournament_id:
            try:
                push_tournament_room_data(handler, f"tourn:waitingroom-{tournament_id}_full", "")
                push_tournament_room_data(handler, f"tourn:tournament-{tournament_id}_full", "")
                push_tournament_room_data(handler, "tourn:lobby_full", "")
                # Also push TournamentInfo (25058) to populate m_TournamentInfos
                ti_inner = encode_objfmt_response(
                    ["Game.Shared.Network.Tournaments.TournamentInfoEventArgs",
                     "Game.Shared.Tournaments.TournamentInfo",
                     "Game.Shared.Tournaments.ETournamentStatus",
                     "Game.Shared.Tournaments.ETournamentCompletionType",
                     "System.UInt64", "System.Int32", "System.Int64", "System.Boolean"],
                    [("Info", "struct", ("Game.Shared.Tournaments.TournamentInfo", [
                        ("TournamentID", "ulong", tournament_id),
                        ("TournamentStatus", "enum1", (
                            "Game.Shared.Tournaments.ETournamentStatus", 2)),
                        ("CompletionType", "enum1", (
                            "Game.Shared.Tournaments.ETournamentCompletionType", 0)),
                        ("ResgistrationOpenTime", "long", 0),
                        ("Public", "bool", False)]))])
                ti_body = compress_gzip(ti_inner)
                ti_dw = encode_datawrapper(0, 25058, ti_body, 1, "00000000-0000-0000-0000-000000000000")
                handler.scnt += 1
                handler.send({
                    "issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{handler.scnt}",
                    "target": target, "instance": instance,
                    "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
                }, ti_dw)
                log_req(f"    Pushed TournamentInfo (25058) for tid={tournament_id} from EnterTournament")
            except Exception: pass
    corinth_entry = _is_corinth_room(entry_room or {})
    if ((corinth_entry and count >= 1) or
            (target_p > 0 and count >= target_p)):
        if corinth_entry:
            # Corinth is a persistent async event: entry itself
            # starts deck construction, while a later matchmaking
            # pass creates the two-player battle session.
            pool = db_tournament_pool(
                tournament_id, player_uid, conn=_db)
            if len(pool) != len(CORINTH_SHARD_GUIDS) * 4:
                db_seed_tournament_pool(
                    tournament_id, player_uid,
                    CORINTH_SHARD_GUIDS * 4, conn=_db)
                pool = db_tournament_pool(
                    tournament_id, player_uid, conn=_db)
            _push_corinth_deck_construction(
                handler, tournament_id, pool)
            log_req(f"    >>> Corinth entry: deck construction "
                    f"started for player {player_uid}")
            return
        log_req(f"    >>> Room {tournament_id} full — starting game")
        # Preserve the handlers found during this join.  The start
        # path sends several unsolicited events after the Enter
        # request; a shared-registry lookup can otherwise lose one
        # of the two recipients during a reconnect/concurrent join.
        room_handlers = {player_uid: handler}
        with player_handler_lock:
            for signup in db_tournament_signups_by_tournament(tournament_id):
                signup_pid = int(signup["player_uid"])
                room_handlers.setdefault(
                    signup_pid, player_handlers.get(signup_pid))
        start_waiting_room_game(
            tournament_id, handler_overrides=room_handlers)


def handle_request_25039(handler, target, instance, data_type, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    # GameEntrance is the async-gauntlet matchmaking boundary.  A
    # saved Corinth deck is applied to the session only when the
    # player starts searching; cancelling search leaves the pool.
    player_uid = int(handler.client_reck_id) if handler.client_reck_id else 0
    tournament_id = int(inner_obj.get("TournamentID", 0) or 0)
    searching = bool(inner_obj.get("searching", False))
    room = db_tournament_room_for_game(tournament_id, conn=_db)
    pool = db_tournament_pool(tournament_id, player_uid, conn=_db)
    main_ids = [card_uid for card_uid, _guid, location in pool
                if int(location) == 0]
    corinth_entry = _is_corinth_room(room or {})
    ok = bool(room and (int(room.get("format") or 0) &
                        int(ETournamentFormats.Iconoclast)))
    match = None
    if corinth_entry:
        if searching:
            active_session = None
            # A client can return to the deckbuilder after the
            # battle connection was lost and press Play Game again.
            # That player is already assigned to an unfinished match;
            # do not put them back into the queue (the matcher will
            # correctly exclude them) and do not leave searching=1
            # falsely displayed in the lobby.
            active_match = db_tournament_match_session(
                tournament_id, player_uid, conn=_db)
            if active_match:
                other_pid = next(
                    (int(pid) for pid in db_game_session_pids(
                        int(active_match[0])) if int(pid) != player_uid),
                    0)
                other_signup = db_tournament_signup_by_player(
                    tournament_id, other_pid, conn=_db)
                other_searching = bool(
                    other_signup and other_signup.get("status") == "active"
                    and int(other_signup.get("searching", 0) or 0))
                if other_searching:
                    # Both players explicitly pressed Play Game after
                    # declining the stale reconnect. Discard only this
                    # unfinished assignment and its game state; it is
                    # not a win or loss in the async event.
                    db_tournament_discard_match(
                        tournament_id, active_match[0], conn=_db)
                    db_delete_game_session(active_match[0], conn=_db)
                    _db.commit()
                    log_req(
                        f">>> GameEntrance: discarded stale Corinth "
                        f"match session={active_match[0]} for new search")
                    active_match = None
                else:
                    # Wait for the opponent's explicit new-search
                    # request; do not silently reconnect this client.
                    db_tournament_signup_set_async_state(
                        tournament_id, player_uid,
                        deck_ready=True, searching=True, conn=_db)
                    _db.commit()
                    ok = True
                    active_match = None
                    log_req(
                        f">>> GameEntrance: player {player_uid} "
                        f"requested new search; awaiting opponent")
            else:
                active_session = None
            if active_session:
                # The existing match has been re-announced above;
                # skip new-search setup for this request.
                pass
            else:
                ok = ok and len(main_ids) == 20
                if ok:
                    # Corinth is a persistent async event. A prior
                    # LeaveTournament marks the signup withdrawn, but
                    # an explicit Play Game/Find Player action is a
                    # new search request and must re-enter that queue.
                    signup = db_tournament_signup_by_player(
                        tournament_id, player_uid, conn=_db)
                    if signup and signup.get("status") != "active":
                        db_tournament_signup_set_status(
                            tournament_id, player_uid, "active", conn=_db)
                    db_tournament_signup_set_async_state(
                        tournament_id, player_uid,
                        deck_ready=True, searching=True,
                        conn=_db)
                    _db.commit()
                    try_start_corinth_match(
                        tournament_id, handler_override=handler)
        else:
            # Cancelling the search leaves the saved deck/pool
            # intact but removes this entrant from the match queue.
            db_tournament_signup_set_async_state(
                tournament_id, player_uid, searching=False, conn=_db)
            _db.commit()
        # Searching is a valid asynchronous state even when no
        # opponent is available yet; the match transition is pushed
        # later when the second ready player arrives.
    elif searching:
        ok = ok and len(main_ids) == 20
        match = (db_tournament_match_session(
            tournament_id, player_uid, conn=_db)
                  if ok else None)
        ok = ok and bool(match)
        if ok:
            db_apply_tournament_main_deck(
                match[0], player_uid, main_ids, conn=_db)
    log_req(f">>> GameEntrance (dt=25039) tid={tournament_id} "
            f"player={player_uid} searching={searching} ok={ok}")
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.GameEntranceResponseArgs",
         "System.Boolean"], [("success", "bool", bool(ok))])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body,
                                  comp, session_id)
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid | 1}",
        "target": target, "instance": instance, "reqid": reqid | 1,
        "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    # Corinth matchmaking is started inside try_start_corinth_match
    # once two ready players exist; that path pushes the session-start
    # events to both handlers.  Do not index ``match`` for the normal
    # one-player-waiting case.
    if ok and searching and match:
        push_tournament_session_start(
            handler, tournament_id, int(match[0]),
            f"tourney-{tournament_id}", 0, room)


def handle_request_25011(handler, target, instance, data_type, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    # SaveTournamentDeck: the client sends the complete deck_bits
    # snapshot.  Corinth owns its physical pool in tournament_pool;
    # this request is the authoritative main/sideboard transition.
    player_uid = int(handler.client_reck_id) if handler.client_reck_id else 0
    tournament_id = int(inner_obj.get("TournamentID", 0) or 0)
    deck = inner_obj.get("TournamentDeck")
    if not isinstance(deck, dict):
        deck = inner_obj
    main_cards = _hc._tournament_deck_card_ids(deck, "CardsInDeck")
    side_cards = _hc._tournament_deck_card_ids(deck, "CardsInSideboard")
    room = db_tournament_room_for_game(tournament_id, conn=_db)
    pool = db_tournament_pool(tournament_id, player_uid, conn=_db)
    pool_by_id = {card_uid: template for card_uid, template, _loc in pool}
    submitted = main_cards + side_cards
    submitted_ids = [card_uid for card_uid, _guid in submitted]
    valid = bool(room and (int(room.get("format") or 0) &
                           int(ETournamentFormats.Iconoclast)))
    valid = valid and len(main_cards) == 20
    valid = valid and len(submitted_ids) == len(set(submitted_ids))
    valid = valid and set(submitted_ids) == set(pool_by_id)
    valid = valid and all(
        pool_by_id[card_uid] == guid
        for card_uid, guid in submitted if card_uid in pool_by_id)
    if valid:
        db_tournament_pool_replace(
            tournament_id, player_uid,
            [(card_uid, guid, 0) for card_uid, guid in main_cards] +
            [(card_uid, guid, 1) for card_uid, guid in side_cards],
            conn=_db)
        if _is_corinth_room(room or {}):
            db_tournament_signup_set_async_state(
                tournament_id, player_uid,
                deck_ready=True, searching=False, conn=_db)
    log_req(f">>> SaveTournamentDeck (dt=25011) tid={tournament_id} "
            f"player={player_uid} main={len(main_cards)} "
            f"side={len(side_cards)} valid={valid}")
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.SaveTournamentDeckResponseArgs",
         "System.Boolean"],
        [("Success", "bool", bool(valid))])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body,
                                  comp, session_id)
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid | 1}",
        "target": target, "instance": instance, "reqid": reqid | 1,
        "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)


def handle_request_25005(handler, target, instance, data_type, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> UnsubscribeDescriptionListener — ack")
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.UnsubscribeDescriptionListenerResponseArgs"], [])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent UnsubscribeDescriptionListener response ({len(dw_bytes)}b)")


def handle_request_25007(handler, target, instance, data_type, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    tournament_id = int(inner_obj.get("TournamentID", 0))
    log_req(f">>> GetTournamentInfo: tid={tournament_id}")
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Tournaments.GetTournamentInfoResponse",
         "Game.Shared.Tournaments.TournamentInfo",
         "System.UInt64",
         "Game.Shared.Network.Tournaments.EGetTournamentInfoError",
         "System.String"],
        [("Results", "struct", ("Game.Shared.Tournaments.TournamentInfo", [
            ("TournamentID", "ulong", tournament_id)])),
         ("Error", "enum1", ("Game.Shared.Network.Tournaments.EGetTournamentInfoError", 0)),
         ("ErrorMessage", "string", "")])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent GetTournamentInfo response ({len(dw_bytes)}b)")


def handle_request_25035(handler, target, instance, data_type, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> LeaveWaitingRoom")
    player_uid = int(handler.client_reck_id) if handler.client_reck_id else 0
    tournament_server.leave_all(player_uid)
    resp_inner = encode_objfmt_response(["Game.Shared.Network.Tournaments.LeaveWaitingRoomResponseArgs"], [])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent LeaveWaitingRoom response ({len(dw_bytes)}b)")


def handle_request_25031(handler, target, instance, data_type, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> LeaveTournament")
    player_uid = int(handler.client_reck_id) if handler.client_reck_id else 0
    tournament_id = int(inner_obj.get("TournamentID", 0) or 0)
    active_tournament_session = db_tournament_match_session(
        tournament_id, player_uid, conn=_db) if tournament_id else None
    forfeit_recorded = bool(
        tournament_id and record_tournament_forfeit(
            tournament_id, player_uid, handler))
    if forfeit_recorded:
        log_req(f"    Recorded tournament forfeit: tid={tournament_id} "
                f"loser={player_uid}")
        status = db_tournament_status(tournament_id, conn=_db)
        if (status and str(status).lower() == "complete"
                and active_tournament_session):
            db_delete_game_session(active_tournament_session[0])
            log_req(f"    Tournament complete: cleaned PvP session "
                    f"{active_tournament_session[0]}")
    # record_tournament_forfeit already applies the async run-limit
    # retirement and deckbuilder transition.  Applying a generic
    # withdrawal afterwards would turn the returned player back into
    # a withdrawn signup and leave the client in the lobby.
    if tournament_id and not forfeit_recorded:
        db_tournament_retire_player_run(
            tournament_id, player_uid, conn=_db)
    tournament_server.leave_all(player_uid)
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.LeaveTournamentResponseArgs",
         "System.UInt64"],
        [("success", "bool", True),
         ("TournamentID", "ulong", tournament_id)])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
    handler.scnt += 1
    handler.send({"issuer": f"0.0.0.0.ServiceTournaments.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{reqid|1}",
               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent LeaveTournament response ({len(dw_bytes)}b)")    



LEGACY_HANDLERS = {
    25021: handle_tryreconnection_25021,
    70023: handle_ladderrecord_70023,
    70027: handle_request_70027,
    70028: handle_request_70028,
    70015: handle_request_70015,
    25019: handle_request_25019,
    25027: handle_testtournamententry_25027,
    25029: handle_entertournament_25029,
    25039: handle_request_25039,
    25011: handle_request_25011,
    25005: handle_request_25005,
    25007: handle_request_25007,
    25035: handle_request_25035,
    25031: handle_request_25031,
}
