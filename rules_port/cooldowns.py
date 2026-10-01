"""Authored ability cooldown state shared by native and compatibility hosts."""

from __future__ import annotations

import threading

import game_engine


_COOLDOWN_LOCK = threading.RLock()
_CHAMPION_COOLDOWNS = "champion_ability_cooldowns"


def champion_cooldown_key(source_uid, ability_guid):
    try:
        source = int(getattr(source_uid, "uid64", source_uid))
    except (TypeError, ValueError):
        return None
    guid = str(ability_guid or "").lower()
    return f"{source}:{guid}" if guid else None


def champion_cooldown(state, source_uid, ability_guid):
    key = champion_cooldown_key(source_uid, ability_guid)
    if key is None:
        return 0
    with _COOLDOWN_LOCK:
        try:
            return max(0, int((state.get(_CHAMPION_COOLDOWNS) or {}).get(
                key, 0) or 0))
        except (TypeError, ValueError):
            return 0


def set_champion_cooldown(state, source_uid, ability_guid, turns):
    key = champion_cooldown_key(source_uid, ability_guid)
    if key is None:
        return 0
    remaining = max(0, int(turns or 0))
    with _COOLDOWN_LOCK:
        values = state.setdefault(_CHAMPION_COOLDOWNS, {})
        if remaining:
            values[key] = remaining
        else:
            values.pop(key, None)
        if not values:
            state.pop(_CHAMPION_COOLDOWNS, None)
    return remaining


def decrement_champion_cooldowns(state, owner_id, *, champion_map=None):
    """Tick only this active owner's synthetic champion powers."""
    owner = int(owner_id or 0)
    champion_map = champion_map or state.get("champ_map") or {}
    source = None
    for player_id, value in champion_map.items():
        try:
            if int(player_id) == owner:
                source = int(getattr(value, "uid64", value))
                break
        except (TypeError, ValueError):
            continue
    if source is None:
        return {}
    prefix = f"{source}:"
    changed = {}
    with _COOLDOWN_LOCK:
        values = state.get(_CHAMPION_COOLDOWNS) or {}
        for key, value in list(values.items()):
            if not str(key).startswith(prefix):
                continue
            try:
                remaining = int(value or 0) - 1
            except (TypeError, ValueError):
                remaining = 0
            if remaining > 0:
                values[key] = remaining
            else:
                values.pop(key, None)
            changed[str(key).split(":", 1)[1]] = max(0, remaining)
        if not values:
            state.pop(_CHAMPION_COOLDOWNS, None)
    return changed


def decrement_and_project_ready_cooldowns(db, session, handler, game,
                                          owner_id, player_uid, ai_uid,
                                          battle_state):
    """Tick and emit the card/champion cooldown view at ReadyState entry."""
    from pvp_db import (db_card_source_info, db_card_state_value,
                        db_decrement_card_cooldowns_for_owner)
    from .runtime_helpers import card_collection_for_location, owner_uid

    owner = int(owner_id or 0)
    card_uids = db_decrement_card_cooldowns_for_owner(
        session.session_id, owner, conn=db)
    champions = decrement_champion_cooldowns(battle_state, owner)
    for uid in card_uids:
        row = db_card_source_info(session.session_id, uid, conn=db)
        if not row:
            continue
        template_guid, card_type, location, card_owner = row
        scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
        try:
            _tpl, card_type, _name, cost, attack, defense, gems = \
                handler._card_full_data(game, scid, template_guid)
        except Exception:
            continue
        game.push_card_updated(
            scid, owner_uid(card_owner, player_uid, ai_uid, battle_state),
            card_collection_for_location(location), card_type,
            template_id=template_guid,
            state=int(db_card_state_value(
                session.session_id, uid, conn=db) or 0),
            cost=cost, attack=attack, defense=defense, gems=gems)

    # Champion cards are synthetic. Their card definition is rebuilt from the
    # current checkpoint so CardUpdated carries the same count the activation
    # gate just decremented.
    for key in champions:
        try:
            source = int(str(key).split(":", 1)[0])
        except (TypeError, ValueError):
            continue
        scid = game_engine.SessionCardId(game_engine.UID(source))
        cdef = getattr(game, "card_defs", {}).get(scid)
        if cdef is None:
            continue
        prefix = f"{source}:"
        cooldowns = {}
        for cooldown_key, value in (
                battle_state.get(_CHAMPION_COOLDOWNS) or {}).items():
            if str(cooldown_key).startswith(prefix):
                try:
                    remaining = int(value or 0)
                except (TypeError, ValueError):
                    continue
                if remaining > 0:
                    cooldowns[str(cooldown_key).split(":", 1)[1]] = remaining
        cdef.cooldown_counts = {
            game_engine.ResourceId.from_str(guid): value
            for guid, value in cooldowns.items()
        }
        game.push_card_updated(
            scid, owner_uid(owner, player_uid, ai_uid, battle_state),
            game_engine.ECardCollections.None_,
            game_engine.ECardTypes.Champion,
            cooldown_counts=cdef.cooldown_counts)
    return card_uids, champions
