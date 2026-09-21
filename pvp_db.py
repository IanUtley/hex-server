"""Shared game-session, card and PVP persistence API.

Practice, campaign battles and tournament PVP all use the same game-session
and card tables. This module groups that shared storage boundary; it is not a
second database file.
"""

import db as _db_layer
import json
from domain.constants import DEFAULT_STARTING_HEALTH, PLAYED_CARD_POSITION
from domain.enums import ECardStates, ECardTypes, ETurnPhases

def db_next_session_instance(conn=None):
    """Atomically allocate the next persisted game-session instance."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT value FROM meta WHERE key='next_session_inst'").fetchone()
    nxt = (row[0] + 1) if row else 1
    connection.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('next_session_inst', ?)",
        (nxt,))
    return nxt


def db_save_session(session, conn=None):
    """Persist a game-session object in the caller's transaction."""
    connection = conn or _db_layer._db
    connection.execute(
        "INSERT OR REPLACE INTO game_sessions "
        "(session_id, server_id, session_name, owner_uid, state, "
        "encounter_data, players_json, turn_order_json, seed_z, seed_w, "
        "deck_template_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, COALESCE(("
        "SELECT created_at FROM game_sessions WHERE session_id=?), datetime('now')))",
        (str(session.session_id), str(session.server_id), session.session_name,
         str(session.owner_uid), session.state, json.dumps(session.encounter_data),
         json.dumps(session.players), json.dumps(session.turn_order), session.seed_z,
         session.seed_w, session.deck_template_id, str(session.session_id)))


def db_get_session(session_id, conn=None):
    """Return one persisted game session by protocol UID."""
    return (conn or _db_layer._db).execute(
        "SELECT * FROM game_sessions WHERE session_id=?", (str(session_id),)
    ).fetchone()


def db_get_session_by_name(session_name, conn=None):
    """Return one persisted game session by client-visible name."""
    return (conn or _db_layer._db).execute(
        "SELECT * FROM game_sessions WHERE session_name=?", (session_name,)
    ).fetchone()


def db_get_sessions(conn=None):
    """Return persisted sessions newest first."""
    return (conn or _db_layer._db).execute(
        "SELECT * FROM game_sessions ORDER BY created_at DESC").fetchall()


def db_remove_session(session_name, conn=None):
    """Remove a game session in the caller's transaction."""
    (conn or _db_layer._db).execute(
        "DELETE FROM game_sessions WHERE session_name=?", (session_name,))


def db_cleanup_ended_sessions(conn=None):
    """Remove game sessions that reached the terminal state."""
    (conn or _db_layer._db).execute(
        "DELETE FROM game_sessions WHERE state='ended'")


def db_insert_game_card(session_id, user_id, card_uid, template_guid, location,
                        card_type="Troop", position=0, abilities_json=None,
                        attributes=0, is_champion=0, resolved_at=0, conn=None):
    """Insert a materialized game card and preserve its original owner."""
    connection = conn or _db_layer._db
    cursor = connection.execute(
        "INSERT INTO game_cards (session_id, user_id, card_uid, template_guid, "
        "card_template_id, location, position, card_type, card_abilities, "
        "card_attributes, owner_user_id, is_champion, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, user_id, card_uid, template_guid, template_guid,
         location, position, card_type, abilities_json or "[]",
         attributes or 0, user_id or 0, is_champion, resolved_at))
    if conn is None:
        connection.commit()
    return cursor.lastrowid


def db_discard_card(session_id, card_uid, owner_user_id=None,
                    extra_set=None, extra_params=None, connection=None,
                    conn=None):
    """Move a card to its owner's discard pile.

    This keeps the legacy return value and trusted extra-update contract, but
    leaves explicit transactions for the caller to commit.
    """
    explicit = connection is not None or conn is not None
    connection = connection or conn or _db_layer._db
    row = connection.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row:
        return None
    owner = int(row[0] if owner_user_id is None else owner_user_id)
    position = connection.execute(
        "SELECT COALESCE(MAX(position) + 1, 1) FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='discard'",
        (session_id, owner)).fetchone()[0]
    sql = "UPDATE game_cards SET user_id=?, location='discard', position=?"
    params = [owner, int(position or 1)]
    if extra_set:
        sql += ", " + extra_set
        params.extend(extra_params or [])
    sql += " WHERE session_id=? AND card_uid=?"
    params.extend([session_id, int(card_uid)])
    connection.execute(sql, params)
    if not explicit:
        connection.commit()
    return owner


def db_move_cards_to_hand(session_id, card_uids, conn=None):
    """Move selected cards to hand, leaving transaction ownership explicit."""
    connection = conn or _db_layer._db
    connection.executemany(
        "UPDATE game_cards SET location='hand', position=100 "
        "WHERE card_uid=? AND session_id=?",
        [(int(uid), session_id) for uid in (card_uids or [])],
    )
    if conn is None:
        connection.commit()


def db_card_template_field(template_guid, field, conn=None):
    """Return one approved printed template field by name."""
    valid = {"abilities_json", "attributes", "card_type", "sacrifice_target",
             "cost", "attack", "defense", "name", "threshold_json", "lethal"}
    if field not in valid:
        return None
    row = (conn or _db_layer._db).execute(
        "SELECT " + field + " FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else None


def db_ability_effects(ability_guid, conn=None):
    """Return the BOM effect GUIDs belonging to an ability."""
    rows = (conn or _db_layer._db).execute(
        "SELECT effect_guid FROM ability_effects WHERE ability_guid=?",
        (ability_guid,)).fetchall()
    return [row[0] for row in rows]


def db_game_card_effective_cost(session_id, card_uid, battle_state=None,
                                conn=None):
    """Return the payable cost including instance/static cost modifiers."""
    connection = conn or _db_layer._db
    try:
        from rules_port.static_rules import effective_cost
        return max(0, int(effective_cost(
            connection, session_id, battle_state or {}, int(card_uid))))
    except Exception:
        if (battle_state or {}).get("_rules_port_attached"):
            raise
        try:
            row = connection.execute(
                "SELECT ct.cost, gc.card_cost_mod "
                "FROM game_cards gc JOIN card_templates ct "
                "ON ct.guid=gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (session_id, int(card_uid))).fetchone()
        except Exception:
            row = connection.execute(
                "SELECT ct.cost FROM game_cards gc "
                "JOIN card_templates ct ON ct.guid=gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (session_id, int(card_uid))).fetchone()
        if not row:
            return 0
        return max(0, int(row[0] or 0) +
                   (int(row[1] or 0) if len(row) > 1 else 0))


def db_game_card_effective_attributes(session_id, card_uid, battle_state=None,
                                      conn=None):
    """Return the attribute bits that gate a card's combat legality.

    ``game_cards.card_attributes`` holds only the attributes written directly
    on the instance.  Template attributes, temporary grants (a troop that
    surfaced this turn carries Speed here) and static modifiers project on top
    of it, so a legality predicate that reads the instance column alone
    rejects an attack the option list already offered the client.
    """
    connection = conn or _db_layer._db
    from rules_port.static_rules import effective_attributes
    return int(effective_attributes(
        connection, session_id, battle_state or {}, int(card_uid)) or 0)


def db_game_champion(session_id, user_id, conn=None):
    """Return ``(card_uid, template_guid)`` for a session champion."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND is_champion=1 LIMIT 1",
        (session_id, user_id)).fetchone()


def db_clear_session_cards(session_id, conn=None):
    """Delete all mutable game-card rows for a session."""
    connection = conn or _db_layer._db
    connection.execute("DELETE FROM game_cards WHERE session_id=?", (session_id,))
    if conn is None:
        connection.commit()


def db_delete_game_session(session_id, conn=None):
    """Delete all database-owned rows for a completed game session."""
    connection = conn or _db_layer._db
    sid = str(session_id)
    for table in ("game_cards", "session_events", "session_transactions",
                  "game_sessions"):
        connection.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
    if conn is None:
        connection.commit()


def db_champion_template_health(guid, conn=None):
    """Return a champion template's starting health."""
    row = (conn or _db_layer._db).execute(
        "SELECT starting_health FROM champion_template_data WHERE guid=?",
        (guid,)).fetchone()
    return row[0] if row else DEFAULT_STARTING_HEALTH


def db_get_charge_power(champion_guid, conn=None):
    """Return a champion template's charge-power ability GUID."""
    row = (conn or _db_layer._db).execute(
        "SELECT charge_power FROM champion_templates WHERE guid=?",
        (champion_guid,)).fetchone()
    return row[0] if row and row[0] else None


def db_get_champion_ability_guids(champion_guid, conn=None):
    """Return all ability GUIDs attached to a champion template."""
    return [row[0] for row in (conn or _db_layer._db).execute(
        "SELECT ability_guid FROM champion_abilities WHERE champion_guid=?",
        (champion_guid,)).fetchall()]


def db_target_template_text(template_id, conn=None):
    """Return a target template's authored game text."""
    row = (conn or _db_layer._db).execute(
        "SELECT game_text FROM target_templates WHERE template_id=?",
        (str(template_id),)).fetchone()
    return (row[0] or "") if row else ""


def db_ability_meta_targets(ability_guid, conn=None):
    """Return targeting and activation metadata for an ability."""
    return (conn or _db_layer._db).execute(
        "SELECT target_template_ids, trigger_event_type, game_text, "
        "casting_behavior, is_manual, activation_cost, uses_per_game, "
        "uses_per_turn FROM card_abilities_meta WHERE ability_guid=?",
        (str(ability_guid),)).fetchone()


def db_champion_template_health_by_class(race_name, cls_name, conn=None):
    """Return starting health from champion class data."""
    row = (conn or _db_layer._db).execute(
        "SELECT starting_health FROM champion_class_data "
        "WHERE race=? AND champion_class=?", (race_name, cls_name)).fetchone()
    return row[0] if row else DEFAULT_STARTING_HEALTH


def db_warzone_troop_count(session_id, user_id, conn=None):
    """Count a player's warzone troops, with zero representing the AI."""
    row = (conn or _db_layer._db).execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='warzone' AND card_type LIKE '%Troop%'",
        (session_id, user_id)).fetchone()
    return int(row[0]) if row else 0


def db_card_save_player_stops(user_id, self_stops_json, opp_stops_json,
                              conn=None):
    """Persist phase-stop preferences in the caller's transaction."""
    connection = conn or _db_layer._db
    connection.execute(
        "INSERT INTO user_prefs (user_id, self_stops, opp_stops) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET self_stops=excluded.self_stops, "
        "opp_stops=excluded.opp_stops",
        (user_id, self_stops_json, opp_stops_json))
    if conn is None:
        connection.commit()


def db_card_load_player_stops(user_id, conn=None):
    """Return persisted phase-stop preferences."""
    row = (conn or _db_layer._db).execute(
        "SELECT self_stops, opp_stops FROM user_prefs WHERE user_id=?",
        (user_id,)).fetchone()
    if not row or not row[0]:
        return None, None
    return row[0], row[1] if row[1] else None


def db_card_original_template(session_id, card_uid, conn=None):
    """Return the original template GUID for a session card."""
    row = (conn or _db_layer._db).execute(
        "SELECT original_template_guid FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (session_id, int(card_uid))).fetchone()
    return (row[0] or None) if row else None


def db_card_template_full(template_guid, conn=None):
    """Return the full template projection used for card reversion."""
    return (conn or _db_layer._db).execute(
        "SELECT card_type, cost, attack, defense, threshold_json, "
        "abilities_json, attributes FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()


def db_card_revert_to_template(session_id, card_uid, attributes, abilities_json,
                               template_guid, card_type, conn=None):
    """Reset a transformed card instance to canonical template data."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET card_attributes=?, card_abilities=?, "
        "card_template_id=?, template_guid=?, card_type=?, card_attack_mod=0, "
        "card_defense_mod=0, card_uses='{}', original_template_guid=?, "
        "position=100 WHERE session_id=? AND card_uid=?",
        (attributes, abilities_json, template_guid, template_guid, card_type,
         template_guid, session_id, int(card_uid)))
    if conn is None:
        connection.commit()


def db_get_card_type(template_guid, conn=None):
    """Return a template card type, defaulting legacy rows to Troop."""
    row = (conn or _db_layer._db).execute(
        "SELECT card_type FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else "Troop"


def db_set_card_state_or(session_id, card_uid, state_bits, conn=None):
    """OR state bits onto a card without committing explicit transactions."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET card_state = (card_state | ?) "
        "WHERE session_id=? AND card_uid=?",
        (int(state_bits), session_id, int(card_uid)))
    if conn is None:
        connection.commit()


def db_set_card_location(session_id, card_uid, location, extra_set=None,
                         extra_params=None, conn=None):
    """Move a card to a zone and leave explicit transactions uncommitted."""
    connection = conn or _db_layer._db
    sql = "UPDATE game_cards SET location=?"
    params = [location]
    if extra_set:
        sql += ", " + extra_set
        params.extend(extra_params or [])
    params.extend([session_id, int(card_uid)])
    connection.execute(sql + " WHERE session_id=? AND card_uid=?", params)
    if conn is None:
        connection.commit()


def db_card_sync_abilities(session_id, card_uid, abilities_json, attributes,
                           template_guid, commit=True, conn=None):
    """Synchronize an instance's authored abilities and reset usage state."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET card_abilities=?, card_attributes=?, "
        "card_uses='{}', original_template_guid = CASE "
        "WHEN original_template_guid IS NULL OR original_template_guid='' "
        "THEN ? ELSE original_template_guid END "
        "WHERE session_id=? AND card_uid=?",
        (abilities_json, attributes, template_guid, session_id, int(card_uid)),
    )
    if commit:
        connection.commit()


def db_game_session_pids(session_id, conn=None):
    """Return the actual player IDs participating in a game session."""
    connection = conn or _db_layer._db
    tournament_rows = connection.execute(
        "SELECT DISTINCT ts.player_uid FROM tournaments t "
        "JOIN tournament_signups ts ON ts.tournament_id=t.id "
        "WHERE t.session_id=? ORDER BY ts.player_uid", (session_id,)
    ).fetchall()
    if len(tournament_rows) >= 2:
        return [row[0] for row in tournament_rows]
    rows = connection.execute(
        "SELECT DISTINCT user_id FROM game_cards WHERE session_id=?",
        (session_id,)).fetchall()
    return [row[0] for row in rows]


def db_game_cards_at_location(session_id, location, card_type=None,
                              user_id=None, conn=None):
    """Return ordered full card rows for one game zone."""
    connection = conn or _db_layer._db
    sql = ("SELECT card_uid, template_guid, user_id, card_type, card_state, "
           "card_abilities, card_attributes FROM game_cards "
           "WHERE session_id=? AND location=?")
    params = [session_id, location]
    if card_type is not None:
        sql += " AND card_type=?"
        params.append(card_type)
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    sql += " ORDER BY position, id"
    return connection.execute(sql, params).fetchall()


def db_game_cards_at_location_scalar(session_id, location, user_id=None,
                                     conn=None):
    """Return ordered card UIDs for one game zone."""
    connection = conn or _db_layer._db
    sql = "SELECT card_uid FROM game_cards WHERE session_id=? AND location=?"
    params = [session_id, location]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    sql += " ORDER BY position, id"
    return [row[0] for row in connection.execute(sql, params).fetchall()]


def db_warzone_troops_basic(session_id, user_id=None, conn=None):
    """Return ``(card_uid, card_type, card_state)`` for warzone troops."""
    connection = conn or _db_layer._db
    sql = ("SELECT card_uid, card_type, card_state FROM game_cards "
           "WHERE session_id=? AND location='warzone' "
           "AND card_type LIKE '%Troop%'")
    params = [session_id]
    if user_id is not None:
        # This query has no ``gc`` alias; using one here makes every caller
        # that asks for one player's attack options fail at DeclareAttack.
        sql += " AND user_id=?"
        params.append(user_id)
    return connection.execute(sql, params).fetchall()


def db_warzone_troops_with_state(session_id, user_id=None, conn=None):
    """Return ``(uid, template_guid, state, user_id)`` warzone troops."""
    connection = conn or _db_layer._db
    sql = ("SELECT card_uid, template_guid, card_state, user_id "
           "FROM game_cards WHERE session_id=? AND location='warzone' "
           "AND card_type LIKE '%Troop%'")
    params = [session_id]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    return connection.execute(sql, params).fetchall()


def db_hand_card_count(session_id, user_id, conn=None):
    """Return the number of cards in a player's hand."""
    row = (conn or _db_layer._db).execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='hand'", (session_id, user_id)).fetchone()
    return row[0] if row else 0


def db_bulk_blocker_state(session_id, blocker_uids, conn=None):
    """Mark each declared blocker as Blocking within the caller's transaction."""
    if not blocker_uids:
        return
    connection = conn or _db_layer._db
    connection.executemany(
        "UPDATE game_cards SET card_state = (card_state | ?) "
        "WHERE session_id=? AND card_uid=?",
        [(ECardStates.Blocking, session_id, int(uid))
         for uid in blocker_uids],
    )
    if conn is None:
        connection.commit()


def db_game_deck_cards(session_id, user_id, conn=None):
    """Return ordered ``(card_uid, template_guid)`` deck rows."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck' "
        "ORDER BY position", (session_id, user_id)).fetchall()


def db_apply_tournament_main_deck(session_id, user_id, main_card_uids,
                                  conn=None):
    """Move a saved tournament main deck into the playable deck zone."""
    connection = conn or _db_layer._db
    ids = [int(uid) for uid in main_card_uids]
    connection.execute(
        "UPDATE game_cards SET location='sideboard', position=0 "
        "WHERE session_id=? AND user_id=? AND card_type!='Champion'",
        (session_id, user_id))
    connection.executemany(
        "UPDATE game_cards SET location='deck', position=? "
        "WHERE session_id=? AND user_id=? AND card_uid=?",
        [(index, session_id, user_id, card_uid)
         for index, card_uid in enumerate(ids)])
    if conn is None:
        connection.commit()


def db_game_draw_cards(session_id, user_id, count=7, conn=None):
    """Move the first ordered deck cards into hand and return their rows."""
    connection = conn or _db_layer._db
    rows = connection.execute(
        "SELECT card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck' "
        "ORDER BY position LIMIT ?", (session_id, user_id, count)
    ).fetchall()
    for card_uid, _template_guid in rows:
        connection.execute(
            "UPDATE game_cards SET location='hand' "
            "WHERE card_uid=? AND session_id=?", (int(card_uid), session_id))
    if conn is None:
        connection.commit()
    return rows


def db_game_shuffle_deck(session_id, user_id, conn=None):
    """Shuffle one player's deck positions within the caller's transaction."""
    import random as _shuf_rnd

    connection = conn or _db_layer._db
    rows = connection.execute(
        "SELECT card_uid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck'",
        (session_id, user_id)).fetchall()
    deck = [int(row[0]) for row in rows]
    _shuf_rnd.shuffle(deck)
    connection.executemany(
        "UPDATE game_cards SET position=? "
        "WHERE card_uid=? AND session_id=?",
        [(position, card_uid, session_id)
         for position, card_uid in enumerate(deck)])
    if conn is None:
        connection.commit()


def db_game_get_hand(session_id, user_id, conn=None):
    """Return ordered ``(card_uid, template_guid)`` hand rows."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='hand' "
        "ORDER BY position", (session_id, user_id)).fetchall()


def db_game_card_type(template_guid, conn=None):
    """Return a template's stored card type, defaulting legacy rows to Troop."""
    if not template_guid:
        return "Troop"
    row = (conn or _db_layer._db).execute(
        "SELECT card_type FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else "Troop"


def db_get_card_abilities(template_guid, conn=None):
    """Return ``(abilities_json, attributes)`` for a card template."""
    row = (conn or _db_layer._db).execute(
        "SELECT abilities_json, attributes FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    if row:
        return row[0] or "[]", int(row[1] or 0)
    return "[]", 0


def db_card_ability_list(session_id, card_uid, conn=None):
    """Return the current normalized ability GUIDs on a game-card instance."""
    row = (conn or _db_layer._db).execute(
        "SELECT card_abilities FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row or not row[0]:
        return []
    try:
        return [guid.lower() for guid in json.loads(row[0])]
    except Exception:
        return []


def db_card_uses(session_id, card_uid, conn=None):
    """Return per-instance ability usage counts."""
    row = (conn or _db_layer._db).execute(
        "SELECT card_uses FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return dict(json.loads(row[0]))
    except Exception:
        return {}


def db_bump_card_use(session_id, card_uid, ability_guid, conn=None):
    """Increment and return one instance ability's usage count."""
    connection = conn or _db_layer._db
    uses = db_card_uses(session_id, card_uid, conn=connection)
    uses[ability_guid] = int(uses.get(ability_guid, 0)) + 1
    connection.execute(
        "UPDATE game_cards SET card_uses=? WHERE session_id=? AND card_uid=?",
        (json.dumps(uses), session_id, int(card_uid)))
    if conn is None:
        connection.commit()
    return uses[ability_guid]


def db_card_template_thresholds(template_guid, conn=None):
    """Return threshold, ability, and attribute fields for a template."""
    return (conn or _db_layer._db).execute(
        "SELECT threshold_json, abilities_json, attributes "
        "FROM card_templates WHERE guid=?", (template_guid,)).fetchone()


def db_card_basic(session_id, card_uid, conn=None):
    """Return ``(template_guid, user_id)`` for one game card."""
    return (conn or _db_layer._db).execute(
        "SELECT template_guid, user_id FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_with_template(session_id, card_uid, conn=None):
    """Return the legacy joined game-card/template projection."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.template_guid, ct.card_type, gc.card_state, gc.user_id, "
        "gc.card_attributes, gc.card_template_id, gc.card_abilities, "
        "gc.card_attack_mod, gc.card_defense_mod, gc.card_damage, "
        "gc.original_template_guid FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_instance_full(session_id, card_uid, conn=None):
    """Return mutable instance fields for one game card."""
    return (conn or _db_layer._db).execute(
        "SELECT card_abilities, card_attack_mod, card_defense_mod, "
        "card_damage, original_template_guid, permanent_buffs, "
        "temporary_buffs, card_cost_mod, cost_mod_json, card_attributes, "
        "temporary_attributes FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_is_champion_template(template_guid, conn=None):
    """Whether a template is present in either champion catalog."""
    connection = conn or _db_layer._db
    try:
        row = connection.execute(
            "SELECT guid FROM champion_templates_extended WHERE guid=? "
            "UNION ALL SELECT guid FROM champion_templates WHERE guid=? LIMIT 1",
            (template_guid, template_guid)).fetchone()
    except Exception as exc:
        if "champion_templates_extended" not in str(exc):
            raise
        row = connection.execute(
            "SELECT guid FROM champion_templates WHERE guid=? LIMIT 1",
            (template_guid,)).fetchone()
    return row is not None


def db_card_set_attacking_state(session_id, card_uid, state_bits, conn=None):
    """OR attacking/tapped state onto a warzone card."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET card_state = (card_state | ?) "
        "WHERE session_id=? AND card_uid=?",
        (int(state_bits), session_id, int(card_uid)),
    )
    if conn is None:
        connection.commit()


def db_card_set_warzone_arrival(session_id, card_uid, conn=None):
    """Move a resolved troop to warzone and apply its arrival state bits."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET location='warzone', "
        "card_state = (card_state & ~?) | ? "
        "WHERE session_id=? AND card_uid=?",
        (ECardStates.StartedATurnOnYourSide, ECardStates.CameOutThisTurn,
         session_id, int(card_uid)),
    )
    if conn is None:
        connection.commit()


def db_set_card_resolved_at(session_id, card_uid, resolved_at, conn=None):
    """Stamp the authoritative resolve-order counter on a game card."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET resolved_at=? WHERE session_id=? AND card_uid=?",
        (int(resolved_at), session_id, int(card_uid)))
    if conn is None:
        connection.commit()


def db_card_discard_spell(session_id, card_uid, conn=None):
    """Discard a resolved spell through the shared discard mutation."""
    return db_discard_card(session_id, card_uid, conn=conn)


def db_card_set_sacrifice_state(session_id, card_uid, conn=None):
    """Clear combat state and discard a sacrificed troop."""
    clear_bits = (ECardStates.Attacking | ECardStates.HasAttacked |
                  ECardStates.Tapped | ECardStates.StartedATurnOnYourSide)
    return db_discard_card(
        session_id, card_uid,
        extra_set="card_state = (card_state & ~?)",
        extra_params=(clear_bits,), connection=conn)


def db_champion_ability_guids(champion_guid, conn=None):
    """Return the authored ability GUIDs for one champion template."""
    rows = (conn or _db_layer._db).execute(
        "SELECT ability_guid FROM champion_abilities WHERE champion_guid=?",
        (champion_guid,)).fetchall()
    return [row[0] for row in rows]


def db_champion_ability_costs(ability_guid, conn=None):
    """Return normalized charge-power costs and phase restrictions."""
    row = (conn or _db_layer._db).execute(
        "SELECT charge_cost, spell_cost, casting_behavior "
        "FROM champion_abilities WHERE ability_guid=? LIMIT 1",
        (str(ability_guid),)).fetchone()
    if not row:
        return None
    casting = row[2] or 0
    if casting == ECardTypes.QuickAction:
        phases = 0
    elif casting == ECardTypes.BasicAction:
        phases = ((1 << ETurnPhases.FirstMainPhase)
                  | (1 << ETurnPhases.SecondMainPhase))
    else:
        phases = 0
        casting = ECardTypes.QuickAction
    return (row[0] or 0, row[1] or 0, phases, casting)


def db_champion_ability_thresholds(ability_guid, conn=None):
    """Return ``(color, quantity)`` threshold requirements for a power."""
    row = (conn or _db_layer._db).execute(
        "SELECT thresholds_json FROM champion_abilities "
        "WHERE ability_guid=? LIMIT 1", (str(ability_guid),)).fetchone()
    if not row or not row[0]:
        return []
    try:
        data = json.loads(row[0])
    except Exception:
        return []
    return [(str(item.get("color", "")),
             int(item.get("quantity", 0) or 0))
            for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def db_warzone_troop_attributes(session_id, user_id, conn=None):
    """Return the instance/template attributes used by attack eligibility."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_uid, gc.card_state, gc.card_attributes, "
        "gc.temporary_attributes, ct.attributes, gc.card_abilities "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%'",
        (session_id, user_id),
    ).fetchall()


def db_warzone_attack_candidates(session_id, user_id, conn=None):
    """Return the joined fields needed by the AI attack evaluator."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_uid, gc.template_guid, ct.attributes, "
        "gc.card_attributes, gc.card_state, ct.attack "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%'",
        (session_id, user_id),
    ).fetchall()


def db_warzone_attack_option_rows(session_id, user_id, conn=None):
    """Return the joined fields used to publish PvP attack options."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_uid, gc.card_state, gc.card_type, "
        "(ct.attributes | gc.card_attributes | "
        "COALESCE(gc.temporary_attributes, 0)) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%'",
        (session_id, user_id),
    ).fetchall()


def db_warzone_blocker_uids(session_id, user_id, tapped_mask, conn=None):
    """Return untapped defender troop UIDs for a PvP block window."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_uid FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%' AND (gc.card_state & ?) = 0",
        (session_id, user_id, tapped_mask),
    ).fetchall()


def db_card_attribute_rows(session_id, card_uids, conn=None):
    """Return effective stored/template attributes for selected session cards."""
    if not card_uids:
        return []
    connection = conn or _db_layer._db
    marks = ",".join("?" for _ in card_uids)
    return connection.execute(
        "SELECT ct.attributes | gc.card_attributes | "
        "COALESCE(gc.temporary_attributes, 0) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid IN (" + marks + ")",
        [session_id] + [int(uid) for uid in card_uids],
    ).fetchall()


def db_card_uids_in_zone(session_id, user_id, zone, conn=None):
    """Return card UIDs owned by a player in one game zone."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location=?", (session_id, user_id, zone)).fetchall()


def db_card_location(session_id, card_uid, conn=None):
    """Return the current zone for one session card, or ``None``."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid)),
    ).fetchone()
    return row[0] if row else None


def db_hand_cards_for_discard(session_id, user_id, conn=None):
    """Return stable hand rows used by discard selection."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.id, gc.card_uid, gc.template_guid, ct.card_type "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' "
        "ORDER BY gc.position",
        (session_id, user_id),
    ).fetchall()


def db_ability_option_cards(session_id, user_id, conn=None):
    """Return owned hand/warzone cards and fields used for activation options."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_uid, gc.template_guid, gc.card_state, "
        "(ct.attributes | gc.card_attributes), ct.card_type, gc.location "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? "
        "AND gc.location IN ('warzone','hand')",
        (session_id, user_id),
    ).fetchall()


def db_card_ability_payload(session_id, card_uid, conn=None):
    """Return the raw per-instance ability payload, retaining NULL semantics."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT card_abilities FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (session_id, int(card_uid))).fetchone()
    return row[0] if row else None


def db_card_ability_state(session_id, card_uid, conn=None):
    """Return a card's serialized abilities, authored abilities, and state."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_abilities, ct.abilities_json, gc.card_state "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid)),
    ).fetchone()


def db_card_filter_view(session_id, card_uid, conn=None):
    """Return the projection consumed by typed damage/immunity filters."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
        "gc.template_guid, gc.card_state, COALESCE(ct.attack,0), "
        "COALESCE(ct.defense,0), ct.name, COALESCE(ct.cost,0), ct.subtype, "
        "ct.threshold_json, gc.card_abilities, gc.permanent_buffs "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid)),
    ).fetchone()


def db_card_damage_shield_fields(session_id, card_uid, conn=None):
    """Return the two serialized modifier stores used by damage shields."""
    return (conn or _db_layer._db).execute(
        "SELECT permanent_buffs, temporary_buffs FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid)),
    ).fetchone()


def db_condition_card_row(session_id, card_uid, conn=None):
    """Return the full mutable/template projection used by conditions."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, COALESCE(gc.card_type, ct.card_type), "
        "gc.location, gc.user_id, gc.card_state, "
        "COALESCE(ct.attack,0), COALESCE(ct.defense,0), gc.template_guid, "
        "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
        "gc.card_attributes, ct.attributes, gc.card_attack_mod, "
        "gc.card_defense_mod, COALESCE(gc.permanent_buffs,'{}') "
        "FROM game_cards gc LEFT JOIN card_templates ct "
        "ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid)),
    ).fetchone()


def db_condition_cards_in_zones(session_id, zones, user_id=None, conn=None):
    """Return card projections needed for zone-based condition filters."""
    if not zones:
        return []
    connection = conn or _db_layer._db
    marks = ",".join("?" for _ in zones)
    params = [session_id] + list(zones)
    sql = (
        "SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
        "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
        "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
        "gc.card_attributes, ct.attributes, gc.template_guid, "
        "COALESCE(gc.permanent_buffs,'{}') FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.location IN (" + marks + ")"
    )
    if user_id is not None:
        sql += " AND gc.user_id=?"
        params.append(user_id)
    return connection.execute(sql, params).fetchall()


def db_template_in_zones(session_id, template_guid, zones, conn=None):
    """Whether a template is present in any requested session zone."""
    if not zones:
        return False
    marks = ",".join("?" for _ in zones)
    row = (conn or _db_layer._db).execute(
        "SELECT 1 FROM game_cards WHERE session_id=? AND template_guid=? "
        "AND location IN (" + marks + ") LIMIT 1",
        [session_id, template_guid] + list(zones),
    ).fetchone()
    return bool(row)


def db_card_permanent_buffs(session_id, card_uid, conn=None):
    """Return one card's persistent modifier payload."""
    row = (conn or _db_layer._db).execute(
        "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (session_id, int(card_uid))).fetchone()
    return row[0] if row else None


def db_charge_ability_cost(ability_guid, conn=None):
    """Return the authored charge cost for a champion/talent ability."""
    connection = conn or _db_layer._db
    for table in ("champion_abilities", "talent_abilities"):
        row = connection.execute(
            "SELECT charge_cost FROM " + table + " WHERE ability_guid=? "
            "LIMIT 1", (str(ability_guid).lower(),)).fetchone()
        if row is not None:
            return row[0]
    return None


def db_effect_condition_json(condition_id, conn=None):
    """Return serialized metadata for one BOM effect condition."""
    row = (conn or _db_layer._db).execute(
        "SELECT condition_json FROM ability_effect_conditions "
        "WHERE condition_id=?", (condition_id,)).fetchone()
    return row[0] if row else None


def db_template_ability_payload(template_guid, conn=None):
    """Return a template's raw authored ability payload."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT abilities_json FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else None


def db_template_ability_data(template_guid, conn=None):
    """Return authored abilities and variable-cost flag for a template."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT abilities_json, variable_cost FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()


def db_card_activation_info(session_id, card_uid, conn=None):
    """Return state, effective stored attributes, and type for one card."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_state, (ct.attributes | gc.card_attributes), "
        "ct.card_type FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_owned_warzone_card(session_id, card_uid, user_id, conn=None):
    """Return template/type only when a card is owned in the warzone."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT template_guid, card_type FROM game_cards "
        "WHERE session_id=? AND card_uid=? AND user_id=? "
        "AND location='warzone'",
        (session_id, int(card_uid), int(user_id))).fetchone()


def db_card_zone_details(session_id, card_uid, conn=None):
    """Return template, instance template ID, owner, and zone for one card."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT template_guid, card_template_id, user_id, location "
        "FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_owner_location_position(session_id, card_uid, conn=None):
    """Return owner, zone, and position for one session card."""
    return (conn or _db_layer._db).execute(
        "SELECT user_id, location, position FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_move_card_for_effect(session_id, card_uid, location, position, state,
                            clear_dead=False, clear_bits=0, conn=None):
    """Move a card using the effect-zone state transition contract."""
    if location not in {"deck", "hand", "warzone", "underground", "discard", "void"}:
        raise ValueError("unsupported effect destination")
    connection = conn or _db_layer._db
    state_expr = "card_state & ~?" if clear_dead else "?"
    params = ((location, int(position), int(clear_bits), session_id, int(card_uid))
              if clear_dead else
              (location, int(position), int(state), session_id, int(card_uid)))
    return connection.execute(
        "UPDATE game_cards SET location=?, position=?, card_state = "
        + state_expr + " WHERE session_id=? AND card_uid=?", params)


def db_restore_card_to_warzone(session_id, card_uid, clear_bits, set_bits,
                               conn=None):
    """Return a voided card to warzone with the authored state transition."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET location='warzone', position=0, "
        "card_state = (card_state & ~?) | ? "
        "WHERE session_id=? AND card_uid=?",
        (int(clear_bits), int(set_bits), session_id, int(card_uid)))


def db_card_collection_info(session_id, card_uid, conn=None):
    """Return collection owner and original/current template IDs."""
    return (conn or _db_layer._db).execute(
        "SELECT user_id, original_template_guid, template_guid "
        "FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_grant_info(session_id, card_uid, conn=None):
    """Return effective abilities and projection fields for a grant."""
    return (conn or _db_layer._db).execute(
        "SELECT card_abilities, template_guid, user_id, location, card_state "
        "FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_mutation_field(session_id, card_uid, field, conn=None):
    """Read one approved mutable JSON field from a session card."""
    if field not in {"permanent_buffs", "temporary_buffs"}:
        raise ValueError("unsupported card mutation field")
    row = (conn or _db_layer._db).execute(
        "SELECT " + field + " FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row[0] if row else None


def db_set_card_mutation_field(session_id, card_uid, field, value, conn=None):
    """Persist one approved mutable JSON field without forcing a commit."""
    if field not in {"permanent_buffs", "temporary_buffs"}:
        raise ValueError("unsupported card mutation field")
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET " + field + "=? WHERE session_id=? AND card_uid=?",
        (value, session_id, int(card_uid))).rowcount


def db_clear_temporary_buffs(session_id, card_uid=None, conn=None):
    """Clear this-turn modifier payloads for one card or the whole session."""
    connection = conn or _db_layer._db
    if card_uid is None:
        return connection.execute(
            "UPDATE game_cards SET temporary_buffs='{}' "
            "WHERE session_id=?", (session_id,)).rowcount
    return connection.execute(
        "UPDATE game_cards SET temporary_buffs='{}' "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).rowcount


def db_card_template_threshold_subtype(session_id, card_uid, conn=None):
    """Return printed threshold JSON and subtype for one session card."""
    return (conn or _db_layer._db).execute(
        "SELECT ct.threshold_json, ct.subtype FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_original_and_template(session_id, card_uid, conn=None):
    """Return original and current template identities for a session card."""
    return (conn or _db_layer._db).execute(
        "SELECT original_template_guid, template_guid FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (session_id, int(card_uid))
    ).fetchone()


def db_card_abilities_state(session_id, card_uid, conn=None):
    """Return mutable ability JSON and state for a session card."""
    return (conn or _db_layer._db).execute(
        "SELECT card_abilities, card_state FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (session_id, int(card_uid))
    ).fetchone()


def db_reset_card_modifiers(session_id, card_uid, permanent_buffs, conn=None):
    """Clear stat/cost modifiers while retaining serialized card buffs."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET card_attack_mod=0, card_defense_mod=0, "
        "card_cost_mod=0, permanent_buffs=? WHERE session_id=? AND card_uid=?",
        (permanent_buffs, session_id, int(card_uid))).rowcount


def db_cards_by_template_owner(session_id, template_guid, user_id, conn=None):
    """Return session-card UID, owner, and zone for owned template copies."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT card_uid, user_id, location FROM game_cards "
        "WHERE session_id=? AND template_guid=? AND user_id=?",
        (session_id, template_guid, int(user_id))).fetchall()


def db_card_position(session_id, card_uid, conn=None):
    """Return a card's current deck/zone position."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT position FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row[0] if row else None


def db_card_type_effective_defense(session_id, card_uid, conn=None):
    """Return card type and effective defense for one session card."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_type, COALESCE(ct.defense,0) "
        "+ COALESCE(gc.card_defense_mod,0) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid)),
    ).fetchone()


def db_cards_for_filter(session_id, owners, locations, conn=None):
    """Return card fields consumed by metadata card-filter evaluation."""
    if not owners or not locations:
        return []
    connection = conn or _db_layer._db
    owner_marks = ",".join("?" for _ in owners)
    location_marks = ",".join("?" for _ in locations)
    return connection.execute(
        "SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
        "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
        "ct.name, COALESCE(ct.cost,0), ct.subtype "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        f"WHERE gc.session_id=? AND gc.user_id IN ({owner_marks}) "
        f"AND lower(gc.location) IN ({location_marks})",
        [session_id, *[int(owner) for owner in owners], *locations],
    ).fetchall()


def db_hand_exists(session_id, user_id, conn=None):
    """Whether a player has at least one card in hand."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT 1 FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='hand' LIMIT 1", (session_id, int(user_id))).fetchone()


def db_hand_card_for_discard(session_id, card_uid, user_id, conn=None):
    """Return a submitted hand card only when controlled by the player."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT user_id, COALESCE(owner_user_id, user_id), "
        "template_guid, card_template_id FROM game_cards "
        "WHERE session_id=? AND card_uid=? AND user_id=? "
        "AND location='hand'",
        (session_id, int(card_uid), int(user_id))).fetchone()


def db_deck_top_card(session_id, user_id, conn=None):
    """Return the next card in a player's deck by authoritative position."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT card_uid, template_guid, card_template_id FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck' "
        "ORDER BY position LIMIT 1", (session_id, int(user_id))).fetchone()


def db_hand_count(session_id, user_id, conn=None):
    """Return the number of cards in a player's hand."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='hand'", (session_id, int(user_id))).fetchone()
    return int(row[0] or 0) if row else 0


def db_card_play_info(session_id, card_uid, conn=None):
    """Return template/card-type/resource fields for a played card."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.template_guid, ct.card_type, ct.name, "
        "ct.current_resources_granted, ct.max_resources_granted, "
        "ct.abilities_json FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_cards_with_ability(session_id, user_id, ability_guid, card_uid=None,
                          conn=None):
    """Return card UIDs carrying an ability in hand or warzone."""
    connection = conn or _db_layer._db
    sql = ("SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
           "AND location IN ('warzone','hand') AND card_abilities LIKE ?")
    params = [session_id, int(user_id), f'%"{ability_guid}"%']
    if card_uid is not None:
        sql += " AND card_uid=?"
        params.append(int(card_uid))
    return connection.execute(sql, params).fetchall()


def db_card_chain_info(session_id, card_uid, conn=None):
    """Return template GUID, type, and cost for a chain card."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.template_guid, ct.card_type, ct.cost "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_warzone_display_rows(session_id, conn=None):
    """Return all warzone fields needed for a PvP CardUpdated projection."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT card_uid, template_guid, user_id, card_state, card_type "
        "FROM game_cards WHERE session_id=? AND location='warzone'",
        (session_id,)).fetchall()


def db_cards_in_zones_with_abilities(session_id, owner_id, zones, conn=None):
    """Return card UIDs and ability payloads for trigger-holder scanning."""
    if not zones:
        return []
    marks = ",".join("?" for _ in zones)
    cursor = (conn or _db_layer._db).execute(
        "SELECT card_uid, card_abilities FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location IN (" + marks + ") "
        "AND card_abilities IS NOT NULL AND card_abilities != ''",
        [session_id, int(owner_id), *zones])
    return cursor.fetchall() if hasattr(cursor, "fetchall") else []


def db_template_name(template_guid, conn=None):
    """Return a card template's display name, or None."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT name FROM card_templates WHERE guid=?", (template_guid,)
    ).fetchone()
    return row[0] if row else None


def db_deck_card_by_uid(session_id, user_id, card_uid, conn=None):
    """Return a specific card instance while it remains in a deck."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, template_guid, card_template_id FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck' "
        "AND card_uid=? LIMIT 1",
        (session_id, int(user_id), int(card_uid))).fetchone()


def db_deck_card_by_name(session_id, user_id, name_fragment, conn=None):
    """Return the first deck card matching an authored display name."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, gc.template_guid, gc.card_template_id "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='deck' "
        "AND LOWER(ct.name) LIKE LOWER(?) ORDER BY gc.position LIMIT 1",
        (session_id, int(user_id), "%" + str(name_fragment) + "%")).fetchone()


def db_hand_card_by_uid(session_id, user_id, card_uid, conn=None):
    """Return a specific controlled hand card with display identity."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, gc.template_guid, gc.card_template_id, ct.name "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' "
        "AND gc.card_uid=? LIMIT 1",
        (session_id, int(user_id), int(card_uid))).fetchone()


def db_hand_card_by_name(session_id, user_id, name, exact=True, conn=None):
    """Return the first controlled hand card matching an authored name."""
    operator = "=" if exact else "LIKE"
    value = str(name) if exact else "%" + str(name) + "%"
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, gc.template_guid, gc.card_template_id, ct.name "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' "
        "AND LOWER(ct.name) " + operator + " LOWER(?) "
        "ORDER BY gc.position, gc.card_uid LIMIT 1",
        (session_id, int(user_id), value)).fetchone()


def db_move_hand_card_to_deck_top(session_id, user_id, card_uid, conn=None):
    """Move a controlled hand card to deck position zero, preserving order."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='deck'", (session_id, int(user_id))).fetchone()
    deck_count = int(row[0] if row else 0)
    connection.execute(
        "UPDATE game_cards SET position=position+? WHERE session_id=? "
        "AND user_id=? AND location='deck'",
        (deck_count + 1, session_id, int(user_id)))
    connection.execute(
        "UPDATE game_cards SET location='deck', position=0, card_state=0 "
        "WHERE session_id=? AND user_id=? AND card_uid=? AND location='hand'",
        (session_id, int(user_id), int(card_uid)))
    connection.execute(
        "UPDATE game_cards SET position=position-? WHERE session_id=? "
        "AND user_id=? AND location='deck' AND card_uid<>?",
        (deck_count, session_id, int(user_id), int(card_uid)))
    return deck_count


def db_zone_display_rows(session_id, user_id, conn=None):
    """Return owned cards ordered for the debug zone listing."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, gc.location, ct.name, ct.guid "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? "
        "ORDER BY gc.location, gc.position",
        (session_id, int(user_id))).fetchall()


def db_hand_display_rows(session_id, user_id, conn=None):
    """Return hand fields used by the debug hand command."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, ct.name, ct.cost, ct.card_type "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' "
        "ORDER BY gc.position", (session_id, int(user_id))).fetchall()


def db_ai_hand_template_rows(session_id, conn=None):
    """Return AI hand UIDs and template GUIDs in display order."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, gc.template_guid FROM game_cards gc "
        "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='hand' "
        "ORDER BY gc.position", (session_id,)).fetchall()


def db_playable_hand_rows(session_id, user_id, limit=7, conn=None):
    """Return the first hand cards eligible for debug outline display."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, ct.name FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.position < 100 "
        "ORDER BY gc.position LIMIT ?",
        (session_id, int(user_id), int(limit))).fetchall()


def db_gencard_template(name, conn=None):
    """Return the preferred authored template for a debug card grant."""
    connection = conn or _db_layer._db
    try:
        return connection.execute(
            "SELECT guid, card_type, cost, attack, defense, abilities_json, attributes "
            "FROM card_templates WHERE LOWER(name) LIKE ? "
            "ORDER BY CASE WHEN LOWER(name)=? THEN 0 ELSE 1 END, "
            "COALESCE(equipment_modified,0) ASC, no_pvp ASC, is_pve ASC, guid ASC LIMIT 1",
            ("%" + str(name) + "%", str(name))).fetchone()
    except Exception:
        return connection.execute(
            "SELECT guid, card_type, cost, attack, defense, abilities_json, attributes "
            "FROM card_templates WHERE LOWER(name) LIKE ? "
            "ORDER BY CASE WHEN LOWER(name)=? THEN 0 ELSE 1 END, "
            "no_pvp ASC, is_pve ASC, guid ASC LIMIT 1",
            ("%" + str(name) + "%", str(name))).fetchone()


def db_move_debug_card(session_id, card_uid, location, conn=None):
    """Move a debug-selected card to an explicitly requested zone."""
    if not location:
        raise ValueError("missing card location")
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET location=? WHERE session_id=? AND card_uid=?",
        (str(location), session_id, int(card_uid)))
    return row[0] if row else None


def db_card_positions(session_id, card_uids, conn=None):
    """Return selected session-card positions in authoritative order."""
    if not card_uids:
        return []
    connection = conn or _db_layer._db
    marks = ",".join("?" for _ in card_uids)
    return connection.execute(
        "SELECT card_uid, position FROM game_cards "
        "WHERE session_id=? AND card_uid IN (" + marks + ") "
        "ORDER BY position", [session_id] + [int(uid) for uid in card_uids]
    ).fetchall()


def db_move_card_to_deck_top(session_id, card_uid, card_state, conn=None):
    """Place a session card at deck position zero and shift existing cards."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET position=COALESCE(position, 0)+1 "
        "WHERE session_id=? AND location='deck' AND card_uid<>?",
        (session_id, int(card_uid)))
    connection.execute(
        "UPDATE game_cards SET location='deck', position=0, card_state=? "
        "WHERE session_id=? AND card_uid=?",
        (card_state, session_id, int(card_uid)))
    if conn is None:
        connection.commit()


def db_move_card_to_deck(session_id, card_uid, card_state, conn=None):
    """Return a card to its deck zone without changing its position."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET location='deck', card_state=? "
        "WHERE session_id=? AND card_uid=?",
        (card_state, session_id, int(card_uid)))
    if conn is None:
        connection.commit()


def db_latest_deck_id_for_user(user_id, conn=None):
    """Return a user's most recently saved deck ID."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT id FROM decks WHERE user_id=? "
        "ORDER BY COALESCE(last_saved, created_at, 0) DESC, id DESC LIMIT 1",
        (user_id,)).fetchone()
    return row[0] if row else None
    return row[0] if row else None


def db_template_card_info(template_guid, conn=None):
    """Return display/type/stat fields used to materialize a card definition."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT card_type, name, cost, attack, defense FROM card_templates "
        "WHERE guid=?", (template_guid,)).fetchone()


def db_template_by_guid(template_guid, conn=None):
    """Return the legacy six-field card-template projection."""
    row = (conn or _db_layer._db).execute(
        "SELECT guid, card_type, name, cost, attack, defense "
        "FROM card_templates WHERE guid=?", (template_guid,)).fetchone()
    if not row:
        return None
    return (row[0], row[1], row[2], row[3] or 0, row[4] or 0, row[5] or 0)


def db_card_template_lethal(template_guid, conn=None):
    """Return the optional printed Lethal flag for a template."""
    try:
        row = (conn or _db_layer._db).execute(
            "SELECT lethal FROM card_templates WHERE guid=?",
            (template_guid,)).fetchone()
    except Exception:
        return 0
    return int(row[0] or 0) if row else 0


def db_rules_port_card_projection(session_id, card_uid, conn=None):
    """Return the typed play/resource card projection."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.template_guid, gc.location, gc.user_id, ct.name, "
        "ct.card_type, ct.abilities_json, ct.current_resources_granted, "
        "ct.max_resources_granted, ct.attack, ct.defense "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_rules_port_card_state_projection(session_id, card_uid, conn=None):
    """Return the typed ready-card state projection."""
    return (conn or _db_layer._db).execute(
        "SELECT template_guid, card_type, user_id, card_state "
        "FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_rules_port_hand_rows(session_id, user_id, conn=None):
    """Return opening-hand row IDs and card UIDs in display order."""
    return (conn or _db_layer._db).execute(
        "SELECT id, card_uid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='hand' "
        "ORDER BY position", (session_id, int(user_id))).fetchall()


def db_rules_port_session_card_ids(session_id, user_id, conn=None):
    """Return stable game-card row IDs for an opening-hand shuffle."""
    return [row[0] for row in (conn or _db_layer._db).execute(
        "SELECT id FROM game_cards WHERE session_id=? AND user_id=?",
        (session_id, int(user_id))).fetchall()]


def db_rules_port_draw_rows(session_id, user_id, count, conn=None):
    """Return ordered deck rows and template fields for redraw projection."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, gc.card_template_id, gc.template_guid, "
        "ct.name, ct.card_type, ct.cost, ct.attack, ct.defense, "
        "ct.threshold_json, ct.abilities_json FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='deck' "
        "ORDER BY gc.position LIMIT ?",
        (session_id, int(user_id), int(count))).fetchall()


def db_rules_port_redraw_hand(session_id, user_id, draw_count, conn=None):
    """Atomically replace an opening hand and return old/new projection rows."""
    import random

    connection = conn or _db_layer._db
    old_rows = db_rules_port_hand_rows(session_id, user_id, conn=connection)
    if old_rows:
        connection.executemany(
            "UPDATE game_cards SET location='deck', card_state=0 WHERE id=?",
            [(row[0],) for row in old_rows])
    ids = db_rules_port_session_card_ids(session_id, user_id, conn=connection)
    random.shuffle(ids)
    if ids:
        connection.executemany(
            "UPDATE game_cards SET position=? WHERE id=?",
            [(position, row_id) for position, row_id in enumerate(ids)])
    new_rows = db_rules_port_draw_rows(
        session_id, user_id, draw_count, conn=connection)
    if new_rows:
        connection.executemany(
            "UPDATE game_cards SET location='hand' "
            "WHERE session_id=? AND card_uid=?",
            [(session_id, row[0]) for row in new_rows])
    if conn is None:
        connection.commit()
    return old_rows, new_rows


def db_rules_port_warzone_projection(session_id, conn=None):
    """Return warzone UID, template, controller, and state rows."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, template_guid, user_id, card_state FROM game_cards "
        "WHERE session_id=? AND location='warzone'", (session_id,)).fetchall()


def db_rules_port_first_hand_card(session_id, user_id, conn=None):
    """Return the first hand card UID in authoritative order."""
    row = (conn or _db_layer._db).execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='hand' ORDER BY position LIMIT 1",
        (session_id, int(user_id))).fetchone()
    return int(row[0]) if row else None


def db_rules_port_hand_card(session_id, card_uid, user_id, conn=None):
    """Return a specific owned hand card's row ID and template GUID."""
    return (conn or _db_layer._db).execute(
        "SELECT id, template_guid FROM game_cards WHERE session_id=? "
        "AND card_uid=? AND user_id=? AND location='hand'",
        (session_id, int(card_uid), int(user_id))).fetchone()


def db_rules_port_card_instance_id(session_id, card_uid, conn=None):
    """Return the stored template/instance ID for a session card."""
    row = (conn or _db_layer._db).execute(
        "SELECT card_template_id FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row[0] if row else None


def db_rules_port_talent_ability_exists(ability_guid, conn=None):
    """Whether an ability GUID is authored in the talent catalog."""
    return bool((conn or _db_layer._db).execute(
        "SELECT 1 FROM talent_abilities WHERE ability_guid=? LIMIT 1",
        (str(ability_guid),)).fetchone())


def db_rules_port_cards_with_ability(session_id, user_id, ability_guid,
                                     card_uid=None, conn=None):
    """Return owned hand/warzone cards carrying an ability and their uses."""
    connection = conn or _db_layer._db
    sql = ("SELECT card_uid, card_uses FROM game_cards WHERE session_id=? "
           "AND user_id=? AND location IN ('warzone','hand') "
           "AND card_abilities LIKE ?")
    params = [session_id, int(user_id), f'%"{ability_guid}"%']
    if card_uid is not None:
        sql += " AND card_uid=?"
        params.append(int(card_uid))
    return connection.execute(sql, params).fetchall()


def db_set_resource_guids(set_guid, conn=None):
    """Return resource template GUIDs from a set for a PvP fixture."""
    return [row[0] for row in (conn or _db_layer._db).execute(
        "SELECT DISTINCT guid FROM card_templates WHERE set_guid=? "
        "AND card_type='Resource'", (set_guid,)).fetchall()]


def db_set_constructed_guids(set_guid, conn=None):
    """Return collectible non-resource PvP template GUIDs from a set."""
    return [row[0] for row in (conn or _db_layer._db).execute(
        "SELECT guid FROM card_templates WHERE set_guid=? "
        "AND card_type!='Resource' AND is_pve=0 AND no_pvp=0",
        (set_guid,)).fetchall()]


def db_card_catalog(conn=None):
    """Return the card catalog fields used by pack/profile projections."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT guid, set_guid, name, rarity, cost, attack, defense, "
        "is_pve, no_pvp, card_type FROM card_templates"
    ).fetchall()


def db_pvp_set_guids(conn=None):
    """Return collectible set IDs that contain eligible PVP cards."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT set_guid FROM card_templates "
        "WHERE is_pve=0 AND no_pvp=0 "
        "AND rarity IN ('Common','Uncommon','Rare','Legendary') "
        "GROUP BY set_guid"
    ).fetchall()


def db_pvp_booster_card_guids(conn=None):
    """Return standard collectible PvP card templates eligible for boosters.

    Non-basic resources use the normal card rarities and belong in this pool;
    basic resources use the ``Land`` rarity and remain excluded.  Equipment-
    modified/alternate-art printings are excluded from the standard pool.
    """
    connection = conn or _db_layer._db
    rows = connection.execute(
        "SELECT guid FROM card_templates "
        "WHERE is_pve=0 AND no_pvp=0 "
        "AND rarity IN ('Common','Uncommon','Rare','Legendary') "
        "AND COALESCE(equipment_modified,0)=0 "
        "ORDER BY guid"
    ).fetchall()
    return [row[0] for row in rows]


def db_deck_cards_json(deck_id, user_id, conn=None):
    """Return a deck's serialized card list when owned by the user."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT cards FROM decks WHERE id=? AND user_id=?",
        (int(deck_id), int(user_id))).fetchone()
    return row[0] if row else None


def db_deck_active_gems(deck_id, conn=None):
    """Return the serialized active-gem map for a deck."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT active_gems FROM decks WHERE id=?", (int(deck_id),)
    ).fetchone()
    return row[0] if row else None


def db_template_exists(template_guid, conn=None):
    """Whether a card template exists in the static card catalog."""
    connection = conn or _db_layer._db
    return bool(connection.execute(
        "SELECT 1 FROM card_templates WHERE guid=?", (template_guid,)
    ).fetchone())


def db_first_template_guid(conn=None):
    """Return a deterministic catalog fallback template, if available."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT guid FROM card_templates ORDER BY guid LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def db_gem_templates(conn=None):
    """Return authored gem names, types, and granted abilities."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gem_type_name, gem_type, abilities_json FROM gem_templates"
    ).fetchall()


def db_encounter_deck_cards(deck_guid, conn=None):
    """Return the authored card rows for an encounter deck."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT card_guid, quantity, gem_types_new_list_json "
        "FROM encounter_deck_cards WHERE deck_guid=?", (deck_guid,)
    ).fetchall()


def db_scene_mods(scene_guid, conn=None):
    """Return the serialized encounter-scene modifier payload."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT mods_json FROM encounter_scenes WHERE guid=?", (scene_guid,)
    ).fetchone()
    return row[0] if row else None


def db_template_ability_guids(template_guid, conn=None):
    """Return the authored ability GUID list for one card template."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT abilities_json FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else None


def db_ability_metadata(ability_guid, conn=None):
    """Return setup-relevant manual/trigger metadata for an ability."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT is_manual, trigger_event_type FROM card_abilities_meta "
        "WHERE ability_guid=?", (str(ability_guid).lower(),)
    ).fetchone()


def db_ability_raw_json(ability_guid, conn=None):
    """Return the raw authored metadata for one ability."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
        (str(ability_guid).lower(),)).fetchone()
    return row[0] if row else None


def db_any_ability_raw_json(ability_guid, conn=None):
    """Return raw metadata for a card or champion ability."""
    try:
        value = db_ability_raw_json(ability_guid, conn=conn)
    except Exception:
        value = None
    if value:
        return value
    try:
        row = (conn or _db_layer._db).execute(
            "SELECT raw_json FROM champion_abilities WHERE ability_guid=? LIMIT 1",
            (str(ability_guid).lower(),)).fetchone()
    except Exception:
        row = None
    return row[0] if row else None


def db_ability_target_template_ids(ability_guid, conn=None):
    """Return serialized authored target-template IDs for an ability."""
    row = (conn or _db_layer._db).execute(
        "SELECT target_template_ids FROM card_abilities_meta "
        "WHERE ability_guid=?", (str(ability_guid).lower(),)).fetchone()
    return row[0] if row else None


def db_ability_trigger_metadata(ability_guid, conn=None):
    """Return trigger/manual classification for one ability."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT is_triggered, is_manual, trigger_event_type "
        "FROM card_abilities_meta WHERE ability_guid=? LIMIT 1",
        (str(ability_guid).lower(),)).fetchone()


def db_ability_activation_metadata(ability_guid, conn=None):
    """Return activation limits, manual flag, and raw metadata."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT activation_cost, uses_per_game, uses_per_turn, "
        "exhausts_on_use, is_manual, raw_json FROM card_abilities_meta "
        "WHERE ability_guid=?", (str(ability_guid).lower(),)).fetchone()


def db_ability_effect_rows(ability_guid, conn=None):
    """Return BOM effect IDs, types, and parameters for an ability."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT effect_guid, effect_type, param FROM ability_effects "
        "WHERE ability_guid=?", (str(ability_guid).lower(),)
    ).fetchall()


def db_ability_effect_type_params(ability_guid, conn=None):
    """Return ordered effect types and serialized parameters."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT effect_type, param FROM ability_effects "
        "WHERE ability_guid=? ORDER BY effect_order",
        (str(ability_guid).lower(),)).fetchall()


def db_effect_parent_ability(effect_guid, conn=None):
    """Return the ability owning one play-card effect."""
    row = (conn or _db_layer._db).execute(
        "SELECT ability_guid FROM ability_effects WHERE effect_guid=? "
        "AND effect_type='PlayCardAbilityEffectTemplate' LIMIT 1",
        (effect_guid,)).fetchone()
    return row[0] if row else None


def db_ability_effect_target_index(ability_guid, effect_guid, conn=None):
    """Return the authored target index for one ability effect."""
    row = (conn or _db_layer._db).execute(
        "SELECT target_index FROM ability_effects WHERE ability_guid=? "
        "AND effect_guid=? LIMIT 1",
        (ability_guid, effect_guid)).fetchone()
    return row[0] if row else None


def db_ability_has_effect(ability_guid, conn=None):
    """Whether an ability has at least one materialized BOM effect."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT 1 FROM ability_effects WHERE ability_guid=? LIMIT 1",
        (str(ability_guid).lower(),)).fetchone()


def db_ability_metadata_exists(ability_guid, conn=None):
    """Whether a card ability is present in the materialized metadata."""
    return (conn or _db_layer._db).execute(
        "SELECT 1 FROM card_abilities_meta WHERE ability_guid=? LIMIT 1",
        (str(ability_guid).lower(),)).fetchone() is not None


def db_ability_target_template_ids(ability_guid, conn=None):
    """Return the serialized target-template IDs for an ability."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT target_template_ids FROM card_abilities_meta "
        "WHERE ability_guid=?", (str(ability_guid).lower(),)
    ).fetchone()
    return row[0] if row else None


def db_champion_ability_target_template_ids(ability_guid, conn=None):
    """Return target-template IDs authored for a champion ability."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT target_template_ids FROM champion_abilities "
        "WHERE ability_guid=? LIMIT 1", (str(ability_guid).lower(),)
    ).fetchone()
    return row[0] if row else None


def db_target_template_filter(template_id, conn=None):
    """Return one target template's serialized filter."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT filter_json FROM target_templates WHERE template_id=?",
        (template_id,)).fetchone()
    return row[0] if row else None


def db_template_subtype(template_guid, conn=None):
    """Return a card template's authored subtype."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT subtype FROM card_templates WHERE guid=?", (template_guid,)
    ).fetchone()
    return row[0] if row else None


def db_session_user_ids(session_id, conn=None):
    """Return distinct controllers represented by session cards."""
    return (conn or _db_layer._db).execute(
        "SELECT DISTINCT user_id FROM game_cards WHERE session_id=?",
        (session_id,)).fetchall()


def db_static_card_rows(session_id, zones, conn=None):
    """Return card fields needed by static filter evaluation."""
    if not zones:
        return []
    marks = ",".join("?" for _ in zones)
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
        "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
        "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
        "gc.card_attributes, ct.attributes FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.location IN (" + marks + ")",
        [session_id, *zones]).fetchall()


def db_card_stat_buffs(session_id, card_uid, conn=None):
    """Return persistent and temporary stat-buff payloads."""
    return (conn or _db_layer._db).execute(
        "SELECT permanent_buffs, temporary_buffs FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_property_state(session_id, card_uid, conn=None):
    """Return base/current stat fields used by typed CardPropertyVariable."""
    return (conn or _db_layer._db).execute(
        "SELECT COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
        "COALESCE(gc.card_attack_mod,0), COALESCE(gc.card_defense_mod,0), "
        "gc.permanent_buffs, gc.temporary_buffs FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_cost_location_state(session_id, card_uid, conn=None):
    """Return printed/instance cost fields and current zone."""
    return (conn or _db_layer._db).execute(
        "SELECT ct.cost, gc.card_cost_mod, gc.cost_mod_json, gc.location "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_static_row(session_id, card_uid, conn=None):
    """Return full card/template combat state for static evaluation."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_attack_mod, gc.card_defense_mod, gc.card_damage, "
        "gc.card_attributes, ct.attack, ct.defense, ct.attributes, "
        "gc.permanent_buffs, gc.temporary_buffs, gc.temporary_attributes, "
        "ct.rage_value, ct.lethal FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_combat_state(session_id, card_uid, conn=None):
    """Return combat stats and mutable payloads without optional columns."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_attack_mod, gc.card_defense_mod, gc.card_damage, "
        "gc.card_attributes, ct.attack, ct.defense, ct.attributes, "
        "gc.permanent_buffs, gc.temporary_buffs, gc.temporary_attributes "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_rage_lethal(session_id, card_uid, conn=None):
    """Return optional printed Rage/Lethal metadata when present."""
    connection = conn or _db_layer._db
    try:
        return connection.execute(
            "SELECT ct.rage_value, ct.lethal FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid=gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session_id, int(card_uid))).fetchone()
    except Exception:
        try:
            row = connection.execute(
                "SELECT ct.rage_value FROM game_cards gc "
                "JOIN card_templates ct ON ct.guid=gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (session_id, int(card_uid))).fetchone()
        except Exception:
            return None
        return (row[0], 0) if row else None


def db_warzone_card_uids(session_id, owner_id=None, conn=None):
    """Return warzone card UIDs, optionally restricted to one controller."""
    connection = conn or _db_layer._db
    if owner_id is None:
        return connection.execute(
            "SELECT card_uid FROM game_cards WHERE session_id=? "
            "AND location='warzone'", (session_id,)).fetchall()
    return connection.execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='warzone'", (session_id, int(owner_id))).fetchall()


def db_warzone_owner_ids(session_id, conn=None):
    """Return controllers with cards in the warzone."""
    return (conn or _db_layer._db).execute(
        "SELECT DISTINCT user_id FROM game_cards "
        "WHERE session_id=? AND location='warzone'", (session_id,)).fetchall()


def db_card_type_threshold(session_id, card_uid, conn=None):
    """Return a card's printed type and threshold metadata."""
    return (conn or _db_layer._db).execute(
        "SELECT ct.card_type, ct.threshold_json FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_next_card_uid(session_id, conn=None):
    """Allocate a collision-free client SessionCard UID."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT COALESCE(MAX(card_uid >> 8), 10000) + 1 "
        "FROM game_cards WHERE session_id=?", (session_id,)).fetchone()
    instance = max(10001, int(row[0] or 10001))
    from game_engine import UID
    while connection.execute(
            "SELECT 1 FROM game_cards WHERE session_id=? AND card_uid=? LIMIT 1",
            (session_id, UID.make(1, instance).uid64)).fetchone():
        instance += 1
    return UID.make(1, instance).uid64


def db_debug_template(template_guid, conn=None):
    """Return the template projection required by DebugCheatTransaction."""
    return (conn or _db_layer._db).execute(
        "SELECT guid, card_type, name, cost, attack, defense, "
        "abilities_json, attributes, threshold_json "
        "FROM card_templates WHERE guid=?", (str(template_guid).lower(),)
    ).fetchone()


def db_insert_debug_card(session_id, owner_id, card_uid, template, location,
                         conn=None):
    """Insert a card created by the client debug-cheat transaction."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location=?",
        (session_id, int(owner_id), location)).fetchone()
    position = int(row[0] or 0)
    if location == "deck":
        connection.execute(
            "UPDATE game_cards SET position=position+1 "
            "WHERE session_id=? AND user_id=? AND location='deck'",
            (session_id, int(owner_id)))
        position = 0
    row_id = db_next_game_card_row_id(session_id, conn=connection)
    connection.execute(
        "INSERT INTO game_cards (id, user_id, session_id, card_uid, "
        "card_template_id, location, position, is_champion, card_type, "
        "template_guid, owner_user_id, original_template_guid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (row_id, int(owner_id), session_id, int(card_uid), 0, location,
         position, template[1], template[0], int(owner_id), template[0]),
    )
    if conn is None:
        connection.commit()
    return int(card_uid), template


def db_debug_nonchampion_cards(session_id, owner_id, conn=None):
    """Return non-champion cards that a debug nuke may move to the void."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND is_champion=0 AND location!='void'",
        (session_id, int(owner_id))).fetchall()


def db_clear_warzone_damage(session_id, conn=None):
    """Clear marked damage from cards currently in the warzone."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET card_damage=0 "
        "WHERE session_id=? AND location='warzone'", (session_id,))


def db_temporary_attribute_rows(session_id, conn=None):
    """Return cards carrying temporary attributes or temporary buffs."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, user_id, temporary_attributes, temporary_buffs "
        "FROM game_cards WHERE session_id=? AND "
        "(temporary_attributes != 0 OR "
        "(temporary_buffs IS NOT NULL AND temporary_buffs != '{}'))",
        (session_id,)).fetchall()


def db_set_temporary_card_state(session_id, card_uid, attributes, buffs,
                                conn=None):
    """Persist temporary attributes and their expiration metadata."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET temporary_attributes=?, temporary_buffs=? "
        "WHERE session_id=? AND card_uid=?",
        (int(attributes), buffs, session_id, int(card_uid)))


def db_card_attribute_value(session_id, card_uid, field, conn=None):
    """Read one approved integer card-attribute field."""
    if field not in {"card_attributes", "temporary_attributes"}:
        raise ValueError("unsupported card attribute field")
    row = (conn or _db_layer._db).execute(
        "SELECT " + field + " FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row[0] if row else 0


def db_set_card_attribute_value(session_id, card_uid, field, value, conn=None):
    """Persist one approved integer card-attribute field."""
    if field not in {"card_attributes", "temporary_attributes"}:
        raise ValueError("unsupported card attribute field")
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET " + field + "=? WHERE session_id=? AND card_uid=?",
        (int(value), session_id, int(card_uid)))


def db_deck_shard_count(session_id, user_id, color, conn=None):
    """Count authored shard cards in one player's deck."""
    row = (conn or _db_layer._db).execute(
        "SELECT COUNT(*) FROM game_cards gc JOIN card_templates ct "
        "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.user_id=? "
        "AND gc.location='deck' AND ct.name LIKE ?",
        (session_id, int(user_id), f"%{color} Shard%")).fetchone()
    return int(row[0] if row else 0)


def db_deck_card_count(session_id, user_id, conn=None):
    """Count cards currently in one player's deck."""
    row = (conn or _db_layer._db).execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='deck'", (session_id, int(user_id))).fetchone()
    return int(row[0] if row else 0)


def db_talent_ability_condition(ability_guid, conn=None):
    """Return the talent and condition attached to an ability."""
    return (conn or _db_layer._db).execute(
        "SELECT talent_guid, condition FROM talent_abilities "
        "WHERE ability_guid=? LIMIT 1", (str(ability_guid),)).fetchone()


def db_talent_description(talent_guid, conn=None):
    """Return localized talent description metadata."""
    row = (conn or _db_layer._db).execute(
        "SELECT description FROM talent_data WHERE talent_guid=?",
        (str(talent_guid),)).fetchone()
    return row[0] if row else None


def db_talent_has_ability(talent_guid, conn=None):
    """Whether a talent has a materialized ability row."""
    return bool((conn or _db_layer._db).execute(
        "SELECT 1 FROM talent_abilities WHERE talent_guid=? LIMIT 1",
        (str(talent_guid),)).fetchone())


def db_talent_target_template_ids(ability_guid, conn=None):
    """Return talent ability target-template IDs."""
    row = (conn or _db_layer._db).execute(
        "SELECT target_template_ids FROM talent_abilities "
        "WHERE ability_guid=? LIMIT 1", (str(ability_guid),)).fetchone()
    return row[0] if row else None


def db_pregame_talent_rows(ability_guids, conn=None):
    """Return selected abilities authored for the pregame phase."""
    if not ability_guids:
        return []
    marks = ",".join("?" for _ in ability_guids)
    return (conn or _db_layer._db).execute(
        "SELECT DISTINCT ability_guid, condition FROM talent_abilities "
        "WHERE ability_guid IN (" + marks + ") AND (activatable_phases & 4) != 0",
        tuple(str(guid) for guid in ability_guids)).fetchall()


def db_card_list_stat_row(session_id, card_uid, conn=None):
    """Return the card/template fields used by list-property variables."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
        "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
        "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
        "gc.card_attributes, ct.attributes, gc.card_attack_mod, "
        "gc.card_defense_mod, gc.permanent_buffs, gc.temporary_buffs "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_static_target_template(template_id, conn=None):
    """Return target fields used by continuous static evaluation."""
    return (conn or _db_layer._db).execute(
        "SELECT collection_flags, player_filter, filter_json, game_text "
        "FROM target_templates WHERE template_id=?", (template_id,)).fetchone()


def db_ability_static_metadata(ability_guid, conn=None):
    """Return trigger/manual/raw metadata for static ability filtering."""
    return (conn or _db_layer._db).execute(
        "SELECT trigger_event_type, is_manual, raw_json "
        "FROM card_abilities_meta WHERE ability_guid=?",
        (str(ability_guid).lower(),)).fetchone()


def db_gem_abilities(gem_type, conn=None):
    """Return the serialized ability list for a gem type."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT abilities_json FROM gem_templates WHERE gem_type=?",
        (gem_type,)).fetchone()
    return row[0] if row else None


def db_set_card_abilities(session_id, card_uid, abilities_json, conn=None):
    """Persist the effective ability list for one materialized card."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET card_abilities=? "
        "WHERE session_id=? AND card_uid=?",
        (abilities_json, session_id, int(card_uid)),
    )


def db_set_card_abilities_and_attributes(session_id, card_uid, abilities_json,
                                         attributes, conn=None):
    """Persist effective abilities and attributes for one session card."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET card_abilities=?, card_attributes=? "
        "WHERE session_id=? AND card_uid=?",
        (abilities_json, int(attributes), session_id, int(card_uid)),
    )


def db_session_turn_order(session_id, conn=None):
    """Return the decoded session battle state, if one was persisted."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT turn_order_json FROM game_sessions WHERE session_id=?",
        (str(session_id),)).fetchone()
    return row[0] if row else None
    if conn is None:
        connection.commit()


def db_ai_hand_cards(session_id, conn=None):
    """Return AI hand cards in the client-visible position order."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=0 AND location='hand' "
        "ORDER BY position", (session_id,)).fetchall()


def db_ai_deck_top_card(session_id, conn=None):
    """Return the complete persisted row needed for an AI draw."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT id, card_uid, card_template_id, template_guid "
        "FROM game_cards WHERE session_id=? AND user_id=0 "
        "AND location='deck' ORDER BY position LIMIT 1", (session_id,)
    ).fetchone()


def db_ai_hand_tunneling_cards(session_id, conn=None):
    """Return non-resource AI hand cards and their tunneling metadata."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_uid, gc.template_guid, gc.card_state, "
        "gc.permanent_buffs, COALESCE(ct.cost, 0), ct.threshold_json, "
        "gc.card_abilities FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='hand' "
        "AND ct.card_type NOT LIKE '%Resource%' "
        "ORDER BY gc.position, gc.card_uid", (session_id,)).fetchall()


def db_target_template_info(template_id, conn=None):
    """Return filter/kind/auto fields for one target template."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT filter_json, target_kind, is_auto_target "
        "FROM target_templates WHERE template_id=?", (template_id,)
    ).fetchone()


def db_target_template_row(template_id, conn=None):
    """Return the complete target-template projection for resolution."""
    return (conn or _db_layer._db).execute(
        "SELECT template_id, game_text, is_auto_target, is_random_target, "
        "optional, explicit, player_filter, collection_flags, "
        "min_target_count, max_target_count, filter_json, target_kind "
        "FROM target_templates WHERE template_id=?", (template_id,)
    ).fetchone()


def db_session_card_owners(session_id, conn=None):
    """Return card UID/controller pairs for a session."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, user_id FROM game_cards WHERE session_id=?",
        (session_id,)).fetchall()


def db_cards_with_name(session_id, name, conn=None):
    """Return session card UIDs matching a template name case-insensitively."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND lower(ct.name)=lower(?)",
        (session_id, name)).fetchall()


def _target_projection_columns(connection):
    """Return optional columns used by the generic target projection."""
    gc = {row[1] for row in connection.execute(
        "PRAGMA table_info(game_cards)").fetchall()}
    ct = {row[1] for row in connection.execute(
        "PRAGMA table_info(card_templates)").fetchall()}
    return (
        "ct.rarity" if "rarity" in ct else "''",
        "ct.socket_count" if "socket_count" in ct else "0",
        "gc.gems" if "gems" in gc else "0",
        "gc.original_template_guid" if "original_template_guid" in gc else "''",
        "gc.card_abilities" if "card_abilities" in gc else "'[]'",
    )


def db_target_source_row(session_id, card_uid, conn=None):
    """Return the optional-column-safe source projection for targeting."""
    connection = conn or _db_layer._db
    rarity, sockets, gems, original, abilities = _target_projection_columns(connection)
    sql = ("SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
           + "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
           + "gc.template_guid, ct.name, COALESCE(ct.cost,0), ct.subtype, "
           + "ct.threshold_json, gc.card_attributes, " + rarity + ", "
           + sockets + ", " + gems + ", " + original + ", " + abilities + ", "
           + "gc.permanent_buffs FROM game_cards gc LEFT JOIN card_templates ct "
           + "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.card_uid=?")
    return connection.execute(
        sql,
        (session_id, int(card_uid))).fetchone()


def db_target_candidate_rows(session_id, zones, controller_uid=None,
                             both_players=False, top_n=False, conn=None):
    """Return optional-column-safe card candidates for target evaluation."""
    if not zones:
        return []
    connection = conn or _db_layer._db
    rarity, sockets, gems, original, _abilities = _target_projection_columns(connection)
    marks = ",".join("?" for _ in zones)
    sql = (
        "SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
        "gc.template_guid, gc.card_state, COALESCE(ct.attack,0), "
        "COALESCE(ct.defense,0), ct.name, COALESCE(ct.cost,0), ct.subtype, "
        "ct.threshold_json, gc.card_abilities, gc.permanent_buffs, "
        + rarity + ", " + sockets + ", " + gems + ", " + original + ", "
        + "COALESCE(gc.card_attributes,0) "
        + "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        + "WHERE gc.session_id=? AND gc.location IN (" + marks + ")"
    )
    params = [session_id] + list(zones)
    if not both_players:
        sql += " AND gc.user_id=?"
        params.append(controller_uid)
    sql += " ORDER BY gc.user_id, gc.position" if top_n else " ORDER BY gc.position"
    return connection.execute(sql, params).fetchall()


def db_gem_template_name(gem_type, conn=None):
    """Return the persisted display name for a socketed gem type."""
    row = (conn or _db_layer._db).execute(
        "SELECT gem_type_name FROM gem_templates WHERE gem_type=?",
        (int(gem_type),)).fetchone()
    return row[0] if row else None


def db_trace_card_rows(session_id, columns, conn=None):
    """Return trace columns, substituting NULL for legacy missing fields."""
    connection = conn or _db_layer._db
    valid = {row[1] for row in connection.execute(
        "PRAGMA table_info(game_cards)").fetchall()}
    selected = [column if column in valid else "NULL AS " + column
                for column in columns]
    return connection.execute(
        "SELECT card_uid, " + ", ".join(selected) +
        " FROM game_cards WHERE session_id=?", (session_id,)).fetchall()


def db_ai_evaluator_card_rows(session_id, user_id, zone, conn=None):
    """Return the joined card snapshot consumed by the AI evaluator."""
    if zone not in {"hand", "warzone"}:
        raise ValueError("unsupported evaluator zone")
    connection = conn or _db_layer._db
    ct_cols = {row[1] for row in connection.execute(
        "PRAGMA table_info(card_templates)").fetchall()}
    current = ("ct.current_resources_granted" if
               "current_resources_granted" in ct_cols else "0")
    maximum = ("ct.max_resources_granted" if
               "max_resources_granted" in ct_cols else "0")
    extra = ", gc.card_attack_mod, gc.card_defense_mod" if zone == "warzone" else ""
    return connection.execute(
        "SELECT gc.card_uid, gc.template_guid, gc.location, ct.card_type, "
        "ct.name, ct.rarity, ct.cost, ct.attack, ct.defense, "
        "ct.threshold_json, ct.abilities_json, ct.attributes, ct.subtype, "
        "ct.variable_cost, " + current + ", " + maximum + ", gc.card_state, "
        "gc.card_damage, gc.permanent_buffs, gc.temporary_buffs, "
        "gc.temporary_attributes" + extra + " FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location=? "
        "ORDER BY gc.position", (session_id, int(user_id), zone)).fetchall()


def db_target_template_targeting_info(template_id, conn=None):
    """Return display/filter fields used by legacy target classification."""
    return (conn or _db_layer._db).execute(
        "SELECT game_text, player_filter, filter_json FROM target_templates "
        "WHERE template_id=?", (template_id,)).fetchone()


def db_target_template_resolution_info(template_id, conn=None):
    """Return fields used by BOM target selection and randomization."""
    return (conn or _db_layer._db).execute(
        "SELECT filter_json, target_kind, is_random_target "
        "FROM target_templates WHERE template_id=?", (template_id,)).fetchone()


def db_target_template_definition(template_id, conn=None):
    """Return filter/kind/collection fields for target evaluation."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT filter_json, target_kind, collection_flags "
        "FROM target_templates WHERE template_id=?", (template_id,)
    ).fetchone()


def db_ability_is_triggered(ability_guid, conn=None):
    """Return the authored trigger classification for an ability."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT is_triggered FROM card_abilities_meta "
        "WHERE lower(ability_guid)=?", (str(ability_guid).lower(),)
    ).fetchone()
    return None if row is None else bool(row[0])


def db_ability_game_text(ability_guid, conn=None):
    """Return authored display text for one ability."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT game_text FROM card_abilities_meta WHERE ability_guid=?",
        (str(ability_guid).lower(),)).fetchone()
    return row[0] if row else None


def db_champion_ability_game_text(ability_guid, conn=None):
    """Return authored display text for one champion ability."""
    try:
        row = (conn or _db_layer._db).execute(
            "SELECT game_text FROM champion_abilities "
            "WHERE ability_guid=? LIMIT 1",
            (str(ability_guid).lower(),)).fetchone()
    except Exception:
        # Minimal RulesPort/leaf fixtures may omit this optional projection.
        return None
    return row[0] if row else None


def db_effect_type(effect_guid, conn=None):
    """Return the materialized effect type for one BOM effect."""
    row = (conn or _db_layer._db).execute(
        "SELECT effect_type FROM ability_effects WHERE effect_guid=? LIMIT 1",
        (effect_guid,)).fetchone()
    return row[0] if row else None


def db_effect_param_for_ability_types(ability_guid, effect_types, conn=None):
    """Return the first BOM parameter matching any supplied effect type."""
    if not effect_types:
        return None
    marks = ",".join("?" for _ in effect_types)
    row = (conn or _db_layer._db).execute(
        "SELECT param FROM ability_effects WHERE ability_guid=? "
        f"AND effect_type IN ({marks}) LIMIT 1",
        [str(ability_guid).lower(), *effect_types]).fetchone()
    return row[0] if row else None


def db_template_attributes(template_guid, conn=None):
    """Return a card template's static attribute bitmask."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT attributes FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else None


def db_card_mutation_info(session_id, card_uid, conn=None):
    """Return template, owner, and type for a mutable session card."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT template_guid, user_id, card_type FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_mutation_snapshot(session_id, card_uid, conn=None):
    """Return template, owner, zone, and state for card mutation paths."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT template_guid, user_id, location, card_state "
        "FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_modifier_state(session_id, card_uid, conn=None):
    """Return card identity, zone, state, and permanent buff payload."""
    return (conn or _db_layer._db).execute(
        "SELECT template_guid, user_id, location, card_state, permanent_buffs "
        "FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_transform_target_info(session_id, card_uid, conn=None):
    """Return the complete card/template projection used by random transforms."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.template_guid, gc.card_type, gc.location, gc.user_id, "
        "gc.card_state, gc.permanent_buffs, ct.name, ct.cost, ct.rarity, "
        "ct.threshold_json, ct.subtype, ct.attributes, gc.card_attributes "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_transform_candidate_templates(conn=None):
    """Return eligible PVP transform candidate templates.

    Equipment-modified printings are not valid generated-card choices.  They
    are encounter/deck equipment variants, even when their base card type and
    PvP flags look collectible.
    """
    connection = conn or _db_layer._db
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(card_templates)").fetchall()}
    rarity_expr = "rarity" if "rarity" in columns else "''"
    threshold_expr = ("threshold_json" if "threshold_json" in columns
                      else "'[]'")
    socket_expr = "socket_count" if "socket_count" in columns else "0"
    is_pve_expr = "is_pve" if "is_pve" in columns else "0"
    no_pvp_expr = "no_pvp" if "no_pvp" in columns else "0"
    equipment_expr = ("equipment_modified" if "equipment_modified" in columns
                      else "0")
    return connection.execute(
        "SELECT guid, name, card_type, cost, " + rarity_expr + ", "
        + threshold_expr + ", subtype, attributes, " + socket_expr
        + " FROM card_templates WHERE COALESCE(" + is_pve_expr + ", 0)=0 "
        + "AND COALESCE(" + no_pvp_expr + ", 0)=0 "
        + "AND COALESCE(" + equipment_expr + ", 0)=0 "
        "AND card_type NOT LIKE '%Choice%'"
    ).fetchall()


def db_resolve_talent_modified_template(template_guid, talent_guids,
                                         conn=None):
    """Select the authored alternate card for the active champion talents.

    CardTemplate's serialized TAC is the client-owned source of the
    alternate-version relationship and its RequiredTalents conditions.  Do
    not select a variant by card name or by the ability text: the same rule is
    used for every talent-modified generated card.
    """
    active = {str(value).lower() for value in (talent_guids or [])}
    if not active:
        return str(template_guid).lower()
    from gamedata import DEFAULT_RECORD_STORE
    from abilities.framework.tac import (_tac_attr_hash, decode_tac_tree)
    record = DEFAULT_RECORD_STORE.get("CardTemplate", str(template_guid))
    if record is None:
        return str(template_guid).lower()
    serialized = record.field("m_SerializedTAC", {}) or {}
    data = serialized.get("data") if isinstance(serialized, dict) else serialized
    tree = decode_tac_tree(data)
    alternates = tree.get(_tac_attr_hash("AlternateVersions"), [])
    chosen = str(template_guid).lower()
    best = 0
    connection = conn or _db_layer._db
    for alternate in alternates if isinstance(alternates, list) else []:
        if not isinstance(alternate, dict):
            continue
        guid = alternate.get(_tac_attr_hash("Guid"))
        condition = alternate.get(_tac_attr_hash("Condition"), {})
        required = condition.get(_tac_attr_hash("RequiredTalents"), []) \
            if isinstance(condition, dict) else []
        required = {
            str(item.get(_tac_attr_hash("Guid"))).lower()
            for item in required if isinstance(item, dict)
        }
        if not guid or not required.issubset(active) or len(required) <= best:
            continue
        exists = connection.execute(
            "SELECT 1 FROM card_templates WHERE guid=? LIMIT 1", (str(guid),)
        ).fetchone()
        if exists:
            chosen, best = str(guid).lower(), len(required)
    return chosen


def db_copy_template_payload(template_guid, conn=None, talent_guids=None):
    """Return typed fields needed to materialize a generated copy."""
    resolved = db_resolve_talent_modified_template(
        template_guid, talent_guids, conn=conn)
    return (conn or _db_layer._db).execute(
        "SELECT card_type, abilities_json, attributes FROM card_templates "
        "WHERE guid=?", (resolved,)).fetchone()


def db_next_game_card_row_id(session_id, conn=None):
    """Allocate the historical per-session generated-card row ID."""
    row = (conn or _db_layer._db).execute(
        "SELECT COALESCE(MAX(id), 10000) + 1 FROM game_cards "
        "WHERE session_id=?", (session_id,)).fetchone()
    return int(row[0])


def db_deck_next_position(session_id, owner_id, conn=None):
    """Return the next stable position for a generated deck card."""
    row = (conn or _db_layer._db).execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck'",
        (session_id, int(owner_id))).fetchone()
    return int(row[0])


def db_randomly_insert_deck_cards(session_id, user_id, card_uids,
                                  connection=None, conn=None):
    """Reinsert selected cards at random deck slots without shuffling others.

    This is the persistence operation behind ``shuffle into deck`` effects.
    The selected cards are randomized among random slots while the relative
    order of all unselected cards remains unchanged.
    """
    import random as _shuf_rnd

    connection = connection or conn or _db_layer._db
    wanted = {int(uid) for uid in (card_uids or [])}
    if not wanted:
        return []
    rows = connection.execute(
        "SELECT card_uid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck' "
        "ORDER BY position", (session_id, user_id)).fetchall()
    if not rows:
        return []
    if len(wanted) == 1:
        card_uid = next(iter(wanted))
        exists = connection.execute(
            "SELECT 1 FROM game_cards WHERE session_id=? AND user_id=? "
            "AND card_uid=? LIMIT 1",
            (session_id, user_id, card_uid)).fetchone()
        if not exists:
            return []
        # Rebuild the deck order with the card at a uniformly random slot.
        # ``rows`` already contains the card when the caller moved it into the
        # deck before randomizing its slot (the "put into deck" leaf does
        # exactly this), so exclude it before choosing the slot.  Shifting the
        # in-place positions instead left a gap and could place the card at
        # ``deck_count`` (past the end).
        deck_uids = [int(row[0]) for row in rows]
        others = [uid for uid in deck_uids if uid != int(card_uid)]
        insert_position = _shuf_rnd.randrange(len(others) + 1)
        ordered = (others[:insert_position] + [int(card_uid)]
                   + others[insert_position:])
        assignments = " ".join("WHEN ? THEN ?" for _uid in ordered)
        params = []
        for position, uid in enumerate(ordered):
            params.extend((uid, position))
        marks = ",".join("?" for _ in ordered)
        connection.execute(
            "UPDATE game_cards SET location='deck', position=CASE card_uid "
            + assignments + " ELSE position END "
            "WHERE session_id=? AND user_id=? AND card_uid IN (" + marks + ")",
            (*params, session_id, user_id, *ordered))
        if connection is _db_layer._db:
            connection.commit()
        return [card_uid]
    selected = [int(row[0]) for row in rows if int(row[0]) in wanted]
    if not selected:
        return []
    _shuf_rnd.shuffle(selected)
    slots = list(range(len(rows)))
    _shuf_rnd.shuffle(slots)
    selected_by_slot = dict(zip(sorted(slots[:len(selected)]), selected))
    remaining = [int(row[0]) for row in rows if int(row[0]) not in wanted]
    remaining_iter = iter(remaining)
    ordered = []
    for rank in range(len(rows)):
        ordered.append(selected_by_slot[rank] if rank in selected_by_slot
                       else next(remaining_iter))
    # Assign the complete permutation in one set-based statement.  The CASE
    # expression gives each card its final position without issuing one UPDATE
    # per card through executemany().
    assignments = " ".join(
        "WHEN ? THEN ?" for _card_uid in ordered)
    params = []
    for position, card_uid in enumerate(ordered):
        params.extend((card_uid, position))
    connection.execute(
        "UPDATE game_cards SET position=CASE card_uid "
        + assignments + " END "
        "WHERE session_id=? AND user_id=? AND location='deck'",
        (*params, session_id, user_id))
    if connection is _db_layer._db:
        connection.commit()
    return [card_uid for card_uid in ordered if card_uid in wanted]


def db_set_card_played_to_zone(session_id, card_uid, location, conn=None):
    """Move a played card to its resolved zone at the client sentinel slot."""
    connection = conn or _db_layer._db
    cursor = connection.execute(
        "UPDATE game_cards SET location=?, position=? "
        "WHERE session_id=? AND card_uid=?",
        (location, PLAYED_CARD_POSITION, session_id, int(card_uid)),
    )
    if conn is None:
        connection.commit()
    return cursor.rowcount


def db_insert_generated_card(session_id, owner_id, card_uid, template_guid,
                             location, card_type, abilities_json, attributes,
                             row_id, conn=None, position=0, card_state=0,
                             owner_user_id=None, original_template_guid=None,
                             gems=None, permanent_buffs=None):
    """Insert a generated copy in the caller's transaction."""
    connection = conn or _db_layer._db
    columns = ["id", "session_id", "user_id", "card_uid", "template_guid",
               "card_template_id", "location", "position", "card_state",
               "card_abilities", "card_type", "card_attributes"]
    values = [int(row_id), session_id, int(owner_id), int(card_uid),
              template_guid, template_guid, location, int(position),
              int(card_state), abilities_json or "[]", card_type,
              int(attributes or 0)]
    optional = {
        "owner_user_id": owner_id if owner_user_id is None else owner_user_id,
        "original_template_guid": (template_guid if original_template_guid is None
                                    else original_template_guid),
        "gems": gems,
        "permanent_buffs": permanent_buffs,
    }
    existing = {row[1] for row in connection.execute(
        "PRAGMA table_info(game_cards)").fetchall()}
    for column, value in optional.items():
        if column in existing and value is not None:
            columns.append(column)
            values.append(value)
    marks = ",".join("?" for _ in columns)
    return connection.execute(
        "INSERT INTO game_cards (" + ",".join(columns) + ") VALUES (" +
        marks + ")", values)


def db_ability_effect_metadata_rows(ability_guid, conn=None):
    """Return effect IDs/types for creation-replacement inspection."""
    return (conn or _db_layer._db).execute(
        "SELECT effect_guid, effect_type FROM ability_effects "
        "WHERE ability_guid=?", (str(ability_guid).lower(),)).fetchall()


def db_card_template_ability_payload(template_guid, conn=None):
    """Return the serialized ability list for a card template."""
    row = (conn or _db_layer._db).execute(
        "SELECT abilities_json FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else None


def db_choice_card_rows(session_id, conn=None):
    """Return temporary choice cards awaiting selection."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, user_id, template_guid, card_type FROM game_cards "
        "WHERE session_id=? AND location='choosing'", (session_id,)).fetchall()


def db_move_choice_to_played_resources(session_id, card_uid, conn=None):
    """Consume a temporary choice card without granting resources."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET location='PlayedResources', position=0, "
        "card_state=0 WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid)))


def db_template_catalog_for_filter(conn=None):
    """Return typed card-template fields used by random-card filters."""
    return (conn or _db_layer._db).execute(
        "SELECT guid, name, card_type, cost, attack, defense, attributes, "
        "subtype, rarity, socket_count, threshold_json, is_pve, no_pvp "
        "FROM card_templates").fetchall()


def db_card_cost_state(session_id, card_uid, conn=None):
    """Return card/template fields needed for a cost-modifier projection."""
    return (conn or _db_layer._db).execute(
        "SELECT template_guid, card_template_id, user_id, location, "
        "cost_mod_json FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_add_card_cost_modifier(session_id, card_uid, delta, conn=None):
    """Apply an additive cost modifier without forcing a commit."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET card_cost_mod = "
        "COALESCE(card_cost_mod, 0) + ? WHERE session_id=? AND card_uid=?",
        (int(delta), session_id, int(card_uid)))


def db_set_card_cost_formulas(session_id, card_uid, formulas, conn=None):
    """Persist serialized dynamic cost formulas without forcing a commit."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET cost_mod_json=? "
        "WHERE session_id=? AND card_uid=?",
        (formulas, session_id, int(card_uid)))


def db_tame_card(session_id, card_uid, permanent_buffs, card_state, conn=None):
    """Persist capture markers and move the captured card to the void."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET permanent_buffs=?, location='void', "
        "position=0, card_state=? WHERE session_id=? AND card_uid=?",
        (permanent_buffs, int(card_state), session_id, int(card_uid)))


def db_card_owner_id(session_id, card_uid, conn=None):
    """Return the persisted controller of a session card."""
    connection = conn or _db_layer._db
    row = connection.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row[0] if row else None


def db_card_original_owner_id(session_id, card_uid, conn=None):
    """Return original owner, falling back to current controller."""
    row = (conn or _db_layer._db).execute(
        "SELECT COALESCE(owner_user_id, user_id) FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row[0] if row else None


def db_card_state_value(session_id, card_uid, conn=None):
    """Return the persisted state bitmask for one session card."""
    row = (conn or _db_layer._db).execute(
        "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row[0] if row else None


def db_add_temporary_attributes(session_id, card_uid, attributes, conn=None,
                                owner_id=None, boundary=None):
    """OR temporary card attributes and optionally record their expiry.

    A grant written without an expiry rule is cleared at the affected card's
    next owner boundary, which is wrong for a grant with an authored duration
    that is issued inside the same turn-start sequence it belongs to (a
    Tunneling Surface resolves at StartTurn and the Prep that follows would
    clear it before the turn it was granted for has ended).  Callers that know
    the duration pass ``owner_id``/``boundary`` so the shared
    ``clear_expired_temporary_attributes`` readers expire it at the right
    boundary; the metadata key and rule shape are theirs.
    """
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET temporary_attributes = "
        "COALESCE(temporary_attributes, 0) | ? "
        "WHERE session_id=? AND card_uid=?",
        (int(attributes), session_id, int(card_uid)))
    if not attributes or owner_id is None or not boundary:
        return
    row = connection.execute(
        "SELECT COALESCE(temporary_attributes, 0), temporary_buffs "
        "FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row:
        return
    bits = int(row[0] or 0) & int(attributes)
    try:
        buffs = json.loads(row[1] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if not isinstance(buffs, dict):
        buffs = {}
    metadata = buffs.setdefault("__attribute_expirations", {})
    for bit in (1 << index for index in range(bits.bit_length())
                if bits & (1 << index)):
        metadata[str(bit)] = {"owner": int(owner_id),
                              "boundary": str(boundary)}
    connection.execute(
        "UPDATE game_cards SET temporary_buffs=? "
        "WHERE session_id=? AND card_uid=?",
        (json.dumps(buffs, separators=(",", ":"), sort_keys=True), session_id,
         int(card_uid)))


def db_warzone_cards_with_state(session_id, conn=None):
    """Return all warzone cards with template, owner, and state."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, template_guid, user_id, card_state "
        "FROM game_cards WHERE session_id=? AND location='warzone'",
        (session_id,)).fetchall()


def db_warzone_troop_state_rows(session_id, conn=None):
    """Return mutable combat fields for warzone troops."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, gc.template_guid, ct.defense, "
        "gc.card_defense_mod, gc.card_damage, gc.permanent_buffs, "
        "gc.temporary_buffs FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%'", (session_id,)).fetchall()


def db_card_death_info(session_id, card_uid, conn=None):
    """Return template, controller, and effective authored attributes."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.template_guid, gc.user_id, "
        "(ct.attributes | gc.card_attributes) FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_kill_card_to_discard(session_id, card_uid, clear_mask, dead_state,
                            conn=None):
    """Move a troop to its discard pile and apply the death state reset."""
    connection = conn or _db_layer._db
    owner_row = connection.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not owner_row:
        return None
    owner = int(owner_row[0])
    position = connection.execute(
        "SELECT COALESCE(MAX(position) + 1, 1) FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='discard'",
        (session_id, owner)).fetchone()[0]
    connection.execute(
        "UPDATE game_cards SET user_id=?, location='discard', position=?, "
        "card_state=(card_state & ~?) | ?, card_damage=0, "
        "temporary_buffs='{}', temporary_attributes=0 "
        "WHERE session_id=? AND card_uid=?",
        (owner, int(position or 1), int(clear_mask), int(dead_state),
         session_id, int(card_uid)))
    return owner


def db_transform_card_instance(session_id, card_uid, template_guid, card_type,
                               abilities_json, attributes, location, position,
                               card_state, conn=None):
    """Apply the canonical template projection for an existing card instance."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET template_guid=?, card_template_id=?, "
        "card_type=?, card_abilities=?, card_attributes=?, "
        "temporary_attributes=0, temporary_buffs='{}', location=?, "
        "position=?, card_state=? WHERE session_id=? AND card_uid=?",
        (template_guid, template_guid, card_type, abilities_json,
         int(attributes or 0), location, int(position), int(card_state),
         session_id, int(card_uid)))


def db_warzone_card_state_attributes(session_id, card_uid, conn=None):
    """Return persisted state and temporary attributes for one warzone card."""
    return (conn or _db_layer._db).execute(
        "SELECT card_state, temporary_attributes FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (session_id, int(card_uid))
    ).fetchone()


def db_card_source_info(session_id, card_uid, conn=None):
    """Return template, type, zone, and owner for a public source card."""
    return (conn or _db_layer._db).execute(
        "SELECT template_guid, card_type, location, user_id FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_card_command_info(session_id, user_id, card_uid, conn=None):
    """Return instance/template IDs and zone for a commanded card."""
    return (conn or _db_layer._db).execute(
        "SELECT card_template_id, location, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND card_uid=?",
        (session_id, int(user_id), int(card_uid))).fetchone()


def db_template_projection(template_guid, conn=None):
    """Return template fields required for a CardUpdated projection."""
    return (conn or _db_layer._db).execute(
        "SELECT card_type, cost, attack, defense, threshold_json, abilities_json "
        "FROM card_templates WHERE guid=?", (template_guid,)).fetchone()


def db_card_effective_combat_stats(session_id, card_uid, conn=None):
    """Return effective attack, attributes, and remaining defense."""
    return (conn or _db_layer._db).execute(
        "SELECT COALESCE(ct.attack,0)+COALESCE(gc.card_attack_mod,0), "
        "(COALESCE(ct.attributes,0) | COALESCE(gc.card_attributes,0)), "
        "COALESCE(ct.defense,0)+COALESCE(gc.card_defense_mod,0) "
        "-COALESCE(gc.card_damage,0) FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_battle_target_stats(session_id, card_uids, conn=None):
    """Return UID, effective attributes, and remaining defense for targets."""
    if not card_uids:
        return []
    placeholders = ",".join("?" * len(card_uids))
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, (COALESCE(ct.attributes,0) | "
        "COALESCE(gc.card_attributes,0)), COALESCE(ct.defense,0) "
        "+COALESCE(gc.card_defense_mod,0)-COALESCE(gc.card_damage,0) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        f"WHERE gc.session_id=? AND gc.card_uid IN ({placeholders})",
        (session_id, *[int(uid) for uid in card_uids])).fetchall()


def db_ability_target_effect_rows(ability_guid, conn=None):
    """Return target-indexed effects in authored order."""
    return (conn or _db_layer._db).execute(
        "SELECT target_index, effect_type FROM ability_effects "
        "WHERE ability_guid=? AND target_index>=0 ORDER BY effect_order",
        (str(ability_guid).lower(),)).fetchall()


def db_champion_trigger_ability_guids(champion_guid, conn=None):
    """Return metadata-indexed triggered abilities for a champion."""
    return (conn or _db_layer._db).execute(
        "SELECT ca.ability_guid FROM champion_abilities ca "
        "JOIN card_abilities_meta cam ON cam.ability_guid=ca.ability_guid "
        "WHERE ca.champion_guid=? AND cam.trigger_event_type IS NOT NULL "
        "AND cam.trigger_event_type != '' ORDER BY ca.ability_guid",
        (str(champion_guid),)).fetchall()


def db_card_cost(session_id, card_uid, conn=None):
    """Return the printed cost for a session card."""
    row = (conn or _db_layer._db).execute(
        "SELECT ct.cost FROM game_cards gc JOIN card_templates ct "
        "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row[0] if row else None


def db_card_is_battleboard(session_id, card_uid, conn=None):
    """Whether a session card uses the hidden battleboard subtype."""
    return (conn or _db_layer._db).execute(
        "SELECT 1 FROM game_cards gc JOIN card_templates ct "
        "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.card_uid=? "
        "AND LOWER(COALESCE(ct.subtype,''))='battleboard' LIMIT 1",
        (session_id, int(card_uid))).fetchone()


def db_warzone_ability_cards(session_id, include_non_troops=False, conn=None):
    """Return AI-eligible warzone cards and their authored ability payload."""
    sql = ("SELECT gc.card_uid, gc.template_guid, gc.card_state, ct.attributes, "
           "gc.card_attributes, gc.card_abilities FROM game_cards gc "
           "JOIN card_templates ct ON ct.guid=gc.template_guid "
           "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='warzone'")
    if not include_non_troops:
        sql += " AND gc.card_type LIKE '%Troop%'"
    return (conn or _db_layer._db).execute(sql, (session_id,)).fetchall()


def db_warzone_troop_stats(session_id, user_id, conn=None):
    """Return UID and mutable/base combat stats for a player's troops."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, ct.attack, ct.defense, gc.card_defense_mod, "
        "gc.card_damage, gc.card_state, gc.position FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%'",
        (session_id, user_id)).fetchall()


def db_warzone_card_stats(session_id, user_id=None, conn=None):
    """Return UID and combat stats for warzone permanents."""
    sql = ("SELECT gc.card_uid, ct.attack, ct.defense, gc.card_defense_mod, "
           "gc.card_damage, gc.card_state, gc.position, gc.card_type "
           "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
           "WHERE gc.session_id=? AND gc.location='warzone'")
    params = [session_id]
    if user_id is not None:
        sql += " AND gc.user_id=?"
        params.append(user_id)
    return (conn or _db_layer._db).execute(sql, params).fetchall()


def db_card_state_rows(session_id, card_uids, conn=None):
    """Return ``(card_uid, card_state)`` for selected session cards."""
    if not card_uids:
        return []
    marks = ",".join("?" for _ in card_uids)
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, card_state FROM game_cards WHERE session_id=? "
        "AND card_uid IN (" + marks + ")",
        [session_id] + [int(uid) for uid in card_uids]).fetchall()


def db_ai_hand_summary(session_id, conn=None):
    """Return AI hand count and resource count in position-independent form."""
    return (conn or _db_layer._db).execute(
        "SELECT COUNT(*), SUM(CASE WHEN COALESCE(ct.card_type, gc.card_type)="
        "'Resource' THEN 1 ELSE 0 END) FROM game_cards gc "
        "LEFT JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='hand'",
        (session_id,)).fetchone()


def db_ai_zone_rows(session_id, zone, limit=None, conn=None):
    """Return AI game-card IDs/Uids ordered by authoritative deck position."""
    sql = ("SELECT id, card_uid FROM game_cards WHERE session_id=? "
           "AND user_id=0 AND location=? ORDER BY position")
    params = [session_id, zone]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return (conn or _db_layer._db).execute(sql, params).fetchall()


def db_deck_top_card_details(session_id, user_id, conn=None):
    """Return the top deck row needed for a draw transaction."""
    return (conn or _db_layer._db).execute(
        "SELECT id, card_uid, card_template_id, template_guid "
        "FROM game_cards WHERE session_id=? AND user_id=? AND location='deck' "
        "ORDER BY position LIMIT 1", (session_id, user_id)).fetchone()


def db_move_card_row_to_hand(session_id, row_id, conn=None):
    """Move one identified session-card row into hand."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET location='hand', position=100 "
        "WHERE session_id=? AND id=?", (session_id, int(row_id))).rowcount


def db_draw_card_to_hand(session_id, row_id, owner_user_id, conn=None):
    """Move one deck row to hand and assign its current controller."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET user_id=?, location='hand', position=100 "
        "WHERE session_id=? AND id=?",
        (int(owner_user_id), session_id, int(row_id))).rowcount


def db_void_card(session_id, card_uid, conn=None):
    """Move a session card to void without forcing a commit."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET location='void', position=0 "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).rowcount


def db_set_card_owner(session_id, card_uid, owner_user_id, conn=None):
    """Change current control of a session card without changing provenance."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET user_id=? WHERE session_id=? AND card_uid=?",
        (int(owner_user_id), session_id, int(card_uid))).rowcount


def db_steal_card_to_hand(session_id, card_uid, owner_user_id, conn=None):
    """Take a deck card into hand, clearing transient state."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET user_id=?, location='hand', position=100, "
        "card_state=0 WHERE session_id=? AND card_uid=?",
        (int(owner_user_id), session_id, int(card_uid))).rowcount


def db_zone_card_count(session_id, user_id, zone, card_type=None, conn=None):
    """Count cards in a player's session zone, optionally by exact type."""
    connection = conn or _db_layer._db
    sql = "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? AND location=?"
    params = [session_id, user_id, zone]
    if card_type is not None:
        sql += " AND card_type=?"
        params.append(card_type)
    row = connection.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def db_warzone_defender_rows(session_id, user_id, tapped_mask, conn=None):
    """Return untapped troop UIDs and effective attributes for blocking."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.card_uid, (ct.attributes | gc.card_attributes | "
        "COALESCE(gc.temporary_attributes, 0)) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%' AND (gc.card_state & ?) = 0",
        (session_id, user_id, tapped_mask)).fetchall()


def db_hand_card_uids(session_id, user_id, conn=None):
    """Return hand card UIDs in authoritative position order."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='hand' ORDER BY position", (session_id, user_id)
    ).fetchall()


def db_talent_ability_exists(ability_guid, conn=None):
    """Whether an ability GUID belongs to the champion/talent catalog."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT 1 FROM talent_abilities WHERE ability_guid=? LIMIT 1",
        (ability_guid,)).fetchone()


def db_card_zone_projection(session_id, card_uid, conn=None):
    """Return template/type/state for a client zone update."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT template_guid, card_type, card_state FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (session_id, int(card_uid))
    ).fetchone()


def db_card_gem_type(session_id, card_uid, conn=None):
    """Return the socketed gem type persisted on a session card."""
    row = (conn or _db_layer._db).execute(
        "SELECT gems FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return row[0] if row else None


def db_card_owner_zone_state(session_id, card_uid, conn=None):
    """Return owner, zone, and state for a selected cost card."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT user_id, location, card_state FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (session_id, int(card_uid))
    ).fetchone()


def db_card_sacrifice_info(session_id, card_uid, conn=None):
    """Return owner, zone, and type for a sacrifice validation."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT user_id, location, card_type FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (session_id, int(card_uid))
    ).fetchone()


def db_ordered_zone_rows(session_id, user_id, zone, conn=None):
    """Return database ID and card UID rows in zone position order."""
    return (conn or _db_layer._db).execute(
        "SELECT id, card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location=? ORDER BY position, id", (session_id, user_id, zone)
    ).fetchall()


def db_card_combat_identity(session_id, card_uid, conn=None):
    """Return owner, zone, template, and mutable combat stats for a card."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.user_id, gc.location, gc.template_guid, "
        "COALESCE(ct.attack,0)+COALESCE(gc.card_attack_mod,0), "
        "COALESCE(ct.defense,0)+COALESCE(gc.card_defense_mod,0) "
        "FROM game_cards gc LEFT JOIN card_templates ct "
        "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_warzone_troop_uids(session_id, conn=None):
    """Return warzone troop UIDs in stable database order."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? "
        "AND location='warzone' AND card_type LIKE '%Troop%'",
        (session_id,)).fetchall()


def db_warzone_troop_uids_except_owner(session_id, owner_id, conn=None):
    """Return warzone troop UIDs controlled by another player."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? "
        "AND user_id<>? AND location='warzone' "
        "AND card_type LIKE '%Troop%'",
        (session_id, int(owner_id))).fetchall()


def db_warzone_troop_uids_for_owner(session_id, owner_id, conn=None):
    """Return warzone troop UIDs controlled by one player."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? "
        "AND location='warzone' AND card_type LIKE '%Troop%' AND user_id=?",
        (session_id, int(owner_id))).fetchall()


def db_owner_card_locations(session_id, owner_id, conn=None):
    """Return all cards controlled by an owner with their current zones."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, location FROM game_cards "
        "WHERE session_id=? AND user_id=?",
        (session_id, int(owner_id))).fetchall()


def db_counter_template_name(template_id, conn=None):
    """Return the display name for a serialized counter template ID."""
    row = (conn or _db_layer._db).execute(
        "SELECT name FROM card_counter_templates WHERE template_id=?",
        (template_id,)).fetchone()
    return row[0] if row else None


def db_counter_template_id(name, conn=None):
    """Return the counter-template ID matching a display name."""
    row = (conn or _db_layer._db).execute(
        "SELECT template_id FROM card_counter_templates "
        "WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
    return row[0] if row else None


def db_underground_card_rows(session_id, owner_id, conn=None):
    """Return underground card UIDs and templates for one controller."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='underground'",
        (session_id, int(owner_id))).fetchall()


def db_visibility_underground_rows(session_id, conn=None):
    """Return underground cards and ability payloads for visibility scans."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, user_id, card_abilities FROM game_cards "
        "WHERE session_id=? AND location='underground'", (session_id,)).fetchall()


def db_visibility_hand_rows(session_id, user_id, conn=None):
    """Return hand projections used when revealing an opponent's hand."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, template_guid, user_id, card_type, card_state "
        "FROM game_cards WHERE session_id=? AND user_id=? AND location='hand' "
        "ORDER BY position, id", (session_id, int(user_id))).fetchall()


def db_card_effective_defense(session_id, card_uid, conn=None):
    """Return printed plus mutable defense for one session card."""
    return (conn or _db_layer._db).execute(
        "SELECT ct.defense, gc.card_defense_mod FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_deck_card_uids(session_id, user_id, limit, conn=None):
    """Return the top deck card UIDs for an owner."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='deck' ORDER BY position, id LIMIT ?",
        (session_id, int(user_id), int(limit))).fetchall()


def db_set_card_owners(card_owners, conn=None):
    """Apply current-controller changes to multiple session cards."""
    if not card_owners:
        return
    (conn or _db_layer._db).executemany(
        "UPDATE game_cards SET user_id=? WHERE session_id=? AND card_uid=?",
        [(int(owner), session_id, int(uid))
         for session_id, uid, owner in card_owners])


def db_deck_top_card_type(session_id, user_id, conn=None):
    """Return the top deck card UID and type."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, card_type FROM game_cards WHERE session_id=? "
        "AND user_id=? AND location='deck' ORDER BY position, id LIMIT 1",
        (session_id, int(user_id))).fetchone()


def db_move_card_to_discard_reset(session_id, card_uid, conn=None):
    """Mill one card to discard and clear its transient state."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET location='discard', position=0, card_state=0 "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).rowcount


def db_ordered_zone_uids(session_id, user_id, location, conn=None):
    """Return all card UIDs in an owned zone by authoritative order."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location=? ORDER BY position, id",
        (session_id, int(user_id), location)).fetchall()


def db_set_card_positions(card_positions, conn=None):
    """Persist ordered positions for session cards."""
    if not card_positions:
        return
    (conn or _db_layer._db).executemany(
        "UPDATE game_cards SET position=? WHERE session_id=? AND card_uid=?",
        [(int(position), session_id, int(uid))
         for session_id, uid, position in card_positions])


def db_hand_resources_with_template(session_id, user_id, conn=None):
    """Return hand resources with their printed grants and authored abilities."""
    connection = conn or _db_layer._db
    return connection.execute(
        "SELECT gc.id, gc.card_uid, gc.template_guid, "
        "COALESCE(ct.current_resources_granted, 0), "
        "COALESCE(ct.max_resources_granted, 0), ct.abilities_json "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' "
        "AND gc.card_type='Resource' ORDER BY gc.position LIMIT 1",
        (session_id, user_id),
    ).fetchall()


def db_hand_cards_with_templates(session_id, user_id, conn=None):
    """Return ordered hand cards with the fields used by play-option logic."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, ct.cost, ct.card_type, ct.threshold_json, "
        "ct.abilities_json FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' "
        "ORDER BY gc.position", (session_id, user_id)).fetchall()


def db_hand_cards_raw(session_id, user_id, conn=None):
    """Return ordered ``(card_uid, card_template_id, template_guid)`` rows."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, card_template_id, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='hand' "
        "ORDER BY position", (session_id, user_id)).fetchall()


def db_hand_cards_full(session_id, user_id, conn=None):
    """Return ordered ``(card_uid, card_type, template_guid)`` hand rows."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.card_uid, gc.card_type, gc.template_guid FROM game_cards gc "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' "
        "ORDER BY gc.position", (session_id, user_id)).fetchall()


def db_card_template_attrs_joined(session_id, card_uid, conn=None):
    """Return template and instance attributes for one game card."""
    return (conn or _db_layer._db).execute(
        "SELECT gc.template_guid, ct.attributes, gc.card_attributes "
        "FROM game_cards gc LEFT JOIN card_templates ct "
        "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.card_uid=?",
        (session_id, int(card_uid))).fetchone()


def db_resource_selection_card(session_id, card_uid, conn=None):
    """Return the resource fields needed by a free resource play."""
    connection = conn or _db_layer._db
    try:
        return connection.execute(
            "SELECT gc.template_guid, gc.card_template_id, ct.card_type, "
            "ct.current_resources_granted, ct.max_resources_granted, "
            "ct.threshold_json, ct.abilities_json "
            "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session_id, int(card_uid))).fetchone()
    except Exception as exc:
        if "current_resources_granted" not in str(exc):
            raise
        row = connection.execute(
            "SELECT gc.template_guid, gc.card_template_id, ct.card_type, "
            "ct.threshold_json, ct.abilities_json "
            "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session_id, int(card_uid))).fetchone()
        return ((row[0], row[1], row[2], 1, 1, row[3], row[4])
                if row else None)


def db_reveal_card_row(session_id, card_uid, owner_id, zone, conn=None):
    """Return one owner/zone card row used by reveal effects."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, position, template_guid, user_id, card_state "
        "FROM game_cards WHERE session_id=? AND card_uid=? "
        "AND user_id=? AND location=?",
        (session_id, int(card_uid), int(owner_id), zone)).fetchone()


def db_reveal_owned_card(session_id, card_uid, owner_id, conn=None):
    """Return one owned card row and its current zone for source reveals."""
    return (conn or _db_layer._db).execute(
        "SELECT card_uid, position, template_guid, user_id, card_state, "
        "location FROM game_cards WHERE session_id=? AND card_uid=? "
        "AND user_id=?",
        (session_id, int(card_uid), int(owner_id))).fetchone()


def db_reveal_cards(session_id, owner_id, zone, limit, card_uids=None,
                    conn=None):
    """Return ordered reveal rows, optionally restricted to candidate UIDs."""
    connection = conn or _db_layer._db
    params = [session_id, int(owner_id), zone]
    sql = ("SELECT card_uid, position, template_guid, user_id, card_state "
           "FROM game_cards WHERE session_id=? AND user_id=? "
           "AND location=?")
    if card_uids is not None:
        if not card_uids:
            return []
        marks = ",".join("?" for _ in card_uids)
        sql += " AND card_uid IN (" + marks + ")"
        params.extend(int(uid) for uid in card_uids)
    sql += " ORDER BY position"
    if card_uids is None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return connection.execute(sql, params).fetchall()


def db_move_card_to_played_resources(session_id, card_uid, conn=None):
    """Move a selected resource to the played-resources zone."""
    return (conn or _db_layer._db).execute(
        "UPDATE game_cards SET location='PlayedResources', position=9999 "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid)))


def db_starting_hand_candidates(session_id, user_id, card_types=None,
                                conn=None):
    """Return hand cards eligible for metadata-driven opening effects."""
    connection = conn or _db_layer._db
    if card_types:
        placeholders = ",".join("?" for _ in card_types)
        return connection.execute(
            "SELECT card_uid, template_guid, permanent_buffs FROM game_cards "
            "WHERE session_id=? AND user_id=? AND location='hand' "
            "AND card_type IN (" + placeholders + ")",
            (session_id, int(user_id), *card_types)).fetchall()
    return connection.execute(
        "SELECT card_uid, template_guid, permanent_buffs FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='hand' "
        "AND card_type LIKE '%Troop%'", (session_id, int(user_id))).fetchall()


def db_apply_starting_hand_effect(session_id, card_uid, cost_mod=0,
                                  permanent_buffs=None, conn=None):
    """Apply an opening-hand cost/rage mutation without committing."""
    connection = conn or _db_layer._db
    if cost_mod:
        connection.execute(
            "UPDATE game_cards SET card_cost_mod=COALESCE(card_cost_mod, 0) + ? "
            "WHERE session_id=? AND card_uid=?",
            (int(cost_mod), session_id, int(card_uid)))
    if permanent_buffs is not None:
        connection.execute(
            "UPDATE game_cards SET permanent_buffs=? WHERE session_id=? "
            "AND card_uid=?", (permanent_buffs, session_id, int(card_uid)))


def db_cleanup_game_sessions(conn=None):
    """Remove ended sessions and cards orphaned by session cleanup."""
    connection = conn or _db_layer._db
    connection.execute("DELETE FROM game_sessions WHERE state='ended'")
    connection.execute(
        "DELETE FROM game_cards WHERE session_id NOT IN "
        "(SELECT session_id FROM game_sessions)"
    )
    if conn is None:
        connection.commit()


def db_ai_hand_playables(session_id, user_id, kind, conn=None):
    """Return AI hand cards eligible for the requested play family."""
    connection = conn or _db_layer._db
    if kind == "permanent":
        predicate = "(ct.card_type LIKE '%Troop%' OR ct.card_type LIKE '%Artifact%' OR ct.card_type LIKE '%Constant%')"
    elif kind == "spell":
        predicate = "ct.card_type IN ('BasicAction','QuickAction')"
    else:
        raise ValueError("unknown AI play family")
    return connection.execute(
        "SELECT gc.id, gc.card_uid, gc.template_guid, ct.cost, ct.card_type, "
        "ct.threshold_json, ct.attack, ct.defense FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' AND "
        + predicate + " ORDER BY gc.position",
        (session_id, user_id),
    ).fetchall()


def db_move_card_if_in_zone(session_id, card_uid, owner_user_id, source_zone,
                            destination_zone, position=0, card_state=None,
                            conn=None):
    """Move an owned card only if it is still in the expected source zone."""
    connection = conn or _db_layer._db
    if card_state is None:
        cursor = connection.execute(
            "UPDATE game_cards SET location=?, position=? "
            "WHERE session_id=? AND card_uid=? AND user_id=? AND location=?",
            (destination_zone, position, session_id, int(card_uid),
             owner_user_id, source_zone),
        )
    else:
        cursor = connection.execute(
            "UPDATE game_cards SET location=?, position=?, card_state=? "
            "WHERE session_id=? AND card_uid=? AND user_id=? AND location=?",
            (destination_zone, position, card_state, session_id, int(card_uid),
             owner_user_id, source_zone),
        )
    connection.commit()
    return int(cursor.rowcount or 0)


def db_add_card_damage(session_id, card_uid, amount, conn=None):
    """Add combat damage to one card; the caller owns the transaction."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET card_damage = card_damage + ? "
        "WHERE session_id=? AND card_uid=?",
        (int(amount), session_id, int(card_uid)),
    )


def db_reset_warzone_troop(session_id, card_uid, clear_state_bits, conn=None):
    """Clear a troop's turn/combat state and reset marked combat damage."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET card_state = card_state & ~?, card_damage=0 "
        "WHERE session_id=? AND card_uid=?",
        (clear_state_bits, session_id, int(card_uid)),
    )


def db_update_card_state(session_id, card_uid, set_bits=0, clear_bits=0,
                         reset_damage=False, conn=None):
    """Apply state bit changes to one card without forcing a commit."""
    connection = conn or _db_layer._db
    damage_sql = ", card_damage=0" if reset_damage else ""
    connection.execute(
        "UPDATE game_cards SET card_state = (card_state | ?) & ~?" + damage_sql
        + " WHERE session_id=? AND card_uid=?",
        (set_bits, clear_bits, session_id, int(card_uid)),
    )


def db_move_card_to_location(session_id, card_uid, location, position=0,
                             reset_state=False, conn=None):
    """Move a session card to a zone, optionally clearing its state bits."""
    if location not in {"deck", "hand", "warzone", "discard", "void",
                        "underground", "resources", "playedresources"}:
        raise ValueError("unsupported card location")
    connection = conn or _db_layer._db
    state_sql = ", card_state=0" if reset_state else ""
    cur = connection.execute(
        "UPDATE game_cards SET location='" + location + "', position=?" + state_sql +
        " WHERE session_id=? AND card_uid=?",
        (int(position), session_id, int(card_uid)))
    return getattr(cur, "rowcount", None)


def db_set_card_state_exact(session_id, card_uid, state, conn=None):
    """Persist an exact card-state bitmask without forcing a commit."""
    connection = conn or _db_layer._db
    cur = connection.execute(
        "UPDATE game_cards SET card_state=? WHERE session_id=? AND card_uid=?",
        (int(state), session_id, int(card_uid)))
    return getattr(cur, "rowcount", None)


def db_clear_warzone_states(session_id, clear_state_bits, conn=None):
    """Clear combat-state bits from every card currently in the warzone."""
    connection = conn or _db_layer._db
    connection.execute(
        "UPDATE game_cards SET card_state = card_state & ~? "
        "WHERE session_id=? AND location='warzone'",
        (clear_state_bits, session_id),
    )
    connection.commit()


def db_move_card_rows(row_ids, destination_zone, position=0, conn=None):
    """Move cards identified by stable game-card row IDs without committing."""
    if not row_ids:
        return
    connection = conn or _db_layer._db
    connection.executemany(
        "UPDATE game_cards SET location=?, position=? WHERE id=?",
        [(destination_zone, position, int(row_id)) for row_id in row_ids],
    )


def db_set_card_row_positions(row_positions, conn=None):
    """Set positions for card rows without committing the caller transaction."""
    if not row_positions:
        return
    connection = conn or _db_layer._db
    connection.executemany(
        "UPDATE game_cards SET position=? WHERE id=?",
        [(int(position), int(row_id)) for row_id, position in row_positions],
    )


def db_insert_game_cards(rows, conn=None):
    """Insert prepared player or opponent cards with the same card shape."""
    if not rows:
        return
    connection = conn or _db_layer._db
    connection.executemany(
        "INSERT INTO game_cards (user_id, session_id, card_uid, "
        "card_template_id, card_type, template_guid, location, position, "
        "owner_user_id, original_template_guid, card_abilities, "
        "card_attributes, card_uses, gems) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def db_update_game_card_abilities(rows, conn=None):
    """Update prepared ability lists after player deck materialization."""
    if not rows:
        return
    connection = conn or _db_layer._db
    connection.executemany(
        "UPDATE game_cards SET card_abilities=? "
        "WHERE session_id=? AND card_uid=?", rows)


__all__ = [name for name in globals() if name.startswith("db_")]
