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


def resource_play_count(state, side: str, *, player_id=None) -> int:
    """Return this turn's resource plays, including older boolean checkpoints."""
    state = state if isinstance(state, dict) else {}
    if player_id is not None:
        try:
            return max(0, int(state.get(f"res_played_{int(player_id)}", 0) or 0))
        except (TypeError, ValueError):
            return 0
    side = "player" if str(side).lower() == "player" else "ai"
    key = f"{side}_resource_plays_this_turn"
    if key in state:
        try:
            return max(0, int(state.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0
    # Existing turn checkpoints stored only a boolean. Treat that as one
    # already-played resource so hot-loaded sessions retain their allowance.
    return int(bool(state.get(f"{side}_resource_played_this_turn")))


def resource_play_limit(db, session_id, battle_state, owner_id) -> int:
    """Compute the current allowance from the owner's champion-context IntAttrs."""
    from .static_rules import player_int_attributes
    attrs = player_int_attributes(
        db, session_id, battle_state, int(owner_id or 0))
    try:
        additional = int(attrs.get(
            "AdditionalResourcesPlayableOnYourTurn", 0) or 0)
    except (TypeError, ValueError):
        additional = 0
    return max(0, 1 + additional)


def can_play_resource(state, side: str, *, limit: int = 1,
                      player_id=None) -> bool:
    """Whether the controller has remaining authored resource plays this turn."""
    return resource_play_count(
        state, side, player_id=player_id) < max(0, int(limit))


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


class _ResourceSession:
    """Minimal session view needed by the shared authored-condition engine."""

    def __init__(self, session_id):
        self.session_id = int(session_id)


_THRESHOLD_INDEX_FLAGS = {0: 0, 1: 4, 2: 8, 3: 16, 4: 32, 5: 64}


def _hand_threshold_requirements(db, session_id, owner_id):
    """Return the greatest authored threshold need in the controller's hand.

    Conditional resource leaves such as Primal Shard are authored as
    ``YouHaveA<colour>CardInYourHand``.  The condition tells us that a colour
    is relevant, while the hand card's typed threshold tells us whether the
    controller still needs more of it.  Keep this derived view local to the
    resource operation; normal fixed-colour resources continue to grant their
    authored threshold on every play.
    """
    rows = db.execute(
        "SELECT ct.threshold_json FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand'",
        (int(session_id), int(owner_id))).fetchall()
    requirements = {}
    for (raw,) in rows:
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        values = payload.get("values")
        if isinstance(values, (list, tuple)) and values:
            for index, count in enumerate(values):
                try:
                    amount = int(count or 0)
                except (TypeError, ValueError):
                    continue
                flag = _THRESHOLD_INDEX_FLAGS.get(index, index)
                if amount > 0 and flag:
                    requirements[flag] = max(
                        requirements.get(flag, 0), amount)
            continue
        counts = {}
        for item in payload.get("list", ()) or ():
            try:
                flag = _THRESHOLD_INDEX_FLAGS.get(int(item), int(item))
            except (TypeError, ValueError):
                continue
            if flag:
                counts[flag] = counts.get(flag, 0) + 1
        for flag, amount in counts.items():
            requirements[flag] = max(requirements.get(flag, 0), amount)
    return requirements


def _active_threshold_count(state, owner_id, flag):
    """Read one owner's threshold count from either PVE or PvP state."""
    state = state if isinstance(state, dict) else {}
    try:
        owner_id = int(owner_id or 0)
        flag = int(flag)
    except (TypeError, ValueError):
        return 0
    thresholds = state.get(f"thresh_{owner_id}")
    if not isinstance(thresholds, dict):
        thresholds = state.get(
            "ai_threshold" if owner_id == 0 else "player_threshold", {})
    if not isinstance(thresholds, dict):
        return 0
    return int(thresholds.get(flag, thresholds.get(str(flag), 0)) or 0)


def resource_threshold_grants(db, session_id, owner_id, ability_guids, state,
                              *, source_uid=None):
    """Return the ``(shard_flag, amount)`` thresholds a resource grants.

    The typed ``ThresholdModifier`` color and amount and the effect's authored
    condition are the source of truth. Missing metadata produces no grant.

    Conditions are evaluated from their authored metadata against the
    controller's current hand. A qualifying card enables that effect's own
    threshold amount; unrelated card costs in hand do not change the grant.
    """
    from pvp_db import db_ability_effect_rows
    from rules_port.metadata import modifier_metadata
    leaves = []
    for guid in ability_guids or ():
        for effect_guid, effect_type, param in db_ability_effect_rows(
                str(guid).lower(), conn=db):
            if effect_type != "CardModifierAbilityEffectTemplate":
                continue
            try:
                value = json.loads(param or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            typed = modifier_metadata(effect_guid)
            property_name = str(typed.get("property") or
                                value.get("property") or "").lower()
            if property_name != "threshold":
                continue
            threshold_color = (typed.get("thresholdcolor") or
                               value.get("thresholdcolor"))
            color_name = str(threshold_color or "").rsplit(".", 1)[-1].lower()
            flag = int(game_engine.SHARD_TO_FLAG.get(color_name, 0))
            if not flag:
                continue
            amount = int(value.get("amount") or 0)
            if amount <= 0:
                continue
            condition_id = str(value.get("condition_id") or "")
            if condition_id.lower() == "0" * 36:
                condition_id = ""
            leaves.append((flag, amount, condition_id))
    if leaves:
        from rules_port.condition_context import ConditionContext
        from rules_port.conditions import evaluate_effect_condition
        context = ConditionContext(
            db, _ResourceSession(session_id), state,
            ability_source_uid=(int(source_uid) if source_uid is not None
                               else None),
            ability_source_owner_id=int(owner_id or 0))
        hand_needs = _hand_threshold_requirements(
            db, session_id, owner_id)
        grants = []
        for flag, amount, condition_id in leaves:
            if condition_id and not evaluate_effect_condition(
                    db, condition_id, context):
                continue
            # A condition that is satisfied by a qualifying hand card should
            # not add a redundant threshold once that card's authored
            # requirement is already met.  No hand requirement means the
            # condition is not this resource-need pattern, so preserve the
            # authored grant.
            required = hand_needs.get(flag)
            if (condition_id and required is not None and
                    _active_threshold_count(state, owner_id, flag) >= required):
                continue
            grants.append((flag, amount))
        return grants
    return []


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
    if property == "chargepoints" and amount > 0:
        # Session.SetChampionChargePoints: positive charge gains add the
        # controller champion's ChargePointBonus.
        from .static_rules import champion_int_attribute
        amount += champion_int_attribute(
            state, _owner_for_side(state, side), "ChargePointBonus")
    key = {
        "currentresource": f"{side}_resources",
        "totalresource": f"{side}_total_resources",
        "chargepoints": f"{side}_charges",
    }[property]
    old = int(state.get(key, 0) or 0)
    new = max(0, old + amount)
    state[key] = new
    if property == "chargepoints" and new > old:
        from .statistics import record_charge_gained
        record_charge_gained(state, _owner_for_side(state, side), new - old)
    return ResourceChange(side, property, amount, old, new)


def _owner_for_side(state, side):
    """Return a raw participant ID from either the PvP or practice view."""
    if isinstance(state, dict):
        pids = [int(pid) for pid in (state.get("pids") or ())]
        if state.get("pvp") and len(pids) >= 2:
            return pids[0] if str(side).lower() == "player" else pids[1]
        mapping = state.get("champ_map") or {}
        for pid in mapping:
            try:
                value = int(pid)
            except (TypeError, ValueError):
                continue
            if (str(side).lower() == "ai" and value == 0) or (
                    str(side).lower() == "player" and value != 0):
                return value
        if str(side).lower() == "ai":
            return 0
        return int(state.get("player_owner_id", 0) or 0)
    return 0


def play_resource(state: MutableMapping, side: str, current_amount: int,
                  total_amount: int, *, threshold_color: int | None = None,
                  charge_amount: int = 1,
                  additional_plays: int = 0) -> ResourcePlay:
    """Apply the complete authored resource-play transition.

    Card movement, triggered abilities, and client events are projections and
    stay outside this module.  The pool, threshold, charge, and once-per-turn
    gameplay state are one atomic RulesPort transition.
    """
    side = "player" if str(side).lower() == "player" else "ai"
    if not can_play_resource(
            state, side, limit=max(0, 1 + int(additional_plays))):
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
    state[f"{side}_resource_plays_this_turn"] = resource_play_count(
        state, side) + 1
    state[f"{side}_resource_played_this_turn"] = True
    return ResourcePlay(current, total, charge, threshold)


def play_resource_for_player(state: MutableMapping, player_id,
                             current_amount: int, total_amount: int,
                             *, threshold_color: int | None = None,
                             charge_amount: int = 1,
                             additional_plays: int = 0) -> ResourcePlay:
    """Apply resource play to a raw-player-id PvP checkpoint."""
    pid = int(player_id)
    played_key = f"res_played_{pid}"
    if not can_play_resource(
            state, "player", limit=max(0, 1 + int(additional_plays)),
            player_id=pid):
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
    bonus = 0
    if int(charge_amount) > 0:
        from .static_rules import champion_int_attribute
        bonus = champion_int_attribute(state, pid, "ChargePointBonus")
    charge = change("chargepoints", int(charge_amount) + bonus)
    if charge.new_value > charge.old_value:
        from .statistics import record_charge_gained
        record_charge_gained(state, pid,
                             charge.new_value - charge.old_value)
    state[played_key] = resource_play_count(
        state, "player", player_id=pid) + 1
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
    bonus_key = f"start_turn_resource_bonus_{pid}"
    bonus = int(state.pop(bonus_key, 0) or 0)
    state[current_key] = total + max(0, bonus)
    state[f"res_played_{pid}"] = 0
    return ResourceChange(str(pid), "currentresource", total + max(0, bonus) - old,
                          old, total + max(0, bonus))


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
    state[f"{normalized}_resource_plays_this_turn"] = 0
    total = int(state.get(f"{normalized}_total_resources", 0) or 0)
    bonus = int(state.pop(f"start_turn_resource_bonus_{normalized}", 0) or 0)
    return apply_resource_change(
        state, normalized, "currentresource",
        total + max(0, bonus) - int(state.get(f"{normalized}_resources", 0) or 0))


def project_resource_change(game, session, state: MutableMapping, player_uid,
                            ai_uid, side: str, property: str, amount: int,
                            *, color: int = 0) -> ResourceChange:
    """Apply a resource transition and publish its existing Game event.

    The Game object is a projection only.  The RulesPort state transition is
    applied first and persisted before the client can submit the next request.
    """
    change = apply_resource_change(
        state, side, property, amount, color=color)
    # Every resource mutation, including thresholds, must reach the durable
    # RulesPort checkpoint before the client can submit the next transaction.
    # Previously only current-resource changes were saved here; a native
    # choice such as Shard of Cunning could emit the threshold event and then
    # have the updated threshold replaced by the older persisted snapshot.
    from .persistence import save_state
    save_state(session, state)
    if property == "currentresource":
        setattr(game, f"{change.side}_resources", change.new_value)
        event_type = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs
    elif property == "totalresource":
        setattr(game, f"{change.side}_total_resources", change.new_value)
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
    event.delta = (abs(int(change.new_value) - int(change.old_value))
                   if property == "chargepoints" else int(amount))
    event.new_value = change.new_value
    if property == "threshold":
        event.color = int(color)
    game._push(event)
    return change
