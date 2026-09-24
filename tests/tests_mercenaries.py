"""Mercenary parties: flag/party encoding, persistence, and campaign party I/O."""

import json
import zlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
import campaign
import hconnect_server
from encoder import encode_chest_list
from services import mercenaries

SCABTONGUE = "48ad285c-7e8b-4e5e-ba65-a66fa73281de"
SET1 = "0382f729-7710-432b-b761-13677982dcd2"
FAILURES = []


def run(name, fn):
    try:
        fn()
        print(f"PASS {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"FAIL {name}: {e}")
    except Exception as e:
        import traceback
        FAILURES.append(name)
        print(f"ERROR {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


def _new_user(user_id):
    db._db.execute("INSERT INTO users (id, name) VALUES (?,?)",
                   (user_id, f"MercTester{user_id}"))
    db._db.commit()
    return user_id


def test_list_writer_matches_chest_encoder():
    chests = [(2, 0, SET1, 9005), (0, 0, SET1, 9006)]
    element = "Game.Shared.Domain.chest_bits"
    writer = mercenaries.ObjFmtListWriter(element, [
        element, "System.Int32", "System.Boolean", "System.UInt64",
        "System.UInt32", "Game.Shared.ResourceId", "System.Guid"])
    elements = [[("ChestRarity", "int", r), ("WOFSpinStatus", "int", s),
                 ("BoosterPackType", "rid", g), ("WasOpened", "bool", False),
                 ("InventoryId", "ulong", i), ("PromoID", "uint", 0),
                 ("TempateID", "rid", g), ("Vendor", "int", 0)]
                for r, s, g, i in chests]
    assert writer.encode(element, elements) == encode_chest_list(chests)


def test_flag_and_party_encoding():
    flags = mercenaries.encode_flag_list([("CAMP_PARTYCAP", 3, 4, False)])
    assert b"List`1#Reckoning.Profile.Messages.FlagData" in flags
    assert b"13;CAMP_PARTYCAP" in flags and b"Progress" in flags
    party = {"Id": 5, "ChampionID": 3084, "PlayerId": 77,
             "Members": [{"Mercenary": SCABTONGUE, "DeckTemplate": 9, "Upgrade": 1}]}
    encoded = mercenaries.encode_party_list([party])
    assert (b"List`1#Game.Shared.Campaign.Messages.CampSysGeneral+Party"
            b"+ChampionParty+ChampionPartyMember") in encoded
    assert SCABTONGUE.encode() in encoded


def test_party_json_accepts_resource_id_shapes():
    party = mercenaries.normalize_party({
        "ChampionID": "3084",
        "Members": [{"Mercenary": {"m_Guid": SCABTONGUE.upper()}, "DeckTemplate": 2},
                    {"Mercenary": SCABTONGUE, "Upgrade": "1"}]})
    assert [m["Mercenary"] for m in party["Members"]] == [SCABTONGUE, SCABTONGUE]
    assert party["ChampionID"] == 3084 and party["Members"][1]["Upgrade"] == 1


class _CampaignHandler:
    def __init__(self, user_id):
        self.user_profile = {"id": user_id}
        self.client_uid = 1
        self.scnt = 0
        self.sid = "test"
        self.sent = []

    def send(self, headers, body=b""):
        self.sent.append(body)

    def _log_req(self, *args):
        pass


def _unwrap(dw_bytes):
    """Return the decompressed payload of a sent DataWrapper."""
    start = dw_bytes.index(bytes([0x1F, 0x8B, 0x08]))
    return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(dw_bytes[start:])


def _envelope_json(dw_bytes):
    """Pull the JSON campaign envelope back out of a sent DataWrapper."""
    inner = _unwrap(dw_bytes)
    begin = min(i for i in (inner.find(b"{"), inner.find(b"[")) if i >= 0)
    return json.JSONDecoder().raw_decode(inner[begin:].decode("utf-8", "replace"))[0]


def _campaign(handler, request):
    envelope = json.dumps(request).encode()
    campaign.handle_campaign_request(
        handler, db._db, {"Envelope": envelope}, 1,
        "00000000-0000-0000-0000-000000000000", 2, "ServiceCampaign", "253",
        0, 1)
    return _envelope_json(handler.sent[-1])


def test_partysave_and_partyload_round_trip():
    user_id = _new_user(9401)
    handler = _CampaignHandler(user_id)
    saved = _campaign(handler, {
        "RequestType": "partysave",
        "Party": {"Id": 0, "ChampionID": 3084, "PlayerId": user_id,
                  "Members": [{"Mercenary": {"m_Guid": SCABTONGUE},
                               "DeckTemplate": 0, "Upgrade": 0}]}})
    assert saved["ChampionID"] == 3084 and saved["Id"] == 3084, saved
    assert saved["Members"][0]["Mercenary"] == SCABTONGUE
    loaded = _campaign(handler, {"RequestType": "partyload",
                                 "PlayerId": user_id, "Champs": [3084]})
    assert loaded == [saved], loaded
    assert mercenaries.get_parties(db._db, user_id) == [saved]


def test_login_push_sends_flags_and_parties():
    user_id = _new_user(9402)
    mercenaries.set_flag(db._db, user_id, mercenaries.PARTY_CAP_FLAG, 3, 4)
    assert mercenaries.get_flags(db._db, user_id) == [("CAMP_PARTYCAP", 3, 4, 0)]
    mercenaries.save_party(db._db, user_id, {"ChampionID": 12, "Members": []})
    pushed = []
    stream = hconnect_server.HCPHandler.__new__(hconnect_server.HCPHandler)
    stream.client_uid = 1
    stream.scnt = 0
    stream.sid = "test"
    stream.send = lambda headers, body=b"": pushed.append(_unwrap(body))
    stream._push_mercenary_stream({"id": user_id})
    assert len(pushed) == 2, len(pushed)
    assert b"FlagData" in pushed[0] and b"CAMP_PARTYCAP" in pushed[0]
    assert b"ChampionParty" in pushed[1]


if __name__ == "__main__":
    run("list writer matches chest encoder", test_list_writer_matches_chest_encoder)
    run("flag and party encoding", test_flag_and_party_encoding)
    run("party JSON accepts ResourceId shapes", test_party_json_accepts_resource_id_shapes)
    run("partysave/partyload round trip", test_partysave_and_partyload_round_trip)
    run("login push sends flags and parties", test_login_push_sends_flags_and_parties)
    if FAILURES:
        sys.exit(1)
