"""Port-facing ability instances backed by the Records-derived mechanics graph.

``gamedata.play_plan.AbilityInstance`` already models the client constructor's
immutable metadata portion (template, target map, options and ordered effect
instances).  This wrapper adds the C# session-owned identity/chain fields
without duplicating that metadata or parsing card text.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Mapping
from typing import Any, Callable, Optional

from gamedata.play_plan import AbilityInstance as MetadataAbilityInstance
from gamedata.play_plan import ActivationData


@dataclass(frozen=True)
class AbilityContinuation:
    """JSON-safe RulesPort checkpoint for a paused ability."""

    ability_guid: str
    source_uid: int | None
    owner_id: int
    target_map: dict[str, Any]
    variables: dict[str, Any]
    resume_effect_order: int = 0

    @classmethod
    def from_state(cls, state=None, *, ability_guid=None, source_uid=None,
                   owner_id=None, target_map=None, variables=None,
                   resume_effect_order=None):
        state = state or {}
        if ability_guid is None:
            ability_guid = state.get("resolving_ability", "")
        if source_uid is None:
            source_uid = state.get("resolving_source_uid")
        if owner_id is None:
            owner_id = state.get("resolving_owner_id", 0)
        if target_map is None:
            target_map = state.get("ability_target_map") or {}
        if variables is None:
            variables = state.get("ability_variables") or {}
        if resume_effect_order is None:
            resume_effect_order = int(
                state.get("resolving_effect_order", 0) or 0) + 1
        return cls(
            ability_guid=str(ability_guid or "").lower(),
            source_uid=(int(source_uid) if source_uid is not None else None),
            owner_id=int(owner_id or 0),
            target_map={str(key): value for key, value in
                        dict(target_map or {}).items()},
            variables=dict(variables or {}),
            resume_effect_order=int(resume_effect_order or 0))

    def to_dict(self):
        import json
        value = {
            "ability_guid": self.ability_guid,
            "source_uid": self.source_uid if self.source_uid is not None else 0,
            "owner_id": self.owner_id,
            "target_map": dict(self.target_map),
            "variables": dict(self.variables),
            "resume_effect_order": self.resume_effect_order,
        }
        json.dumps(value)
        return value

def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dict__"):
        return {str(key): _json_safe(item) for key, item in value.__dict__.items()
                if not str(key).startswith("_")}
    return str(value)


@dataclass
class AbilityInstance:
    """Python counterpart of ``Game.Shared.Mechanics.Abilities.AbilityInstance``.

    The C# object combines static template data with mutable session identity.
    ``metadata`` is the existing Records-backed, typed implementation; this
    class only supplies runtime facts that belong to the session/chain.
    """

    instance_id: int
    metadata: MetadataAbilityInstance
    parent_instance_id: int = 0
    activating_player_id: int | None = None
    responsible_player_id: int | None = None
    trigger_event: object | None = None
    paid: bool = False
    free: bool = False
    resolving: bool = False

    @property
    def ability_template_id(self) -> str:
        return self.metadata.ability_guid

    @property
    def source_uid(self) -> int | None:
        return self.metadata.source_uid

    @property
    def activation(self) -> ActivationData:
        return self.metadata.activation

    @property
    def ordered_effects(self):
        return self.metadata.ordered_effects

    @property
    def effect_groups(self):
        return self.metadata.effect_groups

    @property
    def ignores_chain(self) -> bool:
        graph = self.metadata.graph
        return bool(graph and getattr(graph, "ignores_chain", False))

    @property
    def is_triggered(self) -> bool:
        return self.metadata.is_triggered

    def needs_activation_data(self):
        return self.metadata.required_prompts()

    def validate_activation(self, **available) -> tuple[str, ...]:
        return self.metadata.validate_activation(**available)

    def value(self, db, bstate, field_name: str, *, effect=None,
              default: int = 0) -> int:
        """Read a typed numeric field from the active Records effect."""
        from .fields import effect_field
        return effect_field(self, db, bstate,
                            str(effect or self.ability_template_id),
                            field_name, default)

    def template_value(self, db, bstate, field_name: str, *, effect=None,
                       default=None):
        """Read a typed non-numeric field from the active Records effect."""
        from .fields import effect_template_value
        return effect_template_value(
            self, str(effect or self.ability_template_id), field_name, default)

    def bind_activation(self, value: Mapping[str, Any] | ActivationData) -> bool:
        """Apply one client ``SetAbilityActivationData`` payload.

        The C# session creates an ability before it asks the client for
        targets, then updates that same instance when the response arrives.
        Replacing its typed activation object preserves that identity and lets
        the next scheduler update request any later option/cost choice.
        """
        if not isinstance(value, (Mapping, ActivationData)):
            return False
        previous = self.metadata.activation
        self.metadata.activation = ActivationData.from_dict(value)
        # Mono's ``AbilityActivationData`` serializer flattens additional
        # cost targets (sacrifice/exhaust/discard) into ``TargetMap`` for
        # several card abilities, notably Hideous Conversion.  Preserve the
        # labelled target index in the dedicated cost map before prompt
        # validation; otherwise the port keeps requesting an empty sacrifice
        # prompt indefinitely even though the client supplied the troop.
        cost_targets = getattr(self.metadata.graph, "additional_cost_targets", ()) \
            if self.metadata.graph is not None else ()
        if cost_targets and not self.metadata.activation.cost_target_map:
            inferred = {
                index: selected
                for index, _cost in enumerate(cost_targets)
                for selected in (self.metadata.activation.target_map.get(index,
                              self.metadata.activation.target_map.get(str(index))),)
                if selected
            }
            # A few Mono serializers omit the target-template index entirely
            # when there is one explicit effect target and one additional
            # cost target.  In that shape the sole submitted selection is the
            # cost selection; preserve it instead of reopening an empty
            # sacrifice prompt.
            if (not inferred and len(cost_targets) == 1 and
                    len(self.metadata.activation.target_map) == 1):
                inferred = {0: next(iter(
                    self.metadata.activation.target_map.values()))}
            if inferred:
                self.metadata.activation = replace(
                    self.metadata.activation, cost_target_map=inferred)
        errors = self.validate_activation()
        # Missing fields are expected during a multi-dialog interaction.
        # Everything else is malformed or no longer legal input.
        if any(not error.startswith("missing ") for error in errors):
            self.metadata.activation = previous
            return False
        return True

    def continuation(self, *, resume_effect_order: int = 0,
                     variables: Optional[dict[str, Any]] = None,
                     source_uid: int | None = None,
                     owner_id: int | None = None,
                     target_map: dict[str, Any] | None = None) -> dict[str, Any]:
        """JSON-safe state for a later client transaction, never a coroutine."""
        activation = self.activation.as_dict()
        activation["target_map"] = dict(target_map or self.activation.target_map)
        activation["variables"] = dict(variables or self.activation.variables)
        return {
            "ability_instance_id": self.instance_id,
            "ability_guid": self.ability_template_id,
            "source_uid": (self.source_uid if source_uid is None
                            else int(source_uid)),
            "owner_id": (self.responsible_player_id if owner_id is None
                          else int(owner_id)),
            "parent_instance_id": self.parent_instance_id,
            "activation": activation,
            "resume_effect_order": int(resume_effect_order),
            "variables": dict(variables or {}),
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialize mutable C# identity/lifecycle fields for reconnects."""
        value = self.continuation()
        value.update({"activating_player_id": self.activating_player_id,
                      "paid": bool(self.paid), "free": bool(self.free),
                      "resolving": bool(self.resolving),
                      "trigger_event": _json_safe(self.trigger_event)})
        return value


class AbilityFactory:
    """Allocate session instance IDs around the existing metadata builder."""

    def __init__(self, start_instance_id: int = 1) -> None:
        self._next_instance_id = int(start_instance_id)

    def create(self, metadata: MetadataAbilityInstance, *,
               activating_player_id: int | None = None,
               responsible_player_id: int | None = None,
               parent_instance_id: int = 0, trigger_event=None) -> AbilityInstance:
        instance_id = self._next_instance_id
        self._next_instance_id += 1
        return AbilityInstance(
            instance_id=instance_id,
            metadata=metadata,
            parent_instance_id=int(parent_instance_id),
            activating_player_id=activating_player_id,
            responsible_player_id=(responsible_player_id
                                   if responsible_player_id is not None
                                   else activating_player_id),
            trigger_event=trigger_event,
        )
