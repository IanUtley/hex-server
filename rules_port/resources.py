"""RulesPort resource state transitions and Records-driven resource abilities.

The transition functions own gameplay state. Database writes and Unity events
are performed by the caller's runtime projection boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import MutableMapping

import game_engine
from collections import Counter
import json


@dataclass(frozen=True)
class ResourceChange:
    side: str
    property: str
    amount: int
    old_value: int
    new_value: int
    color: int = 0


@dataclass(frozen=True)
class ResourcePlay:
    """Authoritative state transition for playing one resource card."""

    current: ResourceChange
    total: ResourceChange
    charge: ResourceChange
    threshold: ResourceChange | None


def printed_resource_choice_ability(ability_guids):
    """Find an authored resource ability that creates a choice picker."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    for value in ability_guids or ():
        guid = str(value or "").lower()
        graph = ability_graph(DEFAULT_RECORD_STORE, guid) if guid else None
        if graph is None:
            continue
        has_choice_tokens = False
        has_activation = False
        for effect in graph.effects:
            if effect.concrete_type == "ActivateAbilityEffectTemplate":
                has_activation = True
            elif effect.concrete_type == "SummonTokenTroopAbilityEffectTemplate":
                template = effect.template
                collection = (template.field("m_CardCollection", "")
                              if template is not None else "")
                if str(collection).rsplit(".", 1)[-1].lower() == "choosing":
                    has_choice_tokens = True
        if has_choice_tokens and has_activation:
            return guid
    return None


def _resource_ability_guids(db, session_id, card_uid):
    from pvp_db import db_card_ability_state
    row = db_card_ability_state(session_id, int(card_uid), conn=db)
    if not row:
        return (), ()
    def decode(value):
        try:
            return tuple(str(g).lower() for g in json.loads(value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
    return decode(row[0]), decode(row[1])


def resolve_granted_resource_abilities(game, session, db, handler, pl_t, ai_t,
                                       bstate, card_uid, owner_id, *,
                                       resolver=None):
    """Resolve non-triggered abilities granted to a resource instance."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    current, printed = _resource_ability_guids(
        db, session.session_id, card_uid)
    granted = Counter(current)
    granted.subtract(Counter(printed))
    dynamic = []
    for guid in current:
        if granted[guid] > 0:
            dynamic.append(guid)
            granted[guid] -= 1
    if not dynamic:
        return []
    if resolver is None:
        from .resolution import resolve_port_ability
        resolver = resolve_port_ability
    logs = []
    for guid in dynamic:
        graph = ability_graph(DEFAULT_RECORD_STORE, guid)
        if graph is None or graph.manual or graph.trigger_event_type:
            continue
        result = resolver(
            handler, game, session, db, pl_t, ai_t, bstate,
            guid, int(card_uid), owner_id, target_map={})
        logs.append(f"{guid[:8]}: {result}")
    return logs


def resolve_printed_resource_abilities(game, session, db, handler, pl_t, ai_t,
                                       bstate, card_uid, owner_id,
                                       skip_guids=()):
    """Resolve non-triggered printed abilities not covered by base play."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from pvp_db import db_ability_effect_type_params
    current, _printed = _resource_ability_guids(
        db, session.session_id, card_uid)
    skip = {str(g).lower() for g in (skip_guids or ())}
    from .resolution import resolve_port_ability
    logs = []
    for guid in current:
        if not guid or guid in skip:
            continue
        graph = ability_graph(DEFAULT_RECORD_STORE, guid)
        if graph is None or graph.manual or graph.trigger_event_type:
            continue
        effects = db_ability_effect_type_params(guid, conn=db)
        if effects and all(
                effect_type == "CardModifierAbilityEffectTemplate" and
                _resource_grant_property(param)
                for effect_type, param in effects):
            continue
        result = resolve_port_ability(
            handler, game, session, db, pl_t, ai_t, bstate,
            guid, int(card_uid), owner_id, target_map={})
        logs.append(f"{guid[:8]}: {result}")
    return logs


def _resource_grant_property(param):
    try:
        value = json.loads(param or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return str(value.get("property") or "").lower() in {
        "threshold", "chargepoints",
    }


def apply_resource_change(state: MutableMapping, side: str, property: str,
                          amount: int, *, color: int = 0) -> ResourceChange:
    """Apply one typed resource change and return its client-facing delta."""
    side = "player" if str(side).lower() == "player" else "ai"
    property = str(property).lower()
    amount = int(amount)
    if property == "threshold":
        thresholds = state.setdefault(f"{side}_threshold", {})
        old = thresholds.get(color)
        if old is None:
            old = thresholds.get(str(color), 0)
            thresholds.pop(str(color), None)
        old = int(old or 0)
        new = max(0, old + amount)
        thresholds[color] = new
        return ResourceChange(side, property, amount, old, new, int(color))
    if property not in {"currentresource", "totalresource", "chargepoints"}:
        raise ValueError(f"unsupported resource property: {property}")
    key = {
        "currentresource": f"{side}_resources",
        "totalresource": f"{side}_total_resources",
        "chargepoints": f"{side}_charges",
    }[property]
    old = int(state.get(key, 0) or 0)
    new = max(0, old + amount)
    state[key] = new
    return ResourceChange(side, property, amount, old, new)


def play_resource(state: MutableMapping, side: str, current_amount: int,
                  total_amount: int, *, threshold_color: int | None = None,
                  charge_amount: int = 1) -> ResourcePlay:
    """Apply the complete authored resource-play transition.

    Card movement, triggered abilities, and client events are projections and
    stay outside this module.  The pool, threshold, charge, and once-per-turn
    gameplay state are one atomic RulesPort transition.
    """
    side = "player" if str(side).lower() == "player" else "ai"
    if state.get(f"{side}_resource_played_this_turn"):
        raise ValueError("resource already played this turn")
    current = apply_resource_change(
        state, side, "currentresource", int(current_amount))
    total = apply_resource_change(
        state, side, "totalresource", int(total_amount))
    threshold = None
    if threshold_color is not None:
        threshold = apply_resource_change(
            state, side, "threshold", 1, color=int(threshold_color))
    charge = apply_resource_change(
        state, side, "chargepoints", int(charge_amount))
    state[f"{side}_resource_played_this_turn"] = True
    return ResourcePlay(current, total, charge, threshold)


def play_resource_for_player(state: MutableMapping, player_id,
                             current_amount: int, total_amount: int,
                             *, threshold_color: int | None = None,
                             charge_amount: int = 1) -> ResourcePlay:
    """Apply resource play to a raw-player-id PvP checkpoint."""
    pid = int(player_id)
    played_key = f"res_played_{pid}"
    if state.get(played_key):
        raise ValueError("resource already played this turn")

    def change(property_name, amount, *, color=0):
        amount = int(amount)
        if property_name == "threshold":
            key = f"thresh_{pid}"
            thresholds = state.setdefault(key, {})
            old = thresholds.get(color)
            if old is None:
                old = thresholds.get(str(color), 0)
                thresholds.pop(str(color), None)
            old = int(old or 0)
            new = max(0, old + amount)
            thresholds[color] = new
            return ResourceChange(str(pid), property_name, amount, old, new,
                                  int(color))
        key = {"currentresource": f"res_{pid}",
               "totalresource": f"res_total_{pid}",
               "chargepoints": f"chg_{pid}"}.get(property_name)
        if key is None:
            raise ValueError(f"unsupported resource property: {property_name}")
        old = int(state.get(key, 0) or 0)
        new = max(0, old + amount)
        state[key] = new
        return ResourceChange(str(pid), property_name, amount, old, new)

    current = change("currentresource", current_amount)
    total = change("totalresource", total_amount)
    threshold = (change("threshold", 1, color=int(threshold_color))
                 if threshold_color is not None else None)
    charge = change("chargepoints", charge_amount)
    state[played_key] = 1
    return ResourcePlay(current, total, charge, threshold)


def pay_resource_for_player(state: MutableMapping, player_id,
                            amount: int) -> ResourceChange:
    """Pay a resource cost from a raw-player-id PvP checkpoint."""
    pid = int(player_id)
    amount = int(amount)
    key = f"res_{pid}"
    old = int(state.get(key, 0) or 0)
    if amount < 0 or amount > old:
        raise ValueError("insufficient resources")
    new = old - amount
    state[key] = new
    return ResourceChange(str(pid), "currentresource", -amount, old, new)


def _pay_player_counter(state: MutableMapping, player_id, amount: int,
                        prefix: str, property_name: str) -> ResourceChange:
    """Pay a non-resource player counter from a raw PvP checkpoint."""
    pid = int(player_id)
    amount = int(amount)
    key = f"{prefix}_{pid}"
    old = int(state.get(key, 0) or 0)
    if amount < 0 or amount > old:
        raise ValueError(f"insufficient {property_name}")
    new = old - amount
    state[key] = new
    return ResourceChange(str(pid), property_name, -amount, old, new)


def pay_charge_for_player(state: MutableMapping, player_id,
                          amount: int) -> ResourceChange:
    """Pay champion charge points from a raw-player-id PvP checkpoint."""
    return _pay_player_counter(state, player_id, amount, "chg",
                               "chargepoints")


def pay_spell_points_for_player(state: MutableMapping, player_id,
                                amount: int) -> ResourceChange:
    """Pay spell points from a raw-player-id PvP checkpoint."""
    return _pay_player_counter(state, player_id, amount, "sp",
                               "spellpoints")


def begin_turn_resources_for_player(state: MutableMapping,
                                    player_id) -> ResourceChange:
    """Refill and reopen resource play for a raw tournament player ID."""
    pid = int(player_id)
    current_key = f"res_{pid}"
    total_key = f"res_total_{pid}"
    old = int(state.get(current_key, 0) or 0)
    total = int(state.get(total_key, 0) or 0)
    state[current_key] = total
    state[f"res_played_{pid}"] = 0
    return ResourceChange(str(pid), "currentresource", total - old,
                          old, total)


def pay_resource(state: MutableMapping, side: str, amount: int) -> ResourceChange:
    """Pay a resource cost from the canonical player/AI checkpoint."""
    normalized = "player" if str(side).lower() == "player" else "ai"
    amount = int(amount)
    key = f"{normalized}_resources"
    old = int(state.get(key, 0) or 0)
    if amount < 0 or amount > old:
        raise ValueError("insufficient resources")
    return apply_resource_change(state, normalized, "currentresource", -amount)


def pay_counter(state: MutableMapping, side: str, property: str,
                amount: int) -> ResourceChange:
    """Pay a charge or spell-point cost from the canonical checkpoint."""
    normalized = "player" if str(side).lower() == "player" else "ai"
    property = str(property).lower()
    if property not in {"chargepoints", "spellpoints"}:
        raise ValueError(f"unsupported payment counter: {property}")
    key = (f"{normalized}_charges" if property == "chargepoints"
           else f"{normalized}_spell_points")
    old = int(state.get(key, 0) or 0)
    amount = int(amount)
    if amount < 0 or amount > old:
        raise ValueError(f"insufficient {property}")
    state[key] = old - amount
    return ResourceChange(normalized, property, -amount, old, old - amount)


def begin_turn_resources(state: MutableMapping, side: str) -> ResourceChange:
    """Refill one side's current pool and reopen its resource play."""
    normalized = "player" if str(side).lower() == "player" else "ai"
    state[f"{normalized}_resource_played_this_turn"] = False
    total = int(state.get(f"{normalized}_total_resources", 0) or 0)
    return apply_resource_change(
        state, normalized, "currentresource",
        total - int(state.get(f"{normalized}_resources", 0) or 0))


def project_resource_change(game, session, state: MutableMapping, player_uid,
                            ai_uid, side: str, property: str, amount: int,
                            *, color: int = 0) -> ResourceChange:
    """Apply a resource transition and publish its existing Game event.

    The Game object is a projection only.  The RulesPort state transition is
    applied first and persisted before the client can submit the next request.
    """
    change = apply_resource_change(
        state, side, property, amount, color=color)
    if property == "currentresource":
        from .persistence import save_state
        save_state(session, state)
        setattr(game, f"{change.side}_resources", change.new_value)
        event_type = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs
    elif property == "totalresource":
        event_type = game_engine.PlayerTotalResourcePoolChangedSessionEventArgs
    elif property == "chargepoints":
        setattr(game, f"{change.side}_charges", change.new_value)
        event_type = game_engine.ChampionChargePointsChangedSessionEventArgs
    elif property == "threshold":
        setattr(game, f"{change.side}_threshold",
                dict(state[f"{change.side}_threshold"]))
        event_type = game_engine.PlayerResourceThresholdChangedSessionEventArgs
    else:
        raise ValueError(f"unsupported resource property: {property}")
    event = event_type()
    event.player_id = player_uid if change.side == "player" else ai_uid
    event.operation = 1 if amount >= 0 else 2
    event.delta = int(amount)
    event.new_value = change.new_value
    if property == "threshold":
        event.color = int(color)
    game._push(event)
    return change
