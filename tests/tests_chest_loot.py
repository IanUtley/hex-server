"""Booster treasure chests award their set's chest-loot equipment."""

import os
import random
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
import hconnect_server
from profile_db import db_create_treasure_chest, db_inventory_item
from services.chest_loot import (EQUIPMENT_RARITY_WEIGHTS, ITEMS_PER_CHEST,
                                 chest_loot_pool, roll_chest_equipment)

SET1 = "0382f729-7710-432b-b761-13677982dcd2"
FAILURES = []


def run(name, fn):
    try:
        fn()
        print(f"PASS {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"FAIL {name}: {e}")
    except Exception as e:
        import traceback
        FAILURES.append(name)
        print(f"ERROR {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


def _new_user(user_id):
    db._db.execute("INSERT INTO users (id, name) VALUES (?,?)",
                   (user_id, f"ChestLootTester{user_id}"))
    db._db.commit()
    return user_id


def _rarity_of(guid):
    return db._db.execute(
        "SELECT rarity FROM equipment_templates WHERE guid=?", (guid,)).fetchone()[0]


def _is_equipment(guid):
    return db._db.execute(
        "SELECT 1 FROM equipment_templates WHERE guid=?", (guid,)).fetchone() is not None


def test_equipment_catalog_seeded():
    count = db._db.execute("SELECT COUNT(*) FROM equipment_templates").fetchone()[0]
    assert count > 1900, count
    voltaic = db._db.execute(
        "SELECT name, set_guid, rarity, is_chest_loot FROM equipment_templates "
        "WHERE guid='6e624b3f-938b-4bc5-a0aa-15fb58774d82'").fetchone()
    assert tuple(voltaic) == ("Voltaic Handguards", SET1, "Common", 1), voltaic


def test_set1_pool_matches_client_data():
    pool = chest_loot_pool(db._db, SET1)
    assert {r: len(v) for r, v in pool.items()} == {
        "Common": 39, "Uncommon": 25, "Rare": 15, "Legendary": 11}, pool


def test_item_counts_and_rarity_eligibility():
    rng = random.Random(1)
    for chest_rarity, allowed in EQUIPMENT_RARITY_WEIGHTS.items():
        for _ in range(200):
            items = roll_chest_equipment(db._db, SET1, chest_rarity, rng)
            assert len(items) == ITEMS_PER_CHEST[chest_rarity], (chest_rarity, items)
            for guid in items:
                assert _rarity_of(guid) in allowed, (chest_rarity, _rarity_of(guid))


def test_set_without_chest_loot_rolls_nothing():
    assert roll_chest_equipment(
        db._db, "00000000-0000-0000-0000-000000000000", "Rare") == []


def test_opening_set_chest_awards_equipment_not_cards():
    user_id = _new_user(9101)
    handler = SimpleNamespace(user_profile={"id": user_id})
    chest_db_id = db_create_treasure_chest(user_id, SET1, "Legendary", conn=db._db)
    summary = hconnect_server._open_client_chests(handler, [9000 + chest_db_id])
    assert summary["invalid"] == []
    assert summary["cards"] == [], summary["cards"]
    assert summary["inventory_updates"], summary
    for guid, uid, quantity in summary["inventory_updates"]:
        assert _is_equipment(guid), guid
        assert uid > 0
        row = db_inventory_item(user_id, guid)
        assert row is not None and int(row[1]) == quantity


class _SpinHandler(hconnect_server.HCPHandler):
    """Drives the real SpinWheelOfFate handler with the transport replaced."""

    def __init__(self, user_id):
        self.user_profile = {"id": user_id}
        self.client_uid = 1
        self.scnt = 0
        self.sid = "test"
        self.sent = []
        self.inventory_pushes = []
        self.card_chunks = []

    def send(self, headers, body=b""):
        self.sent.append(body)

    def push_inventory_to_client(self, qty=1, template_guid="", item_id=1001):
        self.inventory_pushes.append((template_guid, item_id, qty))

    def _send_cards_chunk(self, cards):
        self.card_chunks.append(cards)

    def _send_inventory_updated(self, *a, **k):
        pass


def test_spin_wheel_of_fate_reports_equipment():
    user_id = _new_user(9102)
    chest_db_id = db_create_treasure_chest(user_id, SET1, "Rare", conn=db._db)
    handler = _SpinHandler(user_id)
    handler._handle_service_request_legacy(
        "t", "i", 2049, 2, 0, "00000000-0000-0000-0000-000000000000", 0,
        {"ChestID": str(9000 + chest_db_id)}, b"")
    assert handler.sent, "no response sent"
    assert not handler.card_chunks
    pushed = [guid for guid, _, _ in handler.inventory_pushes if _is_equipment(guid)]
    assert len(pushed) == 1, handler.inventory_pushes
    body = handler.sent[0]
    assert b"RewardItems" in body and pushed[0].encode() in body


if __name__ == "__main__":
    run("equipment catalog is seeded", test_equipment_catalog_seeded)
    run("set 1 chest pool matches client data", test_set1_pool_matches_client_data)
    run("item counts and rarity eligibility", test_item_counts_and_rarity_eligibility)
    run("set without chest loot rolls nothing", test_set_without_chest_loot_rolls_nothing)
    run("opening a set chest awards equipment", test_opening_set_chest_awards_equipment_not_cards)
    run("SpinWheelOfFate reports equipment", test_spin_wheel_of_fate_reports_equipment)
    if FAILURES:
        sys.exit(1)
