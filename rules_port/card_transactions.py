"""Typed card-transaction boundary for the RulesPort session.

Validation and ordering belong to ``AuthoritativeSession``.  The mode host
supplies the actual domain mutation through ``apply`` while each mutation is
being moved out of the compatibility dispatcher.  The explicit object keeps
malformed typed payloads from reaching a legacy fallback implicitly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from .resources import ResourceChange


CARD_TRANSACTION_KINDS = frozenset({
    "play_resource", "play_troop", "play_artifact", "play_spell",
    "play_champion", "activate_ability",
})


def automatic_instance_ability_guids(db, session_id, card_uid,
                                     ability_guids=()):
    """Include automatic abilities currently granted to this card instance.

    The static Records card graph does not contain runtime-granted keywords
    such as Runic. C# ``CastSpell`` calls ``CreateAutomaticAbilities`` on the
    live card, so the cast graph must also read the card instance's serialized
    ability list and retain only abilities that C# classifies as automatic.
    """
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from pvp_db import db_card_ability_payload

    result = [str(guid).lower() for guid in (ability_guids or ()) if guid]
    seen = set(result)
    try:
        payload = db_card_ability_payload(
            int(session_id), int(card_uid), conn=db)
        values = json.loads(payload or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return result
    if not isinstance(values, list):
        return result

    for value in values:
        if isinstance(value, Mapping):
            value = (value.get("guid") or value.get("m_Guid") or
                     value.get("ability_guid"))
        if not isinstance(value, str):
            continue
        guid = value.lower()
        if not guid or guid in seen:
            continue
        graph = ability_graph(DEFAULT_RECORD_STORE, guid)
        if graph is None:
            continue
        # Mirrors AbilityTemplate.IsAutomatic: !Manual, no trigger, and no
        # explicit ability index.
        if (graph.manual or graph.trigger_event_type or
                graph.trigger_condition or int(graph.ability_index) >= 0):
            continue
        result.append(guid)
        seen.add(guid)
    return result


@dataclass(frozen=True)
class CardPlayTransition:
    card_uid: int
    owner_id: int
    payment: int
    resource_change: ResourceChange


def apply_card_play(db, session_id, battle_state, card_uid, owner_id, payment,
                    *, destination="CastSpells", position=None,
                    expected_location="hand"):
    """Apply the shared state part of a typed card play.

    Legality and effect/chain ordering are handled by RulesPort callers. This
    one transition owns the resource deduction and hand-to-chain zone move so
    AI and client-submitted plays cannot diverge in those mutations.
    """
    from .resources import apply_resource_change
    from .zone_effects import move_card_to_zone

    uid = int(card_uid)
    owner = int(owner_id or 0)
    payment = int(payment or 0)
    if position is None and str(destination).lower() == "castspells":
        from domain.constants import PLAYED_CARD_POSITION
        position = PLAYED_CARD_POSITION
    side = "player" if owner else "ai"
    change = apply_resource_change(
        battle_state, side, "currentresource", -payment)
    if not move_card_to_zone(
            db, session_id, uid, destination, owner_id=owner,
            expected_location=expected_location, position=position):
        # The resource change must not survive a failed hand transition.
        apply_resource_change(battle_state, side, "currentresource", payment)
        return None
    return CardPlayTransition(uid, owner, payment, change)


def apply_card_play_for_player(db, session_id, battle_state, card_uid,
                               owner_id, payment, *,
                               destination="CastSpells", position=None,
                               expected_location="hand"):
    """Apply payment and hand-to-chain movement for a raw PvP player ID."""
    from .resources import pay_resource_for_player
    from .zone_effects import move_card_to_zone

    uid = int(card_uid)
    owner = int(owner_id)
    payment = int(payment or 0)
    if position is None and str(destination).lower() == "castspells":
        from domain.constants import PLAYED_CARD_POSITION
        position = PLAYED_CARD_POSITION
    resource_key = f"res_{owner}"
    old_resources = int(battle_state.get(resource_key, 0) or 0)
    change = pay_resource_for_player(battle_state, owner, payment)
    if not move_card_to_zone(
            db, session_id, uid, destination, owner_id=owner,
            expected_location=expected_location, position=position):
        battle_state[resource_key] = old_resources
        return None
    return CardPlayTransition(uid, owner, payment, change)


class ResourceTransactionExecutor:
    """Validate and dispatch the resource-play domain mutation only."""

    def __init__(self, apply: Callable[[object], bool]) -> None:
        if not callable(apply):
            raise TypeError("resource transaction apply callback must be callable")
        self.apply = apply

    def __call__(self, transaction) -> bool:
        if getattr(transaction, "kind", None) != "play_resource":
            return False
        payload = getattr(transaction, "payload", None)
        if not isinstance(payload, Mapping) or payload.get("card_id") is None:
            return False
        return bool(self.apply(transaction))


class CardTransactionExecutor:
    """Dispatch one normalized card transaction exactly once."""

    def __init__(self, apply: Callable[[str, object], bool]) -> None:
        if not callable(apply):
            raise TypeError("card transaction apply callback must be callable")
        self.apply = apply

    def __call__(self, kind: str, transaction) -> bool:
        kind = str(kind)
        if kind not in CARD_TRANSACTION_KINDS:
            return False
        payload = getattr(transaction, "payload", None)
        if not isinstance(payload, Mapping):
            return False
        if kind == "activate_ability":
            if (payload.get("source_card_id") is None or
                    not payload.get("ability_template_id") or
                    payload.get("activation_data") is None):
                return False
        elif payload.get("card_id") is None:
            return False
        return bool(self.apply(kind, transaction))


class MetadataCardTransactionExecutor(CardTransactionExecutor):
    """RulesPort card executor for Records-backed ability activations.

    Card-zone mutations are injected as a host projection. The ability
    instance lifecycle itself is owned here: graph lookup, typed
    activation binding, registration and chain action creation no longer live
    in the HConnect transport handler.
    """

    def __init__(self, port, *, graph_loader, owner_id, store=None,
                 projection=None, compatibility=None,
                 resource_compatibility=None, owner_id_resolver=None,
                 activation_compatibility=None, play_plan_loader=None,
                 activation_event=None) -> None:
        self.port = port
        self.graph_loader = graph_loader
        self.owner_id = int(owner_id or 0)
        self.owner_id_resolver = owner_id_resolver
        self.activation_compatibility = activation_compatibility
        self.play_plan_loader = play_plan_loader
        self.activation_event = activation_event
        self.store = store
        # ``compatibility`` remains an import-level alias for older callers;
        # new wiring uses the explicit host projection name.
        self.projection = projection if projection is not None else compatibility
        self.compatibility = self.projection
        self.resource_compatibility = (
            ResourceTransactionExecutor(resource_compatibility)
            if resource_compatibility is not None else None)

    def __call__(self, kind: str, transaction) -> bool:
        kind = str(kind)
        payload = getattr(transaction, "payload", {}) or {}
        if kind != "activate_ability":
            if (self.play_plan_loader is not None and
                    kind in {"play_troop", "play_artifact", "play_spell",
                             "play_champion"}):
                card_id = payload.get("card_id")
                card_uid = getattr(card_id, "uid64", card_id)
                if card_uid is None:
                    return False
                card = self.port.get_card(card_id)
                if card is None:
                    return False
                owner_id = (self.owner_id_resolver(transaction)
                            if callable(self.owner_id_resolver)
                            else self.owner_id)
                if owner_id is None:
                    return False
                try:
                    plan = self.play_plan_loader(
                        card.template_guid, int(cast(Any, card_uid)),
                        int(cast(Any, owner_id)))
                    values = []
                    cost_target_map: dict[int, list[int]] = {}
                    for activation in payload.get("ability_data") or ():
                        if not isinstance(activation, Mapping):
                            continue
                        target_map = activation.get("target_map", {}) or {}
                        for selected in target_map.values():
                            selected = selected if isinstance(
                                selected, (list, tuple, set)) else (selected,)
                            values.extend(int(getattr(value, "uid64", value))
                                           for value in selected)
                        # Additional card costs arrive separately from
                        # TargetMap (XCostData.CardsToSacrifice).  Keep them
                        # distinct so the cost never consumes an effect target
                        # (Abominate).
                        for index, selected in (activation.get(
                                "cost_target_map") or {}).items():
                            try:
                                index = int(index)
                            except (TypeError, ValueError):
                                continue
                            selected = selected if isinstance(
                                selected, (list, tuple, set)) else (selected,)
                            cost_target_map.setdefault(index, []).extend(
                                int(getattr(value, "uid64", value))
                                for value in selected)
                    x_cost = max((int(item.get("x_cost", 0) or 0)
                                  for item in (payload.get("ability_data") or ())
                                  if isinstance(item, Mapping)), default=0)
                    activations, cost_map = plan.activation_bundle(
                        values, x_cost=x_cost,
                        cost_target_map=cost_target_map or None)
                    if plan.validate(
                            variable_cost=x_cost,
                            activations=activations,
                            cost_target_map=cost_map,
                            free=bool(payload.get("playing_for_free", False))):
                        return False
                except (TypeError, ValueError, KeyError):
                    return False
            if (kind == "play_resource" and
                    self.resource_compatibility is not None):
                return bool(self.resource_compatibility(transaction))
            if self.projection is None:
                return False
            return bool(self.projection(kind, transaction))
        if (payload.get("source_card_id") is None or
                not payload.get("ability_template_id") or
                payload.get("activation_data") is None):
            return False
        graph = self.graph_loader(str(payload["ability_template_id"]))
        if graph is None:
            return False
        facts = getattr(self.port, "runtime_facts", None)
        exhausted = getattr(facts, "champion_ability_uses_exhausted", None)
        if callable(exhausted) and exhausted(int(payload["source_card_id"]),
                                             graph):
            # Enforce authored per-game and per-turn limits before paying or
            # queuing a champion power activation.
            return False
        if (getattr(graph, "additional_cost_targets", ()) and
                self.activation_compatibility is not None):
            return bool(self.activation_compatibility(transaction))
        from gamedata import AbilityInstance as MetadataAbility
        from rules_port.abilities import AbilityInstance as PortAbility
        owner_id = self.owner_id
        if callable(self.owner_id_resolver):
            try:
                owner_id = int(cast(
                    Any, self.owner_id_resolver(transaction)))
            except (TypeError, ValueError):
                return False
        metadata = MetadataAbility.from_graph(
            graph, source_uid=int(payload["source_card_id"]),
            owner_id=owner_id,
            responsible_player_id=transaction.player_id,
            store=self.store)
        can_pay = getattr(self.port, "can_pay_ability_cost", None)
        if callable(can_pay) and not can_pay(metadata):
            return False
        ability = PortAbility(
            instance_id=int(payload.get("ability_instance_id") or
                            (len(self.port.ability_manager._instances) + 1)),
            metadata=metadata,
            activating_player_id=transaction.player_id,
            responsible_player_id=transaction.player_id)
        # Bind through the port wrapper so Mono's flattened TargetMap also
        # populates additional-cost targets (Hideous Conversion sacrifice).
        if payload.get("activation_data") and not ability.bind_activation(
                payload["activation_data"]):
            return False
        # Keep the activation on the same durable projected-chain boundary as
        # host/AI card abilities.  A live client commonly sends the response
        # pass on a fresh GameSession wrapper; persisting only the native
        # ability instance ID leaves reconnect with no ability object to
        # resolve and silently drops the chain item.
        if not self.port.pay_ability_cost(ability):
            return False
        consume = getattr(facts, "consume_champion_ability_use", None)
        if callable(consume):
            consume(int(payload["source_card_id"]), graph)
        consume_card = getattr(facts, "consume_card_ability_use", None)
        if callable(consume_card):
            consume_card(int(payload["source_card_id"]), graph)
        activation = ability.activation.as_dict()
        descriptor = {
            "kind": "ability",
            "source_uid": int(payload["source_card_id"]),
            "ability_guid": str(payload["ability_template_id"]).lower(),
            "instance_id": int(ability.instance_id),
            "activation_data": activation,
        }
        self.port.queue_projected_chain(
            descriptor, transaction.player_id,
            first_player_id=transaction.player_id)
        sink = getattr(self.port, "event_sink", None)
        if sink is not None:
            sink.ability_pushed_on_chain(ability)
            priority = self.port.action_stack.priority_player_id
            if priority is not None:
                sink.green_light(priority)
        if callable(self.activation_event):
            self.activation_event(ability, transaction)
        return True
