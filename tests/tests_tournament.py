"""Tournament lobby result data tests."""

import sqlite3
import os
import sys
import gzip
import json
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

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


def test_stale_pvp_disconnect_does_not_notify_current_handler():
    class Session:
        session_id = 42765
        session_name = "tourney-7"
        state = "started"

    stale = object()
    current = object()
    previous_handlers = tournament_game.player_handlers
    try:
        tournament_game.player_handlers = {1001: current, 1002: object()}
        with mock.patch("game_session.find_session_by_player",
                        return_value=Session()), \
                mock.patch.object(tournament_game, "pvp_load_state",
                                  return_value={"pvp": True}), \
                mock.patch.object(tournament_game, "db_game_session_pids",
                                  return_value=[1001, 1002]):
            assert not tournament_game.notify_pvp_player_disconnected(
                1001, stale)
    finally:
        tournament_game.player_handlers = previous_handlers


def test_pvp_disconnect_hands_priority_to_survivor():
    class Session:
        session_id = 42765
        session_name = "tourney-7"
        state = "started"

    survivor_handler = object()
    previous_handlers = tournament_game.player_handlers
    state = {
        "pvp": True,
        "phase": 10,
        "turn_pid": 1001,
        "priority_pid": 1002,
    }
    try:
        tournament_game.player_handlers = {1001: survivor_handler}
        with mock.patch.object(tournament_game, "pvp_session_lock"), \
                mock.patch.object(tournament_game, "pvp_load_state",
                                   side_effect=lambda _session: state), \
                mock.patch.object(tournament_game, "pvp_save_state"), \
                mock.patch.object(tournament_game, "db_game_session_pids",
                                   return_value=[1001, 1002]), \
                mock.patch.object(tournament_game, "_send_pvp_packet"), \
                mock.patch.object(tournament_game, "pvp_push_main_phase_options"):
            assert tournament_game._pvp_reassign_priority_after_disconnect(
                Session(), 1002, 1001)
        assert state["priority_pid"] == 1001
    finally:
        tournament_game.player_handlers = previous_handlers


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
                id INTEGER PRIMARY KEY, type_id INTEGER, status TEXT, session_id TEXT,
                created_at TEXT
            );
            CREATE TABLE tournament_types (
                id INTEGER PRIMARY KEY, style TEXT
            );
            CREATE TABLE game_sessions (session_id TEXT PRIMARY KEY, state TEXT);
            CREATE TABLE game_cards (session_id TEXT, card_uid INTEGER);
            CREATE TABLE session_events (session_id TEXT);
            CREATE TABLE game_replays (session_id TEXT, status TEXT);
            INSERT INTO tournament_types VALUES (1, 'se'), (2, 'async');
            INSERT INTO tournaments VALUES
                (10001, 1, 'started', 'old-session', datetime('now', '-2 days')),
                (10002, 1, 'waiting', NULL, datetime('now', '-2 days')),
                (10003, 1, 'started', 'new-session', datetime('now')),
                (10004, 2, 'waiting', NULL, datetime('now', '-2 days'));
            INSERT INTO game_sessions VALUES
                ('old-session', 'started'), ('new-session', 'started');
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
            (10001, "closed"), (10002, "closed"), (10003, "started"),
            (10004, "waiting")
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


def test_tournament_champion_setup_publishes_the_charge_power_catalog():
    """Corinth's charge power is a synthetic champion card.

    ``AbilityCanBeActivatedRequirement`` gates it on the ability catalog
    projected on the champion.  The tournament setup recorded only the
    champion SessionCardId/GUID, so the native facts bridge read an empty
    catalog and rejected every champion power (live: Corinth's charge power).
    """
    from rules_port.runtime_adapter import PvpRuntimeFacts
    CORINTH = "93d8a5ca-d999-461d-84d8-30975ef4dfc1"
    CHARGE = "286f1891-4404-585e-4fb6-bd9f783f222b"

    class Handler:
        client_reck_id = 2408558011085730

    handler = Handler()
    tournament_game._pvp_set_handler_champions(
        handler, game_engine.SessionCardId(game_engine.UID(10753)),
        game_engine.SessionCardId(game_engine.UID(5377)), CORINTH, CORINTH)
    catalog = {str(value.guid).lower()
               for value in handler._player_champ_abilities}
    assert CHARGE in catalog, catalog

    state = {"pvp": True, "champ_map": {"2408558011085730": 10753,
                                        "1925190388022160": 5377}}
    player_uid = game_engine.UID.make(244, 2408558011085730)
    facts = PvpRuntimeFacts(4242, state, player_uid=player_uid,
                            ai_uid=game_engine.UID.make(244, 1925190388022160))
    facts.client_player_uid = player_uid
    facts.player_champion_card_id = handler._player_champ_scid
    facts.ai_champion_card_id = handler._ai_champ_scid
    facts.ai_champion_ability_guids = list(handler._ai_champ_ability_guids)
    # Pre-fix shape: an empty catalog refused the authored charge power.
    facts.player_champion_ability_guids = ()
    assert not facts.can_activate_champion_ability(10753, player_uid, CHARGE)
    # The catalog published by champion setup lets the gate accept it.
    facts.player_champion_ability_guids = tuple(catalog)
    assert facts.can_activate_champion_ability(10753, player_uid, CHARGE)


def test_pvp_ability_item_releases_its_stack_mirror():
    """A resolved champion ability must not re-queue itself as the next item.

    The persisted stack is only the reconnect/wire mirror.  The ability
    projection drops this item's entry by identity, which is what stops the
    native chain resolver from re-queueing the item it just resolved.
    """
    item = {"kind": "ability",
            "ability_guid": "286f1891-4404-585e-4fb6-bd9f783f222b",
            "source_uid": 9003, "owner_id": 1001, "instance_id": 31}
    state = {"pvp": True, "pids": [1001, 1002], "turn_pid": 1001,
             "phase": int(game_engine.ETurnPhases.FirstMainPhase),
             "champ_map": {"1001": 9001, "1002": 9002},
             "hp_1001": 20, "hp_1002": 20,
             "stack": [dict(item)], "stack_passed": []}

    class Session:
        session_id = 42765
        session_name = "tourney-7"
        turn_order = state
        players = ()

        def _persist(self):
            pass

    class Handler:
        client_reck_id = 1001
        _card_full_data = lambda *args, **kwargs: (None,) * 7

    resolved = []
    with mock.patch.object(tournament_game, "db_game_session_pids",
                           return_value=[1001, 1002]), \
            mock.patch.object(tournament_game, "_pvp_send_same_events"), \
            mock.patch.object(tournament_game, "_pvp_populate_game_state"), \
            mock.patch.object(tournament_game, "_pvp_dispatch_triggers"), \
            mock.patch("rules_port.resolution.resolve_port_ability",
                       lambda *args, **kwargs: resolved.append(1)):
        ok = tournament_game._pvp_resolve_ability_item(
            Session(), state, Handler(), 1001, dict(item))
    assert ok is True
    assert resolved == [1], resolved
    assert not state.get("stack"), state


def test_pvp_session_pids_fall_back_to_the_checkpoint_and_transport():
    """A tourney host names its participants before the card rows exist.

    ``db_game_session_pids`` reads ``game_cards``, which is written with the
    opening hands.  The first gameplay transaction of a resumed match can
    therefore arrive with fewer than two participant rows; the attach used to
    give up and drop the session onto the legacy stack.  The checkpoint and
    the transport metadata both name both players.
    """

    class Session:
        players = ()

    state = {"pvp": True, "pids": [1001, 1002]}
    assert tournament_game._pvp_session_pids(Session(), state) == [1001, 1002]
    assert tournament_game._pvp_session_pids(
        Session(), {"pvp": True, "champ_map": {"7": 9001, "9": 9002}}
    ) == [7, 9]
    typed = [(game_engine.UID.make(244, 31), 0),
             (game_engine.UID.make(244, 32), 1)]
    assert tournament_game._pvp_session_pids(
        SimpleNamespace(players=typed), {"pvp": True}) == [31, 32]
    # A session that names only one participant is still not a two-human game.
    assert tournament_game._pvp_session_pids(
        Session(), {"pvp": True, "pids": [1001]}) == []


def test_pvp_resolves_a_stack_item_without_a_native_host():
    """The no-host pass path resolves through the shared chain seam.

    A tourney session without a native host used to run its own copy of the
    chain-item resolution (``_pvp_resolve_chain``).  It now goes through
    ``rules_port.chain_items``, so the authored effect resolvers and the
    picker-continuation marker cannot drift from the attached path.
    """
    item = {"kind": "trigger",
            "ability_guid": "9853659b-89f4-1e16-f940-67bdb37f5729",
            "source_uid": 9003, "source_owner_uid": 1001,
            "trigger_target_uid": 9003, "instance_id": 21}
    state = {"pvp": True, "pids": [1001, 1002], "turn_pid": 1001,
             "phase": int(game_engine.ETurnPhases.FirstMainPhase),
             "stack": [dict(item)], "stack_passed": [],
             "champ_map": {"1001": 9001, "1002": 9002},
             "hp_1001": 20, "hp_1002": 20}

    class Session:
        session_id = 42765
        session_name = "tourney-7"
        turn_order = state
        players = ()

        def _persist(self):
            pass

    class Handler:
        _card_full_data = lambda *args, **kwargs: (None,) * 7

    calls = []

    def fake_trigger(*_args, **_kwargs):
        calls.append("bom")

    with mock.patch.object(tournament_game, "db_game_session_pids",
                           return_value=[1001, 1002]), \
            mock.patch.object(tournament_game, "_pvp_send_same_events"), \
            mock.patch("rules_port.resolution.resolve_port_trigger",
                       fake_trigger):
        resolved = tournament_game._pvp_resolve_stack_item(
            Session(), state, Handler(), 1001)
    assert resolved is True
    assert calls == ["bom"], calls
    assert not state.get("stack"), state
    assert state.get("priority_pid") == 1001, state


def test_pvp_chain_item_uses_the_shared_seam_and_its_picker_marker():
    """PvP resolves chain items through the shared RulesPort seam.

    Practice/PvE and tournament PvP used to keep a copy of the chain-item
    lifecycle each, and the PvP copy never produced
    ``completed_chain_instance_id``: a Deathcry deck search re-ran its BOM
    once per remaining candidate (Darkspire Priestess asked four times for a
    single death), and PvP carried three overlapping per-kind resolvers.
    """
    from rules_port import chain_items
    from rules_port.actions import AbilityResolutionState

    state = {
        "pvp": True, "pids": [1001, 1002], "turn_pid": 1001,
        "phase": int(game_engine.ETurnPhases.FirstMainPhase),
        "stack": [], "stack_passed": [],
        "champ_map": {"1001": 9001, "1002": 9002},
        "hp_1001": 20, "hp_1002": 20,
    }
    item = {"kind": "trigger",
            "ability_guid": "9853659b-89f4-1e16-f940-67bdb37f5729",
            "source_uid": 9003, "source_owner_uid": 1001,
            "trigger_target_uid": 9003, "instance_id": 12}
    state["stack"].append(dict(item))
    ability = SimpleNamespace(descriptor=dict(item), instance_id=12,
                             ignores_chain=False)

    class Session:
        session_id = 42765
        session_name = "tourney-7"
        turn_order = state

        def _persist(self):
            pass

    class Handler:
        _card_full_data = lambda *args, **kwargs: (None,) * 7

    session = Session()
    host = tournament_game._PvpChainHost(Handler(), session, state, 1001, 1002)
    calls = []

    def fake_trigger(*_args, **_kwargs):
        calls.append("bom")
        # The authored BOM parks on its deck-search picker.
        state["resolution_paused"] = True

    pl_uid = game_engine.UID.make(244, 1001)
    ai_uid = game_engine.UID.make(244, 1002)
    with mock.patch.object(tournament_game, "db_game_session_pids",
                           return_value=[1001, 1002]), \
            mock.patch.object(tournament_game, "_pvp_send_same_events"), \
            mock.patch("rules_port.resolution.resolve_port_trigger",
                       fake_trigger):
        first = chain_items.resolve_chain_item(
            host, None, session, db._db, ability, pl_uid, ai_uid)
        assert first is AbilityResolutionState.WAITING_FOR_INPUT, first
        assert calls == ["bom"], calls
        # The picker's chain item is held with the shared marker.
        assert state["paused_chain_instance_id"] == 12, state
        assert state["resolution_paused"] is True, state
        # The picker answer resolves that BOM and marks the chain item.
        state.pop("resolution_paused")
        state.pop("paused_chain_instance_id", None)
        state["completed_chain_instance_id"] = 12
        # Production builds the projection host per request, so the next pass
        # reads the marker that the answer just wrote.
        answer_host = tournament_game._PvpChainHost(
            Handler(), session, state, 1001, 1002)
        second = chain_items.resolve_chain_item(
            answer_host, None, session, db._db, ability, pl_uid, ai_uid)
    assert second is AbilityResolutionState.COMPLETED, second
    assert calls == ["bom"], calls
    assert "completed_chain_instance_id" not in state, state
    assert "paused_chain_instance_id" not in state, state
    assert state["priority_pid"] == 1001, state


def test_corinth_format_never_deals_a_draw_step():
    """Merry-Melee Corinth has no draw step.

    The native Prep branch reaches Draw only when the port's
    ``active_player_skips_draw`` fact is clear, and the draw projection must
    not deal a card even if a phase cursor still reaches Draw.  Live symptom:
    the turn player drew a card while the champion power says "Skip your draw
    phase".
    """
    from rules_port.pvp_lifecycle import skips_draw_phase

    state = {"pvp": True, "corinth_mode": True, "skip_draw_phase": True,
             "turn_pid": 2408558011085730, "turn_number": 3,
             "draws_first_pid": 1925190388022160}
    assert skips_draw_phase(state)
    phases = tournament_game._pvp_turn_phase_list(
        state, state["turn_pid"], False)
    assert game_engine.ETurnPhases.Draw not in phases, phases

    class Session:
        session_id = 199437

        def _persist(self):
            pass

    with mock.patch.object(tournament_game, "db_game_session_pids",
                           return_value=[2408558011085730,
                                         1925190388022160]), \
            mock.patch.object(tournament_game, "pvp_save_state"), \
            mock.patch.object(tournament_game, "db_game_draw_cards",
                              return_value=[(1, db._db.execute(
                                  "SELECT guid FROM card_templates "
                                  "WHERE card_type='Troop' LIMIT 1"
                              ).fetchone()[0])]) as draw:
        assert tournament_game._pvp_run_draw(Session(), state) is None
        draw.assert_not_called()
        # An ordinary format still draws on turn 3.
        ordinary = dict(state, corinth_mode=False, skip_draw_phase=False)
        tournament_game._pvp_run_draw(Session(), ordinary)
        assert draw.called


if __name__ == "__main__":
    test_pvp_concede_ends_for_both_players()
    test_tournament_session_pids_ignore_non_player_card_owners()
    test_old_tournaments_close_and_remove_only_their_game_state()
    test_pvp_champion_damage_uses_target_player_health()
    test_completed_match_is_visible_in_tournament_lobby()
    test_forfeit_completes_active_bo1_match()
    test_complete_status_event_uses_client_tournament_enums()
    test_completed_result_publishes_final_full_snapshot_synchronously()
    test_tournament_champion_setup_publishes_the_charge_power_catalog()
    test_pvp_ability_item_releases_its_stack_mirror()
    test_pvp_session_pids_fall_back_to_the_checkpoint_and_transport()
    test_pvp_resolves_a_stack_item_without_a_native_host()
    test_pvp_chain_item_uses_the_shared_seam_and_its_picker_marker()
    test_corinth_format_never_deals_a_draw_step()
    print("tournament lobby tests passed")
