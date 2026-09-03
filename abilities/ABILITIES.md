# ABILITIES — Gamedata Ability Fields Reference

This document explains every field in the four `Ability*` record families under
`Records/`, how those fields map onto the C# classes under
`HexClient/Assembly-CSharp-firstpass/`, and how the Python ability code under
`abilities/` should read a JSONL entry and implement an ability — its costs,
its duration, its targets, its conditions — **without hardcoding per-card
logic**.

This is the companion to `RULES.md` (gameplay rules) and the BOM notes in
`HOWTO.md` (database pipeline). If a field here disagrees with a C# file, the
C# file is the authority; this document is a translation aid.

---

## 1. File format

Each `Records/Ability*.jsonl` file is a **JSON Lines** file where every record
spans **two physical lines**:

```
"$$$---$$$\nAbilityTemplate"          <-- line 1: section header (json.dumps'd)
"{...raw record text...}"             <-- line 2: the record, json.dumps'd string
```

So to read a record in Python:

```python
import json

def records(path):
    with open(path) as fh:
        for i, line in enumerate(fh):
            line = line.rstrip("\n")
            if not line or line.startswith('"$'):     # section-header line
                continue
            inner = json.loads(line)                  # the outer string
            if not isinstance(inner, str):
                continue
            # inner is near-JSON; some records have trailing commas / malformed
            # fragments, so tolerate failures and regex-scrape when strict
            # parsing is not required.
            try:
                yield json.loads(inner)
            except json.JSONDecodeError:
                yield inner
```

Record structure (once decoded):

- `_v` — version + polymorphic type stack, e.g.
  `[{ "AbilityTemplate": 42 }, { "AbilityEffectTargetMapping": 15 }]`.
  The **last** entry names the concrete derived type; earlier entries are its
  base classes. `_v[0]` gives the current class version.
- `_t` — fully-qualified concrete class name, e.g.
  `"Game.Shared.Mechanics.Abilities.SummonTokenTroopAbilityEffectTemplate"`.
  The last `.`-segment (class name) is the key the server dispatch uses.
- `m_*` — fields. Nested `_t`-tagged objects are polymorphic (see §9–§11).
- GUIDs appear as `"m_Guid" : "..."` inside a `ResourceId` object
  (e.g. `m_AbilityTemplateId : { m_Guid : "..." }`). All GUIDs are lowercase in
  the DB and in client encoding.
- Booleans are `0`/`1`, enums are the **name string** (e.g. `"Instant"`,
  `"QuickAction"`, `"Self"`).
- `null` means "not set / not applicable".

---

## 2. The four record families and how they relate

| File | Records | C# class | Role |
|------|--------:|----------|------|
| `AbilityTemplate.jsonl` | 8997 | `Reckoning.Game.AbilityTemplate` | The **ability**: costs, triggers, limits, effect list, target list, options, variables. |
| `AbilityEffectTemplate.jsonl` | 5851 | `Game.Shared.Mechanics.Abilities.*AbilityEffectTemplate` (concrete subclasses of `Reckoning.Game.AbilityEffectTemplate`) | The **operation** a single effect performs (draw, summon, move, transform…), plus per-operation parameters. |
| `AbilityTargetTemplate.jsonl` | 1774 | `Game.Shared.Mechanics.Abilities.TargetTemplates.AbilityTargetTemplate` (+ subclasses) | **Who/what may be targeted** and how many targets. |
| `AbilityEffectConditionTemplate.jsonl` | 539 | `Game.Shared.Mechanics.Abilities.AbilityEffectConditionTemplate` | Named, reusable **conditions** an effect can gate on (referenced by `m_ConditionId`). |

The relationship graph:

```
AbilityTemplate
├─ m_AbilityEffectList[]  → AbilityEffectTargetMapping   (execution graph)
│    ├─ m_EffectTemplateId → AbilityEffectTemplate       (the operation)
│    ├─ m_ConditionId      → AbilityEffectConditionTemplate (gate, optional)
│    └─ m_TargetTemplateIndex → index into m_AbilityTargetTemplateIds
├─ m_AbilityTargetTemplateIds[] → AbilityTargetTemplate (legal targets)
├─ m_AbilityCondition / m_TriggerCondition / m_AbilityFreeCondition
│        → inline condition objects (not the template table)
├─ m_AbilityOptions[]     → AbilityOptions / AbilityOptionEntry (player choice)
├─ m_Variables[]          → AbilityField subclasses (named ints)
└─ cost fields            → activation/charge/spell/life, X-costs (below)
```

A card holds abilities via `CardAbilityContainer` (`m_CardAbilityId` →
`AbilityTemplate.m_AbilityTemplateId`, plus per-instance overrides). The client
denormalizes an `AbilityTemplate` into `QuickCardAbility` (a compact
serializable copy) and its options into `QuickAbilityOptions` (§12).

---

## 3. AbilityTemplate.jsonl — full field reference

`_t: "Reckoning.Game.AbilityTemplate"`, version 42.

### Identity

| Field | Type | Meaning |
|-------|------|---------|
| `m_AbilityTemplateId` | ResourceId | GUID that uniquely identifies this ability. Everything references abilities by this GUID (cards, targets, `ActivateAbilityEffectTemplate.m_AbilityToInvoke`). |
| `m_Name` | string | Designer name, editor-only (e.g. `"Play Card Ability"`). Not localized game text. |
| `m_GameText` | string | Localization key for the card's public ability text (e.g. `"When this troop enters play, draw a card"`). The **client** resolves this to the displayed string. Many of our Python executors currently parse this text to infer effect values — see §14 (do this only as a fallback). |
| `m_ActivationGameText` | string | Separate localized text shown at *activation* time (some abilities display a distinct prompt). |

### Conditions & triggers

| Field | Type | Meaning |
|-------|------|---------|
| `m_AbilityCondition` | `IAbilityCondition` (inline object) | If set, must be true for **any** portion of the ability to execute. Evaluated against the source card + session. |
| `m_TriggerEventType` | `ConstrainedType<TriggerEvent>` (inline `{m_InternalType: "..."}`) | If set, the ability is a **triggered** ability that fires on this game event (e.g. `GameStartedEvent`, `PreGameEvent`, `CardEnteredZoneEvent`, `CardAttackedEvent`). `null` → not triggered. |
| `m_TriggerCondition` | `ITriggerCondition` (inline object) | Additional gate specific to the trigger. Must be true for the ability to fire. |
| `m_TriggerCollectionFlags` | `ECardCollections` (flags string) | The zones the ability's source card must be in for the trigger to be allowed to fire. Default `"Warzone"`. |
| `m_UsesPreviousState` | bool | If true, trigger evaluation uses the card's *previous* collection/state (before the event that fired it). |
| `m_AbilityFreeCondition` | `IAbilityCondition` | If set and true, the ability costs nothing to activate (free). |

Derived client helpers built from these:
- `HasTrigger` = `m_TriggerEventType != null`
- `HasRealTrigger` = trigger exists and is not `CardCreatedEvent`
- `HasStartOfGameTrigger` = trigger is `GameStartedEvent` or `PreGameEvent`
- `IsAutomatic` = `!m_Manual && !HasTrigger && m_AbilityIndex < 0`
- `IsAutomaticOrIndexed` = `!m_Manual && !HasTrigger`

### Activation mode

| Field | Type | Meaning |
|-------|------|---------|
| `m_Manual` | bool | True = player must manually activate (an activated ability). False = automatic. |
| `m_Optional` | bool | True = the responsible player may opt out of using this ability. |
| `m_AbilityIndex` | int | `-1` = not indexed. Non-negative indexes group a set of "modular" abilities on one card (socket powers); the client exposes them as selectable powers. |
| `m_IgnoresChain` | bool | If true the ability executes immediately when invoked/triggered, bypassing the chain (e.g. gaining a charge point when a resource is played). Most abilities do NOT bypass the chain. |
| `m_CastingBehavior` | enum name | `"QuickAction"` (64) or `"BasicAction"` (8). For **manual** abilities, restricts when they can be cast per turn phase / priority. Ignored for automatic & triggered abilities. |
| `m_RecalculateAutoTargets` | bool | If false, auto-targets are chosen once at invocation. If true, any valid card played while the ability is active is folded into the target set. |
| `m_ExhaustsCardOnUse` | bool | If true, the source card taps/exhausts when the ability is activated. |

### Costs

| Field | Type | Meaning |
|-------|------|---------|
| `m_ActivationCost` | int | Resource cost to activate. |
| `m_ChargePointCost` | int | Champion charge-point cost. |
| `m_SpellPointCost` | int | Champion spell-point cost. |
| `m_LifeCost` | int | Hero-life cost. |
| `m_VariableActivationCost` | bool | X-cost: player chooses how many **additional** resources to spend. |
| `m_VariableActivationCostMinimum` | int | Floor for the X-cost choice. |
| `m_DiscountMatchCardFilter` | CardFilter | If set, cards matching this filter discount the activation cost. |

X-costs / additional card costs. Each `*Target` field is a `ResourceId` into
`AbilityTargetTemplate.jsonl`; when set, the player must supply cards for that
action as part of activation, in addition to the numeric costs above:

| Field | Cost type | Plural variant |
|-------|-----------|----------------|
| `m_SacrificeTarget` | sacrifice the chosen card(s) | — |
| `m_ExhaustTarget` | exhaust the chosen card(s) | `m_ExhaustTargets[]` |
| `m_DiscardTarget` | discard the chosen card(s) | `m_DiscardTargets[]` |
| `m_VoidTarget` | void the chosen card(s) | — |
| `m_RevealTarget` | reveal the chosen card(s) | — |
| `m_PutIntoDeckTarget` / `m_PutIntoDeckTarget2` | put chosen card(s) into the deck | — |
| `m_PutIntoHandTarget` | put chosen card(s) into the hand | — |
| `m_ShuffleIntoDeckTarget` | shuffle chosen card(s) into the deck | — |
| `m_CounterCosts[]` | counter costs (CounterCost objects) | — |
| `m_IsChargePower` / `m_IsSpellPower` | bool: this ability itself generates/uses charge / spell power | — |

`HasXCosts` (client) = any of the above targets is valid OR `m_VariableActivationCost`
OR the ability's TAC has `Mobilize > 0`. `HasAdditionalCost` = not manual AND
(has X-costs OR `m_SpellPointCost > 0` OR `m_ChargePointCost > 0` OR `m_LifeCost > 0`).

### Usage limits

| Field | Type | Meaning |
|-------|------|---------|
| `m_UsesPerTurn` | int | Max activations per turn (0 = unlimited). |
| `m_UsesPerGame` | int | Max activations per game (0 = unlimited). |
| `m_Cooldown` | int | Number of turns that must pass between uses (0 = none). |

### Effect list — `m_AbilityEffectList[]`

List of `AbilityEffectTargetMapping` objects. **This is the execution graph.**
Each entry is documented fully in §4. Key per-entry fields:

- `m_EffectTemplateId` → which `AbilityEffectTemplate` (operation)
- `m_TargetTemplateIndex` → which of `m_AbilityTargetTemplateIds` this effect uses
- `m_EffectInstanceId`, `m_EffectGroupId`, `m_EffectDuration`, `m_ContingentEffectInstanceId`,
  `m_ConditionId`, `m_IsOptional`, `m_RecalculateTargets`, `m_OutputVariables`,
  `m_Layer`, `m_SecondaryTargetIndex`, plus VFX fields.

The client **sorts** this list at `Initialize()` by `m_EffectGroupId` then
`m_EffectInstanceId` (`AbilityTemplate.SortByEffectExecutionOrder`); effects in
the same group execute "simultaneously". The server must mirror that ordering
when walking the BOM (§13).

### Targets — `m_AbilityTargetTemplateIds[]`

`List<ResourceId>` of `AbilityTargetTemplate` GUIDs. Effects reference them by
index (`m_TargetTemplateIndex`). Several client helpers exist:

- `GetTargetTemplateIdForEffect(effectTemplateId)` → first mapping whose effect id matches.
- `GetEffectInstancesThatUseTargetTemplate(targetTemplateId)` → effect instance ids.
- `HasExplicitTargets()` / `HasOnlyAutoTargets()` / `UntargetedTrigger()` — derived from the
  targets' `m_IsAutoTarget` / `m_Explicit` flags (§9).
- `AreTargetsComplete(activationData, effectInstanceId)` / `AreXCostsComplete(...)` /
  `AreVariablesComplete(...)` / `AreOptionsComplete(...)` — the four completeness gates the
  client uses to decide when an activation is legal. **A Python resolver should implement all
  four checks to mirror client behaviour.**

### Options — `m_AbilityOptions[]`

`List<AbilityOptions>` — the player choice prompt(s) shown before activation.
See §10.

### Variables — `m_Variables[]`

`List<AbilityField>` — named integer variables for this ability
(`AbilityConstant`, `AbilityVariable`, and the many `*AbilityVariable`
subclasses). Effects reference them by name via `EffectInputVariable`.
A variable whose name matches an effect's `m_OutputVariables` value is treated
as an output (`EffectOutputVariable`) at `Initialize()`.
See §11.

### Misc

| Field | Type | Meaning |
|-------|------|---------|
| `m_Threshold[]` | `List<CardThreshold>` | Threshold requirements to activate (e.g. shard-colour counts). |
| `m_AICustomAbilityEvaluator[]` | list | AI hints for evaluating whether to use the ability. |
| `m_AIHints` | string | Free-text AI hints. |
| `m_SerializedTAC` | `SerializableTAC` | Binary TAC blob; at runtime becomes the ability's `TAC` (template attribute collection). Overrides/extra rules live here (e.g. `Mobilize`, attribute ops). Decoder: `abilities/framework/tac.py`. |

---

## 4. AbilityEffectTargetMapping (per-effect wiring)

C# class: `Reckoning.Game.AbilityEffectTargetMapping`, version 15.
These are the objects inside `AbilityTemplate.m_AbilityEffectList[]`.

| Field | Type | Meaning |
|-------|------|---------|
| `m_EffectTemplateId` | ResourceId | GUID of the `AbilityEffectTemplate` to run (§5). |
| `m_TargetTemplateIndex` | int | Index into `AbilityTemplate.m_AbilityTargetTemplateIds`. Multiple effects can share a target template. `-1`/out-of-range → no target. |
| `m_SecondaryTargetIndex` | int | Index of the *secondary* target template (`-1` = none). Used by effects that act on two target sets (e.g. "target a troop, then target another"). |
| `m_EffectInstanceId` | int | Unique id of this effect within the ability (must be unique; `AbilityTemplate.IsValid()` checks this). Used by `m_ContingentEffectInstanceId`. |
| `m_EffectGroupId` | int | Effects in the same group execute "simultaneously"; groups run in ascending order, then instance id. **The server must not yield between effects of the same group.** |
| `m_EffectDuration` | enum name | How long the effect remains active. See `EAbilityDurations` in §6. |
| `m_ContingentEffectInstanceId` | int | If `>= 0`, this effect only happens if the effect with that instance id was *successfully applied* first. `-1` = no contingency. |
| `m_ConditionId` | ResourceId | GUID into `AbilityEffectConditionTemplate.jsonl`; if set, the condition must hold or the effect fizzles. Zero-GUID = no condition. |
| `m_IsOptional` | bool | If true, the target player may choose whether this effect activates. |
| `m_RecalculateTargets` | enum name | `"UseDefault"` / `"False"` / `"True"` (`ETrueFalseUseDefault`). Forces the system to regenerate auto-targets before running this effect for the first time. |
| `m_OutputVariables` | dict | `{EAbilityEffectProperties: string}` map naming which output property feeds which ability-variable name (e.g. `{"TotalDamageDealt": "damageDealt"}`). See `EAbilityEffectProperties` in §6. |
| `m_Layer` | int | Sort key for "CEs" (continuous effects); effects are sorted by layer. |
| `m_VfxProjectileType` | enum | Visual only — projectile animation. |
| `m_VfxEventType` | enum | Visual only — event animation. |
| `m_VfxTargetSelectionType` | enum | Visual only — `"Targetable"` etc. |

---

## 5. AbilityEffectTemplate.jsonl — the operations

C# base: `Reckoning.Game.AbilityEffectTemplate` (abstract), version 10;
concrete operations derive from `CardAbilityEffectTemplate` (most) or directly.

### Common fields (every record)

| Field | Type | Meaning |
|-------|------|---------|
| `m_TemplateId` | ResourceId | GUID identifying this effect template. Referenced by `AbilityEffectTargetMapping.m_EffectTemplateId`. |
| `m_Name` | string | Designer name (e.g. `"Built-In Play Card Effect"`). |
| `m_EditorVariableMap` | dict | Editor-only variable bookkeeping (`{name: value}`). |
| `m_GameText` | string | Localization key for this effect's text (e.g. a CardModifier's `"+2[ATK]/+2[DEF]"` or a SummonToken's `"Create two X"`). The extractors currently parse amounts from this text; the authoritative values are the typed fields below. |
| `m_SerializedTAC` | SerializableTAC | TAC blob specific to this effect (e.g. `TACAbilityEffectTemplate` uses this for its operation + referenced ability GUID). |

### How to dispatch

Read `_t` (or the last `_v` entry), take the final `.`-segment as the class
name, and look up an executor keyed by that name. That class name is exactly
what `ability_effects.effect_type` stores and what `_LEAFS` in `bom.py` is
keyed on.

### Per-operation parameters (most common 60 of ~120 effect classes)

Representative types observed in the dataset and their extra fields (all nested
under the record). This is not exhaustive — new effect classes appear; treat
unknown `_t` values as "needs a new leaf executor".

- **`CardModifierAbilityEffectTemplate`** (1558 records) — apply a stat/property
  modifier. Field: `m_Modifier` (a polymorphic `Modifier` object; subclasses like
  AttackModifier/DefenseModifier carry the delta). The extractor flattens the
  delta + property + duration into `ability_effects.param` as JSON
  `{"text", "property", "amount", "duration"}`.
- **`SummonTokenTroopAbilityEffectTemplate`** (1239) — create tokens.
  Fields: `m_CardTemplateId` (token GUID), `m_Amount` (int) / `m_AmountField`
  (EffectField), `m_CardCollection` (where they appear: `Warzone`/`Deck`),
  `m_CardLocation` (`Top`/…), `m_CopyGems`, `m_EntersPlayExhausted`,
  `m_EntersPlayAttacking`, `m_CombinedCost` (EffectField), `m_Terminus`,
  `m_CardFilter` (filter for matching-target variants).
- **`MoveCardToZoneEffectTemplate`** (446) — move a card between zones.
  Fields: `m_DestinationCollection`, `m_DestinationLocation`, and the control
  flags: `m_AbilityOwnerTakesControl`, `m_AbilityOpponentTakesControl`,
  `m_ArenaChampionTakesControl`, `m_PreviousControllerTakesControl`,
  `m_ControlGivenToTargetIndex` (`-1` = unchanged), `m_AllCardsOfTargetInZone`
  (move all of a target's cards from this zone), `m_RandomLocation`,
  `m_EntersPlayExhausted`, `m_EntersPlayAttacking`, `m_TopHalfOfDeck`.
- **`ActivateAbilityEffectTemplate`** (891) — invoke another ability.
  Fields: `m_AbilityToInvoke` (GUID into AbilityTemplate), `m_RandomlyLuckyOrUnlucky`.
  This is the **recursion** primitive: the BOM walker follows it.
- **`GrantAbilityEffectTemplate`** (562) — give an ability to a card.
  Fields: `m_GrantedAbilityTemplateId`, `m_AbilityIsUnique`,
  `m_RandomInspirePower`, `m_RandomChampionChargePower`, `m_AllSocketedPowersOfSource`,
  `m_AllSocketedPowersOfTarget`, `m_AllPaymentPowersOfSource`,
  `m_AllPaymentPowersOfTarget`, `m_AllSocketedPowersOfMyMaster`, `m_AllRememberedPowers`.
- **`TransformCardAbilityEffectTemplate`** (296) — transform target into a
  specific template. Fields include `m_Portal`. (Siblings:
  `TransformCardIntoReplicaAbilityEffectTemplate`,
  `TransformSelfAbilityEffectTemplate`, `TransformCardToTargetAbilityEffectTemplate`,
  `TransformCardAtRandomAbilityEffectTemplate` — the last uses `m_Filter` +
  `m_CantBeSameCard`.)
- **`DrawNCardsAbilityEffectTemplate`** (84) — draw N. Field: `m_InputValue`
  (EffectField). Sibling: `PutTopOfDeckIntoHandAbilityEffectTemplate`.
- **`RandomizeVariableEffectTemplate`** (75) — roll a value into a variable.
  Fields: `m_MinValue`, `m_MaxValue`, `m_MaxValueField` (EffectField), `m_SecondValue`.
  (Our `bom.py` registers this as `RandomizeVariableAbilityEffectTemplate`, a
  stale name — see §14.)
- **`StoreTargetsAbilityEffectTemplate`** (102) — remember targets for later
  (e.g. "this turn, that troop …"). Fields: `m_TargetIndex`, `m_OnlyUntilEndOfTurn`, `m_SetTargets`.
- **`BuryCardAbilityEffectTemplate`** (61) — mill. Fields: `m_Amount`
  (EffectField), `m_TopHalfOfDeck`, `m_Filter`.
- **`DestroyCardAbilityEffectTemplate`** / `DestroyCardByDefenseAbilityEffectTemplate` — destroy target(s).
- **`UntapCardAbilityEffectTemplate`** (33) / **`TapCardAbilityEffectTemplate`** (1) — ready/exhaust.
- **`RevealCardsAbilityEffectTemplate`** (43) — reveal. Fields: `m_PlayerRevealTargets`, ….
- **`RepeatingAbilityEffectTemplate`** (20) — loop. Fields: `m_RepeatingEffect`
  (nested `CardAbilityEffectTemplate`), `m_LoopCount` (EffectField) / `m_LoopCount_DEPRECATED`.
- **`Battle2CardsAbilityEffectTemplate`** (48) — fight. Field: `m_FightBack` (default true).
- **`PlayCardAbilityEffectTemplate`** (47) — play a card for free. Field: `m_JustActivate`.
- **`TACAbilityEffectTemplate`** (66) — TAC-driven operation (Shift etc.).
  Fields: `m_SerializedTAC` (decoder in `abilities/framework/tac.py`).
- **`VoidCardAbilityEffectTemplate`**, **`SacrificeCardAbilityEffectTemplate`**,
  **`DiscardCardAbilityEffectTemplate`**, **`CounterSpellAbilityEffectTemplate`**,
  **`InterruptSpellAbilityEffectTemplate`**, **`SwapHealthAbilityEffectTemplate`**,
  **`GiveBonusTurnAbilityEffectTemplate`**, **`LoseGameAbilityEffectTemplate`**,
  **`ReplenishResourcesAbilityEffectTemplate`**, etc. — usually parameterless;
  their behaviour is entirely from the operation name + target templates.

When adding a leaf executor, **read the concrete C# file** for the authoritative
parameter list rather than guessing from `m_GameText`.

> **Complete per-effect field map:** every distinct `_t` seen in
> `AbilityEffectTemplate.jsonl` (60 effect classes + 25 modifier types +
> 33 card filters + 3 value fields) and the exact `m_*` JSON fields each one
> consumes is listed in **Appendix A (§16)**. It is generated from the
> `[TDFIncludeField]` members of the C# classes and their base classes, so it is
> authoritative for what a Python leaf executor may read from the record.

---

## 6. Enums referenced by the above fields

### EAbilityDurations (`m_EffectDuration`)
`Unknown, Instant, WhileCardInPlay, EndOfTurn, BeginningOfOwnersTurn, EndOfGame,
Permanent, WhileCardTapped, AfterCardsReadyOnPlayersTurn, AfterNextTimeDamaged,
UntilItLeavesYourHand, UntilAttacksOrBlocks, EndOfNextTurn, UntilEndOfDungeon,
WhileTargetInPlay, UntilDamaged, WhileCardOnTopOfDeck, BeginningOfOpponentsTurn`

The duration dictates **when an effect's side effect ends** — e.g. a `WhileCardInPlay`
modifier must be torn down when the source leaves play; `EndOfTurn` at the
owner's cleanup. The server currently treats most durations as "apply and forget";
a full implementation must track duration expiry (see §14).

### EAbilityEffectProperties (`m_OutputVariables` keys)
`Unknown, NumberOfTargetsAffected, TotalTargetDefense, TotalTargetAttack,
NumberOfTargetsDestroyed, TotalDamageDealt, TotalTargetCost`

The value of each is stored into the ability variable named by the map value
(after the effect runs), so later effects can consume it.

### ECardCollections (`m_TriggerCollectionFlags`, `m_CardCollection`, etc.)
Flags: `None=0, Deck=1, Hand=2, Champions=4, Warzone=8, Discard=16, Void=32,
PlayedResources=64, CastSpells=128, Underground=256, Choosing=512, Mod=1024,
Simulacrum=2048, UI_Warzone=4096, UI_Constant=8192`. String forms can be
`"Deck|Hand|..."` pipe-combined.

### EPlayerCardTargets (`m_PlayerFilter`)
`Unknown, Self, SingleOpponent, SinglePlayer, MultipleOpponents, MultiplePlayers`

### EAbilityCastingBehavior (`m_CastingBehavior`)
`QuickAction = 64, BasicAction = 8`

### ETrueFalseUseDefault (`m_RecalculateTargets`)
`UseDefault = -1, False = 0, True = 1`

---

## 7. AbilityTargetTemplate.jsonl — full field reference

C# base: `Game.Shared.Mechanics.Abilities.TargetTemplates.AbilityTargetTemplate`,
version 12. 1774 records; 1564 are the base type, the rest are subclasses.

### Core fields

| Field | Type | Meaning |
|-------|------|---------|
| `m_TemplateId` | ResourceId | GUID of this target template; referenced by `AbilityTemplate.m_AbilityTargetTemplateIds` and by `m_*Target` cost fields. |
| `m_Name` | string | Designer name (e.g. `"NoOp Card Target"`). |
| `m_GameText` | string | Localization key for target text ("target troop", "target player", …). |
| `m_IsAutoTarget` | bool | If true, targets resolve without player input (system picks them). |
| `m_IsRandomTarget` | bool | If true the server determines targets randomly (must also be an auto-target). |
| `m_PlayerFilter` | enum name | Which player(s) must own the target cards (`Self`, `SingleOpponent`, …). |
| `m_CollectionFlags` | ECardCollections | Zones that may contain targets (`None` = any). |
| `m_CardFilter` | CardFilter | Predicate restricting which cards are legal targets (nested filter tree, §11). |
| `m_IsOptional` | bool | If true, zero targets may be chosen even if `m_MinTargetCount > 0`. |
| `m_Explicit` | bool | If true, this is an explicitly-stated target in the card text (affects the client's targeting UI and `HasExplicitTargets()`). |
| `m_MinTargetCount` | TargetField | Minimum targets (constant or variable, §11). |
| `m_MaxTargetCount` | TargetField | Maximum targets; `null` → unbounded. |
| `m_AllowBestEffortMinimumTargetCount` | bool | If true, minimum = the smaller of the minimum and the number of legal targets (e.g. "opponent discards 2 troops" is best-effort). If false, the ability is unusable unless the minimum can be fully met (e.g. "discard 2 target troops"). |
| `m_MinimumTargetCount_DEPRECATED` / `m_MaximumTargetCount_DEPRECATED` | AbilityField | Legacy fields; `Initialize()` migrates them into `m_MinTargetCount`/`m_MaxTargetCount`. Treat as ignored. |

### Derived client behaviour to mirror
- `IsVariableTarget` = min or max is a `TargetVariable` (a reference, not a constant).
- `GetMinimumTargetCount` / `GetMaximumTargetCount` resolve the TargetField at runtime.
- `ValidateTargetCount(count, …)` = `count <= max && validateMinimum`, where
  validateMinimum applies `m_IsOptional`, `m_AllowBestEffortMinimumTargetCount`,
  and `CountLegalTargets`.
- Legal-target enumeration filters by: player filter, collection flags, card
  filter, spell-shield, spectral, and targeting immunities.

### Subclasses (specialized target resolution)
`AbilitySourceCardTargetTemplate`, `AbilityTriggerCardTargetTemplate`,
`AbilityTriggerTargetTargetTemplate`, `AbilityTriggerTargetsControllerTargetTemplate`,
`AbilityCreatedTargetTemplate`, `SourceDrawnTargetTemplate`, `SourceRevealedTargetTemplate`,
`SourceStoredTargetTemplate`, `SourceBuriedTargetTemplate`, `VoidedTargetTemplate`,
`PlayerTargetTemplate`, `TargetsAPlayerOrHisStuff`, `PlayerOrHisStuff`,
`MatchSecondaryTargetTemplate`, `SecondaryTargetTemplate`, `ChildCardsTargetTemplate`,
`DuplicateCardTargetTemplate`, `SharedNameTargetTemplate`, `ActivePlayerTargetTemplate`,
`ParentCardTargetTemplate`, `ParentStoredTargetTemplate`. Subclass-specific fields
appear as extra `m_*` keys (e.g. `m_TriggerSelector`, `m_StoredInAbility`,
`m_StoredInCard`, `m_IgnoreActedOn`, `m_SameCost`, `m_SameName`, `m_SharesRace`,
`m_CantBePreviousTarget`, `m_ChoiceOverride`).

---

## 8. AbilityEffectConditionTemplate.jsonl — conditions

C# class: `Game.Shared.Mechanics.Abilities.AbilityEffectConditionTemplate`, version 1.

| Field | Type | Meaning |
|-------|------|---------|
| `m_TemplateId` | ResourceId | GUID referenced by `AbilityEffectTargetMapping.m_ConditionId`. |
| `m_Name` | string | Designer name (e.g. `"YouHave100OrMoreCardsInYourDeck"`). |
| `m_Condition` | `IAbilityEffectCondition` (inline object) | The actual predicate; polymorphic (§11). |

The inline `m_Condition` object carries the condition's own fields, e.g.:

- `RequiresCardsControlled` — `m_CardFilter` (nested), `m_CardCollection` (zone
  string like `"Deck|Hand|Champions|Warzone|Discard|Void|CastSpells|Underground|Choosing"`),
  `m_RequiredQuantity`, `m_ComparisonOp` (`"GreaterThanOrEqual"`, …), `m_PlayerFilter`
  (`"Self"`, …), `m_SameOwner`, `m_QuantityIsHighestOpposingChampionsMatch`,
  `m_QuantityIsLowestOpposingChampionsMatch`, `m_OnlyIncludeDifferentCosts`,
  `m_OnlyIncludeDifferentNames`, `m_OnlyIncludeDifferentRacesForFaction`, `m_QuantityCardFilter`.
- Composites: `AndAbilityCondition` / `OrAbilityCondition` / `NotAbilityCondition`
  carry `m_Conditions[]` (nested condition objects).
- Date/time: `IsDate` conditions carry `m_Year, m_Month, m_Day, m_DayOfWeek,
  m_DayOfYear, m_Hour, m_Minute, m_Second`.
- Comparison conditions carry `m_Lhs`, `m_Rhs`, and the compared property fields
  (`m_Attribute`, `m_Value`, `m_CompareToCost`, `m_CardName`, `m_CompareToAbilitySource`,
  `m_CompareToTriggerSource`, `m_CompareToTriggerTarget`, `m_EffectIndex`, …).

The condition interface `IAbilityCondition.IsValid(sourceCard, session)` (and the
card-representation variant) is the runtime contract. On the server, pre-game
conditions are currently summarized into compact `condition` specs on
`talent_abilities` (see §13); the full predicate lives here.

---

## 9. Card filters and conditions (shared building blocks)

`CardFilter` is a nested, polymorphic tree. Fields seen inside filters:

- `AndCardFilter` — `m_TargetFilters[]` (list of filters, all must match)
- `IsType` — `m_CardType` (string like `"Troop|BasicAction|Resource|Artifact|QuickAction|Constant|Bane|Choice"`)
- `IsControlledBy` — `m_TestAgainstActivePlayer` (0/1)
- `InZone` — `m_Collection` (`"Deck"`, `"Warzone"`, …)
- plus `m_PlayerFilter`, `m_RequiredQuantity`, `m_ComparisonOp`, `m_SameOwner`,
  `m_Attribute`, `m_Value`, `m_CompareToCost`, `m_SubType`, `m_Rarity`,
  `m_ResourceCost` variants (`m_AddX`, `m_AddAttack`, `m_AddDefense`,
  `m_AddSourceCardsAttack`, `m_AddSourceCardsCost`, `m_AddRemovedCounters`,
  `m_AddDamageDealt`, `m_AddCardIntegerVariable`, `m_ResourceCostCardFilter`, …),
  `m_ColorFlags`, `m_IncludeResources`, `m_Prismatic`, `m_ThresholdColorFlags`,
  `m_IsBasicResource`, `m_IsNonStandardResource`, `m_MustBeAI`, `m_MustBeHuman`,
  `m_CardCounterTemplateId`, `m_RequiredCounters`, `m_FailUncontrolledCards`,
  `m_TopHalfOfDeck`, `m_CountFromBottom`, `m_ContainsString`, `m_IncludeKeywords`,
  `m_Keyword`, `m_LanguageCode`, `m_EquipmentType`, `m_CardAttributeFlags`,
  `m_AttackValue`, `m_DefenseValue`, `m_CompareToSourceControlledCardFilterCount`,
  `m_CompareToCardIntegerVariable`, `m_CompareToStoredTarget`, `m_Faction`, `m_Zone`.

Filters appear in targets, conditions, variables (`CardCountAbilityVariable`,
`CardSumAbilityVariable`), summon effects (`m_CardFilter`), and cost discounts
(`m_DiscountMatchCardFilter`).

---

## 10. Options — AbilityOptions / AbilityOptionEntry

C#: `Game.Shared.Mechanics.Abilities.AbilityOptions` (v2) and
`AbilityOptionEntry` (v1). Held in `AbilityTemplate.m_AbilityOptions[]`.

| Field | Type | Meaning |
|-------|------|---------|
| `m_TargetProperty` | `EAbilityProperties` | Which property the chosen option affects. |
| `m_Label` | string | Option prompt text ("Choose one", "Which resource?", …). |
| `m_Options[]` | `AbilityOptionEntry[]` | The selectable choices. |

Each `AbilityOptionEntry`:

| Field | Type | Meaning |
|-------|------|---------|
| `m_Value` | int | The option value the server receives / writes. |
| `m_Label` | string | Button text shown to the player. |

Client: `QuickAbilityOptions` copies these to `ValueOptions[]`/`LabelOptions[]`.
The activation is only complete when every option index has a chosen value
(`AbilityTemplate.AreOptionsComplete`). Resource options are recognised by the
labels `[BLOOD]`, `[WILD]`, `[RUBY]`, `[DIAMOND]`, `[SAPPHIRE]`
(`AbilityOptions.IsResourceOptions`).

---

## 11. Variables and value fields

These objects resolve to **integers at runtime**. They appear in
`AbilityTemplate.m_Variables[]`, `m_MinTargetCount`/`m_MaxTargetCount`
(as `TargetField`), and effect parameter slots (as `EffectField`).

### The `AbilityField` hierarchy
- **`AbilityField`** (base): `m_Name`, `m_DefaultValue`.
- **`AbilityConstant`** (`_t` …`AbilityConstant`): a literal — `m_DefaultValue`
  is the value. This is what most `m_Variables[]` entries are in practice.
- **`AbilityVariable`**: named runtime variable. Extra fields: `m_RequiresExplicitSet`,
  `m_RequiresPlayerInput` (player must supply a value at activation → must be
  gathered by the server and checked by `AreVariablesComplete`).
- **`EffectOutputVariable`**: reads a named output property off an effect instance
  (used to close the loop between `m_OutputVariables` and a later effect).
- **Many specialized `*AbilityVariable` subclasses** — each derives its value from
  the game state by name: `CardCountAbilityVariable`, `CardSumAbilityVariable`,
  `TargetCardsCountAbilityVariable`, `SourcePlayerHealthVariable`,
  `SourcePlayerChargeVariable`, `SourcePlayerResourceAbilityVariable`,
  `SourcePlayerShardAbilityVariable`, `SourcePlayerThresholdAbilityVariable`,
  `TargetPlayerThresholdAbilityVariable`, `IntAttrAbilityVariable`,
  `CardPropertyVariable`, `AbilityPropertyVariable`, `TriggerEventProperty`,
  `CounterVariable`, `KeywordAbilityVariable`, `HighestCardAbilityVariable`,
  `TagValueVariable`, `ExpressionAbilityVariable`, `CardIntegerVariable`, …
  Each carries its own query fields (`m_CardFilter`, `m_CollectionFlags`,
  `m_PlayerFilter`, `m_Property`, `m_Attribute`, `m_IntAttrName`,
  `m_ExpressionText`, `m_DontRecalculate`, …).

### `TargetField` (min/max target counts)
- **`TargetConstant`** (`_t` `TargetConstant`): `m_Value` literal (e.g. `1`).
- **`TargetVariable`**: `m_AbilityVariableName` — resolves via the ability
  instance's variable table (`TryGetAbilityVariable`).

### `EffectField` (effect parameters like draw count, summon amount, bury count)
- **`EffectConstant`**: `m_Value`.
- **`EffectInputVariable`**: `m_InputVariableName` — resolves via
  `TryGetInputVariable`.
- **`EffectOutputVariable`**: reads an `EAbilityEffectProperties` output.

---

## 12. Relationship to the client classes

| Client class | Purpose | How it maps to the records |
|--------------|---------|----------------------------|
| `Reckoning.Game.AbilityTemplate` | Full ability definition | Direct serialization target of `AbilityTemplate.jsonl`. |
| `Reckoning.Game.CardAbilityContainer` | Card → ability link | `m_CardAbilityId` (ResourceId) = `AbilityTemplate.m_AbilityTemplateId`; `m_CardAbilityOverrides` = per-card overrides of `[OverrideVariable]`-marked cost fields. |
| `Reckoning.Game.AbilityEffectTargetMapping` | Per-effect wiring | Nested in `m_AbilityEffectList`. |
| `Reckoning.Game.QuickCardAbility` | Compact, serializable copy of an `AbilityTemplate` sent to the client | `CopyValues()` copies: `m_AbilityTemplateId`, trigger-allocated flag, `m_ActivationCost`, `m_ChargePointCost`, effect maps, target ids, `m_VariableActivationCost`, options, sacrifice/life/optional/manual, exhaust/discard/void/shuffle/put-deck/put-hand targets, `m_SpellPointCost`, discard/exhaust target lists, `m_VariableActivationCostMinimum`, charge/spell-power flags, variables, `m_AbilityIndex`. |
| `Reckoning.Game.QuickAbilityOptions` | Options serialized to the client | `ValueOptions[]`, `LabelOptions[]`, `Label` from `AbilityOptions`/`AbilityOptionEntry`. |
| `Reckoning.Game.AbilityEffectTemplate` (base) | Effect operation + common fields | Abstract base of `AbilityEffectTemplate.jsonl`. |
| `Game.Shared.Mechanics.Abilities.AbilityEffectConditionTemplate` | Named condition | `AbilityEffectConditionTemplate.jsonl`. |
| `Game.Shared.Mechanics.Abilities.TargetTemplates.AbilityTargetTemplate` | Legal target set | `AbilityTargetTemplate.jsonl`. |

`AbilityTemplate` also exposes helpers the client uses to drive the UI, all of
which the server must reproduce to stay authoritative (§13, §14):
`HasOptions`, `HasTrigger`, `HasRealTrigger`, `HasStartOfGameTrigger`,
`UntargetedTrigger`, `HasXCosts`, `HasAdditionalCost`, `AreOptionsComplete`,
`AreTargetsComplete`, `AreXCostsComplete`, `AreVariablesComplete`,
`HasExplicitTargets`, `HasOnlyAutoTargets`, `HasTargetTemplate(type)`.

---

## 13. Server-side pipeline: JSONL → SQLite → BOM

The server does **not** read the JSONL files at runtime. The gamedata is
materialized into `hconnect.db` at seed time:

1. **`AssetExtraction/gamedata_seed.py`** reads the client gamedata or
   `Records/*.jsonl` and inserts the extracted seed rows into a fresh SQLite
   database. The generated client data is deliberately not copied into
   `static.py`.
2. **`static.ensure_schema(db)`** seeds the tables when empty:

   - `talent_abilities(talent_guid, ability_guid, charge_cost, spell_cost,
     activatable_phases, casting_behavior, condition)` — head of the BOM for a
     **talent**: costs + phase + a compact condition spec.
   - `ability_effects(ability_guid, effect_guid, effect_order, effect_type,
     param)` — the ordered leaf chain, expanded **transitively** through
     `ActivateAbilityEffectTemplate.m_AbilityToInvoke`. `effect_type` is the
     concrete class name; `param` is `m_AbilityToInvoke` (or, for
     `CardModifierAbilityEffectTemplate`, a JSON blob of text/property/amount/
     duration; for `TACAbilityEffectTemplate`, the serialized TAC data).
   - `card_abilities_meta(ability_guid, casting_behavior, is_manual,
     activation_cost, uses_per_game, uses_per_turn, cooldown, exhausts_on_use,
     is_triggered, target_template_ids, trigger_event_type, game_text, raw_json)` —
     per-ability activation metadata for **card** abilities, mirroring the
     talent cost/phase columns, plus `target_template_ids` (JSON list of target
     GUIDs — the client's targeting picker keys on these) and `raw_json` (the
     full cleaned ability record so the resolver can data-drive per-effect
     values at runtime).
   - `target_templates(template_id, game_text)` — target template GUID → text.
   - `champion_abilities(champion_guid, …)` — champion charge powers and their
     cost/game text.
3. **Resolution**: `abilities.resolve_effect(guid)` returns a registered custom
   handler (in `abilities/cards/`) or a BOM-walking wrapper that iterates
   `ability_effects` in `effect_order` and dispatches each `effect_type` to a
   leaf executor in `_LEAFS` (`abilities/framework/bom.py`), recursing through
   `ActivateAbilityEffectTemplate` rows via `param`.

### Data-driven resolution algorithm (target state)

For an ability GUID, build a resolution plan without any card-specific code:

```python
def plan_ability(db, ability_guid):
    """Return an ordered plan for one ability GUID."""
    meta = db.execute(
        "SELECT casting_behavior, is_manual, activation_cost, uses_per_game, "
        "uses_per_turn, cooldown, exhausts_on_use, is_triggered, "
        "target_template_ids, trigger_event_type, raw_json "
        "FROM card_abilities_meta WHERE ability_guid=?", (ability_guid,)).fetchone()

    target_ids = json.loads(meta[8])          # AbilityTargetTemplate GUIDs, in order
    targets = [load_target_template(g) for g in target_ids]

    bom = db.execute(
        "SELECT effect_guid, effect_type, param FROM ability_effects "
        "WHERE ability_guid=? ORDER BY effect_order", (ability_guid,)).fetchall()

    plan = []
    for effect_guid, effect_type, param in bom:
        # 1. Resolve the concrete AbilityEffectTemplate (raw_json or Records/).
        # 2. Resolve its wiring from the parent ability's m_AbilityEffectList:
        #    effect duration, target index, condition, contingency, optional.
        # 3. Resolve the target template at m_TargetTemplateIndex.
        # 4. Resolve the effect's typed parameters (draw N, summon amount,
        #    destination zone, …) from the effect record fields.
        # 5. Resolve m_Variables to literals / runtime lookups.
        # 6. Dispatch to a leaf executor keyed by effect_type.
        plan.append((effect_type, effect_guid, param))
    return plan
```

The four **completeness gates** (mirror `AbilityTemplate`):
1. options complete — every `m_AbilityOptions` index has a chosen value;
2. targets complete — every non-auto target template has a chosen target set;
3. X-costs complete — every `m_*Target` cost field is satisfied (or auto);
4. variables complete — every `AbilityVariable` with `m_RequiresPlayerInput`
   has a value.

Effect ordering must follow `sort by m_EffectGroupId, then m_EffectInstanceId`,
and effects in the same group are simultaneous (no server yields between them).

---

## 14. Support matrix — what the Python framework currently implements

The Python layer is a **work in progress**. This table shows what is genuinely
data-driven today vs. what is inferred from game text vs. what is missing.

### Implemented leaf executors (`abilities/framework/bom.py`, `_LEAFS`)

| `effect_type` (class name) | Status | Notes |
|----------------------------|--------|-------|
| `DrawNCardsAbilityEffectTemplate` | implemented | Uses the typed `m_InputValue` field, including dynamic ability variables; localized text is only a legacy fallback. |
| `PutTopOfDeckIntoHandAbilityEffectTemplate` | implemented | Moves AI deck-top to hand. |
| `DiscardCardAbilityEffectTemplate` | implemented | Moves the metadata-selected hand/choosing card to its owner's discard pile and emits the discard, move, and updated-card events. |
| `CardModifierAbilityEffectTemplate` | implemented (text-derived) | Amount/property read from `param` JSON when present, else parsed from game text (`+N[ATK]`/`+N[DEF]`, "gain/lose health"). |
| `SummonTokenTroopAbilityEffectTemplate` | implemented | Uses typed token GUID, amount/amount field, collection/location, exhausted/attacking, filter, and copy-gems fields; links/text are compatibility fallbacks. |
| `MoveCardToZoneEffectTemplate` | implemented | Reads the typed `m_DestinationCollection` when present, with legacy effect parameters as a fallback; selected revealed cards are reinserted into the deck. |
| `BuryCardAbilityEffectTemplate` | implemented | Count from `param` JSON, else 1. |
| `VoidCardAbilityEffectTemplate` | implemented | Moves the resolved target to Void, emits the client zone events, records the source/voided relationship, and fires exit triggers. |
| `UntapCardAbilityEffectTemplate` / `TapCardAbilityEffectTemplate` | implemented | Changes the resolved card state and supports metadata auto-target lists. |
| `AnimationTriggerEffectTemplate` | implemented | Reads the typed `m_AnimationTrigger` enum and emits the client class-76 session event. |
| `BlockEffectTemplate` | implemented | Uses the authored secondary target as the blocker, validates the active combat assignment, updates PvE/PvP blocker state, emits class-28 `BlockersAssigned`, and runs blocked-card triggers. |
| `DoubleChoiceAbilityEffectTemplate` | implemented | Creates the metadata-defined random Choice cards, exposes the built-in Choose-and-Play picker, supports the second-choice stage, and resumes the parent ability after selection in PvE and PvP. |
| `TransformCardAbilityEffectTemplate` | implemented | Target from bstate; template GUID from game-text link or `effect_guid`. |
| `ActivateAbilityEffectTemplate` | implemented | Recurses via `param` (m_AbilityToInvoke). |
| `TACAbilityEffectTemplate` | partial | Decodes operation + GUID; only `ShiftAbility` handled. |
| `RandomizeVariableEffectTemplate` | partial | Registered under stale name `RandomizeVariableAbilityEffectTemplate`; delegates to `replenish_spell_power`. |
| `GrantAbilityEffectTemplate`, `PlayCardAbilityEffectTemplate`, `FireEventEffectTemplate`, `RevertPermanentModificationsAbilityEffectTemplate`, `RevealCardsAbilityEffectTemplate`, `StoreTargetsAbilityEffectTemplate`, `VerdictAbilityEffectTemplate` | stubs | Log-only placeholders. |

`RepeatingAbilityEffectTemplate` is resolved by the main resolver because it
contains a nested typed ability and loop-count field rather than a normal
per-target leaf. Random target templates are likewise resolved from their
metadata filter and count, even when the template is not marked as an
auto-target in the extracted record.

`ConversationAbilityEffectTemplate` is the remaining unregistered effect
family. It is deliberately left for human guidance because it starts authored
campaign/UI conversations rather than changing card state; implementing it
requires deciding which campaign conversation and client presentation each
record should invoke.

### Triggered-ability handling (`abilities/framework/triggers.py`, `deathcry.py`)
`resolve_triggers` fires abilities whose `card_abilities_meta.trigger_event_type`
matches a supported event (`AsEntersPlayEvent` Inspire, `CardEnteredZoneEvent`
Deploy/Deathcry, `CardAttackedEvent`, `CardBlockedEvent`, `CardInspiredEvent`).
`resolve_deathcry` walks a dead card's BOM. These cover a subset of
`TriggerEvent` types; other triggers (e.g. `CardCastEvent`, `SpellCastEvent`,
`DamageEvent`, …) are not yet wired.

### NOT yet data-driven (work needed)
- **Target resolution** — targets are currently hand-picked into `bstate`
  (`player_spell_target`, `player_mod_target`, …) or read from the client
  transaction; `AbilityTargetTemplate` filters/counts/player/zone logic
  (`m_PlayerFilter`, `m_CollectionFlags`, `m_CardFilter`, min/max counts,
  best-effort minimums) is not evaluated server-side.
- **Costs** — activation/charge/spell/life costs are read per-source
  (`card_abilities_meta.activation_cost`, `talent_abilities.charge_cost`,
  `spell_cost`); `m_VariableActivationCost`, `m_VariableActivationCostMinimum`,
  X-cost card targets, and `m_LifeCost`/`m_SpellPointCost` are not fully enforced.
- **Durations** — `m_EffectDuration` is captured for CardModifier but the
  teardown semantics (WhileCardInPlay, EndOfTurn, etc.) are not implemented.
- **Conditions** — `talent_abilities.condition` supports only the handful of
  pre-game specs in `conditions.py`; the full
  `AbilityEffectConditionTemplate`/`m_ConditionId`/`m_AbilityCondition`/
  `m_TriggerCondition` tree is not evaluated.
- **Options** — `m_AbilityOptions`/`AbilityOptionEntry` choices are not
  prompted/consumed.
- **Variables** — `m_Variables[]` literals are read for CardModifier/Replenish;
  the `AbilityVariable`/`EffectField`/`TargetField` value resolution is not
  generalised.
- **Output variables** — `m_OutputVariables` → `EffectOutputVariable` feedback
  is not implemented.
- **Limits** — `m_UsesPerTurn`/`m_UsesPerGame`/`m_Cooldown` are stored but not
  enforced as counters.
- **`m_SerializedTAC`** — only the Shift path in `tac.py` is decoded.

---

## 15. Warnings & gotchas

1. **Do not hardcode per-card logic.** Prefer fields in the record; treat
   `m_GameText` parsing as a fallback, never the source of truth, because it is
   a *localization key* whose runtime value is pulled from `localization.db`.
2. **Effect class names change.** `RandomizeVariableEffectTemplate` vs the stale
   `RandomizeVariableAbilityEffectTemplate` registered in `bom.py` is one
   example. Derive the dispatch key from the record's `_t` (last segment), not
   from memory.
3. **Enum values are strings** in the records (`"Instant"`, `"QuickAction"`,
   `"Self"`, `"UseDefault"`). Convert to ints only when mirroring client bit
   math (e.g. `ECardCollections` flags, `EAbilityCastingBehavior` values).
4. **Record JSON is near-JSON**: trailing commas before `}`/`]` and occasional
   malformed fragments. The extractors clean it (regex `,\s*([}\]])` → `\1`)
   before storing `raw_json`; do the same when parsing the raw files directly.
5. **GUIDs are lowercase.** Normalise (`g.lower()`) every GUID read from the
   records and DB keys.
6. **Zero-GUID means "unset"** (e.g. `m_ConditionId` = all-zero GUID → no
   condition; `m_*Target` cost fields → no card cost).
7. **Targets drive the client picker.** `card_abilities_meta.target_template_ids`
   must carry the ability's real target template GUIDs — `Invalid` yields a
   picker with zero valid targets (see HOWTO.md).
8. **The client is authoritative for UI but the server must be authoritative
   for legality.** The client runs no own cost/threshold check; it only trusts
   the `PlayerOptionList` we push and the four `Are*Complete` gates. A correct
   server mirrors those gates.
9. **`QuickCardAbility` field keys** (1…30) are a compact wire format, not the
   JSONL field order — when debugging client serialization, map via
   `QuickCardAbility.AddSerilization` (see §12).
10. **`m_AbilityEffectList` is the graph, not the final order.** Always apply
    the `EffectGroupId → EffectInstanceId` sort before execution.

---

## 16. Appendix A — per-EffectTemplate JSON field usage (complete)

Every distinct `_t` that appears in `AbilityEffectTemplate.jsonl`, mapped to
the exact `m_*` fields the C# class (and its base classes) read from the
record. All effect classes inherit these five from
`Reckoning.Game.AbilityEffectTemplate`: `m_TemplateId`, `m_Name`,
`m_EditorVariableMap`, `m_GameText`, `m_SerializedTAC`. They are omitted
from each row below; a row with "only base-class fields" uses exactly
those five (plus, where noted, the NumericModifier pair).

#### Effect templates (the 60 operations)

| `_t` class (last segment) | Records | Base | Fields used from the JSON record |
|---------------------------|--------:|------|--------------------------------|
| `CardModifierAbilityEffectTemplate` | 1558 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, Modifier, Name, SerializedTAC, TemplateId |
| `SummonTokenTroopAbilityEffectTemplate` | 1239 | `CardAbilityEffectTemplate` | Amount, AmountField, CardCollection, CardFilter, CardLocation, CardTemplateId, CombinedCost, CopyGems, EditorVariableMap, EntersPlayAttacking, EntersPlayExhausted, GameText, Name, SerializedTAC, TemplateId, Terminus |
| `ActivateAbilityEffectTemplate` | 911 | `CardAbilityEffectTemplate` | AbilityToInvoke, EditorVariableMap, GameText, Name, RandomlyLuckyOrUnlucky, SerializedTAC, TemplateId |
| `GrantAbilityEffectTemplate` | 562 | `AbilityEffectTemplate` | AbilityIsUnique, AllPaymentPowersOfSource, AllPaymentPowersOfTarget, AllRememberedPowers, AllSocketedPowersOfMyMaster, AllSocketedPowersOfSource, AllSocketedPowersOfTarget, EditorVariableMap, GameText, GrantedAbilityTemplateId, Name, RandomChampionChargePower, RandomInspirePower, SerializedTAC, TemplateId |
| `MoveCardToZoneEffectTemplate` | 446 | `CardAbilityEffectTemplate` | AbilityOpponentTakesControl, AbilityOwnerTakesControl, AllCardsOfTargetInZone, ArenaChampionTakesControl, ControlGivenToTargetIndex, DestinationCollection, DestinationLocation, EditorVariableMap, EntersPlayAttacking, EntersPlayExhausted, GameText, Name, PreviousControllerTakesControl, RandomLocation, SerializedTAC, TemplateId, TopHalfOfDeck |
| `TransformCardAbilityEffectTemplate` | 296 | `CardAbilityEffectTemplate` | CardTemplateId, EditorVariableMap, GameText, Name, Portal, SerializedTAC, TemplateId |
| `StoreTargetsAbilityEffectTemplate` | 102 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, Name, OnlyUntilEndOfTurn, SerializedTAC, SetTargets, TargetIndex, TemplateId |
| `DrawNCardsAbilityEffectTemplate` | 84 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, InputValue, Name, SerializedTAC, TemplateId |
| `RandomizeVariableEffectTemplate` | 75 | `SetAbilityVariableEffectEffectTemplate` | EditorVariableMap, GameText, MaxValue, MaxValueField, MinValue, Name, SecondValue, SerializedTAC, TemplateId, VariableName |
| `CreateTokenCopyAbilityEffectTemplate` | 69 | `CardAbilityEffectTemplate` | CardCollection, CardLocation, CopyGems, EditorVariableMap, GameText, InputValue, IsReplica, Name, SameOwner, SerializedTAC, TemplateId |
| `TransformCardAtRandomAbilityEffectTemplate` | 67 | `CardAbilityEffectTemplate` | CantBeSameCard, EditorVariableMap, Filter, GameText, Name, SerializedTAC, TemplateId |
| `TACAbilityEffectTemplate` | 66 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `BuryCardAbilityEffectTemplate` | 61 | `CardAbilityEffectTemplate` | Amount, EditorVariableMap, Filter, GameText, Name, SerializedTAC, TemplateId, TopHalfOfDeck |
| `Battle2CardsAbilityEffectTemplate` | 48 | `CardAbilityEffectTemplate` | EditorVariableMap, FightBack, GameText, Name, SerializedTAC, TemplateId |
| `PlayCardAbilityEffectTemplate` | 47 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, JustActivate, Name, SerializedTAC, TemplateId |
| `RevealCardsAbilityEffectTemplate` | 43 | `AbilityEffectTemplate` | EditorVariableMap, GameText, Name, PlayerRevealTargets, SerializedTAC, TemplateId |
| `UntapCardAbilityEffectTemplate` | 33 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `ConscriptAbilityEffectTemplate` | 23 | `CardAbilityEffectTemplate` | Amount, CardFilter, EditorVariableMap, Faction, GameText, Name, SerializedTAC, TemplateId |
| `ConversationAbilityEffectTemplate` | 23 | `CardAbilityEffectTemplate` | ConversationId, EditorVariableMap, GameText, Name, SerializedTAC, TemplateId |
| `RepeatingAbilityEffectTemplate` | 20 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, LoopCount, LoopCount_DEPRECATED, Name, RepeatingEffect, SerializedTAC, TemplateId |
| `ActivateTriggeredAbilityEffectTemplate` | 16 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, Keyword, Name, SerializedTAC, TemplateId |
| `CreateTokenMatchingTargetAbilityEffectTemplate` | 10 | `CardAbilityEffectTemplate` | CardCollection, CardFilter, CardLocation, EditorVariableMap, GameText, InputValue, Name, SerializedTAC, TemplateId |
| `SetCardIntegerVariableEffectTemplate` | 9 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, InputValue, Name, Operation, SerializedTAC, TemplateId, VariableName |
| `TunnelAbilityEffectTemplate` | 7 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `StoreListAttrAbilityEffectTemplate` | 5 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, IntAttrName, IntAttrValue, ListAttrName, Name, OnlyUntilEndOfTurn, SerializedTAC, Set, TemplateId |
| `FireEventEffectTemplate` | 4 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, Name, SerializedTAC, TemplateId, TriggerType |
| `CreateAndCastSpellAbilityEffectTemplate` | 3 | `CardAbilityEffectTemplate` | AmountField, EditorVariableMap, GameText, Name, SendPlayAction, SerializedTAC, TemplateId |
| `PutTopOfDeckIntoHandAbilityEffectTemplate` | 3 | `CardAbilityEffectTemplate` | AbilityOwnerTakesControl, Bloodwash, EditorVariableMap, GameText, InputValue, Name, SerializedTAC, TemplateId |
| `RememberKeywordPowersEffectTemplate` | 3 | `CardAbilityEffectTemplate` | AllPowers, EditorVariableMap, GameText, Keyword, Name, SerializedTAC, TemplateId |
| `StoreNameAbilityEffectTemplate` | 3 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, Name, OnlyUntilEndOfTurn, SerializedTAC, TargetIndex, TemplateId |
| `DestroyCardAbilityEffectTemplate` | 2 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `DoubleChoiceAbilityEffectTemplate` | 2 | `CardAbilityEffectTemplate` | Choices, EditorVariableMap, GameText, Name, NumOptions, SecondChoice, SerializedTAC, TemplateId |
| `LoadPlayerDeckAbilityEffectTemplate` | 2 | `CardAbilityEffectTemplate` | DeckTemplateId, EditorVariableMap, GameText, Name, SerializedTAC, TemplateId |
| `RegisterTriggerAbilityEffectTemplate` | 2 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, Name, SerializedTAC, TemplateId, TriggerAbilityTemplateId |
| `RevokeAbilityEffectTemplate` | 2 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, Name, RevokedAbilityTemplateId, SerializedTAC, TemplateId |
| `TransformSelfAbilityEffectTemplate` | 2 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, IsReplica, Name, PlantGarden, SerializedTAC, TemplateId |
| `VoidCardAbilityEffectTemplate` | 2 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `AnimationTriggerEffectTemplate` | 1 | `CardAbilityEffectTemplate` | AnimationTrigger, EditorVariableMap, GameText, Name, SerializedTAC, TemplateId |
| `BlockEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `BuiltInPlayCardAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `CopyAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | AmountField, EditorVariableMap, GameText, Name, SerializedTAC, TemplateId |
| `CounterSpellAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `DestroyCardByDefenseAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `DiscardCardAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `DiscardOrSacrificeCardAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `FinishMovingCardToWarzoneEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `FinishResolvingCardAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `GiveBonusTurnAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `LoseThresholdAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | EditorVariableMap, GameText, Name, SerializedTAC, TemplateId, Thresholds |
| `RemoveCardFromCombatAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `ReplenishResourcesAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `RevertPermanentModificationsAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `SacrificeCardAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `SwapHealthAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `TapCardAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `TransformCardIntoReplicaAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |
| `VerdictAbilityEffectTemplate` | 1 | `CardAbilityEffectTemplate` | only base-class fields: none |

#### Nested modifier types (appear in `m_Modifier`)

| `_t` class (last segment) | Records | Base | Fields used from the JSON record |
|---------------------------|--------:|------|--------------------------------|
| `DamageModifier` | 312 | `NumericModifier` | Amount_DEPRECATED, InputValue, MasterDealsDamage |
| `CounterModifier` | 210 | `NumericModifier` | Amount_DEPRECATED, CardCounterTemplateId, InputValue, Operation, RemoveAllCounters, RemoveHalfRoundedUp |
| `AttackModifier` | 194 | `NumericModifier` | Amount_DEPRECATED, InputValue, Property, ReplaceExistingValue |
| `DefenseModifier` | 170 | `NumericModifier` | Amount_DEPRECATED, InputValue, Property, ReplaceExistingValue |
| `IntAttrModifier` | 161 | `Modifier` | Attribute, Double, Operation, Value, ValueField |
| `HealHeroModifier` | 107 | `NumericModifier` | only base-class fields: none |
| `CardCostModifier` | 80 | `NumericModifier` | Amount_DEPRECATED, InputValue, ReplaceExistingValue |
| `ChargePointsModifier` | 47 | `NumericModifier` | Amount_DEPRECATED, InputValue, RemoveAllCharges |
| `LoseLifeModifier` | 45 | `NumericModifier` | Amount_DEPRECATED, InputValue, LoseHalfHealth |
| `AttributeModifier` | 42 | `Modifier` | AttributeFlags, Operation |
| `CurrentResourceModifier` | 40 | `NumericModifier` | Amount_DEPRECATED, InputValue, Set |
| `TotalResourceModifier` | 38 | `NumericModifier` | Amount_DEPRECATED, InputValue, Set |
| `ThresholdModifier` | 21 | `NumericModifier` | Amount_DEPRECATED, InputValue, Random, RandomLowestThreshold, ThresholdColor |
| `DamageImmunityModifier` | 18 | `Modifier` | CardFilter, IsCombatDamage |
| `CardThresholdModifier` | 17 | `NumericModifier` | Amount_DEPRECATED, CopySourceCard, InputValue, SetThresholds, Shard |
| `BlockImmunityModifier` | 11 | `Modifier` | CardFilter |
| `BlockImmunityExceptionModifier` | 8 | `Modifier` | CardFilter |
| `DamageMultiplierModifier` | 8 | `NumericModifier` | Amount_DEPRECATED, CombatDamageOnly, InputValue, NonCombatDamageOnly, ReplaceExistingValue |
| `DamageShieldModifier` | 7 | `NumericModifier` | Amount_DEPRECATED, DamageDealerAdditionalTarget, InputValue, LastsIndefinitely, OneShot, OnlyCombatDamage, OnlyPreventFromDamageDealer |
| `SetHeroHealthModifier` | 7 | `NumericModifier` | only base-class fields: none |
| `SpellPointsModifier` | 7 | `NumericModifier` | only base-class fields: none |
| `SubTypeModifier` | 4 | `Modifier` | Operation, Subtype |
| `TargetingImmunityModifier` | 2 | `Modifier` | CardFilter |
| `AttackImmunityModifier` | 1 | `Modifier` | CardFilter |
| `BlockRestrictionModifier` | 1 | `Modifier` | CardFilter |

#### Nested card-filter types (`m_CardFilter`, `m_Filter`, target/condition filters)

| `_t` class (last segment) | Records | Base | Fields used from the JSON record |
|---------------------------|--------:|------|--------------------------------|
| `AndCardFilter` | 315 | `CardFilter` | TargetFilters |
| `IsTroop` | 164 | `StaticCardFilter` | only base-class fields: none |
| `IsType` | 100 | `StaticCardFilter` | CardType |
| `IsSubType` | 99 | `StaticCardFilter` | SubType |
| `HasResourceCost` | 87 | `CardFilter` | AddAttack, AddCardIntegerVariable, AddDefense, AddSumListAttrName, AddSumProperty, AddVariable, AddX, ComparisonOp, ResourceCost, ResourceCostCardFilter |
| `TACFilter` | 60 | `CardFilter, ITriggerCondition` | SerializedTAC |
| `IsColor` | 52 | `StaticCardFilter` | ColorFlags, IncludeResources, Prismatic |
| `HasSourceCastingCostFilter` | 39 | `CardFilter` | AddValue, CastingCost, ComparisonOp, UseSource |
| `HasASharedShardWithSourceFilter` | 37 | `CardFilter` | ExactMatch, StoredShard |
| `OrCardFilter` | 37 | `CardFilter` | TargetFilters |
| `InFaction` | 32 | `StaticCardFilter` | Faction |
| `IsArtifact` | 27 | `StaticCardFilter` | only base-class fields: none |
| `IntAttrFilter` | 18 | `CardFilter, ITriggerCondition` | Attribute, CompareToCost, ComparisonOp, Value |
| `NotCardFilter` | 18 | `CardFilter` | TargetFilter |
| `NameContainsFilter` | 15 | `StaticCardFilter` | ContainsString, IncludeKeywords, IncludeSubType, LanguageCode |
| `IsRarity` | 9 | `StaticCardFilter` | Rarity |
| `HasAttackValue` | 8 | `CardFilter` | AttackValue, CompareToAbilitySource, CompareToCardIntegerVariable, CompareToSourceControlledCardFilterCount, CompareToStoredTarget, CompareToTriggerSource, ComparisonOp |
| `HasDefenseValue` | 5 | `CardFilter` | CompareToAbilitySource, ComparisonOp, DefenseValue |
| `IsSocketable` | 5 | `StaticCardFilter` | ComparisonOp, SocketValue |
| `IsResource` | 4 | `StaticCardFilter` | IsBasicResource, IsNonStandardResource, ThresholdColorFlags |
| `CompareCastingCostToSourceCountersFilter` | 3 | `CardFilter` | ComparisonOp, CounterType |
| `HasAllAttributeFlags` | 3 | `StaticCardFilter` | CardAttributeFlags |
| `BlockingFilter` | 2 | `CardFilter` | Filter |
| `HasASharedClassWithSourceChampionFilter` | 2 | `CardFilter` | only base-class fields: none |
| `HasASharedSubtypeWithSourceFilter` | 2 | `CardFilter` | only base-class fields: none |
| `IsAbilitySource` | 2 | `CardFilter` | only base-class fields: none |
| `IsBlocking` | 2 | `CardFilter` | only base-class fields: none |
| `AnyCard` | 1 | `StaticCardFilter` | only base-class fields: none |
| `CompareAttackAndDefenseFilter` | 1 | `CardFilter` | CompareToAbilitySourceAttack, CompareToAbilitySourceDefense, ComparisonOp |
| `HasASharedRarityWithSourceFilter` | 1 | `CardFilter` | only base-class fields: none |
| `HasSourceTypeFilter` | 1 | `CardFilter` | DontExactlyMatchOriginal |
| `IsControlledBy` | 1 | `CardFilter` | TestAgainstActivePlayer |
| `IsNotControlledBy` | 1 | `CardFilter` | FailUncontrolledCards |

#### Nested value fields (`m_AmountField`, `m_InputValue`, `m_LoopCount`, `m_CombinedCost`, …)

| `_t` class (last segment) | Records | Base | Fields used from the JSON record |
|---------------------------|--------:|------|--------------------------------|
| `EffectInputVariable` | 2761 | `EffectField` | InputVariableName |
| `EffectConstant` | 100 | `EffectField` | Value |
| `EffectAbilityVariable` | 1 | `EffectField` | AbilityVariableName |

### How the nested types plug into the effect records

| Slot on an effect record | Filled by |
|--------------------------|-----------|
| `m_Modifier` | one of the **modifier** types above (CardModifier only) |
| `m_CardFilter` / `m_Filter` | a **card-filter** type above, or a filter tree (`AndCardFilter`/`OrCardFilter`/`NotCardFilter` with `m_TargetFilters`/`m_TargetFilter`) |
| `m_AmountField`, `m_InputValue`, `m_CombinedCost`, `m_LoopCount`, `m_MaxValueField` | a **value field** (`EffectConstant` / `EffectInputVariable`) |
| `m_RepeatingEffect` | a nested effect record (Repeating only) |
| `m_SerializedTAC` | a TAC binary blob decoded by `abilities/framework/tac.py` |

The counts are how often each `_t` appears in `AbilityEffectTemplate.jsonl`
(top-level or nested). The full-qualified `_t` values live under the
`Game.Shared.Mechanics` namespaces `…Abilities`, `…Modifiers`, and
`…Cards.Filters`.

---

## 17. Worked example

`AbilityTemplate.jsonl` "Play Card Ability"
(`m_AbilityTemplateId 5a8783b0-…`):

1. `m_AbilityEffectList` = two mappings:
   - instance 0 → `m_EffectTemplateId 1909f054-…`
     (`BuiltInPlayCardAbilityEffectTemplate`, the built-in "play card" op),
     `m_TargetTemplateIndex 0`, `m_EffectGroupId 0`, `m_EffectDuration "Instant"`,
     `m_ContingentEffectInstanceId -1`, `m_ConditionId` zero, `m_IsOptional 0`.
   - instance 1 → `m_EffectTemplateId 0e40bc0b-…`, `m_EffectGroupId 1`.
2. `m_AbilityTargetTemplateIds` = `[459eebb2-…]` (index 0 → the NoOp target).
3. `m_AbilityTargetTemplateIds[0]` resolves in `AbilityTargetTemplate.jsonl` to
   `e6a705e4-…` (`AbilityTargetTemplate` v12): `m_IsAutoTarget 1`,
   `m_MinTargetCount`/`m_MaxTargetCount` = `TargetConstant {m_Value:1}`,
   `m_PlayerFilter "Self"`, `m_CollectionFlags "None"`, `m_IsRandomTarget 0`,
   `m_Optional 0`, `m_Explicit 0`.
4. Costs: `m_ActivationCost 0`, `m_ChargePointCost 0`, `m_SpellPointCost 0`,
   `m_LifeCost 0`, `m_VariableActivationCost 0`; all `m_*Target` cost fields are
   zero-GUID (no card costs); `m_CastingBehavior "QuickAction"`, `m_Manual 1`.
5. Resolution plan: one ability, two effects in group order
   (group 0 then group 1), both auto-targeted with no conditions, no costs,
   no options, no variables → the activation is always complete.

`AbilityEffectConditionTemplate.jsonl` "YouHave100OrMoreCardsInYourDeck":
`m_Condition` is `RequiresCardsControlled` with an `AndCardFilter`
(`IsType` any card + `IsControlledBy` not-against-active + `InZone Deck`),
`m_CardCollection "Deck|Hand|Champions|…"`, `m_RequiredQuantity 100`,
`m_ComparisonOp "GreaterThanOrEqual"`, `m_PlayerFilter "Self"`. A server
implementation of this condition counts the source player's deck cards and
compares ≥ 100.
