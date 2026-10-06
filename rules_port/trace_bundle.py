"""Build one read-only server/Unity evidence bundle for a 3029 action."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfo

from application.player_transactions import classify_player_transaction
from .sql_capture import packet_capture, transaction_capture


_SUBMIT_RE = re.compile(
    r"SUBMITTING TRANSACTION\s+(-?\d+)\s*:\s*([A-Za-z0-9_]+)")
_TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(\d{1,2}:\d{2}:\d{2}\s+[AP]M)(?!\w)", re.IGNORECASE)
_CLIENT_FAILURE_PATTERN = (
    r"\b\w*Exception\b|Command handler not found|"
    r"^\s*Error\s*:|\|\s*ERROR\s*\|")
_CLIENT_FAILURE_RE = re.compile(_CLIENT_FAILURE_PATTERN, re.IGNORECASE)
_CLIENT_DIAGNOSTIC_RE = re.compile(
    r"UIBattle\|.*(?:Pushing|Popping) UI state|"
    r"SUBMITTING TRANSACTION|Not submitting transaction|" +
    _CLIENT_FAILURE_PATTERN, re.IGNORECASE)


def client_submission_lines(lines, transaction_id: int) -> tuple[str, ...]:
    """Find Unity's transaction log records for one C# transaction ID."""
    expected = int(transaction_id)
    matches = []
    for line in lines:
        match = _SUBMIT_RE.search(line)
        if match and int(match.group(1)) == expected:
            matches.append(line.rstrip("\r\n"))
    return tuple(matches)


def _line_time(line: str, zone: ZoneInfo):
    match = _TIME_RE.search(line)
    if not match:
        return None
    try:
        local = datetime.strptime(
            f"{match.group(1)} {match.group(2)}", "%m/%d/%Y %I:%M:%S %p")
        return local.replace(tzinfo=zone).astimezone(timezone.utc)
    except ValueError:
        return None


def _unity_excerpt(lines, transaction_id: int, received_seq: int,
                   zone: ZoneInfo, radius_seconds: int):
    submissions = client_submission_lines(lines, transaction_id)
    server_time = (datetime.fromtimestamp(received_seq / 1_000_000_000,
                                          timezone.utc)
                   if received_seq else None)
    anchor = None
    if submissions:
        anchor = _line_time(submissions[0], zone)
    if anchor is None:
        anchor = server_time
    if anchor is None:
        return {
            "match": "none",
            "server_received_utc": None,
            "submission_lines": list(submissions),
            "lines": [],
        }

    start = anchor - timedelta(seconds=max(1, radius_seconds))
    end = anchor + timedelta(seconds=max(1, radius_seconds))
    selected = []
    last_time = None
    exception_context = 0
    for line in lines:
        parsed = _line_time(line, zone)
        if parsed is not None:
            last_time = parsed
        in_window = last_time is not None and start <= last_time <= end
        if in_window and _CLIENT_DIAGNOSTIC_RE.search(line):
            selected.append(line.rstrip("\r\n"))
            if _CLIENT_FAILURE_RE.search(line):
                exception_context = 12
            continue
        if in_window and exception_context:
            selected.append(line.rstrip("\r\n"))
            exception_context -= 1

    return {
        "match": "transaction_id" if submissions else "time_window_only",
        "server_received_utc": server_time.isoformat() if server_time else None,
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "submission_lines": list(submissions),
        "lines": selected,
    }


def _client_acceptance(unity: dict[str, Any], expected_ui_states,
                       allow_time_window=False):
    """Evaluate log evidence for a repeatable real-client smoke assertion."""
    state_lines = [line for line in unity.get("lines", ())
                   if re.search(r"UIBattle\|.*(?:Pushing|Popping) UI state",
                                line, re.IGNORECASE)]
    errors = [line for line in unity.get("lines", ())
              if _CLIENT_FAILURE_RE.search(line)]
    expected = tuple(str(state) for state in expected_ui_states)
    ordered_matches = []
    cursor = 0
    for wanted in expected:
        match_index = next((index for index in range(cursor, len(state_lines))
                           if wanted in state_lines[index]), None)
        if match_index is None:
            break
        ordered_matches.append({"expected": wanted,
                                "line": state_lines[match_index]})
        cursor = match_index + 1
    states_match = bool(expected) and len(ordered_matches) == len(expected)
    exact_join = unity.get("match") == "transaction_id"
    time_window_join = unity.get("match") == "time_window_only"
    passed = bool(exact_join and states_match and not errors)
    time_window_passed = bool(
        allow_time_window and time_window_join and states_match and not errors)
    if passed:
        status = "pass"
    elif time_window_passed:
        status = "pass_time_window_only"
    elif not expected or (not exact_join and
                          not (allow_time_window and time_window_join)):
        status = "incomplete"
    else:
        status = "fail"
    return {
        "status": status,
        "transaction_id_join": exact_join,
        "time_window_only_allowed": bool(allow_time_window),
        "expected_ui_states": list(expected),
        "ordered_ui_state_matches": ordered_matches,
        "missing_ui_states": list(expected[len(ordered_matches):]),
        "ui_state_lines": state_lines,
        "error_lines": errors,
    }


def build_trace_bundle(connection: sqlite3.Connection, session_id: int | str,
                       transaction_row_id: int, unity_log_path: str | Path,
                       *, timezone_name="Europe/London",
                       radius_seconds=20,
                       expected_ui_states=(),
                       allow_time_window=False) -> dict[str, Any]:
    """Bundle one server transaction and its linked packets with Unity lines.

    The SQLite connection should be opened read-only. When the Unity log has
    trace-level ``SUBMITTING TRANSACTION`` output, the C# transaction ID is
    an exact join key. Otherwise the client excerpt is explicitly marked as
    a time-window correlation.
    """
    transactions = transaction_capture(connection, session_id)
    transaction = next((item for item in transactions
                        if item["id"] == int(transaction_row_id)), None)
    if transaction is None:
        raise ValueError(
            f"transaction row {transaction_row_id} not found for session {session_id}")

    log_path = Path(unity_log_path)
    log_lines = log_path.read_text(errors="replace").splitlines()
    zone = ZoneInfo(timezone_name)
    decoded_transaction = classify_player_transaction(
        transaction["inner_bytes"])
    client_transaction_id = (
        decoded_transaction.transaction_id
        if decoded_transaction.transaction_id >= 0
        else transaction["transaction_id"])
    unity = _unity_excerpt(
        log_lines, client_transaction_id, transaction["received_seq"],
        zone, int(radius_seconds))
    server_transaction = dict(transaction)
    server_transaction["decoded_transaction_id"] = client_transaction_id
    server_transaction["raw_request_body"] = transaction[
        "raw_request_body"].hex()
    server_transaction["inner_bytes"] = transaction["inner_bytes"].hex()
    packets = packet_capture(
        connection, session_id, transaction_row_id=transaction["id"])
    return {
        "session_id": str(session_id),
        "transaction_row_id": transaction["id"],
        "correlation": unity["match"],
        "transaction": server_transaction,
        "packets": list(packets),
        "projected_packet_batch_count": sum(
            packet["projection_recorded"] for packet in packets),
        "socket_send_count": sum(packet["send_count"] for packet in packets),
        "unconfirmed_projection_count": sum(
            packet["projection_recorded"] and packet["send_count"] == 0
            for packet in packets),
        "send_without_projection_count": sum(
            not packet["projection_recorded"] for packet in packets),
        "client_acceptance": _client_acceptance(
            unity, expected_ui_states, allow_time_window=allow_time_window),
        "unity_log": {"path": str(log_path), **unity},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transaction-row-id", required=True, type=int)
    parser.add_argument("--unity-log", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timezone", default="Europe/London")
    parser.add_argument("--radius-seconds", default=20, type=int)
    parser.add_argument(
        "--expect-ui-state", action="append", default=[],
        help="expected UIBattle state substring; repeat in expected order")
    parser.add_argument(
        "--allow-time-window-correlation", action="store_true",
        help="allow UI assertions to pass with explicit time-window-only join")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve().as_posix()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        bundle = build_trace_bundle(
            connection, args.session_id, args.transaction_row_id,
            args.unity_log, timezone_name=args.timezone,
            radius_seconds=args.radius_seconds,
            expected_ui_states=args.expect_ui_state,
            allow_time_window=args.allow_time_window_correlation)
    finally:
        connection.close()
    output = Path(args.output)
    output.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    print(f"wrote trace bundle {output} ({bundle['correlation']} correlation)")
    acceptance = bundle["client_acceptance"]
    if args.expect_ui_state and acceptance["status"] not in {
            "pass", "pass_time_window_only"}:
        print(f"client acceptance {acceptance['status']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
