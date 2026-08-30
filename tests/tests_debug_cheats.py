"""Small, dependency-free checks for DebugCheatTransaction decoding."""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import debug_cheats


class _Handler:
    def _extract_enum_int(self, raw, field):
        pos = raw.find(field.encode())
        pos = raw.find(b"value__", pos)
        parts = raw[pos + len(b"value__") + 1:].split(b";", 6)
        return struct.unpack("<I", bytes.fromhex(parts[3].decode()))[0]

    def _extract_int32_field(self, raw, field):
        pos = raw.find(field.encode())
        parts = raw[pos + len(field) + 1:].split(b";", 4)
        return struct.unpack("<i", bytes.fromhex(parts[3].decode()))[0]


def _raw(action=11, count=3):
    return (
        b"DebugAction;0;1;1;value__;1;2;0;"
        + struct.pack("<I", action).hex().encode() + b";"
        b"m_PlayerId;0;1;1;m_UID64;1;2;0;f0debc9a78563412;"
        b"Count;0;1;0;"
        + struct.pack("<i", count).hex().encode() + b";"
        b"CardTemplateId;0;1;1;m_Guid;0;3;0;36;"
        b"11111111-2222-3333-4444-555555555555;"
    )


def test_decode_action_count_uid_and_resource_id():
    raw = _raw()
    handler = _Handler()
    assert debug_cheats._enum(handler, raw, "DebugAction") == 11
    assert debug_cheats._int32(handler, raw, "Count") == 3
    assert debug_cheats._uid_after(raw, "m_PlayerId") == 0x123456789ABCDEF0
    assert debug_cheats._resource_id(raw, "CardTemplateId") == (
        "11111111-2222-3333-4444-555555555555"
    )


def test_action_names_cover_client_enum():
    assert set(debug_cheats.ACTION_NAMES) == set(range(28))


if __name__ == "__main__":
    test_decode_action_count_uid_and_resource_id()
    test_action_names_cover_client_enum()
    print("debug cheat tests: ok")

