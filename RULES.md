# Hex server gameplay rules

This is the canonical gameplay contract for the private server. It describes
current behavior and intentional gaps without pretending that an unchecked
parity feature is complete. `HOWTO.md` defines architecture and protocol;
`docs/CLIENT_SERVER_PROTOCOL.md` and `abilities/ABILITIES.md` hold detailed
wire and metadata evidence.

Use these status words when adding a rule:

- **Implemented**: authoritative state, events, persistence, and a focused
  test or client check exist.
- **Partial**: a server path exists but parity, edge cases, or client proof is
  incomplete.
- **Intended**: the desired rule is known but the implementation is not ready.

## Turn phases

The phase values match the client's `Game.Shared.Mechanics.ETurnPhases` and the
server's `game_engine.ETurnPhases`.

| Value | Phase | Rule |
|---:|---|---|
| 0 | `Unknown` | Initial internal state. |
| 1 | `NotPlaying` | Session waiting state. |
| 2 | `PreGame` | Encounter setup and pre-game triggers; no player input. |
| 3 | `PickGoesFirst` | Coin-toss winner chooses Play or Draw and the server waits for that choice. |
| 4 | `Mulligan` | Each player keeps or redraws an opening hand. |
| 5 | `StartGame` | Setup finishes and the first turn is prepared. |
| 6 | `StartTurn` | Active player and turn number are established; per-turn flags reset. |
| 7 | `Ready` | Active cards are readied as allowed. |
| 8 | `Prep` | Start-turn triggers fire, resources refresh, damage heals, and summoning sickness clears where applicable. |
| 9 | `Draw` | Active player draws; an empty-deck loss applies on supported draw paths. |
| 10 | `FirstMainPhase` | Active player may play cards, activate abilities, and play one resource. |
| 11 | `DeclareCombatPriorityWindow` | Pre-combat priority for the attacker. |
| 12 | `DeclareAttack` | Active player declares legal attackers. |
| 13 | `DeclareAttackPriorityWindow` | Priority after attackers are declared. |
| 14 | `DeclareDefense` | Defender declares legal blockers. |
| 15 | `DeclareDefensePriorityWindow` | Priority after blockers are declared. |
| 16 | `AssignFirstStrikeDamage` | Swiftstrike damage is assigned/resolved. |
| 17 | `FirstStrikePriorityWindow` | Priority after first-strike damage. |
| 18 | `AssignDamage` | Normal combat damage is assigned/resolved. |
| 19 | `SecondMainPhase` | Post-combat main phase. |
| 20 | `EndPhase` | End-of-turn triggers and effects. |
| 21 | `Discard` | Active hand is reduced to its limit. |
| 22 | `EndTurn` | Turn ends and control passes. |
| 23 | `Checksum` | Internal consistency phase; not used for normal play. |
| 24 | `EndGame` | Terminal game state and result publication. |

The normal turn is:

```text
StartTurn -> Ready -> Prep -> Draw -> FirstMainPhase
  -> [combat phases 11..18 when combat is legal]
  -> SecondMainPhase -> EndPhase -> Discard -> EndTurn
```

The phase list is persisted in `game_sessions.turn_order_json` so reconnects
resume the authoritative state. The client must receive only transitions its
phase state machine accepts. A phase without a relevant stop may be pushed and
auto-passed; `PickGoesFirst` and `Mulligan` always wait for their transactions.

## Authority, priority, and the chain

The server owns phase, active player, priority, card state, resources, targets,
and results. Client transactions express intent and must be validated against
that state. The client does not send authoritative snapshots.

Priority is granted with `GreenLightSessionEventArgs`. A player can act only
when the server has granted priority to that player. Passing advances the
stored phase/priority state according to the active stop and auto-pass rules.
F10/auto-pass is a client preference plus server-side pass behavior; it must not
skip a required user-input wait or leak priority to the wrong player.

Top-level troops, spells, champion abilities, and triggers use the persisted
chain/stack model when the path supports responses:

1. Push one top-level item and publish its chain event.
2. Grant priority; when both sides pass, resolve the top item.
3. Publish resolution/removal events, then grant priority for the next item.
4. When the chain is empty, apply state-based effects and continue the phase.

Within one ability, effect groups execute in the metadata's
`m_AbilityEffectList` order. Those leaves are not separate priority items.
Some legacy trigger paths still resolve directly; they are compatibility gaps,
not a reason to add a new card-name special case.

## Setup, first turn, and mulligan

- The server creates both player and opponent/AI session identities, champion
  session-card IDs, decks, card instances, and ordered zones before publishing
  battle state.
- `PreGame -> PickGoesFirst -> Mulligan` is the client-compatible setup path.
  Tutorial encounters may choose Play automatically; normal sessions wait for
  `ChoosePlayTransaction` or `ChooseDrawTransaction`.
- The opening hand is normally seven cards. Each redraw decreases the hand
  size by one (`7 -> 6 -> ... -> 0`); zero cards forces a keep. A player is not
  considered ready until both sides have kept.
- The intended campaign hand-size limit is ten and the FRA/PVP limit is seven.
  The server still has paths that use seven universally; these are a known
  parity gap and must not be silently described as complete.
- The draw-first player-order notification and all client-equivalent hand
  reordering remain partial where the feature checklist says so.

## Resources, thresholds, and costs

- A player may play one resource in their own main phase each turn unless a
  rule grants an additional play. Resource play does not open a response
  window.
- Current resources refresh to total resources at `Prep`. Card/ability costs
  are paid from the authoritative current pool; thresholds are checked against
  the player's threshold flags.
- The client-facing threshold flags are Blood `4`, Ruby `8`, Sapphire `16`,
  Wild `32`, and Diamond `64`. Stored threshold arrays use the corresponding
  color indexes and must be converted before encoding.
- Charge powers use their metadata-defined uses per turn. Spell-power costs
  use the card's current modifier; repeated use may permanently increase that
  modifier when the metadata says so.
- Every resource, threshold, current-pool, total-pool, and player update must
  be emitted from the post-mutation state so the client HUD does not revert to
  default values.

## Cards and zones

The server resolves a card instance through its current `game_cards` row and
`template_guid`, then loads typed metadata from `card_templates`, ability
records, target templates, and conditions. Card type, cost, threshold, current
zone, owner, mutable state, and affordability determine playability.

Normalized locations are `deck`, `hand`, `warzone`, `PlayedResources`, `void`,
and `discard`. A card move updates the authoritative DB and ordered position,
then sends the destination `CardUpdated` before `CardMoved`. A draw needs both a
zone/card representation update and the draw animation event. Permanent cards
normally enter the warzone; actions resolve through the cast-spell path and
then leave for the appropriate destination.

Do not expose hidden card identities to the opponent. Reconnect reconstructs
the same filtered projection from the DB rather than trusting client state.

## Combat

- A troop may attack when it is in the warzone, ready, legal for the active
  player, and has begun a turn on that side, unless it has Speed/haste. It is
  normally exhausted on attack unless Steadfast applies.
- Any legal untapped defender may block; summoning sickness does not prevent
  blocking. The attacker chooses damage order among multiple blockers.
- First-strike damage resolves in phases 16/17, then normal damage resolves in
  phase 18. A blocker deals its legal combat damage back to the attacker.
- Crush/Trample carries excess damage through blockers to the defending
  champion. Lifelink/SpiritDrain grants life for damage dealt. Flight,
  Skyguard, unblockable, Swiftstrike, Steadfast, rage, gems, and other keywords
  are legal only when represented by metadata or a documented adapter.
- Lethal troops move to the graveyard, retain the death state needed by death
  triggers, and fire Deathcry through the ability resolver. State-based deaths
  occur after resolution/combat when the chain is empty.
- Champion health is authoritative. Champion defeat and supported empty-deck
  draws end the game; withdrawal is a server loss and must publish the normal
  game-end/campaign continuation events.

Combat and uncommon replacement/prevention effects still have incomplete
original-client parity. Extend shared combat/effect logic and test both player
and AI directions rather than adding a one-card branch.

## Abilities and triggers

The metadata-driven ability pipeline is:

```text
card/ability metadata -> legal targets and costs -> transaction
  -> conditions/effect groups -> authoritative mutation
  -> card/player/resource/zone events -> persisted state
```

Target lists must come from `AbilityTargetTemplate` filters, quantities, and
relationships. Effects must use typed parameters, counters, variables, and
durations where available. A custom adapter is a compatibility boundary for a
known extraction/client gap and must be documented in `abilities/ABILITIES.md`.

The remaining ability gaps include less common leaves, complete target modes,
condition trees, output variables, duration teardown, uses/cooldowns, and some
trigger types. A new implementation should add a generic leaf or metadata
correction first and add a focused regression test.

## PVE, PVP, AI, and tournaments

PVE encounters, FRA, tournament games, and ordinary PVP share session/card
state and event rules. Mode code selects setup, decks, opponent identity,
campaign/tournament consequences, and AI personality; it must not fork the
fundamental phase, zone, event, or ability rules.

AI decisions are server-side. The AI must take legal actions, publish the same
authoritative state/events as a player, and respect waits that require a human
transaction. Campaign encounter metadata may select Aggressive, Comfortable,
or Defensive behavior; it must not be replaced by a card-name guess.

Tournament registration, matching, results, forfeit, and participant-scoped
history are separate domain operations. Auction remains an unimplemented API
surface and should not be removed while its client contract is unresolved.

## Acceptance criteria for a new rule

A rule is ready to call Implemented only when all of the following are true:

- the rule is stated here and, when gameplay-facing, in the relevant RULES
  section;
- the owning domain API and authoritative state mutation are clear;
- valid and invalid inputs are covered;
- event types, recipient filtering, and event order are tested;
- reconnect/persistence behavior is tested when state is durable;
- both PVE/AI and PVP paths are considered where they share the rule;
- focused tests pass, the golden protocol checks pass, and the client log shows
  no new handler/UID/state-machine errors for client-tested changes.

Known incomplete behavior belongs in `docs/PRIVATE_SERVER_FEATURES.md` with a
short reason and next acceptance test. Do not turn this document into a list of
historical prompts or implementation timestamps.
