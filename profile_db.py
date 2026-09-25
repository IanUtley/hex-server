"""Profile, economy, inventory, mail and deck persistence APIs.

This is the profile-domain entry point. The functions are re-exported from
the legacy ``db`` facade for now so callers can migrate without a flag day;
new profile/economy code should import this module directly.
"""

from datetime import datetime, timezone
import json

import db as _db_layer

def _profile_connection(conn=None):
    return conn if conn is not None else _db_layer._db


def display_name_from_identity(name):
    """Strip the hidden discriminator suffix used by client identities."""
    return name.rsplit("#", 1)[0] if name and "#" in name else name or ""


def db_next_card_instance_for_user(user_id, conn=None):
    row = _profile_connection(conn).execute(
        "SELECT COALESCE(MAX(instance_id), 5000) + 1 AS next_id "
        "FROM card_instances WHERE user_id=?", (user_id,)).fetchone()
    return int(_row_value(row, "next_id", 0)) if row else 5001


def db_insert_card_instance(user_id, instance_id, template_guid, conn=None):
    connection = _profile_connection(conn)
    connection.execute(
        "INSERT OR IGNORE INTO card_instances "
        "(user_id, instance_id, template_guid) VALUES (?,?,?)",
        (user_id, instance_id, template_guid))
    if conn is None:
        connection.commit()


def _row_value(row, field_name, index):
    """Read a field from either a named-row or a raw SQLite row."""
    try:
        return row[field_name]
    except (IndexError, KeyError, TypeError):
        return row[index]


def db_get_or_create_user(name, steam_id=None, conn=None):
    """Load or initialize one profile, including the idempotent first grant."""
    import new_player

    connection = _profile_connection(conn)
    uid = (_db_layer.player_id_from_steam(steam_id)
           if steam_id else _db_layer.player_id_from_name(name))
    row = connection.execute(
        "SELECT id, name, gold, platinum, experience, level, flags, last_login "
        "FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        row = connection.execute(
            "SELECT id, name, gold, platinum, experience, level, flags, last_login "
            "FROM users WHERE name=?", (name,)).fetchone()
    if row:
        has_cards = connection.execute(
            "SELECT COUNT(*) AS card_count FROM collections WHERE user_id=?",
            (_row_value(row, "id", 0),)).fetchone()
        if not has_cards or not _row_value(has_cards, "card_count", 0):
            try:
                new_player.grant_new_player(connection, _row_value(row, "id", 0))
            except Exception as exc:
                _db_layer.log(f"    WARN: new-player catch-up grant failed: {exc}")
        old_last_login = _row_value(row, "last_login", 7)
        daily_bonus_xp = 0
        new_xp = _row_value(row, "experience", 4)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if old_last_login and str(old_last_login)[:10] != today:
            daily_bonus_xp = 100
            new_xp = (_row_value(row, "experience", 4) or 0) + daily_bonus_xp
            connection.execute(
                "UPDATE users SET experience=?, last_login=datetime('now'), "
                "last_ip=? WHERE id=?",
                (new_xp, "127.0.0.1", _row_value(row, "id", 0)))
        else:
            connection.execute(
                "UPDATE users SET last_login=datetime('now'), last_ip=? WHERE id=?",
                ("127.0.0.1", _row_value(row, "id", 0)))
        if conn is None:
            connection.commit()
        return {
            "id": _row_value(row, "id", 0), "name": _row_value(row, "name", 1),
            "gold": _row_value(row, "gold", 2),
            "platinum": _row_value(row, "platinum", 3),
            "experience": new_xp, "level": _row_value(row, "level", 5),
            "flags": _row_value(row, "flags", 6) or "{}",
            "daily_bonus_xp": daily_bonus_xp,
        }

    connection.execute(
        "INSERT OR IGNORE INTO users (id, name, last_login, flags) "
        "VALUES (?, ?, datetime('now'), '{}')", (uid, name))
    for rarity in ("common", "uncommon", "rare", "legendary", "promo"):
        connection.execute(
            "INSERT OR IGNORE INTO stardust (user_id, rarity, quantity) "
            "VALUES (?, ?, 100)", (uid, rarity))
    try:
        new_player.grant_new_player(connection, uid)
    except Exception as exc:
        _db_layer.log(f"    WARN: new-player grant failed: {exc}")
    if conn is None:
        connection.commit()
    return {"id": uid, "name": name, "gold": new_player.STARTING_GOLD,
            "platinum": new_player.STARTING_PLATINUM, "experience": 0,
            "level": 1, "flags": "{}"}


def db_reset_account(user_id, conn=None):
    """Reset mutable account/game state and apply fresh-player grants."""
    import new_player
    connection = _profile_connection(conn)
    uid = int(user_id)
    connection.execute(
        "DELETE FROM game_sessions WHERE owner_uid=? OR players_json LIKE ? OR session_id IN "
        "(SELECT DISTINCT session_id FROM game_cards WHERE user_id=? OR owner_user_id=?)",
        (str(uid), f"%{uid}%", uid, uid))
    connection.execute(
        "DELETE FROM game_cards WHERE user_id=? OR owner_user_id=?", (uid, uid))
    for table in ("arena_state", "campaigns", "card_instances", "champions",
                  "collections", "decks", "emails", "fra_challengers",
                  "friend_requests", "friends", "ignored_players",
                  "player_inventory", "stardust", "store_purchases",
                  "treasure_chests", "user_prefs", "chat_messages",
                  "tournament_decks", "tournament_signups"):
        try:
            connection.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
        except Exception:
            pass
    connection.execute(
        "UPDATE users SET gold=0, platinum=0, experience=0, level=1, flags='{}' "
        "WHERE id=?", (uid,))
    new_player.grant_new_player(connection, uid)
    if conn is None:
        connection.commit()


# --- Social persistence ----------------------------------------------------

def db_get_friends(user_id, conn=None):
    rows = _profile_connection(conn).execute(
        "SELECT f.friend_user_id, u.name "
        "FROM friends f JOIN users u ON u.id=f.friend_user_id "
        "WHERE f.user_id=?", (user_id,)).fetchall()
    return [(row["friend_user_id"], row["name"], False) for row in rows]


def db_get_pending_friend_requests(user_id, conn=None):
    rows = _profile_connection(conn).execute(
        "SELECT u.name FROM friend_requests fr "
        "JOIN users u ON u.id=fr.from_user_id "
        "WHERE fr.to_user_id=?", (user_id,)).fetchall()
    return [row["name"] for row in rows]


def db_get_ignored_list(user_id, conn=None):
    rows = _profile_connection(conn).execute(
        "SELECT ip.ignored_user_id, u.name "
        "FROM ignored_players ip JOIN users u ON u.id=ip.ignored_user_id "
        "WHERE ip.user_id=?", (user_id,)).fetchall()
    return {row["ignored_user_id"]: row["name"] for row in rows}


def db_send_friend_request(from_user_id, to_user_name, conn=None):
    connection = _profile_connection(conn)
    to_user = connection.execute(
        "SELECT id FROM users WHERE LOWER(name)=LOWER(?)", (to_user_name,)).fetchone()
    if not to_user:
        return False, "UserDoesNotExist", None
    to_user_id = to_user["id"]
    if from_user_id == to_user_id:
        return False, "SelfAdd", None
    if connection.execute(
            "SELECT 1 FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
            (from_user_id, to_user_id)).fetchone():
        return False, "RequestAlreadySent", to_user_id
    if connection.execute(
            "SELECT 1 FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
            (to_user_id, from_user_id)).fetchone():
        return False, "RequestAlreadyReceived", to_user_id
    if connection.execute(
            "SELECT 1 FROM friends WHERE user_id=? AND friend_user_id=?",
            (from_user_id, to_user_id)).fetchone():
        return False, "RequestAlreadySent", to_user_id
    connection.execute(
        "INSERT OR IGNORE INTO friend_requests (from_user_id, to_user_id) VALUES (?,?)",
        (from_user_id, to_user_id))
    if conn is None:
        connection.commit()
    return True, "Success", to_user_id


def db_accept_friend_request(from_user_id, to_user_name, conn=None):
    connection = _profile_connection(conn)
    to_user = connection.execute(
        "SELECT id FROM users WHERE LOWER(name)=LOWER(?)", (to_user_name,)).fetchone()
    if not to_user:
        return False, None
    to_user_id = to_user["id"]
    if not connection.execute(
            "SELECT 1 FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
            (to_user_id, from_user_id)).fetchone():
        return False, to_user_id
    connection.execute(
        "DELETE FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
        (to_user_id, from_user_id))
    connection.execute(
        "INSERT OR IGNORE INTO friends (user_id, friend_user_id) VALUES (?,?)",
        (from_user_id, to_user_id))
    connection.execute(
        "INSERT OR IGNORE INTO friends (user_id, friend_user_id) VALUES (?,?)",
        (to_user_id, from_user_id))
    if conn is None:
        connection.commit()
    return True, to_user_id


def db_ignore_friend_request(user_id, from_user_name, conn=None):
    connection = _profile_connection(conn)
    from_user = connection.execute(
        "SELECT id FROM users WHERE LOWER(name)=LOWER(?)", (from_user_name,)).fetchone()
    if not from_user:
        return False, None
    from_user_id = from_user["id"]
    connection.execute(
        "DELETE FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
        (from_user_id, user_id))
    if conn is None:
        connection.commit()
    return True, from_user_id


def db_remove_friend(user_id, friend_name, conn=None):
    connection = _profile_connection(conn)
    friend = connection.execute(
        "SELECT id FROM users WHERE LOWER(name)=LOWER(?)", (friend_name,)).fetchone()
    if not friend:
        return False, None
    friend_id = friend["id"]
    connection.execute(
        "DELETE FROM friends WHERE (user_id=? AND friend_user_id=?) "
        "OR (user_id=? AND friend_user_id=?)",
        (user_id, friend_id, friend_id, user_id))
    if conn is None:
        connection.commit()
    return True, friend_id


def db_ignore_player(user_id, player_name, conn=None):
    connection = _profile_connection(conn)
    player = connection.execute(
        "SELECT id FROM users WHERE LOWER(name)=LOWER(?)", (player_name,)).fetchone()
    if not player:
        return False, None, "CouldNotIgnore"
    ignored_id = player["id"]
    if ignored_id == user_id:
        return False, None, "CouldNotIgnore"
    if connection.execute(
            "SELECT 1 FROM ignored_players WHERE user_id=? AND ignored_user_id=?",
            (user_id, ignored_id)).fetchone():
        return False, ignored_id, "AlreadyIgnored"
    connection.execute(
        "INSERT OR IGNORE INTO ignored_players (user_id, ignored_user_id) VALUES (?,?)",
        (user_id, ignored_id))
    if conn is None:
        connection.commit()
    return True, ignored_id, "Success"


def db_unignore_player(user_id, player_name, conn=None):
    connection = _profile_connection(conn)
    player = connection.execute(
        "SELECT id FROM users WHERE LOWER(name)=LOWER(?)", (player_name,)).fetchone()
    if not player:
        return False, None, "CouldNotUnignore"
    unignored_id = player["id"]
    if not connection.execute(
            "SELECT 1 FROM ignored_players WHERE user_id=? AND ignored_user_id=?",
            (user_id, unignored_id)).fetchone():
        return False, unignored_id, "AlreadyUnignored"
    connection.execute(
        "DELETE FROM ignored_players WHERE user_id=? AND ignored_user_id=?",
        (user_id, unignored_id))
    if conn is None:
        connection.commit()
    return True, unignored_id, "Success"


def db_get_user(user_id, conn=None):
    """Load an existing user without changing login/account state."""
    row = _profile_connection(conn).execute(
        "SELECT id, name, gold, platinum, experience, level, flags "
        "FROM users WHERE id=?", (int(user_id),)).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "gold": row[2],
            "platinum": row[3], "experience": row[4], "level": row[5],
            "flags": row[6]}


def db_get_user_by_client_auth_id(auth_id, conn=None):
    """Recover a user from the stable client SAuthID."""
    try:
        auth_id = int(auth_id)
        if auth_id < 45 or (auth_id - 45) % 10:
            return None
        base_id = (auth_id - 45) // 10
    except (TypeError, ValueError):
        return None
    row = _profile_connection(conn).execute(
        "SELECT id, name, gold, platinum, experience, level, flags FROM users "
        "WHERE (id & 281474976710655)=? LIMIT 1", (base_id,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "gold": row[2],
            "platinum": row[3], "experience": row[4], "level": row[5],
            "flags": row[6]}


def db_get_stardust(user_id, conn=None):
    """Return stardust quantities keyed by rarity."""
    return {row[0]: row[1] for row in _profile_connection(conn).execute(
        "SELECT rarity, quantity FROM stardust WHERE user_id=?", (user_id,))}


def db_update_resources(user_id, gold=None, platinum=None, conn=None):
    """Update selected user currencies in the caller's transaction."""
    connection = _profile_connection(conn)
    if gold is not None:
        connection.execute("UPDATE users SET gold=? WHERE id=?", (gold, user_id))
    if platinum is not None:
        connection.execute("UPDATE users SET platinum=? WHERE id=?",
                           (platinum, user_id))
    if conn is None:
        connection.commit()


def db_add_collection(user_id, template_guid, quantity=1, conn=None):
    """Add collection copies in the caller's transaction."""
    connection = _profile_connection(conn)
    row = connection.execute(
        "SELECT id FROM collections WHERE user_id=? AND card_template_id=?",
        (user_id, template_guid)).fetchone()
    if row:
        connection.execute("UPDATE collections SET quantity=quantity+? WHERE id=?",
                           (int(quantity), row[0]))
    else:
        connection.execute(
            "INSERT INTO collections (user_id, card_template_id, quantity) "
            "VALUES (?, ?, ?)", (user_id, template_guid, int(quantity)))
    if conn is None:
        connection.commit()


def db_remove_collection(user_id, template_guid, quantity=1, conn=None):
    connection = _profile_connection(conn)
    connection.execute(
        "UPDATE collections SET quantity=quantity-? "
        "WHERE user_id=? AND card_template_id=? AND quantity>=?",
        (int(quantity), user_id, template_guid, int(quantity)))
    if conn is None:
        connection.commit()


def db_reward_card_template(template_guid, conn=None):
    return _profile_connection(conn).execute(
        "SELECT name, cost, attack, defense FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()


def db_grant_card_instance(user_id, template_guid, conn=None):
    connection = _profile_connection(conn)
    db_add_collection(user_id, template_guid, 1, conn=connection)
    instance_id = db_next_card_instance_for_user(user_id, connection)
    db_insert_card_instance(user_id, instance_id, template_guid, connection)
    if conn is None:
        connection.commit()
    return instance_id


def db_update_champion_xp(champion_id, xp, level, conn=None):
    connection = _profile_connection(conn)
    connection.execute("UPDATE champions SET xp=?, level=? WHERE id=?",
                       (int(xp), int(level), champion_id))
    if conn is None:
        connection.commit()


def db_champion_reward_profile(champion_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT id, champion_name, level, xp, champion_class, race, gender, "
        "last_campaign_id, last_deck_id, is_deleted, pet_name "
        "FROM champions WHERE id=?", (champion_id,)).fetchone()


def db_add_stardust(user_id, rarity, quantity=1, conn=None):
    connection = _profile_connection(conn)
    connection.execute(
        "INSERT INTO stardust (user_id, rarity, quantity) VALUES (?,?,?) "
        "ON CONFLICT(user_id, rarity) DO UPDATE SET quantity=quantity+?",
        (user_id, rarity, quantity, quantity))
    if conn is None:
        connection.commit()


def db_pack_set_info(pack_guid, conn=None):
    return _profile_connection(conn).execute(
        "SELECT set_guid, is_full_set, is_primal FROM pack_set_map "
        "WHERE pack_guid=?", (pack_guid,)).fetchone()


def db_chest_probabilities(conn=None):
    return _profile_connection(conn).execute(
        "SELECT rarity, weight FROM chest_probabilities").fetchall()


def db_create_treasure_chest(user_id, set_guid, rarity, conn=None,
                             template_guid=None):
    connection = _profile_connection(conn)
    if template_guid is None:
        cur = connection.execute(
            "INSERT INTO treasure_chests (user_id, set_guid, chest_rarity) "
            "VALUES (?,?,?)", (user_id, set_guid, rarity))
    else:
        cur = connection.execute(
            "INSERT INTO treasure_chests "
            "(user_id, set_guid, chest_rarity, opened, template_guid) "
            "VALUES (?, ?, ?, 0, ?)",
            (user_id, set_guid, rarity, template_guid))
    if conn is None:
        connection.commit()
    return cur.lastrowid


def db_get_unopened_chests_full(user_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT id, set_guid, chest_rarity, template_guid, wof_status "
        "FROM treasure_chests WHERE user_id=? AND opened=0", (user_id,)).fetchall()


def db_get_chest_by_id(chest_db_id, user_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT id, set_guid, chest_rarity, opened, template_guid "
        "FROM treasure_chests WHERE id=? AND user_id=? AND opened=0",
        (chest_db_id, user_id)).fetchone()


def db_next_card_instance_id(conn=None):
    row = _profile_connection(conn).execute(
        "SELECT COALESCE(MAX(instance_id), 5000) + 1 AS next_id "
        "FROM card_instances").fetchone()
    return int(_row_value(row, "next_id", 0)) if row else 5001


def db_create_card_instance(user_id, instance_id, template_guid, conn=None):
    connection = _profile_connection(conn)
    connection.execute(
        "INSERT OR IGNORE INTO card_instances "
        "(user_id, instance_id, template_guid) VALUES (?,?,?)",
        (user_id, instance_id, template_guid))
    if conn is None:
        connection.commit()


def db_open_chest(chest_db_id, conn=None):
    connection = _profile_connection(conn)
    connection.execute("UPDATE treasure_chests SET opened=1 WHERE id=?",
                       (chest_db_id,))
    if conn is None:
        connection.commit()


def db_chest_template(template_guid, conn=None):
    """Return the authored metadata for one chest template.

    ``chest_templates`` is the Records-derived table of every
    ``InventoryTreasureChest`` definition, so it is the authority for deciding
    whether an inventory item is a chest the fixed client can open.
    """
    return _profile_connection(conn).execute(
        "SELECT guid, name, set_guid, chest_type, spin_type, promotional_id "
        "FROM chest_templates WHERE guid=?", (template_guid,)).fetchone()


def db_get_unopened_chests(user_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT id, template_guid FROM treasure_chests "
        "WHERE user_id=? AND opened=0", (user_id,)).fetchall()


def db_get_user_champions(user_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT id, champion_name, race, champion_class, gender, level, xp, "
        "last_deck_id, last_campaign_id, talents, pet_name FROM champions "
        "WHERE user_id=? AND is_deleted=0", (user_id,)).fetchall()


def db_get_champion_deck_match(user_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT id, champion_name FROM champions "
        "WHERE user_id=? AND is_deleted=0", (user_id,)).fetchall()


def db_add_card(user_id, template_id, conn=None):
    """Add one collection copy."""
    db_add_collection(user_id, template_id, 1, conn=conn)


def db_add_inventory(user_id, template_guid, quantity=1, conn=None):
    """Add inventory quantity in the caller's transaction."""
    connection = _profile_connection(conn)
    row = connection.execute(
        "SELECT id FROM player_inventory WHERE user_id=? AND template_guid=?",
        (user_id, template_guid)).fetchone()
    if row:
        connection.execute(
            "UPDATE player_inventory SET quantity=quantity+? WHERE id=?",
            (int(quantity), row[0]))
    else:
        connection.execute(
            "INSERT INTO player_inventory (user_id, template_guid, quantity) "
            "VALUES (?, ?, ?)", (user_id, template_guid, int(quantity)))
    if conn is None:
        connection.commit()


def db_get_inventory(user_id, conn=None):
    """Return inventory as ``(template_guid, quantity)`` pairs."""
    return [(_row_value(row, "template_guid", 0), _row_value(row, "quantity", 1))
            for row in _profile_connection(conn).execute(
        "SELECT template_guid, quantity FROM player_inventory WHERE user_id=?",
        (user_id,)).fetchall()]


def db_inventory_item(user_id, template_guid, conn=None):
    """Return one inventory item as ``(id, quantity, client_item_uid)``."""
    return _profile_connection(conn).execute(
        "SELECT id, quantity, client_item_uid FROM player_inventory "
        "WHERE user_id=? AND template_guid=? ORDER BY id LIMIT 1",
        (user_id, template_guid)).fetchone()


def db_consume_inventory(user_id, template_guid, quantity, conn=None):
    """Atomically consume inventory and return ``(client_uid, remaining)``."""
    connection = _profile_connection(conn)
    row = db_inventory_item(user_id, template_guid, conn=connection)
    if not row or int(_row_value(row, "quantity", 1) or 0) < int(quantity):
        return None
    row_id = _row_value(row, "id", 0)
    remaining = int(_row_value(row, "quantity", 1) or 0) - int(quantity)
    if remaining:
        connection.execute("UPDATE player_inventory SET quantity=? WHERE id=?",
                           (remaining, row_id))
    else:
        connection.execute("DELETE FROM player_inventory WHERE id=?", (row_id,))
    if conn is None:
        connection.commit()
    return _row_value(row, "client_item_uid", 2) or 0, remaining


def db_inventory_item_by_client_uid(user_id, client_item_uid, conn=None):
    """Return the inventory row a client addresses by its item UID."""
    return _profile_connection(conn).execute(
        "SELECT id, template_guid, quantity, client_item_uid "
        "FROM player_inventory WHERE user_id=? AND client_item_uid=? "
        "ORDER BY id LIMIT 1",
        (user_id, int(client_item_uid))).fetchone()


def db_consume_inventory_row(row_id, quantity=1, conn=None):
    """Consume quantity from one inventory row; returns the remaining count.

    Chest opening addresses a single inventory entry (its client UID), so it
    must consume by row: several entries of the same template can coexist with
    different client UIDs.
    """
    connection = _profile_connection(conn)
    row_id = int(row_id)
    row = connection.execute(
        "SELECT quantity FROM player_inventory WHERE id=?", (row_id,)).fetchone()
    if not row:
        return 0
    remaining = int(_row_value(row, "quantity", 0) or 0) - int(quantity)
    if remaining > 0:
        connection.execute(
            "UPDATE player_inventory SET quantity=? WHERE id=?",
            (remaining, row_id))
    else:
        connection.execute("DELETE FROM player_inventory WHERE id=?", (row_id,))
        remaining = 0
    if conn is None:
        connection.commit()
    return remaining


def db_next_inventory_client_uid(user_id, conn=None):
    row = _profile_connection(conn).execute(
        "SELECT COALESCE(MAX(client_item_uid), 0) + 1 AS next_uid "
        "FROM player_inventory WHERE user_id=?", (user_id,)).fetchone()
    return int(_row_value(row, "next_uid", 0) or 1)


def db_upsert_inventory_item(user_id, template_guid, quantity=1,
                             client_item_uid=0, conn=None):
    connection = _profile_connection(conn)
    row = db_inventory_item(user_id, template_guid, conn=connection)
    if row:
        row_id = _row_value(row, "id", 0)
        new_uid = _row_value(row, "client_item_uid", 2) or client_item_uid
        connection.execute(
            "UPDATE player_inventory SET quantity=?, client_item_uid=? WHERE id=?",
            (int(_row_value(row, "quantity", 1) or 0) + int(quantity), new_uid, row_id))
    else:
        new_uid = client_item_uid
        connection.execute(
            "INSERT INTO player_inventory "
            "(user_id, template_guid, quantity, client_item_uid) VALUES (?,?,?,?)",
            (user_id, template_guid, quantity, new_uid))
    if conn is None:
        connection.commit()
    return new_uid


def db_set_inventory_client_uid(user_id, template_guid, item_id, conn=None):
    _profile_connection(conn).execute(
        "UPDATE player_inventory SET client_item_uid=? "
        "WHERE user_id=? AND template_guid=? AND client_item_uid=0",
        (item_id, user_id, template_guid))
    if conn is None:
        _profile_connection(conn).commit()


# --- Champion and owned-card persistence ----------------------------------

def db_card_instance_template(user_id, instance_id, conn=None):
    row = _profile_connection(conn).execute(
        "SELECT template_guid FROM card_instances "
        "WHERE user_id=? AND instance_id=?", (user_id, instance_id)).fetchone()
    return _row_value(row, "template_guid", 0) if row else None


def db_update_champion_talents(champion_id, user_id, talents, conn=None):
    cur = _profile_connection(conn).execute(
        "UPDATE champions SET talents=? WHERE id=? AND user_id=?",
        (talents, champion_id, user_id))
    if conn is None:
        _profile_connection(conn).commit()
    return cur.rowcount


def db_champion_profile(champion_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT champion_name, race, champion_class, gender, level, "
        "last_deck_id, pet_name FROM champions WHERE id=?", (champion_id,)
    ).fetchone()


def db_delete_champion(champion_id, user_id, conn=None):
    cur = _profile_connection(conn).execute(
        "UPDATE champions SET is_deleted=1 WHERE id=? AND user_id=?",
        (champion_id, user_id))
    if conn is None:
        _profile_connection(conn).commit()
    return cur.rowcount


def db_profile_card_instances(user_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT ci.template_guid, ct.name, ct.cost, ct.attack, ct.defense, "
        "ci.instance_id, ci.is_extended_art FROM card_instances ci "
        "JOIN card_templates ct ON ct.guid=ci.template_guid "
        "WHERE ci.user_id=? ORDER BY ci.instance_id", (user_id,)
    ).fetchall()


def db_collection_card_list(user_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT ct.guid, ct.name, ct.cost, ct.attack, ct.defense, col.quantity "
        "FROM collections col JOIN card_templates ct "
        "ON ct.guid=col.card_template_id WHERE col.user_id=? ORDER BY ct.name",
        (user_id,)).fetchall()


def db_card_instance_display(user_id, instance_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT ci.template_guid, ct.card_type, ct.name, ct.cost, "
        "ct.attack, ct.defense FROM card_instances ci "
        "JOIN card_templates ct ON ci.template_guid=ct.guid "
        "WHERE ci.user_id=? AND ci.instance_id=?", (user_id, instance_id)
    ).fetchone()


def db_card_instance_art(user_id, instance_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT id, template_guid, is_extended_art FROM card_instances "
        "WHERE user_id=? AND instance_id=?", (user_id, instance_id)
    ).fetchone()


def db_set_card_instance_extended_art(user_id, instance_id, conn=None):
    cur = _profile_connection(conn).execute(
        "UPDATE card_instances SET is_extended_art=1 "
        "WHERE user_id=? AND instance_id=?", (user_id, instance_id))
    if conn is None:
        _profile_connection(conn).commit()
    return cur.rowcount


def db_get_store_items(conn=None):
    """Return the client store projection."""
    rows = _profile_connection(conn).execute(
        "SELECT template_guid, name, short_desc, price, currency, store_tab "
        "FROM store_items ORDER BY id").fetchall()
    return [{"n": row[1], "s": row[2] or "", "price": row[3],
             "currency": row[4], "template_guid": row[0], "t": row[5]}
            for row in rows]


def db_get_decks(user_id, conn=None):
    """Return saved decks in the profile projection used by the client."""
    rows = _profile_connection(conn).execute(
        "SELECT id, deck_name, cards, pve_champion_id, pvp_champion_guid, "
        "active_gems, deck_sleeve_guid, gameboard_guid, coin_guid, equipment "
        "FROM decks WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    return [{"id": row[0], "name": row[1], "cards": row[2],
             "pve_champion_id": row[3], "pvp_champion_guid": row[4],
             "active_gems": row[5], "deck_sleeve_guid": row[6],
             "gameboard_guid": row[7], "coin_guid": row[8],
             "equipment": row[9]}
            for row in rows]


def db_deck_sleeve(deck_id, user_id=None, conn=None):
    """Return the selected sleeve for a saved deck."""
    sql = "SELECT deck_sleeve_guid FROM decks WHERE id=?"
    params = [int(deck_id)]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(int(user_id))
    row = _profile_connection(conn).execute(sql, params).fetchone()
    return row[0] if row and row[0] else None


def db_find_mail_recipient(name, conn=None):
    """Find a mail recipient by full identity or display name."""
    if not name:
        return None
    name = str(name).strip()
    row = _profile_connection(conn).execute(
        "SELECT id, name FROM users WHERE LOWER(name)=LOWER(?) OR "
        "LOWER(CASE WHEN instr(name, '#') > 0 THEN substr(name, 1, "
        "instr(name, '#') - 1) ELSE name END)=LOWER(?) LIMIT 1",
        (name, name)).fetchone()
    return {"id": row[0], "name": row[1]} if row else None


def db_find_user_by_name(name, conn=None):
    """Return an exact case-insensitive user identity lookup."""
    if not name:
        return None
    return _profile_connection(conn).execute(
        "SELECT id, name FROM users WHERE LOWER(name)=LOWER(?) LIMIT 1",
        (str(name).strip(),)).fetchone()


def db_send_email(user_id, subject, body, sender="SYSTEM", gold_delivered=0,
                  platinum_delivered=0, attachments=None, conn=None):
    """Insert one mail item in the caller's transaction."""
    connection = _profile_connection(conn)
    connection.execute(
        "INSERT INTO emails (user_id, sender, subject, body, gold_delivered, "
        "platinum_delivered, attachments_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, sender, subject, body, gold_delivered, platinum_delivered,
         json.dumps(attachments or [], separators=(",", ":"))))
    if conn is None:
        connection.commit()


def db_get_unread_mail_count(user_id, conn=None):
    """Return the number of unread mail items."""
    row = _profile_connection(conn).execute(
        "SELECT COUNT(*) FROM emails WHERE user_id=? AND read_at IS NULL",
        (user_id,)).fetchone()
    return row[0] if row else 0


def db_get_mail_list(user_id, conn=None):
    """Return a user's inbox rows newest first."""
    return _profile_connection(conn).execute(
        "SELECT id, sender, subject, body, sent_at, gold_delivered, "
        "platinum_delivered, claimed_at FROM emails WHERE user_id=? "
        "ORDER BY id DESC", (user_id,)).fetchall()


def db_get_sent_mail_list(sender, conn=None):
    """Return sent mail rows with recipient names."""
    return _profile_connection(conn).execute(
        "SELECT e.id, e.sender, u.name, e.subject, e.body, e.sent_at, "
        "e.gold_delivered, e.platinum_delivered, e.claimed_at "
        "FROM emails e LEFT JOIN users u ON u.id=e.user_id "
        "WHERE LOWER(e.sender)=LOWER(?) ORDER BY e.id DESC", (sender,)
    ).fetchall()


def db_delete_sent_mail(sender, email_ids, conn=None):
    """Delete selected mail sent by a user and return affected count."""
    ids = sorted({int(email_id) for email_id in email_ids if int(email_id) > 0})
    if not ids:
        return 0
    connection = _profile_connection(conn)
    marks = ",".join("?" for _ in ids)
    cursor = connection.execute(
        "DELETE FROM emails WHERE LOWER(sender)=LOWER(?) AND id IN (" + marks + ")",
        [sender, *ids])
    if conn is None:
        connection.commit()
    return cursor.rowcount


def db_mark_all_mail_read(user_id, conn=None):
    """Mark all unread mail as read."""
    connection = _profile_connection(conn)
    connection.execute(
        "UPDATE emails SET read_at=datetime('now') WHERE user_id=? "
        "AND read_at IS NULL", (user_id,))
    if conn is None:
        connection.commit()


def db_delete_all_mail(user_id, conn=None):
    """Delete all mail owned by a user."""
    connection = _profile_connection(conn)
    connection.execute("DELETE FROM emails WHERE user_id=?", (user_id,))
    if conn is None:
        connection.commit()


def db_get_mail_by_id(email_id, user_id, conn=None):
    """Return delivery fields for one user's mail item."""
    return _profile_connection(conn).execute(
        "SELECT id, gold_delivered, platinum_delivered, claimed_at FROM emails "
        "WHERE id=? AND user_id=?", (email_id, user_id)).fetchone()


def db_mark_mail_read(user_id, conn=None):
    """Mark unread mail read in the caller's transaction."""
    _profile_connection(conn).execute(
        "UPDATE emails SET read_at=datetime('now') WHERE user_id=? "
        "AND read_at IS NULL", (user_id,))


def db_delete_mail(user_id, conn=None):
    """Delete a user's mail in the caller's transaction."""
    _profile_connection(conn).execute("DELETE FROM emails WHERE user_id=?", (user_id,))


def db_claim_mail_for_user(user_id, email_id, conn=None):
    """Credit and claim one mail item atomically."""
    connection = _profile_connection(conn)
    row = connection.execute(
        "SELECT gold_delivered, platinum_delivered, attachments_json, claimed_at FROM emails "
        "WHERE id=? AND user_id=?", (email_id, user_id)).fetchone()
    if not row or row[3]:
        return None
    gold, platinum = row[0] or 0, row[1] or 0
    try:
        attachments = json.loads(row[2] or "[]")
    except (TypeError, ValueError):
        attachments = []
    cards = []
    for attachment in attachments:
        if not isinstance(attachment, dict) or attachment.get("type") != "CARD":
            continue
        template_guid = str(attachment.get("template") or "")
        quantity = max(1, int(attachment.get("quantity", 1) or 1))
        if not db_reward_card_template(template_guid, conn=connection):
            continue
        for _ in range(quantity):
            cards.append({
                "template": template_guid,
                "instance_id": db_grant_card_instance(
                    user_id, template_guid, conn=connection),
            })
    connection.execute(
        "UPDATE users SET gold=gold+?, platinum=platinum+? WHERE id=?",
        (gold, platinum, user_id))
    connection.execute("UPDATE emails SET claimed_at=datetime('now') WHERE id=?",
                       (email_id,))
    if conn is None:
        connection.commit()
    return {"gold": gold, "platinum": platinum, "cards": cards}


def db_claim_mail(email_id, conn=None):
    """Mark one mail item claimed in the caller's transaction."""
    connection = _profile_connection(conn)
    connection.execute("UPDATE emails SET claimed_at=datetime('now') WHERE id=?",
                       (email_id,))
    if conn is None:
        connection.commit()


def db_campaign_deck_for_champion(user_id, champion_id, conn=None):
    """Return the newest saved campaign deck for an owned champion."""
    import db as _db_layer
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT id FROM decks WHERE user_id=? AND pve_champion_id=? "
        "AND LOWER(deck_name) LIKE '%campaign deck%' "
        "ORDER BY id DESC LIMIT 1", (int(user_id), int(champion_id))).fetchone()


def db_insert_champion(user_id, champion_name, race, champion_class, gender,
                       pet_name, talents, conn=None):
    """Create a champion profile and return its generated ID."""
    import db as _db_layer
    connection = conn or _db_layer._db
    cur = connection.execute(
        "INSERT INTO champions (user_id, champion_name, race, champion_class, "
        "gender, pet_name, talents) VALUES (?,?,?,?,?,?,?)",
        (int(user_id), champion_name, int(race), int(champion_class),
         int(gender), pet_name, talents))
    return cur.lastrowid


def db_purchase_exists(user_id, item_template_id, conn=None):
    """Whether an account already owns a one-time store grant."""
    import db as _db_layer
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT 1 FROM store_purchases WHERE user_id=? AND item_template_id=?",
        (int(user_id), item_template_id)).fetchone() is not None


def db_latest_champion_for_user(user_id, conn=None):
    """Return the newest non-deleted champion and linked deck."""
    import db as _db_layer
    return (conn or _db_layer._db).execute(
        "SELECT id, last_deck_id FROM champions WHERE user_id=? "
        "AND is_deleted=0 ORDER BY id DESC LIMIT 1", (int(user_id),)).fetchone()


def db_latest_deck_for_user(user_id, conn=None):
    """Return the newest non-deleted saved deck ID for a profile."""
    import db as _db_layer
    row = (conn or _db_layer._db).execute(
        "SELECT id FROM decks WHERE user_id=? AND is_deleted=0 "
        "ORDER BY id DESC LIMIT 1", (int(user_id),)).fetchone()
    return row[0] if row else 0


def db_initialize_new_player(user_id, gold, platinum, conn=None):
    """Set the starting currencies for a newly-created player."""
    import db as _db_layer
    return (conn or _db_layer._db).execute(
        "UPDATE users SET gold=?, platinum=? WHERE id=?",
        (int(gold), int(platinum), int(user_id)))


def db_adjust_user_currency(user_id, gold_delta=0, platinum_delta=0, conn=None):
    """Atomically adjust a user's balances and return the new balances.

    The caller owns the transaction when ``conn`` is supplied.  Deltas are
    applied in SQL so concurrent rewards cannot overwrite one another after a
    stale read.
    """
    import db as _db_layer
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE users SET gold=gold+?, platinum=platinum+? WHERE id=?",
        (int(gold_delta), int(platinum_delta), int(user_id)))
    row = connection.execute(
        "SELECT gold, platinum FROM users WHERE id=?", (int(user_id),)
    ).fetchone()
    if conn is None:
        connection.commit()
    return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)


def db_next_card_instance_id(user_id, minimum=5000, conn=None):
    """Return the next safe physical-card instance ID for a user."""
    import db as _db_layer
    row = (conn or _db_layer._db).execute(
        "SELECT MAX(instance_id) FROM card_instances WHERE user_id=?",
        (int(user_id),)).fetchone()
    return max((row[0] + 1) if row and row[0] else 0, int(minimum))


def db_grant_collection_cards(user_id, template_guid, quantity, conn=None):
    """Increase a collection row or create it for a new template."""
    import db as _db_layer
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT id, quantity FROM collections WHERE user_id=? AND card_template_id=?",
        (int(user_id), template_guid)).fetchone()
    if row:
        return connection.execute(
            "UPDATE collections SET quantity=? WHERE id=?",
            (int(row[1]) + int(quantity), row[0]))
    return connection.execute(
        "INSERT INTO collections (user_id, card_template_id, quantity) VALUES (?,?,?)",
        (int(user_id), template_guid, int(quantity)))


def db_insert_collection_card_instances(user_id, instance_start, template_guid,
                                        quantity, conn=None):
    """Insert one physical card row per granted collection card."""
    import db as _db_layer
    connection = conn or _db_layer._db
    for instance_id in range(int(instance_start), int(instance_start) + int(quantity)):
        connection.execute(
            "INSERT OR IGNORE INTO card_instances "
            "(user_id, instance_id, template_guid) VALUES (?,?,?)",
            (int(user_id), instance_id, template_guid))
    return int(instance_start) + int(quantity)


def db_grant_inventory_item(user_id, template_guid, quantity, conn=None):
    """Increase an inventory row or create it for a new item template."""
    import db as _db_layer
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT id, quantity FROM player_inventory WHERE user_id=? AND template_guid=?",
        (int(user_id), template_guid)).fetchone()
    if row:
        return connection.execute(
            "UPDATE player_inventory SET quantity=? WHERE id=?",
            (int(row[1]) + int(quantity), row[0]))
    return connection.execute(
        "INSERT INTO player_inventory (user_id, template_guid, quantity) VALUES (?,?,?)",
        (int(user_id), template_guid, int(quantity)))


def db_auth_user_by_id(user_id, conn=None):
    """Return the auth fields for a profile keyed by numeric ID."""
    import db as _db_layer
    return (conn or _db_layer._db).execute(
        "SELECT id, password_hash, flags FROM users WHERE id=?",
        (int(user_id),)).fetchone()


def db_auth_user_by_name(name, conn=None):
    """Return the auth fields for a profile keyed by display name."""
    import db as _db_layer
    return (conn or _db_layer._db).execute(
        "SELECT id, password_hash, flags FROM users WHERE name=?",
        (name,)).fetchone()


def db_create_auth_user(user_id, name, password_hash, email=None, conn=None):
    """Create an auth profile with the proxy's default starting values."""
    import db as _db_layer
    return (conn or _db_layer._db).execute(
        "INSERT OR IGNORE INTO users "
        "(id, name, gold, platinum, last_login, flags, password_hash, email, created_at) "
        "VALUES (?, ?, 10000, 10000, datetime('now'), '{}', ?, ?, datetime('now'))",
        (int(user_id), name, password_hash, email))


def db_set_auth_password(user_id, password_hash, conn=None):
    """Store an auth password hash without committing the caller's transaction."""
    import db as _db_layer
    return (conn or _db_layer._db).execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (password_hash, int(user_id)))


def db_set_auth_flags(user_id, flags_json, conn=None):
    """Store serialized auth flags without committing the caller's transaction."""
    import db as _db_layer
    return (conn or _db_layer._db).execute(
        "UPDATE users SET flags=? WHERE id=?",
        (flags_json, int(user_id)))


def db_card_instance_for_encoded_deck(user_id, instance_id, conn=None):
    """Return template and extended-art state for encoded deck output."""
    return db_card_instance_art(user_id, instance_id, conn=conn)


def db_get_user_currency(user_id, currency, conn=None):
    if currency not in {"gold", "platinum"}:
        raise ValueError(f"unsupported currency: {currency}")
    row = _profile_connection(conn).execute(
        f"SELECT {currency} FROM users WHERE id=?", (user_id,)).fetchone()
    return _row_value(row, currency, 0) if row else 0


def db_set_user_currency(user_id, currency, value, conn=None):
    if currency not in {"gold", "platinum"}:
        raise ValueError(f"unsupported currency: {currency}")
    connection = _profile_connection(conn)
    connection.execute(f"UPDATE users SET {currency}=? WHERE id=?",
                       (value, user_id))
    if conn is None:
        connection.commit()


def db_save_deck(user_id, deck_name, cards_json="[]", pve_champion_id=None,
                 pvp_champion_guid=None, active_gems_json="{}",
                 gem_abilities_json="{}", deck_sleeve_guid=None,
                 gameboard_guid=None, coin_guid=None, conn=None):
    connection = _profile_connection(conn)
    connection.execute(
        "INSERT INTO decks (user_id, deck_name, cards, pve_champion_id, "
        "pvp_champion_guid, active_gems, gem_abilities, deck_sleeve_guid, "
        "gameboard_guid, coin_guid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, deck_name, cards_json, pve_champion_id, pvp_champion_guid,
         active_gems_json, gem_abilities_json, deck_sleeve_guid,
         gameboard_guid, coin_guid))
    if conn is None:
        connection.commit()
    row = connection.execute("SELECT last_insert_rowid() AS deck_id").fetchone()
    return int(_row_value(row, "deck_id", 0))


def db_update_deck(deck_id, user_id, deck_name=None, cards_json=None,
                   pve_champion_id=None, pvp_champion_guid=None,
                   active_gems_json=None, gem_abilities_json=None,
                   deck_sleeve_guid=None, gameboard_guid=None, coin_guid=None,
                   equipment_json=None, conn=None):
    values = (("deck_name", deck_name), ("cards", cards_json),
              ("pve_champion_id", pve_champion_id),
              ("pvp_champion_guid", pvp_champion_guid),
              ("active_gems", active_gems_json),
              ("gem_abilities", gem_abilities_json),
              ("deck_sleeve_guid", deck_sleeve_guid),
              ("gameboard_guid", gameboard_guid), ("coin_guid", coin_guid),
              ("equipment", equipment_json))
    selected = [(column, value) for column, value in values if value is not None]
    if not selected:
        return False
    connection = _profile_connection(conn)
    clauses = [f"{column}=?" for column, _ in selected]
    params = [value for _, value in selected] + [deck_id, user_id]
    clauses.append("last_saved=datetime('now')")
    connection.execute(f"UPDATE decks SET {', '.join(clauses)} WHERE id=? AND user_id=?",
                       params)
    if conn is None:
        connection.commit()
    return True


def db_get_deck_by_id(deck_id, conn=None):
    row = _profile_connection(conn).execute(
        "SELECT id, deck_name, cards, pve_champion_id, pvp_champion_guid, "
        "active_gems, deck_sleeve_guid, gameboard_guid, coin_guid, equipment "
        "FROM decks WHERE id=?", (deck_id,)).fetchone()
    if not row:
        return None
    fields = ("id", "deck_name", "cards", "pve_champion_id",
              "pvp_champion_guid", "active_gems", "deck_sleeve_guid",
              "gameboard_guid", "coin_guid", "equipment")
    return {field: _row_value(row, field, index)
            for index, field in enumerate(fields)}


def db_deck_equipment(deck_id, conn=None):
    """Return a deck's equipped item GUIDs."""
    row = _profile_connection(conn).execute(
        "SELECT equipment FROM decks WHERE id=?", (int(deck_id),)).fetchone()
    try:
        value = json.loads(_row_value(row, "equipment", 0) or "[]") if row else []
    except (TypeError, ValueError):
        return []
    return [str(guid).lower() for guid in value] if isinstance(value, list) else []


def db_find_deck_owner(deck_id, conn=None):
    row = _profile_connection(conn).execute(
        "SELECT user_id FROM decks WHERE id=?", (deck_id,)).fetchone()
    return _row_value(row, "user_id", 0) if row else None


def db_user_owns_deck(deck_id, user_id, conn=None):
    return bool(_profile_connection(conn).execute(
        "SELECT 1 FROM decks WHERE id=? AND user_id=? LIMIT 1",
        (deck_id, user_id)).fetchone())


def db_deck_champion_name(deck_id, conn=None):
    row = _profile_connection(conn).execute(
        "SELECT ch.champion_name FROM decks d JOIN champions ch "
        "ON ch.id=d.pve_champion_id WHERE d.id=?", (deck_id,)).fetchone()
    return _row_value(row, "champion_name", 0) if row else None


def db_set_champion_last_deck(champion_id, deck_id, user_id=None, conn=None):
    connection = _profile_connection(conn)
    predicate = "id=?"
    params = [deck_id, champion_id]
    if user_id is not None:
        predicate += " AND user_id=?"
        params.append(user_id)
    cur = connection.execute("UPDATE champions SET last_deck_id=? WHERE " + predicate,
                             params)
    if conn is None:
        connection.commit()
    return cur.rowcount


def db_champion_last_deck(champion_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT last_deck_id, pet_name FROM champions WHERE id=?",
        (champion_id,)).fetchone()


def db_record_purchase(user_id, item_name, template_id, price, currency, conn=None):
    connection = _profile_connection(conn)
    connection.execute(
        "INSERT INTO store_purchases "
        "(user_id, item_name, item_template_id, price, currency) VALUES (?, ?, ?, ?, ?)",
        (user_id, item_name, template_id, price, currency))
    if conn is None:
        connection.commit()


def db_redeem_code(code, conn=None):
    connection = _profile_connection(conn)
    row = connection.execute(
        "SELECT id, gold_delta, platinum_delta, uses, max_uses "
        "FROM redeem_codes WHERE code=?", (code,)).fetchone()
    if not row:
        return {"redeemed": False, "error_message": "Invalid redeem code."}
    if _row_value(row, "uses", 3) >= _row_value(row, "max_uses", 4):
        return {"redeemed": False,
                "error_message": "This redeem code has already been redeemed or expired."}
    connection.execute("UPDATE redeem_codes SET uses=uses+1 WHERE id=?",
                       (_row_value(row, "id", 0),))
    if conn is None:
        connection.commit()
    return {"code": str(code),
            "gold": _row_value(row, "gold_delta", 1),
            "platinum": _row_value(row, "platinum_delta", 2),
            "redeemed": True}


def db_get_store_item(item_id, conn=None):
    return _profile_connection(conn).execute(
        "SELECT name, price, currency, template_guid, store_tab "
        "FROM store_items WHERE id=?", (int(item_id),)).fetchone()


def db_store_item_name_for_template(template_guid, conn=None):
    row = _profile_connection(conn).execute(
        "SELECT name FROM store_items WHERE template_guid=?",
        (template_guid,)).fetchone()
    return _row_value(row, "name", 0) if row else None


def db_primal_pack_for(pack_guid, conn=None):
    connection = _profile_connection(conn)
    row = connection.execute(
        "SELECT set_guid, is_full_set, is_primal FROM pack_set_map "
        "WHERE pack_guid=?", (pack_guid,)).fetchone()
    if not row or _row_value(row, "is_full_set", 1) or _row_value(row, "is_primal", 2):
        return None
    primal = connection.execute(
        "SELECT pack_guid FROM pack_set_map "
        "WHERE set_guid=? AND is_primal=1 LIMIT 1",
        (_row_value(row, "set_guid", 0),)).fetchone()
    return _row_value(primal, "pack_guid", 0) if primal else None

__all__ = [name for name in globals() if name.startswith("db_")]
