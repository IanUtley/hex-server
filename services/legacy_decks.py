"""Deck and champion profile requests (create, update, delete, talents).

Moved out of hconnect_server.HCPHandler._handle_service_request_legacy.
"""

from application.champion_defaults import _RACE_DECK_MAP
from application.champion_defaults import _default_talents_for_champion
from application.response_encoding import compress_gzip
from application.response_encoding import encode_datawrapper
from application.response_encoding import encode_objfmt_response
from binascii import hexlify
from binascii import unhexlify
from db import _db
from db import log_req
from domain.enums import EGemTypesNew
from profile_db import db_add_collection
from profile_db import db_campaign_deck_for_champion
from profile_db import db_card_instance_art
from profile_db import db_card_instance_template
from profile_db import db_champion_last_deck
from profile_db import db_champion_profile
from profile_db import db_delete_champion
from profile_db import db_find_deck_owner
from profile_db import db_get_user_by_client_auth_id
from profile_db import db_insert_card_instance
from profile_db import db_insert_champion
from profile_db import db_next_card_instance_for_user
from profile_db import db_profile_card_instances
from profile_db import db_purchase_exists
from profile_db import db_record_purchase
from profile_db import db_save_deck
from profile_db import db_set_card_instance_extended_art
from profile_db import db_set_champion_last_deck
from profile_db import db_update_champion_talents
from profile_db import db_update_deck
import io
import json
import struct


# CreateNewDeckFromTemplate (2091)
def handle_createnewdeckfromtemplate_2091(handler, target, instance, data_type, reqid, comp, session_id,
                                          conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(f">>> CreateNewDeckFromTemplate (dt=2091)")
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Profile.CreateNewDeckFromTemplateResponse",
         "System.Boolean",
         "Game.Shared.Domain.deck_bits",
         "Game.Shared.UID",
         "System.String",
         "System.UInt64",
         "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
         "System.Collections.Generic.List`1#Game.Shared.ResourceId",
         "Game.Shared.ResourceId",
         "System.Guid"],
        [("succeded", "bool", True),
         ("Deckbits", "class", "Game.Shared.Domain.deck_bits"),
         ("failingCardTemplaes", "coll", ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0))]
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent CreateNewDeckFromTemplate response ({len(dw_bytes)}b)")


# AddNewDeck (2089)
def handle_addnewdeck_2089(handler, target, instance, data_type, reqid, comp, session_id,
                           conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(f">>> AddNewDeck (dt=2089)")
    # Extract deck name from raw bytes (parser fails on ResourceId)
    deck_name = "FirstDeck"
    if isinstance(inner_obj, dict) and inner_obj.get("DeckName"):
        deck_name = inner_obj["DeckName"]
    elif isinstance(inner_bytes, bytes) and b"DeckName" in inner_bytes:
        pos = inner_bytes.find(b"DeckName")
        if pos >= 0:
            rest = inner_bytes[pos+8:]
            parts = rest.split(b";", 5)
            if len(parts) >= 5:
                try:
                    namelen = int(parts[4])
                    namebytes = parts[5][:namelen] if len(parts) > 5 else b""
                    deck_name = namebytes.decode("utf-8", errors="replace")
                except:
                    pass
    log_req(f"    Deck name: {deck_name}")

    # Extract card IDs from raw bytes and save to DB first
    card_ids = []
    if isinstance(inner_obj, dict) and inner_obj.get("DeckCardIDs"):
        card_ids = inner_obj["DeckCardIDs"]
    elif isinstance(inner_bytes, bytes) and b"DeckCardIDs" in inner_bytes:
        pos = inner_bytes.find(b"DeckCardIDs")
        if pos >= 0:
            rest = inner_bytes[pos+12:]
            parts = rest.split(b";", 5)
            if len(parts) >= 4:
                try:
                    count = int(parts[3])
                    tail = b";".join(parts[4:])
                    for _ in range(count):
                        # Card ID: idx;size;type;0;value;
                        seg = tail.split(b";", 5)
                        if len(seg) >= 5:
                            card_ids.append(struct.unpack("<Q", unhexlify(seg[4]))[0])
                            tail = b";".join(seg[5:])
                except:
                    pass

    # The real deck ID comes from db_save_deck()'s last_insert_rowid().
    # Parse champion/gems/sleeve from raw bytes
    pve_champion_uid = None
    pvp_champ_guid = None
    active_gems = {}
    deck_sleeve_guid = None
    gameboard_guid = None
    coin_guid = None
    if isinstance(inner_bytes, bytes):
        # PvEChampionId (UID) — the champion this deck belongs to.
        pos = inner_bytes.find(b'PvEChampionId')
        if pos >= 0:
            uid_pos = inner_bytes.find(b'm_UID64', pos)
            if uid_pos >= 0:
                rest = inner_bytes[uid_pos:]; parts = rest.split(b';', 5)
                if len(parts) >= 5:
                    try: pve_champion_uid = struct.unpack("<Q", unhexlify(parts[4]))[0]
                    except: pve_champion_uid = None
        pos = inner_bytes.find(b'PvPChampionId')
        if pos >= 0:
            gpos = inner_bytes.find(b'm_Guid', pos)
            if gpos >= 0:
                rest = inner_bytes[gpos:]; parts = rest.split(b';', 5)
                if len(parts) >= 5:
                    try: pvp_champ_guid = parts[5][:int(parts[4])].decode('utf-8', errors='replace')
                    except: pass
        pos = inner_bytes.find(b'ActiveGems')
        if pos >= 0:
            rest = inner_bytes[pos:]; parts = rest.split(b';', 5)
            if len(parts) >= 5:
                try:
                    count = int(parts[4]); tail = b';'.join(parts[5:])
                    for _ in range(count):
                        seg = tail.split(b';', 18)
                        if len(seg) >= 18:
                            key_val = struct.unpack("<Q", unhexlify(seg[8]))[0]
                            v_val = struct.unpack("<Q", unhexlify(seg[17]))[0]
                            active_gems[str(key_val)] = v_val
                            tail = b';'.join(seg[18:])
                except: pass
        for fname in [b'DeckSleeveId', b'GameboardId', b'CoinId']:
            pos2 = inner_bytes.find(fname)
            if pos2 >= 0:
                gpos2 = inner_bytes.find(b'm_Guid', pos2)
                if gpos2 >= 0:
                    rest2 = inner_bytes[gpos2:]; parts2 = rest2.split(b';', 5)
                    if len(parts2) >= 5:
                        try: val = parts2[5][:int(parts2[4])].decode('utf-8', errors='replace')
                        except: continue
                        if fname == b'DeckSleeveId': deck_sleeve_guid = val
                        elif fname == b'GameboardId': gameboard_guid = val
                        elif fname == b'CoinId': coin_guid = val
    if not handler.user_profile:
        # No authenticated user — error out rather than fabricate a
        # bogus deck ID that the client can't look up again.
        log_req("    ERROR: No user_profile for AddNewDeck — rejecting")
        err_inner = encode_objfmt_response(
            ["Game.Client.Network.Profile.AddNewDeckResponse",
             "Game.Shared.Network.Profile.EAddNewDeckError", "System.String"],
            [("Error", "enum", "Game.Shared.Network.Profile.EAddNewDeckError", 1),
             ("ErrorMessage", "string", "Not authenticated")]
        )
        err_body = compress_gzip(err_inner) if comp else err_inner
        err_reqid = reqid | 1
        dw_err = encode_datawrapper(err_reqid, data_type, err_body, comp, session_id)
        issuer_err = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{err_reqid}"
        handler.scnt += 1
        handler.send({
            "issuer": issuer_err, "target": target, "instance": instance,
            "reqid": err_reqid, "c": comp, "conh": conh, "sid": handler.sid,
        }, dw_err)
        log_req(f"    Sent AddNewDeck error ({len(dw_err)}b)")
        return
    cards_json = json.dumps(card_ids)
    # Persist an explicit empty map too; otherwise removing a gem can
    # leave the previous ActiveGems/gem_abilities pair on the deck.
    ag_json = json.dumps(active_gems)
    gem_abilities = handler._resolve_gem_abilities(active_gems)
    ga_json = json.dumps(gem_abilities)
    user_id = handler.user_profile["id"]
    # Campaign saves arrive as AddNewDeck rather than UpdateDeck.  A
    # campaign character still has one persistent deck, so reuse the
    # newest campaign row for this character instead of creating a
    # duplicate every time the player clicks Save Deck.  Normal decks
    # continue through db_save_deck and may coexist as before.
    existing_campaign = None
    if pve_champion_uid:
        existing_campaign = db_campaign_deck_for_champion(
            user_id, pve_champion_uid, conn=_db)
    if existing_campaign:
        deck_db_id = existing_campaign[0]
        db_update_deck(
            deck_db_id, user_id, deck_name=deck_name,
            cards_json=cards_json, pve_champion_id=pve_champion_uid,
            pvp_champion_guid=pvp_champ_guid,
            active_gems_json=ag_json, gem_abilities_json=ga_json,
            deck_sleeve_guid=deck_sleeve_guid,
            gameboard_guid=gameboard_guid, coin_guid=coin_guid)
        log_req(f"    Updated campaign deck '{deck_name}' "
                f"id={deck_db_id} with {len(card_ids)} cards")
    else:
        deck_db_id = db_save_deck(
            user_id, deck_name, cards_json,
            pve_champion_id=pve_champion_uid,
            pvp_champion_guid=pvp_champ_guid,
            active_gems_json=ag_json, gem_abilities_json=ga_json,
            deck_sleeve_guid=deck_sleeve_guid,
            gameboard_guid=gameboard_guid, coin_guid=coin_guid)
        log_req(f"    Saved deck '{deck_name}' id={deck_db_id} "
                f"with {len(card_ids)} cards")
    deck_uid = deck_db_id
    # Link this deck as the champion's last used deck (pve_champion_id is
    # the champion UID = (db_id << 8) | 12).
    if pve_champion_uid:
        champ_db_id = pve_champion_uid >> 8
        db_set_champion_last_deck(
            champ_db_id, deck_db_id, conn=_db)
        log_req(f"    Set champion {champ_db_id} last_deck_id={deck_db_id}")
    deck_uid64 = (deck_uid << 8) | 17  # UID.Type.Deck=17
    # Build deck_bits manually with all 23 fields (skip ActiveGems)
    tn = ["Game.Client.Network.Profile.AddNewDeckResponse",
          "System.String", "Game.Shared.UID", "System.UInt64",
          "Game.Shared.Domain.deck_bits"]
    def ft2(x):
        if x not in tn: tn.append(x)
        return tn.index(x)
    sz = [] ; b = io.BytesIO()
    W = lambda s2: b.write(s2.encode("utf-8"))
    S = lambda: b.write(b";") ; L = lambda: b.write(b"\n")
    sz.append(0)
    W(""); S(); W("0"); S(); W(str(ft2(tn[0]))); S(); W("3"); S()
    # DeckName
    f = b.tell(); sz.append(0)
    W("DeckName"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.String"))); S(); W("0"); S()
    e = deck_name.encode("utf-8"); W(str(len(e))); S(); b.write(e)
    sz[-1] = b.tell() - f
    # DeckID (UID)
    f = b.tell(); sz.append(0) ; uidx = len(sz)-1
    W("DeckID"); S(); W(str(uidx)); S(); W(str(ft2("Game.Shared.UID"))); S(); W("1"); S()
    fs = b.tell(); sz.append(0) ; midx = len(sz)-1
    W("m_UID64"); S(); W(str(midx)); S(); W(str(ft2("System.UInt64"))); S(); W("0"); S()
    W(hexlify(struct.pack("<Q", deck_uid64)).decode("ascii")); S()
    sz[midx] = b.tell() - fs ; sz[uidx] = b.tell() - f
    # Deckbits (manual 22 fields — skip ActiveGems, Lock, Personality)
    dbidx = len(sz)-1
    f = b.tell(); sz.append(0) ; dbx = len(sz)-1
    W("Deckbits"); S(); W(str(dbx)); S(); W(str(ft2("Game.Shared.Domain.deck_bits"))); S(); W("22"); S()
    def wfld(name, typ, val):
        f2 = b.tell(); sz.append(0)
        W(name); S(); W(str(len(sz)-1)); S(); W(str(ft2(typ))); S()
        if typ == "System.String":
            W("0"); S()
            enc = val.encode("utf-8") if isinstance(val, str) else val
            W(str(len(enc))); S(); b.write(enc)
        elif typ == "System.Boolean":
            W("0"); S()
            W("1" if val else "0")
        else:
            W("0"); S()
            W(val); S()
        sz[-1] = b.tell() - f2
    wfld("Id", "System.UInt64", hexlify(struct.pack("<Q", deck_uid)).decode("ascii"))
    # DeckName (string — manual for proper encoding)
    f2 = b.tell(); sz.append(0)
    W("DeckName"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.String"))); S(); W("0"); S()
    e2 = deck_name.encode("utf-8"); W(str(len(e2))); S(); b.write(e2)
    sz[-1] = b.tell() - f2
    wfld("PVEChampionId", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
    # ResourceId fields — use empty guid
    def wrid2(name):
        f2 = b.tell(); sz.append(0)
        W(name); S(); W(str(len(sz)-1)); S(); W(str(ft2("Game.Shared.ResourceId"))); S(); W("1"); S()
        fs2 = b.tell(); sz.append(0)
        W("guid"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.Guid"))); S(); W("0"); S()
        W("36"); S(); b.write(b"00000000-0000-0000-0000-000000000000")
        sz[-1] = b.tell() - fs2 ; sz[-2] = b.tell() - f2
    wrid2("PVPChampionId")
    for t in range(1,6): wrid2(f"talent_{t}")
    for eq in range(1,7): wrid2(f"equipment_{eq}")
    # CardsInDeck with actual card data from card_instances
    f2 = b.tell(); sz.append(0)
    W("CardsInDeck"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits"))); S(); W("0"); S()
    W(str(len(card_ids))); S()
    for ci, cid in enumerate(card_ids):
        # Look up template GUID from card_instances
        tguid = "00000000-0000-0000-0000-000000000000"
        if handler.user_profile:
            instance_template = db_card_instance_template(
                handler.user_profile["id"], cid, conn=_db)
            if instance_template:
                tguid = instance_template
        fe = b.tell(); sz.append(0); eidx = len(sz)-1
        W(str(ci)); S(); W(str(eidx)); S(); W(str(ft2("Game.Shared.Domain.card_instance_bits"))); S(); W("6"); S()
        # Id
        f1 = b.tell(); sz.append(0)
        W("Id"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.UInt64"))); S(); W("0"); S()
        W(hexlify(struct.pack("<Q", cid)).decode("ascii")); S()
        sz[-1] = b.tell() - f1
        # TemplateID
        f2r = b.tell(); sz.append(0); tidx = len(sz)-1
        W("TemplateID"); S(); W(str(tidx)); S(); W(str(ft2("Game.Shared.ResourceId"))); S(); W("1"); S()
        gs = b.tell(); sz.append(0); gidx = len(sz)-1
        W("guid"); S(); W(str(gidx)); S(); W(str(ft2("System.Guid"))); S(); W("0"); S()
        W("36"); S(); b.write(tguid.encode())
        sz[gidx] = b.tell() - gs; sz[tidx] = b.tell() - f2r
        for bname in ("IsFoil", "IsExtended", "IsNotTradeable"):
            fb = b.tell(); sz.append(0)
            W(bname); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.Boolean"))); S(); W("0"); S()
            W("0"); sz[-1] = b.tell() - fb
        # EscrowStatus
        fb = b.tell(); sz.append(0)
        W("EscrowStatus"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.String"))); S(); W("0"); S()
        enc = b"Clean"; W(str(len(enc))); S(); b.write(enc)
        sz[-1] = b.tell() - fb; sz[eidx] = b.tell() - fe
    sz[-1] = b.tell() - f2
    # CardsInSideboard (empty)
    f2 = b.tell(); sz.append(0)
    W("CardsInSideboard"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits"))); S(); W("0"); S()
    W("0"); S()
    sz[-1] = b.tell() - f2
    wfld("LockHolder", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
    wrid2("deck_sleeve")
    wrid2("gameboard")
    wrid2("Coin")
    wfld("player_id", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
    sz[dbx] = b.tell() - f
    # Size table
    sz[0] = b.tell()
    W(";".join(tn)); L()
    for i, v in enumerate(sz):
        if i > 0: S()
        W(str(v))
    resp_inner = b.getvalue()
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent AddNewDeck response (deck={deck_uid}, {len(dw_bytes)}b)")


# UpdateDeck (2095)
def handle_updatedeck_2095(handler, target, instance, data_type, reqid, comp, session_id,
                           conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> UpdateDeck (dt=2095)")
    deck_id = 0
    deck_name = None
    pve_champion_id = None
    pvp_champion_guid = None
    card_ids = None
    active_gems = {}
    deck_sleeve_guid = None
    gameboard_guid = None
    coin_guid = None
    if isinstance(inner_bytes, bytes):
        # DeckID
        pos = inner_bytes.find(b'DeckID')
        if pos >= 0:
            uid_pos = inner_bytes.find(b'm_UID64', pos)
            if uid_pos >= 0:
                rest = inner_bytes[uid_pos:]; parts = rest.split(b';', 5)
                if len(parts) >= 5:
                    try: deck_id = struct.unpack("<Q", unhexlify(parts[4]))[0] >> 8
                    except: pass
        # DeckName
        pos = inner_bytes.find(b'DeckName')
        if pos >= 0:
            rest = inner_bytes[pos:]; parts = rest.split(b';', 5)
            if len(parts) >= 5:
                try: deck_name = parts[5][:int(parts[4])].decode('utf-8', errors='replace')
                except: pass
        # PvEChampionId
        pos = inner_bytes.find(b'PvEChampionId')
        if pos >= 0:
            uid_pos = inner_bytes.find(b'm_UID64', pos)
            if uid_pos >= 0:
                rest = inner_bytes[uid_pos:]; parts = rest.split(b';', 5)
                if len(parts) >= 5:
                    try: pve_champion_id = struct.unpack("<Q", unhexlify(parts[4]))[0]
                    except: pass
        # PvPChampionId
        pos = inner_bytes.find(b'PvPChampionId')
        if pos >= 0:
            guid_pos = inner_bytes.find(b'm_Guid', pos)
            if guid_pos >= 0:
                rest = inner_bytes[guid_pos:]; parts = rest.split(b';', 5)
                if len(parts) >= 5:
                    try: pvp_champion_guid = parts[5][:int(parts[4])].decode('utf-8', errors='replace')
                    except: pass
        # DeckCardIDs
        pos = inner_bytes.find(b'DeckCardIDs')
        if pos >= 0:
            rest = inner_bytes[pos:]; parts = rest.split(b';', 5)
            if len(parts) >= 5:
                try:
                    count = int(parts[4]); card_ids = []
                    tail = b';'.join(parts[5:])
                    for _ in range(count):
                        seg = tail.split(b';', 5)
                        if len(seg) >= 5:
                            card_ids.append(struct.unpack("<Q", unhexlify(seg[4]))[0])
                            tail = b';'.join(seg[5:])
                except: pass
        # ActiveGems
        pos = inner_bytes.find(b'ActiveGems')
        if pos >= 0:
            rest = inner_bytes[pos:]; parts = rest.split(b';', 5)
            if len(parts) >= 5:
                try:
                    count = int(parts[4]); tail = b';'.join(parts[5:])
                    for _ in range(count):
                        seg = tail.split(b';', 18)
                        if len(seg) >= 18:
                            key_val = struct.unpack("<Q", unhexlify(seg[8]))[0]
                            v_val = struct.unpack("<Q", unhexlify(seg[17]))[0]
                            active_gems[str(key_val)] = v_val
                            tail = b';'.join(seg[18:])
                except: pass
        # DeckSleeveId, GameboardId, CoinId
        for fname, vname in [(b'DeckSleeveId', 'deck_sleeve'), (b'GameboardId', 'gameboard'), (b'CoinId', 'coin')]:
            pos2 = inner_bytes.find(fname)
            if pos2 >= 0:
                gpos = inner_bytes.find(b'm_Guid', pos2)
                if gpos >= 0:
                    rest = inner_bytes[gpos:]; parts = rest.split(b';', 5)
                    if len(parts) >= 5:
                        try:
                            val = parts[5][:int(parts[4])].decode('utf-8', errors='replace')
                            if fname == b'DeckSleeveId': deck_sleeve_guid = val
                            elif fname == b'GameboardId': gameboard_guid = val
                            elif fname == b'CoinId': coin_guid = val
                        except: pass
    log_req(f"    Parsed: id={deck_id}, name={deck_name}, cards={len(card_ids) if card_ids else 0}, gems={len(active_gems)}, sleeve={deck_sleeve_guid}")

    success = False
    owner_id = None
    if deck_id > 0:
        if handler.user_profile:
            owner_id = handler.user_profile["id"]
        else:
            # Reconnect path: the client can send a pending deck edit on
            # a fresh connection before the auth handshake completes
            # (e.g. right after a server restart — the 16:42 log shows
            # UpdateDeck with no preceding auth:req, which used to drop
            # the save silently).  Attribute the deck by its DB owner:
            # the client only ever edits its own decks, and the deck id
            # is the handle the client must already hold.
            owner_id = db_find_deck_owner(deck_id, conn=_db)
    if owner_id:
        cards_json = json.dumps(card_ids) if card_ids is not None else None
        # Keep the denormalized cache in lockstep with ActiveGems,
        # including the empty-socket case.
        ag_json = json.dumps(active_gems)
        gem_abilities = handler._resolve_gem_abilities(active_gems)
        ga_json = json.dumps(gem_abilities)
        success = db_update_deck(deck_id, owner_id,
            deck_name=deck_name, cards_json=cards_json,
            pve_champion_id=pve_champion_id, pvp_champion_guid=pvp_champion_guid,
            active_gems_json=ag_json, gem_abilities_json=ga_json,
            deck_sleeve_guid=deck_sleeve_guid, gameboard_guid=gameboard_guid, coin_guid=coin_guid)
        log_req(f"    DB update: {'OK' if success else 'FAILED'} "
                f"(owner={owner_id})")
        # Link this deck as the champion's last used deck (pve_champion_id
        # is the champion UID = (db_id << 8) | 12).
        if pve_champion_id:
            champ_db_id = pve_champion_id >> 8
            db_set_champion_last_deck(
                champ_db_id, deck_id, conn=_db)
            log_req(f"    Set champion {champ_db_id} last_deck_id={deck_id}")

    # Only report a real deck UID; on failure return invalid (updated=False
    # tells the client it didn't save).
    deck_uid64 = (deck_id << 8) | 17 if deck_id > 0 else 0
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Profile.UpdateDeckResponse",
         "Game.Shared.UID", "System.UInt64", "System.Boolean"],
        [("DeckID", "uid", deck_uid64),
         ("updated", "bool", success)]
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({"issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent UpdateDeck response (deck={deck_id}, {len(dw_bytes)}b)")


# GetPlayerCardIDList (2043)
def handle_getplayercardidlist_2043(handler, target, instance, data_type, reqid, comp, session_id,
                                    conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(f">>> GetPlayerCardIDList (dt=2043)")
    p = handler.user_profile
    db_cards = []
    if p:
        db_cards = list(db_profile_card_instances(p["id"], conn=_db))
    ctype_names = [
        "Game.Client.Network.Profile.GetPlayerCardIDListResponse",
        "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
        "Game.Shared.Domain.card_instance_bits",
        "Game.Shared.ResourceId", "System.Guid", "System.UInt64",
    ]
    def ft(tn):
        if tn not in ctype_names: ctype_names.append(tn)
        return ctype_names.index(tn)
    csizes = []; cbuf = io.BytesIO(); w = lambda s: cbuf.write(s.encode("utf-8")); sep = lambda: cbuf.write(b";"); lf = lambda: cbuf.write(b"\n")
    csizes.append(0)
    total_cards = len(db_cards)
    w(""); sep(); w("0"); sep(); w(str(ft(ctype_names[0]))); sep(); w("1"); sep()
    fc = cbuf.tell(); csizes.append(0)
    w("CardDetails"); sep(); w("1"); sep(); w(str(ft(ctype_names[1]))); sep(); w("0"); sep()
    w(str(total_cards)); sep()
    card_idx = 0
    for guid, name, cost, atk, def_, instance_id, is_ext in db_cards:
        fe = cbuf.tell(); csizes.append(0); eidx = len(csizes)-1
        w(str(card_idx)); sep(); w(str(eidx)); sep(); w(str(ft(ctype_names[2]))); sep(); w("8"); sep()
        f1 = cbuf.tell(); csizes.append(0)
        w("Id"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
        # card_instance_bits.Id is the raw instance number.  The
        # client wraps it as CardId(UID.Type.Card, Id); putting a
        # UID type byte into this field changes the instance id
        # and makes collection entries fail to match.
        w(hexlify(struct.pack("<Q", instance_id)).decode("ascii")); sep()
        csizes[-1] = cbuf.tell() - f1
        f2 = cbuf.tell(); csizes.append(0); tidx = len(csizes)-1
        w("TemplateID"); sep(); w(str(tidx)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
        gs = cbuf.tell(); csizes.append(0); gidx = len(csizes)-1
        w("guid"); sep(); w(str(gidx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
        w("36"); sep(); cbuf.write(guid.encode())
        csizes[gidx] = cbuf.tell() - gs; csizes[tidx] = cbuf.tell() - f2
        # CardStats: empty Dictionary<ECardStatType,int> (numProps=0, count=0)
        f3 = cbuf.tell(); csizes.append(0)
        w("CardStats"); sep(); w(str(len(csizes)-1)); sep()
        w(str(ft("System.Collections.Generic.Dictionary`2#Game.Shared.Domain.ECardStatType!System.Int32")))
        sep(); w("0"); sep(); w("0"); sep()
        csizes[-1] = cbuf.tell() - f3
        f4 = cbuf.tell(); csizes.append(0)
        w("IsFoil"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
        w("0"); csizes[-1] = cbuf.tell() - f4
        f5 = cbuf.tell(); csizes.append(0)
        w("IsExtended"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
        w("1" if is_ext else "0"); csizes[-1] = cbuf.tell() - f5
        # SocketedGems: EGemTypesNew (ulong enum) = Unknown, with value__ child
        f6 = cbuf.tell(); csizes.append(0)
        w("SocketedGems"); sep(); w(str(len(csizes)-1)); sep()
        w(str(ft("Game.Shared.Mechanics.EGemTypesNew"))); sep(); w("1"); sep()
        f6v = cbuf.tell(); csizes.append(0)
        w("value__"); sep(); w(str(len(csizes)-1)); sep()
        w(str(ft("System.UInt64"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<Q", EGemTypesNew.Unknown)).decode("ascii")); sep()
        csizes[-1] = cbuf.tell() - f6v; csizes[-2] = cbuf.tell() - f6
        f7 = cbuf.tell(); csizes.append(0)
        w("IsNotTradeable"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
        w("0"); csizes[-1] = cbuf.tell() - f7
        f8 = cbuf.tell(); csizes.append(0)
        w("EscrowStatus"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
        enc = b"Clean"; w(str(len(enc))); sep(); cbuf.write(enc)
        csizes[-1] = cbuf.tell() - f8; csizes[eidx] = cbuf.tell() - fe
        card_idx += 1
    csizes[1] = cbuf.tell() - fc; csizes[0] = cbuf.tell()
    w(";".join(ctype_names)); lf()
    for i, s in enumerate(csizes):
        if i > 0: w(";")
        w(str(s))
    resp_inner = cbuf.getvalue()
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({"issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent GetPlayerCardIDList response ({total_cards} cards, {len(dw_bytes)}b)")


# DeleteChampion (2035)
def handle_deletechampion_2035(handler, target, instance, data_type, reqid, comp, session_id,
                               conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    champ_id = int(inner_obj.get("ChampionId", 0))
    log_req(f">>> DeleteChampion: id={champ_id}")
    if handler.user_profile and champ_id > 0:
        db_delete_champion(champ_id, handler.user_profile["id"], conn=_db)
        _db.commit()
        log_req(f"    Marked champion {champ_id} as deleted")
    
    # Encode DeleteChampionResponse (ChampionBits + Error + ErrorMessage)
    tnames = ["Game.Client.Network.Profile.DeleteChampionResponse",
               "Game.Shared.Domain.champion_bits",
               "Game.Shared.Network.Profile.EDeleteChampionError",
               "System.Int32", "System.String", "System.UInt64"]
    def dft(tn):
        if tn not in tnames: tnames.append(tn)
        return tnames.index(tn)
    dsizes = []; dbuf = io.BytesIO()
    dw = lambda s: dbuf.write(s.encode("utf-8"))
    dsep = lambda: dbuf.write(b";"); dlf = lambda: dbuf.write(b"\n")
    dsizes.append(0)
    dw(""); dsep(); dw("0"); dsep(); dw(str(dft(tnames[0]))); dsep(); dw("3"); dsep()
    # ChampionBits
    fc = dbuf.tell(); dsizes.append(0)
    dw("ChampionBits"); dsep(); dw(str(len(dsizes)-1)); dsep(); dw(str(dft(tnames[1]))); dsep(); dw("1"); dsep()
    fc_id = dbuf.tell(); dsizes.append(0)
    dw("Id"); dsep(); dw(str(len(dsizes)-1)); dsep(); dw(str(dft("System.UInt64"))); dsep(); dw("0"); dsep()
    dw(hexlify(struct.pack("<Q", champ_id)).decode("ascii")); dsep()
    dsizes[-1] = dbuf.tell() - fc_id
    dsizes[-2] = dbuf.tell() - fc
    # Error enum
    fe = dbuf.tell(); dsizes.append(0)
    dw("Error"); dsep(); dw(str(len(dsizes)-1)); dsep(); dw(str(dft(tnames[2]))); dsep(); dw("1"); dsep()
    fes = dbuf.tell(); dsizes.append(0)
    dw("value__"); dsep(); dw(str(len(dsizes)-1)); dsep(); dw(str(dft(tnames[3]))); dsep(); dw("0"); dsep()
    dw(hexlify(struct.pack("<i", 0)).decode("ascii")); dsep()
    dsizes[-1] = dbuf.tell() - fes
    dsizes[-2] = dbuf.tell() - fe
    # ErrorMessage
    fe2 = dbuf.tell(); dsizes.append(0)
    dw("ErrorMessage"); dsep(); dw(str(len(dsizes)-1)); dsep(); dw(str(dft(tnames[4]))); dsep(); dw("0"); dsep()
    dw("0"); dsep()
    dsizes[-1] = dbuf.tell() - fe2
    dsizes[0] = dbuf.tell()
    dw(";".join(tnames)); dlf()
    for i, s in enumerate(dsizes):
        if i > 0: dw(";")
        dw(str(s))
    
    resp_inner = dbuf.getvalue()
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent DeleteChampionResponse ({len(dw_bytes)}b)")


# AddChampion (2033)
def handle_addchampion_2033(handler, target, instance, data_type, reqid, comp, session_id,
                            conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    champ_name = inner_obj.get("ChampionName", "Unknown")
    pet_name = str(inner_obj.get("PetName", "") or "")
    owner_champ_id = 0
    try:
        if "OwnerChampionId" in inner_obj:
            o = inner_obj["OwnerChampionId"]
            owner_champ_id = int(o) if isinstance(o, str) and o.isdigit() else int(o)
    except: pass
    race_val = 1
    cls_val = 3
    gender_val = 1
    try:
        if "Race" in inner_obj:
            r = inner_obj["Race"]
            race_val = int(r[1]) if isinstance(r, list) else int(r.split(":'")[1].split("'")[0]) if isinstance(r, str) else int(r.get("value__", 1))
    except: pass
    try:
        if "ChampionClass" in inner_obj:
            c = inner_obj["ChampionClass"]
            cls_val = int(c[1]) if isinstance(c, list) else int(c.split(":'")[1].split("'")[0]) if isinstance(c, str) else int(c.get("value__", 3))
    except: pass
    try:
        if "Gender" in inner_obj:
            g = inner_obj["Gender"]
            gender_val = int(g[1]) if isinstance(g, list) else int(g.split(":'")[1].split("'")[0]) if isinstance(g, str) else int(g.get("value__", 1))
    except: pass
    log_req(f">>> AddChampion: name={champ_name} pet={pet_name!r} race={race_val} class={cls_val} gender={gender_val}")

    starting_talents = _default_talents_for_champion(
        race_val, cls_val, gender_val)

    if handler.user_profile:
        champ_db_id = db_insert_champion(
            handler.user_profile["id"], champ_name, race_val, cls_val,
            gender_val, pet_name, json.dumps(starting_talents), conn=_db)
        _db.commit()

        # Grant starter deck cards for this race (once only per race)
        race_name = _RACE_DECK_MAP.get(race_val)
        if race_name and race_name in _hc._STARTER_DECKS:
            deck = _hc._STARTER_DECKS[race_name]
            # Check if this PvE starter deck was already granted
            pve_template_guid = _hc._PVE_CHAMPION_GUIDS.get(race_name, "")
            already_granted = False
            if pve_template_guid:
                if db_purchase_exists(
                        handler.user_profile["id"], pve_template_guid,
                        conn=_db):
                    already_granted = True
            if not already_granted:
                cards_granted = 0
                for card_guid, count in deck["cards"]:
                    db_add_collection(
                        handler.user_profile["id"], card_guid, count,
                        conn=_db)
                    cards_granted += count
                # Create card_instances for each individual card. Start
                # at the first unused ID: the current maximum is already
                # taken, and INSERT OR IGNORE would silently drop that
                # card while its collection copy was still counted.
                cid = db_next_card_instance_for_user(
                    handler.user_profile["id"], conn=_db)
                for card_guid, count in deck["cards"]:
                    for _ in range(count):
                        db_insert_card_instance(
                            handler.user_profile["id"], cid, card_guid,
                            conn=_db)
                        cid += 1
                # Record as a free grant in store_purchases
                if pve_template_guid:
                    db_record_purchase(
                        handler.user_profile["id"],
                        f"PvE {race_name} Starter", pve_template_guid,
                        0, "Free", conn=_db)
                _db.commit()
                log_req(f"    Granted {cards_granted} starter deck cards for {race_name} (first grant)")
                handler.push_cards_to_client()
            else:
                log_req(f"    Starter deck for {race_name} already granted — skipping")

            # Every newly created champion needs its own starter deck,
            # even when this user already received the race's cards.
            cards_list = []
            for card_guid, count in deck["cards"]:
                cards_list.extend([card_guid] * count)
            cards_json = json.dumps(cards_list)
            deck_db_id = db_save_deck(
                handler.user_profile["id"],
                f"{champ_name}'s {race_name} Deck",
                cards_json,
                pve_champion_id=champ_db_id)
            db_set_champion_last_deck(
                champ_db_id, deck_db_id, conn=_db)
            log_req(f"    Auto-created deck id={deck_db_id} for champion {champ_db_id}")
    else:
        champ_db_id = 1

    champ_uid64 = (champ_db_id << 8) | 12  # 12 = UID.Type.Champion

    # Read back the champion's actual last_deck_id (set by the auto-created
    # starter deck above, or 0 if the deck was already granted previously).
    ldid_row = db_champion_last_deck(champ_db_id, conn=_db)
    resp_last_deck_id = ldid_row[0] if ldid_row else 0
    resp_pet_name = ldid_row[1] if ldid_row else pet_name

    # Manual ObjFmt encode: AddChampionResponse
    type_names = [
        "Game.Client.Network.Profile.AddChampionResponse",
        "Game.Shared.Domain.champion_bits",
        "Game.Shared.Domain.deck_bits",
        "Game.Shared.Network.Profile.EAddChampionError",
        "System.UInt64", "System.String", "System.Int32",
        "Game.Shared.Mechanics.EChampionClass",
        "Game.Shared.Mechanics.ERace",
        "Game.Shared.Mechanics.EGender",
        "System.Boolean",
        "Game.Shared.ResourceId", "System.Guid",
        "System.Collections.Generic.List`1#Game.Shared.ResourceId",
    ]
    def ft(tn):
        if tn not in type_names: type_names.append(tn)
        return type_names.index(tn)

    sizes = []; buf = io.BytesIO()
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";"); lf = lambda: buf.write(b"\n")

    def wf_bool(name, val):
        """Write a bool field. Client reads exactly 1 byte, no trailing ;."""
        sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
        w(name); sep(); w(str(fi)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
        w("1" if val else "0")
        sizes[fi] = buf.tell() - f_start

    def wf(name, type_name, val_hex, num_props=0):
        """Write a simple field header + value."""
        sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
        w(name); sep(); w(str(fi)); sep(); w(str(ft(type_name))); sep(); w(str(num_props)); sep()
        w(val_hex); sep()
        sizes[fi] = buf.tell() - f_start

    def wf_enum(name, enum_type, val):
        """Write an enum field with value__ sub-field."""
        sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
        w(name); sep(); w(str(fi)); sep(); w(str(ft(enum_type))); sep(); w("1"); sep()
        sizes.append(0); si = len(sizes)-1; fsub_start = buf.tell()
        w("value__"); sep(); w(str(si)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<i", val)).decode("ascii")); sep()
        sizes[si] = buf.tell() - fsub_start
        sizes[fi] = buf.tell() - f_start

    def wf_str(name, val):
        enc = val.encode("utf-8")
        sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
        w(name); sep(); w(str(fi)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
        w(str(len(enc))); sep(); buf.write(enc)
        sizes[fi] = buf.tell() - f_start

    # --- Root: AddChampionResponse, 4 sub-fields ---
    sizes.append(0)
    w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("4"); sep()

    # ChampionBits - all 17 supported fields, including the authored
    # mandatory talents needed for the initial talent-point display.
    sizes.append(0); fi = len(sizes)-1; fc_start = buf.tell()
    w("ChampionBits"); sep(); w(str(fi)); sep(); w(str(ft(type_names[1]))); sep(); w("17"); sep()
    wf_str("Name", champ_name)
    wf("Id", "System.UInt64", hexlify(struct.pack("<Q", champ_db_id)).decode("ascii"))
    wf("Level", "System.Int32", hexlify(struct.pack("<i", 1)).decode("ascii"))
    wf("CurrentXP", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
    wf_enum("ChampionClass", "Game.Shared.Mechanics.EChampionClass", cls_val)
    wf_enum("Race", "Game.Shared.Mechanics.ERace", race_val)
    wf_enum("Gender", "Game.Shared.Mechanics.EGender", gender_val)
    wf("OwnerChampionId", "System.UInt64", hexlify(struct.pack("<Q", owner_champ_id)).decode("ascii"))
    wf("LastCampaignID", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
    wf("LastDeckID", "System.UInt64", hexlify(struct.pack("<Q", resp_last_deck_id)).decode("ascii"))
    wf_bool("IsDeleted", False)
    wf("LastRespec", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
    wf("FreeRespec", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
    wf("RespecGoldCost", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
    wf_str("PetName", resp_pet_name or "")
    wf("DeckTemplateId", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))

    def wf_rid_list(name, guids):
        sizes.append(0); list_idx = len(sizes)-1; list_start = buf.tell()
        w(name); sep(); w(str(list_idx)); sep()
        w(str(ft("System.Collections.Generic.List`1#Game.Shared.ResourceId")))
        sep(); w("0"); sep(); w(str(len(guids))); sep()
        for i, guid_value in enumerate(guids):
            element_start = buf.tell(); sizes.append(0); element_idx = len(sizes)-1
            w(str(i)); sep(); w(str(element_idx)); sep()
            w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
            field_start = buf.tell(); sizes.append(0); field_idx = len(sizes)-1
            w("m_Guid"); sep(); w(str(field_idx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
            encoded_guid = str(guid_value).encode("utf-8")
            w(str(len(encoded_guid))); sep(); buf.write(encoded_guid)
            sizes[field_idx] = buf.tell() - field_start
            sizes[element_idx] = buf.tell() - element_start
        sizes[list_idx] = buf.tell() - list_start

    wf_rid_list("ChampionTalents", starting_talents)
    sizes[fi] = buf.tell() - fc_start

    # DeckBits
    sizes.append(0); fi = len(sizes)-1; fd_start = buf.tell()
    w("DeckBits"); sep(); w(str(fi)); sep(); w(str(ft(type_names[2]))); sep(); w("3"); sep()
    # The client caches this deck using the ID and later looks it up via
    # ChampionBits.LastDeckID, so this must be the persisted deck ID.
    wf("Id", "System.UInt64", hexlify(struct.pack("<Q", resp_last_deck_id)).decode("ascii"))
    wf_str("DeckName", f"{champ_name}'s Starter")
    wf("PVEChampionId", "System.UInt64", hexlify(struct.pack("<Q", champ_db_id)).decode("ascii"))
    sizes[fi] = buf.tell() - fd_start

    # Error (enum)
    wf_enum("Error", "Game.Shared.Network.Profile.EAddChampionError", 0)

    # ErrorMessage
    wf_str("ErrorMessage", "")

    sizes[0] = buf.tell()
    w(";".join(type_names)); lf()
    for i, s in enumerate(sizes):
        if i > 0: w(";")
        w(str(s))

    resp_inner = buf.getvalue()
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent AddChampionResponse (id={champ_db_id}, name={champ_name}, {len(dw_bytes)}b)")


# UpdateChampionDeckID (2187)
def handle_updatechampiondeckid_2187(handler, target, instance, data_type, reqid, comp, session_id,
                                     conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> UpdateChampionDeckID (dt=2187)")
    champ_uid = 0; deck_id = 0; auth_id = 0
    if isinstance(inner_obj, dict):
        champ_uid = int(inner_obj.get("ChampionID", 0))
        deck_id = int(inner_obj.get("LastDeckID", 0))
        auth_id = int(inner_obj.get("AuthID", 0))
    elif isinstance(inner_bytes, bytes):
        for fname, key in [(b"AuthID", "auth"),
                           (b"ChampionID", "champ"),
                           (b"LastDeckID", "deck")]:
            pos = inner_bytes.find(fname)
            if pos >= 0:
                rest = inner_bytes[pos:]; parts = rest.split(b";", 5)
                if len(parts) >= 5:
                    try:
                        val = struct.unpack("<Q", unhexlify(parts[4]))[0]
                        if key == "auth": auth_id = val
                        elif key == "champ": champ_uid = val
                        else: deck_id = val
                    except: pass
    # On reconnect the client may send this profile update before the
    # auth request. Restore the profile from its stable SAuthID so a
    # following campaign setup has the correct database owner.
    if not handler.user_profile and auth_id:
        restored_profile = db_get_user_by_client_auth_id(auth_id)
        if restored_profile is not None:
            handler.user_profile = restored_profile
        if handler.user_profile:
            handler._set_client_identity_from_profile()
            log_req(f"    Restored profile for auth id {auth_id}: "
                    f"user={handler.user_profile['id']}")
    champ_db_id = champ_uid >> 8 if champ_uid > 0xff else champ_uid
    log_req(f"    ChampionID={champ_uid} (db={champ_db_id}) LastDeckID={deck_id}")
    if handler.user_profile and champ_db_id > 0:
        db_set_champion_last_deck(
            champ_db_id, deck_id, handler.user_profile["id"], conn=_db)
        log_req(f"    Updated champion {champ_db_id} last_deck_id={deck_id}")
    # Return success response with Error=Ok
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Profile.UpdateChampionDeckIDResponse",
         "Game.Shared.Network.Profile.EUpdateChampionDeckIDError"],
        [("Error", "enum1", ("Game.Shared.Network.Profile.EUpdateChampionDeckIDError", 0))])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({"issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent UpdateChampionDeckID response ({len(dw_bytes)}b)")


# UpdateChampionTalents (2037)
def handle_updatechampiontalents_2037(handler, target, instance, data_type, reqid, comp, session_id,
                                      conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> UpdateChampionTalents (dt=2037)")
    # Parse ChampionId (ulong) + Talents (List<ResourceId> GUIDs) from the
    # request. The client always sends the champion's full talent list, so
    # we just persist it — no server-side defaults needed.
    champ_id = None
    talents = []
    if isinstance(inner_bytes, bytes):
        import re as _r
        m = _r.search(rb"ChampionId;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});", inner_bytes)
        if m:
            try:
                champ_id = struct.unpack('<Q', bytes.fromhex(m.group(1).decode()))[0]
            except Exception:
                champ_id = None
        tpos = inner_bytes.find(b"Talents")
        if tpos >= 0:
            for gm in _r.finditer(
                    rb"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
                    inner_bytes[tpos:]):
                talents.append(gm.group(1).decode())
    champ_name = ""
    race_val = 1; cls_val = 3; gender_val = 1; level_val = 1
    last_deck = 0
    if champ_id is not None:
        db_update_champion_talents(
            champ_id, handler.user_profile["id"] if handler.user_profile else 0,
            json.dumps(talents), conn=_db)
        _db.commit()
        log_req(f"    Saved talents for champion {champ_id}: {talents}")
        crow = db_champion_profile(champ_id, conn=_db)
        if crow:
            champ_name = crow[0] or ""
            race_val = crow[1] or 1
            cls_val = crow[2] or 3
            gender_val = crow[3] or 1
            last_deck = crow[5] or 0
            level_val = crow[4] or 1
            pet_name = crow[6] or ""
    else:
        log_req(f"    Could not parse champion id / talents ({len(talents)} guids found)")

    # Build the response with a full ChampionBits (incl. ChampionTalents)
    # so the client's HandleTalentsUpdate can update the champion.
    type_names = [
        "Game.Client.Network.Profile.UpdateChampionTalentsResponse",
        "Game.Shared.Domain.champion_bits",
        "Game.Shared.Network.Profile.EUpdateChampionTalentsError",
        "System.UInt64", "System.String", "System.Int32",
        "Game.Shared.Mechanics.EChampionClass",
        "Game.Shared.Mechanics.ERace",
        "Game.Shared.Mechanics.EGender",
        "System.Boolean",
        "Game.Shared.ResourceId", "System.Guid",
        "System.Collections.Generic.List`1#Game.Shared.ResourceId",
    ]
    def ft(tn):
        if tn not in type_names: type_names.append(tn)
        return type_names.index(tn)

    sizes = []; buf = io.BytesIO()
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";"); lf = lambda: buf.write(b"\n")

    def wf_bool(name, val):
        sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
        w(name); sep(); w(str(fi)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
        w("1" if val else "0")
        sizes[fi] = buf.tell() - f_start

    def wf(name, type_name, val_hex, num_props=0):
        sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
        w(name); sep(); w(str(fi)); sep(); w(str(ft(type_name))); sep(); w(str(num_props)); sep()
        w(val_hex); sep()
        sizes[fi] = buf.tell() - f_start

    def wf_enum(name, enum_type, val):
        sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
        w(name); sep(); w(str(fi)); sep(); w(str(ft(enum_type))); sep(); w("1"); sep()
        sizes.append(0); si = len(sizes)-1; fsub_start = buf.tell()
        w("value__"); sep(); w(str(si)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<i", val)).decode("ascii")); sep()
        sizes[si] = buf.tell() - fsub_start
        sizes[fi] = buf.tell() - f_start

    def wf_str(name, val):
        enc = val.encode("utf-8")
        sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
        w(name); sep(); w(str(fi)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
        w(str(len(enc))); sep(); buf.write(enc)
        sizes[fi] = buf.tell() - f_start

    def wf_rid_list(name, guids):
        """List<ResourceId> — each element uses ResourceId.m_Guid."""
        sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
        w(name); sep(); w(str(fi)); sep(); w(str(ft("System.Collections.Generic.List`1#Game.Shared.ResourceId"))); sep(); w("0"); sep()
        w(str(len(guids))); sep()
        for i, g in enumerate(guids):
            felem = buf.tell(); slot = len(sizes); sizes.append(0)
            w(str(i)); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
            fsub = buf.tell(); sizes.append(0)
            w("m_Guid"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
            gb = g.encode("utf-8")
            w(str(len(gb))); sep(); buf.write(gb)
            sizes[-1] = buf.tell() - fsub
            sizes[slot] = buf.tell() - felem
        sizes[fi] = buf.tell() - f_start

    # --- Root: UpdateChampionTalentsResponse, 3 sub-fields ---
    sizes.append(0)
    w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("3"); sep()

    # ChampionBits (17 fields incl. ChampionTalents)
    sizes.append(0); fi = len(sizes)-1; fc = buf.tell()
    w("ChampionBits"); sep(); w(str(fi)); sep(); w(str(ft(type_names[1]))); sep(); w("17"); sep()
    wf_str("Name", champ_name)
    wf("Id", "System.UInt64", hexlify(struct.pack("<Q", champ_id or 0)).decode("ascii"))
    wf("Level", "System.Int32", hexlify(struct.pack("<i", level_val)).decode("ascii"))
    wf("CurrentXP", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
    wf_enum("ChampionClass", "Game.Shared.Mechanics.EChampionClass", cls_val)
    wf_enum("Race", "Game.Shared.Mechanics.ERace", race_val)
    wf_enum("Gender", "Game.Shared.Mechanics.EGender", gender_val)
    wf("OwnerChampionId", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
    wf("LastCampaignID", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
    wf("LastDeckID", "System.UInt64", hexlify(struct.pack("<Q", last_deck)).decode("ascii"))
    wf_bool("IsDeleted", False)
    wf("LastRespec", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
    wf("FreeRespec", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
    wf("RespecGoldCost", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
    wf_str("PetName", pet_name)
    wf("DeckTemplateId", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
    wf_rid_list("ChampionTalents", talents)
    sizes[fi] = buf.tell() - fc

    # Error (enum) + ErrorMessage (string)
    wf_enum("Error", "Game.Shared.Network.Profile.EUpdateChampionTalentsError", 0)
    wf_str("ErrorMessage", "")

    sizes[0] = buf.tell()
    w(";".join(type_names)); lf()
    for i, s in enumerate(sizes):
        if i > 0: w(";")
        w(str(s))

    resp_inner = buf.getvalue()
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({"issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent UpdateChampionTalents response ({len(dw_bytes)}b)")


# ServiceProfile — UpgradeExtendedArtOnCard (2185)
def handle_upgradeextendedartoncard_2185(handler, target, instance, data_type, reqid, comp, session_id,
                                         conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> UpgradeExtendedArtOnCard (dt=2185)")
    card_instance_id = int(inner_obj.get("CardInstanceId", 0))
    user_id = handler.user_profile["id"] if handler.user_profile else 0
    row = db_card_instance_art(
        user_id, card_instance_id, conn=_db)
    if not row:
        log_req(f"    Card instance {card_instance_id} not found for user {user_id}")
        resp_inner = encode_objfmt_response(
            ["Game.Client.Network.Profile.UpgradeExtendedArtOnCardResponse",
             "System.UInt64", "System.UInt64"],
            [("StardustInventoryId", "ulong", 0),
             ("CardInstanceId", "ulong", card_instance_id)]
        )
    elif row[2]:
        log_req(f"    Card instance {card_instance_id} already extended art")
        resp_inner = encode_objfmt_response(
            ["Game.Client.Network.Profile.UpgradeExtendedArtOnCardResponse",
             "System.UInt64", "System.UInt64"],
            [("StardustInventoryId", "ulong", 0),
             ("CardInstanceId", "ulong", card_instance_id)]
        )
    else:
        db_set_card_instance_extended_art(
            user_id, card_instance_id, conn=_db)
        _db.commit()
        log_req(f"    Upgraded card instance {card_instance_id} to extended art")
        resp_inner = encode_objfmt_response(
            ["Game.Client.Network.Profile.UpgradeExtendedArtOnCardResponse",
             "System.UInt64", "System.UInt64"],
            [("StardustInventoryId", "ulong", card_instance_id),
             ("CardInstanceId", "ulong", card_instance_id)]
        )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent UpgradeExtendedArtOnCard ({len(dw_bytes)}b)")



LEGACY_HANDLERS = {
    2091: handle_createnewdeckfromtemplate_2091,
    2089: handle_addnewdeck_2089,
    2095: handle_updatedeck_2095,
    2043: handle_getplayercardidlist_2043,
    2035: handle_deletechampion_2035,
    2033: handle_addchampion_2033,
    2187: handle_updatechampiondeckid_2187,
    2037: handle_updatechampiontalents_2037,
    2185: handle_upgradeextendedartoncard_2185,
}
