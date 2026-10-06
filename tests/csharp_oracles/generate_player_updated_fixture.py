"""Build a deterministic Python class-65 PlayerUpdated session event."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import game_engine


SLEEVE_GUID = "11111111-2222-3333-4444-555555555555"


def build_player_updated_event():
    player = game_engine.UID.make(244, 1234)
    opponent = game_engine.UID.make(3, 1000)
    champion = game_engine.SessionCardId(game_engine.UID.make(1, 456))
    game = game_engine.Game(42, player, opponent)
    game.player_health = 17
    game.player_charges = 2
    game.player_resources = 4
    game.turn_number = 9
    game.player_total_resources = 11
    game.player_spell_points = 7
    game.max_hand_size = 6
    game.player_threshold = {
        game_engine.ECardShards.Colorless: 1,
        game_engine.ECardShards.Blood: 2,
        game_engine.ECardShards.Ruby: 3,
        game_engine.ECardShards.Sapphire: 4,
        game_engine.ECardShards.Wild: 5,
        game_engine.ECardShards.Diamond: 6,
    }
    game.player_champion_card_id = champion
    game._visibility_by_uid[player.uid64] = {
        "CanSeeOpponentsHand": 1,
        "CanSeeUndergroundTroops": 1,
    }
    game.push_player_updated(player, SLEEVE_GUID, champ_id=champion)
    event = game.events[-1]
    event.remaining_time_h = 1
    event.remaining_time_m = 2
    event.remaining_time_s = 3
    return event


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_player_updated_fixture.py OUTPUT")
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(build_player_updated_event().to_byte_array())


if __name__ == "__main__":
    main()
