# Battle rules coverage and acceptance evidence

This is a semantic coverage matrix, not a count of registered Python handlers.
`rules_port.coverage` inventories class names; registration does not establish
that every field or interaction of a class has been ported. The complete battle
port remains unfinished. Rows without a focused acceptance result are not
certified by the damage tests below.

C# paths below are relative to
`HexClient/Assembly-CSharp-firstpass/Game/Shared/`.

| System | C# entry points | Server owners | Authored input | Evidence / remaining acceptance |
| --- | --- | --- | --- | --- |
| Turns, stops, priority, reconnect | `Mechanics/TurnPhaseState.cs`, `Mechanics/GameActions/PriorityWindowAction.cs`, `Mechanics/EndTurnState.cs` | `session.py`, `phases.py`, `turn_states.py`, `lifecycle.py`, `pvp_lifecycle.py`, `adapter.py` in `rules_port` | Phase stops and saved action/chain descriptors | Existing implementation; complete reconnect and two-client priority traces still required. Champion shield expiry covered in both checkpoint formats. `ReadyState` armor reset and the `CantReady`/`CantReadyNormally`/`CantReadyNormallyHidden` IntAttr gates apply in `ready_cards_for_turn`. |
| Timing, resource plays, costs, thresholds | `Mechanics/Transactions/PlayTroopTransaction.cs`, `PlayResourceTransaction.cs`, `ActivateAbilityTransaction.cs` | `rules_port/transactions.py`, `card_transactions.py`, `resources.py`, `costs.py`, `static_rules.py` | Ability costs, casting behavior, thresholds, target maps | Existing validation; exhaustive invalid-intent and multi-part payment rollback parity not certified here. Free plays require `OwnerCanPlayForFree` or the controller's champion `CanPlayCardsForFree` and project a zero effective cost. `Mobilize` reduces the payment by two per ready troop tapped (capped by the card's value, validated against the client's `CardsToMobilize`), and `CanIgnoreCardsThresholds` bypasses threshold checks; `ChargePointCostModifier` remains open. |
| Chain and choices | `Mechanics/GameActions/PriorityWindowAction.cs`, `ResolveTopOfChainAction.cs`, `WaitForTriggeredAbilitiesAction.cs` | `rules_port/kernel.py`, `chain.py`, `resolution.py`, `async_bridge.py` | Effect group/instance order, continuation and chain identity | Existing continuation machinery; nested choices and reconnect through every checkpoint need acceptance traces. |
| Combat declarations and assignment | `Mechanics/Combat.cs`, `CombatResolver.cs`, `Card.cs:CalculateTotalCombatDamageToDeal`, `Card.cs:CurrentAttackValue` | `rules_port/combat.py`, `combat_rules.py`, `combat_damage.py`, `static_rules.py` | Keywords, blocker order, multiplier rules, Gladiator role bonus | Shared damage callback now reports absorption including prevention; minimum-to-kill is applied after shields. `Gladiator` projects to attack while the controller is active and to defense while defending, with `GladiatorBoth` applying both. Moving a troop to the warzone applies an opposing champion's `OpposingTroopsEnterPlayExhausted`/`OpposingNonArdentTroopsEnterPlayExhausted` flags through the shared played/token/effect entry paths. Full keyword/simultaneous parity remains open. |
| Damage and prevention | `Session.cs:DamageCard`, `DamageChampion`, `Mechanics/DamageShield.cs`, `Card.cs:ResetArmor` | `rules_port/damage_effects.py`, `context.py`, `lifecycle.py` | DamageShield, DamageMultiplier, DamageImmunity modifier fields, Armor/ArmorUsed, additive received modifiers | Focused ordering/accounting tests below. Armor consumption is ordered after shields, `ArmorUsed` persists in the permanent store and resets for every card at Ready. Additive `DamageReceivedModifier`/`*ReceivedModifier`, source `PreventMy*` and noncombat `DamageChampionMultiplier` follow `Session.DamageCard`/`DamageChampion`. Additive `PreventDamageFromCardsThatHaveOwnersThreshold`, the champion `NonCombatDamageReduction`/`OpposingNonCombatDamageReduction` adjustments and the `ChanceToPreventCombatDamage`/`ChanceToPreventNonCombatDamage` roll (same RNG consumption point, including a stored zero) also follow `Session.DamageCard`. |
| Statics and targeting | `Mechanics/Modifiers/DamageMultiplierModifier.cs`, `DamageImmunityModifier.cs` | `rules_port/static_rules.py`, `targeting.py`, `filters.py` | Target templates, conditions, typed modifier operands | Continuous rule modifiers now check their authored target filter. Numeric multipliers preserve multiplication, replacement, separate speed scopes, and zero. Synthetic champion continuous-rule projection remains incomplete. |
| Counters, deaths, zones | `Mechanics/Card.cs`, `Session.cs`, `Mechanics/AbilityManager.cs` | `rules_port/death_effects.py`, `context.py`, `runtime_helpers.py`, `lifecycle.py`, `triggers.py`; `pvp_db.py` projection | Counters, durations, zone collections, Deathcry gates, `Session.GetBuryBonusForPlayer`, end-of-turn damage reset | End-of-turn, end-of-next-turn, owner/opponent turn and after-ready duration boundaries follow C#; burying adds other champions' `BuryBonus`; an opponent's `OpposingDeathcriesCantTrigger` blocks Deathcry and `CantHealAtEndOfTurn` cards keep damage through the reset. `Session.Lifebound` returns the active player's marked discard cards at StartTurn. Noncombat death timing and damage-bound teardown still need parity work. |
| Trigger discovery | `Mechanics/AbilityManager.cs`, `Mechanics/Triggers/Conditions/` | `rules_port/triggers.py`, `trigger_discovery.py`, `conditions.py` | Event source/target, conditions, collections | Damage now publishes prevention and target-damaged events with payloads. Entering play publishes `CardEnteredZoneEvent` -> `AsEntersPlayEvent` -> `CardInspiredEvent` (self enters-play abilities and Inspire), and `None` collection flags are unrestricted like C#. `HandleGameRulesTriggers` equivalents run after authored listeners: Rage on attack and Momentum for the caster's warzone troops on a resource cast (+1/+1 until the owner's next StartTurn). Focused fixtures cover the enters-play chain; not every downstream authored trigger has an acceptance scenario. |
| Ability interpreter and champion powers | `Mechanics/Abilities/`, `Mechanics/Transactions/ActivateAbilityTransaction.cs` | `rules_port/resolution.py`, `context.py`, `abilities.py`, `card_transactions.py`, `ability_usage.py` | Variables, effect groups, costs, uses, cooldowns | Repeating children (including the deprecated loop-count fallback), current Records variable subclasses, use limits, cooldowns and key duration boundaries have focused coverage. Verdict builds the authored good/bad choice tokens and activates the built-in choose/play ability. Full option, statistic and champion-modifier parity remains uncertified. |
| Client projection | `Session.cs`; `HexClient/Assembly-CSharp/UIBattle.cs:OnChampionHealthChanged`, `OnCardUpdated` | `game_engine.py`, `rules_port/adapter.py`, HConnect and mode adapters | Valid SessionCardIds, event order, visible legal options | Existing wire events retained. No new real-client or live PvP acceptance performed. |

## Target filter and selection coverage

`Records/AbilityTargetTemplate.jsonl` contains 1,773 target templates across 15
target classes. Their nested `CardFilter` trees contain 54 predicate classes
and 6,764 predicate nodes in the current extracted snapshot, plus three
`EffectInputVariable` scalar operands. The shared Records adapter in
`rules_port/filters.py` compiles each authored predicate to a concrete filter
instead of the permissive empty-filter fallback. `rules_port/targeting.py`
owns candidate enumeration, selection validation, player/zone rules,
specialized target classes, and client target-count bounds.
`gamedata/models.py:TargetSpec` carries auto, random, optional, explicit,
best-effort-minimum and variable-count metadata into the interpreter.

The C# contract is split between `AbilityTargetTemplate.GetAllTargets` /
`IsTargetValid`, the derived target-template overrides, and
`AbilityInstance.ValidateMinimumTargetCount`. Enumeration and submitted
selection validation are deliberately distinct: for example, base `None`
collection flags enumerate no candidates but direct validation is unrestricted;
`SharedNameTargetTemplate` and `DuplicateCardTargetTemplate` also override
parts of the base validation contract. Those differences are covered by
`tests/tests_targeting_parity.py` rather than hidden behind one universal
predicate.

The focused target fixture covers the shared evaluator with Practice owners
`profile/AI` and PvP owners `51/52`, nested And/Not conditions, AddX plus source
integer variables, comparisons, TopN, subtype matching, blockers, champions,
None-mask semantics, duplicate collection scope, the SharedName override, and
an `EffectInputVariable` cost comparison. The native resolver exposes the
active ability's authored defaults plus activation values while automatic
targets are enumerated, then restores the enclosing resolver's variable scope.
The catalog test compiles every predicate nested in all 1,773 target
templates. These are server acceptance paths for both owner models, not a live
client picker or HUD certification.

## Authored trigger producer and publication trace

Records currently names **45** trigger event types (the previous 44 count
omitted `CardDestroyedEvent`). The C# producer is shown at left; the Python
publisher shown at right feeds the shared RulesPort trigger dispatcher.
Dynamic `FireEventEffectTemplate` events use the authored `m_EventType`, so
those C# rows are not emitted with a `new EventType()` expression.

| Authored event | C# mutation / enqueue point | Python publication boundary |
| --- | --- | --- |
| `AsEntersPlayEvent` | `Session.cs:3827` after zone entry | `rules_port/triggers.py:dispatch_native_trigger` derives it after `CardEnteredZoneEvent` |
| `CardActivatedEvent` | `AuthoritativeSessionBase.cs:3413` | `rules_port/triggers.py:dispatch_card_activated`; HConnect and PvP activation adapters |
| `CardAttackedEvent` | `Session.cs:6501` | HConnect attack resolver and `services/tournament_game.py` PvP combat resolver |
| `CardAttackedOrBlockedEvent` | `Session.cs:6506, 6705` | HConnect/PvP combat adapters; `rules_port.effect_lifetimes` observes these boundaries |
| `CardBattledEvent` | `Mechanics/Abilities/BattleAbilityEffectTemplate.cs:25,33`; `Battle2CardsAbilityEffectTemplate.cs:94,104` | `rules_port/battle_effects.py` |
| `CardBlockedEvent` | `Session.cs:6698` | HConnect/PvP blocker resolution |
| `CardCastEvent` | `AuthoritativeSessionBase.cs:3251,3382` | `rules_port/chain_items.py:dispatch_card_cast` |
| `CardCreatedEvent` | `AuthoritativeSessionBase.cs:3593,3814` | `rules_port/token_effects.py:_publish_created_card` |
| `CardDealtDamageEvent` | `Session.cs:7184,7385` | `rules_port/damage_effects.py` |
| `CardDestroyedEvent` | `Session.cs:2712` | `rules_port/death_effects.py` |
| `CardDiscardedEvent` | `Session.cs:3003` | `rules_port/context.py`, `rules_port/host_mutations.py`, HConnect/PvP discard adapters |
| `CardDrawnEvent` | `AuthoritativeSessionBase.cs:3225` | `rules_port/draw_effects.py`; HConnect/PvP draw adapters |
| `CardEnteredZoneEvent` | `Session.cs:3920` | `rules_port/context.py`, `death_effects.py`, `token_effects.py`, and mode card-transition adapters |
| `CardExitedZoneEvent` | `Session.cs:3910` | `rules_port/death_effects.py`, `context.py`, `chain_items.py` |
| `CardGainedIntAttrEvent` | `Mechanics/Card.cs:681-687`, when current value becomes positive from zero | `EffectContext.emit_int_attribute_gained`; typed int-attribute and Inspire mutations in `rules_port/effects.py` |
| `CardInspiredEvent` | `Session.cs:2898` | `rules_port/triggers.py` after authored Inspire and valid-target checks |
| `CardReadiedEvent` | `Session.cs:4019` | `rules_port/context.py:update_card_state` |
| `CardSacrificedEvent` | `Session.cs:3632` | `rules_port/death_effects.py` |
| `CardScroungedEvent` | `Session.cs:1198`, via `FireEvent` | HConnect cost mutation; RulesPort scrounge event publication |
| `CardTappedEvent` | `Session.cs:3972` | `rules_port/effects.py`; HConnect/PvP card-state adapters |
| `CardTransformedEvent` | `Session.cs:5428,5486` | `rules_port/transform_effects.py` |
| `CardTransformsEvent` | `Session.cs:5392,5442` | `rules_port/transform_effects.py` |
| `CardWouldBeDamagedEvent` | `Session.cs:7106,7324` | `rules_port/damage_effects.py` |
| `CardWouldBeDrawnEvent` | `AuthoritativeSessionBase.cs:3144` | `rules_port/draw_effects.py`; HConnect/PvP draw adapters |
| `CardWouldDealDamageEvent` | `Session.cs:7087,7305` | `rules_port/damage_effects.py` |
| `CardWouldEnterZoneEvent` | `AuthoritativeSessionBase.cs:3047,3757` | `rules_port/death_effects.py`, `draw_effects.py`; HConnect/PvP zone adapters |
| `CardsAttackedEvent` | `Mechanics/DeclareAttackState.cs:69` | HConnect/PvP attack declaration adapters |
| `ChampionHealedEvent` | `Session.cs:7434` | `EffectContext.emit_champion_healed` in `rules_port/context.py` |
| `ChampionWouldLoseEvent` | `Session.cs:2807` | `rules_port/death_effects.py`; PvP lethal-state adapter |
| `CombatEndedEvent` | `Mechanics/FirstStrikePriorityWindowState.cs:37`; `AssignDamageState.cs:27` | Native phase-exit callback in `rules_port/turn_states.py`, HConnect and PvP adapters |
| `ConscriptEvent` | `Mechanics/Abilities/ConscriptAbilityEffectTemplate.cs:129` | `rules_port/token_effects.py` after generated cards and copied thresholds |
| `CounterAddedToCardEvent` | `AuthoritativeSessionBase.cs:4849` | `EffectContext` counter mutation in `rules_port/context.py` |
| `FateweavedEvent` | `Mechanics/Abilities/FireEventEffectTemplate.cs:29`, Records `m_EventType` | `EffectContext.fire_event` -> `rules_port.triggers.dispatch_trigger` |
| `GainChargeEvent` | `Session.cs:4556` | HConnect/PvP resource mutation adapters -> `dispatch_native_trigger` |
| `GainThresholdEvent` | `Session.cs:4293` | HConnect/PvP threshold mutation adapters -> `dispatch_native_trigger` |
| `GameStartedEvent` | `Mechanics/StartTurnState.cs:56` | HConnect/PvP setup adapters -> `dispatch_native_trigger` |
| `HiddenCardEnteredZoneEvent` | `Session.cs:3928` | `rules_port/chain_items.py` and HConnect hidden-zone adapter |
| `IlluminatedEvent` | `Mechanics/Abilities/FireEventEffectTemplate.cs:29`, Records `m_EventType` | `EffectContext.fire_event` -> `rules_port.triggers.dispatch_trigger` |
| `OtherCardCreatedEvent` | `AuthoritativeSessionBase.cs:395,655` | `rules_port/token_effects.py:_publish_created_card` |
| `PowerShiftedEvent` | `Session.cs:5606` | `rules_port/ability_mutations.py`; HConnect/PvP projection adapters |
| `PreGameEvent` | `Mechanics/PreGameState.cs:30` | HConnect/PvP setup adapters -> `dispatch_native_trigger` |
| `TurnEndedEvent` | `Mechanics/EndPhaseState.cs:19` | Native EndPhase entry adapter in HConnect/PvP |
| `TurnPhaseEvent` | `Mechanics/TurnPhaseState.cs:61` | Native phase-entry callback in HConnect/PvP |
| `TurnStartedEvent` | `Mechanics/StartTurnState.cs:63` | Native start-turn lifecycle in HConnect/PvP |
| `VerdictEvent` | `Mechanics/Abilities/FireEventEffectTemplate.cs:29`, Records `m_EventType` | `EffectContext.fire_event` -> `rules_port.triggers.dispatch_trigger` |

For each publication, `dispatch_trigger` or `dispatch_native_trigger` enters
`NativeTriggerBackend`; `RecordsTriggerDiscovery` resolves candidates on both
participants where the client event rules require it, evaluates authored
trigger conditions and collection flags, then `rules_port/triggers.py` orders
and queues eligible abilities through the shared chain lifecycle. Effect
events preserve the C# source/target envelope; phase events use the active
participant as source and no card target.

The shared chain completion tail now follows the C# order.
`Session.ResolveTopOfChain` applies the ability first, then dispatches
`TopOfChainResolved`; removing the instance dispatches `RemovedTopOfChain`
and, when empty, `ChainEmpty`
(`HexClient/Assembly-CSharp-firstpass/Game/Shared/Session.cs:2046-2100`,
`Mechanics/AbilityManager.cs:495-515`). `resolve_chain_item()` therefore emits
effect and zone mutation events before the completion/pop events. HConnect
drains the session `GameEngineEventSink` into the packet before wrapper 3055
serialization; PvP drains the same sink and `_pvp_send_same_events` projects
the objective event sequence to both UIDs. `UIBattle` consumes
`AbilityPushedOnChain`, `TopOfChainResolved`, and `RemovedTopOfChain` in
`OnAbilityPushedOnChain`, `OnTopOfChainResolved`, and `OnRemovedTopOfChain`
(`UIBattle.cs:4396-4400, 5621-5665, 5753-5758, 5778-5784`).

Focused tests cover newly added int-attribute, Conscript and phase-exit
publications and assert card mutation events precede chain completion for
Practice/PvE and PvP owner models. They do not simulate the 45 distinct native
mutation producers or certify live 3055/HUD delivery for every event and mode;
those client traces remain open.

## Damage rule observations and implementation

`Session.DamageCard` rejects immunity before spending consumable shields. It
applies outgoing noncombat multipliers and received multipliers before shields,
then armor, then `CardWouldDealDamageEvent` and `CardWouldBeDamagedEvent`
replacement checks. Combat outgoing multipliers are applied before blocker
assignment by `Card.CalculateTotalCombatDamageToDeal`, so damage application
must not apply them a second time.

`DamageMultiplierModifier.Apply` multiplies the existing value, or replaces it
when `m_ReplaceExistingValue` is set. General, combat-only and noncombat-only
multipliers occupy separate attributes; a value of zero stays zero. A multiplier
on an unrelated friendly troop is not a controller-wide multiplier.

`DamageShield.Apply` checks combat/source restrictions before consumption.
A one-shot shield loses its unused capacity after an eligible hit.
`Card.ClearEndOfTurnDamageShields` retains only `LastsIndefinitely` shields.
Champion shields use the existing serialized session checkpoint because
champions have no `game_cards` row; ordinary shields use the existing mutation
columns through `pvp_db` APIs. Shield grants, consumption and champion expiry
share a reentrant lock. Damage outcome objects are local to each call.

`Session.DamageCard` clamps minimum-to-kill after prevention and returns damage
dealt plus shield/armor prevention through its out parameter. `CombatResolver`
subtracts that value before assigning to another blocker. SpiritDrain heals the
actual damage, including noncombat damage. The server now uses `DamageOutcome`
for that accounting and emits `DamagePreventedEvent`, then replacement checks,
then healing/mutation/projection, then dealt/received trigger events.

### Metadata evidence

Current `Records/AbilityEffectTemplate.jsonl` contains 8 DamageMultiplier,
7 DamageShield and 18 DamageImmunity records. Representative structured fields:

- `abb48fd7-7cc7-5d2f-b786-0913df77310c`: constant multiplier 2, replacement
  false, combat-only false, noncombat-only false.
- `65ea8b5a-bbc5-cf3f-b70e-3b323593d984`: shield capacity 2147483647,
  one-shot true, indefinite true, source restriction false.
- `bc39b916-1134-f59f-15cb-9de6701ea3d8`: immunity with an `IsSubType`
  filter and combat-only true. C# ignores a missing filter; it does not make
  the target immune to everything.

No card-name branches or card-text inference were added.

### Representative path and acceptance

Effect damage follows classified activation -> authoritative transaction
requirements -> native chain/ability resolution -> CardModifier damage ->
`EffectContext.damage` -> `damage_effects.deal_damage` -> `pvp_db` or champion
checkpoint mutation -> card/health wire event -> `UIBattle` handler.
Combat follows declarations/priority -> `combat_damage.resolve` ->
`CombatResolver` -> the same damage function -> state-based death pass.

`tests/tests_damage_rules.py` exercises six shared scenarios:

1. Four damage doubled, then shielded by three, deals/heals five; verify
   persisted damage and prevention/replacement/mutation/trigger ordering.
2. Immunity leaves shield capacity untouched; a replacement sees damage after
   shields and prevents mutation/healing.
3. Champion shields survive JSON restoration, obey combat restrictions and
   one-shot consumption, and expire through both PvE and PvP turn transitions.
4. Multiplier stacking, scope separation, replacement and zero values.
5. Six combat damage against two defense plus a three-point shield absorbs
   five and heals two; a fully shielded blocker consumes damage before the next
   blocker is considered.
6. A continuous rule affects only the authored target set.

These are isolated server tests using real SQLite mutation helpers and a stubbed
stat projection/trigger recorder. They do not certify transaction ingress,
encoded packets, trigger-chain completion, Unity animations or HUD state.
The available client log was read before editing; it contains no reproduction
of these new acceptance scenarios.

Validation: all six focused damage scenarios passed; the existing
`tests/tests_combat.py` regression file passed; protocol goldens passed 34/34.
Changed Python modules passed `py_compile`, and `git diff --check` was clean.
Test initialization used disposable metadata databases, never live
`hconnect.db`. No HConnect process was found for a SIGUSR1 reload. These are
reloadable module changes; no schema migration or full restart is required
for an already-running host.

## Ability metadata observations and implementation

The client applies `RepeatingAbilityEffectTemplate` by reading `m_LoopCount`
and calling the nested effect's whole-instance `Apply(effectInstance)` in a
loop. Its per-card overload is an error path. The RulesPort now dispatches the
nested typed effect for each loop while retaining the authored target set and
effect instance state. Acceptance: two targets with loop count three execute
in client order `[target 1, target 2]` three times.

`Card.PayPerTurnCosts` and `PayPerGameCosts` increment only when their authored
limit is positive; `PayCooldownCosts` sets the authored cooldown; and
`GetUsesPerGameCounts` projects remaining uses only for a used, still-available
limit. Server counters now follow those distinctions, and synthetic champion
powers use the same authored-limit rule in the checkpoint. Cooldowns remain a
separate counter. Acceptance covers independent use-per-game/use-per-turn
counters and one owner-turn cooldown decrement.

Records-driven variable evaluation now distinguishes `MultiplePlayers` from
`MultipleOpponents`, evaluates count, sum, highest-card, counter and list
variables, applies authored list/card filters, and carries `IDamage.Damage`
through queued trigger items. The shipped 457 expression-variable records use
arithmetic syntax supported by the evaluator. `m_DontRecalculate` caches
expression results on the active ability instance, including the metadata
default on invalid expressions.

Shared TAC statistic writers now cover card and champion-card player scopes,
including play counts, charge points spent/gained, health/resource changes,
starting deck size and Inspire count. C# `Card.AddToStat` stores
`PlayerStatsThisTurn` and `PlayerGameStats` on the champion card; the server
uses that same card-keyed scope in PvE and PvP. Entering-play Inspire is counted
after authored trigger conditions and valid-target checks, once per qualifying
ability and source, and `INS` reads the entering source card's
`CardStatsWithSpecificDuration.InspireCount`.

The C# `ESC` identifier reads `SourceCard.EscalationCount`, which starts at one;
the `Escalate` TAC operation increments each selected card and marks it for a
generic update. The server persists that count with the card's existing
permanent mutation payload, uses it for both typed expressions and the
compatibility text fallback, and sends the count with `CardUpdated`. It no
longer treats a RulesPort `ESC` read as an owner-wide cast counter. The
Chronic Madness fixture starts with two same-name cards in separate zones:
the first resolution buries four cards and escalates both instances, and the
second buries eight before escalating them again.

The Records sweep found 22 variable subclass types in current
`AbilityTemplate` records. All present subclasses have an evaluator branch.
The focused fixtures cover key representative paths, but do not exercise every
field combination or every variable subtype; this is implementation coverage,
not exhaustive card-by-card certification. Player/card statistics and
Inspire have dedicated fixtures, with live mode projection still requiring
client traces.

Current Records audits report zero printed-text findings and zero meaningful
unread `m_*` fields across the 57 effect classes. `RepeatingAbilityEffectTemplate`
is resolver-owned rather than a normal leaf. These snapshot audits do not prove
that every trigger event is published at the correct C# mutation point. Records
names 45 trigger event types; see the producer/publication matrix above.
Complete producer-to-resolution client acceptance across those types and both
modes remains open.

These observations follow `CardCountAbilityVariable.cs`,
`CardSumAbilityVariable.cs`, `HighestCardAbilityVariable.cs`,
`CountListAttrAbilityVariable.cs`, `ExpressionAbilityVariable.cs`,
`IntAttrAbilityVariable.cs`, `AbilityInstance.cs`, `Card.cs`, `Session.cs`,
`AbilityManager.cs`, and `AuthoritativeSessionBase.cs` in the client
disassembly, plus typed `Records/AbilityTemplate.jsonl` and
`Records/AbilityEffectTemplate.jsonl` fields. No card text was used to infer
these behaviors.

## Remaining work

The scope of the complete original-client port is not complete. In addition to
the uncertified rows above, the inspected damage path still needs:

- Champion-targeted continuous `CardModifier` leaves that are not IntAttrs
  (champion auras granting `DamageMultiplier`/`DamageImmunity`/stat rules)
  are not folded into a synthetic champion context; only champion runtime
  IntAttrs and the four champion flag IntAttrs are projected. No current
  Records card exercises the missing shapes.
- Total ordering when shields span permanent and temporary stores (the
  current persistence splits those lists; C# keeps one add-ordered list).
- Full replacement pause/resume and UntilDamaged duration removal. The fixture
  validates replacement dispatch ordering, not the entire continuation graph.
- Consistent state-based death timing across effect groups and simultaneous
  damage, with real trigger and zone-event acceptance scenarios.

Effect-field audit follow-ups (`AssetExtraction/audit_effect_leaves.py`
reports no meaningful unread effect-template fields for the current snapshot;
these are the remaining projection limits):

- Current Records contain no `GrantAbility.m_AllSocketedPowersOfMyMaster = 1`
  and no `CreateTokenCopy.m_CopyGems = 1` instances. The master equipment link
  is not represented in the session, so those unused field variants are not
  certified for future content.
- Two `CreateAndCastSpell` records set `m_SendPlayAction = 1`; the client passes
  this value to `AuthoritativeSessionBase.CopyCardAndPutOnChain`, whose current
  implementation does not branch on it. The server therefore has no distinct
  action to mirror for this flag in the inspected client build.

`card_audit.py` reports zero unconsumed intattrs for the current Records
snapshot: every C# `Game/**` runtime IntAttr read now has a Python consumer,
including the champion permissions (`CantActivateAbilities`,
`CantUseChargePowers`, `OpposingCryptPowersCantBeUsed`,
`YouCanActivateYourChargePowersAsThoughTheyWereQuick`), charge and draw
adjustments (`ChargePointBonus`, `ChargePointCostModifier`,
`MaxCardsDrawablePerTurn`, `CantDrawCards`), `Prevent*`/`*DamageReduction`
damage adjustments, `CantRevert`, `RabidNotOneShot`, `Lifebound`,
`ReplenishResourcesEachTurn`, `VerdictChoice`/`VerdictAncient` and the
`CanSee*` visibility permissions. The only remaining audit entry is
`m_SendPlayAction`, whose C# consumer (`CopyCardAndPutOnChain`) does not
branch on it, so the no-op port is exact.

Use `docs/PRIVATE_SERVER_FEATURES.md` for the other known setup, draw, hand-limit,
metadata-interpreter and client acceptance gaps. A clean class inventory must
not clear those items.
