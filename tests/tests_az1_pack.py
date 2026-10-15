"""Regression tests for the Adventure Zone campaign-pack generators."""

import os
import sys
import random
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

# Bind this process's test database before any runtime import
# opens ``db``; the live ``hconnect.db`` is never opened.
SRC = fresh_database()

from az1_pack import (
    AZ1_CARD_SET_GUID,
    AZ1_EQUIPMENT_SET_GUIDS,
    AZ1_EQUIPMENT_SLOT_WEIGHTS,
    AZ1_PVE_RARITY_WEIGHTS,
    AZ1_STARDUST_RARITIES,
    AZ2_PVE_CARD_SET_GUIDS,
    AZ2_PVP_COMMON_SET_GUIDS,
    PVE_CARD_SET_GUIDS,
    PVP_COMMON_SET_GUIDS,
    SET1_TO_3_EQUIPMENT_SET_GUIDS,
    SET3_TO_6_EQUIPMENT_SET_GUIDS,
    DEFAULT_STARDUST_RARITY_WEIGHTS,
    _eligible_pve_cards,
    az1_probability_summary,
    TRANQUIL_DREAM_EQUIPMENT_GUIDS,
    TRANQUIL_DREAM_PVE_CARD_GUIDS,
    TRANQUIL_DREAM_PACK_CONFIG,
    TRANQUIL_DREAM_PVE_CARD_GUIDS,
    generate_az2_pack,
    generate_az1_pack,
    FORT_ROMOR_PACK_CONFIG,
    FORT_ROMOR_EQUIPMENT_GUIDS,
    FORT_ROMOR_PVE_CARD_GUIDS,
    AZ1_CAMPAIGN_PACK_CONFIG,
    AZ2_CAMPAIGN_PACK_CONFIG,
    USURPER_PACK_CONFIG,
    SMOLDERING_DEAD_PACK_CONFIG,
    DEVONSHIRE_KEEP_PACK_CONFIG,
    generate_arena_chest,
    generate_campaign_pack,
    ARENA_CHEST_SET_GUID,
    set_equipment_records,
)
from gamedata import DEFAULT_RECORD_STORE


def card_data_from_db():
    # The seeded card catalogue comes from this process's own test database:
    # ``tests/run_all.py`` shares one snapshot across the run, direct runs
    # build a fresh one.  Never the developer's live ``hconnect.db``.
    con = sqlite3.connect(fresh_database())
    try:
        data = {}
        for row in con.execute(
                "SELECT guid,set_guid,name,rarity,cost,attack,defense,"
                "is_pve,no_pvp,card_type FROM card_templates"):
            guid, set_guid, name, rarity, cost, attack, defense, is_pve, no_pvp, card_type = row
            data.setdefault(set_guid, []).append(
                (guid, name, rarity, cost, attack, defense,
                 is_pve, no_pvp, card_type))
        return data
    finally:
        con.close()


def run(name, fn):
    try:
        fn()
        print(f"PASS {name}")
    except AssertionError as exc:
        print(f"FAIL {name}: {exc}")
    except Exception as exc:
        import traceback
        print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()


def test_table_weight_totals_and_rarity_summary():
    summary = az1_probability_summary()
    assert summary["pve_card"] == {
        rarity: weight / 100.0
        for rarity, weight in AZ1_PVE_RARITY_WEIGHTS.items()
    }
    assert summary["equipment_slot"] == {
        slot_type: weight / 100.0
        for slot_type, weight in AZ1_EQUIPMENT_SLOT_WEIGHTS.items()
    }


def test_metadata_eligible_cards_and_equipment_resolve():
    card_data = card_data_from_db()
    cards = _eligible_pve_cards(card_data, DEFAULT_RECORD_STORE,
                                PVE_CARD_SET_GUIDS)
    assert sum(len(pool) for pool in cards.values()) > 0
    assert set_equipment_records(
        DEFAULT_RECORD_STORE, SET1_TO_3_EQUIPMENT_SET_GUIDS)


def test_pack_has_set1_to_3_cards_and_equipment():
    card_data = card_data_from_db()
    reward = generate_az1_pack(
        card_data, DEFAULT_RECORD_STORE,
        random.Random(17), stardust_rarity_weights=DEFAULT_STARDUST_RARITY_WEIGHTS)
    assert len(reward.cards) == 3
    pvp_pool = {
        card[0]
        for set_guid in PVP_COMMON_SET_GUIDS
        for card in card_data.get(set_guid, ())
        if card[2] == "Common" and not card[6] and not card[7]
    }
    assert all(card[0] in pvp_pool for card in reward.cards[:2])
    eligible = {
        card[0] for pool in _eligible_pve_cards(
            card_data, DEFAULT_RECORD_STORE, (AZ1_CARD_SET_GUID,)).values()
        for card in pool
    } - set(TRANQUIL_DREAM_PVE_CARD_GUIDS)
    pvp_uncommon_pool = {
        card[0]
        for set_guid in PVP_COMMON_SET_GUIDS
        for card in card_data.get(set_guid, ())
        if card[2] == "Uncommon" and not card[6] and not card[7]
    }
    assert reward.cards[2][0] in eligible | pvp_uncommon_pool
    assert len(reward.equipment_guids) == 2
    assert not reward.stardust_rarities
    equipment_pool = {
        str(record.guid) for record in set_equipment_records(
            DEFAULT_RECORD_STORE, AZ1_EQUIPMENT_SET_GUIDS)
    } - set(TRANQUIL_DREAM_EQUIPMENT_GUIDS)
    assert set(reward.equipment_guids) <= equipment_pool


def test_az2_pack_has_set4_to_6_cards_and_equipment():
    card_data = card_data_from_db()
    reward = generate_az2_pack(
        card_data, DEFAULT_RECORD_STORE,
        random.Random(23), stardust_rarity_weights=DEFAULT_STARDUST_RARITY_WEIGHTS)
    pvp_pool = {
        card[0]
        for set_guid in AZ2_PVP_COMMON_SET_GUIDS
        for card in card_data.get(set_guid, ())
        if card[2] == "Common" and not card[6] and not card[7]
    }
    assert all(card[0] in pvp_pool for card in reward.cards[:2])
    eligible = {
        card[0] for pool in _eligible_pve_cards(
            card_data, DEFAULT_RECORD_STORE, AZ2_PVE_CARD_SET_GUIDS).values()
        for card in pool
    }
    pvp_uncommon_pool = {
        card[0]
        for set_guid in AZ2_PVP_COMMON_SET_GUIDS
        for card in card_data.get(set_guid, ())
        if card[2] == "Uncommon" and not card[6] and not card[7]
    }
    assert reward.cards[2][0] in eligible | pvp_uncommon_pool
    equipment_pool = {
        str(record.guid) for record in set_equipment_records(
            DEFAULT_RECORD_STORE, SET3_TO_6_EQUIPMENT_SET_GUIDS)
    }
    assert set(reward.equipment_guids) <= equipment_pool
    assert len(reward.equipment_guids) == 2
    assert not reward.stardust_rarities


def test_tranquil_dream_pack_slots():
    card_data = card_data_from_db()
    reward = generate_campaign_pack(
        card_data, DEFAULT_RECORD_STORE, random.Random(31),
        pack_config=TRANQUIL_DREAM_PACK_CONFIG)
    pvp_pool = {
        card[0]
        for set_guid in PVP_COMMON_SET_GUIDS
        for card in card_data.get(set_guid, ())
        if card[2] == "Common" and not card[6] and not card[7]
    }
    assert len(reward.cards) in (2, 3)
    assert all(card[0] in pvp_pool for card in reward.cards[:2])
    if len(reward.cards) == 3:
        assert reward.cards[2][0] in TRANQUIL_DREAM_PVE_CARD_GUIDS
        assert len(reward.equipment_guids) == 2
    else:
        # The PvE slot became a Tranquil Dream equipment item.
        assert set(reward.equipment_guids) & set(TRANQUIL_DREAM_EQUIPMENT_GUIDS)
        assert len(reward.equipment_guids) == 3
    assert not reward.stardust_rarities
    assert set(reward.equipment_guids) <= (
        set(TRANQUIL_DREAM_EQUIPMENT_GUIDS) | {
            str(record.guid) for record in set_equipment_records(
                DEFAULT_RECORD_STORE, SET1_TO_3_EQUIPMENT_SET_GUIDS)})


def test_arena_chest_has_one_card_and_one_equipment_from_arena_set():
    card_data = card_data_from_db()
    reward = generate_arena_chest(
        card_data, DEFAULT_RECORD_STORE, random.Random(41))
    arena_cards = {
        card[0] for pool in _eligible_pve_cards(
            card_data, DEFAULT_RECORD_STORE, (ARENA_CHEST_SET_GUID,)).values()
        for card in pool
    }
    assert reward.card[0] in arena_cards
    arena_equipment = {
        str(record.guid) for record in set_equipment_records(
            DEFAULT_RECORD_STORE, (ARENA_CHEST_SET_GUID,))
    }
    assert reward.equipment_guid in arena_equipment


def test_fort_romor_pack_slots():
    card_data = card_data_from_db()
    reward = generate_campaign_pack(
        card_data, DEFAULT_RECORD_STORE, random.Random(37),
        pack_config=FORT_ROMOR_PACK_CONFIG)
    pvp_pool = {
        card[0]
        for set_guid in PVP_COMMON_SET_GUIDS
        for card in card_data.get(set_guid, ())
        if card[2] == "Common" and not card[6] and not card[7]
    }
    assert len(reward.cards) in (2, 3)
    assert all(card[0] in pvp_pool for card in reward.cards[:2])
    if len(reward.cards) == 3:
        assert reward.cards[2][0] in FORT_ROMOR_PVE_CARD_GUIDS
    else:
        assert set(reward.equipment_guids) & set(FORT_ROMOR_EQUIPMENT_GUIDS)


def test_campaign_packs_never_award_stardust():
    card_data = card_data_from_db()
    campaign_configs = (AZ1_CAMPAIGN_PACK_CONFIG, AZ2_CAMPAIGN_PACK_CONFIG,
                        TRANQUIL_DREAM_PACK_CONFIG, FORT_ROMOR_PACK_CONFIG,
                        USURPER_PACK_CONFIG, SMOLDERING_DEAD_PACK_CONFIG,
                        DEVONSHIRE_KEEP_PACK_CONFIG)
    for config in campaign_configs:
        for seed in range(60):
            reward = generate_campaign_pack(
                card_data, DEFAULT_RECORD_STORE, random.Random(seed),
                pack_config=config)
            assert not reward.stardust_rarities, (config["allow_stardust"], seed)


if __name__ == "__main__":
    run("AZ1 rarity summary", test_table_weight_totals_and_rarity_summary)
    run("AZ1 metadata pools resolve", test_metadata_eligible_cards_and_equipment_resolve)
    run("AZ1 pack Set 1-3 slots", test_pack_has_set1_to_3_cards_and_equipment)
    run("AZ2 pack Set 4-6 slots", test_az2_pack_has_set4_to_6_cards_and_equipment)
    run("Tranquil Dream pack slots", test_tranquil_dream_pack_slots)
    run("Fort Romor pack slots", test_fort_romor_pack_slots)
    run("Arena chest contents", test_arena_chest_has_one_card_and_one_equipment_from_arena_set)
