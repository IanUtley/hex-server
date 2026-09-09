"""Authoritative AZ1 campaign-pack loot generation.

The normal booster path is a PvP set booster.  AZ1 campaign packs are a
different product: two common PvP cards from Sets 1-3, one weighted PvE card
from Sets 1-3, and two weighted equipment slots from Sets 1-3.  The slot
weights are explicit data so the generator does not grow card-name
conditionals.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Any, Iterable, Mapping

import json
from pathlib import Path

from gamedata.records import deserialize, reference_guid


AZ1_CAMPAIGN_PACK_GUID = "7b8390fd-7d3d-44d4-b285-1aeae3aef98b"
AZ2_CAMPAIGN_PACK_GUID = "d77471f5-7b32-4464-9552-c992a38fb4be"
SET1_PVP_GUID = "0382f729-7710-432b-b761-13677982dcd2"
SET2_PVP_GUID = "b05e69d2-299a-4eed-ac31-3f1b4fa36470"
SET3_PVP_GUID = "fce480eb-15f9-4096-8d12-6beee9118652"
PVP_COMMON_SET_GUIDS = (SET1_PVP_GUID, SET2_PVP_GUID, SET3_PVP_GUID)
SET4_PVP_GUID = "2d05262c-d7a0-408f-a280-36d206a29344"
SET5_PVP_GUID = "ecdbc188-5750-48ef-acac-05e2bcbcc46f"
SET6_PVP_GUID = "fbbac856-2264-4d31-97b0-0d8a646b9597"
AZ2_PVP_COMMON_SET_GUIDS = (SET4_PVP_GUID, SET5_PVP_GUID, SET6_PVP_GUID)

SET01_PVE_ARENA_GUID = "d8ee3b8d-d4b7-4997-bbb3-f00658dbf303"
SET01_PVE_HOLIDAY_GUID = "4f38be98-79e3-404c-ab6f-a68e99fede18"
SET01_PVE_TALENTS_GUID = "cd112780-7766-44e8-bf3b-4cd269d47e3e"
SET02_PVE_TALENTS_GUID = "52bc1da1-af3c-4df0-8afb-c999c9f6d645"
SET03_PVE_PROMO_GUID = "3cc27cc9-b3af-44c7-a5de-4126f78d96ed"
PVE_CARD_SET_GUIDS = (
    SET01_PVE_ARENA_GUID,
    SET01_PVE_HOLIDAY_GUID,
    SET01_PVE_TALENTS_GUID,
    SET02_PVE_TALENTS_GUID,
    SET03_PVE_PROMO_GUID,
)
SET04_PVE_PROMO_GUID = "e3217d24-bff4-4159-94bc-4653012a14cd"
SET05_PVE_PROMO_GUID = "57df329e-d186-4717-aa99-f2c82f0aee73"
SET06_PVE_PROMO_GUID = "e96bc76d-9b12-4f0f-90d7-83d48cd5191a"
AZ2_PVE_CARD_SET_GUIDS = (
    SET04_PVE_PROMO_GUID,
    SET05_PVE_PROMO_GUID,
    SET06_PVE_PROMO_GUID,
)
PVE_EQUIPMENT_SET_GUIDS = PVE_CARD_SET_GUIDS
SET1_TO_3_EQUIPMENT_SET_GUIDS = (
    *PVP_COMMON_SET_GUIDS,
    *PVE_EQUIPMENT_SET_GUIDS,
)
SET3_TO_6_EQUIPMENT_SET_GUIDS = (
    *AZ2_PVP_COMMON_SET_GUIDS,
    *AZ2_PVE_CARD_SET_GUIDS,
)

# Kept as a compatibility alias for callers that used the old AZ1-specific
# name.  AZ1 campaign packs now use PVE_CARD_SET_GUIDS instead.
AZ1_PVE_GUID = "c363c22e-1c03-43c0-a5d3-e3e8759120e7"

AZ1_CAMPAIGN_PACK_CONFIG = {
    "pvp_set_guids": PVP_COMMON_SET_GUIDS,
    "pve_set_guids": PVE_CARD_SET_GUIDS,
    "equipment_set_guids": SET1_TO_3_EQUIPMENT_SET_GUIDS,
}
AZ2_CAMPAIGN_PACK_CONFIG = {
    "pvp_set_guids": AZ2_PVP_COMMON_SET_GUIDS,
    "pve_set_guids": AZ2_PVE_CARD_SET_GUIDS,
    "equipment_set_guids": SET3_TO_6_EQUIPMENT_SET_GUIDS,
}
CAMPAIGN_PACK_CONFIGS = {
    AZ1_CAMPAIGN_PACK_GUID: AZ1_CAMPAIGN_PACK_CONFIG,
    AZ2_CAMPAIGN_PACK_GUID: AZ2_CAMPAIGN_PACK_CONFIG,
}

# Pack-level rarity distributions. Items are selected uniformly from the
# metadata-eligible pool within the selected rarity.
AZ1_PVE_RARITY_WEIGHTS: Mapping[str, float] = {
    "Uncommon": 60.0,
    "Rare": 35.0,
    "Legendary": 5.0,
}
AZ1_EQUIPMENT_SLOT_WEIGHTS: Mapping[str, float] = {
    "Common": 35.0,
    "Uncommon": 25.0,
    "Rare": 10.0,
    "Legendary": 5.0,
    "Stardust": 25.0,
}
STARDUST_REPLACEMENT_RATE = AZ1_EQUIPMENT_SLOT_WEIGHTS["Stardust"] / 100.0
AZ1_STARDUST_RARITIES = ("Common", "Uncommon", "Rare")

# No AZ1-specific Stardust rarity table was supplied.  Use the server's
# existing chest rarity weights until one is extracted from the original loot
# definition.  The caller can inject a different mapping for verification.
DEFAULT_STARDUST_RARITY_WEIGHTS: Mapping[str, float] = {
    "Common": 800.0,
    "Uncommon": 150.0,
    "Rare": 45.0,
}


@dataclass(frozen=True)
class AZ1PackReward:
    cards: tuple[tuple[str, str, int, int, int], ...]
    equipment_guids: tuple[str, ...]
    stardust_rarities: tuple[str, ...]


def _weighted_choice(entries: Iterable[tuple[Any, float]], rng) -> Any:
    entries = tuple(entries)
    total = sum(float(weight) for _, weight in entries)
    if total <= 0:
        raise ValueError("loot table has no positive weights")
    roll = rng.random() * total
    for value, weight in entries:
        roll -= float(weight)
        if roll < 0:
            return value
    return entries[-1][0]


def _all_pve_equipment_records(record_store) -> tuple[Any, ...]:
    """Load all PvE equipment records, including permissive JSON fallbacks."""
    cached = getattr(record_store, "_pve_equipment_records_cache", None)
    if cached is not None:
        return cached
    result = {}

    def add(record):
        if not record.is_a("InventoryEquipmentData"):
            return
        if not bool(record.field("m_IsPvE", 0)):
            return
        guid = str(getattr(record, "guid", "")).lower()
        if guid:
            result[guid] = record

    for record in record_store.load("InventoryItemData"):
        add(record)

    # A handful of InventoryItemData rows contain a literal newline inside a
    # JSON string.  RecordStore deliberately reports those rows as malformed;
    # Python's permissive JSON decoder can still recover the metadata needed
    # for the complete set-filtered equipment pool.
    path = Path(record_store.records_dir) / "InventoryItemData.jsonl"
    if path.exists():
        for line in path.open(encoding="utf-8", errors="replace"):
            try:
                outer = json.loads(line)
                raw = json.loads(outer, strict=False)
                record = deserialize(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            add(record)
    try:
        record_store._pve_equipment_records_cache = tuple(result.values())
    except AttributeError:
        pass
    return tuple(result.values())


def set_equipment_records(record_store,
                          set_guids=SET1_TO_3_EQUIPMENT_SET_GUIDS):
    """Return PvE equipment whose metadata belongs to the requested sets."""
    allowed = {str(guid).lower() for guid in set_guids}
    return tuple(
        record for record in _all_pve_equipment_records(record_store)
        if reference_guid(record.field("m_SetId")).lower() in allowed
    )


def _eligible_pve_cards(card_data, record_store,
                        set_guids=PVE_CARD_SET_GUIDS) -> dict[str, list[tuple]]:
    """Resolve metadata-eligible PvE cards from the requested set records."""
    set_guids = tuple(set_guids)
    cache_key = tuple(sorted(str(guid).lower() for guid in set_guids))
    cached = getattr(record_store, "_pve_card_pool_cache", {}).get(cache_key)
    if cached is not None:
        return {
            rarity: [card for card in cached.get(rarity, [])
                     if any(card[0].lower() == row[0].lower()
                            for set_guid in set_guids
                            for row in card_data.get(set_guid, ()))]
            for rarity in ("Uncommon", "Rare", "Legendary")
        }
    allowed_sets = set(cache_key)
    eligible_guids = set()
    for record in record_store.load("CardTemplate"):
        if reference_guid(record.field("m_SetId")).lower() not in allowed_sets:
            continue
        if not bool(record.field("m_IsPvE", 0)):
            continue
        if bool(record.field("m_IneligibleForPvERandomTemplates", 0)):
            continue
        if str(record.field("m_CardRarity", "")) not in {
                "Uncommon", "Rare", "Legendary"}:
            continue
        eligible_guids.add(record.guid.lower())

    by_rarity = {"Uncommon": [], "Rare": [], "Legendary": []}
    for set_guid in set_guids:
        for card in card_data.get(set_guid, ()):
            if card[0].lower() not in eligible_guids:
                continue
            if card[2] in by_rarity:
                by_rarity[card[2]].append(card)
    try:
        pools = getattr(record_store, "_pve_card_pool_cache", {})
        pools[cache_key] = {
            rarity: list(cards) for rarity, cards in by_rarity.items()
        }
        record_store._pve_card_pool_cache = pools
    except AttributeError:
        pass
    return by_rarity


def _eligible_az1_cards(card_data, record_store) -> dict[str, list[tuple]]:
    """Compatibility helper for the former AZ1-only card pool."""
    return _eligible_pve_cards(card_data, record_store, (AZ1_PVE_GUID,))


def generate_campaign_pack(card_data, record_store, rng=None, *,
                           pack_config=AZ1_CAMPAIGN_PACK_CONFIG,
                           stardust_rarity_weights=None) -> AZ1PackReward:
    """Generate one campaign pack from its metadata-defined set pools."""
    rng = rng or random
    pvp_set_guids = pack_config["pvp_set_guids"]
    pve_set_guids = pack_config["pve_set_guids"]
    equipment_set_guids = pack_config["equipment_set_guids"]
    pvp_commons = [card
                   for set_guid in pvp_set_guids
                   for card in card_data.get(set_guid, ())
                   if card[2] == "Common" and not card[6] and not card[7]]
    if len(pvp_commons) < 2:
        raise ValueError("campaign PvP common pool has fewer than two cards")
    selected = rng.sample(pvp_commons, 2)

    pve_by_rarity = _eligible_pve_cards(card_data, record_store, pve_set_guids)
    pve_rarity = _weighted_choice(AZ1_PVE_RARITY_WEIGHTS.items(), rng)
    if not pve_by_rarity[pve_rarity]:
        raise ValueError("campaign PvE metadata has no eligible "
                         + pve_rarity + " cards")
    # All metadata-eligible cards in the selected rarity share that rarity's
    # probability across the complete configured PvE set pool.
    pve_card = rng.choice(pve_by_rarity[pve_rarity])
    selected.append(pve_card)

    equipment_by_rarity = {}
    for record in set_equipment_records(record_store, equipment_set_guids):
        rarity = str(record.field("m_Rarity", ""))
        if rarity in {"Common", "Uncommon", "Rare", "Legendary"}:
            equipment_by_rarity.setdefault(rarity, []).append((record, 1.0))
    equipment_guids = []
    stardust_rarities = []
    dust_weights = {
        rarity: weight
        for rarity, weight in (
            stardust_rarity_weights or DEFAULT_STARDUST_RARITY_WEIGHTS
        ).items()
        if rarity in AZ1_STARDUST_RARITIES
    }
    for _ in range(2):
        slot_type = _weighted_choice(AZ1_EQUIPMENT_SLOT_WEIGHTS.items(), rng)
        if slot_type == "Stardust":
            rarity = _weighted_choice(dust_weights.items(), rng)
            stardust_rarities.append(rarity)
        else:
            choices = equipment_by_rarity.get(slot_type, ())
            if not choices:
                raise ValueError("campaign equipment metadata has no eligible "
                                 + slot_type + " equipment")
            choice = _weighted_choice(choices, rng)
            equipment_guids.append(str(choice.guid))

    cards = tuple((card[0], card[1], card[3], card[4], card[5])
                  for card in selected)
    return AZ1PackReward(cards, tuple(equipment_guids),
                         tuple(stardust_rarities))


def generate_az1_pack(card_data, record_store, rng=None,
                      *, stardust_rarity_weights=None) -> AZ1PackReward:
    """Compatibility wrapper for an AZ1 Set 1-3 campaign pack."""
    return generate_campaign_pack(
        card_data, record_store, rng,
        pack_config=AZ1_CAMPAIGN_PACK_CONFIG,
        stardust_rarity_weights=stardust_rarity_weights,
    )


def generate_az2_pack(card_data, record_store, rng=None,
                      *, stardust_rarity_weights=None) -> AZ1PackReward:
    """Generate an AZ2/Alachian Sea Set 4-6 campaign pack."""
    return generate_campaign_pack(
        card_data, record_store, rng,
        pack_config=AZ2_CAMPAIGN_PACK_CONFIG,
        stardust_rarity_weights=stardust_rarity_weights,
    )


def az1_probability_summary() -> dict[str, dict[str, float]]:
    """Return the configured normalized slot probabilities for audit/tests."""
    return {
        "pve_card": {
            rarity: weight / 100.0
            for rarity, weight in AZ1_PVE_RARITY_WEIGHTS.items()
        },
        "equipment_slot": {
            slot_type: weight / 100.0
            for slot_type, weight in AZ1_EQUIPMENT_SLOT_WEIGHTS.items()
        },
    }
