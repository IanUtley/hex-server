"""RulesPort-owned draw mutation and event sequencing."""

from __future__ import annotations

import game_engine


def _champion_uid(context, owner):
    if context.bstate.get("pvp"):
        champions = context.bstate.get("champ_map") or {}
        value = champions.get(owner, champions.get(str(owner)))
        return int(value) if value is not None else None
    champion = (getattr(context.handler, "_ai_champ_scid", None)
                if int(owner) == 0 else
                getattr(context.handler, "_player_champ_scid", None))
    return int(champion.uid.uid64) if champion is not None else None


def draw_cards(context, count, owner=None):
    """Draw cards through the native zone, replacement, and trigger path."""
    from pvp_db import db_deck_top_card_details, db_draw_card_to_hand
    from .runtime_helpers import owner_uid
    count = max(0, int(count or 0))
    if owner is None:
        owner = context.target_owner(default=None)
    if owner is None:
        owner = context.bstate.get("resolving_owner_id", 0)
    owner = int(owner or 0)
    drawn = 0
    for _ in range(count):
        row = db_deck_top_card_details(
            context.session.session_id, owner, conn=context.db)
        if not row:
            # Deck-out is a rules transition, not an incidental failure of
            # the storage adapter. Let the mode publish its wire-specific
            # GameEnded packet, but make the decision here so native draws do
            # not silently diverge from the client lifecycle.
            deck_out = getattr(context.handler, "_rules_port_deck_out", None)
            if not callable(deck_out):
                raise RuntimeError(
                    "RulesPort draw requires the deck-out projection")
            context.bstate["_rules_port_deck_out_owner"] = owner
            deck_out(context.game, context.session, owner,
                     context.player_uid, context.ai_uid, context.bstate)
            break
        card_uid = int(row[1])
        if context._emit_trigger(
                "CardWouldBeDrawnEvent", None, owner) or context._emit_trigger(
                "CardWouldEnterZoneEvent", card_uid, owner,
                event_source_collection="deck",
                event_destination_collection="hand"):
            continue
        db_draw_card_to_hand(
            context.session.session_id, row[0], owner, conn=context.db)
        context.db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(card_uid))
        recipient = owner_uid(owner, context.player_uid, context.ai_uid,
                              context.bstate)
        _tpl, card_type, _name, cost, attack, defense, gems = \
            context.handler._card_full_data(
                context.game, scid, row[3], row[2])
        context.game.push_card_moved(
            scid, recipient, game_engine.ECardCollections.Hand,
            game_engine.ECardLocations.Top, 1)
        context.game.push_card_drawn(scid, recipient, 1)
        context.game.push_card_updated(
            scid, recipient, game_engine.ECardCollections.Hand, card_type,
            template_id=_tpl, cost=cost, attack=attack, defense=defense,
            gems=gems, nulling=owner == 0)
        context._emit_trigger(
            "CardEnteredZoneEvent", card_uid, owner,
            event_source_collection="deck",
            event_destination_collection="hand")
        context._emit_trigger(
            "CardDrawnEvent", _champion_uid(context, owner), owner,
            target_card_id=card_uid)
        drawn += 1
    return f"draw {drawn} for owner {owner}"
