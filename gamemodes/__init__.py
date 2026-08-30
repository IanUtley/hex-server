"""Game mode plugin interface for Hex TCG.

To add a new game mode (Swiss tournament, draft, sealed, constructed, etc.),
implement ``GameMode`` and register it.

Example::

    from gamemodes import GameMode, register_mode

    @register_mode
    class SwissTournament(GameMode):
        mode_id = "swiss"
        min_players = 4

        def create_match(self, players, config): ...
        def on_match_end(self, match, winner): ...
        def get_pairings(self, state): ...
        def validate_deck(self, deck): ...
        def format_rewards(self, placement): ...
"""

from dataclasses import dataclass, field
from typing import Any

_MODES: dict[str, type] = {}


@dataclass
class Match:
    match_id: int
    players: list[int]
    config: dict = field(default_factory=dict)


class GameMode:
    """Base class for game format plugins."""
    mode_id: str = ""
    min_players: int = 2
    description: str = ""

    def create_match(self, players: list[int], config: dict) -> Match:
        """Set up a new match between players. Return a Match object."""
        raise NotImplementedError

    def on_match_end(self, match: Match, winner: int) -> list[dict]:
        """Handle match conclusion. Return reward events."""
        raise NotImplementedError

    def get_pairings(self, tournament_state: dict) -> list[tuple[int, int]]:
        """Return next-round pairings as (player_id, player_id) tuples."""
        raise NotImplementedError

    def validate_deck(self, deck: dict) -> bool:
        """Validate a deck for this mode. Return True if valid."""
        return True

    def format_rewards(self, placement: int) -> list[dict]:
        """Return rewards for a given placement."""
        return []


def register_mode(arg=None):
    """Decorator. Register a game mode."""
    if isinstance(arg, type):
        _MODES[arg.mode_id] = arg
        return arg
    elif isinstance(arg, str):
        def deco(cls):
            _MODES[arg] = cls
            return cls
        return deco


def get_mode(mode_id: str):
    return _MODES.get(mode_id)


def list_modes() -> list[str]:
    return list(_MODES.keys())
