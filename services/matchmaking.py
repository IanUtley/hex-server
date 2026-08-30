"""Matchmaking queue — pairs searching players and acknowledges the match.

Also handles direct friend challenges via SendQuickMatchChallenge (4013)
and SendChallengeResponse (4017).
"""

import io, struct, uuid, threading, sys
from binascii import hexlify, unhexlify

from encoder import encode_objfmt_response, compress_gzip, encode_datawrapper, make_uid
from db import _db, log_req
from objfmt_builder import ObjFmtBuilder

_queue = {}       # ladderId → list of (handler, player_uid, player_name)
_lock = threading.Lock()

# Pending challenge matches: {match_guid_str: {"challenger_name": ..., "challenger_deck_id": ..., "challenger_handler": ...}}
_pending_challenges = {}
_pending_lock = threading.Lock()


def _active_clients():
    return sys.modules.get("hconnect_server", sys.modules.get("__main__"))._active_clients


# ── Ladder matchmaking (existing) ────────────────────────────────────────────

def handle_ladder_find_match(handler, target, instance, reqid, comp,
                             session_id, conh, inner_obj, inner_bytes,
                             log_req, **_kw):
    ladder_id = int(inner_obj.get("ladderId", 0) or 0)
    searching = bool(inner_obj.get("searching", False))
    player_uid = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0
    player_name = (handler.user_profile.get("name", "Unknown")
                   if handler.user_profile else "Unknown")

    log_req(f">>> LadderFindMatch: ladderId={ladder_id} searching={searching}"
            f" player={player_name}")

    if not searching:
        with _lock:
            q = _queue.get(ladder_id, [])
            _queue[ladder_id] = [(h, u, n) for h, u, n in q if u != player_uid]
        _send_ladder_response(handler, target, instance, reqid, comp, session_id, conh)
        log_req("    Cancelled search")
        return

    with _lock:
        q = _queue.get(ladder_id, [])
        if q:
            opponent_h, opponent_uid, opponent_name = q.pop(0)
            _queue[ladder_id] = q
            log_req(f"    Paired {player_name} vs {opponent_name}")
            _send_ladder_response(handler, target, instance, reqid, comp, session_id, conh)
            _send_ladder_response(opponent_h, target, instance, reqid, comp, session_id, conh)
        else:
            q.append((handler, player_uid, player_name))
            _queue[ladder_id] = q
            log_req(f"    Queued ({len(q)} waiting)")


def _send_ladder_response(handler, target, instance, reqid, comp, session_id, conh):
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Tournaments.Messages.Tournament+LadderFindMatch+Response"],
        [])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 70022, resp_body, comp, session_id)
    issuer = f"0.0.0.0.ServiceTournaments.{handler.client_uid}" if hasattr(handler, 'client_uid') else ""
    handler.scnt += 1
    handler.send({
        "issuer": issuer, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)


# ── Direct challenge handlers ────────────────────────────────────────────────

def _get_service_uid(handler):
    """Return the matchmaking service UID."""
    from hconnect_server import SERVICE_MATCHMAKING_UID
    return SERVICE_MATCHMAKING_UID


def _extract_field_bytes(inner_bytes, field_name):
    """Extract a raw field value from ObjFmt bytes by field name.
    
    Returns (field_type, raw_value_bytes) or (None, None).
    For strings: raw_value is the string bytes (without length prefix).
    For ints: raw_value is the hex string bytes.
    """
    if not isinstance(inner_bytes, bytes):
        return None, None
    pos = inner_bytes.find(field_name if isinstance(field_name, bytes) else field_name.encode())
    if pos < 0:
        return None, None
    rest = inner_bytes[pos + max(len(field_name), 1):]
    parts = rest.split(b";", 6)
    if len(parts) < 5:
        return None, None
    # parts[0] = size_idx, parts[1] = type_code, parts[2] = numProps, parts[3] = field_content...
    field_type = parts[2]
    num_props = int(parts[3]) if parts[3].isdigit() else 0
    if num_props > 0:
        # Struct type — need to go deeper
        return parts[1].decode("ascii", errors="replace"), rest
    # For string: parts[4] = length, parts[5:] = value
    # For int/ulong: parts[4] = hex value before next ;
    return parts[1].decode("ascii", errors="replace"), parts[4] if len(parts) > 4 else b""


def _extract_str(inner_bytes, field_name):
    """Extract a string field value from raw ObjFmt bytes."""
    if not isinstance(inner_bytes, bytes):
        return ""
    fname = field_name.encode() if isinstance(field_name, str) else field_name
    pos = inner_bytes.find(fname)
    if pos < 0:
        return ""
    rest = inner_bytes[pos + len(fname):]
    parts = rest.split(b";", 7)
    if len(parts) < 6:
        return ""
    try:
        strlen = int(parts[4])
        return parts[5][:strlen].decode("utf-8", errors="replace")
    except (ValueError, IndexError):
        return ""


def _extract_int(inner_bytes, field_name):
    """Extract an int field value (hex LE) from raw ObjFmt bytes."""
    if not isinstance(inner_bytes, bytes):
        return 0
    fname = field_name.encode() if isinstance(field_name, str) else field_name
    pos = inner_bytes.find(fname)
    if pos < 0:
        return 0
    rest = inner_bytes[pos + len(fname):]
    parts = rest.split(b";", 6)
    if len(parts) < 5:
        return 0
    try:
        return struct.unpack("<i", unhexlify(parts[4]))[0]
    except (ValueError, IndexError):
        return 0


def _extract_uid(inner_bytes, field_name):
    """Extract a UID field (ulong m_UID64) from raw ObjFmt bytes."""
    if not isinstance(inner_bytes, bytes):
        return 0
    fname = field_name.encode() if isinstance(field_name, str) else field_name
    pos = inner_bytes.find(fname)
    if pos < 0:
        return 0
    # Skip to m_UID64
    rest = inner_bytes[pos:]
    idx = rest.find(b"m_UID64")
    if idx < 0:
        return 0
    rest2 = rest[idx + 7:]
    parts = rest2.split(b";", 6)
    if len(parts) < 5:
        return 0
    try:
        return struct.unpack("<Q", unhexlify(parts[4]))[0]
    except (ValueError, IndexError):
        return 0


def _extract_guid(inner_bytes, field_name):
    """Extract a Guid field from raw ObjFmt bytes."""
    if not isinstance(inner_bytes, bytes):
        return "00000000-0000-0000-0000-000000000000"
    fname = field_name.encode() if isinstance(field_name, str) else field_name
    pos = inner_bytes.find(fname)
    if pos < 0:
        return "00000000-0000-0000-0000-000000000000"
    rest = inner_bytes[pos + len(fname):]
    # Skip to the guid string: after type_idx, numProps(0), and length prefix
    parts = rest.split(b";", 6)
    if len(parts) < 6:
        return "00000000-0000-0000-0000-000000000000"
    try:
        strlen = int(parts[4])
        return parts[5][:strlen].decode("ascii", errors="replace")
    except (ValueError, IndexError):
        return "00000000-0000-0000-0000-000000000000"


# ── Encode: FoundChallengeMatchEventArgs (4027 push) ─────────────────────────

def encode_found_challenge_match(match_id, keep_name, filter_id_guid, deck_format):
    """Encode FoundChallengeMatchEventArgs in ObjFmt.
    
    Fields (6):
      - matchID (Guid)
      - KeepName (string)
      - FilterID (ResourceId → guid sub-field)
      - DeckFormat (int)
      - RequestHandlerSessionId (Guid, auto-set by client from header)
      - OriginClusterHash (int, auto-set by client)
    """
    b = ObjFmtBuilder("Game.Shared.Network.Matchmaking.FoundChallengeMatchEventArgs")
    b.field_guid("matchID", match_id)
    b.field_str("KeepName", keep_name)
    b.field_resource_id("FilterID", filter_id_guid)
    b.field_int("DeckFormat", deck_format)
    return b.finish(6)


# ── Encode: SendQuickMatchChallengeResponse (4013 response) ──────────────────

def encode_send_quick_match_challenge_response(match_id, error_code=0):
    """Encode SendQuickMatchChallengeResponse.
    
    Fields (4): matchId, Status, Error, ErrorMessage
    Error and ErrorMessage have DataMember(Order=100/101).
    """
    b = ObjFmtBuilder("Game.Client.Network.Matchmaking.SendQuickMatchChallengeResponse")
    b.field_guid("matchId", match_id)
    b.field_enum("Status", "Game.Shared.Network.Matchmaking.EMMRequestMatchResponse", 0)  # Ok
    b.field_enum("Error", "Game.Shared.Network.Matchmaking.ESendQuickMatchChallengeError", error_code)
    b.field_str("ErrorMessage", "")
    return b.finish(4)


# ── Encode: SendChallengeResponseResponse (4017 response) ────────────────────

def encode_send_challenge_response_response(status):
    """Encode SendChallengeResponseResponse.
    
    Fields (3): status, Error, ErrorMessage
    Error/ErrorMessage have DataMember(Order=100/101).
    """
    b = ObjFmtBuilder("Game.Client.Network.Matchmaking.SendChallengeResponseResponse")
    b.field_enum("status", "Game.Shared.Network.Matchmaking.EMMPendingMatchResponse", status)
    b.field_enum("Error", "Game.Shared.Network.Matchmaking.ESendChallengeResponseError", 0)
    b.field_str("ErrorMessage", "")
    return b.finish(3)


# ── Encode: SendChallangeSessionEventArgs (4028 push) ────────────────────────

def encode_send_challenge_session(session_id_uid, session_name):
    """Encode SendChallangeSessionEventArgs in ObjFmt with inline SessionState.
    
    Uses encode_objfmt_response with struct nesting for proper SessionState encoding.
    Returns the full ObjFmt bytes with size table.
    """
    return encode_objfmt_response(
        ["Game.Shared.Network.Matchmaking.SendChallangeSessionEventArgs",
         "Game.Shared.SessionState",
         "Game.Shared.UID", "System.String", "System.Int32",
         "Game.Shared.SessionStateEncounterData", "System.Boolean"],
        [("state", "struct", ("Game.Shared.SessionState", [
            ("SessionId", "uid", session_id_uid),
            ("SessionName", "string", session_name),
            ("MinimumPlayerCount", "int", 2),
            ("MaximumPlayerCount", "int", 2),
            ("EncounterData", "class", "Game.Shared.SessionStateEncounterData"),
            ("JoinInsteadOfReconnect", "bool", True),
        ]))])


# ── Push helpers ─────────────────────────────────────────────────────────────

def _push_matchmaking_event(handler, data_type, args_bytes):
    """Push a server-initiated matchmaking event to a client via ServiceMatchmaking."""
    compressed = compress_gzip(args_bytes)
    dw = encode_datawrapper(0, data_type, compressed, 1)
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceMatchmaking.{_get_service_uid(handler)}.{handler.scnt}",
        "target": "ServiceMatchmaking", "instance": "Shared",
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }, dw)
    log_req(f">>> PUSH matchmaking event dt={data_type} to target={handler.user_profile.get('name', '?') if handler.user_profile else '?'} ({len(dw)}b)")


def _send_mm_response(handler, target, instance, reqid, comp, session_id,
                      conh, data_type, resp_inner):
    """Send a matchmaking response back to the requester."""
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    mm_uid = _get_service_uid(handler)
    issuer = f"0.0.0.0.ServiceMatchmaking.{mm_uid}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent MM response dt={data_type} ({len(dw_bytes)}b)")


# ── Handler: SendQuickMatchChallenge (4013) ──────────────────────────────────

def handle_send_quick_match_challenge(handler, target, instance, reqid, comp,
                                       session_id, conh, inner_obj, inner_bytes,
                                       log_req, **_kw):
    """Handle DT 4013 — a player challenged a friend to a quick match."""
    opponent_name = _extract_str(inner_bytes, "OpponentKeepName")
    challenger_deck_id = _extract_uid(inner_bytes, "DeckID")
    deck_format = _extract_int(inner_bytes, "DeckFormat")
    filter_id_guid = _extract_guid(inner_bytes, "FilterID")

    player_name = (handler.user_profile.get("name", "Unknown")
                   if handler.user_profile else "Unknown")
    player_id = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0

    log_req(f">>> SendQuickMatchChallenge: {player_name} → {opponent_name}"
            f" deckFormat={deck_format} deckID={challenger_deck_id}")

    # Look up opponent in DB
    opp_row = _db.execute(
        "SELECT id, name FROM users WHERE LOWER(name)=LOWER(?) LIMIT 1",
        (opponent_name,)).fetchone()

    if not opp_row:
        log_req(f"    Opponent {opponent_name} not found")
        resp_inner = encode_send_quick_match_challenge_response(
            "00000000-0000-0000-0000-000000000000")
        _send_mm_response(handler, target, instance, reqid, comp, session_id,
                          conh, 4013, resp_inner)
        return

    opp_id = opp_row[0]
    opp_name = opp_row[1]
    match_id = str(uuid.uuid4())

    # Store pending challenge
    with _pending_lock:
        _pending_challenges[match_id] = {
            "challenger_name": player_name,
            "challenger_id": player_id,
            "challenger_handler": handler,
            "challenger_deck_id": challenger_deck_id,
            "deck_format": deck_format,
            "opponent_name": opp_name,
            "opponent_id": opp_id,
        }

    # Push FoundChallengeMatch (4027) to opponent
    opp_clients = _active_clients().get(opp_id, [])
    log_req(f"    Pushing FoundChallengeMatch to {opp_name} (id={opp_id}, clients={len(opp_clients)})")

    for opp_h, _ in opp_clients:
        try:
            # First, send a dummy ping response to establish the MatchmakingService
            # connection on God's client (required for push events to be received).
            ping_args = encode_ping_matchmaking_response()
            _push_matchmaking_event(opp_h, 4001, ping_args)
            
            args = encode_found_challenge_match(
                match_id, player_name, filter_id_guid, deck_format)
            _push_matchmaking_event(opp_h, 4027, args)
            log_req(f"    FoundChallengeMatch pushed to {opp_name}")
        except Exception as e:
            log_req(f"    FoundChallengeMatch push failed: {e}")

    # Send response to challenger
    resp_inner = encode_send_quick_match_challenge_response(match_id)
    _send_mm_response(handler, target, instance, reqid, comp, session_id,
                      conh, 4013, resp_inner)
    log_req(f"    Challenge sent, matchID={match_id}")


# ── Handler: SendChallengeResponse (4017) ────────────────────────────────────

def handle_send_challenge_response(handler, target, instance, reqid, comp,
                                    session_id, conh, inner_obj, inner_bytes,
                                    log_req, **_kw):
    """Handle DT 4017 — opponent responded to a challenge (accept/decline)."""
    accepted = False
    if isinstance(inner_bytes, bytes):
        pos = inner_bytes.find(b"Accepted")
        if pos >= 0:
            rest = inner_bytes[pos + len(b"Accepted"):]
            parts = rest.split(b";", 6)
            if len(parts) >= 5:
                # bool is raw '1' or '0', no hex encoding
                accepted = (parts[4] == b"1" or parts[4] == b"1")

    match_id = _extract_guid(inner_bytes, "matchID") if isinstance(inner_bytes, bytes) else ""
    respondent_deck_id = _extract_uid(inner_bytes, "DeckID")

    player_name = (handler.user_profile.get("name", "Unknown")
                   if handler.user_profile else "Unknown")
    player_id = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0

    log_req(f">>> SendChallengeResponse: {player_name} match={match_id} accepted={accepted}")

    # Accept the bool correctly
    if isinstance(inner_bytes, bytes) and b"Accepted" in inner_bytes:
        p = inner_bytes.find(b"Accepted")
        r = inner_bytes[p + len(b"Accepted"):]
        s = r.split(b";", 6)
        if len(s) >= 5 and s[4] == b"1":
            accepted = True

    with _pending_lock:
        pending = _pending_challenges.pop(match_id, None)

    if not pending:
        log_req(f"    No pending challenge for matchID={match_id}")
        resp_inner = encode_send_challenge_response_response(1)  # Failed
        _send_mm_response(handler, target, instance, reqid, comp, session_id,
                          conh, 4017, resp_inner)
        return

    if not accepted:
        log_req(f"    Challenge declined by {player_name}")
        resp_inner = encode_send_challenge_response_response(3)  # RejectedByPlayer
        _send_mm_response(handler, target, instance, reqid, comp, session_id,
                          conh, 4017, resp_inner)
        # Notify challenger that their challenge was declined
        chall_h = pending["challenger_handler"]
        if chall_h:
            try:
                chall_resp = encode_send_challenge_response_response(3)
                _push_matchmaking_event(chall_h, 4017, chall_resp)
            except Exception as e:
                log_req(f"    Decline push to challenger failed: {e}")
        return

    # Challenge accepted — send response to respondent FIRST
    resp_inner = encode_send_challenge_response_response(0)  # Accepted
    _send_mm_response(handler, target, instance, reqid, comp, session_id,
                      conh, 4017, resp_inner)

    # Create game session
    from game_session import create_encounter_session
    session = create_encounter_session(
        f"Challenge_{pending['challenger_name']}_vs_{pending['opponent_name']}",
        {},
        make_uid(244, pending["challenger_id"])
    )
    session.add_player(make_uid(244, pending["opponent_id"]), 1)
    session.set_state("joined")
    sess_uid = session.session_id

    log_req(f"    Game session {sess_uid} created for challenge")

    # Encode SendChallangeSession (4028)
    args_4028 = encode_send_challenge_session(sess_uid, session.session_name)

    # Push 4028 to BOTH players
    chall_h = pending["challenger_handler"]
    chall_name = pending["challenger_name"]

    # Push to challenger
    if chall_h:
        try:
            _push_matchmaking_event(chall_h, 4028, args_4028)
            log_req(f"    SendChallangeSession (4028) pushed to {chall_name}")
        except Exception as e:
            log_req(f"    SendChallangeSession push to {chall_name} failed: {e}")

    # Push to respondent
    try:
        _push_matchmaking_event(handler, 4028, args_4028)
        log_req(f"    SendChallangeSession (4028) pushed to {player_name}")
    except Exception as e:
        log_req(f"    SendChallangeSession push to {player_name} failed: {e}")

    log_req(f"    Challenge accepted — game session started")


# ── Handler: PingMatchmakingServer (4001) — stub to acknowledge service alive ─

def encode_ping_matchmaking_response():
    """Encode a PingMatchmakingServerResponse.
    
    Fields (3): Timestamp, Error, ErrorMessage
    Error/ErrorMessage have DataMember(Order=100/101).
    """
    import time
    b = ObjFmtBuilder("Game.Client.Network.Matchmaking.PingMatchmakingServerResponse")
    b.field_datetime("Timestamp", time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime()))
    b.field_enum("Error", "Game.Shared.Network.Matchmaking.EPingMatchmakingServerError", 0)
    b.field_str("ErrorMessage", "")
    return b.finish(3)


def handle_ping_matchmaking(handler, target, instance, reqid, comp,
                            session_id, conh, inner_obj, inner_bytes,
                            log_req, **_kw):
    """Handle DT 4001 — client pings matchmaking server."""
    log_req(f">>> PingMatchmakingServer")
    resp_inner = encode_ping_matchmaking_response()
    _send_mm_response(handler, target, instance, reqid, comp, session_id,
                      conh, 4001, resp_inner)
