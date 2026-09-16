"""RulesPort-owned deck construction effects."""

from __future__ import annotations

import json
import pathlib
import re
import game_engine


_DECK_TEMPLATES = None


def _deck_templates():
    global _DECK_TEMPLATES
    if _DECK_TEMPLATES is not None:
        return _DECK_TEMPLATES
    result = {}
    path = pathlib.Path(__file__).resolve().parents[1] / "Records" / "DeckTemplate.jsonl"
    if path.exists():
        try:
            for line in path.open(encoding="utf-8", errors="replace"):
                try:
                    value = json.loads(line)
                    if isinstance(value, str):
                        value = json.loads(re.sub(r",\s*([}\]])", r"\1", value))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(value, dict):
                    continue
                guid = ((value.get("m_Id") or {}).get("m_Guid") or "")
                if guid:
                    result[str(guid).lower()] = value
        except OSError:
            pass
    _DECK_TEMPLATES = result
    return result


def load_player_deck(context):
    """Instantiate the typed DeckTemplate, excluding champion entries."""
    from pvp_db import (db_copy_template_payload, db_deck_next_position,
                        db_insert_generated_card, db_next_game_card_row_id)
    from .runtime_helpers import next_game_card_uid

    guid = context.template_value("m_DeckTemplateId", "")
    if not guid:
        try:
            guid = json.loads(context.param or "{}").get("deck_template_guid", "")
        except (TypeError, ValueError, json.JSONDecodeError):
            guid = ""
    deck = _deck_templates().get(str(guid).lower())
    if not deck:
        return "load player deck: template not found"
    owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    position = db_deck_next_position(context.session.session_id, owner, conn=context.db)
    created = 0
    for entry in deck.get("m_DeckResources") or ():
        if not isinstance(entry, dict):
            continue
        card_guid = str(((entry.get("m_idTemplate") or {}).get("m_Guid") or "")).lower()
        if not card_guid:
            continue
        try:
            count = max(0, int(entry.get("m_Count") or 0))
        except (TypeError, ValueError):
            count = 0
        payload = db_copy_template_payload(card_guid, conn=context.db)
        if not payload or str(payload[0] or "").split("|")[0].lower() == "champion":
            continue
        for _ in range(count):
            uid = next_game_card_uid(context.db, context.session.session_id)
            db_insert_generated_card(
                context.session.session_id, owner, uid, card_guid, "deck",
                payload[0], payload[1], payload[2],
                db_next_game_card_row_id(context.session.session_id, conn=context.db),
                conn=context.db, position=position, card_state=0,
                owner_user_id=owner, original_template_guid=card_guid)
            position += 1
            created += 1
    context.db.commit()
    return f"loaded {created} card(s) into player deck"


def move_deck_card_to_hand(context, card_uid, owner_id):
    """Apply a validated deck-search result and its typed projection."""
    from pvp_db import db_card_zone_details, db_move_card_to_location
    from .runtime_helpers import owner_uid

    details = db_card_zone_details(
        context.session.session_id, int(card_uid), conn=context.db)
    if not details or str(details[3] or "").lower() != "deck":
        return "search deck: card not found"
    db_move_card_to_location(
        context.session.session_id, int(card_uid), "hand", position=100,
        conn=context.db)
    context.db.commit()
    scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
    recipient = owner_uid(owner_id, context.player_uid, context.ai_uid,
                          context.bstate)
    _tpl, card_type, _name, cost, attack, defense, gems = \
        context.handler._card_full_data(
            context.game, scid, details[0], details[1])
    context.game.push_card_moved(
        scid, recipient, game_engine.ECardCollections.Hand,
        game_engine.ECardLocations.Top, 1)
    context.game.push_card_drawn(scid, recipient, 1)
    context.game.push_card_updated(
        scid, recipient, game_engine.ECardCollections.Hand, card_type,
        template_id=details[0], cost=cost, attack=attack,
        defense=defense, gems=gems)
    return f"searched deck card {hex(int(card_uid))} to hand"
