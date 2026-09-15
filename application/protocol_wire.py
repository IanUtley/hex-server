"""HConnect packet framing primitives.

This module owns the transport envelope only.  Service dispatch, ObjFmt
decoding, and session state remain in the server so mixed-version clients keep
the existing compatibility path.
"""

import json
import struct


IDENT = b"~HCP~"


def numeric_target_map(value):
    """Keep only typed numeric TargetMap indices from ObjFmt continuations."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, targets in value.items():
        try:
            result[int(key)] = targets
        except (TypeError, ValueError):
            continue
    return result


def nested_field(value, name):
    """Find one named field in a preserved ObjFmt object tree."""
    if isinstance(value, dict):
        if name in value:
            return value[name]
        for child in value.values():
            found = nested_field(child, name)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = nested_field(child, name)
            if found is not None:
                return found
    return None


def tournament_deck_card_ids(deck_value, field_name):
    """Extract ``(card_uid, template_guid)`` from a deck_bits list."""
    cards = deck_value.get(field_name, []) if isinstance(deck_value, dict) else []
    result = []
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        try:
            card_uid = int(card.get("Id"))
        except (TypeError, ValueError):
            continue
        template = card.get("TemplateID", {})
        if isinstance(template, dict):
            template = template.get("guid", "")
        if template:
            result.append((card_uid, str(template).lower()))
    return result


def make_packet(headers: dict, body: bytes = b"") -> bytes:
    header_bytes = json.dumps(headers, separators=(",", ":")).encode("utf-8")
    rest_length = 4 + len(header_bytes) + 4 + len(body)
    return (IDENT + struct.pack("!I", rest_length) +
            struct.pack("!I", len(header_bytes)) + header_bytes +
            struct.pack("!I", len(body)) + body)


def parse_packet(data: bytes):
    if len(data) < 9:
        raise ValueError("Too short")
    if data[:5] != IDENT:
        raise ValueError(f"Bad ident: {data[:5]!r}")
    rest_length = struct.unpack("!I", data[5:9])[0]
    total = 5 + 4 + rest_length
    if len(data) < total:
        raise ValueError(f"Need {total} bytes, have {len(data)}")
    header_length = struct.unpack("!I", data[9:13])[0]
    header_end = 13 + header_length
    headers = json.loads(data[13:header_end].decode("utf-8"))
    body_length = struct.unpack("!I", data[header_end:header_end + 4])[0]
    body_start = header_end + 4
    return headers, data[body_start:body_start + body_length], total
