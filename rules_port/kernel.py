"""Direct, dependency-light port of core ``Game.Shared`` mechanics.

Source counterparts:

* ``MultiplyWithCarryRng.cs``
* ``Mechanics/GameActions/GameAction.cs``
* ``Mechanics/GameActions/GameActionStack.cs``
* ``Mechanics/GameActions/PriorityWindowAction.cs``

The C# session owns persistence and wire dispatch.  This module intentionally
uses a small duck-typed session contract instead of duplicating either layer:
``player_ids_in_turn_order()``, ``active_player_id``, ``priority_player_id``,
``chain_top()``, ``handle_game_event()``, and ``send_turn_phase_update()``.
"""

from __future__ import annotations

from collections import deque
from enum import Enum
from typing import Deque, Dict, Iterable, Iterator, Optional

from .phases import phase_name


_U64_MASK = (1 << 64) - 1
_I32_MASK = (1 << 32) - 1
_I32_MAX = (1 << 31) - 1


class MultiplyWithCarryRng:
    """Bit-for-bit port of the client MultiplyWithCarryRng.

    Python integers do not overflow, so every mutation is masked to ``ulong``
    before matching C#'s cast to signed ``int`` in :meth:`next`.
    """

    def __init__(self, z: int, w: int):
        self.seed_z = int(z) & _U64_MASK
        self.seed_w = int(w) & _U64_MASK

    def set_seed(self, z: int, w: int) -> None:
        self.seed_z = int(z) & _U64_MASK
        self.seed_w = int(w) & _U64_MASK

    def get_seed(self) -> tuple[int, int]:
        return self.seed_z, self.seed_w

    def next(self, *bounds: int) -> int:
        """Match C# overloads ``Next()``, ``Next(max)``, and ``Next(min,max)``.

        Use ``next(10)`` and ``next(10, 20)`` for the two bounded overloads;
        the no-argument form returns a non-negative signed int.
        """
        if len(bounds) == 1:
            max_value = int(bounds[0])
            return 0 if max_value <= 0 else self.next() % max_value
        if len(bounds) == 2:
            return self.next_range(int(bounds[0]), int(bounds[1]))
        if len(bounds) > 2:
            raise TypeError("next() accepts zero, one, or two bounds")
        self.seed_z = (36969 * (self.seed_z & 0xFFFF) +
                       (self.seed_z >> 16)) & _U64_MASK
        self.seed_w = (18000 * (self.seed_w & 0xFFFF) +
                       (self.seed_w >> 16)) & _U64_MASK
        value = (((self.seed_z << 16) & _U64_MASK) +
                 (self.seed_w & 0xFFFF)) & _I32_MASK
        # C# casts the low 32 bits to signed Int32, then masks a negative
        # result with Int32.MaxValue.
        if value & (1 << 31):
            value &= _I32_MAX
        return value

    def next_range(self, min_value: int, max_value: int) -> int:
        if min_value >= max_value:
            return 0
        return min_value + self.next() % (max_value - min_value)

    def next_bytes(self, length: int) -> bytes:
        return bytes(self.next() % 256 for _ in range(max(0, int(length))))

    def next_double(self, minimum: float = 0.0, maximum: float = 1.0) -> float:
        return minimum + (self.next() * 4.6566128752457969e-10) * (
            maximum - minimum)


class GameActionResult(Enum):
    COMPLETE = "Complete"
    WAITING_FOR_INPUT = "WaitingForInput"
    WORKING = "Working"
    DELETE = "Delete"


class GameAction:
    """Port of the lifecycle used by the client's GameActionStack."""

    _next_instance_id = 0

    def __init__(self) -> None:
        self.instance_id: Optional[int] = None
        self.action_stack: Optional[GameActionStack] = None
        self.session = None
        self.was_interrupted = False

    def initialize(self, action_stack: "GameActionStack", session) -> None:
        self.action_stack = action_stack
        self.session = session
        self.instance_id = GameAction._next_instance_id
        GameAction._next_instance_id += 1

    def on_enter(self) -> None:
        pass

    def on_exit(self) -> None:
        pass

    def post_update(self) -> None:
        pass

    def on_interrupted(self) -> None:
        self.was_interrupted = True

    def untargeted_trigger(self) -> bool:
        return False

    def update(self) -> GameActionResult:
        raise NotImplementedError


class AbilityRegistry:
    """Minimal port-facing view of the client's ``AbilityManager`` registry.

    The metadata framework remains responsible for constructing abilities.  A
    rules-session adapter registers the resulting instance under the same
    chain instance id used in HConnect chain events.
    """

    def __init__(self) -> None:
        self._instances: Dict[int, object] = {}
        self._chain_instance_ids: set[int] = set()

    def add(self, instance_id: int, ability: object) -> None:
        self._instances[int(instance_id)] = ability

    def add_chain(self, instance_id: int, ability: object) -> None:
        """C# ``AbilityManager.AddChainAbility`` registration phase."""
        self.add(instance_id, ability)
        self._chain_instance_ids.add(int(instance_id))

    def get(self, instance_id: int):
        return self._instances.get(int(instance_id))

    def remove(self, instance_id: int):
        instance_id = int(instance_id)
        self._chain_instance_ids.discard(instance_id)
        return self._instances.pop(instance_id, None)

    def is_chain_ability(self, instance_id: int) -> bool:
        return int(instance_id) in self._chain_instance_ids

    def activate_chain(self, instance_id: int):
        """C# removes the pending marker when resolution becomes active."""
        instance_id = int(instance_id)
        if instance_id not in self._chain_instance_ids:
            return None
        self._chain_instance_ids.remove(instance_id)
        return self.get(instance_id)

    def remove_chain(self, instance_id: int):
        self._chain_instance_ids.discard(int(instance_id))


class Chain:
    """Port of ``Game.Shared.Mechanics.Chain``.

    The original stores instance ids, resolves them through ``AbilityManager``,
    and permits normal resolution only from the chain top.  Keeping that split
    prevents an event/persistence adapter from accidentally treating card ids
    as ability-instance ids.
    """

    def __init__(self, ability_manager: AbilityRegistry) -> None:
        self.ability_manager = ability_manager
        self._instance_ids: list[int] = []

    @property
    def is_empty(self) -> bool:
        return not self._instance_ids

    @property
    def count(self) -> int:
        return len(self._instance_ids)

    def __iter__(self) -> Iterator[object]:
        for instance_id in self._instance_ids:
            ability = self.ability_manager.get(instance_id)
            if ability is not None:
                yield ability

    def clear(self) -> None:
        self._instance_ids.clear()

    @staticmethod
    def _instance_id(ability) -> int:
        try:
            return int(ability.instance_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("chain ability must have an integer instance_id") from exc

    def push_ability(self, ability) -> None:
        instance_id = self._instance_id(ability)
        self.ability_manager.add_chain(instance_id, ability)
        self._instance_ids.append(instance_id)

    def pop_ability(self, instance_id: int):
        if not self._instance_ids or self._instance_ids[-1] != int(instance_id):
            return None
        self._instance_ids.pop()
        return self.ability_manager.get(instance_id)

    def peek_ability(self, instance_id: Optional[int] = None):
        if not self._instance_ids:
            return None
        top_id = self._instance_ids[-1]
        if instance_id is not None and top_id != int(instance_id):
            return None
        return self.ability_manager.get(top_id)

    def contains_ability(self, instance_id: int) -> bool:
        return int(instance_id) in self._instance_ids

    def remove_ability(self, instance_id: int):
        instance_id = int(instance_id)
        if instance_id not in self._instance_ids:
            return None
        self._instance_ids.remove(instance_id)
        return self.ability_manager.remove(instance_id)


class GameActionStack:
    """LIFO action scheduler matching ``GameActionStack.Update`` ordering."""

    def __init__(self, session) -> None:
        self.session = session
        self._stack: list[GameAction] = []
        self.current_action: Optional[GameAction] = None
        self.priority_player_id = None

    @property
    def count(self) -> int:
        return len(self._stack)

    def peek(self) -> Optional[GameAction]:
        return self._stack[-1] if self._stack else None

    def update(self) -> bool:
        if not self._stack:
            self.current_action = None
            return False
        if self.peek() is not self.current_action:
            self.current_action = self.peek()
            self.current_action.on_enter()
        action = self.peek()
        result = action.update()
        if result is GameActionResult.COMPLETE:
            self._stack.pop()
            # This intentionally calls the action entered for this update,
            # exactly as the C# code does, even if a child was pushed mid-call.
            self.current_action.on_exit()
            return True
        if result is GameActionResult.WAITING_FOR_INPUT:
            return False
        if result is GameActionResult.WORKING:
            return True
        raise NotImplementedError(f"Unhandled GameAction result: {result!r}")

    def push(self, action: GameAction) -> None:
        held: list[GameAction] = []
        if not action.untargeted_trigger() and not self.has_untargeted_triggers():
            while self._stack and self._stack[-1].__class__.__name__ == (
                    "PushOntoChainAction"):
                held.append(self._stack.pop())
        if self._stack:
            self._stack[-1].on_interrupted()
        action.initialize(self, self.session)
        self._stack.append(action)
        while held:
            self._stack.append(held.pop())

    def push_behind(self, action: GameAction) -> None:
        action.initialize(self, self.session)
        self._stack.insert(0, action)

    def clear(self) -> None:
        self._stack.clear()
        self.current_action = None
        self.priority_player_id = None

    def post_update(self) -> None:
        if self.current_action is not None:
            self.current_action.post_update()

    def has_untargeted_triggers(self) -> bool:
        return any(action.untargeted_trigger() for action in self._stack)


class TurnPhasePlayers(Enum):
    NONE = "None"
    ALL = "All"
    ACTIVE = "Active"
    DEFENDING = "Defending"


class PriorityWindowAction(GameAction):
    """C# priority queue semantics, without client UI policy.

    The host emits GreenLight/option events from ``post_update``.  This kernel
    solely establishes and validates priority so it can be shared by PvE and
    PvP adapters.
    """

    def __init__(self, priority_players: TurnPhasePlayers,
                 ability_responding_to=None) -> None:
        super().__init__()
        self.priority_players = priority_players
        self.ability_responding_to = ability_responding_to
        self._priority_queue: Deque[object] = deque()

    @property
    def priority_player_id(self):
        return self._priority_queue[0] if self._priority_queue else None

    def initialize(self, action_stack: GameActionStack, session) -> None:
        super().initialize(action_stack, session)
        self.reset_priority_window(start_with_active_player=False)

    def update(self) -> GameActionResult:
        self.session.handle_game_event()
        if self.action_stack.peek() is not self:
            return GameActionResult.WORKING
        top = self.session.chain_top()
        if self.ability_responding_to is not None and top is not self.ability_responding_to:
            return GameActionResult.COMPLETE
        if top is not None and getattr(top, "ignores_chain", False):
            return GameActionResult.COMPLETE
        return (GameActionResult.WAITING_FOR_INPUT if self.priority_player_id
                is not None else GameActionResult.COMPLETE)

    def on_enter(self) -> None:
        if self.was_interrupted:
            self.reset_priority_window(start_with_active_player=True)
            self.session.send_turn_phase_update()

    def could_pass_priority(self, player_id) -> bool:
        coerce = getattr(self.session, "coerce_transaction_player_id", None)
        current = self.priority_player_id
        if callable(coerce):
            current = coerce(current)
            player_id = coerce(player_id)
        if current != player_id:
            try:
                if int(getattr(current, "uid64", current)) != int(
                        getattr(player_id, "uid64", player_id)):
                    return False
            except (TypeError, ValueError):
                return False
        if (phase_name(getattr(self.session, "current_turn_phase", "")) == "Discard" and
                self.session.hand_larger_than_maximum(player_id)):
            return False
        return bool(self.session.can_player_pass_priority(player_id))

    def pass_priority(self, player_id) -> bool:
        if not self.could_pass_priority(player_id):
            return False
        self._priority_queue.popleft()
        self.action_stack.priority_player_id = self.priority_player_id
        self.session.send_turn_phase_update()
        return True

    def reset_priority_window(self, start_with_active_player: bool) -> None:
        self._priority_queue.clear()
        if self.priority_players is TurnPhasePlayers.ALL:
            if start_with_active_player or self.action_stack.priority_player_id is None:
                players: Iterable[object] = self.session.player_ids_in_turn_order()
            else:
                players = self.session.player_ids_in_priority_order()
            self._priority_queue.extend(players)
        elif self.priority_players is TurnPhasePlayers.ACTIVE:
            active = self.session.active_player_id
            if active is not None:
                self._priority_queue.append(active)
        elif self.priority_players is TurnPhasePlayers.DEFENDING:
            self._priority_queue.extend(self.session.defending_player_ids())
        self.action_stack.priority_player_id = self.priority_player_id
