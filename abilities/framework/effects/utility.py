"""Small effect executors whose semantics are independent of card keywords."""

import json

import game_engine

from .registry import effect
from .._shared import next_game_card_uid, owner_uid


@effect("NoOpEffectTemplate")
def no_op(effect):
    """Preserve client target-resolution semantics without mutation."""
    return "no-op"


@effect("DrawCardAbilityEffectTemplate")
def draw_one_card(effect):
    """The singular client draw template is exactly one typed draw."""
    return effect.draw(1, owner=effect.target_owner(default=None))


@effect("ReturnToHandAbilityEffectTemplate")
def return_to_hand(effect):
    return effect.return_to_hand()


@effect("ClearStoredAbilityEffectTemplate")
def clear_stored(effect):
    return effect.clear_stored()


@effect("SetResponsiblePlayerAbilityEffectTemplate")
def set_responsible_player(effect):
    return effect.set_responsible_player()


@effect("CopyAbilityVariableEffectTemplate")
def copy_ability_variable(effect):
    return effect.copy_ability_variable()


@effect("SetCardCountVariableEffectTemplate")
def set_card_count_variable(effect):
    return effect.set_card_count_variable()


@effect("SetCardIntegerVariableEffectTemplate")
def set_card_integer_variable(effect):
    return effect.set_card_integer_variable()


@effect("SetConstantValueVariableEffectTemplate")
def set_constant_value_variable(effect):
    return effect.set_constant_value_variable()


@effect("TransformCardToTargetAbilityEffectTemplate")
def transform_card_to_target(effect):
    return effect.transform_card_to_target()


@effect("FinishMovingCardToWarzoneEffectTemplate")
def finish_moving_to_warzone(effect):
    return effect.finish_moving_to_warzone()


@effect("FinishResolvingCardAbilityEffectTemplate")
def finish_resolving_card(effect):
    return effect.finish_resolving_card()


@effect("ActivatePowerAbilityEffectTemplate")
def activate_power(effect):
    # Ordinary power activations share the same child-ability scheduler. The
    # champion charge-power selector remains a metadata/session concern.
    return effect.activate_ability()


@effect("InterruptSpellAbilityEffectTemplate")
def interrupt_spell(effect):
    return effect.counter_spell()


@effect("BuiltInPlayCardAbilityEffectTemplate")
def built_in_play_card(effect):
    return effect.play_card()


@effect("SummonXTokenTroopsAbilityEffectTemplate")
def summon_x_tokens(effect):
    return effect.summon_x_tokens()


@effect("TargetPlayerTakesControlEffectTemplate")
def target_player_takes_control(effect):
    return effect.target_player_takes_control()


@effect("StealCardAbilityEffectTemplate")
def steal_card(effect):
    return effect.steal_card()


@effect("StealEffectsAbilityEffectTemplate")
def steal_effects(effect):
    return effect.steal_effects()


@effect("LoseGameAbilityEffectTemplate")
def lose_game(effect):
    return effect.lose_game()


@effect("RevertTransformedCardAbilityEffectTemplate")
def revert_transformed_card(effect):
    return effect.revert_transformed_card()


@effect("PlayerAttributeAbilityEffectTemplate")
def player_attribute(effect):
    return effect.player_attribute()


@effect("ExchangeCardsAbilityEffectTemplate")
def exchange_cards(effect):
    return effect.exchange_cards()


@effect("MergeCardCollectionsAbilityEffectTemplate")
def merge_card_collections(effect):
    return effect.merge_card_collections()


@effect("ZombiePlagueAbilityEffectTemplate")
def zombie_plague(effect):
    return effect.zombie_plague()


@effect("XarloxAbilityEffectTemplate")
def xarlox(effect):
    return effect.xarlox()


@effect("PlanCAbilityEffectTemplate")
def plan_c(effect):
    return effect.plan_c()


@effect("ShuffleCardCollectionAbilityEffectTemplate")
def shuffle_collection(effect):
    return effect.shuffle_collection()


@effect("ExtraCombatsThisTurnAbilityEffectTemplate")
def extra_combats_obsolete(effect):
    """The client template is obsolete and intentionally has no mutation."""
    return "extra combats: obsolete"


def _resolved_target(bstate):
    return ((bstate or {}).get("resolving_target_uid")
            or (bstate or {}).get("player_mod_target")
            or (bstate or {}).get("player_spell_target")
            or (bstate or {}).get("resolving_source_uid"))


def _push_card_in_zone(game, session, db, handler, pl_t, ai_t, bstate,
                       uid, location):
    from pvp_db import db_card_source_info
    source = db_card_source_info(session.session_id, int(uid), conn=db)
    row = (source[0], source[3]) if source else None
    if not row:
        return
    from .._shared import card_collection_for_location
    scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
    _tpl, ct, _name, cost, atk, defense, _gem = handler._card_full_data(
        game, scid, row[0])
    owner = owner_uid(row[1], pl_t, ai_t, bstate)
    collection = card_collection_for_location(location)
    game.push_card_moved(scid, owner, collection,
                         game_engine.ECardLocations.Top, 0)
    game.push_card_updated(scid, owner, collection, ct, template_id=row[0],
                           cost=cost, attack=atk, defense=defense,
                           nulling=(str(location).lower() == "deck"))
    from .visibility import refresh_player_visibility
    refresh_player_visibility(
        db, session, handler, game, pl_t, ai_t, bstate)


def _create_matching_target(game, session, db, handler, pl_t, ai_t, bstate,
                            target, count, collection, deck_location=""):
    """Create copies of a target template using the normal token projection."""
    from pvp_db import (db_card_source_info, db_copy_template_payload,
                        db_next_game_card_row_id, db_insert_generated_card)
    source = db_card_source_info(session.session_id, int(target), conn=db)
    row = (source[0], source[3]) if source else None
    if not row:
        return 0
    tpl_guid, owner_id = row
    tpl = db_copy_template_payload(tpl_guid, conn=db)
    if not tpl:
        return 0
    loc = {"hand": "hand", "deck": "deck", "underground": "underground",
           "void": "void", "warzone": "warzone"}.get(
               str(collection or "warzone").lower(), "warzone")
    created = []
    for index in range(max(0, int(count))):
        next_id = db_next_game_card_row_id(session.session_id, conn=db)
        uid = next_game_card_uid(db, session.session_id)
        db_insert_generated_card(
            session.session_id, owner_id, uid, tpl_guid, loc, tpl[0], tpl[1],
            tpl[2], next_id, conn=db, owner_user_id=owner_id,
            original_template_guid=tpl_guid, gems=0)
        created.append(int(uid))
    # Deck copies follow the client's Unknown-location rule: a random slot,
    # with the untouched cards keeping their existing relative order.
    if (loc == "deck" and created and
            str(deck_location or "").lower() in ("", "unknown", "random")):
        from pvp_db import db_randomly_insert_deck_cards
        db_randomly_insert_deck_cards(
            session.session_id, owner_id, created, connection=db)
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
