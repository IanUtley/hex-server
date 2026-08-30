"""Friend / social service handlers.

Request handlers (client → server):
    2149  AddFriend         — send a friend request
    2157  AcceptFriendRequest  — accept a pending friend request
    2159  IgnoreFriendRequest  — decline/ignore a pending friend request
    2161  RemoveFriend      — remove someone from your friend list
    2163  IgnorePlayer      — add a player to your ignore list
    2165  UnignorePlayer    — remove a player from your ignore list

Event pushes (server → client):
    2192  FriendRequestReceived
    2193  FriendRequestAccepted
    2194  FriendAdded
    2195  FriendRemoved
    2196  IgnoredListArrived
    2197  PlayerIgnored
    2198  PlayerUnignored
    2199  PendingFriendRequestsArrived
    2200  FriendRequestRemoved
    2202  FriendsListArrived
    2203  FriendComeOnline
    2204  FriendGoesOffline
"""

import io, struct, time, traceback, sys
from binascii import hexlify

from db import _db, log_req
from db import (db_get_friends, db_get_pending_friend_requests,
                db_get_ignored_list, db_send_friend_request,
                db_accept_friend_request, db_ignore_friend_request,
                db_remove_friend, db_ignore_player, db_unignore_player)
from encoder import compress_gzip, encode_datawrapper, make_uid
from objfmt_builder import ObjFmtBuilder


def _active_clients():
    """Return the _active_clients dict from hconnect_server without circular import."""
    return sys.modules.get("hconnect_server", sys.modules.get("__main__"))._active_clients


def _extract_keepname(inner_bytes):
    """Extract the KeepName / Username string from raw ObjFmt request bytes."""
    if not isinstance(inner_bytes, bytes):
        return ""
    for field in (b"KeepName", b"Username"):
        pos = inner_bytes.find(field)
        if pos < 0:
            continue
        rest = inner_bytes[pos + len(field):]
        parts = rest.split(b";", 5)
        if len(parts) >= 5:
            try:
                strlen = int(parts[4])
                return parts[5][:strlen].decode("utf-8", errors="replace")
            except (ValueError, IndexError):
                pass
    return ""


def _send_response(handler, data_type, resp_inner, comp, session_id,
                   reqid, target, instance, conh,
                   SERVICE_PROFILE_UID):
    """Wrap, compress, and send a response DataWrapper to the client."""
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body,
                                  comp, session_id)
    issuer_str = (f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
                  f"ServicePlayer.{handler.client_uid}.{resp_reqid}")
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    return len(dw_bytes)


def _push_event(handler, data_type, args_bytes, SERVICE_PROFILE_UID):
    """Push a server-initiated event (reqid=0) to a client."""
    compressed = compress_gzip(args_bytes)
    dw = encode_datawrapper(0, data_type, compressed, 1)
    issuer = (f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
              f"ServicePlayer.{handler.client_uid}.{handler.scnt}")
    handler.scnt += 1
    handler.send({
        "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }, dw)
    log_req(f">>> PUSH social event dt={data_type} ({len(dw)}b)")


# ── Request handlers ────────────────────────────────────────────────────────

def handle_add_friend(handler, target, instance, reqid, comp, session_id,
                      conh, inner_obj, inner_bytes, SERVICE_PROFILE_UID, **_kw):
    """AddFriend (2149) — send a friend request to *Username*."""
    username = _extract_keepname(inner_bytes)
    if not username or not handler.user_profile:
        b = ObjFmtBuilder("Game.Client.Network.Profile.AddFriendResponse")
        b.field_str("Username", username or "")
        b.field_enum("Code", "Game.Shared.EAddFriendResponseCode", 0)  # UserDoesNotExist
        b.field_enum("Error", "Game.Shared.Network.Profile.EAddFriendError", 0)
        b.field_str("ErrorMessage", "")
        resp_inner = b.finish(4)
        _send_response(handler, 2149, resp_inner, comp, session_id,
                       reqid, target, instance, conh, SERVICE_PROFILE_UID)
        log_req(f"    AddFriend: empty username or no profile")
        return

    user_id = handler.user_profile["id"]
    from application.commands import SocialMutationCommand
    success, code, to_uid = handler._application.execute(
        SocialMutationCommand(user_id, "add_friend", username)).value
    log_req(f">>> AddFriend {user_id} -> {username}: {code}")

    # Map response code to enum index
    code_map = {
        "UserDoesNotExist": 0,
        "SelfAdd": 1,
        "RequestAlreadySent": 2,
        "RequestAlreadyReceived": 3,
        "Success": 4,
    }
    code_idx = code_map.get(code, 0)

    b = ObjFmtBuilder("Game.Client.Network.Profile.AddFriendResponse")
    b.field_str("Username", username)
    b.field_enum("Code", "Game.Shared.EAddFriendResponseCode", code_idx)
    b.field_enum("Error", "Game.Shared.Network.Profile.EAddFriendError", 0)
    b.field_str("ErrorMessage", "")
    resp_inner = b.finish(4)
    _send_response(handler, 2149, resp_inner, comp, session_id,
                   reqid, target, instance, conh, SERVICE_PROFILE_UID)

    if success and to_uid:
        # Push FriendRequestReceived to the target player if online
        
        targets = _active_clients().get(to_uid, [])
        log_req(f"    FriendRequestReceived: to_uid={to_uid} active_clients_hit={len(targets)}")
        for target_h, _ in targets:
            try:
                push_friend_request_received(target_h, handler.user_profile["name"],
                                            SERVICE_PROFILE_UID)
            except Exception as e:
                log_req(f"    FriendRequestReceived push failed: {e}\n{traceback.format_exc()}")


def handle_accept_friend_request(handler, target, instance, reqid, comp,
                                 session_id, conh, inner_obj, inner_bytes,
                                 SERVICE_PROFILE_UID, **_kw):
    """AcceptFriendRequest (2157) — accept a pending friend request from *KeepName*."""
    keepname = _extract_keepname(inner_bytes)
    user_id = handler.user_profile["id"] if handler.user_profile else 0
    from application.commands import SocialMutationCommand
    success, from_uid = (handler._application.execute(
        SocialMutationCommand(user_id, "accept_friend", keepname)).value
        if keepname else (False, None))
    log_req(f">>> AcceptFriendRequest {user_id} <- {keepname}: {'ok' if success else 'fail'}")

    b = ObjFmtBuilder("Game.Client.Network.Profile.AcceptFriendRequestResponse")
    b.field_enum("Error", "Game.Shared.Network.Profile.EAcceptFriendRequestError", 0)
    b.field_str("ErrorMessage", "")
    resp_inner = b.finish(2)
    _send_response(handler, 2157, resp_inner, comp, session_id,
                   reqid, target, instance, conh, SERVICE_PROFILE_UID)

    if success and from_uid:
        # Push FriendAdded with IsOnline=True to the accepting player
        push_friend_added(handler, from_uid, keepname, True, SERVICE_PROFILE_UID)
        # Push FriendAdded to the other player if online
        
        my_name = handler.user_profile["name"] if handler.user_profile else keepname
        my_id = handler.user_profile["id"] if handler.user_profile else user_id
        for other_h, _ in _active_clients().get(from_uid, []):
            try:
                push_friend_added(other_h, my_id, my_name, True, SERVICE_PROFILE_UID)
                log_req(f"    FriendAdded pushed to {from_uid}")
            except Exception as e:
                log_req(f"    FriendAdded push to {from_uid} failed: {e}")


def handle_ignore_friend_request(handler, target, instance, reqid, comp,
                                  session_id, conh, inner_obj, inner_bytes,
                                  SERVICE_PROFILE_UID, **_kw):
    """IgnoreFriendRequest (2159) — decline a pending friend request."""
    keepname = _extract_keepname(inner_bytes)
    user_id = handler.user_profile["id"] if handler.user_profile else 0
    from application.commands import SocialMutationCommand
    success, from_uid = (handler._application.execute(
        SocialMutationCommand(user_id, "ignore_friend_request", keepname)).value
        if keepname else (False, None))
    log_req(f">>> IgnoreFriendRequest {user_id} from {keepname}: {'ok' if success else 'fail'}")

    b = ObjFmtBuilder("Game.Client.Network.Profile.IgnoreFriendRequestResponse")
    b.field_enum("Error", "Game.Shared.Network.Profile.EIgnoreFriendRequestError", 0)
    b.field_str("ErrorMessage", "")
    resp_inner = b.finish(2)
    _send_response(handler, 2159, resp_inner, comp, session_id,
                   reqid, target, instance, conh, SERVICE_PROFILE_UID)


def handle_remove_friend(handler, target, instance, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, SERVICE_PROFILE_UID, **_kw):
    """RemoveFriend (2161) — remove a player from your friend list."""
    keepname = _extract_keepname(inner_bytes)
    user_id = handler.user_profile["id"] if handler.user_profile else 0
    from application.commands import SocialMutationCommand
    success, friend_uid = (handler._application.execute(
        SocialMutationCommand(user_id, "remove_friend", keepname)).value
        if keepname else (False, None))
    log_req(f">>> RemoveFriend {user_id} {keepname}: {'ok' if success else 'fail'}")

    b = ObjFmtBuilder("Game.Client.Network.Profile.RemoveFriendResponse")
    b.field_bool("Success", success)
    b.field_str("Username", keepname or "")
    b.field_enum("Error", "Game.Shared.Network.Profile.ERemoveFriendError", 0)
    b.field_str("ErrorMessage", "")
    resp_inner = b.finish(4)
    _send_response(handler, 2161, resp_inner, comp, session_id,
                   reqid, target, instance, conh, SERVICE_PROFILE_UID)

    if success and friend_uid:
        # Push FriendRemoved to the removing player
        push_friend_removed(handler, keepname, SERVICE_PROFILE_UID)
        # Push FriendRemoved to the other player if online
        
        my_name = handler.user_profile["name"] if handler.user_profile else keepname
        for other_h, _ in _active_clients().get(friend_uid, []):
            try:
                push_friend_removed(other_h, my_name, SERVICE_PROFILE_UID)
            except Exception as e:
                log_req(f"    FriendRemoved push to {friend_uid} failed: {e}")


def handle_ignore_player(handler, target, instance, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes, SERVICE_PROFILE_UID, **_kw):
    """IgnorePlayer (2163) — add a player to your ignore list."""
    keepname = _extract_keepname(inner_bytes)
    user_id = handler.user_profile["id"] if handler.user_profile else 0
    from application.commands import SocialMutationCommand
    success, ignored_uid, code = (handler._application.execute(
        SocialMutationCommand(user_id, "ignore_player", keepname)).value
        if keepname else (False, None, "CouldNotIgnore"))
    log_req(f">>> IgnorePlayer {user_id} {keepname}: {code}")

    b = ObjFmtBuilder("Game.Client.Network.Profile.IgnorePlayerResponse")
    b.field_enum("Error", "Game.Shared.Network.Profile.EIgnorePlayerError", 0)
    b.field_str("ErrorMessage", "")
    resp_inner = b.finish(2)
    _send_response(handler, 2163, resp_inner, comp, session_id,
                   reqid, target, instance, conh, SERVICE_PROFILE_UID)

    if success and ignored_uid:
        push_player_ignored(handler, code, ignored_uid, keepname, SERVICE_PROFILE_UID)


def handle_unignore_player(handler, target, instance, reqid, comp, session_id,
                           conh, inner_obj, inner_bytes, SERVICE_PROFILE_UID, **_kw):
    """UnignorePlayer (2165) — remove a player from your ignore list."""
    keepname = _extract_keepname(inner_bytes)
    user_id = handler.user_profile["id"] if handler.user_profile else 0
    from application.commands import SocialMutationCommand
    success, unignored_uid, code = (handler._application.execute(
        SocialMutationCommand(user_id, "unignore_player", keepname)).value
        if keepname else (False, None, "CouldNotUnignore"))
    log_req(f">>> UnignorePlayer {user_id} {keepname}: {code}")

    b = ObjFmtBuilder("Game.Client.Network.Profile.UnignorePlayerResponse")
    b.field_enum("Error", "Game.Shared.Network.Profile.EUnignorePlayerError", 0)
    b.field_str("ErrorMessage", "")
    resp_inner = b.finish(2)
    _send_response(handler, 2165, resp_inner, comp, session_id,
                   reqid, target, instance, conh, SERVICE_PROFILE_UID)

    if success and unignored_uid:
        push_player_unignored(handler, code, unignored_uid, keepname, SERVICE_PROFILE_UID)


# ── Event push functions ────────────────────────────────────────────────────

def push_friends_list(handler, SERVICE_PROFILE_UID):
    """Push FriendsListArrived (2202) to the player on login."""
    if not handler.user_profile:
        return
    user_id = handler.user_profile["id"]
    friends = db_get_friends(user_id)
    log_req(f">>> PUSH FriendsListArrived ({len(friends)} friends)")

    b = ObjFmtBuilder("Game.Shared.Network.Profile.FriendsListArrivedEventArgs")
    num_friends = len(friends)
    list_idx, _ = b.begin_list("FriendsInfo",
        "System.Collections.Generic.List`1#Game.Shared.FriendInfo", num_friends)
    for i, (fid, fname, _online) in enumerate(friends):
        online = fid in _active_clients()
        b.begin_element(i, "Game.Shared.FriendInfo", 3)
        b.field_bool("IsOnline", online)
        b.field_str("KeepName", fname)
        b.field_ulong("ReckId", fid)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(3)
    _push_event(handler, 2202, args, SERVICE_PROFILE_UID)

    # Push FriendComeOnline for friends who are currently online
    for fid, fname, _online in friends:
        if fid in _active_clients():
            try:
                push_friend_come_online(handler, fid, SERVICE_PROFILE_UID)
            except Exception:
                pass


def push_pending_friend_requests(handler, SERVICE_PROFILE_UID):
    """Push PendingFriendRequestsArrived (2199) to the player on login."""
    if not handler.user_profile:
        return
    user_id = handler.user_profile["id"]
    pending = db_get_pending_friend_requests(user_id)
    log_req(f">>> PUSH PendingFriendRequestsArrived ({len(pending)} pending)")

    b = ObjFmtBuilder("Game.Shared.Network.Profile.PendingFriendRequestsArrivedEventArgs")
    list_idx, _ = b.begin_list("PendingFriendRequests",
        "System.Collections.Generic.List`1#System.String", len(pending))
    for i, name in enumerate(pending):
        b.add_list_item_str(i, name)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(3)
    _push_event(handler, 2199, args, SERVICE_PROFILE_UID)


def push_ignored_list(handler, SERVICE_PROFILE_UID):
    """Push IgnoredListArrived (2196) to the player on login."""
    if not handler.user_profile:
        return
    user_id = handler.user_profile["id"]
    ignored = db_get_ignored_list(user_id)
    log_req(f">>> PUSH IgnoredListArrived ({len(ignored)} ignored)")

    b = ObjFmtBuilder("Game.Shared.Network.Profile.IgnoredListArrivedEventArgs")
    ig_entries = list(ignored.items())
    list_idx, _ = b.begin_list("IgnoresInfo",
        "System.Collections.Generic.Dictionary`2#System.UInt64!System.String",
        len(ig_entries))
    for i, (ig_id, ig_name) in enumerate(ig_entries):
        b.add_dict_entry_uint64_str(i, ig_id, ig_name)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(3)
    _push_event(handler, 2196, args, SERVICE_PROFILE_UID)


def push_friend_request_received(handler, sender_name, SERVICE_PROFILE_UID):
    """Push FriendRequestReceived (2192) to a player who just got a request."""
    log_req(f">>> PUSH FriendRequestReceived from {sender_name}")
    b = ObjFmtBuilder("Game.Shared.Network.Profile.FriendRequestReceivedEventArgs")
    b.field_str("SenderKeepname", sender_name)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(3)
    _push_event(handler, 2192, args, SERVICE_PROFILE_UID)


def push_friend_request_accepted(handler, reck_id, keepname, SERVICE_PROFILE_UID):
    """Push FriendRequestAccepted (2193) — your request was accepted."""
    log_req(f">>> PUSH FriendRequestAccepted {reck_id} ({keepname})")
    b = ObjFmtBuilder("Game.Shared.Network.Profile.FriendRequestAcceptedEventArgs")
    b.field_ulong("ReckId", reck_id)
    b.field_str("Keepname", keepname)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(4)
    _push_event(handler, 2193, args, SERVICE_PROFILE_UID)


def push_friend_added(handler, reck_id, keepname, is_online, SERVICE_PROFILE_UID):
    """Push FriendAdded (2194) — a new friend was added."""
    log_req(f">>> PUSH FriendAdded {reck_id} ({keepname}) online={is_online}")
    b = ObjFmtBuilder("Game.Shared.Network.Profile.FriendAddedEventArgs")
    b.field_ulong("ReckId", reck_id)
    b.field_str("Keepname", keepname)
    b.field_bool("IsOnline", is_online)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(5)
    _push_event(handler, 2194, args, SERVICE_PROFILE_UID)


def push_friend_removed(handler, keepname, SERVICE_PROFILE_UID):
    """Push FriendRemoved (2195) — a friend was removed."""
    log_req(f">>> PUSH FriendRemoved {keepname}")
    b = ObjFmtBuilder("Game.Shared.Network.Profile.FriendRemovedEventArgs")
    b.field_str("Keepname", keepname)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(3)
    _push_event(handler, 2195, args, SERVICE_PROFILE_UID)


def push_friend_request_removed(handler, keepname, SERVICE_PROFILE_UID):
    """Push FriendRequestRemoved (2200) — a pending request was removed."""
    log_req(f">>> PUSH FriendRequestRemoved {keepname}")
    b = ObjFmtBuilder("Game.Shared.Network.Profile.FriendRequestRemovedEventArgs")
    b.field_str("Keepname", keepname)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(3)
    _push_event(handler, 2200, args, SERVICE_PROFILE_UID)


def push_friend_come_online(handler, rek_id, SERVICE_PROFILE_UID):
    """Push FriendComeOnline (2203) to a friend."""
    b = ObjFmtBuilder("Game.Shared.Network.Profile.FriendComeOnlineEventArgs")
    b.field_ulong("rekId", rek_id)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(3)
    _push_event(handler, 2203, args, SERVICE_PROFILE_UID)


def push_friend_goes_offline(handler, rek_id, SERVICE_PROFILE_UID):
    """Push FriendGoesOffline (2204) to a friend."""
    b = ObjFmtBuilder("Game.Shared.Network.Profile.FriendGoesOfflineEventArgs")
    b.field_ulong("rekId", rek_id)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(3)
    _push_event(handler, 2204, args, SERVICE_PROFILE_UID)


def push_player_ignored(handler, code_str, reck_id, keepname, SERVICE_PROFILE_UID):
    """Push PlayerIgnored (2197) — a player was ignored."""
    code_map = {
        "Success": 0, "AlreadyIgnored": 1, "CouldNotIgnore": 2,
        "CouldNotUnignore": 3, "AlreadyUnignored": 4,
    }
    b = ObjFmtBuilder("Game.Shared.Network.Profile.PlayerIgnoredEventArgs")
    b.field_enum("Code", "Game.Shared.EIgnorePlayerResponseCode",
                 code_map.get(code_str, 0))
    b.field_ulong("ReckId", reck_id)
    b.field_str("Keepname", keepname)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(5)
    _push_event(handler, 2197, args, SERVICE_PROFILE_UID)


def push_player_unignored(handler, code_str, reck_id, keepname, SERVICE_PROFILE_UID):
    """Push PlayerUnignored (2198) — a player was unignored."""
    code_map = {
        "Success": 0, "AlreadyIgnored": 1, "CouldNotIgnore": 2,
        "CouldNotUnignore": 3, "AlreadyUnignored": 4,
    }
    b = ObjFmtBuilder("Game.Shared.Network.Profile.PlayerUnignoredEventArgs")
    b.field_enum("Code", "Game.Shared.EIgnorePlayerResponseCode",
                 code_map.get(code_str, 0))
    b.field_ulong("ReckId", reck_id)
    b.field_str("Keepname", keepname)
    b.field_int("OriginClusterHash", 0)
    b.field_guid("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
    args = b.finish(5)
    _push_event(handler, 2198, args, SERVICE_PROFILE_UID)


# ── Batch push (called on login) ────────────────────────────────────────────

def push_all_social_data(handler, SERVICE_PROFILE_UID):
    """Push friend list, pending requests, and ignored list on login."""
    push_friends_list(handler, SERVICE_PROFILE_UID)
    push_pending_friend_requests(handler, SERVICE_PROFILE_UID)
    push_ignored_list(handler, SERVICE_PROFILE_UID)


# ── Online/offline broadcast ────────────────────────────────────────────────

def broadcast_friend_online(handler, SERVICE_PROFILE_UID):
    """Notify all friends of *handler* that they just came online."""
    if not handler.user_profile:
        return
    user_id = handler.user_profile["id"]
    
    # Find all friends of this user
    friends = db_get_friends(user_id)
    for fid, fname, _ in friends:
        for friend_h, _ in _active_clients().get(fid, []):
            try:
                push_friend_come_online(friend_h, user_id, SERVICE_PROFILE_UID)
            except Exception as e:
                log_req(f"    FriendComeOnline broadcast to friend {fid} failed: {e}")


def broadcast_friend_offline(handler, SERVICE_PROFILE_UID):
    """Notify all friends of *handler* that they just went offline."""
    if not handler.user_profile:
        return
    user_id = handler.user_profile["id"]
    
    # Check if user has other active sessions (don't mark offline if still connected)
    if len(_active_clients().get(user_id, [])) > 0:
        return  # still has an active session
    friends = db_get_friends(user_id)
    for fid, fname, _ in friends:
        for friend_h, _ in _active_clients().get(fid, []):
            try:
                push_friend_goes_offline(friend_h, user_id, SERVICE_PROFILE_UID)
            except Exception as e:
                log_req(f"    FriendGoesOffline broadcast to friend {fid} failed: {e}")
