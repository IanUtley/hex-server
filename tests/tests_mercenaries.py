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


def _deck_template_bytes(name, cards=()):
    import io
    import encoded_decks
    buf = io.BytesIO()
    encoded_decks.encode_profile_deck_template(
        buf, name, SCABTONGUE, mercenaries.ZERO_GUID,
        [(guid, count, False, False, False) for guid, count in cards])
    return buf.getvalue()


def test_template_name_and_storage():
    from services import deck_templates
    user_id = _new_user(9403)
    data = _deck_template_bytes("Shin'hare Assault")
    assert deck_templates.template_name(data) == "Shin'hare Assault"
    tid, name = deck_templates.save_template(db._db, user_id, 0, data)
    assert name == "Shin'hare Assault" and tid > 0
    renamed = _deck_template_bytes("Renamed")
    assert deck_templates.save_template(db._db, user_id, tid, renamed) == (tid, "Renamed")
    assert deck_templates.list_templates(db._db, user_id) == [(tid, "Renamed", renamed)]
    # Another player's id is never updated in place.
    other = _new_user(9404)
    new_id, _ = deck_templates.save_template(db._db, other, tid, data)
    assert new_id != tid


def test_pdecktsave_returns_objfmt_template_and_login_lists_it():
    import base64
    from services import deck_templates
    user_id = _new_user(9405)
    data = _deck_template_bytes("Merc Deck", [(SCABTONGUE, 2)])
    handler = _CampaignHandler(user_id)
    handler.authenticated = True
    request = json.dumps({"action": "pdecktsave", "DeckTemplateID": 0,
                          "Template": base64.b64encode(data).decode()}).encode()
    hconnect_server.HCPHandler._handle_service_request_legacy(
        handler, "ServiceProfile", "Shared", 80000, 2, 1,
        "00000000-0000-0000-0000-000000000000", 0, {"Envelope": request}, b"")
    body = _unwrap(handler.sent[-1])
    assert b"Game.Shared.Profile.SavedProfileDeckTemplate" in body, body[:200]
    assert b"Merc Deck" in body and data in body
    (tid, name, stored), = deck_templates.list_templates(db._db, user_id)
    assert (name, stored) == ("Merc Deck", data)
    encoded = deck_templates.encode_saved_template_list([(tid, name, stored)])
    assert b"List`1#Game.Shared.Profile.SavedProfileDeckTemplate" in encoded
    assert data in encoded

    delete = json.dumps({"action": "pdeckdel", "DeckTemplateID": tid}).encode()
    hconnect_server.HCPHandler._handle_service_request_legacy(
        handler, "ServiceProfile", "Shared", 80000, 4, 1,
        "00000000-0000-0000-0000-000000000000", 0, {"Envelope": delete}, b"")
    assert deck_templates.list_templates(db._db, user_id) == []


BEBO_ITEM = "321fd775-ee06-4186-a5d6-368d552f8d09"
BEBO_TEMPLATE = "8a336fec-d970-4613-9308-c0afd41b62eb"
CHARGE_BOT = "7325706e-6bf1-4ca4-8d6b-5da13ac069f4"


def _party_with_bebo(user_id, champion_id):
    from services import deck_templates
    data = _deck_template_bytes("BEBO Bots", [(CHARGE_BOT, 4)])
    template_id, _ = deck_templates.save_template(db._db, user_id, 0, data)
    mercenaries.save_party(db._db, user_id, {
        "ChampionID": champion_id,
        "Members": [{"Mercenary": {"m_Guid": BEBO_ITEM},
                     "DeckTemplate": template_id, "Upgrade": 0}]})
    db._db.commit()


def test_mercenary_champions_are_seeded():
    row = db._db.execute(
        "SELECT name, starting_health FROM champion_templates_extended WHERE guid=?",
        (BEBO_TEMPLATE,)).fetchone()
    assert tuple(row) == ("B.E.B.O.", 22), row
    abilities = db._db.execute(
        "SELECT COUNT(*) FROM champion_abilities WHERE champion_guid=?",
        (BEBO_TEMPLATE,)).fetchone()[0]
    assert abilities == 2, abilities


def test_existing_database_backfills_mercenary_champions():
    import static
    db._db.execute("DELETE FROM champion_abilities WHERE champion_guid=?", (BEBO_TEMPLATE,))
    db._db.execute("DELETE FROM champion_templates_extended WHERE guid=?", (BEBO_TEMPLATE,))
    db._db.commit()
    static.ensure_schema(db._db)
    test_mercenary_champions_are_seeded()


def test_battle_mercenary_resolves_party_member():
    user_id = _new_user(9406)
    _party_with_bebo(user_id, 4)
    merc = mercenaries.battle_mercenary(db._db, user_id, 4, BEBO_ITEM)
    assert merc == {"champion_guid": BEBO_TEMPLATE, "name": "B.E.B.O.",
                    "starting_health": 22, "deck_cards": [CHARGE_BOT] * 4}, merc
    assert mercenaries.battle_mercenary(db._db, user_id, 5, BEBO_ITEM) is None
    assert mercenaries.battle_mercenary(db._db, user_id, 4, mercenaries.ZERO_GUID) is None


def test_battle_config_uses_active_mercenary():
    user_id = _new_user(9407)
    _party_with_bebo(user_id, 4)
    handler = hconnect_server.HCPHandler.__new__(hconnect_server.HCPHandler)
    handler.user_profile = {"id": user_id}
    base = campaign.resolve_battle_config(handler, db._db, 0, "camp_0")
    assert base["mercenary_deck_cards"] is None
    campaign._active_mercenary[0] = (4, BEBO_ITEM)
    try:
        cfg = campaign.resolve_battle_config(handler, db._db, 0, "camp_0")
    finally:
        campaign._active_mercenary.pop(0, None)
    assert cfg["player_champ_guid"] == BEBO_TEMPLATE
    assert cfg["player_champ_name"] == "B.E.B.O."
    assert cfg["player_starting_health"] == 22
    assert cfg["player_talents_json"] == "[]"
    assert cfg["mercenary_deck_cards"] == [CHARGE_BOT] * 4


if __name__ == "__main__":
    run("list writer matches chest encoder", test_list_writer_matches_chest_encoder)
    run("flag and party encoding", test_flag_and_party_encoding)
    run("party JSON accepts ResourceId shapes", test_party_json_accepts_resource_id_shapes)
    run("partysave/partyload round trip", test_partysave_and_partyload_round_trip)
    run("login push sends flags and parties", test_login_push_sends_flags_and_parties)
    run("template name and storage", test_template_name_and_storage)
    run("pdecktsave returns ObjFmt template", test_pdecktsave_returns_objfmt_template_and_login_lists_it)
    run("mercenary champions are seeded", test_mercenary_champions_are_seeded)
    run("existing database backfills mercenary champions", test_existing_database_backfills_mercenary_champions)
    run("battle mercenary resolves party member", test_battle_mercenary_resolves_party_member)
    run("battle config uses active mercenary", test_battle_config_uses_active_mercenary)
    if FAILURES:
        sys.exit(1)
