"""Focused wire tests for event types mirrored from the shipped client."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import game_engine
from domain.events import (
    ChessTimerUpdatedSessionEventArgs,
    CardDestroyedSessionEventArgs,
    CardVoidedSessionEventArgs,
    CardGraveyardedSessionEventArgs,
    SpellCardCastSessionEventArgs,
    ArtifactCardPlayedSessionEventArgs,
    AbilityCancelledSessionEventArgs,
    CardTappedSessionEventArgs,
    CardUntappedSessionEventArgs,
    CardPrimedSessionEventArgs,
    EncounterCardsCreatedInZoneSessionEventArgs,
    CardTransformedSessionEventArgs,
    CardRevertedSessionEventArgs,
    EquipmentSetSessionEventArgs,
    CardCollectionsMergedSessionEventArgs,
)


def test_missing_events_have_client_class_ids_and_serialize():
    classes = [
        ChessTimerUpdatedSessionEventArgs,
        CardDestroyedSessionEventArgs,
        CardVoidedSessionEventArgs,
        CardGraveyardedSessionEventArgs,
        SpellCardCastSessionEventArgs,
        ArtifactCardPlayedSessionEventArgs,
        AbilityCancelledSessionEventArgs,
        CardTappedSessionEventArgs,
        CardUntappedSessionEventArgs,
        CardPrimedSessionEventArgs,
        EncounterCardsCreatedInZoneSessionEventArgs,
        CardTransformedSessionEventArgs,
        CardRevertedSessionEventArgs,
        EquipmentSetSessionEventArgs,
        CardCollectionsMergedSessionEventArgs,
    ]
    expected_ids = [4, 8, 11, 12, 18, 19, 21, 24, 25, 26,
                    43, 44, 45, 46, 49]
    for cls, expected_id in zip(classes, expected_ids):
        ev = cls()
        ev.session_id = game_engine.UID(123)
        raw = ev.to_byte_array()
        assert int.from_bytes(raw[:4], "little", signed=True) == expected_id
        assert len(raw) >= 12, cls.__name__


def test_game_spell_events_are_cast_then_played():
    game = game_engine.Game(
        1, game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000))
    card = game_engine.SessionCardId(game_engine.UID(101))
    game.push_spell_card_cast(card, game.player_uid, free=True)
    game.push_spell_card_played(card, game.player_uid)
    assert [type(ev).CLASS_ID for ev in game.events] == [18, 17]
    assert game.events[0].played_for_free is True


if __name__ == "__main__":
    test_missing_events_have_client_class_ids_and_serialize()
    test_game_spell_events_are_cast_then_played()
    print("PASS missing event protocol tests")
