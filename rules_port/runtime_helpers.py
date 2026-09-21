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


def raw_uid(value):
    """Return the raw identity of a UID/SessionCardId/int.

    ``game_engine.UID`` exposes ``uid64`` and deliberately has no
    ``__int__``; calling ``int()`` on a participant UID raises TypeError, so
    every raw/typed comparison goes through this helper.
    """
    return int(getattr(value, "uid64", value))


def champion_owner_id(handler, battle_state, champion_uid):
    """Return the controller id owning a champion SessionCardId, else None.

    Live champions are synthetic SessionCardIds with no ``game_cards`` row, so
    the controller comes from the PvP champion map or the PvE handler fields
    rather than from converting the participant UID.  Converting the UID
    raised TypeError and silently made every champion counter fall through to
    the ordinary-card persistence path.
    """
    state = battle_state or {}
    try:
        target = raw_uid(champion_uid)
    except (TypeError, ValueError):
        return None
    if state.get("pvp"):
        for owner, champion in (state.get("champ_map") or {}).items():
            try:
                if raw_uid(champion) == target:
                    return int(owner)
            except (TypeError, ValueError):
                continue
        return None
    profile = getattr(handler, "user_profile", None)
    profile_id = (int(profile.get("id", 0) or 0)
                  if isinstance(profile, dict) else 0)
    for attr, owner in (("_player_champ_scid", profile_id),
                        ("_ai_champ_scid", 0)):
        champion = getattr(handler, attr, None)
        if champion is None:
            continue
        try:
            if raw_uid(getattr(champion, "uid", champion)) == target:
                return int(owner)
        except (TypeError, ValueError):
            continue
    return None


def champion_uid_for_owner(handler, battle_state, owner_id):
    """Return the SessionCardId of the champion controlled by *owner_id*.

    Owner ``0`` is always the AI side and any other owner id is the human,
    matching :func:`owner_uid`.
    """
    state = battle_state or {}
    try:
        owner = int(owner_id if owner_id is not None else 0)
    except (TypeError, ValueError):
        return None
    if state.get("pvp"):
        champions = state.get("champ_map") or {}
        value = champions.get(owner, champions.get(str(owner)))
        if value is None:
            return None
        try:
            return raw_uid(value)
        except (TypeError, ValueError):
            return None
    champion = getattr(handler, "_ai_champ_scid" if owner == 0
                       else "_player_champ_scid", None)
    if champion is None:
        return None
    try:
        return raw_uid(getattr(champion, "uid", champion))
    except (TypeError, ValueError):
        return None


def champion_uids_by_owner(adapter, battle_state):
    """Return ``{owner_id: champion_uid}`` for a port runtime-facts adapter.

    Tournament PvP carries the champion map in the battle state; Practice and
    PvE sessions carry the synthetic champion SessionCardIds on the attach
    seam.  Champions are not ``game_cards`` rows, so this map is the only way
    a port rule can tell a champion source from an ordinary card.
    """
    mapping = {}
    for owner, champion in ((battle_state or {}).get("champ_map") or {}).items():
        try:
            mapping[int(owner)] = raw_uid(champion)
        except (TypeError, ValueError):
            continue
    if mapping:
        return mapping
    for owner, attr in ((getattr(adapter, "player_owner_id", None),
                         "player_champion_card_id"),
                        (getattr(adapter, "ai_owner_id", 0),
                         "ai_champion_card_id")):
        champion = getattr(adapter, attr, None)
        if owner is None or champion is None:
            continue
        try:
            mapping[int(owner)] = raw_uid(getattr(champion, "uid", champion))
        except (TypeError, ValueError):
            continue
    return mapping


def pvp_opponent_pid(battle_state, owner_id):
    """Return the other authenticated player in a PvP state."""
    pids = [int(pid) for pid in (battle_state or {}).get("pids", ())]
    owner_id = int(owner_id or 0)
    return next((pid for pid in pids if pid != owner_id), None)
