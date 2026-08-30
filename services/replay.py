"""Replay browser API backed by the replay worker's game_replays index."""

import json
import os
from datetime import datetime, timedelta

from db import _db


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
    request = _json_request(raw)
    ses_filter = request.get("SesFilter") or "%"
    format_filter = request.get("SFormat") or "%"
    template_filter = request.get("STemplate") or "%"
    offset = max(0, int(request.get("Offset") or 0))
    count = max(1, min(100, int(request.get("Count") or 25)))
    rows = _db.execute(
        "SELECT session_name,server_id,start_time,end_time,series_format,"
        "series_points,series_template,is_public,players_json,tournament_round "
        "FROM game_replays WHERE status='ready' AND session_name LIKE ? "
        "AND series_format LIKE ? AND series_template LIKE ? "
        "ORDER BY end_time DESC LIMIT ? OFFSET ?",
        (ses_filter, format_filter, template_filter, count, offset),
    ).fetchall()
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
            "StartUTC": _display_time(start_time),
            "EndUTC": _display_time(end_time),
            # Game.Shared.UID is serialized as an object by Newtonsoft.Json,
            # not as the underlying integer value.
            "Server": {"m_UID64": int(server_id or 0)},
            "Session": session_name,
            "SFormat": series_format or "UNKNOWN",
            "SPoints": int(series_points or 0),
            "STemplate": series_template or "",
            "PubGame": bool(is_public),
            "Players": " vs ".join(names),
            "Winners": ",".join(winners),
            "TournRound": int(round_id if round_id is not None else -1),
            "ExpireUTC": _expire_time(end_time),
        })
    return {
        "Req": {
            "action": "qreplaylst",
            "SesFilter": request.get("SesFilter"),
            "SFormat": request.get("SFormat"),
            "STemplate": request.get("STemplate"),
            "Offset": offset,
            "Count": count,
        },
        "Records": records,
    }


def replay_fetch(raw):
    request = _json_request(raw)
    session_name = str(request.get("Session") or "")
    offset = max(0, int(request.get("Offset") or 0))
    size = max(1, min(1024 * 1024, int(request.get("Size") or 16384)))
    row = _db.execute(
        "SELECT replay_path FROM game_replays "
        "WHERE session_name=? AND status='ready'", (session_name,)
    ).fetchone()
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
    more = next_offset < total
    return data + bytes([1 if more else 0]) + int(total).to_bytes(4, "little")
