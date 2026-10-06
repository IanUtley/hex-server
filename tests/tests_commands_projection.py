"""Focused client-event checks for the private chat movement extensions."""

import os
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()

import commands
import game_engine


TEMPLATE_GUID = "11111111-2222-3333-4444-555555555555"


def _runtime():
    handler = SimpleNamespace(
        user_profile={"id": 5}, client_reck_id=5,
        _player_champ_scid=None,
        _card_full_data=lambda *_args: (
            TEMPLATE_GUID, game_engine.ECardTypes.Troop, "Test Troop",
            1, 2, 3, 0),
        _resolve_fra_deck_id=lambda deck_id: deck_id)
    session = SimpleNamespace(session_id=77, session_name="Session-test")
    player = game_engine.UID.make(244, 5)
    ai = game_engine.UID.make(3, 1000)
    database = SimpleNamespace(commit=lambda: None)
    return handler, session, player, ai, database


def test_chat_card_moves_use_card_updated_without_duplicate_class_50():
    handler, session, player, ai, database = _runtime()
    packets = []
    capture = lambda _handler, game, _session, _player: packets.append(game)

    # !top is a hand-to-Deck move. CardUpdated updates the collection and
    # nulls the identity; the client cache derives the UI CardMoved.
    with mock.patch.object(commands.hconnect_server, "_db", database), \
            mock.patch("pvp_db.db_hand_card_by_uid",
                       return_value=(0x101, TEMPLATE_GUID, 5, "Test Troop")), \
            mock.patch("pvp_db.db_move_hand_card_to_deck_top"), \
            mock.patch.object(commands, "_send_game_events", side_effect=capture):
        result = commands._dispatch(
            handler, "top", [str(0x101)], session, player, ai, "", "tester")
    assert result.startswith("Put Test Troop on top"), result
    top_events = packets.pop().events
    assert [type(event) for event in top_events] == [
        game_engine.CardUpdatedSessionEventArgs]
    assert top_events[0].collection == game_engine.ECardCollections.Deck
    assert top_events[0].nulling is True

    # !discard publishes the semantic discard plus its destination update.
    with mock.patch.object(commands.hconnect_server, "_db", database), \
            mock.patch("pvp_db.db_hand_cards_for_discard",
                       return_value=[(0, 0x102, TEMPLATE_GUID)]), \
            mock.patch("pvp_db.db_card_original_owner_id", return_value=5), \
            mock.patch("pvp_db.db_discard_card"), \
            mock.patch("random.choice", return_value=(0x102, TEMPLATE_GUID)), \
            mock.patch.object(commands, "_send_game_events", side_effect=capture):
        result = commands._dispatch(
            handler, "discard", [], session, player, ai, "", "tester")
    assert result.startswith("Discarded a card"), result
    discard_events = packets.pop().events
    assert [type(event) for event in discard_events] == [
        game_engine.CardDiscardedSessionEventArgs,
        game_engine.CardUpdatedSessionEventArgs,
        game_engine.PlayerUpdatedSessionEventArgs,
        game_engine.GreenLightSessionEventArgs,
    ], discard_events

    # !move relies on the same destination update path and redacts a Deck card.
    with mock.patch.object(commands.hconnect_server, "_db", database), \
            mock.patch("pvp_db.db_move_debug_card") as move_card, \
            mock.patch("pvp_db.db_card_command_info",
                       return_value=(0x103, "hand", TEMPLATE_GUID)), \
            mock.patch("pvp_db.db_template_projection",
                       return_value=("Troop", 1, 2, 3, None, None)), \
            mock.patch("pvp_db.db_deck_active_gems", return_value=None), \
            mock.patch.object(commands.hconnect_server, "db_get_arena_state",
                              return_value={"deck_id": 12}), \
            mock.patch.object(commands, "_send_game_events", side_effect=capture):
        result = commands._dispatch(
            handler, "move", [str(0x103), "deck"], session,
            player, ai, "", "tester")
    assert result == "Moved 259 to deck (zone=1)", result
    move_card.assert_called_once()
    move_events = packets.pop().events
    assert [type(event) for event in move_events] == [
        game_engine.CardUpdatedSessionEventArgs]
    assert move_events[0].collection == game_engine.ECardCollections.Deck
    assert move_events[0].nulling is True


if __name__ == "__main__":
    test_chat_card_moves_use_card_updated_without_duplicate_class_50()
    print("chat command projection tests: ok")
