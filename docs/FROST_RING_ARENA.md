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
- awarding gold bags equal to the opponent's tier for every win;
- accumulating one treasure chest for each boss win.

The arena run is stored per player in `arena_state`. A new deck assignment
resets the run and all reward counters.

## Run and roster rules

Roster selection is kept separate from the protocol and database layers in
[`gamemodes/arena.py`](../gamemodes/arena.py). The current rules are:

- run length: 20 encounters;
- fixed boss ranks: 5, 10, 15, and 20;
- the first tier ends with Eternal Guardian; later boss families are Phenteo,
  Eurig, Princess Cory, and Hogarth;
- elite ranks 9, 12, 14, 17, and 19 select an eligible elite version of a
  normal deck family;
- all other non-boss ranks select a normal encounter;
- elite upgrades are stored as non-boss fights, so they do not receive a boss
  treasure chest.

Players who win all five fights of Tier 1 receive the persistent
`ARENA_TIER1_PERFECT` Reckoning flag. The client checks that account flag to
show its Tier 1 skip button on later runs. `BuyoutArena` verifies the same
flag server-side, marks the first five fights `SKIP`, and starts the run at
the first fight of Tier 2 without granting Tier 1 rewards.

Encounter data is persisted in `fra_encounters`, and the selected run is
persisted in `fra_challengers`. The challenger response exposes the boss state
as the client-facing `IsBoss` field.

Deck strategies are inferred from typed Records by
[`AssetExtraction/evaluate_fra_deck_personalities.py`](../AssetExtraction/evaluate_fra_deck_personalities.py).
The result is stored on `fra_encounters`, copied to `fra_challengers`, and
applied to matching `encounter_scenes.ai_deck_personality` rows. Decks without
a clear strategy use `Default`: the top score must reach 3/10 and lead the
runner-up by at least 1/10. An average troop cost from 3 through 5 is neutral
for `BigThreats`; ramp cards add weight there. `Reanimation` gets weight from
its graveyard setup and recovery package, with large troops as supporting
signals. Direct damage actions that can target a champion contribute to
`Burn`. `HandAdvantage` counts draw effects only; QuickActions without draw do
not contribute. Authored threshold colors add up to 2/10 as an archetype bias:
Sapphire for `HandAdvantage`, Wild for `BigThreats`, Blood or Diamond for
`Reanimation`, Ruby for `Burn`, Wild/Diamond/Ruby for `BuildArmy`, and
Ruby/Wild/Diamond for `Aggressive`. Run the evaluator after changing strategy
rules or the Records snapshot; it prints each category's score out of 10.

## Arena state

The `arena_state` table in [`static.py`](../static.py) contains:

| Field | Meaning |
| --- | --- |
| `deck_id` | Player deck selected for the current run |
| `wins` / `losses` | Run result totals |
| `challenger_index` | Zero-based next opponent index |
| `fight_history` | JSON history for the twenty lobby fight slots |
| `gold_earned` | Gold-bag count for the current run |
| `chests_earned` | Treasure-chest total for the current run |
| `sacks_earned` | Reserved for other reward types; currently unused |

`db_record_arena_fight()` in [`pve_db.py`](../pve_db.py) records the result and
updates the challenger index:

- every opponent: `gold_earned += fight_tier` (five opponents per tier);
- boss: `chests_earned += 1` in addition to the tier's gold bags;
- ordinary loss: the player loses one life and advances to the next opponent;
- boss loss: the player loses one life but stays on the same boss to retry it;
- a boss win advances to the next tier, while a successful boss retry still
  retains the recorded boss-loss marker and is not treated as a lossless tier.

The update is idempotent per game-session ID. Ordinary fight slots are only
recorded once; a boss slot may record each distinct loss attempt and then its
eventual win without awarding the boss twice. `GoldPacks` projects the
accumulated gold-bag count in the arena lobby. At cash-out, each bag becomes
one `ArenaReward` worth 100 gold; `GoldWin` is the resulting account-gold
amount, not the bag count. The full selected roster is sent to the client
before the cash-out response so the summary can reveal completed opponents.

## FRA challenges

Challenge definitions and encounter modifications come from the extracted
`fra_challenges` records. A selected elite encounter receives one random
ordinary challenge. After a win against an elite, the server sends its authored
`... Reward` conversation at game end and stores the paired `... Boss
Notification` challenge for the next boss fight. The notification's authored
encounter modification is included in that boss's `GetArenaBattleMods` reply.
If the player loses that boss fight, the attached boss-notification challenge
and its modification are consumed; a retry remains on the boss but does not
receive that earned boss reward again.

For non-elite encounters after the first six opponents (zero-based challenger
index greater than 5), the server selects an ordinary challenge and applies it
only when its `probability_percent` roll succeeds; boss encounters are excluded
from both ordinary-challenge paths. Reward and notification conversations are
excluded from this selection. Selected challenge IDs and runtime card choices
are persisted with fight history so lobby refreshes do not reroll them.

`Starting Health 15` is a separate run-start challenge. The server rolls it
when assigning the arena deck, stores it in fight-history slot 0, and applies
its health modifier only for challenger index 0.

For every new FRA game session, preserve `IsPvEArena`, `ArenaInstance`, and
`ArenaOwner` in the `ReadyForGameSetup` session state for client challenge UI
and reconnect handling. The fixed local client does not request
`GetArenaBattleMods` during normal setup. The authoritative HConnect setup path
calls the same saved-challenge lookup directly, applies its round-zero mods,
and pushes each authored conversation before PreGame. The conversation's
authored answer event displays its objective panel; the reconnect-only
`GetArenaMCChallenge` response supplies the objective text directly. The fixed
client has no reachable decline action for an MC challenge, so the server
never models a declined challenge: an attached challenge is always accepted
and its mods and rewards apply.

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
cards, equipment, and sleeves. The server cash-out path converts each stored
gold bag into a 100-gold `ArenaReward`, credits the account atomically, and
returns the full selected roster before the client requests final cleanup.

The cash-out path credits the converted gold atomically, returns the gold loot
entries, and leaves final roster cleanup to the client's subsequent
`DestroyArenaData` request.

## Validation notes

After FRA changes, run focused checks before testing with the client:

```bash
python3 -m py_compile pve_db.py services/arena.py gamemodes/arena.py
git diff --check
```

For live behavior, inspect `/tmp/hconnect_log.txt` for the FRA result and
confirm the next `RefreshArenaInfo` or lobby response contains the expected
`GoldPacks` and `EquipmentPacks` values. Changes to the PVE persistence API
require a server restart before the running process uses the new
result-accounting code.
