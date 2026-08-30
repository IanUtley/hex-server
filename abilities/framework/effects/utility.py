"""Small effect executors whose semantics are independent of card keywords."""

import json

import game_engine

from .registry import leaf_register
from ..fields import effect_field, effect_template, effect_template_value
from .._shared import next_game_card_uid, owner_uid


def _resolved_target(bstate):
    return ((bstate or {}).get("resolving_target_uid")
            or (bstate or {}).get("player_mod_target")
            or (bstate or {}).get("player_spell_target")
            or (bstate or {}).get("resolving_source_uid"))


def _push_card_in_zone(game, session, db, handler, pl_t, ai_t, bstate,
                       uid, location):
    row = db.execute(
        "SELECT template_guid, user_id FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (session.session_id, int(uid))
    ).fetchone()
    if not row:
        return
    from .._shared import card_collection_for_location
    scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
    _tpl, ct, _name, cost, atk, defense, _gem = handler._card_full_data(
        game, scid, row[0])
    owner = owner_uid(row[1], pl_t, ai_t, bstate)
    collection = card_collection_for_location(location)
    game.push_card_updated(scid, owner, collection, ct, template_id=row[0],
                           cost=cost, attack=atk, defense=defense,
                           nulling=(str(location).lower() == "deck"))
    game.push_card_moved(scid, owner, collection,
                         game_engine.ECardLocations.Top, 0)


def _create_matching_target(game, session, db, handler, pl_t, ai_t, bstate,
                            target, count, collection):
    """Create copies of a target template using the normal token projection."""
    row = db.execute(
        "SELECT template_guid, user_id FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (session.session_id, int(target))
    ).fetchone()
    if not row:
        return 0
    tpl_guid, owner_id = row
    tpl = db.execute(
        "SELECT card_type, abilities_json, attributes FROM card_templates "
        "WHERE guid=?", (tpl_guid,)).fetchone()
    if not tpl:
        return 0
    loc = {"hand": "hand", "deck": "deck", "underground": "underground",
           "void": "void", "warzone": "warzone"}.get(
               str(collection or "warzone").lower(), "warzone")
    created = []
    columns_info = {row[1] for row in db.execute(
        "PRAGMA table_info(game_cards)").fetchall()}
    for index in range(max(0, int(count))):
        next_id = db.execute(
            "SELECT COALESCE(MAX(id),10000)+1 FROM game_cards "
            "WHERE session_id=?", (session.session_id,)).fetchone()[0]
        uid = next_game_card_uid(db, session.session_id)
        columns = ["id", "session_id", "user_id", "card_uid", "template_guid",
                   "card_template_id", "location", "position", "card_state",
                   "card_abilities", "card_type", "card_attributes"]
        values = [next_id, session.session_id, owner_id, uid, tpl_guid, tpl_guid,
                  loc, 0, 0, tpl[1] or "[]", tpl[0], int(tpl[2] or 0)]
        for name, value in (("owner_user_id", owner_id),
                            ("original_template_guid", tpl_guid),
                            ("gems", 0)):
            if name in columns_info:
                columns.append(name); values.append(value)
        db.execute("INSERT INTO game_cards ({}) VALUES ({})".format(
            ",".join(columns), ",".join("?" for _ in columns)), values)
        created.append(int(uid))
    db.commit()
    for uid in created:
        _push_card_in_zone(game, session, db, handler, pl_t, ai_t, bstate,
                           uid, loc)
    return len(created)


def _owner_for_target(db, session, bstate, target, fallback):
    if target is not None:
        row = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target))).fetchone()
        if row:
            return int(row[0])
        for pid, cuid in ((bstate or {}).get("champ_map") or {}).items():
            try:
                if int(cuid) == int(target):
                    return int(pid)
            except (TypeError, ValueError):
                continue
    return int(fallback or 0)


@leaf_register("ReplenishResourcesAbilityEffectTemplate")
def replenish_resources(game, session, db, handler, pl_t, ai_t, bstate,
                         effect_guid, param):
    """Set the controller's current resources to their total pool."""
    owner = int((bstate or {}).get("resolving_owner_id", 0) or 0)
    if (bstate or {}).get("pvp"):
        current_key = f"res_{owner}"
        total_key = f"res_total_{owner}"
    else:
        side = "player" if owner else "ai"
        current_key = f"{side}_resources"
        total_key = f"{side}_total_resources"
    current = int(bstate.get(current_key, 0) or 0)
    total = int(bstate.get(total_key, current) or 0)
    delta = max(0, total - current)
    bstate[current_key] = total
    if owner:
        game.player_resources = total
    else:
        game.ai_resources = total
    if delta:
        ev = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
        ev.player_id = owner_uid(owner, pl_t, ai_t, bstate)
        ev.operation = 1
        ev.delta = delta
        ev.new_value = total
        game._push(ev)
    return f"replenish resources {current}->{total}"


@leaf_register("LoseThresholdAbilityEffectTemplate")
def lose_threshold(game, session, db, handler, pl_t, ai_t, bstate,
                   effect_guid, param):
    """Remove the typed shard thresholds from the target controller."""
    target = ((bstate or {}).get("resolving_target_uid")
              or (bstate or {}).get("player_mod_target")
              or (bstate or {}).get("resolving_source_uid"))
    owner = _owner_for_target(
        db, session, bstate, target,
        (bstate or {}).get("resolving_owner_id", 0))
    template = effect_template(effect_guid) or {}
    names = template.get("m_Thresholds") or []
    if not names:
        try:
            names = json.loads(param or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            names = []
    colors = {"Colorless": 1, "Blood": 4, "Ruby": 8,
              "Sapphire": 16, "Wild": 32, "Diamond": 64}
    changed = 0
    for name in names:
        color = colors.get(str(name).split(".")[-1], 0)
        if not color:
            continue
        key = f"thresh_{owner}" if (bstate or {}).get("pvp") else (
            "player_threshold" if owner else "ai_threshold")
        thresholds = bstate.setdefault(key, {})
        old = int(thresholds.get(color, thresholds.get(str(color), 0)) or 0)
        if old <= 0:
            continue
        thresholds[color] = 0
        thresholds.pop(str(color), None)
        changed += old
        ev = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
        ev.player_id = owner_uid(owner, pl_t, ai_t, bstate)
        ev.color = color
        ev.operation = 2
        ev.delta = old
        ev.new_value = 0
        game._push(ev)
    return f"lost {changed} threshold(s)"


@leaf_register("RemoveCardFromCombatAbilityEffectTemplate")
def remove_card_from_combat(game, session, db, handler, pl_t, ai_t, bstate,
                             effect_guid, param):
    """Remove a troop from combat while retaining ordinary card state."""
    target = ((bstate or {}).get("resolving_target_uid")
              or (bstate or {}).get("player_mod_target")
              or (bstate or {}).get("resolving_source_uid"))
    if target is None:
        return "remove from combat: no target"
    clear = (game_engine.ECardStates.Attacking |
             game_engine.ECardStates.Blocking |
             game_engine.ECardStates.HasAttacked |
             game_engine.ECardStates.HasBlocked)
    row = db.execute(
        "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    if not row:
        return "remove from combat: card not found"
    state = int(row[0] or 0) & ~int(clear)
    db.execute(
        "UPDATE game_cards SET card_state=? WHERE session_id=? AND card_uid=?",
        (state, session.session_id, int(target)))
    db.commit()
    from ..bom import _push_card_state
    _push_card_state(game, session, db, handler, pl_t, ai_t, int(target), state)
    return f"removed {hex(int(target))} from combat"


@leaf_register("DiscardOrSacrificeCardAbilityEffectTemplate")
def discard_or_sacrifice(game, session, db, handler, pl_t, ai_t, bstate,
                         effect_guid, param):
    target = _resolved_target(bstate)
    if target is None:
        return "discard or sacrifice: no target"
    row = db.execute(
        "SELECT location, user_id FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (session.session_id, int(target))
    ).fetchone()
    if not row:
        return "discard or sacrifice: target not found"
    if row[0] == "warzone":
        from ..kill_troop import kill_troop
        kill_troop(game, session, db, handler, pl_t, ai_t, int(target), bstate,
                   cause="sacrifice")
        return f"sacrificed {hex(int(target))}"
    if row[0] != "hand":
        return f"discard or sacrifice: ignored {row[0]}"
    from db import db_discard_card
    db_discard_card(session.session_id, int(target), connection=db)
    scid = game_engine.SessionCardId(game_engine.UID(int(target)))
    owner = owner_uid(row[1], pl_t, ai_t, bstate)
    game.push_card_discarded(scid, owner)
    _push_card_in_zone(game, session, db, handler, pl_t, ai_t, bstate,
                       int(target), "discard")
    return f"discarded {hex(int(target))}"


@leaf_register("SwapHealthAbilityEffectTemplate")
def swap_health(game, session, db, handler, pl_t, ai_t, bstate, effect_guid,
                param):
    owner = int((bstate or {}).get("resolving_owner_id", 0) or 0)
    target = _resolved_target(bstate)
    if (bstate or {}).get("pvp"):
        from .._shared import pvp_champion_uid, pvp_opponent_pid
        target_pid = pvp_opponent_pid(bstate, owner)
        if target is not None:
            for pid, cuid in (bstate.get("champ_map") or {}).items():
                if int(cuid) == int(target):
                    target_pid = int(pid)
                    break
        if target_pid is None:
            return "swap health: no opposing champion"
        key_a = (bstate.get("pvp_health_map") or {}).get(owner,
                                                           f"hp_{owner}")
        key_b = (bstate.get("pvp_health_map") or {}).get(target_pid,
                                                           f"hp_{target_pid}")
        a = int(bstate.get(key_a, 20)); b = int(bstate.get(key_b, 20))
        bstate[key_a], bstate[key_b] = b, a
        game.player_health = bstate[key_a]
        game.ai_health = bstate[key_b]
        for pid, old, new in ((owner, a, b), (target_pid, b, a)):
            ev = game_engine.ChampionHealthChangedSessionEventArgs()
            ev.player_id = owner_uid(pid, pl_t, ai_t, bstate)
            ev.old_damage_value = old; ev.new_damage_value = new
            game._push(ev)
        return f"swapped health {owner}<->{target_pid}"
    other = 0 if owner else (handler.user_profile["id"]
                             if handler.user_profile else 0)
    akey, bkey = ("player_health", "ai_health") if owner else \
                 ("ai_health", "player_health")
    a = int(bstate.get(akey, getattr(game, akey, 20)))
    b = int(bstate.get(bkey, getattr(game, bkey, 20)))
    bstate[akey], bstate[bkey] = b, a
    setattr(game, akey, b); setattr(game, bkey, a)
    for oid, old, new in ((owner, a, b), (other, b, a)):
        ev = game_engine.ChampionHealthChangedSessionEventArgs()
        ev.player_id = owner_uid(oid, pl_t, ai_t, bstate)
        ev.old_damage_value = old; ev.new_damage_value = new
        game._push(ev)
    return "swapped champion health"


@leaf_register("TransformCardIntoReplicaAbilityEffectTemplate")
def transform_into_replica(game, session, db, handler, pl_t, ai_t, bstate,
                            effect_guid, param):
    target = _resolved_target(bstate)
    if target is None:
        return "transform replica: no target"
    from ..transform import transform_card
    transform_card(handler, game, session, pl_t, ai_t, int(target),
                   db.execute("SELECT template_guid FROM game_cards "
                              "WHERE session_id=? AND card_uid=?",
                              (session.session_id, int(target))).fetchone()[0],
                   keep_zone=True, bstate=bstate)
    return f"replicated {hex(int(target))}"


@leaf_register("CreateTokenMatchingTargetAbilityEffectTemplate")
def create_token_matching_target(game, session, db, handler, pl_t, ai_t,
                                 bstate, effect_guid, param):
    target = _resolved_target(bstate)
    if target is None:
        return "matching token: no target"
    count = effect_field(db, bstate, effect_guid, "m_InputValue", default=1)
    template = effect_template(effect_guid) or {}
    collection = effect_template_value(db, bstate, effect_guid,
                                       "m_CardCollection") or "Warzone"
    made = _create_matching_target(game, session, db, handler, pl_t, ai_t,
                                   int(target), max(1, count), collection)
    return f"created {made} matching token(s)"


@leaf_register("TunnelAbilityEffectTemplate")
def tunnel_card(game, session, db, handler, pl_t, ai_t, bstate, effect_guid,
                param):
    target = _resolved_target(bstate)
    if target is None:
        return "tunnel: no target"
    db.execute("UPDATE game_cards SET location='underground', position=9999 "
               "WHERE session_id=? AND card_uid=?",
               (session.session_id, int(target)))
    db.commit()
    _push_card_in_zone(game, session, db, handler, pl_t, ai_t, bstate,
                       int(target), "underground")
    return f"tunneled {hex(int(target))}"
