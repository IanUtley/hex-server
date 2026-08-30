# Frost Ring Arena

Frost Ring Arena (FRA) is the server-backed twenty-fight PvE arena run. A
player selects a deck, receives a saved opponent roster, plays the encounters
as ordinary campaign-style game sessions, and returns to the arena lobby after
each result.

## Current status

The implemented flow supports:

- selecting and persisting an FRA deck;
- generating and saving a twenty-opponent roster;
- fixed boss positions and elite/boss encounter selection;
- recording wins, losses, and completed-fight history;
- showing the current opponent and completed fights in the lobby;
- accumulating one gold pouch for each non-boss win;
- accumulating one treasure chest for each boss win.

The arena run is stored per player in `arena_state`. A new deck assignment
resets the run and all reward counters.

## Run and roster rules

Roster selection is kept separate from the protocol and database layers in
[`gamemodes/arena.py`](../gamemodes/arena.py). The current rules are:

- run length: 20 encounters;
- fixed boss ranks: 10, 15, and 20;
- known boss families are Phenteo, Eurig, Princess Cory, and Hogarth;
- elite ranks 9, 12, 14, 17, and 19 select an eligible elite version of a
  normal deck family;
- all other non-boss ranks select a normal encounter;
- elite upgrades are stored as non-boss fights, so they award gold rather than
  a boss treasure chest.

Encounter data is persisted in `fra_encounters`, and the selected run is
persisted in `fra_challengers`. The challenger response exposes the boss state
as the client-facing `IsBoss` field.

## Arena state

The `arena_state` table in [`static.py`](../static.py) contains:

| Field | Meaning |
| --- | --- |
| `deck_id` | Player deck selected for the current run |
| `wins` / `losses` | Run result totals |
| `challenger_index` | Zero-based next opponent index |
| `fight_history` | JSON history for the twenty lobby fight slots |
| `gold_earned` | Gold-pouch total for the current run |
| `chests_earned` | Treasure-chest total for the current run |
| `sacks_earned` | Reserved for other reward types; currently unused |

`db_record_arena_fight()` in [`db.py`](../db.py) records the result and
advances the challenger index. On a win it checks the saved challenger's boss
flag:

- non-boss: `gold_earned += 1`;
- boss: `chests_earned += 1`;
- loss: neither counter changes.

The update occurs only when the fight-history slot is unfinished, so repeated
game-end handling cannot award the same fight twice.

## Game-session result flow

When a battle ends, `hconnect_server.py` distinguishes campaign, tournament,
and FRA sessions. FRA sessions are recorded through
`db_record_arena_fight()` before their game-session rows and cards are cleaned
up.

The normal sequence is:

1. The player joins or assigns an arena deck.
2. The server loads the saved challenger and encounter deck.
3. The battle runs through the normal game-session and battle engine.
4. Game end records the result and reward counter.
5. The completed session is removed.
6. The next arena lobby request renders the updated totals and fight history.

## Client protocol

FRA campaign service requests are dispatched through `services/arena.py`:

| Data type | Operation |
| ---: | --- |
| `10001` | Join arena |
| `10003` | Assign/reset arena deck |
| `10005` | Pick next opponent |
| `10007` | Get challenger roster |
| `10009` | Get fight history |
| `10011` | Cash out |
| `10013` | Refresh arena information |

`ArenaData` contains the lobby counters expected by the client. The server
maps:

- `GoldPacks` from `arena_state.gold_earned`;
- `EquipmentPacks` from `arena_state.chests_earned`;
- `CardPacks` remains zero because card-pack rewards are not currently used.

The client combines the card-pack and equipment-pack values for its chest
indicator, so the stored treasure-chest total is visible in the bottom FRA
lobby display.

## Cash-out and remaining work

The client supports richer end-of-run `ArenaReward` entries, including gold,
cards, equipment, and sleeves. The current server cash-out path in
`services/arena.py` returns the accumulated gold value but still sends an
empty `AllLoot` list. It also resets the run counters and clears the saved
challenger roster.

Consequently, the current implementation tracks and displays FRA gold-pouch
and treasure-chest totals during a run, but does not yet convert those totals
into inventory items or populated cash-out loot entries. That should be added
separately from the run-counter logic.

## Validation notes

After FRA changes, run focused checks before testing with the client:

```bash
python3 -m py_compile db.py services/arena.py gamemodes/arena.py
git diff --check
```

For live behavior, inspect `/tmp/hconnect_log.txt` for the FRA result and
confirm the next `RefreshArenaInfo` or lobby response contains the expected
`GoldPacks` and `EquipmentPacks` values. Changes to `db.py` require a server
restart before the running process uses the new result-accounting code.
