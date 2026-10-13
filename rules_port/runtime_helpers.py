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
        # Encounter setup cards (scene mods with a start-of-game grant) live in
        # the hidden Mod collection.  Unity has no zone view for it (the card
        # view is hidden) and the wire projection nulls it; the Warzone
        # fallback below would put such a card on the battlefield on refresh.
        "mod": game_engine.ECardCollections.Mod,
    }.get(str(location or "").lower(), game_engine.ECardCollections.Warzone)


def participant_side(battle_state, owner_id):
    """Return the ``player``/``ai`` prefix that holds ``owner_id``'s values.

    Practice/PvE identifies the AI as owner 0. In PvP both participants are
    nonzero raw IDs, and an effect view maps its *resolving* participant to
    ``player`` (``pvp_view.to_effect_view``), so neither ``bool(owner)`` nor
    ``pids[0]`` is a safe mapping there. The view's ``pvp_health_map`` is the
    authoritative per-view mapping; the participant order is only a fallback
    for a raw checkpoint, which keeps per-participant values in raw keys.
    """
    state = battle_state if isinstance(battle_state, dict) else {}
    try:
        owner = int(owner_id or 0)
    except (TypeError, ValueError):
        owner = 0
    if not state.get("pvp"):
        return "player" if owner else "ai"
    health_map = state.get("pvp_health_map") or {}
    mapped = health_map.get(owner, health_map.get(str(owner)))
    if mapped in ("player_health", "ai_health"):
        return str(mapped).split("_", 1)[0]
    try:
        pids = [int(pid) for pid in (state.get("pids") or ())]
    except (TypeError, ValueError):
        pids = []
    if owner in pids[:2]:
        return "player" if owner == pids[0] else "ai"
    return None


def participant_value(battle_state, owner_id, raw_prefix, side_suffix,
                      default=0):
    """Read one participant's value from a PvP checkpoint, view, or PvE state.

    A raw PvP checkpoint stores ``<raw_prefix>_<pid>``; the side-oriented
    view and Practice/PvE store ``<side>_<side_suffix>``.
    """
    state = battle_state if isinstance(battle_state, dict) else {}
    if state.get("pvp"):
        try:
            raw_key = f"{raw_prefix}_{int(owner_id or 0)}"
        except (TypeError, ValueError):
            raw_key = ""
        if raw_key in state:
            return state.get(raw_key, default)
    side = participant_side(state, owner_id)
    if side is None:
        return default
    return state.get(f"{side}_{side_suffix}", default)


def is_active_participant(battle_state, owner_id):
    """Is ``owner_id`` the active (turn) player in this state or view?"""
    state = battle_state if isinstance(battle_state, dict) else {}
    if state.get("pvp") and state.get("turn_pid") is not None:
        try:
            return int(state.get("turn_pid")) == int(owner_id or 0)
        except (TypeError, ValueError):
            return False
    side = participant_side(state, owner_id)
    return side is not None and state.get("turn_player") == side


def participant_turn_counter_key(battle_state, owner_id, name):
    """State key for a per-participant per-turn counter (PvP keys by pid)."""
    state = battle_state if isinstance(battle_state, dict) else {}
    if state.get("pvp"):
        return f"{name}_{int(owner_id or 0)}"
    return f"{'player' if int(owner_id or 0) else 'ai'}_{name}"


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
