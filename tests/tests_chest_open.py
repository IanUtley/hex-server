"""Regression tests for opening client-addressed treasure chests.

The fixed client opens a chest by the InventoryItem UID the server handed it,
which comes from two spaces: ``9000 + treasure_chests.id`` for chest rewards
and the ``player_inventory.client_item_uid`` for InventoryTreasureChest items
(the store's AZ1/Howling Plains campaign booster).  Resolving only the first
space made the store pack report "Invalid Chest ID".
"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
import hconnect_server
from az1_pack import (AZ1_CAMPAIGN_PACK_GUID, AZ2_CAMPAIGN_PACK_GUID,
                      AZ2_PVE_CARD_SET_GUIDS, AZ2_PVP_COMMON_SET_GUIDS,
                      PVE_CARD_SET_GUIDS, PVP_COMMON_SET_GUIDS)
from profile_db import (db_create_treasure_chest, db_get_chest_by_id,
                        db_inventory_item, db_inventory_item_by_client_uid)
from services.store import apply_purchase
from static import CRAYBURN_PACK_CARD_SEEDS


SET3_PVP_GUID = "fce480eb-15f9-4096-8d12-6beee9118652"
CRAYBURN_VENNEN_GUID = "a7f16a72-2975-464d-8d3a-ff6f5bfd7c3b"


def _new_user(user_id):
    db._db.execute(
        "INSERT INTO users (id, name, gold, platinum) VALUES (?,?,?,?)",
        (user_id, f"ChestTester{user_id}", 100000, 0))
    db._db.commit()
    return user_id


def _stub_handler(user_id):
    from types import SimpleNamespace
    return SimpleNamespace(user_profile={"id": user_id})


def _store_item_id(template_guid):
    row = db._db.execute("SELECT id FROM store_items WHERE template_guid=?",
                         (template_guid,)).fetchone()
    assert row, f"store item {template_guid} is missing from store_items"
    return int(row[0])


def _buy_campaign_booster(user_id):
    """Buy the AZ1 campaign booster; returns its client inventory UID."""
    item_id = _store_item_id(AZ1_CAMPAIGN_PACK_GUID)
    result = apply_purchase(db._db, user_id, item_id, 1)
    assert result["template_guid"] == AZ1_CAMPAIGN_PACK_GUID, result
    # The store's purchase UID space is ``1000 + store_items.id`` and is
    # exactly what the client sends back when the pack is opened.
    return 1000 + item_id


def test_store_chest_uid_resolves_in_inventory_space():
    user_id = _new_user(9001)
    chest_uid = _buy_campaign_booster(user_id)
    chest = hconnect_server._resolve_client_chest(user_id, chest_uid)
    assert chest is not None, "store-purchased AZ1 pack must resolve as a chest"
    assert chest.template_guid == AZ1_CAMPAIGN_PACK_GUID
    assert chest.db_id is None
    assert chest.client_uid == chest_uid
    # The legacy ``uid - 9000`` mapping is what produced InvalidChestID here.
    assert hconnect_server._resolve_client_chest(user_id, 9000 + chest_uid) is None


def test_opening_store_pack_awards_campaign_contents_and_consumes_it():
    user_id = _new_user(9002)
    chest_uid = _buy_campaign_booster(user_id)
    summary = hconnect_server._open_client_chests(
        _stub_handler(user_id), [chest_uid])
    assert summary["invalid"] == []
    assert len(summary["opened"]) == 1
    # Authored AZ1 contents: two PvP commons, one weighted PvE card, and two
    # equipment/Stardust slots.
    assert len(summary["cards"]) == 3
    assert len(summary["card_template_ids"]) == 3
    assert len(summary["inventory_template_ids"]) == 2
    assert len(summary["inventory_updates"]) == 2
    # The chest entry is consumed, and reopening it is now invalid.
    assert db_inventory_item_by_client_uid(user_id, chest_uid) is None
    assert hconnect_server._resolve_client_chest(user_id, chest_uid) is None
    # Every awarded card landed in the collection.
    for guid in summary["card_template_ids"]:
        row = db._db.execute(
            "SELECT quantity FROM collections "
            "WHERE user_id=? AND card_template_id=?",
            (user_id, guid)).fetchone()
        assert row and int(row[0]) >= 1
    for _guid, _uid, quantity in summary["inventory_updates"]:
        assert quantity == 1


def test_stacked_store_pack_keeps_remaining_quantity():
    user_id = _new_user(9003)
    item_id = _store_item_id(AZ1_CAMPAIGN_PACK_GUID)
    apply_purchase(db._db, user_id, item_id, 2)
    chest_uid = 1000 + item_id
    summary = hconnect_server._open_client_chests(
        _stub_handler(user_id), [chest_uid])
    assert [(uid, remaining) for _t, uid, remaining in summary["opened"]] == \
        [(chest_uid, 1)]
    row = db_inventory_item_by_client_uid(user_id, chest_uid)
    assert row is not None and int(row[2]) == 1


def test_reward_chest_uid_space_still_resolves_and_consumes():
    user_id = _new_user(9004)
    chest_db_id = db_create_treasure_chest(
        user_id, SET3_PVP_GUID, "Common", conn=db._db)
    chest_uid = 9000 + chest_db_id
    chest = hconnect_server._resolve_client_chest(user_id, chest_uid)
    assert chest is not None and chest.db_id == chest_db_id
    assert chest.set_guid == SET3_PVP_GUID
    summary = hconnect_server._open_client_chests(
        _stub_handler(user_id), [chest_uid])
    assert len(summary["opened"]) == 1
    assert summary["opened"][0][2] == 0
    assert db_get_chest_by_id(chest_db_id, user_id) is None
    assert hconnect_server._resolve_client_chest(user_id, chest_uid) is None


def test_non_chest_inventory_items_are_not_openable_chests():
    user_id = _new_user(9005)
    # Set 1 booster is an InventoryCardPack opened through OpenCardPack.
    booster_guid = "a8b78207-686a-4994-b6cd-4548d1349841"
    item_id = _store_item_id(booster_guid)
    apply_purchase(db._db, user_id, item_id, 1)
    booster_uid = 1000 + item_id
    assert db_inventory_item(user_id, booster_guid) is not None
    assert hconnect_server._resolve_client_chest(user_id, booster_uid) is None


def _promo_chest(template_guid):
    return hconnect_server.ClientChest(0, template_guid, "", "Promo", None, None)


def _split_campaign_cards(cards, templates):
    """Return ``(set_guid, rarity)`` for the PvP commons and the PvE cards."""
    by_guid = {card[0]: card for cards_ in templates.values() for card in cards_}
    sets = {card[0]: set_guid for set_guid, cards_ in templates.items()
            for card in cards_}
    pvp = [c for c in cards if not by_guid[c[0]][6]]
    pve = [c for c in cards if by_guid[c[0]][6]]
    return ([(sets[c[0]], by_guid[c[0]][2]) for c in pvp],
            [(sets[c[0]], by_guid[c[0]][2]) for c in pve])


def test_promo_packs_use_their_own_authored_contents():
    templates = hconnect_server._load_card_templates()

    # Crayburn Castle packs are a fixed authored card list with no equipment.
    crayburn_cards, crayburn_items = hconnect_server._generate_chest_rewards(
        _promo_chest(CRAYBURN_VENNEN_GUID), templates)
    assert [card[0] for card in crayburn_cards] == \
        list(CRAYBURN_PACK_CARD_SEEDS[CRAYBURN_VENNEN_GUID])
    assert crayburn_items == []

    az1_cards, az1_items = hconnect_server._generate_chest_rewards(
        _promo_chest(AZ1_CAMPAIGN_PACK_GUID), templates, random.Random(7))
    az2_cards, az2_items = hconnect_server._generate_chest_rewards(
        _promo_chest(AZ2_CAMPAIGN_PACK_GUID), templates, random.Random(7))

    # AZ1 draws Set 1-3; AZ2 draws Set 4-6.  Both are 2 PvP commons + 1 PvE
    # card + 2 equipment/Stardust slots.
    az1_pvp, az1_pve = _split_campaign_cards(az1_cards, templates)
    az2_pvp, az2_pve = _split_campaign_cards(az2_cards, templates)
    assert all(set_guid in PVP_COMMON_SET_GUIDS and rarity == "Common"
               for set_guid, rarity in az1_pvp)
    assert all(set_guid in PVE_CARD_SET_GUIDS for set_guid, _ in az1_pve)
    assert all(set_guid in AZ2_PVP_COMMON_SET_GUIDS and rarity == "Common"
               for set_guid, rarity in az2_pvp)
    assert all(set_guid in AZ2_PVE_CARD_SET_GUIDS for set_guid, _ in az2_pve)
    assert len(az1_pvp) == len(az2_pvp) == 2
    assert len(az1_pve) == len(az2_pve) == 1
    assert len(az1_items) == len(az2_items) == 2
    # The three promo packs are distinct products: the Crayburn list is fixed,
    # and the AZ1/AZ2 pools never overlap because their sets are disjoint.
    assert {set_guid for set_guid, _ in az1_pvp + az1_pve}.isdisjoint(
        {set_guid for set_guid, _ in az2_pvp + az2_pve})
    assert [card[0] for card in crayburn_cards] != \
        [card[0] for card in az1_cards]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures += 1
                import traceback
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
