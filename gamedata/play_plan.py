"""Activation and card-play plans built from the client Mechanics graph.

The client separates three concerns which used to be mixed together in the
server's card-play handlers: constructing an AbilityInstance, collecting the
activation data it still needs, and applying the effect groups.  These classes
model the first two concerns and provide the stable hand-off to the existing
effect interpreter.  They deliberately do not mutate a game or spend a
resource; callers must validate the plan before doing those runtime actions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .models import AbilityCost, AbilityOptionGroup, TargetSpec
from .records import RecordObject, RecordStore
from .semantics import (AbilityGraph, EffectSpec, card_ability_graphs,
                        runtime_effects as graph_runtime_effects)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, RecordObject):
        return value.field(name, default)
    if isinstance(value, Mapping):
        return value.get(name, default)
    if hasattr(value, name):
        return getattr(value, name)
    return default


def _normalise_targets(value: Any) -> dict[int, tuple[int, ...]]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[int, tuple[int, ...]] = {}
    for key, selected in value.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        values = selected if isinstance(selected, (list, tuple, set)) else [selected]
        normalised = []
        for item in values:
            try:
                normalised.append(int(item))
            except (TypeError, ValueError):
                continue
        result[index] = tuple(normalised)
    return result


def _normalise_options(value: Any) -> dict[int, int]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[int, int] = {}
    for key, selected in value.items():
        try:
            result[int(key)] = int(selected)
        except (TypeError, ValueError):
            continue
    return result


@dataclass(frozen=True)
class ActivationData:
    """The server-side equivalent of client ``AbilityActivationData``.

    ``TargetMap`` is keyed by target-template index.  ``OptionMap`` is keyed by
    option index, while additional-cost targets use their own map because they
    are not ability effect targets.  Values are normalized to integers so the
    same validation works for decoded protocol data and test fixtures.
    """

    target_map: dict[int, tuple[int, ...]] = field(default_factory=dict)
    option_map: dict[int, int] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    cost_target_map: dict[int, tuple[int, ...]] = field(default_factory=dict)
    x_cost: int | None = None
    index: int = -1
    opted: bool = False

    @classmethod
    def from_values(cls, *, target_map: Any = None, option_map: Any = None,
                    variables: Mapping[str, Any] | None = None,
                    cost_target_map: Any = None, x_cost: Any = None,
                    index: Any = -1, opted: Any = False) -> "ActivationData":
        parsed_x = None if x_cost is None else _int(x_cost, -1)
        return cls(
            target_map=_normalise_targets(target_map),
            option_map=_normalise_options(option_map),
            variables=dict(variables or {}),
            cost_target_map=_normalise_targets(cost_target_map),
            x_cost=parsed_x,
            index=_int(index, -1),
            opted=bool(opted),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_map": {str(key): list(value)
                           for key, value in self.target_map.items()},
            "option_map": {str(key): value
                           for key, value in self.option_map.items()},
            "variables": dict(self.variables),
            "cost_target_map": {str(key): list(value)
                                for key, value in self.cost_target_map.items()},
            "x_cost": self.x_cost,
            "index": self.index,
            "opted": self.opted,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ActivationData":
        """Hydrate activation data persisted on a chain item.

        Chain state is JSON, so maps are stored with string keys.  Keeping the
        conversion here makes the resolver independent of whether activation
        data came from the client transaction or from persisted stack state.
        """
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        return cls.from_values(
            target_map=value.get("target_map"),
            option_map=value.get("option_map"),
            variables=value.get("variables"),
            cost_target_map=value.get("cost_target_map"),
            x_cost=value.get("x_cost"),
            index=value.get("index", -1),
            opted=value.get("opted", False),
        )


@dataclass(frozen=True)
class PromptSpec:
    """A typed request the UI/protocol layer must satisfy before activation."""

    kind: str
    index: int
    ability_guid: str
    template_guid: str = ""
    label: str = ""
    minimum: int = 0
    maximum: int = 0
    optional: bool = False
    cost_kind: str = ""
    choices: tuple[tuple[int, str], ...] = ()


@dataclass
class AbilityInstance:
    """One client-style activation over an immutable :class:`AbilityGraph`."""

    graph: AbilityGraph | None
    source_uid: int | None = None
    owner_id: int | None = None
    responsible_player_id: int | None = None
    activation: ActivationData = field(default_factory=ActivationData)
    runtime_effects: tuple[Mapping[str, Any], ...] = ()
    runtime_target_count: int | None = None
    runtime_ability_guid: str = ""
    store: RecordStore | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_graph(cls, graph: AbilityGraph, *, source_uid: int | None = None,
                   owner_id: int | None = None,
                   responsible_player_id: int | None = None,
                   activation: ActivationData | None = None,
                   store: RecordStore | None = None) -> "AbilityInstance":
        # Keep the graph attached for costs, prompts, and validation while
        # exposing the same normalized effect mapping used by the leaf ABI.
        # The mapping is derived from this graph; it is not a second rules
        # source.
        return cls(graph, source_uid, owner_id, responsible_player_id,
                   activation or ActivationData(),
                   runtime_effects=tuple(graph_runtime_effects(graph)),
                   store=store)

    @classmethod
    def from_runtime(cls, ability_guid: str, effects: list[Mapping[str, Any]],
                     target_count: int, *, source_uid: int | None = None,
                     owner_id: int | None = None,
                     activation: ActivationData | None = None,
                     store: RecordStore | None = None) -> "AbilityInstance":
        """Adapt Records-derived effect data to the shared instance API.

        The live resolver uses this only as a boundary between the typed
        Records graph and the existing leaf-function ABI; it is not a second
        card-data source.
        """
        return cls(
            graph=None,
            source_uid=source_uid,
            owner_id=owner_id,
            activation=activation or ActivationData(),
            runtime_effects=tuple(effects),
            runtime_target_count=target_count,
            runtime_ability_guid=str(ability_guid).lower(),
            store=store,
        )

    @property
    def ability_guid(self) -> str:
        return (self.graph.guid if self.graph is not None
                else self.runtime_ability_guid)

    @property
    def is_triggered(self) -> bool:
        return bool(self.graph and self.graph.trigger_event_type
                    or self.graph and self.graph.trigger_condition)

    @property
    def costs(self) -> AbilityCost:
        return self.graph.costs if self.graph is not None else AbilityCost()

    @property
    def targets(self) -> tuple[TargetSpec, ...]:
        return self.graph.targets if self.graph is not None else ()

    @property
    def options(self) -> tuple[Any, ...]:
        return self.graph.options if self.graph is not None else ()

    @property
    def effects(self) -> tuple[EffectSpec | Mapping[str, Any], ...]:
        if self.runtime_effects:
            return self.runtime_effects
        return self.graph.effects if self.graph is not None else ()

    @staticmethod
    def _effect_key(item: tuple[int, EffectSpec | Mapping[str, Any]]) -> tuple[int, int, int, int]:
        position, effect = item
        group = _int(_field(effect, "effect_group_id",
                            _field(effect, "m_EffectGroupId", 0)), 0)
        instance = _int(_field(effect, "effect_instance_id",
                               _field(effect, "m_EffectInstanceId", -1)), -1)
        order = _int(_field(effect, "effect_order", position), position)
        return group, instance, order, position

    @property
    def ordered_effects(self) -> tuple[EffectSpec | Mapping[str, Any], ...]:
        """Effects in client application order: group, then instance."""
        return tuple(effect for _, effect in sorted(
            enumerate(self.effects), key=self._effect_key))

    @property
    def effect_groups(self) -> tuple[tuple[int, tuple[Any, ...]], ...]:
        groups: dict[int, list[Any]] = {}
        for effect in self.ordered_effects:
            group = _int(_field(effect, "effect_group_id",
                                _field(effect, "m_EffectGroupId", 0)), 0)
            groups.setdefault(group, []).append(effect)
        return tuple((group, tuple(effects)) for group, effects in groups.items())

    @property
    def referenced_target_indexes(self) -> tuple[int, ...]:
        indexes = set()
        for effect in self.effects:
            index = _int(_field(effect, "target_index",
                                _field(effect, "m_TargetTemplateIndex", -1)), -1)
            if index >= 0:
                indexes.add(index)
        return tuple(sorted(indexes))

    @staticmethod
    def _variable_requires_input(variable: Any) -> bool:
        return bool(_int(_field(variable, "m_RequiresPlayerInput"))) or bool(
            _int(_field(variable, "m_RequiresExplicitSet")))

    def _cost_target_spec(self, index: int) -> TargetSpec | None:
        if self.store is None or index < 0 or index >= len(self.costs.target_costs):
            return None
        target = self.store.get(
            "AbilityTargetTemplate", self.costs.target_costs[index][1])
        return target.target_spec if target is not None else None

    def required_prompts(self) -> tuple[PromptSpec, ...]:
        prompts: list[PromptSpec] = []
        for index in self.referenced_target_indexes:
            spec = self.targets[index] if index < len(self.targets) else None
            if spec is None or not spec.requires_input:
                continue
            if index in self.activation.target_map:
                continue
            prompts.append(PromptSpec(
                kind="target", index=index, ability_guid=self.ability_guid,
                template_guid=spec.guid, label=spec.name,
                minimum=spec.minimum, maximum=spec.maximum,
                optional=spec.optional,
            ))
        for position, option in enumerate(self.options):
            # AbilityInstance.UpdateOptionMap indexes m_AbilityOptions itself;
            # the entries inside a group are selected by their list index.
            index = position
            if index not in self.activation.option_map:
                if isinstance(option, AbilityOptionGroup):
                    label = option.label
                    choices = tuple((entry_index, entry.label)
                                    for entry_index, entry in enumerate(option.options))
                else:
                    label = str(_field(option, "m_Name", "Choose an option"))
                    choices = ()
                prompts.append(PromptSpec(
                    kind="option", index=index, ability_guid=self.ability_guid,
                    label=label, choices=choices,
                ))
        for position, variable in enumerate(
                self.graph.variables if self.graph is not None else ()):
            if not self._variable_requires_input(variable):
                continue
            name = str(_field(variable, "m_Name", f"variable_{position}"))
            if name not in self.activation.variables:
                prompts.append(PromptSpec(
                    kind="variable", index=position,
                    ability_guid=self.ability_guid, label=name,
                ))
        if self.costs.variable_activation and self.activation.x_cost is None:
            prompts.append(PromptSpec(
                kind="x_cost", index=-1, ability_guid=self.ability_guid,
                label="Choose X", minimum=self.costs.variable_minimum,
            ))
        for index, (kind, guid) in enumerate(
                self.graph.additional_cost_targets if self.graph is not None else ()):
            spec = self._cost_target_spec(index)
            if spec is not None and not spec.requires_input:
                continue
            if index not in self.activation.cost_target_map:
                prompts.append(PromptSpec(
                    kind="additional_cost", index=index,
                    ability_guid=self.ability_guid, template_guid=guid,
                    label=kind.replace("_", " "), cost_kind=kind,
                ))
        return tuple(prompts)

    def validate_activation(self, *, resources_available: int | None = None,
                            charge_available: int | None = None,
                            spell_points_available: int | None = None,
                            life_available: int | None = None) -> tuple[str, ...]:
        errors: list[str] = []
        target_count = len(self.targets) if self.graph is not None else (
            self.runtime_target_count or 0)
        for index, selected in self.activation.target_map.items():
            if index < 0 or index >= target_count:
                errors.append(f"unknown target-template index {index}")
                continue
            if self.graph is None:
                continue
            spec = self.targets[index]
            count = len(selected)
            if count > spec.maximum > 0:
                errors.append(f"target {index} has {count} selections; maximum is {spec.maximum}")
            if count < spec.minimum and not spec.optional and not spec.allow_best_effort_minimum:
                errors.append(f"target {index} needs at least {spec.minimum} selections")
        for prompt in self.required_prompts():
            errors.append(f"missing {prompt.kind} input at index {prompt.index}")
        for index, selected in self.activation.option_map.items():
            if self.graph is None or index < 0 or index >= len(self.options):
                errors.append(f"unknown option-group index {index}")
                continue
            option = self.options[index]
            if isinstance(option, AbilityOptionGroup) and not option.is_valid(selected):
                errors.append(f"option group {index} has invalid selection {selected}")
        for index, selected in self.activation.cost_target_map.items():
            if (self.graph is None or index < 0 or
                    index >= len(self.costs.target_costs)):
                errors.append(f"unknown additional-cost index {index}")
                continue
            spec = self._cost_target_spec(index)
            if spec is None:
                continue
            count = len(selected)
            if count > spec.maximum > 0:
                errors.append(
                    f"additional cost {index} has {count} selections; "
                    f"maximum is {spec.maximum}")
            if (count < spec.minimum and not spec.optional and
                    not spec.allow_best_effort_minimum):
                errors.append(
                    f"additional cost {index} needs at least "
                    f"{spec.minimum} selections")
        if resources_available is not None:
            resource_cost = self.costs.activation
            if self.costs.variable_activation:
                resource_cost += max(self.activation.x_cost or 0,
                                     self.costs.variable_minimum)
            if resources_available < resource_cost:
                errors.append(
                    f"ability costs {resource_cost} resources; only "
                    f"{resources_available} available")
        if charge_available is not None and charge_available < self.costs.charge_points:
            errors.append("ability charge-point cost cannot be paid")
        if (spell_points_available is not None and
                spell_points_available < self.costs.spell_points):
            errors.append("ability spell-point cost cannot be paid")
        if life_available is not None and life_available < self.costs.life:
            errors.append("ability life cost cannot be paid")
        return tuple(errors)

    @property
    def is_complete(self) -> bool:
        return not self.validate_activation()


@dataclass(frozen=True)
class CardPlayCost:
    resource: int
    variable: bool
    variable_minimum: int
    life: int
    threshold: Any
    additional_cost_targets: tuple[tuple[str, str], ...] = ()

    @staticmethod
    def cost_type(kind: str) -> int:
        """Wire value of ``EAbilityCostType`` for a card cost kind."""
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


@dataclass
class PlayPlan:
    """Complete metadata plan for playing one card instance."""

    card: RecordObject
    cost: CardPlayCost
    abilities: tuple[AbilityInstance, ...]
    source_uid: int | None = None
    owner_id: int | None = None
    store: RecordStore | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_card(cls, store: RecordStore, card_guid: str, *,
                  source_uid: int | None = None,
                  owner_id: int | None = None) -> "PlayPlan":
        card = store.get("CardTemplate", card_guid)
        if card is None:
            raise KeyError(f"unknown card template {card_guid}")
        graphs = card_ability_graphs(store, card_guid)
        abilities = tuple(AbilityInstance.from_graph(
            graph, source_uid=source_uid, owner_id=owner_id,
            responsible_player_id=owner_id, store=store)
            for graph in graphs)
        cost = CardPlayCost(
            resource=_int(_field(card, "m_ResourceCost")),
            variable=bool(_int(_field(card, "m_VariableCost"))),
            variable_minimum=_int(_field(card, "m_VariableCostMinimum")),
            life=_int(_field(card, "m_LifeCost")),
            threshold=_field(card, "m_Threshold"),
            additional_cost_targets=tuple(
                getattr(card, "additional_cost_targets", ())),
        )
        return cls(card, cost, abilities, source_uid, owner_id, store)

    @property
    def cost_instances(self) -> tuple[dict[str, Any], ...]:
        """Return client ``CostInstance`` descriptors for card-level costs."""
        result = []
        for index, (kind, guid) in enumerate(self.cost.additional_cost_targets):
            target = (self.store.get("AbilityTargetTemplate", guid)
                      if self.store is not None else None)
            minimum = int(target.minimum) if target is not None else 1
            maximum = int(target.maximum) if target is not None else 1
            if maximum <= 0:
                maximum = -1
            result.append({
                "index": index,
                "kind": kind,
                "target_guid": guid,
                "cost_type": self.cost.cost_type(kind),
                "minimum": max(0, minimum),
                "maximum": maximum,
                "auto": bool(target and not target.target_spec.requires_input),
            })
        return tuple(result)

    def activation_bundle(self, target_uids: Any = None, *,
                          x_cost: int | None = None,
                          option_map: Mapping[int, int] | None = None,
                          variables: Mapping[str, Any] | None = None
                          ) -> tuple[dict[str, ActivationData], dict[int, tuple[int, ...]]]:
        """Bind one client card-play selection to every affected ability.

        The client submits card-play selections as one activation payload, but
        the effects that consume those selections belong to separate ability
        graphs.  This method partitions the selected card IDs by the authored
        additional-cost fields first, then binds the remaining IDs to the
        authored explicit target-template indexes.  It is deliberately based
        on graph shape, never on card names or localized text.

        Returns ``(ability activations, card-cost target map)``.  The latter is
        keyed by the order of ``CardTemplate`` additional-cost target fields.
        """
        values = []
        raw_values = target_uids if isinstance(target_uids, (list, tuple, set)) \
            else ([] if target_uids is None else [target_uids])
        for value in raw_values:
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                continue

        cost_target_map: dict[int, tuple[int, ...]] = {}
        offset = 0
        for index, (_kind, _guid) in enumerate(self.cost.additional_cost_targets):
            # Singular card cost fields are represented by one target in the
            # client XCostData.  Plural fields are already expanded into one
            # descriptor per authored target by CardTemplate.
            spec = self.cost_instances[index]
            if spec["auto"]:
                continue
            count = max(1, int(spec["minimum"]))
            maximum = int(spec["maximum"])
            if maximum >= 0:
                count = min(count, maximum)
            selected = values[offset:offset + count]
            if selected:
                cost_target_map[index] = tuple(selected)
                offset += len(selected)

        slots = []
        # Card-play activations are the non-triggered graphs.  ``manual`` is
        # the client flag for a separately activated warzone ability, so it
        # must not exclude a spell's ordinary on-cast graph here.
        play_abilities = tuple(ability for ability in self.abilities
                               if not ability.is_triggered)
        for ability in play_abilities:
            for index in ability.referenced_target_indexes:
                if index >= len(ability.targets):
                    continue
                target = ability.targets[index]
                if target.requires_input:
                    slots.append((ability, index, target))

        remaining = values[offset:]
        assignments: dict[str, dict[int, tuple[int, ...]]] = {}
        cursor = 0
        for slot_index, (ability, index, target) in enumerate(slots):
            left = len(remaining) - cursor
            if left <= 0:
                break
            max_count = int(target.maximum or 0)
            if max_count <= 0:
                max_count = left
            if len(slots) == 1:
                count = min(left, max_count)
            else:
                required_after = sum(
                    max(0, int(other.minimum or 0))
                    for _a, _i, other in slots[slot_index + 1:])
                count = min(left - required_after, max_count)
                count = max(count, min(left, int(target.minimum or 0)))
            if count <= 0:
                continue
            assignments.setdefault(ability.ability_guid.lower(), {})[index] = \
                tuple(remaining[cursor:cursor + count])
            cursor += count

        activations = {}
        for ability in play_abilities:
            bound = assignments.get(ability.ability_guid.lower(), {})
            activations[ability.ability_guid.lower()] = ActivationData.from_values(
                target_map=bound,
                option_map=option_map,
                variables=variables,
                x_cost=x_cost,
                cost_target_map=cost_target_map,
            )
        return activations, cost_target_map

    @property
    def manual_abilities(self) -> tuple[AbilityInstance, ...]:
        """Abilities activated while this card is being played.

        ``m_Manual`` describes a warzone power, not whether an ability is part
        of the card-cast activation.  A card's ordinary on-play graph is often
        non-manual, so excluding it here would omit its target and option
        prompts from the play plan.
        """
        return tuple(ability for ability in self.abilities
                     if not ability.is_triggered)

    @property
    def triggered_abilities(self) -> tuple[AbilityInstance, ...]:
        return tuple(ability for ability in self.abilities if ability.is_triggered)

    def prompts(self, activations: Mapping[str, ActivationData] | None = None,
                *, cost_target_map: Any = None) -> tuple[PromptSpec, ...]:
        """Return the remaining prompts after applying supplied activations."""
        activations = {str(key).lower(): value
                       for key, value in (activations or {}).items()}
        prompts = []
        for ability in self.manual_abilities:
            bound = activations.get(ability.ability_guid.lower())
            if bound is None:
                bound = ability.activation
            if bound is ability.activation:
                prompts.extend(ability.required_prompts())
            else:
                prompts.extend(AbilityInstance.from_graph(
                    ability.graph, source_uid=ability.source_uid,
                    owner_id=ability.owner_id, activation=bound,
                    store=self.store).required_prompts())
        cost_targets = _normalise_targets(cost_target_map)
        for index, (kind, guid) in enumerate(self.cost.additional_cost_targets):
            spec = self.cost_instances[index]
            if spec["auto"]:
                continue
            if index not in cost_targets:
                prompts.append(PromptSpec(
                    kind="additional_cost", index=index, ability_guid="",
                    template_guid=guid, label=kind.replace("_", " "),
                    cost_kind=kind,
                ))
        return tuple(prompts)

    @property
    def required_prompts(self) -> tuple[PromptSpec, ...]:
        return self.prompts()

    def validate(self, *, resources_available: int | None = None,
                 variable_cost: int | None = None,
                 life_available: int | None = None,
                 free: bool = False,
                 activations: Mapping[str, ActivationData] | None = None,
                 cost_target_map: Any = None) -> tuple[str, ...]:
        errors: list[str] = []
        chosen_cost = self.cost.resource
        if self.cost.variable:
            chosen_cost = variable_cost if variable_cost is not None else (
                self.cost.variable_minimum)
            if chosen_cost < self.cost.variable_minimum:
                errors.append("variable card cost is below its minimum")
        if not free and resources_available is not None and resources_available < chosen_cost:
            errors.append(f"card costs {chosen_cost} resources; only {resources_available} available")
        if not free and life_available is not None and life_available < self.cost.life:
            errors.append(f"card costs {self.cost.life} life; only {life_available} available")
        activation_map = {str(key).lower(): value
                          for key, value in (activations or {}).items()}
        supplied_cost_targets = _normalise_targets(cost_target_map)
        if not supplied_cost_targets:
            for activation in activation_map.values():
                if isinstance(activation, ActivationData):
                    supplied_cost_targets.update(activation.cost_target_map)
        for index, spec in enumerate(self.cost_instances):
            if spec["auto"]:
                continue
            selected = supplied_cost_targets.get(index)
            if selected is None:
                errors.append(f"missing additional-cost input at index {index}")
                continue
            minimum = int(spec["minimum"])
            maximum = int(spec["maximum"])
            if len(selected) < minimum:
                errors.append(
                    f"additional cost {index} needs at least {minimum} selections")
            if maximum >= 0 and len(selected) > maximum:
                errors.append(
                    f"additional cost {index} has {len(selected)} selections; "
                    f"maximum is {maximum}")
        for ability in self.manual_abilities:
            bound = activation_map.get(ability.ability_guid.lower())
            if bound is None:
                bound = ability.activation
            errors.extend(AbilityInstance.from_graph(
                ability.graph, source_uid=ability.source_uid,
                owner_id=ability.owner_id, activation=bound,
                store=self.store).validate_activation())
        if self.prompts(activation_map, cost_target_map=supplied_cost_targets):
            errors.append("card play is awaiting activation data")
        return tuple(errors)

    @property
    def playable(self) -> bool:
        return not self.validate()
