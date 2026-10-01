"""Small runtime/projection helpers used by native RulesPort effects."""

from __future__ import annotations

import threading

import game_engine


_CHAMPION_ABILITY_USES_LOCK = threading.RLock()


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


def game_health_projection_attr(game, battle_state, owner_id):
    """Return the player/AI health field for a PvP owner in this Game view."""
    state = battle_state or {}
    if not state.get("pvp"):
        return None
    try:
        owner = int(owner_id or 0)
    except (TypeError, ValueError):
        return None
    health_map = state.get("pvp_health_map") or {}
    mapped = health_map.get(owner, health_map.get(str(owner)))
    if mapped in ("player_health", "ai_health"):
        return mapped
    for attribute in ("player_uid", "ai_uid"):
        participant = getattr(game, attribute, None)
        try:
            packed = raw_uid(participant)
        except (TypeError, ValueError):
            continue
        if (packed & 0xFF) == 244 and (packed >> 8) == owner:
            return "player_health" if attribute == "player_uid" else "ai_health"
    return None


def set_game_champion_health(game, battle_state, owner_id, health_key, value):
    """Update both the checkpoint key and its client-facing Game health field."""
    setattr(game, str(health_key), int(value))
    projected = game_health_projection_attr(game, battle_state, owner_id)
    if projected:
        setattr(game, projected, int(value))


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
    if state.get("pvp") or isinstance(state.get("champ_map"), dict):
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


def champion_ability_use_key(source_uid, ability_guid):
    """Return a stable per-champion, per-ability usage key."""
    try:
        source = raw_uid(source_uid)
    except (TypeError, ValueError):
        return None
    guid = str(ability_guid or "").lower()
    return f"{source}:{guid}" if guid else None


def champion_ability_uses_this_turn(battle_state, key):
    """Return this turn's use count for a synthetic champion ability."""
    if not isinstance(battle_state, dict) or not key:
        return 0
    try:
        turn = int(battle_state.get("turn_number", 1) or 1)
    except (TypeError, ValueError):
        turn = 1
    with _CHAMPION_ABILITY_USES_LOCK:
        usages = battle_state.get("champion_ability_uses_per_turn")
        entry = usages.get(str(key)) if isinstance(usages, dict) else None
        if not isinstance(entry, dict) or entry.get("turn_number") != turn:
            return 0
        try:
            return int(entry.get("uses", 0) or 0)
        except (TypeError, ValueError):
            return 0


def record_champion_ability_use_this_turn(battle_state, key):
    """Record one synthetic champion ability use for the current turn."""
    if not isinstance(battle_state, dict) or not key:
        return 0
    try:
        turn = int(battle_state.get("turn_number", 1) or 1)
    except (TypeError, ValueError):
        turn = 1
    with _CHAMPION_ABILITY_USES_LOCK:
        usages = battle_state.get("champion_ability_uses_per_turn")
        if not isinstance(usages, dict):
            usages = {}
            battle_state["champion_ability_uses_per_turn"] = usages
        previous = usages.get(str(key))
        if isinstance(previous, dict) and previous.get("turn_number") == turn:
            try:
                count = int(previous.get("uses", 0) or 0)
            except (TypeError, ValueError):
                count = 0
        else:
            count = 0
        count += 1
        usages[str(key)] = {"turn_number": turn, "uses": count}
        return count


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
