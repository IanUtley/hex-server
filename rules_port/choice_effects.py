"""RulesPort choice scheduling for metadata DoubleChoice effects."""

from __future__ import annotations

import random
import json
import game_engine

from .wire import extract_session_card_uids


def extract_card_uids(raw):
    """Extract typed card SessionCardIds from a client activation payload."""
    return list(extract_session_card_uids(raw))


def _resource_guids(value):
    result = []
    for item in value or ():
        item = item.get("m_Guid") or item.get("guid") if isinstance(item, dict) else item
        if item and str(item) != "0" * 36:
            result.append(str(item).lower())
    return result


def _clear_choice_zone(context):
    from pvp_db import db_choice_card_rows, db_move_choice_to_played_resources
    from .runtime_helpers import owner_uid
    rows = db_choice_card_rows(context.session.session_id, conn=context.db)
    for uid, owner, template_guid, card_type_name in rows:
        db_move_choice_to_played_resources(context.session.session_id, int(uid),
                                           conn=context.db)
        scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
        _tpl, card_type, card_name, cost, attack, defense, gems = \
            context.handler._card_full_data(
                context.game, scid, template_guid, card_type_name)
        recipient = owner_uid(owner, context.player_uid, context.ai_uid,
                              context.bstate)
        context.game.push_card_updated(
            scid, recipient, game_engine.ECardCollections.PlayedResources,
            card_type, template_id=template_guid, card_name=card_name,
            cost=cost, attack=attack, defense=defense, gems=gems, state=0)
        # The C# client cache derives the UI move from the destination
        # CardUpdated. An explicit class-50 event would duplicate it.
    if rows:
        context.db.commit()


def _create_choice_cards(context, owner_id, template_guids, parent_uid=None):
    from pvp_db import (db_copy_template_payload, db_next_game_card_row_id,
                        db_insert_generated_card)
    from .runtime_helpers import next_game_card_uid, owner_uid
    created = []
    for template_guid in template_guids:
        row = db_copy_template_payload(template_guid, conn=context.db)
        if not row:
            continue
        uid = next_game_card_uid(context.db, context.session.session_id)
        parent_buffs = (json.dumps({"parent_uid": int(parent_uid)})
                        if parent_uid is not None else None)
        db_insert_generated_card(
            context.session.session_id, int(owner_id), uid, template_guid,
            "choosing", row[0] or "Choice", row[1], row[2],
            db_next_game_card_row_id(context.session.session_id, conn=context.db),
            conn=context.db, position=0, card_state=0,
            owner_user_id=int(owner_id), original_template_guid=template_guid,
            permanent_buffs=parent_buffs)
        created.append((int(uid), template_guid))
    context.db.commit()
    player = owner_uid(owner_id, context.player_uid, context.ai_uid,
                       context.bstate)
    for uid, template_guid in created:
        scid = game_engine.SessionCardId(game_engine.UID(uid))
        _tpl, card_type, name, cost, attack, defense, gems = \
            context.handler._card_full_data(context.game, scid, template_guid)
        context.game.push_card_updated(
            scid, player, game_engine.ECardCollections.Choosing, card_type,
            template_id=template_guid, card_name=name, cost=cost,
            attack=attack, defense=defense, gems=gems, state=0)
        # A first CardUpdated seeds the client cache and derives the UI move
        # from None -> Choosing; class 50 would duplicate that notification.
    return [uid for uid, _template in created]


def _choose_ai(context, choice_uids):
    if not choice_uids:
        raise IndexError("cannot choose from an empty sequence")
    rng = context.bstate.get("_rules_rng")
    if rng is not None and hasattr(rng, "next"):
        return int(choice_uids[int(rng.next(len(choice_uids)))])
    return int(choice_uids[0])


def _play_choice_card(context, chosen_uid, owner_id):
    from pvp_db import db_card_source_info, db_move_choice_to_played_resources
    row = db_card_source_info(context.session.session_id, int(chosen_uid),
                              conn=context.db)
    if not row or row[2] != "choosing" or int(row[3] or 0) != int(owner_id):
        return False
    db_move_choice_to_played_resources(context.session.session_id,
                                       int(chosen_uid), conn=context.db)
    context.db.commit()
    from .runtime_helpers import owner_uid
    player = owner_uid(owner_id, context.player_uid, context.ai_uid,
                       context.bstate)
    scid = game_engine.SessionCardId(game_engine.UID(int(chosen_uid)))
    _tpl, card_type, name, cost, attack, defense, gems = \
        context.handler._card_full_data(context.game, scid, row[0])
    context.game.push_card_updated(
        scid, player, game_engine.ECardCollections.PlayedResources, card_type,
        template_id=row[0], card_name=name, cost=cost, attack=attack,
        defense=defense, gems=gems, state=0)
    # CardUpdated changes the collection and causes ClientStateCache to
    # deliver the UI CardMoved that the C# client derives locally.
    context.game.push_spell_card_played(scid, player)
    return True


def _resolve_choice_card_abilities(context, chosen_uid, source_uid, owner_id):
    from pvp_db import (db_card_ability_payload, db_card_mutation_field,
                        db_set_card_mutation_field)
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from .resolution import resolve_port_ability
    if source_uid is not None and int(source_uid) != int(chosen_uid):
        raw_buffs = db_card_mutation_field(
            context.session.session_id, int(chosen_uid), "permanent_buffs",
            conn=context.db)
        try:
            parent_buffs = json.loads(raw_buffs or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            parent_buffs = {}
        if not isinstance(parent_buffs, dict):
            parent_buffs = {}
        if not int(parent_buffs.get("parent_uid", 0) or 0):
            parent_buffs["parent_uid"] = int(source_uid)
            db_set_card_mutation_field(
                context.session.session_id, int(chosen_uid),
                "permanent_buffs", json.dumps(parent_buffs), conn=context.db)
            context.db.commit()
    try:
        values = json.loads(db_card_ability_payload(
            context.session.session_id, int(chosen_uid), conn=context.db) or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    results = []
    # C# PlayChoiceCard activates the selected card's automatic abilities with
    # that card as SourceCardId. ChoiceOverride source targets still refer to
    # the originating ability card through the generated card's parent link.
    choice_source_uid = int(chosen_uid)
    for guid in values or ():
        graph = ability_graph(DEFAULT_RECORD_STORE, str(guid).lower())
        if graph is None:
            continue
        ability_index = int(
            graph.source.field("m_AbilityIndex", -1) or -1)
        # C# Card.GetAutomaticAbilities excludes manual, event-triggered,
        # indexed, and effect-free abilities. Triggered abilities are handled
        # by event dispatch, not by playing a choice card.
        if (graph.manual or graph.trigger_event_type or ability_index >= 0 or
                not graph.effects):
            continue
        old_source = context.bstate.get("resolving_source_uid")
        old_targets = {
            key: context.bstate.get(key)
            for key in ("player_spell_target", "player_mod_target",
                        "resolving_target_uid")}
        context.bstate["resolving_source_uid"] = choice_source_uid
        for key in old_targets:
            context.bstate.pop(key, None)
        try:
            results.append(resolve_port_ability(
                context.handler, context.game, context.session, context.db,
                context.player_uid, context.ai_uid, context.bstate,
                str(guid).lower(), choice_source_uid, owner_id,
                target_map={}, variables={}))
        finally:
            if old_source is None:
                context.bstate.pop("resolving_source_uid", None)
            else:
                context.bstate["resolving_source_uid"] = old_source
            for key, value in old_targets.items():
                if value is not None:
                    context.bstate[key] = value
        if context.bstate.get("resolution_paused"):
            break
    return results


def double_choice(context):
    """Create the authored choice stage and either resolve or suspend it."""
    typed_second = bool(context.template_value("m_SecondChoice", False))
    owner_id = int(context.bstate.get("resolving_owner_id", 0) or 0)
    source_uid = context.bstate.get("resolving_source_uid")
    ability_guid = context.bstate.get("resolving_ability", "")
    _clear_choice_zone(context)
    if typed_second:
        templates = list(context.bstate.pop(
            "double_choice_remaining_guids", []))
        if not templates:
            return "double choice: no remaining choices"
    else:
        choices = _resource_guids(context.template_value("m_Choices", []))
        if not choices:
            return "double choice: no choices"
        # ``m_NumOptions`` is an EffectConstant/EffectInputVariable, so it must
        # be resolved through ``value`` (int(dict) raised TypeError and aborted
        # the effect).
        count = max(0, min(int(context.value("m_NumOptions", 0) or 0),
                           len(choices)))
        random_source = context.bstate.get("_rules_rng")
        remaining = list(choices)
        templates = []
        for _ in range(count):
            if random_source is not None and hasattr(random_source, "next"):
                index = int(random_source.next(len(remaining)))
            else:
                index = random.randrange(len(remaining))
            templates.append(remaining.pop(index))
        context.bstate["double_choice_remaining_guids"] = remaining
    choice_uids = _create_choice_cards(
        context, owner_id, templates, parent_uid=source_uid)
    if not choice_uids:
        return "double choice: no card templates"
    if owner_id == 0:
        chosen = _choose_ai(context, choice_uids)
        # The authored ability normally follows DoubleChoice with an
        # ActivateAbility child that plays the selected choice. Preserve the
        # typed card identity for that child; the controller identity (AI=0)
        # is not a valid card target.
        context.bstate["selected_choice_uid"] = int(chosen)
        _play_choice_card(context, chosen, owner_id)
        _resolve_choice_card_abilities(
            context, chosen, source_uid, owner_id)
        return f"double choice: AI chose {hex(int(chosen))}"
    pending = context.continuation()
    pending.update({"kind": "double_choice", "choice_uids": list(dict.fromkeys(
        int(uid) for uid in choice_uids))})
    context.bstate["pending_choice"] = pending
    context.bstate["resolution_paused"] = True
    prompt = getattr(context.handler, "_prompt_choice_cards", None)
    if callable(prompt):
        prompt(context.game, context.session, context.player_uid,
               context.ai_uid, context.bstate, pending)
    return f"double choice: awaiting {len(choice_uids)} choices"


def _resolve_child(handler, game, session, db, pl_t, ai_t, bstate,
                   guid, source, owner, target_map=None, variables=None):
    from .resolution import resolve_port_ability
    return resolve_port_ability(
        handler, game, session, db, pl_t, ai_t, bstate, guid, source, owner,
        target_map=target_map or {}, variables=variables or {})
