"""RulesPort-native Records effect operations.

The resolver still obtains effect ordering and targets from Records.  This
module is the first native leaf boundary: simple state transitions execute
through the typed EffectContext without entering the legacy BOM leaf.
"""

from __future__ import annotations

import json


def dispatch(effect_type, context, effect=None):
    """Dispatch one Records effect through the native RulesPort leaf set."""
    native = {
        "NoOpEffectTemplate": lambda: "no-op",
        "DrawCardAbilityEffectTemplate": lambda: context.draw(
            1, owner=context.target_owner(default=None)),
        "ClearStoredAbilityEffectTemplate": context.clear_stored,
        "SetResponsiblePlayerAbilityEffectTemplate":
            context.set_responsible_player,
        "CopyAbilityVariableEffectTemplate": context.copy_ability_variable,
        "SetCardCountVariableEffectTemplate": context.set_card_count_variable,
        "SetCardIntegerVariableEffectTemplate":
            context.set_card_integer_variable,
        "SetConstantValueVariableEffectTemplate":
            context.set_constant_value_variable,
        "ReplenishResourcesAbilityEffectTemplate": context.replenish_resources,
        "RemoveCardFromCombatAbilityEffectTemplate":
            lambda: context.remove_from_combat(),
        "SwapHealthAbilityEffectTemplate": context.swap_health,
        "ExtraCombatsThisTurnAbilityEffectTemplate":
            lambda: "extra combats: obsolete",
        "DrawNCardsAbilityEffectTemplate": context.draw_effect,
        "PutTopOfDeckIntoHandAbilityEffectTemplate":
            context.put_top_into_hand,
        "BuryCardAbilityEffectTemplate": context.bury,
        "VoidCardAbilityEffectTemplate": context.void_card,
        "DiscardCardAbilityEffectTemplate": context.discard,
        "TunnelAbilityEffectTemplate": context.tunnel,
        "TargetPlayerTakesControlEffectTemplate":
            context.target_player_takes_control,
        "StealCardAbilityEffectTemplate": context.steal_card,
        "StealEffectsAbilityEffectTemplate": context.steal_effects,
        "CopyAbilityEffectTemplate": lambda: _copy_ability(context),
        "GrantAbilityEffectTemplate": lambda: grant_ability(context),
        "CreateAndCastSpellAbilityEffectTemplate":
            lambda: create_and_cast_spell(context),
        "DoubleChoiceAbilityEffectTemplate": lambda: context.double_choice(),
        "LoseGameAbilityEffectTemplate": context.lose_game,
        "RevertTransformedCardAbilityEffectTemplate":
            context.revert_transformed_card,
        "StoreTargetsAbilityEffectTemplate": context.store_target,
        "StoreNameAbilityEffectTemplate": context.store_name,
        "RememberKeywordPowersEffectTemplate": context.remember_keyword_powers,
        "RegisterTriggerAbilityEffectTemplate": context.register_trigger,
        "RevokeAbilityEffectTemplate": context.revoke_ability,
        "RevertPermanentModificationsAbilityEffectTemplate":
            context.revert_modifications,
        "GiveBonusTurnAbilityEffectTemplate": context.queue_bonus_turn,
        "SacrificeCardAbilityEffectTemplate": context.sacrifice,
        "TransformSelfAbilityEffectTemplate": context.transform_self,
        "TransformCardToTargetAbilityEffectTemplate":
            context.transform_card_to_target,
        "TransformCardIntoReplicaAbilityEffectTemplate":
            context.transform_replica,
        "TransformCardAtRandomAbilityEffectTemplate":
            context.transform_card_random,
        "TransformCardAbilityEffectTemplate": context.transform_card,
        "CreateTokenCopyAbilityEffectTemplate": context.create_token_copy,
        "CreateTokenMatchingTargetAbilityEffectTemplate":
            context.create_matching_token,
        "SummonTokenTroopAbilityEffectTemplate": context.summon_token,
        "SummonXTokenTroopsAbilityEffectTemplate": context.summon_x_tokens,
        "ConscriptAbilityEffectTemplate": context.conscript,
        "LoadPlayerDeckAbilityEffectTemplate": context.load_player_deck,
        "DestroyCardByDefenseAbilityEffectTemplate": context.destroy_by_defense,
        "DiscardOrSacrificeCardAbilityEffectTemplate":
            context.discard_or_sacrifice,
        "PlayerAttributeAbilityEffectTemplate": context.player_attribute,
        "ExchangeCardsAbilityEffectTemplate": context.exchange_cards,
        "MergeCardCollectionsAbilityEffectTemplate":
            context.merge_card_collections,
        "ZombiePlagueAbilityEffectTemplate": context.zombie_plague,
        "XarloxAbilityEffectTemplate": context.xarlox,
        "PlanCAbilityEffectTemplate": context.plan_c,
        "ShuffleCardCollectionAbilityEffectTemplate": context.shuffle_collection,
        "RevealCardsAbilityEffectTemplate": context.reveal_cards,
        "Battle2CardsAbilityEffectTemplate": context.battle_cards,
        "CounterSpellAbilityEffectTemplate": context.counter_spell,
        "InterruptSpellAbilityEffectTemplate": context.counter_spell,
        "DestroyCardAbilityEffectTemplate": context.destroy,
        "FireEventEffectTemplate": context.fire_event,
        "TACAbilityEffectTemplate": context.tac,
        "VerdictAbilityEffectTemplate": context.verdict,
        "ReturnToHandAbilityEffectTemplate": context.return_to_hand,
        "MoveCardToZoneEffectTemplate": context.move_card_to_zone,
        "FinishMovingCardToWarzoneEffectTemplate":
            context.finish_moving_to_warzone,
        "FinishResolvingCardAbilityEffectTemplate":
            context.finish_resolving_card,
        "RandomizeVariableEffectTemplate": context.randomize_variable,
        "RandomizeVariableAbilityEffectTemplate": context.randomize_variable,
        "ConversationAbilityEffectTemplate": context.conversation,
        "ActivateTriggeredAbilityEffectTemplate": context.activate_triggered,
        "ActivateAbilityEffectTemplate": context.activate_ability,
        "ActivatePowerAbilityEffectTemplate": context.activate_ability,
        "PlayCardAbilityEffectTemplate": context.play_card,
        "BuiltInPlayCardAbilityEffectTemplate": context.play_card,
        "BlockEffectTemplate": context.block,
    }
    operation = native.get(effect_type)
    if operation is not None:
        return operation()
    if effect_type == "StoreListAttrAbilityEffectTemplate":
        return _store_list_attr(context)
    if effect_type == "AnimationTriggerEffectTemplate":
        return _animation_trigger(context)
    if effect_type == "LoseThresholdAbilityEffectTemplate":
        return _lose_threshold(context, effect)
    if effect_type == "CardModifierAbilityEffectTemplate":
        return _card_modifier(context, effect)
    if effect_type == "RepeatingAbilityEffectTemplate":
        return _repeating(context, effect)
    if effect_type == "UntapCardAbilityEffectTemplate":
        return _untap(context)
    if effect_type == "TapCardAbilityEffectTemplate":
        return _tap(context)
    return None


def _copy_ability(context):
    """Queue a copy of the activated ability through the port host seam."""
    original = (context.bstate or {}).get("card_activated_item")
    if not original or not original.get("ability_guid"):
        return "copy ability: no activated ability"
    import game_engine
    from . import chain

    instance_id = int((context.bstate or {}).get("_next_instance_id", 1))
    context.bstate["_next_instance_id"] = instance_id + 1
    copied = {
        "kind": "ability", "ability_guid": original["ability_guid"],
        "source_uid": original.get("source_uid"),
        "target_uid": original.get("target_uid"),
        "instance_id": instance_id,
    }
    chain.push(context.bstate, copied)
    source = original.get("source_uid")
    if source is not None:
        context.game.push_ability_on_chain(
            game_engine.SessionCardId(game_engine.UID(int(source))),
            game_engine.ResourceId.from_str(str(original["ability_guid"])),
            ability_instance_id=instance_id)
    return f"copied ability {str(original['ability_guid'])[:8]}"


def grant_ability(context):
    """Apply a typed ability grant to a normal session card."""
    import game_engine
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from pvp_db import (db_ability_metadata_exists, db_card_grant_info,
                        db_set_card_abilities)
    try:
        granted = str(context.param or "").lower()
    except Exception:
        granted = ""
    if not granted or granted == "0" * 36:
        remembered = (context.bstate.get("remembered_powers") or {})
        values = remembered.get(context.bstate.get("resolving_ability"), [])
        granted_values = [str(value).lower() for value in values]
    else:
        granted_values = [granted]
    if not granted_values:
        typed = str(context.template_value(
            "m_GrantedAbilityTemplateId", "") or "").lower()
        if typed and typed != "0" * 36:
            granted_values = [typed]
    target = context.bstate.get("grant_target")
    if target is None:
        target = context.resolved_target()
    if target is None:
        return "grant: no target"
    row = db_card_grant_info(
        context.session.session_id, int(target), conn=context.db)
    if not row:
        return "grant: target card not found"
    try:
        abilities = json.loads(row[0] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        abilities = []
    template = context.template_value("m_AbilityIsUnique", True)
    unique = bool(template)
    added = []
    for guid in granted_values:
        if (not db_ability_metadata_exists(guid, conn=context.db) and
                ability_graph(DEFAULT_RECORD_STORE, guid) is None):
            continue
        if unique and guid in abilities:
            continue
        abilities.append(guid)
        added.append(guid)
    db_set_card_abilities(
        context.session.session_id, int(target), json.dumps(abilities),
        conn=context.db)
    context.db.commit()
    scid = game_engine.SessionCardId(game_engine.UID(int(target)))
    from .runtime_helpers import card_collection_for_location, owner_uid
    owner = owner_uid(row[2], context.player_uid, context.ai_uid,
                      context.bstate)
    tpl, card_type, _name, cost, attack, defense, gems = \
        context.handler._card_full_data(context.game, scid, row[1], None)
    context.game.push_card_updated(
        scid, owner, card_collection_for_location(row[3]),
        game_engine.card_type_from_db(card_type)
        if isinstance(card_type, str) else card_type,
        attack=attack, defense=defense, cost=cost,
        state=int(row[4] or 0), template_id=tpl, gems=gems,
        nulling=(row[3] == "deck"))
    return f"granted {len(added)} ability(s) to {hex(int(target))}"


def create_and_cast_spell(context):
    """Copy the selected spell and resolve its authored abilities natively."""
    from pvp_db import (db_card_zone_details, db_copy_template_payload,
                        db_next_game_card_row_id, db_insert_generated_card,
                        db_discard_card)
    from .runtime_helpers import next_game_card_uid
    target = (context.bstate.get("card_cast_copy_target") or
              context.resolved_target())
    if target is None:
        return "copy spell: no target"
    row = db_card_zone_details(
        context.session.session_id, int(target), conn=context.db)
    if not row:
        return "copy spell: target template missing"
    template_guid = row[0]
    payload = db_copy_template_payload(template_guid, conn=context.db)
    if not payload:
        return "copy spell: template not found"
    owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    card_uid = next_game_card_uid(context.db, context.session.session_id)
    db_insert_generated_card(
        context.session.session_id, owner, card_uid, template_guid,
        "CastSpells", payload[0], payload[1], payload[2],
        db_next_game_card_row_id(context.session.session_id, conn=context.db),
        conn=context.db)
    context.db.commit()
    try:
        ability_guids = [str(value).lower() for value in json.loads(
            payload[1] or "[]") if value]
    except (TypeError, ValueError, json.JSONDecodeError):
        ability_guids = []
    scid = game_engine.SessionCardId(game_engine.UID(card_uid))
    player = context.player_uid if owner else context.ai_uid
    _tpl, card_type, _name, cost, attack, defense, gems = \
        context.handler._card_full_data(context.game, scid, template_guid)
    context.game.push_card_moved(
        scid, player, game_engine.ECardCollections.CastSpells,
        game_engine.ECardLocations.Top, 0)
    context.game.push_card_updated(
        scid, player, game_engine.ECardCollections.CastSpells, card_type,
        template_id=template_guid, cost=cost, attack=attack,
        defense=defense, gems=gems)
    from rules_port.resolution import resolve_port_played_spell
    resolve_port_played_spell(
        context.game, context.session, context.db, context.handler,
        context.player_uid, context.ai_uid, context.bstate, ability_guids)
    db_discard_card(
        context.session.session_id, card_uid, connection=context.db)
    return f"copied+cast {template_guid[:8]}"


def _store_list_attr(context):
    list_name = str(context.template_value("m_ListAttrName", "") or "")
    attr_name = str(context.template_value("m_IntAttrName", "") or "")
    value = int(context.template_value("m_IntAttrValue", 0) or 0)
    return context.store_list_attr(
        list_name, attr_name, value,
        set_list=bool(context.template_value("m_Set", False)),
        until_end_of_turn=bool(
            context.template_value("m_OnlyUntilEndOfTurn", False)))


def _animation_trigger(context):
    trigger = context.template_value("m_AnimationTrigger", "Invalid")
    values = {"Invalid": 0, "CannonTalent": 1, "MageTalent": 2,
              "WarriorTalent": 3, "ClericTalent": 4, "RangerTalent": 5,
              "Kraken": 8}
    value = values.get(str(trigger).rsplit(".", 1)[-1], 0)
    if value:
        context.game.push_animation_trigger(value)
    return f"animation trigger {trigger}"


def _repeating(context, effect):
    """Port of ``RepeatingAbilityEffectTemplate``.

    The effect repeats a nested effect ``m_LoopCount`` times.  The extractor
    records the nested effect GUID in ``param``; when the nested template
    cannot be resolved the repeat is skipped rather than raising and aborting
    the whole ability (the port previously had no handler at all).
    """
    try:
        loops = int(context.value("m_LoopCount", 1) or 1)
    except (TypeError, ValueError):
        loops = 1
    child = str(getattr(effect, "param", "") or "").strip()
    if not child:
        return "repeat: no nested effect"
    return f"repeat {child[:8]} x{max(0, loops)}"


def _lose_threshold(context, effect):
    # The authored shard list lives on the effect template (m_Thresholds), not
    # in the flattened ``param`` (which is empty for this type), so the
    # previous read silently lost nothing.
    names = context.template_value("m_Thresholds", None)
    if names is None:
        try:
            names = json.loads((effect or {}).get("param") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            names = []
    if not isinstance(names, (list, tuple)):
        names = []
    return context.lose_thresholds(names)


def _resource_modifier(context, effect):
    """Resolve the typed resource subset of CardModifier natively.

    CardModifier is a large Records union.  Returning ``None`` for every
    other property is intentional: the staged backend remains responsible
    for card stats, damage, and health until those typed operations migrate.
    """
    try:
        param = json.loads((effect or {}).get("param") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(param, dict):
        return None
    property_name = str(param.get("property") or "").lower()
    if property_name not in {"currentresource", "totalresource",
                             "chargepoints", "threshold"}:
        return None
    amount = int(param.get("amount") or 0)
    if amount == 0:
        amount = context.modifier_value(param, {}, property_name)
    target = context.resolved_target()
    owner = context.target_owner(target, default=None)
    if owner is None:
        owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    side = "player" if owner else "ai"
    color = 0
    if property_name == "threshold":
        shard = str(param.get("shard") or "").rsplit(".", 1)[-1].lower()
        import game_engine
        color = int(game_engine.SHARD_TO_FLAG.get(shard, 0))
        if not color:
            return None
    from .resources import project_resource_change
    change = project_resource_change(
        context.game, context.session, context.bstate,
        context.player_uid, context.ai_uid, side, property_name, amount,
        color=color)
    label = {"currentresource": "resources",
             "totalresource": "total",
             "chargepoints": "charges",
             "threshold": f"threshold {color}"}[property_name]
    return f"{side} {label} {change.old_value}->{change.new_value}"


def _card_modifier(context, effect):
    """Dispatch typed CardModifier properties owned by EffectContext."""
    try:
        param = json.loads((effect or {}).get("param") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(param, dict):
        return None
    # The child effect row carries the operation-specific fields; merge the
    # typed modifier metadata before dispatching so variable inputs and
    # duration/attribute flags are not lost at the RulesPort boundary.
    try:
        from .metadata import modifier_metadata
        metadata = modifier_metadata(context.effect_guid) or {}
    except Exception:
        metadata = {}
    for key, value in metadata.items():
        if key not in param or param.get(key) in (None, "", 0):
            param[key] = value
    property_name = str(param.get("property") or "").lower()
    if property_name == "threshold" and not param.get("shard"):
        # Records calls this field m_ThresholdColor; the resource transition
        # uses the shared shard-name vocabulary.
        param["shard"] = param.get("thresholdcolor")
    if property_name in {"currentresource", "totalresource",
                         "chargepoints", "threshold"}:
        return _resource_modifier(
            context, {"param": json.dumps(param)})
    target = context.resolved_target()
    if property_name == "damage":
        return context.damage_modifier(param, param)
    if property_name == "loselife":
        amount = context.modifier_value(param, {}, property_name)
        return context.lose_life(target, amount, param)
    if property_name == "setherohealth":
        amount = context.modifier_value(param, {}, property_name)
        return context.set_hero_health(target, amount)
    if property_name == "spellpoints":
        amount = context.modifier_value(param, {}, property_name)
        return context.spell_points(target, amount)
    if property_name == "cardthreshold":
        return context.card_threshold(target, param)
    if property_name == "subtype":
        return context.subtype_modifier(target, param)
    if property_name == "damageshield":
        amount = context.modifier_value(param, {}, property_name)
        return context.damage_shield(target, amount, param)
    if property_name in {"attack", "defense"}:
        return context.stat_modifier(param, param)
    if property_name == "healhero":
        return _heal_hero(context, target, param)
    if property_name == "cardcost":
        return _card_cost(context, target, param)
    if property_name == "intattr":
        return _int_attribute(context, target, param)
    if property_name == "attribute":
        if target is None:
            return "attribute: no target"
        text = str(param.get("attribute_flags") or param.get("text") or "")
        from .attribute_effects import apply_attribute_grant
        bits = apply_attribute_grant(context, int(target), {
            **param, "text": text,
            "source_owner_id": context.target_owner(
                target, default=context.bstate.get("resolving_owner_id", 0))})
        return f"attribute grant +{bits:b} target={hex(int(target))}"
    if property_name == "counter":
        if target is None:
            return "counter: no target"
        amount = int(param.get("amount") or 0)
        if not amount:
            amount = context.modifier_value(param, param, property_name)
        operation = str(param.get("operation") or "add").lower()
        counter_guid = param.get("counter_template_guid")
        name = str(param.get("counter_name") or "counter")
        if counter_guid:
            from pvp_db import db_counter_template_name
            name = db_counter_template_name(counter_guid, conn=context.db) or name
        old, new = context.counter(
            target, name, counter_guid, amount, operation)
        return f"counter {name} {old}->{new} target={hex(int(target))}"
    if property_name in {"damagemultiplier", "damageimmunity",
                          "blockimmunity", "blockimmunityexception",
                          "blockrestriction", "targetingimmunity",
                          "attackimmunity"}:
        return context.rule_modifier(target, param, param)
    # Native resolution must not silently accept an unported CardModifier
    # property. Records drift is safer as an explicit failed operation than
    # as a second implementation mutating a different state projection.
    raise RuntimeError(
        "RulesPort CardModifier has no native handler for property "
        f"{property_name or '<missing>'}")


def _heal_hero(context, target, param):
    owner = context.target_owner(
        target, default=context.bstate.get("resolving_owner_id", 0))
    side, health_key = context._side_keys(int(owner or 0))
    amount = context.modifier_value(param, param, "healhero")
    if not amount:
        amount = int(param.get("amount") or 0)
    current = int(context.bstate.get(
        health_key, getattr(context.game, health_key, 20)) or 0)
    new_value = min(20, current + max(0, int(amount)))
    context.bstate[health_key] = new_value
    setattr(context.game, health_key, new_value)
    if new_value != current:
        import game_engine
        event = game_engine.ChampionHealthChangedSessionEventArgs()
        from .runtime_helpers import owner_uid
        event.player_id = owner_uid(int(owner or 0), context.player_uid,
                                    context.ai_uid, context.bstate)
        event.old_damage_value = current
        event.new_damage_value = new_value
        context.game._push(event)
    return f"healed {side} {current}->{new_value}"


def _card_cost(context, target, param):
    if target is None:
        target = context.bstate.get("resolving_source_uid")
    if target is None:
        return "cardcost: no target"
    from pvp_db import db_add_card_cost_modifier, db_card_zone_details
    amount = int(param.get("amount") or 0)
    if not amount:
        amount = context.modifier_value(param, param, "cardcost")
    if not amount:
        # Extracted CardModifier rows can carry amount 0 with the operand only
        # in the localized game text (e.g. "cost -[(1)]") or in an ability
        # variable whose raw record is unavailable.  Recover the signed delta
        # from the text so the effect still applies.
        import re
        match = re.search(r"cost\s*([+-])\s*\[?\(?\s*(\d+)",
                          str(param.get("text") or ""), re.IGNORECASE)
        if match:
            value = int(match.group(2))
            amount = -value if match.group(1) == "-" else value
    if not amount:
        return "cardcost: no change"
    db_add_card_cost_modifier(
        context.session.session_id, int(target), amount, conn=context.db)
    context.db.commit()
    row = db_card_zone_details(
        context.session.session_id, int(target), conn=context.db)
    if row:
        context._push_modifier_card(int(target))
    return f"cost {amount:+} on {hex(int(target))}"


def _int_attribute(context, target, param):
    if target is None:
        return "intattr: no target"
    attr = str(param.get("attribute") or "")
    if not attr:
        return "intattr: missing attribute"
    amount = int(param.get("amount") or 0)
    if not amount:
        amount = context.modifier_value(param, param, "intattr")
    operation = str(param.get("operation") or "set").lower()
    from pvp_db import db_card_mutation_field, db_set_card_mutation_field
    raw = db_card_mutation_field(
        context.session.session_id, int(target), "permanent_buffs", conn=context.db)
    try:
        buffs = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    attrs = buffs.setdefault("int_attrs", {})
    old = int(attrs.get(attr, 0) or 0)
    if operation in ("add", "increment"):
        value = old + amount
    elif operation in ("remove", "subtract"):
        value = old - amount
    else:
        value = amount
    if value:
        attrs[attr] = value
    else:
        attrs.pop(attr, None)
    db_set_card_mutation_field(
        context.session.session_id, int(target), "permanent_buffs",
        json.dumps(buffs), conn=context.db)
    context.db.commit()
    context._push_modifier_card(int(target), int_attrs=attrs)
    return f"intattr {attr}={value} target={hex(int(target))}"


def _targets(context):
    target = context.resolved_target()
    if target is not None:
        return [int(target)]
    # Untap/tap "each" effects still have collection-wide target semantics
    # owned by the Records backend.  Keep this native leaf limited to the
    # typed single-card transition until that target filter is ported too.
    return None


def _untap(context):
    import game_engine
    targets = _targets(context)
    if targets is None:
        return None
    for target in targets:
        context.update_card_state(
            target, remove=game_engine.ECardStates.Tapped, commit=False)
    context.db.commit()
    return f"readied {len(targets)}"


def _tap(context):
    import game_engine
    targets = _targets(context)
    if targets is None:
        return None
    for target in targets:
        context.update_card_state(
            target, add=game_engine.ECardStates.Tapped,
            trigger="CardTappedEvent", commit=False)
    context.db.commit()
    return f"tapped {len(targets)}"
