# HEX TCG — Server Rules & Gameplay Decisions

This document is the canonical reference for how the private server plays a
battle. Phase names match the client's `Game.Shared.Mechanics.ETurnPhases`
enum and the server's `game_engine.ETurnPhases`. Gameplay decisions (both
implemented and intended) are recorded here.

## Turn Phases (ETurnPhases)

| Value | Name | Purpose / Notes |
|------:|------|-----------------|
| 0 | `Unknown` | Initial state; transitions to `NotPlaying`. |
| 1 | `NotPlaying` | Session waiting; the client's catch-all state. |
| 2 | `PreGame` | Game hasn't begun. Client runs encounter scenario setup (`RunScenarioSetup`) and queues a `PreGameEvent` trigger. No player input. Server passes straight through. |
| 3 | `PickGoesFirst` | Coin-flip result; the winner chooses Play or Draw. Client shows the **Play/Draw dialog** and sends `ChoosePlayTransaction` / `ChooseDrawTransaction`. The server **waits** for this transaction before advancing. |
| 4 | `Mulligan` | Opening-hand decision. Each player keeps or mulligans (see Mulligan Rules). |
| 5 | `StartGame` | Setup complete; board is readied for the first turn. |
| 6 | `StartTurn` | Turn begins for the active player. |
| 7 | `Ready` | Untap / ready the active player's cards. |
| 8 | `Prep` | On your turn start triggers fire. **Current resources are reset to total resources (untap).** Summoning sickness is cleared from warzone troops (`ECardStates.StartedATurnOnYourSide`). |
| 9 | `Draw` | Active player draws a card. |
| 10 | `FirstMainPhase` | Play cards, activate abilities, play one resource. |
| 11 | `DeclareCombatPriorityWindow` | Pre-combat priority (attacker). |
| 12 | `DeclareAttack` | Active player declares attackers. |
| 13 | `DeclareAttackPriorityWindow` | Priority after attackers are declared. |
| 14 | `DeclareDefense` | Defender assigns blockers. |
| 15 | `DeclareDefensePriorityWindow` | Priority after blockers are declared. |
| 16 | `AssignFirstStrikeDamage` | First-strike (Swiftstrike) damage is assigned/resolved. |
| 17 | `FirstStrikePriorityWindow` | Priority after first-strike damage. |
| 18 | `AssignDamage` | Normal combat damage is assigned/resolved. |
| 19 | `SecondMainPhase` | Second main phase after combat. |
| 20 | `EndPhase` | End-of-turn triggers fire. |
| 21 | `Discard` | Hand is reduced to the hand-size limit. |
| 22 | `EndTurn` | Turn ends; control passes to the opponent. |
| 23 | `Checksum` | Internal consistency check (never used in play). |
| 24 | `EndGame` | Game over; winner/loser is reported. |

## Frequently Asked Rule Questions (quick answers)

| Question | Answer |
|----------|--------|
| **How many resources can I play per turn?** | **1**, in your own main phase (First or Second). Cards/abilities may allow extras. No stack, no response window. See "Resource / Threshold Rules". |
| **How many times can I activate my Charge Power per turn?** | **Once per turn.** Charge powers (`m_IsChargePower=1`) carry `m_UsesPerTurn=1` on the ability template (e.g. Replenish Spell Power `ccd5c608`). |
| **How many times can I use a Spell Power per turn?** | **Unlimited**, but each use increases that spell's SP cost by **+1** permanently (`IncrementSpellPointCostModifier`, Session.cs:1154). The card text "Using a Spell increases its cost by 1[SP]" is literal. |
| **What happens when I try to draw with an empty deck?** | **You lose the game.** `DrawCard` calls `LoseGame(player, EPlayerEliminatedReason.DeckExhausted)` when the deck is empty (AuthoritativeSessionBase.cs:3175). This is the only deck-exhaustion penalty. |
| **Is there a max cards drawn per turn?** | Default **unlimited**; `MaxCardsDrawablePerTurn` (IntAttrs) overrides when present. `CantDrawCards` forces 0. |
| **When can I act?** | Only when you hold **priority**, which the client enters ONLY via `GreenLightSessionEventArgs` (class 48). See "Priority & Passing". |
| **Can I play a shard on the opponent's turn?** | No — resources are played in your own main phase only, and resource play never opens a response window. |
| **Do troops resolve instantly?** | No — they go on the stack (`CastSpells`, zone 128), the opponent gets priority, then resolve to `Warzone`. See "Playability". |
| **When do I lose / win?** | Currently only via champion defeat (life ≤ 0) or empty-deck draw. Victory/defeat is exposed via `!game_end` until combat/life is wired. |
| **What is the hand size limit?** | Campaign PvE: **10**; FRA/PVP: **7**. See "Hand Size Limit". |
| **Can I mulligan more than once?** | Yes, drawing one fewer card each time (7→6→…→0), forced keep at 0. See "Mulligan Rules". |
| **In what order do an ability's effects resolve?** | Forward, in `m_AbilityEffectList` array order (grouped by `EffectGroupId`) — NOT a stack. The *chain* between separate abilities is LIFO. See "Ability effect ordering & effect groups". |
| **Can I activate an ability that exhausts the card the turn it entered play?** | **No** — if the activation cost includes exhausting the card (`m_ExhaustsCardOnUse`), the troop must have survived to the start of your turn (`ECardStates.StartedATurnOnYourSide`, set at Prep) and be untapped, unless it has **Speed** (`ECardAttributes.Speed`). The tap cost cannot be paid while summoning sick — same rule as attacking. |

## Per-Phase Breakdown (client vs server)

Each phase is pushed as `TurnPhaseUpdatedSessionEventArgs` (class 3) with
`ActivePlayerId` + `PriorityPlayerId`. `ETurnPhasePlayers` (who is in the
priority window) is a **client-side static per-phase constant** — the server
never sends it. Phases not in the turn player's stop list are auto-passed by
the server (pushed, then immediately advanced).

| Phase (ETurnPhases) | Client behavior | Server behavior |
|---------------------|-----------------|-----------------|
| **2 PreGame** | Synchronous scenario setup (`RunScenarioSetup`), queues a `PreGameEvent`. No input. | Pushes PreGame; auto-advances. No transaction arrives (no priority window). |
| **3 PickGoesFirst** | Shows the **Play/Draw dialog**; sends `ChoosePlayTransaction` / `ChooseDrawTransaction`. | Stops and waits. **Play** → Mulligan. **Draw** → must also push `PlayerWishesToDrawFirst` (class 58) to reorder players (TODO). |
| **4 Mulligan** | Shows Keep/Redraw dialog; sends `AcceptStartingHandTransaction` or `MulliganTransaction`. | Stops and waits per player, one at a time, until BOTH keep. See "Mulligan Rules". |
| **5 StartGame** | No BattleState — client pushes nothing. | Server pushes it and advances itself to StartTurn. |
| **6 StartTurn** | Turn marker; turn number increments. | Sets active/priority player; resets per-turn flags. |
| **7 Ready** | Untaps cards visually. | Prepares to clear summoned cards. |
| **8 Prep** | Turn-start triggers fire. | Resets current resources = total (untap), clears `StartedATurnOnYourSide` (summoning sickness) from warzone troops. |
| **9 Draw** | Draws top card. | Draws top card (player draw → `_player_draw_card`, AI → `_ai_draw_card`). **Empty deck = lose** (DeckExhausted). |
| **10 FirstMainPhase** | Play cards/abilities/resources. Sends `Play*Transaction` / `ActivateAbilityTransaction`. | Grants priority; pushes `PlayerOptionList` (playable cards + affordable champion abilities). See "Playability". |
| **11 DeclareCombatPriorityWindow** | Pre-combat priority window (attacker). | Stop. Grants priority; on pass → `DeclareAttack`. |
| **12 DeclareAttack** | Declare attackers. `BattleStateDeclareAttackers`; sends `CommitTroopsToAttackTransaction`. | Always-stop. Pushes attack options (`ECardUsage.Attack` for ready, non-sick troops). On `CommitTroopsToAttack` → records attackers, pushes `AttackDeclared` (27) + `CombatListing` (62), advances. |
| **13 DeclareAttackPriorityWindow** | Priority after attackers. | Stop. On pass → `DeclareDefense`. |
| **14 DeclareDefense** | Assign blockers (defender). | **AI never blocks (2026-08-02):** server auto-passes, pushes `BlockersAssigned` (28, empty) per attacker. |
| **15 DeclareDefensePriorityWindow** | Priority after blockers. | Stop. On pass → `AssignFirstStrikeDamage`. |
| **16 AssignFirstStrikeDamage** | Swiftstrike damage. | No swiftstrike yet: auto-passes. |
| **17 FirstStrikePriorityWindow** | Priority after first-strike. | Auto-passes. |
| **18 AssignDamage** | Normal damage. | Stop. Client auto-sends `AssignDamageOrderTransaction` (no blockers); server resolves combat damage — each unblocked attacker's `attack` subtracts from the AI champion's health (persisted in battle state) — then advances to SecondMain. If the AI champion's health ≤ 0, the player wins. |
| **19 SecondMainPhase** | Same as FirstMainPhase. | Same as FirstMainPhase. |
| **20 EndPhase** | End-of-turn triggers. | Auto-pass unless it's a stop. |
| **21 Discard** | Auto-passes with no-op animation if hand ≤ limit (sends NO transaction). | Auto-skips `Discard → EndTurn` when hand fits. |
| **22 EndTurn** | Turn ends; opponent's turn begins. | Advances; on pass at EndTurn hands turn to the AI (or back to player). |
| **23 Checksum / 24 EndGame** | Internal checksum / game-over. | Not used / pushed via GameEnded (class 2). |

## Client Phase State Machine

The client's `Session` state machine permits these transitions:

```
Unknown → NotPlaying
NotPlaying → (any phase)
PreGame → PickGoesFirst | StartGame | EndGame
PickGoesFirst → PreGame | Mulligan | EndGame
Mulligan → StartGame | Mulligan | EndGame
StartGame → StartTurn | EndGame
StartTurn → Ready | EndGame
Ready → Prep | EndGame
Prep → Draw | FirstMainPhase | EndGame
Draw → EndGame | FirstMainPhase
FirstMainPhase → DeclareCombatPriorityWindow | SecondMainPhase | EndGame
DeclareCombatPriorityWindow → DeclareAttack | AssignDamage | EndGame
SecondMainPhase → DeclareAttack | DeclareCombatPriorityWindow | SecondMainPhase | EndPhase | EndGame
EndPhase → Discard | EndGame
Discard → EndTurn | EndGame
EndTurn → StartTurn | Mulligan | EndGame
EndGame → (terminal)
```

The server must only emit transitions that the client permits, or the client's
state machine will reject (or mis-handle) the phase change.

## Client → Server Transactions (3029 `PlayerTransaction`)

Every transaction carries `m_PlayerId` (UID) + `m_TransactionId` (int). The
server is authoritative — the client sends *intent*, not state snapshots, and
the server infers + persists the resulting state. Transaction type name is
matched from the raw `inner_bytes` (e.g. `b"PassPriorityTransaction"`).

| Transaction | Purpose |
|-------------|---------|
| `PassPriorityTransaction` | Player passes priority. Carries `m_TurnPhase` (the client's current phase) + validation requirements (`TurnPhaseRequirement`, `PlayerHasPriorityRequirement`) — server can cross-check its own phase. |
| `ChoosePlayTransaction` / `ChooseDrawTransaction` | Coin-toss winner picks Play or Draw (PickGoesFirst phase). |
| `AcceptStartingHandTransaction` | Player keeps their opening hand (Mulligan). |
| `MulliganTransaction` | Player mulligans; draws one fewer card. |
| `PlayResourceTransaction` / `PlayTroopTransaction` / `PlaySpellTransaction` / `PlayArtifactTransaction` / `PlayChampionTransaction` | Play a card of the given type (carries `m_SessionCardId`). |
| `ActivateAbilityTransaction` / `ActivateTriggeredAbiliesTransaction` | Activate a card ability / triggered abilities (carries `SetAbilityActivationData`). |
| `CommitTroopsToAttackTransaction` / `CommitTroopsToDefenseTransaction` | Declare attackers / blockers. |
| `AssignDamageOrderTransaction` | Assign combat damage order. |
| `ReadyCardTransaction` | Ready (untap) selected cards. |
| `DiscardTransaction` | Discard a card during the Discard phase. |
| `QuitGameTransaction` | Withdraw / concede (server treats as a loss). |
| `SetAutoPassTransaction` / `CancelAutoPassTransaction` | Set / clear the player's auto-pass state (`EPassingState`). `Resolve` also passes if the player holds priority. |
| `SetTurnPhasesTransaction` | Client reports its stop positions (`m_SelfTurnPhases` / `m_OpponentTurnPhases`). Informational — the server does not act on them. |
| `RequestPrioritySyncTransaction` | Client asks the server to resend the GreenLight (`ResendGreenlight`) when priority packets were lost. |
| `RequestPlayerOptionsTransaction` | Client asks the server to recompute + push the `PlayerOptionList`. |
| `SendGameStateChecksumTransaction` | Client sends a checksum of its game state; server compares to detect desync. |
| `DebugCheatTransaction` | Cheat command (`DebugAction`). |
| `EncounterModDialogTransaction` | Respond to an encounter-modifier dialog. |
| `TipWindowClosed` | Tutorial tip acknowledged. |
| `NonsenseTransaction` | No-op / placeholder. |

### Client-controlled sub-state (NOT sent by the server)

- **`ETurnPhasePlayers`** (None / All / Active / Defending): which players
  participate in a phase's priority window. Set *statically per phase* in the
  client's `TurnPhaseState.m_TurnPhasePlayers`. The server only controls *who is
  active / priority* via `ActivePlayerId` / `PriorityPlayerId` in
  `TurnPhaseUpdatedSessionEventArgs` (class 3) — it never sends `ETurnPhasePlayers`.
- **`EPassingState`** (None / Attack / EndOfTurn / EndPhase): the player's
  auto-pass preference, set via `SetAutoPassTransaction` / `CancelAutoPassTransaction`.
  Client-controlled; the server never sends or broadcasts it.

### State querying / validation

- No full state snapshots flow client → server. The server maintains
  authoritative state in `game_sessions.turn_order_json` and infers changes from
  transaction intents.
- The client's `m_TurnPhase` in `PassPriorityTransaction` and the
  `SendGameStateChecksumTransaction` checksum let the server validate its own
  view.
- On reconnect (`TryReconnectionToDisconnectedGame`), the server rebuilds +
  resends state **from the DB** — the DB is the source of truth.

## Server Turn Cycle (battle_engine.py)

The per-turn phase cycle is **dynamic** and stored in battle state
(`turn_phases`, persisted in `turn_order_json` so a reconnect can resume):

```
No combat (AI always, or player with no ready troop):
StartTurn(6) → Ready(7) → Prep(8) → Draw(9) → FirstMainPhase(10) →
SecondMainPhase(19) → EndPhase(20) → Discard(21) → EndTurn(22)

Player combat (player controls a warzone troop with StartedATurnOnYourSide):
StartTurn → Ready → Prep → Draw → FirstMainPhase →
DeclareCombatPriorityWindow(11) → DeclareAttack(12) →
DeclareAttackPriorityWindow(13) → DeclareDefense(14) →
DeclareDefensePriorityWindow(15) → AssignFirstStrikeDamage(16) →
FirstStrikePriorityWindow(17) → AssignDamage(18) →
SecondMainPhase(19) → EndPhase(20) → Discard(21) → EndTurn(22)
```

`advance_phase()` walks the stored list, wrapping `EndTurn` back to `StartTurn`
and switching the turn player (player ↔ AI). Combat phases (11–18) are present
only when the active player controls a ready troop (the AI never attacks yet).

### Combat — attacking & blocking (2026-08-03)

**Declaring attackers**
- **Summoning sickness**: a troop can attack iff it is a troop, has
  `ECardStates.StartedATurnOnYourSide`, and is not tapped (a `Speed`/haste
  troop may attack the turn it enters). Prep sets `StartedATurnOnYourSide` and
  clears `CameOutThisTurn`; playing a troop sets `CameOutThisTurn` (sick until
  the next Prep). All combat bits (`Tapped`, `Attacking`, `HasAttacked`,
  `Blocking`, `HasBlocked`, `Damaged`) are cleared at the controller's Prep.
- **DeclareAttack** → the attacker picks ready warzone troops (server pushes
  `ECardUsage.Attack` options). `CommitTroopsToAttackTransaction` carries the
  chosen `SessionCardId`s; the server persists them in
  `battle_state['player_attackers']` (or `['ai_attackers']`), marks each
  `Attacking|HasAttacked` (+`Tapped` unless Steadfast), and pushes
  `AttackDeclared` (27) + `CombatListing` (62).
- **AI attackers** are chosen by `ai_declare_attackers` (aggressive personality
  alpha-strikes; Comfortable/Defensive only commit troops with attack ≥
  `min_x_value`).

**Declaring blockers**
- Any **untapped** warzone troop may block (summoning sickness does NOT affect
  blocking). The defender's options carry `ECardUsage.Defend` + a
  `ResourceId.Blocking` target list of the attackers.
- If the human is the defender, the server grants the blocker UI only when the
  player controls at least one untapped troop (`_player_can_block`); otherwise
  DeclareDefense auto-passes (attackers unblocked). `CommitTroopsToDefenseTransaction`
  records the assignments in `battle_state['ai_blockers']` and pushes
  `BlockersAssigned` (28).
- **AI blockers** (`ai_pass_declare_defense`), biggest threats first:
  1. **Single survivor** — the cheapest unused blocker that survives the hit
     (defense > attacker attack).
  2. **Multiblock** — a blocker already blocking may also face another attacker
     if it survives the cumulative damage (defense − damage taken > attack).
  3. **Dogpile** — if no single blocker survives a threat (attack ≥ 3), trade
     enough blockers (combined attack ≥ attacker defense) to bring it down.

**Damage assignment order**
- When an attacker is blocked by **multiple** blockers, the ATTACKER chooses the
  order (the client's `AssignDamageOrderTransaction` →
  `battle_state['player_damage_order']`; the AI defaults weakest-first).
- The attacker assigns its attack in that order: each blocker takes as much
  damage as it needs to die, then the leftover damage flows to the next blocker.
- Each blocker deals its **full attack** back to the attacker, regardless of the
  damage it took. A troop dies if `effective defense − accumulated damage ≤`
  incoming damage. Deaths move the card to the graveyard (`Dead` state) and fire
  Deathcry.
- **Trample / Crush** (`Juggernaught`): after assigning enough damage to kill
  the blockers, any REMAINING damage breaks through to the defender's champion.
- **Lifelink** (`SpiritDrain`): the attacker's controller gains life equal to
  the total combat damage the attacker dealt; each lifelink blocker's controller
  gains life equal to the damage that blocker dealt.
- **Buffs**: effective attack/defense include `card_attack_mod`/`card_defense_mod`
  (permanent buffs) minus `card_damage` (temporary damage); temporary damage is
  shown red and heals at Prep.
- Unblocked attackers deal their attack directly to the defending champion.

**Resolution flow** — the shared `ai.resolve_combat` handles BOTH directions
(the AI attacks the player and the player attacks the AI) with the attacking /
defending players swapped. Events are wrapped in `BeginCombatResolution` (30) →
per-combat `CombatPhaseResolved` (29) → `CombatRemoved` (32) →
`EndCombatResolution` (31); combat-death Deathcries are drained after.
- **Combat events** (classes): `AttackDeclared` 27, `BlockersAssigned` 28,
  `CombatPhaseResolved` 29, `BeginCombatResolution` 30, `EndCombatResolution` 31,
  `CombatRemoved` 32, `CombatsThatNeedDamage` 61, `CombatListing` 62,
  `CombatSession` 63.

## State-Based Effects

State-based effects are checked whenever the **stack is empty** (after every
chain resolves / combat ends), and any that are true are applied immediately
without using the stack:

- **Zero / negative defense**: a troop whose effective defense (base defense +
  `card_defense_mod` − `card_damage`) is **0 or less** dies. It gets the
  `Dead` state (64) and moves to the owner's graveyard, firing Deathcry.
  (`kill_troop` in ability.py.)
- **Card attributes** that change death:
  - `Immortal` (Invincible) — does NOT die to damage or card/destroy effects,
    but still dies to state-based effects (defense 0 or less) and to sacrifice.
  - `Flight` — a troop may only block a `Flight` attacker if the blocker itself
    has `Flight` or `SkyGuard`.
  - `Speed` — may attack the turn it enters play (exempt from summoning
    sickness).

## Game Setup Flow

1. **PreGame** — server pushes `PreGame` (client runs synchronous scenario setup;
   nothing blocks advancing). The client sends **no transaction** during PreGame:
   `PreGameState.m_TurnPhasePlayers = None`, so the priority window auto-completes
   with no pass. Defensively, if a `PassPriorityTransaction` arrives while the
   game is still in setup (battle state not yet created), the server treats it
   as a **no-op pass** — it acknowledges the transaction but does not advance
   the turn cycle (otherwise it would corrupt the setup flow).
2. **PickGoesFirst** — server pushes this phase + GreenLight and **stops**. The
   player (always the coin-toss winner, i.e. TurnOrder[0]) sees the Play/Draw
   dialog and clicks **Play** or **Draw**.
   - **Play** → client sends `ChoosePlayTransaction`.
   - **Draw** → client sends `ChooseDrawTransaction`.
   - The server responds by pushing the `Mulligan` phase. (Drawing first also
     requires pushing `PlayerWishesToDrawFirst` class-58 so the client reorders
     players, and the opponent should go first — TODO, see below.)
3. **Mulligan** — see Mulligan Rules.

## Mulligan Rules

- Each player is asked, one at a time, starting with the turn player
  (the coin-toss winner). Priority stays with the deciding player until they
  keep.
- The Mulligan phase ends only when **both** players have kept their hand.
- **Keep**: client sends `AcceptStartingHandTransaction`; server pushes
  `AcceptedStartingHand`.
- **Mulligan / redraw**: client sends `MulliganTransaction`; server pushes
  `PlayerMulliganedHand`, returns the cards to the deck, reshuffles, and draws
  one **fewer** card (7 → 6 → 5 → … → 0). A player is forced to keep at 0 cards.
- **AI mulligan** (`_resolve_ai_mulligan`): keeps if its opening hand contains at
  least one shard/resource; otherwise mulligans (drawing one fewer card per
  redraw) until it has a shard or reaches 0 cards.
- Mulligan shuffling is scoped per session in `game_cards.position`.

## Hand Size Limit

- **Campaign (PvE encounters): max hand = 10 cards.**
- **Frost Ring Arena / PVP: max hand = 7 cards.**

The `PlayerUpdated.max_hand_size` field and the Discard-phase auto-skip both
depend on this value. **TODO**: the server currently hardcodes 7; it must be
wired to the game mode (10 for campaign, 7 otherwise).

**Exceptions:** some cards override this limit:
- Cards that grant `UnlimitedHandSize` (IntAttrs) remove the cap entirely — no
  discard is required regardless of hand size.
- `MaximumHandSizeModifiers` (IntAttrs) adjust the cap by a static delta.

The server's Discard-phase logic must respect these when card effects are
implemented; until then the plain hand-size limit applies.

## Drawing & Deck Exhaustion

- The turn player draws **1 card** at the start of their turn (Draw phase).
  Additional draws come from card/ability effects.
- **Drawing from an empty deck = instant loss.** `DrawCard`
  (AuthoritativeSessionBase.cs:3175) calls
  `LoseGame(player, EPlayerEliminatedReason.DeckExhausted)` when the deck has no
  cards. This is the ONLY penalty for an empty deck — there is no "fatigue
  damage" like other card games; a player who cannot draw simply loses.
- **Max cards drawable per turn**: default unlimited. `IntAttrs.MaxCardsDrawablePerTurn`
  caps it when present; `IntAttrs.CantDrawCards` sets the cap to 0. The server's
  per-turn draw currently ignores this (no effect cards wired yet).
- The server's draw (`_player_draw_card` / `_ai_draw_card`) fetches the top card
  from `game_cards WHERE location='deck' ORDER BY position`, moves it to `hand`,
  and pushes `CardMoved` (50) + `CardDrawn` (7) + `CardUpdated(Hand)` (64). If no
  deck cards remain it currently **silently returns** (no loss) — the DeckExhausted
  loss is not yet implemented server-side. **TODO.**
- Draw order and reshuffles are scoped by `session_id` (concurrent battles never
  interfere); the AI and player decks share the same `game_cards` table
  (AI rows have `user_id=0`).

## Resource / Threshold Rules

- A player may play **only 1 resource (shard/land) per turn**, and only **in
  their own main phase** (`FirstMainPhase` or `SecondMainPhase`) — never on the
  opponent's turn. The client's `CanPlayResourceCard` (Session.cs:373) requires:
  `player == active player` AND phase ∈ {FirstMain, SecondMain} AND
  `m_PlayedResource < 1 + AdditionalResourcesPlayableOnYourTurn`. Cards can add
  extra resource plays per turn via `IntAttrs.AdditionalResourcesPlayableOnYourTurn`.
- Playing a resource moves it to `PlayedResources` and increments:
  - current resource pool,
  - total resource pool,
  - the matching threshold colour count,
  - a champion charge (basic thresholds only).
- **Playing a resource does NOT go on the stack and does NOT give the opponent
  a priority window.** It resolves immediately to `PlayedResources` — no
  `CastSpells`/chain display, no response opportunity. Only actual spells/troops
  (quick actions on the opponent's turn) create stack/window interactions.
- A resource cannot be played again until the next turn
  (`player_resource_played_this_turn` / `ai_resource_played_this_turn`), reset
  in the Prep phase (`_run_ai_turn` line 1499 for the AI; Prep handler for the
  player). The server mirrors `m_PlayedResource` with these two flags.

## Priority & Passing

- The active (turn) player holds priority at the start of every phase.
- Passing priority hands it to the opponent; when **both** players pass in a
  phase, the phase advances.
- **The stack:** passing priority does NOT always move the phase. If anything
  is on the stack (a spell/troop awaiting resolution in `CastSpells`), passing
  resolves the top item instead of advancing the phase — repeated until the
  stack is empty, then the phase advances. When a troop is played it
  goes on the stack (moves to `CastSpells`, zone 128); the opponent gets
  priority (`ResolveTopOfChain` GreenLight). When the opponent passes, the
   troop resolves → moves to `Warzone` (zone 8).  **In progress (2026-08-01)**.
- On `EndTurn` the turn player switches and a new turn starts at `StartTurn`.

### Ability effect ordering & effect groups (2026-08-01)
- **Effects within a single ability are NOT a stack.** The client resolves them
  **forward, in ascending `m_CurrentEffectIndex` order** through the ability's
  `m_AbilityEffectList` (AbilityInstance.cs:636-717): it groups consecutive
  entries by `EffectGroupId`, applies each group in array order
  (`ApplyEffectGroup`), then advances `m_CurrentEffectIndex += list.Count`.
  So for Soothsaying, effect 0 (draw) resolves BEFORE effect 1 (discard).
- **The chain BETWEEN separate abilities IS a stack (LIFO).** `Chain` is a
  `LinkedList` used with Push/Pop/Peek from the tail (Chain.cs:61-108) — the
  last ability pushed resolves first.
- **The client is not authoritative in a real game.** Effect resolution
  (`m_CurrentEffectIndex`, `CurrentlyResolving()`, `ReconnectedApply`,
  `RequestAbilityActivationData`) runs on the **server**'s authoritative
  Session. The client only renders the resulting events; it does not walk the
  effect list itself.
- **`AbilityActivationDataRequiredSessionEventArgs` (class 23)** is how the
  authoritative server pauses mid-resolution to ask the player for targets: it
  carries `EffectGroupId` + `EffectInstanceIds` (UIBattle.cs:5743 →
  `BattleStateUseTriggeredAbility` → `BattleStateConfigureAbility`). This is the
  correct mechanism for a choose-and-discard prompt AT the discard's position in
  the BOM (draw first, then prompt), versus attaching a target instance to the
  option up front (which prompts BEFORE the ability activates).
- `CardMoved.Index` is the **zone position**, not an effect index; `CardUpdated`
  (class 64) has no effect-index field.

- **The server pushes ONE phase at a time** — never a burst of phase events in a
  single packet. Each phase is pushed in its own network packet.
- **Auto-pass driven by the server (phase stops):** the server captures the
  player's stop positions (from `SetTurnPhasesTransaction`, or the client
  defaults when not configured) and uses them to drive progression:
  - If the current phase is **not** a stop for the turn player → the server
    auto-passes it: pushes the phase in its own packet and immediately advances
    to the next phase (no priority window is granted).
  - If it **is** a stop → the server grants priority (GreenLight) and waits for
    the player to act or pass.
  - **`PickGoesFirst` and `Mulligan` can never be auto-passed** — they are always
    stops (they require the Play/Draw and Keep/Mulligan dialogs).
- **Stale-pass guard:** the client also auto-passes non-stop phases, which races
  with the server's own auto-advance. `PassPriorityTransaction` carries
  `m_TurnPhase`; the server ignores a pass whose phase doesn't match its current
  phase.
- **GreenLight context = destination label:** when the server grants priority it
  sends the `EPriorityContext` that describes what happens next, which drives
  the client's Pass button text (`Battle_Priority_*`): e.g. `FirstMainPhase` →
  `ProcedeToSecondMain` ("Proceed to Next Main Phase"), `SecondMainPhase` →
  `ProceedToEndTurn` ("Proceed to End Turn").
- **`StartGame` exception:** the client pushes **no BattleState** for
  `StartGame`, so no pass can ever arrive; the server pushes it and advances to
  `StartTurn` itself.
  - **Stop positions are a user preference.** Each player configures which
    phases pause (their own turn and the opponent's) via the client's
    `SetTurnPhases` (Player.cs). The auto-pass logic references these lists via
    `HasStop()` in `PriorityWindowAction.PostUpdate`: a phase in the stop lists
    gets a GreenLight and pauses; any other phase auto-passes. Defaults stop at
    `FirstMainPhase`, `SecondMainPhase`, `DeclareAttackPriorityWindow`,
    `DeclareDefensePriorityWindow`, plus `PickGoesFirst`/`Mulligan`/`StartGame`/
    `DeclareAttack`/`AssignDamage`/`AssignFirstStrikeDamage` (self) and
    `DeclareDefense` (opponent).
  - **Server capture:** the server reads these stops from
    `SetTurnPhasesTransaction` (only sent when the player opens the Phase Stops
    dialog) and stores them in `battle_state` (`player_self_stops` /
    `player_opp_stops`). When not configured it falls back to the client
    defaults. The server auto-passes phases not in the effective stop list
    (see above) rather than relying on the client's auto-pass.
- The client shows the **Pass Priority** button only when
  `HasPriority()` is true, which is set exclusively by `GreenLightSessionEventArgs`
  (class 48). The server must re-grant GreenLight after every card play / phase
  advance or the button disappears.
- The client auto-passes the `Discard` phase with a no-op animation when the
  hand is within the hand-size limit (sends no transaction). The server
  auto-skips `Discard → EndTurn` in that case so the turn doesn't stall.

## AI Turn (`_run_ai_turn`)

The AI has no client, so its turn is played out server-side, but it is
**pass-gated and phase-paced** so the client renders each step:

1. Pause `AI_PHASE_DELAY = 1.0s` (simulate thinking).
2. Walk through `TURN_PHASES` with the AI as active + priority player, pushing
   **one phase per packet** with a `AI_PHASE_DELAY = 1.0s` pause between pushes
   so the client renders each phase.
3. The AI receives a **GreenLight in every phase** (so the client's Pass button
   / priority window state stays consistent) and the AI passes each one
   server-side. `Draw` → draw the top card of the AI deck; `FirstMainPhase` →
   play one resource if available (obeys the 1-per-turn rule).
4. When a phase is a **stop for the human (opponent-stop)**, the AI turn pauses
   and the human gets a `ResumeTopOfChain` GreenLight → the client pushes
   `BattleStateInactivePriorityWindow` (their priority window); the AI resumes
   after the human passes.
5. `EndTurn` → switch the turn player back to the human; the player's turn
   auto-starts (`_advance_to_priority`), resetting
   `player_resource_played_this_turn` for the new turn.

## Victory / Defeat

- `!game_end victory` / `!game_end defeat` ends the current battle:
  - pushes the **GameEnded** event (class 2, via the 3055 channel) with the
    winner/loser UID lists, which displays the Victory/Defeat screen, and
  - updates the campaign state (`gameendnotify`; a win reveals the quest-giver
    NPC and sets `TutorialDone`).
- **Withdraw (concede)** — the client's Withdraw button sends a
  `QuitGameTransaction` (3029). The server detects it (raw `m_QuitEntireSeries`
  / `m_Surrendered` bytes in `inner_bytes` — the field's **presence**, not its
  boolean value, marks the quit, since a normal concede sends
  `m_QuitEntireSeries=false`) and ends the game as a **loss** for the player
  (`GameEnded`, marked loss) — the same `commands.push_battle_game_end` path as
  `!game_end defeat`. This works in FRA and campaign battles.

### Client one-at-a-time transaction pipeline (Withdraw blocker)
The client's `SessionClient.SubmitTransaction` sends only **ONE** transaction at
a time: while `m_HasPreviousTransactionBeenRespondedByServer` is false it
silently **drops** every further transaction (incl. `QuitGameTransaction`).
That flag is reset to true ONLY when a `NetworkPacketSessionEventArgs`
(class 255 — the top level of every 3055 sync packet) arrives
(ClientSessionBase.cs:30). So for any 3029 transaction the server handles
**without pushing 3055 events** — `SetTurnPhases`, stale/no-op passes — the
server MUST send an empty 3055 sync packet afterwards
(`_push_transaction_ack`), or the next transaction (e.g. Withdraw) never reaches
the server. Fix applied 2026-08-01.
- Combat / life totals are not yet implemented, so there is no natural
  win condition — victory/defeat is currently triggered via `!game_end`.

## Implemented vs. Intended

| Rule | Status |
|------|--------|
| Turn phase cycle (no combat) | Implemented (battle_engine.py) |
| Pass priority & GreenLight | Implemented |
| Mulligan (player + AI) | Implemented |
| 1 resource per turn | Implemented (player + AI) |
| Server-driven auto-pass (phase stops) | Implemented (2026-08-01) |
| AI turn pass-gated, one phase per packet | Implemented (2026-08-01) |
| Victory/Defeat via `!game_end` | Implemented (2026-08-01) |
| Withdraw → loss (`QuitGameTransaction`) | Implemented (2026-08-01) |
| Champion charges on basic threshold | Implemented (2026-08-01) |
| Champion talents persisted (2037) + `last_deck_id` | Implemented (2026-08-01) |
| Play/Draw dialog (PickGoesFirst split) | Implemented; **Draw-First** player-reorder still TODO |
| Hand size: Campaign 10 / FRA-PVP 7 | **Intended** — server still hardcodes 7 |
| Empty-deck draw → loss (DeckExhausted) | **Intended** — server silently no-ops on empty deck |
| Charge-power 1/turn + spell-cost escalation | **Intended** — server doesn't enforce UsesPerTurn/escalation |
| Stack resolution loop (pass resolves stack) | Not implemented — cards resolve immediately |
| **Combat phases (11–18) + player attacks** | Implemented (2026-08-02) — player declares attackers, unblocked attackers deal damage to the AI champion; **AI does not attack or block yet** |
| Combat damage to champion → victory | Implemented (2026-08-02) — AI champion at 0 health = player wins |
| Card attributes (`ECardAttributes`) + full ability lists | Implemented (2026-08-02) — extracted to `card_templates.attributes`/`abilities_json` (all abilities, not just the first) |
| Champion charge/spell power activation | Implemented (2026-08-01) — see "Champion Abilities & Talents" below; per-turn limits still TODO |

## Champion Abilities & Talents

### Talent resolution
Champion talents (charge powers, spell powers, passives) are stored in
`champions.talents` as a JSON array of GUIDs.  Each talent GUID is a
`ChampionTalentData` entry in gamedata; ability-granting talents (flagged
`CardAbilityContainer: 1` in `_v`) have an `m_Abilities` array whose first
element's `m_CardAbilityId` is the **actual ability GUID** that goes on the
champion card.  The mapping is precomputed in the `talent_data` DB table.

### Resource threshold → charges & spell points
- Playing a **basic shard** (resource) grants +1 **charge point** to the
  champion who played it.
- **Spell points do NOT increase from playing shards.** They start at 0 and
  are gained only through champion abilities (e.g. Replenish Spell Power
  costs charges and generates SP).
- Charges are pushed via `ChampionChargePointsChanged` (class 36) and the
  cumulative total in `PlayerUpdated.charges` (class 65).
- Spell points are pushed via `PlayerUpdated.spell_points` (class 65) and
  `ChampionSpellPointsChanged` (class 37) when an ability modifies them.
- Champions start at 0 charges and 0 spell points.

### Activation
- The client renders charge/spell power buttons on the champion HUD by
  reading abilities from `CardUpdated.abilities`.
- To make a button **pressable**, the server must push a `PlayerOptionList`
  (class 70) containing the champion card with `ECardUsage.Activate` (2)
  during a main phase when the champion has charges/SP >= the ability's cost.
  The ability's charge/SP cost comes from its gamedata `AbilityTemplate`
  (`m_ChargePointCost` / `m_SpellPointCost`).
- The client's `UseChargePower` / `UseSpellPower` buttons send an
  `ActivateAbilityTransaction` when clicked.
- **Per-turn activation limits** come from the ability template:
  - **Charge powers** (`m_IsChargePower=1`) have **`m_UsesPerTurn=1`** — one
    activation per turn, ever. Replenish Spell Power (`ccd5c608`, 3 charge) is
    `UsesPerTurn=1`.
  - **Spell powers** (`m_IsSpellPower=1`) have `UsesPerTurn=0` — unlimited per
    turn — but each use permanently increases that spell's SP cost by **+1**
    (`IncrementSpellPointCostModifier`, Session.cs:1154; reflected in the
    `CardUpdated.SpellPointCostModifiers` dict). E.g. Soothsaying starts at 4 SP,
    then 5, 6, ... The client's `CanActivateAbilityBase` enforces both
    `UsesPerTurn`/`UsesPerGame`/`Cooldown` and the escalated cost
    (`m_SpellPoints < SpellPointCost + GetSpellPointCostModifier`).
  - Other per-use gates: `UsesPerGame` (limit over whole game), `Cooldown`
    (turns between uses), `ExhaustsCardOnUse` (taps the card — the champion
    can't be tapped, but troop/artifact abilities can), and `m_OncePerTurn`.
  - **Exhaust-as-cost & summoning sickness**: an ability whose cost exhausts
    the card (`m_ExhaustsCardOnUse`) **cannot be activated by a troop that
    entered play this turn** (`ECardStates.CameOutThisTurn`) unless that troop
    has **Speed** (`ECardAttributes.Speed`) — the tap cost can't be paid while
    summoning sick. Prep clears `CameOutThisTurn` and sets
    `StartedATurnOnYourSide`, making the troop eligible again (if untapped).
    The client's `CanActivateAbilityBase` enforces this the same way it
    enforces attack eligibility; the server must gate `ExhaustsCardOnUse`
    manual abilities on `StartedATurnOnYourSide | Speed` in
    `_affordable_troop_abilities` / `_activate_troop_ability`.
- The client also refuses activation unless `activatingPlayer.IsPriorityPlayer()`
  (no priority → button dead), the chain is empty for non-quick manual
  abilities, and the current phase is a main-phase priority window for
  non-quick abilities.
- **Server enforcement**: `_filter_affordable_abilities` gates on phase bit +
  cost (including the escalated SP cost); the activation handler deducts
  charges/SP and applies the effect. **Spell-power escalation IS enforced** —
  each use permanently adds +1 to that spell's SP cost, tracked in
  `battle_state['player_sp_uses']` (mirrors the client's
  `IncrementSpellPointCostModifier`), and the updated cost is pushed via
  `CardUpdated.SpellPointCostModifiers`. **TODO**: enforce `UsesPerTurn` (charge
  powers are once per turn — currently repeatable while charges last).
- **Choose-and-discard (e.g. Soothsaying)**: the ability's BOM runs in order —
  **draw a card, THEN discard** — and effects never change the turn phase. The
  discard is presented as a target selection on the champion option: a
  `TargetInstanceSessionEventArgs` (class 67) whose `TargetId` matches the
  ability's own `AbilityTargetTemplateIds` entry (Soothsaying: `eb7e48cd` "You")
  with the player's hand cards as targets, so the client shows the
  `BattleStateConfigureAbility` → `BattleStateAssignTargets` picker. The chosen
  card arrives in the activation's `TargetMap`; the server moves it to discard
  after the BOM's draw resolves. **Note**: this prompts BEFORE the ability
  activates (the client collects targets up front). The authoritative approach —
  prompting at the discard's position via class 23 `AbilityActivationDataRequired`
  — is documented under "Ability effect ordering & effect groups".
- Champions start at 0 charges and 0 spell points; charges only grow by playing
  basic shards (+1 each), SP only by abilities (e.g. Replenish).

## Playability (PlayerOptionList)

During `FirstMainPhase` and `SecondMainPhase` the server pushes
`PlayerOptionList` (class 70) with every card the player can act on:

| Card type | Rule |
|-----------|------|
| **Resource (shard)** | Playable if `resource_played_this_turn` is false (max 1/turn). |
| **Troop / Artifact** | Playable if `cost ≤ available_resources` AND for every shard requirement in `threshold_json` the player's matching threshold count ≥ required count. |
| **Champion ability** | `ECardUsage.Activate` with `OptionInstanceSessionEventArgs`. Playable when charges/SP ≥ ability cost. |

### Troop play → stack
1. Troop played → moves to `CastSpells` (zone 128, "on the chain")
2. Opponent gets priority (`ResolveTopOfChain` GreenLight)
3. Opponent passes → troop resolves → moves to `Warzone` (zone 8)
4. Priority returns to active player

## Trigger Resolution Order (Simultaneous Events)

When an event fires that triggers effects on cards controlled by **both**
players (e.g. `StartOfTurn`, `TurnEnded`), triggers resolve in this order:

1. **Turn player's triggers resolve first**, then the opponent's.
2. Within each player's set: **oldest card on the field to newest** (first-in,
   first-resolved).
3. If a trigger adds a new trigger to the same event, it queues **behind**
   already-queued triggers for that player and resolves after them (FIFO).

**Examples:**
- `Prep` fires `StartOfTurn` — the turn player's "At the start of your turn…"
  triggers resolve before the opponent's triggers.
- A symmetrical effect like a board wipe triggered by `TurnEnded` — the
  active player's deathcries / end-of-turn triggers resolve first.
- The "oldest to newest" ordering is determined by the warzone position
  (a card that entered play first is older), not by card UID or DB id.
