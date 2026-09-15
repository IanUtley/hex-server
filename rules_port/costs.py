"""Pure ability-cost decisions for the RulesPort.

The port decides whether an ability can be paid and what the payment is.  A
mode adapter may then project the accepted plan into SQLite/Game state.  Keeping
this calculation free of HConnect and SQLite prevents the host from becoming a
second rules engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


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
    for index, (kind, guid) in enumerate(graph.additional_cost_targets or ()):
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
            maximum if maximum > 0 else -1, is_source_auto, candidates))
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
                      ability_key: str = "") -> AbilityCostPlan | None:
    """Return an affordable payment plan, or ``None`` if it is unaffordable.

    ``activation`` carries the typed X value selected by the client.  The
    caller remains responsible for resolving ownership and non-numeric card
    selections; those are separate typed cost requirements.
    """
    activation_cost = int(getattr(costs, "activation", 0) or 0)
    variable = int(getattr(costs, "variable_activation", 0) or 0)
    x_cost = int(getattr(activation, "x_cost", 0) or 0)
    resource = activation_cost + (x_cost if variable else 0)
    charge = int(getattr(costs, "charge_points", 0) or 0)
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
