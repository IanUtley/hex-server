"""ObjFmt encoder — all game object encoding functions extracted from hconnect_server.py."""
import io
import struct
import time
import gzip
import json
from binascii import hexlify


# =============================================================================
#  Helper: compress / decompress
# =============================================================================

def compress_gzip(data):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=1) as f:
        f.write(data)
    return buf.getvalue()


def decompress_gzip(data):
    import zlib
    return zlib.decompress(data, 16 + zlib.MAX_WBITS)


# =============================================================================
#  UID utilities
# =============================================================================

def make_uid(type_byte, instance_id):
    return (type_byte & 0xFF) | ((instance_id & 0x00FFFFFFFFFFFFFF) << 8)


def client_session_guid(handler):
    """Return the handler's cached RequestHandlerSessionId or the zero GUID."""
    return getattr(handler, 'client_req_session_id', None) or \
        "00000000-0000-0000-0000-000000000000"


# =============================================================================
#  Low-level element encoders (called from within encode_objfmt_response)
# =============================================================================

def encode_inventory_item(buf, sizes, ft, template_guid, item_id, elem_idx, quantity=1, bound=True):
    """Encode one inventory_bits inline and append its sizes to the sizes list."""
    f = buf.tell()
    sizes.append(0)
    idx = len(sizes) - 1
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";")
    
    w(str(elem_idx)); sep(); w(str(idx)); sep(); w(str(ft("Game.Shared.Domain.inventory_bits"))); sep(); w("6"); sep()
    
    # Id (ulong)
    f1 = buf.tell(); sizes.append(0)
    w("Id"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", item_id)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f1
    
    # TemplateID (ResourceId)
    f2 = buf.tell(); sizes.append(0); tidx = len(sizes)-1
    w("TemplateID"); sep(); w(str(tidx)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
    gs = buf.tell(); sizes.append(0); gidx = len(sizes)-1
    w("guid"); sep(); w(str(gidx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
    w("36"); sep(); buf.write(template_guid.encode())
    sizes[gidx] = buf.tell() - gs
    sizes[tidx] = buf.tell() - f2
    
    # BoundToProfile (bool)
    f3 = buf.tell(); sizes.append(0)
    w("BoundToProfile"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
    w("1" if bound else "0")
    sizes[-1] = buf.tell() - f3
    
    # ItemQuantity (int)
    f4 = buf.tell(); sizes.append(0)
    w("ItemQuantity"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", quantity)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f4
    
    # ClaimDate (DateTime) — use MinValue to bypass expiration filter
    f5 = buf.tell(); sizes.append(0)
    w("ClaimDate"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.DateTime"))); sep(); w("0"); sep()
    enc = b"01/01/0001 00:00:00"  # DateTime.MinValue = no expiration
    w(str(len(enc))); sep(); buf.write(enc)
    sizes[-1] = buf.tell() - f5
    
    # EscrowStatus (string)
    f6 = buf.tell(); sizes.append(0)
    w("EscrowStatus"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
    enc = b"Clean"
    w(str(len(enc))); sep(); buf.write(enc)
    sizes[-1] = buf.tell() - f6
    
    sizes[idx] = buf.tell() - f
    return idx


def encode_player_state_coll(buf, sizes, find_type, player_states):
    """Encode a List<PlayerState> value: count;element0;element1;...

    Each player_state is (uid_val_hex, position_int, name_bytes).
    """
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";")

    ft = find_type
    t_uid = ft("Game.Shared.UID")
    t_uint64 = ft("System.UInt64")
    t_int = ft("System.Int32")
    t_string = ft("System.String")
    t_playerstate = ft("Game.Shared.PlayerState")

    w(str(len(player_states))); sep()

    for i, (uid_val_hex, position, name_bytes) in enumerate(player_states):
        f = buf.tell(); sizes.append(0); eidx = len(sizes) - 1
        w(str(i)); sep(); w(str(eidx)); sep(); w(str(t_playerstate)); sep(); w("2"); sep()

        # PlayerId (UID)
        f1 = buf.tell(); sizes.append(0)
        w("PlayerId"); sep(); w(str(len(sizes) - 1)); sep(); w(str(t_uid)); sep(); w("1"); sep()
        f1a = buf.tell(); sizes.append(0)
        w("m_UID64"); sep(); w(str(len(sizes) - 1)); sep(); w(str(t_uint64)); sep(); w("0"); sep()
        w(uid_val_hex); sep()
        sizes[-1] = buf.tell() - f1a
        sizes[-2] = buf.tell() - f1

        # PlayerPosition (int)
        f2 = buf.tell(); sizes.append(0)
        w("PlayerPosition"); sep(); w(str(len(sizes) - 1)); sep(); w(str(t_int)); sep(); w("0"); sep()
        w(hexlify(struct.pack("<i", position)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f2

        sizes[eidx] = buf.tell() - f


def encode_card_instance(buf, sizes, ft, guid, name, card_id, cost, atk, def_, idx):
    """Encode one card_instance_bits inline. 6 fields."""
    f = buf.tell(); sizes.append(0)
    sidx = len(sizes) - 1
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";")
    w(str(idx)); sep(); w(str(sidx)); sep(); w(str(ft("Game.Shared.Domain.card_instance_bits"))); sep(); w("6"); sep()

    # Id
    f1 = buf.tell(); sizes.append(0)
    w("Id"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", card_id)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f1

    # TemplateID
    f2 = buf.tell(); sizes.append(0); tidx = len(sizes)-1
    w("TemplateID"); sep(); w(str(tidx)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
    gs = buf.tell(); sizes.append(0); gidx = len(sizes)-1
    w("guid"); sep(); w(str(gidx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
    w("36"); sep(); buf.write(guid.encode())
    sizes[gidx] = buf.tell() - gs
    sizes[tidx] = buf.tell() - f2

    # IsFoil
    f4 = buf.tell(); sizes.append(0)
    w("IsFoil"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
    w("0")
    sizes[-1] = buf.tell() - f4

    # IsExtended
    f5 = buf.tell(); sizes.append(0)
    w("IsExtended"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
    w("0")
    sizes[-1] = buf.tell() - f5

    # IsNotTradeable
    f7 = buf.tell(); sizes.append(0)
    w("IsNotTradeable"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
    w("0")
    sizes[-1] = buf.tell() - f7

    # EscrowStatus
    f8 = buf.tell(); sizes.append(0)
    w("EscrowStatus"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
    enc = b"Clean"; w(str(len(enc))); sep(); buf.write(enc)
    sizes[-1] = buf.tell() - f8

    sizes[sidx] = buf.tell() - f
    return sidx


def encode_deck_bits(buf, sizes, ft, did, dname, did_val, champ_did, card_guids, idx):
    """Encode one deck_bits inline WITH element header (for use in cardlist/decklist)."""
    f = buf.tell(); sizes.append(0)
    sidx = len(sizes) - 1
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";")
    w(str(idx)); sep(); w(str(sidx)); sep(); w(str(ft("Game.Shared.Domain.deck_bits"))); sep(); w("25"); sep()
    encode_deck_bits_fields(buf, sizes, ft, did, dname, did_val, champ_did, card_guids)
    sizes[sidx] = buf.tell() - f
    return sidx


def encode_deck_bits_fields(buf, sizes, ft, did, dname, did_val, champ_did, card_guids):
    """Encode the 25 deck_bits fields directly (no element wrapper)."""
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";")

    def wf_rid(name, guid_val):
        fb = buf.tell(); sizes.append(0); fl = len(sizes)-1
        w(name); sep(); w(str(fl)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
        gs = buf.tell(); sizes.append(0); gl = len(sizes)-1
        w("guid"); sep(); w(str(gl)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
        g = guid_val.encode("utf-8") if isinstance(guid_val, str) else guid_val
        w(str(len(g))); sep(); buf.write(g)
        sizes[gl] = buf.tell() - gs; sizes[fl] = buf.tell() - fb

    # 1. Id
    f1 = buf.tell(); sizes.append(0)
    w("Id"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", did_val)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f1
    # 2. DeckName
    f2 = buf.tell(); sizes.append(0)
    w("DeckName"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
    enc = dname.encode("utf-8"); w(str(len(enc))); sep(); buf.write(enc)
    sizes[-1] = buf.tell() - f2
    # 3. PVEChampionId
    f3 = buf.tell(); sizes.append(0)
    w("PVEChampionId"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", champ_did)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f3
    # 4. PVPChampionId
    wf_rid("PVPChampionId", b"00000000-0000-0000-0000-000000000000")
    # 5-9. Talents 1-5
    for tn in ("talent_1","talent_2","talent_3","talent_4","talent_5"):
        wf_rid(tn, b"00000000-0000-0000-0000-000000000000")
    # 10-15. Equipment 1-6
    for en in ("equipment_1","equipment_2","equipment_3","equipment_4","equipment_5","equipment_6"):
        wf_rid(en, b"00000000-0000-0000-0000-000000000000")
    # 16. CardsInDeck (empty)
    f_cd = buf.tell(); sizes.append(0)
    w("CardsInDeck"); sep(); w(str(len(sizes)-1)); sep()
    w(str(ft("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits"))); sep(); w("0"); sep()
    w("0"); sep()
    sizes[-1] = buf.tell() - f_cd
    # 17. CardsInSideboard (empty)
    f_cs = buf.tell(); sizes.append(0)
    w("CardsInSideboard"); sep(); w(str(len(sizes)-1)); sep()
    w(str(ft("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits"))); sep(); w("0"); sep()
    w("0"); sep()
    sizes[-1] = buf.tell() - f_cs
    # 18. ActiveGems — Dictionary<ulong, EGemTypesNew> (empty)
    f_ag = buf.tell(); sizes.append(0)
    w("ActiveGems"); sep(); w(str(len(sizes)-1)); sep()
    w(str(ft("System.Collections.Generic.Dictionary`2#System.UInt64!Game.Shared.Mechanics.EGemTypesNew"))); sep(); w("0"); sep()
    w("0"); sep()
    sizes[-1] = buf.tell() - f_ag
    # 19. Lock — EDeckLock enum (Not_Locked = 0)
    f_lock = buf.tell(); sizes.append(0)
    w("Lock"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.Mechanics.EDeckLock"))); sep(); w("1"); sep()
    f_lv = buf.tell(); sizes.append(0)
    w("value__"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f_lv; sizes[-2] = buf.tell() - f_lock
    # 20. LockHolder (ulong 0)
    f_lh = buf.tell(); sizes.append(0)
    w("LockHolder"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f_lh
    # 21. deck_sleeve
    wf_rid("deck_sleeve", b"00000000-0000-0000-0000-000000000000")
    # 22. gameboard
    wf_rid("gameboard", b"00000000-0000-0000-0000-000000000000")
    # 23. Coin
    wf_rid("Coin", b"00000000-0000-0000-0000-000000000000")
    # 24. player_id (ulong 0)
    f_pid = buf.tell(); sizes.append(0)
    w("player_id"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f_pid
    # 25. Personality — EDeckPersonality enum (Default = 0)
    f_per = buf.tell(); sizes.append(0)
    w("Personality"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.Mechanics.EDeckPersonality"))); sep(); w("1"); sep()
    f_pv = buf.tell(); sizes.append(0)
    w("value__"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f_pv; sizes[-2] = buf.tell() - f_per


def encode_champion_bits_minimal(buf, sizes, ft, cu64, cname, cid, lvl, xp, cc, rc, gd, idx,
                                 last_campaign_id=0, last_deck_id=0, talents=None,
                                 pet_name=""):
    """Encode one champion_bits inline, optionally including ChampionTalents.

    The profile stream historically used the 11-field form. Keep that form
    when ``talents`` is omitted so existing non-profile callers remain wire
    compatible; the login profile passes a list and receives the persisted
    talent list as the twelfth field.
    """
    f = buf.tell(); sizes.append(0)
    sidx = len(sizes) - 1
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";")
    w(str(idx)); sep(); w(str(sidx)); sep(); w(str(ft("Game.Shared.Domain.champion_bits"))); sep()
    w(str(11 + (1 if talents is not None else 0))); sep()
    
    def wf_enum(name, etype, val):
        fld = buf.tell(); sizes.append(0)
        w(name); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(etype))); sep(); w("1"); sep()
        fsub = buf.tell(); sizes.append(0)
        w("value__"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<i", val)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - fsub
        sizes[-2] = buf.tell() - fld
    
    # Name
    f1 = buf.tell(); sizes.append(0)
    w("Name"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
    enc = cname.encode("utf-8"); w(str(len(enc))); sep(); buf.write(enc)
    sizes[-1] = buf.tell() - f1
    # Id
    f2 = buf.tell(); sizes.append(0)
    w("Id"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", cid)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f2
    # Level
    f3 = buf.tell(); sizes.append(0)
    w("Level"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", lvl)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f3
    # CurrentXP
    f4 = buf.tell(); sizes.append(0)
    w("CurrentXP"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", xp)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f4
    # ChampionClass (enum)
    wf_enum("ChampionClass", "Game.Shared.Mechanics.EChampionClass", cc)
    # Race (enum)
    wf_enum("Race", "Game.Shared.Mechanics.ERace", rc)
    # Gender (enum)
    wf_enum("Gender", "Game.Shared.Mechanics.EGender", gd)
    # OwnerChampionId
    f5 = buf.tell(); sizes.append(0)
    w("OwnerChampionId"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f5
    # LastCampaignID
    f6 = buf.tell(); sizes.append(0)
    w("LastCampaignID"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", last_campaign_id)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f6
    # LastDeckID
    f7 = buf.tell(); sizes.append(0)
    w("LastDeckID"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", last_deck_id)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f7
    # PetName
    f8 = buf.tell(); sizes.append(0)
    w("PetName"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
    encoded_pet_name = str(pet_name or "").encode("utf-8")
    w(str(len(encoded_pet_name))); sep(); buf.write(encoded_pet_name)
    sizes[-1] = buf.tell() - f8
    if talents is not None:
        # ChampionTalents is List<ResourceId>. ResourceId is a data contract
        # struct whose serialized member is ``m_Guid`` (the ``guid`` property
        # is only a convenience accessor on the client).
        f9 = buf.tell(); sizes.append(0)
        talent_field_idx = len(sizes) - 1
        w("ChampionTalents"); sep(); w(str(len(sizes)-1)); sep()
        w(str(ft("System.Collections.Generic.List`1#Game.Shared.ResourceId"))); sep(); w("0"); sep()
        w(str(len(talents))); sep()
        for talent_idx, guid in enumerate(talents):
            fe = buf.tell(); sizes.append(0); elem_idx = len(sizes)-1
            w(str(talent_idx)); sep(); w(str(elem_idx)); sep()
            w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
            fg = buf.tell(); sizes.append(0); guid_idx = len(sizes)-1
            w("m_Guid"); sep(); w(str(guid_idx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
            encoded_guid = str(guid).encode("utf-8")
            w(str(len(encoded_guid))); sep(); buf.write(encoded_guid)
            sizes[guid_idx] = buf.tell() - fg
            sizes[elem_idx] = buf.tell() - fe
        sizes[talent_field_idx] = buf.tell() - f9
    sizes[sidx] = buf.tell() - f
    return sidx


# =============================================================================
#  Core ObjFmt encoder
# =============================================================================

def encode_objfmt_response(type_names, fields):
    sizes = []
    buf = io.BytesIO()

    def w(s):
        buf.write(s.encode("utf-8"))
    def sep():
        buf.write(b";")
    def lf():
        buf.write(b"\n")

    def find_type(tname):
        if tname not in type_names:
            type_names.append(tname)
        return type_names.index(tname)

    def encode_field(name, tcode, val):
        """Encode a single field and return. Updates sizes."""
        if tcode == "long":
            tname = "System.Int64"
        elif tcode == "ulong":
            tname = "System.UInt64"
        elif tcode == "int":
            tname = "System.Int32"
        elif tcode == "uint":
            tname = "System.UInt32"
        elif tcode == "byte":
            tname = "System.Byte"
        elif tcode == "bool":
            tname = "System.Boolean"
        elif tcode == "bytes":
            tname = "System.Byte[]"
        elif tcode == "guid":
            tname = "System.Guid"
        elif tcode == "string":
            tname = "System.String"
        elif tcode == "datetime":
            tname = "System.DateTime"
        elif tcode == "enum":
            tname, evalue = val
        elif tcode == "enum1":
            tname = val[0]
            evalue_int = val[1]
        elif tcode == "coll":
            tname = val[0]
            ecount = val[1]
            elem_data = val[2] if len(val) > 2 else []
        elif tcode == "cardlist":
            tname = val[0]
            ecount = val[1]
            card_data = val[2] if len(val) > 2 else []
        elif tcode == "decklist":
            tname = val[0]
            ecount = val[1]
            deck_data = val[2] if len(val) > 2 else []
        elif tcode == "champlist":
            tname = val[0]
            ecount = val[1]
            champ_data = val[2] if len(val) > 2 else []
        elif tcode == "intlist":
            tname = val[0]
            ecount = val[1]
            elems = val[2] if len(val) > 2 else []
        elif tcode == "arenafightlist":
            tname = val[0]
            ecount = val[1]
            fight_data = val[2] if len(val) > 2 else []
        elif tcode == "encountermodlist":
            tname = val[0]
            ecount = val[1]
            mod_data = val[2] if len(val) > 2 else []
        elif tcode == "uid":
            tname = "Game.Shared.UID"
            uid_val = val
        elif tcode == "struct":
            tname, sub_fields = val
        elif tcode == "raw":
            tname, raw_bytes = val
        elif tcode == "deckbits":
            tname = "Game.Shared.Domain.deck_bits"
        elif tcode == "playerstate_coll":
            tname, player_data = val
        elif tcode == "uidlist":
            tname = val[0]
            uid_data = val[2] if len(val) > 2 else []
        elif tcode == "class":
            tname = val
        else:
            tname = tcode

        f_start = buf.tell()
        sizes.append(0)
        field_idx = len(sizes) - 1

        w(name)
        sep()
        w(str(len(sizes) - 1))
        sep()
        w(str(find_type(tname)))
        sep()
        if tcode in ("enum1", "uid"):
            num_props = 1
        elif tcode == "struct":
            num_props = len(sub_fields)
        elif tcode == "deckbits":
            num_props = 25
        else:
            num_props = 0
        w(str(num_props))
        sep()

        if tcode == "long":
            w(hexlify(struct.pack("<q", val)).decode("ascii"))
            sep()
        elif tcode == "ulong":
            w(hexlify(struct.pack("<Q", val)).decode("ascii"))
            sep()
        elif tcode == "int":
            w(hexlify(struct.pack("<i", val)).decode("ascii"))
            sep()
        elif tcode == "uint":
            w(hexlify(struct.pack("<I", val)).decode("ascii"))
            sep()
        elif tcode == "byte":
            w(hexlify(bytes([val])).decode("ascii"))
            sep()
        elif tcode == "bool":
            w('1' if val else '0')
        elif tcode == "bytes":
            buf.write(struct.pack("!I", len(val)))
            buf.write(val)
        elif tcode == "guid":
            w(str(len(val)))
            sep()
            buf.write(val.encode("utf-8"))
        elif tcode == "string":
            enc = val.encode("utf-8")
            w(str(len(enc)))
            sep()
            buf.write(enc)
        elif tcode == "datetime":
            enc = val.encode("utf-8")
            w(str(len(enc)))
            sep()
            buf.write(enc)
        elif tcode == "enum":
            w(evalue)
            sep()
        elif tcode == "enum1":
            f_enum_start = buf.tell(); sizes.append(0)
            w("value__"); sep(); w(str(len(sizes)-1)); sep(); w(str(find_type("System.Int32"))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<i", evalue_int)).decode("ascii")); sep()
            sizes[-1] = buf.tell() - f_enum_start
        elif tcode == "coll":
            w(str(ecount))
            sep()
            if elem_data:
                for i, (tguid, iid, qty) in enumerate(elem_data):
                    encode_inventory_item(buf, sizes, find_type, tguid, iid, i, qty)
        elif tcode == "cardlist":
            w(str(ecount))
            sep()
            for i, (guid, name, card_id, cost, atk, def_) in enumerate(card_data):
                encode_card_instance(buf, sizes, find_type, guid, name, card_id, cost, atk, def_, i)
        elif tcode == "decklist":
            w(str(ecount))
            sep()
            for i, (did, dname, did_val, champ_did, cards_json, card_guids) in enumerate(deck_data):
                encode_deck_bits(buf, sizes, find_type, did, dname, did_val, champ_did, card_guids, i)
        elif tcode == "deckbits":
            did, dname, did_val, champ_did, card_guids = val
            encode_deck_bits_fields(buf, sizes, find_type, did, dname, did_val, champ_did, card_guids)
        elif tcode == "champlist":
            w(str(ecount))
            sep()
            for i, entry in enumerate(champ_data):
                cu64, cname, cid, lvl, xp, cc, rc, gd = entry[:8]
                lcid = entry[8] if len(entry) > 8 else 0
                ldid = entry[9] if len(entry) > 9 else 0
                talents = entry[10] if len(entry) > 10 else None
                pet_name = entry[11] if len(entry) > 11 else ""
                encode_champion_bits_minimal(buf, sizes, find_type, cu64, cname, cid, lvl, xp, cc, rc, gd, i,
                                             lcid, ldid, talents, pet_name)
        elif tcode == "uid":
            f_uid_start = buf.tell(); sizes.append(0)
            w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(find_type("System.UInt64"))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<Q", uid_val)).decode("ascii")); sep()
            sizes[-1] = buf.tell() - f_uid_start
        elif tcode == "intlist":
            w(str(ecount))
            sep()
            for i, e in enumerate(elems):
                se = buf.tell(); sizes.append(0)
                w(str(i)); sep(); w(str(len(sizes)-1)); sep()
                w(str(find_type("System.Int32"))); sep(); w("0"); sep()
                w(hexlify(struct.pack("<i", e)).decode("ascii")); sep()
                sizes[-1] = buf.tell() - se
        elif tcode == "arenafightlist":
            w(str(ecount))
            sep()
            for i, fight in enumerate(fight_data):
                se = buf.tell(); sizes.append(0)
                w(str(i)); sep(); w(str(len(sizes) - 1)); sep()
                w(str(find_type("Reckoning.Campaign.Messages.Arena.ArenaFight")))
                sep(); w("8"); sep()
                for name, stcode, sval in (
                    ("FightID", "ulong", int(fight.get("fight_id", i + 1))),
                    ("FightTier", "int", int(fight.get("fight_tier", i // 5 + 1))),
                    ("FightOrder", "int", int(fight.get("fight_order", i + 1))),
                    ("ArenaInstance", "ulong", int(fight.get("arena_instance", 1))),
                    ("ChallengerInstance", "ulong", int(fight.get("challenger_instance", i + 1))),
                    ("FightResults", "string", str(fight.get("result", "NONE"))),
                    ("ChallengeResponse", "string", str(fight.get("challenge_response", ""))),
                    ("RoundChallenge", "struct", ("Game.Shared.ResourceId", [
                        ("m_Guid", "guid", str(fight.get(
                            "round_challenge",
                            "00000000-0000-0000-0000-000000000000")))
                    ])),
                ):
                    encode_field(name, stcode, sval)
                sizes[-1] = buf.tell() - se
        elif tcode == "encountermodlist":
            w(str(ecount))
            sep()
            for i, modification in enumerate(mod_data):
                se = buf.tell(); sizes.append(0)
                w(str(i)); sep(); w(str(len(sizes) - 1)); sep()
                mod_type = modification.get(
                    "wire_type", "Reckoning.Game.EncounterModAddChampionHealth")
                w(str(find_type(mod_type))); sep(); w("6"); sep()
                for name, stcode, sval in (
                    ("Amount", "int", int(modification.get("amount", 0))),
                    ("Absolute", "bool", bool(modification.get("absolute", False))),
                    ("IsApplied", "bool", bool(modification.get("is_applied", False))),
                    ("RoundToApply", "int", int(modification.get("round_to_apply", 0))),
                    ("ConversationId", "struct", ("Game.Shared.ResourceId", [
                        ("m_Guid", "guid", str(modification.get(
                            "conversation_id",
                            "00000000-0000-0000-0000-000000000000")))
                    ])),
                    ("TargetPlayer", "enum1", (
                        "Game.Shared.Mechanics.EModTarget",
                        int(modification.get("target_player", 0)))),
                ):
                    encode_field(name, stcode, sval)
                sizes[-1] = buf.tell() - se
        elif tcode == "struct":
            for sname, stcode, sval in sub_fields:
                encode_field(sname, stcode, sval)
        elif tcode == "raw":
            buf.write(raw_bytes)
        elif tcode == "playerstate_coll":
            encode_player_state_coll(buf, sizes, find_type, player_data)
        elif tcode == "uidlist":
            w(str(len(uid_data)))
            sep()
            for i, e in enumerate(uid_data):
                se = buf.tell(); sizes.append(0)
                w(str(i)); sep(); w(str(len(sizes)-1)); sep()
                w(str(find_type("Game.Shared.UID"))); sep(); w("1"); sep()
                fe = buf.tell(); sizes.append(0)
                w("m_UID64"); sep(); w(str(len(sizes)-1)); sep()
                w(str(find_type("System.UInt64"))); sep(); w("0"); sep()
                w(hexlify(struct.pack("<Q", e)).decode("ascii")); sep()
                sizes[-1] = buf.tell() - fe
                sizes[-2] = buf.tell() - se
        elif tcode == "class":
            pass

        sizes[field_idx] = buf.tell() - f_start

    # Root header
    root_start = buf.tell()
    sizes.append(0)

    w("")
    sep()
    w("0")
    sep()
    w("0")
    sep()
    w(str(len(fields)))
    sep()

    for name, tcode, val in fields:
        encode_field(name, tcode, val)

    sizes[0] = buf.tell()

    w(";".join(type_names))
    lf()
    for i, s in enumerate(sizes):
        if i > 0:
            w(";")
        w(str(s))

    return buf.getvalue()


# =============================================================================
#  High-level message / response encoders
# =============================================================================

def encode_objfmt_string(s_value):
    """Encode a standalone System.String value in ObjFmt format."""
    type_names = ["System.String"]
    buf = io.BytesIO()
    def w(s):
        buf.write(s.encode("utf-8"))
    def sep():
        buf.write(b";")

    w(""); sep(); w("0"); sep(); w("0"); sep(); w("0"); sep()
    enc = s_value.encode("utf-8")
    w(str(len(enc))); sep(); buf.write(enc)

    data_size = buf.tell()
    w(";".join(type_names))
    buf.write(b"\n")
    w(str(data_size))
    return buf.getvalue()


def encode_chest_list(chests):
    """Encode a standalone List<chest_bits> as ObjFmt bytes.

    Used to deliver unopened treasure chests in the login profile stream
    (the client's PlayerProfile.HandleProfileStream collects List<chest_bits>
    objects and feeds them to CreateLocalTreasureCache).

    chests: list of tuples (chest_rarity, spin_status, set_guid, inventory_id)
            chest_rarity is the ETreasureChestType enum value
            (Common=0, Uncommon=1, Rare=2, Legendary=3, Primal=4, Promo=5).
    """
    type_names = [
        "System.Collections.Generic.List`1#Game.Shared.Domain.chest_bits",
        "Game.Shared.Domain.chest_bits",
        "System.Int32",
        "System.Boolean",
        "System.UInt64",
        "System.UInt32",
        "Game.Shared.ResourceId",
        "System.Guid",
    ]
    sizes = []
    buf = io.BytesIO()

    def w(s):
        buf.write(s.encode("utf-8"))
    def sep():
        buf.write(b";")
    def lf():
        buf.write(b"\n")
    def ft(tn):
        if tn not in type_names:
            type_names.append(tn)
        return type_names.index(tn)

    # Root header: the List<T> object itself.
    sizes.append(0)
    w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("0"); sep()
    fc = buf.tell(); sizes.append(0)
    w(str(len(chests))); sep()

    for i, (rarity, spin, set_guid, inventory_id) in enumerate(chests):
        fe = buf.tell(); sizes.append(0); eidx = len(sizes) - 1
        w(str(i)); sep(); w(str(eidx)); sep(); w(str(ft(type_names[1]))); sep(); w("8"); sep()

        # ChestRarity (int)
        f1 = buf.tell(); sizes.append(0)
        w("ChestRarity"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<i", rarity)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f1

        # WOFSpinStatus (int)
        f2 = buf.tell(); sizes.append(0)
        w("WOFSpinStatus"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<i", spin)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f2

        # BoosterPackType (ResourceId -> set guid)
        f3 = buf.tell(); sizes.append(0); tidx = len(sizes)-1
        w("BoosterPackType"); sep(); w(str(tidx)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
        gs = buf.tell(); sizes.append(0); gidx = len(sizes)-1
        w("guid"); sep(); w(str(gidx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
        w("36"); sep(); buf.write(set_guid.encode())
        sizes[gidx] = buf.tell() - gs; sizes[tidx] = buf.tell() - f3

        # WasOpened (bool)
        f4 = buf.tell(); sizes.append(0)
        w("WasOpened"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
        w("0"); sizes[-1] = buf.tell() - f4

        # InventoryId (ulong)
        f5 = buf.tell(); sizes.append(0)
        w("InventoryId"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<Q", inventory_id)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f5

        # PromoID (uint)
        f6 = buf.tell(); sizes.append(0)
        w("PromoID"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt32"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<I", 0)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f6

        # TempateID (ResourceId -> set guid)
        f7 = buf.tell(); sizes.append(0); tidx2 = len(sizes)-1
        w("TempateID"); sep(); w(str(tidx2)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
        gs2 = buf.tell(); sizes.append(0); gidx2 = len(sizes)-1
        w("guid"); sep(); w(str(gidx2)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
        w("36"); sep(); buf.write(set_guid.encode())
        sizes[gidx2] = buf.tell() - gs2; sizes[tidx2] = buf.tell() - f7

        # Vendor (int)
        f8 = buf.tell(); sizes.append(0)
        w("Vendor"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<i", 0)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f8

        sizes[eidx] = buf.tell() - fe

    sizes[1] = buf.tell() - fc
    sizes[0] = buf.tell()
    w(";".join(type_names)); lf()
    for i, s in enumerate(sizes):
        if i > 0:
            w(";")
        w(str(s))
    return buf.getvalue()


def encode_session_state(session_id, session_name, min_players=2, max_players=2):
    """Manually encode a SessionState object as ObjFmt."""
    sizes = []
    buf = io.BytesIO()
    def w(s): buf.write(s.encode("utf-8"))
    def sep(): buf.write(b";")

    type_names = ["Game.Shared.SessionState", "Game.Shared.UID",
                  "System.UInt64", "System.String", "System.Int32",
                  "Game.Shared.SessionStateEncounterData", "System.Boolean"]

    def ft(tn):
        if tn not in type_names:
            type_names.append(tn)
        return type_names.index(tn)

    sizes.append(0)
    # Root: SessionId;idx;UID_type;1;
    w(""); sep(); w("0"); sep(); w(str(ft("Game.Shared.SessionState"))); sep(); w("6"); sep()

    # SessionId (UID)
    f1 = buf.tell(); sizes.append(0)
    w("SessionId"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
    f1a = buf.tell(); sizes.append(0)
    w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", session_id)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f1a
    sizes[-2] = buf.tell() - f1

    # SessionName (string)
    f2 = buf.tell(); sizes.append(0)
    w("SessionName"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
    enc = session_name.encode("utf-8")
    w(str(len(enc))); sep(); buf.write(enc)
    sizes[-1] = buf.tell() - f2

    # MinimumPlayerCount (int)
    f3 = buf.tell(); sizes.append(0)
    w("MinimumPlayerCount"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", min_players)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f3

    # MaximumPlayerCount (int)
    f4 = buf.tell(); sizes.append(0)
    w("MaximumPlayerCount"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", max_players)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f4

    # EncounterData (null)
    f5 = buf.tell(); sizes.append(0)
    w("EncounterData"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.SessionStateEncounterData"))); sep(); w("0"); sep()
    sizes[-1] = buf.tell() - f5

    # JoinInsteadOfReconnect (bool)
    f6 = buf.tell(); sizes.append(0)
    w("JoinInsteadOfReconnect"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
    w("0")
    sizes[-1] = buf.tell() - f6

    sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, s in enumerate(sizes):
        if i > 0: sep()
        w(str(s))
    return buf.getvalue()


def encode_campaign_session_state(session_id, session_name, scene_template_id,
                                  session_flags=4, session_uid=0,
                                  tournament_id=0, first_player=0,
                                  tournament_player_ids=None):
    """Encode a SessionState (with EncounterData) as ObjFmt bytes.

    Used by the campaign 'gamestarted' notification. The client decodes this
    via EncData.Decode(GameSession) -> SessionState, then reads
    EncounterData.SceneTemplateId to load the battle scene + AI deck.

    session_flags: ESessionFlags bitmask (IsEncounter=1, IsPvE=4,
                   IsTutorial=8, IsPvEArena=128).
    """
    sizes = []
    buf = io.BytesIO()
    def w(s): buf.write(s.encode("utf-8"))
    def sep(): buf.write(b";")

    type_names = [
        "Game.Shared.SessionState",
        "Game.Shared.UID", "System.UInt64", "System.String", "System.Int32",
        "Game.Shared.SessionStateEncounterData", "System.Boolean",
        "Game.Shared.ResourceId", "System.Guid",
        "Game.Shared.ESessionFlags",
    ]
    def ft(tn):
        if tn not in type_names:
            type_names.append(tn)
        return type_names.index(tn)

    sizes.append(0)
    # Root: SessionState, 6 props
    w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("6"); sep()

    # SessionId (UID)
    f1 = buf.tell(); sizes.append(0)
    w("SessionId"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
    f1a = buf.tell(); sizes.append(0)
    w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", session_id)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f1a; sizes[-2] = buf.tell() - f1

    # SessionName (string)
    f2 = buf.tell(); sizes.append(0)
    w("SessionName"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
    enc = session_name.encode("utf-8")
    w(str(len(enc))); sep(); buf.write(enc)
    sizes[-1] = buf.tell() - f2

    # MinimumPlayerCount (int)
    f3 = buf.tell(); sizes.append(0)
    w("MinimumPlayerCount"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", 1)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f3

    # MaximumPlayerCount (int)
    f4 = buf.tell(); sizes.append(0)
    w("MaximumPlayerCount"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", 2)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f4

    # EncounterData (SessionStateEncounterData) with SceneTemplateId + flags
    f5 = buf.tell(); sizes.append(0); ed_idx = len(sizes)-1
    w("EncounterData"); sep(); w(str(ed_idx)); sep(); w(str(ft("Game.Shared.SessionStateEncounterData"))); sep(); w("16"); sep()

    # SceneTemplateId (ResourceId -> guid)
    f5a = buf.tell(); sizes.append(0); rt_idx = len(sizes)-1
    w("SceneTemplateId"); sep(); w(str(rt_idx)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
    f5b = buf.tell(); sizes.append(0); gidx = len(sizes)-1
    w("m_Guid"); sep(); w(str(gidx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
    w("36"); sep(); buf.write(scene_template_id.encode())
    sizes[gidx] = buf.tell() - f5b; sizes[rt_idx] = buf.tell() - f5a

    # DungeonTemplateId (ResourceId -> null/invalid)
    f5c = buf.tell(); sizes.append(0); rt2 = len(sizes)-1
    w("DungeonTemplateId"); sep(); w(str(rt2)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
    f5d = buf.tell(); sizes.append(0); g2 = len(sizes)-1
    w("m_Guid"); sep(); w(str(g2)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
    w("36"); sep(); buf.write("00000000-0000-0000-0000-000000000000".encode())
    sizes[g2] = buf.tell() - f5d; sizes[rt2] = buf.tell() - f5c

    # NodeTrackerId (ResourceId -> null/invalid)
    f5e = buf.tell(); sizes.append(0); rt3 = len(sizes)-1
    w("NodeTrackerId"); sep(); w(str(rt3)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
    f5f = buf.tell(); sizes.append(0); g3 = len(sizes)-1
    w("m_Guid"); sep(); w(str(g3)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
    w("36"); sep(); buf.write("00000000-0000-0000-0000-000000000000".encode())
    sizes[g3] = buf.tell() - f5f; sizes[rt3] = buf.tell() - f5e

    # SessionFlags (ESessionFlags enum) — 1 prop with value__ int32
    f5g = buf.tell(); sizes.append(0)
    w("SessionFlags"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.ESessionFlags"))); sep(); w("1"); sep()
    f5g2 = buf.tell(); sizes.append(0)
    w("value__"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", session_flags)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f5g2; sizes[-2] = buf.tell() - f5g

    # SessionUID (UID)
    f5h = buf.tell(); sizes.append(0)
    w("SessionUID"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
    f5i = buf.tell(); sizes.append(0)
    w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", int(session_uid or 0))).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f5i; sizes[-2] = buf.tell() - f5h

    # TournamentDecks (List -> empty)
    f5j = buf.tell(); sizes.append(0); tl = len(sizes)-1
    w("TournamentDecks"); sep(); w(str(tl)); sep(); w(str(ft("System.Collections.Generic.List`1#Game.Shared.Tournaments.TournamentDeckBitsWrapper"))); sep(); w("0"); sep()
    w("0"); sep()
    sizes[tl] = buf.tell() - f5j

    # MatchPreviousWinners (List<ulong> -> empty)
    f5k = buf.tell(); sizes.append(0); ml = len(sizes)-1
    w("MatchPreviousWinners"); sep(); w(str(ml)); sep(); w(str(ft("System.Collections.Generic.List`1#System.UInt64"))); sep(); w("0"); sep()
    w("0"); sep()
    sizes[ml] = buf.tell() - f5k

    # TournamentPlayerIDs (List<ulong>)
    f5l = buf.tell(); sizes.append(0); pl = len(sizes)-1
    w("TournamentPlayerIDs"); sep(); w(str(pl)); sep(); w(str(ft("System.Collections.Generic.List`1#System.UInt64"))); sep(); w("0"); sep()
    player_ids = [int(x) for x in (tournament_player_ids or [])]
    w(str(len(player_ids))); sep()
    for i, player_id in enumerate(player_ids):
        fe = buf.tell(); sizes.append(0)
        w(str(i)); sep(); w(str(len(sizes)-1)); sep()
        w(str(ft("System.UInt64"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<Q", player_id)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - fe
    sizes[pl] = buf.tell() - f5l

    # DeckHash (List<string> -> empty)
    f5m = buf.tell(); sizes.append(0); hl = len(sizes)-1
    w("DeckHash"); sep(); w(str(hl)); sep(); w(str(ft("System.Collections.Generic.List`1#System.String"))); sep(); w("0"); sep()
    w("0"); sep()
    sizes[hl] = buf.tell() - f5m

    # FirstPlayer (UID)
    f5n = buf.tell(); sizes.append(0)
    w("FirstPlayer"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
    f5o = buf.tell(); sizes.append(0)
    w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", int(first_player or 0))).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f5o; sizes[-2] = buf.tell() - f5n

    # ArenaInstance (ulong)
    f5p = buf.tell(); sizes.append(0)
    w("ArenaInstance"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f5p

    # ArenaOwner (ulong)
    f5q = buf.tell(); sizes.append(0)
    w("ArenaOwner"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f5q

    # ParticipatingPlayers (List -> empty)
    f5r = buf.tell(); sizes.append(0); pp = len(sizes)-1
    w("ParticipatingPlayers"); sep(); w(str(pp)); sep(); w(str(ft("System.Collections.Generic.List`1#Game.Shared.RemotePlayer"))); sep(); w("0"); sep()
    w("0"); sep()
    sizes[pp] = buf.tell() - f5r

    # TournamentID (ulong)
    f5s = buf.tell(); sizes.append(0)
    w("TournamentID"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", int(tournament_id or 0))).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f5s

    # AiDifficulty (enum EDifficulty) — 1 prop with value__ int32
    f5t = buf.tell(); sizes.append(0)
    w("AiDifficulty"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.AI.EDifficulty"))); sep(); w("1"); sep()
    f5t2 = buf.tell(); sizes.append(0)
    w("value__"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f5t2; sizes[-2] = buf.tell() - f5t

    # TestDeckID (ulong)
    f5u = buf.tell(); sizes.append(0)
    w("TestDeckID"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f5u

    sizes[ed_idx] = buf.tell() - f5

    # JoinInsteadOfReconnect (bool)
    f6 = buf.tell(); sizes.append(0)
    w("JoinInsteadOfReconnect"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
    w("0")
    sizes[-1] = buf.tell() - f6

    sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, s in enumerate(sizes):
        if i > 0: sep()
        w(str(s))
    return buf.getvalue()


def encode_sync_event(packet) -> bytes:  # packet: game_engine.NetworkPacketSessionEventArgs
    """Encode a SessionSyncEventEventArgs wrapping a NetworkPacketSessionEventArgs as ObjFmt.

    SessionSyncEventEventArgs:
      - RoutingPlayerId (UID)
      - SessionArgs (NetworkPacketSessionEventArgs)
        - PlayerId (UID)
        - EventIds (List`1#System.Int32)
        - EventData (List`1#System.Byte[])
    """
    sizes = []
    buf = io.BytesIO()
    def w(s): buf.write(s.encode("utf-8"))
    def sep(): buf.write(b";")

    type_names = [
        "Game.Shared.Network.GameSession.SessionSyncEventEventArgs",
        "Game.Shared.UID", "System.UInt64",
        "Game.Shared.NetworkPacketSessionEventArgs",
        "System.Int32",
        "System.Collections.Generic.List`1#System.Int32",
        "System.Collections.Generic.List`1#System.Byte[]",
        "System.Byte[]",
    ]
    def ft(tn):
        if tn not in type_names:
            type_names.append(tn)
        return type_names.index(tn)

    sizes.append(0)

    # Root: ;0;0;2; (2 fields: RoutingPlayerId, SessionArgs)
    w(""); sep(); w("0"); sep(); w("0"); sep(); w("2"); sep()

    # RoutingPlayerId (UID)
    f1 = buf.tell(); sizes.append(0)
    w("RoutingPlayerId"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
    f1a = buf.tell(); sizes.append(0)
    w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", packet.player_id.to_uint64())).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f1a
    sizes[-2] = buf.tell() - f1

    # SessionArgs (NetworkPacketSessionEventArgs) — 5 fields alphabetically
    f2 = buf.tell(); f2_sz_idx = len(sizes); sizes.append(0)
    w("SessionArgs"); sep(); w(str(f2_sz_idx)); sep(); w(str(ft("Game.Shared.NetworkPacketSessionEventArgs"))); sep(); w("5"); sep()

    # -- Class (int)
    f2c = buf.tell(); sizes.append(0)
    w("Class"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", 255)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f2c

    # -- EventData (List<byte[]>)
    f2d = buf.tell(); f2d_sz_idx = len(sizes); sizes.append(0)
    w("EventData"); sep(); w(str(f2d_sz_idx)); sep(); w(str(ft("System.Collections.Generic.List`1#System.Byte[]"))); sep(); w("0"); sep()
    w(str(len(packet.event_data))); sep()
    for idx, edata in enumerate(packet.event_data):
        se = buf.tell(); sizes.append(0)
        w(str(idx)); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Byte[]"))); sep(); w("0"); sep()
        buf.write(struct.pack("!I", len(edata)))
        buf.write(edata)
        sizes[-1] = buf.tell() - se
    sizes[f2d_sz_idx] = buf.tell() - f2d

    # -- EventIds (List<int>)
    f2e = buf.tell(); f2e_sz_idx = len(sizes); sizes.append(0)
    w("EventIds"); sep(); w(str(f2e_sz_idx)); sep(); w(str(ft("System.Collections.Generic.List`1#System.Int32"))); sep(); w("0"); sep()
    w(str(len(packet.event_ids))); sep()
    for idx, eid in enumerate(packet.event_ids):
        se = buf.tell(); sizes.append(0)
        w(str(idx)); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<i", eid)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - se
    sizes[f2e_sz_idx] = buf.tell() - f2e

    # -- PlayerId (UID)
    f2p = buf.tell(); sizes.append(0)
    w("PlayerId"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
    f2p1 = buf.tell(); sizes.append(0)
    w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", packet.player_id.to_uint64())).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f2p1
    sizes[-2] = buf.tell() - f2p

    # -- SessionId (UID)
    f2s = buf.tell(); sizes.append(0)
    w("SessionId"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
    f2s1 = buf.tell(); sizes.append(0)
    w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<Q", packet.session_id.to_uint64())).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f2s1
    sizes[-2] = buf.tell() - f2s

    sizes[f2_sz_idx] = buf.tell() - f2  # SessionArgs size

    sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, s in enumerate(sizes):
        if i > 0: sep()
        w(str(s))
    return buf.getvalue()


def encode_challenger_list(challengers):
    """Encode a List<ArenaChallenger> as ObjFmt raw bytes."""
    sizes = []
    buf = io.BytesIO()
    def w(s): buf.write(s.encode("utf-8"))
    def sep(): buf.write(b";")

    type_names = [
        "Reckoning.Campaign.Messages.Arena.ArenaChallenger",
        "System.UInt64", "Game.Shared.ResourceId",
        "System.Guid", "System.String",
        "System.Collections.Generic.List`1#Game.Shared.ResourceId",
    ]
    def ft(tn):
        if tn not in type_names:
            type_names.append(tn)
        return type_names.index(tn)

    sizes.append(0)
    w(""); sep(); w("0"); sep(); w("0"); sep(); w(str(len(challengers))); sep()

    for idx, c in enumerate(challengers):
        felem = buf.tell()
        elem_slot = len(sizes)
        sizes.append(0)
        w(str(idx)); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[0]))); sep(); w("5"); sep()

        # ChallengerID (ulong)
        f1 = buf.tell(); sizes.append(0)
        w("ChallengerID"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<Q", c["id"])).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f1

        # EncounterDeck (ResourceId)
        f2 = buf.tell(); sizes.append(0)
        w("EncounterDeck"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
        f2a = buf.tell(); sizes.append(0)
        w("guid"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
        guid_bytes = c["deck"].encode("utf-8")
        w(str(len(guid_bytes))); sep(); buf.write(guid_bytes)
        sizes[-1] = buf.tell() - f2a
        sizes[-2] = buf.tell() - f2

        # ChallengerName (string)
        f3 = buf.tell(); sizes.append(0)
        w("ChallengerName"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
        name_bytes = c["name"].encode("utf-8")
        w(str(len(name_bytes))); sep(); buf.write(name_bytes)
        sizes[-1] = buf.tell() - f3

        # IsBoss (string)
        f4 = buf.tell(); sizes.append(0)
        w("IsBoss"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
        boss_bytes = c["boss"].encode("utf-8")
        w(str(len(boss_bytes))); sep(); buf.write(boss_bytes)
        sizes[-1] = buf.tell() - f4

        # Equipment (empty list)
        f5 = buf.tell(); sizes.append(0)
        w("Equipment"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Collections.Generic.List`1#Game.Shared.ResourceId"))); sep(); w("0"); sep()
        w("0"); sep()
        sizes[-1] = buf.tell() - f5

        sizes[elem_slot] = buf.tell() - felem

    sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, s in enumerate(sizes):
        if i > 0: sep()
        w(str(s))
    return buf.getvalue()


def encode_get_challengers_response(success, challengers):
    """Encode GetMasterListOfChallengersResponse with inline challenger list."""
    sizes = []
    buf = io.BytesIO()
    def w(s): buf.write(s.encode("utf-8"))
    def sep(): buf.write(b";")

    type_names = [
        "Game.Client.Network.Campaign.GetMasterListOfChallengersResponse",
        "System.Boolean",
        "Reckoning.Campaign.Messages.Arena.ArenaChallenger",
        "System.UInt64", "Game.Shared.ResourceId",
        "System.Guid", "System.String",
        "System.Collections.Generic.List`1#Game.Shared.ResourceId",
        "Game.Shared.Network.Campaign.EGetMasterListOfChallengersError",
        "System.Int32",
    ]
    def ft(tn):
        if tn not in type_names:
            type_names.append(tn)
        return type_names.index(tn)

    sizes.append(0)
    w(""); sep(); w("0"); sep(); w("0"); sep(); w("4"); sep()

    # Challengers — inline list (FIRST - alphabetical order before Success)
    f_ch = buf.tell(); sizes.append(0)
    w("Challengers"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Collections.Generic.List`1#Reckoning.Campaign.Messages.Arena.ArenaChallenger"))); sep(); w("0"); sep()
    w(str(len(challengers))); sep()
    for idx, c in enumerate(challengers):
        felem = buf.tell()
        se = len(sizes); sizes.append(0)
        w(str(idx)); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Reckoning.Campaign.Messages.Arena.ArenaChallenger"))); sep(); w("5"); sep()

        # ChallengerID
        f1 = buf.tell(); sizes.append(0)
        w("ChallengerID"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<Q", c["id"])).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f1

        # EncounterDeck (ResourceId)
        f2 = buf.tell(); sizes.append(0)
        w("EncounterDeck"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
        f2a = buf.tell(); sizes.append(0)
        w("m_Guid"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
        gb = c["deck"].encode("utf-8")
        w(str(len(gb))); sep(); buf.write(gb)
        sizes[-1] = buf.tell() - f2a; sizes[-2] = buf.tell() - f2

        # ChallengerName (string)
        f3 = buf.tell(); sizes.append(0)
        w("ChallengerName"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
        en = c["name"].encode("utf-8")
        w(str(len(en))); sep(); buf.write(en)
        sizes[-1] = buf.tell() - f3

        # IsBoss (string "True"/"False")
        f4 = buf.tell(); sizes.append(0)
        w("IsBoss"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
        eb = c["boss"].encode("utf-8")
        w(str(len(eb))); sep(); buf.write(eb)
        sizes[-1] = buf.tell() - f4

        # Equipment (empty)
        f5 = buf.tell(); sizes.append(0)
        w("Equipment"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Collections.Generic.List`1#Game.Shared.ResourceId"))); sep(); w("0"); sep()
        w("0"); sep()
        sizes[-1] = buf.tell() - f5

        sizes[se] = buf.tell() - felem

    sizes[-1] = buf.tell() - f_ch  # Challengers field size

    # Success (bool) - SECOND alphabetically
    f_succ = buf.tell(); sizes.append(0)
    w("Success"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
    w("1" if success else "0")
    sizes[-1] = buf.tell() - f_succ

    # Error (enum)
    f_err = buf.tell(); sizes.append(0)
    w("Error"); sep(); w(str(len(sizes)-1)); sep()
    w(str(ft("Game.Shared.Network.Campaign.EGetMasterListOfChallengersError"))); sep(); w("1"); sep()
    f_ev = buf.tell(); sizes.append(0)
    w("value__"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<i", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f_ev; sizes[-2] = buf.tell() - f_err

    # ErrorMessage (string)
    f_em = buf.tell(); sizes.append(0)
    w("ErrorMessage"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
    w("0"); sep()
    sizes[-1] = buf.tell() - f_em

    sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, s in enumerate(sizes):
        if i > 0: sep()
        w(str(s))
    return buf.getvalue()


def encode_login_stream_done():
    """Encode ProfileGenericUpdate wrapping ProfileGenericLoginStreamDone."""
    inner_done = encode_objfmt_response(
        ["Game.Shared.ProfileGenericLoginStreamDone"],
        []
    )
    type_names = [
        "Game.Shared.Network.Profile.ProfileGenericUpdateEventArgs",
        "Game.Shared.ProfileGenericMessage",
        "System.Byte[]",
    ]
    sizes = []
    buf = io.BytesIO()
    def w(s): buf.write(s.encode("utf-8"))
    def sep(): buf.write(b";")
    def lf(): buf.write(b"\n")
    def find_type(tname):
        if tname not in type_names:
            type_names.append(tname)
        return type_names.index(tname)

    sizes.append(0)
    # Root: ;0;0;1;
    w(""); sep(); w("0"); sep(); w(str(find_type(type_names[0]))); sep(); w("1"); sep()

    f_msg = buf.tell()
    sizes.append(0)
    # Message;1;1;1;
    w("Message"); sep(); w("1"); sep(); w(str(find_type(type_names[1]))); sep(); w("1"); sep()

    f_data = buf.tell()
    sizes.append(0)
    # Data;2;2;0;
    w("Data"); sep(); w("2"); sep(); w(str(find_type(type_names[2]))); sep(); w("0"); sep()
    # <4-byte len><inner_done>
    buf.write(struct.pack("!I", len(inner_done)))
    buf.write(inner_done)

    sizes[2] = buf.tell() - f_data
    sizes[1] = buf.tell() - f_msg
    sizes[0] = buf.tell()

    w(";".join(type_names))
    lf()
    for i, s in enumerate(sizes):
        if i > 0:
            w(";")
        w(str(s))
    return buf.getvalue()


def encode_datawrapper(request_id, data_type, body_bytes, comp,
                       session_guid="00000000-0000-0000-0000-000000000000",
                       conh=0):
    """Encode a DataWrapper with the given fields."""
    fields = [
        ("RequestId", "long", request_id),
        ("DataType", "int", data_type),
        ("Bytes", "bytes", body_bytes),
        ("RequestHandlerSessionId", "guid", session_guid),
        ("Comp", "byte", comp),
    ]
    return encode_objfmt_response(
        ["Game.Shared.Network.DataWrapper",
         "System.Int64", "System.Int32", "System.Byte[]",
         "System.Guid", "System.Byte"],
        fields
    )


def encode_get_unread_mail_count_response(unread_count=0):
    """Encode GetUnreadMailCount.Response as ObjFmt bytes."""
    return encode_objfmt_response(
        ["Game.Shared.Mail.Messages.Mail+GetUnreadMailCount+Response",
         "System.Int32"],
        [("UnreadMailCount", "int", unread_count)]
    )


def encode_ping_mail_server_response(timestamp=None):
    """Encode PingMailServerResponse as ObjFmt bytes."""
    if timestamp is None:
        timestamp = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
    return encode_objfmt_response(
        ["Game.Client.Network.Mail.PingMailServerResponse",
         "System.DateTime",
         "Game.Shared.Network.Mail.EPingMailServerError",
         "System.String"],
        [
            ("Timestamp", "datetime", timestamp),
            ("Error", "enum", ("Game.Shared.Network.Mail.EPingMailServerError", "Ok")),
            ("ErrorMessage", "string", ""),
        ]
    )


def encode_profile_response(envelope_bytes):
    return encode_objfmt_response(
        ["Game.Shared.Profile.Network+Response",
         "System.Byte[]"],
        [("Envelope", "bytes", envelope_bytes)]
    )


# =============================================================================
#  Store response encoder (pure — caller provides items list)
# =============================================================================

def encode_store_response(items):
    """Build ObjFmt for GetStoreItemsResponseArgs with a list of StoreItems."""
    type_names = [
        "Game.Shared.Network.Escrow.GetStoreItemsResponseArgs",
        "System.Collections.Generic.List`1#Game.Shared.StoreItem",
        "Game.Shared.StoreItem",
        "System.Byte[]",
        "System.UInt64",
        "Game.Shared.ResourceId",
        "System.Guid",
        "System.Int32",
        "System.String",
        "System.Boolean",
    ]

    def ft(tn):
        if tn not in type_names:
            type_names.append(tn)
        return type_names.index(tn)

    sizes = []
    buf = io.BytesIO()
    def w(s): buf.write(s.encode("utf-8"))
    def sep(): buf.write(b";")
    def lf(): buf.write(b"\n")

    sizes.append(0)
    w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("1"); sep()

    f_col = buf.tell(); sizes.append(0)
    w("StoreItems"); sep(); w("1"); sep(); w(str(ft(type_names[1]))); sep(); w("0"); sep()
    w(str(len(items))); sep()

    for i, it in enumerate(items):
        f_el = buf.tell(); sizes.append(0)
        w(str(i)); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[2]))); sep(); w("19"); sep()

        # rawData JSON
        is_deck = it["t"] == "collectordeck"
        is_fullset = "Full Set" in it["n"]
        is_non_set1_booster = it["t"] == "ShopBoosterTab" and it["template_guid"] != "a8b78207-686a-4994-b6cd-4548d1349841"
        purchase_limit = 1 if (is_deck or is_fullset) else -1
        hide_in_store = is_non_set1_booster

        raw_json = json.dumps({
            "Price": it["price"], "ItemType": "Inventory", "CurrencyType": it["currency"],
            "ItemTemplateId": "{" + it["template_guid"].upper() + "}",
            "Name": it["n"], "StoreTab": it["t"],
        })
        raw_bytes = raw_json.encode("utf-8")
        guid_lower = it["template_guid"]

        # rawData
        f = buf.tell(); sizes.append(0)
        w("rawData"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Byte[]"))); sep(); w("0"); sep()
        buf.write(struct.pack("!I", len(raw_bytes))); buf.write(raw_bytes)
        sizes[-1] = buf.tell() - f

        # id (ulong)
        f = buf.tell(); sizes.append(0)
        w("id"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<Q", i + 1)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f

        # TemplateId (ResourceId with guid)
        f = buf.tell(); sizes.append(0)
        tidx = len(sizes) - 1
        w("TemplateId"); sep(); w(str(tidx)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
        gs = buf.tell(); sizes.append(0); gidx = len(sizes) - 1
        w("guid"); sep(); w(str(gidx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
        w("36"); sep(); buf.write(guid_lower.encode())
        sizes[gidx] = buf.tell() - gs
        sizes[tidx] = buf.tell() - f

        # Price
        f = buf.tell(); sizes.append(0)
        w("Price"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<i", it["price"])).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f

        store_fields = [
            ("ItemType", "string", "Inventory"),
            ("CurrencyType", "string", it["currency"]),
            ("Name", "string", it["n"]),
            ("HideInStore", "bool", hide_in_store),
            ("New", "bool", False),
            ("NoTrade", "bool", False),
            ("OnSale", "bool", False),
            ("PurchaseLimit", "int", purchase_limit),
            ("SoftPurchaseLimit", "int", -1),
            ("LocationIndex", "int", -1),
            ("ImageUrl", "string", ""),
            ("Description", "string", ""),
             ("ShortDesc", "string", it["s"]),
             ("LongDesc", "string", ""),
             ("StoreTab", "string", it["t"]),
        ]
        for fname, ftype, fval in store_fields:
            f = buf.tell(); sizes.append(0)
            tn_map = {"string": "System.String", "bool": "System.Boolean", "int": "System.Int32"}
            w(fname); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(tn_map[ftype]))); sep(); w("0"); sep()
            if ftype == "string":
                enc = str(fval).encode("utf-8")
                w(str(len(enc))); sep(); buf.write(enc)
            elif ftype == "bool":
                w("1" if fval else "0")
            elif ftype == "int":
                w(hexlify(struct.pack("<i", fval)).decode("ascii")); sep()
            sizes[-1] = buf.tell() - f

        sizes[2 + i * 21] = buf.tell() - f_el  # 21 entries per element

    sizes[1] = buf.tell() - f_col
    sizes[0] = buf.tell()

    w(";".join(type_names))
    lf()
    for j, s in enumerate(sizes):
        if j > 0: w(";")
        w(str(s))

    return buf.getvalue()
