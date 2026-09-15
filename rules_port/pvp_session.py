"""RulesPort host for the existing two-human PvP checkpoint.

Tournament sessions use raw player ids and a numeric ``phase`` field in
``turn_order``. The generic kernel uses typed ServicePlayer UIDs and its own
phase/priority attributes. This adapter is the single translation boundary;
it does not create a second rules state or implement PvP decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

from .session import AuthoritativeSession, _json_value
from .actions import ResolveTopOfChainAction
from .kernel import PriorityWindowAction, TurnPhasePlayers
from .phases import phase_name
from . import pvp_lifecycle
import game_engine


def _raw_player_id(value) -> int:
    value = int(getattr(value, "uid64", value))
    return value >> 8 if (value & 0xFF) == 244 else value


class PvpAuthoritativeSession(AuthoritativeSession):
    """Generic RulesPort scheduler synchronized with a PvP checkpoint."""

    def snapshot(self):
        saved = super().snapshot()
        active_ids = set(self.chain._instance_ids)
        saved["projected_chain"] = [
            _json_value(descriptor)
            for instance_id, descriptor in getattr(
                self, "_projected_chain_descriptors", {}).items()
            if int(instance_id) in active_ids
        ]
        return saved

    def restore_snapshot(self, saved) -> bool:
        restored = super().restore_snapshot(saved)
        if not restored:
            return False
        self._projected_chain_descriptors = {}
        descriptors = saved.get("projected_chain", ()) if isinstance(saved, dict) else ()
        if isinstance(descriptors, (list, tuple)):
            for descriptor in descriptors:
                if not isinstance(descriptor, dict):
                    continue
                try:
                    instance_id = int(descriptor.get("instance_id", 0))
                except (TypeError, ValueError):
                    continue
                if instance_id > 0:
                    self._projected_chain_descriptors[instance_id] = dict(descriptor)
        return True

    def rehydrate_projected_chain(self) -> bool:
        """Rebuild the native response window after a reconnect.

        PvP's compatibility-shaped stack remains the durable wire/reconnect
        projection. Only its current descriptor is reified here; chain
        ownership and subsequent ordering stay with the native resolver.
        """
        descriptors = getattr(self, "_projected_chain_descriptors", {})
        if not descriptors or self.chain._instance_ids:
            return False
        descriptor = next(reversed(descriptors.values()))
        instance_id = int(descriptor.get("instance_id", 0) or 0)
        owner_id = descriptor.get("owner_id")
        if owner_id is None:
            owner_id = descriptor.get("owner_pid")
        if owner_id is None:
            owner_id = descriptor.get("player_id")
        if owner_id is None:
            return False
        self.queue_projected_chain(
            descriptor, owner_id,
            first_player_id=self.action_stack.priority_player_id)
        return instance_id in self.chain._instance_ids

    def forget_projected_chain(self, instance_id) -> None:
        """Drop a projected descriptor once its chain item has resolved."""
        try:
            self._projected_chain_descriptors.pop(int(instance_id), None)
        except (AttributeError, TypeError, ValueError):
            return

    def _uid_for_raw_player(self, raw_id):
        try:
            raw_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        for player_id in self.player_ids:
            if _raw_player_id(player_id) == raw_id:
                return player_id
        return None

    def coerce_transaction_player_id(self, player_id):
        """Use the typed participant UID used by the PvP native session."""
        try:
            raw_id = _raw_player_id(player_id)
        except (TypeError, ValueError):
            return player_id
        return self._uid_for_raw_player(raw_id) or player_id

    def sync_from_pvp_state(self, state) -> bool:
        """Refresh kernel ownership from the authoritative PvP state."""
        if not isinstance(state, dict) or not state.get("pvp"):
            return False
        try:
            self.current_turn_phase = int(state.get("phase"))
        except (TypeError, ValueError):
            return False
        active = self._uid_for_raw_player(state.get("turn_pid"))
        if active is not None:
            self.active_player_id = active
        priority = self._uid_for_raw_player(state.get("priority_pid"))
        self.action_stack.priority_player_id = priority
        # ``restore_snapshot`` restores the durable phase/action descriptor,
        # while the current PvP checkpoint is authoritative for who owns the
        # live window.  Updating only GameActionStack.priority_player_id is
        # insufficient: PriorityWindowAction reads its owner from its private
        # APNAP queue, so a reattach could reject a valid card/resource action
        # for the player shown by GreenLight.
        from collections import deque
        from .kernel import PriorityWindowAction
        action = self.action_stack.peek()
        if isinstance(action, PriorityWindowAction) and priority is not None:
            queue = list(getattr(action, "_priority_queue", ()) or ())
            if priority in queue:
                queue.remove(priority)
            queue.insert(0, priority)
            action._priority_queue = deque(queue)
            self.action_stack.priority_player_id = priority
        return True

    def configure_phase_priority(self, action) -> None:
        """Apply the persisted PvP first-priority owner to a new window.

        The generic phase states correctly decide whether a window is ACTIVE
        or ALL-player, but PvP's DeclareDefense window starts with the
        defending player.  Keeping this translation here means the service
        does not reach into the action stack during a pass.
        """
        state = None
        store = getattr(self.snapshot_store, "game_session", None)
        if store is not None:
            try:
                from .persistence import load_pvp_state, load_state
                state = load_pvp_state(store) or load_state(store)
            except Exception:
                state = None
        if not isinstance(state, dict):
            return
        try:
            phase = int(self.current_turn_phase)
            turn_pid = _raw_player_id(self.active_player_id)
            player_ids = [_raw_player_id(pid) for pid in self.player_ids]
            opponent_pid = next(pid for pid in player_ids if pid != turn_pid)
        except (StopIteration, TypeError, ValueError):
            return
        window_players = self.priority_players_for_phase(
            state, phase, turn_pid, opponent_pid)
        action.priority_players = window_players
        # The phase state constructs the action before the PvP stop policy is
        # known.  Changing only ``priority_players`` leaves the constructor's
        # original ACTIVE queue intact (notably at Discard), so the client can
        # receive a phantom GreenLight in a phase that should auto-complete.
        action.reset_priority_window(start_with_active_player=False)
        self._apply_persisted_passes(action, state)
        priority = self._uid_for_raw_player(state.get("priority_pid"))
        if priority is None:
            return
        queue = list(getattr(action, "_priority_queue", ()) or ())
        if priority in queue:
            from collections import deque
            queue.remove(priority)
            queue.insert(0, priority)
            action._priority_queue = deque(queue)
            self.action_stack.priority_player_id = priority

    @staticmethod
    def priority_players_for_phase(state, phase, turn_pid, opponent_pid):
        """Translate persisted self/opponent stops into a native window.

        This is the RulesPort phase-control policy.  The service adapter may
        project the resulting window, but must not independently decide which
        players receive priority.
        """
        phase = int(phase)
        turn_pid = int(turn_pid)
        opponent_pid = int(opponent_pid)
        lifecycle = pvp_lifecycle.lifecycle
        # These lifecycle states have no client priority window.  In
        # particular, StartGame is a legal phase transition but the client
        # must not receive a GreenLight for it while the native scheduler is
        # moving into StartTurn.
        if phase in {
                int(game_engine.ETurnPhases.StartGame),
                int(game_engine.ETurnPhases.StartTurn),
                int(game_engine.ETurnPhases.Ready),
                int(game_engine.ETurnPhases.Prep),
                int(game_engine.ETurnPhases.Draw)}:
            return TurnPhasePlayers.NONE
        if (phase == int(game_engine.ETurnPhases.Discard) and
                state.get("discard_required")):
            return TurnPhasePlayers.ACTIVE
        self_stops = set(lifecycle.SELF_ALWAYS_STOPS)
        self_stops.update(state.get(f"stops_self_{turn_pid}") or
                          lifecycle.SELF_DEFAULT_STOPS)
        opponent_stops = set(lifecycle.OPP_ALWAYS_STOPS)
        opponent_stops.update(state.get(f"stops_opp_{opponent_pid}") or
                              lifecycle.OPP_DEFAULT_STOPS)
        if phase == int(game_engine.ETurnPhases.DeclareDefense):
            return TurnPhasePlayers.DEFENDING
        if phase in self_stops and phase in opponent_stops:
            return TurnPhasePlayers.ALL
        if phase in self_stops:
            return TurnPhasePlayers.ACTIVE
        if phase in opponent_stops:
            return TurnPhasePlayers.ALL
        return TurnPhasePlayers.NONE

    def pass_priority_and_drive(self, player_id, *, max_steps=64) -> bool:
        """Consume one native pass and run the scheduler to its next input.

        PvP callers must not manually tick the action stack after a pass: an
        empty priority queue may require an action cleanup tick followed by a
        phase-transition tick.  Keeping that sequence here makes the native
        scheduler the sole owner of that boundary.
        """
        if not self.pass_player_priority(player_id):
            return False
        self.drive_until_input(max_steps=max_steps)
        return True

    def begin_pvp_turn(self, *, max_steps=128) -> int:
        """Leave mulligan and run the native first-turn lifecycle.

        PvP setup still uses its historical wire transactions through the
        mulligan checkpoint, but once both players have kept there must be a
        single phase owner.  Entering ``StartGame`` through the native phase
        graph lets the normal StartTurn/Ready/Prep/Draw entry hooks and the
        stop-policy priority actions drive the game to its first client input.
        The caller supplies only the initial checkpoint; it does not walk the
        phase list or push a parallel priority action.
        """
        if phase_name(self.current_turn_phase) == "Mulligan":
            self.transition_to(game_engine.ETurnPhases.StartGame)
        elif phase_name(self.current_turn_phase) != "StartGame":
            raise ValueError(
                "PvP turn start requires native Mulligan or StartGame phase")
        return self.drive_until_input(max_steps=max_steps)

    def auto_pass_waiting_player(self, state, waiting_player_id,
                                 *, has_quick_action=False) -> bool:
        """Auto-pass a native window when the persisted stop policy allows it."""
        phase = int(state.get("phase", self.current_turn_phase) or 0)
        if pvp_lifecycle.waiting_player_requires_priority(
                state, phase, _raw_player_id(waiting_player_id),
                has_quick_action=has_quick_action):
            return False
        return self.pass_priority_and_drive(
            self.coerce_transaction_player_id(waiting_player_id))

    def sync_to_pvp_state(self, state) -> bool:
        """Project native phase ownership into the PvP checkpoint shape."""
        if not isinstance(state, dict) or not state.get("pvp"):
            return False
        state["phase"] = int(self.current_turn_phase)
        state["turn_pid"] = _raw_player_id(self.active_player_id)
        priority = self.action_stack.priority_player_id
        if priority is None:
            state.pop("priority_pid", None)
        else:
            state["priority_pid"] = _raw_player_id(priority)
        return True

    def submit_transaction(self, transaction) -> bool:
        """Validate against the latest PvP phase before queueing an intent."""
        from .persistence import load_pvp_state, load_state
        session = getattr(self.snapshot_store, "game_session", None)
        # PvP's turn_order is the mode-owned checkpoint.  The shared native
        # reference can lag while setup/mulligan projections are being saved;
        # preferring it here prevents a stale Prep phase from replacing the
        # authoritative FirstMain phase during card validation.
        state = (load_pvp_state(session) if session is not None else None)
        if state is None and session is not None:
            state = load_state(session)
        self.sync_from_pvp_state(state)
        # A reconnect/setup race can persist the authoritative phase and
        # priority after the native PriorityWindowAction was lost in memory.
        # Rebuild that window before validation; otherwise a valid card play
        # (and even the pass which should advance the phase) fails the native
        # PlayerHasPriorityRequirement.
        if isinstance(state, dict) and state.get("pvp"):
            active = self._uid_for_raw_player(state.get("turn_pid"))
            priority = self._uid_for_raw_player(state.get("priority_pid"))
            if active is not None and priority is not None:
                self.sync_checkpoint(
                    phases=[self.current_turn_phase], phase_idx=0,
                    active_player_id=active, client_player_id=priority,
                    ensure_main_priority=True,
                    ensure_current_priority=True)
                action = self.action_stack.peek()
                if isinstance(action, PriorityWindowAction):
                    self._apply_persisted_passes(action, state)
        return super().submit_transaction(transaction)

    def _apply_persisted_passes(self, action, state) -> None:
        """Restore only responders that have not passed this window.

        HConnect materializes a new adapter object for many requests. The
        checkpoint's current ``priority_pid`` is not enough to rebuild an
        ALL window: after A passes, rebuilding ``[B, A]`` makes B's pass hand
        priority back to A forever. The durable pass list is the remainder
        of the native APNAP queue.
        """
        if not isinstance(state, dict):
            return
        is_chain = getattr(action, "ability_responding_to", None) is not None
        passed = {int(value) for value in
                  (state.get("stack_passed" if is_chain else "passes") or ())}
        if not passed:
            return
        queue = [player for player in
                 (getattr(action, "_priority_queue", ()) or ())
                 if _raw_player_id(player) not in passed]
        from collections import deque
        action._priority_queue = deque(queue)
        self.action_stack.priority_player_id = action.priority_player_id

    def queue_projected_chain(self, descriptor, owner_id, *, first_player_id=None):
        """Queue a mode-projected card as a native chain item.

        The descriptor contains only the mode's persistence projection. Chain
        identity and response priority are owned by the shared RulesPort
        action stack; the mode supplies the final SQLite/effect projection
        through the normal ability resolver callback.
        """
        descriptor = dict(descriptor or {})
        instance_id = int(descriptor.get("instance_id", 0) or 0)
        if instance_id <= 0:
            raise ValueError("projected chain item needs an instance id")
        descriptors = getattr(self, "_projected_chain_descriptors", None)
        if descriptors is None:
            descriptors = self._projected_chain_descriptors = {}
        if instance_id in descriptors and self.chain.contains_ability(instance_id):
            return self.chain.peek_ability(instance_id)
        persisted_descriptor = dict(descriptor)
        # Keep the mode-boundary owner with the descriptor.  The native
        # ability only stores a typed UID in memory, while reconnect restore
        # has no callback argument from which to recover the raw PvP owner.
        persisted_descriptor.setdefault("owner_id", _raw_player_id(owner_id))
        descriptors[instance_id] = _json_value(persisted_descriptor)
        ability = ProjectedChainAbility(
            instance_id=instance_id,
            descriptor=descriptor,
            owner_id=self._uid_for_raw_player(owner_id) or owner_id,
        )
        self.chain.push_ability(ability)
        self.ability_manager.activate_chain(instance_id)
        self.push_game_action(ResolveTopOfChainAction(ability))
        if first_player_id is not None:
            self.action_stack.priority_player_id = first_player_id
        self.push_game_action(PriorityWindowAction(
            TurnPhasePlayers.ALL, ability))
        return ability


@dataclass
class ProjectedChainAbility:
    """Chain identity for a card whose final effects are mode-projected."""

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
        return False
