"""Hex TCG domain types: UID, ResourceId, SessionCardId, CombatId."""

import struct
import uuid

from domain.binary_io import BinaryWriter, BinaryReader


# ======================================================================
#  UID
# ======================================================================

class UID:
    """8 bytes: low byte = Type enum, high 56 bits = instance ID shifted."""
    INVALID = 0

    def __init__(self, uid64: int = 0):
        # Defensive unwrap: callers occasionally pass a UID or SessionCardId
        # where an int was expected.  Storing a wrapper object makes the wire
        # writer crash with "SessionCardId' object has no attribute
        # 'to_uint64'", so normalize here instead.
        if isinstance(uid64, SessionCardId):
            uid64 = uid64.uid
        if isinstance(uid64, UID):
            uid64 = uid64.uid64
        self.uid64 = uid64

    @property
    def uid_type(self) -> int:
        return self.uid64 & 0xFF

    @property
    def instance_id(self) -> int:
        return self.uid64 >> 8

    def to_uint64(self) -> int:
        return self.uid64

    def to_hex(self) -> str:
        return format(self.uid64, '016x')

    @staticmethod
    def make(uid_type: int, instance: int) -> 'UID':
        return UID((instance << 8) | uid_type)

    @staticmethod
    def invalid() -> 'UID':
        return UID(0)

    def write(self, w: BinaryWriter):
        w.write_uint64(self.uid64)

    @staticmethod
    def read(r: BinaryReader) -> 'UID':
        return UID(r.read_uint64())

    def __eq__(self, other):
        return isinstance(other, UID) and self.uid64 == other.uid64

    def __hash__(self):
        return hash(self.uid64)

    def __repr__(self):
        return f"UID({self.uid64})"


# ======================================================================
#  ResourceId
# ======================================================================

class ResourceId:
    """Wraps a 16-byte GUID. Wire format: int32(16) + 16 bytes."""
    def __init__(self, guid: uuid.UUID = None):
        self.guid = guid or uuid.UUID(int=0)

    def to_bytes(self) -> bytes:
        return self.guid.bytes_le

    def write(self, w: BinaryWriter):
        b = self.to_bytes()
        w.write_int32(len(b))
        w.write_raw_bytes(b)

    @staticmethod
    def read(r: BinaryReader) -> 'ResourceId':
        length = r.read_int32()
        guid_bytes = r.read_bytes(length)
        return ResourceId(uuid.UUID(bytes_le=guid_bytes))

    @staticmethod
    def from_str(s: str) -> 'ResourceId':
        return ResourceId(uuid.UUID(s))

    @staticmethod
    def invalid() -> 'ResourceId':
        return ResourceId(uuid.UUID(int=0))

    def __repr__(self):
        return f"ResourceId({self.guid})"


# ======================================================================
#  SessionCardId
# ======================================================================

class SessionCardId:
    """Wraps UID. Wire format: 8 bytes uint64."""
    def __init__(self, uid=None):
        # Defensive unwrap: a SessionCardId passed to another SessionCardId
        # (e.g. PreGame re-asserting deck cards) would serialize its UID as a
        # SessionCardId and crash make_network_packet.  Unwrap to the inner
        # UID so double-wraps are harmless.
        if isinstance(uid, SessionCardId):
            uid = uid.uid
        if isinstance(uid, UID):
            pass
        elif isinstance(uid, int):
            uid = UID(uid)
        self.uid = uid if uid else UID.invalid()

    def write(self, w: BinaryWriter):
        w.write_uint64(self.uid.to_uint64())

    @staticmethod
    def read(r: BinaryReader) -> 'SessionCardId':
        return SessionCardId(UID.read(r))

    def __repr__(self):
        return f"SessionCardId({self.uid})"


# ======================================================================
#  CombatId
# ======================================================================

class CombatId:
    """Identifies one attacker's combat. Wire format: UID + int64 serial."""
    def __init__(self, attacker: UID = None, serial: int = 0):
        self.attacker = attacker or UID.invalid()
        self.serial = serial

    def write(self, w: BinaryWriter):
        self.attacker.write(w)
        w.write_int64(self.serial)

    @staticmethod
    def read(r: BinaryReader) -> 'CombatId':
        return CombatId(UID.read(r), r.read_int64())

    def __eq__(self, other):
        return (isinstance(other, CombatId) and
                self.attacker == other.attacker and
                self.serial == other.serial)

    def __hash__(self):
        return hash((self.attacker.uid64, self.serial))

    def __repr__(self):
        return f"CombatId({self.attacker}, {self.serial})"
