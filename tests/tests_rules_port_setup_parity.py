"""Focused native setup transaction checks for the U4 parity backlog."""

import os
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_db import fresh_database

# The RulesPort session imports shared runtime modules, which bind the active
# SQLite connection during import. Keep this focused check isolated.
fresh_database()

import game_engine
from rules_port.kernel import PriorityWindowAction, TurnPhasePlayers
from rules_port.session import (AuthoritativeSession, GameEngineEventSink,
                                RulesTransaction)


def test_opening_choice_preserves_both_coin_winners_and_csharp_order():
    """Both coin winners can choose play/draw and reach the Mulligan phase."""
    base_players = (game_engine.UID.make(244, 5101),
                    game_engine.UID.make(3, 5102))

    for case_number, players in enumerate((base_players, base_players[::-1])):
        for choose_play in (True, False):
            session_id = 5100 + case_number * 10 + int(choose_play)
            game = game_engine.Game(session_id, *players)
            session = AuthoritativeSession(
                session_id, players, seed_z=1, seed_w=2,
                event_sink=GameEngineEventSink(game))
            session.current_turn_phase = game_engine.ETurnPhases.PickGoesFirst
            session.active_player_id = players[0]
            session.push_game_action(
                PriorityWindowAction(TurnPhasePlayers.ACTIVE))
            assert session.action_stack.update() is False

            if choose_play:
                transaction = RulesTransaction.choose_play_first(
                    players[0], game_engine.ETurnPhases.PickGoesFirst)
                event_name = "PlayerWishesToPlayFirstSessionEventArgs"
                expected_order = players
            else:
                transaction = RulesTransaction.choose_draw_first(
                    players[0], game_engine.ETurnPhases.PickGoesFirst)
                event_name = "PlayerWishesToDrawFirstSessionEventArgs"
                expected_order = (players[1], players[0])

            assert session.submit_transaction(transaction)
            assert session.handle_transaction()
            assert session.player_ids == expected_order
            assert session.active_player_id == expected_order[0]
            assert any(event.__class__.__name__ == event_name
                       for event in game.events), game.events
            assert session.advance_turn_phase() == \
                game_engine.ETurnPhases.Mulligan


def test_pve_coin_toss_uses_turn_order_without_forced_first_signal():
    """A random PvE toss starts Unity's animation and reports its winner."""
    import hconnect_server as host

    player_uid = game_engine.UID.make(244, 5211)
    ai_uid = game_engine.UID.make(3, 5212)
    player_guid = "11111111-2222-4333-8444-555555555555"
    ai_guid = "66666666-7777-4888-9999-aaaaaaaaaaaa"
    player_sleeve = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    ai_sleeve = "01234567-89ab-4cde-8fab-0123456789ab"

    for case_number, player_won in enumerate((True, False)):
        game = game_engine.Game(5211 + case_number, player_uid, ai_uid)
        host._push_pve_game_started(
            game, player_name="Human champion", ai_name="AI champion",
            player_champion_guid=player_guid,
            ai_champion_guid=ai_guid,
            player_sleeve_guid=player_sleeve,
            ai_sleeve_guid=ai_sleeve,
            coin_winner_is_player=player_won)

        started = game.events[0]
        expected_players = ((player_uid, ai_uid) if player_won
                            else (ai_uid, player_uid))
        expected_names = (("Human champion", "AI champion") if player_won
                          else ("AI champion", "Human champion"))
        expected_champions = ((player_guid, ai_guid) if player_won
                              else (ai_guid, player_guid))
        expected_sleeves = ((player_sleeve, ai_sleeve) if player_won
                            else (ai_sleeve, player_sleeve))
        assert tuple(started.turn_order) == expected_players
        assert tuple(started.champion_names) == expected_names
        assert tuple(str(value.guid) for value in
                     started.champion_template_ids) == expected_champions
        assert tuple(str(value.guid) for value in
                     started.sleeve_template_ids) == expected_sleeves
        assert len(game.events) == 1
        assert all(event.__class__.__name__ !=
                   "FirstPlayerDictatedSessionEventArgs"
                   for event in game.events)


def test_pvp_setup_coin_winner_is_persisted_and_shared_by_both_players():
    """Both 22027 responses use one C#-ordered PvP coin winner."""
    from application.objfmt_wire import parse_datawrapper
    from services import tournament_game

    player_ids = [1001, 1002]
    session = SimpleNamespace(session_id=5199, session_name="tourney-u4")
    state = {"pvp": True, "pids": player_ids, "champ_map": {}}
    ready = {}
    persisted = dict(state)

    def load_state(_session):
        return dict(persisted)

    def save_state(_session, value):
        persisted.clear()
        persisted.update(value)

    def collect_uids(value):
        found = []
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                if "m_UID64" in current:
                    found.append(int(current["m_UID64"]))
                else:
                    pending.extend(current.values())
            elif isinstance(current, (list, tuple)):
                pending.extend(current)
        return tuple(found)

    responses = []
    with mock.patch.object(tournament_game, "db_game_session_pids",
                          return_value=player_ids), \
            mock.patch.object(tournament_game, "pvp_load_state",
                              side_effect=load_state), \
            mock.patch.object(tournament_game, "pvp_save_state",
                              side_effect=save_state):
        for player_id in player_ids:
            handler = SimpleNamespace(client_reck_id=player_id)
            response, game_started = tournament_game.handle_ready_for_game_setup(
                handler, session, ready, {})
            assert response is not None
            assert game_started is False
            decoded = parse_datawrapper(response, preserve_complex=True)
            responses.append(decoded)

    import hashlib

    bit = hashlib.md5(b"5199:1001:1002").digest()[0] & 1
    expected_first = player_ids[0] if bit else player_ids[1]
    expected_order = tuple((pid << 8) | 244
                           for pid in (expected_first,
                                       next(pid for pid in player_ids
                                            if pid != expected_first)))
    orders = tuple(collect_uids(response["TurnOrder"])
                   for response in responses)
    assert orders == (expected_order, expected_order)
    assert persisted["goes_first_pid"] == expected_first
    assert set(ready[5199]["handlers"]) == set(player_ids)


def test_pvp_reconnect_uses_valid_champion_ids_before_phase_projection():
    """Reconnect recovers champions from rows before Unity resolves the IDs."""
    from services import tournament_game

    player_id, opponent_id = 5151, 5152
    player_card = game_engine.UID.make(1, 9151).uid64
    opponent_card = game_engine.UID.make(1, 9152).uid64
    player_template = "11111111-2222-4333-8444-555555555555"
    opponent_template = "66666666-7777-4888-9999-aaaaaaaaaaaa"

    def run_reconnect(champion_rows, session_id):
        session = SimpleNamespace(
            session_id=session_id, server_id=246, session_name="tourney-u4")
        persisted = {
            "pvp": True,
            "pids": [player_id, opponent_id],
            "goes_first_pid": opponent_id,
            "turn_pid": player_id,
            "priority_pid": player_id,
            "phase": int(game_engine.ETurnPhases.StartTurn),
        }
        sent_games = []
        handler = SimpleNamespace(
            client_reck_id=player_id,
            client_req_session_id="00000000-0000-0000-0000-000000000001",
            scnt=0,
            sid="u4-reconnect",
            send=lambda *_args: None,
            _card_full_data=lambda *_args: None,
        )

        def load_state(_session):
            return dict(persisted)

        def save_state(_session, state):
            persisted.clear()
            persisted.update(state)

        def cards_at(_session_id, zone, card_type=None, user_id=None, conn=None):
            if zone != "champion":
                return []
            return [
                (card_uid, template, owner, int(game_engine.ECardTypes.Champion),
                 0, [], {})
                for owner, card_uid, template in (
                    (player_id, player_card, player_template),
                    (opponent_id, opponent_card, opponent_template))
                if owner in champion_rows
            ]

        with mock.patch.object(tournament_game, "db_game_session_pids",
                               return_value=[player_id, opponent_id]), \
                mock.patch.object(tournament_game, "pvp_load_state",
                                  side_effect=load_state), \
                mock.patch.object(tournament_game, "pvp_save_state",
                                  side_effect=save_state), \
                mock.patch.object(tournament_game, "db_game_champion",
                                  side_effect=lambda _sid, owner:
                                  champion_rows.get(owner)), \
                mock.patch.object(tournament_game, "_pvp_populate_game_state"), \
                mock.patch.object(tournament_game, "_pvp_set_handler_champions"), \
                mock.patch.object(tournament_game, "db_tournament_player_name_for_session",
                                  side_effect=lambda _sid, owner:
                                  f"Player {owner}"), \
                mock.patch.object(tournament_game, "db_game_cards_at_location",
                                  side_effect=cards_at), \
                mock.patch.object(tournament_game, "pvp_push_warzone_updates"), \
                mock.patch.object(tournament_game, "_pvp_push_reconnect_underground"), \
                mock.patch.object(tournament_game, "_send_pvp_packet",
                                  side_effect=lambda _handler, _session, game,
                                  _player, _label: sent_games.append(game)), \
                mock.patch.object(tournament_game, "pvp_push_main_phase_options"), \
                mock.patch("rules_port.visibility.merge_underground_visibility_flags",
                           return_value={}), \
                mock.patch("rules_port.visibility.apply_player_visibility_to_game"):
            result = tournament_game._pvp_push_reconnect_snapshot(
                handler, session, player_id)
        return result, persisted, sent_games

    rows = {
        player_id: (player_card, player_template),
        opponent_id: (opponent_card, opponent_template),
    }
    result, persisted, sent_games = run_reconnect(rows, 5153)
    assert result is True
    assert persisted["champ_map"] == {
        str(player_id): player_card,
        str(opponent_id): opponent_card,
    }
    assert len(sent_games) == 1
    game = sent_games[0]
    player_updates = [
        (index, event) for index, event in enumerate(game.events)
        if isinstance(event, game_engine.PlayerUpdatedSessionEventArgs)
    ]
    assert [event.champion_id.uid.uid64 for _, event in player_updates] == [
        player_card, opponent_card]
    assert all(event.champion_id.uid.uid_type != 0
               for _, event in player_updates)
    card_updates = [
        (index, event) for index, event in enumerate(game.events)
        if isinstance(event, game_engine.CardUpdatedSessionEventArgs)
    ]
    assert {event.session_card_id.uid.uid64 for _, event in card_updates} == {
        player_card, opponent_card}
    phase_index = next(
        index for index, event in enumerate(game.events)
        if isinstance(event, game_engine.TurnPhaseUpdatedSessionEventArgs))
    assert max(index for index, _ in player_updates) < min(
        index for index, _ in card_updates) < phase_index
    started = next(
        event for event in game.events
        if isinstance(event, game_engine.GameStartedSessionEventArgs))
    assert tuple(started.turn_order) == (
        game_engine.UID.make(244, opponent_id),
        game_engine.UID.make(244, player_id))

    result, _persisted, sent_games = run_reconnect({}, 5154)
    assert result is False
    assert sent_games == []


def test_pvp_mulligan_redraw_and_handoff_follow_csharp_active_player():
    """Redraw keeps priority; accepting emits the next active Mulligan phase."""
    from rules_port.pvp_lifecycle import mulligan_transition
    from services import tournament_game

    player_ids = [1001, 1002]
    redraw = mulligan_transition({"kept": []}, player_ids, 1001)
    assert redraw == {"action": "prompt", "next_player": 1001}
    accepted = mulligan_transition({"kept": [1001]}, player_ids, 1001)
    assert accepted == {"action": "prompt", "next_player": 1002}

    class Session:
        session_id = 5200
        server_id = 100

    for ask_pid in player_ids:
        session = Session()
        state = {"pvp": True, "pids": player_ids, "champ_map": {}}
        packets = []
        with mock.patch.object(tournament_game, "db_game_session_pids",
                               return_value=player_ids), \
                mock.patch.object(tournament_game, "player_handlers",
                                  {pid: object() for pid in player_ids}), \
                mock.patch.object(tournament_game, "_pvp_populate_game_state",
                                  lambda *args: None), \
                mock.patch.object(tournament_game, "_send_pvp_packet",
                                  lambda _h, _s, game, uid, _label:
                                  packets.append((int(uid.uid64) >> 8, game))), \
                mock.patch.object(tournament_game, "pvp_save_state"), \
                mock.patch.object(tournament_game, "_pvp_push_waiting_on"):
            tournament_game._pvp_push_mulligan_prompt(
                session, state, ask_pid)

        assert {pid for pid, _game in packets} == set(player_ids)
        expected_uid = game_engine.UID.make(244, ask_pid).uid64
        for pid, game in packets:
            events = game.events
            greenlight = next(
                event for event in events
                if isinstance(event, game_engine.GreenLightSessionEventArgs))
            phase = next(
                event for event in events
                if isinstance(event, game_engine.TurnPhaseUpdatedSessionEventArgs))
            disabled = next(
                event for event in events
                if isinstance(event, game_engine.DisableInterfaceSessionEventArgs))
            assert greenlight.player_id.uid64 == expected_uid
            assert phase.turn_phase == game_engine.ETurnPhases.Mulligan
            assert phase.active_player_id.uid64 == expected_uid
            assert phase.priority_player_id.uid64 == expected_uid
            assert events.index(greenlight) < events.index(phase)
            assert disabled.disabled is (pid != ask_pid)


def test_practice_mulligan_start_follows_csharp_active_player():
    """Practice resolves an AI-first mulligan before prompting the human."""
    from hconnect_server import _push_pve_mulligan_start

    player_uid = game_engine.UID.make(244, 5201)
    ai_uid = game_engine.UID.make(3, 1000)
    game = game_engine.Game(5201, player_uid, ai_uid)
    ai_calls = []

    def resolve_ai():
        ai_calls.append("resolved")
        game.push_accepted_starting_hand(ai_uid, mulliganed=False)

    assert _push_pve_mulligan_start(
        game, player_uid, ai_uid, False, resolve_ai) is True
    phases = [event for event in game.events
              if isinstance(event,
                            game_engine.TurnPhaseUpdatedSessionEventArgs)]
    lights = [event for event in game.events
              if isinstance(event, game_engine.GreenLightSessionEventArgs)]
    assert ai_calls == ["resolved"]
    assert [event.active_player_id.uid64 for event in phases] == [
        ai_uid.uid64, player_uid.uid64]
    assert [event.priority_player_id.uid64 for event in phases] == [
        ai_uid.uid64, player_uid.uid64]
    assert [event.player_id.uid64 for event in lights] == [
        ai_uid.uid64, player_uid.uid64]
    assert game.events.index(lights[0]) < game.events.index(phases[0])
    assert game.events.index(phases[0]) < game.events.index(
        next(event for event in game.events
             if isinstance(event,
                           game_engine.PlayerAcceptedStartingHandSessionEventArgs)))
    assert game.events.index(phases[1]) > game.events.index(phases[0])

    player_first_game = game_engine.Game(5202, player_uid, ai_uid)
    ai_calls.clear()
    assert _push_pve_mulligan_start(
        player_first_game, player_uid, ai_uid, True, resolve_ai) is False
    assert ai_calls == []
    first_phase = next(
        event for event in player_first_game.events
        if isinstance(event, game_engine.TurnPhaseUpdatedSessionEventArgs))
    assert first_phase.active_player_id.uid64 == player_uid.uid64
    assert first_phase.priority_player_id.uid64 == player_uid.uid64


def test_ai_mulligan_emits_acceptance_only_when_the_hand_is_kept():
    """The acceptance event reports prior redraws, as in C# Session.Mulligan."""
    import ai

    player_uid = game_engine.UID.make(244, 5301)
    ai_uid = game_engine.UID.make(3, 1000)
    game = game_engine.Game(5301, player_uid, ai_uid)
    hand_rows = [(index, game_engine.UID.make(1, index).uid64)
                 for index in range(1, 8)]
    replacement_rows = [(100 + index,
                         game_engine.UID.make(1, 100 + index).uid64)
                        for index in range(1, 7)]

    def zone_rows(_session_id, zone, limit=None, conn=None):
        if zone == "hand":
            return hand_rows
        if limit is None:
            return [(index,) for index in range(1, 8)]
        return replacement_rows[:limit]

    with mock.patch.object(ai, "db_ai_hand_summary",
                           side_effect=[(7, 0), (4, 0)]), \
            mock.patch.object(ai, "_checkpoint_engine",
                              side_effect=RuntimeError("fixture")), \
            mock.patch.object(ai, "db_ai_zone_rows", side_effect=zone_rows), \
            mock.patch.object(ai, "db_move_card_rows"), \
            mock.patch.object(ai, "db_set_card_row_positions"):
        assert ai.resolve_ai_mulligan(
            SimpleNamespace(user_profile={"id": 5301}),
            SimpleNamespace(session_id=5301), game, ai_uid)

    redraws = [event for event in game.events
               if isinstance(event,
                             game_engine.PlayerMulliganedHandSessionEventArgs)]
    accepted = [event for event in game.events
                if isinstance(event,
                              game_engine.PlayerAcceptedStartingHandSessionEventArgs)]
    assert len(redraws) == 1
    assert len(accepted) == 1
    assert accepted[0].player_id.uid64 == ai_uid.uid64
    assert accepted[0].mulliganed is True


if __name__ == "__main__":
    test_opening_choice_preserves_both_coin_winners_and_csharp_order()
    test_pve_coin_toss_uses_turn_order_without_forced_first_signal()
    test_pvp_mulligan_redraw_and_handoff_follow_csharp_active_player()
    test_practice_mulligan_start_follows_csharp_active_player()
    test_ai_mulligan_emits_acceptance_only_when_the_hand_is_kept()
    print("PASS focused RulesPort setup parity checks")
