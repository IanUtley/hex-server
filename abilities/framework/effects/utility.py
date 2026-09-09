"""Small effect executors whose semantics are independent of card keywords."""

import json

import game_engine

from .registry import effect
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


@effect("ReplenishResourcesAbilityEffectTemplate")
def replenish_resources(effect):
    """Set the controller's current resources to their total pool."""
    return effect.replenish_resources()


@effect("AnimationTriggerEffectTemplate")
def animation_trigger(effect):
    """Dispatch the typed presentation-only animation trigger."""
    trigger = effect.template_value("m_AnimationTrigger", "Invalid")
    values = {"Invalid": 0, "CannonTalent": 1, "MageTalent": 2,
              "WarriorTalent": 3, "ClericTalent": 4, "RangerTalent": 5,
              "Kraken": 8}
    value = values.get(str(trigger).rsplit(".", 1)[-1], 0)
    if value:
        effect.game.push_animation_trigger(value)
    return f"animation trigger {trigger}"


@effect("LoseThresholdAbilityEffectTemplate")
def lose_threshold(effect):
    """Remove the typed shard thresholds from the target controller."""
    names = effect.template_value("m_Thresholds", []) or []
    if not names and effect.param:
        try:
            names = json.loads(effect.param)
        except (TypeError, ValueError, json.JSONDecodeError):
            names = []
    return effect.lose_thresholds(names)


@effect("RemoveCardFromCombatAbilityEffectTemplate")
def remove_card_from_combat(effect):
    """Remove a troop from combat while retaining ordinary card state."""
    return effect.remove_from_combat()


@effect("DiscardOrSacrificeCardAbilityEffectTemplate")
def discard_or_sacrifice(effect):
    """Discard or sacrifice through the shared context operation."""
    return effect.discard_or_sacrifice()


@effect("SwapHealthAbilityEffectTemplate")
def swap_health(effect):
    """Exchange champion health through the shared context operation."""
    return effect.swap_health()


@effect("TransformCardIntoReplicaAbilityEffectTemplate")
def transform_into_replica(effect):
    """Replicate through the context operation boundary."""
    return effect.transform_replica()


@effect("CreateTokenMatchingTargetAbilityEffectTemplate")
def create_token_matching_target(effect):
    """Create target copies through typed context values."""
    return effect.create_matching_token()


@effect("TunnelAbilityEffectTemplate")
def tunnel_card(effect):
    """Move the target underground through the context boundary."""
    return effect.tunnel()
