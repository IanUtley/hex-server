# Hex TCG Private Server — Operations and Development Guide

This is the working guide for running, debugging, and extending the
authoritative private server. It documents current behavior and stable
integration rules; it is not a chronological development log. For a concise
status summary see [docs/PRIVATE_SERVER_FEATURES.md](docs/PRIVATE_SERVER_FEATURES.md),
for the wire protocol see [docs/CLIENT_SERVER_PROTOCOL.md](docs/CLIENT_SERVER_PROTOCOL.md),
and for the project's purpose and client configuration see [README.md](README.md).

## Quick start
```bash
cd /home/ianutley/Hex
bash restart.sh    # starts the HConnect server (9933) and HTTP proxy (8081)
```

After code-only changes to reloadable modules, HConnect can reload the runtime
modules without dropping connected clients by sending it `SIGUSR1`:

```bash
kill -USR1 "$(pgrep -f '[h]connect_server.py' | head -n1)"
```

The server logs `Signal reload complete` when the reload finishes. A full
`bash restart.sh` is still required for changes to HConnect's own startup
code or other non-reloadable process state.

For a fresh database populated from the original client data, either set
`HEX_GAMEDATA` or use the checked-in `Records/` snapshot. Existing databases
are not replaced automatically.

```bash
export HEX_GAMEDATA='/path/to/HEX SHARDS OF FATE/Data/gamedata'
bash restart.sh
```

## Server Architecture

| File | Purpose |
|------|---------|
| `hconnect_server.py` | HConnect protocol, service handlers, transactions, and session wiring |
| `db.py` | Shared SQLite connection (`db._db`) + all reusable `db_*` DML helpers |
| `static.py` | ALL schema DDL + static reference data (single source of truth) |
| `encoder.py` | ObjFmt and service-response encoders |
| `proxy.py` | HTTP proxy for Steam-compatible auth and news endpoints |
| `game_engine.py` | Session-event model and binary event serialization |
| `battle_engine.py` | DB-backed turn, phase, priority, and stack state |
| `abilities/` | Gamedata-driven ability resolution and effect executors |
| `restart.sh` | Kill port 9933 + 8081, restart both services |
| `hconnect.db` | SQLite database for profiles, collections, sessions, cards, and static data |

### Database code layout
- **All DDL lives in `static.py`** (`static.ensure_schema(db)`), including
  `session_events`. No other module creates tables.
- **`db.py` owns the connection** (`_db`, WAL, `check_same_thread=False`) and
  every reusable `db_*` helper (`db_get_or_create_user`, `db_get_inventory`,
  `db_get_arena_state`, `db_save_deck`, `db_store_chat`, `db_redeem_code`,
  `_record_session_events`, ...). `hconnect_server.py` does
  `from db import _db, db_get_inventory, ...`.
- `HEX_DB_PATH` overrides the default `hconnect.db` location for Docker or
  another deployment. Point it inside the mounted state directory so SQLite's
  database, `-wal`, and `-shm` files persist together.
- `static.py` also applies server-owned seeds and the stardust backfill.
  `AssetExtraction/gamedata_seed.py` supplies client-derived rows from
  `HEX_GAMEDATA` when configured, otherwise from `Records/`.
- `import_cards.py` is a standalone one-off importer and is the only exception
  to the normal “all schema DDL lives in `static.py`” convention.

### Tournament database capacity

SQLite is suitable for development and small-scale testing. Before attempting
32-player tournaments (up to 16 simultaneous games), plan to evaluate an
upgrade to PostgreSQL rather than assuming SQLite will provide sufficient
write concurrency. WAL mode allows concurrent readers, but SQLite still has a
single database-wide writer, so concurrent `game_cards` updates from multiple
games can queue or hit lock retries. A PostgreSQL deployment will require a
connection pool and database-adapter work; `HEX_DB_PATH` is not itself a
drop-in PostgreSQL migration.

## Profile and inventory synchronization

### Inventory item fields
The client hides inventory entries whose `ClaimDate` is in the past. For
permanent server-owned items, use `DateTime.MinValue` rather than the current
time so the stash does not treat them as expired. A consumed item sent through
`InventoryUpdated` is a special case: the fixed client removes the cached item
when the update has a non-minimum `ClaimDate`, so quantity-zero removal events
must use the current timestamp rather than `DateTime.MinValue`.

### Push Mechanism
The stash/Open Packs screen reads `PlayerProfile.InventoryItems`
(`m_InventoryItems`). The reliable update path for normal inventory changes is
`ProfileGenericUpdate` (DataType 2211), not the initial `reckoning_bits` profile
push. A consumed chest is removed with the concrete `InventoryUpdated` profile
event (DataType 2207, quantity zero, non-minimum `ClaimDate`); newly granted
cards use `CardsAdded` (DataType 2205). `OpenChestResponse` contains reward
template IDs for display but does not by itself update the collection or remove
the chest item.

HexClient/Assembly-CSharp-firstpass/Game/Client/PlayerProfile.cs
contains all the handlers for Request/Response messages. In addition it 
has onEvent handlers. This type of message can be sent any time.
e.g. After a currency update we can push a BalanceUpdate event, see
HandleBalanceUpdate method in that class.
There is a more generic event ProfileGenericUpdate that handles a few
different types. e.g. it can also do card updates and currency updates.
Exploring this method in detail would be valuable.

Flow:
1. Login → `push_profile_stream()` sends reckoning_bits (InventoryIds
   goes to a SEPARATE collection, not used by stash).
2. `LoginStreamDone` → sets `self._inventory_pending = True`.
3. On the first routed request, push the DB inventory via
   `push_inventory_to_client()` using DataType 2211.
4. Purchases and other changes push the affected item or a full refresh as
   appropriate.

### Inventory persistence
The `player_inventory` table stores all purchased items.  On login,
`db_get_inventory()` returns the list and everything is re-pushed.

## ObjFmt Encoding Rules

### Numbers
- `int`: `<hex_little_endian>;` (e.g. `00000000;` for 0)
- `ulong`: `<hex_little_endian>;` (e.g. `0100000000000000;` for 1)
- `bool`: raw `'1'` or `'0'` — NO hex, NO separator
- `uint`: `<hex_little_endian>;`

### Strings & DateTime
- `<length>;<bytes>` — raw bytes, no `;` after the value
- DateTime format: `MM/dd/yyyy HH:mm:ss` (19 chars)

### Collections
- `List<T>` → `ecount;` then elements
- Element header: `<index>;<size_idx>;<type_idx>;<numProps>;`
- Pre-encoded elements use `encode_inventory_item()`

### Dictionary types and the 32-bit client
The game is 32-bit Mono.  Some generic type strings fail to resolve:
- `Dictionary`2#System.UInt64#System.UInt64` — FAILS type resolution
- `Dictionary`2#System.Int32#System.Int32` — WORKS
- `List`1#System.UInt64` — WORKS (lists are fine)

Avoid introducing a new `Dictionary<ulong, T>` field without checking the
client's loaded type table. In the few existing responses where the Mono
type resolver cannot load a UInt64 dictionary, use the already validated
32-bit-compatible representation in the corresponding encoder.

### Size Table
- Format: `type_names\nsize0;size1;...;sizeN`
- NO trailing `\n` after size table (C# encoder doesn't write one)
- Parse C#: reads backwards from end for `\n`, then `ReadToEnd()`

### Enum handling
- Follow the concrete client type. Hex enums are generally encoded as a
  struct containing the `value__` integer field, not as a bare name string.
- Use the namespace loaded by the client, normally `Game.Shared.*` rather than
  a similarly named `Game.Client.*` type.
- Do not add optional error fields to a response until the client handler has
  been checked; an unknown enum type can make the whole response fail to
  deserialize.

### Field Order
- `[DataMember(Order = N)]` fields serialize LAST (higher Order = later)
- Error/ErrorMessage type fields use `Order=100`/`Order=101`

## Infrastructure

### Database Schema & Migrations
- **Fresh databases** are created by `restart.sh` from `static.py`
  (`static.ensure_schema`) when `hconnect.db` does not exist. Docker runs the
  same initialization in `docker/docker_bootstrap.py`.
- Docker also runs `static.ensure_schema` against an existing mounted database
  before starting services. This applies new columns, indexes, and idempotent
  server-owned seeds in place while preserving users, cards, decks, and
  campaign state; it never copies the repository's local `hconnect.db` into
  the image.
- **Migrations** are one-off scripts in `migration.py`: `restart.sh` runs it if
  present, then deletes it. Each migration must be idempotent (check before
  altering). Add new column/schema changes to BOTH `static.py`'s DDL (for fresh
  DBs) and `migration.py` (for existing DBs), and commit them together.
- `game_cards` denormalises card data for a single code path across AI/player
  cards:
  - `card_type` — `Resource`, `Troop`, etc.
  - `template_guid` — the resolved card template GUID (player cards may store an
    instance id in `card_template_id`; AI cards store a template GUID there).
    CardUpdated/type/cost lookups join `game_cards.template_guid =
    card_templates.guid` regardless of player or AI.

### Client configuration
The server does not read the client's configuration. Edit the client's
`config.ini` (or `UnityConfig.json` in custom builds) and use an address
reachable from that client:

```ini
[SystemSettings]
GameServerIP = 192.168.1.50:9933
CZEAuthUrl = http://192.168.1.50:8081/auth/hexlogin
NewsEventsURL = http://192.168.1.50:8081/NewsEvents/NewsEvents-{0}.txt
```

In WSL, use the address from `hostname -I`, not `127.0.0.1`, unless the game
client is running in the same network namespace. See [README.md](README.md)
for the Windows and Docker examples.

For a local Docker Desktop server on the same Windows PC, the default client
installation can be updated from Command Prompt:

```cmd
set "HEX_GAME=C:\Program Files (x86)\Steam\steamapps\common\HEX SHARDS OF FATE"
copy "%HEX_GAME%\config.ini" "%HEX_GAME%\config.ini.backup"
notepad "%HEX_GAME%\config.ini"
```

Set the existing `[SystemSettings]` values to `127.0.0.1:9933` for
`GameServerIP` and `http://127.0.0.1:8081/...` for `CZEAuthUrl`, `CZEPayUrl`,
and `NewsEventsURL`. Run Command Prompt as Administrator if the installation
under `Program Files (x86)` is not writable, then fully restart the client.

### Ports
- 9933 — HConnect game server
- 8081 — HTTP proxy (Steam auth + news)

### Proxy Endpoints
The game UI does not use HTTP for collection synchronization. It updates its
collection from server-pushed DataWrapper messages (DataType 2211 for
inventory and `OpenCardPackResponse.NewCardInstances` for cards). The former
third-party collection-sync routes (`/collection`, `/inventory`, `/card`,
`/deck`, and `/accepts.txt`) now return 404.
- `/steam/login` — Steam auth → `{"result":"success","token":"test_token_abc123","username":"TestPlayer"}`
- `/steam/dlccheck` — DLC check → empty result
- `/NewsEvents...` and `/news/...` — local news feed and image assets when
  present; the client can point `NewsEventsURL` at the proxy.

## Collection and pack generation

### Card data
The current client extraction contains 7,214 card templates. Set 1 remains
`0382f729-7710-432b-b761-13677982dcd2` with 435 cards. The server's canonical
static tables come from `Records/` or, for a fresh deployment, directly from
the mounted `Data/gamedata` blob via `HEX_GAMEDATA`.

### PVE vs PVP Card Filtering
PVE cards are excluded from booster packs using two filters (see above):
- **Set-level**: only sets containing Common/Uncommon/Rare/Legendary rarities
- **Card-level**: `m_IsPvE` and `m_IneligibleForPvPRandomTemplates` flags from JSON

### Rarity Distribution (per 17-card pack)
- 12 Common/Land
- 4 Uncommon
- 1 Rare (11% chance Legendary)

### Card Fields
- `m_CardRarity`: Common, Uncommon, Rare, Legendary, Epic, Land, Promo
- `m_SetId.m_Guid`: identifies which set

### OpenCardPack Response (DataType 2127)
- `NewCardInstances` — `List<card_instance_bits>` (8 fields each)
- `NewGemInstances` — empty list
- `NewChestInstances` — empty list
- `Error` — `"Ok"` (EOpenCardPackError enum)
- `SocketedGems` — MUST use valid EGemTypesNew value (e.g. `"Unknown"`)
- `CardStats` — empty Dictionary (numProps=0) is fine

## Common Gotchas

1. **GUIDs are lowercase** — both in DB and encoding, matching template JSON.
2. **DateTime values use `CultureInfo.InvariantCulture`** — `MM/dd/yyyy HH:mm:ss`.
3. **`HashSet` vs `List`** — client checks `obj2 is List<inventory_bits>`,
   so we use `List` type names, not `HashSet`.
4. **Profile push timing** — send inventory after login, not during the loading
   screen, and use DataType 2211 for the reliable stash update.
5. **Connector port** — hardcoded to 9933 in `Game.Shared.Network.HConnect.Connector.cs:491`.
6. **Enum type namespaces** — use `Game.Shared.Network.Escrow.*`, not `Game.Client.*`.
7. **Enum encoding in ObjFmt** — enums are encoded as structs with `value__` int sub-field,
   NOT as string values. String enum encoding causes `EnumType X decode expect 1 property had 0`.
8. **Size table format** — `\ntype_names\nsize0;size1;...;sizeN` with no trailing `\n`.
   First size (size0) must be included. Use `if i>0: w(";"); w(str(s))`, NOT `w(";"+str(s) if i>0 else "")`.
9. **ResourceId in requests** — client's `ResourceId` has `[DataMember]` on field `m_Guid`
    (not `guid`). When ObjFmt parsing fails (`__skipped__`), extract from raw `inner_bytes`.

## Log Files

| Source | Path | Notes |
|--------|------|-------|
| Server | `/tmp/hconnect_log.txt` | `hconnect_server.py` stdout/stderr |
| Server requests | `/tmp/hconnect_requests.log` | Per-request details written by `log_req()` |
| Proxy | `/tmp/proxy_log.txt` | `proxy.py` stdout/stderr |
| Client | `/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Hex_Data/output_log.txt` | Unity output log — check here when client hangs, crashes, or progress bar is stuck |

### Debugging "progress bar stuck" / client hang
When the client appears frozen (progress indicator never completes):
1. Check the **client log** (`output_log.txt`) for `Exception`, `ERROR`, `Failed` lines
2. Common causes:
   - **EnumType decode expect 1 property had 0** — response included an enum type the client cannot decode. Check the concrete enum namespace and `value__` struct shape (see Enum handling above).
   - **Failed to find .NET type for name** — type name in ObjFmt size table doesn't match any loaded assembly.  Usually a namespace mismatch (`Game.Client.*` vs `Game.Shared.*`).
   - **HEARTBEATEXPIRE / Write failure** — server restarted while client was connected; client needs to reconnect.
3. Check **server log** (`/tmp/hconnect_log.txt`) for the matching `>>> Routed msg` entry — if the server returned Unhandled for the DataType, implement the handler.

## DB Schema

### Important table groups
- **Account and collection:** `users`, `collections`, `card_instances`,
  `player_inventory`, `stardust`, `store_items`, `store_purchases`, `emails`.
- **Card and ability metadata:** `card_templates`, `card_abilities_meta`,
  `ability_effects`, `target_templates`, `ability_effect_conditions`,
  `card_counter_templates`, `gem_templates`.
- **Decks and champions:** `decks`, `deck_template_cards`, `starter_decks`,
  `champions`, `champion_templates`, `champion_templates_extended`,
  `champion_abilities`, `talent_data`, `talent_abilities`.
- **Sessions and gameplay:** `game_sessions`, `game_cards`, `session_events`,
  `user_prefs`, `arena_state`, `campaigns`.
- **Tournaments and communication:** `tournaments`, `tournament_matches`,
  `tournament_signups`, `tournament_decks`, `chat_messages`, `friends`,
  `friend_requests`, `ignored_players`.

All DDL is defined in `static.py`. A schema change must be added there and in
an idempotent `migration.py` for existing databases; `restart.sh` runs and
removes the migration after success.

## Looking Up GUIDs from Game Data

When you see an unknown GUID in a client request (card, champion, equipment, etc.),
you can identify it from the game's data files:

### Method 1: `localization.db` (quickest)
```
grep "<guid>" "/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Data/localization.db"
```
Contains human-readable records like:
```
Bunoshi the Ruthless ^NAME^Bunoshi the Ruthless ^5c0a66c0-103b-4e1c-b150-b27f5c23f5e1
```

### Method 2: `Data/gamedata` (gzip-compressed, contains all templates)
```
python3 -c "
import gzip
with gzip.open('/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Data/gamedata', 'rb') as f:
    data = f.read().decode('utf-8')
idx = data.find('<guid>')
if idx >= 0:
    # Find template type
    t = data.rfind('\"_t\"', 0, idx)
    print(data[t:data.find(chr(10), t)].strip())
    # Find name
    n = data.find('m_Name', idx)
    print(data[n:n+100])
"
```
Each entry in gamedata has `"_t"` (template type, e.g. `ChampionTemplate`, `CardTemplate`)
and `"m_Name"` (display name).

## Decks and response encoders

Deck requests are handled by the profile service and persisted in the `decks`
table. The client expects complete `deck_bits`/`GetDeckInfo` structures, so
use the shared UID and ResourceId helpers in `encoder.py` rather than building
partial ad-hoc responses. In particular:

- UID values contain the `m_UID64` sub-field.
- ResourceId values use the field shape expected by the concrete client type.
- Lists include their element type and size-table entries.
- Responses are encoded from current state; do not cache a response across
  server restarts.

For a new response, first inspect the corresponding client handler and then
add an encoding test. The protocol document has the general ObjFmt rules.

## PlayerProfile — Client-Side Event Handlers

The client binary (`Assembly-CSharp-firstpass.dll`) contains `PlayerProfile`
which subscribes to server-pushed events. We CANNOT modify this code — all
fixes must be server-side.

### PlayerProfile Event Subscriptions (constructor)
| DataType | Server Push | Client Handler |
|----------|-------------|----------------|
| 2205 | `CardsAdded` | `HandleCardsAdded` — iterates CardBits, adds to `m_CardList` (CardCache) via `AddNewCardToCollection` |
| 2211 | `ProfileGenericUpdate` | `HandleProfileGenericUpdate` — dispatches to type-specific handlers |
| 2212 | `BalanceUpdate` | `HandleBalanceUpdate` — updates currency displays |
| 2127 response | `OpenCardPack.OnResponse` | `HandleOpenCardPack` — calls `LoadCardsFromBits` → `AddCardToCollection` |

### ProfileGenericUpdate (2211) Sub-handlers
The `HandleProfileGenericUpdate` method decodes the inner payload and dispatches
by C# type:

| Type | Handler |
|------|---------|
| `ProfileGenericBatchUpdate` | Adds Cards via `AddNewCardToCollection`, Items via `AddInventoryItem` |
| `ProfileGenericCardColUpdate` | Adds cards via binary `CardGroupId.DecodeGroup` format |
| `ProfileGenericInvenColUpdate` | Adds inventory via binary `InvenGroupId.DecodeGroup` format |
| `ProfileGenericCurrencyUpdate` | Updates gold/platinum balances |
| `ProfileGenericDisplayRewards` | Displays reward notifications |
| `ProfileGenericAccountUpdate` | Updates XP/level |
| `ProfileGenericLoginPoolUpdate` | Login queue position |

### Card Pack Opening Flow
1. Client sends `OpenCardPack` (2127) with ItemId (ResourceId→pack GUID) + OpenAmount
2. Server generates cards, persists to `card_instances` table with sequential IDs
3. Server pushes cards via `ProfileGenericUpdate` (2211) with `ProfileGenericBatchUpdate.Cards`
   → client calls `AddNewCardToCollection` → adds to `m_CardList` (CardCache)
4. Server sends `OpenCardPackResponse` (2127) with same card instance IDs
5. Client's `OpenCardPack.Dispatch`: OnResponse→HandleOpenCardPack adds cards, then handler→openPackResponseHandler looks them up in CardList by `TryGetCard`
6. Cards appear with flip animations

### Key Server→Client DataTypes
| DataType | Name | Used For |
|----------|------|----------|
| 2205 | CardsAddedEventArgs | Pushing cards into `m_CardList` (deprecated — use 2211 instead) |
| 2211 | ProfileGenericUpdate | Cards (ProfileGenericBatchUpdate), inventory, currency |
| 2212 | BalanceUpdate | Currency-only push |
| 2200 | DeckRemovedEventArgs | Deck deletion notification |

### CardsAddedEventArgs Format (DataType 2205)
```
CardBits: List<card_instance_bits>
```
Each card_instance_bits has: Id (ulong), TemplateID (ResourceId→guid),
IsFoil (bool), IsExtended (bool), IsNotTradeable (bool), EscrowStatus (string).

`HandleCardsAdded` processes these by:
1. If card already in CardList → removes old copy, updates any decks referencing it
2. Calls `AddNewCardToCollection(cardBits)` which:
   a. `AddCardToCollection` → `m_CardList.AddCard(card)` (CardCache dictionary)
   b. Marks as new in `m_NewCards` dict
3. Fires `OnCardAdded` event → UI updates collection count
4. Calls `SendCollection()` → notifies all listeners

### CardCache (m_CardList)
Dictionary keyed by `CardId` (constructed from UID.Type.Card + instance_id ulong).
`TryGetCard(CardId, out ICard)` is the lookup used by pack opening UI.

### Inventory Update Mechanism
Inventory items on the stash/pack-bag screen are populated by TWO mechanisms:
- `ProfileGenericBatchUpdate.Items` (ObjFmt format) — adds NEW items via `AddInventoryItem(inventory_bits)`. 
  Does NOT update quantity of existing items — each push creates a new entry if UID differs.
- `ProfileGenericInvenColUpdate` (binary `InvenGroupId.DecodeGroup` format) — the canonical DB sync path.

### AddInventoryItem — How Client Inventory Works
`PlayerProfile.AddInventoryItem(UID, ResourceId, quantity, escrow, bound, claimDate)`:
1. **Deduplication**: scans `m_InventoryItems` for an item with matching `ItemUid` AND `TemplateId`.
   If found, removes the old entry, then adds a fresh one with new values.
2. Fires `OnInventoryUpdated` → UI stash/pack-bag refreshes.
3. `SendCollection()` → 30-second debounced Notifier sync.

**Key insight for updating pack counts**: Push with the SAME `ItemUid` (UID) and `TemplateId` (pack GUID)
but a new `Quantity`. The client will replace the existing entry rather than creating a duplicate.

### InvenGroupId Binary Format (for ProfileGenericInvenColUpdate)
`InvenGroupId.DecodeGroup(BinaryReader)` parses a compact binary format into groups of:
```
InvenGroupId { TemplateId, Quantity, Escrow, NoTrade, ClaimDate }
  + List<ulong> instance UIDs
```
Each group represents identical items grouped by shared attributes. The client calls
`AddInventoryItem` for each instance UID with the group's quantity/template/escrow/notrade/claimdate.

### ProfileGenericBatchUpdate — Bulk Update Hub
Fields the client processes from a `ProfileGenericBatchUpdate` (ObjFmt):
| Field | Client Action |
|-------|---------------|
| `Cards` (List<card_instance_bits>) | `AddNewCardToCollection` per card |
| `Items` (List<inventory_bits>) | `AddInventoryItem(inventory_bits)` per item |
| `GoldDelta` (int) | `UpdateBalance(gold+delta, plat)` |
| `PlatDelta` (int) | `UpdateBalance(gold, plat+delta)` |

### ProfileGenericCardColUpdate — DB Card Sync
`CardGroupId.DecodeGroup(BinaryReader)` parses into:
```
CardGroupId { Escrow, Extended, NoTrade, TemplateId } + List<ulong> instance UIDs
```
For each instance, creates a `card_instance_bits` and calls `AddCardToCollection`.

### Full Inventory Refresh
`ProfileService.FullInventoryRefresh.OnEvent` (via `FullInventoryRefreshEventArgs`):
Clears `m_InventoryItems` entirely, then repopulates from `ev.PlayerItems` list.
This is the "nuclear option" for inventory sync — guarantees client mirrors DB exactly.

### InventoryUpdated — Single-Item Change
`ProfileService.InventoryUpdated.OnEvent` (via `InventoryUpdatedEventArgs`):
- If item found with Quantity=0 or expired claim date → removes item
- If not found → adds new item
- Fires `OnInventoryUpdated` + `SendCollection()`

For inventory COUNT updates (e.g., consuming a booster pack), the client requires
`ProfileGenericInvenColUpdate` which uses a complex binary format. We currently
only remove packs from the DB; the count syncs on next login when full inventory
is re-pushed.

### Pack-specific client behavior

Card pools are filtered at two levels:
1. **Set-level** — only PVP sets (sets containing any card with rarity Common/Uncommon/Rare/Legendary).
   PVE-only sets (all Land/Epic/Promo) are excluded.
2. **Card-level** — individual cards with `m_IsPvE=1` or `m_IneligibleForPvPRandomTemplates=1`
   are excluded from the pool.

#### Card DB fields (from `CardTemplate` gamedata)
| Field | DB Column | Purpose |
|-------|-----------|---------|
| `m_IsPvE` | `is_pve` | 1 = PVE-only card |
| `m_IneligibleForPvPRandomTemplates` | `no_pvp` | 1 = should not appear in PVP packs |
| `m_CardRarity` | `rarity` | Common, Uncommon, Rare, Legendary, Epic, Land, Promo |
| `m_ResourceCost` | `cost` | Resource cost |
| `m_BaseAttackValue` | `attack` | Attack value |
| `m_BaseDefenseValue` | `defense` | Defense value |
| `m_CardType` | `card_type` | Troop, QuickAction, BasicAction, etc. |

Rarity distribution (per 17-card pack): 12 Common/Land, 4 Uncommon, 1 Rare (11% Legendary).
Epic and Promo rarities are excluded (alternate art / promo cards).

Full-set grants use the same standard PvP rarity filter and therefore grant
four copies of each Common, Uncommon, Rare, and Legendary card only; Epic and
Promo alternate-art/promotional templates are excluded.

### Pack response encoding
Enums are encoded as structs with `value__` sub-field containing the integer value.
String-based enum encoding (`"Ok"`) causes `EnumType X decode expect 1 property had 0`.

Correct format:
```
Error;idx;EOpenCardPackError_type;1;
  value__;idx;System.Int32;0;00000000;
```

### Size Table Format
```
type_names\nsize0;size1;size2;...sizeN
```
NO trailing `\n` after size table. The first size (size0) must NOT be omitted.
Using `w(";" + str(s) if i > 0 else "")` drops size0 — use `if i > 0: w(";"); w(str(s))` instead.

### Pack GUID Extraction from Raw Bytes
The client sends `OpenCardPackRequestArgs` with `ItemId` as a `ResourceId`.
The ObjFmt parser fails on ResourceId (returns `__skipped__` in dict).
Extract from raw `inner_bytes` instead — the ResourceId's `[DataMember]` field
is named `m_Guid` (not `guid`), encoded as:
```
ItemId;...;ResourceId_type;1;
  m_Guid;...;System.Guid;0;36;<36-char-guid>
```

## Current limitations

The implementation checklist is the broader status record. These are the
limitations most likely to affect a live test:

- **Tutorial battle rendering** is still partial. Session setup works, but the
  tutorial's complete event routing and client battle presentation need more
  validation.
- **Frost Ring Arena** returns the challenger list, but the complete
  challenger/deck/game lifecycle is not finished.
- **Inventory quantity refresh** is not uniform in every client path. Pack
  consumption is authoritative in SQLite; a full inventory push on the next
  login is the reliable fallback.
- **Auction House** remains unhandled.
- **A server restart ends live sessions.** Reconnect the client after a
  restart; persistent profile and card data remain in SQLite.
- **Two local Windows clients:** prefer two distinct non-Steam accounts through
  the proxy's `/auth/hexlogin` path. This avoids Steam's single-instance and
  ticket-authentication behavior. Configure both clients with the same server
  address but use different account credentials; Steam identities are only
  needed when deliberately testing Steam authentication.
- **Replay listing/download endpoints** are not implemented, although session
  event capture exists, but the old chat replay command is no longer exposed.

### Verified client-sensitive flow: opening a pack

For the OpenCardPack UI to animate correctly, push the newly created card
instances through ProfileGenericUpdate (2211) before returning the
OpenCardPack response (2127). Use the same instance IDs in both messages and
encode enum fields with the client's `value__` representation. This ordering
keeps the client's CardCache populated before the pack view looks up its cards.

## Game session management

### LoadBalancer DataTypes Implemented

| DataType | Name | Handler Status |
|----------|------|----------------|
| 22013 | TryReconnectionToDisconnectedGame | Returns `HasDisconnectedGame=false` |
| 22015 | StartSession | Creates session, returns Success + SessionState |
| 22017 | StartEncounter | Creates session, returns Success + SessionState/SessionID/ServerID |
| 22019 | FindSession | Returns `Success=false` (no session found) |
| 22021 | JoinSession | Adds player, returns Success + player list |
| 22031 | ReadyToStartGame | Fire-and-forget (client sends with null callbacks) |

### Session Lifecycle Flow
1. Client sends FindSession (22019) → server returns "no session"
2. Client sends StartEncounter (22017) → server creates session, returns SessionID/ServerID
3. Client sends JoinSession (22021) → server adds player
4. Client sends ReadyToStartGame (22031) → client expects GameStarted event

### ServiceGameSession routing
GameSession events (DataType 3050-3056: PlayerAdded, GameStarted, SessionSync,
etc.) pushed with `target: ServiceGameSession` ARE processed by the client
when `instance` matches the session and `reqid=0` (event, not response).
The most common failure is an opponent not being registered in `State.Players`:
when a `CardDrawn` event references an unknown player UID, the client crashes with
`KeyNotFoundException` in `Dictionary<UID,int>.get_Item` inside
`UIBattle.OnCardDrawn`.  Fix: push `PlayerAdded` (3050) before any card
events so the opponent is in `State.Players`.

PvP tournament sessions use `instance=str(session.server_id)` and the
standard issuer format below; the same routing works for both 3050 and
3055 events:
```
issuer: 0.0.0.0.ServiceGameSession.246.{session_id}.{scnt}
target: ServiceGameSession
instance: {server_id}
```

### ReadyToStartGame (22031)
This is a fire-and-forget request — the client sends it with `null` callbacks
and expects GameStarted events as a response.  Sending a reply causes
`Command handler not found for data wrapper type 22031`.

## PvP tournament architecture

### Tournament Lifecycle
1. Server starts with 6 waiting rooms (2 per type: 1v1 Constructed, Sealed, Draft)
2. Player joins via `EnterTournament` (25029) → pushed lobby rdata + `TournamentInfo` (25058)
3. Room fills (2 players for 1v1) → `start_waiting_room_game` fires
4. Server pushes `DeckConstructionStarted` (25072) → `TournamentSessionStart` (25060, SessionState + DeckId) → `TournamentInfo` (25058 LAST, triggers `GoToTargetState`)
5. Client transitions to Battle via `TournamentManager.TransitionToBattle` → `UIBattle.StartTwoPlayerGame` → sends `ReadyForGameSetup` (22027)
6. Server responds with `ReadyForGameSetupResponse` (SessionState, OpponentsInfo, TurnOrder)
7. Client sends `ReadyForGameEvents` (22029) — when both ready, server pushes game events

### Game Session Setup
Both players share ONE `game_session` row. Players are differentiated by `user_id` in `game_cards`. Session lookup uses `find_session_by_player` (scans `players_json`).

### Card UID Format
`game_cards.card_uid` MUST be a proper UID in `(instance << 8) | type` format (e.g. 257 = instance 1, type Card=1). Simple integers (1001, 1002, ...) cause `_card_full_data` lookups to fail because `scid.uid.uid64` doesn't match the DB. Use `UID(int(card_uid))` to wrap DB values, not `UID.make(1, card_uid)`.

### Champion Templates
PvP champion template GUIDs live in `champion_templates_extended` (NOT `champion_templates`). Their abilities are in `champion_abilities`. When `_template_by_guid` returns None (champion not in `card_templates`), fall back to `champion_templates_extended` for the template GUID, then load abilities from `champion_abilities`.

### OpponentsInfo Encoding (22027 Response)
The `ReadyForGameSetupResponse` OpponentsInfo uses `playerstate_coll` encoding.  **Critical:** `"Game.Shared.PlayerState"` MUST be listed in the `type_names` array passed to `encode_objfmt_response`, otherwise the client's ObjFmt size table has no entry for PlayerState and the entire response silently fails to deserialize — causing `OnReadyForGameSetup` to crash and the battle UI to never start.

### Startup event invariants
Session startup is sent in separate synchronization waves. Register both
players and valid champion/card definitions before publishing events that
reference them, send setup before the coin-toss priority window, and keep
private hand/deck representations nulling-aware. The exact event construction
is shared by the Practice and tournament paths; do not combine all setup and
phase events into one packet without checking the client log.

`ReadyToStartGame` (22031) and `ReadyForGameEvents` (22029) are fire-and-forget;
do not send ordinary responses. Send `ReadyForGameSetup` (22027) before the
3055 event waves, and set the player/opponent champion `SessionCardId`s before
publishing `PlayerUpdated`.

### PlayerAdded (3050) — MUST be before card events
The opponent MUST be in `State.Players` before any `CardDrawn` event fires.
If it isn't, `UIBattle.OnCardDrawn` crashes with `KeyNotFoundException` at
`Dictionary<UID,int>.get_Item`.  Push `PlayerAdded` (3050) BEFORE the first
3055 game packet.  The ObjFmt encoding uses `RoutingPlayerId` (UID) +
`PlayerState` (PlayerId UID + PlayerPosition int).

### PvP Pass Sync
Passes flow through `route_pvp_pass` in `services/tournament_game.py`:
- Phase state is persisted in `session.turn_order` (PvP state dict with `"pvp": True`)
- Mulligan/PickGoesFirst passes are ignored (phases 3-4)
- When both players pass: TurnPhaseUpdated pushed to BOTH first, then server-side phase logic runs, then GreenLight to turn player
- No intermediate green light passthrough between players
- Server-side phase stops and auto-pass are derived from the stored client stop
  preferences; `PickGoesFirst` and `Mulligan` are never auto-passed.

### PvP Transaction Router
Non-pass transactions (card plays, abilities, combat) are routed through
`pvp_handle_transaction` in `services/tournament_game.py`. Resource plays,
card plays, ability activation,
combat, and chain resolution now use the shared rules paths; coverage is still
less complete than the Practice path. Each action must push events to BOTH
players — `CardUpdated`/`CardMoved` with the correct controller UID and
`nulling=True` for hidden zones (Deck, Hand, Underground).

### PvP Quit / Withdraw
`QuitGameTransaction` (m_QuitEntireSeries or m_Surrendered) pushes `GameEnded`
to BOTH players: loss to the withdrawer, victory to the opponent.
Withdrawer always loses; opponent always wins.

### Nested fields in `encode_objfmt_response`
- `struct` recursively encodes nested objects through `encode_field`.
- `raw` embeds pre-encoded ObjFmt bytes when a client-specific binary shape is
  required.

## Game Engine (session events)

A full `game_engine.py` module exists with:
- .NET BinaryWriter-compatible binary serializer
- All 84 SessionEventArgs subclasses (IDs 1-83)
- Tutorial script parser for `tutorial.txt`
- Game state engine with cards, decks, zones, timers

### SessionEventArgs Binary Format
NOT ObjFmt — uses C# BinaryWriter format:
- int32 (4 bytes LE), int64 (8 bytes LE), uint64 (8 bytes LE)
- bool (1 byte), string (7-bit-encoded length + UTF-8)
- UID (8 bytes uint64 LE), ResourceId (int32(16) + 16 Guid bytes)
- List<T> (int32 count + items), Dictionary<K,V> (int32 count + pairs)
- Nested SessionEventArgs: [ClassId:int32][byteLen:int32][ToByteArray bytes]

### NetworkPacketSessionEventArgs (Class 255)
Top-level event container.  Has DataMember fields: PlayerId (UID),
EventIds (List<int>), EventData (List<byte[]>).  This is the payload
inside SessionSyncEventEventArgs.SessionArgs and goes through the
custom binary serializer.

### Key session events
1. GameStartedSessionEventArgs (ID 1) — turn order, champions, seeds
3. TurnPhaseUpdatedSessionEventArgs (ID 3) — phase changes
47. DeckCreatedSessionEventArgs (ID 47) — deck card IDs
48. GreenLightSessionEventArgs (ID 48) — priority windows
50. CardMovedSessionEventArgs (ID 50) — cards moving between zones
64. CardUpdatedSessionEventArgs (ID 64) — full card state (45 fields)
65. PlayerUpdatedSessionEventArgs (ID 65) — health, resources, threshold
70. PlayerOptionListSessionEventArgs (ID 70) — available actions
71. BulkSessionEventSessionEventArgs (ID 71) — batched events
80. ShowTipSessionEventArgs (ID 80) — tutorial tooltips
82. SkipSetupSessionEventArgs (ID 82) — skip mulligan/setup
83. DisableInterfaceSessionEventArgs (ID 83) — lock/unlock UI

### Practice and FRA validation notes

The Practice and tournament routes share the same session-event and rules
helpers, but their client startup paths are not identical. When validating a
new route, confirm that player/champion IDs, card definitions, deck ownership,
and the two-wave startup event order are correct before investigating card
rules.

**FRA (inline 22031 handler):**
- Verify that the arena/session state selects the intended user's deck and
  champion before comparing the opening hand.
- Validate both players' `game_cards` rows and template GUIDs when a card is
  missing or has the wrong type in the battle UI.

### Event pitfalls

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| `KeyNotFoundException` in a card/champion handler | A player or champion UID was not registered before a referencing event | Send valid `PlayerAdded`/`GameStarted` state and champion IDs first |
| Cards render with the wrong type or art | CardUpdated used a guessed type or stale template | Resolve the current `game_cards.template_guid` through `card_templates` |
| Hand does not match the selected deck | Session/deck lookup used the wrong owner or stale deck ID | Verify the session's player, deck, and `game_cards` rows together |
| Client drops the next transaction | A handled request produced no 3055 synchronization packet | Send `_push_transaction_ack(session)` |
| A card is missing after reconnect | CardUpdated/CardMoved order or hidden-zone filtering is wrong | Republish valid card definitions, location, position, and visibility |

## Battle engine and AI turns

### `battle_engine.py`
DB-backed turn/priority engine. Battle state (whose turn, phase index, pass
flags, resource counts) is stored in the `game_sessions.turn_order_json` column
so concurrent players never interfere and a reconnect can resume.

Per-turn phase cycle (`TURN_PHASES` is **dynamic**, stored in
`battle_state['turn_phases']`):
```
StartTurn(6) → Ready(7) → Prep(8) → Draw(9) → FirstMainPhase(10) →
SecondMainPhase(19) → EndPhase(20) → Discard(21) → EndTurn(22)
```
Combat phases (DeclareCombatPriorityWindow 11 → AssignDamage 18) are inserted
between the two main phases **only when the player controls a ready warzone
troop** (`battle_state['player_has_ready_troop']`, set at StartTurn from
`game_cards.card_state & StartedATurnOnYourSide`). The AI never attacks, so its
turn always uses the no-combat list.
`advance_phase()` wraps back to StartTurn after EndTurn and switches the turn
player (player ↔ AI), incrementing the turn number.

### Combat
- **Summoning sickness** is persisted per-card in `game_cards.card_state`
  (DB): Prep sets `StartedATurnOnYourSide` (16K) and clears `CameOutThisTurn`
  (8K) for every warzone troop (player + AI); playing a troop sets
  `CameOutThisTurn`. A troop can attack iff it has `StartedATurnOnYourSide`
  and is not tapped (mirrors client `Card.HasSummoningSickness` /
  `CanAttack`).
- **DeclareAttack** (`12`): the server pushes `PlayerOptionList` with
  `ECardUsage.Attack` for ready troops; the player's `CommitTroopsToAttackTransaction`
  carries the attacker `SessionCardId`s. Server persists them in
  `battle_state['player_attackers']` and pushes `AttackDeclared` (27) +
  `CombatListing` (62).
- **DeclareDefense** (`14`): the AI (defender) never blocks yet — the server
  pushes `BlockersAssigned` (28) with empty blocker lists and advances.
- **AssignDamage** (`18`): the client auto-sends `AssignDamageOrderTransaction`
  (`BattleStateAssignDamage` auto-commits when no combat has blockers). The
  server subtracts each unblocked attacker's `attack` from
  `battle_state['ai_health']` (DB), pushes `ChampionHealthChanged` (38) +
  `PlayerUpdated` (65), and ends the game (player win) when the AI champion
  reaches 0 health.
- **Reconnect**: combat state (`player_attackers`, `ai_health`, per-card
  `card_state`) is all in the DB, so a reconnecting player resumes mid-combat.
- New session event classes: `AttackDeclaredSessionEventArgs` (27),
  `BlockersAssignedSessionEventArgs` (28), `CombatPhaseResolvedSessionEventArgs`
  (29), `BeginCombatResolutionSessionEventArgs` (30),
  `CombatRemovedSessionEventArgs` (32), `CombatsThatNeedDamageSessionEventArgs`
  (61), `CombatListingSessionEventArgs` (62), `CombatSessionEventArgs` (63),
  plus the `CombatId` (UID + int64) wire type and `ECardStates.CameOutThisTurn`.

### One phase per packet + server-driven auto-pass
- The server pushes **ONE phase per packet** — never a burst.
- **Stop positions are a user preference.** The client reports them via
  `SetTurnPhasesTransaction` (m_PlayerId, m_TransactionId, m_SelfTurnPhases,
  m_OpponentTurnPhases). The server captures them at battle start and stores
  them in `user_prefs`; when not configured it falls back to the client
  defaults.
- Phases that are **not** a stop for the turn player are auto-passed
  server-side (pushed in their own packet, then immediately advanced — no
  priority window granted). `PickGoesFirst` and `Mulligan` can never be
  auto-passed. `StartGame` has no client BattleState, so the server pushes it
  and advances to `StartTurn` itself.
- **Stale-pass guard:** `PassPriorityTransaction` carries `m_TurnPhase`; a pass
  whose phase doesn't match the server's current phase is ignored.
- Full rule set: see `RULES.md` (canonical gameplay reference).

### Client one-at-a-time transaction pipeline (critical!)
`SessionClient.SubmitTransaction` (Game/Client/SessionClient.cs:45) sends only
ONE transaction at a time and **silently drops** any further transaction while
`m_HasPreviousTransactionBeenRespondedByServer` is false. That flag is reset to
true ONLY when a `NetworkPacketSessionEventArgs` (class 255, the top level of
every 3055 packet) arrives (ClientSessionBase.cs:30).
- For transactions the server handles WITH 3055 events (passes, card plays,
  mulligan keep/redraw) the flag resets naturally.
- For transactions handled WITHOUT 3055 events — `SetTurnPhases`, stale/no-op
  passes — the server must push an empty 3055 sync packet afterwards via
  `_push_transaction_ack(session)` or the **next transaction is dropped
  client-side**. This is why the Withdraw button did "nothing": the player's
  last transaction was `SetTurnPhases` (fire-and-forget, no reply), which left
  the pipeline blocked, so the `QuitGameTransaction` never reached the server.
- Withdraw = `QuitGameTransaction`; the server detects it by the **presence**
  of `m_QuitEntireSeries`/`m_Surrendered` in `inner_bytes` (a normal concede
  sends `m_QuitEntireSeries=false`, so the boolean value must NOT gate the
  detection).

### Pass priority flow (3029 `PassPriorityTransaction`)
- Detected via `b"PassPriorityTransaction" in inner_bytes`
- Player passes → if at EndTurn/Discard → hand the turn to the AI
  (`_run_ai_turn`); otherwise advance one phase + GreenLight back to the player
- The client auto-passes Discard with a NO-OP animation when the hand fits the
  max hand size (sends no transaction) → the server auto-skips Discard→EndTurn
  and runs the AI turn when hand ≤ 7

### AI turn (`_run_ai_turn`)
The AI has no client, so its turn plays out server-side, but **pass-gated and
phase-paced**:
1. Pause `AI_THINK_DELAY = 3.0s` (simulate thinking)
2. Walk through TURN_PHASES with the AI as active+priority player, one phase
   per packet, `AI_PHASE_DELAY = 1.0s` between pushes
3. AI gets a GreenLight in every phase and passes it server-side; Draw phase →
   `_ai_draw_card` (top of AI deck → hand); FirstMainPhase →
   `_ai_play_resource` (play a shard from hand if able, push
   `CardUpdated(PlayedResources)` BEFORE `ResourceCardPlayed` so the shard
   clears the chain)
4. During `SecondMainPhase`, after normal hand plays are exhausted, the AI
   searches its warzone for metadata-backed manual resource abilities. X-cost
   abilities spend all remaining resources (subject to their authored minimum),
   so cards such as Soul Marble can be pumped before the AI ends its turn.
   AI-owned choice effects continue through the normal server-side AI choice
   policy rather than pausing for human input.
5. When a phase is a human stop (opponent-stop), the AI turn pauses and the
   human gets a `ResumeTopOfChain` GreenLight → `BattleStateInactivePriorityWindow`
   (this is the ONLY context in which `GainGreenLight` pushes the priority
   window / Pass button on the opponent's turn)
6. EndTurn → switch turn player back to the human; the player's new turn
   auto-starts and `player_resource_played_this_turn` is reset

### GreenLight / Pass button
- The client shows the Pass Priority button ONLY when `HasPriority()` is true,
  which is set exclusively by `GreenLightSessionEventArgs` (class 48)
- The server pushes GreenLight after the keep-handler phases AND after every
  card play, otherwise the button stays hidden

### Victory / Defeat / Withdraw
- `commands.push_battle_game_end(handler, session, winners, losers)` pushes the
  **GameEnded** event (class 2, 3055 channel) with winner/loser UID lists →
  client shows the Victory/Defeat screen.
- `!game_end victory|defeat` (debug) and the client's **Withdraw** button both
  route through it. Withdraw = `QuitGameTransaction` (3029), detected from the
  raw `m_QuitEntireSeries` bytes; the server ends the game as a **loss** for
  the player. Works in FRA and campaign.
- A campaign win also sends `gameendnotify` (reveals the quest-giver NPC, sets
  `TutorialDone`).

### Mulligan (sequential)
- Order: turn player acts → opposing player asked → back to the other if they
  didn't keep → leave Mulligan phase only when BOTH have kept
- AI (`_resolve_ai_mulligan`): keeps if a shard is in hand, else mulligans
  drawing one fewer card per redraw (7→6→…→0), forced-keep at 0 cards
- Every mulligan fully reshuffles that session's deck in `game_cards`
  (`position` column), scoped by `session_id`
- The AI's opening hand = first 7 of the shuffled AI deck (dealt to `hand`)

### Replay event logging
- `session_events` table records every SessionEventArgs batch pushed to any
  player (player AND AI actions), in send order: session_id, target_player_uid,
  seq (ms timestamp), event_class (int32 LE prefix of payload), event_bytes
- Hooked via `game_engine.event_logger`, installed by hconnect_server at
  startup; fires in `TutorialGame.make_network_packet` for every packet sent
- `replay_server.py` consumes completed PvP sessions and writes a client
  `.replay` GameEventLog plus a `game_replays` index row. The
  `qreplaylst`/`replayfetch` service endpoints expose those indexed artifacts
  to the client in paged/chunked form.

## Chat System

### HConnect Chat Protocol
Chat messages flow through `target=Session, instance=chat` with JSON body.
Client sends with empty issuer; server responses use `issuer=Session, target=chat`.

### Action Types
- `rjoin` — join room (server pushes chat history on this)
- `rleave` — leave room
- `rchat` — send chat message
- `glist` — request global user list (no room field)

### rchat Response Format
```json
{"action":"rchat","room":"general","rflg":"","user":"TestPlayer","msg":"Hi","flags":"","icon":"I_CoyotleMageMale_Happy"}
```
Required fields: `room`, `rflg`, `user`, `msg`, `flags`. Optional: `icon`.

### Chat Symbol Tags
Bracket tags render as in-game glyphs:
- Shards: `[BLOOD]` `[DIAMOND]` `[RUBY]` `[SAPPHIRE]` `[WILD]`
- Numbers: `[0]` through `[20]`, `[X]`, `[XX]`
- Circled: `[(0)]` through `[(10)]`, `[(X)]`

Authenticated handlers are tracked in the server's active-client registry;
messages are broadcast to connected users and inactive entries are cleaned up
after the configured timeout. The chat path is independent of the in-game
battle session, so reconnecting to a battle does not require replaying chat
history beyond the room join.

## Champions and talents

- `champion_templates` holds all playable champions, rebuilt from gamedata:
  69 `selectable=1` gendered PvE champions, with an `is_player` flag.
  The player always resolves their PvE starting-class portrait via
  `champion_templates WHERE is_player=1`; AI opponents may reference other
  rows (overlap acknowledged). Necrotic Mage Male =
  `1961cdc1-cf00-4ad2-a0e8-65d8d0d89337`.
- `champions.talents` column stores the chosen talent GUIDs. The 2037 handler
  persists the full talent list the client sends and returns the complete
  17-field `ChampionBits` incl. `ChampionTalents` — without `ChampionTalents`
  the client's `HandleTalentsUpdate` NREs and shows "unsaved changes".
  The server does NOT compute default talents; the client always sends the
  full list and the server just persists it.
- Deck save/update (2089 AddNewDeck / 2095 UpdateDeck) parse `PvEChampionId`
  and set the champion's `last_deck_id`. This fixes the "Deck.0" deck-list
  miss (the client validates the champion's last deck before campaign entry).
- `game_cards` denormalises `card_type` + `template_guid` so player and AI cards
  share one lookup path (`JOIN card_templates ON ct.guid = gc.template_guid`
  via the `_card_full_data` helper) for CardUpdated/cost/threshold lookups.

## Champion abilities and charge powers

### How They Work

Every PvP champion has a charge power (e.g. "Pay 4 charges → deal 2 damage").
These are **data-driven** via the BOM (Bill of Materials) pattern and do NOT
need per-ability custom code.

Three tables drive the system:

| Table | Purpose |
|-------|---------|
| `champion_abilities` | Maps champion → ability GUID + name, charge cost, threshold, game text |
| `champion_templates_extended` | Extended champion metadata (race, class, health, faction) |
| `ability_effects` | BOM leaf-effect chain for each ability (shared with PvE talents) |

### Architecture

```
champion_abilities.ability_guid    ←── champion's charge power
         │
         ▼
ability_effects (BOM chain)        ←── ordered list of leaf effects
         │
         ├── CardModifierAbilityEffectTemplate   (heal, damage, stat buffs)
         ├── SummonTokenTroopAbilityEffectTemplate (summon a troop)
         ├── DrawNCardsAbilityEffectTemplate      (draw cards)
         ├── BuryCardAbilityEffectTemplate        (bury/discard)
         ├── ActivateAbilityEffectTemplate        (sub-ability chain)
         └── ... (24+ effect types)
                  │
                  ▼
abilities/framework/bom.py         ←── @leaf_register executors
         │
         ▼
  custom handler?                  ←── @register_custom_ability (rare)
  (abilities/cards/*.py)
```

When a champion activates their charge power in battle:

1. **Cost deduction** — server checks `champion_abilities.charge_cost` +
   threshold, deducts charges/SP, pushes `ChampionChargePointsChanged` +
   `ChampionSpellPointsChanged` events. Handled by
   `abilities/framework/champions.py:apply_ability_cost()`.

2. **Stack on chain** — the ability is pushed onto the game chain (client
   stack). When the chain resolves, `abilities.resolve_effect(ability_guid)`
   walks the BOM.

3. **BOM execution** — each leaf effect in `ability_effects` is dispatched to
   its registered executor in `bom.py`. Most effects are automatic (deal
   damage, summon, draw, buff, etc.).

4. **Custom handlers** — if a `@register_custom_ability` handler exists for
   the GUID, it runs INSTEAD of the BOM walk. Custom handlers go in
   `abilities/cards/` (one file per ability GUID).

### Database Seeding

`AssetExtraction/gamedata_seed.py` is the single extractor for all
client-derived tables. It reads the original gzip-compressed `Data/gamedata`
file when `HEX_GAMEDATA` (or the legacy `GAMEDATA`) is set; otherwise it reads
the complete `Records/*.jsonl` snapshot. `static.ensure_schema()` calls this
pipeline only when `card_templates` is empty, so existing databases are not
silently replaced.

To inspect or compare a client installation after gamedata changes:

```bash
HEX_GAMEDATA=/path/to/Data/gamedata python3 AssetExtraction/extract_gamedata.py
HEX_GAMEDATA=/path/to/Data/gamedata python3 AssetExtraction/extract_gamedata.py --compare-db hconnect.db
```

The older per-table extractors remain as historical/offline utilities; they
are not part of fresh database startup and must not be used to regenerate
`static.py`.

### Adding a New Custom Champion Ability

Only needed for effects NOT expressible in the BOM (e.g. "Replenish Spell
Power" which gains a random 3-5 SP).  Create a file under
`abilities/cards/`:

```python
# abilities/cards/my_champ_power.py
from abilities.registry import register_custom_ability

@register_custom_ability("the-ability-guid-here")
def my_power(game, session, db, handler, pl_t, ai_t, bstate, guid, scid):
    # implement effect ...
    return "log message"
```

The function signature matches all leaf executors.  The module is
auto-discovered at startup via `abilities.registry.discover()`.

### Framework Utilities

`abilities/framework/champions.py` provides shared helpers for activation
and common champion ability patterns:

| Function | Purpose |
|----------|---------|
| `validate_ability_cost()` | Check charge/SP/threshold requirements |
| `apply_ability_cost()` | Deduct costs, push events, handle SP escalation |
| `heal_self()` / `damage_self()` | Modify player health |
| `gain_resource()` | Add threshold resource |
| `draw_cards()` | Draw N cards |
| `random_sp_gain()` | Random spell point gain |
| `stack_push_ability()` | Push ability onto the game chain |

### Coverage snapshot

- **109 of 119** selectable PvP champion abilities have BOM coverage
- **10** have ability GUIDs not found in AbilityTemplate records (need
  manual handlers or updated gamedata extraction)

## License

- `LICENSE` — AGPL-3.0 (network copyleft: modified server code must be
  shareable with players).
- `NOTICE` — intent statement: no charging players; changes must be visible
  (Stop Killing Games spirit).

## Frost Ring Arena

### Campaign DataTypes
- 10001: JoinCampaignArena — returns the current opponent from the player's
  saved FRA run snapshot (or `Success=false` until a deck is assigned)
- 10003: AssignArenaDeck — starts a run and saves 20 rank-eligible opponents
- 10007: GetMasterListOfChallengers — returns that player's saved 20 challengers

The `fra_challengers` table is keyed by `users.id` and stores one generated
roster per player. Ranks 10, 15, and 20 select the known boss-version
encounters. Positions 9, 12, 14, 17, and 19 always select an eligible elite
version of a normal deck family; other positions select normal encounters.
`DeckTemplate` names ending in `_Elite` identify elite variants, while the
known boss families are Phenteo, Eurig, Princess Cory, and Hogarth. Elite
upgrades are not automatically treated as bosses for rewards.

### ArenaChallenger Encoding
Each challenger is an ObjFmt struct with 5 fields:
- ChallengerID (ulong), EncounterDeck (ResourceId→guid), ChallengerName (string),
  IsBoss (string "True"/"False"), Equipment (List<ResourceId> — empty)

### Key Learnings
- Request parsing exposes ResourceId as `m_Guid`; some manually encoded response
  structs use the client's expected `guid` sub-field. Confirm the concrete
  handler before reusing either shape.
- `raw` tcode breaks decoder — always encode complex types inline with manual BytesIO
- Arena requires a valid deck before entering (pops up "no valid decks")
- Scrolling background without opponents = client parsing response successfully
  but no game session to start

## Protocol encoding details

### ResourceId
```python
# Field header: EncounterDeck;idx;ResourceId_type;1;
w("EncounterDeck"); sep(); ...; w("1"); sep()
# Sub-field: guid;idx;Guid_type;0;36;<guid_string>
w("guid"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
w(str(len(guid_bytes))); sep(); buf.write(guid_bytes)
```

### Inline Lists (no raw tcode)
```python
# List header with count
w("Challengers"); sep(); ...; w("0"); sep()
w(str(len(items))); sep()
# Each element: idx;size_slot;type_idx;num_props;
for idx, item in enumerate(items):
    w(str(idx)); sep(); w(str(len(sizes)-1)); sep(); ...
```

### bool Fields
```python
w(name); sep(); ...; w("0"); sep()
w("1" if val else "0")  # bare '1'/'0', NO SEPARATOR after
```

### Debugging Encoding
- Run `python3 -c "import hconnect_server; print(srv.encode_X(...)[:500])"` to verify
- The size table must match exactly — check with manual byte counting
- `struct.pack` for integers is always little-endian (`<i`, `<Q`, `<q`)

## Card Zones (ECardCollections)

Card locations in the game are tracked by `ECardCollections`, a `[Flags]` enum in `Game.Shared.Mechanics`:

| Value | Zone | Description |
|-------|------|-------------|
| 0 | None | No zone / invalid |
| 1 | Deck | Face-down draw pile |
| 2 | Hand | Cards held by player |
| 4 | Champions | Champion cards (command zone) |
| 8 | Warzone | Troops/artifacts in play |
| 16 | Discard | Graveyard for destroyed/discarded cards |
| 32 | Void | Exiled/removed from game |
| 64 | PlayedResources | Shards/resources played this turn |
| 128 | CastSpells | Spells on the stack (chain) |
| 256 | Underground | Tunneling troops |
| 512 | Choosing | Cards being chosen/selected |
| 1024 | Mod | Modded/transformed zone |
| 2048 | Simulacrum | Simulacrum copies |
| 4096 | UI_Warzone | Warzone as displayed in UI |
| 8192 | UI_Constant | Constants zone in UI |

### Card Movement Events

| Event | CLASS_ID | Description |
|-------|----------|-------------|
| `CardMovedSessionEventArgs` | 50 | Move card to a zone (to_collection + to_location + index). Does NOT update cache collection — send `CardUpdated` with new collection first. |
| `CardDrawnSessionEventArgs` | 7 | Card drawn from deck (stats tracking). Does NOT move the card — combine with `CardUpdated(Hand)`. |
| `ResourceCardPlayedSessionEventArgs` | 16 | Shard played — auto-moves to `PlayedResources` and runs animation. Fields: PlayerId, SessionCardId, free(bool). |
| `CardDestroyedSessionEventArgs` | 8 | Card destroyed/sacrificed |
| `CardDiscardedSessionEventArgs` | 38 | Card discarded |
| `CardVoidedSessionEventArgs` | 36 | Card exiled/voided |
| `CardGraveyardedSessionEventArgs` | 37 | Card sent to graveyard |

### Resource/Threshold Display Events

| Event | CLASS_ID | Fields | Description |
|-------|----------|--------|-------------|
| `PlayerCurrentResourcePoolChanged` | 33 | PlayerId, Operation(Add=1), Delta, NewValue | Updates current/available resource display |
| `PlayerTotalResourcePoolChanged` | 34 | PlayerId, Operation, Delta, NewValue | Updates max/total resource display |
| `PlayerResourceThresholdChanged` | 35 | PlayerId, Color(ECardShards), Operation, Delta, NewValue | Updates threshold gem display for a color |

`ECardShards` values: Colorless=1, Blood=4, Ruby=8, Sapphire=16, Wild=32, Diamond=64.

Thresholds from `card_templates.threshold_json` use indices: `{0:Blood, 1:Ruby, 2:Sapphire, 3:Wild, 4:Diamond}` — must convert to bit-flags: `{0:0, 1:4, 2:8, 3:16, 4:32, 5:64}`.

## Game Replays

### Overview
Replays are stored as `.replay` binary files (C# BinaryWriter format, version 5).
The client replays a game by reading the file into a `GameEventLog`, then feeding
its events into a `ReplayClient` (extends `ClientSessionBase`) which injects them
into the normal battle rendering pipeline.

### File Format
```
[int32 Version = 5]
[GameMetaData header]
[int32 GenerationCount]
  [Generation 0..N]:
    [int32 EventCount]
      [EventTargets]:
        [int32 TargetCount]
        [uint64 TargetIDs...]        -- UID targets (observing player filter)
        [int32 ClassID]              -- SessionEventArgs subclass ID
        [int32 CompressedLen]
        [bytes Compressor.Compress(data, 9)]  -- event.ToByteArray()
        [int32 TOffset]              -- ms since previous event (if Version > 4)
```

### GameMetaData Header
Serialized field order:
| Version gate | Field | Type |
|-------------|-------|------|
| — | Version | int32 (always first) |
| V > 0 | SessionFlags | uint32 |
| V > 0 | ServiceID | uint64 (UID) |
| — | SessionName | len-prefixed UTF-8 string |
| V > 0 | StartUTC | fixed 25-byte UTF-8 `"yyyy/MM/ddTHH:mm:ssZ00:00"` |
| V > 1 | FinishUTC | same format |
| V > 1 | TournamentRound | int32 |
| V > 2 | IsPublic | bool |
| V > 3 | SFormat | len-prefixed UTF-8 string |
| V > 3 | SPoints | int32 |
| V > 3 | STemplate | len-prefixed UTF-8 string |
| V > 0 | PlayerCount | int32 |
| V > 0 | Per player: ID (uint64), Name (len-prefixed UTF-8), Winner (bool, V > 1), Deck (len-prefixed `ProfileDeckTemplate.ToBytes()`) |

### ReplayClient Playback (ReplayClient.cs)
`ReplayClient` extends `ClientSessionBase` and reads events from a `GameEventLog`.
Key methods:

- **FlushGenZero()** — Plays generation 0 twice (initial setup/bootstrapping),
  then resets `_event` counter to 0. Called before the main update loop.

- **Update()** — Called each frame. Processes events sequentially:

  **Event buffering**: Events are accumulated into `_gameEvents` list. Processing
  breaks (flushes) when encountering:
  - `GreenLightSessionEventArgs` — priority windows
  - `AbilityActivationDataRequiredSessionEventArgs` — ability prompts
  - `TriggeredAbilityActivationDataRequiredSessionEventArgs`
  - `EncounterModDialogSessionEventArgs`
  - `TurnPhaseUpdatedSessionEventArgs` — phase changes

  **Observer filtering**: Only events targeting the observing player's UID
  are included, EXCEPT:
  - `CardUpdatedSessionEventArgs` — always included (unless Hand + Nulling)
  - `PlayerUpdatedSessionEventArgs` — always included
  - `PlayerOptionListSessionEventArgs` / `PlayerOptionSessionEventArgs` — always EXCLUDED

  **Sleep/delay types**: After playing these, a 500ms delay is inserted:
  - `TroopCardPlayedSessionEventArgs`
  - `SpellCardPlayedSessionEventArgs`
  - `ArtifactCardPlayedSessionEventArgs`
  - `BlockersAssignedSessionEventArgs`

  **Mulligan pausing**: When `TurnPhase == Mulligan`, each `GreenLightSessionEventArgs`
  targeting a unique player triggers a pause. The pause is removed when that player
  accepts/mulligans their hand.

- **Player speed mode** (`_usePlayerSpeed`, default on): When an event has
  TOffset ≥ 5000ms, all events in the current batch are delayed by that amount.
  The UI shows a "think timer" countdown with a Skip button.

### Replay Controls (UIReplayControls.cs)
Five buttons during replay:
| Button | Action |
|--------|--------|
| Play | `ReplayResume()` |
| Pause | `ReplayPause()` |
| ThinkOn | Enable player speed (show think timers) |
| ThinkOff | Disable player speed (instant playback) |
| SkipThink | Skip current think timer |
| Exit | `ExitReplay()` — jumps to GameEnded event |

### Server Protocol

#### Replay Listing — `ServiceProfile`, TransID 80000
- Action: `qreplaylst`
- Request JSON: `{action: "qreplaylst", SesFilter, SFormat, STemplate, Offset, Count}`
  - Filters support `%` suffix for LIKE matching
- Response: `ReplayQueryResponse` JSON deserialized from Envelope bytes
  ```json
  {
    "Req": {...},
    "Records": [{
      "StartUTC": "2025-01-15 14:30:00",
      "EndUTC":   "2025-01-15 14:45:00",
      "Server":   <uint64 UID>,
      "Session":  "session.name.here",
      "SFormat":  "CONSTRUCTED",
      "SPoints":  0,
      "STemplate": "Standard",
      "PubGame":  true,
      "Players":  "Alice vs Bob",
      "Winners":  "Alice",
      "TournRound": -1,
      "ExpireUTC": "2025-04-15 14:45:00"
    }]
  }
  ```
- DateTime format: `"yyyy-MM-dd HH:mm:ss"`

#### Replay Download — `ServiceGameSession`, TransID 160000
- Action: `replayfetch`
- Request JSON: `{action: "replayfetch", Session: "<session_name>", Offset: 0, Size: 16384}`
- Response: raw binary (the `.replay` file chunk) with 5-byte trailer:
  ```
  [data bytes...][More: byte][FileLength: int32 LE]
  ```
  - `More` = 1 if there are more chunks, 0 if this is the last
  - `FileLength` = total uncompressed file size
- Client sends sequential requests incrementing Offset until More=0

### Entry Points
- **Local replay file**: `GameReplay.RunReplay(file, showNames)` — loads `.replay`
  from disk via `GameEventLog.FromStream()`, creates `BattleStateContext.Replay`
  settings, transitions to `EGameState.Battle`
- **Remote replay**: `UIReplayViewVM` UI lists replays from server, downloads on
  demand (chunked fetch), then calls `RunReplay()` with the local temp file
- **Console command**: `replay.load <file>` in debug console

### Replay status
Session events are captured in `session_events` with their target, sequence,
event class, and serialized bytes. The separate `replay_server.py` worker
assembles completed PvP streams into the client GameEventLog format, stores
the artifact under `HEX_REPLAY_DIR` (default: a `replays/` directory beside
the database), and indexes it in `game_replays`. It merges identical event
payloads delivered to both players while retaining recipient-specific events.
The endpoints are implemented by `services/replay.py`: `qreplaylst` returns
the browser metadata and `replayfetch` returns 16 KiB chunks with the client's
five-byte continuation/length trailer.

## Game Client Data (Hex Installation Folder)

The game installation folder (`D:\SteamLibrary\steamapps\common\HEX SHARDS OF FATE`)
contains asset and template data that the private server references.

### Directory Layout

```
HEX SHARDS OF FATE/
├── Hex.exe
├── Hex_Data/              # Unity engine data
│   ├── Managed/           # .NET assemblies (Assembly-CSharp.dll, etc.)
│   ├── Resources/
│   └── StreamingAssets/
├── Data/                  # Game template & configuration data
│   ├── gamedata           # 6.8 MB gzip blob — all card/ability/set templates
│   ├── localization.db    # 16 MB localization DB
│   ├── tutorial.txt       # Tutorial script DSL
│   ├── Sets/              # Card set definitions (JSON/XML)
│   ├── Abilities/
│   ├── Items/
│   └── Localization/
├── AssetBundles/          # Unity asset bundles (visual assets, prefabs)
│   ├── cardsets/
│   ├── gameboards/
│   ├── general/
│   └── pve/               # Campaign/PvE content
│       ├── adventurezone01/   # AZ1 (Entrath overworld map)
│       │   ├── common.ab      #   Common UI elements
│       │   ├── map.ab         #   Overworld map prefab
│       │   ├── nodescapes.ab  #   Node/encounter visual assets
│       │   ├── p_hmm_wrenscastle.ab   #   Human dungeon
│       │   ├── p_dwrf_cavein.ab       #   Dwarf dungeon
│       │   ├── p_elf_aryndelpalace.ab #   Elf dungeon
│       │   ├── p_orc_xamahuac.ab     #   Orc dungeon
│       │   ├── p_ncrtc_necropolis.ab #   Necrotic dungeon
│       │   ├── p_cytl_amblingmesa.ab #   Coyotle merchant
│       │   └── ... (one per race + shared)
│       ├── adventurezone02/   # AZ2
│       ├── arena_frost/       # Frost Ring Arena
│       └── strongholds/       # Stronghold buildings
└── version.txt
```

### The gamedata Binary Blob

`Data/gamedata` is a **gzip-compressed binary blob** (6.8 MB → 104 MB uncompressed).
It uses a custom text-based format with section markers:

```
$$$---$$$           # File header
.SectionName.       # Template section identifier
$$--$$              # Section-to-data separator
{JSON-like record}  # Template data (variant JSON with `.` separators)
$$--$$
{JSON-like record}
...
$$$---$$$           # Next section
```

**Section format**: Each section starts with `$$$---$$$`, a section name on its own
line (e.g., `AbilityTemplate`, `CardTemplate`, `SceneData`), then `$$--$$` on a line.

**Records** are JSON-like objects with:
- `_v`: version array `[{ "TypeName": version }, ...]`
- `_t`: C# type name (`"Reckoning.Game.AbilityTemplate"`, etc.)
- `m_Id`: `{ "m_Guid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" }`
- `m_Name`: human-readable name
- Type-specific fields

Delimiters use `.` instead of `,` between key-value pairs.

### Sections in gamedata

| Section | Type | Contents |
|---------|------|----------|
| `AbilityTemplate` | 42+ versions | All card abilities |
| `AbilityEffectTemplate` | 15+ versions | Ability effect definitions |
| `CardTemplate` | 40+ versions | Card definitions (cost, types, shards) |
| `CardSetTemplate` | 4+ versions | Card sets (AZ1="1d1ecaea-...", AZ2="ccde3b6a-...")
| `DeckTemplate` | 4+ versions | AI encounter decks |
| `SceneData` | 3+ versions | Campaign scenes (dungeons, encounters) |
| `ChampionTemplate` | ... | Champion class/race templates |
| `GlobeData` | (empty) | Globe definitions (loaded from AssetBundles) |
| `AdventureZone` | (empty) | Adventure zone data (loaded from AssetBundles) |

### Key Campaign GUIDs Extracted from gamedata

| Name | GUID | Type |
|------|------|------|
| **Castle Crayburn** | `5bcba43a-95c7-44b4-ba09-a3555a5edf05` | DungeonTemplate |
| **AZ1 CardSet** | `c363c22e-1c03-43c0-a5d3-e3e8759120e7` | CardSetTemplate |
| **AZ1 Equipment** | `7cef8345-4f5b-407a-a15e-1978ef5ff2db` | CardSetTemplate |
| **AZ2** | `1d1ecaea-47c9-4d2a-91a0-9c78fdac49a1` | CardSetTemplate |

### How the Client Loads Templates

1. `TemplateManager.LoadTemplatesFromDisk()` loads from `Data/Campaign/*` folders
   (if they exist on disk) OR from the compressed `gamedata` blob via `FastParseTemplates()`
2. The fallback path reads section-separated data from gamedata and passes each
   section's buffer to the appropriate template parser
3. Campaign visual assets (prefabs, scenes, backgrounds) are loaded from
   `AssetBundles/pve/*.ab` Unity bundles on demand
4. The client's `TemplateTypeInfo` in `CampSummary` responses can override
   asset paths, but local templates serve as defaults

### Mapping Server Responses to Client Assets

When the server sends `CampSummary` with `TemplateTypeInfo`:
- `AssetBundle`: subfolder under `AssetBundles/pve/` (e.g., `"adventurezone01"`)
- `LevelPrefab`: prefab name within that bundle (e.g., `"map"` for overworld)
- `NodesPrefab`: node visual assets (e.g., `"nodescapes"`)
- `BackgroundPrefab`: background elements (e.g., `"common"`)
- `CampaignTemplateId`: GUID matching a `SceneData` template in gamedata

### Extracting data

Use the repository extractor for table-shaped output and database comparison:

```bash
HEX_GAMEDATA='/path/to/Data/gamedata' \
  python3 AssetExtraction/extract_gamedata.py --manifest /tmp/gamedata.json
HEX_GAMEDATA='/path/to/Data/gamedata' \
  python3 AssetExtraction/extract_gamedata.py --compare-db hconnect.db
```

For low-level inspection, the blob can still be decompressed directly:

```python
import gzip
with gzip.open('Data/gamedata', 'rb') as f:
    data = f.read()

# Find section markers
import re
sections = re.findall(rb'\n([A-Za-z0-9_]+)\n\x24\x24--\x24\x24\n', data)
```

### Note on WSL Paths

The Windows path `D:\SteamLibrary\steamapps\common\HEX SHARDS OF FATE` maps to
`/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE` in WSL.

## Campaign Resources.Load Paths

The Hex client's `UICampaignZoneVMBase.InstantiatePrefab()` calls
`UnityEngine.Resources.Load(string)` to load dungeon UI prefabs.
The paths are NOT simple GameObject names — they use the compiled
asset paths from `globalgamemanagers` ResourceManager's `m_Container`.

### How to Find Resource Paths

```python
import UnityPy
env = UnityPy.load('Hex_Data/globalgamemanagers')
for obj in env.objects:
    if obj.type.name == 'ResourceManager':
        tree = obj.read_typetree(check_read=False)
        for item in tree['m_Container']:
            path, info = item
            # path = Resources.Load key (e.g., "campaign/azmap/azmap")
            # info['m_PathID'] = GameObject PathID in resources.assets
```

### Key Resource Paths

**Shared/Generic:**
| Path | PID | Description |
|------|-----|-------------|
| `campaign/shared/dungeonmap` | 1533 | Generic dungeon map overlay |
| `campaign/shared/dungeonmap_old` | 1508 | Older version |
| `campaign/shared/fow_camera` | 289 | Fog-of-war camera |
| `campaign/shared/npcindicator` | 5541 | NPC indicator marker |

**AREA — AZ1 Overworld Map:**
| Path | PID | Description |
|------|-----|-------------|
| `campaign/azmap/azmap` | 3344 | AZ1 overworld map (UIDungeonCameraController) |
| `campaign/az01/nodes` | 3731 | AZ1 area nodes |

**DUNGEON — Crayburn Castle (AZ1):**
| Path | PID | Description |
|------|-----|-------------|
| `campaign/az01/crayburncastle/prefabs/nodes` | 3728 | Castle nodes (UIDungeonNodes) |
| `campaign/az01/crayburncastle/prefabs/background` | 5751 | Castle background |

**Other AZ1 Dungeons:**
| Path | PID | Dungeon |
|------|-----|---------|
| `campaign/az01/devonshirekeep/prefabs/nodes` | 3729 | Devonshire Keep |
| `campaign/az01/fortromor/prefabs/nodes` | 3730 | Fort Romor |
| `campaign/az01/smolderingdead/prefabs/nodes` | 6511 | Smoldering Dead |
| `campaign/az01/theusurper/prefabs/nodes` | 5679 | The Usurper |
| `campaign/az01/tranquildream/prefabs/nodes` | 5357 | Tranquil Dream |
| `campaign/az01/tranquildream/prefabs/background` | 5937 | Tranquil Dream bg |

**AZ2 Dungeons:**
| Path | PID | Dungeon |
|------|-----|---------|
| `campaign/az02/brutecrownbluff/prefabs/nodes` | 5333 | Brute Crown Bluff |
| `campaign/az02/greatmachinegraveyard/prefabs/nodes` | 5005 | Great Machine Graveyard |
| `campaign/az02/kraken/prefabs/nodes` | 5646 | The Kraken |
| `campaign/az02/ruinsofkukatan/prefabs/nodes` | 5647 | Ruins of Kukatan |
| `campaign/az02/nodes` | 3732 | AZ2 area nodes |

### Component → Resource Mappings

Verified via UnityPy `read_typetree` on `resources.assets` MonoBehaviours:

| Component | MonoScript PID | Resource Path |
|-----------|---------------|---------------|
| `UIDungeonCameraController` | 2603 | `campaign/azmap/azmap` |
| `UIDungeonNodes` | 3497 | `campaign/az01/crayburncastle/prefabs/nodes` |
| `UIDungeonZoneViewModel` | 3055 | level19 `UIDungeonViewModel` |
| `UIDungeonCharacterToken` | 81 | level19 `DungeonCharacterToken` |

### Campaign Flow with Resources

In `UICampaignZoneVMBase.OnStartUp_GetSummary()`:
```csharp
// These are synchronous — called BEFORE AssetBundle loads
if (!string.IsNullOrEmpty(backgroundPrefab))
    m_BackgroundObject = InstantiatePrefab(backgroundPrefab);
if (!string.IsNullOrEmpty(nodesPrefab))
    m_NodesObject = InstantiatePrefab(nodesPrefab);

// Then AssetBundle loads asynchronously
if (!string.IsNullOrEmpty(assetBundle) && !string.IsNullOrEmpty(levelPrefab))
    m_LevelPrefab.SetAssetRequest(levelPrefab, AssetBundleFile.PvE, assetBundle, "", false);
else
    OnPrefabLoaded(); // Called directly if no bundle
```

Key: `InstantiatePrefab` → `Resources.Load(PrefabName)` → `Instantiate()`.
The `PrefabName` must match the exact path from `globalgamemanagers`.
Simple GameObjects names (like "AZMap") return NULL without the path prefix.

## Critical game-session debugging

### KeyNotFound + Undefined UID = Invalid ChampionId
The client's `OnTurnPhaseUpdated` does `State.Cards[ChampionSessionCardId]`.
If `PlayerUpdated.ChampionId` is `SessionCardId(UID(0))` (type 0 = undefined),
the client logs `KeyNotFoundException` + `Attempting to create a new
SessionCardId with a UID of an undefined type!`. This corrupts the card cache
and causes cascading failures (playability, charge power, resource display).

**Fix**: Every `Game()` object created for battle events has
`player_champion_card_id = None` by default. `push_player_updated` accepts an
optional `champ_id` parameter. Store the player and opponent champion
`SessionCardId`s during game initialization and pass them whenever publishing
`PlayerUpdated`; a fresh Game without those IDs is not safe for battle events.

### Game.card_defs lost between objects
Every `Game(session_id, pl, ai)` creates a FRESH `card_defs` dict. If a card
was set up (CardDef, shards, abilities) on a previous Game object, those are
lost. Calling `push_card_updated` on a new Game looks up `self.card_defs[scid]`
which returns None → no thresholds/cost/atk/def rendered.

**Fix**: Before `push_card_updated`, call `_card_full_data(game, scid, guid)` or
manually create a CardDef on the new Game object. This is required in the Prep
phase warzone refresh (player AND AI) or cards lose their stats.

**Champion ability buttons vanishing at AI turn start**: the AI-turn re-push of
champion CardUpdateds (to keep `State.Cards[ChampionSessionCardId]` warm for the
client's KeyNotFound-prone `OnTurnPhaseUpdated`) rebuilt the champion CardDef on
a fresh Game that had empty `card_defs`. With no CardDef, `push_card_updated`
attaches **zero abilities**, and `GoHUDPlayer.UpdateAbilityButtons` (driven by
`BattleAnimationUpdateChampionAbilities` on every PlayerOptionList) rebuilds the
buttons from the now-empty ability list — wiping all charge powers / spell
powers / talents. Always re-register the champion CardDef (with its full
`abilities` list) on the fresh Game before pushing the champion CardUpdated.

### Champion starting health
The client's profile health uses `TalentManager.GetStartingHealth` =
`ChampionClassData.m_StartingHealth` (by race+class): Cleric=22, Mage=17,
Ranger=18, Rogue=20, Warlock=20, Warrior=25 (all races share the class value).
The in-battle starting health must match. AI champions (trainers) resolve their
health from their own `ChampionTemplate.m_StartingHealth` — all 8 AZ0 trainers
are health **10**.

**Source tables**: `champion_class_data` (race+class → health/hand size) and
`champion_template_data` (champion GUID → health/hand size), both populated
from gamedata.

### Champion abilities & phases
`talent_abilities` (one-to-many): talent → abilities with `charge_cost`,
`spell_cost`, `activatable_phases` (bitmask, bit N = 1<<N), `casting_behavior`
(QuickAction=64 / BasicAction=8), and `condition` (a compact function-name spec
for gated PreGame abilities, generated from gamedata `m_TriggerCondition`).
- PreGame (2) → `1<<2`; FirstMain (10)|SecondMain (19) → `(1<<10)|(1<<19)`
- The `PlayerOptionList` filter (`_filter_affordable_abilities`) includes an
  ability only when the current phase bit is set AND charges/SP >= cost.
  **QuickAction abilities (casting_behavior=64) skip the phase gate** — they
  may be activated in any priority window (mirrors the client's
  `CanActivateAbilityBase`, Session.cs:1528).
- PreGame pass (`ability.apply_pregame_abilities`) runs each selected talent
  ability marked PreGame, evaluates its `condition` spec via the
  `ability._CONDITIONS` function registry (e.g.
  `pregame_shards_in_deck:Blood,8` = 8+ Blood shards in deck), and applies its
  BOM effects only when it returns True. Token effects whose metadata targets
  `Deck` create and shuffle those cards before the opening hand is dealt.
  Deck-count conditions use the deck size at the start of PreGame, so multiple
  100-card talents do not change one another's branch selection.
  Shard Attuned: one ability per color, each `pregame_shards_in_deck:COLOR,8`
  → +1 health per satisfied color (for BOTH players).
- Ability effects live in `ability.py`; `hconnect_server.py` only wires calls.
- A talent's ability is manual iff its AbilityTemplate has no
  `m_TriggerEventType`; `PreGameEvent` = pre-game passive.

### PreGame condition functions (`ability._CONDITIONS`)
`talent_abilities.condition` stores a compact spec naming a Python function in
`ability._CONDITIONS` plus its args (the function-name-as-condition pattern):
- `pregame_shards_in_deck:COLOR,COUNT` — player has COUNT+ cards of COLOR in deck
  (Shard Attuned `...:Blood,8` etc.)
- `pregame_cards_in_deck:COUNT` — COUNT+ cards in deck (Cosmic Powers: 100,
  Friend of Jank Bot: 150)
- `pregame_is_dungeon` — running a dungeon encounter (Heroism/Fearless/Fortitude)

The extractor derives these from `m_TriggerCondition` (`RequiresCardsControlled`
→ shards/cards-in-deck; `IntAttrFilter IsDungeonBoss` → is_dungeon). Unconditional
abilities have `condition=''` (always fires).

### Ability bill-of-materials (`ability_effects`)
A champion ability is a bill-of-materials:
- **`talent_abilities`** is the head of the bill — the granted ability with its
  cost + phase requirements (and casting_behavior).
- **`ability_effects`** expands it into the ordered leaf effect templates
  (from `AbilityTemplate.m_AbilityEffectList`): columns `ability_guid`,
  `effect_guid`, `effect_order`, `effect_type` (AbilityEffectTemplate class
  name), `param` (m_AbilityToInvoke for `ActivateAbilityEffectTemplate`).
- If a granted ability has no BOM rows it *is* the leaf — its cost/phases sit
  in `talent_abilities` like any other (e.g. Replenish has a bespoke handler
  in ability.py; Soothsaying `55a6024b` → draw 1 → invoke DiscardACard).
- `ability.resolve_effect(ability_guid)` returns a bespoke `_EFFECTS` handler
  if one exists, else a BOM-walking wrapper that runs each leaf in order
  (Soothsaying: **draw a card, THEN discard** — effects never change the turn
  phase). `ActivateAbilityEffectTemplate` leaves recurse into their `param`
  ability. Leaf executors are keyed by `effect_type` in ability.py (`_LEAFS`).
- **Choose-and-discard (e.g. Soothsaying)**: the discard is prompted via the
  authoritative **class-23** flow so it happens AFTER the draw resolves:
  the server pushes `AbilityActivationDataRequiredSessionEventArgs` (class 23,
  `effect_group_id=1`, `effect_instance_ids=[0]`,
  `ability_template_id=06570445` DiscardACard) and sets
  `battle_state['pending_discard_ability']`. The client's
  `OnAbilityActivationDataRequested` → `BattleStateUseTriggeredAbility` →
  `BattleStateConfigureAbility` shows the hand-card picker. The player's
  `SetAbilityActivationDataTransaction` carries the chosen card (last Card-type
  UID in the transaction); the server discards it and clears the pending flag.
  The champion option carries target instances for both the granted ability
  (`eb7e48cd` "You") and the invoked discard ability (`84e4acf1` hand-card) so
  `GetTargetsFor` finds the hand cards at prompt time.
- **Playability is 100% server-driven**: the client's `ShowCardSelectionType`
  (UIBattle.cs:7608) and `CanPlayCard` (7651) only read the `ECardUsage` flags
  from the `PlayerOptionList` we push — the client runs NO own cost/threshold
  check. The "highlight then un-highlight" flicker is a server sequencing
  artifact: the main-phase options push is immediately followed by a post-play
  refresh that may remove the card. Do not push stale option refreshes.

### Talent seed extraction (AssetExtraction)
The `talent_data`/`talent_abilities`/`ability_effects` rows are not
hand-written. They are extracted by `gamedata_seed.py` from either the client
file or `Records/` when a fresh database is created. The old
`extract_talents.py` script is retained for historical analysis only; it does
not define the server's seed source.

The extractor derives: `has_ability` = talent has an `m_Abilities[].m_CardAbilityId`;
cost = `m_ChargePointCost`/`m_SpellPointCost`; phase = `PreGameEvent` trigger →
PreGame (4) else main phases (525312); casting = `m_CastingBehavior`; BOM =
`m_AbilityEffectList` expanded transitively through `m_AbilityToInvoke`;
condition = function-name spec from `m_TriggerCondition`.

### Card extraction + card-ability BOM (AssetExtraction/gamedata_seed.py)
`card_templates` is seeded from **gamedata's `CardTemplate` section** (7,214
records — the complete card pool), not the `.card` files under `Sets/` (a
5062-card subset). `AssetExtraction/gamedata_seed.py` reads the client data or
the equivalent `Records/` files and inserts the extracted rows directly into
SQLite:

- `card_templates` — guid, set_guid, name, rarity, cost, attack, defense,
  card_type, socket_count, no_pvp, is_pve, threshold_json, abilities_json,
  **attributes** (the `ECardAttributes` bitmask from `m_AttributeFlags`, e.g.
  Flight=2, FirstStrike=16384 "Swift Strike", Steadfast=32).
- `ability_effects` — the transitive leaf-effect BOM for EVERY card
  ability, merged with the talent BOM into the `ability_effects` table at seed
  time so champion AND card abilities resolve through the same walker.

`abilities_json` keeps the FULL `m_CardAbilities` list (earlier imports stored
only the first ability). The old `extract_cards.py` parser is still used
internally for shared BOM parsing, but its static-file writer is no longer part
of the supported seed workflow.

For an EXISTING DB, `restart.sh` also runs `migration.py` (if present) which
adds the `attributes`/`card_state`/`card_attributes`/`card_abilities` columns
and backfills them; the migration is deleted after running.

### Docker/client gamedata seeding
`AssetExtraction/gamedata_seed.py` is the shared parser used by the server and
the offline extractor. It reads the original gzip-compressed client file when
available, or the local `Records/` snapshot otherwise. Set `HEX_GAMEDATA` to
the mounted file path when a deployment should use a particular client build:

```bash
HEX_GAMEDATA=/client/HEX/Data/gamedata python3 AssetExtraction/extract_gamedata.py
HEX_GAMEDATA=/client/HEX/Data/gamedata python3 AssetExtraction/extract_gamedata.py --compare-db hconnect.db
```

On Windows with Docker Desktop, use a permanent state directory under the
user's home directory:

```powershell
$HexState = Join-Path $HOME 'HexServer'
$ClientData = 'D:\SteamLibrary\steamapps\common\HEX SHARDS OF FATE\Data'
New-Item -ItemType Directory -Force $HexState | Out-Null
docker run --rm `
  -v "${HexState}:/hex/state" `
  -v "${ClientData}:/client/Data:ro" `
  -e HEX_DB_PATH=/hex/state/hconnect.db `
  -e HEX_GAMEDATA=/client/Data/gamedata `
  ghcr.io/<github-owner>/<repository>:latest
```

On a fresh database, `static.ensure_schema` populates the client-derived
tables from that file. Existing databases are not replaced. If the variable
is absent, the complete `Records/` snapshot is used. Server-owned
catalogue/configuration tables such as `store_items`, `pack_set_map`, tournament
types, and chest probabilities continue to use server seed data.

### JSON int-key corruption
`game_sessions.turn_order_json` stores dicts like `{4: 1}`. JSON serializes int
keys to strings: `{"4": 1}`. On reload, `th.get(4, 0)` misses the string key,
creates a duplicate `int 4` entry.

**Fix**: `battle_engine.load_state` converts threshold dict string keys back to ints.

### Shard flag mapping
`threshold_json` uses 6-element format `{values: [c, b, r, s, w, d]}` and
`list: [indices]`. Indices map to ECardShards via
`{0:0, 1:4, 2:8, 3:16, 4:32, 5:64}` (not `{0:4, 1:8, ...}`). Index 0 is
Colorless (flag 0), index 1 is Blood (flag 4).

## Container and Fargate deployment

### What's in the image
`docker/Dockerfile` builds a `python:3.11-slim` image that runs the network
services and background workers in one task:
- `hconnect_server.py` → **TCP 9933** (HConnect game protocol)
- `proxy.py` → **TCP 8081** (Steam auth / collection / `/news/`)
- `gamemodes/tournament_server.py` → tournament pool/refill scheduler
- `replay_server.py` → completed-session replay worker

Bundled in the default image: the server `.py` files, campaign data, and
`news/`. The database, `Records/`, and generated starter-deck data are not
copied into the image. The client's `UnityConfig.json`/`config.ini` is NOT
used by the server — it is the CLIENT config that must point at the Fargate
task's public address.

At startup, `docker/docker_bootstrap.py` validates `HEX_GAMEDATA` (or the legacy
`GAMEDATA` alias). If `HEX_DB_PATH` does not exist, it creates `Records/` from
the mounted gamedata when that directory is absent or incomplete, then creates
the database in an atomic temporary file with `static.ensure_schema`, which
invokes the shared `AssetExtraction/gamedata_seed.py` pipeline. It then generates
`generated/starter_decks.json` from the same DeckTemplate source and runs the
supported test suite through `tests/run_all.py`. The runner creates one database
snapshot and restores it into an isolated in-memory database for each test
process. Set `HEX_RUN_TESTS_ON_BOOT=0` to skip the first-start test pass.
Test failures are reported but do not block service startup unless
`HEX_FAIL_ON_TEST_FAILURE=1` is set. Existing database files are reused and
are not overwritten.

Client-derived data is loaded only when the new database has no
`card_templates`. Therefore setting `HEX_GAMEDATA` while using the default
database snapshot does not replace that snapshot. A deployment that must derive
data from a particular client installation should set `HEX_DB_PATH` to a
database inside a mounted host directory, then mount the client `gamedata`
file read-only. A fresh Docker database requires either that gamedata mount or
a complete `HEX_RECORDS` directory mount.

### Build & run locally
```powershell
docker build -f docker/Dockerfile -t hex-server .
$HexState = Join-Path $HOME 'HexServer'
New-Item -ItemType Directory -Force $HexState | Out-Null
docker run --rm `
  -p 9933:9933 -p 8081:8081 `
  -v "${HexState}:/hex/state" `
  -e HEX_DB_PATH=/hex/state/hconnect.db `
  hex-server
```

### Fargate notes
- **Task definition**: one container, port mappings `9933:9933` and `8081:8081`.
  Use a public IP or an ALB/NLB in front for external testers.
- **State**: for a Windows Docker Desktop deployment, use a permanent
  directory such as `$HOME\HexServer` and mount it at `/hex/state`; set
  `HEX_DB_PATH` to `/hex/state/hconnect.db`. Keep the database, `-wal`, and
  `-shm` files together. For Fargate, use an EFS or task volume mounted at
  `/hex/state` instead. SQLite over network storage is suitable only for low
  concurrency; use local host storage for development and a single-writer
  deployment for production.
- **Concurrency**: `hconnect_server.py` uses one thread per client connection
  and SQLite with `check_same_thread=False`. SQLite serializes writes; fine for
  a handful of simultaneous testers, not for high concurrency. Evaluate
  PostgreSQL before running 32-player tournaments.
- **Lock retries**: `db.py` retries transient SQLite lock operations at the
  statement/commit boundary. Retry milestones are logged as normalized
  `[sqlite-retry]` lines in `/tmp/hconnect_requests.log`; inspect the in-process
  aggregate with `db.sqlite_retry_stats()`. This identifies hot SQL shapes but
  does not replace finding transactions that are held open too long.
- **Security**: the game protocol (9933) is plain TCP and the proxy (8081) is
  plain HTTP. Put the task in a private subnet behind a load balancer, or add
  TLS at the LB for 8081. No auth on 9933 beyond the Steam ticket passthrough.
- **Logs**: `docker/docker_entrypoint.sh` tails `/tmp/*.log` to stdout so `docker logs`
  / CloudWatch Logs captures server, request, proxy, tournament, and replay output.

### Client connection
Testers edit their `config.ini` (or `UnityConfig.json` for a custom build) so
`GameServerIP = <fargate-public-ip-or-lb>:9933` and `CZEAuthUrl` /
`http://<address>:8081/...`. Steam auth flows through the proxy.

## How To (quick index)

The companion docs hold the living details — this file explains the *why*.
When a "How to..." question comes up, start here; each entry points at the
section with the full answer.

| How to... | Where |
|-----------|-------|
| Restart the server | `bash restart.sh` — applies a pending migration, compiles the server/packages, starts HConnect, proxy, and tournament services, then verifies the ports. |
| Debug a stuck client / find what the client rejects | **Check the client log FIRST**: `/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Hex_Data/output_log.txt`. Grep `Error`, `Exception`, `Command handler not found`, `KeyNotFoundException`. See "Log Files". |
| Find an unknown GUID's name/type | `grep "<guid>" Data/localization.db`, or decompress `Data/gamedata` and find the `_t`/`m_Name` record. See "Looking Up GUIDs from Game Data". |
| Find Unity `Resources.Load` asset paths | UnityPy over `Hex_Data/globalgamemanagers` `ResourceManager.m_Container`. See "Campaign Resources.Load Paths". |
| Understand campaign types, state, quest linkage, scenes, rewards, and XP | `docs/CAMPAIGN.md` |
| Read the gamedata blob | `python3 AssetExtraction/extract_gamedata.py --manifest ...`; use direct `gzip.open()` only for low-level record inspection. |
| Add a DB column / schema change | Add to BOTH `static.py` DDL (fresh DBs) and `migration.py` (existing DBs), committed together. See "Database Schema & Migrations". |
| Encode an ObjFmt response | `encode_objfmt_response()` plus the type/size-table rules. See "ObjFmt Encoding Rules" and "Protocol encoding details". |
| Push a card/player/inventory update | `push_card_updated` (needs a registered CardDef), `push_player_updated` (needs a valid `champ_id`), and ProfileGenericUpdate 2211 for inventory. See "Critical game-session debugging" and "PlayerProfile". |
| Run a battle / test an encounter | Debug console (backtick): `camp.encounter AZ0_Necrotic` (scene names in `docs/ENCOUNTERS.md`). In-game chat: `!resource`, `!threshold`, `!drawcard`, `!game_end`. See `docs/DebugConsole.md`, `docs/COMMANDS.md`. |
| Run card/rules sweeps | Run `python3 tests/run_all.py` for the supported suite, or run `python3 tests/tests_set1_sweep.py` separately for the optional Set 1 rules sweep. |
| Understand the canonical rules | `RULES.md` (turn phases, playability, champion abilities, mulligan). Battle decisions derive from it. |
| Debug/replay a game session | Inspect the `session_events` DB table and use the replay service work described in "Replay event logging". |
| Identify a template's fields | Check `Assembly-CSharp-firstpass/Game/Shared/...` source or gamedata `_v` version arrays. |
| Implement a played spell / card effect | `abilities.resolve_played_spell` plus BOM leaves in `abilities/framework`; target in `bstate["player_spell_target"]`. See "Code organization and abilities". |
| Why the resource/charge/SP bar reads 0 | Every DB change must push a fresh `PlayerUpdated` + warzone CardUpdateds from `_fresh_game` (MVC). See "MVC: the DB is the model". |

### Which log to read for what
- **Client hang / stuck UI / missing event** → client `output_log.txt` FIRST.
- **Wrong behavior that "worked before"** → `/tmp/hconnect_log.txt` (server) + `/tmp/hconnect_requests.log` (per-request routing).
- **Auth / collection sync failures** → `/tmp/proxy_log.txt`.

---

## Code organization and abilities

### Where logic lives (keep it here)
- **`db.py`** — the shared connection `_db` + **reusable DB helpers** (`db_*`): `db_template_by_guid`, `db_card_ability_list`, `db_card_uses`, `db_bump_card_use`, `db_card_state`, `db_warzone_troop_count`, `db_card_stat_mods`. Handler methods are thin wrappers over these.
- **`abilities/`** — authoritative ability resolution: BOM leaf executors,
  conditions, triggers, deathcry, transforms, stat modifiers, custom ability
  registry, and `resolve_played_spell`. `ability.py` is a compatibility facade
  for imports that still use the older module name.
- **`battle_engine.py`** — **game flow**: turn/priority phase cycle (`BASE_TURN_PHASES`/`COMBAT_TURN_PHASES`), `build_turn_phases`, `is_self_stop`/`is_opp_stop`, `current_phase`, `advance_phase`, `load_state`/`save_state`/`default_state` (battle state lives in `session.turn_order_json`).
- **`ai.py`** — **AI opponent logic**: `run_ai_turn` (turn driver), drawing,
  resource/troop plays, attacker declaration, combat damage, defense, and
  mulligan decisions. It delegates card lookups and event publication to the
  session handler.
- **`hconnect_server.py`** — HConnect protocol + transaction handlers (3029 card play, ability activation, combat commit), pushing events to the client. AI methods are thin delegators to `ai.py`.

**AI personality (aggressive)** — the AI is "attack attack attack": at FirstMain it switches to the `COMBAT_TURN_PHASES` list when it controls a ready troop, declares **all** eligible troops as attackers at DeclareAttack (pushing `AttackDeclared`/`CombatListing`/`CombatSession` and marking them `Attacking|HasAttacked|Tapped` unless Steadfast), auto-declines to block, and deals their attack to the player's champion at AssignDamage (`bstate["player_health"]`; the player loses at ≤ 0). Attackers persist in `battle_state["ai_attackers"]`.

### AI personality model (ported from the client)
`ai.py` ports `AIPersonality.cs`'s value model: `PERSONALITIES` keyed by attitude (`Aggressive`/`Comfortable`/`Defensive`), each with `min_x_value` (the `MinimumXValue` attack threshold — Aggressive=3, Comfortable=4, Defensive=5 from AIPersonality.cs:32) and `alpha_strike` (Aggressive attacks with every eligible troop; Comfortable/Defensive hold back troops whose attack is below `min_x_value`). The campaign race configs (`campaign._AZ0_RACE_CONFIG`) carry an `ai_personality` per trainer (e.g. GarethKay=Comfortable, Nerissa=Defensive, WhisperingBreeze=Aggressive); `lookup_training_encounter` returns it and battle init stores it on the handler (`self._ai_personality`), defaulting to `Aggressive`.

### Why we can't make the client authoritative (client session architecture)
The client is a fixed binary; we can only drive it through the network protocol. It has two mutually-exclusive startup modes:
- **`NetworkGameClient`** (default, `Main.cs:914`) → `SessionClient : ClientSessionBase` — a non-authoritative **projection** that mirrors the server's events. This is what our backend talks to.
- **`StandaloneGameClient`** (`Main.cs:910`, gated by the **startup command-line flag** `GameConfig.Standalone`, GameConfig.cs:171) → `StandaloneSession : AuthoritativeSessionBase` — an **authoritative, fully offline** loop: `SubmitTransaction` enqueues locally, `SendPlayerOptions`/`SendPlayerInformationUpdates` return false, `DispatchSessionEvent` routes to the local `StandaloneGameClient` and local `AIPlayer`/`SessionClient` sessions, and `HasDeckAndChampInfo` returns false.

There is **no network message that turns a connected client into an authoritative session** — the two modes are chosen at launch and are closed to each other. Even if we flipped `GameConfig.Standalone`, the client would run a self-contained offline game with no profile/decks/campaign/rewards/our AI. Hence the server-authoritative model, and porting the client's AI heuristics (personality, combat) into Python `ai.py`, is the correct architecture. The full client AI framework lives in `HexClient/Assembly-CSharp-firstpass/Game/Shared/AI/` (AIPersonality, AICombat, AITactical, AICardEvaluator, AIAbilityManager, AIFunctions, AIHints, recognizers) and is a **design reference** for our Python port, not directly invocable.

### The chain (stack)
The chain holds pending resolutions for the current phase. It is **empty at the start of each phase**; during the phase, troops, spells, champion abilities and triggers (Deathcry) get **pushed onto it** as single items (the card lives in the **CastSpells** zone, which is the visual stack; a champion ability/trigger is anchored to its source card via `AbilityPushedOnChainSessionEventArgs`). When **both players pass** priority, the **top resolves and executes** (pushing `TopOfChainResolved` + `RemovedTopOfChain`), then priority is re-granted for the next item (`ResolveTopOfChain` GreenLight → the client's pass button reads "Resolve <CardName>"); when it empties, `ChainEmpty` fires and the phase advances.

State lives in `battle_state["stack"]` (list of items: `{"kind": "troop"|"spell"|"ability"|"trigger", "source_uid", "ability_guid", "targets", ...}`) plus `stack_player_passed`/`stack_ai_passed`; helpers in `battle_engine.py` (`stack_push/pop/top/empty/set_pass/both_passed/reset_passes`). Resolution is centralized in `hconnect_server._resolve_stack_item` (troop→warzone, spell→BOM+discard, ability→resolve_effect+class-23 discard prompt, trigger→`ability.resolve_stack_trigger`). One chain item per top-level card/ability — sub-effects (Soothsaying's draw+discard, ActivateAbilityEffectTemplate-invoked sub-abilities) are bundled and resolve together, so a counterspell has a single entry to cancel. The AI auto-passes, so the player's pass (or the AI turn's) resolves items.

Chain event classes (added to game_engine.py, matching the client wire format): `AbilityPushedOnChain` (22), `TopOfChainResolved` (41), `RemovedTopOfChain` (42), `ChainEmpty` (77). `EPriorityContext.ResolveTopOfChain` (=6) is returned by `_priority_context_for` whenever the stack is non-empty.

### MVC: the DB is the model, push to the view on every change
The client's resource/charge/SP bar and card icons are driven by events the server sends. **Every time the DB battle state changes, push a fresh `PlayerUpdated` (both players)** with the real values, and **re-push CardUpdateds for all warzone cards** (`_push_warzone_card_updates`) — a bare `Game()` defaults to 0/20 and wipes the UI. This is why `_push_main_phase_options`, `_push_attack_options`, `_push_phase_options_empty`, the stop branch and every auto-passed phase all build from `_fresh_game()` and include a PlayerUpdated. Watch the phase order: build the game from `bstate` **after** Prep refills resources / Draw draws, or re-sync the game's fields from bstate right before the push (otherwise the client flickers to 0/0 at turn start).

### Card types (`game_engine.CARD_TYPE_BY_DB`)
`card_templates.card_type` strings map to `ECardTypes` bit flags: Troop=2, Artifact=32, BasicAction=8, QuickAction=64, **Constant=2048**, Gear=4, Token=4096, Quick=8192. Combined strings (`"Troop|Artifact"`, `"Constant|Quick"`) are OR'd by `card_type_from_db`. **Constants resolve to the Warzone** when played (the client renders them in its constants area — `Session.cs` places Warzone cards whose type includes Troop|Artifact|Constant there). Non-permanent actions (BasicAction/QuickAction) go CastSpells → Discard.

### Playability (main-phase options)
All **non-shard** cards are offered when the player can afford the cost and meet the threshold. A card whose ability **targets a troop** (via `AbilityTargetTemplate` game text in the `target_templates` table) is **not** offered when the relevant player has no warzone troops:
- "troop you control" → needs a friendly troop
- "opposing/enemy troop" → needs an AI troop
- bare "target troop" (e.g. "Destroy target troop") → needs any troop

### Played-spell resolution
When a BasicAction/QuickAction resolves, the handler stores the picked target in `bstate["player_spell_target"]` and calls `ability.resolve_played_spell`, which walks each ability's BOM (`ability_effects`) and applies the common leaves. **All events go onto the same `game` object → ONE 3055 packet** (mirrors Soothsaying's single-packet draw+discard flow).
- `CardModifierAbilityEffectTemplate` → apply the `+/-N[ATK]/[DEF]` delta parsed from the ability's `game_text` to the target once per ability; if the target's defense hits 0 it dies.
- `DrawNCardsAbilityEffectTemplate` → draw a card.
- `MoveCardToZoneEffectTemplate` → move the target from the crypt to hand (Call the Grave).
- `GrantAbilityEffectTemplate` → grant the referenced ability to the target
  (for example Atrophy), provided the extracted ability GUID is present in
  `card_abilities_meta`.

### Sacrifice costs (Abominate)
Abominate's card template has `m_SacrificeTarget` = target template `38e37324` ("a troop you control") — *"As an additional cost to play this, sacrifice a troop."* The sacrifice is a cost on the **card template** (`card_templates.sacrifice_target`, extracted from gamedata), not the ability. When the spell resolves, the sacrificed troop (the FIRST non-source UID in the play transaction) is moved to its owner's graveyard via `_sacrifice_troop`; the effect target is the LAST UID. **The sacrificed troop still DIES, so its Deathcry triggers** (e.g. Abominate on a Spiritbound Spy).

### Troop death & Deathcry
Deathcry fires on ANY death — sacrifice, card effect (defense ≤ 0), or battle. `kill_troop` (effect/battle) and `_sacrifice_troop` both call `ability.resolve_deathcry`, which resolves the dying card's `Deathcry` abilities' BOM: CardModifier, DrawNCards, and **TransformCardAbilityEffectTemplate** (parses the target template from the game text's `data=<guid>` card link). Deathcry effects are resolved directly (no trigger-stack modelling yet).

**Transforms reuse the SAME `card_uid`** (`ability.transform_card`): the `game_cards` row's `template_guid`/`card_type`/abilities/attributes/base stats are copied from the NEW template in place, the card moves from the graveyard back to the owner's warzone, `StartedATurnOnYourSide` is preserved (a Spy that survived a turn can attack as the Phantom that turn) while `Tapped`/`Attacking`/`HasAttacked`/`CameOutThisTurn` are reset, and `card_attack_mod`/`card_defense_mod` carry over (so Abominate's +3/+3 targeting a sacrificed Spy makes a 4/4 Phantom). The instance keeps its ORIGINAL template in `game_cards.original_template_guid` (set at creation, preserved through Shift/Transform); **Reversion restores the instance to that original template** as though it were a fresh card (resetting template_guid/card_type/abilities/attributes/mods/uses) — a transformed Spy reverts to Spiritbound Spy, not Phantom.

### `_card_full_data` is instance-aware
It reads the card's **current** `card_abilities` and `card_attack_mod`/`card_defense_mod` from `game_cards` (Shift moves abilities, PowerShifted triggers grant +atk/+def), so a Prep re-push keeps a shifted Lifedrain / granted ability / buff instead of reverting to the template's printed list.

**Gotcha — `game_cards.card_uid` is the FULL encoded UID**, not the instance id: `card_uid = cid.uid.to_uint64()` (e.g. `15617`), so a `SessionCardId`'s `uid.uid64` equals the DB `card_uid` directly. Do **NOT** `>> 8` it when querying `game_cards` (that yields the instance 61 and finds nothing — which silently dropped Shift buffs/abilities on re-push). Use `scid.uid.uid64` as-is.

### Spell-power escalation
Each use of a **spell power** (spell_cost > 0) permanently bumps that spell's SP cost by +1, tracked in `bstate["player_sp_uses"]` (`eff_sc = spell_cost + uses`). **Charge powers must NOT escalate** (they have spell_cost = 0 — the old code bumped `sp_uses` for every ability and broke Replenish: "need 3 charges/0 SP, have 4/0"). `_push_champions_warm` carries the mods onto the champion CardUpdated (`spell_point_cost_mods`) so the client's button shows the increased cost (Soothsaying 4→5 after one use).
