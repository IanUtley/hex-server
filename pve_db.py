"""Campaign and PVE persistence API.

PVE and PVP share sessions, cards and event storage. This module therefore
owns campaign/FRA-specific queries rather than pretending PVE has a separate
physical database.
"""

import db as _db_module
import json
import random
from domain.enums import ECardTypes, ETurnPhases

from pvp_db import db_delete_game_session, db_champion_template_health


def _connection(conn=None):
    return conn if conn is not None else _db_module._db


def db_get_player_champion_guid(deck_db_id, conn=None):
    """Return the PVP champion GUID selected on a saved player deck."""
    row = _connection(conn).execute(
        "SELECT pvp_champion_guid FROM decks WHERE id=?",
        (int(deck_db_id),)).fetchone()
    guid = row[0] if row else None
    return guid if guid and guid != "00000000-0000-0000-0000-000000000000" else None


def db_get_champion_guid(pve_champion_id, conn=None):
    """Return the champion template GUID for a PVE champion row."""
    row = _connection(conn).execute(
        "SELECT ct.guid FROM champion_templates ct "
        "JOIN champions c ON ct.race=c.race "
        "AND ct.champion_class=c.champion_class AND ct.gender=c.gender "
        "AND ct.is_player=1 WHERE c.id=?",
        (int(pve_champion_id),)).fetchone()
    return row[0] if row else None


def db_get_arena_state(user_id, initialize=True, conn=None):
    """Return one player's FRA state, optionally creating its row."""
    connection = _connection(conn)
    if initialize:
        connection.execute(
            "INSERT OR IGNORE INTO arena_state (user_id) VALUES (?)",
            (user_id,))
        if conn is None:
            connection.commit()
    row = connection.execute(
        "SELECT deck_id, wins, losses, challenger_index, fight_history, "
        "gold_earned, chests_earned, sacks_earned FROM arena_state "
        "WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return {"deck_id": 0, "wins": 0, "losses": 0,
                "challenger_index": 0, "fight_history": "[]",
                "gold_earned": 0, "chests_earned": 0, "sacks_earned": 0}
    return {
        "deck_id": row[0] or 0, "wins": row[1] or 0,
        "losses": row[2] or 0, "challenger_index": row[3] or 0,
        "fight_history": row[4] or "[]", "gold_earned": row[5] or 0,
        "chests_earned": row[6] or 0, "sacks_earned": row[7] or 0,
    }


def db_get_arena_fight_history(user_id, conn=None):
    """Return the fixed FRA fight-slot projection expected by the client."""
    try:
        raw = json.loads(db_get_arena_state(user_id, conn=conn)
                         .get("fight_history", "[]") or "[]")
    except (TypeError, ValueError):
        raw = []
    history = []
    for index in range(20):
        item = raw[index] if index < len(raw) and isinstance(raw[index], dict) else {}
        result = str(item.get("result", "NONE") or "NONE").upper()
        history.append({
            "fight_id": int(item.get("fight_id", index + 1) or index + 1),
            "fight_tier": int(item.get("fight_tier", index // 5 + 1) or index // 5 + 1),
            "fight_order": int(item.get("fight_order", index + 1) or index + 1),
            "challenger_instance": int(item.get("challenger_instance", index + 1) or index + 1),
            "result": "LOSE" if result == "LOSS" else result,
            "is_boss": item.get("is_boss"),
            "round_challenge": str(item.get("round_challenge", "") or ""),
            "challenge_response": str(item.get("challenge_response", "NONE") or "NONE").upper(),
            "active_challenges": [str(guid) for guid in item.get("active_challenges", [])
                                  if guid and str(guid) != "00000000-0000-0000-0000-000000000000"],
        })
    return history


def db_get_fra_challenge(conversation_guid=None, challenge_key=None, conn=None):
    """Return one enabled extracted FRA challenge by GUID or stable key."""
    if conversation_guid is None and challenge_key is None:
        return None
    column, value = ("conversation_guid", conversation_guid) if conversation_guid is not None else ("challenge_key", challenge_key)
    row = _connection(conn).execute(
        "SELECT conversation_guid, challenge_key, challenge_name, "
        "challenge_order, probability_percent, dialogue_text, answer_text, "
        "objective_heading, objective_text, modifications_json, metadata_json "
        "FROM fra_challenges WHERE " + column + "=? AND enabled=1",
        (str(value),)).fetchone()
    if not row:
        return None
    keys = ("conversation_guid", "challenge_key", "challenge_name",
            "challenge_order", "probability_percent", "dialogue_text",
            "answer_text", "objective_heading", "objective_text",
            "modifications_json", "metadata_json")
    return dict(zip(keys, row))


def db_update_arena_state(user_id, conn=None, **kwargs):
    """Update selected FRA state fields in the caller's transaction."""
    if not kwargs:
        return
    invalid = set(kwargs) - {
        "deck_id", "wins", "losses", "challenger_index", "fight_history",
        "gold_earned", "chests_earned", "sacks_earned",
    }
    if invalid:
        raise ValueError("unsupported arena state fields: " + ", ".join(sorted(invalid)))
    connection = _connection(conn)
    assignments = ", ".join(key + "=?" for key in kwargs)
    connection.execute("UPDATE arena_state SET " + assignments + " WHERE user_id=?",
                       (*kwargs.values(), user_id))
    if conn is None:
        connection.commit()


def db_get_active_fra_challenges(user_id, conn=None):
    """Return challenge definitions active for the current FRA run."""
    history = db_get_arena_fight_history(user_id, conn=conn)
    if not history:
        return []
    guids = list(history[0].get("active_challenges", []))
    if not guids and history[0].get("round_challenge"):
        guids = [history[0]["round_challenge"]]
    return [challenge for guid in guids
            if (challenge := db_get_fra_challenge(conversation_guid=guid,
                                                  conn=conn))]


def db_get_fra_challengers(user_id, conn=None):
    """Return the saved FRA opponent roster for one player."""
    rows = _connection(conn).execute(
        "SELECT challenger_index, name, champion_guid, encounter_deck_guid, "
        "is_boss FROM fra_challengers WHERE user_id=? "
        "ORDER BY challenger_index", (user_id,)).fetchall()
    return [{"id": row[0] + 1, "name": row[1], "champion_guid": row[2],
             "deck": row[3], "boss": "True" if row[4] else "False"}
            for row in rows]


def db_clear_fra_challengers(user_id, conn=None):
    """Remove the saved FRA opponent roster."""
    connection = _connection(conn)
    connection.execute("DELETE FROM fra_challengers WHERE user_id=?", (user_id,))
    if conn is None:
        connection.commit()


def db_create_fra_challengers(user_id, rng=None, conn=None):
    """Select and persist the twenty authored FRA opponents."""
    rows = _connection(conn).execute(
        "SELECT deck_guid, name, champion_guid, COALESCE(is_boss, 0), "
        "COALESCE(is_elite, 0), base_deck_name, COALESCE(min_rank, 6), "
        "COALESCE(max_rank, 19) FROM fra_encounters").fetchall()
    encounters = [{"deck": row[0], "name": row[1], "champion": row[2],
                   "is_boss": bool(row[3]), "is_elite": bool(row[4]),
                   "base": row[5], "min_rank": row[6], "max_rank": row[7]}
                  for row in rows]
    from gamemodes.arena import is_boss_encounter, select_fra_roster
    selected = [(user_id, rank - 1, chosen["name"], chosen["champion"],
                 chosen["deck"], int(is_boss_encounter(chosen)))
                for rank, chosen in select_fra_roster(encounters, rng=rng)]
    connection = _connection(conn)
    connection.execute("DELETE FROM fra_challengers WHERE user_id=?", (user_id,))
    connection.executemany(
        "INSERT INTO fra_challengers (user_id, challenger_index, name, "
        "champion_guid, encounter_deck_guid, is_boss) VALUES (?, ?, ?, ?, ?, ?)",
        selected)
    if conn is None:
        connection.commit()
    return db_get_fra_challengers(user_id, conn=connection)


def db_roll_fra_start_challenge(user_id, rng=None, conn=None):
    """Select and persist the optional challenge for a fresh FRA run."""
    arena = db_get_arena_state(user_id, conn=conn)
    if int(arena.get("challenger_index", 0) or 0) != 0:
        return None
    history = db_get_arena_fight_history(user_id, conn=conn)
    existing = history[0].get("round_challenge", "") if history else ""
    if existing:
        return db_get_fra_challenge(conversation_guid=existing, conn=conn)
    challenge = db_get_fra_challenge(challenge_key="starting_health_15", conn=conn)
    if not challenge:
        return None
    probability = max(0, min(100, int(challenge.get("probability_percent", 5) or 0)))
    if (rng or random.SystemRandom()).randrange(100) >= probability:
        return None
    history[0]["round_challenge"] = challenge["conversation_guid"]
    history[0]["active_challenges"] = [challenge["conversation_guid"]]
    db_update_arena_state(user_id, conn=conn, fight_history=json.dumps(history))
    return challenge


def db_record_arena_fight(user_id, won, conn=None):
    """Record the current FRA result and advance its roster index."""
    arena = db_get_arena_state(user_id, conn=conn)
    challengers = db_get_fra_challengers(user_id, conn=conn)
    index = int(arena.get("challenger_index", 0) or 0)
    if index >= len(challengers):
        return False
    history = db_get_arena_fight_history(user_id, conn=conn)
    result = "WIN" if won else "LOSE"
    if history[index]["result"] not in ("WIN", "LOSE"):
        challenger = challengers[index]
        history[index].update({
            "result": result, "challenger_instance": challenger["id"],
            "fight_id": challenger["id"], "fight_tier": index // 5 + 1,
            "fight_order": index + 1, "is_boss": challenger["boss"] == "True",
        })
        gold = int(arena.get("gold_earned", 0) or 0)
        chests = int(arena.get("chests_earned", 0) or 0)
        if won:
            if challenger["boss"] == "True":
                chests += 1
            else:
                gold += 1
        db_update_arena_state(
            user_id, conn=conn, wins=int(arena.get("wins", 0) or 0) + int(bool(won)),
            losses=int(arena.get("losses", 0) or 0) + int(not won),
            challenger_index=index + 1, fight_history=json.dumps(history),
            gold_earned=gold, chests_earned=chests)
    return True


def db_get_fra_public_base_encounter(deck_guid, conn=None):
    """Return the public base encounter behind an elite FRA deck."""
    row = _connection(conn).execute(
        "SELECT base.deck_guid, base.name, base.champion_guid "
        "FROM fra_encounters AS selected JOIN fra_encounters AS base "
        "ON base.base_deck_name=selected.base_deck_name "
        "AND COALESCE(base.is_elite, 0)=0 WHERE selected.deck_guid=? "
        "AND COALESCE(selected.is_elite, 0)=1 ORDER BY base.deck_guid LIMIT 1",
        (deck_guid,)).fetchone()
    return {"deck": row[0], "name": row[1], "champion_guid": row[2]} if row else None


def db_next_campaign_id(conn=None):
    """Return the next campaign row ID without exposing SQL to handlers."""
    row = _connection(conn).execute(
        "SELECT COALESCE(MAX(id), 0) + 1 FROM campaigns").fetchone()
    return int(row[0])


def db_campaign_champion(champion_id, conn=None):
    """Return the champion fields needed to materialize campaign state."""
    return _connection(conn).execute(
        "SELECT id, user_id, race, champion_name, level FROM champions WHERE id=?",
        (champion_id,),
    ).fetchone()


def db_campaign_for_champion(champion_id, campaign_type="PANORAMA", conn=None):
    """Return the existing campaign identity/state for a champion and type."""
    return _connection(conn).execute(
        "SELECT id, camp_uid_lo, camp_uid_hi, is_started, state_json "
        "FROM campaigns WHERE champion_id=? AND campaign_type=?",
        (champion_id, campaign_type),
    ).fetchone()


def db_latest_campaign_for_champion(champion_id, campaign_type, conn=None):
    """Return the newest campaign identity/state without creating one."""
    return _connection(conn).execute(
        "SELECT id, camp_uid_lo, camp_uid_hi, is_started, state_json "
        "FROM campaigns WHERE champion_id=? AND campaign_type=? "
        "ORDER BY id DESC LIMIT 1",
        (champion_id, campaign_type),
    ).fetchone()


def db_campaign_user_id(champion_id, conn=None):
    row = _connection(conn).execute(
        "SELECT user_id FROM champions WHERE id=?", (champion_id,)).fetchone()
    return row[0] if row else None


def db_latest_campaign_for_user(user_id, conn=None):
    """Return the newest campaign ID belonging to a profile."""
    return _connection(conn).execute(
        "SELECT c.id FROM campaigns c JOIN champions ch "
        "ON ch.id=c.champion_id WHERE ch.user_id=? "
        "ORDER BY c.id DESC LIMIT 1", (int(user_id),)).fetchone()


def db_create_campaign(campaign_id, instance_id, champion_id, user_id,
                       champion_name, template_name, campaign_type,
                       state_json, conn=None, *, is_started=False):
    """Insert one campaign row; the caller owns the surrounding transaction."""
    _connection(conn).execute(
        "INSERT INTO campaigns (id, camp_uid_lo, camp_uid_hi, champion_id, user_id, "
        "champion_name, template_name, campaign_type, is_started, state_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (campaign_id, instance_id, 0, champion_id, user_id, champion_name,
         template_name, campaign_type, int(bool(is_started)), state_json),
    )


def db_campaign_update_state(campaign_id, state_json, conn=None, *, started=None,
                             template_name=None):
    """Persist campaign state, optionally changing its started/template flags."""
    connection = _connection(conn)
    assignments = ["state_json=?"]
    params = [state_json]
    if started is not None:
        assignments.append("is_started=?")
        params.append(int(bool(started)))
    if template_name is not None:
        assignments.append("template_name=?")
        params.append(template_name)
    params.append(campaign_id)
    connection.execute(
        "UPDATE campaigns SET " + ", ".join(assignments) + " WHERE id=?",
        params,
    )


def db_promote_campaign_to_dungeon(campaign_id, state_json, conn=None):
    """Promote an existing campaign row to the Crayburn dungeon."""
    _connection(conn).execute(
        "UPDATE campaigns SET campaign_type='DUNGEON', "
        "template_name='Crayburn Castle', is_started=1, state_json=? "
        "WHERE id=?", (state_json, campaign_id))


def db_latest_quest_state(champion_id, conn=None):
    """Return the newest quest campaign ID and serialized state."""
    row = _connection(conn).execute(
        "SELECT id, state_json FROM campaigns WHERE champion_id=? "
        "AND campaign_type='QUEST' ORDER BY id DESC LIMIT 1", (champion_id,)
    ).fetchone()
    return row


def db_latest_campaign_any(champion_id, conn=None):
    """Return the newest campaign identity/state for a champion."""
    return _connection(conn).execute(
        "SELECT id, champion_id, state_json FROM campaigns "
        "WHERE champion_id=? ORDER BY id DESC LIMIT 1", (champion_id,)
    ).fetchone()


def db_campaign_forfeit_row(campaign_id, conn=None):
    """Return the fields needed to forfeit a campaign."""
    return _connection(conn).execute(
        "SELECT champion_id, state_json, campaign_type FROM campaigns "
        "WHERE id=?", (campaign_id,)).fetchone()


def db_campaign_clear_state(campaign_id, conn=None):
    """Clear a saved state for an explicit campaign reset operation."""
    _connection(conn).execute(
        "UPDATE campaigns SET state_json=NULL WHERE id=?", (campaign_id,))


def db_campaign_set_last_campaign(champion_id, campaign_id, conn=None):
    _connection(conn).execute(
        "UPDATE champions SET last_campaign_id=? WHERE id=?",
        (campaign_id, champion_id),
    )


def db_quest_conversations(quest_script, conn=None):
    """Return enabled authored quest conversation variants."""
    return _connection(conn).execute(
        "SELECT conversation_guid, role, faction, conversation_name "
        "FROM quest_conversations WHERE quest_script=? AND enabled=1 "
        "ORDER BY priority, conversation_guid", (str(quest_script),)
    ).fetchall()


def db_encounter_scene_exists(scene_guid, conn=None):
    """Whether an authored encounter scene exists."""
    return bool(_connection(conn).execute(
        "SELECT 1 FROM encounter_scenes WHERE guid=?", (scene_guid,)
    ).fetchone())


def db_az1_scene_rows(conn=None):
    """Return AZ1 node scene identity/display fields for objective matching."""
    return _connection(conn).execute(
        "SELECT guid, name, title FROM encounter_scenes "
        "WHERE name LIKE 'AZ 1 - NODE %' ORDER BY name"
    ).fetchall()


def db_az1_scene_metadata(conn=None):
    """Return AZ1 scene identity, names, and authored rewards."""
    return _connection(conn).execute(
        "SELECT guid, name, rewards_json FROM encounter_scenes "
        "WHERE name LIKE 'AZ 1 - NODE %'"
    ).fetchall()


def db_az1_edges(conn=None):
    """Return the authored directed AZ1 map edges."""
    return _connection(conn).execute(
        "SELECT from_node, to_node, path_name FROM campaign_node_edges "
        "WHERE campaign_template='AZ1'"
    ).fetchall()


def db_az1_edge_exists(from_node, to_node, conn=None):
    """Whether two AZ1 nodes are directly connected."""
    return bool(_connection(conn).execute(
        "SELECT 1 FROM campaign_node_edges "
        "WHERE campaign_template='AZ1' AND from_node=? AND to_node=?",
        (str(from_node), str(to_node))).fetchone())


def db_az1_fork_edge_exists(from_node, to_node, conn=None):
    """Whether two AZ1 nodes connect through one client-only fork node."""
    return bool(_connection(conn).execute(
        "SELECT 1 FROM campaign_node_edges first "
        "JOIN campaign_node_edges second ON second.campaign_template=first.campaign_template "
        "AND second.from_node=first.to_node "
        "WHERE first.campaign_template='AZ1' AND first.from_node=? "
        "AND second.to_node=? AND lower(first.to_node) LIKE 'fork%'",
        (str(from_node), str(to_node))).fetchone())


def db_quest_template(script_name=None, campaign_group=None, conn=None):
    """Return the first enabled quest template matching the requested scope."""
    sql = ("SELECT script_name, title, objectives_json, campaign_group, "
           "start_hook FROM quest_templates WHERE enabled=1")
    params = []
    if script_name:
        sql += " AND script_name=?"
        params.append(script_name)
    if campaign_group:
        sql += " AND campaign_group=?"
        params.append(campaign_group)
    sql += " ORDER BY script_name LIMIT 1"
    return _connection(conn).execute(sql, params).fetchone()


def db_quest_campaign_id(champion_id, script_name, conn=None):
    """Return an existing quest campaign ID for a champion/script."""
    row = _connection(conn).execute(
        "SELECT id FROM campaigns WHERE champion_id=? "
        "AND campaign_type='QUEST' AND template_name=?",
        (champion_id, script_name),
    ).fetchone()
    return row[0] if row else None


def db_campaign_state_rows(champion_id, campaign_type, conn=None,
                           *, template_name=None, exclude_template=None,
                           require_state=True, newest_first=False):
    """Return campaign IDs/state JSON for one champion and campaign type."""
    sql = ("SELECT id, state_json FROM campaigns "
           "WHERE champion_id=? AND campaign_type=?")
    params = [champion_id, campaign_type]
    if template_name is not None:
        sql += " AND template_name=?"
        params.append(template_name)
    if exclude_template is not None:
        sql += " AND template_name<>?"
        params.append(exclude_template)
    if require_state:
        sql += " AND state_json IS NOT NULL"
    sql += " ORDER BY id " + ("DESC" if newest_first else "ASC")
    return _connection(conn).execute(sql, params).fetchall()


def db_active_campaign_candidates(champion_id, campaign_type, template=None,
                                  conn=None):
    """Find active campaigns using the protocol's legacy fallback order."""
    connection = _connection(conn)
    rows = []
    if template:
        rows = connection.execute(
            "SELECT id, camp_uid_lo, campaign_type, template_name "
            "FROM campaigns WHERE champion_id=? AND campaign_type=? "
            "AND lower(template_name)=lower(?)",
            (champion_id, campaign_type, template)).fetchall()
    if not rows and template:
        rows = connection.execute(
            "SELECT id, camp_uid_lo, campaign_type, template_name "
            "FROM campaigns WHERE champion_id=? "
            "AND lower(template_name)=lower(?)",
            (champion_id, template)).fetchall()
    if not rows:
        rows = connection.execute(
            "SELECT id, camp_uid_lo, campaign_type, template_name "
            "FROM campaigns WHERE champion_id=? AND campaign_type=?",
            (champion_id, campaign_type)).fetchall()
    if not rows and (not campaign_type or campaign_type == "ANY"):
        rows = connection.execute(
            "SELECT id, camp_uid_lo, campaign_type, template_name "
            "FROM campaigns WHERE champion_id=?", (champion_id,)).fetchall()
    return [row for row in rows if connection.execute(
        "SELECT json_extract(state_json, '$.Finished') FROM campaigns "
        "WHERE id=?", (row[0],)).fetchone()[0] is None]


def db_latest_campaign_state(champion_id, campaign_type, conn=None,
                             *, template_name=None):
    """Return the newest campaign identity and state for a champion."""
    sql = ("SELECT id, camp_uid_lo, camp_uid_hi, state_json FROM campaigns "
           "WHERE champion_id=? AND campaign_type=?")
    params = [champion_id, campaign_type]
    if template_name is not None:
        sql += " AND template_name=?"
        params.append(template_name)
    sql += " ORDER BY id DESC LIMIT 1"
    return _connection(conn).execute(sql, params).fetchone()


def db_gaal_nodes(campaign_template, conn=None):
    """Return enabled authored Gaal camp node IDs for an area."""
    return _connection(conn).execute(
        "SELECT DISTINCT node_id FROM campaign_node_conversations "
        "WHERE campaign_template=? AND enabled=1 "
        "AND lower(conversation_name) LIKE '%gaal%' "
        "AND lower(conversation_name) NOT LIKE '%already has fortune%'",
        (str(campaign_template or ""),)).fetchall()


def db_panorama_conversation_npc(node_id, conversation_guid, conn=None):
    """Return the authored NPC/role for an AZ1 conversation node."""
    return _connection(conn).execute(
        "SELECT npc, role FROM quest_conversations "
        "WHERE campaign_template='AZ1' AND node_id=? "
        "AND conversation_guid=? AND enabled=1 "
        "ORDER BY priority, rowid LIMIT 1",
        (node_id, str(conversation_guid or ""))).fetchone()


def db_champion_template_guid_by_name(name, conn=None):
    """Return the extended champion GUID matching an NPC display name."""
    row = _connection(conn).execute(
        "SELECT guid FROM champion_templates_extended "
        "WHERE lower(name)=lower(?) LIMIT 1", (name,)).fetchone()
    return row[0] if row else None


def db_panorama_quest_rows(node, conn=None):
    """Return authored quest-linked AZ1 conversations for a node."""
    return _connection(conn).execute(
        "SELECT qc.quest_script, qc.conversation_guid, qc.role, qc.faction, "
        "qc.npc, qc.priority, COALESCE(NULLIF(qc.start_hook, ''), "
        "NULLIF(qt.start_hook, ''), '') "
        "FROM quest_conversations qc "
        "LEFT JOIN quest_templates qt ON qt.script_name=qc.quest_script "
        "WHERE qc.campaign_template='AZ1' AND qc.node_id=? "
        "AND qc.enabled=1 ORDER BY qc.priority, qc.rowid", (str(node),)
    ).fetchall()


def db_panorama_generic_rows(node, conn=None):
    """Return generic authored AZ1 node conversations."""
    return _connection(conn).execute(
        "SELECT conversation_guid, trigger_json, conversation_name, priority "
        "FROM campaign_node_conversations "
        "WHERE campaign_template='AZ1' AND node_id=? AND enabled=1 "
        "ORDER BY priority, conversation_guid", (str(node),)
    ).fetchall()


def db_node_conversation_rows(node, campaign_template="AZ1", conn=None):
    """Return enabled authored conversations for an exact campaign node."""
    return _connection(conn).execute(
        "SELECT conversation_guid, trigger_json, priority, conversation_name "
        "FROM campaign_node_conversations WHERE campaign_template=? "
        "AND node_id=? AND enabled=1 ORDER BY priority, conversation_guid",
        (str(campaign_template or "AZ1"), str(node))).fetchall()


def db_panorama_npc_quest_scripts(node, npc, conn=None):
    """Return quest scripts associated with an AZ1 node/NPC pair."""
    return _connection(conn).execute(
        "SELECT DISTINCT quest_script FROM quest_conversations "
        "WHERE campaign_template='AZ1' AND node_id=? AND npc=? "
        "AND enabled=1", (str(node), str(npc))).fetchall()


def db_encounter_scene_rewards(scene_guid, conn=None):
    """Return the authored serialized rewards for one encounter scene."""
    row = _connection(conn).execute(
        "SELECT rewards_json FROM encounter_scenes WHERE guid=?",
        (str(scene_guid),)).fetchone()
    return row[0] if row else None


def db_void_tamed_troop_rows(session_id, player_user_id, owner, conn=None):
    """Return voided troop templates/markers for reward conditions."""
    owner_sql = "gc.user_id=?" if str(owner).lower() in {
        "player", "self", "champion"} else "gc.user_id<>?"
    return _connection(conn).execute(
        "SELECT gc.template_guid, gc.permanent_buffs FROM game_cards gc "
        "WHERE gc.session_id=? AND LOWER(COALESCE(gc.location,''))='void' "
        "AND LOWER(COALESCE(gc.card_type,'')) LIKE '%troop%' AND " + owner_sql +
        " ORDER BY gc.card_uid", (int(session_id), int(player_user_id))
    ).fetchall()


def db_campaign_identity_state(campaign_id, conn=None):
    """Return the champion and serialized state for one campaign."""
    return _connection(conn).execute(
        "SELECT champion_id, state_json FROM campaigns WHERE id=?",
        (campaign_id,)).fetchone()


def db_campaign_runtime_row(campaign_id, conn=None):
    """Return serialized state and type for one campaign runtime."""
    return _connection(conn).execute(
        "SELECT state_json, campaign_type FROM campaigns WHERE id=?",
        (campaign_id,)).fetchone()


def db_campaign_protocol_row(campaign_id, conn=None):
    """Return campaign fields consumed by protocol handlers."""
    return _connection(conn).execute(
        "SELECT champion_id, state_json, campaign_type, template_name "
        "FROM campaigns WHERE id=?", (campaign_id,)).fetchone()


def db_campaign_query_row(campaign_id, conn=None):
    """Return campaign fields used by QueryCampState."""
    return _connection(conn).execute(
        "SELECT champion_id, is_started, state_json, campaign_type, "
        "template_name FROM campaigns WHERE id=?", (campaign_id,)).fetchone()


def db_campaign_summary_row(campaign_id, conn=None):
    """Return the profile-facing campaign summary fields."""
    return _connection(conn).execute(
        "SELECT c.camp_uid_lo, c.campaign_type, c.template_name, ch.race, "
        "c.state_json FROM campaigns c JOIN champions ch "
        "ON c.champion_id=ch.id WHERE c.id=?", (campaign_id,)).fetchone()


def db_campaign_battle_profile(campaign_id, conn=None):
    """Return champion/deck/talent fields needed for battle setup."""
    return _connection(conn).execute(
        "SELECT c.champion_name, ch.last_deck_id, ch.race, "
        "ch.champion_class, ch.gender, c.state_json, ch.talents "
        "FROM campaigns c JOIN champions ch ON ch.id=c.champion_id "
        "WHERE c.id=?", (campaign_id,)).fetchone()


def db_first_deck_id_for_user(user_id, conn=None):
    """Return the oldest saved deck for a profile as a fallback."""
    row = _connection(conn).execute(
        "SELECT id FROM decks WHERE user_id=? ORDER BY id LIMIT 1",
        (user_id,)).fetchone()
    return row[0] if row else None


def db_player_champion_template(race, champion_class, gender, conn=None):
    """Return the authored player champion template for an identity."""
    row = _connection(conn).execute(
        "SELECT guid FROM champion_templates WHERE race=? "
        "AND champion_class=? AND gender=? AND is_player=1 LIMIT 1",
        (race, champion_class, gender)).fetchone()
    return row[0] if row else None


def db_campaign_talents(campaign_id, conn=None):
    """Return the serialized talents for a campaign's champion."""
    row = _connection(conn).execute(
        "SELECT ch.talents FROM campaigns c JOIN champions ch "
        "ON ch.id=c.champion_id WHERE c.id=?", (campaign_id,)).fetchone()
    return row[0] if row else None


def db_talent_descriptions(talent_guids, conn=None):
    """Return descriptions for a set of authored talent GUIDs."""
    if not talent_guids:
        return []
    connection = _connection(conn)
    placeholders = ",".join("?" for _ in talent_guids)
    return connection.execute(
        "SELECT description FROM talent_data WHERE talent_guid IN (" +
        placeholders + ")", tuple(talent_guids)).fetchall()


def db_starting_hand_size(race, champion_class, conn=None):
    """Return the authored starting hand size for a class identity."""
    row = _connection(conn).execute(
        "SELECT starting_hand_size FROM champion_class_data "
        "WHERE race=? AND champion_class=?", (race, champion_class)
    ).fetchone()
    return row[0] if row else None


def db_campaign_champion_race(campaign_id, conn=None):
    """Return the race of the champion owning a campaign."""
    row = _connection(conn).execute(
        "SELECT ch.race FROM campaigns c JOIN champions ch "
        "ON c.champion_id=ch.id WHERE c.id=?", (campaign_id,)).fetchone()
    return row[0] if row else None


def db_champion_last_deck_id(champion_id, conn=None):
    """Return the saved deck linked to a champion."""
    row = _connection(conn).execute(
        "SELECT last_deck_id FROM champions WHERE id=?", (champion_id,)
    ).fetchone()
    return row[0] if row else None


def db_champion_last_campaign_id(champion_id, conn=None):
    """Return the most recently selected campaign for a champion."""
    row = _connection(conn).execute(
        "SELECT last_campaign_id FROM champions WHERE id=?", (champion_id,)
    ).fetchone()
    return row[0] if row else None


def db_quest_start_rows(conversation_guid, campaign_template, conn=None):
    """Return eligible authored quest starts in their source order."""
    return _connection(conn).execute(
        "SELECT qc.quest_script, COALESCE(NULLIF(qt.start_hook, ''), "
        "NULLIF(qc.start_hook, ''), ''), qc.faction, "
        "COALESCE(qt.campaign_group, 'AREA') FROM quest_conversations qc "
        "LEFT JOIN quest_templates qt ON qt.script_name=qc.quest_script "
        "WHERE qc.conversation_guid=? AND qc.campaign_template=? "
        "AND qc.role='start' AND qc.enabled=1 "
        "ORDER BY qc.priority, qc.quest_script",
        (str(conversation_guid), str(campaign_template))).fetchall()


def db_quest_node_rows(node, conn=None):
    """Return enabled AZ1 quest conversations for one node."""
    return _connection(conn).execute(
        "SELECT quest_script, conversation_guid, role, faction "
        "FROM quest_conversations WHERE campaign_template='AZ1' "
        "AND node_id=? AND enabled=1 ORDER BY priority, conversation_guid",
        (str(node),)).fetchall()


def db_conversation_reward(conversation_guid, conn=None):
    """Return the authored reward policy for one conversation."""
    return _connection(conn).execute(
        "SELECT reward_json, one_time, enabled FROM conversation_rewards "
        "WHERE conversation_guid=?", (str(conversation_guid),)).fetchone()


def db_fortune_card_guids(set_guid, conn=None):
    """Return the enabled AZ1 Fortune card templates in stable order."""
    return _connection(conn).execute(
        "SELECT guid FROM card_templates WHERE set_guid=? "
        "AND card_type='Choice' AND no_pvp=1 AND name LIKE 'Fortune of %' "
        "ORDER BY guid", (str(set_guid),)).fetchall()


def db_encounter_scene_runtime(scene_guid, conn=None):
    """Return scene fields used to construct a campaign encounter."""
    return _connection(conn).execute(
        "SELECT guid, name, title, gameboard, ai_deck_guid, "
        "ai_champion_guid, ai_deck_personality FROM encounter_scenes "
        "WHERE guid=?", (str(scene_guid),)).fetchone()


def db_campaign_template_name(campaign_id, conn=None):
    """Return a campaign's template/script name."""
    row = _connection(conn).execute(
        "SELECT template_name FROM campaigns WHERE id=?",
        (campaign_id,)).fetchone()
    return row[0] if row else None


def db_champion_template_data_exists(template_guid, conn=None):
    """Whether a champion template has player-facing static data."""
    return bool(_connection(conn).execute(
        "SELECT 1 FROM champion_template_data WHERE guid=?",
        (template_guid,)).fetchone())


def db_extended_champion_health(template_guid, conn=None):
    """Return extended champion starting health, if present."""
    row = _connection(conn).execute(
        "SELECT starting_health FROM champion_templates_extended "
        "WHERE guid=?", (template_guid,)).fetchone()
    return row[0] if row else None


def db_champion_template_name(template_guid, conn=None):
    """Return a champion template's display name."""
    row = _connection(conn).execute(
        "SELECT name FROM champion_template_data WHERE guid=?",
        (template_guid,)).fetchone()
    return row[0] if row else None


def db_champion_talents_for_deck(user_id, deck_id, conn=None):
    """Return the active non-deleted champion talents for a deck."""
    return _connection(conn).execute(
        "SELECT talents FROM champions WHERE user_id=? AND last_deck_id=? "
        "AND is_deleted=0 ORDER BY id LIMIT 1", (user_id, deck_id)
    ).fetchone()


def db_campaign_owner_user_id(campaign_id, conn=None):
    """Return the profile owning one persisted campaign."""
    row = _connection(conn).execute(
        "SELECT user_id FROM campaigns WHERE id=?", (campaign_id,)
    ).fetchone()
    return row[0] if row else None


def db_active_area_campaign(champion_id, template_name="AZ1", conn=None):
    """Return the latest active area campaign identity and state."""
    return _connection(conn).execute(
        "SELECT id, state_json FROM campaigns WHERE champion_id=? "
        "AND campaign_type='AREA' AND template_name=? ORDER BY id DESC LIMIT 1",
        (champion_id, template_name)).fetchone()


def db_active_quest_hooks(champion_id, conn=None):
    """Return started quest campaigns and their authored start hooks."""
    return _connection(conn).execute(
        "SELECT c.id, qt.start_hook FROM campaigns c "
        "JOIN quest_templates qt ON qt.script_name=c.template_name "
        "WHERE c.champion_id=? AND c.campaign_type='QUEST' "
        "AND c.state_json IS NOT NULL AND c.is_started=1 "
        "AND qt.enabled=1 AND qt.start_hook<>'' ORDER BY c.id", (champion_id,)
    ).fetchall()


def db_campaign_state(campaign_id, conn=None):
    """Return serialized state for one campaign."""
    row = _connection(conn).execute(
        "SELECT state_json FROM campaigns WHERE id=?", (campaign_id,)
    ).fetchone()
    return row[0] if row else None


def db_az1_node_conversation_ids(conn=None):
    """Return enabled authored AZ1 node/conversation pairs."""
    return _connection(conn).execute(
        "SELECT node_id, conversation_guid FROM campaign_node_conversations "
        "WHERE campaign_template='AZ1' AND enabled=1"
    ).fetchall()


def db_started_quest_states(champion_id, conn=None):
    """Return started quest campaign scripts and serialized states."""
    return _connection(conn).execute(
        "SELECT template_name, state_json FROM campaigns "
        "WHERE champion_id=? AND campaign_type='QUEST' "
        "AND is_started=1 AND state_json IS NOT NULL", (champion_id,)
    ).fetchall()


def db_encounter_deck_personality(deck_guid, conn=None):
    """Return an authored AI deck personality, if one exists."""
    row = _connection(conn).execute(
        "SELECT ai_deck_personality FROM encounter_scenes "
        "WHERE ai_deck_guid=? AND ai_deck_personality IS NOT NULL LIMIT 1",
        (deck_guid,)).fetchone()
    return row[0] if row else None


def db_default_talents(race, champion_class, gender, conn=None):
    """Return authored default player talents for a champion identity."""
    return _connection(conn).execute(
        "SELECT default_talents FROM champion_templates "
        "WHERE race=? AND champion_class=? AND gender=? AND is_player=1 "
        "LIMIT 1", (race, champion_class, gender)
    ).fetchone()


def db_talent_data_ability_guid(talent_guid, conn=None):
    """Return the primary ability authored on a talent, if any."""
    row = _connection(conn).execute(
        "SELECT ability_guid FROM talent_data "
        "WHERE talent_guid=? AND has_ability=1", (talent_guid,)
    ).fetchone()
    return row[0] if row else None


def db_talent_ability_rows(talent_guid, conn=None):
    """Return ability GUIDs authored for one talent."""
    return _connection(conn).execute(
        "SELECT ability_guid FROM talent_abilities WHERE talent_guid=?",
        (talent_guid,)).fetchall()


def db_talent_ability_costs(ability_guid, conn=None):
    """Return normalized talent costs and activation restrictions."""
    row = _connection(conn).execute(
        "SELECT charge_cost, spell_cost, activatable_phases, "
        "casting_behavior FROM talent_abilities "
        "WHERE ability_guid=? LIMIT 1", (str(ability_guid),)).fetchone()
    if not row:
        return None
    phases = int(row[2] or 0)
    casting = int(row[3] or 0)
    if not phases and casting == ECardTypes.BasicAction:
        phases = ((1 << ETurnPhases.FirstMainPhase)
                  | (1 << ETurnPhases.SecondMainPhase))
    return (row[0] or 0, row[1] or 0, phases, casting)

__all__ = [name for name in globals() if name.startswith("db_")]
