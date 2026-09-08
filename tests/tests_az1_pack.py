"""Regression tests for the Adventure Zone campaign-pack generators."""

import os
import sys
import random
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from az1_pack import (
    AZ1_PVE_CARD_WEIGHTS,
    AZ1_EQUIPMENT_WEIGHTS,
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
    az1_equipment_records,
    az1_probability_summary,
    generate_az2_pack,
    generate_az1_pack,
    set_equipment_records,
)
from gamedata import DEFAULT_RECORD_STORE


def card_data_from_db():
    # ``tests/run_all.py`` gives each test process an isolated in-memory
    # runtime DB, while this test reads the seeded card catalogue directly.
    # Prefer the suite's immutable snapshot so this read does not point at a
    # separate empty ``:memory:`` database.
    db_path = os.environ.get("HEX_TEST_SOURCE_DB") or os.environ.get(
        "HEX_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "hconnect.db"))
    con = sqlite3.connect(db_path)
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
    assert sum(weight for _rarity, weight in AZ1_PVE_CARD_WEIGHTS.values()) == 50.0
    assert round(sum(weight for _name, _rarity, weight in AZ1_EQUIPMENT_WEIGHTS), 2) == 200.08
    summary = az1_probability_summary()
    assert summary["pve_card"] == {
        rarity: weight / 100.0
        for rarity, weight in AZ1_PVE_RARITY_WEIGHTS.items()
    }
    assert summary["equipment_slot"] == {
        slot_type: weight / 100.0
        for slot_type, weight in AZ1_EQUIPMENT_SLOT_WEIGHTS.items()
    }


def test_all_metadata_eligible_cards_and_table_equipment_resolve():
    card_data = card_data_from_db()
    cards = _eligible_pve_cards(card_data, DEFAULT_RECORD_STORE,
                                PVE_CARD_SET_GUIDS)
    assert sum(len(pool) for pool in cards.values()) > 0
    assert not [name for name, _rarity, _weight in AZ1_EQUIPMENT_WEIGHTS
                if name.casefold() not in az1_equipment_records(DEFAULT_RECORD_STORE)]
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
            card_data, DEFAULT_RECORD_STORE, PVE_CARD_SET_GUIDS).values()
        for card in pool
    }
    assert reward.cards[2][0] in eligible
    assert len(reward.equipment_guids) + len(reward.stardust_rarities) == 2
    equipment_pool = {
        str(record.guid) for record in set_equipment_records(
            DEFAULT_RECORD_STORE, SET1_TO_3_EQUIPMENT_SET_GUIDS)
    }
    assert set(reward.equipment_guids) <= equipment_pool
    assert all(rarity in AZ1_STARDUST_RARITIES
               for rarity in reward.stardust_rarities)


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
    assert reward.cards[2][0] in eligible
    equipment_pool = {
        str(record.guid) for record in set_equipment_records(
            DEFAULT_RECORD_STORE, SET3_TO_6_EQUIPMENT_SET_GUIDS)
    }
    assert set(reward.equipment_guids) <= equipment_pool
    assert len(reward.equipment_guids) + len(reward.stardust_rarities) == 2


if __name__ == "__main__":
    run("AZ1 table totals and rarity summary", test_table_weight_totals_and_rarity_summary)
    run("AZ1 metadata pools resolve", test_all_metadata_eligible_cards_and_table_equipment_resolve)
    run("AZ1 pack Set 1-3 slots", test_pack_has_set1_to_3_cards_and_equipment)
    run("AZ2 pack Set 4-6 slots", test_az2_pack_has_set4_to_6_cards_and_equipment)
