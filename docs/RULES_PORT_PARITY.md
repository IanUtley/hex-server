# C# and Python rules parity

This document describes the behavioral contract for the Python RulesPort and
records source evidence, semantic coverage, acceptance scenarios, and known
gaps. It is not a count of registered Python handlers: `rules_port.coverage`
inventories class names, but registration does not establish that every field
or interaction of a class has been ported. The complete battle port remains
unfinished. Each acceptance result applies only to the scenarios it names; the
damage scenarios below do not certify unrelated systems.

## What parity means

For the same initial game state, typed player inputs, and equivalent random
draws, parity means the Python server makes the same legal/illegal decisions
and produces the same gameplay result as the reference C# rules engine. The
comparison includes costs and counters, target candidates and validation,
effect/trigger order, zone and card state, prompts and resumptions, visibility,
and the ordered client event stream. Matching only the final board is
insufficient: different trigger timing or event order can change the next
legal action or leave the client in a different UI state. A matching seed is
not enough if the two RNG implementations or their draw order differ.

## Unity is part of the parity contract

The end-to-end path includes Unity's cached game state and UI state machine:

```text
Unity UI state and options
  -> typed 3029 PlayerTransaction
  -> Python classification, RulesPort decision, and persisted mutation
  -> ordered 3055 session events (or the required acknowledgement)
  -> Unity SessionEventArgs dispatch and UIBattle state transition
```

The server can resolve the gameplay rule correctly and still violate parity if
it encodes the wrong event fields, sends events in the wrong order or to the
wrong player, omits a required sync, or references a card Unity has not cached.
The checked-in `HexClient/Assembly-CSharp-firstpass/` disassembly describes
session, transaction, and event handling; `HexClient/Assembly-CSharp/UIBattle.cs`
describes the battle UI states that consume those events. The client is fixed,
so these expectations have to be met by the server's request handling and
projection.

For a reported client failure, correlate three observations before choosing a
fix: the Unity `output_log.txt` exception and `UIBattle|...|Pushing/Popping UI
state` lines, the matching server request/session log, and the exact ordered
3055 event list plus authoritative state delta. Follow each event through
`SessionEventArgs.BuildArgs` to its client handler. This separates a rules
decision defect from a wire/projection defect or a UI state transition that
never received its expected event.

The implementation does not need the same class layout. In C#, `Session`,
`Player`, `Card`, `AbilityInstance`, transactions, and the phase state machine
share a live object graph. Python separates those responsibilities across
`rules_port/`, the persisted battle checkpoint, SQLite card rows, and host
adapters that emit `Game` events. The adapter boundary is correct only when it
projects a decision already made by RulesPort; it must not add a second rules
decision or resolve the same input again.

The reference for this checkout is the `HexClient/` C# disassembly plus the
matching structured data in `Records/`. Use `RULES.md` for documented private
server decisions. The original C# project cannot currently be built from this
checkout, and `rules_port/parity.py` compares saved captures but cannot produce
a reference C# run. That means the current claim can be evidence-based parity
for inspected and accepted scenarios, not an unconditional proof that every
possible game state is identical. A runnable C# oracle or checked-in C# golden
captures are needed for automated differential proof.

## Where the implementations differ

| Concern | C# implementation | Python implementation | What must match |
| --- | --- | --- | --- |
| State and authority | `Session` mutates live `Card`/`Player` state and emits session events at rule boundaries. | `AuthoritativeSession` owns rule decisions; the host persists accepted mutations through SQLite and a namespaced battle snapshot. | One authoritative transition per request; the persisted result, not a client snapshot, determines the next action. Host projection must not change the ruling. |
| Authored and runtime data | `TemplateManager` provides typed templates while live `Card`/`AbilityInstance` objects hold mutable session state. | `DEFAULT_RECORD_STORE`/`AbilityGraph` provide authored Records and SQLite `card_templates`/`game_cards` plus the battle snapshot provide runtime state. | Use the same Records snapshot and GUIDs; distinguish authored template fields from per-instance abilities, modifiers, counters, and zones. Never substitute a card name or translated text for available metadata. |
| Actions and timing | Transaction classes compose requirements and invoke phase/session actions. | Typed transactions are normalized, checked by RulesPort, then passed to one native resolver. | Same phase, priority, speed, ownership, threshold, cost, and rejection behavior, including no partial payment on rejection. |
| Built-in rules | C# methods on `Session`, `Card`, phase states, and transaction requirements implement rules that are not represented as Records effects. | Python ports these rules in shared RulesPort modules such as `combat_rules.py`, `static_rules.py`, and lifecycle handlers. | Port both authored effects and built-in rules. A complete Records effect inventory does not prove parity for keywords, costs, combat, state-based actions, or turn boundaries. |
| Ability execution | `AbilityInstance` walks its ordered effect groups and invokes concrete C# effect implementations; nested effects call other abilities through their authored lifecycle. | `AbilityGraph` reads typed Records and `resolution.py` runs the native effect walk through `EffectContext` and focused effect modules. | Same effect-group and child order, input values, variable scope, cost/use accounting, and exactly-once activation. A paused ability resumes the same instance; nested child instances and parent links are created only where C# does so. Resume must not replay the parent or create a duplicate picker. |
| Targets and filters | `AbilityTargetTemplate.GetAllTargets`, `IsTargetValid`, derived target classes, and `AbilityInstance.ValidateMinimumTargetCount` define separate enumeration and validation behavior. | `targeting.py`, `targets.py`, and `filters.py` compile Records target/filter metadata and validate submitted selections. | Keep candidate enumeration, selection validation, optional/minimum counts, collection-mask behavior, and target-index mapping distinct. Preserve each authored `AbilityTargetIndex` through the picker and continuation; do not rewrite `target_map` globally to repair one effect. |
| Trigger discovery | `Session` and `AbilityManager` publish/discover triggers from C# mutation and phase boundaries. | Python publishers call `dispatch_native_trigger`; `trigger_discovery.py` evaluates authored trigger metadata and queues abilities. | Publish once at the same before/after mutation point, with the same source, target, owner, event payload, condition context, collection rules, and ordering. Missing or duplicate publication is a semantic bug even if the final board happens to match. |
| Zones and lifecycle | C# collection moves and card lifecycle methods also drive built-in rules and events. | `zone_effects.py`, lifecycle/effect modules, SQLite helpers, and mode adapters coordinate persisted rows and events. | Same owner/controller distinction, old/new zone, card visibility, duration boundary, state-based action timing, and follow-up triggers. A database row change without its ordered client events is incomplete. |
| Client contract | C# session synchronization invokes the client event handlers directly. | `domain/events.py`, `game_engine.py`, `encoder.py`, and the HConnect/PvP send paths serialize equivalent event data. | Same event class, field values, valid IDs, viewer-scoped card data, and event order; verify the resulting client transition as well as server state. |
| Game modes | PvE and PvP both use the shared C# rules model with different session facts. | The core RulesPort is shared, while PvE/PvP adapters translate owner IDs, champions, persistence, and packet destinations. | Adapters may supply mode facts and project results, but must not implement different card rules. Exercise both owner models whenever an interaction has a choice or mode-specific state. |

Live gameplay uses the native RulesPort backends. Legacy `abilities/` leaf
registrations, old direct APIs, and class inventories are not evidence that a
behavior is correct on the live RulesPort path. The adapter and implementation
boundaries are described in [`rules_port/README.md`](../rules_port/README.md).

## Parity gate for a rule change

Use this checklist before describing a behavior as matching C#:

1. **Pin the reference.** Record the relevant C# method/call site and the
   matching typed Records fields: ability/effect template, conditions, targets,
   filters, constants, and TAC where applicable. Card names and localized text
   are labels, not rule inputs, when structured metadata exists.
2. **Trace the whole decision.** Follow one typed input through classification,
   phase/priority and cost validation, the ability or combat lifecycle, state
   mutation, trigger publication, persistence, and client event projection.
   Include the client behavior when the result uses a picker, chain window, or
   phase transition.
3. **Assert semantics, not registration.** A fixture should assert legal and
   illegal inputs, exact state deltas, selected targets, ordered trigger/effect
   execution, and ordered emitted events. Include rejection rollback and
   boundary cases. Generated metadata-driven scenarios should cover rule
   families; do not hand-maintain one test per card or treat a no-crash sweep as
   proof of the printed behavior.
4. **Cover resumptions and interactions.** For a choice, discard, or nested
   activation, assert the paused instance, source, owner, variables, target
   slots, parent link, and next effect position survive persistence. Resume the
   paused instance once; create a nested child only when C# does. Include
   trigger-on-trigger and reconnect cases where the rule can pause.
5. **Run both ownership models.** Exercise the shared rule with Practice/PvE
   and PvP owner/champion mappings when ownership or response priority matters.
   Assert that the two adapters produce equivalent rule outcomes and the
   correct viewer-specific events.
6. **Compare an oracle trace.** When a C# execution or golden capture exists,
   capture the complete relevant state and normalize it with the exact ordered
   event sequence before comparing through `ParityCapture` /
   `compare_captures`. Keep RNG seeds and transaction order fixed. A
   matching seed is useful only when the RNG algorithm and call order also
   match; otherwise inject or record equivalent random draws. A Python-only
   expected result is a regression test, not a differential C# proof.
7. **Report the evidence honestly.** Update the relevant row below with its
   tested scenarios and remaining gaps. Use `Partial` or `open` when a handler
   exists without semantic acceptance or client evidence. Class inventory,
   compilation, and successful server startup are useful checks, but none
   certify gameplay parity by themselves.

The automated inventories in `rules_port/coverage.py` help catch new C#
transaction, effect-template, and filter classes. They do not compare field
semantics, target subclasses, conditions, variable combinations, built-in
rules, mutation timing, or client behavior. Those require typed metadata audits
and focused semantic scenarios. The detailed matrices below state which parts
currently have that evidence and which remain open.

## Ordered work list to close the parity gap

These are open engineering tasks, ordered so each step supplies evidence for
the next. Existing protocol catalogues, server fixtures, and individual client
traces are useful partial evidence; they do not complete a task until its done
condition is met.

- [ ] **U1 — Map live client flows to C# handlers.** For every live setup,
  phase, card, ability, target, trigger, combat, and reconnect flow, record the
  client state, request/event class, C# serializer/parser, C# session or
  `UIBattle` handler, recipient, reply policy, and expected next UI state.
  Extend `docs/CLIENT_SERVER_PROTOCOL.md` where its event catalogue lacks the
  consumer or state transition. **Done when** every live transaction and
  emitted event has a source-to-client path and no used class has an unknown
  response policy.
- [ ] **U2 — Make one correlated trace for a client action.** Capture the raw
  3029 request, decoded typed intent, pre/post RulesPort and SQLite state,
  ordered 3055 event classes and recipients, response/ack count, and the
  matching Unity exception and UI state-stack lines. Correlate them by session
  and transaction. **Done when** a failing report can be followed from the
  Unity action through the server mutation and back to the Unity handler in one
  trace bundle.
- [ ] **U3 — Gate the packet and acknowledgement contract.** Add focused
  golden checks for the active 3029 request shapes, 3055 envelope and nested
  event bytes, and the 3053 game-start response. Verify event class IDs, field
  order, enum/UID types, recipient privacy, and one response per transaction.
  Check both empty 3055 acknowledgements for handled requests with no sync and
  no invented replies for client fire-and-forget requests. **Done when** the
  golden checks cover every live transaction/event family and a Unity client
  accepts the packets without handler or decode errors.
- [ ] **U4 — Close setup, identity, and reconnect flows.** Exercise setup from
  `ReadyForGameSetup` through the service-level 3053 GameStarted response and
  the 3055 `GameStartedSessionEventArgs`, valid player/champion/card cache
  entries, coin-flip/first-player choice, mulligan, and the first normal
  priority window. Repeat with either player winning the toss and after a
  reconnect. **Done when** client logs show the expected UI state progression,
  every referenced `SessionCardId` resolves, and the client and server agree on
  phase, active player, and priority.
- [ ] **U5 — Close options, plays, and activation pickers.** Start from the
  server's `GreenLight` and `PlayerOptionList`, then exercise accepted and
  rejected card/ability transactions. Cover class 23 activation data, class 39
  triggered-ability data, additional costs, multiple targets, and nested
  prompts. Preserve authored `AbilityTargetIndex` slots through decoding and
  continuation; verify that a paused ability resumes once and its parent is
  not replayed. **Done when** every picker state reaches its expected selection
  state and returns to the right parent/priority state in both PvE and PvP.
- [ ] **U6 — Close chain, trigger, and priority presentation.** For one
  authored trigger of each event family, compare C# publication timing and
  payload with Python discovery, queueing, and resolution. Verify the same
  chain instance IDs across persisted state and `AbilityPushedOnChain`,
  `TopOfChainResolved`, `RemovedTopOfChain`, and `ChainEmpty`; verify both
  players see the correct response window. **Done when** each tested trigger
  resolves once, receives the correct priority window, and leaves Unity's
  chain/UI state settled.
- [ ] **U7 — Close card/player projection and visibility.** Exercise draw,
  reveal, transform, token creation, discard, destroy, zone move, resource and
  threshold changes, counters, and hidden-card updates. Verify full
  `CardUpdated` representations precede events that reference new cards,
  `CardMoved` uses the correct destination, and private identities reach only
  authorized viewers. Repeat after reconnect. **Done when** Unity's card cache,
  HUD, zones, and visibility match the authoritative server state at each
  checkpoint.
- [ ] **U8 — Close phase and combat UI flows.** Drive attack, blocker, damage
  assignment, combat resolution, end phase, and automatic phase-entry actions
  through the real client. Assert one `TurnPhaseUpdated` per transition,
  correct `GreenLight`/options, matching combat IDs, and no second transaction
  from a duplicated auto-entered UI state. **Done when** both attack and
  defense directions complete without client exceptions, stale buttons, or
  server/client phase disagreement.
- [ ] **U9 — Complete semantic coverage behind those flows.** For the open
  rows below, finish typed Records field audits and generated rule-family
  scenarios for effects, conditions, variables, target/filter subclasses,
  built-in rules, and interactions. Assert state deltas and ordered events,
  including boundary/rejection cases. Reuse fixtures across PvE and PvP; do
  not add one hand-maintained test per card. **Done when** every concrete C#
  class and meaningful current Records field is native, intentionally staged,
  or documented as an explicit gap, with semantic acceptance for the claimed
  behavior.
- [ ] **U10 — Make client acceptance repeatable.** Maintain a short real-Unity
  smoke run for the flows above and add a two-real-client PvP run for setup,
  private pickers, chain priority, combat, and reconnect. Save the relevant
  client/server trace with each result. **Done when** the run can be repeated
  after a change and its log/event assertions give a clear pass or failure;
  headless tests remain separate evidence for rules semantics.
- [ ] **U11 — Enforce the release bar.** For each changed rule, wire event, or
  picker, update its matrix row with source references, focused tests, both-mode
  coverage where applicable, and a Unity trace or an explicit missing client
  check. Keep unfinished rows marked partial/open. **Done when** no change is
  called C#-equivalent based only on handler registration, compilation,
  database state, or a no-crash sweep.

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
