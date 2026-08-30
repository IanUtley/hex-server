# PlayerProfile.cs — Server Integration Reference

**File:** `HexClient/Assembly-CSharp-firstpass/Game/Client/PlayerProfile.cs` (5,130 lines)  
**Purpose:** Client-side mirror of server state. All server pushes mutate this object.

> Pre-built binary — all fixes must be server-side.

---

## DataTypes We Push From Server → Client Handler

| DataType | Push Name | Handler | Effect |
|----------|-----------|---------|--------|
| **2211** | `ProfileGenericUpdate` | `HandleProfileGenericUpdate` | Dispatches to type-specific handlers below |
| 2205 | `CardsAdded` | `HandleCardsAdded` | Bulk card add/upgrade via `AddNewCardToCollection` |
| 2212 | `BalanceUpdate` | `HandleBalanceUpdate` | Gold/platinum update |

### ProfileGenericUpdate (2211) — Sub-types

The `HandleProfileGenericUpdate` method decodes the inner payload and dispatches by C# type:

| # | Inner Type | What It Does |
|---|-----------|-------------|
| 1 | `ProfileGenericBatchUpdate` | Cards→`AddNewCardToCollection`, Items→`AddInventoryItem`, GoldDelta/PlatDelta→`UpdateBalance` |
| 2 | `ProfileGenericCardColUpdate` | **Binary** `CardGroupId.DecodeGroup` → `AddCardToCollection` per card instance |
| 3 | `ProfileGenericInvenColUpdate` | **Binary** `InvenGroupId.DecodeGroup` → `AddInventoryItem` per inventory instance |
| 4 | `ProfileGenericCurrencyUpdate` | `UpdateBalance` with gold/plat values |
| 5 | `ProfileGenericAccountUpdate` | XP, level, cap info |
| 6 | `ProfileGenericDisplayRewards` | Reward popup |
| 7 | `ProfileGenericLoginStreamDone` | `SendCollection()` — signals login complete |
| 8 | `ProfileMainData` | Template data download (incremental) |

---

## Inventory Methods

### `AddInventoryItem` (line 4259) — THE key method

```csharp
AddInventoryItem(UID itemInstanceUid, ResourceId itemTemplateId, int quantity,
    string escrowStatus=null, bool? bound=null, DateTime? claimDate=null)
```

1. **Deduplication:** Scans `m_InventoryItems` for existing item with same `ItemUid` **AND** `TemplateId`.
   If found → removes old entry, then adds fresh one with new values.
2. Fires `OnInventoryUpdated` → stash/pack-bag UI refreshes.
3. `SendCollection()` → 30-second debounced Notifier sync.

**Key insight for pack count updates:** Push with the SAME `ItemUid` (UID) and `TemplateId` (pack GUID)
as the original inventory entry, with a new `Quantity`. The client replaces rather than duplicates.

### `HandleInventoryUpdate` (line 1986) — `InventoryUpdated` event

1. Matches by `ev.ItemInstanceUid`
2. If Quantity=0 or expired claim → removes item
3. Otherwise adds/updates
4. Fires `OnInventoryUpdated` + `SendCollection()`

### `HandleFullInventoryRefresh` (line 1969) — `FullInventoryRefresh` event

Clears `m_InventoryItems` and repopulates from `ev.PlayerItems`. The "nuclear option."

### `ProfileGenericInvenColUpdate` (line 2382)

**Binary format** — `InvenGroupId.DecodeGroup(BinaryReader)` produces:
```
InvenGroupId { TemplateId, Quantity, Escrow, NoTrade, ClaimDate }
  + List<ulong> instance UIDs
```
For each instance UID, calls `AddInventoryItem(UID(InventoryItem, uid), ...)`.
Deferred until `TemplateManager.PostLoad` completes.

### `ProfileGenericBatchUpdate.Items` (line 2304-2321)

ObjFmt format — calls `AddInventoryItem(inventory_bits)` for each item.
Note: pushes via `SetInventoryItem` semantics — adds NEW entries rather than updating existing ones.

---

## Card Methods

### `AddCardToCollection` (line 4030)

```csharp
AddCardToCollection(ICard card)
```
Checks init → `m_CardList.AddCard(card)` → fires `OnCardAdded` → deck builder UI updates.

### `AddNewCardToCollection` (line 4045)

```csharp
AddNewCardToCollection(card_instance_bits bits)
```
Calls `AddCardToCollection(bits)` then adds to `m_NewCards` dict (the "new card" glow in collection).

### `HandleCardsAdded` (line 1855) — `CardsAdded` event (2205)

For each card: if already exists by CardId → removes old + updates decks, then `AddNewCardToCollection`.

### `LoadCardsFromBits` (line 886)

Iterates card bits → `AddCardToCollection` each. If `asNew=true` → adds to `m_NewCards`. Calls `SendCollection()`.

### `HandleOpenCardPack` (line 1689)

Called by `OpenCardPack.OnResponse` BEFORE the UI handler:
1. `LoadCardsFromBits(resp.NewCardInstances, asNew=true)`
2. Adds chest items

### `CardCache.AddCard` (in CardCache.cs)

```csharp
AddCard(ICard card)
```
Checks `TemplateId.IsValid && CardId.IsValid` → `ContainsKey` check (DupeAdded++ if exists) → stores in dictionary keyed by CardId.

---

## Key UI Events Fired

| Event | Fired By | UI Effect |
|-------|----------|-----------|
| `OnInventoryUpdated` | AddInventoryItem, HandleInventoryUpdate, SetInventoryItem | Stash/pack-bag refreshes |
| `OnCardAdded` | AddCardToCollection | Deck builder count updates |
| `OnBalanceUpdated` | UpdateBalance | Currency display updates |
| `OnCardRemoved` | RemoveCardFromCollection | Deck builder count updates |
| `OnDeckAdded` | AddDeck(deck_bits), GetPlayerDecksResponse | Deck list refreshes |
| `OnPlayerDeckUpdated` | GetDeckInfoResponse | Deck editor refreshes contents |

## Deck Methods & Types

### `deck_bits` — 23 DataMember Fields
Id, DeckName, PVEChampionId, PVPChampionId, talent_1-5, equipment_1-7,
ActiveGems (Dictionary<ulong, EGemTypesNew>), CardsInDeck, CardsInSideboard,
LockHolder, deck_sleeve, gameboard, Coin, player_id.

### `GetDeckInfoResponse` — 15 Fields
DeckName, DeckID (UID), PvEChampionId (UID), PvPChampionId (ResourceId),
DeckCardIDs (List<ulong>), SideboardCardIDs (List<ulong>),
EquipmentIDs (List<ResourceId>), TalentIDs (List<ResourceId>),
DeckSleeveId, GameboardId, CoinId, ActiveGems (Dictionary), Persona (enum),
Error, ErrorMessage.

### `GetPlayerDecksResponse` — 4 Fields
PlayerDeckIDs (List<UID>), PlayerDeckNames (List<string>), Error, ErrorMessage.
Note: NOT a `Decks` list of deck_bits.

### `AddNewDeckRequestArgs` — 14 Fields
RecID, DeckName, PvEChampionId, PvPChampionId, DeckCardIDs, SideboardCardIDs,
EquipmentIDs (6 slots), TalentIDs, ActiveGems, DeckSleeveId, GameboardId,
CoinId, Overwrite, Persona.

## Dictionary & UID ObjFmt Format

Dictionary generic args use `!`: `Dictionary`2#UInt64!EGemTypesNew`
Empty dict: `field;idx;Dict_type;0; 0;`

UID: `field;idx;Game.Shared.UID;1; m_UID64;idx;UInt64;0;hex;`
| `OnPlayerCardListUpdated` | LoadCardsFromBits, GetPlayerCardIDListResponse | Collection view refreshes |

---

## DB ↔ Client Sync Flow

**Server-authoritative** — client mirrors DB state via pushes:

1. **Login:** `HandleProfileStream` → `reckoning_bits` (cards, decks, inventory, champions, currency)
2. **Cards:** `ProfileGenericBatchUpdate.Cards` (ObjFmt) or `ProfileGenericCardColUpdate` (binary)
3. **Inventory:** `ProfileGenericBatchUpdate.Items` (ObjFmt, adds new) or `ProfileGenericInvenColUpdate` (binary, upserts)
4. **Currency:** `ProfileGenericCurrencyUpdate` or `BalanceUpdate` (2212)
5. **After any change:** `SendCollection()` starts 30-second debounced Notifier sync

---

## Enums & Values

### UID.Type (byte enum)
- Card = 1
- InventoryItem = 12
- Champion = 13 (ServicePlayer? check)
- Deck = 17

### EOpenCardPackError
- 0 = Ok
- 1 = InvalidInstance
- 2 = InvalidItemType
- 3 = CardCreationFailed
- 4 = GemCreationFailed
- 5 = NotEnoughInventory
- -1 = InternalServerError

### ERarity (for glow/flip display)
- Common = 0, Uncommon = 1, Rare = 2, Legendary = 3, Epic = 4, Land = 5, Promo = 6
- Glow shown for `rarity >= Rare`

## Inventory UID Tracking

The client identifies inventory items by `UID` (ItemUid) which must be consistent
across pushes for `AddInventoryItem` deduplication to work (matches on UID+TemplateId).

**UID sources:**
- Profile push (`InventoryIds` in reckoning_bits): sequential IDs starting after stardust/chest
- `_inventory_pending` block: `2000 + quantity` (used for initial 2211 push)
- Purchase response (`GrantedInventory`): `1000 + store_item_id` — **this is what the client actually uses**

**For pack count updates**, the UID must match what the client received during purchase.
Store the UID in `player_inventory.client_item_uid` when the pack is acquired,
then reuse it when pushing quantity updates via `push_inventory_to_client`.
