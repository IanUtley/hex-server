"""Equipment catalog derived from the client Records, deck equipment helpers.

Nothing here is stored in the database: the equipment items
(``InventoryItemData`` records of type ``InventoryEquipmentData``) and the
equipment-modified cards (``CardTemplate.m_EquipmentModifiedCard``) are read
from the Records store once per process and cached.

Chest-loot pools
----------------
The client data marks which booster chest awards an equipment item only in
the item's design note ("Set 1 Chest Loot", "Scars Chest", "Frostheart Chest
Loot", or just "Chest").  The item's own ``m_SetId`` is not the pool: "Set 2
Chest Loot" items also live in the Set01_PvE_Arena and Set03_PvE_Promo card
sets.  A note resolves to a set number by, in order:

* an explicit "Set N" in the note;
* the words before "Chest", matched against the start of a CardSetTemplate
  name ("Scars" -> Scars of War, "Frostheart" -> Frostheart);
* for a bare "Chest" note, the item's own card set.

Set numbers come from the CardSetTemplate bundle name (``Set07_PvE_Promo``,
``Set001``), falling back to ``m_SetNo`` (which is 1 for every PvE promo set),
so a chest of a booster set draws from every item whose note names that set.

Deck equipment
--------------
A PvE deck equips at most one item per equipment type.  Each item upgrades
specific cards; the client data ships the upgraded card for every
(base card, equipped items) combination as an equipment-modified CardTemplate
whose serialized TAC lists the base card GUID followed by the equipment GUIDs.
"""

import base64
import re
import threading
from dataclasses import dataclass

ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# deck_bits.equipment_1..6 follow Reckoning.Game.EEquipmentType order.
EQUIPMENT_SLOT_ORDER = ("Head", "Chest", "Gloves", "Feet", "Weapon", "Trinket")

_GUID = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SET_IN_NOTE = re.compile(r"\bset\s*0*(\d+)\b", re.IGNORECASE)
_SET_IN_BUNDLE = re.compile(r"^set0*(\d+)", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


@dataclass(frozen=True)
class EquipmentItem:
    guid: str
    name: str
    rarity: str
    equipment_type: str
    set_guid: str
    design_notes: str
    description: str
    is_live: bool
    chest_set_number: int | None   # booster set whose chests award it


_LOCK = threading.Lock()
_CATALOG = None    # (items by guid, {set number: {rarity: (guid, ...)}})
_VARIANTS = None   # {base guid: ((frozenset(equipment), variant guid), ...)}


def _store():
    from gamedata import DEFAULT_RECORD_STORE
    return DEFAULT_RECORD_STORE


def _record_guid(value):
    if isinstance(value, dict):
        value = value.get("m_Guid") or value.get("guid")
    return str(value or "").lower()


def _set_numbers():
    """{card set guid: set number} and [(lowercase set name, number)]."""
    numbers, names = {}, []
    for card_set in _store().load("CardSetTemplate"):
        bundle = _SET_IN_BUNDLE.match(str(card_set.field("m_BundleName") or ""))
        try:
            number = int(bundle.group(1)) if bundle else int(card_set.field("m_SetNo") or 0)
        except (TypeError, ValueError):
            number = 0
        if not number:
            continue
        numbers[card_set.guid.lower()] = number
        name = str(card_set.field("m_Name") or "").strip().lower()
        if name:
            names.append((name, number))
    return numbers, names


def chest_note_set_number(design_notes, item_set_guid, set_numbers, set_names):
    """Return the booster set number a chest-loot design note names, or None."""
    notes = str(design_notes or "").strip()
    lowered = notes.lower()
    if "chest" not in lowered:
        return None
    explicit = _SET_IN_NOTE.search(notes)
    if explicit:
        return int(explicit.group(1))
    prefix = lowered[:lowered.index("chest")].strip()
    if prefix:
        matches = {number for name, number in set_names if name.startswith(prefix)}
        return matches.pop() if len(matches) == 1 else None
    return set_numbers.get(str(item_set_guid or "").lower())


def _equipment_records():
    """Every InventoryEquipmentData record of the Records snapshot.

    About 90 equipment records carry a literal newline inside their design
    note (e.g. the "Scars Chest CB ..." notes), which strict JSON rejects, so
    the shared record store skips them.  Read the section here with control
    characters allowed so those items (and their chest pools) are kept.
    """
    import json
    from gamedata.records import deserialize, RecordObject
    store = _store()
    path = store.records_dir / "InventoryItemData.jsonl"
    if not path.exists():
        return [record for record in store.load("InventoryItemData")
                if record.is_a("InventoryEquipmentData")]
    records = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if "InventoryEquipmentData" not in line:
                continue
            try:
                inner = json.loads(line)
                if isinstance(inner, str):
                    inner = json.loads(_TRAILING_COMMA.sub(r"\1", inner),
                                       strict=False)
                record = deserialize(inner)
            except (TypeError, ValueError):
                continue
            if isinstance(record, RecordObject) and record.is_a("InventoryEquipmentData"):
                records.append(record)
    return records


def _build_catalog():
    set_numbers, set_names = _set_numbers()
    items, pools = {}, {}
    for record in _equipment_records():
        guid = record.guid.lower()
        set_guid = _record_guid(record.field("m_SetId"))
        notes = str(record.field("m_DesignNotes") or "").strip()
        item = EquipmentItem(
            guid=guid,
            name=str(record.field("m_Name") or ""),
            rarity=str(record.field("m_Rarity") or ""),
            equipment_type=str(record.field("m_EquipmentType") or ""),
            set_guid=set_guid,
            design_notes=notes,
            description=str(record.field("m_Description") or ""),
            is_live=bool(int(record.field("m_IsLive") or 0)),
            chest_set_number=chest_note_set_number(
                notes, set_guid, set_numbers, set_names))
        items[guid] = item
        if item.is_live and item.chest_set_number:
            pools.setdefault(item.chest_set_number, {}).setdefault(
                item.rarity, []).append(guid)
    frozen_pools = {number: {rarity: tuple(sorted(guids))
                             for rarity, guids in by_rarity.items()}
                    for number, by_rarity in pools.items()}
    return items, frozen_pools, set_numbers


def _catalog():
    global _CATALOG
    catalog = _CATALOG
    if catalog is None:
        with _LOCK:
            if _CATALOG is None:
                _CATALOG = _build_catalog()
            catalog = _CATALOG
    return catalog


def equipment_items():
    """All equipment items by lowercase GUID."""
    return _catalog()[0]


def equipment_item(guid):
    return _catalog()[0].get(str(guid or "").lower())


def set_number(set_guid):
    """The booster set number of a card set (CardSetTemplate bundle name)."""
    return _catalog()[2].get(str(set_guid or "").lower())


def chest_loot_pool(set_guid):
    """Return {equipment rarity: [template guid, ...]} for a set's chests.

    Built from the design-note pools; empty for a set whose chests award no
    equipment.
    """
    number = set_number(set_guid)
    pools = _catalog()[1].get(number, {}) if number else {}
    return {rarity: list(guids) for rarity, guids in pools.items()}


# ── Deck equipment ────────────────────────────────────────────────────

def parse_request_equipment_ids(inner_bytes):
    """Return the EquipmentIDs GUIDs from a raw UpdateDeck request, or None.

    ``None`` means the request carried no EquipmentIDs field, so the saved
    equipment should be left untouched.
    """
    if not isinstance(inner_bytes, (bytes, bytearray)):
        return None
    start = inner_bytes.find(b"EquipmentIDs")
    if start < 0:
        return None
    end = inner_bytes.find(b"TalentIDs", start)
    section = inner_bytes[start:end if end > start else len(inner_bytes)]
    guids = []
    for raw in _GUID.findall(section):
        guid = raw.decode("ascii").lower()
        if guid != ZERO_GUID and guid not in guids:
            guids.append(guid)
    return guids


def sanitize_deck_equipment(db, user_id, equipment_guids):
    """Keep owned, known equipment with at most one item per equipment type."""
    from profile_db import db_inventory_item
    kept, used_types = [], set()
    for guid in equipment_guids or []:
        item = equipment_item(guid)
        if item is None or item.equipment_type in used_types:
            continue
        owned = db_inventory_item(user_id, item.guid, conn=db)
        if not owned or int(owned[1] or 0) <= 0:
            continue
        used_types.add(item.equipment_type)
        kept.append(item.guid)
    return kept


def equipment_slots(equipment_guids):
    """Return six GUIDs for deck_bits.equipment_1..6 (zero GUID when empty)."""
    slots = [ZERO_GUID] * len(EQUIPMENT_SLOT_ORDER)
    for guid in equipment_guids or []:
        item = equipment_item(guid)
        if item and item.equipment_type in EQUIPMENT_SLOT_ORDER:
            slots[EQUIPMENT_SLOT_ORDER.index(item.equipment_type)] = item.guid
    return slots


def _build_variants():
    variants = {}
    for record in _store().load("CardTemplate"):
        if not int(record.field("m_EquipmentModifiedCard") or 0):
            continue
        tac = record.field("m_SerializedTAC") or {}
        payload = tac.get("data") if isinstance(tac, dict) else None
        if not payload:
            continue
        try:
            guids = [g.decode().lower() for g in _GUID.findall(base64.b64decode(payload))]
        except (ValueError, TypeError):
            continue
        if len(guids) < 2:
            continue
        variants.setdefault(guids[0], set()).add(
            (frozenset(guids[1:]), record.guid.lower()))
    return {base: tuple(sorted(entries, key=lambda e: (sorted(e[0]), e[1])))
            for base, entries in variants.items()}


def card_variants(base_guid):
    """((equipment GUID set, variant card GUID), ...) for a base card."""
    global _VARIANTS
    variants = _VARIANTS
    if variants is None:
        with _LOCK:
            if _VARIANTS is None:
                _VARIANTS = _build_variants()
            variants = _VARIANTS
    return variants.get(str(base_guid or "").lower(), ())


def equipped_variant(base_guid, equipment_guids):
    """Return the equipment-modified card for ``base_guid``, or None.

    Picks the variant that covers the most of the deck's equipped items; a
    card with two equipped items and no combined variant still receives the
    single-item upgrade.
    """
    if not base_guid or not equipment_guids:
        return None
    equipped = {str(g).lower() for g in equipment_guids}
    best, best_size = None, 0
    for items, variant in card_variants(base_guid):
        if items <= equipped and len(items) > best_size:
            best, best_size = variant, len(items)
    return best
