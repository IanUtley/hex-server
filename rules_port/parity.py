"""Deterministic state/event capture comparison for C#-to-Python ports.

The C# project is not buildable in this checkout (its original managed
dependencies and a build tool are absent), but this format accepts captures
made from it later and compares them to Python using the same transaction/RNG
seed. It intentionally compares event *order*, not just final state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


def _normalise(value: Any):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalise(item) for key, item in sorted(
            value.items(), key=lambda pair: str(pair[0]))}
    uid64 = getattr(value, "uid64", None)
    if uid64 is not None:
        return int(uid64)
    guid = getattr(value, "guid", None)
    if guid is not None:
        return str(guid)
    if hasattr(value, "__dict__"):
        return {key: _normalise(item) for key, item in sorted(value.__dict__.items())
                if not key.startswith("_") and key != "ser"}
    return str(value)


def event_record(event: object) -> dict[str, Any]:
    """Stable protocol-agnostic event record preserving emitted order."""
    return {"type": event.__class__.__name__, "fields": _normalise(event)}


@dataclass(frozen=True)
class ParityCapture:
    seed_z: int
    seed_w: int
    transactions: tuple[dict[str, Any], ...]
    state: dict[str, Any]
    events: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_session(cls, session, game=None) -> "ParityCapture":
        """Capture one Python run in the same shape as a C# replay.

        ``game`` is optional so headless tests can compare state/transactions;
        when supplied, its event list is recorded in emission order.
        """
        seed_z, seed_w = session.random_number_generator.get_seed()
        transactions = tuple(getattr(session, "transaction_history", ()))
        state_builder = getattr(session, "parity_state", None)
        state = dict(state_builder() if callable(state_builder) else {})
        events = tuple(event_record(event) for event in (
            getattr(game, "events", ()) if game is not None else ()))
        return cls(int(seed_z), int(seed_w), transactions, state, events)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ParityCapture":
        return cls(int(value["seed_z"]), int(value["seed_w"]),
                   tuple(dict(item) for item in value.get("transactions", ())),
                   dict(value.get("state", {})),
                   tuple(dict(item) for item in value.get("events", ())))


class ParityMismatch(AssertionError):
    pass


def compare_captures(reference: ParityCapture, actual: ParityCapture) -> None:
    """Raise an actionable difference for state or ordered events."""
    if (reference.seed_z, reference.seed_w) != (actual.seed_z, actual.seed_w):
        raise ParityMismatch("RNG seed mismatch")
    if reference.transactions != actual.transactions:
        raise ParityMismatch("transaction capture mismatch")
    if reference.state != actual.state:
        raise ParityMismatch(
            f"state mismatch: expected={reference.state!r} actual={actual.state!r}")
    if reference.events != actual.events:
        for index, (expected, observed) in enumerate(zip(reference.events,
                                                          actual.events)):
            if expected != observed:
                raise ParityMismatch(
                    f"event {index} mismatch: expected={expected!r} actual={observed!r}")
        raise ParityMismatch(f"event count mismatch: expected={len(reference.events)} "
                             f"actual={len(actual.events)}")


def replay_transactions(session, transactions, factory: Callable) -> None:
    """Feed captured normalized transactions through a fresh session."""
    if not callable(factory):
        raise TypeError("transaction factory must be callable")
    for record in transactions:
        transaction = factory(record)
        if not session.submit_transaction(transaction):
            raise ParityMismatch(f"transaction rejected during replay: {record!r}")
        if not session.handle_transaction():
            raise ParityMismatch(f"transaction was not handled during replay: {record!r}")


def load_capture(path: str | Path) -> ParityCapture:
    with open(path, encoding="utf-8") as source:
        return ParityCapture.from_dict(json.load(source))


def save_capture(path: str | Path, capture: ParityCapture) -> None:
    with open(path, "w", encoding="utf-8") as target:
        json.dump(capture.to_dict(), target, indent=2, sort_keys=True)
        target.write("\n")
