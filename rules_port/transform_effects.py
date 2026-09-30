"""RulesPort-owned transform effect operations."""

from __future__ import annotations

import json
import random
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


def transform_instance(context, card_uid, new_template_guid, *, keep_zone=False,
                       reverted=False):
    """Transform one live card while preserving its SessionCardId/state."""
    from pvp_db import (db_card_owner_location_position,
                        db_card_owner_zone_state, db_copy_template_payload,
                        db_transform_card_instance, db_card_attribute_value,
                        db_card_owner_id, db_reset_card_modifiers)
    row = db_card_owner_location_position(
        context.session.session_id, int(card_uid), conn=context.db)
    if not row:
        return "transform: target not found"
    owner_id, old_zone, old_position = int(row[0] or 0), row[1], int(row[2] or 0)
    state_row = db_card_owner_zone_state(
        context.session.session_id, int(card_uid), conn=context.db)
    old_state = int(state_row[2] or 0) if state_row else 0
    payload = db_copy_template_payload(new_template_guid, conn=context.db)
    if not payload:
        return "transform: template not found"
    card_type = payload[0] or "Troop"
    if reverted:
        old_state &= ~int(game_engine.ECardStates.Damaged)
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
        old_state, conn=context.db)
    context.db.commit()
    sync = getattr(context.handler, "_sync_instance_card_data", None)
    if callable(sync):
        sync(context.session, int(card_uid), new_template_guid)
    scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
    _tpl, rendered_type, _name, cost, attack, defense, gems = \
        context.handler._card_full_data(context.game, scid, new_template_guid)
    attrs = int(db_card_attribute_value(
        context.session.session_id, int(card_uid), "card_attributes",
        conn=context.db) or payload[2] or 0)
    from .runtime_helpers import card_collection_for_location, owner_uid
    recipient = owner_uid(owner_id, context.player_uid, context.ai_uid,
                          context.bstate)
    collection = card_collection_for_location(new_zone)
    if reverted:
        context.game.push_card_reverted(scid, new_template_guid)
    else:
        context.game.push_card_transformed(scid, new_template_guid, gems=gems)
    context.game.push_card_updated(
        scid, recipient, collection, rendered_type, attack=attack,
        defense=defense, cost=cost, template_id=new_template_guid, gems=gems,
        state=old_state, attributes=attrs)
    context.game.push_card_moved(
        scid, recipient, collection, game_engine.ECardLocations.Top,
        new_position)
    if not reverted:
        from .triggers import dispatch_trigger
        dispatch_trigger(context, "CardTransformedEvent", int(card_uid), owner_id,
                         data={"event_source_collection": old_zone,
                               "event_destination_collection": new_zone,
                               "event_previous_state": old_state})
        dispatch_trigger(context, "CardTransformsEvent", int(card_uid), owner_id,
                         data={"event_source_collection": old_zone,
                               "event_destination_collection": new_zone,
                               "event_previous_state": old_state})
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
