# Client-Server Protocol and Card State

This document is a compact map of the protocol used by the fixed 32-bit Mono
Hex client and the Python private server. It is an orientation document; the
source, `HOWTO.md`, `RULES.md`, `encoder.py`, and the client disassembly remain
the authoritative references for individual fields. Durable implementation
and ownership rules are kept in `HOWTO.md`; this file retains protocol and
gamedata detail only. The ordered C#-to-Python rules and Unity parity backlog
is in [`RULES_PORT_PARITY.md`](RULES_PORT_PARITY.md).

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
| 54 | `CardCountersChangedSessionEventArgs` | Card counters changed. | Implemented; native counter effects emit it after the full card update. |
| 55 | `EncounterModDialogSessionEventArgs` | Encounter conversation requested by an ability; resumes through `EncounterModDialogTransaction`. | Implemented; PvE and PvP conversation prompts emit it to the chosen player. |
| 56 | `ConfigureAISessionEventArgs` | AI configuration/setup data. | Available |
| 57 | `CardPutInHandSessionEventArgs` | Card was put into a hand by an effect. | Available |
| 58 | `PlayerWishesToDrawFirstSessionEventArgs` | Player chose draw first. | Implemented |
| 59 | `PlayerWishesToPlayFirstSessionEventArgs` | Player chose play first. | Implemented |
| 60 | `FirstPlayerDictatedSessionEventArgs` | Forced-first-player signal; Unity sets `m_CoinFlipSkip`. A normal toss winner is first in class-1 `GameStartedSessionEventArgs.TurnOrder`. | Implemented |
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

### Live client flow path inventory (U1)

This section joins the server request/event path to the fixed C# client path.
The wire source files are under `hex-private/HexClient/Assembly-CSharp-firstpass/` unless
noted otherwise.

**Transport and dispatch path.** The client builds a 3029 request from
`GameSession.PlayerTransactionRequestArgs`; `Mechanics/Transactions/Transaction.cs`
serializes each concrete transaction with `EncData.Encode` and declares its
polymorphic `KnownType` set. `Game.Client.SessionClient.SubmitTransaction`
sends it through `GameClient.SendSessionTransaction` and gates the next normal
transaction until a 3055 sync packet arrives. In
`ClientSessionBase`'s class-255 handler, the client parses and routes every
nested event, then marks the previous transaction responded; this also runs
for a 3055 packet with no nested events, which is why an empty acknowledgement
unblocks the next normal request. The server classifies the raw
request in `application/player_transactions.py:classify_player_transaction`,
decodes typed values at the `hconnect_server.py` boundary, normalizes them in
`rules_port/wire.py:normalize_player_transaction`, and dispatches them through
the RulesPort session.

For server events, the 3055 body is a `NetworkPacketSessionEventArgs` (nested
class 255). The client parses it with
`NetworkPacketSessionEventArgs.Get` -> `SessionEventArgs.BuildArgs`; each
event's `byte[]` constructor reads that event's fields. `ClientSessionBase`
routes the parsed events, updates `ClientStateCache` for options, combats,
cards, and players, then forwards them to `UIBattle`'s public event handlers.
`UIBattle.OnTurnPhaseUpdated` and `PushStateForPhase` select the active
`BattleState` from phase, active-player, and priority data. The C# parser map is
the authority for accepted event IDs; `domain/events.py` is the Python
serializer/model source.

The C# packet parser also performs a cross-event rewrite before routing. In
`NetworkPacketSessionEventArgs.CombineRevealAndChoose`, when a packet contains
`CardsRevealed` and a matching class-23 `AbilityActivationDataRequired` (same
ability instance or parent), the client finds the option for that request's
source card and appends each revealed card not already in a target's legal
`Targets` to that `TargetInstance.AdditionalTargets`. This happens inside
`Get()`, after nested events are decoded but before `ClientSessionBase` sees
them. Therefore a reveal-plus-picker interaction depends on those events being
in the same 3055 packet and on the ability/source IDs matching; valid class-51,
class-23, and class-70 payloads sent in separate packets do not trigger the
client rewrite. The Python event model serializes `additional_targets`, so
server-side projection can also express the value explicitly when needed. Any
change to reveal or picker batching must account for this client behavior and
verify the resulting `BattleStateAssignTargets` selection in Unity.
`tests/tests_u3_wire_contract.py::test_csharp_packet_parser_combines_matching_reveal_and_picker`
projects classes 51, 23, and 70 together for the choosing player and checks
that the opponent receives none of them. It passes those projected event bytes
to `tests/csharp_oracles/verify_reveal_picker_combine.sh`, which compiles this
original packet rewrite and the relevant original event readers against small
value and base-class stubs. The C# assertions cover the class-23 parent match,
source-card match, retained authored target slot, and resulting
`AdditionalTargets`; this does not exercise the Unity scene or a network
connection.

ObjFmt emits the class-255 packet's declared `NetworkPacketSessionEventArgs`
members in order (`PlayerId`, `EventIds`, `EventData`), followed by inherited
`SessionEventArgs` members (`Class`, `SessionId`). Numeric ObjFmt values use
uppercase hexadecimal. Focused empty-ack and class-70 packet fixtures compare
the Python envelope byte-for-byte with the extracted C# `ObjFmt.cs` encoder.
This verifies serialization, not Unity's receipt or recipient privacy.

The full session-entry sequence is driven by `GameClient`: find/join an
existing session (22019/22021), start a normal session (22015), or start an
encounter (22017); then receive typed responses, add the players, and send
ReadyToStartGame (22031), ReadyForGameSetup (22027), and ReadyForGameEvents
(22029) at the corresponding boundaries. The client decodes these service
responses through `DataWrapper.FromBytes<T>` in `GameSessionService` or
`LoadBalancerService`. The 22027 response reaches
`GameClient.OnReadyForGameSetup`, which installs both players and turn order
before `Session.StartGame`; the server's 3053 `GameStartedEventArgs` service
event is separate from nested 3055 class 1 `GameStartedSessionEventArgs`.
22029 is a fire-and-forget readiness signal: normal sessions do not receive a
typed reply, while tournament PvP waits until both players send it before
publishing the game start packets.
For reconnect, `TryReconnectionToDisconnectedGame` (22013) and
`JoinDisconnectedGame` (22023) receive typed responses; tournament PvP then
uses `ReadyToContinueGame` (22025), which is fire-and-forget and receives no
typed response; its 3055 reconnect snapshot is the only resulting packet.
`QuitOnReconnectionGame` (22011) is also fire-and-forget and has no response.
The server records a 22011 decline per player in that session's checkpoint;
later 22013 probes return no reconnect offer to that player, while other
participants remain eligible. Reconnect snapshot events arrive through 3055
after the session is restored.

| Live flow | Request or event classes | C# consumer and expected next client state | Recipient and reply policy |
|---|---|---|---|
| Session setup | Load Balancer/GameSession 22019, 22015, 22017, 22021, 22031, 22027, 22029; service event 3053 `GameStartedEventArgs`; session events 1, 47, 50, 64, 65, 82, 83 | `GameClient.OnFindSession`, `OnStartSession`, `OnStartEncounter`, `OnJoinSession`, and `OnReadyForGameSetup` create the session and install the players/turn order. `GameSessionService.GameStarted` dispatches 3053; nested class 1 reaches `ClientSessionBase` and `UIBattle.OnGameStarted`. `OnSkipSetup` (82) advances the setup presentation and `OnDisabledInterface` (83) controls input. Deck/player/card cache is populated before setup animation and phase states. | 22019/22015/22017/22021/22031/22027 receive typed responses. 22029 and 3053 are one-way signals/events. 3055 setup packets carry a per-viewer `PlayerId`; public setup is sent to both players, while hidden card identity is viewer-filtered. |
| Coin choice and opening hand | 3029 `ChoosePlayTransaction`, `ChooseDrawTransaction`, `AcceptStartingHandTransaction`, `MulliganTransaction`; events 3, 5, 6, 58–60 | `UIBattle.OnTurnPhaseUpdated` pushes `BattleStatePickGoesFirst` or `BattleStateMulligan`; handlers `OnPlayerWishesToPlayFirst`, `OnPlayerWishesToDrawFirst`, `OnFirstPlayerDictated`, `OnHandMulliganed`, and `OnAcceptedStartingHand` update choice/dialog state. | Choice/mulligan state is public; each private hand/card representation is sent only to its authorized viewer. Every handled 3029 request is completed by a 3055 packet, event-bearing or empty. |
| Priority and phase options | 3029 `PassPriorityTransaction`, `SetTurnPhasesTransaction`, `RequestPrioritySyncTransaction`, `SetAutoPassTransaction`, `CancelAutoPassTransaction`; events 3, 48, 70, 79 | `ClientSessionBase` records phase and priority; `UIBattle.OnTurnPhaseUpdated` enters the corresponding phase state, `OnGreenLight` grants/withdraws local priority, and `OnWaitingOnPlayer` presents the wait state. Class 70 updates cached legal uses before `OnPlayerOptionsUpdated` refreshes selection and the active state. | Phase/priority/wait events are public; class 70 is private to the player whose legal options it describes. SetTurnPhases and stale/no-op requests still need an empty 3055 when they emit no state events. Auto-pass requests are allowed through the client's ordinary transaction gate. |
| Card play and zone changes | 3029 `PlayChampionTransaction`, `PlayTroopTransaction`, `PlayResourceTransaction`, `PlayArtifactTransaction`, `PlaySpellTransaction`, `DiscardTransaction`; events 7, 8, 10–12, 14–19, 24–26, 43–50, 54, 64 | `UIBattle` maps them to `OnCardDrawn`, `OnCardDestroyed`, `OnCardDiscarded`, `OnCardVoided`, `OnCardGraveyarded`, `OnChampionCardPlayed`, `OnTroopCardPlayed`, `OnResourceCardPlayed`, `OnSpellCardPlayed`, `OnSpellCardCast`, `OnArtifactCardPlayed`, `OnCardTapped`, `OnCardUntapped`, `OnCardPrimed`, `OnEncounterCardsCreatedInZone`, `OnCardTransformed`, `OnCardReverted`, `OnEquipmentItemSet`, `OnDeckCreated`, `OnCardCollectionsMerged`, `OnCardMoved`, `OnCardCountersChanged`, and `OnCardUpdated`. Class 64 refreshes or creates the cached card representation before its view is updated. | Public board/zone changes go to both viewers. Class 64 for a hidden hand/deck card is projected per viewer; the complete `CardUpdated` must precede an event that references a newly created card. Event-bearing 3055 is the requester's completion signal. |
| Ability activation, targets, and triggers | 3029 `ActivateAbilityTransaction`, `ActivateTriggeredAbiliesTransaction`, `SetAbilityActivationDataTransaction`; events 22, 23, 39, 41, 42, 51, 70, 77, with nested option classes 66–69 | Class 70 populates `ClientStateCache.Options`; classes 66–69 are parsed as nested cost/target/option records. `UIBattle.OnAbilityPushedOnChain`, `OnAbilityActivationDataRequested`, `OnTriggeredAbilityActivationDataRequested`, `OnTopOfChainResolved`, `OnRemovedTopOfChain`, `OnAbilityCancelled`, and `OnCardsRevealed` drive chain animation, selection states, and resolution cleanup. Class 77 is parsed but has no `ClientSessionBase` or `UIBattle` message handler in this client; it does not itself choose the next UI state. The client submits the chosen activation/continuation as another 3029 request. | Player options, activation-data requests, and private reveals are delivered only to the choosing player; public chain/resolution events go to both. The event packet that opens a continuation also releases the preceding request; its answer then requires its own 3055 completion packet. |
| Attack, block, and damage | 3029 `CommitTroopsToAttackTransaction`, `CommitTroopsToDefenseTransaction`, `AssignDamageOrderTransaction`; events 3, 27–32, 48, 61–63, 64, 70 | Class 3 selects `BattleStateDeclareAttackers`, `BattleStateDeclareBlockers`, or `BattleStateAssignDamage` through `UIBattle.PushStateForPhase`. `OnAttackDeclared`, `OnBlockersAssigned`, `OnBeginCombatResolution`, `OnCombatPhaseResolved`, `OnEndCombatResolution`, and `OnCombatRemoved` animate combat; `ClientSessionBase` updates its combat cache from classes 61–63 before forwarding. | Combat and phase state are public; class 70 remains private. A handled attack/block/damage transaction completes with the event-bearing 3055 for its sender. |
| Resource and player HUD state | Events 33–38, 65 | `UIBattle.OnPlayerCurrentResourcePoolChanged`, `OnPlayerTotalResourcePoolChanged`, `OnPlayerResourceThresholdChanged`, `OnChampionChargePointsChanged`, `OnChampionSpellPointsChanged`, and `OnChampionHealthChanged` update HUD state. Class 65 first updates `ClientStateCache`/`PlayerRepresentation` and the session's player fields, including ChampionId and resource thresholds. `ClientSessionBase` and `PlayerRepresentation.Update` both consume `Thresholds[i]` paired with `ThresholdValues[i]`; Python must send both lists in matching order and length or the client silently drops threshold values. | Player/HUD values are public in the current two-player game. The event packet's recipient remains explicit in the class-255 envelope. |
| Reconnect | 22013 `TryReconnectionToDisconnectedGame`, 22023 `JoinDisconnectedGame`, and tournament 22025 `ReadyToContinueGame`; events 53, then the relevant 47, 50, 64, 65, 3, 48, and 70 snapshot events | `GameClient.OnTryReconnectionToDisconnectedGame` marks the reconnect probe complete; `GameClient.JoinDisconnectedGame` creates the session, then `OnJoinDisconnectedGame` attaches the player. `ClientSessionBase` routes class 53 to `UIBattle.SyncReconnectDone`. Earlier cache/player/card events rebuild references, then phase, priority, and private options restore the correct state. | 22013/22023 receive typed responses. 22025 receives none; the 3055 snapshot is its only response. Snapshot packets are per player. Rebuild valid player/champion and card cache entries before phase events or any event that references those IDs. Reconnect's final event marks the UI synchronization boundary. |

Class 57 (`CardPutInHandSessionEventArgs`) is a client-supported event path, but
the current Python event model has no class-57 constructor or emitter. Current
put-into-hand projections use card update/move events; class 57 is therefore
not included in the active server event list above.

### Concrete 3029 transaction paths

The table enumerates the concrete transaction types declared by
`Transaction.cs` and the additional compiled client request types used by
`LocalPlayer` or encounter UI. The additional types are not in that base
class's `KnownType` list, but the server has explicit classifiers for them.
The Python route follows `classify_player_transaction` through
`normalize_player_transaction` and the RulesPort host. `DebugCheatTransaction`
uses the server's debug handler; `NonsenseTransaction` is intentionally
acknowledged and dropped.

| Client transaction type | Python dispatch and outcome | C# client state/event result | Completion and recipient |
|---|---|---|---|
| `ChoosePlayTransaction` | `is_choose_pick` -> `choose_play_first`; records play-first and advances setup. | Class 59 records the choice; opening-hand/mulligan events and class 3 select the next phase. | No 3029 response wrapper. Requester receives event-bearing or empty 3055; public choice events go to both players. |
| `ChooseDrawTransaction` | `is_choose_pick` -> `choose_draw_first`; rotates the selected player behind the other and advances setup. | Class 58 updates player order; opening-hand/mulligan events and class 3 select the next phase. | Same setup completion policy; private hand contents remain owner-only. |
| `AcceptStartingHandTransaction` | `is_mulligan_keep` -> `accept_starting_hand`; enters the kept-hand/start-turn path. | Class 6 confirms the kept hand; phase/priority events select the first gameplay state. | Requester receives event-bearing or empty 3055; no 3029 wrapper. |
| `MulliganTransaction` | `is_mulligan_redraw` -> `mulligan`; replaces the opening hand and remains in setup. | Class 5 and private draw/card updates refresh the hand; mulligan state stays active until accepted. | Requester receives event-bearing or empty 3055; hand data is owner-only. |
| `PassPriorityTransaction` | `is_pass_priority` -> `pass_priority`; validates the submitted phase and drives the current priority action/chain. | Classes 3/48 select the next phase and priority; class 70 refreshes private legal options when needed. | Requester receives event-bearing or empty 3055, including stale/no-op passes. Public phase/chain events go to both; class 70 is private. |
| `RequestPrioritySyncTransaction` | `is_priority_sync` -> `request_priority_sync`; reprojects the existing window without changing rules state. | Classes 3/48 and phase-appropriate class 70 restore the current battle state and legal actions. | Requester receives that event 3055, or an empty 3055 if no battle window exists. |
| `SetTurnPhasesTransaction` (compiled client type) | `is_set_stops` -> `set_turn_phases`; persists self/opponent stop preferences and may resume an AI phase that is no longer a stop. | No immediate `UIBattle` state transition; preferences control later phase stops. | No 3029 wrapper. Empty 3055 releases the ordinary Mono request gate when no event packet was sent. |
| `SetAutoPassTransaction` (compiled client type) | `is_set_auto_pass` -> `set_auto_pass`; records the passing state and routes it through priority. | Later classes 3/48 reflect the resulting phase or priority window. | Mono exempts this type from its request gate; any 3055 is sent to the requester, with public phase state sent to both players. |
| `CancelAutoPassTransaction` (compiled client type) | `is_cancel_auto_pass` -> `cancel_auto_pass`; clears the player's auto-pass preference. | No immediate state change; later priority events follow normal phase rules. | Mono exempts this type from its request gate; any 3055 is sent to the requester. |
| `RequestPlayerOptionsTransaction` (compiled client type) | `is_request_player_options` -> `request_player_options`; rebuilds legal options for the current phase. | Class 70 refreshes `ClientStateCache.Options`; `UIBattle.OnPlayerOptionsUpdated` refreshes selection. | Class 70 is private to the requester and arrives in a 3055 packet. |
| `PlayResourceTransaction` | `is_play_resource` -> `play_resource`; validates the resource action and applies pool/threshold changes. | Class 16, applicable classes 34/35, and class 64/card movement update board and HUD; class 70 refreshes legal actions. | Requester receives event 3055; public resource state goes to both, hidden card identity remains viewer-filtered. |
| `PlayTroopTransaction` | `is_play_troop` -> `play_troop`; validates cost/ability data, pays, and resolves the card. | Class 15 and class 64 place/update the troop; chain/trigger events precede the next class 3/48/70 window. | Requester receives event 3055; public board/chain state goes to both, private picker/options/reveals stay with the chooser. |
| `PlayArtifactTransaction` | `is_play_artifact` -> `play_artifact`; validates cost/ability data, pays, and resolves the card. | Class 19 and class 64 place/update the artifact; chain/trigger events precede the next class 3/48/70 window. | Same card-play completion and visibility contract as `PlayTroopTransaction`. |
| `PlaySpellTransaction` | `is_play_spell` -> `play_spell`; validates cost/targets, pays, and resolves the spell. | Classes 17/18, chain classes 22/23/41/42, and effect events lead to the next class 3/48/70 window. | Requester receives event 3055; resolution is public, picker/options/reveal data is private. |
| `PlayChampionTransaction` | `is_play_champion` -> `play_champion`; validates and applies the champion play. | Class 14 and class 64 initialize/update the champion view and player cache; class 3/48/70 establish the next state. | Requester receives event 3055; public champion state goes to both. |
| `ActivateAbilityTransaction` | `is_ability_activate` -> `activate_ability`; a persisted choice/discard checkpoint can reclassify it as a continuation. | Class 22 opens the chain; class 23 may open target/cost selection; classes 41/42 and 3/48/70 restore the next action state. | Requester receives event 3055; chain is public, class 23/70 and private reveal data go only to the chooser. |
| `ActivateTriggeredAbiliesTransaction` | `is_activate_triggered_abilities` -> `activate_triggered_abilities`, or resumes a persisted trigger/deck-search continuation. | Class 39 opens trigger selection; class 22 pushes accepted instances; classes 23/51 may continue a private picker; 41/42 finish resolution. | Requester receives event 3055; chain changes are public, trigger targets/reveals/options are private. |
| `SetAbilityActivationDataTransaction` | `is_set_ability_data` -> `set_ability_activation_data`, or the typed choice/trigger/discard continuation selected from persisted state. | Completes the open class-23 picker and resumes its chain item; classes 41/42 and phase/priority/options events select the next state. | Requester receives event 3055; picker and selection options stay private to the chooser. |
| `DiscardTransaction` | `is_discard` -> `discard`; moves the selected card or resolves a hand-limit discard. | Classes 10/64 update the hand/graveyard; class 3/48/70 update phase and actions when the discard window ends. | Requester receives event 3055; card identity and hand updates are viewer-filtered. |
| `ReadyCardTransaction` | `is_ready_card` -> `ready_card`; clears tapped/combat state through the mode projection. | Classes 25/64 refresh the card; phase is unchanged unless the action advances it. | Requester receives event-bearing 3055; successful no-event projections receive an empty 3055. |
| `CommitTroopsToAttackTransaction` | `is_commit_attack` -> `commit_troops_to_attack`; validates attackers, declares combats, and consumes declaration priority. | Class 27 and combat classes 62/63 update attack lines; class 3/48 enter response/blocker state and class 70 supplies private actions. | Requester receives event 3055; combat events are public and options private. |
| `CommitTroopsToDefenseTransaction` | `is_commit_defense` -> `commit_troops_to_defense`; validates blockers and consumes defense priority. | Class 28 and classes 62/63 redraw combat lines, followed by class 3/48 and combat resolution. | Requester receives event 3055; blocker/combat state is public. |
| `AssignDamageOrderTransaction` | `is_assign_damage` -> `assign_damage_order`; validates/stages order and resolves or advances the damage phase. | Class 29 and damage/death events update combat; classes 30/31/32 and class 3/48 leave combat and select the next phase. | Requester receives event 3055; combat and damage state is public. |
| `EncounterModDialogTransaction` (compiled encounter UI type) | `is_encounter_mod_dialog` -> `encounter_mod_dialog`; forwards the conversation ID to the encounter handler. | The matching class-55 prompt pauses the ability chain; this typed answer resumes that continuation rather than selecting a normal priority state. | Requester receives event-bearing or empty 3055; no 3029 response wrapper. |
| `QuitGameTransaction` | `is_quit_game` -> `quit_game`; tournament PvP uses `pvp_concede`, other sessions use the game-end projection. | Class 2 `GameEnded` marks `UIBattle.GameOver`, collapses battle states, leaves the room, and queues inactive priority. | Requester receives event 3055; game-end state is public. |
| `DebugCheatTransaction` | `is_cheat` -> `_handle_debug_cheat_transaction`; privileged debug handling is outside player-action normalization. | Result depends on the debug operation; any board/phase changes use their corresponding events. | Debug handler sends event-bearing or empty 3055 to requester; no 3029 response wrapper. |
| `SendGameStateChecksumTransaction` | `is_state_checksum` -> `send_state_checksum`; C# marks it defunct, so the default port treats it as diagnostic-only. | No gameplay state transition. | Requester receives an empty 3055 if no event packet was sent; no 3029 response wrapper. |
| `TipWindowClosed` | `is_tip_window_closed` -> `tip_window_closed`; accepted as a no-op acknowledgement. | Tip animation/dialog closes; battle phase is unchanged. | Requester receives an empty 3055 if no event packet was sent; no 3029 response wrapper. |
| `NonsenseTransaction` | No RulesPort classification or mutation; the unclassified boundary drops the intent. | No state transition. | Requester receives an empty 3055 so the client gate is released; no 3029 response wrapper. |

After a successful RulesPort handler, the server checks whether `sendall`
delivered a 3055 on the requester's connection. If no event packet or handler-
owned empty sync was sent, it sends an empty 3055. This covers valid no-event
intents and successful projections that published no event, without adding a
duplicate acknowledgement. Fire-and-forget means there is no 3029
`PlayerTransactionResponse`; the 3055 session packet releases the client's
ordinary one-request gate.

Additional client consumers include class 2 `GameEnded`, which runs
`UIBattle.OnGameEnded` and moves the battle to its terminal inactive state,
and class 80 `ShowTip`, which runs `UIBattle.OnShowTip` and queues a tip
animation. Class 4 `ChessTimerUpdated` runs `UIBattle.OnTimerUpdated` and
updates the indicated player's timer, but the current Python server has no
call site that emits it, so it is not a current outbound flow. Classes 79 and
82/83 are covered by the priority/setup flows above. Event classes marked
`Available` in the catalogue are understood by the client parser but are not
currently constructed by the Python event model.

The RulesPort event packet builder is `domain/game.py:Game.make_network_packet`;
it stamps the recipient and applies private-hand, underground-card, secret
counter, and other viewer filters before serializing events. Session event
history records both `event_class` and `target_player_uid` in
`session_events`. Persisted saved traces through 2026-09-30 contain classes 1, 3, 5–7, 14–19, 22,
23, 27–31, 33–36, 38, 41–42, 47–48, 50, 53, 58–60, 62, 64–65, 70, 77, 79,
and 83. Each class maps to the C# parser above and to the consumer listed here;
class 62 carries class-63 combat records, while class 65 and nested option and
combat records first affect the session cache rather than owning an independent
`UIBattle` handler. This is a static
source and saved-capture inventory. A correlated current Unity trace and
real-client acceptance are tracked separately by U2 and U10.

### Event ordering rules

The client treats event order as meaningful. A typical play/ability sequence
is:

1. `GreenLight` and a private `PlayerOptionList` establish who may act.
2. The client sends a transaction containing the selected card/ability and
   any target/cost selections.
3. The server emits destination `CardUpdated` state and the semantic
   play/draw/discard events required by the C# lifecycle. When collection or
   controller changes, `ClientStateCache.Update(CardUpdated)` derives the UI
   `CardMoved`; do not also send a wire class-50 event for that transition.
   The cache does not derive class 50 for position-only changes, while
   `UIBattle.OnCardUpdated` still moves the view home. The inspected C# source
   has no separate class-50 producer, so do not add one for an inferred reorder
   without a concrete client path that requires it.
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
representation. A destination `CardUpdated` supplies or refreshes the full
representation and mutable fields. If its collection or controller differs
from the cached representation, the client cache derives an internal UI
`CardMoved` before forwarding the update. A separate wire class-50 event is
not synthesized for same-collection position changes; `UIBattle.OnCardUpdated`
still moves the view home. This checkout has no separate C# class-50 producer,
so require a concrete client path before adding one. On reconnect, the server
republishes enough player, card, zone, and priority state to rebuild that
projection.

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
