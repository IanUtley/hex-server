# abilities/

Gamedata-driven card, champion, and talent ability resolution: BOM
(bill-of-materials) walking, leaf executors, custom handlers, and shared
framework utilities.

## How abilities work

Most abilities are **data-driven** — the `ability_effects` table defines an
ordered list of leaf effect templates (the BOM).  The server walks the BOM
and executes each leaf.  No custom Python code is needed for these.

**Custom Python** is only needed for abilities that cannot be expressed via
BOM leaves (for example, Replenish Spell Power's random 3–5 result). These
live in `cards/` and are discovered when `discover_abilities()` runs during
server startup.

## Adding a new card ability

1. Create a file in `abilities/cards/` named after the ability, e.g.
   `fireball.py`.

2. Use the `@register_custom_ability` decorator with the ability's GUID:

```python
# abilities/cards/fireball.py
from abilities.registry import register_custom_ability

@register_custom_ability("some-ability-guid-here")
def fireball(game, session, db, handler, pl_t, ai_t, bstate, ability_guid, source_scid):
    # Push events onto `game`, apply effects, return a log string.
    ...
    return "Fireball: dealt 3 damage"
```

3. The file is **auto-discovered** at startup.  Nothing else to change.
   Two contributors adding different cards never touch the same file.

## Submodules

| Path | Purpose |
|------|---------|
| `registry.py` | `@register_custom_ability` decorator and custom-handler discovery |
| `framework/bom.py` | BOM walking and the core leaf executors |
| `framework/tac.py` | TAC v2 binary decoder (template attribute collection) |
| `framework/conditions.py` | Pre-game condition functions + `apply_pregame_abilities` |
| `framework/kill_troop.py` | Kill a troop (Dead state, graveyard, Deathcry) |
| `framework/deathcry.py` | Deathcry trigger resolution |
| `framework/transform.py` | Transform a card into a new template |
| `framework/stat_mod.py` | Apply permanent ATK/DEF modifiers |
| `framework/_shared.py` | Logger, card-state helper, stat-delta parser |
| `cards/` | One file per custom ability GUID — **add your cards here** |

## Public API

Import from `abilities`:
- `resolve_effect(guid)` → effect function or `None`
- `resolve_played_spell(...)` → resolves a played BasicAction/QuickAction
- `discover_abilities()` → imports custom handlers once at server startup
- `kill_troop(...)`, `state_based_deaths(...)`, `transform_card(...)`, etc.
