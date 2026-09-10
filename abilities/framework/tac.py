"""TAC (Template Attribute Collection) v2 binary decoder.

A TAC is the client's Game.Shared.Mechanics.TAC serialized to bytes:
    [int16 version = 2]
    repeat until int32 == 0:
        int32 attributeHash
        value (StringAttrs=7-bit-length+UTF-8, IntAttrs=int32, nested TAC)

Decodes by name hash so nothing is hardcoded to a specific card.
"""

import base64 as _b64
import hashlib as _hl
import struct as _st

_MD5 = None


def _tac_attr_hash(name):
    """Match AttributeName.CreateHash: MD5(name)[0:4], zero-guarded, big-endian."""
    d = _hl.md5(name.encode("ascii")).digest()
    arr = bytearray(d[:4])
    if arr[0] == 0:
        arr[0] = 1
    if arr[3] == 0:
        arr[3] = 1
    h = 0
    for x in arr:
        h = (h << 8) + x
    return h


_TAC_GUID_HASH = _tac_attr_hash("Guid")
_TAC_FUNC_HASH = _tac_attr_hash("FunctionName")

# These are the structural TAC attributes used by authored trigger
# conditions.  Ordinary unknown attributes are integer values in the event
# TACs we currently receive; keeping the structural names explicit preserves
# the metadata-driven decoder without turning card names into rules.
_TAC_CONTAINER_HASHES = {
    _tac_attr_hash(name) for name in (
        "CardStatsThisTurn", "CardStatsWithSpecificDuration", "CardGameStats",
        "Condition", "DataToAppend", "HasAsSubset", "MinimumValues",
        "PlayerStatsThisTurn", "PlayerGameStats", "PlayerHighestTurnStats",
        "PermanentData", "ThisTurnsData")}
_TAC_LIST_HASHES = {_tac_attr_hash("Conditions")}


def decode_tac_tree(data_b64):
    """Decode TAC while retaining nested TAC/list structure.

    ``decode_tac`` remains the flat compatibility API used for simple ability
    flags. Trigger conditions need the client's nested ``MinimumValues`` and
    ``HasAsSubset`` structure, so expose a small structural decoder alongside
    it. Unknown leaf attributes are read as IntAttrs, matching TriggerEvent
    TAC payloads such as GainThresholdEvent.
    """
    try:
        b = _b64.b64decode(data_b64)
    except Exception:
        return {}
    if len(b) < 2:
        return {}
    i = 2

    def parse_body():
        nonlocal i
        out = {}
        while i + 4 <= len(b):
            attr = _st.unpack_from("<I", b, i)[0]
            i += 4
            if attr == 0:
                break
            if attr in _TAC_CONTAINER_HASHES:
                out[attr] = parse_body()
            elif attr in _TAC_LIST_HASHES:
                out.setdefault(attr, []).append(parse_body())
            else:
                if i + 4 > len(b):
                    break
                out[attr] = _st.unpack_from("<i", b, i)[0]
                i += 4
        return out

    try:
        return parse_body()
    except (IndexError, struct.error, ValueError):
        return {}


def decode_tac(data_b64):
    """Decode a serialized TAC (base64 str) into {name_hash: value}.

    Values are str for StringAttrs and int for IntAttrs. Nested TACs are
    flattened as {name_hash: dict}. Returns {} on any parse failure.
    """
    try:
        b = _b64.b64decode(data_b64)
    except Exception:
        return {}
    if len(b) < 2:
        return {}
    i = 2
    out = {}

    def _r7():
        nonlocal i
        shift = 0
        v = 0
        while True:
            if i >= len(b):
                raise ValueError("tac truncated")
            byt = b[i]
            i += 1
            v |= (byt & 0x7F) << shift
            if not (byt & 0x80):
                return v
            shift += 7

    def _rstr():
        nonlocal i
        ln = _r7()
        s = b[i:i + ln].decode("utf-8", "replace")
        i += ln
        return s

    def _rawstr(ln):
        nonlocal i
        s = b[i:i + ln].decode("utf-8", "replace")
        i += ln
        return s

    while i < len(b):
        if i + 4 > len(b):
            break
        rid = _st.unpack_from("<i", b, i)[0]
        i += 4
        if rid == 0:
            break
        rid &= 0xFFFFFFFF
        saved = i
        try:
            ln = _r7()
            if 0 < ln < 200 and i + ln <= len(b):
                out[rid] = _rawstr(ln)
                continue
        except Exception:
            pass
        i = saved
        if i + 4 <= len(b):
            out[rid] = _st.unpack_from("<i", b, i)[0]
            i += 4
    return out


def tac_guid(data_b64):
    """The ability GUID a TAC references (StringAttrs.Guid), or None."""
    t = decode_tac(data_b64)
    v = t.get(_TAC_GUID_HASH)
    return v if isinstance(v, str) and v else None


def tac_function(data_b64):
    """The TACAbilityEffectTemplate operation key (StringAttrs.FunctionName)."""
    t = decode_tac(data_b64)
    v = t.get(_TAC_FUNC_HASH)
    return v if isinstance(v, str) else ""
