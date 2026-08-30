"""Assemble completed PvP session events into client ``.replay`` files.

Live event capture deliberately stays on the HConnect request path.  This
worker consumes the durable session_events stream in a separate process and
creates the replay index/artifact used by the eventual replay list/fetch
services.
"""

import gzip
import json
import os
import struct
import time
from datetime import datetime, timezone

import db as db_layer


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("HEX_DB_PATH", os.path.join(BASE_DIR, "hconnect.db"))
REPLAY_DIR = os.environ.get(
    "HEX_REPLAY_DIR", os.path.join(os.path.dirname(DB_PATH), "replays"))
POLL_SECONDS = float(os.environ.get("HEX_REPLAY_POLL_SECONDS", "5"))
REPLAYABLE_PREFIXES = ("tourney-", "pvp-", "Challenge_")
# A packet is recorded once for each recipient.  The two SQLite inserts are
# not guaranteed to receive the same millisecond timestamp (or to complete in
# recipient order), even though they describe the same public event.
RECIPIENT_DUPLICATE_WINDOW_MS = int(
    os.environ.get("HEX_REPLAY_DUPLICATE_WINDOW_MS", "250"))


def _connect():
    return db_layer.connect(DB_PATH)


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
    """GameEventLog metadata strings use BinaryWriter.Write(byte[].)."""
    raw = (value or "").encode("utf-8")
    return struct.pack("<i", len(raw)) + raw


def _varint(value):
    return _read_7bit(int(value))


def _empty_deck():
    """ProfileDeckTemplate.ToBytes() for a deck with no cards/equipment."""
    return (
        _dotnet_string("") +
        (b"\0" * 16) +  # Champion ResourceId.ToByteArray()
        (b"\0" * 16) +  # Sleeve ResourceId.ToByteArray()
        _varint(0) +     # equipment count
        _varint(0) +     # card descriptor count
        _varint(0)       # extended data count
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
    # GameEventLog writes this as a fixed 25-byte ASCII field, not a length
    # prefixed BinaryWriter string.
    return _format_time(value, fallback).encode("ascii")


def _players(conn, session_id, session_name, players_json):
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
    match = conn.execute(
        "SELECT tournament_id, round_id, player1_uid, player2_uid, game1_winner "
        "FROM tournament_matches WHERE session_id=? LIMIT 1", (session_id,)
    ).fetchone()
    names = {}
    winners = set()
    round_id = -1
    tournament_id = None
    if match:
        tournament_id, round_id, p1, p2, winner = match
        winner = _uid(winner)
        if winner:
            winners.add((winner << 8) | 244)
        if not any(player_uids):
            player_uids = [(_uid(p1) << 8) | 244, (_uid(p2) << 8) | 244]
        signups = conn.execute(
            "SELECT player_uid, player_name FROM tournament_signups "
            "WHERE tournament_id=?", (tournament_id,)).fetchall()
        names.update({_uid(uid) << 8 | 244: name for uid, name in signups})
    result = []
    for uid in player_uids:
        raw = _raw_player_uid(uid)
        source = supplied.get(uid, {})
        name = (source.get("name") if isinstance(source, dict) else None)
        name = name or names.get(uid) or names.get((raw << 8) | 244) or f"Player {raw}"
        result.append({"id": uid, "name": name,
                       "winner": bool(source.get("winner")) or uid in winners,
                       "deck": source.get("deck") or _empty_deck().hex()})
    return result, sorted(winners), round_id


def _event_stream(conn, session_id):
    rows = conn.execute(
        "SELECT id, target_player_uid, seq, event_class, event_bytes "
        "FROM session_events WHERE session_id=? ORDER BY seq, id", (session_id,)
    ).fetchall()
    events = []
    first_phase_seq = {}
    for _row_id, target, seq, event_class, _payload in rows:
        if int(event_class) != 3:
            continue
        target = _uid(target)
        first_phase_seq[target] = min(
            int(seq), first_phase_seq.get(target, int(seq)))
    # The logger stores one row per event per recipient.  Usually events from
    # the same packet share ``seq``, but the two recipient writes can straddle
    # a few clock ticks.  Coalesce matching payloads within a short window so
    # the replay receives one public event with both recipients as targets.
    # Never coalesce a repeated event for the same recipient.
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

    # Each live client receives its own startup snapshot.  Those snapshots
    # can be written a few milliseconds apart, so one recipient's first phase
    # may sort before the other recipient's GameStarted/PlayerUpdated events.
    # ReplayClient treats CardUpdated as stateful even when its target is not
    # the observer, which makes the delayed snapshot look like cards being
    # played before the first main phase.  Put every recipient's pre-phase
    # snapshot into the same generation before the first phase event.  The
    # first-phase boundaries come from the uncoalesced rows above; using the
    # coalesced event's timestamp would lose the later recipient's boundary.
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
    """Put the first startup recipient first in GameEventLog.PlayerData.

    ReplayClient chooses its local observer from the first ServicePlayer in
    PlayerData.  A live session may persist the two recipient startup packets
    in either order; matching PlayerData to the first coherent preamble keeps
    GameStarted, PlayerUpdated and CardUpdated visible during FlushGenZero.
    """
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
    """Ensure ReplayClient can finish or honour Exit Replay.

    Some live sessions persist their terminal state without delivering the
    class-2 event through the normal per-player event stream.  ReplayClient's
    exit path deliberately replays the final GameEnded event, so synthesize
    the same wire payload from the indexed player/winner metadata when it is
    absent.
    """
    if any(event["event_class"] == 2 for event in events):
        return
    player_ids = [_uid(player.get("id")) for player in players if player.get("id")]
    if not player_ids:
        return
    winners = [uid for uid, player in zip(player_ids, players)
               if player.get("winner")]
    losers = [uid for uid in player_ids if uid not in winners]
    # SessionEventArgs serializes [Class:int32][SessionId:UID:uint64]
    # before the two UID lists.  Packing the session id as int32 shifts the
    # first list count by four bytes and makes the client construct a list with
    # an invalid capacity when it reaches the synthetic terminal event.
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
        "event_class": 2,
        "payload": bytes(payload),
        "targets": player_ids,
    })


def _replay_bytes(metadata, events):
    now = datetime.now(timezone.utc)
    start = _fixed_time(metadata["start_time"], now)
    end = _fixed_time(metadata["end_time"], now)
    out = bytearray(struct.pack("<i", 5))
    out.extend(struct.pack("<I", int(metadata["session_flags"])))
    out.extend(struct.pack("<Q", _uid(metadata["server_id"])))
    out.extend(_game_log_string(metadata["session_name"]))
    out.extend(start)
    out.extend(end)
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

    # Setup events are generation zero because ReplayClient flushes that
    # generation twice to initialize the battle state. Gameplay starts at the
    # first TurnPhaseUpdated event.
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
            if previous_seq is None:
                offset = 0
            else:
                offset = max(0, min(2_147_483_647, event["seq"] - previous_seq))
            out.extend(struct.pack("<i", offset))
            previous_seq = event["seq"]
    return bytes(out), len(generations), len(events)


def process_once(conn=None):
    owns = conn is None
    conn = conn or _connect()
    try:
        candidates = conn.execute(
            "SELECT gs.session_id, gs.session_name, gs.server_id, gs.state, "
            "gs.players_json, gs.created_at "
            "FROM game_sessions gs JOIN session_events se "
            "ON se.session_id=gs.session_id "
            "WHERE (gs.session_name LIKE 'tourney-%' OR "
            "gs.session_name LIKE 'pvp-%' OR gs.session_name LIKE 'Challenge_%') "
            "AND (gs.state='ended' OR se.event_class=2) "
            "GROUP BY gs.session_id "
            "UNION "
            "SELECT gr.session_id, gr.session_name, gr.server_id, 'ended', "
            "gr.players_json, gr.start_time "
            "FROM game_replays gr JOIN session_events se "
            "ON se.session_id=gr.session_id "
            "WHERE gr.status IN ('stale', 'error') "
            "AND (gr.session_name LIKE 'tourney-%' OR "
            "gr.session_name LIKE 'pvp-%' OR gr.session_name LIKE 'Challenge_%') "
            "GROUP BY gr.session_id",).fetchall()
        built = 0
        os.makedirs(REPLAY_DIR, exist_ok=True)
        for session_id, session_name, server_id, state, players_json, created_at in candidates:
            events, max_event_id = _event_stream(conn, session_id)
            if not events:
                continue
            existing = conn.execute(
                "SELECT source_event_max_id, status FROM game_replays WHERE session_id=?",
                (session_id,)).fetchone()
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
            conn.execute(
                "INSERT INTO game_replays "
                "(session_id,session_name,server_id,session_flags,start_time,end_time,"
                "tournament_round,is_public,series_format,series_points,series_template,"
                "players_json,winners_json,replay_path,generation_count,event_count,"
                "source_event_max_id,status,error,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(session_id) DO UPDATE SET session_name=excluded.session_name,"
                "server_id=excluded.server_id,session_flags=excluded.session_flags,"
                "start_time=excluded.start_time,end_time=excluded.end_time,"
                "tournament_round=excluded.tournament_round,is_public=excluded.is_public,"
                "series_format=excluded.series_format,series_points=excluded.series_points,"
                "series_template=excluded.series_template,players_json=excluded.players_json,"
                "winners_json=excluded.winners_json,replay_path=excluded.replay_path,"
                "generation_count=excluded.generation_count,event_count=excluded.event_count,"
                "source_event_max_id=excluded.source_event_max_id,status=excluded.status,"
                "error=excluded.error,updated_at=datetime('now')",
                (session_id, session_name, server_id or "", metadata["session_flags"],
                 created_at or "", end_time, round_id, 1, "CONSTRUCTED", 0, "Standard",
                 json.dumps(players), json.dumps(winners), path, generations,
                 event_count, max_event_id, "ready", ""))
            conn.commit()
            built += 1
        return built
    finally:
        if owns:
            conn.close()


def run():
    print(f"[replay_server] Watching {DB_PATH}; output={REPLAY_DIR}", flush=True)
    while True:
        try:
            built = process_once()
            if built:
                print(f"[replay_server] Built {built} replay(s)", flush=True)
        except Exception as exc:
            print(f"[replay_server] Worker error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
