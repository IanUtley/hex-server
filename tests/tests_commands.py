"""Focused tests for in-game debug chat commands."""

import sqlite3
import os
import sys
import io
import tempfile
from urllib.parse import parse_qs, urlparse
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

import commands
import db as db_module
import game_engine
import battle_engine


class SessionStub:
    session_id = 7
    session_name = "practice-test"


class HandlerStub:
    user_profile = {"id": 5}
    client_reck_id = 5

    def _card_full_data(self, _game, _scid, template_guid, _instance_id=None):
        return template_guid, "Troop", "Chosen Card", 1, 2, 3, 0


def test_version_command_is_available_without_debug_console():
    old_flags = commands.hconnect_server.PROFILE_FEATURE_FLAGS
    commands.hconnect_server.PROFILE_FEATURE_FLAGS = ()
    try:
        with open(os.path.join(os.path.dirname(commands.__file__), "VERSION"),
                  encoding="utf-8") as version_file:
            expected = version_file.read().strip()
        assert commands.handle_command(HandlerStub(), "!version", "", "") == expected
    finally:
        commands.hconnect_server.PROFILE_FEATURE_FLAGS = old_flags


def test_arena_clear_command_is_available_without_debug_console():
    old_flags = commands.hconnect_server.PROFILE_FEATURE_FLAGS
    commands.hconnect_server.PROFILE_FEATURE_FLAGS = ()
    try:
        with mock.patch("pve_db.db_clear_arena_run") as clear_run:
            result = commands.handle_command(HandlerStub(), "!arena-cleanup", "", "")
        assert result == "Arena run cleared", result
        clear_run.assert_called_once_with(5, conn=commands.hconnect_server._db)
    finally:
        commands.hconnect_server.PROFILE_FEATURE_FLAGS = old_flags


def test_help_lists_only_public_commands_without_debug_console():
    old_flags = commands.hconnect_server.PROFILE_FEATURE_FLAGS
    commands.hconnect_server.PROFILE_FEATURE_FLAGS = ()
    try:
        expected = (
            "Available commands: !help, !commands, !version, !arena-cleanup, "
            "!account-cleanup, !issue <title>")
        assert commands.handle_command(HandlerStub(), "!help", "", "") == expected
        assert commands.handle_command(HandlerStub(), "!commands", "", "") == expected
    finally:
        commands.hconnect_server.PROFILE_FEATURE_FLAGS = old_flags


def test_issue_command_is_available_without_debug_console():
    old_flags = commands.hconnect_server.PROFILE_FEATURE_FLAGS
    commands.hconnect_server.PROFILE_FEATURE_FLAGS = ()
    try:
        with mock.patch.object(
                commands, "_ERROR_LOG_PATHS", ("/tmp/hex-issue-test.log",)), \
                mock.patch.object(commands, "player_log_path", return_value=None), \
                mock.patch.object(
                    commands, "_read_log_tail",
                    return_value=[
                        "[12:00:00] normal diagnostic",
                        "[12:01:02] RulesPort trigger error: test failure",
                    ]):
                result = commands.handle_command(
                    HandlerStub(), "!issue Client crash", "", "")
        assert result.startswith("Click the link below to open")
        assert "[url=HTTPS://github.com/IanUtley/hex-server/issues/new?" in result
        issue_url = result.split("[url=", 1)[1].split("]", 1)[0]
        query = parse_qs(urlparse(issue_url).query)
        assert query["title"] == [
            f"[{commands._version_command()}] Client crash"]
        assert "RulesPort trigger error: test failure" in query["body"][0]
    finally:
        commands.hconnect_server.PROFILE_FEATURE_FLAGS = old_flags


def test_session_logger_writes_bound_session_file():
    old_dir = db_module.SESSION_LOG_DIR
    old_player_dir = db_module.PLAYER_LOG_DIR
    old_log_file = db_module._log_req_file
    with tempfile.TemporaryDirectory() as temp_dir:
        capture = io.StringIO()
        db_module.SESSION_LOG_DIR = temp_dir
        db_module.PLAYER_LOG_DIR = temp_dir
        db_module._log_req_file = capture
        try:
            with db_module.log_session_context(
                    77, player_id=5, participant_ids=(5, 6)):
                assert db_module.log_req("session diagnostic") is True
            session_path = os.path.join(temp_dir, "session-77-p5-p6.log")
            player_path = os.path.join(temp_dir, "player-5.log")
            with open(session_path,
                      encoding="utf-8") as session_log:
                contents = session_log.read()
            with open(player_path, encoding="utf-8") as player_log:
                player_contents = player_log.read()
            assert "session diagnostic" in contents
            assert "session diagnostic" in player_contents
            assert db_module.latest_session_log_path_for_players((5,)) == session_path
        finally:
            db_module.SESSION_LOG_DIR = old_dir
            db_module.PLAYER_LOG_DIR = old_player_dir
            db_module._log_req_file = old_log_file


def test_issue_report_combines_player_and_latest_game_logs():
    old_session_dir = db_module.SESSION_LOG_DIR
    old_player_dir = db_module.PLAYER_LOG_DIR
    with tempfile.TemporaryDirectory() as temp_dir:
        db_module.SESSION_LOG_DIR = temp_dir
        db_module.PLAYER_LOG_DIR = temp_dir
        old_session_path = db_module.session_log_path(76, (5, 6))
        latest_session_path = db_module.session_log_path(77, (5, 6))
        with open(old_session_path, "w", encoding="utf-8") as stream:
            stream.write("[12:00:00] old game\n")
        with open(latest_session_path, "w", encoding="utf-8") as stream:
            stream.write("[12:01:00] latest game\n")
        old_mtime = os.stat(old_session_path).st_mtime
        os.utime(latest_session_path, (old_mtime + 2, old_mtime + 2))
        with open(db_module.player_log_path(5), "w", encoding="utf-8") as stream:
            stream.write("[12:02:00] profile/chat failure\n")
        handler = HandlerStub()
        try:
            with mock.patch.object(commands, "_handler_game_session",
                                   return_value=None):
                report = commands._error_report(handler)
            assert "Recent player log:" in report
            assert "profile/chat failure" in report
            assert "Recent game log: session-77.log" in report
            assert "latest game" in report
            assert "old game" not in report
        finally:
            db_module.SESSION_LOG_DIR = old_session_dir
            db_module.PLAYER_LOG_DIR = old_player_dir


def test_issue_command_reports_session_state_and_tail():
    session = SessionStub()
    session.turn_order = battle_engine.default_state()
    session.turn_order["phase_idx"] = 1
    tail = [
        "[12:02:00] entering FirstMainPhase",
        "[12:02:01] Error resolving trigger: test failure",
    ]
    handler = HandlerStub()
    handler._checkpoint_engine = lambda _session: battle_engine
    expected_phase = getattr(
        battle_engine.current_phase(session.turn_order), "name",
        str(battle_engine.current_phase(session.turn_order)))
    old_flags = commands.hconnect_server.PROFILE_FEATURE_FLAGS
    commands.hconnect_server.PROFILE_FEATURE_FLAGS = ()
    try:
        with mock.patch.object(commands, "_handler_game_session",
                               return_value=session), \
                mock.patch.object(commands, "player_log_path", return_value=None), \
                mock.patch.object(commands, "_session_log_tail",
                                  return_value=tail):
                result = commands.handle_command(
                    handler, "!issue Remote issue", "", "")
        issue_url = result.split("[url=", 1)[1].split("]", 1)[0]
        report = parse_qs(urlparse(issue_url).query)["body"][0]
        assert "Session diagnostics: id=7" in report
        assert f"phase={expected_phase}" in report
        assert "Error resolving trigger" in report
        assert report.count("Error resolving trigger") == 1
    finally:
        commands.hconnect_server.PROFILE_FEATURE_FLAGS = old_flags


def test_help_lists_commands_without_active_game_when_console_enabled():
    old_flags = commands.hconnect_server.PROFILE_FEATURE_FLAGS
    commands.hconnect_server.PROFILE_FEATURE_FLAGS = ("allowcon",)
    try:
        result = commands.handle_command(HandlerStub(), "!help", "", "")
        assert result.startswith("=== Commands ===")
        assert "!pass — advance turn phase" in result
        assert result != "No active game"
    finally:
        commands.hconnect_server.PROFILE_FEATURE_FLAGS = old_flags


class BattleSessionStub(SessionStub):
    server_id = 1

    def __init__(self, state):
        self.turn_order = state

    def _persist(self):
        pass


def test_top_moves_named_hand_card_to_deck_position_zero():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE card_templates (
            guid TEXT PRIMARY KEY, name TEXT, card_type TEXT);
        CREATE TABLE game_cards (
            session_id INTEGER, user_id INTEGER, card_uid INTEGER,
            template_guid TEXT, card_template_id TEXT, location TEXT,
            position INTEGER, card_state INTEGER);
    """)
    db.executemany("INSERT INTO card_templates VALUES (?, ?, ?)", [
        ("00000000-0000-0000-0000-000000000101", "Chosen Card", "Troop"),
        ("00000000-0000-0000-0000-000000000102", "Deck A", "Troop"),
        ("00000000-0000-0000-0000-000000000103", "Deck B", "Troop"),
    ])
    db.executemany("INSERT INTO game_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (7, 5, 101, "00000000-0000-0000-0000-000000000101", "00000000-0000-0000-0000-000000000101", "hand", 0, 0),
        (7, 5, 102, "00000000-0000-0000-0000-000000000102", "00000000-0000-0000-0000-000000000102", "deck", 0, 0),
        (7, 5, 103, "00000000-0000-0000-0000-000000000103", "00000000-0000-0000-0000-000000000103", "deck", 1, 0),
    ])
    db.commit()
    old_db = commands.hconnect_server._db
    old_send = commands._send_game_events
    commands.hconnect_server._db = db
    commands._send_game_events = lambda *_args: None
    try:
        result = commands._dispatch(
            HandlerStub(), "top", ["Chosen"], SessionStub(),
            game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000),
            "", "")
        assert result == "Put Chosen Card on top of deck", result
        rows = db.execute(
            "SELECT card_uid, location, position FROM game_cards "
            "WHERE session_id=7 AND user_id=5 ORDER BY position"
        ).fetchall()
        assert rows == [
            (101, "deck", 0), (102, "deck", 1), (103, "deck", 2)
        ], rows
    finally:
        commands.hconnect_server._db = old_db
        commands._send_game_events = old_send
        db.close()


def test_resource_command_persists_authoritative_pools():
    session = BattleSessionStub(battle_engine.default_state())
    session.turn_order["player_resources"] = 2
    session.turn_order["player_total_resources"] = 3
    old_send = commands._send_game_events
    packets = []
    commands._send_game_events = lambda *args: packets.append(args[1])
    try:
        result = commands._dispatch(
            HandlerStub(), "resource", ["7", "9"], session,
            game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000),
            "", "")
        assert result == "Resources: 7/9 for me"
        state = battle_engine.load_state(session)
        assert state["player_resources"] == 7
        assert state["player_total_resources"] == 9
        events = packets[0].events
        assert events[-1].resources == 7
        assert events[-1].total_resources == 9
    finally:
        commands._send_game_events = old_send


def test_threshold_command_reports_delta_from_previous_value():
    state = battle_engine.default_state()
    state["player_threshold"] = {4: 1}
    session = BattleSessionStub(state)
    old_send = commands._send_game_events
    packets = []
    commands._send_game_events = lambda *args: packets.append(args[1])
    try:
        commands._dispatch(
            HandlerStub(), "threshold", ["me", "0", "3", "0", "0", "0", "0"],
            session, game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000),
            "", "")
        assert battle_engine.load_state(session)["player_threshold"][4] == 3
        threshold_events = [
            event for event in packets[0].events
            if isinstance(event, game_engine.PlayerResourceThresholdChangedSessionEventArgs)
        ]
        assert len(threshold_events) == 1
        assert threshold_events[0].delta == 2
        assert threshold_events[0].new_value == 3
    finally:
        commands._send_game_events = old_send


if __name__ == "__main__":
    test_issue_command_is_available_without_debug_console()
    test_session_logger_writes_bound_session_file()
    test_issue_report_combines_player_and_latest_game_logs()
    test_issue_command_reports_session_state_and_tail()
    test_top_moves_named_hand_card_to_deck_position_zero()
    test_resource_command_persists_authoritative_pools()
    test_threshold_command_reports_delta_from_previous_value()
    print("PASS !top command")
