"""Results and committed events returned by application commands."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SessionRemoved:
    session_name: str
    player_uid: int
    reason: str


@dataclass(frozen=True)
class SessionStateChanged:
    session_name: str
    player_uid: int
    state: str


@dataclass(frozen=True)
class MailChanged:
    user_id: int
    operation: str
    email_id: int | None = None


@dataclass(frozen=True)
class StoreChanged:
    user_id: int
    operation: str
    item_id: int | None = None


@dataclass(frozen=True)
class SocialChanged:
    user_id: int
    operation: str
    target_user_id: int | None = None


@dataclass(frozen=True)
class CommandResult:
    value: Any = None
    events: tuple[Any, ...] = field(default_factory=tuple)
