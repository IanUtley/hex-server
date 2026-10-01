"""Focused tests for profile-domain persistence helpers."""

import os
import sqlite3
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

from profile_db import (db_card_instance_template, db_champion_last_deck,
                        db_consume_inventory, db_find_deck_owner,
                        db_insert_card_instance, db_reset_account,
                        db_set_champion_last_deck, db_upsert_inventory_item,
                        db_adjust_user_currency, db_get_or_create_user,
                        WELCOME_MAIL_BODY, WELCOME_MAIL_SUBJECT)


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, gold INTEGER DEFAULT 0,
            platinum INTEGER DEFAULT 0, experience INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1, flags TEXT DEFAULT '{}',
            name TEXT, last_login TEXT, last_ip TEXT
        );
        CREATE TABLE emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            sender TEXT, subject TEXT, body TEXT,
            gold_delivered INTEGER DEFAULT 0,
            platinum_delivered INTEGER DEFAULT 0,
            attachments_json TEXT DEFAULT '[]', read_at TEXT,
            claimed_at TEXT
        );
        CREATE TABLE stardust (
            user_id INTEGER, rarity TEXT, quantity INTEGER,
            PRIMARY KEY (user_id, rarity)
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            instance_id INTEGER, template_guid TEXT,
            is_extended_art INTEGER DEFAULT 0
        );
        CREATE TABLE card_templates (
            guid TEXT PRIMARY KEY, is_pve INTEGER DEFAULT 0
        );
        CREATE TABLE game_sessions (
            session_id INTEGER PRIMARY KEY, owner_uid TEXT,
            players_json TEXT
        );
        CREATE TABLE game_cards (
            user_id INTEGER, owner_user_id INTEGER, session_id INTEGER
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


def test_reset_account_keeps_pve_and_extended_art_cards():
    """!account-cleanup removes only plain PvP copies from the collection.

    PvE printings and extended-art (alternate art) PvP copies are permanent
    account rewards; the reset must leave both their instances and their
    matching ``collections`` counts in place.
    """
    conn = make_db()
    conn.execute(
        "INSERT INTO users(id, gold, platinum, experience, level, flags) "
        "VALUES (1, 5, 6, 7, 3, '{\"x\":1}')")
    conn.executemany(
        "INSERT INTO card_templates(guid, is_pve) VALUES (?,?)",
        [("pvp-plain", 0), ("pvp-alt", 0), ("pve-card", 1)])
    conn.executemany(
        "INSERT INTO card_instances(user_id, instance_id, template_guid, "
        "is_extended_art) VALUES (1,?,?,?)",
        [(10, "pvp-plain", 0), (11, "pvp-plain", 0), (12, "pvp-alt", 1),
         (13, "pve-card", 0)])
    conn.executemany(
        "INSERT INTO collections(user_id, card_template_id, quantity) "
        "VALUES (1,?,?)",
        [("pvp-plain", 2), ("pvp-alt", 1), ("pve-card", 1)])

    db_reset_account(1, conn=conn)

    def instances(template):
        return conn.execute(
            "SELECT instance_id, is_extended_art FROM card_instances "
            "WHERE user_id=1 AND template_guid=? ORDER BY instance_id",
            (template,)).fetchall()

    def quantity(template):
        row = conn.execute(
            "SELECT quantity FROM collections "
            "WHERE user_id=1 AND card_template_id=?", (template,)).fetchone()
        return row[0] if row else None

    assert instances("pvp-plain") == []
    assert instances("pvp-alt") == [(12, 1)]
    assert instances("pve-card") == [(13, 0)]
    assert quantity("pvp-plain") is None
    assert quantity("pvp-alt") == 1
    assert quantity("pve-card") == 1
    # The rest of the reset still runs (currency back to the grant).
    assert conn.execute(
        "SELECT gold, platinum FROM users WHERE id=1").fetchone() == (
        10000, 10000)


def test_new_user_receives_only_public_command_welcome_mail():
    conn = make_db()
    with mock.patch("profile_db._db_layer.player_id_from_name",
                    return_value=77), \
            mock.patch("new_player.grant_new_player"):
        db_get_or_create_user("NewPlayer", conn=conn)
        db_get_or_create_user("NewPlayer", conn=conn)

    row = conn.execute(
        "SELECT subject, body FROM emails WHERE user_id=77").fetchone()
    assert row == (WELCOME_MAIL_SUBJECT, WELCOME_MAIL_BODY)
    assert conn.execute(
        "SELECT COUNT(*) FROM emails WHERE user_id=77").fetchone()[0] == 1
    assert "!help" in row[1]
    assert "!issue <title>" in row[1]
    assert "!gencard" not in row[1]
    assert "allowcon" not in row[1]
    assert "!greenlight" not in row[1]


if __name__ == "__main__":
    test_inventory_consume_and_upsert_are_connection_scoped()
    test_deck_champion_and_card_instance_helpers_preserve_ownership()
    test_adjust_user_currency_is_atomic_and_connection_scoped()
    test_reset_account_keeps_pve_and_extended_art_cards()
    test_new_user_receives_only_public_command_welcome_mail()
    print("profile DB tests passed")
