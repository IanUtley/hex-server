"""RulesPort-owned card reveal selection and visibility projection."""

from __future__ import annotations

import json
import random


def _target_metadata(context):
    """Return the Records target metadata referenced by this effect."""
    ability = getattr(context, "ability", None)
    metadata = getattr(ability, "metadata", None)
    targets = getattr(metadata, "targets", ()) if metadata is not None else ()
    effects = getattr(metadata, "effects", ()) if metadata is not None else ()
    target_index = -1
    for effect in effects:
        guid = str(getattr(effect, "guid", "") or
                   getattr(effect, "effect_guid", "")).lower()
        if guid == str(context.effect_guid or "").lower():
            target_index = int(getattr(effect, "target_index", -1) or -1)
            break
    if 0 <= target_index < len(targets):
        target = targets[target_index]
        return (str(getattr(target, "guid", "") or "").lower(),
                str(getattr(target, "target_kind", "") or ""),
                bool(getattr(target, "is_random", False)))
    return "", "", False


def _find_zone(filter_node):
    if isinstance(filter_node, dict):
        kind = str(filter_node.get("_t", "")).rsplit(".", 1)[-1]
        if kind == "InZone" and filter_node.get("m_Collection"):
            return str(filter_node["m_Collection"]).rsplit(".", 1)[-1].lower()
        for value in filter_node.values():
            found = _find_zone(value)
            if found:
                return found
    elif isinstance(filter_node, list):
        for value in filter_node:
            found = _find_zone(value)
            if found:
                return found
    return "deck"


def _reveal_owner(context, owner, target_kind):
    if target_kind != "MatchSecondaryTargetTemplate":
        return owner
    stored = ((context.bstate.get("stored_targets") or {}).get(
        context.bstate.get("resolving_ability")) or [])
    if not stored:
        return owner
    from pvp_db import db_card_owner_id
    value = db_card_owner_id(
        context.session.session_id, int(stored[-1]), conn=context.db)
    if value is not None:
        return int(value)
    for player, champion in (context.bstate.get("champ_map") or {}).items():
        if int(champion) == int(stored[-1]):
            return int(player)
    return owner


def reveal_cards(context):
    """Select and project cards according to the Records target template."""
    from pvp_db import (db_target_template_filter,
                        db_target_template_resolution_info,
                        db_reveal_card_row, db_reveal_cards,
                        db_reveal_owned_card)
    from .targeting import legal_targets
    import game_engine
    from .runtime_helpers import card_collection_for_location, owner_uid

    template_id, target_kind, random_target = _target_metadata(context)
    # Synthetic/direct callers may still provide the compact adapter payload.
    count = 1
    if context.param:
        try:
            adapter = json.loads(context.param)
            count = max(0, int(adapter.get("count", 1) or 1))
            template_id = str(adapter.get("target_template_id") or template_id).lower()
            target_kind = str(adapter.get("target_kind") or target_kind)
            random_target = bool(adapter.get("random_target", random_target))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    filter_json = db_target_template_filter(template_id, conn=context.db) \
        if template_id else None
    try:
        filt = json.loads(filter_json) if filter_json else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        filt = {}
    resolution = (db_target_template_resolution_info(
        template_id, conn=context.db) if template_id else None)
    if resolution:
        target_kind = resolution[1] or target_kind
        random_target = bool(resolution[2])
        try:
            for node in (json.loads(resolution[0] or "{}").get(
                    "m_TargetFilters", []) or []):
                if str(node.get("_t", "")).rsplit(".", 1)[-1] == "TopNOfDeck":
                    count = max(0, int(node.get("m_Amount", 1) or 1))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    owner = _reveal_owner(context, owner, target_kind)
    zone = _find_zone(filt)
    reveal_collection = card_collection_for_location(zone)

    if target_kind == "AbilitySourceCardTargetTemplate":
        target = context.resolved_target()
        row = (db_reveal_owned_card(
            context.session.session_id, int(target), owner, conn=context.db)
               if target is not None else None)
        rows = [row] if row else []
        if row:
            reveal_collection = card_collection_for_location(row[5])
    else:
        candidates = None
        if template_id and zone in ("hand", "deck"):
            candidates = legal_targets(
                context.db, context.session.session_id, owner, template_id,
                context.bstate.get("resolving_source_uid"), both_players=False,
                champions=[], battle_state=context.bstate)
        if random_target and candidates is not None:
            selected = random.choice(candidates) if candidates else None
            rows = ([db_reveal_card_row(
                context.session.session_id, selected, owner, zone,
                conn=context.db)] if selected is not None else [])
            rows = [row for row in rows if row]
        else:
            if zone == "hand" and candidates is not None:
                if random_target and candidates:
                    candidates = [random.choice(candidates)]
                rows = db_reveal_cards(
                    context.session.session_id, owner, zone, count,
                    candidates, conn=context.db)
            else:
                rows = db_reveal_cards(
                    context.session.session_id, owner, zone, count, conn=context.db)
                if zone == "hand" and rows:
                    rows = [random.choice(rows)]

    uids = [int(row[0]) for row in rows]
    context.bstate["revealed_cards"] = uids
    if not uids:
        return "revealed 0"

    reveal_targets = str(context.template_value(
        "m_PlayerRevealTargets", "Everyone") or "Everyone")
    if owner == 0:
        reveal_targets = "Everyone"
    private = False
    if (context.bstate.get("pvp") and reveal_targets.lower() in
            ("self", "you", "controller")):
        sender = getattr(context.handler, "_push_private_revealed_cards", None)
        if callable(sender):
            private = bool(sender(
                context.session, context.bstate, owner, rows,
                context.player_uid, context.ai_uid))
    if not private:
        for row in rows:
            scid = game_engine.SessionCardId(game_engine.UID(int(row[0])))
            card_owner = owner_uid(row[3], context.player_uid,
                                   context.ai_uid, context.bstate)
            _tpl, card_type, _name, cost, attack, defense, gems = \
                context.handler._card_full_data(context.game, scid, row[2])
            context.game.push_card_updated(
                scid, card_owner, reveal_collection, card_type,
                state=int(row[4] or 0), template_id=row[2], cost=cost,
                attack=attack, defense=defense, gems=gems, nulling=False)
        event = game_engine.CardsRevealedSessionEventArgs()
        event.player_id = owner_uid(owner, context.player_uid,
                                    context.ai_uid, context.bstate)
        event.session_card_ids = [game_engine.SessionCardId(game_engine.UID(uid))
                                  for uid in uids]
        event.collections = [reveal_collection] * len(uids)
        event.owning_players = [event.player_id] * len(uids)
        event.positions = [int(row[1] or 0) for row in rows]
        context.game._push(event)
    return f"revealed {len(uids)}"
