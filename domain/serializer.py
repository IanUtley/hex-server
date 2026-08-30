"""SessionEventArgs binary serializer (.NET BinaryWriter format)."""

from typing import List, Dict

from domain.binary_io import BinaryWriter, BinaryReader
from domain.types import UID, ResourceId, SessionCardId, CombatId


class Serializer:
    def __init__(self):
        self.w: BinaryWriter = None
        self.r: BinaryReader = None

    def begin_write(self):
        self.w = BinaryWriter()

    def end_write(self) -> bytes:
        return self.w.get_bytes()

    def begin_read(self, data: bytes):
        self.r = BinaryReader(data)

    def end_read(self):
        self.r = None

    def add_int(self, v: int): self.w.write_int32(v)
    def add_long(self, v: int): self.w.write_int64(v)
    def add_ulong(self, v: int): self.w.write_uint64(v)
    def add_bool(self, v: bool): self.w.write_bool(v)
    def add_string(self, s: str): self.w.write_string(s)
    def add_uid(self, uid: UID): uid.write(self.w)
    def add_resource_id(self, rid: ResourceId): rid.write(self.w)
    def add_scid(self, scid: SessionCardId): scid.write(self.w)
    def add_combat_id(self, cid: CombatId): cid.write(self.w)
    def add_enum_int(self, v): self.w.write_int32(int(v))
    def add_enum_ulong(self, v): self.w.write_uint64(int(v))

    def add_timespan(self, value):
        """Write the three Int32 components used by the client serializer."""
        if value is None:
            hours, minutes, seconds = 0, 0, 0
        elif isinstance(value, (tuple, list)):
            parts = list(value) + [0, 0, 0]
            hours, minutes, seconds = parts[:3]
        elif hasattr(value, "total_seconds"):
            total = int(value.total_seconds())
            sign = -1 if total < 0 else 1
            total = abs(total)
            hours, remainder = divmod(total, 3600)
            minutes, seconds = divmod(remainder, 60)
            hours, minutes, seconds = (sign * hours, sign * minutes,
                                        sign * seconds)
        else:
            hours, minutes, seconds = 0, 0, 0
        self.w.write_int32(int(hours))
        self.w.write_int32(int(minutes))
        self.w.write_int32(int(seconds))

    def add_list_int(self, lst: List[int]):
        self.w.write_int32(len(lst))
        for v in lst: self.w.write_int32(v)

    def add_list_long(self, lst: List[int]):
        """List<long> (int64 elements) — used by class 39
        TriggeredAbilityActivationDataRequired.AbilityInstanceIds."""
        self.w.write_int32(len(lst))
        for v in lst: self.w.write_int64(v)

    def add_list_uid(self, lst: List[UID]):
        self.w.write_int32(len(lst))
        for v in lst: v.write(self.w)

    def add_list_resource_id(self, lst: List[ResourceId]):
        self.w.write_int32(len(lst))
        for v in lst: v.write(self.w)

    def add_list_string(self, lst: List[str]):
        self.w.write_int32(len(lst))
        for v in lst: self.w.write_string(v)

    def add_list_scid(self, lst: List[SessionCardId]):
        self.w.write_int32(len(lst))
        for v in lst: v.write(self.w)

    def add_list_events(self, lst):
        self.w.write_int32(len(lst))
        for arg in lst:
            self.add_event(arg)

    def add_event(self, arg):
        """Nested SessionEventArgs: [Class:int32][byteLen:int32][ToByteArray bytes]"""
        raw = arg.to_byte_array()
        self.w.write_int32(arg.CLASS_ID)
        self.w.write_int32(len(raw))
        self.w.write_raw_bytes(raw)

    def add_dict_str_int(self, d: Dict[str, int]):
        self.w.write_int32(len(d))
        for k, v in d.items():
            self.w.write_string(k)
            self.w.write_int32(v)

    def add_dict_str_str(self, d: Dict[str, str]):
        self.w.write_int32(len(d))
        for k, v in d.items():
            self.w.write_string(k)
            self.w.write_string(v)

    def add_dict_rid_int(self, d):
        self.w.write_int32(len(d))
        for k, v in d.items():
            k.write(self.w)
            self.w.write_int32(v)

    def read_int(self) -> int: return self.r.read_int32()
    def read_long(self) -> int: return self.r.read_int64()
    def read_ulong(self) -> int: return self.r.read_uint64()
    def read_bool(self) -> bool: return self.r.read_bool()
    def read_string(self) -> str: return self.r.read_string()
    def read_uid(self) -> UID: return UID.read(self.r)
    def read_resource_id(self) -> ResourceId: return ResourceId.read(self.r)
    def read_scid(self) -> SessionCardId: return SessionCardId.read(self.r)
