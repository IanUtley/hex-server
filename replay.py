"""Build client ``.replay`` artifacts from the durable session event stream.

Replay packaging is deliberately independent of the worker process so the
same builder can be exercised by tests or an administrative job.  The
``replay_server`` module only provides the polling loop.
"""

import gzip
import json
import os
import struct
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("HEX_DB_PATH", os.path.join(BASE_DIR, "hconnect.db"))
REPLAY_DIR = os.environ.get(
    "HEX_REPLAY_DIR", os.path.join(os.path.dirname(DB_PATH), "replays"))
POLL_SECONDS = float(os.environ.get("HEX_REPLAY_POLL_SECONDS", "5"))
REPLAYABLE_PREFIXES = ("tourney-", "pvp-", "Challenge_")
RECIPIENT_DUPLICATE_WINDOW_MS = int(
    os.environ.get("HEX_REPLAY_DUPLICATE_WINDOW_MS", "250"))


def _connect():
    import db
    return db.connect(DB_PATH)


def _read_7bit(value):
    out = bytearray()
    while value >= 0x80:
        out.append((value | 0x80) & 0xff)
        value >>= 7
    out.append(value & 0xff)
    return bytes(out)


def _dotnet_string(value):
    raw = (value or "").encode("utf-8")
    return _read_7bit(len(raw)) + raw


def _game_log_string(value):
    raw = (value or "").encode("utf-8")
    return struct.pack("<i", len(raw)) + raw


def _varint(value):
    return _read_7bit(int(value))


def _empty_deck():
    """ProfileDeckTemplate.ToBytes() for a deck with no cards/equipment."""
    return (
        _dotnet_string("") +
        (b"\0" * 16) +
        (b"\0" * 16) +
        _varint(0) +
        _varint(0) +
        _varint(0)
    )


def _uid(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _raw_player_uid(value):
    value = _uid(value)
    return value >> 8 if (value & 0xff) == 244 else value


def _format_time(value, fallback=None):
    if value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y/%m/%dT%H:%M:%SZ00:00")
        except ValueError:
            pass
    dt = fallback or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y/%m/%dT%H:%M:%SZ00:00")


def _fixed_time(value, fallback=None):
    return _format_time(value, fallback).encode("ascii")


def _players(conn, session_id, session_name, players_json):
    import replay_db as db_layer
    try:
        entries = json.loads(players_json or "[]")
    except (TypeError, ValueError):
        entries = []
    supplied = {}
    player_uids = []
    for row in entries:
        if isinstance(row, dict):
            uid = _uid(row.get("id"))
            if uid:
                supplied[uid] = row
        else:
            uid = _uid(row[0] if isinstance(row, (list, tuple)) else row)
        player_uids.append(uid)
    match = db_layer.db_get_replay_match(session_id, conn=conn)
    names = {}
    winners = set()
    round_id = -1
    if match:
        tournament_id, round_id, p1, p2, winner = match
        winner = _uid(winner)
        if winner:
            winners.add((winner << 8) | 244)
        if not any(player_uids):
            player_uids = [(_uid(p1) << 8) | 244, (_uid(p2) << 8) | 244]
        signups = db_layer.db_get_tournament_signup_names(tournament_id, conn=conn)
        names.update({_uid(uid) << 8 | 244: name for uid, name in signups})
    result = []
    for uid in player_uids:
        raw = _raw_player_uid(uid)
        source = supplied.get(uid, {})
        name = source.get("name") if isinstance(source, dict) else None
        name = name or names.get(uid) or names.get((raw << 8) | 244) or f"Player {raw}"
        result.append({
            "id": uid, "name": name,
            "winner": bool(source.get("winner")) or uid in winners,
            "deck": source.get("deck") or _empty_deck().hex(),
        })
    return result, sorted(winners), round_id


def _event_stream(conn, session_id):
    import replay_db as db_layer
    rows = db_layer.db_get_replay_events(session_id, conn=conn)
    events = []
    first_phase_seq = {}
    for _row_id, target, seq, event_class, _payload in rows:
        if int(event_class) != 3:
            continue
        target = _uid(target)
        first_phase_seq[target] = min(
            int(seq), first_phase_seq.get(target, int(seq)))
    recent = {}
    for row_id, target, seq, event_class, payload in rows:
        target = _uid(target)
        key = (int(event_class), bytes(payload))
        idx = recent.get(key)
        if (idx is not None and target not in events[idx]["targets"] and
                int(seq) - events[idx]["seq"] <= RECIPIENT_DUPLICATE_WINDOW_MS):
            events[idx]["targets"].append(target)
            continue
        recent[key] = len(events)
        events.append({"id": row_id, "seq": int(seq),
                       "event_class": int(event_class),
                       "payload": bytes(payload), "targets": [target]})

    preamble = []
    gameplay = []
    for event in events:
        is_preamble = (
            event["event_class"] != 3 and event["targets"] and
            all(target in first_phase_seq and
                event["seq"] <= first_phase_seq[target]
                for target in event["targets"]))
        (preamble if is_preamble else gameplay).append(event)
    events = sorted(preamble, key=lambda event: (event["seq"], event["id"]))
    events.extend(sorted(gameplay, key=lambda event: (event["seq"], event["id"])))
    return events, max((row[0] for row in rows), default=0)


def _order_players_for_observer(players, events):
    startup_target = next(
        (event["targets"][0] for event in events
         if event["event_class"] == 1 and event["targets"]), None)
    if startup_target is None:
        return players
    for index, player in enumerate(players):
        if _uid(player.get("id")) == startup_target:
            return players[index:] + players[:index]
    return players


def _append_game_end(events, session_id, players):
    if any(event["event_class"] == 2 for event in events):
        return
    player_ids = [_uid(player.get("id")) for player in players if player.get("id")]
    if not player_ids:
        return
    winners = [uid for uid, player in zip(player_ids, players)
               if player.get("winner")]
    losers = [uid for uid in player_ids if uid not in winners]
    payload = bytearray(struct.pack("<iQ", 2, _uid(session_id)))
    payload.extend(struct.pack("<i", len(winners)))
    for uid in winners:
        payload.extend(struct.pack("<Q", uid))
    payload.extend(struct.pack("<i", len(losers)))
    for uid in losers:
        payload.extend(struct.pack("<Q", uid))
    events.append({
        "id": (events[-1]["id"] if events else 0) + 1,
        "seq": (events[-1]["seq"] if events else 0) + 1,
        "event_class": 2, "payload": bytes(payload), "targets": player_ids,
    })


def _replay_bytes(metadata, events):
    now = datetime.now(timezone.utc)
    out = bytearray(struct.pack("<i", 5))
    out.extend(struct.pack("<I", int(metadata["session_flags"])))
    out.extend(struct.pack("<Q", _uid(metadata["server_id"])))
    out.extend(_game_log_string(metadata["session_name"]))
    out.extend(_fixed_time(metadata["start_time"], now))
    out.extend(_fixed_time(metadata["end_time"], now))
    out.extend(struct.pack("<i", int(metadata["tournament_round"])))
    out.extend(struct.pack("<?", bool(metadata["is_public"])))
    out.extend(_game_log_string(metadata["series_format"]))
    out.extend(struct.pack("<i", int(metadata["series_points"])))
    out.extend(_game_log_string(metadata["series_template"]))
    players = _order_players_for_observer(metadata["players"], events)
    out.extend(struct.pack("<i", len(players)))
    for player in players:
        out.extend(struct.pack("<Q", _uid(player["id"])))
        out.extend(_game_log_string(player["name"]))
        out.extend(struct.pack("<?", bool(player["winner"])))
        deck = bytes.fromhex(player["deck"])
        out.extend(struct.pack("<i", len(deck)))
        out.extend(deck)

    split = next((i for i, event in enumerate(events)
                  if event["event_class"] == 3), len(events))
    generations = (events[:split], events[split:])
    out.extend(struct.pack("<i", len(generations)))
    previous_seq = None
    for generation in generations:
        out.extend(struct.pack("<i", len(generation)))
        for event in generation:
            targets = event["targets"]
            out.extend(struct.pack("<i", len(targets)))
            for target in targets:
                out.extend(struct.pack("<Q", _uid(target)))
            out.extend(struct.pack("<i", event["event_class"]))
            compressed = gzip.compress(event["payload"], compresslevel=9)
            out.extend(struct.pack("<i", len(compressed)))
            out.extend(compressed)
            offset = (0 if previous_seq is None else
                      max(0, min(2_147_483_647, event["seq"] - previous_seq)))
            out.extend(struct.pack("<i", offset))
            previous_seq = event["seq"]
    return bytes(out), len(generations), len(events)


def process_once(conn=None):
    """Build any completed replay candidates and return the build count."""
    import replay_db as db_layer
    owns = conn is None
    conn = conn or _connect()
    try:
        candidates = db_layer.db_get_replay_candidates(conn=conn)
        built = 0
        os.makedirs(REPLAY_DIR, exist_ok=True)
        for session_id, session_name, server_id, _state, players_json, created_at in candidates:
            events, max_event_id = _event_stream(conn, session_id)
            if not events:
                continue
            existing = db_layer.db_get_replay_source(session_id, conn=conn)
            if existing and existing[0] >= max_event_id and existing[1] == "ready":
                continue
            players, winners, round_id = _players(
                conn, session_id, session_name, players_json)
            _append_game_end(events, session_id, players)
            metadata = {
                "session_id": session_id, "session_name": session_name,
                "server_id": server_id or "", "session_flags": 16 | 4096,
                "start_time": created_at or "", "end_time": None,
                "tournament_round": round_id, "is_public": 1,
                "series_format": "CONSTRUCTED", "series_points": 0,
                "series_template": "Standard", "players": players,
            }
            artifact, generations, event_count = _replay_bytes(metadata, events)
            filename = f"{session_name.replace('/', '_')}.replay"
            path = os.path.join(REPLAY_DIR, filename)
            temp_path = path + ".tmp"
            with open(temp_path, "wb") as replay_file:
                replay_file.write(artifact)
            os.replace(temp_path, path)
            end_time = datetime.now(timezone.utc).isoformat()
            db_layer.db_upsert_replay(
                (session_id, session_name, server_id or "", metadata["session_flags"],
                 created_at or "", end_time, round_id, 1, "CONSTRUCTED", 0, "Standard",
                 json.dumps(players), json.dumps(winners), path, generations,
                 event_count, max_event_id, "ready", ""), conn=conn)
            conn.commit()
            built += 1
        return built
    finally:
        if owns:
            conn.close()


def _json_request(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _display_time(value):
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value).replace("T", " ").split(".", 1)[0]


def _expire_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed + timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return _display_time(value)


def replay_list(raw):
    """Return the legacy replay-browser envelope for a list request."""
    import replay_db as db_layer
    request = _json_request(raw)
    ses_filter = request.get("SesFilter") or "%"
    format_filter = request.get("SFormat") or "%"
    template_filter = request.get("STemplate") or "%"
    offset = max(0, int(request.get("Offset") or 0))
    count = max(1, min(100, int(request.get("Count") or 25)))
    rows = db_layer.db_get_replay_list_rows(
        (ses_filter, format_filter, template_filter, count, offset))
    records = []
    for (session_name, server_id, start_time, end_time, series_format,
         series_points, series_template, is_public, players_json, round_id) in rows:
        try:
            players = json.loads(players_json or "[]")
        except (TypeError, ValueError):
            players = []
        names = [str(player.get("name") or "Player") for player in players]
        winners = [str(player.get("name") or "Player") for player in players
                   if player.get("winner")]
        records.append({
            "StartUTC": _display_time(start_time), "EndUTC": _display_time(end_time),
            "Server": {"m_UID64": int(server_id or 0)},
            "Session": session_name, "SFormat": series_format or "UNKNOWN",
            "SPoints": int(series_points or 0), "STemplate": series_template or "",
            "PubGame": bool(is_public), "Players": " vs ".join(names),
            "Winners": ",".join(winners),
            "TournRound": int(round_id if round_id is not None else -1),
            "ExpireUTC": _expire_time(end_time),
        })
    return {
        "Req": {"action": "qreplaylst", "SesFilter": request.get("SesFilter"),
                "SFormat": request.get("SFormat"),
                "STemplate": request.get("STemplate"),
                "Offset": offset, "Count": count},
        "Records": records,
    }


def replay_fetch(raw):
    """Read one bounded replay chunk for the legacy browser request."""
    import replay_db as db_layer
    request = _json_request(raw)
    session_name = str(request.get("Session") or "")
    offset = max(0, int(request.get("Offset") or 0))
    size = max(1, min(1024 * 1024, int(request.get("Size") or 16384)))
    row = db_layer.db_get_replay_path(session_name)
    path = row[0] if row else ""
    try:
        total = os.path.getsize(path)
    except (OSError, TypeError):
        total = 0
    if total:
        with open(path, "rb") as replay_file:
            replay_file.seek(min(offset, total))
            data = replay_file.read(size)
    else:
        data = b""
    next_offset = offset + len(data)
    return data + bytes([1 if next_offset < total else 0]) + int(total).to_bytes(4, "little")
