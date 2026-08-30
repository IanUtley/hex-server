# gamemodes/

Game-mode interface for tournament formats and other match variants.

## How it works

A game mode is a class implementing `GameMode`. The decorator registers the
class in the in-process mode registry. Files are not currently discovered
from the filesystem automatically, so the runtime must import/register a mode
before `get_mode()` can return it.

## Adding a new game mode

1. Create a new file, e.g. `gamemodes/swiss_tournament.py`:

```python
from gamemodes import GameMode, Match, register_mode

@register_mode
class SwissTournament(GameMode):
    mode_id = "swiss"
    min_players = 4
    description = "Swiss-pairing tournament"

    def create_match(self, players: list[int], config: dict) -> Match:
        """Set up a new match between `players`."""
        return Match(match_id=..., players=players)

    def on_match_end(self, match: Match, winner: int) -> list[dict]:
        """Handle match conclusion. Return reward events."""
        return [{"type": "gold", "amount": 100, "player": winner}]

    def get_pairings(self, tournament_state: dict) -> list[tuple[int, int]]:
        """Return next-round pairings."""
        return [(a, b) for a, b in ...]

    def validate_deck(self, deck: dict) -> bool:
        """Validate a deck for this mode."""
        return len(deck.get("cards", [])) >= 60

    def format_rewards(self, placement: int) -> list[dict]:
        """Return rewards for a given placement."""
        return [{"type": "booster_pack", "count": max(5 - placement, 1)}]
```

2. Import the module during the relevant server startup path so the decorator
   runs. The registry then exposes it through `get_mode(mode_id)`.

## Required methods

| Method | Purpose |
|--------|---------|
| `create_match(players, config)` | Allocate a match instance |
| `on_match_end(match, winner)` | Process match result, return reward events |
| `get_pairings(state)` | Pair players for the next round |
| `validate_deck(deck)` | Return `True` if the deck is legal |
| `format_rewards(placement)` | Return rewards for final placement |

## Match dataclass

```python
@dataclass
class Match:
    match_id: int
    players: list[int]
    config: dict
```
