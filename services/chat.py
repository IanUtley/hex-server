"""Chat service handler: rjoin, rleave, rchat, say, glist."""

import json, time
import sys
from datetime import datetime

import db as _db_mod
from db import _db, log_req, db_store_chat, db_get_recent_chat, display_name_from_identity
from gamemodes.tournament_engine import push_tournament_room_data


def handle_chat_message(handler, body):
    """Process a chat message received on target=Session, instance=chat.
    Called from the HCPHandler's main dispatch loop."""
    try:
        chat_data = json.loads(body.decode("utf-8"))
        action = chat_data.get("action", "")
        room = chat_data.get("room", "")
        if action == "rjoin":
            _handle_rjoin(handler, room, chat_data)
        elif action == "rleave":
            getattr(handler, "_chat_rooms", set()).discard(room)
            _broadcast_room_event(handler, room, "rleave")
            log_req(f">>> Chat leave room={room}")
        elif action in ("rchat", "say"):
            _handle_rchat(handler, room, chat_data)
        elif action == "glist":
            pass  # global user list — ignore for now
        else:
            log_req(f">>> Chat unknown action={action}")
    except Exception as e:
        log_req(f">>> Chat parse error: {e}")


def _handle_rjoin(handler, room, chat_data):
    log_req(f">>> Chat join room={room}")
    if not hasattr(handler, "_chat_rooms"):
        handler._chat_rooms = set()
    handler._chat_rooms.add(room)
    username = (handler.user_profile.get("name", "Unknown")
                if handler.user_profile else "Unknown")
    display_name = display_name_from_identity(username)
    ack = json.dumps({
        "action": "rjoin", "room": room, "rflg": "",
        "user": display_name, "flags": "", "icon": "",
    })
    handler.scnt += 1
    handler.send({"issuer": "Session", "target": "chat", "sid": handler.sid},
                 body=ack.encode("utf-8"))
    # Tell the other members as well. UIBattle uses this room-presence event
    # to maintain its opponent list and online status.
    _broadcast_room_event(handler, room, "rjoin")

    # Push tournament room data on both base and '_full' joins. Clients can
    # leave the _full room while catching up, so a broadcast sent during that
    # gap is not enough; the next _full join must provide a fresh snapshot.
    if room.startswith("tourn:") and not room.endswith("_resume"):
        data_room = room if room.endswith("_full") else room + "_full"
        push_tournament_room_data(handler, data_room, display_name)
        return

    # Push chat history for non-tournament rooms
    elif handler.user_profile:
        recent = db_get_recent_chat(room, 30)
        for msg in recent:
            ts = msg.get("time", "")
            time_str = ""
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    time_str = f"[{dt.strftime('%H:%M')}] "
                except Exception:
                    pass
            hist = json.dumps({
                "action": "rchat",
                "room": room,
                "rflg": "",
                "user": f"{msg['user']} {time_str}",
                "msg": msg["msg"],
                "flags": msg.get("flags", ""),
                "icon": msg.get("icon", ""),
            })
            handler.scnt += 1
            handler.send({
                "issuer": "Session", "target": "chat", "sid": handler.sid,
            }, body=hist.encode("utf-8"))
        log_req(f"    Pushed {len(recent)} chat history msgs to {room}")


def _broadcast_room_event(handler, room, action):
    """Broadcast a room join/leave event to the other live room members."""
    if not room:
        return 0
    server = sys.modules.get("hconnect_server")
    if server is None or not hasattr(server, "_active_clients"):
        server = sys.modules.get("__main__")
    active_clients = getattr(server, "_active_clients", {})
    profile = getattr(handler, "user_profile", None) or {}
    username = display_name_from_identity(profile.get("name", "Unknown"))
    flags = ""
    event = json.dumps({
        "action": action,
        "room": room,
        "rflg": "",
        "user": username,
        "flags": flags,
    }).encode("utf-8")
    sent = 0
    for entries in list(active_clients.values()):
        for peer, _ in list(entries):
            if peer is handler or not getattr(peer, "authenticated", False):
                continue
            if room not in getattr(peer, "_chat_rooms", set()):
                continue
            try:
                peer.scnt += 1
                peer.send({
                    "issuer": "Session", "target": "chat", "sid": peer.sid,
                }, body=event)
                sent += 1
            except OSError as exc:
                log_req(f"    Chat {action} broadcast failed: {exc}")
    if sent:
        log_req(f"    Chat {action}: {username} in {room}, sent to {sent}")
    return sent


def notify_chat_player_disconnected(handler):
    """Broadcast rleave for every chat room held by a disconnected handler."""
    rooms = tuple(getattr(handler, "_chat_rooms", set()))
    sent = 0
    for room in rooms:
        sent += _broadcast_room_event(handler, room, "rleave")
    if rooms:
        log_req(f"    Chat disconnect: {len(rooms)} room(s), sent to {sent}")
    return sent


def _handle_rchat(handler, room, chat_data):
    msg_text = chat_data.get("msg", "")
    icon = chat_data.get("icon", "")
    flags = chat_data.get("flags", "")
    username = (handler.user_profile.get("name", "Unknown")
                if handler.user_profile else "Unknown")
    user_id = handler.user_profile.get("id", 0) if handler.user_profile else 0
    log_req(f">>> Chat msg room={room} from={username} msg={msg_text[:50]}")

    if msg_text.startswith("/") or msg_text.startswith("!"):
        resp = handler._handle_chat_command(msg_text[1:], room, username)
        if resp:
            cmd_echo = json.dumps({
                "action": "rchat", "room": room, "rflg": "",
                "user": f"Server [{time.strftime('[%H:%M]')}]",
                "msg": resp, "flags": "", "icon": "",
            })
            handler.scnt += 1
            handler.send({
                "issuer": "Session", "target": "chat", "sid": handler.sid,
            }, body=cmd_echo.encode("utf-8"))
    else:
        if user_id:
            db_store_chat(user_id, username, room, msg_text, icon, flags)
        # Echo
        echo = json.dumps({
            "action": "rchat", "room": room, "rflg": "",
            "user": f"{username} [{time.strftime('%H:%M')}]",
            "msg": msg_text, "flags": flags, "icon": icon,
        })
        handler.scnt += 1
        handler.send({
            "issuer": "Session", "target": "chat", "sid": handler.sid,
        }, body=echo.encode("utf-8"))
        log_req(f"    Echoed chat to {username}")

        # Session tracking and broadcast
        # The live server runs as __main__; use the aliased running module so
        # this registry is the same one populated during authentication.
        server = sys.modules.get("hconnect_server")
        if server is None or not hasattr(server, "_active_clients"):
            server = sys.modules.get("__main__")
        server.touch_session(handler)
        server.cleanup_stale_sessions()

        active_clients = server._active_clients
        broadcast_count = 0
        for uid, handlers_list in active_clients.items():
            for h, t in handlers_list:
                if h is handler or not h.authenticated:
                    continue
                if h.user_profile is None:
                    continue
                if room not in getattr(h, "_chat_rooms", set()):
                    continue
                try:
                    h.scnt += 1
                    h.send({
                        "issuer": "Session", "target": "chat", "sid": h.sid,
                    }, body=echo.encode("utf-8"))
                    broadcast_count += 1
                except Exception as _bex:
                    log_req(f"    Chat broadcast to {uid} failed: {_bex}")
        log_req(f"    Chat broadcast: {len(active_clients)} sessions, "
                f"sent to {broadcast_count} other users")
        if broadcast_count:
            log_req(f"    Broadcast to {broadcast_count} other users")
