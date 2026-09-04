# Ability framework layout

The framework has two different axes and should keep them separate:

1. **Effects** answer “what operation happens?” — move, search, create a
   token, deal damage, add/remove counters, play, transform, and so on.
2. **Keywords/lifecycle** answer “when is an ability offered or triggered?” —
   Deploy, Inspire, Deathcry, Shift, Escalation, and combat keywords such as
   Rage.

## Current ownership

| Concern | Module | Boundary |
| --- | --- | --- |
| Effect-type registry | `effects/registry.py` | Metadata `effect_type` to executor |
| Deck search and selection | `effects/search.py` | Search candidates, prompts, zone move to hand |
| Damage | `effects/damage.py` | Troop/champion damage, prevention, death hand-off |
| Counters | `effects/counters.py` | Persist counters and project them to the client |
| Token creation | `effects/tokens.py` | Create card instances and run created/enters-play triggers |
| Deathcry | `deathcry.py` | Death event filtering and delegation only |
| Continuous stats | `statics.py` | Effective stats, cost, attributes, Rage values |
| Card transformation | `transform.py` | Replace a card's template while preserving its UID |
| Death lifecycle | `kill_troop.py` | Move to discard, replacement effects, Deathcry dispatch |
| Keyword lifecycle | `keywords/lifecycle.py` | Deploy/Inspire entry point over the trigger engine |
| Combat keywords | `keywords/combat.py` | Rage adapter over metadata-derived stats |
| Ability orchestration | `resolution.py` | One Records-backed AbilityInstance interpreter for plays, activations, and triggers |

`bom.py` remains large because it still contains leaf implementations that have
not yet migrated into `effects/`. New leaf code should be added to a focused
effect module and registered through `effects/registry.py`; the resolver itself
does not maintain a second legacy flat-walk implementation.

## Keyword guidance

Keywords should be split out when they have their own trigger, state, or timing
rules—not merely because they appear in card text:

- Deploy, Inspire, and Deathcry belong to lifecycle/event handling.
- Shift and Escalation belong to action/state handling.
- Rage and Sockets belong to continuous/combat state calculation.
- Transform is an effect operation; a keyword-specific trigger may call it,
  but the card mutation remains in `transform.py`.

Keyword modules should consume structured gamedata and effect metadata. They
must not become another place for card-name or localized `game_text` parsing.
