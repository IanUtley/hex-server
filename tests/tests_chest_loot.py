"""Booster treasure chests award their set's chest loot."""

import os
import random
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
import hconnect_server
from profile_db import db_create_treasure_chest, db_inventory_item
from db import STARDUST_TEMPLATES
from services.chest_loot import (CHEST_CONTENT_WEIGHTS, ITEMS_PER_CHEST,
                                 aa_card_pool, booster_pack_pool,
                                 chest_inventory_items,
                                 chest_loot_pool, pve_card_pool, roll_chest_loot)

SET1 = "0382f729-7710-432b-b761-13677982dcd2"
SET4 = "2d05262c-d7a0-408f-a280-36d206a29344"
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


def _names(cards):
    return sorted(card[1] for card in cards)


def _item_names(guids):
    from gamedata import DEFAULT_RECORD_STORE
    return sorted(DEFAULT_RECORD_STORE.get("InventoryItemData", guid).field("m_Name")
                  for guid in guids)


def test_set1_prize_pools():
    templates = hconnect_server._load_card_templates()
    assert _names(aa_card_pool(templates, SET1, "low")) == [
        "Hex Engine", "Rot Caster", "Savage Raider", "Wall of Corpses"]
    assert _names(aa_card_pool(templates, SET1, "high")) == [
        "Crash of Beasts", "Mastery of Time"]
    assert all(card[2] == "Epic" for card in aa_card_pool(templates, SET1, "any"))
    assert _names(pve_card_pool(templates, SET1, "any")) == [
        "Alchemy Lab", "Cerulean Sky Mage", "Horrific Poltergeist",
        "Radiant Salvation", "Scourgecrag Witch", "Volley"]
    # Cerulean Sky Mage is Legendary: only Legendary and Primal chests.
    assert "Cerulean Sky Mage" not in _names(pve_card_pool(templates, SET1, "rare"))
    mercenaries, sleeves = chest_inventory_items(SET1)
    assert _item_names(mercenaries) == [
        "Count Davian", "Spirit of the Triumverate", "The Transcended"]
    assert _item_names(sleeves) == [
        "Count Davian Sleeve", "Transcended Sleeve", "Triumverate Sleeve"]
    # The standard Shards of Fate booster, not the full-set or Primal pack.
    assert booster_pack_pool(db._db, SET1) == ["a8b78207-686a-4994-b6cd-4548d1349841"]


def _classify(guid, kind, templates):
    """Return (kind, tier) for an awarded prize so eligibility can be checked."""
    if kind == "card":
        for tier in ("low", "high"):
            if guid in {c[0] for c in aa_card_pool(templates, SET1, tier)}:
                return "aa_card", tier
        rare = {c[0] for c in pve_card_pool(templates, SET1, "rare")}
        assert guid in {c[0] for c in pve_card_pool(templates, SET1, "any")}, guid
        return "pve_card", "rare" if guid in rare else "legendary"
    if kind == "equipment":
        return kind, _rarity_of(guid)
    if kind == "stardust":
        return kind, next(r for r, g in STARDUST_TEMPLATES.items() if g == guid)
    return kind, ""


def test_item_counts_and_prize_eligibility():
    templates = hconnect_server._load_card_templates()
    rng = random.Random(1)
    for chest_rarity, weights in CHEST_CONTENT_WEIGHTS.items():
        allowed = set(weights)
        if ("aa_card", "any") in allowed:
            allowed |= {("aa_card", "low"), ("aa_card", "high")}
        if any(kind == "pve_card" and tier == "any" for kind, tier in allowed):
            allowed |= {("pve_card", "rare"), ("pve_card", "legendary")}
        seen = set()
        for _ in range(300):
            cards, items = roll_chest_loot(db._db, templates, SET1, chest_rarity, rng)
            assert len(cards) + len(items) == ITEMS_PER_CHEST[chest_rarity],                 (chest_rarity, cards, items)
            prizes = [(card[0], "card") for card in cards] + items
            for guid, kind in prizes:
                prize = _classify(guid, kind, templates)
                assert prize in allowed, (chest_rarity, prize)
                seen.add(prize[0])
        assert "equipment" in seen, (chest_rarity, seen)
    assert ("sleeve", "") not in CHEST_CONTENT_WEIGHTS["Legendary"]
    assert ("mercenary", "") not in CHEST_CONTENT_WEIGHTS["Uncommon"]
    assert ("booster", "") not in CHEST_CONTENT_WEIGHTS["Rare"]


def test_common_chest_odds_follow_the_survey():
    templates = hconnect_server._load_card_templates()
    rng = random.Random(2)
    rolls = 4000
    counts = {"equipment": 0, "stardust": 0, "card": 0}
    for _ in range(rolls):
        cards, items = roll_chest_loot(db._db, templates, SET1, "Common", rng)
        counts["card"] += len(cards)
        for _, kind in items:
            counts[kind] += 1
    # Survey: 85.8% equipment, 7.0% Stardust, 6.7% AA cards.
    assert 0.83 < counts["equipment"] / rolls < 0.89, counts
    assert 0.05 < counts["stardust"] / rolls < 0.09, counts
    assert 0.05 < counts["card"] / rolls < 0.09, counts


def test_set_without_known_aa_cards_skips_them():
    templates = hconnect_server._load_card_templates()
    assert aa_card_pool(templates, SET4, "high") == []
    rng = random.Random(4)
    for _ in range(300):
        cards, _items = roll_chest_loot(db._db, templates, SET4, "Primal", rng)
        for card in cards:
            assert card[0] in {c[0] for c in pve_card_pool(templates, SET4, "any")}


def test_set_without_chest_loot_rolls_nothing():
    assert roll_chest_loot(
        db._db, hconnect_server._load_card_templates(),
        "00000000-0000-0000-0000-000000000000", "Rare") == ([], [])


def _only(prize):
    """Patch the chest table so every roll awards one kind of prize."""
    return mock.patch.dict(
        CHEST_CONTENT_WEIGHTS, {rarity: {prize: 1} for rarity in CHEST_CONTENT_WEIGHTS})


def test_opening_set_chest_awards_inventory_prizes():
    user_id = _new_user(9101)
    handler = SimpleNamespace(user_profile={"id": user_id})
    mercenaries, _ = chest_inventory_items(SET1)
    boosters = booster_pack_pool(db._db, SET1)
    for prize, check in ((("equipment", "Legendary"), _is_equipment),
                         (("mercenary", ""), lambda guid: guid in mercenaries),
                         (("booster", ""), lambda guid: guid in boosters)):
        chest_db_id = db_create_treasure_chest(user_id, SET1, "Legendary", conn=db._db)
        with _only(prize):
            summary = hconnect_server._open_client_chests(handler, [9000 + chest_db_id])
        assert summary["invalid"] == []
        assert summary["cards"] == [], summary["cards"]
        assert summary["inventory_updates"], summary
        for guid, uid, quantity in summary["inventory_updates"]:
            assert check(guid), (prize, guid)
            assert uid > 0
            row = db_inventory_item(user_id, guid)
            assert row is not None and int(row[1]) == quantity


def test_opening_set_chest_awards_cards():
    user_id = _new_user(9103)
    handler = SimpleNamespace(user_profile={"id": user_id})
    templates = hconnect_server._load_card_templates()
    aa_cards = {card[0] for card in aa_card_pool(templates, SET1, "high")}
    chest_db_id = db_create_treasure_chest(user_id, SET1, "Primal", conn=db._db)
    with _only(("aa_card", "high")):
        summary = hconnect_server._open_client_chests(handler, [9000 + chest_db_id])
    assert summary["inventory_updates"] == [], summary
    assert len(summary["cards"]) == ITEMS_PER_CHEST["Primal"], summary["cards"]
    assert set(summary["card_template_ids"]) <= aa_cards, summary["card_template_ids"]


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
    with _only(("equipment", "Rare")):
        handler._handle_service_request_legacy(
            "t", "i", 2049, 2, 0, "00000000-0000-0000-0000-000000000000", 0,
            {"ChestID": str(9000 + chest_db_id)}, b"")
    assert handler.sent, "no response sent"
    assert not handler.card_chunks
    pushed = [guid for guid, _, _ in handler.inventory_pushes if _is_equipment(guid)]
    assert len(pushed) == 1, handler.inventory_pushes
    body = handler.sent[0]
    assert b"RewardItems" in body and pushed[0].encode() in body


def test_spin_wheel_of_fate_reports_cards():
    user_id = _new_user(9104)
    chest_db_id = db_create_treasure_chest(user_id, SET1, "Rare", conn=db._db)
    handler = _SpinHandler(user_id)
    with _only(("pve_card", "rare")):
        handler._handle_service_request_legacy(
            "t", "i", 2049, 2, 0, "00000000-0000-0000-0000-000000000000", 0,
            {"ChestID": str(9000 + chest_db_id)}, b"")
    assert handler.sent, "no response sent"
    assert len(handler.card_chunks) == 1 and len(handler.card_chunks[0]) == 1
    guid = handler.card_chunks[0][0][0]
    assert b"RewardCards" in handler.sent[0] and guid.encode() in handler.sent[0]


if __name__ == "__main__":
    run("equipment catalog is seeded", test_equipment_catalog_seeded)
    run("set 1 chest pool matches client data", test_set1_pool_matches_client_data)
    run("set 1 prize pools", test_set1_prize_pools)
    run("item counts and prize eligibility", test_item_counts_and_prize_eligibility)
    run("common chest odds follow the survey", test_common_chest_odds_follow_the_survey)
    run("set without known AA cards skips them", test_set_without_known_aa_cards_skips_them)
    run("set without chest loot rolls nothing", test_set_without_chest_loot_rolls_nothing)
    run("opening a set chest awards inventory prizes", test_opening_set_chest_awards_inventory_prizes)
    run("opening a set chest awards cards", test_opening_set_chest_awards_cards)
    run("SpinWheelOfFate reports equipment", test_spin_wheel_of_fate_reports_equipment)
    run("SpinWheelOfFate reports cards", test_spin_wheel_of_fate_reports_cards)
    if FAILURES:
        sys.exit(1)
