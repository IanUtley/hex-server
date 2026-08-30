# Client-Server Protocol and Card State

This document is a compact map of the protocol used by the fixed 32-bit Mono
Hex client and the Python private server. It is an orientation document; the
source, `HOWTO.md`, `RULES.md`, `encoder.py`, and the client disassembly remain
the authoritative references for individual fields.

## Authority and transport

The client is primarily a UI and local-state projection. The server owns the
session, player priority, card locations, mutable card state, rules
resolution, and the result of transactions. A client sends an action; the
server validates and resolves it, persists the result, and publishes events
that make the client render the new state.

There are several related wire formats:

| Layer | Purpose | Encoding shape |
|---|---|---|
| HConnect/DataWrapper | Routes service requests and responses | Typed wrapper plus ObjFmt or binary payload |
| ObjFmt | Profile, store, mail, tournament, load-balancer, and service data | Type/field records with Hex-specific integer, bool, string, enum, UID, and array encoding |
| Session event stream | Game setup, card movement, priority, options, and battle updates | Event class 255, event ID, then a custom binary payload |
| Chat/session control | Login to rooms, chat, join/leave, and lobby updates | HConnect route messages such as `rjoin`, `rleave`, `rchat`, and `glist` |
| HTTP proxy | Local auth compatibility and news hooks | HTTP on port 8081 |

The game protocol listens on TCP 9933. The proxy listens on TCP 8081. Rules
integers are little-endian 32-bit values, booleans are bare `0`/`1` values in
ObjFmt, strings carry a length followed by raw bytes, and enums must use the
exact client enum type/value expected by the handler. `ResourceId` and
`SessionCardId` values must have valid client-recognized UID types.

The client sends only one transaction at a time. A handled transaction that
does not otherwise produce a synchronization packet must still receive an
empty 3055 acknowledgement, or the client can silently drop the next
transaction.

## Main service families

The exact request class names are in the encoder and client sources. These are
the useful service families and commonly encountered data types:

| Family | Examples |
|---|---|
| Profile/store | 2037, 2043, 2081, 2083, 2089, 2091, 2095, 2185, 2187, 2205, 2210, 2211 |
| Load balancer/session discovery | 22011, 22013, 22015, 22017, 22019, 22021, 22027, 22029, 22031 |
| Game session transactions/events | 3029 and 3050–3056 |
| Tournaments | 25021, 25027, 25029, 25058, plus room/session events |
| Mail | 60003 and related mail requests |
| Campaign/arena | 10001, 10003, 10005, 10007, 10011 |

Some requests are fire-and-forget from the client's point of view. In
particular, sending an unexpected reply to setup or phase-control requests
can result in a client “command handler not found” error.

## Server-to-client session events

The server sends game state through a `NetworkPacketSessionEventArgs` (wire
class `255`). Its payload contains the recipient player ID, a list of event
class IDs, and the serialized payload for each event. Each nested event starts
with its own 32-bit class ID and 64-bit session ID. The client dispatches the
nested payload through `SessionEventArgs.BuildArgs(classId, payload)`.

The event is sent to one or more recipients. A public event can be sent to
both players; private events such as hand updates, target pickers, and card
reveals must only be sent to the appropriate player. The server must send a
complete, client-valid event contract: for example, a `PlayerOptionList` must
contain the corresponding `OptionInstance`, `TargetInstance`, and
`CostInstance` data, and every `SessionCardId` must use a recognized UID type.

### Event class catalogue

The `Server model` column describes the current Python implementation in
`domain/events.py`. “Available” means the fixed client understands the event
class, but the current server does not generally construct it yet.

| Class | Client event type | Purpose | Server model |
|---:|---|---|---|
| 1 | `GameStartedSessionEventArgs` | Session setup: turn order, champion/template IDs, sleeves, board, coin, divisions, and series data. | Implemented |
| 2 | `GameEndedSessionEventArgs` | Terminal winners and losers. | Implemented |
| 3 | `TurnPhaseUpdatedSessionEventArgs` | Current phase, active player, priority player, and elapsed priority timer. | Implemented |
| 4 | `ChessTimerUpdatedSessionEventArgs` | Chess-clock value for a player. | Implemented |
| 5 | `PlayerMulliganedHandSessionEventArgs` | Player mulliganed and the replacement-card count. | Implemented |
| 6 | `PlayerAcceptedStartingHandSessionEventArgs` | Player kept or accepted the opening hand. | Implemented |
| 7 | `CardDrawnSessionEventArgs` | A player drew a card, including card instance and draw ordinal. | Implemented |
| 8 | `CardDestroyedSessionEventArgs` | Card was destroyed, with responsible card if known. | Implemented |
| 10 | `CardDiscardedSessionEventArgs` | Card was discarded from hand or another effect explicitly discarded it. | Implemented |
| 11 | `CardVoidedSessionEventArgs` | Card moved to the Void/removed-from-game zone. | Implemented |
| 12 | `CardGraveyardedSessionEventArgs` | Card moved to the graveyard/discard zone because it died or was graveyarded. | Implemented |
| 14 | `ChampionCardPlayedSessionEventArgs` | Champion card was played/initialized. | Implemented |
| 15 | `TroopCardPlayedSessionEventArgs` | Troop entered play. | Implemented |
| 16 | `ResourceCardPlayedSessionEventArgs` | Resource entered play; includes whether it was free. | Implemented |
| 17 | `SpellCardPlayedSessionEventArgs` | Spell card was played. | Implemented |
| 18 | `SpellCardCastSessionEventArgs` | Spell resolved/cast; includes whether it was free. | Implemented |
| 19 | `ArtifactCardPlayedSessionEventArgs` | Artifact entered play. | Implemented |
| 21 | `AbilityCancelledSessionEventArgs` | Ability on the chain was cancelled and by whom. | Implemented |
| 22 | `AbilityPushedOnChainSessionEventArgs` | Ability/trigger was added to the chain, including source, targets, costs, and template. | Implemented |
| 23 | `AbilityActivationDataRequiredSessionEventArgs` | Private request for activation targets/cost data for a resolving ability. | Implemented |
| 24 | `CardTappedSessionEventArgs` | Card became tapped/exhausted. | Implemented |
| 25 | `CardUntappedSessionEventArgs` | Card became ready/untapped. | Implemented |
| 26 | `CardPrimedSessionEventArgs` | Card was primed for an effect or combat action. | Implemented |
| 27 | `AttackDeclaredSessionEventArgs` | An attacker/combat was declared. | Implemented |
| 28 | `BlockersAssignedSessionEventArgs` | Blockers were assigned to a combat. | Implemented |
| 29 | `CombatPhaseResolvedSessionEventArgs` | One combat damage phase resolved. | Implemented |
| 30 | `BeginCombatResolutionSessionEventArgs` | Combat resolution began. | Implemented |
| 31 | `EndCombatResolutionSessionEventArgs` | Combat resolution ended. | Implemented |
| 32 | `CombatRemovedSessionEventArgs` | Combat was removed from the active combat list. | Implemented |
| 33 | `PlayerCurrentResourcePoolChangedSessionEventArgs` | Current spendable resource/mana pool changed: operation, delta, and new value. | Implemented |
| 34 | `PlayerTotalResourcePoolChangedSessionEventArgs` | Maximum/total resource pool changed. | Implemented |
| 35 | `PlayerResourceThresholdChangedSessionEventArgs` | Typed shard threshold changed. This is distinct from current spendable mana. | Implemented |
| 36 | `ChampionChargePointsChangedSessionEventArgs` | Champion charge points changed. | Implemented |
| 37 | `ChampionSpellPointsChangedSessionEventArgs` | Champion spell points changed. | Implemented |
| 38 | `ChampionHealthChangedSessionEventArgs` | Champion health/damage value changed. | Implemented |
| 39 | `TriggeredAbilityActivationDataRequiredSessionEventArgs` | Private request to choose targets for one or more triggered abilities. | Implemented |
| 41 | `TopOfChainResolvedSessionEventArgs` | Top chain item resolved. | Implemented |
| 42 | `RemovedTopOfChainSessionEventArgs` | Top chain item was removed. | Implemented |
| 43 | `EncounterCardsCreatedInZoneSessionEventArgs` | Encounter cards were created in a specified collection/location. | Implemented |
| 44 | `CardTransformedSessionEventArgs` | Existing card changed template, including replica/gem information. | Implemented |
| 45 | `CardRevertedSessionEventArgs` | Transformed card reverted to a template. | Implemented |
| 46 | `EquipmentSetSessionEventArgs` | Equipment IDs assigned to a player/champion. | Implemented |
| 47 | `DeckCreatedSessionEventArgs` | Deck/encounter deck was created. | Implemented |
| 48 | `GreenLightSessionEventArgs` | Priority notification: identifies the player and priority context allowed to act. | Implemented |
| 49 | `CardCollectionsMergedSessionEventArgs` | One card collection was merged into another. | Implemented |
| 50 | `CardMovedSessionEventArgs` | Card collection, location, owner, and ordered index changed. | Implemented |
| 51 | `CardsRevealedSessionEventArgs` | Cards were revealed, with collections, owners, positions, and ability instance. | Implemented |
| 52 | `PlayerStateModifiedSessionEventArgs` | Generic player state modifier notification. | Available |
| 53 | `ReconnectDoneSessionEventArgs` | Reconnect/state-rebuild boundary completed. | Implemented |
| 54 | `CardCountersChangedSessionEventArgs` | Card counters changed. | Available |
| 55 | `EncounterModDialogSessionEventArgs` | Encounter modifier dialog/options for PvE. | Available |
| 56 | `ConfigureAISessionEventArgs` | AI configuration/setup data. | Available |
| 57 | `CardPutInHandSessionEventArgs` | Card was put into a hand by an effect. | Available |
| 58 | `PlayerWishesToDrawFirstSessionEventArgs` | Player chose draw first. | Implemented |
| 59 | `PlayerWishesToPlayFirstSessionEventArgs` | Player chose play first. | Implemented |
| 60 | `FirstPlayerDictatedSessionEventArgs` | Server declares the first player/coin-flip result. | Implemented |
| 61 | `CombatsThatNeedDamageSessionEventArgs` | Combat(s) requiring player damage assignment. | Implemented |
| 62 | `CombatListingSessionEventArgs` | Lists active combats for the combat UI. | Implemented |
| 63 | `CombatSessionEventArgs` | Attacker/blocker pairing and combat state. | Implemented |
| 64 | `CardUpdatedSessionEventArgs` | Full card representation: stats, state flags, controller, collection, template, abilities, counters, gems, and mutable values. | Implemented |
| 65 | `PlayerUpdatedSessionEventArgs` | Full player/champion state: health, resources, thresholds, charges, timers, and champion ID. | Implemented |
| 66 | `CostInstanceSessionEventArgs` | Nested option data describing a selectable cost and legal cards. | Implemented |
| 67 | `TargetInstanceSessionEventArgs` | Nested option data describing a target template and legal targets. | Implemented |
| 68 | `OptionInstanceSessionEventArgs` | Nested ability option: template, target instances, and min/max counts. | Implemented |
| 69 | `PlayerOptionSessionEventArgs` | Nested card option and its legal play/activate usage. | Implemented |
| 70 | `PlayerOptionListSessionEventArgs` | Private complete list of actions currently available to one player. | Implemented |
| 71 | `BulkSessionEventSessionEventArgs` | Groups multiple session events into one logical update. | Implemented |
| 72 | `CycleCardArtSessionEventArgs` | Requests a card-art cycle/variant change. | Available |
| 73 | `AbilityActivatedSessionEventArgs` | Ability activation notification. | Available |
| 74 | `ScroungeSessionEventArgs` | Scrounge-related client notification. | Available |
| 75 | `ForceSynchronizationSessionEventArgs` | Requests/marks a client synchronization. | Available |
| 76 | `AnimationTriggerSessionEventArgs` | Client animation trigger. | Available |
| 77 | `ChainEmptySessionEventArgs` | Chain became empty; priority can return to the normal window. | Implemented |
| 78 | `ActiveChessTimerPlayerSessionEventArgs` | Identifies the player whose chess timer is active. | Available |
| 79 | `WaitingOnPlayerSessionEventArgs` | Client is waiting on a player. | Available |
| 80 | `ShowTipSessionEventArgs` | Displays a client tip, optionally with a button. | Implemented |
| 81 | `SetDeckPersonalityEventArgs` | Sets deck/AI personality presentation. | Available |
| 82 | `SkipSetupSessionEventArgs` | Tells the client to skip setup animation/state. | Implemented |
| 83 | `DisableInterfaceSessionEventArgs` | Enables/disables client interaction. | Implemented |

Class IDs 0, 9, 13, 20, 40, and 84+ are not valid event types in the
client's current `SessionEventArgs` dispatch table. Class `255` is the packet
envelope, not a nested gameplay event. The nested option types 66–70 are
normally serialized inside a `PlayerOptionList`; they are not separate
priority messages on their own.

### Event ordering rules

The client treats event order as meaningful. A typical play/ability sequence
is:

1. `GreenLight` and a private `PlayerOptionList` establish who may act.
2. The client sends a transaction containing the selected card/ability and
   any target/cost selections.
3. The server emits card movement/play events and `CardUpdated` events.
4. Ability effects emit chain, card, player, resource, combat, or reveal
   events in resolution order.
5. `TopOfChainResolved`/`ChainEmpty`, `TurnPhaseUpdated`, and the next
   `GreenLight` establish the next legal action.

`CardUpdated` is the authoritative visual representation for mutable card
state. Do not rely on a tap/untap animation event alone when a state update is
also required. Likewise, a resource threshold event (class 35) is not a
spendable-resource event (class 33), and a `CardMoved` event does not replace
the full `CardUpdated` needed to create or refresh a card cache entry.

### Visibility rules

The event target is part of the server contract, not merely a transport
optimization:

- Send phase, combat, public board, and public resource changes to both
  players where appropriate.
- Send `CardUpdated` for a hand/deck card only to a player allowed to know its
  identity. Opponent hidden cards need a face-down/limited representation.
- Send `PlayerOptionList`, `AbilityActivationDataRequired`,
  `TriggeredAbilityActivationDataRequired`, and private `CardsRevealed`
  events only to the controlling/choosing player.
- On reconnect, republish valid `PlayerUpdated.ChampionId` and card IDs before
  sending phase/priority events that reference them. An undefined UID type
  corrupts the client's card cache and commonly produces a
  `KeyNotFoundException` in `UIBattle.OnTurnPhaseUpdated`.

## Session startup and event ordering

The normal game setup is a sequence of server events rather than one complete
snapshot:

1. The client connects, authenticates, and requests a game/session.
2. The server returns session identity and player/opponent information.
3. `ReadyForGameSetup` establishes the setup boundary.
4. `PlayerAdded` and the first 3055 synchronization wave publish valid player,
   champion, game, and deck/card identities.
5. Opening cards and setup state are sent as card updates/moves.
6. `SkipSetup`/`PreGame` transitions the session to `PickGoesFirst`,
   `Mulligan`, and then normal turn phases.
7. `GreenLight` identifies the player currently allowed to act.

Every card that the client may reference must first have a valid cached
representation. A `CardMoved` event normally describes the zone transition;
`CardUpdated` supplies or refreshes the full representation and mutable
fields. On reconnect, the server republishes enough player, card, zone, and
priority state to rebuild that projection.

## Transactions and priority

Typical client-to-server actions include play card, activate ability, select
targets/options, pass priority, declare attackers, declare blockers, assign
combat damage, keep/redraw mulligan cards, concede, and withdraw. The server
checks the current phase, priority owner, card location, costs, targets, and
rules before applying one.

The gameplay phase values are:

| Value | Phase |
|---:|---|
| 0 | Unknown |
| 1 | NotPlaying |
| 2 | PreGame |
| 3 | PickGoesFirst |
| 4 | Mulligan |
| 5 | StartGame |
| 6 | StartTurn |
| 7 | Ready |
| 8 | Prep |
| 9 | Draw |
| 10 | FirstMain |
| 11–15 | DeclareCombatPriority, DeclareAttack, DeclareAttackPriority, DeclareDefense, DeclareDefensePriority |
| 16–18 | FirstStrikeDamage, FirstStrikePriority, AssignDamage |
| 19 | SecondMain |
| 20–23 | EndPhase, Discard, EndTurn, Checksum |
| 24 | EndGame |

`GreenLight`, options, phase updates, and the resulting card/player events
must be ordered consistently. The server's `battle_engine.py` is the rules
owner for this state machine; `game_engine.py` serializes its view for the
client.

## Card zones and state

The client calls card collections `ECardCollections`. The server persists
human-readable locations in `game_cards` and maps them to these wire values:

| Value | Client zone | Server meaning |
|---:|---|---|
| 0 | None | No collection/undefined |
| 1 | Deck | Draw deck |
| 2 | Hand | Player hand |
| 4 | Champions | Champion card collection |
| 8 | Warzone | Cards in play |
| 16 | Discard | Graveyard/discard pile |
| 32 | Void | Removed from game |
| 64 | PlayedResources | Resources in play |
| 128 | CastSpells | Cast-spell/history collection |
| 256 | Underground | Underground zone |
| 512 | Choosing | Temporarily revealed/choice zone |
| 1024 | Mod | Modification collection |
| 2048 | Simulacrum | Simulacrum collection |
| 4096 | UI_Warzone | UI-only warzone view |
| 8192 | UI_Constant | UI-only constant view |

A persisted `game_cards` row combines stable identity and current state:

- `card_uid`/instance identity and owner identify the card instance.
- `template_guid` resolves the printed card and its gamedata.
- `location`, `session_id`, and `user_id` identify the current zone and game.
- `position` preserves ordering, especially in deck, hand, discard, and void.
- Mutable values hold attack/defense, damage, exhaustion, counters, gems,
  abilities, and other runtime changes.

Card movement is represented by server-side database updates followed by
client events. The relevant event families include:

| Event | Meaning |
|---:|---|
| 7 | Card drawn, including the drawn card instance and draw ordinal |
| 8 | Card destroyed |
| 10 | Card discarded |
| 11 | Card moved to Void/removed from the game |
| 12 | Card sent to graveyard/discard because it died or was graveyarded |
| 14–19 | Champion, troop, resource, spell, cast-spell, and artifact play/cast notifications |
| 50 | Card moved to a collection/location/index |
| 64 | Full card representation/state refresh |

The server should not use a guessed hard-coded index when appending a card to
an ordered zone. For a single-card move it calculates the next position for
the same location/session/player, so reconnecting or opening the discard view
retains the previous order.

## Gamedata-driven rules

The original client `Data/gamedata` is a compressed record store. The
extraction scripts decode it into JSONL/seed data. At fresh database startup,
`HEX_GAMEDATA` (or the `GAMEDATA` alias) can point to that client file; the
server populates client-derived static tables and then applies server-owned
fallback/compatibility seeds. Existing databases are not silently replaced.

The important record families are:

| Gamedata record | Runtime use |
|---|---|
| `CardTemplate` | Printed card identity, types, costs, stats, tags, text keys, and ability references |
| `AbilityTemplate` | Ability identity, trigger/timing, costs, conditions, targets, and effect list |
| `AbilityEffectTemplate` | Typed effect leaf and its fields/parameters |
| `AbilityTargetTemplate` | Target restrictions, filters, quantities, and target relationships |
| Condition/counter records | Conditions, counters, thresholds, and state checks |
| Champion/talent records | Champion templates, selectable powers, class data, and talent abilities |
| Gem/inventory/chest records | Socketed gems, rewards, packs, and collection/static metadata |
| Scene/encounter records | Campaign scene, encounter deck, and related setup data |

The runtime pipeline is:

1. Resolve a card instance to its `template_guid` and load the card template.
2. Resolve the card's ability GUIDs to `card_abilities_meta` and the ability
   template/effect rows.
3. Build legal `PlayerOptionList` entries from the current phase, owner,
   costs, target templates, conditions, and mutable card state.
4. When the player selects an option, create a transaction/ability resolution
   and evaluate its effect groups in order.
5. Map each typed effect to a generic BOM leaf or a small server adapter,
   applying the result to authoritative DB state.
6. Emit card/player/resource/option/phase events and persist the resulting
   chain or stack state.

The data-driven path is important because the server can support a card by
adding a generic effect executor or correcting extracted metadata rather than
adding a branch for a card name. A limited compatibility fallback still reads
game text for legacy/incomplete records, but typed gamedata fields take
precedence.

Current gaps are concentrated in less common effect leaves, full generic
target and condition evaluation, variables/output variables, duration
teardown, uses/cooldowns, and some trigger types. Those gaps should be fixed
in the metadata/resolver layer and covered by a focused test rather than by
duplicating card-specific rules in the session handler.

## Visibility, reconnect, and debugging

Hidden zones are still authoritative server state. The client receives only
the representation appropriate to that player and zone; a deck card may be
known by identity to the server while remaining hidden to the opponent.
Reconnect is therefore a state reconstruction: publish valid champion/player
IDs, card representations, locations, ordered positions, current phase,
priority, options, resources, and pending chain state in a client-safe order.

When a client is stuck, inspect its `Hex_Data/output_log.txt` first. Invalid
UID types, missing `CardUpdated` records, wrong enum names, event ordering, or
an omitted empty 3055 acknowledgement commonly leave the UI waiting even
when the database state looks correct.
