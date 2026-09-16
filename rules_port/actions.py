"""Client-derived ability actions for the Python authoritative scheduler."""

from __future__ import annotations

from enum import Enum

from .kernel import GameAction, GameActionResult


class AbilityResolutionState(Enum):
    WAITING_FOR_CLIENT_MESSAGES = "WaitingForClientToProcessOtherMessages"
    WAITING_FOR_INPUT = "WaitingForInput"
    COMPLETED = "Completed"
    BLOCKED = "Blocked"


class PushOntoChainAction(GameAction):
    """Port of C# ``PushOntoChainAction`` prompt-before-finish behavior."""

    def __init__(self, ability, *, free: bool = False) -> None:
        super().__init__()
        self.ability = ability
        self.free = bool(free)
        self._finish_ability = False
        self._sent_request = None

    def on_enter(self) -> None:
        # C# ``Session.CreateAbility`` registers this before prompt handling.
        # The standalone adapter has no separate factory call, so mirror it
        # here and make the waiting reply addressable by instance ID.
        if self.session.ability_manager.get(self.ability.instance_id) is None:
            self.session.ability_manager.add_chain(self.ability.instance_id,
                                                   self.ability)

    def update(self) -> GameActionResult:
        prompts = tuple(self.ability.needs_activation_data())
        if prompts:
            # The callback emits the existing client picker/option event and
            # stores a JSON continuation. It never awaits a local UI thread.
            if self._sent_request != prompts:
                self._sent_request = prompts
                self.session.request_activation_data(self.ability, prompts)
            return GameActionResult.WAITING_FOR_INPUT
        self._finish_ability = True
        return GameActionResult.COMPLETE

    def on_exit(self) -> None:
        if self._finish_ability:
            self.session.finish_ability_on_chain(self.ability, free=self.free)

    def untargeted_trigger(self) -> bool:
        return bool(getattr(self.ability, "untargeted_trigger", False))


class ResolveTopOfChainAction(GameAction):
    """Port of C# resolve-loop result handling."""

    def __init__(self, ability) -> None:
        super().__init__()
        self.ability = ability

    def update(self) -> GameActionResult:
        if self.session.chain.is_empty:
            return GameActionResult.COMPLETE
        state = self.session.resolve_top_of_chain(self.ability.instance_id)
        while state is AbilityResolutionState.WAITING_FOR_CLIENT_MESSAGES:
            state = self.session.resolve_top_of_chain(self.ability.instance_id)
        if state not in (AbilityResolutionState.COMPLETED,
                         AbilityResolutionState.BLOCKED):
            return GameActionResult.WAITING_FOR_INPUT
        if state is AbilityResolutionState.BLOCKED or self.action_stack.peek() is not self:
            return GameActionResult.WORKING
        return GameActionResult.COMPLETE
