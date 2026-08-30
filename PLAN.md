# Hex Private Server — Current Status (2026-08-04)

## Working Features
- **FRA end-to-end**: Deck picker → challenger list → fight start → mulligan → phase transitions
- **Card playability**: Cards show golden outlines, can be played from hand
- **Resource system**: Play 1 shard per turn, resources/thresholds update via display events
- **Mulligan Replace Hand**: Works correctly, N-1 cards drawn each redraw
- **Card zones**: `!zones` command, proper DB location tracking, CardMoved+CardUpdated events
- **Deck save/load**: Full CRUD, champion + ActiveGems + cosmetics persist
- **Store**: 82 items including 28 starter/collector decks from gamedata
- **Champion creation**: Grants race-specific starter deck once per race
- **Chat commands**: 20+ debug commands with `me`/`opp` target support
- **AZ0 campaign panorama**: Full flow with real race configs
  - Panorama conversation chain: intro NPC → trainer NPC (battle convo) → quest-giver
  - Per-race `_AZ0_RACE_CONFIG`: bundle, prefab, intro/trainer/quest NPC names,
    conversation GUIDs, training encounter GUID, AI champion GUID, gameboard
  - **`conv_done` SendEvent** advances the chain; trainer stays hidden until the
    intro convo finishes; trainer node converts to an Encounter after the battle convo
  - **`enc_cancel`** returns to the panorama with the trainer still launchable
  - **`start`** pushes a `gamestarted` notification → client transitions to Battle
  - **`choice_battle_yes/no`** toggle `RaceTutorialBattleUnlocked` in PublicState
  - **Quest-giver convo end** switches the campaign row to DUNGEON (Castle Crayburn)
- **Training battle (encounter) live**: Coin toss, Play/Draw dialog, 7-card mulligan,
  battle scene loads, correct HUD portraits (trainer NPC name for AI, champion name for player)
- **Coin toss / turn player**: `push_game_started` takes `player_first` — tutorial battles
  always give the player Heads (first turn); otherwise the turn player is randomized.
  Tutorial flag lives in campaign state (`TutorialDone`), set after the training battle is won
- **AI opening hand**: AI gets a 7-card hand (first 7 of the shuffled AI deck → Hand)
- **Sequential mulligan**: turn player mulligans → opposing player is then asked → back to
  the other if they didn't keep → only leave the Mulligan phase when BOTH have kept.
  - AI keeps if its hand has a shard; otherwise it mulligans
  - Each mulligan redraws one fewer card (7→6→…→0); at 0 cards it is forced to keep
  - Every mulligan fully reshuffles that session's deck (mulliganed cards can return)
  - `_resolve_ai_mulligan` pushes `PlayerMulliganedHand`/`CardUpdated`/`AcceptedStartingHand`
    for the AI so the client shows its decisions
- **Turn/priority engine**: DB-backed `battle_engine.py` (state in `turn_order_json`).
  Player Pass Priority (3029 `PassPriorityTransaction`) advances the phase when both pass;
  the AI plays out its whole turn server-side (draw 1, play a resource in FirstMainPhase,
  pass all remaining priority), then hands the turn back
- **Server-driven auto-pass (phase stops)**: stop positions captured from
  `SetTurnPhasesTransaction` at battle start, persisted in `user_prefs`; non-stop phases
  are auto-passed server-side one phase per packet. `PickGoesFirst`/`Mulligan` never
  auto-pass; `StartGame` auto-advances (no client BattleState). Stale-pass guard via
  `m_TurnPhase` in `PassPriorityTransaction`.
- **AI turn pass-gated**: `_run_ai_turn` pushes one phase per packet (`AI_PHASE_DELAY=1.0s`),
  GreenLight per phase, pauses at human (opponent-stop) phases via `ResumeTopOfChain`
  GreenLight → `BattleStateInactivePriorityWindow`; resets `player_resource_played_this_turn`
  on handoff.
- **`!game_end victory|defeat`**: pushes a real battle `GameEnded` (class 2, 3055) via
  `commands.push_battle_game_end` + campaign `gameendnotify`; `!help` updated; `!update` fixed.
- **Champion charges**: granted on basic threshold play (player + AI), state in DB.
- **Champion templates rebuilt**: 69 selectable PvE champions with `is_player` flag (player
  resolves PvE starting-class portrait); `champions.talents` column + 2037 handler persists
  talents and returns full `ChampionBits` (incl. `ChampionTalents`, avoids `HandleTalentsUpdate` NRE).
- **Deck → champion link**: deck save/update (2089/2095) sets champion `last_deck_id`
  (fixes "Deck.0" deck-list miss).
- **`game_cards` denormalised**: `card_type` + `template_guid` columns → single lookup path
  (`JOIN card_templates ON ct.guid = gc.template_guid`) for player AND AI cards.
- **Migrations**: `migration.py` run-and-delete by `restart.sh`; fresh DBs from `static.py`.
- **License**: AGPL-3.0 (`LICENSE`) + `NOTICE` (no charging players; changes must be visible).
- **AI thinking pause**: `AI_THINK_DELAY = 3.0s` before the AI's turn, simulating the
  opponent thinking (each connection runs in its own daemon thread, so only that player's
  battle is delayed)
- **GreenLight / Pass button**: server pushes `GreenLight` after the keep-handler phases
  AND after every card play, so the client keeps showing the Pass Priority button
  (previously hidden — the card-play path pushed options but no GreenLight event)
- **Discard auto-skip**: the client auto-passes Discard with a no-op animation (sends NO
  transaction) when the hand fits within the max hand size, so the server was stuck at
  Discard forever. The pass handler now checks hand size ≤7 and advances Discard→EndTurn
  and hands the turn to the AI automatically
- **Event logging for replay**: `session_events` table records every SessionEventArgs batch
  pushed to any player (player + AI actions) via the `event_logger` hook in
  `make_network_packet`; `game_sessions` UUIDs stored as TEXT
- **Steam auth**: `/steam/login` returns `token=steam:<steamId>`; Steam ID is the authoritative player key
- **DB-backed sessions**: `game_sessions` (TEXT UID columns) + `meta` counter table; no shared in-memory state
- **Encounter static tables**: `encounter_scenes` (8 AZ0 training scenes), `encounter_deck_cards` (33 rows,
  60-card real AI decks from DeckTemplate m_DeckResources), `champion_templates` (76 playable race+class+gender)
- **News/carousel**: `/news/hex2_newspaper.png`, `/news/skg_white.png`, `NewsEvents.txt`
- **Inventory push**: Rewritten via encoder.py (was mis-encoded — collection sizes stayed 0)
- **Chests persist**: chest as inventory_bits keyed 9000+id in login push
- **Resources.Load paths**: must use full ResourceManager paths with `campaign/` prefix

## Current Blockers
1. **Withdraw (concede) — done.** Pipeline fix + gameendnotify + ALoc="" applied 2026-08-01.
2. **Panorama portrait blank after gameend** — `HandleOnGameEndNotifyMessage` only
   caches the campaign state (ClientCampaignManager.cs:719); unlike the `conv_done`
   response path, the unsolicited gameendnotify push never triggers a panorama VM
   rebuild. Visual GameObjects for NPC portraits persist from before the battle and
   may render blank when state changes (e.g. ALoc cleared). Conv_done works because
   it sends a Response to an active request. **Workaround**: resetting the campaign
   state to fresh clears the visual cache. **Future**: BepInEx Harmony patch to
   force a panorama rebuild on gameendnotify, or implement a cmpupdate handler that
   pushes a follow-up response.
3. **Slow initial mulligan** — the first Mulligan redraw takes ~8s, subsequent
   redraws are instant. Not from DB volume (batched + indexed) or missing card
   data (pre-caching deck CardUpdated revealed the top card and didn't help).
   Likely client-side first-time asset/template load for newly-revealed cards.
   **Future**: BepInEx patch or investigate client card reveal path.
4. **Champion card ID undefined-UID warnings** — client logs `SessionCardId
   undefined type` when a `PlayerUpdated.ChampionId` is `SessionCardId(UID(0))`.
   Every `push_player_updated` in the battle handler now passes `champ_id`.
   Debug commands (`!resource`/`!threshold`) still create a fresh Game with no
   champion id → warnings. **Fix**: set `player_champion_card_id` on the Game in
   `commands.py` too.
5. **Bloatcap playability flaky in SecondMainPhase** — playability not always
   refreshed on entering SecondMainPhase. Debug "Playability: skip" logging added;
   investigate whether SecondMain options push fires and why a 2-cost troop with
   met threshold is skipped.
6. **AI troop stats lost in Prep phase** — fixed via `_card_full_data` in the AI
   Prep loop (was sending CardUpdated with no CardDef). Verify AI warzone troops
   keep cost/threshold/atk/def/gems across turns.
7. **DUNGEON ViewModel missing-script crash**: client logs `The referenced script on
   this Behaviour (Game Object 'DungeonMapNode') is missing!` →
   `UIDungeonZoneViewModel.OnHandleStateChange` (SetNodeType deref, ~line 355)
   NullReferenceExceptions on the null `UIDungeonMapNode` entries. Asset forensics:
   `UIDungeonMapNode` IS compiled in `Assembly-CSharp.dll` and bound in
   globalgamemanagers.assets (MonoScript pathids 2407/3244); NO prefab file exists in
   the install (scan of all 248 loadable assets/bundles); level19 type-tree is stripped.
   Not an assembly or prefab-GUID mismatch → fixed client binary needs asset rebuild /
   `m_Script` rebind. PANORAMA workaround failed and was reverted; server back on DUNGEON.
   Node colors + map-driven AutoStart blocked (NRE aborts the node loop), BUT the castle
   background loads and direct encounter launches (`camp.encounter`) bypass the map.
8. **Combat system** — attack/block/assign damage not implemented
9. **Gem socket abilities** — not rendered (need EGemTypesNew→AbilityTemplate mapping)
10. **Replay playback** — event logging is recorded (`session_events`); the client-side
    `qreplaylst`/`replayfetch` serve endpoints are not yet implemented
11. **Custom conversations** — client only loads ConversationTemplates from local disk
    (`Data/Campaign/Conversations/*.conversation`) or the gamedata blob; no HTTP/network
    path for conversation templates. Not started.
12. **Champion card/battle name** — client shows the template name (e.g. Savvas) via
    `cardRepresentation.Name`, not the custom champion name (Victor) — not yet overridden
    server-side.
13. **Hand size** — server still hardcodes 7; campaign 10 / FRA-PVP 7 not wired.

## Next Steps — Crayburn Castle Dungeon Auto-Advance (planned 2026-08-04)
Playable server-driven quest chain despite the missing `DungeonMapNode` script: launch
each step directly (direct `gamestarted` bypasses the broken map; between battles the
castle backdrop shows, NRE is caught). Node colors stay uncolored client-side (script
gone) but the STATE still marks nodes visited/completed.

Node split is data-driven by the presence of `success`/`fail` conversations in
`_CRAYBURN_CASTLE["races"][race]["nodes"]`:
- **Conv-only (auto-advance, no battle)**: The Watchtower, The Drawbridge, Inner Bailey
- **Encounter (launch via `_launch_encounter`)**: Castle Gatehouse, Tower Gatehouse,
  Tower of Penworth (boss) — win → advance, loss → play the `fail` conversation

Chain to implement (`_advance_crayburn(state, race, from_node)`):
```
Entrance → The Watchtower conv → The Drawbridge conv → Castle Gatehouse ⚔
        → Inner Bailey conv → Tower Gatehouse ⚔ → Tower of Penworth ⚔
        → Quest End conv
```
- Conv-only step: push node conversation; on `conv_done` mark node completed + advance.
- Encounter step: set ALoc to node, launch battle directly; on `gameendnotify` (win) mark
  completed + advance to next node; on loss push the `fail` conversation, allow re-entry.
- After the boss, push the Quest End conversation; when done, campaign finished.
- Wire into existing `conv_done`/gameend handlers; entrance conv stays Margugram's
  quest-start handoff that opens the DUNGEON state.

## Key Technical Findings

### Conversation/Encounter flow
- Campaign messages: dt=110000 ServiceCampaign; responses use
  `CampSysGeneral+Response` root, unsolicited pushes use `CampSysGeneral+Request`
  (the client's `CustomNetworkMessage.Incomming` only routes pushes as `Request`)
- `handleNofityGameStarted` decodes base64 `GameSession` → `EncounterData.SceneTemplateId`
  → `BattleStateContext{PlayerDeckId=DeckID}` → `TransitionToState(EGameState.Battle)`
- Battle session is NOT pre-created — StartEncounter (22017) allocates the DB-backed session
- AI opponent: user_id=0 rows scoped by session_id in `game_cards`; AI UID fixed `UID.make(3,1000)`
- AI deck comes from `encounter_deck_cards` (real 60-card deck per race)
- 22031 ReadyToStartGame resolves: player deck via campaign→champion→last_deck_id,
  player champion GUID via `champion_templates`, AI deck from `encounter_deck_cards`,
  AI name = trainer NPC, player name = champion name

### Coin toss / Play-Draw / Mulligan phase order
- `push_game_started` takes `player_first`:
  - `True` → `turn_order=[player, ai]` → `TurnOrder[0]`=player → client `m_startingPlayerUID`=player
    → `StartVSScreen(true)` → coin is **Heads** (player wins)
  - `None` → `turn_order` is randomly shuffled (fair toss)
- Tutorial battles (campaign state `TutorialDone=False`) pass `player_first=True`; FRA and
  post-tutorial battles pass `None` (random)
- The client shows the Play/Draw dialog at `ETurnPhases.PickGoesFirst` (3), between
  PreGame (2) and Mulligan (4). The server previously jumped PreGame→Mulligan, skipping it.
- FIXED (hconnect_server.py ~2505): push order is now
  `PreGame → PickGoesFirst → Mulligan`
- `BattleStatePickGoesFirst` sends `ChoosePlayTransaction`/`ChooseDrawTransaction`
  (dt=3029 PlayerTransaction) — server safely ignores them (falls through to quit/surrender check)
- Tutorial auto-chooses: if `SessionFlags.IsTutorial`, `PushStateForPhase` calls
  `ChoosePlayFirst()` and auto-accepts the hand instead of showing dialogs

### GreenLight / Pass Priority button
- The client shows the Pass button only when `UIBattle.Instance.HasPriority()` is true,
  which is set EXCLUSIVELY by a `GreenLightSessionEventArgs` (class 48) for the player
- `BattleStateBase.RebuildPassButton()` returns `PassButtonType.None` when
  `!HasPriority()` → button hidden
- Server must push `GreenLight` after the keep-handler phases AND after every card play
  (the card-play path previously pushed options but no GreenLight — button stayed hidden)
- `EPriorityContext.Normal = 0`; `GainGreenLight` also enables GoToAttack/GoToEndPhase
  buttons and the turn-phase timer

### Mulligan flow (sequential)
- The client's `PushStateForPhase` for Mulligan pushes `BattleStateMulligan`; Keep → sends
  `AcceptStartingHandTransaction`; Redraw → `MulliganTransaction`
- Order: turn player acts → opposing player asked → back to the other if they didn't keep →
  leave the Mulligan phase only when BOTH have kept
- AI (`_resolve_ai_mulligan`): keeps if a shard is in hand, else mulligans drawing one fewer
  card per redraw until a shard or 0 cards (forced keep)
- Every mulligan fully reshuffles that session's deck in `game_cards` (position column),
  scoped by `session_id` so concurrent battles never interfere
- `BattleStateDiscard` (Discard phase) calls `LoseGreenLight()`+`Finish()` on OK and sends
  NO pass transaction when the hand fits max hand size → server must auto-skip Discard

### Event Order (Keep Handler)
```
CardUpdated(hand) → AcceptedStartingHand → AI mulligan resolve → phases(6) →
PlayerUpdated → GreenLight → PlayerOptionList
```
The `GreenLight removed` note in earlier notes is obsolete — GreenLight IS now required
and pushed (without it the Pass button never appears).

### Pass priority / turn engine
- `PassPriorityTransaction` (3029, class name in inner_bytes) detected by
  `b"PassPriorityTransaction" in inner_bytes`
- `battle_engine.py` TURN_PHASES: StartTurn→Ready→Prep→Draw→FirstMainPhase→SecondMainPhase
  →EndPhase→Discard→EndTurn; `advance_phase` switches turn player after EndTurn
- On pass: if player is at EndTurn/Discard (or hand fits, auto-skip Discard) → `_run_ai_turn`
  (draw 1, play resource in FirstMainPhase if able, pass rest, `AI_THINK_DELAY`=3s first)
- Otherwise advance one phase + GreenLight back to the player

### Card Playability
- `CanPlayCard` in UIBattle.cs:7649 checks `m_AcceptedStartingHand && (Usage & Play)`
- TemplateId must NOT be `ResourceId.Invalid` — `ShowCardSelectionType` returns early if it is
- `card_template_id` in `game_cards` stores instance IDs, not GUIDs — resolve via `card_instances`
- AI cards (instance IDs 8000+) were mixed with player cards (same user_id) — fixed with user_id=0
- Order matters: PlayerOptionList must arrive after phase transitions settle

### CardUpdated Requirements
- Must include `template_id` (valid GUID), `collection`, `card_type`, `cost`, `attack`, `defense`
- Thresholds/abilities come from `card_defs` lookup — must register card before `push_card_updated`
- Without `template_id`, golden outlines don't appear

### Resource Display Events
- `PlayerCurrentResourcePoolChanged` (33): Operation, Delta, NewValue
- `PlayerTotalResourcePoolChanged` (34): Operation, Delta, NewValue
- `PlayerResourceThresholdChanged` (35): Color(ECardShards bit-flag), Operation, Delta, NewValue
- ECardShards values: Blood=4, Ruby=8, Sapphire=16, Wild=32, Diamond=64
- threshold_json indices: {0:Blood, 1:Ruby, 2:Sapphire, 3:Wild, 4:Diamond} — must convert to flags

### Card Movement
- `CardMoved` (50): Only sets TO collection — must send `CardUpdated` with new collection FIRST
- `ResourceCardPlayed` (16): Auto-moves to PlayedResources + animation
- `CardDrawn` (7): Stats only, doesn't move — combine with `CardUpdated(Hand)`

### DB Location Tracking
- `game_cards.location` now used instead of position-based guessing
- Values: 'hand', 'deck', 'PlayedResources', 'void', 'discard', 'warzone'
- Updated on initial deal, redraw, card play, and `!move`

### UID / DB conventions
- SQLite INTEGER is signed 64-bit; unsigned UIDs stored as TEXT (session_id, server_id, owner_uid)
- `meta` table holds counters (e.g. `next_session_inst`); `make_uid(246, inst)` for session UIDs
- `_find_campaign_for_champion` returns NEW `(cid, inst_id, 0, state)` or EXISTING `(row[0], row[1], row[2], loaded_state)`
- Champion `LastDeckID` sent raw (client wraps in `new UID(UID.Type.Deck, id)`), never pre-encoded
- Campaign `champion_name` column backfilled so 22031 resolves the player name without user_id-scoped queries

## Server Architecture
- `hconnect_server.py`: HConnect protocol, all handlers (6699 lines), AI turn engine, playability
- `db.py`: Shared SQLite connection (`db._db`) + all reusable `db_*` DML helpers
- `encoder.py`: All 19 ObjFmt encode functions + compress/decompress
- `commands.py`: Chat/debug command handler
- `game_engine.py`: SessionEventArgs classes (60+ types), Game event builder,
  Serializer, `event_logger` replay hook
- `ability.py`: champion ability effects (Replenish +3-5 SP, Shard Attuned PreGame
  health), BOM leaf executors (`_LEAFS` keyed by effect_type), PreGame dispatch
- `battle_engine.py`: DB-backed turn/priority engine (battle state in `turn_order_json`)
- `campaign.py`: Campaign/adventure service (dt=110000), AZ0 race configs, conversation chain
- `game_session.py`: DB-backed session state (game_sessions + meta), set_state persistence
- `static.py`: ALL DDL + server-owned seed data and configuration
- `AssetExtraction/gamedata_seed.py`: reads the client gamedata or `Records/`
  snapshot and seeds all client-derived tables directly into a fresh SQLite
  database; the generated data is not copied into `static.py`
- `generate_goldens.py` / `tests/verify_goldens.py` / `tests/goldens/`: byte-for-byte protocol golden regression tests
- `restart.sh`: Clean start (kills old, truncates logs to 1000 lines, compiles, starts server+proxy)
- `hconnect.db`: SQLite with 7214 card templates, 28 starter decks, 6762 card abilities,
  session_events replay log
- `proxy.py`: HTTP proxy on port 8081

Removed 2026-08-01 (dead code): `server.py`, `tcp_listener.py`, `bridge.py` —
obsolete pre-hconnect_server monolith and TCP bridge superseded by
`hconnect_server.py`/`proxy.py`. All DB helpers consolidated into `db.py`;
all schema DDL consolidated into `static.py` (no other module creates tables).
