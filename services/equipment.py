"""Deck equipment: persistence helpers and equipment-modified card lookup.

A PvE deck equips at most one item per equipment type.  Each item upgrades
specific cards; the client data ships the upgraded card for every
(base card, equipped items) combination, which ``gamedata_seed`` extracts into
``equipment_card_variants``.
"""

import re

ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# deck_bits.equipment_1..6 follow Reckoning.Game.EEquipmentType order.
EQUIPMENT_SLOT_ORDER = ("Head", "Chest", "Gloves", "Feet", "Weapon", "Trinket")

_GUID = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


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
    kept, used_types = [], set()
    for guid in equipment_guids or []:
        row = db.execute(
            "SELECT e.equipment_type FROM equipment_templates e "
            "JOIN player_inventory p ON p.template_guid=e.guid "
            "WHERE e.guid=? AND p.user_id=? AND p.quantity>0 LIMIT 1",
            (guid, user_id)).fetchone()
        if not row or row[0] in used_types:
            continue
        used_types.add(row[0])
        kept.append(guid)
    return kept


def equipment_slots(db, equipment_guids):
    """Return six GUIDs for deck_bits.equipment_1..6 (zero GUID when empty)."""
    slots = [ZERO_GUID] * len(EQUIPMENT_SLOT_ORDER)
    for guid in equipment_guids or []:
        row = db.execute(
            "SELECT equipment_type FROM equipment_templates WHERE guid=?",
            (guid,)).fetchone()
        if row and row[0] in EQUIPMENT_SLOT_ORDER:
            slots[EQUIPMENT_SLOT_ORDER.index(row[0])] = guid
    return slots


def equipped_variant(db, base_guid, equipment_guids):
    """Return the equipment-modified card for ``base_guid``, or None.

    Picks the variant that covers the most of the deck's equipped items; a
    card with two equipped items and no combined variant still receives the
    single-item upgrade.
    """
    if not base_guid or not equipment_guids:
        return None
    equipped = {str(g).lower() for g in equipment_guids}
    best, best_size = None, 0
    for key, variant in db.execute(
            "SELECT equipment_key, variant_guid FROM equipment_card_variants "
            "WHERE base_guid=?", (str(base_guid).lower(),)).fetchall():
        items = set(key.split(","))
        if items <= equipped and len(items) > best_size:
            best, best_size = variant, len(items)
    return best
