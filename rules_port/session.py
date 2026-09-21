"""Authoritative Python session host derived from the C# session tick loop.

This is intentionally an opt-in adapter. Once attached, its scheduler
snapshot is stored under ``turn_order['rules_port']`` in the same mutable
checkpoint consumed by the compatibility host; there is no second battle
state dictionary.

Source counterparts: ``Session.cs:InternalTick2`` and
``AuthoritativeSessionBase.cs:Tick``.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Mapping, Optional

import game_engine
from domain.constants import CARD_UID_TYPE
from domain.events import (PlayerWishesToDrawFirstSessionEventArgs,
                           PlayerWishesToPlayFirstSessionEventArgs)

from .kernel import (AbilityRegistry, Chain, GameAction, GameActionStack,
                     MultiplyWithCarryRng, PriorityWindowAction,
                     TurnPhasePlayers)
from .phases import phase_name, require_legal_transition
from .transactions import (AbilityExistsRequirement, AllDamageAssignedRequirement,
                           AttackExistsRequirement, AttackerIsValidRequirement,
                           AbilityCanBeActivatedRequirement,
                           AbilityHasValidTargetsRequirement,
                           CardCanBePlayedRequirement, CardInCollectionRequirement,
                           CardTypeRequirement,
                           AbilityHasTriggerRequirement,
                           AbilityIsManuallyActivatedRequirement,
                           PlayerIsAtFrontOfTriggeredAbilityQueueRequirement,
                           AbilitiesAreTriggeredRequirement,
                           DefenseDeclarationsLegalRequirement, Requirement,
                           MainPhaseRequirement, PriorityWindowRequirement,
                           QuickActionCardRequirement,
                           OrRequirement,
                           PlayerHasPriorityRequirement,
                           PlayerIsActiveRequirement,
                           PlayerIsResponsibleForAbilityRequirement,
                           NotRequirement,
                           TurnPhaseRequirement, XCostRequirement)
from .turn_states import default_phase_states
from .actions import AbilityResolutionState, ResolveTopOfChainAction
from .combat import CombatFlags, CombatId, CombatManager, CombatPhase
from .projections import TransactionProjection


def _uid_value(value) -> int:
    """Serialize Python UID wrappers like the C# ``UID`` value type."""
    return int(getattr(value, "uid64", value))


def _same_player_uid(left, right) -> bool:
    """Compare participant identities across raw/typed UID and plain forms.

    Test and focused fixtures use plain string participants; ``_uid_value``
    cannot coerce those, so fall back to ordinary equality instead of raising.
    """
    try:
        return _uid_value(left) == _uid_value(right)
    except (TypeError, ValueError):
        return left == right


def _serial_id(value):
    try:
        return _uid_value(value)
    except (TypeError, ValueError):
        return str(value)


def _json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: _json_value(item) for key, item in value.__dict__.items()
                if not key.startswith("_")}
    return str(value)


@dataclass(frozen=True)
class RulesTransaction:
    """Normalized server intent; never a client-provided state snapshot."""

    player_id: object
    kind: str
    phase: object | None
    payload: Mapping[str, Any] = field(default_factory=dict)
    requirements: tuple[Requirement, ...] = ()

    def validate(self, session) -> bool:
        return all(requirement.is_valid(session, self.player_id)
                   for requirement in self.requirements)


    @classmethod
    def pass_priority(cls, player_id, phase) -> "RulesTransaction":
        """C# ``PassPriorityTransaction.Create`` validation contract."""
        return cls(player_id, "pass_priority", phase, requirements=(
            TurnPhaseRequirement(phase),
            PlayerHasPriorityRequirement(player_id),
        ))

    @classmethod
    def choose_play_first(cls, player_id, phase) -> "RulesTransaction":
        return cls(player_id, "choose_play_first", phase, requirements=(
            PlayerHasPriorityRequirement(player_id),))

    @classmethod
    def choose_draw_first(cls, player_id, phase) -> "RulesTransaction":
        return cls(player_id, "choose_draw_first", phase, requirements=(
            PlayerHasPriorityRequirement(player_id),))

    @classmethod
    def set_auto_pass(cls, player_id, as_active: bool, passing_state) -> "RulesTransaction":
        return cls(player_id, "set_auto_pass", None,
                   payload={"as_active": bool(as_active),
                            "passing_state": passing_state})

    @classmethod
    def cancel_auto_pass(cls, player_id) -> "RulesTransaction":
        return cls(player_id, "cancel_auto_pass", None)

    @classmethod
    def request_priority_sync(cls, player_id) -> "RulesTransaction":
        return cls(player_id, "request_priority_sync", None,
                   requirements=(PlayerHasPriorityRequirement(player_id),))

    @classmethod
    def discard(cls, player_id, card_id) -> "RulesTransaction":
        return cls(player_id, "discard", game_engine.ETurnPhases.Discard,
                   payload={"card_id": card_id}, requirements=(
                       CardInCollectionRequirement(
                           player_id, game_engine.ECardCollections.Hand, card_id),))

    @classmethod
    def play_champion(cls, player_id, card_id) -> "RulesTransaction":
        return cls(player_id, "play_champion", game_engine.ETurnPhases.StartGame,
                   payload={"card_id": card_id}, requirements=(
                       CardInCollectionRequirement(
                           player_id, game_engine.ECardCollections.Hand, card_id),
                       CardTypeRequirement(card_id, game_engine.ECardTypes.Champion),
                       TurnPhaseRequirement(game_engine.ETurnPhases.StartGame)))

    @classmethod
    def quit_game(cls, player_id, was_bugged=False, quit_entire_series=False) -> "RulesTransaction":
        from .transactions import PlayerIsNotEliminatedRequirement
        return cls(player_id, "quit_game", None,
                   payload={"was_bugged": bool(was_bugged),
                            "quit_entire_series": bool(quit_entire_series)},
                   requirements=(PlayerIsNotEliminatedRequirement(player_id),))

    @classmethod
    def set_turn_phases(cls, player_id, self_phases, opponent_phases) -> "RulesTransaction":
        return cls(player_id, "set_turn_phases", None, payload={
            "self_phases": tuple(self_phases or ()),
            "opponent_phases": tuple(opponent_phases or ()),
        })

    @classmethod
    def request_player_options(cls, player_id) -> "RulesTransaction":
        return cls(player_id, "request_player_options", None)

    @classmethod
    def send_state_checksum(cls, player_id, checksum_data) -> "RulesTransaction":
        return cls(player_id, "send_state_checksum", None,
                   payload={"checksum_data": checksum_data})

    @classmethod
    def tip_window_closed(cls, player_id) -> "RulesTransaction":
        return cls(player_id, "tip_window_closed", None)

    @classmethod
    def encounter_mod_dialog(cls, player_id, conversation_id) -> "RulesTransaction":
        return cls(player_id, "encounter_mod_dialog", None,
                   payload={"conversation_id": conversation_id})

    @classmethod
    def ready_card(cls, player_id, card_ids) -> "RulesTransaction":
        """C# ``ReadyCardTransaction`` validation contract."""
        ids = tuple(card_ids or ())
        from .transactions import CardCanUntapRequirement, CardTappedRequirement
        requirements = [TurnPhaseRequirement(game_engine.ETurnPhases.Ready),
                        PlayerIsActiveRequirement(player_id)]
        for card_id in ids:
            requirements.extend((CardTappedRequirement(card_id),
                                 CardCanUntapRequirement(card_id),
                                 CardInCollectionRequirement(
                                     player_id, game_engine.ECardCollections.Warzone,
                                     card_id)))
        return cls(player_id, "ready_card", game_engine.ETurnPhases.Ready,
                   payload={"card_ids": ids}, requirements=tuple(requirements))

    @classmethod
    def accept_starting_hand(cls, player_id) -> "RulesTransaction":
        return cls(player_id, "accept_starting_hand", game_engine.ETurnPhases.Mulligan,
                   requirements=(TurnPhaseRequirement(game_engine.ETurnPhases.Mulligan),
                                 PlayerHasPriorityRequirement(player_id)))

    @classmethod
    def mulligan(cls, player_id) -> "RulesTransaction":
        return cls(player_id, "mulligan", game_engine.ETurnPhases.Mulligan,
                   requirements=(TurnPhaseRequirement(game_engine.ETurnPhases.Mulligan),
                                 PlayerHasPriorityRequirement(player_id)))

    @classmethod
    def set_ability_activation_data(cls, player_id, ability_instance_id,
                                    activation_data) -> "RulesTransaction":
        return cls(player_id, "set_ability_activation_data", None,
                   payload={"ability_instance_id": int(ability_instance_id),
                            "activation_data": dict(activation_data or {})},
                   requirements=(
                       # Exact C# transaction contract: this may update an
                       # existing responsible ability outside one particular
                       # prompt type, so it intentionally has no phase gate.
                       AbilityExistsRequirement(ability_instance_id),
                       PlayerIsResponsibleForAbilityRequirement(
                           player_id, ability_instance_id),
                   ))

    @classmethod
    def resolve_choice_continuation(cls, player_id, activation_data) -> "RulesTransaction":
        """Resume a metadata choice prompt that has no port AbilityInstance."""
        return cls(player_id, "resolve_choice_continuation", None,
                   payload={"activation_data": dict(activation_data or {})})

    @classmethod
    def assign_damage_order(cls, player_id, phase, assignments) -> "RulesTransaction":
        """Port ``AssignDamageOrderTransaction`` payload and requirements."""
        normalized = tuple((combat_id, None if card_ids is None else tuple(card_ids))
                           for combat_id, card_ids in assignments)
        requirements: list[Requirement] = [
            OrRequirement((TurnPhaseRequirement(game_engine.ETurnPhases.AssignDamage),
                           TurnPhaseRequirement(
                               game_engine.ETurnPhases.AssignFirstStrikeDamage)))
        ]
        for combat_id, card_ids in normalized:
            if card_ids is not None:
                requirements.extend((AttackExistsRequirement(combat_id),
                                     AllDamageAssignedRequirement(combat_id,
                                                                 card_ids)))
        return cls(player_id, "assign_damage_order", phase,
                   payload={"assignments": normalized},
                   requirements=tuple(requirements))

    @classmethod
    def resolve_triggered_continuation(cls, player_id, activation_data) -> "RulesTransaction":
        """Resume a metadata trigger target without a transient ability id."""
        return cls(player_id, "resolve_triggered_continuation", None,
                   payload={"activation_data": dict(activation_data or {})})

    @classmethod
    def resolve_discard_continuation(cls, player_id, activation_data) -> "RulesTransaction":
        """Resume a class-23 discard checkpoint from its persisted prompt."""
        return cls(player_id, "resolve_discard_continuation", None,
                   payload={"activation_data": dict(activation_data or {})})

    @classmethod
    def commit_troops_to_attack(cls, player_id, phase, declarations) -> "RulesTransaction":
        """Port ``CommitTroopsToAttackTransaction`` declarations."""
        normalized = tuple((defender, tuple(attackers or ()))
                           for defender, attackers in declarations or ())
        requirements: list[Requirement] = [
            TurnPhaseRequirement(game_engine.ETurnPhases.DeclareAttack),
            PlayerIsActiveRequirement(player_id),
        ]
        for defender, attackers in normalized:
            for attacker in attackers:
                requirements.extend((
                    CardInCollectionRequirement(
                        player_id, game_engine.ECardCollections.Warzone, attacker),
                    AttackerIsValidRequirement(defender, attacker),
                ))
        return cls(player_id, "commit_troops_to_attack", phase,
                   payload={"declarations": normalized},
                   requirements=tuple(requirements))

    @classmethod
    def commit_troops_to_defense(cls, player_id, phase, declarations) -> "RulesTransaction":
        """Port ``CommitTroopsToDefenseTransaction``'s atomic blocker set."""
        normalized = tuple((attacker, tuple(blockers or ()))
                           for attacker, blockers in declarations or ())
        return cls(player_id, "commit_troops_to_defense", phase,
                   payload={"declarations": normalized},
                   requirements=(
                       TurnPhaseRequirement(game_engine.ETurnPhases.DeclareDefense),
                       DefenseDeclarationsLegalRequirement(normalized),
                   ))

    @classmethod
    def play_resource(cls, player_id, card_id) -> "RulesTransaction":
        """Port ``PlayResourceTransaction`` (its sole C# requirement)."""
        return cls(player_id, "play_resource", None,
                   payload={"card_id": card_id, "playing_for_free": False},
                   requirements=(MainPhaseRequirement(),
                                 PlayerHasPriorityRequirement(player_id),
                                 CardCanBePlayedRequirement(card_id, False)))

    @classmethod
    def play_card(cls, kind: str, player_id, card_id, ability_data=(),
                  playing_for_free=False, phase=None) -> "RulesTransaction":
        """Build troop/artifact/spell play transactions from typed payloads."""
        data = tuple(dict(item or {}) for item in (ability_data or ()))
        # Permanents use the two main phases; actions may also be cast during
        # a priority response window.  The requirement is evaluated against
        # the authoritative port phase, never the phase claimed by the wire
        # request.
        phase_requirement = OrRequirement((MainPhaseRequirement(),
                                           PriorityWindowRequirement(),
                                           QuickActionCardRequirement(card_id)))
        requirements: list[Requirement] = [
            phase_requirement,
            PlayerHasPriorityRequirement(player_id),
            CardCanBePlayedRequirement(card_id, bool(playing_for_free))]
        requirements.extend(XCostRequirement(item) for item in data)
        return cls(player_id, str(kind), phase,
                   payload={"card_id": card_id, "ability_data": data,
                            "playing_for_free": bool(playing_for_free)},
                   requirements=tuple(requirements))

    @classmethod
    def play_troop(cls, player_id, card_id, ability_data=(), playing_for_free=False,
                   phase=None) -> "RulesTransaction":
        return cls.play_card("play_troop", player_id, card_id, ability_data,
                             playing_for_free, phase)

    @classmethod
    def play_artifact(cls, player_id, card_id, ability_data=(), playing_for_free=False,
                      phase=None) -> "RulesTransaction":
        return cls.play_card("play_artifact", player_id, card_id, ability_data,
                             playing_for_free, phase)

    @classmethod
    def play_spell(cls, player_id, card_id, ability_data=(), playing_for_free=False,
                   phase=None) -> "RulesTransaction":
        return cls.play_card("play_spell", player_id, card_id, ability_data,
                             playing_for_free, phase)

    @classmethod
    def activate_ability(cls, player_id, source_card_id, ability_template_id,
                         activation_data, ability_instance_id=0) -> "RulesTransaction":
        data = dict(activation_data or {})
        requirements: list[Requirement] = [
            NotRequirement(AbilityHasTriggerRequirement(ability_template_id)),
            AbilityIsManuallyActivatedRequirement(ability_template_id),
            AbilityCanBeActivatedRequirement(source_card_id, ability_template_id),
            XCostRequirement(data),
        ]
        # ActivateAbilityTransaction's instance field is not a persisted
        # RulesPort AbilityInstance for a fresh manual activation (the Mono
        # client commonly sends ``1`` here).  Treating it as one made valid
        # hand activations such as Subterranean Spy's Tunnel fail with a
        # stale AbilityHasValidTargetsRequirement. Target legality is checked
        # when the activation is resolved; continuation transactions use
        # SetAbilityActivationData instead.
        return cls(player_id, "activate_ability", None,
                   payload={"source_card_id": source_card_id,
                            "ability_template_id": ability_template_id,
                            "activation_data": data,
                            "ability_instance_id": int(ability_instance_id or 0)},
                   requirements=tuple(requirements))

    @classmethod
    def activate_triggered_abilities(cls, player_id, activation_data) -> "RulesTransaction":
        data = tuple(activation_data or ())
        return cls(player_id, "activate_triggered_abilities", None,
                   payload={"activation_data": data}, requirements=(
                       PlayerIsAtFrontOfTriggeredAbilityQueueRequirement(player_id),
                       AbilitiesAreTriggeredRequirement(player_id, data),
                       *(XCostRequirement(item) for item in data)))


def projected_ability_ignores_chain(descriptor) -> bool:
    """Resolve authored ``m_IgnoresChain`` for a projected chain item.

    A projected ability carries only its activation projection, not the
    Records graph.  The client's ``PriorityWindowAction.Update`` completes
    immediately when the chain's top ability ignores the chain, so the native
    scheduler must agree: otherwise the ability waits on a response window the
    client will never service and its BOM (and any picker) never resolves.
    """
    if not isinstance(descriptor, Mapping):
        return False
    if "ignores_chain" in descriptor:
        return bool(descriptor.get("ignores_chain"))
    guid = str(descriptor.get("ability_guid") or "").lower()
    if not guid:
        return False
    try:
        from gamedata import DEFAULT_RECORD_STORE, ability_graph
        graph = ability_graph(DEFAULT_RECORD_STORE, guid)
    except Exception:
        return False
    return bool(graph and getattr(graph, "ignores_chain", False))


@dataclass
class ProjectedChainAbility:
    """Native chain identity for a mode-owned card projection.

    The descriptor is the durable mode projection; the RulesPort action
    stack owns priority, ordering, and resolution timing.
    """

    instance_id: int
    descriptor: dict
    owner_id: object

    @property
    def source_uid(self):
        return self.descriptor.get("source_uid")

    @property
    def responsible_player_id(self):
        return self.owner_id

    @property
    def ability_template_id(self):
        return str(self.descriptor.get("ability_guid") or "")

    @property
    def ignores_chain(self):
        return projected_ability_ignores_chain(self.descriptor)

    @property
    def is_triggered(self):
        return self.descriptor.get("kind") == "trigger"


class SQLiteRulesSnapshot:
    """Persist the port scheduler inside the shared battle checkpoint."""

    KEY = "rules_port"

    def __init__(self, game_session) -> None:
        self.game_session = game_session
        self._ephemeral: dict[str, Any] = {}

    def load(self) -> Dict[str, Any]:
        state = getattr(self.game_session, "_rules_port_battle_state", None)
        if not isinstance(state, dict):
            state = getattr(self.game_session, "turn_order", {})
        if not isinstance(state, dict):
            return dict(self._ephemeral)
        saved = state.get(self.KEY, {})
        if isinstance(saved, dict):
            return dict(saved)
        return dict(self._ephemeral)

    def save(self, snapshot: Mapping[str, Any], conn=None) -> None:
        state = getattr(self.game_session, "_rules_port_battle_state", None)
        shared = isinstance(state, dict)
        if not shared:
            state = getattr(self.game_session, "turn_order", {})
        if not isinstance(state, dict):
            self._ephemeral = dict(snapshot)
            return
        if not shared:
            state = dict(state)
        state[self.KEY] = dict(snapshot)
        self.game_session._rules_port_battle_state = state
        self.game_session.turn_order = state
        self.game_session._persist(conn=conn)


class GameEngineEventSink:
    """Emit already-supported Python session events, not a parallel protocol."""

    def __init__(self, game: game_engine.Game, *, mutation_adapter=None,
                 event_observer: Optional[Callable[[object], None]] = None) -> None:
        self.game = game
        self.mutation_adapter = mutation_adapter
        self.event_observer = event_observer

    def _publish(self, event) -> None:
        self.game._push(event)
        if self.event_observer is not None:
            self.event_observer(event)

    @staticmethod
    def _uid(value):
        return value if isinstance(value, game_engine.UID) else game_engine.UID(value)

    def turn_phase_updated(self, phase, active_player_id, priority_player_id) -> None:
        event = self.game._make_event(game_engine.TurnPhaseUpdatedSessionEventArgs)
        event.turn_phase = phase
        event.active_player_id = self._uid(active_player_id)
        event.priority_player_id = (
            self._uid(priority_player_id) if priority_player_id is not None
            else game_engine.UID.invalid())
        self._publish(event)

    def green_light(self, player_id,
                    context=game_engine.EPriorityContext.Normal) -> None:
        event = self.game._make_event(game_engine.GreenLightSessionEventArgs)
        event.player_id = self._uid(player_id)
        event.context = context
        self._publish(event)

    def ability_pushed_on_chain(self, ability) -> None:
        """Publish the client chain entry for a native ability activation.

        The RulesPort scheduler owns the ability lifecycle, but the Unity
        client still needs the existing class-22 event to create its chain
        animation and priority state.  Previously a typed activation paid
        its cost and entered the native chain without producing that wire
        projection, leaving the client showing only the cost change.
        """
        source_uid = getattr(ability, "source_uid", None)
        try:
            source_uid = int(getattr(source_uid, "uid64", source_uid))
        except (TypeError, ValueError):
            source_uid = 0
        source_card_id = game_engine.SessionCardId(
            game_engine.UID(source_uid) if source_uid else game_engine.UID.invalid())

        target_card_ids = []
        activation = getattr(ability, "activation", None)
        target_map = getattr(activation, "target_map", {}) or {}
        for selected in target_map.values():
            values = selected if isinstance(selected, (list, tuple, set)) else (selected,)
            for value in values:
                try:
                    uid64 = int(getattr(value, "uid64", value))
                except (TypeError, ValueError):
                    continue
                if uid64 and (uid64 & 0xFF) == CARD_UID_TYPE:
                    target_card_ids.append(game_engine.SessionCardId(
                        game_engine.UID(uid64)))

        template_id = getattr(ability, "ability_template_id", "")
        try:
            template_id = game_engine.ResourceId.from_str(str(template_id))
        except (TypeError, ValueError):
            template_id = game_engine.ResourceId.invalid()
        self.game.push_ability_on_chain(
            source_card_id, template_id,
            ability_instance_id=int(getattr(ability, "instance_id", 0) or 0),
            target_card_ids=target_card_ids,
            ignores_chain=bool(getattr(ability, "ignores_chain", False)))

    def card_moved(self, session_card_id, player_id, collection,
                   location=game_engine.ECardLocations.Top, index=0) -> bool:
        """Apply a card-zone mutation, then publish its client event.

        The persistence adapter runs first so a failed authoritative mutation
        cannot produce a misleading ``CardMoved`` packet.
        """
        event = self.game._make_event(game_engine.CardMovedSessionEventArgs)
        event.session_card_id = (session_card_id if isinstance(
            session_card_id, game_engine.SessionCardId)
            else game_engine.SessionCardId(game_engine.UID(
                int(getattr(session_card_id, "uid64", session_card_id)))))
        event.player_id = self._uid(player_id)
        event.collection = collection
        event.location = location
        event.index = int(index)
        if (self.mutation_adapter is not None and
                not self.mutation_adapter(event)):
            return False
        self._publish(event)
        return True

    def player_wishes_to_play_first(self, player_id) -> None:
        event = self.game._make_event(PlayerWishesToPlayFirstSessionEventArgs)
        event.player_id = self._uid(player_id)
        self._publish(event)

    def player_wishes_to_draw_first(self, player_id) -> None:
        event = self.game._make_event(PlayerWishesToDrawFirstSessionEventArgs)
        event.player_id = self._uid(player_id)
        self._publish(event)

    def ability_activation_data_required(self, ability, prompts: tuple) -> None:
        """Emit the client class-23 UI checkpoint for a waiting ability.

        The prompt is still data-driven: effect group/instance identifiers are
        read from the Records-derived ability instance, never invented from a
        card name or UI node. The client uses this envelope to enter its
        target/option dialog and later sends SetAbilityActivationData.
        """
        event = self.game._make_event(
            game_engine.AbilityActivationDataRequiredSessionEventArgs)
        event.player_id = self._uid(ability.responsible_player_id)
        event.ability_instance_id = int(ability.instance_id)
        event.ability_parent_id = int(getattr(ability, "parent_instance_id", 0))
        source_uid = getattr(ability, "source_uid", None)
        event.source_card_id = (game_engine.SessionCardId(game_engine.UID(int(source_uid)))
                                if source_uid is not None else
                                game_engine.SessionCardId())
        try:
            event.ability_template_id = game_engine.ResourceId.from_str(
                ability.ability_template_id)
        except (TypeError, ValueError):
            event.ability_template_id = game_engine.ResourceId.invalid()
        first_prompt = prompts[0] if prompts else None
        target_index = getattr(first_prompt, "index", -1)
        effects = tuple(getattr(ability, "ordered_effects", ()))
        matching = [effect for effect in effects if isinstance(effect, dict) and
                    int(effect.get("target_index", effect.get(
                        "m_TargetTemplateIndex", -2))) == target_index]
        selected = matching or list(effects)
        def value(effect, *keys, default=0):
            for key in keys:
                if isinstance(effect, dict) and key in effect:
                    return effect[key]
                if hasattr(effect, key):
                    return getattr(effect, key)
            return default
        event.effect_group_id = int(value(
            selected[0], "effect_group_id", "m_EffectGroupId", default=0)
            if selected else 0)
        event.effect_instance_ids = [int(value(
            effect, "effect_instance_id", "m_EffectInstanceId", default=index))
            for index, effect in enumerate(selected)]
        event.resolve_chain = bool(not getattr(ability, "is_triggered", False))
        self._publish(event)


class AuthoritativeSession:
    """Python port of the C# authoritative scheduler, without Unity.

    A mode supplies transaction handlers and optional trigger/state-based
    callbacks.  The scheduler ensures they are executed in client order and
    every externally visible state update is emitted via ``GameEngineEventSink``.
    """

    def __init__(self, session_id, player_ids, *, seed_z: int, seed_w: int,
                 event_sink: Optional[GameEngineEventSink] = None,
                 snapshot: Optional[SQLiteRulesSnapshot] = None) -> None:
        players = tuple(player_ids)
        if not players:
            raise ValueError("an authoritative session needs at least one player")
        self.session_id = session_id
        self.player_ids = players
        self.active_player_id = players[0]
        self.current_turn_phase = game_engine.ETurnPhases.NotPlaying
        self.phase_states = default_phase_states(game_engine.ETurnPhases)
        self.random_number_generator = MultiplyWithCarryRng(seed_z, seed_w)
        self.ability_manager = AbilityRegistry()
        self.chain = Chain(self.ability_manager)
        self.combat_manager = CombatManager()
        self.action_stack = GameActionStack(self)
        self.event_sink = event_sink
        self.snapshot_store = snapshot
        self._transactions: Deque[RulesTransaction] = deque()
        self._transaction_history: list[dict[str, Any]] = []
        self._trigger_events: Deque[object] = deque()
        self._transaction_handlers: Dict[str, Callable[[RulesTransaction], bool]] = {}
        self._trigger_handler: Optional[Callable[[object], None]] = None
        self._state_based_handler: Optional[Callable[[], bool]] = None
        # C# Session.cardsReadyToPlay: cards a host/effect chose to play
        # without an ordinary client transaction, finalized on the next tick.
        self._cards_ready_to_play: list[dict[str, Any]] = []
        self._card_finisher: Optional[Callable[[dict[str, Any]], bool]] = None
        self._turn_start_resolver: Optional[Callable[[], object]] = None
        self._turn_boundary_resolver: Optional[Callable[[object], object]] = None
        self._turn_phase_entry_resolver: Optional[Callable[[object], object]] = None
        self._phase_priority_resolver: Optional[Callable[[object, object], object]] = None
        self._ability_resolver: Optional[Callable[[object], AbilityResolutionState]] = None
        self._activation_requester: Optional[Callable[[object, tuple], None]] = None
        self._ability_cost_payer: Optional[Callable[[object], bool]] = None
        self._combat_damage_backend = None
        self._card_transaction_resolver: Optional[Callable[[str, RulesTransaction], bool]] = None
        self._resource_transaction_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._hand_transaction_resolver: Optional[Callable[[str, RulesTransaction], bool]] = None
        self._setup_transaction_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._priority_transaction_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._discard_transaction_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._discard_continuation_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._ready_card_transaction_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._triggered_ability_transaction_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._player_options_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._checksum_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._encounter_mod_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._quit_game_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._attack_transaction_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._defense_transaction_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._damage_transaction_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._choice_transaction_resolver: Optional[Callable[[RulesTransaction], bool]] = None
        self._projection = TransactionProjection()
        self.runtime_facts = None
        self.pending_activation: Optional[dict[str, Any]] = None
        self.auto_pass_states: dict[object, object] = {}
        self.restored_action_descriptors: tuple[dict[str, Any], ...] = ()
        self.restored_chain_instance_ids: tuple[int, ...] = ()
        self._in_transaction_handler = False
        self.terminated = False
        self.eliminated_player_ids: set[object] = set()
        self.turn_phase_preferences: dict[object, dict[str, tuple[object, ...]]] = {}
        self.total_turns_taken = 0
        self._phase_transition_depth = 0
        # Branch facts consumed by the client-derived phase states.  They are
        # refreshed by the mode adapter at checkpoint boundaries; defaults are
        # deliberately conservative for standalone scheduler tests.
        self.skip_setup = False
        self.skip_mulligan = False
        self.all_players_ready_to_start = False
        self.active_player_skips_draw = False
        self.active_player_skips_attack = False
        self.has_legal_attackers = False
        self.has_legal_blockers = False
        self.has_forced_attackers = False
        self.has_extra_combats = False
        self.register_transaction("pass_priority", self._resolve_pass_priority)
        self.register_transaction("choose_play_first", self._resolve_choose_play_first)
        self.register_transaction("choose_draw_first", self._resolve_choose_draw_first)
        self.register_transaction("accept_starting_hand", self._resolve_hand_transaction)
        self.register_transaction("mulligan", self._resolve_hand_transaction)
        self.register_transaction("set_auto_pass", self._resolve_set_auto_pass)
        self.register_transaction("cancel_auto_pass", self._resolve_cancel_auto_pass)
        self.register_transaction("request_priority_sync", self._resolve_priority_sync)
        self.register_transaction("discard", self._resolve_discard)
        self.register_transaction("ready_card", self._resolve_ready_card)
        self.register_transaction("quit_game", self._resolve_quit_game)
        self.register_transaction("set_turn_phases", self._resolve_set_turn_phases)
        self.register_transaction("request_player_options", self._resolve_request_player_options)
        self.register_transaction("send_state_checksum", self._resolve_send_state_checksum)
        self.register_transaction("tip_window_closed", lambda transaction: True)
        self.register_transaction("encounter_mod_dialog", self._resolve_encounter_mod_dialog)
        self.register_transaction("activate_triggered_abilities", self._resolve_triggered_abilities)
        self.register_transaction("set_ability_activation_data",
                                  self._resolve_set_ability_activation_data)
        self.register_transaction("resolve_choice_continuation",
                                  self._resolve_choice_continuation)
        self.register_transaction("resolve_triggered_continuation",
                                  self._resolve_triggered_continuation)
        self.register_transaction("resolve_discard_continuation",
                                  self._resolve_discard_continuation)
        self.register_transaction("assign_damage_order", self._resolve_assign_damage_order)
        self.register_transaction("commit_troops_to_attack",
                                  self._resolve_commit_troops_to_attack)
        self.register_transaction("commit_troops_to_defense",
                                  self._resolve_commit_troops_to_defense)
        for kind in ("play_resource", "play_troop", "play_artifact", "play_spell",
                     "play_champion",
                     "activate_ability"):
            self.register_transaction(kind, self._resolve_card_transaction)

    def register_transaction(self, kind: str,
                             handler: Callable[[RulesTransaction], bool]) -> None:
        self._transaction_handlers[str(kind)] = handler

    def set_turn_start_resolver(self, resolver: Callable[[], object] | None) -> None:
        """Register the authoritative StartTurn card/state mutation hook."""
        self._turn_start_resolver = resolver

    def set_turn_boundary_resolver(self, resolver) -> None:
        """Register the mode projection for native EndTurn rotation.

        The scheduler chooses the next active player.  A mode may persist that
        typed result in its own checkpoint and emit no gameplay events here;
        phase-entry projection remains responsible for client packets.
        """
        self._turn_boundary_resolver = resolver

    def resolve_turn_start(self):
        """Run the mode's StartTurn rules at the port lifecycle boundary."""
        if self._turn_start_resolver is None:
            return None
        result = self._turn_start_resolver()
        self.persist()
        return result

    def set_turn_phase_entry_resolver(self, resolver) -> None:
        """Register the mode projection run at native phase entry.

        The callback may update the mode's persisted card/resource projection
        and publish its wire events, but it does not choose the next phase or
        manipulate the native action stack.
        """
        self._turn_phase_entry_resolver = resolver

    def set_phase_priority_resolver(self, resolver) -> None:
        """Register the mode's stop policy for newly entered phase windows."""
        self._phase_priority_resolver = resolver

    def configure_phase_priority(self, action) -> None:
        """Apply the host stop policy to one materialized native action.

        ``TurnPhaseState`` constructs the action before a mode can inspect its
        persisted stop preferences.  Rebuilding the queue here keeps that
        policy in the native action, including ``NONE`` for an auto-passed
        phase, rather than leaving a phantom ALL-player GreenLight active.
        """
        resolver = self._phase_priority_resolver
        if resolver is None:
            return
        priority_players = resolver(self, action)
        if priority_players is None:
            return
        action.priority_players = priority_players
        action.reset_priority_window(start_with_active_player=False)

    def resolve_turn_phase_entry(self, phase=None):
        """Run the phase-entry projection after the native state transition."""
        if self._turn_phase_entry_resolver is None:
            return None
        result = self._turn_phase_entry_resolver(
            self.current_turn_phase if phase is None else phase)
        self.persist()
        return result

    def materialize_current_phase(self) -> bool:
        """Enter an unmaterialized checkpoint phase through its native state.

        A freshly created Practice checkpoint starts at ``StartTurn`` before
        the first scheduler tick.  Reconnects and ordinary transitions already
        have an action on the stack; only that initial checkpoint needs this
        explicit entry so its turn-start resolver and native priority action
        are both created before the mode driver runs.
        """
        if self.action_stack.count:
            return False
        state = self.phase_states.get(phase_name(self.current_turn_phase))
        if state is None:
            return False
        state.on_entry(self)
        self.persist()
        return True

    def begin_first_turn(self, active_player_id) -> bool:
        """Start the first PvE/Practice turn after both hands are kept.

        Practice setup still sends the client's non-interactive ``StartGame``
        packet from the host setup projection.  The native scheduler must
        nevertheless leave its Mulligan action behind before the AI driver is
        invoked; otherwise a fresh checkpoint can report ``turn_player=ai``
        while the native session remains active for the human in Mulligan.
        Entering ``StartTurn`` through the normal state object keeps the
        first-turn lifecycle and priority action RulesPort-owned.
        """
        if phase_name(self.current_turn_phase) != "Mulligan":
            return False
        self.action_stack.clear()
        self.active_player_id = self.coerce_transaction_player_id(
            active_player_id)
        self.current_turn_phase = game_engine.ETurnPhases.StartTurn
        return self.materialize_current_phase()

    def set_choice_transaction_resolver(self, resolver) -> None:
        self._choice_transaction_resolver = resolver
        self._projection.bind("choice_transaction", resolver)

    def set_trigger_handler(self, handler: Callable[[object], None]) -> None:
        self._trigger_handler = handler

    def set_state_based_handler(self, handler: Callable[[], bool]) -> None:
        self._state_based_handler = handler

    def set_ability_resolver(self, resolver: Callable[[object], AbilityResolutionState]) -> None:
        self._ability_resolver = resolver

    def set_activation_requester(self, requester: Callable[[object, tuple], None]) -> None:
        self._activation_requester = requester

    def set_ability_cost_payer(self, payer: Callable[[object], bool]) -> None:
        """Attach the existing resource/counter persistence adapter.

        Costs remain owned by the live card/session domain. The port only
        controls when that adapter runs, matching the client transaction and
        chain lifecycle.
        """
        self._ability_cost_payer = payer

    def set_runtime_facts(self, facts) -> None:
        """Attach the existing card/player state adapter for requirements.

        This is deliberately a read/validation seam. Mutations continue to
        pass through the existing domain operation adapters at transaction
        boundaries rather than letting the port issue SQL.
        """
        self.runtime_facts = facts

    def set_card_transaction_resolver(
            self, resolver: Callable[[str, RulesTransaction], bool]) -> None:
        """Attach existing card/resource mutation operations by transaction kind."""
        self._card_transaction_resolver = resolver
        self._projection.bind("card_transaction", resolver)

    def set_resource_transaction_resolver(
            self, resolver: Callable[[RulesTransaction], bool]) -> None:
        """Attach the resource-play domain operation explicitly."""
        self._resource_transaction_resolver = resolver
        self._projection.bind("resource_transaction", resolver)

    def set_hand_transaction_resolver(
            self, resolver: Callable[[str, RulesTransaction], bool]) -> None:
        """Attach existing opening-hand/mulligan mutation operations."""
        self._hand_transaction_resolver = resolver
        self._projection.bind("hand_transaction", resolver)

    def set_setup_transaction_resolver(
            self, resolver: Callable[[RulesTransaction], bool]) -> None:
        """Attach the authoritative play/draw setup transition."""
        self._setup_transaction_resolver = resolver
        self._projection.bind("setup_transaction", resolver)

    def set_priority_transaction_resolver(
            self, resolver: Callable[[RulesTransaction], bool]) -> None:
        """Attach the host phase/AI continuation after a priority pass."""
        self._priority_transaction_resolver = resolver
        self._projection.bind("priority_transaction", resolver)

    def set_auto_pass_transaction_resolver(self, resolver) -> None:
        self._auto_pass_transaction_resolver = resolver
        self._projection.bind("auto_pass_transaction", resolver)

    def set_cancel_auto_pass_transaction_resolver(self, resolver) -> None:
        self._cancel_auto_pass_transaction_resolver = resolver
        self._projection.bind("cancel_auto_pass_transaction", resolver)

    def set_priority_sync_resolver(self, resolver) -> None:
        self._priority_sync_resolver = resolver
        self._projection.bind("priority_sync", resolver)

    def set_turn_phase_resolver(self, resolver) -> None:
        self._turn_phase_resolver = resolver
        self._projection.bind("turn_phase_transaction", resolver)

    def sync_checkpoint(self, *, phases, phase_idx, active_player_id,
                        client_player_id, phase_facts=None,
                        ensure_main_priority=True,
                        ensure_current_priority=False) -> None:
        """Reconcile the native scheduler with a persisted battle checkpoint.

        The SQLite battle state is the compatibility projection while a mode is
        being migrated.  Keeping this small reconciliation operation here
        prevents service handlers from reaching into the native action stack
        and inventing priority state during reconnect or hot reload.
        """
        # Phase-entry projection can synchronously build another Game and
        # re-enter the adapter. At that point transition_to has already
        # selected the new phase/owner but has not finished materializing its
        # priority action. A compatibility checkpoint captured before the
        # transition must not rewrite either value in the middle of that
        # operation (the live symptom was AI StartTurn becoming the human's
        # FirstMainPhase). The outer attach will persist the completed native
        # transition normally.
        if self._phase_transition_depth:
            return
        # Checkpoint JSON historically stored raw uint64 IDs, while the live
        # session/action queue uses typed UID values.  Keep every value that
        # enters the native scheduler in the session's participant identity
        # domain.  Otherwise ``PriorityWindowAction`` compares a raw integer
        # with a UID, so a displayed pass is accepted by ingress but cannot
        # consume the native queue; the same phase is then projected again.
        participants = tuple(self.player_ids)

        def canonical_participant(value):
            if value is None:
                return None
            try:
                raw = _uid_value(value)
            except (TypeError, ValueError):
                raw = None
            for participant in participants:
                if value is participant or value == participant:
                    return participant
                if raw is not None:
                    try:
                        if _uid_value(participant) == raw:
                            return participant
                    except (TypeError, ValueError):
                        continue
            return value

        active_player_id = canonical_participant(active_player_id)
        client_player_id = canonical_participant(client_player_id)
        try:
            index = int(phase_idx)
        except (TypeError, ValueError):
            index = -1
        if 0 <= index < len(phases or ()):
            self.current_turn_phase = phases[index]
        self.active_player_id = active_player_id
        for name, value in (phase_facts or {}).items():
            if hasattr(self, name):
                setattr(self, name, bool(value))
        self.action_stack.priority_player_id = client_player_id
        current = self.phase_states.get(phase_name(self.current_turn_phase))
        top = self.action_stack.peek()
        if isinstance(top, PriorityWindowAction):
            # Rehydrate the action's queue as well as the stack mirror.  A
            # cached action can survive a reconnect with raw IDs even when no
            # stop-policy resolver is installed (notably in focused/native
            # hosts), and the queue is the value used by pass_priority().
            from collections import deque
            top._priority_queue = deque(
                canonical_participant(player)
                for player in tuple(getattr(top, "_priority_queue", ()))
                if canonical_participant(player) is not None)
            self.action_stack.priority_player_id = top.priority_player_id
        # A cached host can cross a phase boundary while its old compatibility
        # action is still in memory. A native chain response is distinct from
        # a phase window and must remain untouched.
        if (isinstance(top, PriorityWindowAction) and
                getattr(top, "ability_responding_to", None) is None and
                getattr(top, "_rules_port_phase", None) !=
                phase_name(self.current_turn_phase)):
            self.action_stack.clear()
            top = None
        if (isinstance(top, PriorityWindowAction) and
                getattr(top, "ability_responding_to", None) is None and
                top.priority_player_id is not None):
            # Reattached actions are materialized from the durable snapshot,
            # so they need the same stop-policy translation as actions created
            # by TurnPhaseState.on_entry.  An exhausted queue (``None``) is
            # deliberately excluded: every participant the window asked has
            # already passed, so the window is complete and the next tick pops
            # it and advances the phase.  Re-applying the stop policy there
            # would rebuild the queue and re-ask an already-answered decision
            # (live symptom: the DeclareDefense blocker prompt reopened after
            # CommitTroopsToDefense because an ordinary Game projection
            # re-synced the checkpoint mid-transaction).
            self.configure_phase_priority(top)
        initial_start_turn = (
            phase_name(self.current_turn_phase) == "StartTurn" and
            int(getattr(self, "total_turns_taken", 0) or 0) == 0)
        if (ensure_current_priority and self.action_stack.count == 0 and
                current is not None and
                current.priority_players is not TurnPhasePlayers.NONE and
                not initial_start_turn):
            from collections import deque
            priority_action = PriorityWindowAction(current.priority_players)
            priority_action._rules_port_phase = phase_name(
                self.current_turn_phase)
            self.action_stack.push(priority_action)
            self.configure_phase_priority(priority_action)
            # The checkpoint already tells us which human owns the current
            # client window. For ALL-player windows this preserves APNAP order
            # while ensuring reconnect does not revert to the active side.
            if client_player_id is not None:
                queue = list(priority_action._priority_queue)
                if client_player_id in queue:
                    queue.remove(client_player_id)
                    queue.insert(0, client_player_id)
                    priority_action._priority_queue = deque(queue)
                self.action_stack.priority_player_id = \
                    priority_action.priority_player_id
        if (ensure_main_priority and self.action_stack.count == 0 and
                self.current_turn_phase in (
                    game_engine.ETurnPhases.FirstMainPhase,
                    game_engine.ETurnPhases.SecondMainPhase)):
            from collections import deque
            priority_action = PriorityWindowAction(TurnPhasePlayers.ACTIVE)
            priority_action._rules_port_phase = phase_name(
                self.current_turn_phase)
            self.action_stack.push(priority_action)
            priority_action._priority_queue = deque([client_player_id])
            self.action_stack.priority_player_id = client_player_id
        # A persisted projected chain must always own the top-level response
        # action. Run this after ordinary checkpoint materialization too, so a
        # stale phase action cannot shadow a chain restored from JSON.
        self.ensure_projected_chain_action()

    def prune_orphan_actions(self, legacy_stack=None) -> bool:
        """Discard a stale RulesPort action that owns no live work.

        A double-clicked ability can persist a ``ResolveTopOfChainAction``
        after the chain item is gone; the orphaned action then makes every
        later card appear unplayable.  Only discard the action when nothing
        authoritative still owns the window: an ordinary phase window, a live
        projected chain item, a legacy stack descriptor, and a pending
        activation are all live and must be preserved.  In particular a
        manual/triggered ability response window has ``ability_responding_to``
        set, so it is not an ordinary phase window; discarding it while the
        chain still holds the ability strands the chain and the client pass
        loop never resolves it.
        """
        if self.action_stack is None or self.action_stack.count == 0:
            return False
        top = self.action_stack.peek()
        if (isinstance(top, PriorityWindowAction) and
                getattr(top, "ability_responding_to", None) is None):
            return False
        if not self.chain.is_empty:
            return False
        if legacy_stack:
            return False
        if self.pending_activation:
            return False
        self.action_stack.clear()
        return True

    def set_discard_transaction_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._discard_transaction_resolver = resolver
        self._projection.bind("discard_transaction", resolver)

    def set_discard_continuation_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._discard_continuation_resolver = resolver
        self._projection.bind("discard_continuation", resolver)

    def set_ready_card_transaction_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._ready_card_transaction_resolver = resolver
        self._projection.bind("ready_card_transaction", resolver)

    def set_triggered_ability_transaction_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._triggered_ability_transaction_resolver = resolver
        self._projection.bind("triggered_ability_transaction", resolver)

    def set_player_options_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._player_options_resolver = resolver
        self._projection.bind("player_options", resolver)

    def set_checksum_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._checksum_resolver = resolver

    def set_encounter_mod_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._encounter_mod_resolver = resolver
        self._projection.bind("encounter_mod", resolver)

    def set_quit_game_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._quit_game_resolver = resolver
        self._projection.bind("quit_game", resolver)

    def set_attack_transaction_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._attack_transaction_resolver = resolver
        self._projection.bind("attack_transaction", resolver)

    def set_defense_transaction_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._defense_transaction_resolver = resolver
        self._projection.bind("defense_transaction", resolver)

    def set_damage_transaction_resolver(self, resolver: Callable[[RulesTransaction], bool]) -> None:
        self._damage_transaction_resolver = resolver
        self._projection.bind("damage_transaction", resolver)

    def set_combat_damage_backend(self, backend) -> None:
        """Attach the explicit detailed-damage projection backend."""
        self._combat_damage_backend = backend
        self._projection.bind(
            "damage_transaction", lambda transaction: backend(self, transaction))

    # These callbacks are deliberately limited to host-owned projections
    # (SQLite writes and Unity-compatible event packets).  Keeping the
    # inventory here gives the migration a machine-checkable boundary: a
    # transaction cannot be considered live merely because its validator is
    # native while its state/event projection is silently absent.
    _PROJECTION_RESOLVERS = {
        "card_transaction": "_card_transaction_resolver",
        "resource_transaction": "_resource_transaction_resolver",
        "hand_transaction": "_hand_transaction_resolver",
        "setup_transaction": "_setup_transaction_resolver",
        "priority_transaction": "_priority_transaction_resolver",
        "auto_pass_transaction": "_auto_pass_transaction_resolver",
        "cancel_auto_pass_transaction": "_cancel_auto_pass_transaction_resolver",
        "priority_sync": "_priority_sync_resolver",
        "turn_phase_transaction": "_turn_phase_resolver",
        "discard_transaction": "_discard_transaction_resolver",
        "ready_card_transaction": "_ready_card_transaction_resolver",
        "triggered_ability_transaction": "_triggered_ability_transaction_resolver",
        "player_options": "_player_options_resolver",
        "encounter_mod": "_encounter_mod_resolver",
        "quit_game": "_quit_game_resolver",
        "attack_transaction": "_attack_transaction_resolver",
        "defense_transaction": "_defense_transaction_resolver",
        "damage_transaction": "_damage_transaction_resolver",
        "choice_transaction": "_choice_transaction_resolver",
        "discard_continuation": "_discard_continuation_resolver",
    }

    def missing_projection_resolvers(self) -> tuple[str, ...]:
        """Return host projections not wired for this live session.

        The port's kernel remains usable in isolated tests without these
        callbacks; a production mode must wire every entry before accepting
        typed gameplay traffic.  Returning names (rather than a boolean)
        makes attach diagnostics actionable and prevents another silent
        legacy fallback seam.
        """
        return self._projection.missing(self._PROJECTION_RESOLVERS)

    def projection(self, role: str):
        """Return the bound host projection for a normalized role."""
        return self._projection.handler(role)

    def assert_projection_wiring(self) -> None:
        missing = self.missing_projection_resolvers()
        if missing:
            raise RuntimeError("RulesPort projection wiring incomplete: " +
                               ", ".join(missing))

    def is_at_triggered_ability_queue_front(self, player_id) -> bool:
        checker = getattr(self.runtime_facts, "is_at_triggered_ability_queue_front", None)
        if callable(checker):
            return bool(checker(player_id))
        # Native fallback for sessions whose trigger queue is represented by
        # the port's AbilityManager rather than a mode-specific queue table.
        return any(
            getattr(ability, "is_triggered", False) and
            getattr(ability, "responsible_player_id", None) == player_id and
            not self.ability_manager.is_chain_ability(instance_id)
            for instance_id, ability in self.ability_manager._instances.items())

    def validate_triggered_abilities(self, player_id, activation_data) -> bool:
        checker = getattr(self.runtime_facts, "validate_triggered_abilities", None)
        if callable(checker):
            return bool(checker(player_id, activation_data))
        if not activation_data:
            return False
        for activation in activation_data:
            if not isinstance(activation, Mapping):
                return False
            try:
                ability = self.ability_manager.get(int(
                    activation.get("ability_instance_id",
                                    activation.get("instance_id"))))
            except (TypeError, ValueError):
                return False
            if (ability is None or not getattr(ability, "is_triggered", False) or
                    getattr(ability, "responsible_player_id", None) != player_id):
                return False
        return True

    def get_card(self, card_id):
        getter = getattr(self.runtime_facts, "get_card", None)
        return getter(card_id) if callable(getter) else None

    def get_player(self, player_id):
        getter = getattr(self.runtime_facts, "get_player", None)
        return getter(player_id) if callable(getter) else None

    def can_play_card(self, card, player_id, playing_for_free=False) -> bool:
        checker = getattr(self.runtime_facts, "can_play_card", None)
        return bool(checker(card, player_id, playing_for_free)) if callable(checker) else False

    def can_activate_ability(self, card, player_id, ability_template_id) -> bool:
        checker = getattr(self.runtime_facts, "can_activate_ability", None)
        return bool(checker(card, player_id, ability_template_id)) if callable(checker) else False

    def can_pay_ability_cost(self, ability) -> bool:
        checker = getattr(self.runtime_facts, "can_pay_ability_cost", None)
        return bool(checker(ability)) if callable(checker) else True

    def can_attack(self, attacker, defender, player_id) -> bool:
        checker = getattr(self.runtime_facts, "can_attack", None)
        return bool(checker(attacker, defender, player_id)) if callable(checker) else False

    def are_defense_declarations_legal(self, declarations, player_id) -> bool:
        checker = getattr(self.runtime_facts, "validate_blocks", None)
        return bool(checker(self, declarations, player_id)) if callable(checker) else False

    def validate_x_cost(self, player_id, activation_data) -> bool:
        checker = getattr(self.runtime_facts, "validate_x_cost", None)
        return bool(checker(player_id, activation_data)) if callable(checker) else False

    def validate_ability_targets(self, ability, activation_data, player_id) -> bool:
        checker = getattr(self.runtime_facts, "validate_ability_targets", None)
        return bool(checker(ability, activation_data, player_id)) if callable(checker) else False

    def refresh_checkpoint_state(self) -> None:
        """Adopt the durable checkpoint's phase before validating a transaction.

        The session wrapper can be rehydrated with a new checkpoint while this
        native scheduler object is cached.  Refresh the facts bridge at the
        transaction boundary so validation never reads an attach-time resource,
        card-state, or ownership snapshot.

        The phase must be refreshed before normalization, not only inside
        ``submit_transaction``.  Practice/PvE keeps two phase representations
        (the native port's ``current_turn_phase`` and the compatibility
        checkpoint's ``phase_idx`` cursor); the AI driver can advance one
        without updating the other.  Normalizing against one value and then
        validating against the other rejected a legitimate client pass with
        ``requirements=phase/player/handler`` even though both requirements
        passed on re-inspection.  Callers normalize after this refresh so both
        steps observe the same phase.
        """
        facts = self.runtime_facts
        game_session = getattr(self.snapshot_store, "game_session", None)
        if facts is None or game_session is None:
            return
        from .persistence import load_state
        live_state = load_state(game_session)
        if not (isinstance(live_state, dict) and live_state):
            return
        facts.battle_state = live_state
        # Practice/PvE stores the native phase at the checkpoint cursor. PvP
        # supplies its own raw-phase synchronization in
        # PvpAuthoritativeSession.submit_transaction.
        if not live_state.get("pvp"):
            from .persistence import current_phase
            live_phase = current_phase(live_state)
            if live_phase is not None:
                self.current_turn_phase = live_phase

    def submit_transaction(self, transaction: RulesTransaction) -> bool:
        self.refresh_checkpoint_state()
        # A server-driven card can be queued between two compatibility
        # projections.  The durable RulesPort snapshot is the transaction
        # boundary in that case; repair a live response queue that still has
        # the previous participant before PlayerHasPriorityRequirement runs.
        # Without this, the wire GreenLight can name the human while the
        # freshly re-entered PriorityWindowAction still rejects that pass.
        self.reconcile_projected_chain_priority()
        if self.terminated or transaction.player_id not in self.player_ids:
            return False
        if ((transaction.phase is not None and
             phase_name(transaction.phase) != phase_name(self.current_turn_phase))
                or not transaction.validate(self)):
            return False
        self._transactions.append(transaction)
        self._transaction_history.append({
            "player_id": _serial_id(transaction.player_id),
            "kind": transaction.kind,
            "phase": (None if transaction.phase is None
                       else phase_name(transaction.phase)),
            "payload": _json_value(transaction.payload),
        })
        return True

    def reconcile_projected_chain_priority(self) -> bool:
        """Align a live chain response queue with its durable priority.

        AI card plays are projected to the client without a client transaction
        of their own.  A reconnect or re-entrant projection can therefore
        leave the response action's queue at the AI while the persisted native
        snapshot already names the human.  Repair only that projected-chain
        boundary; ordinary phase windows retain their configured stop policy.
        """
        top = self.action_stack.peek()
        if (self.chain.is_empty or not isinstance(top, PriorityWindowAction) or
                getattr(top, "ability_responding_to", None) is None or
                self.snapshot_store is None):
            return False
        try:
            saved = self.snapshot_store.load()
        except Exception:
            return False
        if not isinstance(saved, Mapping):
            return False
        desired = saved.get("priority_player_id")
        if desired is None:
            return False
        desired = self.coerce_transaction_player_id(desired)
        if desired not in self.player_ids:
            return False

        def same(left, right):
            try:
                return _uid_value(left) == _uid_value(right)
            except (TypeError, ValueError):
                return left == right

        current = top.priority_player_id
        if same(current, desired) and same(
                self.action_stack.priority_player_id, desired):
            return False

        queue = list(getattr(top, "_priority_queue", ()) or ())
        queue = [player for player in queue if not same(player, desired)]
        if (top.priority_players is TurnPhasePlayers.ACTIVE and
                not same(desired, self.active_player_id)):
            # The active AI has already made its server-side decision.  The
            # explicitly persisted human responder is the only participant
            # that should remain in this response window.
            queue.clear()
        queue.insert(0, desired)
        top._priority_queue = deque(queue)
        self.action_stack.priority_player_id = desired
        return True

    @property
    def transaction_history(self) -> tuple[dict[str, Any], ...]:
        """Normalized accepted intents for deterministic parity captures."""
        return tuple(dict(item) for item in self._transaction_history)

    def parity_state(self) -> Dict[str, Any]:
        """Return the state slice compared against a captured C# run."""
        return self.snapshot()

    def enqueue_trigger_event(self, event: object) -> None:
        self._trigger_events.append(event)

    def handle_game_event(self) -> None:
        while self._trigger_events:
            event = self._trigger_events.popleft()
            if self._trigger_handler is not None:
                self._trigger_handler(event)

    def set_card_finisher(self, resolver) -> None:
        """Register the host finalizer for queued ready-to-play cards."""
        self._card_finisher = resolver

    def queue_card_ready_to_play(self, card_uid, player_id, ability=None) -> None:
        """Port of ``Session.PrepareToPlayCard`` (queue a deferred play)."""
        self._cards_ready_to_play.append({
            "card_uid": int(card_uid),
            "player_id": player_id,
            "ability": ability,
        })

    def finish_playing_cards(self) -> bool:
        """Port of ``Session.FinishPlayingCards``.

        A card put into the ready-to-play queue is finalized here on the next
        scheduler tick, so its zone/state projection matches the C# ordering.
        """
        if not self._cards_ready_to_play:
            return False
        queue = list(self._cards_ready_to_play)
        self._cards_ready_to_play.clear()
        finisher = self._card_finisher
        if finisher is None:
            return False
        progressed = False
        for item in queue:
            try:
                if finisher(item):
                    progressed = True
            except Exception:
                continue
        return progressed

    def handle_transaction(self) -> bool:
        if not self._transactions:
            return False
        transaction = self._transactions.popleft()
        handler = self._transaction_handlers.get(transaction.kind)
        self._in_transaction_handler = True
        try:
            handled = bool(handler and handler(transaction))
        finally:
            self._in_transaction_handler = False
        # A successful mutation is the authoritative transaction boundary.
        # Rejected/unknown intents are not persisted, matching the host's
        # existing DB ownership and avoiding snapshots that imply a mutation.
        if handled:
            self.persist()
        return handled

    def _resolve_pass_priority(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("priority_transaction")
        if resolver is not None:
            return bool(resolver(transaction))
        return self.pass_player_priority(transaction.player_id)

    def _resolve_choose_play_first(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("setup_transaction")
        if resolver is not None:
            return bool(resolver(transaction))
        if self.event_sink is not None:
            self.event_sink.player_wishes_to_play_first(transaction.player_id)
        return self.pass_player_priority(transaction.player_id)

    def _resolve_choose_draw_first(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("setup_transaction")
        if resolver is not None:
            return bool(resolver(transaction))
        if transaction.player_id not in self.player_ids:
            return False
        # C# removes the choosing player then appends them, making the other
        # player active/starting in the ordinary two-player case.
        players = tuple(pid for pid in self.player_ids if pid != transaction.player_id)
        self.player_ids = players + (transaction.player_id,)
        self.active_player_id = self.player_ids[0]
        if self.event_sink is not None:
            self.event_sink.player_wishes_to_draw_first(transaction.player_id)
        return self.pass_player_priority(transaction.player_id)

    def _resolve_set_ability_activation_data(self, transaction: RulesTransaction) -> bool:
        return self.resume_activation_data(
            transaction.player_id,
            transaction.payload.get("ability_instance_id"),
            transaction.payload.get("activation_data"),
        )

    def _resolve_choice_continuation(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("choice_transaction")
        if resolver is None:
            return False
        return bool(resolver(transaction))

    def _resolve_triggered_continuation(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("triggered_ability_transaction")
        return bool(resolver(transaction)) if resolver is not None else False

    def _resolve_discard_continuation(self, transaction: RulesTransaction) -> bool:
        resolver = (self.projection("discard_continuation") or
                    self.projection("discard_transaction"))
        return bool(resolver(transaction)) if resolver is not None else False

    def _resolve_assign_damage_order(self, transaction: RulesTransaction) -> bool:
        assignments = transaction.payload.get("assignments", ())
        staged = False
        # Assignments are part of the port-owned Combat object.  The host
        # projection may still perform detailed damage/death effects, but it
        # must consume this validated order rather than reconstructing it
        # from a second legacy combat representation.
        for combat_id, card_ids in assignments:
            if card_ids is None:
                continue
            combat = self.combat_manager.get(combat_id)
            if combat is None:
                staged = False
                break
            if not combat.assign_damage_order(card_ids):
                return False
            staged = True
        resolver = self.projection("damage_transaction")
        if resolver is not None:
            # A legacy-only AI combat may not yet have a port Combat object;
            # its compatibility projection remains available in that case.
            return bool(resolver(transaction))
        if assignments and not staged:
            return False
        # The client resolves this transaction with DoPassPriorityTransaction.
        # If this phase has a normal priority action, drive that same path;
        # phase-specific combat actions can otherwise advance on their tick.
        if self.action_stack.priority_player_id == transaction.player_id:
            return self.pass_player_priority(transaction.player_id)
        return True

    def _resolve_commit_troops_to_attack(self, transaction: RulesTransaction) -> bool:
        staged = []
        for defender_id, attacker_ids in transaction.payload.get(
                "declarations", ()):
            defender = self.get_card(defender_id) or defender_id
            for attacker_id in attacker_ids:
                attacker = self.get_card(attacker_id)
                if attacker is None:
                    return False
                combat = self.declare_attack(
                    transaction.player_id, defender, attacker)
                if combat is None:
                    return False
                staged.append(combat)
        # C# ``CommitTroopsToAttackTransaction.Resolve`` declares the attacks,
        # sorts the combats, then calls ``session.DoPassPriorityTransaction()``.
        # The declarations are already staged in the combat manager above, so
        # consume the active player's ``DeclareAttack`` priority window BEFORE
        # the host projection drives the phase boundary.  Passing only in the
        # no-resolver branch left the window waiting in the live host, so the
        # phase never advanced to ``DeclareAttackPriorityWindow`` and the client
        # stayed stuck in Select Attackers.
        if (self.action_stack.priority_player_id is not None and
                _same_player_uid(
                    self.coerce_transaction_player_id(
                        self.action_stack.priority_player_id),
                    self.coerce_transaction_player_id(
                        transaction.player_id))):
            self.pass_player_priority(transaction.player_id)
        resolver = self.projection("attack_transaction")
        if resolver is not None:
            handled = bool(resolver(transaction))
            if not handled:
                for combat in staged:
                    self.combat_manager.remove_combat(combat.combat_id)
            return handled
        return True

    def _resolve_commit_troops_to_defense(self, transaction: RulesTransaction) -> bool:
        staged = []
        for attacker_id, blocker_ids in transaction.payload.get(
                "declarations", ()):
            attacker = self.get_card(attacker_id)
            combats = self.combat_manager.combats_with_attacker(attacker)
            if not combats:
                return False
            blockers = tuple(self.get_card(card_id) for card_id in blocker_ids)
            if any(card is None for card in blockers):
                return False
            for combat in combats:
                if not combat.declare_blockers(blockers):
                    return False
                staged.append((combat, tuple(blockers)))
        # C# ``CommitTroopsToDefenseTransaction.Resolve`` commits the blockers
        # and then calls ``session.DoPassPriorityTransaction()``; the defender's
        # ``DeclareDefense`` window must be consumed before the host projection
        # drives the next boundary (see ``_resolve_commit_troops_to_attack``).
        if (self.action_stack.priority_player_id is not None and
                _same_player_uid(
                    self.coerce_transaction_player_id(
                        self.action_stack.priority_player_id),
                    self.coerce_transaction_player_id(
                        transaction.player_id))):
            self.pass_player_priority(transaction.player_id)
        resolver = self.projection("defense_transaction")
        if resolver is not None:
            handled = bool(resolver(transaction))
            if not handled:
                for combat, _blockers in staged:
                    combat.blockers = []
                    combat.flags &= ~(CombatFlags.BLOCKERS_DECLARED |
                                      CombatFlags.ATTACK_BLOCKED |
                                      CombatFlags.DAMAGE_ASSIGNED)
            return handled
        return True

    def _resolve_card_transaction(self, transaction: RulesTransaction) -> bool:
        if transaction.kind == "play_resource":
            resolver = self.projection("resource_transaction")
            if resolver is not None:
                return bool(resolver(transaction))
        resolver = self.projection("card_transaction")
        if resolver is None:
            return False
        return bool(resolver(transaction.kind, transaction))

    def _resolve_hand_transaction(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("hand_transaction")
        if resolver is None:
            return False
        return bool(resolver(transaction.kind, transaction))

    def _resolve_set_auto_pass(self, transaction: RulesTransaction) -> bool:
        if self.current_turn_phase in (game_engine.ETurnPhases.Mulligan,
                                       game_engine.ETurnPhases.PickGoesFirst,
                                       game_engine.ETurnPhases.StartTurn):
            return False
        if self.action_stack.priority_player_id != transaction.player_id:
            return False
        if (transaction.payload.get("as_active") !=
                (transaction.player_id == self.active_player_id)):
            return False
        resolver = self.projection("auto_pass_transaction")
        if resolver is not None:
            handled = bool(resolver(transaction))
            if handled:
                self.auto_pass_states[transaction.player_id] = transaction.payload.get(
                    "passing_state")
            return handled
        self.auto_pass_states[transaction.player_id] = transaction.payload.get(
            "passing_state")
        self.pass_player_priority(transaction.player_id)
        return True

    def _resolve_cancel_auto_pass(self, transaction: RulesTransaction) -> bool:
        if transaction.player_id not in self.player_ids:
            return False
        resolver = self.projection("cancel_auto_pass_transaction")
        if resolver is not None:
            handled = bool(resolver(transaction))
            if handled:
                self.auto_pass_states.pop(transaction.player_id, None)
            return handled
        self.auto_pass_states.pop(transaction.player_id, None)
        return True

    def _resolve_priority_sync(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("priority_sync")
        if resolver is not None:
            return bool(resolver(transaction))
        self.send_green_light(transaction.player_id)
        return True

    def _resolve_discard(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("discard_transaction")
        if resolver is None:
            return False
        return bool(resolver(transaction))

    def _resolve_ready_card(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("ready_card_transaction")
        if resolver is None:
            # A ReadyCardTransaction is a state mutation, not an acknowledgement
            # placeholder.  Reject it until the mode wires its persistence and
            # CardUpdated projection; silently accepting would desynchronise
            # the client's tapped state.
            return False
        return bool(resolver(transaction))

    def _resolve_triggered_abilities(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("triggered_ability_transaction")
        if resolver is not None:
            return bool(resolver(transaction))
        # Native batch lifecycle for ``ActivateTriggeredAbiliesTransaction``.
        # Runtime facts validate queue ownership and the complete batch before
        # this point; the port now owns binding, one-time payment, and chain
        # insertion even when the mode has not supplied a compatibility hook.
        activations = transaction.payload.get("activation_data", ())
        if not activations:
            return False
        staged = []
        for activation in activations:
            if not isinstance(activation, Mapping):
                return False
            instance_id = activation.get("ability_instance_id",
                                         activation.get("instance_id"))
            try:
                ability = self.ability_manager.get(int(instance_id))
            except (TypeError, ValueError):
                return False
            if (ability is None or
                    getattr(ability, "responsible_player_id", None) !=
                    transaction.player_id or
                    not hasattr(ability, "bind_activation")):
                return False
            if not ability.bind_activation(activation):
                return False
            if not self.pay_ability_cost(ability):
                return False
            staged.append(ability)
        for ability in staged:
            if not self.finish_ability_on_chain(ability):
                return False
        return True

    def _resolve_quit_game(self, transaction: RulesTransaction) -> bool:
        if transaction.player_id not in self.player_ids:
            return False
        resolver = self.projection("quit_game")
        if resolver is not None:
            handled = bool(resolver(transaction))
            if not handled:
                return False
            self.eliminated_player_ids.add(transaction.player_id)
            if len(self.eliminated_player_ids) >= len(self.player_ids):
                self.terminated = True
            return True
        self.eliminated_player_ids.add(transaction.player_id)
        # A single-player session is terminal; multiplayer sessions remain
        # live until the normal elimination/state-based flow concludes.
        if len(self.eliminated_player_ids) >= len(self.player_ids):
            self.terminated = True
        return True

    def _resolve_set_turn_phases(self, transaction: RulesTransaction) -> bool:
        if transaction.player_id not in self.player_ids:
            return False
        resolver = self.projection("turn_phase_transaction")
        if resolver is not None:
            handled = bool(resolver(transaction))
            if handled:
                self.turn_phase_preferences[transaction.player_id] = {
                    "self": tuple(transaction.payload.get("self_phases", ())),
                    "opponent": tuple(transaction.payload.get("opponent_phases", ())),
                }
            return handled
        self.turn_phase_preferences[transaction.player_id] = {
            "self": tuple(transaction.payload.get("self_phases", ())),
            "opponent": tuple(transaction.payload.get("opponent_phases", ())),
        }
        return True

    def _resolve_request_player_options(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("player_options")
        if resolver is None:
            return False
        return bool(resolver(transaction))

    def _resolve_send_state_checksum(self, transaction: RulesTransaction) -> bool:
        if self._checksum_resolver is None:
            # The extracted client implementation documents this transaction
            # as defunct: it is a diagnostic report, not a state transition.
            # Once the typed payload has passed ingress validation, the
            # authoritative session can acknowledge it without a host-side
            # mutation adapter.
            return True
        return bool(self._checksum_resolver(transaction))

    def _resolve_encounter_mod_dialog(self, transaction: RulesTransaction) -> bool:
        resolver = self.projection("encounter_mod")
        if resolver is None:
            return False
        return bool(resolver(transaction))

    def is_player_eliminated(self, player_id) -> bool:
        return player_id in self.eliminated_player_ids

    def pass_player_priority(self, player_id) -> bool:
        """Port of ``AuthoritativeSessionBase.PassPlayerPriority`` dispatch."""
        from .kernel import PriorityWindowAction

        action = self.action_stack.peek()
        if not isinstance(action, PriorityWindowAction):
            return False
        return action.pass_priority(player_id)

    def coerce_transaction_player_id(self, player_id):
        """Map a wire/raw participant ID onto this session's typed identity."""
        try:
            incoming = _uid_value(player_id)
        except (TypeError, ValueError):
            return player_id
        for participant in self.player_ids:
            try:
                if _uid_value(participant) == incoming:
                    return participant
            except (TypeError, ValueError):
                continue
        return player_id

    def declare_attack(self, player_id, defending_card, attacking_card):
        """Session-owned counterpart of the client's ``DeclareAttack`` call.

        Card legality belongs to the transaction requirement/target adapter;
        once validated, combat identity and declaration belong to the shared
        combat manager rather than the transport handler.
        """
        # Compare across raw/typed UID domains; a strict ``!=`` rejected the
        # client's CommitTroopsToAttack when the wire carried the raw id.
        player_id = self.coerce_transaction_player_id(player_id)
        if player_id != self.active_player_id:
            return None
        existing = self.combat_manager.combat_for_attacker(attacking_card)
        if existing is not None:
            return existing
        combat = self.combat_manager.create_attack(CombatId(), player_id,
                                                   defending_card)
        combat.declare_attacker(attacking_card)
        return combat

    def chain_top(self):
        return self.chain.peek_ability()

    def request_activation_data(self, ability, prompts: tuple) -> None:
        responsible = getattr(ability, "responsible_player_id", None)
        if responsible is None:
            responsible = getattr(ability, "activating_player_id", None)
        self.pending_activation = {
            "ability_instance_id": int(ability.instance_id),
            "responsible_player_id": (None if responsible is None
                                      else _uid_value(responsible)),
            "continuation": _json_value(ability.continuation()),
            "prompts": _json_value(prompts),
        }
        # A prompt is a transaction boundary: persist before awaiting the
        # client's later target/choice transaction.
        self.persist()
        if self.event_sink is not None:
            self.event_sink.ability_activation_data_required(ability, prompts)
        if self._activation_requester is not None:
            self._activation_requester(ability, prompts)

    def resume_activation_data(self, player_id, ability_instance_id,
                               activation_data) -> bool:
        """Port ``SetAbilityActivationDataTransaction`` to the waiting action.

        The reply mutates the existing ability and persists that boundary; the
        waiting push action runs again on the next scheduler tick. This keeps
        the C# order: request, reply, finish/push, then priority/resolve.
        """
        try:
            instance_id = int(ability_instance_id)
        except (TypeError, ValueError):
            return False
        ability = self.ability_manager.get(instance_id)
        if ability is None or not hasattr(ability, "bind_activation"):
            return False
        responsible = getattr(ability, "responsible_player_id", None)
        if responsible != player_id:
            return False
        if not ability.bind_activation(activation_data):
            return False
        if not self.pay_ability_cost(ability):
            return False
        pending = self.pending_activation
        if (isinstance(pending, dict) and
                pending.get("ability_instance_id") == instance_id):
            self.pending_activation = None
        if not self._in_transaction_handler:
            self.persist()
        return True

    def pay_ability_cost(self, ability) -> bool:
        """Pay at most once, as C# tracks payment on the ability instance."""
        if getattr(ability, "paid", False):
            return True
        if self._ability_cost_payer is not None and not self._ability_cost_payer(ability):
            return False
        ability.paid = True
        return True

    def finish_ability_on_chain(self, ability, *, free: bool = False) -> bool:
        if self.chain.contains_ability(ability.instance_id):
            return False
        ability.free = bool(free)
        if not self.pay_ability_cost(ability):
            return False
        self.pending_activation = None
        self.chain.push_ability(ability)
        self.ability_manager.activate_chain(ability.instance_id)
        self.push_game_action(ResolveTopOfChainAction(ability))
        metadata = getattr(ability, "metadata", None)
        graph = getattr(metadata, "graph", None)
        manual_or_triggered = bool(
            getattr(ability, "is_triggered", False) or
            getattr(graph, "manual", False))
        if (manual_or_triggered and
                not bool(getattr(ability, "ignores_chain", False)) and
                not bool(getattr(ability, "untargeted_trigger", False))):
            # Client Session.FinishAbilityOnChain inserts the response window
            # above ResolveTopOfChainAction for manual and triggered abilities.
            # Without this, native PvP activations resolved immediately and
            # could not be interrupted by the opposing player.
            self.push_game_action(PriorityWindowAction(
                TurnPhasePlayers.ALL, ability))
        # Keep the authoritative native lifecycle and the existing client
        # projection together.  Without this event a manual ability can be
        # paid and queued successfully while Unity never enters its chain UI.
        if self.event_sink is not None:
            self.event_sink.ability_pushed_on_chain(ability)
            if self.action_stack.priority_player_id is not None:
                self.event_sink.green_light(
                    self.action_stack.priority_player_id,
                    game_engine.EPriorityContext.Normal)
        return True

    def resolve_top_of_chain(self, ability_instance_id: int) -> AbilityResolutionState:
        ability = self.chain.peek_ability(ability_instance_id)
        if ability is None:
            return AbilityResolutionState.COMPLETED
        if self._ability_resolver is None:
            return AbilityResolutionState.WAITING_FOR_INPUT
        state = self._ability_resolver(ability)
        if state is AbilityResolutionState.COMPLETED:
            popped = self.chain.pop_ability(ability_instance_id)
            # ``Session.RemoveFromTopOfChain`` removes the manager entry once
            # resolution is over, except for an ability with ongoing effects.
            if popped is not None and not getattr(popped, "has_ongoing_effects", False):
                self.ability_manager.remove(ability_instance_id)
            # Drop the durable projected-chain descriptor too.  Leaving it in
            # ``_projected_chain_descriptors`` lets the next reattach's
            # ``rehydrate_projected_chain`` re-queue the resolved item, so the
            # ability resolves again on every subsequent transaction.
            self.forget_projected_chain(ability_instance_id)
            # Effect application and chain removal form one authoritative
            # resolution boundary for reconnect/persistence.
            self.persist()
            # C# returns priority immediately after the resolved stack item
            # is removed.  Without this checkpoint the client remains on the
            # chain UI even though the authoritative state is complete.
            if (self.pending_activation is None and
                    self.action_stack.priority_player_id is not None and
                    self.event_sink is not None):
                self.event_sink.green_light(
                    self.action_stack.priority_player_id,
                    game_engine.EPriorityContext.Normal)
        return state

    def _participant_index(self, player_id):
        """Find a participant across raw and typed UID checkpoint forms."""
        try:
            player_value = _uid_value(player_id)
        except (TypeError, ValueError):
            player_value = player_id
        for index, participant in enumerate(self.player_ids):
            if participant is player_id or participant == player_id:
                return index
            try:
                if _uid_value(participant) == player_value:
                    return index
            except (TypeError, ValueError):
                continue
        raise ValueError(f"unknown session participant: {player_id!r}")

    def player_ids_in_turn_order(self):
        active_at = self._participant_index(self.active_player_id)
        return self.player_ids[active_at:] + self.player_ids[:active_at]

    def player_ids_in_priority_order(self):
        priority = self.action_stack.priority_player_id
        try:
            index = self._participant_index(priority)
        except ValueError:
            return self.player_ids_in_turn_order()
        return self.player_ids[index:] + self.player_ids[:index]

    def defending_player_ids(self):
        try:
            active_at = self._participant_index(self.active_player_id)
        except ValueError:
            return tuple(self.player_ids)
        return tuple(pid for index, pid in enumerate(self.player_ids)
                     if index != active_at)

    @property
    def has_combats(self) -> bool:
        if self.combat_manager.combats:
            return True
        # AI declarations are projected into the persisted battle state while
        # the native combat object is rehydrated between client transactions.
        # Keep the phase graph from skipping DeclareDefense/AssignDamage when
        # that rehydration has not recreated the object yet.
        state = getattr(getattr(self, "runtime_facts", None),
                        "battle_state", {}) or {}
        return bool(state.get("ai_attackers") or
                    state.get("player_attackers"))

    @property
    def combat_has_first_strike(self) -> bool:
        if self.combat_manager.combat_cares_about_phase(CombatPhase.FIRST_STRIKE):
            return True
        state = getattr(getattr(self, "runtime_facts", None),
                        "battle_state", {}) or {}
        facts = self.runtime_facts
        getter = getattr(facts, "get_card", None)
        if callable(getter):
            for raw_uid in tuple((state.get("ai_attackers") or {}).keys()) + \
                    tuple((state.get("player_attackers") or {}).keys()):
                try:
                    card = getter(int(raw_uid))
                except (TypeError, ValueError):
                    card = None
                if card is not None and card.cares_about_combat_phase(
                        CombatPhase.FIRST_STRIKE):
                    return True
        return False

    @property
    def combat_has_standard_damage(self) -> bool:
        if self.combat_manager.combat_cares_about_phase(CombatPhase.STANDARD):
            return True
        state = getattr(getattr(self, "runtime_facts", None),
                        "battle_state", {}) or {}
        return bool(state.get("ai_attackers") or
                    state.get("player_attackers"))

    def can_player_pass_priority(self, player_id) -> bool:
        return player_id in self.player_ids and not self.terminated

    def hand_larger_than_maximum(self, player_id) -> bool:
        # Card-zone policy belongs in the future Player/Card port.
        return False

    def send_turn_phase_update(self) -> None:
        if self.event_sink is not None:
            self.event_sink.turn_phase_updated(self.current_turn_phase,
                                               self.active_player_id,
                                               self.action_stack.priority_player_id)

    def send_green_light(self, player_id,
                         context=game_engine.EPriorityContext.Normal) -> None:
        if self.event_sink is not None:
            self.event_sink.green_light(player_id, context)

    def transition_to(self, next_phase) -> None:
        require_legal_transition(self.current_turn_phase, next_phase)
        self._phase_transition_depth += 1
        try:
            old_state = self.phase_states.get(
                phase_name(self.current_turn_phase))
            if old_state is not None:
                old_state.on_exit(self)
            self.current_turn_phase = next_phase
            new_state = self.phase_states.get(phase_name(next_phase))
            if new_state is not None:
                new_state.on_entry(self)
            self.send_turn_phase_update()
            # Phase transitions are authoritative scheduler mutations too.
            # Save after the client-visible update so reconnect resumes here.
            self.persist()
        finally:
            self._phase_transition_depth -= 1

    def advance_turn_phase(self):
        """Use the current C# phase-state object to choose the next phase.

        ``GameActionStack`` owns priority/chain work inside a phase. Once it
        empties, the client tick invokes the state's `GetNextTurnPhase`; doing
        it here makes that handoff explicit and leaves card-specific branch
        facts behind the existing session adapters.
        """
        state = self.phase_states.get(phase_name(self.current_turn_phase))
        if state is None:
            return None
        next_phase = state.get_next_turn_phase(self)
        if isinstance(next_phase, str):
            next_phase = getattr(game_engine.ETurnPhases, next_phase)
        # ``EndTurnState`` hands control to the next player before entering
        # that player's StartTurnState.  Keep this rotation in the native
        # scheduler; leaving it to a mode handler makes the supposedly native
        # phase graph depend on the legacy turn cursor and can run the same
        # player's start-turn lifecycle twice.
        if (phase_name(self.current_turn_phase) == "EndTurn" and
                phase_name(next_phase) == "StartTurn" and
                self.player_ids):
            # Reconnect checkpoints may still carry the uint64 form while
            # the native participant tuple contains UID wrappers.  Strict
            # tuple.index() then returns -1 and the fallback selects player 0
            # again, so a human turn never rotates back to the AI.
            try:
                active_value = _uid_value(self.active_player_id)
            except (TypeError, ValueError):
                active_value = self.active_player_id
            def same_participant(participant):
                try:
                    return _uid_value(participant) == active_value
                except (TypeError, ValueError):
                    return participant == self.active_player_id

            active_index = next(
                (index for index, participant in enumerate(self.player_ids)
                 if same_participant(participant)),
                -1)
            if os.environ.get("HEX_RULES_PORT_TRACE"):
                print(
                    "[rules-trace] end-turn-rotation "
                    f"active={self.active_player_id!r} "
                    f"players={self.player_ids!r} "
                    f"active_index={active_index}",
                    flush=True)
            self.active_player_id = self.player_ids[
                (active_index + 1) % len(self.player_ids)]
            self.auto_pass_states.clear()
            self.active_player_skips_draw = False
            self.active_player_skips_attack = False
            self.has_legal_attackers = False
            self.has_legal_blockers = False
            self.has_forced_attackers = False
            self.has_extra_combats = False
            self.combat_manager.combats.clear()
            if self._turn_boundary_resolver is not None:
                boundary_result = self._turn_boundary_resolver(
                    self.active_player_id)
                # A mode adapter may return a typed override when its durable
                # checkpoint selects a bonus/current-player turn.  The
                # ordinary bool return remains a projection acknowledgement.
                if (boundary_result not in (None, True, False) and
                        boundary_result in self.player_ids):
                    self.active_player_id = boundary_result
            if os.environ.get("HEX_RULES_PORT_TRACE"):
                print(
                    "[rules-trace] end-turn-rotated "
                    f"active={self.active_player_id!r}", flush=True)
        self.transition_to(next_phase)
        return next_phase

    def push_game_action(self, action: GameAction) -> None:
        self.action_stack.push(action)

    def push_game_action_behind(self, action: GameAction) -> None:
        self.action_stack.push_behind(action)

    def queue_projected_chain(self, descriptor, owner_id, *, first_player_id=None):
        """Queue a mode-projected card in the native response lifecycle."""
        descriptor = dict(descriptor or {})
        try:
            instance_id = int(descriptor.get("instance_id", 0) or 0)
        except (TypeError, ValueError):
            instance_id = 0
        if instance_id <= 0:
            raise ValueError("projected chain item needs an instance id")
        descriptors = getattr(self, "_projected_chain_descriptors", None)
        if descriptors is None:
            descriptors = self._projected_chain_descriptors = {}
        if instance_id in descriptors and self.chain.contains_ability(instance_id):
            return self.chain.peek_ability(instance_id)
        persisted = dict(descriptor)
        persisted.setdefault("owner_id", _serial_id(owner_id))
        descriptors[instance_id] = _json_value(persisted)
        ability = ProjectedChainAbility(instance_id, descriptor, owner_id)
        self.chain.push_ability(ability)
        self.ability_manager.activate_chain(instance_id)
        self.push_game_action(ResolveTopOfChainAction(ability))
        if first_player_id is not None:
            self.action_stack.priority_player_id = first_player_id
        # Manual/triggered abilities use the same ALL-player response window
        # as the client session. Card-play projections remain ACTIVE-only in
        # the generic practice host; PvP overrides this method with its own
        # two-human ALL-player window.
        priority_players = (TurnPhasePlayers.ALL
                            if descriptor.get("kind") in ("ability", "trigger")
                            else TurnPhasePlayers.ACTIVE)
        priority_action = PriorityWindowAction(priority_players, ability)
        self.push_game_action(priority_action)
        if first_player_id is not None:
            # Preserve APNAP order while allowing a mode adapter to nominate
            # the first responder for a server-driven activation.  An
            # ACTIVE-only window normally contains just the active player,
            # but an AI-owned card is deliberately handed to the human first
            # so the human can respond.  Insert that nominated participant
            # even when the policy queue did not include it.
            from collections import deque
            queue = list(priority_action._priority_queue)
            try:
                queue.remove(first_player_id)
            except ValueError:
                # The explicit responder is authoritative for a host-driven
                # activation; do not leave the queue owned by the AI merely
                # because the generic ACTIVE policy was used to construct it.
                # The AI's decision has already happened, so an ACTIVE window
                # with a non-active first responder contains only that one
                # responder. ALL-player APNAP windows retain the remainder.
                if priority_players is TurnPhasePlayers.ACTIVE:
                    queue.clear()
            queue.insert(0, first_player_id)
            priority_action._priority_queue = deque(queue)
            self.action_stack.priority_player_id = first_player_id
        return ability

    def ensure_projected_chain_action(self) -> bool:
        """Keep the native action stack aligned with an active projected chain.

        The projected descriptor and chain ids are durable, while action
        objects are intentionally rebuilt on reconnect. A compatibility
        checkpoint can therefore contain a live chain together with an old
        phase ``PriorityWindowAction``. Letting that ordinary window consume
        a pass leaves the chain unresolved forever. Rebuild the response
        window around the existing chain item before ingress handles another
        transaction.
        """
        if self.chain.is_empty:
            return False
        ability = self.chain.peek_ability()
        if ability is None:
            return False
        top = self.action_stack.peek()
        if isinstance(top, PriorityWindowAction):
            responding_to = getattr(top, "ability_responding_to", None)
            # Action objects are rebuilt independently from the durable
            # projected-chain descriptor on a reconnect/reload.  They are
            # therefore not guaranteed to retain Python object identity with
            # the Chain's freshly rehydrated ability.  The ability instance
            # id is the authoritative identity at this boundary.  Treating
            # two wrappers for that same id as different actions rebuilt the
            # response window on every client pass, putting priority back on
            # the human and leaving the ability permanently on the chain.
            try:
                same_chain_item = (
                    int(getattr(responding_to, "instance_id", -1)) ==
                    int(getattr(ability, "instance_id", -2)))
            except (TypeError, ValueError):
                same_chain_item = False
            if same_chain_item:
                return False
        elif isinstance(top, ResolveTopOfChainAction):
            # The chain item is already in its resolve lifecycle: the response
            # window above it was consumed by the last pass and only the
            # resolver remains.  The interrupted phase window BENEATH it must
            # be preserved.  Clearing the stack here dropped FirstMainPhase, so
            # once the item resolved the stack was empty and
            # ``advance_turn_phase`` moved the turn on even though the player
            # had merely resolved their own spell.
            try:
                same_chain_item = (
                    int(getattr(getattr(top, "ability", None),
                                "instance_id", -1)) ==
                    int(getattr(ability, "instance_id", -2)))
            except (TypeError, ValueError):
                same_chain_item = False
            if same_chain_item:
                return False

        # The chain is authoritative here; any ordinary phase action is stale
        # relative to it. Recreate the same LIFO pair used by
        # ``queue_projected_chain`` without adding the chain item twice.
        first_player_id = self.action_stack.priority_player_id
        self.action_stack.clear()
        self.push_game_action(ResolveTopOfChainAction(ability))
        descriptor = getattr(ability, "descriptor", {}) or {}
        priority_players = (TurnPhasePlayers.ALL
                            if descriptor.get("kind") == "ability"
                            else TurnPhasePlayers.ACTIVE)
        priority_action = PriorityWindowAction(priority_players, ability)
        self.push_game_action(priority_action)
        if first_player_id is not None:
            from collections import deque
            queue = list(priority_action._priority_queue)
            if first_player_id in queue:
                queue.remove(first_player_id)
            elif priority_players is TurnPhasePlayers.ACTIVE:
                queue.clear()
            # The durable chain checkpoint can nominate the non-active human
            # for an AI-owned card. Rebuilding an ACTIVE response action must
            # preserve that explicit responder rather than restoring an AI
            # queue that rejects the client's next pass.
            queue.insert(0, first_player_id)
            priority_action._priority_queue = deque(queue)
            self.action_stack.priority_player_id = first_player_id
        return True

    def forget_projected_chain(self, instance_id) -> None:
        try:
            self._projected_chain_descriptors.pop(int(instance_id), None)
        except (AttributeError, TypeError, ValueError):
            return

    def _checkpoint_chain_descriptors(self):
        """Read the durable compatibility chain from the battle checkpoint."""
        state = None
        facts = getattr(self, "runtime_facts", None)
        candidate = getattr(facts, "battle_state", None) if facts else None
        if isinstance(candidate, Mapping):
            state = candidate
        if state is None:
            session = getattr(getattr(self, "snapshot_store", None),
                              "game_session", None)
            candidate = getattr(session, "_rules_port_battle_state", None)
            if not isinstance(candidate, Mapping):
                candidate = getattr(session, "turn_order", None)
            if isinstance(candidate, Mapping):
                state = candidate
        items = state.get("stack") if isinstance(state, Mapping) else None
        return list(items) if isinstance(items, (list, tuple)) else []

    def _durable_chain_descriptors(self) -> list:
        """Return the pending chain descriptors in durable resolution order.

        ``projected_chain`` is the port-owned projection of the native chain
        at the last save.  The battle checkpoint's ``stack`` list is the same
        projection for the compatibility wire format, so including it keeps an
        item queued in an earlier transaction even when a later checkpoint
        already lost its native counterpart.
        """
        ordered: list = []
        seen: set = set()

        def add(descriptor):
            if not isinstance(descriptor, Mapping):
                return
            try:
                instance_id = int(descriptor.get("instance_id", 0) or 0)
            except (TypeError, ValueError):
                return
            if instance_id <= 0 or instance_id in seen:
                return
            seen.add(instance_id)
            ordered.append(dict(descriptor))

        for descriptor in self._checkpoint_chain_descriptors():
            add(descriptor)
        projected = getattr(self, "_projected_chain_descriptors", {}) or {}
        for instance_id in getattr(self, "restored_chain_instance_ids", ()):
            add(projected.get(instance_id))
        for descriptor in projected.values():
            add(descriptor)
        return ordered

    def rehydrate_projected_chain(self) -> bool:
        """Rebuild every pending projected chain item after a reconnect.

        Practice rebuilds this host from the persisted checkpoint for each
        client transaction, so a chain item queued in an earlier transaction
        only survives when its durable descriptor is re-created here.  Rebuild
        only the most recent descriptor (or none at all) left the durable
        ``stack`` projecting items no native action owned: their effects never
        resolved, and ``stack_empty`` stayed false for the rest of the game,
        which suppressed BasicAction activations such as Tunnel.
        """
        if self.chain._instance_ids:
            return False
        restored = False
        for descriptor in self._durable_chain_descriptors():
            try:
                instance_id = int(descriptor.get("instance_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if instance_id <= 0 or instance_id in self.chain._instance_ids:
                continue
            owner_id = descriptor.get("owner_id")
            if owner_id is None:
                owner_id = descriptor.get("source_owner_uid")
            if owner_id is None:
                owner_id = self.active_player_id
            try:
                self.queue_projected_chain(
                    descriptor, owner_id,
                    first_player_id=self.action_stack.priority_player_id)
            except (TypeError, ValueError):
                continue
            restored = instance_id in self.chain._instance_ids or restored
        return restored

    def chain_can_resolve(self) -> bool:
        """Port of ``TurnPhaseState.ChainCanResolve`` for the live phase."""
        state = self.phase_states.get(phase_name(self.current_turn_phase))
        return True if state is None else state.chain_can_resolve()

    def tick(self) -> bool:
        """Perform one C#-ordered scheduler step; never await a UI callback."""
        if self.terminated or phase_name(self.current_turn_phase) == "NotPlaying":
            return False
        if self._state_based_handler is not None and self._state_based_handler():
            return True
        # C# InternalTick2 drains the trigger queue and finishes queued plays
        # only when the phase permits chain resolution and no chain action owns
        # the top of the stack.
        from .kernel import PriorityWindowAction
        if (self.chain_can_resolve() and
                (self.action_stack.count == 0 or
                 isinstance(self.action_stack.peek(), PriorityWindowAction))):
            self.handle_game_event()
            if self.finish_playing_cards():
                return True
        if self.action_stack.count == 0:
            return self.advance_turn_phase() is not None
        if self.action_stack.update():
            return True
        if self.handle_transaction():
            return True
        self.action_stack.post_update()
        return False

    def drive_until_input(self, max_steps=64) -> int:
        """Run native phase/action scheduling until a client input is needed.

        A service may use this after setup or a priority response.  It is
        deliberately bounded so a malformed phase branch cannot monopolize a
        request worker; the returned count is also useful in trace tests.
        """
        steps = 0
        while steps < int(max_steps):
            steps += 1
            progressed = self.tick()
            if not progressed:
                break
        return steps

    def auto_pass_internal_priority(self, player_ids=None) -> bool:
        """Pass a native priority window for server-driven phases.

        AI/mode drivers use this only after deciding that no human stop is
        required.  The native action remains responsible for validation and
        queue ordering; this helper merely supplies the server-side passes.
        """
        action = self.action_stack.peek()
        if not isinstance(action, PriorityWindowAction):
            return False
        allowed = None if player_ids is None else set(player_ids)
        while action.priority_player_id is not None:
            player_id = action.priority_player_id
            if allowed is not None and player_id not in allowed:
                return False
            if not action.pass_priority(player_id):
                return False
        # Let the action stack perform its normal completion/on-exit path.
        self.action_stack.update()
        return True

    def snapshot(self) -> Dict[str, Any]:
        z, w = self.random_number_generator.get_seed()
        projected = getattr(self, "_projected_chain_descriptors", {})
        active_ids = set(self.chain._instance_ids)
        return {
            "version": 1,
            "phase": phase_name(self.current_turn_phase),
            "active_player_id": _serial_id(self.active_player_id),
            "priority_player_id": (None if self.action_stack.priority_player_id is None
                                   else _serial_id(self.action_stack.priority_player_id)),
            "seed_z": z,
            "seed_w": w,
            "total_turns_taken": self.total_turns_taken,
            "terminated": bool(self.terminated),
            "eliminated_player_ids": [_serial_id(player)
                                       for player in self.eliminated_player_ids],
            "turn_phase_preferences": {
                _serial_id(player): _json_value(value)
                for player, value in self.turn_phase_preferences.items()
            },
            "pending_activation": _json_value(self.pending_activation),
            "action_stack": [
                {"type": action.__class__.__name__,
                 "instance_id": int(getattr(action, "instance_id", 0))}
                for action in self.action_stack._stack
            ],
            "chain_instance_ids": [int(instance_id)
                                   for instance_id in self.chain._instance_ids],
            "combats": [
                {"attacker_id": _serial_id(combat.attacker.session_card_id),
                 "defender_id": _serial_id(combat.defender.session_card_id)
                 if hasattr(combat.defender, "session_card_id")
                 else _serial_id(combat.defender),
                 "combat_serial": int(combat.combat_id.serial_number),
                 "blocker_ids": [
                     _serial_id(blocker.session_card_id)
                     for blocker in combat.blockers
                     if hasattr(blocker, "session_card_id")
                 ],
                 "flags": int(combat.flags),
                 "resolved_phases": int(combat.resolved_phases)}
                for combat in self.combat_manager.combats
                if combat.attacker is not None
            ],
            "projected_chain": [
                _json_value(descriptor)
                for instance_id, descriptor in projected.items()
                if int(instance_id) in active_ids
            ],
            "transaction_history": _json_value(self._transaction_history),
            "auto_pass_states": {_serial_id(player): _json_value(state)
                                 for player, state in self.auto_pass_states.items()},
        }

    def restore_snapshot(self, saved: Mapping[str, Any] | None) -> bool:
        """Rehydrate persisted scheduler state without replaying UI events.

        Reconnects must resume from the last transaction boundary.  We restore
        only values owned by this port; action/ability objects are deliberately
        not fabricated from JSON, so a caller can hydrate those through its
        normal Records/metadata factory before resuming a waiting action.
        """
        if not isinstance(saved, Mapping):
            return False

        phase = saved.get("phase")
        if phase is not None:
            candidate = getattr(game_engine.ETurnPhases, str(phase), None)
            if candidate is not None:
                self.current_turn_phase = candidate

        def known_player(value):
            if value is None:
                return None
            try:
                value = int(getattr(value, "uid64", value))
            except (TypeError, ValueError):
                for player_id in self.player_ids:
                    if player_id == value or str(player_id) == str(value):
                        return player_id
                return None
            for player_id in self.player_ids:
                try:
                    if _uid_value(player_id) == value:
                        return player_id
                except (TypeError, ValueError):
                    if player_id == value:
                        return player_id
            return None

        active = known_player(saved.get("active_player_id"))
        if active is not None:
            self.active_player_id = active
        priority = known_player(saved.get("priority_player_id"))
        self.action_stack.priority_player_id = priority

        try:
            self.random_number_generator.set_seed(int(saved["seed_z"]),
                                                   int(saved["seed_w"]))
        except (KeyError, TypeError, ValueError):
            pass
        try:
            self.total_turns_taken = int(saved.get("total_turns_taken", 0))
        except (TypeError, ValueError):
            pass
        self.terminated = bool(saved.get("terminated", False))
        eliminated = saved.get("eliminated_player_ids", ())
        if isinstance(eliminated, (list, tuple)):
            self.eliminated_player_ids = {
                resolved for value in eliminated
                if (resolved := known_player(value)) is not None
            }
        preferences = saved.get("turn_phase_preferences", {})
        if isinstance(preferences, Mapping):
            self.turn_phase_preferences = {}
            for player, value in preferences.items():
                resolved = known_player(player)
                if resolved is not None and isinstance(value, Mapping):
                    self.turn_phase_preferences[resolved] = {
                        "self": tuple(value.get("self", ())),
                        "opponent": tuple(value.get("opponent", ())),
                    }
        pending = saved.get("pending_activation")
        self.pending_activation = (dict(pending) if isinstance(pending, Mapping)
                                   else None)
        auto_pass = saved.get("auto_pass_states", {})
        if isinstance(auto_pass, Mapping):
            self.auto_pass_states = {}
            for player, state in auto_pass.items():
                resolved_player = known_player(player)
                if resolved_player is not None:
                    self.auto_pass_states[resolved_player] = state
        history = saved.get("transaction_history", ())
        if isinstance(history, (list, tuple)):
            self._transaction_history = [dict(item) for item in history
                                         if isinstance(item, Mapping)]
        descriptors = saved.get("action_stack", ())
        if isinstance(descriptors, (list, tuple)):
            restored = []
            for descriptor in descriptors:
                if not isinstance(descriptor, Mapping):
                    continue
                action_type = descriptor.get("type")
                if not action_type:
                    continue
                try:
                    instance_id = int(descriptor.get("instance_id", 0))
                except (TypeError, ValueError):
                    instance_id = 0
                restored.append({"type": str(action_type),
                                 "instance_id": instance_id})
            self.restored_action_descriptors = tuple(restored)
        chain_ids = saved.get("chain_instance_ids", ())
        if isinstance(chain_ids, (list, tuple)):
            try:
                self.restored_chain_instance_ids = tuple(int(value) for value in chain_ids)
            except (TypeError, ValueError):
                self.restored_chain_instance_ids = ()
        self._projected_chain_descriptors = {}
        projected = saved.get("projected_chain", ())
        if isinstance(projected, (list, tuple)):
            for descriptor in projected:
                if not isinstance(descriptor, Mapping):
                    continue
                try:
                    instance_id = int(descriptor.get("instance_id", 0))
                except (TypeError, ValueError):
                    continue
                if instance_id > 0:
                    self._projected_chain_descriptors[instance_id] = dict(descriptor)
        self.restored_combat_descriptors = ()
        combats = saved.get("combats", ())
        if isinstance(combats, (list, tuple)):
            self.restored_combat_descriptors = tuple(
                dict(descriptor) for descriptor in combats
                if isinstance(descriptor, Mapping))
        return True

    def restore_actions(self, factory: Callable[[Mapping[str, Any]], GameAction]) -> bool:
        """Hydrate persisted actions through an explicit caller-owned factory."""
        if not callable(factory):
            return False
        actions = []
        try:
            for descriptor in self.restored_action_descriptors:
                action = factory(descriptor)
                if not isinstance(action, GameAction):
                    return False
                action.initialize(self.action_stack, self)
                if descriptor.get("instance_id"):
                    action.instance_id = int(descriptor["instance_id"])
                actions.append(action)
        except (TypeError, ValueError, KeyError):
            return False
        self.action_stack._stack = actions
        self.action_stack.current_action = None
        return True

    def restore_chain_abilities(self, abilities) -> bool:
        """Hydrate persisted chain IDs from caller-created ability instances."""
        try:
            by_id = {int(ability.instance_id): ability for ability in abilities}
            ids = self.restored_chain_instance_ids
        except (TypeError, ValueError, AttributeError):
            return False
        if any(instance_id not in by_id for instance_id in ids):
            return False
        self.chain.clear()
        for instance_id in ids:
            self.ability_manager.add_chain(instance_id, by_id[instance_id])
            self.chain._instance_ids.append(instance_id)
        return True

    def rehydrate_combats(self) -> bool:
        """Rebuild native combat identity from the persisted descriptors."""
        if self.combat_manager.combats or not self.restored_combat_descriptors:
            return False
        facts = self.runtime_facts
        get_card = getattr(facts, "get_card", None)
        if not callable(get_card):
            return False

        def as_int(value):
            return int(getattr(value, "uid64", value))

        try:
            for descriptor in self.restored_combat_descriptors:
                attacker_id = as_int(descriptor["attacker_id"])
                defender_id = as_int(descriptor["defender_id"])
                attacker = get_card(attacker_id)
                if attacker is None:
                    return False
                defender = get_card(defender_id) or defender_id
                serial = int(descriptor.get("combat_serial", attacker_id & 0xFFFF))
                combat = self.combat_manager.create_attack(
                    CombatId(attacker_id, serial), self.active_player_id,
                    defender)
                combat.declare_attacker(attacker)
                blockers = []
                for blocker_id in descriptor.get("blocker_ids", ()):
                    blocker = get_card(as_int(blocker_id))
                    if blocker is None:
                        return False
                    blockers.append(blocker)
                combat.blockers = blockers
                combat.flags = CombatFlags(int(descriptor.get("flags", 0)))
                combat.resolved_phases = CombatPhase(
                    int(descriptor.get("resolved_phases", 0)))
            self.restored_combat_descriptors = ()
            return True
        except (KeyError, TypeError, ValueError):
            self.combat_manager.clear()
            return False

    def persist(self, conn=None) -> None:
        if self.snapshot_store is not None:
            self.snapshot_store.save(self.snapshot(), conn=conn)
