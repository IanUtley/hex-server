# Hex server implementation specification

This is the compact architecture and operations contract for the private Hex
server. Read it before changing code and update it when a new, reusable
implementation rule is discovered.

`RULES.md` is the gameplay contract. This file defines ownership, persistence,
wire compatibility, data flow, debugging, and the recipe for implementing a
new behavior. Detailed evidence belongs in the focused documents listed at the
end, not in a dated work log.

## 1. Source of truth

Use the sources in this order:

1. The original client disassembly in `HexClient/` and client `Data/gamedata`
   define wire types, event expectations, and metadata when compatibility is
   uncertain.
2. `RULES.md` defines the server's intended gameplay behavior and records
   deliberate deviations from the original game.
3. This file defines module boundaries, persistence, encoding, and operational
   invariants.
4. Focused documents provide evidence and subsystem detail:
   `docs/CLIENT_SERVER_PROTOCOL.md`, `docs/GAMEDATA_MODEL.md`,
   `docs/CAMPAIGN.md`, `docs/FROST_RING_ARENA.md`,
   `abilities/ABILITIES.md`, and `docs/PRIVATE_SERVER_FEATURES.md`.
5. Tests and the running code are the current executable check. If they
   disagree with a document, fix the implementation and the document together;
   do not silently establish a new rule in code.

When reverse-engineering a behavior, record the observation, the inferred
contract, and an acceptance test. A future implementation should be generated
from the contract rather than from a card-name or endpoint-name guess.

## 2. Run and validate

From the repository root:

```bash
bash restart.sh
```

The server listens on TCP `9933`; the HTTP compatibility/auth proxy listens on
`8081`. `restart.sh` initializes or upgrades the database, runs one-off
`migration.py` work when present, compiles the runtime, and starts HConnect,
the proxy, tournament service, and replay worker.

Reload-only modules can be refreshed without restarting HConnect:

```bash
kill -USR1 "$(pgrep -f '[h]connect_server.py' | head -n1)"
```

During development, Supervisor can own all four long-running processes after
installing the dependencies from `requirements.txt`:

```bash
HEX_USE_SUPERVISOR=1 bash restart.sh
supervisorctl -c supervisord.conf status
```

The Docker entrypoint runs the same `supervisord.conf` in the foreground after
database bootstrap. Supervisor restarts a failed service and writes the
service logs under `/tmp`; use `supervisorctl` for targeted stop, start, or
restart operations. `restart.sh` retains its direct-process mode when
`HEX_USE_SUPERVISOR` is unset.

Use a full restart after changing `hconnect_server.py`, startup wiring,
encoders, schema initialization, or process configuration. Do not run tests in
parallel when they use SQLite or the shared runtime database.

The repository includes a pre-commit hook for syntax checks and the quick core
test set. Enable it once per checkout with:

```bash
git config core.hooksPath .githooks
```

The hook uses the disposable test database created by `tests/run_all.py` and
does not access the live `hconnect.db`. Run `python3 tests/run_all.py` before
pushing to `main` for the full suite and golden verification.

For client-visible failures, read the client log first:
`/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Hex_Data/output_log.txt`.
Search for `Error`, `Exception`, `Command handler not found`,
`KeyNotFoundException`, `NullReferenceException`, and
`UIBattle|...|Pushing/Popping UI state`. Then compare server request/session
logs and database state.

For transient RulesPort phase/turn investigations, the server has an opt-in
debugger launcher and compact native/compatibility snapshots. Start it with:

```bash
HEX_DEBUGPY=1 HEX_RULES_PORT_TRACE=1 bash restart.sh
```

Attach a Python debugger to `127.0.0.1:5678`. The listener does not wait for a
client unless `HEX_DEBUGPY_WAIT=1` is set. Snapshots are written to
`/tmp/hconnect_log.txt` at RulesPort attachment, scheduler drives, projection,
and AI-turn boundaries; ordinary restarts do not enable either feature.

## 4. Module ownership

| Module | Owns |
|---|---|
| `hconnect_server.py` | TCP framing, connection lifecycle, legacy transaction compatibility, and protocol glue |
| `application/dispatcher.py` | application/session request dispatch and transaction orchestration |
| `services/__init__.py` | authoritative client-service registry and handler lookup |
| `services/*.py` | one client-request family per module; new handlers validate input and call domain APIs |
| `services/auction.py` | retained Auction House API surface; currently an intentional stub |
| `game_engine.py` | session event types, event construction, and event serialization |
| `battle_engine.py` | legacy rollback implementation for turn phases, priority, auto-pass, chain state, and phase persistence; normal live gameplay uses `rules_port/` |
| `rules_port/` | Semantic port of HexClient/Game.Shared rules; owns C#-ordered session/action, phase, priority, chain, combat, transaction, ability, trigger, and target primitives |
| `game_session.py` | session lifecycle and DB-backed session state |
| `ai.py` | AI turn, card-choice, and combat decisions |
| `abilities/` | metadata-driven ability parsing, targeting, conditions, effect leaves, and the shared `EffectContext`/`AbilityBuilder` adapters |
| `ability.py` | compatibility facade for the `abilities/` package |
| `campaign.py` | campaign protocol and authored campaign state; campaign SQL migration into `pve_db` remains work in progress |
| `gamemodes/` | mode-specific orchestration (PVP, FRA, tournament) |
| `static.py` | all schema DDL and server-owned seed/configuration data |
| `db.py` | one SQLite connection, transactions, and compatibility persistence facade |
| `profile_db.py` | profile, champion, deck, collection, inventory, mail, social, and store persistence API |
| `pve_db.py` | campaign/FRA/encounter persistence API |
| `pvp_db.py` | shared session, card, and battle persistence API used by PVE and PVP |
| `chat_db.py` | chat-history persistence API |
| `tournament_db.py` | tournament, bracket, signup, and tournament-match persistence API |
| `replay_db.py` | replay event/index persistence API |
| `replay.py` | replay packaging plus list/fetch compatibility helpers |
| `replay_server.py` | polling worker only; no replay business logic |
| `encoder.py` | ObjFmt response and binary event encoding |
| `proxy.py` | HTTP authentication, payment-link compatibility, news, and static proxy endpoints |

`practice.py` is not a service: Practice is one client action and follows the
normal session path. The old `services/practice.py` and replay browser module
were removed. `auction.py` stays because Auction is a substantial unimplemented
API surface. Replay packaging stays in the root `replay.py` even if playback
endpoints remain incomplete.

The `rules_port/` package is the semantic migration boundary for battle rules;
see [`rules_port/README.md`](rules_port/README.md) for its contracts and
adapter responsibilities. `application.player_transactions.classify_player_transaction`
remains the protocol decoder, while `rules_port.wire.submit_classified_transaction`
normalizes typed intents and validates them against the authoritative port
session. `rules_port.adapter.rules_session_for` caches that host on a live
session wrapper; `enable_rules_port` supplies the SQLite mutation and PvP-facts
adapters, and a newly loaded wrapper rehydrates from its namespaced snapshot.

`restart.sh` enables live RulesPort attachment (`HEX_RULES_PORT_AUTO_ATTACH=1`)
by default. Payload-bearing card, ability, choice, discard, combat, and phase
transactions are consumed by the port, acknowledged on both success and
rejection, and never reinterpreted by a legacy handler. Set
`HEX_RULES_PORT_AUTO_ATTACH=0` only as an explicit rollback switch while
diagnosing a migration regression. The port requires typed nested values from
the decoder; it never guesses card IDs or ability targets from display text.
When a rule pauses for UI input, its continuation is persisted and the matching
typed response resumes the same ability instance before the next priority
event. Debug-cheat and non-gameplay probes remain outside this boundary.
After each RulesPort scheduler tick, the host persists the post-action-stack
snapshot as well; this keeps a completed chain resolver from reappearing on a
reconnect and blocking the next card transaction. A settled manual ability in
First/Second Main also rebuilds the metadata-derived `PlayerOptionList` before
returning the normal green light.

The service registry is the first dispatch path. Unsupported or not-yet-
converted service types may use the compatibility path in HConnect until their
handler is extracted. New service types belong in the registry and a service
module; do not grow another dispatch dictionary in `hconnect_server.py`.
Several older extracted handlers still contain direct SQL; that is migration
debt covered by the same rule in the database section below.

Ability leaves have a compatibility boundary while the framework is being
refactored. Existing leaves may keep the historical positional ABI, but new
simple leaves should use `abilities.framework.context.EffectContext` through
the `@effect` decorator. `AbilityBuilder` must wrap the authoritative
`AbilityGraph`/`AbilityInstance` and reuse its costs, target templates, typed
fields, ordering, conditions, and continuation behavior; it must not introduce
a parallel card-rules source.

Resource and cost transitions are likewise RulesPort-owned. Use the typed
resource transitions for current/total pools, thresholds, charges, spell
points, resource-play resets, and payments; mode services and AI may only
project their returned deltas into SQLite and client events. The raw
player-ID variants are for the tournament checkpoint schema, while the
canonical `player`/`ai` variants are for Practice/PvE sessions.
`AbilityCostPlan` and its application transition own numeric ability costs;
the host must not decrement those counters directly after planning.
Attached-session card display costs must use `rules_port.static_rules.effective_cost`
as well, so the client-visible cost and the payment validator cannot be fed by
different evaluators. The historical `abilities.framework.cost_mod` path is
reserved for explicitly disabled rollback sessions.

## 5. Database contract

There is one logical SQLite database, not one file per domain. Domain modules
are ownership boundaries and stable APIs, so storage can later move to separate
databases or PostgreSQL without changing handlers.

- `static.ensure_schema()` in `static.py` is the only place that creates or
  alters schema for fresh databases. No service, mode, engine, or handler may
  execute DDL.
- `db.py` owns `_db`, WAL/connection setup, transaction primitives, and the
  compatibility `db_*` facade. New reusable SQL belongs behind a domain API;
  old names may remain in `db.py` while callers migrate.
- New code imports `profile_db`, `pve_db`, `pvp_db`, `tournament_db`, or
  `replay_db` according to ownership. A cross-domain operation uses one
  explicit transaction and calls each domain API with the same connection.
- SQL is parameterized. Reads return stable rows or plain data structures;
  rows returned through `db.connect()` (including the shared `_db`) support
  both `row["field_name"]` and legacy numeric indexing. Prefer field names in
  new DB/domain code so callers do not depend on SELECT-column order;
  writes make their transaction boundary explicit. Do not commit from a leaf
  helper when the caller is coordinating multiple writes.
- Existing direct SQL in the large legacy handlers is migration debt. Do not
  add to it: extract a named domain function when touching that path.
- `HEX_DB_PATH` selects the database. Keep the database, `-wal`, and `-shm`
  files in the same persistent directory. SQLite is suitable for development
  and small groups; 32-player tournaments may require PostgreSQL and a pool.
- Schema changes go in `static.py` and an idempotent one-off `migration.py`
  together. `restart.sh` runs and then removes the migration. Never replace an
  existing player database with repository seed data.

Important persistence entities include `users`, `champions`, `decks`,
`collections`, `card_instances`, `player_inventory`, `emails`, `campaigns`,
`game_sessions`, `game_cards`, `session_events`, tournament tables, and the
client-derived metadata tables. `game_cards` is the authoritative per-session
card representation; resolve its `template_guid` through `card_templates`.
Store unsigned client UIDs as text where SQLite signed integers are unsafe.

Practice sessions (`Session-*`) do not write replay event or transaction
capture rows. Tournament cleanup removes stale tournament `game_sessions` and
`game_cards` after replay generation is safe. The replay worker retains the
generated artifact for its configured retention period, then removes its
`game_replays`, `session_events`, and `session_transactions` rows and the
expired artifact file.

## 6. Protocol invariants

The server is authoritative. A client transaction is intent, not a state
snapshot. Validate the server's phase, priority, ownership, current zone,
cost, target, and transaction identity before mutating state.

- HConnect messages use the client-compatible binary framing and ObjFmt
  responses. Session events are serialized into the `3055` event wrapper.
- Client transactions use the `3029` `PlayerTransaction` wrapper. The client
  sends one transaction at a time. A handled transaction that does not produce
  a normal `3055` sync must receive an empty transaction acknowledgement, or
  the next client transaction may be dropped.
- Fire-and-forget requests must not receive an invented response. Check the
  client handler before replying.
- Every `SessionCardId` field must contain a client-recognized UID type. In
  particular, `PlayerUpdated.ChampionId` must be the real champion session-card
  ID, never `UID(0)` or an undefined type.
- When moving a card, send `CardUpdated` with the destination collection before
  `CardMoved`. `CardDrawn` does not change the client's zone by itself.
- For every top-level chain item, allocate one instance ID and use it in the
  persisted stack item, `AbilityPushedOnChain`, `TopOfChainResolved`, and
  `RemovedTopOfChain`. Card plays may use the client's built-in
  `PLAY_CARD_ABILITY_TEMPLATE_ID` as the chain-rendering template.
- Inventory, card, resource, phase, priority, and player events must be built
  from the same authoritative state after the mutation. A bare fresh `Game`
  can reset the client's displayed resources or champion to defaults.
- Hidden zones are filtered per recipient. The opponent must not receive card
  identity, hand contents, or private deck information unless an explicit game
  rule reveals it.
- `DisableInterface(true)` latches client-side (`UIBattle.m_DisabledInput`) and
  is only cleared by a later `DisableInterface(false)`. While latched,
  `HandleInputs` silently drops every button action (charge power, Pass), though
  mouse card clicks still work. Any flow that disables a client (e.g. the PvP
  mulligan waiting player) must send `DisableInterface(false)` before handing
  back control.

### ObjFmt minimum rules

- Little-endian `int`, `uint`, and `ulong` values are hexadecimal followed by
  `;`; booleans are raw `0`/`1`.
- Strings and byte arrays use `<length>;<raw bytes>` with no trailing
  separator. DateTime is `MM/dd/yyyy HH:mm:ss`.
- Lists encode a count followed by elements. Element type/size indexes and
  field order must match the concrete client type.
- `[DataMember(Order=N)]` fields are emitted in the client's effective order;
  higher order values are later. Use the client's loaded namespace and concrete
  enum representation.
- The 32-bit Mono client has type-resolution limitations. Verify new generic
  type strings against `HexClient`; do not introduce a
  `Dictionary<ulong, T>` response without evidence that the client can load it.

### Card zones and event order

The server's normalized `game_cards.location` values are `deck`, `hand`,
`warzone`, `PlayedResources`, `void`, and `discard`. The ordered `position`
column is scoped by session, owner, and location. A zone mutation updates the
DB first, then publishes the appropriate `CardUpdated`, movement, draw, play,
resource, or combat events in client-compatible order.

## 7. Gamedata and generated data

The original client `Data/gamedata` is the primary source. `Records/` is the
checked-in extraction fallback. The seed pipeline is:

```text
client gamedata / Records
        -> AssetExtraction/gamedata_seed.py and extractors
        -> static.py seed materialization / generated runtime data
        -> SQLite metadata tables
        -> generic ability, targeting, playability, and event code
```

Card templates provide identity, type, cost, stats, tags, and ability links.
Ability templates provide triggers, costs, conditions, targets, and ordered
effects. Target/filter/condition/counter records must drive options and effect
amounts. Rerun the appropriate extractor after gamedata changes; never hand-
edit generated talent seed blocks.

When a typed field is missing or demonstrably wrong, add a small compatibility
adapter and document why. Do not create a handler branch for a card name when
the same behavior can be represented by metadata.