"""Pure ability-cost decisions for the RulesPort.

The port decides whether an ability can be paid and what the payment is.  A
mode adapter may then project the accepted plan into SQLite/Game state.  Keeping
this calculation free of HConnect and SQLite prevents the host from becoming a
second rules engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, cast


@dataclass(frozen=True)
class AbilityCostPlan:
    """The complete numeric payment selected by RulesPort."""

    resource: int = 0
    charge_points: int = 0
    spell_points: int = 0
    life: int = 0
    spell_use_key: str | None = None
    next_spell_use: int | None = None


@dataclass(frozen=True)
class AbilityCostTarget:
    """Native metadata view of one selectable additional cost."""
    index: int
    kind: str
    guid: str
    minimum: int
    maximum: int
    is_source_auto_target: bool
    candidates: tuple[int, ...]
    allow_best_effort_minimum: bool = False


@dataclass(frozen=True)
class CounterCostPayment:
    """One resolved counter payment against an ability source card."""

    counter_guid: str
    counter_name: str
    amount: int
    old_value: int
    new_value: int


def _source_is_champion(db, session_id: int, source_uid: int, state) -> bool:
    """Identify synthetic champion cards in PvE and PvP checkpoints."""
    champion_values = (state.get("champ_map") or {}).values()
    for value in champion_values:
        try:
            if int(getattr(value, "uid64", value)) == int(source_uid):
                return True
        except (TypeError, ValueError):
            continue
    if str(int(source_uid)) in (state.get("champion_counters") or {}):
        return True
    row = db.execute(
        "SELECT 1 FROM game_cards WHERE session_id=? AND card_uid=?",
        (int(session_id), int(source_uid))).fetchone()
    # Champions are synthetic SessionCards with no game_cards row. Ability
    # sources passed here are already resolved by the active RulesPort item.
    return row is None


def plan_source_counter_costs(db, session_id: int, source_uid: int | None,
                              costs, battle_state=None):
    """Resolve authored ``m_CounterCosts`` against the source's counters.

    C# ``Session.PayCounterCost`` treats -10000 as "remove all" and records
    the amount removed on the source card as ``CounterCostPaid``.  Keep that
    interpretation here so affordability and payment use the same metadata.
    """
    counter_costs = tuple(getattr(costs, "counter_costs", ()) or ())
    if not counter_costs:
        return ()
    if source_uid is None:
        return None
    try:
        source_uid = int(source_uid)
    except (TypeError, ValueError):
        return None

    from pvp_db import db_card_mutation_field, db_counter_template_name

    state = battle_state if isinstance(battle_state, dict) else {}
    champion_values = (state.get("champion_counters") or {}).get(
        str(source_uid), {})
    if not isinstance(champion_values, dict):
        champion_values = {}
    is_champion = _source_is_champion(
        db, session_id, source_uid, state)
    working_champion = dict(champion_values)
    raw = db_card_mutation_field(
        session_id, source_uid, "permanent_buffs", conn=db)
    try:
        permanent = json.loads(raw or "{}") if raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        permanent = {}
    if not isinstance(permanent, dict):
        permanent = {}
    counters = permanent.get("counters") or {}
    counter_guids = permanent.get("counter_guids") or {}
    if not isinstance(counters, dict):
        counters = {}
    if not isinstance(counter_guids, dict):
        counter_guids = {}
    # Simulate each cost in order. The client applies sequential payments, so
    # two costs against the same counter cannot both spend the same points.
    working = {str(name): int(value or 0)
               for name, value in counters.items()}
    working_guids = {str(name): str(value or "").lower()
                     for name, value in counter_guids.items()}
    payments = []
    for cost in counter_costs:
        guid = str(getattr(cost, "counter_guid", "") or "").lower()
        if not guid:
            return None
        counter_name = db_counter_template_name(guid, conn=db)
        if not counter_name:
            return None
        name_key = str(counter_name).lower()
        matching = [key for key in working
                    if key.lower() == name_key or
                    working_guids.get(key, "") == guid]
        if is_champion:
            old_value = int(working_champion.get(guid, 0) or 0)
        elif matching:
            old_value = sum(int(working.get(key, 0) or 0)
                            for key in matching)
        else:
            old_value = 0
        amount = int(getattr(cost, "amount", 0) or 0)
        if amount not in (-10000, 0) and old_value < amount:
            return None
        paid = old_value if amount == -10000 else amount
        new_value = (0 if amount == -10000 else
                     max(0, old_value - amount))
        payments.append(CounterCostPayment(
            guid, str(counter_name), paid, old_value, new_value))
        if is_champion:
            if new_value:
                working_champion[guid] = new_value
            else:
                working_champion.pop(guid, None)
        else:
            for key in matching:
                working.pop(key, None)
                working_guids.pop(key, None)
            if new_value:
                working[str(counter_name)] = new_value
                working_guids[str(counter_name)] = guid
    return tuple(payments)


def apply_source_counter_cost_plan(db, session_id: int, source_uid: int,
                                   plan, battle_state,
                                   ability_instance_id=None):
    """Apply a validated source-counter plan and return projection deltas."""
    from pvp_db import db_card_mutation_field, db_set_card_mutation_field
    from .statistics import add_ability_stat

    source_uid = int(source_uid)
    state = battle_state if isinstance(battle_state, dict) else {}
    is_champion = _source_is_champion(
        db, session_id, source_uid, state)
    changes = []
    removed_total = 0
    if is_champion:
        counters = state.setdefault("champion_counters", {}).setdefault(
            str(source_uid), {})
        for payment in plan or ():
            guid = str(payment.counter_guid).lower()
            old = int(counters.get(guid, 0) or 0)
            new = int(payment.new_value)
            if new:
                counters[guid] = new
            else:
                counters.pop(guid, None)
            if old != new:
                changes.append({"counter_guid": guid, "old": old,
                                "new": new, "source_uid": source_uid})
            removed_total += max(0, old - new)
    else:
        raw = db_card_mutation_field(
            session_id, source_uid, "permanent_buffs", conn=db)
        try:
            permanent = json.loads(raw or "{}") if raw else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            permanent = {}
        if not isinstance(permanent, dict):
            permanent = {}
        counters = permanent.setdefault("counters", {})
        counter_guids = permanent.setdefault("counter_guids", {})
        if not isinstance(counters, dict):
            counters = {}
            permanent["counters"] = counters
        if not isinstance(counter_guids, dict):
            counter_guids = {}
            permanent["counter_guids"] = counter_guids
        for payment in plan or ():
            guid = str(payment.counter_guid).lower()
            name = str(payment.counter_name)
            name_key = name.lower()
            matching = [key for key in counters
                        if str(key).lower() == name_key or
                        str(counter_guids.get(key, "")).lower() == guid]
            old = sum(int(counters.get(key, 0) or 0) for key in matching)
            new = int(payment.new_value)
            for key in matching:
                counters.pop(key, None)
                counter_guids.pop(key, None)
            if new:
                counters[name] = new
                counter_guids[name] = guid
            if old != new:
                changes.append({"counter_guid": guid, "old": old,
                                "new": new, "source_uid": source_uid})
            removed_total += max(0, old - new)
        db_set_card_mutation_field(
            session_id, source_uid, "permanent_buffs",
            json.dumps(permanent), conn=db)
        db.commit()

    if plan:
        # Card.CounterCostPaid is source-card state. The latest authored cost
        # wins, matching Session.PayCounterCost's assignment order.
        state.setdefault("card_counter_cost_paid", {})[
            str(source_uid)] = int(plan[-1].amount)
        for change in changes:
            change["source_uid"] = source_uid
    if removed_total:
        add_ability_stat(
            state, "CountersRemoved", removed_total,
            instance_id=ability_instance_id)
    return tuple(changes)


def validate_cost_target_selection(db, session_id: int, controller_uid: int,
                                   source_uid: int | None,
                                   cost: AbilityCostTarget, selected,
                                   *, champions=None, battle_state=None,
                                   variables=None):
    """Validate one submitted additional-cost selection against its template.

    Candidate enumeration is not sufficient for specialized target classes.
    ``SharedNameTargetTemplate`` deliberately enumerates every card from a
    qualifying same-name group, so a caller must still validate that the
    submitted selection belongs to one group.  Keep this check beside the
    authored cost projection so PvE, PvP, and card-play adapters cannot each
    grow a slightly different interpretation of the cost.
    """
    values = selected if isinstance(selected, (list, tuple, set)) else (
        () if selected is None else (selected,))
    try:
        values = tuple(int(getattr(value, "uid64", value))
                      for value in values)
    except (TypeError, ValueError):
        return None

    if cost.is_source_auto_target:
        if source_uid is None:
            return values if not values else None
        expected = (int(source_uid),)
        return expected if not values or values == expected else None

    minimum = int(cost.minimum or 0)
    maximum = int(cost.maximum or -1)
    if (len(values) < minimum and
            not cost.allow_best_effort_minimum) or \
            (maximum > 0 and len(values) > maximum):
        return None
    if any(value not in {int(candidate) for candidate in cost.candidates}
           for value in values):
        return None

    from .targeting import validate_target_selection
    validated = validate_target_selection(
        db, session_id, int(controller_uid), cost.guid, source_uid, values,
        both_players=False, champions=champions, battle_state=battle_state,
        variables=variables)
    if tuple(validated) != values:
        return None
    return values


def cost_type_for_kind(kind: str) -> int:
    """Return the client wire value for an authored card-cost kind."""
    return {
        "exhaust": 1,
        "sacrifice": 2,
        "shuffle_into_deck": 4,
        "discard": 8,
        "void": 16,
        "put_into_deck": 32,
        "reveal": 64,
        "put_into_hand": 128,
    }.get(str(kind), 0)


def ability_cost_targets(graph, db, session_id: int, owner_id: int,
                         source_uid: int | None, *, champions=None,
                         battle_state=None) -> tuple[AbilityCostTarget, ...]:
    """Evaluate authored additional-cost targets without the legacy builder."""
    if graph is None:
        return ()
    from .targeting import legal_targets_for
    from gamedata import DEFAULT_RECORD_STORE
    result = []
    for index, (kind, guid) in enumerate(cast(
            Any, graph.additional_cost_targets or ())):
        target = next((value for value in graph.targets
                       if str(value.guid).lower() == str(guid).lower()), None)
        if target is None:
            record = DEFAULT_RECORD_STORE.get(
                "AbilityTargetTemplate", str(guid).lower())
            target = record.target_spec if record is not None else None
        if target is None:
            result.append(AbilityCostTarget(
                index, str(kind), str(guid), 1, 1, False, ()))
            continue
        is_source_auto = bool(target.is_auto and
                              target.target_kind ==
                              "AbilitySourceCardTargetTemplate")
        candidates = ((int(source_uid),) if is_source_auto and source_uid is not None
                      else () if is_source_auto else tuple(int(value) for value in
                          legal_targets_for(
                              db, session_id, owner_id, target, source_uid,
                              both_players=False, champions=champions,
                              battle_state=battle_state)))
        maximum = int(target.maximum or 0)
        result.append(AbilityCostTarget(
            index, str(kind), str(guid), max(0, int(target.minimum or 0)),
            maximum if maximum > 0 else -1, is_source_auto, candidates,
            bool(getattr(target, "allow_best_effort_minimum", False))))
    return tuple(result)


def card_cost_targets(play_plan, db, session_id: int, owner_id: int,
                      source_uid: int | None, *, champions=None,
                      battle_state=None) -> tuple[AbilityCostTarget, ...]:
    """Evaluate card-level PlayPlan costs through native targeting."""
    if play_plan is None:
        return ()
    from .targeting import legal_targets_for
    from gamedata import DEFAULT_RECORD_STORE
    store = getattr(play_plan, "store", None) or DEFAULT_RECORD_STORE
    result = []
    for spec in play_plan.cost_instances:
        guid = str(spec["target_guid"])
        target = store.get("AbilityTargetTemplate", guid.lower())
        target = target.target_spec if target is not None else None
        auto = bool(spec.get("auto"))
        candidates = ((int(source_uid),) if auto and source_uid is not None
                      else () if auto else tuple(int(value) for value in
                          legal_targets_for(
                              db, session_id, owner_id, target or guid,
                              source_uid, both_players=False,
                              champions=champions, battle_state=battle_state)))
        result.append(AbilityCostTarget(
            int(spec["index"]), str(spec["kind"]), guid,
            int(spec["minimum"]), int(spec["maximum"]), auto, candidates,
            bool(spec.get("allow_best_effort_minimum", False) or
                 (target and target.allow_best_effort_minimum))))
    return tuple(result)


def apply_ability_cost_plan(state, side: str, plan: AbilityCostPlan) -> dict:
    """Apply an already validated numeric ability payment atomically."""
    if not isinstance(state, dict) or not isinstance(plan, AbilityCostPlan):
        raise TypeError("state and plan types are invalid")
    prefix = "player" if str(side).lower() == "player" else "ai"
    keys = {
        "resource": f"{prefix}_resources",
        "charge_points": f"{prefix}_charges",
        "spell_points": f"{prefix}_spell_points",
        "life": f"{prefix}_health",
    }
    amounts = {
        "resource": int(plan.resource),
        "charge_points": int(plan.charge_points),
        "spell_points": int(plan.spell_points),
        "life": int(plan.life),
    }
    old = {name: int(state.get(key, 0) or 0)
           for name, key in keys.items()}
    if any(amount < 0 or amount > old[name]
           for name, amount in amounts.items()):
        raise ValueError("ability cost plan is no longer affordable")
    new = {name: old[name] - amounts[name] for name in amounts}
    for name, key in keys.items():
        state[key] = new[name]
    if plan.spell_use_key is not None:
        uses_key = f"{prefix}_sp_uses"
        uses = dict(state.get(uses_key) or {})
        uses[str(plan.spell_use_key)] = int(plan.next_spell_use or 0)
        state[uses_key] = uses
    return {name: {"old": old[name], "new": new[name],
                   "amount": amounts[name]} for name in amounts}


def plan_ability_cost(costs, activation, *, current_resource: int,
                      charges: int, spell_points: int, health: int,
                      spell_uses: Mapping[str, int] | None = None,
                      ability_key: str = "",
                      charge_modifier: int = 0) -> AbilityCostPlan | None:
    """Return an affordable payment plan, or ``None`` if it is unaffordable.

    ``activation`` carries the typed X value selected by the client.  The
    caller remains responsible for resolving ownership and non-numeric card
    selections; those are separate typed cost requirements.  ``charge_modifier``
    is the source card's ``ChargePointCostModifier`` (C#
    ``Card.GetChargePointCostModifier``).
    """
    activation_cost = int(getattr(costs, "activation", 0) or 0)
    variable = int(getattr(costs, "variable_activation", 0) or 0)
    x_cost = int(getattr(activation, "x_cost", 0) or 0)
    resource = activation_cost + (x_cost if variable else 0)
    charge = max(0, int(getattr(costs, "charge_points", 0) or 0)
                 + int(charge_modifier or 0))
    spell = int(getattr(costs, "spell_points", 0) or 0)
    life = int(getattr(costs, "life", 0) or 0)
    uses = spell_uses or {}
    escalation = (int(uses.get(ability_key, 0) or 0)
                  if getattr(costs, "is_spell_power", False) else 0)
    effective_spell = spell + escalation
    if (resource > int(current_resource) or charge > int(charges) or
            effective_spell > int(spell_points) or life > int(health)):
        return None
    return AbilityCostPlan(
        resource=resource,
        charge_points=charge,
        spell_points=effective_spell,
        life=life,
        spell_use_key=(ability_key if getattr(costs, "is_spell_power", False)
                       and spell else None),
        next_spell_use=(escalation + 1
                        if getattr(costs, "is_spell_power", False) and spell
                        else None),
    )
