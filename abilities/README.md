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

## Context-style effects

The resolver still accepts the historical leaf ABI for compatibility, but new
simple leaves should use the context adapter:

```python
from abilities import effect

@effect("DrawNCardsAbilityEffectTemplate")
def draw(effect):
    return effect.draw(effect.value("m_InputValue"))
```

`EffectContext` provides typed field values, current and secondary targets,
target ownership, and shared state-changing operations. When resolution has an
active `AbilityBuilder`, typed reads are delegated through that builder so
effect code has one metadata hand-off. The adapter translates the old
positional resolver call into one context, so leaves can migrate one at a time
without creating a second execution path.

`AbilityBuilder` compiles an authoritative `AbilityGraph` into the existing
`AbilityInstance` hand-off. It exposes authored costs, target filters, typed
values, effect-local conditions, continuation dependencies, prompt
requirements, activation validation, and client effect ordering/groups. It
must not become a second card-data source: Records and the resulting
`AbilityGraph` remain authoritative.

## RulesPort resolution contract

Battle transactions enter the ability framework through `rules_port.wire` as
typed activation data. The port validates the phase, priority, source-card
collection, activation cost, thresholds, and metadata-defined targets before
an ability is allowed to resolve. An accepted activation creates one
`AbilityInstance` on the RulesPort action stack; it is not reinterpreted by a
legacy card handler.

`abilities.framework.resolution.resolve_ability` then mirrors the client
`AbilityInstance.ApplyEffectGroup` sequence:

1. Read the `AbilityGraph` and walk effect groups/instances in authored order.
2. Resolve auto, explicit, secondary, and created-card targets from the typed
   target templates.
3. Evaluate effect conditions and contingencies, then execute the registered
   BOM leaf through `EffectContext`.
4. If a UI decision is required, persist the continuation (instance, source,
   targets, variables, and resume order) and wait for the matching typed
   response. The response resumes that same instance rather than starting a
   second ability.
5. When the chain is complete, the host projection commits the SQLite
   mutation, emits the matching Unity events, and rebuilds options/priority.

The host projection is deliberately narrow: it applies an accepted mutation
and publishes `CardMoved`, `CardUpdated`, `CardDiscarded`, resource, threshold,
and priority events. It does not choose cards or infer rules from `card_text`.
For example, a discard cost uses the normal hand discard transaction, and a
choice-card ability plays the generated token before resolving its authored
threshold effect against the parent card. This is also how an AI-controlled
Shard of Cunning selects a legal Blood/Sapphire option and emits the threshold
event without opening a human picker.

Resource modifiers retain the client distinction between `currentresource`
and `totalresource`: the former is a temporary/current pool grant and the
latter increases the maximum pool. Hideous Conversion's authored `[L1][R0]`
therefore grants one temporary current resource; it is not life gain or a
permanent maximum-resource increase.

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
