"""RulesPort-native Records effect operations.

The resolver still obtains effect ordering and targets from Records.  This
module is the first native leaf boundary: simple state transitions execute
through the typed EffectContext without entering the legacy BOM leaf.
"""

from __future__ import annotations

import json
from typing import Any, cast

import game_engine


# Effect templates that read their *whole* resolved target set from Records
# instead of acting on a single target.  C# applies these once per
# AbilityEffectInstance (``AbilityEffectTemplate.Apply``), so the resolver's
# per-target loop must not invoke them once per member card.
SELF_TARGETED_EFFECTS = frozenset({
    # "Look at the top five cards of your deck": re-running the reveal for
    # each of the five resolved cards revealed the same five cards five times
    # and queued one client Coverflow per copy.
    "RevealCardsAbilityEffectTemplate",
    # C# overrides Apply(AbilityEffectInstance) and applies its nested child
    # to the whole target instance once. Calling it once per target multiplies
    # the authored loop count by the target count.
    "RepeatingAbilityEffectTemplate",
})


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
        "Battle2CardsAbilityEffectTemplate":
            lambda: context.battle_cards(effect),
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
        "activation_data": dict(original.get("activation_data") or {}),
    }
    chain.push(context.bstate, copied)
    source = original.get("source_uid")
    if source is not None:
        activation = copied["activation_data"]
        target_map = activation.get("target_map") or {}
        target_ids = []
        for selected in target_map.values() if isinstance(target_map, dict) else ():
            selected = selected if isinstance(selected, (tuple, list, set)) else (selected,)
            for value in selected:
                try:
                    target_ids.append(int(getattr(value, "uid64", value)))
                except (TypeError, ValueError):
                    continue
        context.game.push_ability_on_chain(
            game_engine.SessionCardId(game_engine.UID(int(source))),
            game_engine.ResourceId.from_str(str(original["ability_guid"])),
            ability_instance_id=instance_id,
            target_card_ids=target_ids)
    return f"copied ability {str(original['ability_guid'])[:8]}"


def grant_ability(context):
    """Apply a typed ability grant to a session card or champion."""
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
    template = context.template_value("m_AbilityIsUnique", True)
    unique = bool(template)

    # Port of GrantAbilityEffectTemplate.Apply's typed power sources.  The
    # random powers are picked once per ability instance (the client caches
    # the choice in the ability instance's RandomPowerTemplate string).
    source_uid = context.bstate.get("resolving_source_uid")
    random_inspire = bool(context.template_value("m_RandomInspirePower", False))
    random_charge = bool(context.template_value(
        "m_RandomChampionChargePower", False))
    all_socket_source = bool(context.template_value(
        "m_AllSocketedPowersOfSource", False))
    all_socket_target = bool(context.template_value(
        "m_AllSocketedPowersOfTarget", False))
    all_socket_master = bool(context.template_value(
        "m_AllSocketedPowersOfMyMaster", False))
    all_pay_source = bool(context.template_value(
        "m_AllPaymentPowersOfSource", False))
    all_pay_target = bool(context.template_value(
        "m_AllPaymentPowersOfTarget", False))
    all_remembered = bool(context.template_value("m_AllRememberedPowers", False))
    if (random_inspire or random_charge) and not granted_values:
        picked = context.bstate.get("random_power_template")
        if not picked:
            from pvp_db import (db_champion_charge_power_guids,
                                db_inspire_ability_guids)
            pool = (db_inspire_ability_guids(conn=context.db) if random_inspire
                    else db_champion_charge_power_guids(conn=context.db))
            if pool:
                rng = context.bstate.get("_rules_rng")
                if rng is not None and hasattr(rng, "next"):
                    picked = pool[int(rng.next(len(pool))) % len(pool)]
                else:
                    import random as _random
                    picked = pool[_random.randrange(len(pool))]
                context.bstate["random_power_template"] = picked
        if picked:
            granted_values = [str(picked).lower()]
    if (all_socket_source or all_socket_target or all_socket_master or
            all_pay_source or all_pay_target or all_remembered):
        from pvp_db import (db_card_gem_ability_guids,
                            db_card_manual_ability_guids)
        values = set(granted_values)
        if source_uid is not None and (all_socket_source or all_socket_master):
            # ParentLink.GemAbilities is the master's socketed powers.  The
            # master link is not materialized server-side, so the source's own
            # socketed gems are the available authored set.
            values.update(db_card_gem_ability_guids(
                context.session.session_id, int(source_uid), conn=context.db))
        if all_socket_target:
            values.update(db_card_gem_ability_guids(
                context.session.session_id, int(target), conn=context.db))
        if source_uid is not None and all_pay_source:
            values.update(db_card_manual_ability_guids(
                context.session.session_id, int(source_uid), conn=context.db))
        if all_pay_target:
            values.update(db_card_manual_ability_guids(
                context.session.session_id, int(target), conn=context.db))
        if all_remembered:
            remembered = context.bstate.get("remembered_powers") or {}
            for guid in remembered.get(
                    context.bstate.get("resolving_ability"), []):
                if guid:
                    values.add(str(guid).lower())
        granted_values = list(dict.fromkeys(
            str(value).lower() for value in values if value))
    # C# grants "powers of the target" to the ability's source card (Mega-Bot
    # 9000, Gemborn Prowler); every other source grants to the target.
    destination = target
    if (all_socket_target or all_pay_target) and source_uid is not None:
        destination = source_uid
    duration = context.effect_duration
    grant_owner = int(context.bstate.get("resolving_owner_id", 0) or 0)

    def record_duration_bound_grants(added_values, target_owner):
        if not added_values:
            return
        from .effect_lifetimes import record_grant
        expiration_owner = grant_owner
        if duration == "BeginningOfOpponentsTurn":
            if context.bstate.get("pvp"):
                expiration_owner = next((int(pid) for pid in
                    context.bstate.get("pids", ())
                    if int(pid) != grant_owner), 0)
            else:
                profile = getattr(context.handler, "user_profile", None) or {}
                player_id = int(profile.get("id", 0) or 0)
                expiration_owner = 0 if grant_owner else player_id
        elif duration == "AfterCardsReadyOnPlayersTurn":
            expiration_owner = int(target_owner or grant_owner)
        for granted_guid in added_values:
            record_grant(
                context.bstate, source_uid=source_uid,
                target_uid=int(destination), ability_guid=granted_guid,
                duration=duration, owner_id=grant_owner,
                target_owner_id=target_owner,
                expiration_owner_id=expiration_owner)

    # Champions are represented in the client session by SessionCardId, but
    # deliberately have no ``game_cards`` row.  Encounter setup cards can
    # grant an ability to a champion (for example Cockatwice grants its
    # opposing champion the GameStarted Taming Sphere summon), so retain
    # those abilities on the handler's per-battle champion list.
    profile = getattr(context.handler, "user_profile", None) or {}
    player_owner = int(profile.get("id", 0) or 0)
    for attr, owner in (("_player_champ_scid", player_owner),
                        ("_ai_champ_scid", 0)):
        champion = getattr(context.handler, attr, None)
        try:
            champion_uid = int(getattr(cast(Any, champion), "uid").uid64)
        except (AttributeError, TypeError, ValueError):
            continue
        if champion_uid != int(target):
            continue
        dynamic = getattr(context.handler,
                          "_champion_granted_ability_guids", None)
        if dynamic is None:
            dynamic = context.handler._champion_granted_ability_guids = {}
        abilities = dynamic.setdefault(champion_uid, [])
        added = []
        for guid in granted_values:
            if (not db_ability_metadata_exists(guid, conn=context.db) and
                    ability_graph(DEFAULT_RECORD_STORE, guid) is None):
                continue
            if unique and guid in abilities:
                continue
            abilities.append(guid)
            added.append(guid)
        record_duration_bound_grants(
            added, context.target_owner(int(destination), default=grant_owner))
        # A hidden encounter setup card can grant a GameStarted ability while
        # the event is already traversing the opposing side.  That champion's
        # normal discovery pass has then finished, so run the newly granted
        # authored trigger now instead of losing its one setup opportunity.
        # A permanent CardModifier with a card-filter target is a continuous
        # champion aura.  Resolve its current board effect at the same point;
        # later CardEnteredZoneEvent dispatches keep that aura current.
        if (added and context.bstate.get("event_type") ==
                "GameStartedEvent"):
            from rules_port.resolution import resolve_port_ability
            for guid in added:
                graph = ability_graph(DEFAULT_RECORD_STORE, guid)
                continuous_card_modifier = bool(graph and not graph.trigger_event_type and
                    any(effect.concrete_type == "CardModifierAbilityEffectTemplate" and
                        str(effect.duration).lower() == "permanent"
                        for effect in graph.effects) and
                    any(target.card_filter for target in graph.targets))
                if (graph is not None and
                    (str(graph.trigger_event_type or "").rsplit(
                        ".", 1)[-1] == "GameStartedEvent" or
                     continuous_card_modifier)):
                    resolve_port_ability(
                        context.handler, context.game, context.session,
                        context.db, context.player_uid, context.ai_uid,
                        context.bstate, guid, champion_uid, owner,
                        target_map={})
        return f"granted {len(added)} champion ability(s) to {hex(champion_uid)}"

    row = db_card_grant_info(
        context.session.session_id, int(destination), conn=context.db)
    if not row:
        return "grant: target card not found"
    try:
        abilities = json.loads(row[0] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        abilities = []
    added = []
    for guid in granted_values:
        if (not db_ability_metadata_exists(guid, conn=context.db) and
                ability_graph(DEFAULT_RECORD_STORE, guid) is None):
            continue
        if unique and guid in abilities:
            continue
        abilities.append(guid)
        added.append(guid)
    record_duration_bound_grants(
        added, context.target_owner(int(destination), default=grant_owner))
    db_set_card_abilities(
        context.session.session_id, int(destination), json.dumps(abilities),
        conn=context.db)
    context.db.commit()
    if random_inspire:
        # C# sets the Inspire IntAttr on the granted card in the target loop.
        from pvp_db import db_card_mutation_field, db_set_card_mutation_field
        try:
            buffs = json.loads(db_card_mutation_field(
                context.session.session_id, int(target), "permanent_buffs",
                conn=context.db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            buffs = {}
        attrs = buffs.get("int_attrs")
        if not isinstance(attrs, dict):
            attrs = {}
        previous_inspire = int(attrs.get("Inspire", 0) or 0)
        attrs["Inspire"] = 1
        buffs["int_attrs"] = attrs
        db_set_card_mutation_field(
            context.session.session_id, int(target), "permanent_buffs",
            json.dumps(buffs), conn=context.db)
        context.db.commit()
        context.emit_int_attribute_gained(
            int(target), "Inspire", previous_inspire, 1)
    scid = game_engine.SessionCardId(game_engine.UID(int(destination)))
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
    return f"granted {len(added)} ability(s) to {hex(int(destination))}"


def create_and_cast_spell(context):
    """Copy the selected spell and resolve its authored abilities natively.

    ``m_AmountField`` is the typed copy count (C# ``CopyCardAndPutOnChain``
    runs once per amount); the typed single-copy form defaults to one.
    ``m_SendPlayAction`` is a client-side presentation flag and does not alter
    the server's chain behavior.
    """
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
    try:
        amount = int(context.value("m_AmountField", 1) or 1)
    except (TypeError, ValueError):
        amount = 1
    amount = max(1, min(amount, 20))
    owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    try:
        ability_guids = [str(value).lower() for value in json.loads(
            payload[1] or "[]") if value]
    except (TypeError, ValueError, json.JSONDecodeError):
        ability_guids = []
    from rules_port.resolution import resolve_port_played_spell
    copied = 0
    for _ in range(amount):
        card_uid = next_game_card_uid(context.db, context.session.session_id)
        db_insert_generated_card(
            context.session.session_id, owner, card_uid, template_guid,
            "CastSpells", payload[0], payload[1], payload[2],
            db_next_game_card_row_id(context.session.session_id, conn=context.db),
            conn=context.db)
        context.db.commit()
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
        resolve_port_played_spell(
            context.game, context.session, context.db, context.handler,
            context.player_uid, context.ai_uid, context.bstate, ability_guids)
        db_discard_card(
            context.session.session_id, card_uid, connection=context.db)
        copied += 1
        # A copy that opened a picker/continuation must finish before the
        # next copy is created, exactly like one chain item at a time.
        if context.bstate.get("resolution_paused"):
            break
    return f"copied+cast {template_guid[:8]} x{copied}"


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
    """Apply C#'s nested ``RepeatingEffect`` on the shared target instance."""
    try:
        loops = int(context.value("m_LoopCount", 1) or 1)
    except (TypeError, ValueError):
        loops = 1
    child = context.template_value("m_RepeatingEffect", None)
    if child is None:
        return "repeat: no nested effect"
    return context.apply_repeating_effect(child, max(0, loops))


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
    # Practice uses ``0`` for the AI owner, while PvP uses raw participant
    # IDs for both players.  Truthiness therefore cannot identify the side in
    # a PvP effect view; map the resolved owner against the view's ordered
    # participant IDs instead.
    if context.bstate.get("pvp"):
        pids = [int(pid) for pid in (context.bstate.get("pids") or ())]
        side = "player" if pids and int(owner) == pids[0] else "ai"
    else:
        side = "player" if owner else "ai"
    color = 0
    if property_name == "threshold":
        shard = str(param.get("shard") or "").rsplit(".", 1)[-1].lower()
        import game_engine
        random_lowest = param.get("randomlowestthreshold", False)
        random_color = param.get("random", False)
        random_lowest = (random_lowest is True or
                         str(random_lowest).strip().lower() in ("1", "true"))
        random_color = (random_color is True or
                        str(random_color).strip().lower() in ("1", "true"))
        if random_lowest or random_color:
            # ThresholdModifier.Apply uses the session RNG. RandomLowestThreshold
            # enumerates the player's five initialized thresholds in Player's
            # constructor order; Random uses the five shard bits in enum order.
            color_names = (("blood", "sapphire", "wild", "diamond", "ruby")
                           if random_lowest else
                           ("blood", "ruby", "sapphire", "wild", "diamond"))
            colors = [int(game_engine.SHARD_TO_FLAG[name])
                      for name in color_names]
            if random_lowest:
                thresholds = context.bstate.get(f"{side}_threshold") or {}
                current = {
                    flag: int(thresholds.get(
                        flag, thresholds.get(str(flag), 0)) or 0)
                    for flag in colors
                }
                minimum = min(current.values())
                colors = [flag for flag in colors
                          if current[flag] == minimum]
            if len(colors) > 1:
                rng = context.bstate.get("_rules_rng")
                if rng is not None and hasattr(rng, "next"):
                    color = colors[int(rng.next(0, len(colors))) % len(colors)]
                else:
                    import random
                    color = random.choice(colors)
            elif colors:
                color = colors[0]
        else:
            color = int(game_engine.SHARD_TO_FLAG.get(shard, 0))
        if not color:
            return None
    from .resources import project_resource_change
    change = project_resource_change(
        context.game, context.session, context.bstate,
        context.player_uid, context.ai_uid, side, property_name, amount,
        color=color)
    # The C# resource modifiers keep per-effect-instance accounting alongside
    # the player pool mutation. These values back shipped IntAttr operands such
    # as ResourcesDepleted and ChargePointsDrained.
    target_info = None
    if target is not None:
        from pvp_db import db_card_source_info
        target_info = db_card_source_info(
            context.session.session_id, int(target), conn=context.db)
    is_resource_card = bool(
        target_info and "Resource" in str(target_info[1] or ""))
    if not is_resource_card:
        from .statistics import add_ability_stat
        delta = int(change.new_value) - int(change.old_value)
        if property_name == "currentresource":
            if delta > 0:
                add_ability_stat(context.bstate, "ResourcesReplenished", delta)
            elif delta < 0:
                add_ability_stat(context.bstate, "ResourcesDepleted", -delta)
        elif property_name == "totalresource":
            if delta > 0:
                add_ability_stat(context.bstate, "ResourcesGained", delta)
            elif delta < 0:
                add_ability_stat(context.bstate, "ResourcesLost", -delta)
        elif property_name == "chargepoints" and delta < 0:
            add_ability_stat(
                context.bstate, "ChargePointsDrained",
                min(-delta, int(change.old_value)))
    # TurnStarted triggers resolve before the following Prep refill. Preserve
    # positive temporary current-resource gains so Prep restores the normal
    # total and then reapplies the authored bonus (for example Lithe
    # Lyricist's +1 current resource).
    if property_name == "currentresource" and amount > 0:
        import game_engine
        phase = context.bstate.get("phase")
        if phase == game_engine.ETurnPhases.StartTurn:
            owner_id = int(owner or 0)
            if context.bstate.get("pvp"):
                bonus_key = f"start_turn_resource_bonus_{owner_id}"
            else:
                bonus_key = ("start_turn_resource_bonus_player"
                             if owner_id else
                             "start_turn_resource_bonus_ai")
            context.bstate[bonus_key] = int(
                context.bstate.get(bonus_key, 0) or 0) + int(amount)
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
        metadata = modifier_metadata(
            context.effect_guid,
            template=getattr(context, "effect_template_override", None)) or {}
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
    if property_name == "tag":
        return _card_tag(context, target, param)
    if property_name == "attribute":
        if target is None:
            return "attribute: no target"
        text = str(param.get("attribute_flags") or param.get("text") or "")
        from .attribute_effects import apply_attribute_grant
        resolving_owner = context.bstate.get("resolving_owner_id", 0)
        # BeginningOfOwnersTurn is owned by the ability's responsible player
        # (the source controller), even when it grants an attribute to an
        # opposing card.  AfterCardsReadyOnPlayersTurn is the exception: that
        # duration ends at the affected troop controller's next Ready step.
        expiration_owner = resolving_owner
        if param.get("duration") == "AfterCardsReadyOnPlayersTurn":
            expiration_owner = context.target_owner(
                target, default=resolving_owner)
        bits = apply_attribute_grant(context, int(target), {
            **param, "text": text,
            "source_owner_id": expiration_owner})
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
    amount = context.modifier_value(param, param, "healhero")
    if not amount:
        amount = int(param.get("amount") or 0)
    return context.gain_health(int(owner or 0), amount)


def _card_cost(context, target, param):
    if target is None:
        target = context.bstate.get("resolving_source_uid")
    if target is None:
        return "cardcost: no target"
    from pvp_db import (db_add_card_cost_modifier, db_card_zone_details,
                        db_card_mutation_field, db_set_card_mutation_field)
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
    duration = str(param.get("duration") or context.effect_duration)
    if duration == "UntilItLeavesYourHand":
        try:
            buffs = json.loads(db_card_mutation_field(
                context.session.session_id, int(target), "temporary_buffs",
                conn=context.db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            buffs = {}
        mods = buffs.setdefault("temporary_cost_modifiers", [])
        mods.append({"delta": int(amount), "duration": duration,
                     "source_uid": context.bstate.get("resolving_source_uid"),
                     "target_uid": int(target)})
        db_set_card_mutation_field(
            context.session.session_id, int(target), "temporary_buffs",
            json.dumps(buffs, separators=(",", ":"), sort_keys=True),
            conn=context.db)
    else:
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
    duration = str(param.get("duration") or context.effect_duration)
    temporary_durations = {
        "EndOfTurn", "EndOfNextTurn", "BeginningOfOwnersTurn",
        "BeginningOfOpponentsTurn", "AfterCardsReadyOnPlayersTurn",
        "UntilDamaged", "UntilItLeavesYourHand",
    }
    resolving_owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    if duration == "BeginningOfOwnersTurn":
        expiration_owner = resolving_owner
    elif duration == "BeginningOfOpponentsTurn":
        if context.bstate.get("pvp"):
            expiration_owner = next((int(pid) for pid in
                context.bstate.get("pids", ())
                if int(pid) != resolving_owner), 0)
        else:
            profile = getattr(context.handler, "user_profile", None) or {}
            player_owner = int(profile.get("id", 0) or 0)
            expiration_owner = (0 if resolving_owner == player_owner
                                else player_owner)
    elif duration == "AfterCardsReadyOnPlayersTurn":
        expiration_owner = int(context.target_owner(
            target, default=resolving_owner) or resolving_owner)
    else:
        expiration_owner = None
    champion_owner = context._champion_owner(target)
    if champion_owner is not None:
        uid_key = str(int(target))
        all_attrs = context.bstate.setdefault("champion_int_attrs", {})
        attrs = all_attrs.setdefault(uid_key, {})
        previous = attrs.get(attr)
        if operation in ("add", "increment"):
            value = int(previous or 0) + amount
        elif operation in ("remove", "subtract"):
            value = int(previous or 0) - amount
        else:
            value = amount
        if value:
            attrs[attr] = value
        else:
            attrs.pop(attr, None)
        if duration in temporary_durations:
            from .effect_lifetimes import record_temporary_intattr
            record_temporary_intattr(
                context.bstate, int(target), attr, previous, attrs.get(attr),
                champion=True, duration=duration,
                owner_id=resolving_owner,
                target_owner_id=champion_owner,
                source_uid=context.bstate.get("resolving_source_uid"),
                expiration_owner_id=expiration_owner)
        context._push_champion_intattrs(champion_owner, int(target))
        context.emit_int_attribute_gained(target, attr, previous, value)
        return f"intattr {attr}={value} champion={hex(int(target))}"

    from pvp_db import db_card_mutation_field, db_set_card_mutation_field
    temporary = duration in {
        "EndOfTurn", "EndOfNextTurn", "BeginningOfOwnersTurn",
        "BeginningOfOpponentsTurn",
        "AfterCardsReadyOnPlayersTurn", "UntilDamaged",
        "UntilItLeavesYourHand"}
    column = "temporary_buffs" if temporary else "permanent_buffs"
    raw = db_card_mutation_field(
        context.session.session_id, int(target), column, conn=context.db)
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
        context.session.session_id, int(target), column,
        json.dumps(buffs), conn=context.db)
    context.db.commit()
    if duration in temporary_durations:
        from .effect_lifetimes import record_temporary_intattr
        record_temporary_intattr(
            context.bstate, int(target), attr, old if old else None,
            attrs.get(attr), duration=duration,
            owner_id=resolving_owner,
            target_owner_id=context.target_owner(int(target), default=None),
            source_uid=context.bstate.get("resolving_source_uid"),
            expiration_owner_id=expiration_owner)
    context._push_modifier_card(int(target), int_attrs=attrs)
    context.emit_int_attribute_gained(target, attr, old, value)
    return f"intattr {attr}={value} target={hex(int(target))}"


def _card_tag(context, target, param):
    if target is None:
        target = context.bstate.get("resolving_source_uid")
    if target is None:
        return "tag: no target"
    tag = str(param.get("tag") or "")
    if not tag:
        return "tag: missing tag"
    amount = int(param.get("amount") or 0)
    if not amount:
        amount = context.modifier_value(param, param, "tag")
    amount = int(amount or 0)
    operation = str(param.get("operation") or "add").lower()
    duration = str(param.get("duration") or context.effect_duration)
    temporary = duration in {
        "EndOfTurn", "EndOfNextTurn", "BeginningOfOwnersTurn",
        "BeginningOfOpponentsTurn", "AfterCardsReadyOnPlayersTurn",
        "UntilDamaged", "UntilItLeavesYourHand"}
    from pvp_db import db_card_mutation_field, db_set_card_mutation_field
    column = "temporary_buffs" if temporary else "permanent_buffs"
    raw = db_card_mutation_field(
        context.session.session_id, int(target), column, conn=context.db)
    try:
        buffs = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if not isinstance(buffs, dict):
        buffs = {}
    tags = buffs.get("tags")
    if not isinstance(tags, dict):
        tags = {}
    key = tag.lower()
    old = int(tags.get(key, 0) or 0)
    if operation in ("add", "increment"):
        tags[key] = old + amount
    elif operation in ("remove", "subtract"):
        if amount == 0:
            tags.pop(key, None)
        else:
            tags[key] = max(0, old - amount)
    elif operation == "set":
        if amount:
            tags[key] = amount
        else:
            tags.pop(key, None)
    else:
        return f"tag: unsupported operation {operation}"
    if tags:
        buffs["tags"] = tags
    else:
        buffs.pop("tags", None)
    db_set_card_mutation_field(
        context.session.session_id, int(target), column,
        json.dumps(buffs), conn=context.db)
    context.db.commit()
    context._push_modifier_card(int(target))
    return f"tag {key}={int(tags.get(key, 0) or 0)} target={hex(int(target))}"


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
    from pvp_db import db_card_owner_id
    from .combat_rules import card_int_attr
    resolving = int(context.bstate.get("resolving_owner_id", 0) or 0)
    tapped = 0
    for target in targets:
        # C# TapCardAbilityEffectTemplate: CantBeExhaustedByOpponent blocks an
        # opponent-authored exhaust.
        if card_int_attr(context.db, context.session.session_id, int(target),
                         "CantBeExhaustedByOpponent") > 0:
            owner = db_card_owner_id(
                context.session.session_id, int(target), conn=context.db)
            if owner is not None and int(owner) != resolving:
                continue
        context.update_card_state(
            target, add=game_engine.ECardStates.Tapped,
            trigger="CardTappedEvent", commit=False)
        tapped += 1
    context.db.commit()
    return f"tapped {tapped}"
