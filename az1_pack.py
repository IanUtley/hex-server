"""Authoritative AZ1 campaign-pack loot generation.

The normal booster path is a PvP set booster.  AZ1 campaign packs are a
different product: two common PvP cards from Sets 1-3, a third card slot that
is a weighted PvE card 50% of the time (otherwise a PvP Uncommon from Sets
1-3), and two weighted equipment slots from Sets 1-3.  The slot weights and
the PvE chance are explicit data so the generator does not grow card-name
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

# AZ1 campaign-only card set (includes the Tranquil Dream cards).
AZ1_CARD_SET_GUID = "c363c22e-1c03-43c0-a5d3-e3e8759120e7"
# AZ1 equipment comes from Sets 1-3 and the AZ1 set only.
AZ1_EQUIPMENT_SET_GUIDS = (*PVP_COMMON_SET_GUIDS, AZ1_CARD_SET_GUID)
# Equipment that the Tranquil Dream pack hands out explicitly; AZ1 never draws it.
TRANQUIL_DREAM_EQUIPMENT_GUIDS = (
    "d92baafa-7f33-4fa3-8bab-d21954b52721",  # Bear Idol (Rare)
    "24c1fce6-5703-4c2a-a67e-6702a4c231e3",  # Chitinous Gloves (Rare)
    "c5226cd4-fd83-481f-a7e1-5b665c0e5136",  # Dream Cap (Uncommon)
    "fcb83d01-b06e-4f7f-af2c-6dd430b672f3",  # Dream Galoshes (Uncommon)
    "47252359-5e8d-4421-820d-8265f370a8b9",  # Dream Staff (Legendary)
    "b7b7ce27-2f01-4c75-bb94-3f76537ab928",  # Eagle Feathered Gloves (Uncommon)
    "3a2a8076-de2d-4b9e-aa5a-4b1b4c9db153",  # Turtle Shell Bulwark (Uncommon)
)

# Dream PvE cards handed out explicitly by the Tranquil Dream pack; AZ1 never
# draws them.
TRANQUIL_DREAM_PVE_CARD_GUIDS = (
    "b62a1583-fe80-4580-9592-d3c3c32acf39",  # Dream Bear (Uncommon)
    "e2be4847-c1b4-4aa9-87f6-14b9728d12e6",  # Dream Eagle (Uncommon)
    "b38def9f-1532-4141-8bff-877827584396",  # Dream Turtle (Uncommon)
    "7cf4854c-1bf4-4d49-a0e3-aff9d048d2f4",  # Dream Skarn (Rare)
    "c71ec563-6b44-4328-b0a4-ad63c96f5d29",  # Dream Spider (Rare)
    "84a7cebf-0df8-44bf-bc77-6bc3a420c382",  # Enter the Dream (Legendary)
)

# Chance that the third card slot is the weighted PvE card.  When it is not,
# the slot becomes a PvP Uncommon from the pack's own PvP set range.
AZ1_CAMPAIGN_PACK_CONFIG = {
    # Campaign packs award no Stardust from their equipment slots.
    "allow_stardust": False,
    "pvp_set_guids": PVP_COMMON_SET_GUIDS,
    "pve_set_guids": (AZ1_CARD_SET_GUID,),
    "pve_exclude_guids": TRANQUIL_DREAM_PVE_CARD_GUIDS,
    "equipment_set_guids": AZ1_EQUIPMENT_SET_GUIDS,
    "equipment_exclude_guids": TRANQUIL_DREAM_EQUIPMENT_GUIDS,
    "pve_slot_chance": 0.5,
}
AZ2_CAMPAIGN_PACK_CONFIG = {
    # Campaign packs award no Stardust from their equipment slots.
    "allow_stardust": False,
    "pvp_set_guids": AZ2_PVP_COMMON_SET_GUIDS,
    "pve_set_guids": AZ2_PVE_CARD_SET_GUIDS,
    "equipment_set_guids": SET3_TO_6_EQUIPMENT_SET_GUIDS,
    "pve_slot_chance": 0.5,
}
# Tranquil Dream Pack (InventoryTreasureChest).  Two PvP commons from Sets
# 1-3 and a PvE card from an explicit list, rarity-weighted like AZ1.  Half
# the time one equipment slot is drawn from an explicit list instead.
TRANQUIL_DREAM_PACK_GUID = "88d29ef0-5ec7-49b1-bcc5-8f70c5c4cda5"
TRANQUIL_DREAM_PACK_CONFIG = {
    # Dungeon packs award no Stardust from their equipment slots.
    "allow_stardust": False,
    "pvp_set_guids": PVP_COMMON_SET_GUIDS,
    "pve_set_guids": (),
    "pve_card_guids": TRANQUIL_DREAM_PVE_CARD_GUIDS,
    "equipment_set_guids": SET1_TO_3_EQUIPMENT_SET_GUIDS,
    "pve_slot_chance": 0.5,
    "pve_fallback_equipment_guids": TRANQUIL_DREAM_EQUIPMENT_GUIDS,
}

# Fort Romor Pack (InventoryTreasureChest).  Same as the AZ1 pack, except the
# PvE card comes from an explicit list of six AZ1-set cards.
FORT_ROMOR_PACK_GUID = "faea143f-7193-4ac1-bbb2-f79469d8ad9f"
FORT_ROMOR_PVE_CARD_GUIDS = (
    "147a8268-489e-4c40-80e0-137309e7c6bb",  # Conquest of Romor (Rare)
    "1a5212e2-6153-427a-8cd2-f77c7d60f7ef",  # Grim Summons (Uncommon)
    "93b8f129-d68b-4986-b4ef-4685c2c39d9b",  # Hellshot Catapult (Rare)
    "1903e6db-d537-428b-a150-97869b90de38",  # Infiltrate (Uncommon)
    "7a21f883-26fb-43a5-a72a-88c94cac5f0e",  # Romor's Reckoning (Legendary)
    "d36653ff-6e3c-466b-ab04-cae667292d20",  # Underworld Recruiter (Uncommon)
)
# Equipment named by the equipment-modified variants of the cards above.
FORT_ROMOR_EQUIPMENT_GUIDS = (
    "25eed7b2-f8e6-4dd3-9233-95b74b3f0579",  # Hideous Hood (Rare)
    "58358fbc-4d0b-4f65-920e-8876ddc4356a",  # Gloves of Conquest (Uncommon)
    "d6f00bc4-592c-42e5-9c92-207c9e885a97",  # Hasty Summoners (Uncommon)
    "3ddb4b87-c0d1-430e-b6af-b1b414f05ff2",  # Hellshot Vest (Uncommon)
    "922ff504-be0b-4fbf-831c-06bcbfb6ff24",  # Prickly Ammo (Rare)
    "c9261ee1-2f6e-4b28-8c1a-dc031e9d53f5",  # The Infiltrator (Uncommon)
    "013ebf95-da88-4cfd-b87c-20446a086cab",  # Hell Horror Helmet (Legendary)
    "c6e41452-867d-4e9a-ada9-4442a8ab4ef9",  # Ruin Rod of Romor (Uncommon)
    "700618d1-59a4-4eda-a879-0c015e4a5e21",  # Recruiter's Ring (Rare)
)
FORT_ROMOR_PACK_CONFIG = {
    # Dungeon packs award no Stardust from their equipment slots.
    "allow_stardust": False,
    **AZ1_CAMPAIGN_PACK_CONFIG,
    "pve_card_guids": FORT_ROMOR_PVE_CARD_GUIDS,
    "pve_fallback_equipment_guids": FORT_ROMOR_EQUIPMENT_GUIDS,
}

# The Usurper Pack (InventoryTreasureChest).  Same shape as the Fort Romor
# pack, with its own six PvE cards and the equipment their variants name.
USURPER_PACK_GUID = "eda1cb9f-9900-4218-a8d1-6564573a9cf2"
USURPER_PVE_CARD_GUIDS = (
    "973527b0-a935-48e2-b0c4-3c12915a38a1",  # Ardent Recruiter (Uncommon)
    "c860c489-9262-4fba-bda6-4ca0d1bb0218",  # Elusive Specter (Uncommon)
    "24a0c086-65b9-44d5-9173-f67c0e238fd9",  # King Lodegan's Call (Legendary)
    "4723a3b1-2102-4134-9ce8-d91d7d6f2c3c",  # King Lodegan's Verdict (Rare)
    "23398082-0512-4953-8dba-37dfe6add4a9",  # Phantasmal Slayer (Uncommon)
    "26024ed7-78a9-49c7-bf26-7e5afbc78029",  # Purging Flames (Rare)
)
USURPER_EQUIPMENT_GUIDS = (
    "b65bac39-90f1-49d5-afea-1e535c9d9f64",  # Ardent Chestplate (Rare)
    "a1473147-29d3-49da-a33d-0de3ecdd4b40",  # Ghostly Waders (Uncommon)
    "219c12e5-4138-4d1b-a4c7-4fe19b65aee4",  # Spiritcaller Breastplate (Legendary)
    "876429d8-6592-4fbe-a7cb-9263832a801a",  # Royal Helm (Uncommon)
    "ab05b247-9b62-4db7-bbd5-fc553abb3a5d",  # Royal Cuirass (Uncommon)
    "d4208be6-9e55-49a1-a428-775f6b8478df",  # Lodegan's Ring (Rare)
    "d7b6a844-4a0c-48d7-b3d2-309ab9113cac",  # Mist Shroud (Uncommon)
    "48710904-6dd0-40dd-ad52-491dae07e4d0",  # Purple Pointed Purgers (Rare)
    "b9fb5d95-f339-4a16-a133-d517cb1e074e",  # Fresh Fire Stoker (Uncommon)
)
USURPER_PACK_CONFIG = {
    # Dungeon packs award no Stardust from their equipment slots.
    "allow_stardust": False,
    **AZ1_CAMPAIGN_PACK_CONFIG,
    "pve_card_guids": USURPER_PVE_CARD_GUIDS,
    "pve_fallback_equipment_guids": USURPER_EQUIPMENT_GUIDS,
}

# The Smoldering Dead Pack (InventoryTreasureChest).  Always contains one of
# its six PvE cards; the rest of the pack is the AZ1 shape.
SMOLDERING_DEAD_PACK_GUID = "56e231a3-f752-4e87-b04a-51c508c87c2d"
SMOLDERING_DEAD_PVE_CARD_GUIDS = (
    "53ee2141-c755-4403-bd22-675671bc81c0",  # Ancestral Ire (Uncommon)
    "d915993e-b9ee-4718-8173-a97048c4c2d7",  # Decaying Giant (Uncommon)
    "5b21250d-5c6f-4003-8d4b-d3dad65948d9",  # Inferno Goliath (Rare)
    "834cdaa6-bd0e-4d11-87a8-0adb578c2245",  # Lost Wanderer (Uncommon)
    "49a2b136-6ff9-4d2e-9a0d-ea8bfc566410",  # Moldmass (Rare)
    "61f0ff5d-d2c6-4ccb-822a-e4574b4da29f",  # Soul of Raqqar (Legendary)
)
# Equipment named by the equipment-modified variants of the cards above.
SMOLDERING_DEAD_EQUIPMENT_GUIDS = (
    "fc955842-ca69-4e5f-a888-46721f310915",  # Ancestral Puzzle (Uncommon)
    "4e464580-6114-47d2-b04e-4496d4c0b0c1",  # Decaying Hand (Rare)
    "c53f166c-e075-46e8-a4d0-c86337341144",  # Goliath's Gear (Rare)
    "f0e3b20b-127c-487b-86a3-73bea9144907",  # Inferno Bracer (Uncommon)
    "5bb8260f-9c55-4521-9036-7d1efca656aa",  # Nomad's Vest (Uncommon)
    "155c63ed-b833-476d-a285-228ce2012a4e",  # Wreath of Thorns (Uncommon)
    "e2ad3ab9-d2ab-4fdb-b5bc-5e7442af3189",  # Nature's Protection (Rare)
    "6e7ecfc4-a325-49f7-b59e-2355d5d57649",  # Soul Touch Tachi (Legendary)
    "73637229-8e24-49cc-a378-3468f973e84e",  # Ghostly Galoshes (Uncommon)
)
SMOLDERING_DEAD_PACK_CONFIG = {
    # Dungeon packs award no Stardust from their equipment slots.
    "allow_stardust": False,
    **AZ1_CAMPAIGN_PACK_CONFIG,
    "pve_slot_chance": 1.0,
    "pve_card_guids": SMOLDERING_DEAD_PVE_CARD_GUIDS,
    "equipment_pool_guids": SMOLDERING_DEAD_EQUIPMENT_GUIDS,
    "equipment_pool_chance": 0.5,
}

# Devonshire Keep Pack (InventoryTreasureChest).  Same shape as the Usurper
# pack: a PvE card 50% of the time, otherwise one of the equipment its
# variants name.
DEVONSHIRE_KEEP_PACK_GUID = "1d54da38-e03e-449c-aa2d-c9a7c0ad2cb1"
DEVONSHIRE_KEEP_PVE_CARD_GUIDS = (
    "360c945e-4ad3-4172-9683-32b839894959",  # Blight Steed (Uncommon)
    "dba5c450-f5bc-4c4c-b920-ec555de1fff2",  # Bulwark of Faith (Rare)
    "c67c7b38-2744-47a6-999e-92f5cf01c6ff",  # Burning Zombie (Uncommon)
    "1b13e7c5-ffa1-4f23-8787-2892bda2ccac",  # Lady Devonshire (Legendary)
    "dd47d876-0b59-47e1-8bd9-56029a77f428",  # Sealed Tomb (Uncommon)
    "53f09083-2c44-4790-8db6-5fe617de036c",  # Sporeshambler (Rare)
)
DEVONSHIRE_KEEP_EQUIPMENT_GUIDS = (
    "bf57f57e-bed9-401d-b33d-9e9ee0831b93",  # Steed Whip (Uncommon)
    "525b9c55-8390-4611-98f7-0fe65eecb19e",  # Faithful Gauntlets (Rare)
    "5f26c1f6-b3ba-470a-b77b-37d2908d11f5",  # Faith's Shield (Uncommon)
    "69d33978-b670-487e-8d7b-5c17725b641c",  # Burning Armband (Uncommon)
    "50638655-46e2-44fe-a366-c0d48a913b87",  # Ghostly Wrap (Uncommon)
    "c849e8c1-be46-45dd-9d72-cf5582f98688",  # Spiritual Slippers (Legendary)
    "9593f2cb-2cbb-43f7-8054-8d5a458c3719",  # Tombskin Hands (Rare)
    "73637229-8e24-49cc-a378-3468f973e84e",  # Ghostly Galoshes (Uncommon)
    "1b009a82-14ca-49ca-8a3a-add49017cad6",  # Sewn Sigil Gloves (Uncommon)
    "45acb4c1-674f-41cc-8650-1629560d96fb",  # Spore Shine Shoots (Rare)
)
DEVONSHIRE_KEEP_PACK_CONFIG = {
    # Dungeon packs award no Stardust from their equipment slots.
    "allow_stardust": False,
    **AZ1_CAMPAIGN_PACK_CONFIG,
    "pve_card_guids": DEVONSHIRE_KEEP_PVE_CARD_GUIDS,
    "pve_fallback_equipment_guids": DEVONSHIRE_KEEP_EQUIPMENT_GUIDS,
}

CAMPAIGN_PACK_CONFIGS = {
    AZ1_CAMPAIGN_PACK_GUID: AZ1_CAMPAIGN_PACK_CONFIG,
    AZ2_CAMPAIGN_PACK_GUID: AZ2_CAMPAIGN_PACK_CONFIG,
    TRANQUIL_DREAM_PACK_GUID: TRANQUIL_DREAM_PACK_CONFIG,
    FORT_ROMOR_PACK_GUID: FORT_ROMOR_PACK_CONFIG,
    USURPER_PACK_GUID: USURPER_PACK_CONFIG,
    SMOLDERING_DEAD_PACK_GUID: SMOLDERING_DEAD_PACK_CONFIG,
    DEVONSHIRE_KEEP_PACK_GUID: DEVONSHIRE_KEEP_PACK_CONFIG,
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


def _rarity_weighted_explicit_equipment(record_store, equipment_guids,
                                        rng) -> str:
    """Pick one listed equipment item: rarity by the AZ1 PvE split, then uniform."""
    wanted = {str(guid).lower() for guid in equipment_guids}
    by_rarity: dict[str, list[str]] = {}
    for record in _all_pve_equipment_records(record_store):
        if str(record.guid).lower() in wanted:
            rarity = str(record.field("m_Rarity", ""))
            by_rarity.setdefault(rarity, []).append(str(record.guid))
    rarities = [(rarity, weight)
                for rarity, weight in AZ1_PVE_RARITY_WEIGHTS.items()
                if by_rarity.get(rarity)]
    if not rarities:
        raise ValueError("explicit equipment list has no eligible items")
    rarity = _weighted_choice(rarities, rng)
    return rng.choice(by_rarity[rarity])


def _explicit_pve_cards(card_data, card_guids) -> dict[str, list[tuple]]:
    """Group the listed PvE card templates by rarity (Uncommon/Rare/Legendary)."""
    wanted = {str(guid).lower() for guid in card_guids}
    by_rarity = {"Uncommon": [], "Rare": [], "Legendary": []}
    for cards in card_data.values():
        for card in cards:
            if card[0].lower() in wanted and card[2] in by_rarity:
                by_rarity[card[2]].append(card)
    return by_rarity


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

    fallback_equipment = None
    if rng.random() < pack_config["pve_slot_chance"]:
        if pack_config.get("pve_card_guids"):
            pve_by_rarity = _explicit_pve_cards(
                card_data, pack_config["pve_card_guids"])
        else:
            pve_by_rarity = _eligible_pve_cards(
                card_data, record_store, pve_set_guids)
        pve_excluded = {str(guid).lower()
                        for guid in pack_config.get("pve_exclude_guids", ())}
        pve_by_rarity = {
            rarity: [card for card in cards
                     if card[0].lower() not in pve_excluded]
            for rarity, cards in pve_by_rarity.items()
        }
        pve_rarity = _weighted_choice(AZ1_PVE_RARITY_WEIGHTS.items(), rng)
        if not pve_by_rarity[pve_rarity]:
            raise ValueError("campaign PvE metadata has no eligible "
                             + pve_rarity + " cards")
        # All metadata-eligible cards in the selected rarity share that
        # rarity's probability across the complete configured PvE set pool.
        selected.append(rng.choice(pve_by_rarity[pve_rarity]))
    elif pack_config.get("pve_fallback_equipment_guids"):
        # Otherwise the slot becomes one of the pack's explicit equipment.
        fallback_equipment = _rarity_weighted_explicit_equipment(
            record_store, pack_config["pve_fallback_equipment_guids"], rng)
    else:
        # Otherwise the slot is a PvP Uncommon from the same set range.
        pvp_uncommons = [card
                         for set_guid in pvp_set_guids
                         for card in card_data.get(set_guid, ())
                         if card[2] == "Uncommon" and not card[6] and not card[7]]
        if not pvp_uncommons:
            raise ValueError("campaign PvP uncommon pool is empty")
        selected.append(rng.choice(pvp_uncommons))

    excluded = {str(guid).lower()
                for guid in pack_config.get("equipment_exclude_guids", ())}
    equipment_by_rarity = {}
    for record in set_equipment_records(record_store, equipment_set_guids):
        if str(record.guid).lower() in excluded:
            continue
        rarity = str(record.field("m_Rarity", ""))
        if rarity in {"Common", "Uncommon", "Rare", "Legendary"}:
            equipment_by_rarity.setdefault(rarity, []).append((record, 1.0))
    equipment_guids = [fallback_equipment] if fallback_equipment else []
    stardust_rarities = []
    dust_weights = {
        rarity: weight
        for rarity, weight in (
            stardust_rarity_weights or DEFAULT_STARDUST_RARITY_WEIGHTS
        ).items()
        if rarity in AZ1_STARDUST_RARITIES
    }
    # Some packs draw their first equipment slot from an explicit list instead
    # of the weighted rarity table (no Stardust on that slot).
    explicit_pool = pack_config.get("equipment_pool_guids")
    explicit_slot = bool(explicit_pool) and (
        rng.random() < pack_config["equipment_pool_chance"])
    if explicit_slot:
        equipment_guids.append(_rarity_weighted_explicit_equipment(
            record_store, explicit_pool, rng))
    slot_weights = {
        slot: weight for slot, weight in AZ1_EQUIPMENT_SLOT_WEIGHTS.items()
        if slot != "Stardust" or pack_config.get("allow_stardust", True)}
    for _ in range(1 if explicit_slot else 2):
        slot_type = _weighted_choice(slot_weights.items(), rng)
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


# Frost Ring Arena chest: one PvE card and one equipment item, both drawn from
# Set01_PvE_Arena with the AZ1 rarity splits.  Stardust is not part of the
# arena chest, so the equipment rarity weights leave it out.
ARENA_CHEST_SET_GUID = SET01_PVE_ARENA_GUID
ARENA_CHEST_EQUIPMENT_WEIGHTS: Mapping[str, float] = {
    rarity: weight for rarity, weight in AZ1_EQUIPMENT_SLOT_WEIGHTS.items()
    if rarity != "Stardust"
}


@dataclass(frozen=True)
class ArenaChestReward:
    card: tuple[str, str, int, int, int]
    equipment_guid: str


def generate_arena_chest(card_data, record_store, rng=None) -> ArenaChestReward:
    """Roll one Frost Ring Arena chest from Set01_PvE_Arena."""
    rng = rng or random
    pve_by_rarity = _eligible_pve_cards(card_data, record_store,
                                        (ARENA_CHEST_SET_GUID,))
    pve_rarity = _weighted_choice(AZ1_PVE_RARITY_WEIGHTS.items(), rng)
    if not pve_by_rarity[pve_rarity]:
        raise ValueError("arena chest PvE metadata has no eligible "
                         + pve_rarity + " cards")
    card = rng.choice(pve_by_rarity[pve_rarity])

    equipment_by_rarity: dict[str, list] = {}
    for record in set_equipment_records(record_store, (ARENA_CHEST_SET_GUID,)):
        rarity = str(record.field("m_Rarity", ""))
        if rarity in ARENA_CHEST_EQUIPMENT_WEIGHTS:
            equipment_by_rarity.setdefault(rarity, []).append(record)
    rarity = _weighted_choice(
        [(r, w) for r, w in ARENA_CHEST_EQUIPMENT_WEIGHTS.items()
         if equipment_by_rarity.get(r)], rng)
    equipment = rng.choice(equipment_by_rarity[rarity])
    return ArenaChestReward(
        (card[0], card[1], card[3], card[4], card[5]), str(equipment.guid))
