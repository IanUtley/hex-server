"""Typed host projection boundary for the RulesPort.

The rules kernel decides whether and when a transaction is legal.  A mode
still has to project that accepted mutation into its persistent state and
client event stream.  This small registry keeps those projections explicit,
auditable, and replaceable without teaching the kernel about HConnect or
SQLite.
"""

from __future__ import annotations

from collections.abc import Callable


class TransactionProjection:
    """Bind and invoke host-owned projections by normalized transaction role."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}

    def bind(self, role: str, handler: Callable) -> None:
        if not callable(handler):
            raise TypeError(f"projection for {role!r} must be callable")
        self._handlers[str(role)] = handler

    def resolve(self, role: str, *args, **kwargs) -> bool:
        handler = self._handlers.get(str(role))
        return bool(handler and handler(*args, **kwargs))

    def handler(self, role: str):
        """Return the bound host projection for a normalized role."""
        return self._handlers.get(str(role))

    def missing(self, roles) -> tuple[str, ...]:
        return tuple(str(role) for role in roles
                     if not callable(self._handlers.get(str(role))))

    def roles(self) -> tuple[str, ...]:
        return tuple(self._handlers)
