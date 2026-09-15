"""Small runtime/projection helpers used by native RulesPort effects."""

from __future__ import annotations

import game_engine


def next_game_card_uid(db, session_id):
    from pvp_db import db_next_card_uid
    return db_next_card_uid(session_id, conn=db)


def card_collection_for_location(location):
    return {
        "deck": game_engine.ECardCollections.Deck,
        "hand": game_engine.ECardCollections.Hand,
        "discard": game_engine.ECardCollections.Discard,
        "void": game_engine.ECardCollections.Void,
        "warzone": game_engine.ECardCollections.Warzone,
        "castspells": game_engine.ECardCollections.CastSpells,
        "playedresources": game_engine.ECardCollections.PlayedResources,
        "underground": game_engine.ECardCollections.Underground,
        "choosing": game_engine.ECardCollections.Choosing,
    }.get(str(location or "").lower(), game_engine.ECardCollections.Warzone)


def owner_uid(owner_id, player_uid, ai_uid, battle_state=None):
    if (battle_state or {}).get("pvp"):
        return game_engine.UID.make(244, int(owner_id or 0))
    return player_uid if int(owner_id or 0) else ai_uid


def pvp_opponent_pid(battle_state, owner_id):
    """Return the other authenticated player in a PvP state."""
    pids = [int(pid) for pid in (battle_state or {}).get("pids", ())]
    owner_id = int(owner_id or 0)
    return next((pid for pid in pids if pid != owner_id), None)
