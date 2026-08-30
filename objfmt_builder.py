"""
ObjFmt Builder — eliminates the repetitive encoding boilerplate in hconnect_server.py.

Patterns eliminated:
  sizes = []; buf = io.BytesIO(); w = lambda s: buf.write(s.encode("utf-8"))
  sep = lambda: buf.write(b";"); lf = lambda: buf.write(b"\n")
  ft(tn): if tn not in type_names: type_names.append(tn); return type_names.index(tn)
  field header: w(name); sep(); w(str(idx)); sep(); w(str(ft(type))); sep(); w(str(numProps)); sep();
  size table: sizes[0] = buf.tell(); w(";".join(type_names)); lf(); for i,s in enumerate(sizes): ...

Usage:
    b = ObjFmtBuilder("My.Response.Type")
    b.field_int("GoldAward", 100)
    b.field_str("Name", "Hello")
    b.field_enum("Error", "My.ErrorEnum", 0)
    result = b.finish()
"""

import io
import struct
from binascii import hexlify


class ObjFmtBuilder:
    """Streaming ObjFmt encoder that tracks sizes and type names automatically."""

    def __init__(self, root_type):
        self._buf = io.BytesIO()
        self._sizes = []
        self._types = []
        # Write root header
        self._root_idx = self._add_type(root_type)
        self._w("")
        self._sep()
        self._w("0")
        self._sep()
        self._w(str(self._root_idx))
        self._sep()
        # numProps placeholder — caller sets via _set_root_props()
        self._root_props_pos = self._buf.tell()
        self._w("00")
        self._sep()  # overwritten later
        # Root size tracks from after header
        self._sizes.append(self._buf.tell())

    # ── Internal helpers ──────────────────────────────────────────
    def _w(self, s):
        self._buf.write(s.encode("utf-8"))

    def _sep(self):
        self._buf.write(b";")

    def _nl(self):
        self._buf.write(b"\n")

    def _add_type(self, tname):
        if tname not in self._types:
            self._types.append(tname)
        return self._types.index(tname)

    def _push_size(self):
        """Push a size placeholder, return its index in self._sizes."""
        idx = len(self._sizes)
        self._sizes.append(0)
        return idx

    def _set_size(self, idx, start_pos):
        self._sizes[idx] = self._buf.tell() - start_pos

    def _set_root_props(self, num_props):
        """Write the real root numProps over the placeholder."""
        pos = self._buf.tell()
        self._buf.seek(self._root_props_pos)
        val = str(num_props)
        if len(val) < 2:
            val = "0" + val
        self._w(val)
        self._sep()  # re-write the separator
        self._buf.seek(pos)

    # ── Simple field helpers ──────────────────────────────────────
    def field_int(self, name, val):
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type("System.Int32")
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        self._w(hexlify(struct.pack("<i", val)).decode("ascii")); self._sep()
        self._set_size(idx, start)

    def field_ulong(self, name, val):
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type("System.UInt64")
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        self._w(hexlify(struct.pack("<Q", val)).decode("ascii")); self._sep()
        self._set_size(idx, start)

    def field_uint(self, name, val):
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type("System.UInt32")
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        self._w(hexlify(struct.pack("<I", val)).decode("ascii")); self._sep()
        self._set_size(idx, start)

    def field_bool(self, name, val):
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type("System.Boolean")
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        self._w("1" if val else "0")
        self._set_size(idx, start)

    def field_str(self, name, val):
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type("System.String")
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        enc = val.encode("utf-8") if isinstance(val, str) else val
        self._w(str(len(enc))); self._sep(); self._buf.write(enc)
        self._set_size(idx, start)

    def field_datetime(self, name, val):
        """val: datetime string like '01/01/0001 00:00:00'"""
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type("System.DateTime")
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        enc = val.encode("utf-8")
        self._w(str(len(enc))); self._sep(); self._buf.write(enc)
        self._set_size(idx, start)

    def field_guid(self, name, val):
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type("System.Guid")
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        guid_str = val if isinstance(val, str) else str(val)
        self._w(str(len(guid_str))); self._sep(); self._buf.write(guid_str.encode("utf-8"))
        self._set_size(idx, start)

    def field_ulong_list(self, name, values):
        """Write a List<UInt64> field."""
        start = self._buf.tell()
        list_idx = self._push_size()
        t = self._add_type("System.Collections.Generic.List`1#System.UInt64")
        self._w(name); self._sep(); self._w(str(list_idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        self._w(str(len(values))); self._sep()
        for i, value in enumerate(values):
            self.add_list_item_uint64(i, int(value))
        self._set_size(list_idx, start)

    def field_resource_id_list(self, name, values):
        """Write a List<ResourceId> field."""
        start = self._buf.tell()
        list_idx = self._push_size()
        t = self._add_type("System.Collections.Generic.List`1#Game.Shared.ResourceId")
        self._w(name); self._sep(); self._w(str(list_idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        self._w(str(len(values))); self._sep()
        for i, value in enumerate(values):
            self.add_list_item_resource_id(i, value)
        self._set_size(list_idx, start)

    def field_bytes(self, name, data):
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type("System.Byte[]")
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        self._buf.write(struct.pack("!I", len(data)))
        self._buf.write(data)
        self._set_size(idx, start)

    def field_resource_id(self, name, guid_val):
        """ResourceId — 1 sub-prop: guid"""
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type("Game.Shared.ResourceId")
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("1"); self._sep()
        sub_start = self._buf.tell()
        sub_idx = self._push_size()
        gt = self._add_type("System.Guid")
        self._w("guid"); self._sep(); self._w(str(sub_idx)); self._sep()
        self._w(str(gt)); self._sep(); self._w("0"); self._sep()
        guid_str = guid_val if isinstance(guid_val, str) else str(guid_val)
        self._w(str(len(guid_str))); self._sep(); self._buf.write(guid_str.encode("utf-8"))
        self._set_size(sub_idx, sub_start)
        self._set_size(idx, start)

    def field_enum(self, name, enum_type_name, val):
        """Enum struct format with value__ sub-field."""
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type(enum_type_name)
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("1"); self._sep()
        sub_start = self._buf.tell()
        sub_idx = self._push_size()
        it = self._add_type("System.Int32")
        self._w("value__"); self._sep(); self._w(str(sub_idx)); self._sep()
        self._w(str(it)); self._sep(); self._w("0"); self._sep()
        self._w(hexlify(struct.pack("<i", val)).decode("ascii")); self._sep()
        self._set_size(sub_idx, sub_start)
        self._set_size(idx, start)

    def field_uid(self, name, uid64):
        """Write a UID field with m_UID64 sub-field."""
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type("Game.Shared.UID")
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("1"); self._sep()
        sub_start = self._buf.tell()
        sub_idx = self._push_size()
        ut = self._add_type("System.UInt64")
        self._w("m_UID64"); self._sep(); self._w(str(sub_idx)); self._sep()
        self._w(str(ut)); self._sep(); self._w("0"); self._sep()
        self._w(hexlify(struct.pack("<Q", uid64)).decode("ascii")); self._sep()
        self._set_size(sub_idx, sub_start)
        self._set_size(idx, start)

    # ── Collection / list helpers ─────────────────────────────────
    def begin_list(self, name, list_type_name, count):
        """Start a collection. Returns (list_idx, start)."""
        start = self._buf.tell()
        list_idx = self._push_size()
        t = self._add_type(list_type_name)
        self._w(name); self._sep(); self._w(str(list_idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        self._w(str(count)); self._sep()
        return list_idx, start

    def add_list_item_uint64(self, item_idx, val):
        """Add a UInt64 item to the currently open list."""
        start = self._buf.tell()
        el_idx = self._push_size()
        t = self._add_type("System.UInt64")
        self._w(str(item_idx)); self._sep(); self._w(str(el_idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        self._w(hexlify(struct.pack("<Q", val)).decode("ascii")); self._sep()
        self._set_size(el_idx, start)

    def add_list_item_resource_id(self, item_idx, guid_val):
        """Add a ResourceId item to the currently open list."""
        start = self._buf.tell()
        el_idx = self._push_size()
        t = self._add_type("Game.Shared.ResourceId")
        self._w(str(item_idx)); self._sep(); self._w(str(el_idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("1"); self._sep()
        sub_start = self._buf.tell()
        sub_idx = self._push_size()
        gt = self._add_type("System.Guid")
        self._w("guid"); self._sep(); self._w(str(sub_idx)); self._sep()
        self._w(str(gt)); self._sep(); self._w("0"); self._sep()
        guid_str = guid_val if isinstance(guid_val, str) else str(guid_val)
        self._w(str(len(guid_str))); self._sep(); self._buf.write(guid_str.encode("utf-8"))
        self._set_size(sub_idx, sub_start)
        self._set_size(el_idx, start)

    def add_dict_entry_gem(self, item_idx, card_instance_id, gem_type_val):
        """Add a Dictionary<UInt64, EGemTypesNew> entry to an open list.
        Each entry is a KeyValuePair with key (uint64) and value (enum with value__ uint64)."""
        import struct
        from binascii import hexlify
        start = self._buf.tell()
        el_idx = self._push_size()
        kvp_type = self._add_type("System.Collections.Generic.KeyValuePair`2#System.UInt64!Game.Shared.Mechanics.EGemTypesNew")
        self._w(str(item_idx)); self._sep(); self._w(str(el_idx)); self._sep()
        self._w(str(kvp_type)); self._sep(); self._w("2"); self._sep()
        # key sub-field
        k_start = self._buf.tell()
        k_idx = self._push_size()
        kt = self._add_type("System.UInt64")
        self._w("key"); self._sep(); self._w(str(k_idx)); self._sep()
        self._w(str(kt)); self._sep(); self._w("0"); self._sep()
        self._w(hexlify(struct.pack("<Q", card_instance_id)).decode("ascii")); self._sep()
        self._set_size(k_idx, k_start)
        # value sub-field (EGemTypesNew enum with value__ as UInt64)
        v_start = self._buf.tell()
        v_idx = self._push_size()
        vt = self._add_type("Game.Shared.Mechanics.EGemTypesNew")
        self._w("value"); self._sep(); self._w(str(v_idx)); self._sep()
        self._w(str(vt)); self._sep(); self._w("1"); self._sep()
        # value__ sub-sub-field (UInt64, matching client encoding)
        sv_start = self._buf.tell()
        sv_idx = self._push_size()
        svt = self._add_type("System.UInt64")
        self._w("value__"); self._sep(); self._w(str(sv_idx)); self._sep()
        self._w(str(svt)); self._sep(); self._w("0"); self._sep()
        self._w(hexlify(struct.pack("<Q", gem_type_val)).decode("ascii")); self._sep()
        self._set_size(sv_idx, sv_start)
        self._set_size(v_idx, v_start)
        self._set_size(el_idx, start)

    def add_list_item_str(self, item_idx, val):
        """Add a String item to the currently open list."""
        start = self._buf.tell()
        el_idx = self._push_size()
        t = self._add_type("System.String")
        self._w(str(item_idx)); self._sep(); self._w(str(el_idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        encoded = val.encode("utf-8")
        self._w(str(len(encoded))); self._sep(); self._buf.write(encoded)
        self._set_size(el_idx, start)

    def add_dict_entry_uint64_str(self, item_idx, key_val, value_val):
        """Add a Dictionary<UInt64, String> entry to an open list.
        Each entry is a KeyValuePair with key (uint64) and value (string)."""
        import struct
        from binascii import hexlify
        start = self._buf.tell()
        el_idx = self._push_size()
        kvp_type = self._add_type("System.Collections.Generic.KeyValuePair`2#System.UInt64!System.String")
        self._w(str(item_idx)); self._sep(); self._w(str(el_idx)); self._sep()
        self._w(str(kvp_type)); self._sep(); self._w("2"); self._sep()
        # key sub-field
        k_start = self._buf.tell()
        k_idx = self._push_size()
        kt = self._add_type("System.UInt64")
        self._w("key"); self._sep(); self._w(str(k_idx)); self._sep()
        self._w(str(kt)); self._sep(); self._w("0"); self._sep()
        self._w(hexlify(struct.pack("<Q", key_val)).decode("ascii")); self._sep()
        self._set_size(k_idx, k_start)
        # value sub-field
        v_start = self._buf.tell()
        v_idx = self._push_size()
        vt = self._add_type("System.String")
        self._w("value"); self._sep(); self._w(str(v_idx)); self._sep()
        self._w(str(vt)); self._sep(); self._w("0"); self._sep()
        encoded = value_val.encode("utf-8")
        self._w(str(len(encoded))); self._sep(); self._buf.write(encoded)
        self._set_size(v_idx, v_start)
        self._set_size(el_idx, start)

    def begin_element(self, elem_idx, elem_type_name, num_props):
        """Start a collection element. Returns element_idx."""
        start = self._buf.tell()
        el_idx = self._push_size()
        t = self._add_type(elem_type_name)
        self._w(str(elem_idx)); self._sep(); self._w(str(el_idx)); self._sep()
        self._w(str(t)); self._sep(); self._w(str(num_props)); self._sep()
        self._element_starts = getattr(self, '_element_starts', {})
        self._element_starts[el_idx] = start
        return el_idx

    # ── Card / chest / inventory field groups ─────────────────────
    CARD_PROPS = 6
    CHEST_PROPS = 8
    INVENTORY_PROPS = 6

    def card_fields(self, guid, card_id, is_ext=0):
        """Write 6 card_instance_bits fields inline."""
        self.field_ulong("Id", card_id)
        self.field_resource_id("TemplateID", guid)
        self.field_bool("IsFoil", False)
        self.field_bool("IsExtended", bool(is_ext))
        self.field_bool("IsNotTradeable", False)
        self.field_str("EscrowStatus", "Clean")

    def chest_fields(self, rarity_val, spin_status, pack_guid, inventory_id):
        """Write 8 chest_bits fields inline."""
        self.field_int("ChestRarity", rarity_val)
        self.field_int("WOFSpinStatus", spin_status)
        self.field_resource_id("BoosterPackType", pack_guid)
        self.field_bool("WasOpened", False)
        self.field_ulong("InventoryId", inventory_id)
        self.field_uint("PromoID", 0)
        self.field_resource_id("TempateID", pack_guid)
        self.field_int("Vendor", 0)

    def inventory_fields(self, template_guid, item_id, quantity=1, bound=True):
        """Write 6 inventory_bits fields inline."""
        self.field_ulong("Id", item_id)
        self.field_resource_id("TemplateID", template_guid)
        self.field_bool("BoundToProfile", bound)
        self.field_int("ItemQuantity", quantity)
        self.field_datetime("ClaimDate", "01/01/0001 00:00:00")
        self.field_str("EscrowStatus", "Clean")

    # ── List slots (SpinEntryColors/SpinEntrySymbols) ─────────────
    def int_list(self, name, values):
        """Write a List<int> field with the given values."""
        start = self._buf.tell()
        list_idx = self._push_size()
        t = self._add_type("System.Collections.Generic.List`1#System.Int32")
        self._w(name); self._sep(); self._w(str(list_idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        self._w(str(len(values))); self._sep()
        for i, v in enumerate(values):
            self.field_int(str(i), v)
        self._set_size(list_idx, start)

    def field_struct_raw(self, name, type_name, raw_bytes):
        """Write a struct field with raw pre-encoded ObjFmt bytes.
        
        The raw_bytes should be the inner content of the struct (after the
        root header), without the outer field header or size tracking.
        Encoded as: numProps=0, then raw bytes.
        """
        start = self._buf.tell()
        idx = self._push_size()
        t = self._add_type(type_name)
        self._w(name); self._sep(); self._w(str(idx)); self._sep()
        self._w(str(t)); self._sep(); self._w("0"); self._sep()
        if raw_bytes:
            self._buf.write(raw_bytes)
        self._set_size(idx, start)

    # ── Finish ────────────────────────────────────────────────────
    def finish(self, num_root_props=None):
        """Write size table, return encoded bytes."""
        if num_root_props is not None:
            self._set_root_props(num_root_props)
        self._sizes[0] = self._buf.tell()
        self._w(";".join(self._types))
        self._nl()
        for i, s in enumerate(self._sizes):
            if i > 0:
                self._w(";")
            self._w(str(s))
        return self._buf.getvalue()

    def getvalue(self):
        """Return bytes without writing size table (for embedded use)."""
        return self._buf.getvalue()


# ── Standalone helpers (delegate to builder) ──────────────────────
def encode_card_6fields(b, guid, card_id, is_ext=0):
    """Encode 6 card_instance_bits fields into an existing builder b."""
    b.card_fields(guid, card_id, is_ext)


def encode_chest_8fields(b, rarity_val, spin_status, pack_guid, inventory_id):
    """Encode 8 chest_bits fields into an existing builder b."""
    b.chest_fields(rarity_val, spin_status, pack_guid, inventory_id)


def encode_inventory_6fields(b, template_guid, item_id, quantity=1, bound=True):
    """Encode 6 inventory_bits fields into an existing builder b."""
    b.inventory_fields(template_guid, item_id, quantity, bound)
