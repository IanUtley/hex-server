"""ObjFmt DataWrapper decoding kept separate from the HConnect server."""

import struct
from binascii import unhexlify

def log(_message):
    return None

def parse_datawrapper(body, *, preserve_complex=False):
    """
    Parse an ObjFmt-encoded DataWrapper.
    Returns dict with: request_id, data_type, raw_bytes, session_guid, comp, conh
    """
    sizes = []
    pos = len(body) - 1
    while pos >= 0:
        if body[pos] == 0x0a:
            size_part = body[pos+1:].decode("utf-8")
            sizes = [int(s) for s in size_part.split(";")]
            break
        pos -= 1
    if not sizes:
        raise ValueError("No size table found")

    type_table_start = sizes[0]
    type_end = body.index(0x0a, type_table_start)
    type_part = body[type_table_start:type_end].decode("utf-8")
    type_names = type_part.split(";")
    root_type = type_names[0] if type_names else "?"

    buf = memoryview(body)
    idx = 0

    def read_to_sep():
        nonlocal idx
        start = idx
        while idx < len(body) and body[idx] != 0x3b:
            idx += 1
        result = body[start:idx].decode("utf-8")
        if idx < len(body):
            idx += 1
        return result

    def parse_one(name_hint, f_num):
        nonlocal idx
        name = read_to_sep()
        f_size_idx = int(read_to_sep())
        f_type_idx = int(read_to_sep())
        num = int(read_to_sep())
        f_type = type_names[f_type_idx] if f_type_idx < len(type_names) else "?"

        if f_type == "System.Int64" and num == 0:
            return name, struct.unpack("<q", unhexlify(read_to_sep()))[0]
        elif f_type == "System.UInt64" and num == 0:
            return name, struct.unpack("<Q", unhexlify(read_to_sep()))[0]
        elif f_type == "System.Int32" and num == 0:
            return name, struct.unpack("<i", unhexlify(read_to_sep()))[0]
        elif f_type == "System.Byte" and num == 0:
            return name, int(read_to_sep(), 16)
        elif f_type == "System.Byte[]" and num == 0:
            raw_len = struct.unpack("!I", body[idx:idx+4])[0]
            idx += 4
            val = body[idx:idx+raw_len]
            idx += raw_len
            return name, val
        elif f_type == "System.Guid" and num == 0:
            guid_len = int(read_to_sep())
            val = body[idx:idx+guid_len].decode("utf-8")
            idx += guid_len
            return name, val
        elif f_type == "System.String" and num == 0:
            str_len = int(read_to_sep())
            val = body[idx:idx+str_len].decode("utf-8")
            idx += str_len
            return name, val
        elif f_type == "System.Boolean" and num == 0:
            val = (body[idx] == 0x31)
            idx += 1
            return name, val
        elif ("ResourceId" in f_type or "UID" in f_type or
              "SessionCardId" in f_type or
              "AbilityActivationData" in f_type):
            # Nested identifiers are normally skipped for legacy handlers,
            # but the rules-port ingress needs their typed fields (SourceCardId,
            # AbilityTemplateId, and activation targets) preserved.
            if not preserve_complex:
                for _ in range(num):
                    parse_one("", 0)
                return name, {"__skipped__": f_type}
            sub = {}
            for _ in range(num):
                sn, sv = parse_one(name, 0)
                sub[sn] = sv
            return name, sub
        elif f_type.startswith("System.Collections.Generic.Dictionary`2#") and num == 0:
            # ObjFmt dictionaries are serialized as a collection of
            # KeyValuePair records.  TargetMap is one such dictionary; if we
            # leave its count unread, the next field is parsed as the literal
            # structural key name and the whole transaction becomes corrupt.
            count = int(read_to_sep())
            result_map = {}
            for index in range(count):
                _entry_name = read_to_sep()
                _entry_size = int(read_to_sep())
                _entry_type = int(read_to_sep())
                entry_props = int(read_to_sep())
                entry = {}
                for _ in range(entry_props):
                    sn, sv = parse_one("", 0)
                    entry[sn] = sv
                if preserve_complex:
                    key = entry.get("key", index)
                    result_map[key] = entry.get("value")
            return name, result_map if preserve_complex else {"__skipped__": f_type}
        elif f_type.startswith("System.Collections.Generic.List`1#") and num == 0:
            count = int(read_to_sep())
            elem_type = f_type.split("#", 1)[1] if "#" in f_type else ""
            vals = []
            for _ in range(count):
                ename = read_to_sep()  # element index (ignored)
                esize = int(read_to_sep())
                etype = int(read_to_sep())
                enum = int(read_to_sep())
                if elem_type == "System.UInt64":
                    v = struct.unpack("<Q", unhexlify(read_to_sep()))[0]
                    vals.append(v)
                elif elem_type == "System.Int32":
                    v = struct.unpack("<i", unhexlify(read_to_sep()))[0]
                    vals.append(v)
                elif elem_type == "System.String":
                    slen = int(read_to_sep())
                    v = body[idx:idx+slen].decode("utf-8")
                    idx += slen
                    vals.append(v)
                else:
                    # Complex list elements (notably
                    # AbilityActivationData) carry their own property count
                    # in ``enum`` followed by normal ObjFmt fields.  The old
                    # parser consumed one token here, leaving the cursor in
                    # the middle of the element and making the following
                    # transaction fields look like TargetMap keys.  Preserve
                    # the labelled fields when RulesPort requested complex
                    # decoding; otherwise consume the complete element.
                    element = {}
                    for _ in range(enum):
                        sn, sv = parse_one(ename, 0)
                        if preserve_complex:
                            element[sn] = sv
                    vals.append(element if preserve_complex else
                                {"__skipped__": elem_type})
            return name, vals
        elif "ResourceId" in f_type or "UID" in f_type:
            for _ in range(num):
                parse_one("", 0)
            return name, {"__skipped__": f_type}
        elif num > 0:
            sub = {}
            for _ in range(num):
                sn, sv = parse_one(name, 0)
                sub[sn] = sv
            return name, sub
        else:
            log(f"  Unhandled field {name}: type={f_type} num={num}")
            return name, f"<unhandled type={f_type} num={num}>"

    # Root field: name, size_ref, type_ref, num_props
    root_name = read_to_sep()
    size_idx = int(read_to_sep())
    type_idx = int(read_to_sep())
    num_props = int(read_to_sep())

    result = {"__type__": root_type}
    for _ in range(num_props):
        fn, fv = parse_one("", 0)
        result[fn] = fv

    return result


