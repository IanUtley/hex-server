"""
Encode decks in the game's EncodedDecks binary format.
This format is used in the profile stream to send deck data to the client.
"""
import io
import struct
import json
import sqlite3

_g_db_path = "/home/ianutley/Hex/hconnect.db"

def _get_db():
    db = sqlite3.connect(_g_db_path)
    db.row_factory = sqlite3.Row
    return db

def write_varint(buf, val):
    while val >= 0x80:
        buf.write(bytes([(val & 0x7F) | 0x80]))
        val >>= 7
    buf.write(bytes([val]))

def write_csharp_string(buf, s):
    b = s.encode('utf-8') if isinstance(s, str) else s
    write_varint(buf, len(b))
    buf.write(b)

def write_guid(buf, guid_str):
    """Write GUID in .NET Guid.ToByteArray() format (mixed-endian)."""
    parts = guid_str.replace('-', '')
    b = bytes.fromhex(parts)
    # .NET Guid format: int32 LE, int16 LE, int16 LE, last 8 bytes BE
    guid_le = struct.pack('<IHH', 
        int.from_bytes(b[0:4], 'big'), 
        int.from_bytes(b[4:6], 'big'), 
        int.from_bytes(b[6:8], 'big'))
    buf.write(guid_le)
    buf.write(b[8:16])

def encode_profile_deck_template(buf, name, champ_guid, sleeve_guid, cards, extended_data=None, card_gems=None):
    """Encode a ProfileDeckTemplate.ToBytes() payload.
    card_gems: dict of {instance_id: gem_type_int} for per-instance gem data."""
    write_csharp_string(buf, name)
    write_guid(buf, champ_guid)
    write_guid(buf, sleeve_guid)
    write_varint(buf, 0)                    # Equip count
    write_varint(buf, len(cards))
    for (tguid, count, is_ext, is_foil, is_reserve) in cards:
        write_guid(buf, tguid)
        write_varint(buf, count)
        buf.write(b'\x00' if not is_reserve else b'\x01')
        buf.write(b'\x00' if not is_ext else b'\x01')
        buf.write(b'\x00' if not is_foil else b'\x01')
        gem_types = [int(v) for v in (card_gems or {}).values() if v]
        write_varint(buf, len(gem_types))
        for g in gem_types:
            write_varint(buf, g)
    # ExtendedData dict
    edata = extended_data or {}
    write_varint(buf, len(edata))
    for k, v in edata.items():
        write_csharp_string(buf, k)
        write_csharp_string(buf, v)

def encode_card_group_id(buf, template_guid, is_extended, card_ids):
    """Encode a single CardGroupId entry."""
    write_guid(buf, template_guid)
    buf.write(b'\x01' if is_extended else b'\x00')  # Extended
    escrow_bytes = b'NONE'
    buf.write(struct.pack('<i', len(escrow_bytes)))   # Escrow length (int32)
    buf.write(escrow_bytes)                            # Escrow bytes
    buf.write(b'\x00')                                  # NoTrade
    buf.write(struct.pack('<i', len(card_ids)))        # Card count (int32)
    sorted_ids = sorted(card_ids)
    prev = 0
    for cid in sorted_ids:
        delta = cid - prev
        write_varint(buf, delta)
        prev = cid

def encode_encoded_decks(db_decks, user_id):
    """Create the full EncodedDecks binary payload."""
    db = _get_db()
    buf = io.BytesIO()

    # Collect card data for CardGroupId encoding
    all_cards_by_group = {}  # (template_guid, is_extended) -> [card_ids]

    deck_entries = []
    for dk in db_decks:
        card_ids = json.loads(dk.get("cards", "[]")) if dk.get("cards") else []
        active_gems = {}
        try:
            gems_raw = dk.get("active_gems", "{}")
            if isinstance(gems_raw, str):
                active_gems = json.loads(gems_raw)
            active_gems = {int(k): v for k, v in active_gems.items()}
        except:
            active_gems = {}
        cards = []
        for cid in card_ids:
            row = db.execute(
                "SELECT template_guid, is_extended_art FROM card_instances WHERE user_id=? AND instance_id=?",
                (user_id, cid)).fetchone()
            if row:
                tguid = row["template_guid"]
                is_ext = row["is_extended_art"]
                cards.append((tguid, 1, bool(is_ext), False, False))
                key = (tguid, bool(is_ext))
                if key not in all_cards_by_group:
                    all_cards_by_group[key] = []
                all_cards_by_group[key].append(cid)

        deck_entries.append({
            'name': dk.get('name', ''),
            'champ': dk.get('pvp_champion_guid') or '00000000-0000-0000-0000-000000000000',
            'sleeve': dk.get('deck_sleeve_guid') or '00000000-0000-0000-0000-000000000000',
            'gameboard': dk.get('gameboard_guid') or '00000000-0000-0000-0000-000000000000',
            'coin': dk.get('coin_guid') or '00000000-0000-0000-0000-000000000000',
            'id': dk.get('id', 0),
            'cards': cards,
            'active_gems': active_gems,
        })

    buf.write(struct.pack('<i', 1))  # version
    buf.write(struct.pack('<i', len(deck_entries)))  # count

    for dk in deck_entries:
        # ProfileDeckTemplate.ToBytes()
        pt_buf = io.BytesIO()
        encode_profile_deck_template(pt_buf, dk['name'], dk['champ'], dk['sleeve'], dk['cards'], card_gems=dk['active_gems'])
        pt_bytes = pt_buf.getvalue()

        buf.write(struct.pack('<i', len(pt_bytes)))  # template length
        buf.write(pt_bytes)                            # template bytes
        write_guid(buf, dk['gameboard'])               # gameboard
        buf.write(struct.pack('<Q', dk['id']))         # Id (raw instance id, client creates UID)
        buf.write(struct.pack('<i', 0))                # Lock (Not_Locked)
        write_varint(buf, 0)                           # LockHolder
        write_varint(buf, 0)                           # PvEChampionId
        write_varint(buf, 0)                           # Personality (Default)
        write_csharp_string(buf, dk['coin'])           # Coin

    # CardGroupId.EncodeGroup
    buf.write(struct.pack('<i', len(all_cards_by_group)))
    for (tguid, is_ext), card_ids in sorted(all_cards_by_group.items()):
        if card_ids:
            encode_card_group_id(buf, tguid, is_ext, card_ids)

    db.close()
    return buf.getvalue()
