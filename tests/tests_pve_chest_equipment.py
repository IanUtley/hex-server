"""PvE chest loot, Wheels of Fate, deck equipment and mercenary parties.

Key paths only: the design-note chest-loot pool (no equipment table), one
chest roll, one Wheels of Fate spin, deck equipment on a saved deck, and a
mercenary party member resolved for a campaign battle.
"""

import io
import os
import random
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
import campaign
import encoded_decks
import hconnect_server
from profile_db import (db_create_treasure_chest, db_deck_equipment,
                        db_get_user_currency, db_save_deck,
                        db_upsert_inventory_item)
from services import deck_templates, equipment, mercenaries
from services import wheel_of_fate as wof
from services.chest_loot import ITEMS_PER_CHEST, roll_chest_loot

SET1 = "0382f729-7710-432b-b761-13677982dcd2"
SCARS = "fbbac856-2264-4d31-97b0-0d8a646b9597"
VOLTAIC_HANDGUARDS = "6e624b3f-938b-4bc5-a0aa-15fb58774d82"   # Set 1 Chest Loot
CORONA_SPEAR = "f6a182cd-417a-4a24-8e69-79ab7f2f4a84"         # multi-line Scars note
CHARGE_BOT = "7325706e-6bf1-4ca4-8d6b-5da13ac069f4"
CHARGE_BOT_EQUIPPED = "dd93fe6b-764a-4851-8bdb-6d7369818962"
BEBO_ITEM = "321fd775-ee06-4186-a5d6-368d552f8d09"
BEBO_TEMPLATE = "8a336fec-d970-4613-9308-c0afd41b62eb"
ZERO = "00000000-0000-0000-0000-000000000000"
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


def _new_user(user_id, gold=0):
    db._db.execute("INSERT INTO users (id, name, gold, platinum) VALUES (?,?,?,0)",
                   (user_id, f"PveLootTester{user_id}", gold))
    db._db.commit()
    return user_id


def test_chest_pool_comes_from_design_notes():
    # No table: the pool is built from the Records design notes.
    assert not db._db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='equipment_templates'").fetchone()
    pool = equipment.chest_loot_pool(SET1)
    assert {r: len(v) for r, v in pool.items()} == {
        "Common": 40, "Uncommon": 30, "Rare": 20, "Legendary": 12}, pool
    assert VOLTAIC_HANDGUARDS in pool["Common"]
    # "Scars Chest ..." names Scars of War by its set name; the note has a
    # literal newline, which the strict record store rejects.
    spear = equipment.equipment_item(CORONA_SPEAR)
    assert spear is not None and spear.chest_set_number == 6, spear
    assert CORONA_SPEAR in equipment.chest_loot_pool(SCARS)["Legendary"]
    assert equipment.chest_loot_pool(ZERO) == {}
    # A note resolves explicit set numbers, set names, and bare "Chest".
    names = [("shards of fate", 1), ("scars of war", 6)]
    assert equipment.chest_note_set_number("set 4 chest loot", "", {}, names) == 4
    assert equipment.chest_note_set_number("Scars Chest", "", {}, names) == 6
    assert equipment.chest_note_set_number("Chest", "s", {"s": 7}, names) == 7
    assert equipment.chest_note_set_number("Set 4 WOF", "", {}, names) is None


def test_chest_roll_uses_the_set_pool():
    templates = hconnect_server._load_card_templates()
    pool = {guid for guids in equipment.chest_loot_pool(SET1).values() for guid in guids}
    rng = random.Random(7)
    for rarity, count in ITEMS_PER_CHEST.items():
        for _ in range(60):
            cards, items = roll_chest_loot(db._db, templates, SET1, rarity, rng)
            assert len(cards) + len(items) == count, (rarity, cards, items)
            for guid, kind in items:
                if kind == "equipment":
                    assert guid in pool, guid


def _only(outcome):
    table = {name: (1 if name == outcome else 0,) + row[1:]
             for name, row in wof.OUTCOMES.items()}
    return [mock.patch.dict(wof.OUTCOMES, table),
            mock.patch.object(wof, "RED_CHANCE", 0.0),
            mock.patch.object(wof, "GOLD_CHANCE", 0.0)]


def _spin(handler, chest_db_id, outcome):
    patches = _only(outcome)
    for patch in patches:
        patch.__enter__()
    try:
        return hconnect_server._spin_client_chest(handler, 9000 + chest_db_id)
    finally:
        for patch in reversed(patches):
            patch.__exit__(None, None, None)


def test_spin_upgrades_and_keeps_the_chest():
    user_id = _new_user(9601, 5000)
    handler = SimpleNamespace(user_profile={"id": user_id})
    chest_db_id = db_create_treasure_chest(user_id, SET1, "Common", conn=db._db)
    spin = _spin(handler, chest_db_id, "upgrade")
    assert spin["error"] == wof.OK and spin["rarity"] == "Uncommon", spin
    assert db_get_user_currency(user_id, "gold", conn=db._db) == 5000 - 1200
    row = db._db.execute("SELECT chest_rarity, opened, wof_spun, wof_status "
                         "FROM treasure_chests WHERE id=?", (chest_db_id,)).fetchone()
    assert tuple(row) == ("Uncommon", 0, 1, wof.NO_SPIN), tuple(row)
    assert _spin(handler, chest_db_id, "paid_spin")["error"] == wof.NO_SPIN_LEFT
    reply = hconnect_server._encode_spin_response(spin, 9000 + chest_db_id)
    assert b"SpinWheelOfFateResponse" in reply and b"RewardedItems" in reply
    summary = hconnect_server._open_client_chests(handler, [9000 + chest_db_id])
    assert summary["opened"] and not summary["invalid"], summary


class _Handler(hconnect_server.HCPHandler):
    def __init__(self, user_id):
        self.user_profile = {"id": user_id}
        self.client_uid = 1
        self.scnt = 0
        self.sid = "test"
        self.sent = []

    def send(self, headers, body=b""):
        self.sent.append(body)


def _update_deck_request(deck_id, equipment_guids):
    from objfmt_builder import ObjFmtBuilder
    b = ObjFmtBuilder("Game.Shared.Network.Profile.UpdateDeckRequestArgs")
    b.field_uid("DeckID", (deck_id << 8) | 17)
    b.field_str("DeckName", "Equip Test")
    b.field_resource_id_list("EquipmentIDs", list(equipment_guids))
    b.field_resource_id_list("TalentIDs", [])
    return b.finish(4)


def test_deck_equipment_is_saved_and_upgrades_cards():
    user_id = _new_user(9602)
    db_upsert_inventory_item(user_id, VOLTAIC_HANDGUARDS, 1, 1, conn=db._db)
    deck_id = db_save_deck(user_id, "Equip Test", conn=db._db)
    db._db.commit()
    handler = _Handler(user_id)
    unowned_head = sorted(item.guid for item in equipment.equipment_items().values()
                          if item.equipment_type == "Head")[0]
    handler._handle_service_request_legacy(
        "t", "i", 2095, 2, 0, ZERO, 0, {},
        _update_deck_request(deck_id, [VOLTAIC_HANDGUARDS, unowned_head]))
    assert db_deck_equipment(deck_id, conn=db._db) == [VOLTAIC_HANDGUARDS]
    assert equipment.equipped_variant(CHARGE_BOT, [VOLTAIC_HANDGUARDS]) == CHARGE_BOT_EQUIPPED
    assert equipment.equipped_variant(CHARGE_BOT, [unowned_head]) is None
    slots = equipment.equipment_slots([VOLTAIC_HANDGUARDS])
    assert slots == [ZERO, ZERO, VOLTAIC_HANDGUARDS, ZERO, ZERO, ZERO], slots


def test_mercenary_party_member_fights_the_battle():
    user_id = _new_user(9603)
    buf = io.BytesIO()
    encoded_decks.encode_profile_deck_template(
        buf, "BEBO Bots", BEBO_TEMPLATE, ZERO,
        [(CHARGE_BOT, 4, False, False, False)])
    template_id, name = deck_templates.save_template(db._db, user_id, 0, buf.getvalue())
    assert name == "BEBO Bots"
    mercenaries.save_party(db._db, user_id, {
        "ChampionID": 4,
        "Members": [{"Mercenary": {"m_Guid": BEBO_ITEM},
                     "DeckTemplate": template_id, "Upgrade": 0}]})
    db._db.commit()
    handler = hconnect_server.HCPHandler.__new__(hconnect_server.HCPHandler)
    handler.user_profile = {"id": user_id}
    campaign._active_mercenary[0] = (4, BEBO_ITEM)
    try:
        cfg = campaign.resolve_battle_config(handler, db._db, 0, "camp_0")
    finally:
        campaign._active_mercenary.pop(0, None)
    assert cfg["player_champ_guid"] == BEBO_TEMPLATE, cfg["player_champ_guid"]
    assert cfg["player_talents_json"] == "[]"
    assert cfg["mercenary_deck_cards"] == [CHARGE_BOT] * 4


if __name__ == "__main__":
    run("chest pool comes from design notes", test_chest_pool_comes_from_design_notes)
    run("chest roll uses the set pool", test_chest_roll_uses_the_set_pool)
    run("spin upgrades and keeps the chest", test_spin_upgrades_and_keeps_the_chest)
    run("deck equipment is saved and upgrades cards",
        test_deck_equipment_is_saved_and_upgrades_cards)
    run("mercenary party member fights the battle",
        test_mercenary_party_member_fights_the_battle)
    if FAILURES:
        sys.exit(1)
