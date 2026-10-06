"""RulesPort-owned transform effect operations."""

from __future__ import annotations

import json
import random
from types import SimpleNamespace
import game_engine


def linked_template_guids(db, ability_guid):
    """Return card ResourceIds linked by the typed Records ability graph."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from pvp_db import db_template_exists
    graph = ability_graph(DEFAULT_RECORD_STORE, str(ability_guid or "").lower())
    if graph is None:
        return []
    found = []
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"m_Guid", "guid"} and isinstance(child, str):
                    value = child.lower()
                    if db_template_exists(value, conn=db) and value not in found:
                        found.append(value)
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(graph.source.to_dict())
    return found


def _transform_event_viewers(context, card_uid, owner_id, old_zone,
                             old_type, template_guid):
    """Snapshot C# ``CanPlayerSeeCard`` recipients before the mutation."""
    from domain.game import _card_update_visible_to_viewer
    from rules_port.runtime_helpers import (
        card_collection_for_location, owner_uid, raw_uid)

    game = context.game
    controller = owner_uid(owner_id, context.player_uid, context.ai_uid,
                           context.bstate)
    collection = card_collection_for_location(old_zone)
    try:
        transformed_identity = game_engine.ResourceId.from_str(template_guid)
    except (TypeError, ValueError):
        transformed_identity = game_engine.ResourceId.invalid()
    projection = SimpleNamespace(
        card_type=old_type, collection=collection, controller=controller,
        player_id=controller,
        card_id=transformed_identity)
    visibility_by_uid = getattr(game, "_visibility_by_uid", {}) or {}
    participants = [getattr(game, "player_uid", None),
                    getattr(game, "ai_uid", None)]

    # Card.CanPlayerSeeCard also permits a viewer with the appropriate
    # authored top-of-deck flag to see only the first deck card.
    is_top_deck_card = False
    participant_owners = []
    if (context.bstate.get("pvp") and context.bstate.get("pids")):
        participant_owners = [int(owner) for owner in
                              context.bstate.get("pids", ())]
    else:
        profile = getattr(context.handler, "user_profile", None) or {}
        participant_owners = [int(profile.get("id", 0) or 0), 0]
    if str(old_zone or "").lower() == "deck":
        try:
            from pvp_db import db_deck_top_card_details
            top = db_deck_top_card_details(
                context.session.session_id, int(owner_id), conn=context.db)
            is_top_deck_card = bool(top and int(top[1]) == int(card_uid))
        except (TypeError, ValueError, AttributeError):
            is_top_deck_card = False

    visible = set()
    for viewer in participants:
        if viewer is None:
            continue
        try:
            viewer_id = raw_uid(viewer)
        except (TypeError, ValueError):
            continue
        flags = visibility_by_uid.get(viewer_id, {}) or {}
        deck_reveal = False
        if is_top_deck_card:
            viewer_owner = next((
                participant_owner for participant_owner in participant_owners
                if raw_uid(owner_uid(
                    participant_owner, context.player_uid, context.ai_uid,
                    context.bstate)) == viewer_id), None)
            if viewer_owner is not None:
                try:
                    from .static_rules import player_int_attributes
                    attrs = player_int_attributes(
                        context.db, context.session.session_id,
                        context.bstate, int(viewer_owner))
                    permission = ("CanSeeTopOfDeck" if
                                  int(viewer_owner) == int(owner_id) else
                                  "CanSeeOpponentsTopOfDeck")
                    deck_reveal = int(attrs.get(permission, 0) or 0) > 0
                except (TypeError, ValueError, AttributeError):
                    deck_reveal = False
        if _card_update_visible_to_viewer(
                projection, viewer, flags, viewer_reveal=deck_reveal):
            visible.add(viewer_id)
    return visible


def _requires_transform_revert(old_type, new_type):
    """Mirror the category gates in Session.TransformCard/TransformIntoCopy."""
    old_type = int(old_type or 0)
    new_type = int(new_type or 0)
    action_or_resource = int(
        game_engine.ECardTypes.BasicAction |
        game_engine.ECardTypes.QuickAction |
        game_engine.ECardTypes.Resource)
    return bool(
        old_type & int(game_engine.ECardTypes.Troop) and
        not new_type & int(game_engine.ECardTypes.Troop) or
        bool(old_type & action_or_resource) !=
        bool(new_type & action_or_resource))


def transform_instance(context, card_uid, new_template_guid, *, keep_zone=True,
                       reverted=False, copy_from_uid=None, is_replica=False,
                       event_gems=None, trigger_already_dispatched=False):
    """Transform one live card while preserving its SessionCardId/state."""
    from pvp_db import (db_card_owner_location_position,
                        db_card_owner_zone_state, db_copy_template_payload,
                        db_transform_card_instance, db_card_attribute_value,
                        db_card_owner_id, db_reset_card_modifiers,
                        db_card_sacrifice_info, db_reset_card_uses,
                        db_card_source_info, db_card_original_and_template,
                        db_copy_transform_instance_state,
                        db_capture_original_gems, db_card_original_gems)
    row = db_card_owner_location_position(
        context.session.session_id, int(card_uid), conn=context.db)
    if not row:
        return "transform: target not found"
    owner_id, old_zone, old_position = int(row[0] or 0), row[1], int(row[2] or 0)
    state_row = db_card_owner_zone_state(
        context.session.session_id, int(card_uid), conn=context.db)
    old_state = int(state_row[2] or 0) if state_row else 0
    if not reverted and not trigger_already_dispatched:
        # Session.TransformCard enqueues CardTransformsEvent and immediately
        # handles it before it changes the card template. Listeners therefore
        # inspect the pre-transform instance and may queue their chain items
        # before the transform's own client events.
        from .triggers import dispatch_trigger
        dispatch_trigger(context, "CardTransformsEvent", int(card_uid), owner_id)
        # Card.SetActiveGems(setOriginal=True) keeps the first active-gem
        # value for Session.RevertCard. Persist it before changing this card.
        db_capture_original_gems(
            context.session.session_id, int(card_uid), conn=context.db)

    if copy_from_uid is not None:
        source_info = db_card_source_info(
            context.session.session_id, int(copy_from_uid), conn=context.db)
        if not source_info or not source_info[0]:
            return "transform: copy source not found"
        new_template_guid = str(source_info[0]).lower()
    payload = db_copy_template_payload(new_template_guid, conn=context.db)
    if not payload:
        return "transform: template not found"
    card_type = payload[0] or "Troop"
    old_card = db_card_sacrifice_info(
        context.session.session_id, int(card_uid), conn=context.db)
    old_type = game_engine.card_type_from_db(
        old_card[2] if old_card else "Troop")
    new_type = game_engine.card_type_from_db(
        db_card_sacrifice_info(
            context.session.session_id, int(copy_from_uid), conn=context.db)[2]
        if copy_from_uid is not None else card_type)
    if not reverted and _requires_transform_revert(old_type, new_type):
        original, _current = db_card_original_and_template(
            context.session.session_id, int(card_uid), conn=context.db) or (None, None)
        if original:
            # C# calls RevertCard after CardTransformsEvent and before the
            # class-44 transform. Reuse the same path so class 45 and its
            # reset state precede class 44 in the outgoing packet.
            transform_instance(
                context, int(card_uid), str(original).lower(), keep_zone=True,
                reverted=True, trigger_already_dispatched=True)
            row = db_card_owner_location_position(
                context.session.session_id, int(card_uid), conn=context.db)
            if row:
                owner_id, old_zone, old_position = (
                    int(row[0] or 0), row[1], int(row[2] or 0))
            state_row = db_card_owner_zone_state(
                context.session.session_id, int(card_uid), conn=context.db)
            old_state = int(state_row[2] or 0) if state_row else 0
            old_card = db_card_sacrifice_info(
                context.session.session_id, int(card_uid), conn=context.db)
            old_type = game_engine.card_type_from_db(
                old_card[2] if old_card else "Troop")
            if copy_from_uid is not None:
                source_info = db_card_source_info(
                    context.session.session_id, int(copy_from_uid),
                    conn=context.db)
                if not source_info or not source_info[0]:
                    return "transform: copy source not found"
                new_template_guid = str(source_info[0]).lower()
                payload = db_copy_template_payload(
                    new_template_guid, conn=context.db)
                if not payload:
                    return "transform: template not found"
                card_type = payload[0] or "Troop"
    # C# TransformCardBase dispatches class 44/45 before replacing the
    # template. Capture its recipients after CardTransformsEvent has run, but
    # before changing the card's type, collection projection, or identity.
    transform_event_viewers = _transform_event_viewers(
        context, int(card_uid), owner_id, old_zone, old_type,
        new_template_guid)
    if reverted:
        old_state &= ~int(game_engine.ECardStates.Damaged)
        original_gems = db_card_original_gems(
            context.session.session_id, int(card_uid), conn=context.db)
        db_reset_card_modifiers(
            context.session.session_id, int(card_uid), "{}", conn=context.db)
        context.db.execute(
            "UPDATE game_cards SET card_damage=0, cost_mod_json='[]', "
            "card_state=? WHERE session_id=? AND card_uid=?",
            (old_state, context.session.session_id, int(card_uid)))
    new_zone, new_position = (old_zone, old_position) if keep_zone else ("warzone", 0)
    db_transform_card_instance(
        context.session.session_id, int(card_uid), new_template_guid, card_type,
        payload[1] or "[]", int(payload[2] or 0), new_zone, new_position,
        old_state, conn=context.db,
        preserve_instance_state=copy_from_uid is not None,
        gems=original_gems if reverted else None)
    if copy_from_uid is None:
        db_reset_card_uses(
            context.session.session_id, int(card_uid),
            preserve_turn_and_cooldowns=not reverted, conn=context.db)
    else:
        db_copy_transform_instance_state(
            context.session.session_id, int(card_uid), int(copy_from_uid),
            is_replica=bool(is_replica), conn=context.db)
    context.db.commit()
    sync = getattr(context.handler, "_sync_instance_card_data", None)
    if callable(sync):
        sync(context.session, int(card_uid), new_template_guid)
    scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
    _tpl, rendered_type, _name, cost, attack, defense, rendered_gems = \
        context.handler._card_full_data(context.game, scid, new_template_guid)
    attrs = int(db_card_attribute_value(
        context.session.session_id, int(card_uid), "card_attributes",
        conn=context.db) or payload[2] or 0)
    from .runtime_helpers import card_collection_for_location, owner_uid
    recipient = owner_uid(owner_id, context.player_uid, context.ai_uid,
                          context.bstate)
    collection = card_collection_for_location(new_zone)
    if reverted:
        context.game.push_card_reverted(
            scid, new_template_guid,
            visible_to_viewer_uids=transform_event_viewers)
    else:
        context.game.push_card_transformed(
            scid, new_template_guid, is_replica=bool(is_replica),
            gems=event_gems,
            visible_to_viewer_uids=transform_event_viewers)
    context.game.push_card_updated(
        scid, recipient, collection, rendered_type, attack=attack,
        defense=defense, cost=cost, template_id=new_template_guid,
        gems=rendered_gems,
        state=old_state, attributes=attrs)
    # The C# cache derives class 50 only when collection/controller changes.
    # OnCardUpdated already returns the view home for same-collection updates;
    # the source client has no separate class-50 producer for reordering.
    if not reverted:
        # The C# event carries only TriggerEvent's source card/player fields.
        # Collection snapshots are not members of CardTransformedEvent.
        dispatch_trigger(context, "CardTransformedEvent", int(card_uid), owner_id)
    return int(card_uid)


def revert_instance(context, card_uid):
    """Restore one transformed card to its recorded original template."""
    from pvp_db import db_card_original_and_template
    row = db_card_original_and_template(
        context.session.session_id, int(card_uid), conn=context.db)
    original = str(row[0] or "").lower() if row else ""
    current = str(row[1] or "").lower() if row else ""
    if not original or original == current:
        return "revert transform: no original template"
    result = transform_instance(
        context, int(card_uid), original, keep_zone=True, reverted=True)
    if isinstance(result, str):
        return result
    return f"reverted {hex(int(card_uid))}"


def transform_card_at_random(context):
    """Apply the authored Records filter to the transform candidate pool."""
    target = context.resolved_target()
    if target is None:
        return "transform random: no target"
    filter_spec = context.template_value("m_Filter")
    if not isinstance(filter_spec, dict):
        return "transform random: no typed filter"

    from pvp_db import (db_transform_target_info,
                        db_transform_candidate_templates,
                        db_ability_raw_json)
    from rules_port.filters import records_filter_matches

    def shards_from_threshold(value):
        try:
            data = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        values = data if isinstance(data, list) else (
            data.get("list") or data.get("values") or [])
        flags = {0: 0, 1: 4, 2: 8, 3: 16, 4: 32, 5: 64}
        return [flags.get(int(item), 0) for item in values]

    target_row = db_transform_target_info(
        context.session.session_id, int(target), conn=context.db)
    if not target_row:
        return "transform random: target not found"

    def card_record(row, uid):
        data = {
            "card_uid": int(uid), "template_guid": row[0],
            "name": row[6] or "", "card_type": row[1] or "",
            "location": row[2] or "", "user_id": int(row[3] or 0),
            "state": int(row[4] or 0), "cost": int(row[7] or 0),
            "rarity": row[8] or "", "shards": shards_from_threshold(row[9]),
            "subtype": row[10] or "", "attributes": int(row[11] or 0) |
            int(row[12] or 0),
        }
        try:
            saved = json.loads(row[5] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            saved = {}
        data["counters"] = saved.get("counters") or {}
        data["counter_guids"] = saved.get("counter_guids") or {}
        data["tags"] = saved.get("tags") or {}
        return data

    source_card = card_record(target_row, target)
    # HasSourceCastingCostFilter compares against the card being transformed.
    try:
        raw = json.loads(db_ability_raw_json(
            context.bstate.get("resolving_ability", ""), conn=context.db) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
    source_card["ability_variables"] = {
        str(value.get("m_Name")): int(value.get("m_DefaultValue", 0) or 0)
        for value in raw.get("m_Variables", [])
        if isinstance(value, dict) and value.get("m_Name")
    }
    source_card["cost_delta"] = 0
    add_value = filter_spec.get("m_AddValue")
    if isinstance(add_value, dict):
        variable = (add_value.get("m_InputVariableName") or
                    add_value.get("m_VariableName"))
        if variable:
            source_card["cost_delta"] = source_card["ability_variables"].get(
                variable, 0)

    target_types = {part.strip() for part in source_card["card_type"].split("|")
                    if part.strip()}
    rows = db_transform_candidate_templates(conn=context.db)
    candidates = []
    cant_same = bool(context.template_value("m_CantBeSameCard", False))
    owner = source_card.get("user_id", 0)
    # Apply the source card's category only for the authored
    # Artifact|Constant|Troop union used by same-cost permanent transforms.
    # Other effects can intentionally transform between card types (e.g.
    # Morphology turns a troop into a BasicAction or QuickAction).
    type_union = None
    if str(filter_spec.get("_t", "")).rsplit(".", 1)[-1] == "AndCardFilter":
        for child in filter_spec.get("m_TargetFilters", []):
            if str(child.get("_t", "")).rsplit(".", 1)[-1] != "OrCardFilter":
                continue
            categories = set()
            for branch in child.get("m_TargetFilters", []):
                branch_type = str(branch.get("_t", "")).rsplit(".", 1)[-1]
                if branch_type == "IsArtifact":
                    categories.add("Artifact")
                elif branch_type == "IsTroop":
                    categories.add("Troop")
                elif (branch_type == "IsType" and
                      str(branch.get("m_CardType") or "") == "Constant"):
                    categories.add("Constant")
            if categories == {"Artifact", "Constant", "Troop"}:
                type_union = categories
                break
    for row in rows:
        candidate = {
            "card_uid": 0, "template_guid": row[0], "name": row[1] or "",
            "card_type": row[2] or "", "cost": int(row[3] or 0),
            "rarity": row[4] or "", "shards": shards_from_threshold(row[5]),
            "subtype": row[6] or "", "attributes": int(row[7] or 0),
            "location": "", "user_id": owner,
        }
        if cant_same and row[0].lower() == source_card["template_guid"].lower():
            continue
        candidate_types = {part.strip() for part in candidate["card_type"].split("|")
                           if part.strip()}
        if type_union and target_types and not target_types.intersection(
                candidate_types):
            continue
        if records_filter_matches(candidate, filter_spec,
                                  source=source_card, context=context):
            candidates.append(row[0])
    if not candidates:
        return "transform random: no candidates"
    new_template = random.choice(candidates)
    # TransformCard mutates the current card in place. In particular,
    # "put into hand, then transform" must leave the generated action in hand.
    transform_instance(context, int(target), new_template, keep_zone=True)
    return f"transformed {hex(int(target))} -> random {new_template[:8]}"
