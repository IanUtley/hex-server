# abilities/cards/

One file per custom ability GUID. Each file is imported by
`discover_abilities()` at server startup; no central registration list is
needed.

## Adding a new card ability

Create a `.py` file here with a function decorated by `@register_custom_ability`:

```python
from abilities.registry import register_custom_ability

@register_custom_ability("your-ability-guid-here")
def your_ability(game, session, db, handler, pl_t, ai_t, bstate, ability_guid, source_scid):
    """Short description of what this ability does."""
    ...
    return "log message for the server log"
```

## Function signature

Every registered function receives:

| Parameter | Type | Description |
|-----------|------|-------------|
| `game` | `domain.game.Game` | Event queue — call `game.push_*()` methods to emit events |
| `session` | `game_session.GameSession` | The current game session |
| `db` | `sqlite3.Connection` | Database handle for queries |
| `handler` | `HCPHandler` | The client connection — has helpers like `_card_full_data()`, `_player_draw_card()` |
| `pl_t` | `domain.types.UID` | Player's target UID |
| `ai_t` | `domain.types.UID` | AI's target UID |
| `bstate` | `dict` | Battle state dict (health, resources, thresholds, etc.) |
| `ability_guid` | `str` | The ability GUID being resolved |
| `source_scid` | `domain.types.SessionCardId` | The source card (champion or troop) |

Return a string for the server log, or `None`.

## Finding ability GUIDs

Ability GUIDs come from `AbilityTemplate.m_AbilityTemplateId` in the game data.
They are linked to champion talents via the `talent_data` and `talent_abilities`
DB tables.  The `card_abilities_meta` table maps each GUID to its casting
behavior, cost, and BOM leaves.

## Data-driven vs custom

**Do not write custom Python** for abilities the BOM already handles. The
registered leaf executors in `abilities/framework/` cover common effects such
as:
- `DrawNCards` — draw a card
- `DiscardCard` — discard a chosen card
- `CardModifier` — +/- ATK/DEF
- `TACAbilityEffectTemplate` — Shift, stat buffs, triggers
- `SummonTokenTroop` — create token troops
- `ActivateAbilityEffectTemplate` — recurse into another ability
- `RandomizeVariable` — variable-value effects

Only write a custom handler when the ability **cannot** be expressed as a
combination of these leaves (e.g. random 3-5 spell points like Replenish).
