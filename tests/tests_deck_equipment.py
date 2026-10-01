"""Deck equipment: saving, reloading, login encoding, and equipped cards."""

import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
import encoded_decks
import hconnect_server
from objfmt_builder import ObjFmtBuilder
from profile_db import db_deck_equipment, db_save_deck, db_upsert_inventory_item
from services.equipment import (ZERO_GUID, equipment_slots, equipped_variant,
                                parse_request_equipment_ids,
                                sanitize_deck_equipment)

CHARGE_BOT = "7325706e-6bf1-4ca4-8d6b-5da13ac069f4"
CHARGE_BOT_EQUIPPED = "dd93fe6b-764a-4851-8bdb-6d7369818962"
VOLTAIC_HANDGUARDS = "6e624b3f-938b-4bc5-a0aa-15fb58774d82"   # Gloves
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
                   (user_id, f"DeckEquipTester{user_id}"))
    db._db.commit()
    return user_id


def _equipment_of_type(equipment_type, exclude=()):
    return db._db.execute(
        "SELECT guid FROM equipment_templates WHERE equipment_type=? "
        "AND guid NOT IN (%s) ORDER BY guid LIMIT 1"
        % ",".join("?" * len(exclude) or "''"),
        (equipment_type, *exclude)).fetchone()[0]


def _two_item_card():
    """Return (base, item_a, item_b, variant_a, variant_ab) for one card."""
    rows = db._db.execute(
        "SELECT base_guid, equipment_key, variant_guid FROM equipment_card_variants "
        "WHERE base_guid IN (SELECT base_guid FROM equipment_card_variants "
        "WHERE equipment_key LIKE '%,%') ORDER BY base_guid").fetchall()
    by_base = {}
    for base, key, variant in rows:
        by_base.setdefault(base, {})[key] = variant
    for base, variants in by_base.items():
        pairs = [k for k in variants if "," in k]
        for pair in pairs:
            a, b = pair.split(",")
            if a in variants and b in variants:
                return base, a, b, variants[a], variants[pair]
    raise AssertionError("no card with single and combined variants")


def test_variant_catalog_seeded():
    count = db._db.execute("SELECT COUNT(*) FROM equipment_card_variants").fetchone()[0]
    assert count > 2500, count


def test_equipped_variant_lookup():
    assert equipped_variant(db._db, CHARGE_BOT, [VOLTAIC_HANDGUARDS]) == CHARGE_BOT_EQUIPPED
    assert equipped_variant(db._db, CHARGE_BOT, []) is None
    other = _equipment_of_type("Head")
    assert equipped_variant(db._db, CHARGE_BOT, [other]) is None
    base, a, b, variant_a, variant_ab = _two_item_card()
    assert equipped_variant(db._db, base, [a]) == variant_a
    assert equipped_variant(db._db, base, [b, a]) == variant_ab


def _update_deck_request(deck_id, equipment, talents=()):
    b = ObjFmtBuilder("Game.Shared.Network.Profile.UpdateDeckRequestArgs")
    b.field_uid("DeckID", (deck_id << 8) | 17)
    b.field_str("DeckName", "Equip Test")
    b.field_resource_id_list("EquipmentIDs", list(equipment))
    b.field_resource_id_list("TalentIDs", list(talents))
    return b.finish(4)


def test_parse_request_equipment_ids():
    talent = "11111111-2222-3333-4444-555555555555"
    raw = _update_deck_request(1, [VOLTAIC_HANDGUARDS, ZERO_GUID], [talent])
    assert parse_request_equipment_ids(raw) == [VOLTAIC_HANDGUARDS]
    assert parse_request_equipment_ids(_update_deck_request(1, [])) == []
    b = ObjFmtBuilder("Game.Shared.Network.Profile.UpdateDeckRequestArgs")
    b.field_str("DeckName", "No equipment field")
    assert parse_request_equipment_ids(b.finish(1)) is None


def test_sanitize_keeps_owned_items_one_per_type():
    user_id = _new_user(9201)
    second_gloves = _equipment_of_type("Gloves", exclude=(VOLTAIC_HANDGUARDS,))
    unowned_head = _equipment_of_type("Head")
    db_upsert_inventory_item(user_id, VOLTAIC_HANDGUARDS, 1, 1, conn=db._db)
    db_upsert_inventory_item(user_id, second_gloves, 1, 2, conn=db._db)
    kept = sanitize_deck_equipment(
        db._db, user_id, [VOLTAIC_HANDGUARDS, second_gloves, unowned_head])
    assert kept == [VOLTAIC_HANDGUARDS], kept


def test_equipment_slots_follow_equipment_type_order():
    head = _equipment_of_type("Head")
    slots = equipment_slots(db._db, [VOLTAIC_HANDGUARDS, head])
    assert slots == [head, ZERO_GUID, VOLTAIC_HANDGUARDS, ZERO_GUID, ZERO_GUID, ZERO_GUID]


def test_encoded_deck_template_writes_equipment():
    def encode(slots):
        buf = io.BytesIO()
        encoded_decks.encode_profile_deck_template(
            buf, "D", ZERO_GUID, ZERO_GUID, [], equipment_slots=slots)
        return buf.getvalue()
    empty = encode(None)
    assert empty == encode([ZERO_GUID] * 6)
    equipped = encode([ZERO_GUID, ZERO_GUID, VOLTAIC_HANDGUARDS] + [ZERO_GUID] * 3)
    guid_buf = io.BytesIO()
    encoded_decks.write_guid(guid_buf, VOLTAIC_HANDGUARDS)
    # Equip count 1, key Gloves=2, then the 16-byte ResourceId.
    assert b"\x01\x02" + guid_buf.getvalue() in equipped
    assert len(equipped) == len(empty) + 17


class _Handler(hconnect_server.HCPHandler):
    def __init__(self, user_id):
        self.user_profile = {"id": user_id}
        self.client_uid = 1
        self.scnt = 0
        self.sid = "test"
        self.sent = []

    def send(self, headers, body=b""):
        self.sent.append(body)


def _request(handler, data_type, raw):
    handler._handle_service_request_legacy(
        "t", "i", data_type, 2, 0, ZERO_GUID, 0, {}, raw)
    return handler.sent[-1]


def test_update_deck_saves_and_get_deck_info_returns_equipment():
    user_id = _new_user(9202)
    db_upsert_inventory_item(user_id, VOLTAIC_HANDGUARDS, 1, 1, conn=db._db)
    deck_id = db_save_deck(user_id, "Equip Test", conn=db._db)
    db._db.commit()
    handler = _Handler(user_id)
    unowned = _equipment_of_type("Head")
    _request(handler, 2095, _update_deck_request(deck_id, [VOLTAIC_HANDGUARDS, unowned]))
    assert db_deck_equipment(deck_id, conn=db._db) == [VOLTAIC_HANDGUARDS]

    b = ObjFmtBuilder("Game.Shared.Network.Profile.GetDeckInfoRequestArgs")
    b.field_uid("DeckID", (deck_id << 8) | 17)
    body = _request(handler, 2083, b.finish(1))
    assert VOLTAIC_HANDGUARDS.encode() in body, "GetDeckInfo omits equipment"

    # A save without an EquipmentIDs field leaves the equipment alone.
    b = ObjFmtBuilder("Game.Shared.Network.Profile.UpdateDeckRequestArgs")
    b.field_uid("DeckID", (deck_id << 8) | 17)
    b.field_str("DeckName", "Renamed")
    _request(handler, 2095, b.finish(2))
    assert db_deck_equipment(deck_id, conn=db._db) == [VOLTAIC_HANDGUARDS]

    # Clearing the list unequips everything.
    _request(handler, 2095, _update_deck_request(deck_id, []))
    assert db_deck_equipment(deck_id, conn=db._db) == []


if __name__ == "__main__":
    run("variant catalog is seeded", test_variant_catalog_seeded)
    run("equipped variant lookup", test_equipped_variant_lookup)
    run("parse request equipment ids", test_parse_request_equipment_ids)
    run("sanitize keeps owned items, one per type", test_sanitize_keeps_owned_items_one_per_type)
    run("equipment slots follow EEquipmentType", test_equipment_slots_follow_equipment_type_order)
    run("encoded deck template writes equipment", test_encoded_deck_template_writes_equipment)
    run("UpdateDeck/GetDeckInfo round-trip equipment",
        test_update_deck_saves_and_get_deck_info_returns_equipment)
    if FAILURES:
        sys.exit(1)
