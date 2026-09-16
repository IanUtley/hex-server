"""Native decoder for the client's Template Attribute Collection format."""

from __future__ import annotations

import base64
import hashlib
import struct


def _tac_attr_hash(name):
    data = hashlib.md5(str(name).encode("ascii")).digest()[:4]
    data = bytearray(data)
    if data[0] == 0:
        data[0] = 1
    if data[3] == 0:
        data[3] = 1
    value = 0
    for byte in data:
        value = (value << 8) + byte
    return value


_GUID = _tac_attr_hash("Guid")
_FUNCTION = _tac_attr_hash("FunctionName")
_CONTAINERS = {_tac_attr_hash(name) for name in (
    "CardStatsThisTurn", "CardStatsWithSpecificDuration", "CardGameStats",
    "Condition", "DataToAppend", "HasAsSubset", "MinimumValues",
    "PlayerStatsThisTurn", "PlayerGameStats", "PlayerHighestTurnStats",
    "PermanentData", "ThisTurnsData")}
_LISTS = {_tac_attr_hash("Conditions"), _tac_attr_hash("RequiredEquipment")}
_STRINGS = {_tac_attr_hash(name) for name in (
    "Guid", "FunctionName", "ListName", "Where", "SourceCardGuid",
    "GainedCounterType", "RemovedCounterType", "CompareWith", "Name")}


def decode_tac_tree(data):
    try:
        raw = base64.b64decode(data)
    except Exception:
        return {}
    if len(raw) < 2:
        return {}
    index = 2

    def parse():
        nonlocal index
        result = {}
        while index + 4 <= len(raw):
            attr = struct.unpack_from("<I", raw, index)[0]
            index += 4
            if not attr:
                break
            if attr in _CONTAINERS:
                result[attr] = parse()
            elif attr in _LISTS:
                result.setdefault(attr, []).append(parse())
            elif attr in _STRINGS:
                length = 0
                shift = 0
                while index < len(raw):
                    byte = raw[index]
                    index += 1
                    length |= (byte & 0x7F) << shift
                    if not byte & 0x80:
                        break
                    shift += 7
                result[attr] = raw[index:index + length].decode("utf-8", "replace")
                index += length
            else:
                if index + 4 > len(raw):
                    break
                result[attr] = struct.unpack_from("<i", raw, index)[0]
                index += 4
        return result
    try:
        return parse()
    except (IndexError, struct.error, ValueError):
        return {}


def decode_tac(data):
    tree = decode_tac_tree(data)
    result = {}
    def flatten(value):
        if isinstance(value, dict):
            return {key: flatten(child) for key, child in value.items()}
        if isinstance(value, list):
            return [flatten(child) for child in value]
        return value
    return flatten(tree)


def tac_int(data, name, default=0):
    value = decode_tac_tree(data).get(_tac_attr_hash(name))
    return value if isinstance(value, int) else default


def tac_string(data, name, default=""):
    value = decode_tac_tree(data).get(_tac_attr_hash(name))
    return value if isinstance(value, str) else default


def tac_guid(data):
    value = decode_tac(data).get(_GUID)
    return value if isinstance(value, str) and value else None


def tac_function(data):
    value = decode_tac(data).get(_FUNCTION)
    return value if isinstance(value, str) else ""
