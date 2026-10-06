"""Build a deterministic Python class-1 game-start session event."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import game_engine


def build_game_started_event():
    player = game_engine.UID.make(244, 1234)
    opponent = game_engine.UID.make(3, 1000)
    game = game_engine.Game(42, player, opponent)
    game.push_game_started(
        champion_names=["Necrotic", "Iddi"],
        champion_template_ids=[
            "1d462ffb-0744-4996-804c-ba61b2c5c2f1",
            "f8f86969-2e47-4901-8c9e-7fbf8d859e22",
        ],
        player_first=True,
        sleeve_template_ids=[
            "11111111-2222-3333-4444-555555555555",
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ],
    )
    return game.events[-1]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_game_started_session_fixture.py OUTPUT")
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(build_game_started_event().to_byte_array())


if __name__ == "__main__":
    main()
