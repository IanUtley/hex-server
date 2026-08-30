# Private Server Feature Checklist

This is the current implementation map for the private Hex server. A checked
item means that the behavior exists in the server and has either a focused
test, a client validation, or both. It does not mean that every edge case has
full parity with the original game. An unchecked item is known work rather
than a promise that the feature is entirely absent.

The gameplay rules are defined in [RULES.md](../RULES.md). The wire-level
overview is in [CLIENT_SERVER_PROTOCOL.md](CLIENT_SERVER_PROTOCOL.md).
Frost Ring Arena implementation details are in
[FROST_RING_ARENA.md](FROST_RING_ARENA.md).

## Server foundation and persistence

- [x] HConnect TCP server on port 9933.
- [x] HTTP proxy on port 8081 for local Steam-auth-compatible login and
  news/status endpoints.
- [x] ObjFmt request/response encoding for profile, store, mail, load-balancer,
  tournament, and related services.
- [x] Custom binary session-event encoder and typed event dispatch to clients.
- [x] SQLite schema and reusable database helpers for users, profiles,
  inventory, currencies, decks, sessions, cards, mail, chat, and tournaments.
- [x] Persistent authoritative battle state, including card positions and
  discard/graveyard ordering across reconnects.
- [x] Session-event recording used by debugging and replay capture.
- [x] Fresh-database static-data loading from the original client `gamedata`
  through `HEX_GAMEDATA`/`GAMEDATA`, with checked-in fallback seeds.

## Profile, collection, store, and decks

- [x] Login/profile initialization and delayed inventory synchronization.
- [x] Server-owned card instances with stable IDs, ownership, location, and
  mutable state.
- [x] Booster opening and full-set grants with normal-copy limits and
  alternate-art exclusion.
- [x] Race-specific Crayburn Castle reward chests with authored five-card
  pools, direct no-spin opening, collection synchronization, and standard
  (non-alternate-art) card printings.
- [x] Store items, purchases, redemption codes, currencies, stardust, and
  profile pushes.
- [x] Deck creation/loading, champion selection, talents, and deck validation
  paths used by constructed and limited sessions.
- [x] Extended-art and card-variant handling in collection generation.
- [x] Immediate client inventory removal for direct chest/pack opening using
  the client-compatible `InventoryUpdated` event; other inventory paths still
  need broader auditing.
- [ ] Auction House protocol and persistence.

## Mail and notifications

- [x] Text mail delivery to a full identity or display name, with persisted
  inbox rows and client-compatible send responses.
- [x] Inbox and sent-mail listing, unread-count, mark-read, inbox-delete, and
  sent-mail-delete service paths.
- [x] Client-compatible Mail UID encoding and sender-scoped sent-mail deletion.
- [x] New-mail counter notifications on login and when mail is delivered to an
  online recipient.
- [ ] Mail attachments, gold/platinum transfers, COD, and claimable mail
  rewards; current text-mail responses return empty attachment collections.

## Session setup and turn engine

- [x] Load-balancer/session service flow: start, join, ready, and session
  metadata responses.
- [x] Game-session creation, player identity/order, champion IDs, deck setup,
  and initial card/session-ID publication.
- [x] Server-driven phase cycle covering setup, mulligan, main phases, combat,
  discard, end turn, and end game.
- [x] Priority/GreenLight events, configurable stops, auto-pass, and the
  client's one-transaction-at-a-time rule, including the empty 3055
  acknowledgement required after handled transactions.
- [x] Play/draw selection and mulligan transactions.
- [x] Resources, thresholds, played-resource state, charges, and dynamic card
  options used by the tested rules.
- [x] Player and AI turn progression, victory/defeat, withdrawal, and session
  cleanup.
- [x] Campaign encounter deck personality selection with deck-level strategy
  taking precedence over campaign personality fallback.
- [x] Reconnect and state republishing for active sessions.
- [ ] Full client-equivalent draw-first hand reorder flow.
- [ ] Deck-exhaustion loss behavior on every draw path.
- [ ] Campaign hand-size rules everywhere; campaign uses 10 while PvP uses 7,
  and remaining paths need auditing.
- [ ] A reliable automated two-real-client PvP harness for all setup and combat
  flows.

## Card state and combat

- [x] Attack declaration, blocker declaration, combat priority, damage
  assignment, combat cleanup, and the corresponding client events.
- [x] Ready/exhausted state, summoning sickness, speed, attack/defense,
  damage, and other mutable card state.
- [x] Death state is attached to troops that die, is required by death-based
  triggers, and is cleared when a card returns to the warzone.
- [x] Tested handling for Swiftstrike, Steadfast, Flight, Skyguard, Crush,
  lifedrain, unblockable/cannot-block effects, rage, gems, and state-based
  troop deaths.
- [x] Graveyard/deathcry movement and ordered discard/graveyard placement.
- [x] AI legal-action selection, combat heuristics, and delayed AI turns.
- [x] Reconnect publication of card representations, player state, and zone
  contents.
- [ ] Complete parity for all combat keywords, prevention/replacement effects,
  simultaneous damage, and every edge case in the original client.

## Gamedata-driven abilities and rules

- [x] Extraction of card templates, abilities, effects, targets, conditions,
  counters, gems, champions, talents, encounters, chests, and set metadata.
- [x] SQLite-backed card-template and ability metadata used by the runtime.
- [x] Shared ability resolution model with effect groups, a stack/chain model,
  trigger dispatch, target options, and authoritative event publication.
- [x] Tested leaves for drawing, damage, healing, stat changes, summoning,
  transforming, moving, burying, resources, thresholds, counters, Deathcry,
  Inspire, Deploy, and chained effects.
- [x] Card-specific behavior represented through metadata and a small custom
  registry where the original data needs a server-side adapter.
- [x] Set 1 sweep coverage: 425/425 abilities resolved cleanly in the focused
  server sweep at the time of the last validation.
- [ ] Remaining ability leaves such as full discard/void, tap/untap,
  grant/play/fire-event/reveal/revert/store-targets/verdict behavior.
- [ ] Complete generic target filters and all target-selection modes.
- [ ] Full condition trees, variables/output variables, option lists, effect
  durations/teardown, uses/cooldowns, and every trigger type.
- [ ] Eliminate the remaining game-text compatibility fallbacks where typed
  gamedata fields are available.

## Campaign, tournaments, chat, and replay

- [x] In-game chat commands and persisted chat history paths.
- [x] Tournament rooms, deck construction/session start, entry, game linking,
  completion, forfeit/concede, and participant-scoped completed history paths.
- [x] Campaign profile/resource/path state and the implemented AZ0/server-driven
  dungeon flows.
- [x] Frost Ring Arena deck assignment, twenty-opponent roster generation,
  challenger-list/history responses, boss detection, and battle result hooks.
- [x] Frost Ring Arena run totals: non-boss wins add gold pouches, boss wins
  add treasure chests, and the totals are shown in the arena lobby.
- [x] Session-event replay capture.
- [ ] Complete Frost Ring Arena cash-out loot delivery; the current cash-out
  response returns accumulated gold but does not yet populate `AllLoot` or
  convert the chest total into inventory chests.
- [ ] Complete Frost Ring Arena start-to-finish parity, including all rewards,
  encounter effects, challenges, buyouts, and client-equivalent challenger
  match lifecycle.
- [ ] Tutorial battle event routing and rendering through the live client.
- [ ] Replay list/fetch service endpoints and complete playback validation.
- [ ] All campaign zones, rewards, encounters, and client-equivalent campaign
  rules.
- [ ] Resolve the remaining transient completed-tournament-history filtering
  issue seen in successive lobby updates.

## Operations and distribution

- [x] Restart workflow with schema initialization, one-off migrations, and
  focused compilation checks.
- [x] Debug logging for server requests, proxy traffic, client logs, session
  events, and card/ability resolution.
- [x] Docker image and single-task deployment documentation for ports 9933 and
  8081.
- [x] Runtime `gamedata` mount/ENV_VAR support so a fresh image can populate
  client-derived static tables without copying the original blob into the
  repository.
- [ ] Preserve live sessions through a server restart.
- [ ] Production-grade authentication, TLS, durable multi-instance storage,
  and operational monitoring beyond the current small-group deployment model.

## How to read this list

The checked list describes the server's current tested surface, not a claim
that the original C# client has been replaced line-for-line. The unchecked
items are the main parity, coverage, and operational gaps. For a new card or
effect, start with the client records under `Records/`, verify the extracted
metadata in the static tables, add or correct the generic resolver, and then
add a focused test before changing a card-name-specific rule.
