"""Authoritative Python session host derived from the C# session tick loop.

This is intentionally an opt-in adapter. Once attached, its scheduler
snapshot is stored under ``turn_order['rules_port']`` in the same mutable
checkpoint consumed by the compatibility host; there is no second battle
state dictionary.

Source counterparts: ``Session.cs:InternalTick2`` and
``AuthoritativeSessionBase.cs:Tick``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Mapping, Optional

import game_engine
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
        return False

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
        self._turn_start_resolver: Optional[Callable[[], object]] = None
        self._turn_boundary_resolver: Optional[Callable[[object], object]] = None
        self._turn_phase_entry_resolver: Optional[Callable[[object], object]] = None
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

    def resolve_turn_phase_entry(self, phase=None):
        """Run the phase-entry projection after the native state transition."""
        if self._turn_phase_entry_resolver is None:
            return None
        result = self._turn_phase_entry_resolver(
            self.current_turn_phase if phase is None else phase)
        self.persist()
        return result

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
        # A cached host can cross a phase boundary while its old compatibility
        # action is still in memory. A native chain response is distinct from
        # a phase window and must remain untouched.
        if (isinstance(top, PriorityWindowAction) and
                getattr(top, "ability_responding_to", None) is None and
                getattr(top, "_rules_port_phase", None) !=
                phase_name(self.current_turn_phase)):
            self.action_stack.clear()
            top = None
        if (ensure_current_priority and self.action_stack.count == 0 and
                current is not None and
                current.priority_players is not TurnPhasePlayers.NONE):
            from collections import deque
            priority_action = PriorityWindowAction(current.priority_players)
            priority_action._rules_port_phase = phase_name(
                self.current_turn_phase)
            self.action_stack.push(priority_action)
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

    def submit_transaction(self, transaction: RulesTransaction) -> bool:
        # The session wrapper can be rehydrated with a new checkpoint while
        # this native scheduler object is cached. Refresh the facts bridge at
        # the transaction boundary so validation never reads an attach-time
        # resource, card-state, or ownership snapshot.
        facts = self.runtime_facts
        game_session = getattr(self.snapshot_store, "game_session", None)
        if facts is not None and game_session is not None:
            from .persistence import load_state
            live_state = load_state(game_session)
            if isinstance(live_state, dict) and live_state:
                facts.battle_state = live_state
                # Practice/PvE stores the native phase at the checkpoint
                # cursor. PvP supplies its own raw-phase synchronization in
                # PvpAuthoritativeSession.submit_transaction.
                if not live_state.get("pvp"):
                    from .persistence import current_phase
                    live_phase = current_phase(live_state)
                    if live_phase is not None:
                        self.current_turn_phase = live_phase
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
        resolver = self.projection("attack_transaction")
        if resolver is not None:
            handled = bool(resolver(transaction))
            if not handled:
                for combat in staged:
                    self.combat_manager.remove_combat(combat.combat_id)
            return handled
        if self.action_stack.priority_player_id == transaction.player_id:
            return self.pass_player_priority(transaction.player_id)
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
        for attacker_id, blocker_ids in transaction.payload.get("declarations", ()):
            attacker = self.get_card(attacker_id)
            combats = self.combat_manager.combats_with_attacker(attacker)
            if not combats:
                return False
            blockers = tuple(self.get_card(card_id) for card_id in blocker_ids)
            if any(card is None for card in blockers):
                return False
            for combat in combats:
                combat.declare_blockers(blockers)
        if self.action_stack.priority_player_id == transaction.player_id:
            return self.pass_player_priority(transaction.player_id)
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

    def declare_attack(self, player_id, defending_card, attacking_card):
        """Session-owned counterpart of the client's ``DeclareAttack`` call.

        Card legality belongs to the transaction requirement/target adapter;
        once validated, combat identity and declaration belong to the shared
        combat manager rather than the transport handler.
        """
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

    def player_ids_in_turn_order(self):
        active_at = self.player_ids.index(self.active_player_id)
        return self.player_ids[active_at:] + self.player_ids[:active_at]

    def player_ids_in_priority_order(self):
        priority = self.action_stack.priority_player_id
        if priority not in self.player_ids:
            return self.player_ids_in_turn_order()
        index = self.player_ids.index(priority)
        return self.player_ids[index:] + self.player_ids[:index]

    def defending_player_ids(self):
        return tuple(pid for pid in self.player_ids if pid != self.active_player_id)

    @property
    def has_combats(self) -> bool:
        return bool(self.combat_manager.combats)

    @property
    def combat_has_first_strike(self) -> bool:
        return self.combat_manager.combat_cares_about_phase(CombatPhase.FIRST_STRIKE)

    @property
    def combat_has_standard_damage(self) -> bool:
        return self.combat_manager.combat_cares_about_phase(CombatPhase.STANDARD)

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
        old_state = self.phase_states.get(phase_name(self.current_turn_phase))
        if old_state is not None:
            old_state.on_exit(self)
        self.current_turn_phase = next_phase
        new_state = self.phase_states.get(phase_name(next_phase))
        if new_state is not None:
            new_state.on_entry(self)
        self.send_turn_phase_update()
        # Phase transitions are authoritative scheduler mutations too. Save
        # after the client-visible update so reconnect resumes at this phase.
        self.persist()

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
            try:
                active_index = self.player_ids.index(self.active_player_id)
            except ValueError:
                active_index = -1
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
        # The generic host has one client and an internally-driven AI; its
        # opponent does not submit a transaction. PvP overrides this method
        # with the two-human ALL-player window.
        priority_action = PriorityWindowAction(
            TurnPhasePlayers.ACTIVE, ability)
        self.push_game_action(priority_action)
        if first_player_id is not None:
            # ACTIVE windows normally begin with the session's active player,
            # but a server-driven AI card must expose the response window to
            # the human first. The mode adapter supplies that typed identity.
            from collections import deque
            priority_action._priority_queue = deque([first_player_id])
            self.action_stack.priority_player_id = first_player_id
        return ability

    def forget_projected_chain(self, instance_id) -> None:
        try:
            self._projected_chain_descriptors.pop(int(instance_id), None)
        except (AttributeError, TypeError, ValueError):
            return

    def rehydrate_projected_chain(self) -> bool:
        """Rebuild the active projected chain after reconnect."""
        descriptors = getattr(self, "_projected_chain_descriptors", {})
        if not descriptors or self.chain._instance_ids:
            return False
        descriptor = next(reversed(descriptors.values()))
        try:
            instance_id = int(descriptor.get("instance_id", 0) or 0)
        except (TypeError, ValueError):
            return False
        owner_id = descriptor.get("owner_id")
        if owner_id is None:
            return False
        self.queue_projected_chain(
            descriptor, owner_id,
            first_player_id=self.action_stack.priority_player_id)
        return instance_id in self.chain._instance_ids

    def tick(self) -> bool:
        """Perform one C#-ordered scheduler step; never await a UI callback."""
        if self.terminated or phase_name(self.current_turn_phase) == "NotPlaying":
            return False
        if self._state_based_handler is not None and self._state_based_handler():
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
