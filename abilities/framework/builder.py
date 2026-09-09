"""Metadata-backed ability builder facade.

This is intentionally a facade over the existing ``AbilityInstance`` and
``PlayPlan`` contracts.  It gives effect code one place to ask for costs,
targets, filters, and ordered effects without introducing a second card-data
source or resolver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gamedata.play_plan import (AbilityInstance, ActivationData, CardPlayCost,
                                PlayPlan)


@dataclass(frozen=True)
class TargetRef:
    """Named view of one metadata target template."""

    index: int
    spec: Any

    @property
    def guid(self) -> str:
        return str(self.spec.guid)

    @property
    def filter(self) -> Any:
        return self.spec.card_filter

    @property
    def requires_input(self) -> bool:
        return bool(self.spec.requires_input)

    @property
    def minimum(self) -> int:
        return max(0, int(self.spec.minimum or 0))

    @property
    def maximum(self) -> int:
        value = int(self.spec.maximum or 0)
        return value if value > 0 else -1

    @property
    def target_kind(self) -> str:
        return str(self.spec.target_kind or "")

    @property
    def is_auto(self) -> bool:
        return bool(self.spec.is_auto)


@dataclass(frozen=True)
class CostRef:
    """One authored additional-cost target and its client wire type."""

    index: int
    kind: str
    guid: str
    cost_type: int
    target: TargetRef | None

    @property
    def minimum(self) -> int:
        return self.target.minimum if self.target is not None else 1

    @property
    def maximum(self) -> int:
        return self.target.maximum if self.target is not None else 1

    @property
    def requires_input(self) -> bool:
        return bool(self.target and self.target.requires_input)

    @property
    def is_auto(self) -> bool:
        return bool(self.target and self.target.is_auto)

    @property
    def is_source_auto_target(self) -> bool:
        return self.is_auto and self.target_kind == \
            "AbilitySourceCardTargetTemplate"

    @property
    def target_kind(self) -> str:
        return self.target.target_kind if self.target is not None else ""


@dataclass(frozen=True)
class EffectRef:
    """Stable view of one ordered effect mapping."""

    index: int
    spec: Any

    def _get(self, name: str, default=None):
        if isinstance(self.spec, dict):
            return self.spec.get(name, default)
        return getattr(self.spec, name, default)

    @property
    def guid(self) -> str:
        return str(self._get("effect_guid", self._get("guid", "")))

    @property
    def type_name(self) -> str:
        return str(self._get("effect_type", self._get("concrete_type", "")))

    @property
    def target_index(self) -> int:
        return int(self._get("target_index", -1) or -1)

    @property
    def instance_id(self) -> int:
        return int(self._get(
            "effect_instance_id", self._get("instance_id", -1)) or -1)

    @property
    def order(self) -> int:
        return int(self._get(
            "effect_order", self._get("order", self.index)) or self.index)

    @property
    def group(self) -> int:
        return int(self._get("effect_group_id", 0) or 0)

    @property
    def condition_guid(self) -> str:
        return str(self._get("condition_id", self._get("condition_guid", "")) or "")

    @property
    def contingent_instance_id(self) -> int:
        return int(self._get("contingent_effect_instance_id", -1) or -1)

    @property
    def secondary_target_index(self) -> int:
        return int(self._get("secondary_target_index", -1) or -1)

    @property
    def optional(self) -> bool:
        return bool(self._get("is_optional", self._get("optional", False)))


class AbilityBuilder:
    """Fluent read/compile facade for one authoritative ability instance.

    ``from_instance`` is the preferred entry point for resolution.  The
    builder does not mutate the graph; it binds activation data and exposes
    the same cost/target/effect ordering that the client-facing plan uses.
    """

    def __init__(self, instance: AbilityInstance, plan: PlayPlan | None = None):
        self.instance = instance
        self.plan = plan

    @classmethod
    def from_instance(cls, instance: AbilityInstance) -> "AbilityBuilder":
        return cls(instance)

    @classmethod
    def from_graph(cls, graph, *, source_uid: int | None = None,
                   owner_id: int | None = None,
                   activation: ActivationData | None = None,
                   store=None) -> "AbilityBuilder":
        """Compile one authoritative ``AbilityGraph`` into an instance."""
        try:
            instance = AbilityInstance.from_graph(
                graph, source_uid=source_uid, owner_id=owner_id,
                responsible_player_id=owner_id, activation=activation,
                store=store)
        except AttributeError:
            # A few protocol fixtures provide a small graph-shaped double
            # with only targets/costs.  Keep the builder useful for metadata
            # projection without weakening the real typed graph path.
            instance = AbilityInstance(
                graph=graph, source_uid=source_uid, owner_id=owner_id,
                responsible_player_id=owner_id,
                activation=activation or ActivationData(),
                runtime_effects=tuple(getattr(graph, "effects", ()) or ()),
                store=store)
        return cls(instance)

    @classmethod
    def from_runtime(cls, ability_guid: str, effects, target_count: int,
                     *, source_uid: int | None = None,
                     owner_id: int | None = None,
                     activation: ActivationData | None = None,
                     store=None) -> "AbilityBuilder":
        """Adapt the synthetic resolver boundary for focused fixtures."""
        return cls(AbilityInstance.from_runtime(
            ability_guid, effects, target_count, source_uid=source_uid,
            owner_id=owner_id, activation=activation, store=store))

    @classmethod
    def from_plan(cls, plan: PlayPlan, ability_guid: str,
                  activation: ActivationData | None = None) -> "AbilityBuilder":
        for ability in plan.abilities:
            if ability.ability_guid.lower() == str(ability_guid).lower():
                builder = cls(ability, plan)
                return builder.bind(activation) if activation is not None \
                    else builder
        raise KeyError(f"ability {ability_guid} is not part of the play plan")

    @classmethod
    def from_play_plan(cls, plan: PlayPlan,
                       ability_guid: str | None = None) -> "AbilityBuilder":
        """Create a builder for a card plan while retaining card-level costs.

        Card-play cost descriptors belong to ``PlayPlan`` rather than to one
        ability graph.  Selecting an ability is still useful because it keeps
        the builder's normal target/effect view available; the selected
        ability does not change the enclosing plan's cost descriptors.
        """
        if ability_guid is not None:
            return cls.from_plan(plan, ability_guid)
        abilities = plan.cast_abilities or plan.abilities
        if not abilities:
            raise ValueError("a play plan must contain an ability")
        return cls(abilities[0], plan)

    @property
    def guid(self) -> str:
        return self.instance.ability_guid

    @property
    def costs(self):
        return self.instance.costs

    @property
    def card_costs(self):
        """Return the enclosing PlayPlan's authored card-play costs."""
        return self.plan.cost if self.plan is not None else None

    @property
    def card_cost_instances(self) -> tuple[dict[str, Any], ...]:
        """Return normalized card-level payment target descriptors."""
        return self.plan.cost_instances if self.plan is not None else ()

    @property
    def activation(self):
        return self.instance.activation

    @property
    def conditions(self) -> dict[str, Any]:
        graph = self.instance.graph
        if graph is None:
            return {}
        return {
            "ability": getattr(graph, "ability_condition", None),
            "trigger": getattr(graph, "trigger_condition", None),
            "free": getattr(graph, "ability_free_condition", None),
        }

    @property
    def effects(self):
        return self.instance.ordered_effects

    @property
    def effect_refs(self) -> tuple[EffectRef, ...]:
        """Stable metadata views over effects in resolver application order."""
        return tuple(EffectRef(index, spec)
                     for index, spec in enumerate(self.effects))

    @property
    def effect_groups(self):
        return self.instance.effect_groups

    @property
    def effect_conditions(self) -> dict[int, str]:
        """Effect-local condition IDs keyed by ordered effect position."""
        return {ref.index: ref.condition_guid
                for ref in self.effect_refs if ref.condition_guid}

    @property
    def continuations(self) -> tuple[EffectRef, ...]:
        """Effects whose metadata depends on an earlier effect or resumes work."""
        return tuple(ref for ref in self.effect_refs
                     if ref.contingent_instance_id >= 0 or
                     ref.type_name in (
                         "ActivateAbilityEffectTemplate",
                         "RepeatingAbilityEffectTemplate",
                         "DoubleChoiceAbilityEffectTemplate",
                         "ChoiceAbilityEffectTemplate"))

    def _effect_guid(self, effect: EffectRef | int | str | None = None) -> str:
        if isinstance(effect, EffectRef):
            return effect.guid
        if isinstance(effect, int):
            return self.effect(effect).guid
        if effect is None:
            return ""
        return str(effect)

    def value(self, db, bstate, field_name: str,
              effect: EffectRef | int | str | None = None,
              default: int = 0) -> int:
        """Evaluate one typed numeric field from the authoritative Records graph."""
        from .fields import effect_field

        return effect_field(
            db, bstate, self._effect_guid(effect), field_name, default)

    def template_value(self, db, bstate, field_name: str,
                       effect: EffectRef | int | str | None = None,
                       default: Any = None) -> Any:
        """Read one non-numeric typed field from an effect template."""
        from .fields import effect_template_value

        return effect_template_value(
            db, bstate, self._effect_guid(effect), field_name, default)

    def target(self, index: int | str = 0) -> TargetRef:
        if isinstance(index, str):
            key = index.lower()
            for position, spec in enumerate(self.instance.targets):
                if (str(spec.name).lower() == key or
                        str(spec.target_kind).lower() == key):
                    return TargetRef(position, spec)
            if key in ("primary", "first"):
                index = 0
            else:
                raise KeyError(
                    f"ability {self.guid} has no target template named {index}")
        try:
            return TargetRef(index, self.instance.targets[index])
        except (IndexError, TypeError):
            raise KeyError(
                f"ability {self.guid} has no target template {index}") from None

    def targets(self, *, requires_input: bool | None = None,
                include_costs: bool = True) -> tuple[TargetRef, ...]:
        """Return authored target templates in graph order.

        ``include_costs=False`` is useful for client target pickers: payment
        targets are represented by ``CostInstance`` rather than ordinary
        effect-target ``TargetInstance`` records.
        """
        cost_guids = {cost.guid.lower() for cost in self.cost_targets}
        result = []
        for index, spec in enumerate(self.instance.targets):
            ref = TargetRef(index, spec)
            if not include_costs and ref.guid.lower() in cost_guids:
                continue
            if (requires_input is not None and
                    ref.requires_input != requires_input):
                continue
            result.append(ref)
        return tuple(result)

    @property
    def cost_targets(self) -> tuple[CostRef, ...]:
        """Return typed additional-cost targets from the authoritative graph."""
        graph = self.instance.graph
        if graph is None:
            return ()
        result = []
        for index, (kind, guid) in enumerate(
                getattr(graph, "additional_cost_targets", ()) or ()):
            target = None
            for target_index, spec in enumerate(self.instance.targets):
                if str(spec.guid).lower() == str(guid).lower():
                    target = TargetRef(target_index, spec)
                    break
            if target is None and self.instance.store is not None:
                record = self.instance.store.get(
                    "AbilityTargetTemplate", str(guid).lower())
                if record is not None:
                    target = TargetRef(-1, record.target_spec)
            result.append(CostRef(
                index=index, kind=str(kind), guid=str(guid),
                cost_type=CardPlayCost.cost_type(kind), target=target))
        return tuple(result)

    def filter_target(self, index: int | str = 0) -> Any:
        """Return the authored card filter for target template ``index``."""
        return self.target(index).filter

    def target_candidates(self, db, session_id: int, controller_uid: int,
                          target: TargetRef | int | str = 0,
                          source_uid: int | None = None, *,
                          both_players: bool | None = None,
                          champions=None, battle_state=None):
        """Evaluate one authored target through the shared legal-target path.

        PvE and PvP may provide different champion pools and battle-state
        projections, but converting a ``TargetRef`` into the authoritative
        metadata predicate is identical. Keeping that conversion here prevents
        activation handlers from becoming a second targeting implementation.
        """
        from .targeting import legal_targets_for

        if isinstance(target, TargetRef):
            target_ref = target
        elif isinstance(target, str):
            try:
                target_ref = self.target(target)
            except KeyError:
                # Additional-cost metadata may refer to a target template that
                # is not repeated in the instance's ordinary target list.
                target_ref = target
        else:
            target_ref = self.target(target)
        return legal_targets_for(
            db, session_id, controller_uid, target_ref, source_uid,
            both_players=both_players, champions=champions,
            battle_state=battle_state)

    def cost_candidates(self, db, session_id: int, controller_uid: int,
                        source_uid: int | None = None, *,
                        champions=None, battle_state=None):
        """Return ``(CostRef, legal candidate UIDs)`` for authored payments.

        Automatic ``this`` payments are represented as the source card, while
        selectable costs use the same target filter as the client picker.
        The caller still decides minimum/maximum selection and payment order.
        """
        result = []
        for cost in self.cost_targets:
            if cost.target is None:
                result.append((cost, ()))
                continue
            if cost.is_source_auto_target:
                candidates = (() if source_uid is None else (int(source_uid),))
            else:
                candidates = self.target_candidates(
                    db, session_id, controller_uid, cost.target, source_uid,
                    both_players=False, champions=champions,
                    battle_state=battle_state)
            result.append((cost, tuple(int(uid) for uid in candidates)))
        return tuple(result)

    def card_cost_candidates(self, db, session_id: int, controller_uid: int,
                             source_uid: int | None = None, *,
                             both_players: bool = False, champions=None,
                             battle_state=None):
        """Return ``(card-cost descriptor, legal candidate UIDs)``.

        Card-level costs are authored on ``CardTemplate`` and normalized by
        ``PlayPlan``.  This method deliberately delegates candidate evaluation
        to the same metadata target path as ability targets; callers retain
        responsibility for minimum/maximum selection and payment effects.
        Automatic descriptors represent the played source card, matching the
        existing card-play activation contract.
        """
        result = []
        for spec in self.card_cost_instances:
            if spec["auto"]:
                candidates = (() if source_uid is None else (int(source_uid),))
            else:
                candidates = self.target_candidates(
                    db, session_id, controller_uid, spec["target_guid"],
                    source_uid, both_players=both_players,
                    champions=champions, battle_state=battle_state)
            result.append((spec, tuple(int(uid) for uid in candidates)))
        return tuple(result)

    def effect(self, index: int = 0) -> EffectRef:
        """Return one ordered effect mapping by position."""
        try:
            return EffectRef(index, self.instance.ordered_effects[index])
        except (IndexError, TypeError):
            raise KeyError(
                f"ability {self.guid} has no ordered effect {index}") from None

    def required_prompts(self):
        return self.instance.required_prompts()

    def validate(self, **available) -> tuple[str, ...]:
        return self.instance.validate_activation(**available)

    def compile(self) -> AbilityInstance:
        """Return the normalized instance consumed by the common resolver."""
        return self.instance

    def bind(self, activation: ActivationData | None = None,
             **values) -> "AbilityBuilder":
        """Return a builder bound to normalized activation data."""
        if activation is None:
            activation = ActivationData.from_values(**values)
        return type(self)(AbilityInstance(
            graph=self.instance.graph,
            source_uid=self.instance.source_uid,
            owner_id=self.instance.owner_id,
            responsible_player_id=self.instance.responsible_player_id,
            activation=activation,
            runtime_effects=self.instance.runtime_effects,
            runtime_target_count=self.instance.runtime_target_count,
            runtime_ability_guid=self.instance.runtime_ability_guid,
            store=self.instance.store,
        ), self.plan)
