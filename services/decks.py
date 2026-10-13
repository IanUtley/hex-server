"""Deck list requests (ServiceProfile) moved out of the HConnect legacy dispatcher."""

import io
import json
import struct
from binascii import hexlify, unhexlify

from application.response_encoding import compress_gzip, encode_datawrapper
from profile_db import db_get_decks


def handle_get_player_decks(handler, target, instance, reqid, comp, session_id,
                            conh, SERVICE_PROFILE_UID, log_req, **_kw):
    """ServiceProfile — GetPlayerDecks (2081)."""
    data_type = 2081
    log_req(">>> GetPlayerDecks (dt=2081)")
    decks = []
    if handler.user_profile:
        decks = db_get_decks(handler.user_profile["id"])
    log_req(f"    Returning {len(decks)} decks")

    # Manual encoding: GetPlayerDecksResponse { PlayerDeckIDs, PlayerDeckNames, Error, ErrorMessage }
    tn = ["Game.Client.Network.Profile.GetPlayerDecksResponse",
          "System.Collections.Generic.List`1#Game.Shared.UID",
          "Game.Shared.UID", "System.UInt64",
          "System.Collections.Generic.List`1#System.String",
          "System.String",
          "Game.Shared.Network.Profile.EGetPlayerDecksError",
          "System.Int32"]
    def _profile_type_index(x):
        if x not in tn: tn.append(x)
        return tn.index(x)
    sz = []; buf = io.BytesIO()
    W = lambda s: buf.write(s.encode("utf-8"))
    S = lambda: buf.write(b";")

    sz.append(0)
    W(""); S(); W("0"); S(); W(str(_profile_type_index(tn[0]))); S(); W("4"); S()

    # PlayerDeckIDs: List<UID>
    fc1 = buf.tell(); sz.append(0); list1_idx = len(sz)-1
    W("PlayerDeckIDs"); S(); W(str(list1_idx)); S(); W(str(_profile_type_index(tn[1]))); S(); W("0"); S()
    W(str(len(decks))); S()
    for i, dk in enumerate(decks):
        fe = buf.tell(); sz.append(0); eidx = len(sz)-1
        deck_uid64 = (dk["id"] << 8) | 17
        W(str(i)); S(); W(str(eidx)); S(); W(str(_profile_type_index(tn[2]))); S(); W("1"); S()
        f1 = buf.tell(); sz.append(0)
        W("m_UID64"); S(); W(str(len(sz)-1)); S(); W(str(_profile_type_index("System.UInt64"))); S(); W("0"); S()
        W(hexlify(struct.pack("<Q", deck_uid64)).decode("ascii")); S()
        sz[-1] = buf.tell() - f1
        sz[eidx] = buf.tell() - fe
    sz[list1_idx] = buf.tell() - fc1

    # PlayerDeckNames: List<string>
    fc2 = buf.tell(); sz.append(0); list2_idx = len(sz)-1
    W("PlayerDeckNames"); S(); W(str(list2_idx)); S(); W(str(_profile_type_index(tn[4]))); S(); W("0"); S()
    W(str(len(decks))); S()
    for i, dk in enumerate(decks):
        fe = buf.tell(); sz.append(0); eidx = len(sz)-1
        W(str(i)); S(); W(str(eidx)); S(); W(str(_profile_type_index(tn[5]))); S(); W("0"); S()
        enc = dk["name"].encode("utf-8")
        W(str(len(enc))); S(); buf.write(enc)
        sz[eidx] = buf.tell() - fe
    sz[list2_idx] = buf.tell() - fc2

    # Error (enum)
    f3 = buf.tell(); sz.append(0)
    W("Error"); S(); W(str(len(sz)-1)); S(); W(str(_profile_type_index(tn[6]))); S(); W("1"); S()
    f3v = buf.tell(); sz.append(0)
    W("value__"); S(); W(str(len(sz)-1)); S(); W(str(_profile_type_index("System.Int32"))); S(); W("0"); S()
    W(hexlify(struct.pack("<i", 0)).decode("ascii")); S()
    sz[-1] = buf.tell() - f3v; sz[-2] = buf.tell() - f3

    # ErrorMessage
    f4 = buf.tell(); sz.append(0)
    W("ErrorMessage"); S(); W(str(len(sz)-1)); S(); W(str(_profile_type_index("System.String"))); S(); W("0"); S()
    W("0"); S()
    sz[-1] = buf.tell() - f4

    sz[0] = buf.tell()
    W(";".join(tn)); buf.write(b"\n")
    for i, s in enumerate(sz):
        if i > 0: S()
        W(str(s))
    resp_inner = buf.getvalue()
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({"issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent GetPlayerDecks ({len(decks)} decks, {len(dw_bytes)}b)")


def handle_get_deck_info(handler, target, instance, reqid, comp, session_id, conh,
                         inner_bytes=b"", SERVICE_PROFILE_UID=None, log_req=None,
                         **_kw):
    """ServiceProfile — GetDeckInfo (2083): one deck with its cards and gems."""
    data_type = 2083
    log_req(">>> GetDeckInfo (dt=2083)")
    # Extract deck ID from request raw bytes (UID encoded with m_UID64)
    deck_id_val = 0
    deck_id = 0
    if isinstance(inner_bytes, bytes) and b"DeckID" in inner_bytes:
        pos = inner_bytes.find(b"DeckID")
        if pos >= 0:
            rest = inner_bytes[pos:]
            # Find m_UID64 sub-field within the DeckID UID
            uid_pos = rest.find(b"m_UID64")
            if uid_pos >= 0:
                rest2 = rest[uid_pos:]
                parts = rest2.split(b";", 5)
                if len(parts) >= 5:
                    try:
                        deck_id_val = struct.unpack("<Q", unhexlify(parts[4]))[0]
                        # deck_id_val is the combined UID: (deck_db_id << 8) | 17
                        deck_id = deck_id_val >> 8
                    except:
                        pass
    log_req(f"    deck_id={deck_id}")
    # Look up deck from DB
    deck_name = ""
    db_deck = None
    if handler.user_profile and deck_id > 0:
        db_decks = db_get_decks(handler.user_profile["id"])
        for dk in db_decks:
            if dk["id"] == deck_id:
                db_deck = dk
                deck_name = dk.get("name", "")
                break
    if not db_deck:
        # Return error response
        from objfmt_builder import ObjFmtBuilder
        b = ObjFmtBuilder("Game.Client.Network.Profile.GetDeckInfoResponse")
        b.field_enum("Error", "Game.Shared.Network.Profile.EGetDeckInfoError", 2)  # no_such_deck
        b.field_str("ErrorMessage", "Deck not found")
        resp_inner = b.finish(2)
    else:
        from objfmt_builder import ObjFmtBuilder
        b = ObjFmtBuilder("Game.Client.Network.Profile.GetDeckInfoResponse")
        b.field_str("DeckName", deck_name)
        b.field_uid("DeckID", deck_id_val)
        b.field_uid("PvEChampionId", int(db_deck.get("pve_champion_id") or 0))
        b.field_resource_id("PvPChampionId", db_deck.get("pvp_champion_guid") or "00000000-0000-0000-0000-000000000000")
        # Parse card IDs from DB
        card_ids = []
        try:
            card_ids = json.loads(db_deck.get("cards", "[]")) if db_deck.get("cards") else []
        except: pass
        b.begin_list("DeckCardIDs", "System.Collections.Generic.List`1#System.UInt64", len(card_ids))
        for ci, cid in enumerate(card_ids):
            b.add_list_item_uint64(ci, cid)
        b.begin_list("SideboardCardIDs", "System.Collections.Generic.List`1#System.UInt64", 0)
        try:
            deck_equipment = json.loads(db_deck.get("equipment") or "[]")
        except (TypeError, ValueError):
            deck_equipment = []
        b.field_resource_id_list("EquipmentIDs", deck_equipment)
        b.begin_list("TalentIDs", "System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)
        b.field_resource_id("DeckSleeveId", db_deck.get("deck_sleeve_guid") or "00000000-0000-0000-0000-000000000000")
        b.field_resource_id("GameboardId", db_deck.get("gameboard_guid") or "00000000-0000-0000-0000-000000000000")
        b.field_resource_id("CoinId", db_deck.get("coin_guid") or "00000000-0000-0000-0000-000000000000")
        # ActiveGems
        active_gems = {}
        try:
            ag_raw = db_deck.get("active_gems", "{}")
            active_gems = json.loads(ag_raw) if ag_raw else {}
        except: pass
        b.begin_list("ActiveGems", "System.Collections.Generic.Dictionary`2#System.UInt64!Game.Shared.Mechanics.EGemTypesNew", len(active_gems))
        for gi, (k_str, v_val) in enumerate(active_gems.items()):
            b.add_dict_entry_gem(gi, int(k_str), int(v_val))
        b.field_enum("Persona", "Game.Shared.Mechanics.EDeckPersonality", 0)
        b.field_enum("Error", "Game.Shared.Network.Profile.EGetDeckInfoError", 0)
        b.field_str("ErrorMessage", "")
        resp_inner = b.finish(15)
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({"issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent GetDeckInfo response ({len(dw_bytes)}b)")
