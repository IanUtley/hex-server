"""Mercenary parties: profile flags, party persistence, and wire encoding.

Mercenaries join a PvE champion's party.  The client shows as many open
party slots as the ``CAMP_PARTYCAP`` profile flag's Progress (plus talent
bonuses), up to three (four for Humans).  The retail servers raised the cap
when the AZ2 mercenary recruitment encounters were won.

Profile data reaches the client as standalone ObjFmt objects in the login
profile stream (``PlayerProfile.HandleProfileStream``): ``List<FlagData>``
and ``List<CampSysGeneral+Party+ChampionParty>``.  The campaign service
answers ``partysave`` with the saved ChampionParty as JSON.
"""

import io
import json
import struct
from binascii import hexlify

PARTY_CAP_FLAG = "CAMP_PARTYCAP"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
FLAG_DATA_TYPE = "Reckoning.Profile.Messages.FlagData"
PARTY_TYPE = "Game.Shared.Campaign.Messages.CampSysGeneral+Party+ChampionParty"
PARTY_MEMBER_TYPE = PARTY_TYPE + "+ChampionPartyMember"


def list_type(element_type):
    return f"System.Collections.Generic.List`1#{element_type}"


def max_party_slots(race):
    """Client UIPartyMercenarySelector.GetMaxPartyForRace (Human=1 -> 4)."""
    return 4 if int(race or 0) == 1 else 3


# ── ObjFmt list encoding ─────────────────────────────────────────────

class ObjFmtListWriter:
    """Encode a standalone ``List<T>`` exactly like ``encode_chest_list``.

    Fields are written as ``(name, kind, value)`` tuples where kind is one of
    ``int``, ``uint``, ``ulong``, ``bool``, ``string``, ``rid`` (ResourceId),
    or ``list`` with value ``(element_type, [fields, ...])``.
    """

    PRIMITIVES = {"int": "System.Int32", "uint": "System.UInt32",
                  "ulong": "System.UInt64", "bool": "System.Boolean",
                  "string": "System.String"}

    def __init__(self, element_type, type_names=None):
        self.types = [list_type(element_type)] + list(type_names or [])
        self.sizes = []
        self.buf = io.BytesIO()

    def _w(self, text):
        self.buf.write(text.encode("utf-8"))

    def _sep(self):
        self.buf.write(b";")

    def _ft(self, name):
        if name not in self.types:
            self.types.append(name)
        return self.types.index(name)

    def _field(self, name, kind, value):
        start = self.buf.tell()
        self.sizes.append(0)
        idx = len(self.sizes) - 1
        if kind == "rid":
            self._w(name); self._sep(); self._w(str(idx)); self._sep()
            self._w(str(self._ft("Game.Shared.ResourceId"))); self._sep(); self._w("1"); self._sep()
            g_start = self.buf.tell()
            self.sizes.append(0)
            g_idx = len(self.sizes) - 1
            self._w("guid"); self._sep(); self._w(str(g_idx)); self._sep()
            self._w(str(self._ft("System.Guid"))); self._sep(); self._w("0"); self._sep()
            guid = str(value or ZERO_GUID).encode()
            self._w(str(len(guid))); self._sep(); self.buf.write(guid)
            self.sizes[g_idx] = self.buf.tell() - g_start
        elif kind == "list":
            element_type, elements = value
            self._w(name); self._sep(); self._w(str(idx)); self._sep()
            self._w(str(self._ft(list_type(element_type)))); self._sep(); self._w("0"); self._sep()
            self._elements(element_type, elements)
        else:
            self._w(name); self._sep(); self._w(str(idx)); self._sep()
            self._w(str(self._ft(self.PRIMITIVES[kind]))); self._sep(); self._w("0"); self._sep()
            if kind == "bool":
                self._w("1" if value else "0")
            elif kind == "string":
                data = str(value or "").encode("utf-8")
                self._w(str(len(data))); self._sep(); self.buf.write(data)
            else:
                fmt = {"int": "<i", "uint": "<I", "ulong": "<Q"}[kind]
                self._w(hexlify(struct.pack(fmt, int(value or 0))).decode("ascii")); self._sep()
        self.sizes[idx] = self.buf.tell() - start

    def _elements(self, element_type, elements):
        self._w(str(len(elements))); self._sep()
        for i, fields in enumerate(elements):
            start = self.buf.tell()
            self.sizes.append(0)
            idx = len(self.sizes) - 1
            self._w(str(i)); self._sep(); self._w(str(idx)); self._sep()
            self._w(str(self._ft(element_type))); self._sep(); self._w(str(len(fields))); self._sep()
            for field in fields:
                self._field(*field)
            self.sizes[idx] = self.buf.tell() - start

    def encode(self, element_type, elements):
        self.sizes.append(0)
        self._w(""); self._sep(); self._w("0"); self._sep()
        self._w(str(self._ft(self.types[0]))); self._sep(); self._w("0"); self._sep()
        start = self.buf.tell()
        self.sizes.append(0)
        self._elements(element_type, elements)
        self.sizes[1] = self.buf.tell() - start
        self.sizes[0] = self.buf.tell()
        self._w(";".join(self.types)); self.buf.write(b"\n")
        self._w(";".join(str(size) for size in self.sizes))
        return self.buf.getvalue()


def encode_flag_list(flags):
    """flags: iterable of (name, progress, maximum, completed)."""
    elements = [[("Name", "string", name), ("Progress", "int", progress),
                 ("Maximum", "int", maximum), ("Completed", "bool", completed)]
                for name, progress, maximum, completed in flags]
    return ObjFmtListWriter(FLAG_DATA_TYPE).encode(FLAG_DATA_TYPE, elements)


def encode_party_list(parties):
    """parties: iterable of ChampionParty dicts (see ``normalize_party``)."""
    elements = []
    for party in parties:
        members = [[("Mercenary", "rid", member["Mercenary"]),
                    ("DeckTemplate", "ulong", member["DeckTemplate"]),
                    ("Upgrade", "int", member["Upgrade"])]
                   for member in party["Members"]]
        elements.append([("Id", "ulong", party["Id"]),
                         ("ChampionID", "ulong", party["ChampionID"]),
                         ("PlayerId", "ulong", party["PlayerId"]),
                         ("Members", "list", (PARTY_MEMBER_TYPE, members))])
    return ObjFmtListWriter(PARTY_TYPE).encode(PARTY_TYPE, elements)


# ── Party JSON ────────────────────────────────────────────────────────

def _guid(value):
    """Accept a ResourceId as a GUID string or {"m_Guid"/"guid": ...}."""
    if isinstance(value, dict):
        for key in ("m_Guid", "guid", "Guid", "Id"):
            if key in value:
                return _guid(value[key])
        return ZERO_GUID
    return str(value or ZERO_GUID).lower()


def normalize_party(party):
    """Return a ChampionParty dict with plain types for storage/encoding."""
    party = party or {}
    members = []
    for member in party.get("Members") or []:
        members.append({
            "Mercenary": _guid(member.get("Mercenary")),
            "DeckTemplate": int(member.get("DeckTemplate") or 0),
            "Upgrade": int(member.get("Upgrade") or 0),
        })
    return {"Id": int(party.get("Id") or 0),
            "ChampionID": int(party.get("ChampionID") or 0),
            "PlayerId": int(party.get("PlayerId") or 0),
            "Members": members}


# ── Persistence ───────────────────────────────────────────────────────

def get_flags(db, user_id):
    return [tuple(row) for row in db.execute(
        "SELECT name, progress, maximum, completed FROM profile_flags "
        "WHERE user_id=? ORDER BY name", (user_id,)).fetchall()]


def set_flag(db, user_id, name, progress, maximum=0, completed=False):
    db.execute(
        "INSERT INTO profile_flags (user_id, name, progress, maximum, completed) "
        "VALUES (?,?,?,?,?) ON CONFLICT(user_id, name) DO UPDATE SET "
        "progress=excluded.progress, maximum=excluded.maximum, "
        "completed=excluded.completed",
        (user_id, name, int(progress), int(maximum), 1 if completed else 0))


def get_parties(db, user_id):
    parties = []
    for (party_json,) in db.execute(
            "SELECT party_json FROM champion_parties WHERE user_id=? "
            "ORDER BY champion_id", (user_id,)).fetchall():
        try:
            parties.append(normalize_party(json.loads(party_json)))
        except (TypeError, ValueError):
            continue
    return parties


def save_party(db, user_id, party):
    """Store a party for the player and return the normalized copy."""
    party = normalize_party(party)
    party["PlayerId"] = party["PlayerId"] or int(user_id)
    if not party["Id"]:
        party["Id"] = party["ChampionID"]
    db.execute(
        "INSERT INTO champion_parties (champion_id, user_id, party_json) "
        "VALUES (?,?,?) ON CONFLICT(champion_id) DO UPDATE SET "
        "user_id=excluded.user_id, party_json=excluded.party_json",
        (party["ChampionID"], user_id, json.dumps(party)))
    return party
