"""Focused tests for profile-domain persistence helpers."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

from profile_db import (db_card_instance_template, db_champion_last_deck,
                        db_consume_inventory, db_find_deck_owner,
                        db_insert_card_instance, db_set_champion_last_deck,
                        db_upsert_inventory_item, db_adjust_user_currency)


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, gold INTEGER DEFAULT 0,
            platinum INTEGER DEFAULT 0
        );
        CREATE TABLE collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            card_template_id TEXT, quantity INTEGER
        );
        CREATE TABLE player_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            template_guid TEXT, quantity INTEGER, client_item_uid INTEGER
        );
        CREATE TABLE decks (id INTEGER PRIMARY KEY, user_id INTEGER);
        CREATE TABLE champions (
            id INTEGER PRIMARY KEY, user_id INTEGER, last_deck_id INTEGER,
            pet_name TEXT
        );
        CREATE TABLE card_instances (
            user_id INTEGER, instance_id INTEGER, template_guid TEXT
        );
    """)
    return conn


def test_adjust_user_currency_is_atomic_and_connection_scoped():
    conn = make_db()
    conn.execute("INSERT INTO users(id, gold, platinum) VALUES (1, 10, 20)")
    conn.commit()
    assert db_adjust_user_currency(1, gold_delta=7, platinum_delta=-3,
                                   conn=conn) == (17, 17)
    assert conn.execute(
        "SELECT gold, platinum FROM users WHERE id=1").fetchone() == (17, 17)
    # The helper must not commit a transaction supplied by the caller.
    conn.rollback()
    assert conn.execute(
        "SELECT gold, platinum FROM users WHERE id=1").fetchone() == (10, 20)


def test_inventory_consume_and_upsert_are_connection_scoped():
    conn = make_db()
    conn.execute(
        "INSERT INTO player_inventory(user_id,template_guid,quantity,client_item_uid) "
        "VALUES (1,'pack',3,44)")
    assert db_consume_inventory(1, "pack", 2, conn=conn) == (44, 1)
    assert db_consume_inventory(1, "pack", 2, conn=conn) is None
    assert db_upsert_inventory_item(1, "reward", 1, 45, conn=conn) == 45
    assert db_upsert_inventory_item(1, "reward", 2, 46, conn=conn) == 45
    assert conn.execute(
        "SELECT quantity, client_item_uid FROM player_inventory "
        "WHERE user_id=1 AND template_guid='reward'").fetchone() == (3, 45)


def test_deck_champion_and_card_instance_helpers_preserve_ownership():
    conn = make_db()
    conn.execute("INSERT INTO decks(id,user_id) VALUES (7,11)")
    conn.execute("INSERT INTO champions(id,user_id,pet_name) VALUES (3,11,'Fox')")
    assert db_find_deck_owner(7, conn=conn) == 11
    assert db_set_champion_last_deck(3, 7, user_id=11, conn=conn) == 1
    assert db_set_champion_last_deck(3, 8, user_id=99, conn=conn) == 0
    assert tuple(db_champion_last_deck(3, conn=conn)) == (7, "Fox")
    db_insert_card_instance(11, 501, "template-guid", conn=conn)
    assert db_card_instance_template(11, 501, conn=conn) == "template-guid"


if __name__ == "__main__":
    test_inventory_consume_and_upsert_are_connection_scoped()
    test_deck_champion_and_card_instance_helpers_preserve_ownership()
    test_adjust_user_currency_is_atomic_and_connection_scoped()
    print("profile DB tests passed")
