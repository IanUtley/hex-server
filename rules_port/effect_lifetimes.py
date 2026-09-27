"""Runtime lifetime handling for Records-mapped ability effects.

The client binds duration to an ``AbilityEffectInstance`` mapping.  The
checkpoint stores only the effects whose behavior must outlive the resolver;
this module keeps those entries JSON-safe and removes exactly the grant that
was added when its authored boundary is reached.
"""

from __future__ import annotations

import json
import threading
from typing import Any, cast


_STATE_KEY = "temporary_ability_grants"
_ZONE_DURATIONS = frozenset({
    "WhileCardInPlay", "WhileTargetInPlay", "WhileCardTapped",
    "UntilItLeavesYourHand", "WhileCardOnTopOfDeck",
})
_LIFETIME_LOCK = threading.RLock()


def mapping_duration(context) -> str:
    """Return the duration on the active AbilityEffectTargetMapping."""
    ability = getattr(context, "ability", None)
    metadata = getattr(ability, "metadata", ability)
    for effect in getattr(metadata, "effects", ()) or ():
        guid = str(getattr(effect, "guid", "") or "").lower()
        if guid == str(getattr(context, "effect_guid", "") or "").lower():
            return str(getattr(effect, "duration", "Instant") or "Instant")
    return "Instant"


def record_grant(state, *, source_uid, target_uid, ability_guid,
                 duration, owner_id, target_owner_id=None,
                 expiration_owner_id=None):
    """Remember one ability instance added by a duration-bound mapping."""
    duration = str(duration or "Instant")
    if duration in ("Instant", "Permanent", "EndOfGame", "UntilEndOfDungeon"):
        return
    entry = {
        "source_uid": int(source_uid) if source_uid is not None else None,
        "target_uid": int(target_uid),
        "ability_guid": str(ability_guid).lower(),
        "duration": duration,
        "owner_id": int(owner_id or 0),
        "target_owner_id": (int(target_owner_id)
                            if target_owner_id is not None else None),
        "expiration_owner_id": (int(expiration_owner_id)
                                if expiration_owner_id is not None else None),
        "turn_number": int(state.get("turn_number", 1) or 1),
    }
    with _LIFETIME_LOCK:
        state.setdefault(_STATE_KEY, []).append(entry)


def champion_grants(state, champion_uid):
    """Return duration-bound ability GUIDs currently granted to a champion."""
    try:
        uid = int(champion_uid)
    except (TypeError, ValueError):
        return ()
    with _LIFETIME_LOCK:
        return tuple(str(item.get("ability_guid") or "").lower()
                     for item in state.get(_STATE_KEY, ())
                     if isinstance(item, dict) and
                     item.get("target_uid") == uid and
                     item.get("ability_guid"))


def _opponent_owner(state, owner_id, fallback):
    owner_id = int(owner_id or 0)
    if state.get("pvp"):
        return next((int(pid) for pid in state.get("pids", ())
                     if int(pid) != owner_id), int(fallback or 0))
    profile_id = int(fallback or 0)
    return 0 if owner_id == profile_id else profile_id


def _card_location(db, session_id, uid, state=None, handler=None):
    if uid is None:
        return None
    for champion in ((state or {}).get("champ_map") or {}).values():
        try:
            if int(champion) == int(uid):
                return "champions"
        except (TypeError, ValueError):
            continue
    for attr in ("_player_champ_scid", "_ai_champ_scid"):
        champion = getattr(handler, attr, None)
        try:
            if int(getattr(cast(Any, champion), "uid").uid64) == int(uid):
                return "champions"
        except (AttributeError, TypeError, ValueError):
            continue
    from pvp_db import db_card_owner_location_position
    row = db_card_owner_location_position(session_id, int(uid), conn=db)
    if not row:
        return None
    return str(row[1] or "").lower()


def _expired(entry, db, session_id, state, *, boundary=None,
             boundary_owner=None, damaged_uid=None, event_type=None,
             event_source_uid=None, handler=None):
    duration = str(entry.get("duration") or "")
    source = entry.get("source_uid")
    target = entry.get("target_uid")
    if damaged_uid is not None and duration == "UntilDamaged":
        return target is not None and int(target) == int(damaged_uid)
    if (event_type in {"AttackDeclaredEvent", "CardAttackedEvent",
                       "CardAttackedOrBlockedEvent", "BlockDeclaredEvent",
                       "CardBlockedEvent"} and
            duration == "UntilAttacksOrBlocks"):
        return (target is not None and event_source_uid is not None and
                int(target) == int(event_source_uid))
    if boundary == "end_turn":
        return ((duration == "EndOfTurn")
                or (duration == "EndOfNextTurn" and
                    int(state.get("turn_number", 1) or 1) >
                    int(entry.get("turn_number", 1) or 1)))
    if boundary in ("start_turn", "prep"):
        owner = int(entry.get("owner_id", 0) or 0)
        expiration_owner = entry.get("expiration_owner_id")
        target_owner = entry.get("target_owner_id")
        if duration == "BeginningOfOwnersTurn":
            expected = expiration_owner if expiration_owner is not None else owner
            return int(expected) == int(boundary_owner or 0)
        if duration == "BeginningOfOpponentsTurn":
            expected = expiration_owner
            if expected is None:
                expected = _opponent_owner(state, owner, 0)
            return int(expected) == int(boundary_owner or 0)
        if (duration == "AfterCardsReadyOnPlayersTurn" and
                boundary == "prep" and
                int(state.get("turn_number", 1) or 1) !=
                int(entry.get("turn_number", 1) or 1)):
            expected = (target_owner if target_owner is not None else owner)
            return int(expected) == \
                int(boundary_owner or 0)
    if duration == "WhileCardInPlay" and source is not None:
        return _card_location(db, session_id, source, state, handler) != "warzone" and \
            _card_location(db, session_id, source, state, handler) != "champions"
    if duration == "WhileTargetInPlay" and target is not None:
        return _card_location(db, session_id, target, state, handler) != "warzone" and \
            _card_location(db, session_id, target, state, handler) != "champions"
    if duration == "UntilItLeavesYourHand" and source is not None:
        return _card_location(db, session_id, source) != "hand"
    if duration == "WhileCardOnTopOfDeck" and source is not None:
        from pvp_db import db_card_owner_location_position, db_deck_top_card_details
        row = db_card_owner_location_position(session_id, int(source), conn=db)
        if not row or str(row[1] or "").lower() != "deck":
            return True
        top = db_deck_top_card_details(session_id, int(row[0] or 0), conn=db)
        return not top or int(top[1]) != int(source)
    if duration == "WhileCardTapped" and source is not None:
        from pvp_db import db_card_state_value
        import game_engine
        return not (int(db_card_state_value(
            session_id, int(source), conn=db) or 0) &
            int(game_engine.ECardStates.Tapped))
    return False


def expire_grants(db, session_id, state, *, boundary=None,
                  boundary_owner=None, damaged_uid=None, event_type=None,
                  event_source_uid=None, handler=None, game=None,
                  player_uid=None, ai_uid=None):
    """Remove grants whose C# effect-instance duration has elapsed.

    Returns card target UIDs whose client ability lists changed.  Champion
    grants stay in the JSON checkpoint and are read by trigger discovery, so
    they survive reconnect without relying on handler-local state.
    """
    with _LIFETIME_LOCK:
        entries = list(state.get(_STATE_KEY) or ())
    if not entries:
        return []
    from pvp_db import (db_card_ability_list, db_card_source_info,
                        db_set_card_abilities)
    remaining = []
    changed = []
    champion_uids = {int(value) for value in
                     (state.get("champ_map") or {}).values() if value}
    if handler is not None:
        for attr in ("_player_champ_scid", "_ai_champ_scid"):
            champion = getattr(handler, attr, None)
            try:
                champion_uids.add(int(getattr(
                    cast(Any, champion), "uid").uid64))
            except (AttributeError, TypeError, ValueError):
                continue
    for entry in entries:
        if not isinstance(entry, dict) or not _expired(
                entry, db, session_id, state, boundary=boundary,
                boundary_owner=boundary_owner, damaged_uid=damaged_uid,
                event_type=event_type, event_source_uid=event_source_uid,
                handler=handler):
            remaining.append(entry)
            continue
        target = entry.get("target_uid")
        ability = str(entry.get("ability_guid") or "").lower()
        if target is None or not ability:
            continue
        target = int(target)
        if target in champion_uids:
            if handler is not None:
                dynamic = getattr(handler,
                                  "_champion_granted_ability_guids", {}) or {}
                values = dynamic.get(target)
                if isinstance(values, list) and ability in values:
                    values.remove(ability)
            if game is not None:
                try:
                    import game_engine
                    from .runtime_helpers import owner_uid
                    scid = game_engine.SessionCardId(game_engine.UID(target))
                    cdef = getattr(game, "card_defs", {}).get(scid)
                    if cdef is not None:
                        cdef.abilities = [value for value in
                                          (getattr(cdef, "abilities", []) or [])
                                          if str(getattr(value, "guid", value)).lower()
                                          != ability]
                    owner = next((int(pid) for pid, uid in
                                  (state.get("champ_map") or {}).items()
                                  if int(uid) == target), 0)
                    recipient = owner_uid(owner, player_uid, ai_uid, state)
                    game.push_player_updated(recipient, champ_id=scid)
                except (AttributeError, TypeError, ValueError):
                    pass
            continue
        if not db_card_source_info(session_id, target, conn=db):
            continue
        abilities = [str(value).lower() for value in db_card_ability_list(
            session_id, target, conn=db)]
        if ability in abilities:
            abilities.remove(ability)
            db_set_card_abilities(
                session_id, target, json.dumps(abilities), conn=db)
            changed.append(target)
    with _LIFETIME_LOCK:
        if remaining:
            state[_STATE_KEY] = remaining
        else:
            state.pop(_STATE_KEY, None)
    if changed:
        db.commit()
    return changed


def record_temporary_intattr(state, uid, attribute, previous, value,
                             *, champion=False, duration="UntilDamaged",
                             owner_id=0, target_owner_id=None,
                             source_uid=None, expiration_owner_id=None):
    """Record a reversible duration-bound IntAttr write."""
    item = {"uid": int(uid), "attribute": str(attribute),
            "previous": previous, "value": value,
            "duration": str(duration or "UntilDamaged"),
            "owner_id": int(owner_id or 0),
            "turn_number": int(state.get("turn_number", 1) or 1),
            "target_owner_id": (int(target_owner_id)
                                if target_owner_id is not None else None),
            "expiration_owner_id": (int(expiration_owner_id)
                                    if expiration_owner_id is not None else None),
            "source_uid": (int(source_uid)
                           if source_uid is not None else None)}
    key = ("temporary_champion_int_attrs" if champion else
           "temporary_card_int_attrs")
    with _LIFETIME_LOCK:
        state.setdefault(key, []).append(item)


def expire_temporary_intattrs(db, session_id, state, *, boundary=None,
                              boundary_owner=None, damaged_uid=None):
    """Restore authored IntAttrs after their effect-instance duration."""
    with _LIFETIME_LOCK:
        results = []
        for key in ("temporary_champion_int_attrs",
                    "temporary_card_int_attrs"):
            values = list(state.get(key) or ())
            matching = [item for item in values
                        if isinstance(item, dict) and
                        _intattr_expired(item, db, session_id, state,
                                         boundary=boundary,
                                         boundary_owner=boundary_owner,
                                         damaged_uid=damaged_uid)]
            if matching:
                state[key] = [item for item in values if item not in matching]
                results.extend((key, item) for item in reversed(matching))
                if not state[key]:
                    state.pop(key, None)
    for key, item in results:
        uid = int(item.get("uid", -1))
        attr = str(item.get("attribute") or "")
        if key == "temporary_champion_int_attrs":
            attrs = state.setdefault("champion_int_attrs", {}).setdefault(
                str(uid), {})
            current = attrs.get(attr)
            if current == item.get("value"):
                if item.get("previous") is None:
                    attrs.pop(attr, None)
                else:
                    attrs[attr] = item.get("previous")
            if not attrs:
                state.get("champion_int_attrs", {}).pop(str(uid), None)
            continue
        from pvp_db import db_card_mutation_field, db_set_card_mutation_field
        try:
            buffs = json.loads(db_card_mutation_field(
                session_id, uid, "temporary_buffs", conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        attrs = buffs.get("int_attrs") if isinstance(buffs, dict) else None
        if isinstance(attrs, dict) and attrs.get(attr) == item.get("value"):
            if item.get("previous") is None:
                attrs.pop(attr, None)
            else:
                attrs[attr] = item.get("previous")
            if not attrs:
                buffs.pop("int_attrs", None)
            db_set_card_mutation_field(
                session_id, uid, "temporary_buffs",
                json.dumps(buffs, separators=(",", ":"), sort_keys=True),
                conn=db)
            db.commit()
    return tuple(key for key, _item in results)


def _intattr_expired(item, db, session_id, state, *, boundary=None,
                     boundary_owner=None, damaged_uid=None):
    duration = str(item.get("duration") or "")
    owner = int(item.get("owner_id", 0) or 0)
    target_owner = item.get("target_owner_id")
    boundary_owner = int(boundary_owner or 0)
    if damaged_uid is not None:
        return (duration == "UntilDamaged" and
                int(item.get("uid", -1)) == int(damaged_uid))
    if boundary == "end_turn" and duration == "EndOfTurn":
        return True
    if (boundary == "end_turn" and duration == "EndOfNextTurn" and
            int(state.get("turn_number", 1) or 1) >
            int(item.get("turn_number", 1) or 1)):
        return True
    if boundary == "start_turn":
        if duration == "BeginningOfOwnersTurn":
            return int(item.get("expiration_owner_id", owner) or 0) == \
                boundary_owner
        if duration == "BeginningOfOpponentsTurn":
            expected = item.get("expiration_owner_id")
            if expected is None:
                expected = _opponent_owner(state, owner, boundary_owner)
            return int(expected or 0) == boundary_owner
    if (boundary == "prep" and
            duration == "AfterCardsReadyOnPlayersTurn" and
            int(state.get("turn_number", 1) or 1) !=
            int(item.get("turn_number", 1) or 1)):
        return int(target_owner if target_owner is not None else owner) == \
            boundary_owner
    if duration == "UntilItLeavesYourHand":
        source = item.get("source_uid")
        if source is None:
            source = item.get("uid")
        return _card_location(db, session_id, source, state) != "hand"
    return False


def expire_damage_bound_intattrs(db, session_id, state, uid):
    """Restore target IntAttrs removed by C#'s UntilDamaged teardown."""
    return expire_temporary_intattrs(
        db, session_id, state, damaged_uid=int(uid))


def expire_zone_bound_modifiers(db, session_id, state):
    """Expire hand-scoped modifiers after their target leaves the hand."""
    from pvp_db import (db_temporary_attribute_rows,
                        db_set_card_mutation_field)
    changed = []
    # The rows helper includes cards with temporary payloads and is owned by
    # pvp_db; no direct SQL is introduced in the RulesPort layer.
    for uid, _owner, _attributes, _raw in db_temporary_attribute_rows(
            session_id, conn=db):
        try:
            buffs = json.loads(_raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            continue
        modifiers = buffs.get("temporary_cost_modifiers")
        remaining = []
        for item in modifiers or ():
            if (not isinstance(item, dict) or
                    str(item.get("duration") or "") !=
                    "UntilItLeavesYourHand"):
                remaining.append(item)
                continue
            source_uid = item.get("source_uid")
            if source_uid is None:
                source_uid = item.get("target_uid", uid)
            if _card_location(db, session_id, source_uid, state) == "hand":
                remaining.append(item)
        if len(remaining) == len(modifiers or ()):
            continue
        if remaining:
            buffs["temporary_cost_modifiers"] = remaining
        else:
            buffs.pop("temporary_cost_modifiers", None)
        db_set_card_mutation_field(
            session_id, int(uid), "temporary_buffs",
            json.dumps(buffs, separators=(",", ":"), sort_keys=True),
            conn=db)
        changed.append(int(uid))
    if changed:
        db.commit()
    return changed
