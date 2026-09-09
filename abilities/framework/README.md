# abilities/framework/

Shared utilities that card abilities and other server code depend on.
New card abilities (`abilities/cards/`) import from here as needed.

## Files

| File | Purpose |
|------|---------|
| `bom.py` | BOM (bill-of-materials) walking + built-in leaf executors. The `_LEAFS` dict maps `AbilityEffectTemplate` class names to executor functions. All current production leaves use `@effect`; retain `@leaf_register` only as a compatibility API for future compatibility-heavy leaves. |
| `builder.py` | Metadata-backed `AbilityBuilder`, `TargetRef`, `CostRef`, and `EffectRef` views over `AbilityGraph`/`AbilityInstance`, including typed values and effect conditions/continuations; no parallel card-rules source. |
| `context.py` | `EffectContext` adapter for typed values, targets, ownership, counters, stats, damage, draws, and client event/persistence operations. |
| `resolution.py` | Shared target, condition, variable, and effect-resolution helpers used by the BOM path. |
| `effects/` | Operation-specific effect implementations and leaf registry, including damage, token, search, counter, and utility effects. |
| `tac.py` | TAC v2 binary decoder. `tac_guid(b64)` extracts the ability GUID from a serialized TAC. `tac_function(b64)` extracts the operation key (e.g. `"ShiftAbility"`). |
| `conditions.py` | Pre-game condition functions (`pregame_shards_in_deck`, etc.) evaluated during the PreGame phase. `apply_pregame_abilities()` is the public entry point. |
| `kill_troop.py` | `kill_troop()` — marks a troop Dead, moves it to the graveyard, and fires its Deathcry. `state_based_deaths()` — kills any warzone troop with effective defense ≤ 0. |
| `deathcry.py` | `resolve_deathcry()` — walks a dead card's abilities and resolves Deathcry-triggered BOM leaves (CardModifier, Draw, Discard, Transform). |
| `transform.py` | `transform_card()` — replaces a card instance's template GUID, type, and stats in-place while preserving its card_uid. |
| `stat_mod.py` | `apply_card_stat_mod()` — persists +/- ATK/DEF modifiers to the `game_cards` table and pushes a `CardUpdated` event. |
| `triggers.py` | Trigger and event matching used to decide when metadata-defined abilities resolve. |
| `_shared.py` | `_log()`, `_card_state_of()`, `_stat_delta()` — tiny helpers used by multiple framework modules. |

## Adding a new BOM leaf executor

For a simple effect, use the context adapter:

```python
from abilities import effect

@effect("MyNewEffectTemplate")
def my_new_effect(effect):
    return effect.damage(effect.target(), effect.value("m_InputValue"))
```

The adapter preserves the old resolver ABI while giving the implementation a
single context. For an effect that still needs the full compatibility surface,
`@leaf_register` remains available:

```python
@leaf_register("MyNewEffectTemplate")
def _leaf_my_new_effect(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    ...
    return "log message"
```

The legacy function signature must match `(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param) -> str`.
