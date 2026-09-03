"""Tournament lobby result data tests."""

import sqlite3
import os
import sys
import gzip
import json
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import db
import game_engine
import gamemodes.tournament_engine as tournament_engine
import services.tournament_game as tournament_game
from encoder import decompress_gzip


def test_pvp_concede_ends_for_both_players():
    class Session:
        session_id = 42765
        session_name = "tourney-7"

    class Handler:
        client_reck_id = 1002

    with mock.patch.object(tournament_game, "db_game_session_pids",
                           return_value=[1001, 1002]), \
            mock.patch.object(tournament_game, "pvp_load_state",
                              return_value={"phase": 10}), \
            mock.patch.object(tournament_game, "_pvp_end_game") as end_game:
        assert tournament_game.pvp_concede(Handler(), Session())
        end_game.assert_called_once_with(
            mock.ANY, {"phase": 10}, 1001, 1002, "player conceded"
        )


def test_tournament_session_pids_ignore_non_player_card_owners():
    test_db = sqlite3.connect(":memory:")
    previous_db = db._db
    try:
        test_db.executescript("""
            CREATE TABLE tournaments (id INTEGER PRIMARY KEY, session_id TEXT);
            CREATE TABLE tournament_signups (
                tournament_id INTEGER, player_uid INTEGER
            );
            CREATE TABLE game_cards (session_id INTEGER, user_id INTEGER);
        """)
        test_db.execute("INSERT INTO tournaments VALUES (?, ?)",
                        (10118, "47629"))
        test_db.executemany(
            "INSERT INTO tournament_signups VALUES (?, ?)",
            [(10118, 1925190388022160), (10118, 2408558011085730)])
        # This is a card-owner row, not a third participant.
        test_db.execute("INSERT INTO game_cards VALUES (?, ?)",
                        (47629, 5176002727556651092))
        db._db = test_db

        assert db.db_game_session_pids(47629) == [
            1925190388022160, 2408558011085730]
    finally:
        db._db = previous_db
        test_db.close()


def test_orphaned_started_tournaments_are_closed_but_live_and_waiting_remain():
    previous_db = db._db
    test_db = sqlite3.connect(":memory:")
    try:
        test_db.executescript(
            """
            CREATE TABLE tournaments (
                id INTEGER PRIMARY KEY, status TEXT, session_id TEXT
            );
            CREATE TABLE game_sessions (session_id TEXT PRIMARY KEY);
            INSERT INTO tournaments VALUES
                (10001, 'started', 'missing-session'),
                (10002, 'started', 'live-session'),
                (10003, 'waiting', NULL),
                (10004, 'closed', 'old-session');
            INSERT INTO game_sessions VALUES ('live-session');
            """
        )
        db._db = test_db

        assert db.db_tournament_close_orphaned_started() == 1
        rows = test_db.execute(
            "SELECT id, status FROM tournaments ORDER BY id"
        ).fetchall()
        assert rows == [
            (10001, "closed"), (10002, "started"),
            (10003, "waiting"), (10004, "closed"),
        ]
    finally:
        db._db = previous_db
        test_db.close()


def test_old_tournaments_close_and_remove_only_their_game_state():
    previous_db = db._db
    test_db = sqlite3.connect(":memory:")
    try:
        test_db.executescript(
            """
            CREATE TABLE tournaments (
                id INTEGER PRIMARY KEY, status TEXT, session_id TEXT,
                created_at TEXT
            );
            CREATE TABLE game_sessions (session_id TEXT PRIMARY KEY);
            CREATE TABLE game_cards (session_id TEXT, card_uid INTEGER);
            INSERT INTO tournaments VALUES
                (10001, 'started', 'old-session', datetime('now', '-2 days')),
                (10002, 'waiting', NULL, datetime('now', '-2 days')),
                (10003, 'started', 'new-session', datetime('now'));
            INSERT INTO game_sessions VALUES ('old-session'), ('new-session');
            INSERT INTO game_cards VALUES ('old-session', 1), ('new-session', 2);
            """
        )
        db._db = test_db

        assert db.db_tournament_cleanup_old() == {
            "tournaments_closed": 2,
            "game_sessions_removed": 1,
            "game_cards_removed": 1,
        }
        assert test_db.execute(
            "SELECT id, status FROM tournaments ORDER BY id"
        ).fetchall() == [
            (10001, "closed"), (10002, "closed"), (10003, "started")
        ]
        assert test_db.execute(
            "SELECT session_id FROM game_sessions ORDER BY session_id"
        ).fetchall() == [("new-session",)]
        assert test_db.execute(
            "SELECT session_id FROM game_cards ORDER BY session_id"
        ).fetchall() == [("new-session",)]
    finally:
        db._db = previous_db
        test_db.close()


def test_pvp_result_is_published_before_game_over():
    events = []

    class Session:
        session_id = 42765
        server_id = 1

        def set_state(self, state):
            events.append(state)

    handlers = {1001: object(), 1002: object()}
    with mock.patch.object(tournament_game, "db_game_session_pids",
                           return_value=[1001, 1002]), \
            mock.patch.object(tournament_game, "player_handlers", handlers), \
            mock.patch.object(
                tournament_game, "record_tournament_game_result",
                side_effect=lambda *_args: events.append("result")), \
            mock.patch("commands.push_battle_game_end",
                       side_effect=lambda *_args: events.append("game_end")), \
            mock.patch.object(tournament_game, "pvp_discard_session_lock"):
        tournament_game._pvp_end_game(
            Session(), {"phase": 10}, 1001, 1002, "player conceded"
        )

    assert events[:3] == ["result", "game_end", "game_end"]


def test_pvp_champion_damage_uses_target_player_health():
    from abilities.framework.bom import _deal_damage

    class Session:
        session_id = 42765

    class Handler:
        user_profile = {"id": 1001}
        _player_champ_scid = None
        _ai_champ_scid = None

    player_uid = game_engine.UID.make(244, 1001)
    opponent_uid = game_engine.UID.make(244, 1002)
    game = game_engine.Game(Session.session_id, player_uid, opponent_uid)
    state = {
        "pvp": True,
        "pids": [1001, 1002],
        "champ_map": {"1001": 7001, "1002": 7002},
        "pvp_health_map": {1001: "player_health", 1002: "ai_health"},
        "player_health": 20,
        "ai_health": 20,
    }
    with mock.patch("abilities.framework.triggers.resolve_triggers",
                    return_value=""):
        _deal_damage(game, Session(), None, Handler(), player_uid,
                     opponent_uid, state, 7002, 3)
    assert state["player_health"] == 20
    assert state["ai_health"] == 17
    assert game.events[-1].player_id.uid64 == opponent_uid.uid64


def test_completed_match_is_visible_in_tournament_lobby():
    previous_db = db._db
    previous_engine_db = tournament_engine._db
    test_db = sqlite3.connect(":memory:")
    try:
        test_db.executescript(
            """
            CREATE TABLE tournament_types (
                id INTEGER PRIMARY KEY, name TEXT, style TEXT, format INTEGER,
                min_players INTEGER, max_players INTEGER, games_count INTEGER,
                set_id TEXT
            );
            CREATE TABLE tournaments (
                id INTEGER PRIMARY KEY, type_id INTEGER, status TEXT,
                players_json TEXT, session_id TEXT, created_at TEXT
            );
            CREATE TABLE tournament_signups (
                id INTEGER PRIMARY KEY, tournament_id INTEGER, player_uid INTEGER,
                player_name TEXT, deck_id INTEGER, entry_group INTEGER,
                fee_paid INTEGER, status TEXT, created_at TEXT
            );
            CREATE TABLE tournament_matches (
                id INTEGER PRIMARY KEY, tournament_id INTEGER, round_id INTEGER,
                match_id INTEGER, player1_uid INTEGER, player2_uid INTEGER,
                session_id TEXT, state TEXT, status TEXT, start_time INTEGER,
                end_time INTEGER, game1_winner INTEGER, game2_winner INTEGER,
                game3_winner INTEGER
            );
            INSERT INTO tournament_types VALUES
                (1, '1v1 Immortal - Best of 1', 'se', 16, 2, 2, 1, NULL);
            INSERT INTO tournaments VALUES
                (10007, 1, 'complete', '{}', '42765', '2026-08-18 07:00:00');
            INSERT INTO tournament_signups VALUES
                (1, 10007, 1001, 'Alice', 11, 0, 0, 'active', ''),
                (2, 10007, 1002, 'Bob', 12, 0, 0, 'active', '');
            INSERT INTO tournament_matches VALUES
                (3, 10007, 1, 1, 1001, 1002, '42765', 'PlayGame',
                 'InProgress', 638900000000000000, 0, 0, 0, 0);
            """
        )
        db._db = test_db
        tournament_engine._db = test_db

        class Session:
            session_id = 42765
            session_name = "tourney-10007"

        assert tournament_engine.record_tournament_game_result(
            Session(), 1001, 1002
        )

        payload = tournament_engine.build_tournament_info_data(
            "tourn:tournament-10007"
        )["tourn:tournament-10007"]

        assert payload["state"] == "Complete"
        assert payload["completionType"] == 1
        assert payload["matches"]["3"]["player1id"] == "p1001"
        assert payload["matches"]["3"]["player2id"] == "p1002"
        assert payload["matches"]["3"]["game1Winner"] == 1001
        assert payload["players"]["1001"]["wins"] == 1
        assert payload["players"]["1001"]["rank"] == 1
        assert payload["players"]["1002"]["state"] == "Eliminated"
        assert payload["players"]["1002"]["eliminationReason"] == 3
        assert payload["players"]["1002"]["eliminationRound"] == 1
        assert payload["players"]["1001"]["eliminationReason"] == 0
        assert payload["players"]["1001"]["eliminationRound"] == 0
        description = payload["description"]
        assert description["numPlayers"] == 2
        assert description["minPlayers"] == 2
        assert description["maxPlayers"] == 2
        assert description["format"] == 16
        assert description["style"] == 0
        assert description["startTime"] == 638900000000000000
        assert description["endTime"] > description["startTime"]
        assert payload["players"]["1001"]["gwr"] == 1.0
        assert payload["players"]["1001"]["omwr"] == 1.0 / 3.0
        assert payload["players"]["1001"]["oomwr"] == 1.0
        assert payload["players"]["1002"]["gwr"] == 0.0
        assert payload["players"]["1002"]["omwr"] == 1.0
        assert payload["players"]["1002"]["oomwr"] == 1.0 / 3.0

        class Handler:
            scnt = 0
            sid = "0"
            client_reck_id = 1001

            def send(self, _headers, data=None, **kwargs):
                self.data = data if data is not None else kwargs.get("body")

        lobby_handler = Handler()
        with mock.patch.object(tournament_engine.tournament_server,
                               "get_active_rooms", return_value=[]):
            tournament_engine.push_tournament_room_data(
                lobby_handler, "tourn:lobby_full", "")
        lobby_json = gzip.decompress(
            lobby_handler.data[lobby_handler.data.find(b"\x1f\x8b"):])
        lobby_payload = json.loads(lobby_json)
        assert lobby_payload[0][3] > 600_000_000_000_000_000
        lobby = lobby_payload[0][2]["tournament-10007"]
        assert lobby["state"] == "Complete"
        assert lobby["numPlayers"] == 2
        assert lobby["roomType"] == ""

        unrelated_handler = Handler()
        unrelated_handler.client_reck_id = 1003
        with mock.patch.object(tournament_engine.tournament_server,
                               "get_active_rooms", return_value=[]):
            tournament_engine.push_tournament_room_data(
                unrelated_handler, "tourn:lobby_full", "")
        unrelated_json = gzip.decompress(
            unrelated_handler.data[unrelated_handler.data.find(b"\x1f\x8b"):])
        unrelated_lobby = json.loads(unrelated_json)[0][2]
        assert "tournament-10007" not in unrelated_lobby
    finally:
        tournament_engine._db = previous_engine_db
        db._db = previous_db
        test_db.close()


def test_forfeit_completes_active_bo1_match():
    previous_db = db._db
    previous_engine_db = tournament_engine._db
    test_db = sqlite3.connect(":memory:")
    try:
        test_db.executescript(
            """
            CREATE TABLE tournament_types (
                id INTEGER PRIMARY KEY, name TEXT, style TEXT, format INTEGER,
                min_players INTEGER, max_players INTEGER, games_count INTEGER,
                set_id TEXT
            );
            CREATE TABLE tournaments (
                id INTEGER PRIMARY KEY, type_id INTEGER, status TEXT,
                players_json TEXT, session_id TEXT, created_at TEXT
            );
            CREATE TABLE tournament_signups (
                id INTEGER PRIMARY KEY, tournament_id INTEGER, player_uid INTEGER,
                player_name TEXT, deck_id INTEGER, entry_group INTEGER,
                fee_paid INTEGER, status TEXT, created_at TEXT
            );
            CREATE TABLE tournament_matches (
                id INTEGER PRIMARY KEY, tournament_id INTEGER, round_id INTEGER,
                match_id INTEGER, player1_uid INTEGER, player2_uid INTEGER,
                session_id TEXT, state TEXT, status TEXT, start_time INTEGER,
                end_time INTEGER, game1_winner INTEGER, game2_winner INTEGER,
                game3_winner INTEGER
            );
            INSERT INTO tournament_types VALUES
                (1, '1v1 Immortal - Best of 1', 'se', 16, 2, 2, 1, NULL);
            INSERT INTO tournaments VALUES
                (10007, 1, 'started', '{}', '42765', '2026-08-18 07:00:00');
            INSERT INTO tournament_signups VALUES
                (1, 10007, 1001, 'Alice', 11, 0, 0, 'active', ''),
                (2, 10007, 1002, 'Bob', 12, 0, 0, 'active', '');
            INSERT INTO tournament_matches VALUES
                (3, 10007, 1, 1, 1001, 1002, '42765', 'PlayGame',
                 'InProgress', 638900000000000000, 0, 0, 0, 0);
            """
        )
        db._db = test_db
        tournament_engine._db = test_db

        assert tournament_engine.record_tournament_forfeit(10007, 1002)
        match = test_db.execute(
            "SELECT state, status, game1_winner FROM tournament_matches "
            "WHERE id=3").fetchone()
        assert match == ("Complete", "Complete", 1001)
        room = test_db.execute(
            "SELECT status FROM tournaments WHERE id=10007").fetchone()
        assert room == ("complete",)
    finally:
        tournament_engine._db = previous_engine_db
        db._db = previous_db
        test_db.close()


def test_complete_status_event_uses_client_tournament_enums():
    class Handler:
        scnt = 0
        sid = "0"
        client_req_session_id = "00000000-0000-0000-0000-000000000000"

        def send(self, _headers, data):
            self.data = data

    handler = Handler()
    tournament_engine._push_tournament_status_event(handler, 10007, True)
    body = decompress_gzip(handler.data[handler.data.find(b"\x1f\x8b"):])
    assert b"Game.Shared.Tournaments.ETournamentStatus" in body
    assert b"Game.Shared.Tournaments.ETournamentCompletionType" in body
    assert b"07000000" in body  # ETournamentStatus.Closed
    assert b"01000000" in body  # ETournamentCompletionType.Complete


def test_completed_result_publishes_final_full_snapshot_synchronously():
    handler = object()
    with mock.patch.object(
                tournament_engine, "_push_tournament_status_event") as status, \
            mock.patch.object(
                tournament_engine, "push_tournament_room_data") as room, \
            mock.patch.object(tournament_engine.threading, "Timer") as timer:
        tournament_engine._publish_tournament_result(
            10007, [{"player_uid": 1001}], True,
            {1001: handler},
        )

    status.assert_called_once_with(handler, 10007, True)
    assert room.call_args_list == [
        mock.call(handler, "tourn:tournament-10007_full", ""),
        mock.call(handler, "tourn:lobby_full", "", include_tournament_id=10007),
        mock.call(handler, "tourn:tournament-10007_full", ""),
        mock.call(handler, "tourn:lobby_full", "", include_tournament_id=10007),
    ]
    timer.assert_not_called()


if __name__ == "__main__":
    test_pvp_concede_ends_for_both_players()
    test_tournament_session_pids_ignore_non_player_card_owners()
    test_old_tournaments_close_and_remove_only_their_game_state()
    test_pvp_champion_damage_uses_target_player_health()
    test_completed_match_is_visible_in_tournament_lobby()
    test_forfeit_completes_active_bo1_match()
    test_complete_status_event_uses_client_tournament_enums()
    test_completed_result_publishes_final_full_snapshot_synchronously()
    print("tournament lobby tests passed")
