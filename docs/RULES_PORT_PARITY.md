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
The checked-in `hex-private/HexClient/Assembly-CSharp-firstpass/` disassembly describes
session, transaction, and event handling; `hex-private/HexClient/Assembly-CSharp/UIBattle.cs`
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

The reference for this checkout is the `hex-private/HexClient/` C# disassembly plus the
matching structured data in `hex-private/Records/`. Use `RULES.md` for documented private
server decisions. The original C# project cannot currently be rebuilt from
this checkout. An experimental Unity-hosted adapter can now run one
metadata-selected ability in the original loaded runtime and export its
initial/final state, ordered event trace, and RNG draws. It uses a synthetic
two-player fixture; it does not yet import Python state, submit the full
transaction family, or compare the result automatically with Python.
`rules_port/parity.py` still compares saved captures rather than producing a
C# run. The adapter therefore enables focused C# evidence, but does not prove
universal parity or close a task without matching Python scenarios and the
required Unity acceptance evidence. See
[`CSHARP_HEADLESS_ORACLE.md`](CSHARP_HEADLESS_ORACLE.md) for its runbook and
limits.

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

## Rare and legendary card sweep (2026-10-05)

The Records snapshot contains 1,911 Rare and 578 Legendary card templates
(2,489 total). A generated, contract-only Python sweep exercised
metadata-selected ability, effect, target, choice, trigger, and selected static
modifier scenarios:

| Rarity | Templates | Passed | Failed (incl. timeouts) | Timed out (subset) |
| --- | ---: | ---: | ---: | ---: |
| Rare | 1,911 | 1,755 | 156 | 6 |
| Legendary | 578 | 518 | 60 | 0 |
| Total | 2,489 | 2,273 | 216 | 6 |

All six timeouts are Rare templates: two each for Claws of the Dead,
Spellstorm Titan, and Enthralling Gaze. The run passed 60 more templates than
the initial contract baseline (2,213/2,489), and 17 more than the previous
full run (2,256/2,489). The remaining failures classify as authored trigger
staging or dispatch (80), missing typed effect traces (60), zone or cost
postconditions (37), static projection/effect assertions (27), nested
activations (5), timeouts (6), and summon postconditions (1). Choice or
continuation failures fell from nine to zero.

The full-catalog contract run used a 30-second per-template timeout:

```bash
python3 -u tests/tests_card_play_matrix.py --rarity Rare --rarity Legendary \
  --contracts-only --card-timeout 30
```

The current and previous runs used the same 30-second per-template timeout and
temporary seeded database. Failure-family counts describe contract assertions,
not distinct engine bug counts.

These are triage signals from synthetic Python fixtures, not a count of cards
known to be broken. Some scenarios may not stage all the state required by the
authored condition or target, and this run did not compare each case against a
C# oracle trace or exercise the Unity client. Per-card failures still need
classification against the typed Records metadata and C# call path before they
can be called parity defects. The sweep validates the paths it can stage; it
does not prove every card branch or client interaction.

### Choice-contract follow-up (2026-10-05)

The choice fixture now distinguishes a `choice_zone_target` selection from the
built-in choose/play (`double_choice`) path. A target selection follows its
authored child effect to the requested zone; it is not required to end in
`PlayedResources`. The choose/play path checks that zone at the play boundary,
before later effects can move the card again. The selected card's ability check
now matches the C# automatic-ability definition (not manual, no trigger event,
no indexed activation, and at least one effect); triggered abilities remain
with native event dispatch. The RulesPort choice play uses the selected card as
the ability source and retains its originating source link for authored source
target behavior.

Focused contracts passed after this correction for Entrath Settlers (4/4
templates, 22 card abilities, 4 invoked child abilities, 50 typed effects, 44
choice branches) and Corners of the World (1/1 template, 1 card ability, 2
typed effects, 9 choice branches). The full Rare/Legendary rerun passed
2,273/2,489 templates and exercised 238 choice branches. It also reduced the
zone/cost and summon fixture failures compared with the previous full run.

The parity repairs from this sweep are generic and metadata-driven. Static
modifier fixtures now evaluate authored inputs (including a legitimate
zero-valued no-op) and check supported immunity/block-restriction projections.
`CounterVariable` evaluation now includes synthetic champion objects and their
counters, matching the C# player-collection traversal. `RequiresTotalResources`
now follows the C# temporary-resource flag, highest-opposing-resource rule,
and player-filter semantics. Contract fixtures also stage legal stored-card
targets, relative card/resource counts, and source-keyword witnesses from the
authored filters. Focused contracts passed for Vaarician, Brilliant Annihilix,
Smitebringer, Dark Pact Demon, Blizzard, and Scarcliff Chimera.

The earlier sweep also uncovered two `RequiresCardsControlled` mismatches,
now corrected: C# includes an opponent with zero matching cards when computing
the lowest opposing count, and preserves authored `m_RequiredQuantity=0` for
conditions such as “you control no Candlekin.” The focused condition fixture
covers both cases, distinct cost/name/race counting, quantity filters, and the
highest opposing count.

The contract checker now honors per-ability resolver application outcomes and
nested `NotContingentAbilityCondition` traces when an authored leaf is correctly
skipped. It still requires a trace for every applicable leaf. Its zone-move
postcondition accepts a same-zone controller change when Records requests a
control transfer, and draw postconditions compare persisted card moves with the
count returned by the native draw operation, including zero. The fixture also
stages combat state from authored filters and records sacrifice and discard
costs in their corresponding ability lists. This fixed Professor Sporzin's
follow-up effect: its `SourceBuriedTargetTemplate` reads `SacrificedCards`, so
the prior fixture's generic `BuriedCards` entry left the typed effect without
its sacrificed Shroomkin.

Focused verification passed for the zero-quantity condition regression,
Professor Sporzin's four template variants (12 abilities, 30 typed effects,
and 10 choice branches), Saarthu's four Rare/Legendary templates, and 14 draw
contract templates across five cards. The full sweep remains synthetic Python
coverage; it does not compare every case against a C# oracle trace or exercise
the Unity client. Remaining failures need per-case classification against
typed Records metadata and the C# call path before being called parity defects.
The earlier focused contracts for Beseech the Ancients, Slaughtergear's Reaver,
Oath of Valor, Gas Troll, Jenson the Joymaker, Commander P.R.O.M.P.T., Nori,
and Egg Hunt remain part of the existing evidence.

## Ordered work list to close the parity gap

These are open engineering tasks, ordered so each step supplies evidence for
the next. Existing protocol catalogues, server fixtures, and individual client
traces are useful partial evidence; they do not complete a task until its done
condition is met.

- [x] **U1 — Map live client flows to C# handlers.** For every live setup,
  phase, card, ability, target, trigger, combat, and reconnect flow, record the
  client state, request/event class, C# serializer/parser, C# session or
  `UIBattle` handler, recipient, reply policy, and expected next UI state.
  Extend `docs/CLIENT_SERVER_PROTOCOL.md` where its event catalogue lacks the
  consumer or state transition. **Done when** every live transaction and
  emitted event has a source-to-client path and no used class has an unknown
  response policy. Static C# paths, current server dispatch, recipient rules,
  reply contracts, all 24 base `KnownType` transactions plus five additional
  compiled client transaction classes, and saved event classes are inventoried
  in `docs/CLIENT_SERVER_PROTOCOL.md`. A source audit compared the 79 nested
  `SessionEventArgs.BuildArgs` class IDs with its event catalogue; every ID is
  listed, with abstract class 0 and envelope class 255 documented separately.
  The RulesPort boundary now checks successful transaction completion against
  actual 3055 sends and supplies an empty sync when an accepted intent produced
  no packet. `tests_u3_wire_contract.py` now drives a successful, no-event
  `TipWindowClosed` through the 3029 dispatcher and verifies exactly one C#
  empty 3055, alongside the explicit SetTurnPhases and no-op PassPriority
  acknowledgement checks. The correlated Unity action is captured under U2;
  broader packet-family and repeatable-client acceptance remain U3/U10 work.
  The current source callsite audit corrects two catalogue statuses: native
  counter effects emit class 54 through `rules_port/counter_effects.py` and
  `Game.push_card_counters_changed`, while PvE/PvP conversation prompts emit
  class 55 through the shared conversation continuation. Class 57 has a C#
  client handler but no Python event model or emitter, so it is documented as
  client-supported rather than active server output.
- [x] **U2 — Make one correlated trace for a client action.** Capture the raw
  3029 request, decoded typed intent, pre/post RulesPort and SQLite state,
  ordered 3055 event classes and recipients, response/ack count, and the
  matching Unity exception and UI state-stack lines. Correlate them by session
  and transaction. **Done when** a failing report can be followed from the
  Unity action through the server mutation and back to the Unity handler in one
  trace bundle. Current partial capture stores the exact 3029 DataWrapper
  body, its decompressed inner payload, classifier fields, pre/post state
  hashes, and per-event bytes/recipient for replayable PvP sessions. New
  opt-in capture records the decoded intent, complete
  pre/post persisted session-and-card snapshots, in-memory RulesPort
  snapshots, projected 3055 packet batches (including empty acknowledgements),
  and their request/transaction IDs. After `sendall` succeeds, the handler
  marks the matching 3055 batch with its raw DataWrapper bytes and send time.
  Per-effect card snapshots also include `original_gems`, so transform and
  reversion deltas retain the newly persisted active-gem state.
  Direct 3055 sends without a matching event projection are kept as send-only
  rows. The bundle reports successful `sendall` count, projected batches with
  no matched send, and send-only rows separately. PvP remains captured by default;
  `HEX_CAPTURE_SESSION_TRACES=1` also includes practice and encounter sessions.
  `rules_port.sql_capture.capture_session` exposes these records.
  `python3 -m rules_port.trace_bundle` packages one transaction, its linked
  3055 batches, and the matching Unity submission/UI/error lines;
  trace-level Unity logs join on C# `m_TransactionId`, with a clearly marked
  time-window fallback when that log line is absent. The focused
  `python3 tests/tests_rules_port_sql_capture.py` check passes for request and
  state snapshots, projected and send-only 3055 rows, successful-send counts,
  transform gem-state before/after projection, and transaction/UI-log bundle
  correlation; its fixtures are synthetic. C#
  `SessionClient` emits the submission marker at Trace level. A source audit
  found that the production 3029 hook passed `raw_data_wrapper_bytes`, but the
  recorder accepted only `raw_request_body`; its best-effort exception handler
  therefore dropped the transaction snapshots. The recorder and focused test
  now use the production keyword. This repairs future captures but cannot
  recover requests that were already dropped. If an uncaught 3029 handler
  exception unwinds before normal completion, the request-local capture now
  closes its transaction row as `error`, stores the exception, and attempts a
  persisted post-state snapshot without masking the original exception. The
  focused capture fixture checks that failure path.
  On 2026-10-04, an Immortal Practice game supplied the correlated client
  action: session `234230`, capture transaction row `24252`, and C# transaction
  ID `4` join exactly. The raw compressed 3029 DataWrapper and decoded
  `PlayResourceTransaction` identify Shards of Fate (card UID `2560257`);
  complete pre/post SQLite and RulesPort snapshots and state hashes are saved.
  The request completed without error and moved the card from hand into
  `PlayedResources`. Its linked 3055 request 80 targets the acting player,
  contains 60 ordered events (classes 16, 33, 34, 36, 39, 48, 64x53, and 70),
  and has exactly one successful send, with no unmatched projection or
  send-only packet. Unity's Trace-level submission marker joins by transaction
  ID; client acceptance passes with no correlated error lines and the expected
  ordered UI states `BattleStateMainPhase`, `BattleStatePlayCard`,
  `BattleStateTriggeredAbilities`, `BattleStateAssignTargets`, and
  `BattleStateTargetCoverflow`. This follows the server mutation through
  Unity's target picker; the picker continuation and resolution are tracked
  separately under U5. The full bundle is
  [`experimental/captures/u2_live_playresource_20261004.json`](../experimental/captures/u2_live_playresource_20261004.json).
  The run used disposable database `/tmp/hex_u2_session_20261004_1.db`.
  For another practice/encounter trace, enable `HEX_CAPTURE_SESSION_TRACES=1`,
  note the session and transaction row IDs, and set `HEX_DB_PATH` to a
  disposable scratch path because importing `rules_port.trace_bundle`
  initializes the package database before parsing `--db`:

  ```bash
  HEX_DB_PATH=/tmp/hex_trace_tool_import_scratch.db \
    python3 -m rules_port.trace_bundle --db /tmp/hex-session-capture.db \
    --session-id <session_uid> --transaction-row-id <row_id> \
    --unity-log "/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Hex_Data/output_log.txt" \
    --output /tmp/rules-port-trace.json
  ```
- [ ] **U3 — Gate the packet and acknowledgement contract.** Add focused
  golden checks for the active 3029 request shapes, 3055 envelope and nested
  event bytes, and the 3053 game-start response. Verify event class IDs, field
  order, enum/UID types, recipient privacy, and one response per transaction.
  Check both empty 3055 acknowledgements for handled requests with no sync and
  no invented replies for client fire-and-forget requests. **Done when** the
  golden checks cover every live transaction/event family and a Unity client
  accepts the packets without handler or decode errors. Initial coverage now
  routes the server's service-level 3053 encoder through
  `encoder.encode_game_started_response` and checks exact goldens for its body
  and an empty class-255 3055 acknowledgement, plus five existing phase,
  combat, and card nested-event streams in `tests/tests_u3_wire_contract.py`.
  The empty acknowledgement and representative class-70 3055 envelope are
  compared byte-for-byte with the extracted C# `ObjFmt.cs` oracle.
  Active 3029 PassPriority and manual activation requests now have binary
  fixtures generated by the extracted client `ObjFmt.cs` under Mono. The
  activation fixture carries a selected SessionCardId at TargetMap slot 2 and
  verifies the decoder and RulesPort ingress preserve both. A `SetTurnPhases`
  fixture checks the two ordered `List<ETurnPhases>` fields, transaction ID,
  DataWrapper ID, and request session GUID through the Python decoder and
  classifier. C#-generated fixtures for `ChoosePlay`, `ChooseDraw`,
  `Mulligan`, and `AcceptStartingHand` also check request IDs, session GUIDs,
  transaction IDs, concrete transaction types, and Python classifier results;
  these opening-flow requests are decoded but not executed through a live
  setup. C#-generated `PlayResourceTransaction` and `PlayTroopTransaction`
  wrappers also verify their concrete classes, transaction IDs, typed card
  IDs, and the troop's `PlayingForFree` field through Python normalization.
  These fixtures do not execute a card play. The same oracle
  generates the service-level 3053 `GameStartedEventArgs` body, which the
  Python encoder now compares byte-for-byte against. That comparison caught
  and fixed the 3053 field order and uppercase numeric encoding. The small
  oracle DTOs carry the
  same `[DataMember]` names/types as the client declarations; the focused test
  checks the DataWrapper request ID, session GUID, compression byte, request
  class, transaction ID, and phase enum through the Python decoder and
  classifier. Regenerate it with
  `bash tests/csharp_oracles/generate_wire_goldens.sh`. A representative
  class-70 `PlayerOptionList` event now includes nested class 69/68/67/66
  card, ability option, target and cost records. The focused Python fixture is
  parsed by the extracted C# `Serializer.cs` and original event readers using
  `bash tests/csharp_oracles/verify_player_option_list.sh`; wire-value,
  non-option dependency, and focused base-dispatch stubs replace the
  Unity-bound base plumbing. The five option readers are compiled unchanged.
  It checks target
  index 2, target/cost IDs, SessionCardIds, and option/cost enums. These remain
  serializer/parser oracles, not Unity network captures. The fixture now also
  checks the synthetic routing rule: the chooser receives class 70 and the
  opponent receives none. The class-255 check independently runs the extracted
  C# ObjFmt encoder over the Python event bytes; it does not compare class 70
  with a C#-written event payload. That distinction matters because the client
  `Serializer.EndWrite()` returns `MemoryStream.GetBuffer()`: nested C# event
  writers length-prefix allocated buffer capacity (and its zero padding), while
  Python length-prefixes the bytes actually written. The original C# readers
  accept the compact Python fixture and read the same fields, but exact
  class-70 C# writer byte identity is not established. The Python class-44
  and class-45 transform/revert payloads are now also parsed by the original
  extracted C# event readers; the focused assertion checks class IDs, session
  and card UIDs, ResourceIds, replica state, and the 64-bit gem enum. More transaction
  types and client acceptance remain open.
  The extracted `NetworkPacketSessionEventArgs.Get` and
  `CombineRevealAndChoose` implementations now have a separate composite
  fixture containing classes 51, 23, and 70 in one packet. The Python
  `Game.make_network_packet` projection is the fixture source: it asserts all
  three events reach the choosing player together and none reach the opponent.
  The extracted C# parser checks the ability-parent match, source-card match,
  target slot, removal of the reveal event from the logical batch, and addition
  of only the revealed non-legal card to `AdditionalTargets`; an unrelated card
  option is unchanged. This guards the server packet boundary and client
  packet-level picker rewrite, but does not certify Unity's rendered picker or
  a live transaction/resolution.
  A deterministic Python class-1 `GameStartedSessionEventArgs` fixture is now
  read by the extracted C# event reader. It checks player order, champion names
  and template IDs, sleeves, divisions, seeds, and series fields; it validates
  the setup event payload shape without exercising setup state transitions.
  A deterministic class-65 `PlayerUpdatedSessionEventArgs` fixture is also
  read by the extracted C# event reader. It verifies member order, typed
  ChampionId, sleeve and visibility fields, resources, and the six shard keys
  parallel to their threshold values. This caught an empty Python `Thresholds`
  list: Unity's `ClientSessionBase` applies `ThresholdValues` only while
  iterating that key list, so it had silently ignored all six values. The
  shared `Game.push_player_updated` projection now sends the ordered shard
  keys. A lifecycle event fixture now compares Python payloads with the
  extracted C# writers and parses them through the original event constructors
  for classes 5, 6, 53, 58, 59, and 60. It checks player/session UIDs, the
  mulligan replacement count and accepted-hand flag, and the forced-first
  event payload; it does not exercise a live setup or reconnect. This is parser
  and wire-shape coverage; live resource refresh and
  reconnect acceptance remain open. The class-35
  `PlayerResourceThresholdChangedSessionEventArgs` now has a separate focused
  contract: its Python bytes are compared against the extracted C# writer,
  then parsed by the extracted C# reader with the Blood shard enum, Remove
  operation, signed delta, new value, session UID, and player UID asserted.
  The synthetic public event packet is also projected to both players. The
  focused test is `test_csharp_threshold_event_writer_and_reader_match_python_class_35`
  in `tests/tests_u3_wire_contract.py`; it does not replace live client
  acceptance. The neighboring resource-change events for current resources
  (33), total resources (34), champion charges (36), and champion spell points
  (37) now have the same extracted C# writer/reader byte and field checks.
  Their synthetic packets are asserted public to both players. These grouped
  contracts live in `test_csharp_resource_pool_event_writers_and_readers_match_python`.
  Class 16 `ResourceCardPlayedSessionEventArgs`, which appeared in the correlated
  Shards of Fate Practice packet, now has an extracted C# writer/reader check
  for its player UID, typed SessionCardId, and `PlayingForFree` flag; its
  synthetic event is also asserted public to both recipients. Classes 3
  `TurnPhaseUpdatedSessionEventArgs`, 48 `GreenLightSessionEventArgs`, and 22
  `AbilityPushedOnChainSessionEventArgs` now have paired exact payload checks.
  The focused fixture compares Python bytes with the extracted C# writers,
  dispatches the payloads through the original C# readers, and asserts phase
  and context enums, participant UIDs, the 64-bit priority timer, ordered
  target SessionCardIds, costs, flags, and ability ResourceId. It also confirms
  that all three events reach both synthetic viewers in the same order. The
  same batch is wrapped in 3055 and compared byte-for-byte with the extracted
  C# ObjFmt encoder using the client-declared envelope member shapes. The
  single focused test passes; live phase-state acceptance remains part of U10.
  Class 64 `CardUpdatedSessionEventArgs` now has an extracted writer/reader
  comparison covering its ordered stat block, typed card/controller/template
  IDs, enums, ability/counter maps, gem and threshold values, related cards,
  modifier dictionaries, and trailing flags. Its public Warzone representation
  is also asserted in both recipient packets by
  `test_csharp_card_updated_event_writer_and_reader_match_python_class_64`.
  The representative class-70 event is also wrapped in 3055 and compared
  byte-for-byte with an extracted C# ObjFmt oracle. This caught a mismatch in
  derived-member/inherited-member order and lowercase numeric hex in the Python
  envelope; `encoder.encode_sync_event` now matches the oracle. The focused
  suite also checks the five existing Python 3055 phase, combat, and card stream
  goldens. This verifies serializer output only; live client acceptance,
  recipient privacy, and the remaining event families are open.
  Classes 7, 8, 10, 11, 12, 14–19, and 50 now have deterministic Python event
  goldens compared with the extracted C# writers and parsed by the original C#
  readers. The batch covers draw ordinal, destroyed/responsible card IDs,
  champion name/AI fields, troop/resource/spell/artifact play events, free-play
  flags, and moved-card destination/index fields. Packet projection confirms
  this public event sequence reaches both players, and each recipient's full
  class-255 3055 is compared with extracted C# ObjFmt output. Building the
  batch also exposed five public event types missing from the `game_engine`
  compatibility facade; all are now re-exported and identity-checked. These
  are serializer and routing-fixture checks, not live Unity animations.
  The focused `python3 tests/run_all.py --only tests_u3_wire_contract.py` run
  now passes after updating only the combat-window and combat-damage stream
  goldens for the six serialized shard keys in `PlayerUpdated`. The C# reader
  harnesses that share `SessionEventArgs.BuildArgs` now compile the extracted
  class-65 reader, so their dispatch stubs match the real class map. This
  closes the current focused fixture run; it does not close live-client,
  privacy, or remaining event-family acceptance criteria.
  Focused route checks now exercise `SetTurnPhases` and a setup no-op
  `PassPriority`; each sends exactly one empty 3055 matching the C# fixture.
  A successful `TipWindowClosed` bookkeeping transaction also runs through the
  3029 dispatcher and confirms that its no-event completion receives exactly
  one empty 3055 from the generic guard. A focused rejected `PassPriority`
  case now forces RulesPort to reject the request, asserts the old transaction
  dispatcher is never called, and checks for exactly one C# empty 3055. The
  dead transaction fallback block guarded by a literal false flag has been
  deleted from `hconnect_server.py`; unsupported native intents reach the
  existing fail-closed acknowledgement path directly.
  They also drive the no-session 22011 path, the first PvP 22029 readiness
  signal, and the 22025 reconnect path with snapshot output stubbed, asserting
  no unsolicited typed reply. The C# ObjFmt oracle now emits fixtures for all
  28 transaction shapes currently recognized by `PlayerTransactionCommand`,
  including card plays, discard, manual/triggered continuation, attack,
  defense, damage assignment, auto-pass, concede, encounter-mod, checksum,
  and debug requests. The Python matrix decodes each wrapper and verifies its
  transaction type, ID, discriminant, and authored member labels; applicable
  RulesPort normalization is also checked. This exposed that nested C# `CombatId`
  records (`Attacker` plus `SerialNumber`) were being discarded as non-scalar
  values, turning a populated damage order into an empty one. The decoder now
  constructs the typed RulesPort `CombatId`, and the fixture asserts its full
  identity and ordered card IDs. A focused run of
  `test_csharp_objfmt_client_request_family_matrix` passes across the current
  28 recognized transaction shapes. The oracle fixtures remain serializer and
  ingress checks for the broader family: request execution, nested event
  families, recipient privacy, and reply counts for all routes remain open.
  The correlated Shards of Fate Practice captures add live acceptance evidence.
  U2 transaction 4 was joined by its client transaction ID, passed the ordered
  phase/play/trigger/target UI-state checks, and had no correlated client error;
  its single 3055 batch contained classes 16, 33, 34, 36, 39, 48, 70, and 53
  class-64 updates. U5 transaction 5 also passed its transaction-ID and
  `BattleStateMainPhase` checks with no correlated client error; its first
  59-event 3055 batch contained classes 3, 35, 48, 64, 65, 70, and 77. The
  same transaction produced four successful sends total: that batch, a
  class-48 batch, an empty acknowledgement, and a final class-70/65/48 batch.
  Together these runs exercise the
  resource, threshold, card/player projection, trigger, picker, phase, and
  priority families through Unity. Other active 3029 routes, 3055 event
  families, privacy cases, and reply counts remain uncovered, so U3 remains
  open. On 2026-10-05, the focused U3 module exposed a packet fixture being
  read after its temporary directory was removed and several C# verifier build
  scripts omitting client event readers referenced by their shared
  `SessionEventArgs.BuildArgs` stub. The packet comparison now reads the C#
  output before cleanup, and the verifier scripts compile those concrete
  readers. `python3 tests/run_all.py --only tests_u3_wire_contract.py` passes;
  this repairs the focused harness run but does not expand the covered live
  packet families or close U3.
  The wire suite now covers combat events 27–32 as one family: attacker and
  blocker SessionCardIds, ordered blockers, the 64-bit CombatId serial, the
  reconnect flag, and both FirstStrike and Standard enum values are compared
  against extracted C# event writers and readers. The Python event sequence is
  public to both recipients, and each recipient's combined 3055 is compared
  with the extracted C# ObjFmt encoder. The focused combat contract passes;
  this verifies serialization and routing, not the combat UI flow in U8.
  A chain lifecycle contract now covers classes 41, 42, and 77 using one
  shared ability-instance ID across `TopOfChainResolved` and
  `RemovedTopOfChain`, followed by `ChainEmpty`. It compares the original C#
  writers/readers and the complete 3055 envelope delivered to both recipients.
  This validates the packet fields and public routing; trigger order, priority
  settlement, and rendered chain state remain U6/U10 acceptance work.
  The active class-39 triggered-ability request from the saved Practice trace
  now has a C# writer/reader contract for paired ability-instance IDs, template
  ResourceIds, source SessionCardIds, and the requesting player UID. The event is
  asserted private to that player, and its one-event 3055 envelope is compared
  with the extracted ObjFmt encoder. This covers the wire and privacy contract,
  not the Unity triggered-picker interaction or its continuation.
  Class 65 `PlayerUpdated` now compares the deterministic Python fixture with
  both the extracted C# writer and reader, including ordered threshold keys
  and values, the typed champion SessionCardId, remaining time, visibility
  flags, sleeve ResourceId, and spell points. Client cache acceptance for
  initial setup and reconnect remains U4/U10 work.
- [ ] **U4 — Close setup, identity, and reconnect flows.** Exercise setup from
  `ReadyForGameSetup` through the service-level 3053 GameStarted response and
  the 3055 `GameStartedSessionEventArgs`, valid player/champion/card cache
  entries, coin-flip/first-player choice, mulligan, and the first normal
  priority window. Repeat with either player winning the toss and after a
  reconnect. **Done when** client logs show the expected UI state progression,
  every referenced `SessionCardId` resolves, and the client and server agree on
  phase, active player, and priority. An earlier Unity log snapshot showed
  `BattleStatePickGoesFirst` -> `BattleStateMulligan` -> `BattleStateMainPhase`
  and later `BattleStateGameOver`, without a matching server capture. The
  current log no longer contains those state lines: it records the
  restart-window connection abort and client shutdown described under U2.
  Neither snapshot supplies the server packets or transaction IDs needed to
  close setup parity. The extracted C# class-1 reader now accepts a deterministic
  Python `GameStartedSessionEventArgs` fixture and verifies player order,
  champion and sleeve IDs, divisions, seeds, and series fields. This adds
  parser coverage
  for the setup event; the both-winner cases, cache references, mulligan, and
  reconnect still lack a correlated live server/client run. A focused
  PvP reconnect regression now starts with a checkpoint that has no
  `champ_map`, restores both typed champion IDs from authoritative champion
  rows, and checks the `PlayerUpdated` and matching `CardUpdated` events arrive
  before a `StartTurn` phase event. If either champion UID is missing or has
  type 0, reconnect aborts before sending a malformed snapshot. This follows
  C# `ClientSessionBase` / `PlayerRepresentation.Update`, which stores the
  `PlayerUpdated` ChampionId, and `UIBattle.OnTurnPhaseUpdated`, which indexes
  the card cache by that ID. It is a focused server regression; a real
  reconnect and Unity cache acceptance trace remain open. A focused
  RulesPort setup test now drives both initial player orders through both
  play-first/draw-first transactions and checks the resulting order, active
  player, event class, and transition to Mulligan. This covers native
  transaction semantics only; it does not exercise mode setup, accepted hands,
  first normal priority, or reconnect. A second focused test now calls the
  tournament PvP `handle_ready_for_game_setup` adapter for both participants,
  decodes both 22027 bodies, and checks that each reports the same persisted
  first player in the expected turn order. This covers the PvP service adapter's
  coin-winner contract; it does not execute the following 22029 broadcast or
  3053/3055 setup packets. C# source review also exposed two mulligan order
  mismatches: the active first-turn player, rather than necessarily the coin
  winner, starts; and redrawing retains priority until AcceptStartingHand.
  The PvP path now uses that active player, re-announces the shared Mulligan
  phase on keep handoffs, and leaves priority with the redrawer. A focused test
  checks the lifecycle decision and both recipients' active/priority event.
  Practice/PvE now starts mulligan with the C# active player, resolves an AI
  first hand before handing priority to the human, and retains that setup
  result through the human keep transaction. Both mode adapters preserve the
  accepted-hand `Mulliganed` flag; redraw to zero auto-accepts and passes
  priority as in C#. A focused event-sequence fixture checks both active-player
  orders, and a mocked AI deck fixture checks that a redraw is followed by one
  accepted-hand event carrying the mulligan flag. It does not exercise real
  AI valuation or the full PvE adapter. Setup packet delivery, first normal
  priority, reconnect, and a correlated Unity/server trace remain open. Unity
  treats the toss animation and Play/Draw choice as separate flows:
  `UIBattle.OnGameStarted` starts the animation unless the game is on Reins;
  its starting player comes from class-1
  `GameStartedSessionEventArgs.TurnOrder[0]`. Class 60 invokes
  `OnFirstPlayerDictated`, which sets `m_CoinFlipSkip` outside tutorials, so a
  normal random toss result must not use that event. The human's Play/Draw
  picker comes later from the active-player `PickGoesFirst` phase and
  `GreenLight`; tutorials can choose automatically and `SkipSetup` suppresses
  that picker. If the AI wins in Practice/PvE, the server chooses Play/Draw
  and sends the corresponding choice and mulligan events without a human
  choice transaction. The PvE adapter now publishes the toss winner in class-1
  `TurnOrder` and omits class 60 for that random result; the focused
  `test_pve_coin_toss_uses_turn_order_without_forced_first_signal` regression
  passes for both winner orders and parallel champion/sleeve arrays. Existing PvP
  class-60 production remains unchanged. Reconnect also skips the initial
  animation. U4 still needs a correlated trace of the 22027 `TurnOrder`, 3053
  `TurnOrderPlayerIds`, class-1 `TurnOrder`, class 60 only for forced-first
  cases, and subsequent phase/choice events; neither 3053 nor class 1 has a
  `PlayerFirst` field.
  The class-65 extracted C# writer/reader fixture additionally confirms the typed
  `PlayerUpdated` ChampionId and six paired threshold entries consumed by
  `ClientSessionBase`. It verifies the wire contract, but does not prove that
  Unity accepts either initial setup or reconnect snapshots. U3's focused
  lifecycle event fixture also checks C# writer/reader parity for mulligan,
  accepted-hand, play/draw choice, forced-first, and reconnect-complete events;
  these remain serializer checks without a live setup/reconnect trace.
  The pregame compatibility entry point now delegates to the RulesPort setup
  resolver instead of retaining a second legacy effect walk. A focused setup
  case exposed unstable SQL ordering: Zodiac could add a card before Great
  Spore Beast checked the same starting deck size, changing a 99-card setup
  into the 100-card branch. RulesPort now preserves the supplied ability order
  and evaluates the authored deck-count predicate against the initial deck
  snapshot. `tests/tests_pregame_talents.py::test_skylak_uses_original_deck_size_for_both_talents`
  passes for both 99- and 100-card starts. C# trigger registration is consumed
  by its assigned registration index; this focused setup result still lacks a
  complete mode-adapter and Unity setup trace.
- [ ] **U5 — Close options, plays, and activation pickers.** Start from the
  server's `GreenLight` and `PlayerOptionList`, then exercise accepted and
  rejected card/ability transactions. Cover class 23 activation data, class 39
  triggered-ability data, additional costs, multiple targets, and nested
  prompts. Preserve authored `AbilityTargetIndex` slots through decoding and
  continuation; verify that a paused ability resumes once and its parent is
  not replayed. **Done when** every picker state reaches its expected selection
  state and returns to the right parent/priority state in both PvE and PvP. On
  2026-10-04, the Immortal Practice capture closed one Shards of Fate PvE path:
  the class-39 `ActivateTriggeredAbiliesTransaction` (C# transaction ID 5,
  capture row 24253) resumed child ability
  `8168d9c0-9b56-2f88-22c0-56cd8b9616b9`, instance 1, with target slot 0 set to
  Blood Shard UID `15617`. Its pre/post RulesPort snapshots show Blood 5 to 6,
  with one class-35 threshold event. Four 3055 batches each have one successful
  send. Unity accepted the matching transaction, returned to
  `BattleStateMainPhase`, and logged no correlated client error. The bundle is
  [`experimental/captures/u5_live_shards_continuation_20261004.json`](../experimental/captures/u5_live_shards_continuation_20261004.json).
  The C# source/oracle audit identifies the authored
  parent target as `You` (`PlayerTargetTemplate`, represented by champion
  SessionCardId plus player UID) and the nested child target as a Standard
  resource in the owner's Deck. The earlier v3/v4 synthetic C# captures show
  two Blood threshold gains, but the headless adapter had mistakenly converted
  both legal deck candidates into selected targets even though the child
  target's authored maximum is one. The rebuilt adapter now separates
  candidate and selected maps and records nested selections; a corrected C#
  replay remains pending. This fixture issue does not change the live
  RulesPort target map. A C# generated class-23 fixture also verifies that one
  selected SessionCardId at target
  slot 2 survives ObjFmt decoding and RulesPort ingress normalization; it does
  not exercise resolving
  that activation or returning through a live Unity picker. The native
  `test_activation_reply_binds_the_waiting_instance_then_finishes_push` check
  also verifies that the class-23 reply binds to the waiting ability instance
  and does not repeat its payment when the push action completes. It is a
  scheduler fixture, not a real picker replay. At the time of the user's
  initial report that Shards seemed fixed, no correlated transaction trace had
  been retained; the later live trace above now establishes that one Shards
  continuation resolved once. The native class-39 fallback
  now follows C# `PushAbilityOnChain`: nonoptional selected triggers with
  remaining activation data enter the prompt-before-push action, while complete
  activations proceed directly. The focused scheduler fixture covers a
  remaining option prompt, a class-23 continuation, one payment, the original
  target slot, and the parent instance. A focused run of
  `test_triggered_activation_waits_for_nested_picker_before_payment_and_push`
  passes this synthetic continuation path. It does not cover multi-trigger UI
  ordering or reconnect. The class-70 option fixture parses target and cost
  subrecords through the extracted C# reader. A focused run of
  `test_csharp_packet_parser_combines_matching_reveal_and_picker` passes; the
  extracted C# parser checks the batched class-51/class-23/class-70 rewrite,
  source/parent linkage, target slot, and additional target. Those packet and
  scheduler fixtures do not validate Unity UI by themselves; the live Shards
  trace above validates only that one target flow. Multi-target, nested, PvP,
  reconnect, and other picker families remain open.
- [ ] **U6 — Close chain, trigger, and priority presentation.** For one
  authored trigger of each event family, compare C# publication timing and
  payload with Python discovery, queueing, and resolution. Verify the same
  chain instance IDs across persisted state and `AbilityPushedOnChain`,
  `TopOfChainResolved`, `RemovedTopOfChain`, and `ChainEmpty`; verify both
  players see the correct response window. **Done when** each tested trigger
  resolves once, receives the correct priority window, and leaves Unity's
  chain/UI state settled. Source comparison found that C# sends
  `EPriorityContext.ResolveTopOfChain` from `PriorityWindowAction.PostUpdate`
  when a chain item is pending; native Python chain pushes now project that
  context after `AbilityPushedOnChain`; the focused
  `test_manual_ability_finish_projects_chain_entry_and_green_light` run passes
  and asserts that event order.
  C# also keeps registered trigger listeners in session-wide Current/Previous
  event maps. Python candidate discovery now scans both owners' current card
  collections for each broadcast trigger and lets Records collection flags
  and conditions filter the candidates. Focused Records contracts exercise a
  Void listener on an opposing troop's CardEnteredZoneEvent and an automatic
  Void listener on TurnStartedEvent. The focused zone-transfer regression
  still rejects a trigger when the moved card came from the wrong player's
  discard, and the shared collection-mask check covers Simulacrum's C# rule.
  `python3 tests/tests_trigger_parity.py` passes its eight focused checks,
  including the 45-event Records inventory, zero-to-positive int-attribute
  publication, phase-exit hook ownership, Simulacrum collection exception,
  and mutation-before-chain-completion ordering for PvE/PvP owner models.
  `test_native_chain_resolves_trigger_without_legacy_fallback` also passes for
  one synthetic PvP champion `TurnEndedEvent` trigger through discovery,
  queueing, and native resolution while the old ability resolver is patched to
  fail if called. It checks the resulting deck-card update, not response
  priority. The existing AI card-push fixture compares the class
  22 instance ID with the saved chain descriptor. The focused
  `_chain_mutation_events_precede_client_completion_in_both_owner_models`
  assertion now checks that a resolved and removed card item both carry that
  same instance ID and that the last item emits exactly one `ChainEmpty`, in
  both owner models. These are separate card and trigger fixtures, not one
  correlated trigger trace. RulesPort chain dispatch now uses the explicit
  mode `chain_dispatch` hook or calls `dispatch_native_trigger` directly; it
  no longer probes `_dispatch_game_trigger` as a compatibility fallback.
  HCP's native forwarding method is now named `_dispatch_native_trigger`. The
  focused `test_rules_port_chain_dispatch_skips_legacy_trigger_hook` verifies
  the old hook is not called. The chain-order fixture resolves a card item, so
  it does not stand in for trigger-item event order. One authored scenario per
  trigger family, same-trigger push/resolve/remove ID correlation, response
  priority for both players, and Unity's rendered chain state still need
  acceptance traces.
- [ ] **U7 — Close card/player projection and visibility.** Exercise draw,
  reveal, transform, token creation, discard, destroy, zone move, resource and
  threshold changes, counters, and hidden-card updates. Verify full
  `CardUpdated` representations precede events that reference new cards,
  collection changes do not send redundant `CardMoved` events that the C#
  client derives from `CardUpdated`, and private identities reach only
  authorized viewers. Repeat after reconnect. **Done when** Unity's card cache,
  HUD, zones, and visibility match the authoritative server state at each
  checkpoint. C# `ClientStateCache.Update(CardUpdated)` hydrates the card
  representation before deriving the UI `CardMoved` notification, and
  `UIBattle.OnCardMoved` immediately resolves the card view by SessionCardId.
  Draw, deck-search, top-of-deck-to-hand, PvP draw, mulligan, and choice-card
  creation/cleanup/resolution now rely on the client cache to derive
  `CardMoved` from destination `CardUpdated`; redundant class-50 events were
  removed for those transitions. Focused assertions check the draw,
  deck-search, mulligan, choice creation/cleanup, generated Verdict, and PvP
  draw event streams.
  The RulesPort and live mode projections now avoid a second class-50 event
  when a destination `CardUpdated` changes collection or controller. This
  source audit covered native effects plus `hconnect_server.py`, `ai.py`, and
  `services/tournament_game.py`; it removed paired moves from Practice/PvE
  draws, plays, costs, encounter additions, and PvP draws, discards, plays, and
  zone projections. The C# client cache synthesizes class 50 only when
  collection/controller changes; `UIBattle.OnCardUpdated` also moves the view
  home, and a source search found no separate C# class-50 producer. RulesPort
  same-collection sends were removed from `context.py` and
  `transform_effects.py`; the Gearsmith fixture now asserts the returned Deck
  cards have `CardUpdated` representations without wire class 50, and a generic
  transform fixture asserts the transformed/updated events contain no class 50
  for a same-collection update. The ordinary
  game path still has one explicit move: the pregame deck-seeding rehome for
  gem-bearing cards in `hconnect_server.py`, an existing Unity workaround that
  still needs live-client acceptance. `commands.py` chat extensions and
  `debug_cheats.py` have separate entry points. The client-backed
  `DebugCheatTransaction` now uses destination `CardUpdated` for add/move
  projections and nulls cards added to Deck; a focused fixture checks those
  event streams. Private `!top`, `!discard`, and `!move` commands now follow
  the same client projection contract, with Deck updates nulled; their focused
  fixture checks update order and absence of a duplicate class 50. Other `!`
  commands remain server extensions rather than C# transaction paths.
  `domain.Game.step` is a scripted sample path with no runtime caller, and
  `GameEngineEventSink.card_moved` has no production caller. `NativeEffectBackend` sets
  `_rules_port_native_effect`, and the live zone, token, transform, and death
  paths dispatch to `rules_port/*`; the `abilities/framework` movement calls
  in `EffectContext` are guarded legacy fallbacks. The C# client also only
  applies `State` from a nulling `CardUpdated`; C# `Card.CanPlayerSeeCard` and
  `Card.IsCardCollectionFaceDown` keep a `Choosing` card's full identity with
  its controller. The Python packet boundary now evaluates visibility per
  recipient for champions, public collections, Deck, Hand, Underground,
  Choosing, and other private collections, including when a producer omitted
  the Nulling flag. Deck reveals require an explicit viewer-scoped projection;
  Hand and Underground use the corresponding active permission; Choosing is
  controller-only. A trusted native chain reveal is limited to a valid full
  representation outside Underground with zero Tunneling, and its placeholder
  suppression is scoped to the same viewer and collection. The client
  `AcceptedStartingHand` part of C#'s chain exception is not independently
  rechecked at packet time; startup-chain acceptance remains open under U4.
  Nulling updates clear template-derived identity and stats, preserve public
  Underground counters, and apply the existing secret-counter filter. The
  top-of-deck projector now appends a scoped full event even if a shared
  refresh already queued an unscoped representation. One focused matrix
  assertion checks full updates accidentally queued for hidden collections,
  controller and permission visibility, public collections, chain reveals,
  top-deck privacy, and viewer scoping; a focused run of
  `test_packet_visibility_matches_csharp_collections_and_reveal_scopes` passes.
  `UIBattle` also consumes
  `PlayerUpdated.CanSeeEnemyUnderground`; visibility refresh derives and emits
  that flag from active Records-backed player attributes, including
  revocation. PvP reconnect now refreshes the same Records-backed flag before
  `PlayerUpdated`, seeds Underground membership for both players, and appends
  viewer-scoped full updates for authorized Underground visibility. The
  focused assertion checks owner, unauthorized opponent, and permission-based
  reconnect projections; the same run passes owner, unauthorized-opponent,
  and permission-based reconnect packet checks. The native draw, deck-search, and top-deck-to-hand
  paths now send destination `CardUpdated`; ordinary draws then send
  `CardDrawn`. The cache derives the corresponding UI move. The focused
  regression checks these streams, mulligan's old Deck/new Hand updates, and
  choice-zone cleanup. The Verdict fixture checks generated Choosing cards
  without a redundant class-50 event. The source audit also covered
  death/discard/void, return-to-hand, generic zone moves, generated-spell
  transitions, and mode adapter projections; same-collection reorder events
  no longer emit explicit class-50 events from RulesPort. These checks cover
  emitted Python events, not Unity's rendered result. A source comparison of
  C# `Session.TransformCard`/`TransformIntoCopy` against RulesPort found that
  Python dispatched `CardTransformsEvent` after mutation and attached
  collection snapshots that the C# `TriggerEvent` does not carry. The native
  transform path now dispatches that event before mutation, dispatches
  `CardTransformedEvent` after the update, and preserves the source collection
  for `TransformIntoCopy` through `transform_card_to_target`. Focused
  `test_native_same_collection_transform_emits_no_wire_card_moved` now asserts
  trigger/mutation/projection order and no class 50; the new
  `test_native_transform_to_target_preserves_source_collection` checks the
  target-copy contract. Direct transforms now also preserve the current
  collection by default, matching `Session.TransformCard`. Class-44/45
  transform/revert identity is scoped from the pre-mutation card type and zone
  using the C# `CanPlayerSeeCard` rules, including controller and hand/underground
  permissions plus the authorized top-deck case. The focused
  `test_native_transform_event_uses_pre_transform_viewer_visibility` checks
  hidden-hand redaction, hand permission, and top-deck permission delivery.
  The focused U3 class-44/45 C# reader fixture also validates the emitted
  identity, template, replica, and gem fields against the extracted client
  event constructors. Direct transform now clears only per-game ability-use
  counts while retaining per-turn use and cooldown data; reversion clears all
  three as `Card.Revert` does. The focused
  `test_card_transform_reset_and_category_gates_match_csharp` checks both use
  reset boundaries and the C# Troop/Action/Resource category gates. It does not
  yet assert the full class-45/reset/class-44 state order. The
  `TransformIntoCopy` persistence helper now copies
  active gems, card integer variables, and escalation count for both regular
  and replica copies; regular copies also copy damage and use counts and add
  counters and damage shields, following the persisted subset of
  `Mechanics/Card.cs:CopyFrom`. Its focused
  `test_copy_transform_copies_supported_csharp_mutable_card_fields` checks
  those fields. It cannot yet copy/rebind the C# active `AbilityEffectInstance`
  graph, ability activation-data entries, session boon maps, extended-card
  state, or total-resources-spent value; Python has no equivalent persisted
  fields for several of these. It also cannot yet reproduce `CopyEffects`'
  copyable-versus-retargeted effect distinction. Type-category transitions
  now follow the C# `RevertCard` gates before the outer transform, but still
  need a dedicated order/state regression. Direct-template gem socket filling,
  alternate-template selection, irreversible original-template changes, and
  cleanup/recreation of card-created and chain abilities need further parity
  work. RulesPort now captures the pre-transform active gems in nullable
  `game_cards.original_gems` and restores that value on reversion. Null is the
  unset marker so the valid `1 << 62` empty-socket value can also be preserved.
  Rows already transformed before this migration remain unset because their
  earlier value is not recoverable from the stored card row; reversion leaves
  those rows' current gem value unchanged.
  `test_native_same_collection_transform_emits_no_wire_card_moved` checks the
  transform/revert call and event order plus the restored value;
  `test_persisted_original_gems_survive_transform_and_revert` checks one-time
  capture and the SQLite mutation helpers. The focused
  `python3 tests/run_all.py --only tests_effect_context.py` run passed. Direct
  transforms still do not port `TransformCardBase.SetActiveGems`/
  `GenerateFillerGems`. Actual Records-backed
  listener outcomes, all discard/destroy branches,
  the broader mulligan setup flow, PvE reconnect, and live Unity acceptance
  remain open.
- [ ] **U8 — Close phase and combat UI flows.** Drive attack, blocker, damage
  assignment, combat resolution, end phase, and automatic phase-entry actions
  through the real client. Assert one `TurnPhaseUpdated` per transition,
  correct `GreenLight`/options, matching combat IDs, and no second transaction
  from a duplicated auto-entered UI state. **Done when** both attack and
  defense directions complete without client exceptions, stale buttons, or
  server/client phase disagreement. Native evidence compares the permitted
  phase graph against every parsed C# state and runs the original C# combat
  declaration primitive against the Python combat manager. The focused
  `test_phase_stop_packet_announces_each_phase_once` regression also checks the
  server packet boundary that previously repeated `TurnPhaseUpdated` and
  caused Unity's `BattleStateAssignDamage` entry action to submit a second
  damage-order transaction. The AI no-blocker path sends an empty typed
  `CommitTroopsToDefense` declaration through RulesPort, matching
  `DeclareDefenseState.OnExit()` calling `Combat.DeclareBlockers(null)`;
  `test_empty_defense_commit_consumes_native_declaration_window_once` checks
  the empty combat state and that the native phase advances once; its focused
  regression passes. These are source and headless checks; attack and defense
  still need real-client state,
  transaction, and error-log traces.
- [ ] **U9 — Complete semantic coverage behind those flows.** For the open
  rows below, finish typed Records field audits and generated rule-family
  scenarios for effects, conditions, variables, target/filter subclasses,
  built-in rules, and interactions. Assert state deltas and ordered events,
  including boundary/rejection cases. Reuse fixtures across PvE and PvP; do
  not add one hand-maintained test per card. **Done when** every concrete C#
  class and meaningful current Records field is native, intentionally staged,
  or documented as an explicit gap, with semantic acceptance for the claimed
  behavior. Current `rules_port.coverage_report()` has no class-inventory drift:
  it lists 12 native transactions, 15 native-plus-projection transactions,
  2 legacy-only transactions, 82 native effect templates, and 96 native
  filters. These are registration counts; they do not close the semantic
  scenario gaps described here. An opt-in generated runner in
  `tests/tests_card_play_matrix.py` builds a broad typed board and sends each
  selected Records card through the native play/trigger path. Its `--contracts`
  mode also traces every authored ability graph, checks typed effect
  postconditions, and resumes each generated choice branch. This replaces
  hand-maintained per-card fixtures. A bounded `--all --limit 5 --contracts
  --card-timeout 15` run passed five cards, seven abilities, and eight typed
  effects. The run first exposed a Juurdin trigger amount mismatch: Python's
  authored default map masked the active ability instance's `DamageDealt` TAC.
  The generic typed-variable lookup now follows C# `CreateAbility` (which
  appends the trigger event TAC) and `IntAttrAbilityVariable.GetValue` (which
  reads that active TAC); the focused runtime parity check covers a live value
  alongside the authored zero default. This bounded run and focused check close
  this one semantic gap only. The full catalog and trigger-family acceptance
  remain open. The fixture uses the shared PvE-style owner model, so PvP
  adapter scenarios remain separate. An initial bounded sample,
  `--all --limit 25 --contracts --card-timeout 15`, passed 22/25 play and
  22/25 contract scenarios and exposed three fixture gaps: a missing
  `SourceCardHasCounters >= 20`
  precondition, no candidate for `DragonsInYourHand`, and a direct Poltergeist
  resolution that did not stage its Void listener or the opposing troop's
  death. The generator now seeds positive source-counter gates, subtype-based
  targets, positive `IntAttrFilter` target requirements, and supported
  `CardEnteredZoneEvent` conditions from typed Records metadata. Focused
  reruns passed Tormented Puzzle Box (1/1), Tome of Uruunaz (3/3 variants), and
  Horrific Poltergeist (3/3 variants, eight abilities, 13 typed effects). The
  Poltergeist contract requires native discovery to find its listener in Void
  for both an opposing Warzone-to-Discard event and an automatic start-of-turn
  event. The same bounded 25-card sample
  then passed 25/25 play scenarios and 25/25 ability contracts, covering 37
  abilities and 52 typed effects. This sample does not certify the full
  catalog, every target/filter family, PvP adapters, or Unity presentation.
  A source differential of spell completion found that C#
  `Session.MoveSpellAfterFinishedResolving` reads the current card type after
  effects: a card still in CastSpells moves to Warzone if it is now a troop,
  artifact, or constant. Python's old chain finalizer always sent such a card
  to Discard. The shared Python finalizer now re-reads the current template,
  applies the Warzone entry state and exhaustion rule, projects the new type,
  and dispatches the ordered zone-exit/zone-entry triggers. It leaves a source
  already moved by an effect in that zone. The focused chain scenario changes
  a generated action into a troop during resolution and verifies the effective
  type controls its destination; the Runic Growth matrix sample also passes
  2/2 templates, five abilities, and nine typed effects after its fixture
  cleanup was corrected to preserve effect-selected zones. This closes the
  final-destination rule only. Both mode hosts now apply Warzone arrival
  state and stamp the persistent resolve-order counter when the finalizer
  sends a transformed permanent into play; the PvP host uses the same
  bookkeeping for ordinary troop arrivals. A focused adapter check covers
  those PvP state changes, but a paired PvP resolution and matched Unity trace
  remain open.
  The Unity-hosted adapter now adds a focused C# oracle command for one
  metadata-selected card ability. It captures the original engine's
  deterministic fixture state before resolution, after resolution, and as an
  ordered event/RNG trace. This makes C# behavior inspectable for targeted
  U9 investigations. The Python fixture is not yet generated from or loaded
  into the adapter, and the command does not drive every ability through its
  real transaction/trigger path; it is supporting evidence, not U9 semantic
  acceptance. See the oracle runbook and the U11 ledger entry below.
  Charge-power cost now follows the C# source-card context: champion-source
  activations include that champion's runtime context value, while troop or
  other card sources use only their own effective context. A focused kernel
  scenario gives the champion and a troop different modifiers and checks the
  resulting payment plans. It does not yet resolve the Records-authored,
  duration-bound modifier abilities through PvE and PvP or verify their
  resulting Unity updates, so this closes only the source-selection gap.
  An earlier expanded bounded sample, captured with
  `--all --limit 100 --contracts --card-timeout 15`, passed 92/100 card
  scenarios and 92/100 authored ability-contract groups, covering 146
  abilities, 227 typed effects, and 10 choice branches. Its eight reported
  failures at that time were Constantina, Cerberus, Pippit Pal, Brood Baron,
  Runic Growth, Subtle Striker, Tricky Tykes, and Rust and Ruin. The report
  identifies unprojected `IntAttr`/`DamageMultiplier` modifiers, a missing
  Brood Baron effect trace, unresolved Runic Growth effect traces, an
  event-specific Tricky Tykes branch, and a computed Rust and Ruin token
  amount. A focused Rust and Ruin rerun traced that amount report to the
  generated board: C# `CardCountAbilityVariable` reads the opposing Discard,
  while the fixture's artifact and constant were in Warzone; the contract also
  treated base `m_Amount=1` as final even when typed `m_AmountField` overrides
  it. The generated fixture now stages witnesses from the variable's typed
  filter and collection fields and checks tokens against the resolved amount
  field. `--only 'Rust and Ruin' --contracts --card-timeout 15` passes both
  reprints (4 abilities, 5 typed effects). This corrects that fixture and
  postcondition only; at that point the 100-card sample had not been rerun, and
  no matching C# dynamic-value capture existed. The same bounded run reported
  Constantina because its `CantBeExhaustedByOpponent` aura was not represented in the
  Python card-context IntAttr projection. A shared Records-backed IntAttr
  projector now applies C# `Double`/`Add`/`Remove`/`Set` semantics, and the
  generated Constantina contract verifies that an opposing exhaust is blocked
  while the controller's exhaust remains allowed. The focused run
  `--only 'Constantina' --contracts --card-timeout 15` passes all 8/8 card
  variants, 12 abilities, and 12 typed effects. At that point the 100-card
  sample had not been rerun after these changes. This remains a bounded
  PvE-style sample, not PvP or
  Unity acceptance. Focused contracts now pass
  all four The Buffalord reprints and Depleted Bumblebot after two shared
  parity fixes: template `IsColor` now derives `CardShards` from authored
  thresholds, and native `SourceCardHasCounters` preserves an authored zero
  instead of replacing it with one. Their Unity projection and PvP behavior
  remain unverified.
  The run also closed two concrete gaps from the prior sample. The Harbinger
  of Hastur `TopNOfDeck` trigger now passes both reprints after the condition
  context began exposing the current card pool, persisted deck positions, and
  active Sapphire threshold state to Records filters. A SummonToken effect
  authored with `m_CardCollection=None` now keeps its generated card in the
  None collection until its following `MoveCardToZone` effect; this restores
  the authored creation-then-move sequence. The focused None-collection test
  checks that creation emits no premature visible card update, and the
  generated Blessing contract checks the paired summon and zone move. Both
  changes are recorded as partial in the U11 ledger pending PvP and Unity
  acceptance.
  A focused Cerberus run then exposed a real typed-operand bug: its passive
  `DamageMultiplierModifier` has `m_InputValue=EffectConstant(3)` while the
  deprecated normalized amount is zero. C# `NumericModifier.GetValue` uses
  `m_InputValue` when present; `_literal_leaf` now follows that precedence.
  The generated static contract checks the projected rule and both combat and
  noncombat multiplier outputs. `--only 'Cerberus' --contracts
  --card-timeout 15` passes both templates (2/2 cards, 3 abilities, 4 typed
  effects). At that point the broader 100-card sample remained unrerun.
  Pippit Pal's reported `AdditionalResourcesPlayableOnYourTurn` failure was
  also in the generated contract: its `PlayerTargetTemplate` was being checked
  as a card target even though `player_int_attributes` already projects the
  continuous player modifier and `resource_play_limit` consumes it. The contract
  now checks both player owners, the authored player filter, and the resulting
  resource allowance. `--only 'Pippit Pal' --contracts --card-timeout 15`
  passes both templates (2/2 cards, 4 abilities, 4 typed effects). No
  production rules change was needed for this finding; PvP and Unity acceptance
  remain open. A focused Records audit of Brood Baron then exposed two native
  resolution gaps behind its contingent destroy: C#
  `RequiresCardsControlled.IsValid(..., AbilityEffectInstance)` honors
  `m_SameOwner` through the effect mapping's `m_SecondaryTargetIndex`, while
  Python counted matching cards without binding them to that target owner; and
  C# `Session.DestroyCard` ends the game when the selected card is a champion,
  while the native Python path sent that SessionCardId to the troop-only death
  handler. The condition context now receives the referenced effect targets,
  applies the SameOwner controller filter, and matches C#'s
  `SingleOpponent`/`SinglePlayer` handling. Native champion destruction now
  uses the shared game-end path. A focused condition regression checks 249 vs
  250 Eggs, an unrelated owner's Eggs, a missing secondary target, and both
  player-filter cases. The generated effect contract also stages typed
  `RequiresCardsControlled` witnesses on the owner selected by the referenced
  target, rejects a destroy that resolves to no card/champion outcome, and
  requires a champion destroy to end its generated session. The focused
  `--only 'Brood Baron' --contracts --card-timeout 15` run passes all four
  variants (4 abilities, 8 typed effects). A follow-up C# audit found that
  `Session.DestroyCard(champion)` calls `LoseGame`, which emits
  `ChampionWouldLoseEvent`; `CheckForReplacementAbilities` accepts only the
  first eligible replacement. The native destroy path now dispatches that
  event before game end and stops after one accepted trigger. A focused PvP
  adapter assertion checks that the replacement branch suppresses the direct
  game-end call, and checks participant ownership for the ordinary defeat
  branch. The generated Brood Baron scenario still has no survival ability, so
  it does not execute a Records-backed replacement. Python source order has
  not yet been proven identical to C# event-registration order when multiple
  replacement sources qualify. These remain generated PvE-style / adapter
  checks; no live PvP game-end or Unity defeat-screen trace has been captured,
  and, at that point, the historical 100-card sample had not been rerun.
  On 2026-10-04 the refreshed `--all --limit 100 --contracts
  --card-timeout 15` run passed 100/100 card scenarios and 100/100 ability
  contract groups, covering 159 abilities, 250 typed effects, and 10 choice
  branches. This sample includes the focused Constantina, Cerberus, Pippit Pal,
  Brood Baron, Rust and Ruin, Runic Growth, Subtle Striker, and Tricky Tykes
  fixes. The last three required generated-contract corrections: seed the
  parent `PlayedFromHand` TAC for Runic Growth's direct graph fixture, evaluate
  static IntAttr effects on synthetic champion targets for Subtle Striker, and
  stage both combatants in Warzone for Tricky Tykes' source-side one-shot
  `CardBlockedEvent`. Tricky Tykes' four variants pass both resource and
  nonresource reveal branches (6 abilities, 28 typed effects). These last
  fixture changes do not establish C# runtime equivalence on their own. The
  refreshed result is still a bounded PvE-style Python run; full-catalog
  coverage, PvP adapter scenarios, matched C# resolution traces, and Unity
  projection remain open.
  A Shards of Fate differential then exposed an oracle-fixture error: C#
  `AbilityTargetTemplate.GetLegalTargets` returns candidates, while the old
  `HeadlessSession` copied every candidate into a locked selected map. The
  Shards child target allows exactly one resource, but the synthetic Deck held
  two Blood Shards, so the old oracle applied its threshold effect twice. The
  adapter now enforces constant authored target maxima, uses the C# RNG for
  random target templates, and records candidate maps separately from selected
  root/nested targets. Variable target bounds that lack an active effect
  instance fail explicitly. The starting snapshot is now taken before target
  selection. The corrected probe compiled to
  `/tmp/HexHeadlessUnity_targetcap.dll`; it has not been reloaded into Unity,
  so a corrected C# capture and same-state Python/C# event comparison remain
  open. This is isolated to the experimental oracle; the RulesPort target map
  and the successful live Shards activation are unchanged.
- [ ] **U10 — Make client acceptance repeatable.** Maintain a short real-Unity
  smoke run for the flows above and add a two-real-client PvP run for setup,
  private pickers, chain priority, combat, and reconnect. Save the relevant
  client/server trace with each result. **Done when** the run can be repeated
  after a change and its log/event assertions give a clear pass or failure;
  headless tests remain separate evidence for rules semantics. The trace tool
  now accepts repeated `--expect-ui-state` substrings in expected order and
  writes a `client_acceptance` result. By default, it exits nonzero unless the
  Unity submission joins by transaction ID, every expected state appears in
  order, and the matched log window contains no client error.
  Append one `--expect-ui-state '<UIBattle log substring>'` per expected
  transition to make the command an acceptance gate and a nonzero process exit
  on failure. `--allow-time-window-correlation` enables a clearly labeled
  `pass_time_window_only` result for clients whose Trace-level submission
  marker is absent. The two-client PvP run and client-acceptance correlation
  still need execution. The 32-bit BepInEx/Harmony loader and probe DLL are
  installed in the client directory from `/tmp/hex-bepinex86`; the previous
  root-level headless command file was preserved. The `time_bug` Unity-hosted
  smoke completed: both damage events applied, two legal targets were found,
  and the fixture emitted two events. `oracle_cast` then accepted a Runic
  Growth `PlaySpellTransaction`, completed nine chain steps, and left an empty
  chain. The v1 report exposed recorder defects: board collections were cut
  off by the JSON depth limit, event payloads omitted subclass fields and wire
  bytes, and the input source GUID reflected Runic Growth's post-transform
  Rune template. The v2 recorder fixes those defects and was built against the
  client's .NET 2.0 assemblies, installed, loaded by Unity, and exercised by
  the queued `oracle_cast`. Its capture contains the original source GUID and
  selected target map, both players' ordered per-zone card snapshots, all 30
  event payloads with exact `ToByteArray()` bytes, and the queued/resolved
  transaction. In that synthetic fixture the cast paid 3 resources, changed
  champion damage from 24 to 27, changed the current/total resource pools from
  99/99 to 97/100, transformed the source into the Rune template, and emptied
  the chain. The client log records four `Ability instance 0 not found` errors
  during transaction validation; this count matches the four activation data
  entries and the `PlaySpellTransaction`/`XCostRequirement` path calling
  `ValidateXCost`, which looks up each data item's instance ID. Two subsequent
  `Ability instance 1 not found` errors remain unlocated. Treat these as C# log
  evidence, not as proof that the play failed: the transaction was accepted
  and its resolution events completed. The fixture still lacks the live
  `ClientStateCache` eligibility gate, the normal state-machine/network path,
  and Unity battle UI assertions, so neither this oracle run nor the earlier
  smoke is ordinary client acceptance. A repeat run produced the same
  initial/final state and ordered events, including wire bytes; only
  `m_TransactionId` advanced from 0 to 1. The first v2 capture is preserved at
  [`experimental/captures/runic_growth_oracle_v2.json`](../experimental/captures/runic_growth_oracle_v2.json).
  A Shards of Fate `oracle_cast` also accepted `PlayResourceTransaction`, but
  the pre-fix fixture had no Standard resources in the active player's Deck,
  so it emitted no threshold change. The parent target is the authored `You`
  `PlayerTargetTemplate`; C# represents that target with the player's champion
  SessionCardId and UID. The child ability separately targets a Standard
  resource in the owner's Deck. This mapping is correct; do not patch the
  shared target-map projection for it. The missing Deck cards traced to the
  fixture's no-ability-only resource filler filter. The fixture now selects
  basic resources by `CardTemplate.IsBasicResource()` while allowing their
  authored abilities, and seeds both Decks. The rebuilt Mono-compatible probe
  was loaded by a fresh Unity process, and `oracle_cast` accepted the
  `PlayResourceTransaction`, resolved four chain steps, and emptied the chain.
  Its two ordered `PlayerResourceThresholdChangedSessionEventArgs` events add
  Blood +1 each (new values 6 and 7); the fixture starts the player at Blood 5.
  A later audit found this double gain came from the old adapter selecting both
  legal candidates for a target whose maximum is one; it is not valid expected
  output for a single selected Standard resource (see U9 and the oracle
  runbook).
  Captures at
  [`experimental/captures/shards_of_fate_oracle_v3.json`](../experimental/captures/shards_of_fate_oracle_v3.json)
  and
  [`experimental/captures/shards_of_fate_oracle_v4.json`](../experimental/captures/shards_of_fate_oracle_v4.json)
  are identical, including initial/final state, event order, and wire bytes.
  The command file was consumed and the run is logged as
  `20261004174032002`. This is repeatable Unity-hosted C# evidence for the
  fixture, not an ordinary client play: the probe still bypasses the live
  state-machine/network path and Unity battle UI, and HConnect connection
  attempts fail because no local server is running. The generated Python
  `--contracts` fixture now resolves Shard's invoked child ability against a
  metadata-matched Standard resource and asserts its provided-threshold event
  set and state delta. It uses a separate generated state; same-state C#/Python
  import and ordered-event comparison remain open. The historical shutdown
  `ObjectDisposedException` remains uncorrelated. A rebuilt probe was loaded by
  fresh process PID 100300 and repeated as run `20261004181001606`; the output
  was byte-identical to v4 and the installed DLL hash matched the build output.
  The Unity runner now waits through slow template loading and emits at most
  one warning after its initial 1,200 updates, instead of logging a timeout
  exception on every frame. This did not add a live HConnect transaction or UI
  assertion. The later transport check did reach `CONNECTED` against the
  isolated trace-enabled server, but produced no gameplay 3029 request or
  battle UI flow at that point; U10 remained open. Fresh process PID 53480 then repeated
  the oracle as run `20261004182146513`: it logged one 1,200-update wait
  warning, completed template initialization, and produced a byte-identical
  v4 capture with the same accepted transaction and Blood threshold events.
  On 2026-10-04, a new direct `Start-Process` launch created PID 43160
  (19:37:53); BepInEx loaded the installed plugin, templates completed, and
  run `20261004183821795` consumed a fresh `oracle_cast` command. The C# result
  was `PASS`: accepted `PlayResourceTransaction`, one valid target, four chain
  steps, 13 events, and an empty chain. The new 243,933-byte JSON has the same
  SHA-256 as `shards_of_fate_oracle_v4.json`. The Unity process also completed
  synthetic profile authentication, but that fixture still bypasses the live
  client state machine and battle UI; its zero-deck/zero-champion profile made
  no gameplay request. The client log again contains the `auth:res` unknown
  Session message and the `Format.setBans` null-reference from the profile
  event. This repeats the C# fixture capture in a fresh process, not a Unity
  gameplay acceptance run. Later on 2026-10-04, the ordinary Immortal Practice
  session `234230` produced the correlated gameplay traces in U2 and U5: Unity
  submitted Shards of Fate as transaction ID 4, received the 3055 play result,
  opened its authored picker, submitted transaction ID 5 with one Blood Shard
  at child target slot 0, and returned to `BattleStateMainPhase`. The server
  snapshot changed Blood threshold 5 to 6 with one class-35 event, and both
  bundles pass transaction-ID joining, ordered UI-state, and no-client-error
  checks. For reruns, use Immortal Practice with a 60-card deck made only from
  Shards, then use `!gencard`, `!thresholds`, and `!resources`
  to stage the chosen card and make it playable. Captures:
  [`u2_live_playresource_20261004.json`](../experimental/captures/u2_live_playresource_20261004.json)
  and
  [`u5_live_shards_continuation_20261004.json`](../experimental/captures/u5_live_shards_continuation_20261004.json).
  This validates the Shards flow once in PvE; it does not yet establish a
  repeat-after-change harness, PvP picker behavior, or acceptance for other
  card/trigger families.
- [ ] **U11 — Enforce the release bar.** For each changed rule, wire event, or
  picker, update its matrix row with source references, focused tests, both-mode
  coverage where applicable, and a Unity trace or an explicit missing client
  check. Keep unfinished rows marked partial/open. **Done when** no change is
  called C#-equivalent based only on handler registration, compilation,
  database state, or a no-crash sweep. Use the evidence ledger below for each
  parity-sensitive change; U11 remains open until active changes have complete
  entries and the release review checks them.

### Per-change release evidence ledger (U11)

Add one row for each changed rule, emitted event, or picker. Link both the C#
source and the focused Python assertion. Record PvE and PvP coverage where the
flow exists in both modes, and attach a trace bundle or state why Unity evidence
is missing. Keep the status `partial` or `open` while any required item is
missing.

| Change / status | C# source and authored metadata | Focused semantic or wire check | PvE / PvP coverage | Unity trace or missing check |
| --- | --- | --- | --- | --- |
| Pregame setup ordering and initial deck count / partial | `PreGameState.OnEntry`; `AuthoritativeSessionBase.HandleGameEvent`; `Session.RegisterTriggerEventHandler`; `TriggerEventMap`; `hex-private/Records/AbilityEffectConditionTemplate.jsonl` (`RequiresCardsControlled`, deck filter) | `tests/tests_pregame_talents.py::test_skylak_uses_original_deck_size_for_both_talents` checks ordered Great Spore Beast and Zodiac effects against 99- and 100-card starting deck snapshots | RulesPort setup resolver and compatibility entry point share the same implementation; the focused fixture uses a PvE-shaped setup, with no paired PvP adapter execution | Missing: full PvE/PvP setup packet sequence, client setup UI, and correlated Unity trace |
| Spell finalizer uses current post-resolution type / partial | `Session.cs:MoveSpellAfterFinishedResolving`; `FinishResolvingCardAbilityEffectTemplate.Apply`; `Session.cs:FinishMovingCard`; zone events at `Session.cs:FinishMovingCard` | `tests/tests_chain_abilities.py::test_spell_finish_uses_the_effective_post_resolution_type` transforms the source during resolution and checks Warzone projection, entry state, discard fallback, and event order; `tests/tests_tournament.py::test_pvp_chain_warzone_entry_stamps_arrival_and_resolve_order` checks PvP arrival bits and `resolved_at` | Shared chain-item helper serves both mode hosts; separate PvP host check covers arrival bookkeeping, but not a paired spell-resolution scenario | Missing: matching C# transformed-permanent oracle capture, PvP spell-resolution trace, and Unity card/trigger UI acceptance |
| Charge-power cost source context / partial | `Mechanics/Card.cs:GetChargePointCostModifier`; `Session.cs:CanActivateAbility` and charge payment; typed `ChargePointCostModifier` leaves in `hex-private/Records/AbilityEffectTemplate.jsonl` | `tests/tests_rules_port_kernel.py::test_charge_point_cost_modifier_applies_to_the_plan` checks distinct champion/troop values and computed plans | Shared source-context helper and planner run against PvE- and PvP-shaped state fixtures; mode transaction adapters and Records-backed duration handling not exercised | Missing: Records-backed modifier resolution through both live mode adapters, matching C# state/event capture, and Unity charge HUD/update trace |
| RulesPort chain trigger dispatch removes legacy hook lookup / partial | `AuthoritativeSessionBase.cs:HandleGameEvent`; native host `chain_dispatch` hooks in `HCPHandler` and tournament PvP; `rules_port.triggers.dispatch_native_trigger` | `tests/tests_rules_port_kernel.py::test_rules_port_chain_dispatch_skips_legacy_trigger_hook` asserts the retired `_dispatch_game_trigger` hook is not called; `tests/tests_chain_abilities.py::test_native_chain_resolves_trigger_without_legacy_fallback` patches the old ability resolver to fail if reached | Both production mode hosts implement `chain_dispatch`; focused no-hook path also reaches the native dispatcher. End-to-end mode trigger resolution is covered separately; no complete paired PvE/PvP trace | Missing: a correlated live trigger request, chain event/priority response, and Unity chain UI trace |
| RulesPort trigger API blocks legacy scan for attached checkpoints / partial | `abilities/framework/triggers.py::resolve_triggers`; native `rules_port.triggers.NativeTriggerBackend`; `_rules_port_attached` persisted adapter marker | `tests/tests_trigger_parity.py::_rules_port_checkpoint_cannot_fall_back_to_legacy_trigger_scan` requires the native dispatcher for a marker-only attached checkpoint | Shared entry check covers attached checkpoint adapters; no paired live PvE/PvP trigger trace | Missing: correlated client trigger request, ordered trigger/chain events, and Unity chain UI trace |
| RulesPort effect context rejects legacy leaf entry / partial | `Session.ActivateAbilityFromEffect`; `AuthoritativeSessionBase` ability lifecycle; authored child ability graphs in `hex-private/Records/` | `tests/tests_effect_context.py::test_legacy_effect_context_rejects_rules_port_sessions` checks the legacy factory and direct constructor; `tests/tests_resolution.py::test_attached_session_rejects_legacy_walker_even_with_native_dispatch` | Shared guard rejects attached host and persisted RulesPort checkpoints before legacy effect walking starts; no per-mode gameplay trace | Missing: correlated PvE/PvP nested-ability request and Unity continuation/picker acceptance |
| Unity-hosted C# ability-resolution oracle / partial | `AuthoritativeSessionBase`, `AbilityInstance`, `PlaySpellTransaction`, `XCostRequirement`, `AuthoritativeSessionBase.ValidateXCost`, the selected `AbilityTemplate`, and the loaded `TemplateManager` in `hex-private/HexClient/`; source/ability GUIDs and typed Records metadata | `experimental/HeadlessSession.cs`, `experimental/OracleJson.cs`, and `experimental/Set1MatrixFixture.cs`; standalone `--transaction` probe accepts the original C# `RequestPlayerOptionsTransaction`; `oracle_cast` queues the play transaction through the original authoritative validation/resolve handler before synchronous chain resolution | Deterministic C# fixtures plus a separate metadata-driven Python native contract; no imported C# state comparison or injected trigger event | BepInEx/Harmony and the v2 plugin were loaded in Unity. The `time_bug` smoke applied two damage events and found two legal targets. Runic Growth's `oracle_cast` accepted `PlaySpellTransaction`, completed nine chain steps, and emptied the chain. Its v2 capture preserves the source GUID and target map, both players' ordered per-zone card snapshots, all 30 event fields and exact `ToByteArray()` bytes, queued activation data, and resolved transaction at `experimental/captures/runic_growth_oracle_v2.json`. A second run reproduced the same state and ordered event trace, including wire bytes; only `m_TransactionId` changed from 0 to 1. Four ID 0 lookup errors match the four activation entries checked by `XCostRequirement`/`ValidateXCost`; two later ID 1 errors remain unlocated. The pre-fix Shards capture has no Standard deck resource and no threshold-effect evidence. Fresh Unity `oracle_cast` runs are preserved at `experimental/captures/shards_of_fate_oracle_v3.json` and `experimental/captures/shards_of_fate_oracle_v4.json`; both accepted `PlayResourceTransaction`, resolved four chain steps, emptied the chain, and emitted ordered Blood +1 events to 6 then 7 from 5. The captures are identical. A separate generated Python contract now checks Shard's root and invoked child abilities, a metadata-matched Standard deck target, provided-threshold events, and the resulting threshold delta (`tests/tests_card_play_matrix.py --only 'Shards of Fate' --contracts --card-timeout 15`: 1 card ability, 1 invoked child ability, 3 typed effects). The C# run remains a synthetic fixture without ordinary network, picker, or UI acceptance; Python currently uses a separately generated state. Offline `--load` hits the documented Mono/Newtonsoft abstract `AbilityField`/`CardFilter` deserialization failure. Missing: same-state Python/C# initial-state import and event comparator, injected trigger scenarios, correlated ordinary gameplay trace, and real Unity UI acceptance |
| Shards of Fate nested provided-threshold effect / partial | `Session.cs:PlayResource`; `AbilityTargetTemplate.GetLegalTargets`, `GetMaximumTargetCount`, and `IsTargetValid`; `TACAbilityEffectTemplate.GainTargetsProvidedThresholds.Apply`; `CardTemplate.GetThresholdsProvided`; Records card `00e13fdf-b2c3-4fe7-a064-ce4481b24e8d`, child ability `8168d9c0-9b56-2f88-22c0-56cd8b9616b9`, and target `b5664968-c53c-ccaa-e49e-c7479d155907` | Existing generated contract `tests/tests_card_play_matrix.py --only 'Shards of Fate' --contracts --card-timeout 15` checks the root/child graph, Standard resource target, provided-threshold event, and state delta; live trace has one class-35 threshold event | Generated Python contract and one correlated Immortal Practice/PvE selection; no PvP replay or identical imported C#/Python fixture | `u2_live_playresource_20261004.json` and `u5_live_shards_continuation_20261004.json` show a real selected Blood Shard UID `15617` and Blood 5 to 6. Old C# v3/v4 +2 output is invalid as one-target evidence: the fixture locked both candidates despite max 1. The corrected adapter is compiled but not reloaded; missing: fresh C# capture with selected target trace, same-state event comparator, PvP and rerun-after-change client acceptance |
| 3053 `GameStartedEventArgs` body / partial | `Network/ObjFmt.cs`, `Network/GameSession/GameStartedEventArgs.cs` DataMembers | `tests/tests_u3_wire_contract.py`; Python and C# goldens under `tests/goldens/game_started_3053*.golden` | Serializer fixture only; live PvE/PvP setup not run | Missing: real client setup/UI trace |
| 3055 class-1 `GameStartedSessionEventArgs` / partial | `GameStartedSessionEventArgs.cs`, `Serializer.cs`, `SessionEventArgs.cs` | `tests/tests_u3_wire_contract.py::test_game_started_session_event_matches_csharp_reader_fixture`; `bash tests/csharp_oracles/verify_game_started_session_event.sh`; `tests/goldens/game_started_session_event.golden` | Deterministic shared fixture only; live setup not run | Missing: Unity setup transitions, valid cache references, coin winner variants, and reconnect trace |
| 3055 class-65 `PlayerUpdatedSessionEventArgs` / partial | `PlayerUpdatedSessionEventArgs.cs`; `ClientSessionBase.cs` threshold application and champion cache update | `tests/tests_u3_wire_contract.py::test_csharp_player_updated_writer_reader_and_champion_threshold_fields`; extracted C# event writer/reader verifies ordered shard keys parallel to six values, typed ChampionId, sleeve, visibility, resources, and field layout | Python projection matches original C# writer and parses through original C# reader; no live PvE/PvP refresh or reconnect | Missing: Unity acceptance of threshold updates and champion cache references in setup and reconnect |
| Setup and reconnect lifecycle events / partial | `PlayerMulliganedHandSessionEventArgs.cs`, `PlayerAcceptedStartingHandSessionEventArgs.cs`, `PlayerWishesToDrawFirstSessionEventArgs.cs`, `PlayerWishesToPlayFirstSessionEventArgs.cs`, `FirstPlayerDictatedSessionEventArgs.cs`, `ReconnectDoneSessionEventArgs.cs`; `SessionEventArgs.BuildArgs` | `tests/tests_u3_wire_contract.py::test_csharp_setup_lifecycle_event_writers_read_python_events`; six Python payload goldens compared with extracted C# writers and parsed by the original C# event constructors | Shared deterministic wire fixture only; opening-order and mulligan semantics have separate PvE/PvP-focused checks | Missing: Unity setup/coin/mulligan UI acceptance and reconnect snapshot acceptance |
| 3055 `SessionSyncEvent` / `NetworkPacket` envelope / partial | `Network/ObjFmt.cs`, `Network/GameSession/SessionSyncEventEventArgs.cs`, `Network/NetworkPacketSessionEventArgs.cs`, inherited `Network/SessionEventArgs.cs` members | `tests/tests_u3_wire_contract.py`; extracted C# comparisons at `tests/goldens/sync_empty_ack_csharp.golden` and `player_option_list_3055_csharp.golden`; five focused Python stream goldens | Serializer fixtures only; live PvE/PvP packet delivery not run | Missing: Unity handler acceptance, recipient privacy, and correlated server trace |
| 3029 no-sync transaction acknowledgements / partial | `Game/Client/SessionClient.cs:SubmitTransaction`, `ClientSessionBase.cs`; server `HCPHandler.handle_message`, `send`, `_handle_set_turn_phases_transaction`, `_handle_pass_priority_transaction`, `_push_transaction_ack` | `tests/tests_u3_wire_contract.py::test_no_sync_3029_paths_send_one_csharp_empty_ack`, `test_successful_no_event_3029_gets_one_empty_sync_ack`, and `test_rejected_classified_3029_is_not_replayed_to_legacy_dispatcher`; exact empty body compared with `tests/goldens/sync_empty_ack_csharp.golden` | SetTurnPhases, setup no-op PassPriority, rejected RulesPort PassPriority, and successful no-event TipWindowClosed paths exercised with isolated fixtures; no live PvE/PvP transaction submitted | Missing: Unity acceptance of the ack and next queued request |
| One-way readiness/reconnect requests / partial | `GameClient` readiness methods; `services/tournament_game.py:handle_ready_for_game_events`, `handle_ready_to_continue_game`; `HCPHandler` 22011 route | `tests/tests_u3_wire_contract.py::test_fire_and_forget_setup_and_reconnect_routes_send_no_typed_reply` | Dispatch paths exercised with no-session, first-ready, and reconnect snapshot stub cases | Missing: captured active-session packet sequence and client log correlation |
| PvP reconnect champion identity and cache order / partial | `ClientSessionBase.cs:74`, `PlayerRepresentation.cs:270`, `PlayerUpdatedSessionEventArgs.cs`; `UIBattle.cs:4546` (`OnTurnPhaseUpdated` cache access) | `tests/tests_rules_port_setup_parity.py::test_pvp_reconnect_uses_valid_champion_ids_before_phase_projection` restores both IDs from champion rows, verifies valid UIDs and matching `CardUpdated` before `StartTurn`, and rejects missing identities | PvP reconnect snapshot covered with isolated state and adapter stubs; PvE reconnect not exercised | Missing: real Unity reconnect, `State.Cards` acceptance, and correlated server/client log trace |
| 3029 transaction trace capture / partial | `Game/Client/SessionClient.cs:SubmitTransaction`, `ClientSessionBase.cs`; server request dispatch and `HCPHandler.send`; `replay_db.db_record_session_transaction` | `tests/tests_rules_port_sql_capture.py` now calls the same `raw_data_wrapper_bytes` keyword as the production 3029 handler and asserts the exact DataWrapper bytes, decoded intent, state snapshots, projected/send-only packets, and bundle correlation; `rules_port.trace_bundle` exact transaction-ID join and ordered UI-state gate | Capture code supports PvP by default and opt-in practice/encounter; existing live rows predate socket-send capture and the signature fix | Missing: a fresh correlated Unity submission, decoded request, authoritative state delta, and accepted 3055 sequence |
| 3029 PassPriority ObjFmt request / partial | `Network/ObjFmt.cs`, `Network/DataWrapper.cs`, `Network/GameSession/PlayerTransactionRequestArgs.cs`, `Mechanics/Transactions/Transaction.cs`, `Mechanics/Transactions/PassPriorityTransaction.cs`, `Mechanics/ETurnPhases.cs` | `tests/tests_u3_wire_contract.py`; `tests/goldens/3029_pass_priority_datawrapper.golden`; `tests/csharp_oracles/generate_wire_goldens.sh` | Serializer fixture only; no gameplay action run in either mode | Missing: live client request and matching server trace |
| 3029 SetTurnPhases ObjFmt request / partial | `Network/ObjFmt.cs`, `Network/GameSession/PlayerTransactionRequestArgs.cs`, `Mechanics/Transactions/Transaction.cs`, `Mechanics/Transactions/SetTurnPhasesTransaction.cs`, `Mechanics/ETurnPhases.cs`, `LocalPlayer.cs:SetTurnPhases` | `tests/tests_u3_wire_contract.py::test_csharp_objfmt_set_turn_phases_request_decodes_both_lists`; `tests/goldens/3029_set_turn_phases_datawrapper.golden` | Decoder fixture only; request action not exercised in PvE/PvP | Missing: live stops change and matching empty-ack/client gate trace |
| 3029 opening pick and mulligan requests / partial | `Network/ObjFmt.cs`, `Network/GameSession/PlayerTransactionRequestArgs.cs`, `Mechanics/Transactions/Transaction.cs`, `ChoosePlayTransaction.cs`, `ChooseDrawTransaction.cs`, `MulliganTransaction.cs`, `AcceptStartingHandTransaction.cs`; `Mechanics/MulliganState.cs`, `Mechanics/Transactions/Requirements/PlayerHasPriorityRequirement.cs` | `tests/tests_u3_wire_contract.py::test_csharp_objfmt_opening_choice_and_mulligan_transactions`; `tests/tests_rules_port_setup_parity.py::test_pvp_mulligan_redraw_and_handoff_follow_csharp_active_player`; choose-play, choose-draw, mulligan, and accept-starting-hand goldens generated through extracted ObjFmt | PvP active-player and redraw/keep decision plus handoff covered; Practice/PvE event sequence fixture covers active-player order, not the live adapter | Missing: both coin-toss winners through service setup, mulligan/keep UI, first priority, and reconnect trace |
| Practice/PvE active mulligan player / partial | `Mechanics/MulliganState.cs`, `Mechanics/Transactions/MulliganTransaction.cs`, `AcceptStartingHandTransaction.cs`, `AuthoritativeSessionBase.cs:Mulligan`, `Session.cs:PlayerAcceptedStartingHand`, `AI/AITactical.cs:AIHandleMulligan`; host `hconnect_server.py:_push_pve_mulligan_start`, `ai.py:resolve_ai_mulligan` | `tests/tests_rules_port_setup_parity.py::test_practice_mulligan_start_follows_csharp_active_player` checks AI-first sequence ordering and human-first ownership; `test_ai_mulligan_emits_acceptance_only_when_the_hand_is_kept` checks redraw/accept event order and flag | Shared Practice/PvE path; AI event test uses mocked card-zone rows and an evaluator fallback | Missing: real AI value evaluation, full adapter request/response, reconnect, and Unity UI trace |
| 3029 resource/troop play requests / partial | `Network/ObjFmt.cs`, `Mechanics/Transactions/PlayResourceTransaction.cs`, `PlayTroopTransaction.cs`, `SessionCardId.cs` | `tests/tests_u3_wire_contract.py::test_csharp_objfmt_resource_and_troop_play_payloads`; C# ObjFmt goldens `3029_play_resource_datawrapper.golden` and `3029_play_troop_datawrapper.golden` | Serializer and RulesPort ingress normalization only; plays not executed in PvE/PvP | Missing: live play request, accepted/rejected transaction behavior, and Unity state/event trace |
| Remaining C# 3029 transaction shapes and `CombatId` payload / partial | `Network/ObjFmt.cs`, `Network/GameSession/PlayerTransactionRequestArgs.cs`, and the matching C# `Mechanics/Transactions/*.cs` declarations mirrored by `tests/csharp_oracles/objfmt_wire_oracle.cs` | `tests/tests_u3_wire_contract.py::test_csharp_objfmt_client_request_family_matrix`; generated goldens cover discard, artifact/spell/champion play, activation/trigger continuation, attack/defense/damage assignment, auto-pass, concede, encounter-mod, ready/options/tip, checksum, and debug; RulesPort maps C# `CombatId.Attacker`/`SerialNumber` into `rules_port.combat.CombatId` | Shared decoder and ingress normalization only; actions were not run through PvE/PvP adapters | Missing: live accepted/rejected requests, response-count behavior, and Unity state/event traces |
| Opening play/draw choice and turn order / partial | `Mechanics/PickGoesFirstState.cs`, `Mechanics/Transactions/ChoosePlayTransaction.cs`, `ChooseDrawTransaction.cs`, `Session.cs:PlayerWishesToPlayFirst`, `PlayerWishesToDrawFirst`; PvP adapter `services.tournament_game.handle_ready_for_game_setup` | `tests/tests_rules_port_setup_parity.py::test_opening_choice_preserves_both_coin_winners_and_csharp_order` exercises both initial player orders and both choices; `test_pvp_setup_coin_winner_is_persisted_and_shared_by_both_players` decodes both PvP 22027 responses; C# request bytes remain covered by the U3 opening-choice fixtures | RulesPort semantics and PvP setup adapter covered; Practice/PvE setup adapter not exercised | Missing: Unity coin choice/toss, accepted hands, first priority, service-level 3053/3055 delivery, and reconnect trace |
| 3029 manual activation TargetMap / partial | `Network/ObjFmt.cs`, `Mechanics/Transactions/ActivateAbilityTransaction.cs`, `Mechanics/Transactions/SetAbilityActivationDataTransaction.cs`, `Mechanics/Abilities/AbilityActivationData.cs`, `Mechanics/Abilities/TargetTemplates/AbilityTargetInstance.cs`, `SessionCardId.cs` | `tests/tests_u3_wire_contract.py`; `tests/goldens/3029_activate_ability_targetmap.golden`; `tests/tests_rules_port_kernel.py::test_activation_reply_binds_the_waiting_instance_then_finishes_push` | Serializer/ingress and native waiting-instance fixture only; user reports one fresh Necrotic game appears fixed; no PvP replay captured | Missing: correlated live picker/resolution trace, observed exactly-once client resume, and checks for other picker flows |
| Reveal-to-picker client packet rewrite / partial | `Network/NetworkPacketSessionEventArgs.cs:CombineRevealAndChoose`, `AbilityActivationDataRequiredSessionEventArgs.cs`, `CardsRevealedSessionEventArgs.cs`, and nested option readers | `tests/tests_u3_wire_contract.py::test_csharp_packet_parser_combines_matching_reveal_and_picker`; `tests/csharp_oracles/verify_reveal_picker_combine.sh`; Python packet projection asserts owner-only class-51/23/70 batching; C# fixture asserts parent/source linkage, target slot, and `AdditionalTargets` | Composite packet/parser fixture only; no PvE/PvP game action submitted | Missing: Unity-rendered picker selection, continuation result, and correlated one-activation trace |
| Duplicate phase event causing automatic damage submission / partial | `Mechanics/TurnPhaseState.cs`, `UIBattle.cs:OnTurnPhaseUpdated, PushStateForPhase`, `BattleStateAssignDamage.cs:OnEnter` | `tests/tests_combat.py::test_phase_stop_packet_announces_each_phase_once`; `tests/tests_rules_port_csharp_parity.py::test_python_phase_graph_matches_every_client_state_permit`; `test_original_csharp_combat_declaration_matches_python_port` | Headless packet projection and primitive comparison only; no full mode combat sequence | Missing: real attack and defense UI traces proving one phase entry, one transaction, and no stale combat controls |
| Triggered ability batch with follow-up picker / partial | `Mechanics/Transactions/ActivateTriggeredAbiliesTransaction.cs`, `AuthoritativeSessionBase.cs:ActivateTargetedTriggeredAbilities`, `Session.cs:ActivateTriggeredAbility, PushAbilityOnChain`, `Mechanics/GameActions/WaitForTriggeredAbilitiesAction.cs`, `PushOntoChainAction.cs` | `tests/tests_rules_port_kernel.py::test_triggered_activation_waits_for_nested_picker_before_payment_and_push`; class-39 and class-23 C# ObjFmt fixtures | Native scheduler fixture checks same ability and parent instance, preserved target slot, follow-up option input, and one payment | Missing: actual Unity class-39 picker/order and follow-up class-23 event trace in PvE and PvP |
| Generated-card representation / partial | `AuthoritativeSessionBase.cs:CreateNTokensFromResource, CreateTokenFromResource`; `CardRepresentation.cs:1566`; `ClientSessionBase.cs:66` | `tests/tests_rules_port_kernel.py::test_verdict_creates_pool_tokens_and_activates_choose`; `tests/tests_effect_context.py::test_card_zone_projection_uses_client_derived_moves` | Shared native fixtures; no separate live mode adapter run | Missing: captured Unity card-cache/UI sequence, privacy, and reconnect validation |
| Records `TopNOfDeck` during trigger-condition evaluation / partial | `Mechanics/Cards/Filters/TopNOfDeck.cs:IsMatch, GetAmount`; Records `AbilityTemplate.jsonl` Harbinger of Hastur trigger metadata | `tests/tests_card_play_matrix.py --only 'Harbinger of Hastur' --contracts --card-timeout 15` passed both reprints; `rules_port/condition_context.py::ConditionContext.iter_cards` exposes deck candidates and position, and `filters.py::TopNOfDeck` reads the live threshold state | Shared filter evaluator; focused generated fixture uses the PvE-style owner model, with no PvP-specific threshold fixture | Missing: correlated Unity trigger/zone trace and a PvP condition-context scenario |
| `None` collection token creation followed by authored zone move / partial | `AuthoritativeSessionBase.cs:CreateNTokensFromResource, CreateTokenFromResource`; `Mechanics/Abilities/SummonTokenTroopAbilityEffectTemplate.cs`; `MoveCardToZoneEffectTemplate.cs`; authored effect graph in `hex-private/Records/AbilityEffectTemplate.jsonl` | `tests/tests_resolution.py::test_summon_none_collection_waits_for_authored_zone_move`; generated Blessing ability contract asserts its None summon and following Warzone move | Shared RulesPort effect path; focused fixture and generated card contract use PvE-style state, with no PvP replay | Missing: live token cache/update ordering, Unity trigger order, and a PvP resolution trace |
| Card lifecycle/play event payloads / partial | `CardDrawnSessionEventArgs`, `CardDestroyedSessionEventArgs`, `CardDiscardedSessionEventArgs`, `CardVoidedSessionEventArgs`, `CardGraveyardedSessionEventArgs`, `ChampionCardPlayedSessionEventArgs`, `TroopCardPlayedSessionEventArgs`, `ResourceCardPlayedSessionEventArgs`, `SpellCardPlayedSessionEventArgs`, `SpellCardCastSessionEventArgs`, `ArtifactCardPlayedSessionEventArgs`, and `CardMovedSessionEventArgs`; `SessionEventArgs.BuildArgs`; `UIBattle` event handlers | `tests/tests_u3_wire_contract.py::test_csharp_zone_card_event_family_writers_readers_and_public_projection`; 12 deterministic payload goldens; extracted C# writers/readers; full 3055 ObjFmt compared for both recipient UIDs; `game_engine.py` now re-exports all five previously omitted public event classes | Synthetic public event batch and both recipient packets only; no live action scheduler or private-event scenario | Missing: transaction-produced card draw/play/destroy/discard flows, viewer-specific event privacy, reconnect ordering, and Unity zone/play animations |
| CardUpdated movement and same-collection updates / partial | `CardRepresentation.cs:Update`, `ClientStateCache.cs:Update`, `UIBattle.cs:OnCardMoved, OnCardUpdated`, `AuthoritativeSessionBase.MoveCardWithDispatch`, `DebugCheatTransaction.Resolve`; the cache constructs class 50 only for collection/controller changes | `tests/tests_effect_context.py::test_card_zone_projection_uses_client_derived_moves`; `tests/tests_u3_wire_contract.py::test_csharp_zone_card_event_family_writers_readers_and_public_projection` verifies class 50 in the writer/reader batch, both recipients, and full 3055 ObjFmt packets; `tests/tests_effect_context.py::test_native_same_collection_transform_emits_no_wire_card_moved`; `tests/tests_tournament.py::test_pvp_draw_uses_cache_derived_collection_move`; `tests/tests_chain_abilities.py::test_gearsmith_takes_the_revealed_artifact_and_stays_in_play` asserts same-Deck updates have no wire class 50; `tests/tests_debug_cheats.py::test_debug_card_updates_use_cache_moves_and_hide_deck_identity`; `tests/tests_commands_projection.py::test_chat_card_moves_use_card_updated_without_duplicate_class_50`; source-audited `ai.py`, `hconnect_server.py`, and `services/tournament_game.py`; `git diff --check` passed | RulesPort collection transitions share the cache-derived contract in Practice/PvE and PvP draw; Gearsmith, transform, debug-cheat, and chat-command fixtures use Practice/PvE state; PvP debug cheat and discard/play have no focused parity fixture | Missing: Unity trace showing the cache move/sound once, a client-tested same-collection transform/reorder, and remaining encounter/AI/PvP event branches; the pregame gem-card rehome event remains an unverified Unity workaround |
| Hidden and Choosing card projection / partial | `CardRepresentation.cs:Update`; `Card.cs:CanPlayerSeeCard, IsCardCollectionFaceDown`; `UIBattle.cs:OnCardMoved` | `tests/tests_effect_context.py::test_hidden_card_update_sends_no_template_identity_or_stats`, `test_other_players_choosing_card_is_hidden_even_without_nulling_flag`; underground visibility test keeps its public Tunneling counter | Shared packet projector used by PvE/PvP; no live viewer packet captured | Missing: packet capture from both clients, visibility grant/revoke, and reconnect acceptance |
| Underground visibility HUD flag / partial | `PlayerUpdatedSessionEventArgs.cs:CanSeeEnemyUnderground`; `UIBattle.cs:CheckCanSeeUnderground`; `Card.cs:CanPlayerSeeCard` | `tests/tests_effect_context.py::test_visibility_refresh_projects_underground_permission_to_unity_flag` checks grant and revoke | Shared visibility refresh; PvE/PvP live projections not captured | Missing: real underground card reveal/hide and reconnect trace |
| C# collection visibility and PvP Underground reconnect / partial | `Mechanics/Card.cs:CanPlayerSeeCard, IsCardCollectionFaceDown`; `CardRepresentation.cs:Update`; `UIBattle.cs:IsCardCollectionFaceDown, CheckCanSeeUnderground` | `tests/tests_effect_context.py::test_packet_visibility_matches_csharp_collections_and_reveal_scopes`; reconnect checks owner, unauthorized, and permitted viewer packets using `merge_underground_visibility_flags` and `project_visible_underground` | Shared viewer projector; PvP reconnect refreshes the HUD flag and seeds owner-hidden rows plus viewer-scoped authorized rows; no PvE reconnect packet fixture | Missing: two-client packet capture, reconnect cache/HUD acceptance, and startup chain/AcceptedStartingHand interaction |
| PlayerOptionList target/cost records / partial | `Serializer.cs`, `PlayerOptionListSessionEventArgs.cs`, `PlayerOptionSessionEventArgs.cs`, `OptionInstanceSessionEventArgs.cs`, `TargetInstanceSessionEventArgs.cs`, `CostInstanceSessionEventArgs.cs` | `test_player_option_list_target_cost_event_matches_csharp_fixture`; original C# readers accept nested classes 70/69/68/67/66; extracted C# ObjFmt encodes the outer 3055; synthetic projection confirms chooser-only delivery | Shared fixture only; no mode-specific picker execution | Missing: class-70 C# writer byte identity (nested `GetBuffer()` capacity padding), Unity option rendering, target/cost picker transitions, and live two-client privacy trace |
| 3029 `m_TransactionId` decode / partial | `Mechanics/Transactions/Transaction.cs` DataMember `int` and `GetSerializedBytes()` | `tests/tests_application.py`; little-endian 32-bit fixture | Decoder fixture only; no mode-specific execution | Missing: fresh trace joining the client submission ID |
| Damage and prevention semantics / partial | `Session.cs:DamageCard`, `Session.cs:DamageChampion`, `Mechanics/DamageShield.cs:Apply`, `Mechanics/Card.cs:AddDamageShield`, `ClearEndOfTurnDamageShields`, and `ResetArmor` | `tests/tests_damage_rules.py` focused state/outcome assertions; `tests/tests_effect_context.py::test_context_typed_damage_shield_uses_client_flags`; updated combat/card stream goldens | Shared path covers PvE/PvP shield checkpoint expiry; live damage trace not captured | Missing: correlated Unity damage event, animation, and HUD state; legacy checkpoints lack historical cross-store shield order |
| Generated Records card and ability matrix / partial | C# `Mechanics/AbilityInstance.cs`, `Mechanics/AbilityManager.cs`, effect-template sources; authored ability/effect/target Records | Refreshed `--all --limit 100 --contracts --card-timeout 15` passes 100/100 cards, 159 abilities, 250 typed effects, and 10 choice branches; focused Puzzle Box, Tome, Poltergeist, Buffalord, Depleted Bumblebot, Rust and Ruin, Constantina, Cerberus, Pippit Pal, Brood Baron, Runic Growth, Subtle Striker, and Tricky Tykes contracts pass; input-bearing TurnStarted graphs remain direct effect contracts | Shared PvE-style fixture; no PvP adapter scenario run | Missing: full catalog execution, PvP adapter scenario, matched C# resolution trace, Unity projection trace, and triggered picker continuation |
| Records `IsColor` template `CardShards` and zero-counter condition / partial | `Mechanics/Card.cs:CardShards`; `Mechanics/Cards/Filters/IsColor.cs:IsMatch(CardTemplate)`; `Mechanics/Abilities/Conditions/SourceCardHasCounters.cs:IsValid(Card)` | `tests/tests_targeting_parity.py::_template_and_cost_value_support` checks authored threshold colors; `tests/tests_conditions.py::test_native_source_counter_condition_allows_zero`; generated The Buffalord and Depleted Bumblebot contracts pass | Shared filter and condition evaluators; generated matrix is PvE-style and no mode-specific PvP counter fixture has run | Missing: correlated Unity stat/keyword projection after aura application and PvP threshold/counter scenarios |
| `IntAttrAbilityVariable` reads triggered `DamageDealt` / partial | `Session.cs:CreateAbility` appends `TriggerEvent.tac`; `Mechanics/Abilities/IntAttrAbilityVariable.cs:GetValue` reads the active instance TAC | `tests/tests_ability_runtime_parity.py::test_typed_variables_read_highest_charge_and_trigger_damage`; `tests/tests_card_play_matrix.py --all --limit 5 --contracts --card-timeout 15` caught and then passed the Juurdin token-count contract | Focused shared resolver fixture only; no separate PvP adapter scenario | Missing: correlated Unity trigger, token projection, and HUD trace |
| Static card-context IntAttr aura / partial | `Mechanics/Modifiers/IntAttrModifier.cs:Apply`; `Mechanics/Abilities/TapCardAbilityEffectTemplate.cs:Apply`; Constantina's typed `IntAttrModifier` and target templates in `hex-private/Records/AbilityEffectTemplate.jsonl` / `AbilityTargetTemplate.jsonl` | `tests/tests_card_play_matrix.py --only 'Constantina' --contracts --card-timeout 15` passes 8/8 card variants, 12 abilities, and 12 typed effects; the generated static contract checks opposing versus controller exhaust; `combat_rules.card_int_attr` applies Records-backed static values using C# `Double`/`Add`/`Remove`/`Set` order | Generated PvE-style owner scenarios only; no PvP aura/controller scenario | Missing: Unity tap event/UI trace, PvP owner model, and unresolved dynamic `m_ValueField` cases |
| Static `DamageMultiplierModifier` operand / partial | `Mechanics/Modifiers/NumericModifier.cs:GetValue`; `Mechanics/Modifiers/DamageMultiplierModifier.cs:Apply`; Cerberus `m_InputValue=EffectConstant(3)` in `hex-private/Records/AbilityEffectTemplate.jsonl` | `tests/tests_card_play_matrix.py --only 'Cerberus' --contracts --card-timeout 15` passes both templates (2/2 cards, 3 abilities, 4 typed effects); the generated contract checks the static rule plus combat/noncombat `card_damage_multiplier` results | Generated PvE-style owner scenarios only; no PvP damage scenario | Missing: matching C# damage trace, PvP adapter run, and Unity damage event/HUD trace |
| Static player-target IntAttr and resource allowance / partial | `Mechanics/Modifiers/IntAttrModifier.cs:Apply`; `PlayerTargetTemplate`; Pippit Pal's typed `AdditionalResourcesPlayableOnYourTurn` effect in `hex-private/Records/AbilityEffectTemplate.jsonl`; `rules_port/static_rules.py:player_int_attributes`; `rules_port/resources.py:resource_play_limit` | `tests/tests_card_play_matrix.py --only 'Pippit Pal' --contracts --card-timeout 15` passes both templates (2/2 cards, 4 abilities, 4 typed effects); the generated contract checks the authored player filter and resulting resource allowance; no production change was needed | Generated PvE-style owner scenarios only; no PvP resource-limit scenario | Missing: matching Unity resource-play request/UI trace and a two-client PvP acceptance run |
| `RequiresCardsControlled.m_SameOwner`, champion destroy, and loss replacement / partial | `Mechanics/Abilities/Conditions/RequiresCardsControlled.cs:IsValid(TriggerEvent, Card, Session, AbilityEffectInstance)`; `Mechanics/Abilities/AbilityEffectInstance.cs:AreApplyContingenciesMet`; `Session.cs:DestroyCard`, `LoseGame`, `CheckForReplacementAbilities`; Brood Baron condition `ThereAre250OrMoreEggsInThatChampionSDeck` and `m_SecondaryTargetIndex=0` in `hex-private/Records/AbilityEffectConditionTemplate.jsonl` / `AbilityTemplate.jsonl`; 13 typed `ChampionWouldLoseEvent` Records abilities | `tests/tests_rules_port_condition_parity.py::test_requires_cards_controlled_same_owner_uses_contingent_target` checks the exact 249/250 boundary, ownership, missing target, `SingleOpponent`, and `SinglePlayer`; `test_native_pvp_champion_destroy_uses_persisted_participant_owner` and `test_native_pvp_champion_destroy_honors_loss_replacement` check PvP owner routing and end-game suppression at the adapter seam; generated `tests/tests_card_play_matrix.py --only 'Brood Baron' --contracts --card-timeout 15` passes 4/4 variants, 4 abilities, and 8 typed effects, checks the destroy outcome and ended session | Generated PvE condition/destroy fixture plus mocked PvP adapter branches; no Records-backed survival ability execution or PvP SameOwner fixture | Missing: C#-equivalent ordering when multiple replacement sources qualify, a live two-client PvP defeat/replacement trace, and Unity defeat-screen/zone trace |
| Card-count expression feeding dynamic token amount / partial | `Mechanics/Abilities/CardCountAbilityVariable.cs:GetValue`, `ExpressionAbilityVariable.cs:GetValue,CalculateValue,LookUpVariable`, `Mechanics/Abilities/SummonTokenTroopAbilityEffectTemplate.cs`; Rust and Ruin typed `CardCountAbilityVariable` filters and `m_AmountField` in `hex-private/Records/AbilityTemplate.jsonl` and `AbilityEffectTemplate.jsonl` | Generated `tests/tests_card_play_matrix.py` stages typed variable witnesses and checks the resolved summon amount; `--only 'Rust and Ruin' --contracts --card-timeout 15` passes 2/2 reprints, 4 abilities, 5 effects | Generated PvE-style owner model only; no PvP variable or token scenario | Missing: matching C# dynamic-value capture, PvP adapter run, and Unity token/update trace |
| Records-driven trigger and target fixture staging / partial | `Mechanics/Card.cs:RegisterTriggerEvents`, `GetTriggeredAbilities`, `PassesCollectionFlagRequirements`; `AuthoritativeSessionBase.cs:HandleGameEvent`; `Session.cs:EmitBlockerEvents`; typed Records trigger and target filters | Focused Poltergeist and Tricky Tykes matrix; refreshed bounded 100-card sample above; Tricky Tykes checks both `CardBlockedEvent` source/target identities, Warzone registration, both reveal branches, and its one-shot path; `tests/tests_trigger_parity.py::_simulacrum_ignores_authored_trigger_collection_mask`, `_chain_mutation_events_precede_client_completion_in_both_owner_models`; `tests/tests_chain_abilities.py::test_friendly_zone_trigger_ignores_cards_taken_from_an_opponent`, `test_native_chain_resolves_trigger_without_legacy_fallback` | Generated PvE-style owner fixtures plus one synthetic PvP champion trigger through the native chain resolver; no broad both-mode event-family equivalence scenario | Missing: per-family semantic coverage, full instance-ID/event correlation, Unity presentation, and a correlated live event trace |
| Transform trigger, state, collection, visibility, and wire / partial | `Session.cs:TransformCard`, `TransformIntoCopy`, `TransformCardBase`, `RevertCard`; `Mechanics/Card.cs:SetActiveGems`, `CanPlayerSeeCard`, `CopyFrom`, `Revert`; `AuthoritativeSessionBase.cs:HandleGameEvent`; `Mechanics/TriggerEvent.cs`; `CardTransformedSessionEventArgs.cs`; `CardRevertedSessionEventArgs.cs` | `tests/tests_effect_context.py::test_native_same_collection_transform_emits_no_wire_card_moved` checks trigger/mutation/projection order, reversion and restored gem argument; `test_persisted_original_gems_survive_transform_and_revert` checks one-time gem capture, valid empty-socket value, and SQLite restore; `test_native_transform_to_target_preserves_source_collection` checks copy collection; `test_native_transform_event_uses_pre_transform_viewer_visibility` checks hidden-hand identity, hand permission, and top-deck permission; `test_card_transform_reset_and_category_gates_match_csharp` checks use-reset boundaries and the C# type-category gates; `test_copy_transform_copies_supported_csharp_mutable_card_fields` checks the persisted subset of `Card.CopyFrom`; `tests/tests_u3_wire_contract.py::test_csharp_transform_event_readers_accept_python_class_44_and_45` sends Python events through the extracted C# class-44/45 readers | Shared native transform helper; PvE/PvP transaction adapters and Records-backed trigger outcomes not exercised | Missing: historic gem values for rows transformed before the new column, C# active-effect/boon/activation-data copy and retarget semantics, direct-transform gem filling/alternate templates/irreversible behavior, card-created and chain-ability cleanup, transform transition state/order regression, the AcceptedStartingHand chain exception, and Unity transform/zone acceptance |

C# paths below are relative to
`hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/`.

| System | C# entry points | Server owners | Authored input | Evidence / remaining acceptance |
| --- | --- | --- | --- | --- |
| Turns, stops, priority, reconnect | `Mechanics/TurnPhaseState.cs`, `Mechanics/GameActions/PriorityWindowAction.cs`, `Mechanics/EndTurnState.cs` | `session.py`, `phases.py`, `turn_states.py`, `lifecycle.py`, `pvp_lifecycle.py`, `adapter.py` in `rules_port` | Phase stops and saved action/chain descriptors | Existing implementation; complete reconnect and two-client priority traces still required. Champion shield expiry covered in both checkpoint formats. `ReadyState` armor reset and the `CantReady`/`CantReadyNormally`/`CantReadyNormallyHidden` IntAttr gates apply in `ready_cards_for_turn`. |
| Timing, resource plays, costs, thresholds | `Mechanics/Transactions/PlayTroopTransaction.cs`, `PlayResourceTransaction.cs`, `ActivateAbilityTransaction.cs`, `Session.cs:CanActivateAbility` and charge payment | `rules_port/transactions.py`, `card_transactions.py`, `resources.py`, `costs.py`, `static_rules.py` | Ability costs, casting behavior, thresholds, target maps | Existing validation; exhaustive invalid-intent and multi-part payment rollback parity not certified here. Free plays require `OwnerCanPlayForFree` or the controller's champion `CanPlayCardsForFree` and project a zero effective cost. `Mobilize` reduces the payment by two per ready troop tapped (capped by the card's value, validated against the client's `CardsToMobilize`), and `CanIgnoreCardsThresholds` bypasses threshold checks. `ChargePointCostModifier` is source-card scoped as in `Card.GetChargePointCostModifier`; `test_charge_point_cost_modifier_applies_to_the_plan` checks separate champion and troop contexts. Records-backed timed modifier resolution, both mode adapters, and Unity updates remain open. |
| Chain and choices / partial | `Mechanics/GameActions/PriorityWindowAction.cs`, `ResolveTopOfChainAction.cs`, `WaitForTriggeredAbilitiesAction.cs` | `rules_port/kernel.py`, `chain.py`, `resolution.py`, `async_bridge.py`, `session.py` | Native class-39 activation now honors prompt-before-push; chain notification precedes a `ResolveTopOfChain` GreenLight; card fixtures match the pushed descriptor, resolved, and removed instance IDs and emit one final ChainEmpty in both owner models | Focused lifecycle/event assertions exist; a same-trigger push-to-removal trace, trigger-family ordering, reconnect through every checkpoint, and Unity chain UI still need acceptance traces. |
| Combat declarations and assignment | `Mechanics/Combat.cs`, `CombatResolver.cs`, `Card.cs:CalculateTotalCombatDamageToDeal`, `Card.cs:CurrentAttackValue` | `rules_port/combat.py`, `combat_rules.py`, `combat_damage.py`, `static_rules.py` | Keywords, blocker order, multiplier rules, Gladiator role bonus | Shared damage callback now reports absorption including prevention; minimum-to-kill is applied after shields. `Gladiator` projects to attack while the controller is active and to defense while defending, with `GladiatorBoth` applying both. Moving a troop to the warzone applies an opposing champion's `OpposingTroopsEnterPlayExhausted`/`OpposingNonArdentTroopsEnterPlayExhausted` flags through the shared played/token/effect entry paths. Full keyword/simultaneous parity remains open. |
| Damage and prevention | `Session.cs:DamageCard`, `DamageChampion`, `Mechanics/DamageShield.cs`, `Card.cs:ResetArmor`, `Card.cs:AddDamageShield` | `rules_port/damage_effects.py`, `context.py`, `lifecycle.py` | DamageShield add/consume order, DamageMultiplier, DamageImmunity modifier fields, Armor/ArmorUsed, additive received modifiers | Focused ordering/accounting tests below. `rules_port_order` preserves C# add order across permanent and temporary stores; older checkpoints use the documented fallback. Armor consumption is ordered after shields, `ArmorUsed` persists in the permanent store and resets for every card at Ready. Additive `DamageReceivedModifier`/`*ReceivedModifier`, source `PreventMy*` and noncombat `DamageChampionMultiplier` follow `Session.DamageCard`/`DamageChampion`. Additive `PreventDamageFromCardsThatHaveOwnersThreshold`, the champion `NonCombatDamageReduction`/`OpposingNonCombatDamageReduction` adjustments and the `ChanceToPreventCombatDamage`/`ChanceToPreventNonCombatDamage` roll (same RNG consumption point, including a stored zero) also follow `Session.DamageCard`. |
| Statics and targeting | `Mechanics/Modifiers/DamageMultiplierModifier.cs`, `DamageImmunityModifier.cs` | `rules_port/static_rules.py`, `targeting.py`, `filters.py` | Target templates, conditions, typed modifier operands | Continuous rule modifiers now check their authored target filter. Numeric multipliers preserve multiplication, replacement, separate speed scopes, and zero. Synthetic champion continuous-rule projection remains incomplete. |
| Counters, deaths, zones | `Mechanics/Card.cs`, `Session.cs`, `Mechanics/AbilityManager.cs` | `rules_port/death_effects.py`, `context.py`, `runtime_helpers.py`, `lifecycle.py`, `triggers.py`; `pvp_db.py` projection | Counters, durations, zone collections, Deathcry gates, `Session.GetBuryBonusForPlayer`, end-of-turn damage reset | End-of-turn, end-of-next-turn, owner/opponent turn and after-ready duration boundaries follow C#; burying adds other champions' `BuryBonus`; an opponent's `OpposingDeathcriesCantTrigger` blocks Deathcry and `CantHealAtEndOfTurn` cards keep damage through the reset. `Session.Lifebound` returns the active player's marked discard cards at StartTurn. Noncombat death timing and damage-bound teardown still need parity work. |
| Trigger discovery | `Mechanics/Card.cs:RegisterTriggerEvents`, `GetTriggeredAbilities`, `PassesCollectionFlagRequirements`; `AuthoritativeSessionBase.cs:HandleGameEvent`; `Session.cs:m_EventMaps`; `Mechanics/Triggers/Conditions/` | `rules_port/triggers.py`, `trigger_discovery.py`, `conditions.py` | Event source/target, conditions, collections | Damage now publishes prevention and target-damaged events with payloads. Entering play publishes `CardEnteredZoneEvent` -> `AsEntersPlayEvent` -> `CardInspiredEvent` (self enters-play abilities and Inspire), and `None` collection flags are unrestricted like C#. Candidate discovery scans both owners' current card collections independently of the event's `zones` payload; the native dispatcher checks each listener's authored collection flags and conditions. Focused Records contracts cover a Void listener on an opposing Discard entry and on TurnStartedEvent. `HandleGameRulesTriggers` equivalents run after authored listeners: Rage on attack and Momentum for the caster's warzone troops on a resource cast (+1/+1 until the owner's next StartTurn). Not every trigger family has an acceptance scenario. |
| Ability interpreter and champion powers | `Mechanics/Abilities/`, `Mechanics/Transactions/ActivateAbilityTransaction.cs` | `rules_port/resolution.py`, `context.py`, `abilities.py`, `card_transactions.py`, `ability_usage.py` | Variables, effect groups, costs, uses, cooldowns | Repeating children (including the deprecated loop-count fallback), current Records variable subclasses, use limits, cooldowns and key duration boundaries have focused coverage. Verdict builds the authored good/bad choice tokens and activates the built-in choose/play ability. Full option, statistic and champion-modifier parity remains uncertified. |
| Client projection | `Session.cs`; `hex-private/HexClient/Assembly-CSharp/UIBattle.cs:OnChampionHealthChanged`, `OnCardUpdated` | `game_engine.py`, `rules_port/adapter.py`, HConnect and mode adapters | Valid SessionCardIds, event order, visible legal options | Existing wire events retained. No new real-client or live PvP acceptance performed. |

## Target filter and selection coverage

`hex-private/Records/AbilityTargetTemplate.jsonl` contains 1,773 target templates across 15
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
(`hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/Session.cs:2046-2100`,
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
Python records an internal `rules_port_order` when each shield is granted and
consumes permanent and temporary stores in that combined order, matching
`Card.AddDamageShield`'s single list. Champion shields use the existing
serialized session checkpoint because champions have no `game_cards` row;
ordinary shields use the existing mutation columns through `pvp_db` APIs.
Shield grants, consumption and champion expiry share a reentrant lock. Older
checkpoints without order metadata retain a deterministic permanent-then-
temporary fallback because their original cross-store order was not persisted.
Damage outcome objects are local to each call.

`Session.DamageCard` clamps minimum-to-kill after prevention and returns damage
dealt plus shield/armor prevention through its out parameter. `CombatResolver`
subtracts that value before assigning to another blocker. SpiritDrain heals the
actual damage, including noncombat damage. The server now uses `DamageOutcome`
for that accounting and emits `DamagePreventedEvent`, then replacement checks,
then healing/mutation/projection, then dealt/received trigger events.

### Metadata evidence

Current `hex-private/Records/AbilityEffectTemplate.jsonl` contains 8 DamageMultiplier,
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

`tests/tests_damage_rules.py` exercises seven shared scenarios:

1. Four damage doubled, then shielded by three, deals/heals five; verify
   persisted damage and prevention/replacement/mutation/trigger ordering.
2. A temporary one-shot shield added before a permanent shield absorbs first
   and leaves the later shield untouched, matching C# list order.
3. Immunity leaves shield capacity untouched; a replacement sees damage after
   shields and prevents mutation/healing.
4. Champion shields survive JSON restoration, obey combat restrictions and
   one-shot consumption, and expire through both PvE and PvP turn transitions.
5. Multiplier stacking, scope separation, replacement and zero values.
6. Six combat damage against two defense plus a three-point shield absorbs
   five and heals two; a fully shielded blocker consumes damage before the next
   blocker is considered.
7. A continuous rule affects only the authored target set.

These are isolated server tests using real SQLite mutation helpers and a stubbed
stat projection/trigger recorder. They do not certify transaction ingress,
encoded packets, trigger-chain completion, Unity animations or HUD state.
The available client log was read before editing; it contains no reproduction
of these new acceptance scenarios.

Validation for the ordering change: `python3 tests/run_all.py --only
tests_damage_rules.py` passed all 12 tests, and
`python3 tests/run_all.py --only tests_effect_context.py` passed. Changed
Python modules passed `py_compile`, and `git diff --check` was clean. The
focused runner used disposable/in-memory databases, never live `hconnect.db`.
No HConnect process was running for a hot reload, and no live Unity acceptance
was performed.

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
disassembly, plus typed `hex-private/Records/AbilityTemplate.jsonl` and
`hex-private/Records/AbilityEffectTemplate.jsonl` fields. No card text was used to infer
these behaviors.

## Remaining work

The scope of the complete original-client port is not complete. In addition to
the uncertified rows above, the inspected damage path still needs:

- Champion-targeted continuous `CardModifier` leaves that are not IntAttrs
  (champion auras granting `DamageMultiplier`/`DamageImmunity`/stat rules)
  are not folded into a synthetic champion context; only champion runtime
  IntAttrs and the four champion flag IntAttrs are projected. No current
  Records card exercises the missing shapes.
- Historical cross-store ordering cannot be reconstructed for checkpoints
  created before `rules_port_order` was added. New grants sequence existing
  legacy shields in a stable fallback order before appending the new shield.
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
