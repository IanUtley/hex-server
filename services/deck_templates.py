"""Profile deck templates (mercenary decks and saved deck templates).

The client saves a ProfileDeckTemplate through the profile service's JSON
``Network+Request``: ``{"action": "pdecktsave", "Template": <base64
ProfileDeckTemplate.ToBytes()>, "DeckTemplateID": <0 for new>}``.  The
response envelope is decoded with ``EncData.Decode`` (ObjFmt, not JSON) and
must be a ``Game.Shared.Profile.SavedProfileDeckTemplate``.  Saved templates
are also sent at login as ``List<SavedProfileDeckTemplate>``.
"""

import base64
import struct

from encoder import encode_objfmt_response

SAVED_TEMPLATE_TYPE = "Game.Shared.Profile.SavedProfileDeckTemplate"


def _read_varint(data, pos):
    value, shift = 0, 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return value, pos


def _read_guid(data, pos):
    """Read a .NET Guid.ToByteArray() (mixed-endian) GUID."""
    raw = data[pos:pos + 16]
    a, b, c = struct.unpack("<IHH", raw[:8])
    tail = raw[8:].hex()
    return f"{a:08x}-{b:04x}-{c:04x}-{tail[:4]}-{tail[4:]}", pos + 16


def template_cards(data):
    """Return the card template GUIDs of ProfileDeckTemplate bytes.

    Layout (ProfileDeckTemplate.ToBytes): name, champion GUID, sleeve GUID,
    Equip {varint slot, GUID}*, Cards {GUID, varint count, reserve, extended,
    foil bytes, varint gem count, varint gems}*.  Reserve (sideboard) cards
    are skipped; each card is repeated ``count`` times.
    """
    length, pos = _read_varint(data, 0)
    pos += length + 32                       # name, champion, sleeve
    equipped, pos = _read_varint(data, pos)
    for _ in range(equipped):
        _slot, pos = _read_varint(data, pos)
        pos += 16
    count, pos = _read_varint(data, pos)
    cards = []
    for _ in range(count):
        guid, pos = _read_guid(data, pos)
        copies, pos = _read_varint(data, pos)
        reserve = data[pos]
        pos += 3
        gems, pos = _read_varint(data, pos)
        for _ in range(gems):
            _gem, pos = _read_varint(data, pos)
        if not reserve:
            cards.extend([guid] * copies)
    return cards


def template_name(data):
    """Return the deck name stored at the start of ProfileDeckTemplate bytes."""
    try:
        length, pos = _read_varint(data, 0)
        return data[pos:pos + length].decode("utf-8")
    except (UnicodeDecodeError, IndexError, TypeError):
        return ""


def save_template(db, user_id, template_id, data):
    """Insert or update a template; return (id, name)."""
    name = template_name(data)
    row = None
    if template_id:
        row = db.execute(
            "SELECT id FROM profile_deck_templates WHERE id=? AND user_id=?",
            (int(template_id), user_id)).fetchone()
    if row:
        db.execute(
            "UPDATE profile_deck_templates SET name=?, data=?, "
            "updated_at=datetime('now') WHERE id=?", (name, data, row[0]))
        return row[0], name
    cursor = db.execute(
        "INSERT INTO profile_deck_templates (user_id, name, data) VALUES (?,?,?)",
        (user_id, name, data))
    return cursor.lastrowid, name


def delete_template(db, user_id, template_id):
    db.execute("DELETE FROM profile_deck_templates WHERE id=? AND user_id=?",
               (int(template_id or 0), user_id))


def list_templates(db, user_id):
    return [(row[0], row[1], bytes(row[2])) for row in db.execute(
        "SELECT id, name, data FROM profile_deck_templates WHERE user_id=? "
        "ORDER BY id", (user_id,)).fetchall()]


def get_template(db, user_id, template_id):
    row = db.execute(
        "SELECT id, name, data FROM profile_deck_templates WHERE id=? AND user_id=?",
        (int(template_id or 0), user_id)).fetchone()
    return (row[0], row[1], bytes(row[2])) if row else None


def encode_saved_template(template_id, name, data):
    return encode_objfmt_response(
        [SAVED_TEMPLATE_TYPE, "System.UInt64", "System.String",
         "System.Boolean", "System.Byte[]"],
        [("Id", "ulong", int(template_id)), ("Name", "string", name),
         ("Comp", "bool", False), ("Data", "bytes", data)])


def encode_saved_template_list(templates):
    from services.mercenaries import ObjFmtListWriter
    elements = [[("Id", "ulong", tid), ("Name", "string", name),
                 ("Comp", "bool", False), ("Data", "bytes", data)]
                for tid, name, data in templates]
    return ObjFmtListWriter(SAVED_TEMPLATE_TYPE).encode(SAVED_TEMPLATE_TYPE, elements)


def handle_profile_action(db, user_id, env_json):
    """Handle pdecktsave/pdeckdel; return the response envelope or None."""
    action = env_json.get("action")
    if action == "pdecktsave":
        data = base64.b64decode(env_json.get("Template") or "")
        template_id, name = save_template(
            db, user_id, env_json.get("DeckTemplateID"), data)
        db.commit()
        return encode_saved_template(template_id, name, data)
    if action == "pdeckdel":
        delete_template(db, user_id, env_json.get("DeckTemplateID"))
        db.commit()
        return b"{}"
    return None
