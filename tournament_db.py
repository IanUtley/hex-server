"""Tournament and bracket persistence API."""

import json
import random

import db as _db_layer

from db import (
    db_tournament_types, db_tournament_list,
    db_tournament_completed_for_player, db_tournament_by_id,
    db_tournament_create, db_tournament_update_players,
    db_tournament_set_status, db_tournament_count_by_status,
    db_tournament_close_orphaned_started, db_tournament_cleanup_old,
    db_tournament_count_active_by_type, db_tournament_next_id,
    db_tournament_deck_create, db_tournament_deck_by_player,
    db_tournament_signup_add, db_tournament_signup_by_player,
    db_tournament_signups_by_tournament, db_tournament_signup_set_status,
    db_tournament_players_name_map, db_tournament_matches,
    db_tournament_match_start, db_tournament_match_result,
    db_get_replay_match, db_get_tournament_signup_names,
)


def db_tournament_room_for_game(tournament_id, conn=None):
    """Return the joined tournament/type row used to start a game."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT t.*, tt.name AS type_name, tt.style, tt.format, "
        "tt.min_players, tt.max_players, tt.games_count, tt.set_id "
        "FROM tournaments t JOIN tournament_types tt ON t.type_id = tt.id "
        "WHERE t.id=? LIMIT 1", (tournament_id,)).fetchone()
    if not row:
        return None
    keys = ["id", "type_id", "status", "players_json", "session_id",
            "created_at", "type_name", "style", "format", "min_players",
            "max_players", "games_count", "set_id"]
    return dict(zip(keys, row))


def db_tournament_game_deck(deck_id, conn=None):
    """Return the deck fields required to materialize tournament cards."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT cards, pvp_champion_guid, user_id, active_gems "
        "FROM decks WHERE id=?", (deck_id,)).fetchone()
    if not row:
        return None
    return {
        "cards": row[0] or "[]", "champion_guid": row[1] or "",
        "owner_user_id": row[2], "active_gems": row[3] or "{}",
    }


def db_seed_tournament_game_deck(session_id, player_uid, deck_id, conn=None):
    """Materialize one tournament deck into authoritative ``game_cards``.

    This keeps deck-instance resolution, socketed gem abilities, template
    backfill, and the card UID sequence in the tournament persistence boundary.
    The caller owns the surrounding game/session orchestration.
    """
    connection = conn or _db_layer._db
    deck = db_tournament_game_deck(deck_id, connection)
    if not deck:
        return {"inserted": 0, "skipped_int": 0, "skipped_invalid": 0,
                "skipped_error": 0, "champion_guid": ""}
    try:
        active_gems = json.loads(deck["active_gems"] or "{}")
    except (TypeError, ValueError):
        active_gems = {}
    card_guids = json.loads(deck["cards"]) if isinstance(deck["cards"], str) else list(deck["cards"])
    random.shuffle(card_guids)

    gem_ability_by_instance = {}
    for instance_key, gem_type in (active_gems or {}).items():
        try:
            gem_type = int(gem_type)
        except (TypeError, ValueError):
            continue
        if gem_type <= 0:
            continue
        row = connection.execute(
            "SELECT abilities_json FROM gem_templates WHERE gem_type=?",
            (gem_type,)).fetchone()
        if not row or not row[0]:
            continue
        try:
            abilities = json.loads(row[0])
        except (TypeError, ValueError):
            abilities = []
        if abilities:
            gem_ability_by_instance[str(instance_key)] = [
                str(ability).lower() for ability in abilities]

    counts = {"inserted": 0, "skipped_int": 0, "skipped_invalid": 0,
              "skipped_error": 0, "champion_guid": deck["champion_guid"]}
    for position, original_guid in enumerate(card_guids):
        instance_key = None
        template_guid = original_guid
        if isinstance(original_guid, (int, float)):
            instance_key = str(int(original_guid))
            resolved = connection.execute(
                "SELECT template_guid FROM card_instances "
                "WHERE instance_id=? AND user_id=?",
                (int(original_guid), deck["owner_user_id"])).fetchone()
            if resolved:
                template_guid = resolved[0]
            else:
                counts["skipped_int"] += 1
                continue
        if not isinstance(template_guid, str) or len(template_guid) != 36:
            counts["skipped_invalid"] += 1
            continue
        max_uid = connection.execute(
            "SELECT COALESCE(MAX(card_uid), 0) FROM game_cards "
            "WHERE session_id=?", (session_id,)).fetchone()[0]
        card_uid = int(max_uid) + 256 if max_uid else 257
        try:
            connection.execute(
                "INSERT INTO game_cards (session_id, user_id, card_uid, "
                "template_guid, card_template_id, location, position) "
                "VALUES (?, ?, ?, ?, ?, 'deck', ?)",
                (session_id, player_uid, card_uid, template_guid,
                 original_guid, position))
            template = connection.execute(
                "SELECT card_type, abilities_json, attributes "
                "FROM card_templates WHERE guid=?", (template_guid,)).fetchone()
            if template:
                card_type, abilities_json, attributes = template
                try:
                    abilities = json.loads(abilities_json or "[]")
                except (TypeError, ValueError):
                    abilities = []
                for ability in gem_ability_by_instance.get(instance_key, []):
                    if ability not in abilities:
                        abilities.append(ability)
                connection.execute(
                    "UPDATE game_cards SET card_type=?, card_abilities=?, "
                    "card_attributes=?, gems=?, original_template_guid = CASE "
                    "WHEN COALESCE(original_template_guid,'')='' THEN ? "
                    "ELSE original_template_guid END "
                    "WHERE session_id=? AND card_uid=?",
                    (card_type or "Unknown", json.dumps(abilities),
                     int(attributes or 0),
                     int(active_gems.get(instance_key, 0) or 0)
                     if instance_key else 0,
                     template_guid, session_id, card_uid))
            counts["inserted"] += 1
        except Exception:
            counts["skipped_error"] += 1
    connection.commit()
    return counts


def db_insert_tournament_champion_card(session_id, player_uid, champion_guid,
                                        conn=None):
    """Create the session-card row representing a tournament champion."""
    connection = conn or _db_layer._db
    max_uid = connection.execute(
        "SELECT COALESCE(MAX(card_uid), 0) FROM game_cards "
        "WHERE session_id=?", (session_id,)).fetchone()[0]
    card_uid = int(max_uid) + 256 if max_uid else 257
    connection.execute(
        "INSERT INTO game_cards (session_id, user_id, card_uid, "
        "template_guid, card_template_id, card_type, location, position, "
        "is_champion) VALUES (?, ?, ?, ?, ?, 'Champion', 'champion', 0, 1)",
        (session_id, player_uid, card_uid, champion_guid, champion_guid))
    connection.commit()
    return card_uid


def db_random_card_guids_for_set(set_id, count, conn=None):
    """Return up to ``count`` eligible PVP cards from a set."""
    connection = conn or _db_layer._db
    rows = connection.execute(
        "SELECT guid FROM card_templates WHERE set_id=? AND rarity "
        "IN ('Common','Uncommon','Rare','Legendary') AND is_pve=0 AND no_pvp=0",
        (set_id,)).fetchall()
    guids = [row[0] for row in rows]
    random.shuffle(guids)
    return guids[:max(0, min(int(count), len(guids)))]

__all__ = [name for name in globals() if name.startswith("db_")]
