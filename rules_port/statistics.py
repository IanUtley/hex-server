"""RulesPort TAC statistic storage shared by ability evaluation and effects."""

from __future__ import annotations

import json

import sqlite3
from threading import RLock


_STATS_LOCK = RLock()


def cached_ability_variable(state, name, *, instance_id=None):
    if not isinstance(state, dict) or not name:
        return None
    instance_id = (state.get("resolving_ability_instance_id")
                   if instance_id is None else instance_id)
    if instance_id is None:
        return None
    with _STATS_LOCK:
        values = (state.get("ability_variable_cache") or {}).get(
            str(instance_id), {})
        return values.get(str(name))


def cache_ability_variable(state, name, value, *, instance_id=None):
    if not isinstance(state, dict) or not name:
        return value
    instance_id = (state.get("resolving_ability_instance_id")
                   if instance_id is None else instance_id)
    if instance_id is None:
        return value
    with _STATS_LOCK:
        values = state.setdefault("ability_variable_cache", {}).setdefault(
            str(instance_id), {})
        return values.setdefault(str(name), value)


def _number(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def add_ability_stat(state, name, amount, *, instance_id=None):
    """Apply ``AbilityInstance.Add(IntAttrs, amount)`` to a live instance."""
    if not isinstance(state, dict) or not name or not amount:
        return 0
    instance_id = (state.get("resolving_ability_instance_id")
                   if instance_id is None else instance_id)
    if instance_id is None:
        return 0
    with _STATS_LOCK:
        values = state.setdefault("ability_runtime_state", {}).setdefault(
            str(instance_id), {})
        values[str(name)] = _number(values.get(str(name))) + _number(amount)
        return values[str(name)]


def ability_stat(state, name, *, instance_id=None, default: int | None = 0):
    if not isinstance(state, dict) or not name:
        return _number(default)
    instance_id = (state.get("resolving_ability_instance_id")
                   if instance_id is None else instance_id)
    if instance_id is None:
        return _number(default)
    with _STATS_LOCK:
        values = (state.get("ability_runtime_state") or {}).get(
            str(instance_id), {})
        return _number(values.get(str(name), default))


def _tac_scope(state, collection, uid, scope, *, create=False):
    key = "tac_statistics"
    root = state.get(key)
    if not isinstance(root, dict):
        if not create:
            return None
        root = state.setdefault(key, {})
    values = root.get(collection)
    if not isinstance(values, dict):
        if not create:
            return None
        values = root.setdefault(collection, {})
    entry = values.get(str(int(uid)))
    if not isinstance(entry, dict):
        if not create:
            return None
        entry = values.setdefault(str(int(uid)), {})
    stats = entry.get(scope)
    if not isinstance(stats, dict):
        if not create:
            return None
        stats = entry.setdefault(scope, {})
    return stats


def tac_stat(state, collection, uid, scope, name, *, default: int | None = 0):
    if not isinstance(state, dict) or uid is None:
        return None if default is None else _number(default)
    with _STATS_LOCK:
        values = _tac_scope(state, collection, uid, scope)
        if values is None or name not in values:
            return None if default is None else _number(default)
        return _number(values[name])


def tac_list(state, collection, uid, scope, name):
    """Read a TACList stored alongside the same card TAC statistics."""
    if not isinstance(state, dict) or uid is None or not name:
        return ()
    with _STATS_LOCK:
        values = _tac_scope(state, collection, uid, scope)
        if values is None:
            return ()
        result = values.get(str(name), ())
        return tuple(result) if isinstance(result, (list, tuple)) else ()


def clear_tac_list(state, collection, uid, scope, name):
    """Remove one TACList under the same lock used by its readers/writers."""
    if not isinstance(state, dict) or uid is None or not name:
        return False
    with _STATS_LOCK:
        values = _tac_scope(state, collection, uid, scope)
        if values is None or str(name) not in values:
            return False
        del values[str(name)]
        return True


def add_tac_stat(state, collection, uid, scope, name, amount):
    if not isinstance(state, dict) or uid is None or not name or not amount:
        return 0
    with _STATS_LOCK:
        values = _tac_scope(state, collection, uid, scope, create=True)
        assert values is not None
        values[str(name)] = _number(values.get(str(name))) + _number(amount)
        return values[str(name)]


def set_tac_max(state, collection, uid, scope, name, value):
    if not isinstance(state, dict) or uid is None or not name:
        return 0
    with _STATS_LOCK:
        values = _tac_scope(state, collection, uid, scope, create=True)
        assert values is not None
        values[str(name)] = max(_number(values.get(str(name))),
                                _number(value))
        return values[str(name)]


def set_tac_stat(state, collection, uid, scope, name, value):
    """Set one TAC value exactly, as ``Card.Set(TACAttrs, IntAttrs, ...)``."""
    if not isinstance(state, dict) or uid is None or not name:
        return 0
    with _STATS_LOCK:
        values = _tac_scope(state, collection, uid, scope, create=True)
        assert values is not None
        values[str(name)] = _number(value)
        return values[str(name)]


def card_escalation_count(db, session_id, state, card_uid):
    """Read ``Card.EscalationCount``; new card instances start at one."""
    if card_uid is None:
        return 1
    uid = _number(card_uid)
    if uid <= 0:
        return 1
    with _STATS_LOCK:
        if isinstance(state, dict):
            values = state.get("escalation_counts_by_card") or {}
            value = values.get(uid, values.get(str(uid)))
            if value is not None:
                return max(1, _number(value))
        if db is not None and session_id is not None:
            try:
                from pvp_db import db_card_mutation_field
                raw = db_card_mutation_field(
                    session_id, uid, "permanent_buffs", conn=db)
                buffs = json.loads(raw or "{}") if raw else {}
                if isinstance(buffs, dict) and "escalation_count" in buffs:
                    value = max(1, _number(buffs.get("escalation_count")))
                    if isinstance(state, dict):
                        state.setdefault("escalation_counts_by_card", {})[
                            str(uid)] = value
                    return value
            except (TypeError, ValueError, json.JSONDecodeError,
                    sqlite3.Error):
                pass
    return 1


def increment_card_escalation(db, session_id, state, card_uid):
    """Apply ``Session.Escalate`` to one card and persist its count."""
    if card_uid is None:
        return 1
    uid = _number(card_uid)
    if uid <= 0:
        return 1
    with _STATS_LOCK:
        value = card_escalation_count(db, session_id, state, uid) + 1
        if isinstance(state, dict):
            state.setdefault("escalation_counts_by_card", {})[
                str(uid)] = value
        if db is not None and session_id is not None:
            try:
                from pvp_db import (db_card_mutation_field,
                                    db_set_card_mutation_field)
                raw = db_card_mutation_field(
                    session_id, uid, "permanent_buffs", conn=db)
                buffs = json.loads(raw or "{}") if raw else {}
                if not isinstance(buffs, dict):
                    buffs = {}
                buffs["escalation_count"] = value
                db_set_card_mutation_field(
                    session_id, uid, "permanent_buffs",
                    json.dumps(buffs), conn=db)
                db.commit()
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return value


def add_card_stat(state, card_uid, owner_id, name, amount):
    """Port ``Card.AddToStat`` including its controller's player statistics."""
    add_tac_stat(state, "cards", card_uid, "CardStatsThisTurn", name, amount)
    add_tac_stat(state, "cards", card_uid, "CardGameStats", name, amount)
    if owner_id is not None:
        owner = _number(owner_id)
        champions = state.get("champ_map") or {}
        champion_uid = champions.get(owner, champions.get(str(owner)))
        if champion_uid is not None:
            # PlayerStatsThisTurn and PlayerGameStats are TAC scopes on the
            # champion Card, not a separate Player object in C#.
            add_tac_stat(state, "cards", champion_uid,
                         "PlayerStatsThisTurn", name, amount)
            add_tac_stat(state, "cards", champion_uid,
                         "PlayerGameStats", name, amount)
        else:
            # Keep the raw-owner projection for partial/headless checkpoints
            # that have not materialized champion SessionCardIds yet.
            add_tac_stat(state, "players", owner,
                         "PlayerStatsThisTurn", name, amount)
            add_tac_stat(state, "players", owner,
                         "PlayerGameStats", name, amount)


def add_champion_card_stat(state, owner_id, name, amount):
    """Port direct champion-card TAC writes (which are not AddToStat)."""
    champ_map = (state.get("champ_map") or {}) if isinstance(state, dict) else {}
    uid = champ_map.get(owner_id, champ_map.get(str(owner_id)))
    if uid is None:
        return
    add_tac_stat(state, "cards", uid, "CardStatsThisTurn", name, amount)
    add_tac_stat(state, "cards", uid, "CardGameStats", name, amount)


def record_charge_gained(state, owner_id, amount):
    if _number(amount) > 0:
        add_champion_card_stat(state, owner_id, "ChargePointsGained", amount)


def record_ability_card_list(state, name, card_uid):
    """Append one affected card to the current AbilityInstance TAC list."""
    if not isinstance(state, dict) or card_uid is None:
        return
    guid = str(state.get("resolving_ability") or "").lower()
    if not guid:
        return
    uid = _number(card_uid)
    if not uid:
        return
    with _STATS_LOCK:
        flat = state.setdefault("ability_lists", {})
        values = flat.setdefault(str(name), [])
        if uid not in values:
            values.append(uid)
