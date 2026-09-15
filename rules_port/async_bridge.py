"""Async UI/server-event bridge for C# actions that wait for client input."""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from typing import Any, Awaitable, Callable, Mapping

from .parity import event_record


class AsyncUIEventBus:
    """Small request/reply bus replacing Unity's blocking dialog callbacks.

    Rules code publishes a typed checkpoint and awaits its reply; transport
    code can deliver the reply later through :meth:`respond`.  The bus has no
    knowledge of cards or UI names and is safe to use from one asyncio loop.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[str, Mapping[str, Any]],
                                                 Awaitable[None] | None]]] = defaultdict(list)
        self._pending: dict[str, asyncio.Future] = {}

    def subscribe(self, event_type: str, handler: Callable) -> None:
        if not callable(handler):
            raise TypeError("event handler must be callable")
        self._handlers[str(event_type)].append(handler)

    async def emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Deliver a one-way server event to subscribers in registration order."""
        for handler in tuple(self._handlers.get(str(event_type), ())):
            result = handler(str(event_type), dict(payload))
            if inspect.isawaitable(result):
                await result

    def emit_nowait(self, event_type: str, payload: Mapping[str, Any]):
        """Schedule a one-way event from a synchronous event sink."""
        return asyncio.get_running_loop().create_task(
            self.emit(event_type, payload))

    async def request(self, event_type: str, request_id: str,
                      payload: Mapping[str, Any]) -> Any:
        request_key = str(request_id)
        if request_key in self._pending:
            raise ValueError(f"request already pending: {request_key}")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_key] = future
        try:
            for handler in tuple(self._handlers.get(str(event_type), ())):
                result = handler(request_key, dict(payload))
                if inspect.isawaitable(result):
                    await result
            return await future
        finally:
            self._pending.pop(request_key, None)

    def respond(self, request_id: str, value: Any) -> bool:
        future = self._pending.get(str(request_id))
        if future is None or future.done():
            return False
        future.set_result(value)
        return True

    def cancel(self, request_id: str, error: BaseException | None = None) -> bool:
        future = self._pending.get(str(request_id))
        if future is None or future.done():
            return False
        future.set_exception(error or asyncio.CancelledError())
        return True

    def request_nowait(self, event_type: str, request_id: str,
                       payload: Mapping[str, Any]):
        """Schedule a checkpoint from a synchronous rules callback."""
        return asyncio.get_running_loop().create_task(
            self.request(event_type, request_id, payload))


class AsyncActivationPublisher:
    """Adapt ``AuthoritativeSession`` activation callbacks to the bus.

    The returned task only publishes the checkpoint. A later protocol
    transaction must still call ``session.resume_activation_data`` so the
    normal responsibility and ability requirements remain authoritative.
    """

    def __init__(self, bus: AsyncUIEventBus,
                 event_type: str = "ability_activation_data_required") -> None:
        self.bus = bus
        self.event_type = str(event_type)

    def __call__(self, ability, prompts):
        instance_id = int(ability.instance_id)
        payload = {
            "ability_instance_id": instance_id,
            "responsible_player_id": getattr(
                getattr(ability, "responsible_player_id", None), "uid64",
                getattr(ability, "responsible_player_id", None)),
            "prompts": tuple(prompts or ()),
        }
        return self.bus.request_nowait(self.event_type,
                                       f"ability:{instance_id}", payload)


class AsyncRulesCoordinator:
    """Cooperatively drive a synchronous rules session from asyncio."""

    def __init__(self, session: object, bus: AsyncUIEventBus) -> None:
        self.session = session
        self.bus = bus
        setter = getattr(session, "set_activation_requester", None)
        if callable(setter):
            setter(AsyncActivationPublisher(bus))

    def submit(self, command, player_id, *, payload=None) -> bool:
        from .wire import submit_classified_transaction
        return submit_classified_transaction(self.session, command, player_id,
                                             payload=payload)

    async def submit_and_drive(self, command, player_id, *, payload=None,
                               max_steps: int = 1000) -> int:
        """Submit one classified command and drive to the next async checkpoint."""
        if not self.submit(command, player_id, payload=payload):
            return -1
        return await self.drive_until_wait(max_steps=max_steps)

    async def drive_until_wait(self, *, max_steps: int = 1000) -> int:
        """Run ticks until waiting/idle, yielding to transport each step."""
        steps = 0
        while steps < int(max_steps):
            if not self.session.tick():
                return steps
            steps += 1
            await asyncio.sleep(0)
        raise RuntimeError("rules session exceeded async tick budget")


class AsyncEventPublisher:
    """Turn a synchronous ``GameEngineEventSink`` observer into async events."""

    def __init__(self, bus: AsyncUIEventBus) -> None:
        self.bus = bus

    def __call__(self, event) -> None:
        record = event_record(event)
        self.bus.emit_nowait(record["type"], record)
