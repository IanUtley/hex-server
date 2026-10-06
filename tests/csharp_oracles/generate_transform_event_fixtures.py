"""Build deterministic Python class-44 and class-45 session events."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import game_engine


def write_transform_event_fixtures(transformed_path, reverted_path):
    player = game_engine.UID.make(244, 1234)
    opponent = game_engine.UID.make(3, 1000)
    card_id = game_engine.SessionCardId(game_engine.UID.make(1, 456))
    game = game_engine.Game(42, player, opponent)

    game.push_card_transformed(
        card_id,
        "4ef824d5-5d8f-4b4f-9e77-63f82c6b17cd",
        is_replica=True,
        gems=0x123456789ABCDEF0,
    )
    transformed_path.write_bytes(game.events[-1].to_byte_array())

    game.push_card_reverted(
        card_id,
        "5badd4e4-5af4-409d-9d1f-1b839b39c35e",
    )
    reverted_path.write_bytes(game.events[-1].to_byte_array())
