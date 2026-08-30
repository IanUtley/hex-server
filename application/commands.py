"""Commands accepted by the application layer."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RemoveSessionCommand:
    """Remove the active game session for a player."""

    player_uid: int
    reason: str = "leave"


@dataclass(frozen=True)
class StartSessionCommand:
    session_name: str
    player_uid: int


@dataclass(frozen=True)
class StartEncounterCommand:
    session_name: str
    encounter_data: object
    player_uid: int


@dataclass(frozen=True)
class JoinSessionCommand:
    session_id: int
    player_uid: int


@dataclass(frozen=True)
class SetSessionStateCommand:
    player_uid: int
    state: str


@dataclass(frozen=True)
class MarkMailReadCommand:
    user_id: int


@dataclass(frozen=True)
class DeleteMailCommand:
    user_id: int


@dataclass(frozen=True)
class ClaimMailCommand:
    user_id: int
    email_id: int


@dataclass(frozen=True)
class PurchaseStoreItemCommand:
    user_id: int
    item_id: int
    quantity: int = 1


@dataclass(frozen=True)
class RedeemCodeCommand:
    user_id: int
    code: str


@dataclass(frozen=True)
class SocialMutationCommand:
    user_id: int
    operation: str
    target_name: str


@dataclass(frozen=True)
class ServiceRequestCommand:
    """Protocol request envelope passed into the application boundary.

    The envelope is deliberately transport-neutral enough to support the
    existing ObjFmt protocol while the individual request handlers migrate to
    typed application commands.
    """

    target: str
    instance: str
    data_type: int
    request_id: int
    compressed: int
    session_id: str
    connection_handle: str
    inner_object: object
    inner_bytes: bytes
