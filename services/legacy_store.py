"""Store, card-pack, chest-spin and reward requests from the legacy chain.

Moved out of hconnect_server.HCPHandler._handle_service_request_legacy.
"""

from application.response_encoding import compress_gzip
from application.response_encoding import encode_datawrapper
from application.response_encoding import encode_get_store_items_response
from application.response_encoding import encode_objfmt_response
from az1_pack import CAMPAIGN_PACK_CONFIGS
from az1_pack import STARDUST_REPLACEMENT_RATE
from az1_pack import generate_campaign_pack
from binascii import hexlify
from db import STARDUST_TEMPLATES
from db import _db
from db import log_req
from profile_db import db_add_card
from profile_db import db_chest_probabilities
from profile_db import db_consume_inventory
from profile_db import db_create_treasure_chest
from profile_db import db_insert_card_instance
from profile_db import db_inventory_item
from profile_db import db_next_card_instance_for_user
from profile_db import db_pack_set_info
from profile_db import db_redeem_code
from profile_db import db_send_email
from profile_db import db_update_resources
from services.card_pools import full_set_pool as _full_set_pool
from services.card_pools import generate_booster as _generate_booster
from services.card_pools import generate_starter_deck as _generate_starter_deck
from services.card_pools import load_card_templates as _load_card_templates
from services.chests import _grant_pack_inventory_rewards
from services.chests import _open_client_chests
from services.chests import _resolve_client_chest
import io
import json
import random
import re
import struct


# ServiceEscrow — GetStoreItems (6009)
def handle_getstoreitems_6009(handler, target, instance, data_type, reqid, comp, session_id,
                              conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    _hc.log_req(">>> Respond GetStoreItems -> 1 booster pack")
    resp_inner = _hc.encode_get_store_items_response()
    resp_body = _hc.compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = _hc.encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceEscrow.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str,
        "target": target,
        "instance": instance,
        "reqid": resp_reqid,
        "c": comp,
        "conh": conh,
        "sid": handler.sid,
    }, dw_bytes)
    _hc.log_req(f"    Sent GetStoreItems response ({len(dw_bytes)}b)")


# ServiceEscrow — RedeemCode (6013)
def handle_redeemcode_6013(handler, target, instance, data_type, reqid, comp, session_id,
                           conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    redeem_code = inner_obj.get("RedeemCode", "")
    _hc.log_req(f">>> RedeemCode: code='{redeem_code}'")
    
    p = handler.user_profile
    result = _hc.db_redeem_code(redeem_code, p["id"])
    if not result["redeemed"]:
        _hc.log_req(f"    RedeemCode invalid: {redeem_code}")
        resp_inner = _hc.encode_objfmt_response(
            ["Game.Client.Network.Escrow.RedeemCodeResponse",
             "System.Collections.Generic.List`1#Game.Shared.ResourceId",
             "Game.Shared.ResourceId",
             "System.Guid",
             "System.Int32",
             "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
             "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits",
             "Game.Shared.Network.Escrow.ERedeemCodeError", "System.String"],
            [("ItemTemplateIds", "coll", ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)),
             ("GoldDelta", "int", 0),
             ("PlatinumDelta", "int", 0),
             ("CardBits", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
             ("StarterDecksBits", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0)),
             ("Error", "enum1", ("Game.Shared.Network.Escrow.ERedeemCodeError", 1)),
             ("ErrorMessage", "string", result["error_message"])]
        )
        resp_body = _hc.compress_gzip(resp_inner) if comp else resp_inner
        resp_reqid = reqid | 1
        dw_bytes = _hc.encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
        issuer_str = f"0.0.0.0.ServiceEscrow.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
        handler.scnt += 1
        handler.send({
            "issuer": issuer_str, "target": target, "instance": instance,
            "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
        }, dw_bytes)
        _hc.log_req(f"    Sent RedeemCode (invalid) response ({len(dw_bytes)}b)")
    else:
        gold_delta = result["gold"]
        plat_delta = result["platinum"]
        from services.store import grant_vendor_code_cards
        card_grants = grant_vendor_code_cards(p["id"], redeem_code)
        new_gold = p["gold"] + gold_delta
        new_plat = p["platinum"] + plat_delta
        _hc.db_update_resources(p["id"], gold=new_gold, platinum=new_plat)
        p["gold"] = new_gold
        p["platinum"] = new_plat
    
        parts = []
        if gold_delta > 0:
            parts.append(f"{gold_delta:,} Gold")
        if plat_delta > 0:
            parts.append(f"{plat_delta:,} Platinum")
        if card_grants:
            parts.append(f"{len(card_grants)} PvP card templates x4")
        reward_desc = ", ".join(parts)
    
        _hc.db_send_email(p["id"],
            f"Code Redeemed: {redeem_code}",
            f"You have successfully redeemed the code '{redeem_code}' and received {reward_desc}.\n\nThank you for playing Hex: Shards of Fate!",
            "SYSTEM")
    
        _hc.log_req(f"    RedeemCode success: gold+{gold_delta} plat+{plat_delta}")
        if card_grants:
            handler.push_cards_to_client()
    
        resp_inner = _hc.encode_objfmt_response(
            ["Game.Client.Network.Escrow.RedeemCodeResponse",
             "System.Collections.Generic.List`1#Game.Shared.ResourceId",
             "Game.Shared.ResourceId",
             "System.Guid",
             "System.Int32",
             "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
             "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits",
             "Game.Shared.Network.Escrow.ERedeemCodeError", "System.String"],
            [("ItemTemplateIds", "coll", ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)),
             ("GoldDelta", "int", gold_delta),
             ("PlatinumDelta", "int", plat_delta),
             ("CardBits", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
             ("StarterDecksBits", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0)),
             ("Error", "enum1", ("Game.Shared.Network.Escrow.ERedeemCodeError", 0)),
             ("ErrorMessage", "string", "")]
        )
        resp_body = _hc.compress_gzip(resp_inner) if comp else resp_inner
        resp_reqid = reqid | 1
        dw_bytes = _hc.encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
        issuer_str = f"0.0.0.0.ServiceEscrow.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
        handler.scnt += 1
        handler.send_and_cache({
            "issuer": issuer_str, "target": target, "instance": instance,
            "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
        }, dw_bytes, data_type, reqid, target, instance)
        _hc.log_req(f"    Sent RedeemCode (ok) response ({len(dw_bytes)}b)")


# OpenCardPack (2127)
def handle_opencardpack_2127(handler, target, instance, data_type, reqid, comp, session_id,
                             conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    item_id = inner_obj.get("ItemId", "{}")
    open_amount = inner_obj.get("OpenAmount", 1)
    _hc.log_req(f">>> OpenCardPack: id={item_id} amount={open_amount}")
    
    # Extract template guid from client raw bytes
    # ResourceId uses DataMember field "m_Guid" (not "guid")
    pack_guid = "0382f729-7710-432b-b761-13677982dcd2"
    if isinstance(inner_bytes, bytes) and b"ItemId" in inner_bytes:
        pos = inner_bytes.find(b"ItemId")
        if pos >= 0:
            rest = inner_bytes[pos:]
            for fname in (b"m_Guid", b"guid"):
                gpos = rest.find(fname)
                if gpos >= 0:
                    rest2 = rest[gpos:]
                    parts = rest2.split(b";", 5)
                    if len(parts) >= 5 and parts[4].isdigit():
                        try:
                            guid_len = int(parts[4])
                            tail = b";".join(rest2.split(b";", 5)[:5])
                            remain = rest2[len(tail)+1:]
                            if len(remain) >= guid_len:
                                pack_guid = remain[:guid_len].decode("ascii")
                                break
                        except:
                            pass
    _hc.log_req(f">>> OpenCardPack: pack_guid={pack_guid} amount={open_amount}")

    # Map store item GUID to card set GUID — query pack_set_map table
    row = _hc.db_pack_set_info(pack_guid, conn=_hc._db)
    if row:
        set_guid, is_full_set, is_primal = row
    else:
        set_guid = pack_guid  # fallback: treat pack GUID as set GUID
        is_full_set = 0
        is_primal = 0
    _hc.log_req(f"    pack_guid={pack_guid} → set_guid={set_guid} full={is_full_set} primal={is_primal}")

    # Validate player owns this pack
    pack_error = None
    pack_error_code = 0
    if handler.user_profile:
        existing = _hc.db_inventory_item(
            handler.user_profile["id"], pack_guid, conn=_hc._db)
        if not existing or existing[1] < open_amount:
            pack_error = "NotEnoughInventory"
            pack_error_code = 5
            _hc.log_req(f"    Pack not in inventory! (have={existing[1] if existing else 0}, want={open_amount})")
    
    # Generate card instances for the pack
    card_templates = _hc._load_card_templates()
    all_cards = []
    pack_inventory_rewards = []
    pack_inventory_updates = []
    campaign_pack_config = _hc.CAMPAIGN_PACK_CONFIGS.get(pack_guid.lower())
    starter_pack = None
    if not pack_error:
        try:
            starter_pack = _hc._generate_starter_deck(
                card_templates, pack_guid)
        except ValueError as exc:
            pack_error = "CardCreationFailed"
            pack_error_code = 3
            _hc.log_req(f"    Starter deck contents unavailable: {exc}")
    if not pack_error:
        if campaign_pack_config:
            # Adventure Zone campaign packs are not normal PvP
            # boosters.  Each has two common PvP cards, one weighted
            # PvE card, and two equipment/Stardust slots from its
            # configured set range.
            from gamedata import DEFAULT_RECORD_STORE
            for _ in range(open_amount):
                reward = _hc.generate_campaign_pack(
                    card_templates, DEFAULT_RECORD_STORE, random,
                    pack_config=campaign_pack_config)
                all_cards.extend(reward.cards)
                pack_inventory_rewards.extend(
                    (guid, "equipment")
                    for guid in reward.equipment_guids)
                pack_inventory_rewards.extend(
                    (_hc.STARDUST_TEMPLATES[rarity.lower()], "stardust")
                    for rarity in reward.stardust_rarities)
                _hc.log_req(
                    f"    Campaign pack: {len(all_cards)} cards, "
                f"{sum(kind == 'equipment' for _, kind in pack_inventory_rewards)} "
                f"equipment, "
                    f"{sum(kind == 'stardust' for _, kind in pack_inventory_rewards)} "
                    f"stardust (replacement rate={_hc.STARDUST_REPLACEMENT_RATE:.0%})")
        elif starter_pack:
            for _ in range(open_amount):
                all_cards.extend(starter_pack["cards"])
            _hc.log_req(
                f"    Starter deck {starter_pack['definition']['name']}: "
                f"{len(starter_pack['cards'])} cards x {open_amount}")
        elif is_full_set:
            # Grant every PVP card from the set (4x each)
            pool = card_templates.get(set_guid, [])
            pool = _hc._full_set_pool(pool)
            for _ in range(open_amount):
                for c in pool:
                    for _ in range(4):  # 4x each card
                        all_cards.append((c[0], c[1], c[3], c[4], c[5]))
            _hc.log_req(f"    Full set: {len(pool)} unique cards x4 = {len(all_cards)} total")
        else:
            for _ in range(open_amount):
                all_cards.extend(_hc._generate_booster(card_templates, set_guid))
    
    # Track instance IDs for response encoding (fallback: 5000+)
    card_instance_ids = [5000 + i for i in range(len(all_cards))]
    chest_rarity = None
    chest_db_id = None
    
    # Persist generated cards to user's collection
    if handler.user_profile and not pack_error:
        for guid, _, _, _, _ in all_cards:
            _hc.db_add_card(handler.user_profile["id"], guid)
    
        # Remove the booster pack from inventory
        consumed = _hc.db_consume_inventory(
            handler.user_profile["id"], pack_guid, open_amount, conn=_hc._db)
        if consumed:
            pack_client_uid, new_qty = consumed
            _hc.log_req(f"    Consumed {open_amount}x pack {pack_guid} from inventory (remaining={max(0, new_qty)})")
            # Push inventory update so client pack count decrements
            if pack_client_uid:
                handler.push_inventory_to_client(qty=max(0, new_qty), template_guid=pack_guid, item_id=pack_client_uid)
    
        # Persist new cards to card_instances and get their instance IDs
        # ``db_next_card_instance_for_user`` already returns the
        # first unused instance ID.  Do not subtract one here: the
        # old calculation reused the current maximum, and the
        # INSERT OR IGNORE in ``db_insert_card_instance`` then kept
        # whatever card happened to occupy that ID.  That made the
        # first card in an opened pack appear as an unrelated card.
        next_id = _hc.db_next_card_instance_for_user(
            handler.user_profile["id"], conn=_hc._db)
        new_card_data = []
        for i, (guid, name, cost, atk, def_) in enumerate(all_cards):
            cid = next_id + i
            _hc.db_insert_card_instance(
                handler.user_profile["id"], cid, guid, conn=_hc._db)
            new_card_data.append((guid, name, cost, atk, def_, cid, 0))
            card_instance_ids[i] = cid  # Update for response encoding
        _hc._db.commit()
        _hc.log_req(f"    Created {len(new_card_data)} card_instances IDs {new_card_data[0][5]}-{new_card_data[-1][5]}")

        # OpenCardPackResponse.NewCardInstances is the authoritative
        # collection update.  Do not also send these same instances
        # through ProfileGenericUpdate: PlayerProfile.LoadCardsFromBits
        # adds them to the client's CardList, and the duplicate update
        # causes the client's dictionary to throw "same key already
        # exists" while opening the pack.

        # Campaign equipment and Stardust are InventoryItemData, not
        # card_instance_bits.  Persist them with stable per-player
        # inventory IDs and push the same quantity update to the
        # client.  Keeping the rewards in player_inventory also makes
        # them survive a reconnect.
        if pack_inventory_rewards:
            pack_inventory_updates = _hc._grant_pack_inventory_rewards(
                handler, pack_inventory_rewards)
            _hc._db.commit()

        # Generate treasure chest for normal boosters.  Campaign
        # packs already contain their two equipment/Stardust slots;
        # they do not award the standard booster chest.
        import random as _rand
        probs = ([] if campaign_pack_config or starter_pack else
                 _hc.db_chest_probabilities(conn=_hc._db))
        if probs:
            total_weight = sum(p[1] for p in probs)
            roll = _rand.randint(1, total_weight)
            cumulative = 0
            for rarity, weight in probs:
                cumulative += weight
                if roll <= cumulative:
                    chest_rarity = rarity
                    break
            if chest_rarity:
                chest_db_id = _hc.db_create_treasure_chest(
                    handler.user_profile["id"], set_guid, chest_rarity,
                    conn=_hc._db)
                _hc.log_req(f"    Generated {chest_rarity} chest id={chest_db_id}")
    
    # Encode OpenCardPackResponse using builder
    from objfmt_builder import ObjFmtBuilder
    b = ObjFmtBuilder("Game.Client.Network.Profile.OpenCardPackResponse")

    # NewCardInstances
    card_count = len(all_cards)
    b.begin_list("NewCardInstances",
        "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", card_count)
    for i, (tguid, tname, cost, atk, def_) in enumerate(all_cards):
        b.begin_element(i, "Game.Shared.Domain.card_instance_bits", 6)
        b.card_fields(tguid, card_instance_ids[i])

    # NewGemInstances (empty)
    b.begin_list("NewGemInstances",
        "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", 0)

    # NewChestInstances
    if chest_rarity and chest_db_id:
        b.begin_list("NewChestInstances",
            "System.Collections.Generic.List`1#Game.Shared.Domain.chest_bits", 1)
        b.begin_element(0, "Game.Shared.Domain.chest_bits", 8)
        chest_map = {"Common":0, "Uncommon":1, "Rare":2, "Legendary":3, "Primal":4}
        b.chest_fields(chest_map.get(chest_rarity, 0), 2, set_guid, 9000 + chest_db_id)
        # Override BoosterPackType with set_guid
    else:
        b.begin_list("NewChestInstances",
            "System.Collections.Generic.List`1#Game.Shared.Domain.chest_bits", 0)

    # Error + ErrorMessage
    error_val = pack_error_code
    error_msg = ("Not enough inventory" if pack_error_code == 5 else
                 "Card creation failed" if pack_error_code == 3 else "")
    b.field_enum("Error", "Game.Shared.Network.Profile.EOpenCardPackError", error_val)
    b.field_str("ErrorMessage", error_msg)

    resp_inner = b.finish(5)
    resp_body = _hc.compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = _hc.encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    for template_guid, item_uid, quantity in pack_inventory_updates:
        handler.push_inventory_to_client(
            qty=quantity, template_guid=template_guid,
            item_id=item_uid)
    _hc.log_req(f"    Sent OpenCardPack response ({len(all_cards)} cards, {len(dw_bytes)}b)")


# SpinWheelOfFate (2049) — chest spinning
def handle_chest_spinning_2049(handler, target, instance, data_type, reqid, comp, session_id,
                               conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    chest_id_raw = inner_obj.get("ChestID", "0")
    # ObjFmt may decode the integer ChestID as either a Python int or
    # a string, depending on the request shape.  Normalize both forms
    # at the protocol boundary instead of calling string-only APIs.
    try:
        chest_uid = int(chest_id_raw)
    except (TypeError, ValueError):
        chest_uid = 0
    _hc.log_req(f">>> SpinWheelOfFate: ChestID={chest_uid}")

    chest = (_hc._resolve_client_chest(handler.user_profile["id"], chest_uid)
             if handler.user_profile else None)
    if chest is None:
        _hc.log_req(f"    Invalid chest or no profile")
        resp_inner = b""
    else:
        summary = _hc._open_client_chests(handler, [chest_uid])
        reward_card_bits = summary["cards"]
        _hc.log_req(f"    Spun chest {chest.chest_type} uid={chest_uid}, "
                f"awarded {len(reward_card_bits)} cards")

        # Push reward cards and inventory changes to client
        handler._send_cards_chunk(reward_card_bits)
        for template_guid, item_uid, quantity in summary["inventory_updates"]:
            handler.push_inventory_to_client(
                qty=quantity, template_guid=template_guid,
                item_id=item_uid)
        for template_guid, opened_uid, remaining in summary["opened"]:
            if remaining:
                handler.push_inventory_to_client(
                    qty=remaining, template_guid=template_guid,
                    item_id=opened_uid)
            else:
                handler._send_inventory_updated(
                    template_guid, opened_uid, quantity=0)

        # Encode SpinWheelOfFateResponse
        # Chest (chest_bits, marked opened), RewardCards (List<card_instance_bits>), RewardItems (empty)
        rtn = ["Game.Client.Network.Profile.SpinWheelOfFateResponse",
               "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
               "Game.Shared.Domain.card_instance_bits",
               "Game.Shared.ResourceId", "System.Guid", "System.UInt64",
               "System.Boolean", "System.String", "System.Int32",
               "System.UInt32",
               "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
               "Game.Shared.Domain.chest_bits"]
        def rft(tn):
            if tn not in rtn: rtn.append(tn)
            return rtn.index(tn)
        rsizes = []; rbuf = io.BytesIO()
        rw = lambda s: rbuf.write(s.encode("utf-8"))
        rsep = lambda: rbuf.write(b";")
        rsizes.append(0)
        rw(""); rsep(); rw("0"); rsep(); rw(str(rft(rtn[0]))); rsep(); rw("6"); rsep()

        # Chest field (chest_bits, 8 props, WasOpened=true)
        rc = rbuf.tell(); rsizes.append(0)
        rw("Chest"); rsep(); rw("1"); rsep(); rw(str(rft("Game.Shared.Domain.chest_bits"))); rsep(); rw("0"); rsep()
        rw("1"); rsep()
        rfe = rbuf.tell(); rsizes.append(0); reidx = len(rsizes)-1
        rw("0"); rsep(); rw(str(reidx)); rsep(); rw(str(rft("Game.Shared.Domain.chest_bits"))); rsep(); rw("8"); rsep()
        # ChestRarity
        rf1 = rbuf.tell(); rsizes.append(0)
        rw("ChestRarity"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
        cmap = {"Common":0, "Uncommon":1, "Rare":2, "Legendary":3, "Primal":4, "Promo":5}
        rw(hexlify(struct.pack("<i", cmap.get(chest.chest_type, 0))).decode("ascii")); rsep()
        rsizes[-1] = rbuf.tell() - rf1
        # WOFSpinStatus = 0
        rf2 = rbuf.tell(); rsizes.append(0)
        rw("WOFSpinStatus"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
        rw(hexlify(struct.pack("<i", 0)).decode("ascii")); rsep()
        rsizes[-1] = rbuf.tell() - rf2
        # BoosterPackType
        rf3 = rbuf.tell(); rsizes.append(0); rti = len(rsizes)-1
        rw("BoosterPackType"); rsep(); rw(str(rti)); rsep(); rw(str(rft("Game.Shared.ResourceId"))); rsep(); rw("1"); rsep()
        rgs = rbuf.tell(); rsizes.append(0); rgi = len(rsizes)-1
        rw("guid"); rsep(); rw(str(rgi)); rsep(); rw(str(rft("System.Guid"))); rsep(); rw("0"); rsep()
        booster_type_guid = chest.template_guid or chest.set_guid
        rw("36"); rsep(); rbuf.write(booster_type_guid.encode())
        rsizes[rgi] = rbuf.tell() - rgs; rsizes[rti] = rbuf.tell() - rf3
        # WasOpened = true
        rf4 = rbuf.tell(); rsizes.append(0)
        rw("WasOpened"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Boolean"))); rsep(); rw("0"); rsep()
        rw("1"); rsizes[-1] = rbuf.tell() - rf4
        # InventoryId
        rf5 = rbuf.tell(); rsizes.append(0)
        rw("InventoryId"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.UInt64"))); rsep(); rw("0"); rsep()
        rw(hexlify(struct.pack("<Q", chest_uid)).decode("ascii")); rsep()
        rsizes[-1] = rbuf.tell() - rf5
        # PromoID
        rf6 = rbuf.tell(); rsizes.append(0)
        rw("PromoID"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.UInt32"))); rsep(); rw("0"); rsep()
        rw(hexlify(struct.pack("<I", 0)).decode("ascii")); rsep()
        rsizes[-1] = rbuf.tell() - rf6
        # TempateID
        rf7 = rbuf.tell(); rsizes.append(0); rti2 = len(rsizes)-1
        rw("TempateID"); rsep(); rw(str(rti2)); rsep(); rw(str(rft("Game.Shared.ResourceId"))); rsep(); rw("1"); rsep()
        rgs2 = rbuf.tell(); rsizes.append(0); rgi2 = len(rsizes)-1
        rw("guid"); rsep(); rw(str(rgi2)); rsep(); rw(str(rft("System.Guid"))); rsep(); rw("0"); rsep()
        rw("36"); rsep(); rbuf.write(booster_type_guid.encode())
        rsizes[rgi2] = rbuf.tell() - rgs2; rsizes[rti2] = rbuf.tell() - rf7
        # Vendor
        rf8 = rbuf.tell(); rsizes.append(0)
        rw("Vendor"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
        rw(hexlify(struct.pack("<i", 0)).decode("ascii")); rsep()
        rsizes[-1] = rbuf.tell() - rf8
        rsizes[reidx] = rbuf.tell() - rfe
        rsizes[1] = rbuf.tell() - rc

        # RewardCards (List<card_instance_bits>, encoded with reward cards)
        rfc = rbuf.tell(); rsizes.append(0)
        rw("RewardCards"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft(rtn[1]))); rsep(); rw("0"); rsep()
        rw(str(len(reward_card_bits))); rsep()
        for ri, (guid, name, cost, atk, def_, cid, iext) in enumerate(reward_card_bits):
            rfe2 = rbuf.tell(); rsizes.append(0); rei = len(rsizes)-1
            rw(str(ri)); rsep(); rw(str(rei)); rsep(); rw(str(rft(rtn[2]))); rsep(); rw("6"); rsep()
            # Id
            f1c = rbuf.tell(); rsizes.append(0)
            rw("Id"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.UInt64"))); rsep(); rw("0"); rsep()
            rw(hexlify(struct.pack("<Q", cid)).decode("ascii")); rsep()
            rsizes[-1] = rbuf.tell() - f1c
            # TemplateID
            f2c = rbuf.tell(); rsizes.append(0); rti3 = len(rsizes)-1
            rw("TemplateID"); rsep(); rw(str(rti3)); rsep(); rw(str(rft("Game.Shared.ResourceId"))); rsep(); rw("1"); rsep()
            rgs3 = rbuf.tell(); rsizes.append(0); rgi3 = len(rsizes)-1
            rw("guid"); rsep(); rw(str(rgi3)); rsep(); rw(str(rft("System.Guid"))); rsep(); rw("0"); rsep()
            rw("36"); rsep(); rbuf.write(guid.encode())
            rsizes[rgi3] = rbuf.tell() - rgs3; rsizes[rti3] = rbuf.tell() - f2c
            for bname in ("IsFoil", "IsExtended", "IsNotTradeable"):
                tb = rbuf.tell(); rsizes.append(0)
                rw(bname); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Boolean"))); rsep(); rw("0"); rsep()
                rw("0"); rsizes[-1] = rbuf.tell() - tb
            # EscrowStatus
            te = rbuf.tell(); rsizes.append(0)
            rw("EscrowStatus"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.String"))); rsep(); rw("0"); rsep()
            enc = b"Clean"; rw(str(len(enc))); rsep(); rbuf.write(enc)
            rsizes[-1] = rbuf.tell() - te
            rsizes[rei] = rbuf.tell() - rfe2
        rsizes[-1] = rbuf.tell() - rfc

        # RewardItems (empty list)
        fri = rbuf.tell(); rsizes.append(0)
        rw("RewardItems"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft(rtn[10]))); rsep(); rw("0"); rsep()
        rw("0"); rsep()
        rsizes[-1] = rbuf.tell() - fri

        # GoldAward (int 0)
        fga = rbuf.tell(); rsizes.append(0)
        rw("GoldAward"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
        rw(hexlify(struct.pack("<i", 0)).decode("ascii")); rsep()
        rsizes[-1] = rbuf.tell() - fga

        # SpinEntryColors (List<int>, 3 entries — random symbols for slot reels)
        import random as _rand3
        fsc = rbuf.tell(); rsizes.append(0)
        rw("SpinEntryColors"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Collections.Generic.List`1#System.Int32"))); rsep(); rw("0"); rsep()
        colors = [_rand3.randint(0, 2) for _ in range(3)]
        rw(str(len(colors))); rsep()
        for ci, cv in enumerate(colors):
            fec = rbuf.tell(); rsizes.append(0); eci = len(rsizes)-1
            rw(str(ci)); rsep(); rw(str(eci)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
            rw(hexlify(struct.pack("<i", cv)).decode("ascii")); rsep()
            rsizes[eci] = rbuf.tell() - fec
        rsizes[-1] = rbuf.tell() - fsc

        # SpinEntrySymbols (List<int>, 3 entries)
        fss = rbuf.tell(); rsizes.append(0)
        rw("SpinEntrySymbols"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Collections.Generic.List`1#System.Int32"))); rsep(); rw("0"); rsep()
        symbols = [_rand3.randint(0, 7) for _ in range(3)]
        rw(str(len(symbols))); rsep()
        for si, sv in enumerate(symbols):
            fes = rbuf.tell(); rsizes.append(0); esi = len(rsizes)-1
            rw(str(si)); rsep(); rw(str(esi)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
            rw(hexlify(struct.pack("<i", sv)).decode("ascii")); rsep()
            rsizes[esi] = rbuf.tell() - fes
        rsizes[-1] = rbuf.tell() - fss

        # Error (Ok=0) — enum struct format
        ferr = rbuf.tell(); rsizes.append(0)
        rw("Error"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("Game.Shared.Network.Profile.ESpinWheelOfFateError"))); rsep(); rw("1"); rsep()
        ferrv = rbuf.tell(); rsizes.append(0)
        rw("value__"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
        rw(hexlify(struct.pack("<i", 0)).decode("ascii")); rsep()
        rsizes[-1] = rbuf.tell() - ferrv; rsizes[-2] = rbuf.tell() - ferr

        # ErrorMessage (empty string)
        fem = rbuf.tell(); rsizes.append(0)
        rw("ErrorMessage"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.String"))); rsep(); rw("0"); rsep()
        rw("0"); rsep()
        rsizes[-1] = rbuf.tell() - fem

        rsizes[0] = rbuf.tell()
        rw(";".join(rtn))
        for i, s in enumerate(rsizes):
            if i > 0: rw(";")
            rw(str(s))
        resp_inner = rbuf.getvalue()

    if not resp_inner:
        resp_inner = _hc.encode_objfmt_response(
            ["Game.Client.Network.Profile.SpinWheelOfFateResponse"],
            []
        )
    resp_body = _hc.compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = _hc.encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    _hc.log_req(f"    Sent SpinWheelOfFate response ({len(dw_bytes)}b)")


# OpenChest (2129) — direct opening for NoSpin promo chests and for
# InventoryTreasureChest items granted as inventory (Crayburn Castle
# packs, the AZ1/AZ2 campaign boosters).  SpinWheelOfFate (2049) is
# reserved for chests with a spin sequence.
def handle_reserved_for_chests_with_a_2129(handler, target, instance, data_type, reqid, comp, session_id,
                                           conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    raw_chest_ids = inner_obj.get("ChestIds", [])
    if isinstance(raw_chest_ids, str):
        try:
            raw_chest_ids = json.loads(raw_chest_ids)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_chest_ids = re.findall(r"\d+", raw_chest_ids)
    if not isinstance(raw_chest_ids, (list, tuple)):
        raw_chest_ids = [raw_chest_ids]
    requested_ids = []
    for raw_id in raw_chest_ids:
        try:
            chest_uid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if chest_uid > 0:
            requested_ids.append(chest_uid)
    _hc.log_req(f">>> OpenChest: ids={requested_ids}")

    error_val = 0
    error_message = ""
    summary = None
    if not handler.user_profile:
        error_val = 1  # InvalidChestID
        error_message = "Invalid chest ID"
    else:
        summary = _hc._open_client_chests(handler, requested_ids)
        if requested_ids and not summary["opened"]:
            error_val = 1
            error_message = "Invalid chest ID"

    valid_chest_ids = [uid for _t, uid, _r in summary["opened"]] \
        if summary else []
    inventory_template_ids = summary["inventory_template_ids"] \
        if summary else []
    card_template_ids = summary["card_template_ids"] if summary else []
    from objfmt_builder import ObjFmtBuilder
    b = ObjFmtBuilder("Game.Client.Network.Profile.OpenChestResponse")
    b.field_ulong_list("validChestIds", valid_chest_ids)
    b.field_resource_id_list("inventoryTemplateIDs",
                             inventory_template_ids)
    b.field_resource_id_list("cardTemplateIDs", card_template_ids)
    b.field_int("goldAcquired", 0)
    b.field_int("platinumAcquired", 0)
    b.field_enum(
        "Error", "Game.Shared.Network.Profile.EOpenChestError",
        error_val)
    b.field_str("ErrorMessage", error_message)
    resp_inner = b.finish(7)
    resp_body = _hc.compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = _hc.encode_datawrapper(
        resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = (
        f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}."
        f"ServicePlayer.{handler.client_uid}.{resp_reqid}")
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    _hc.log_req(
        f"    Sent OpenChest response ({len(card_template_ids)} cards, "
        f"{len(dw_bytes)}b)")
    # OpenChestResponse only drives the reward display.  Collection
    # cards, equipment/Stardust rewards and the consumed chest entry
    # arrive through the normal profile events, just as they do for the
    # other pack-opening paths.
    if summary:
        if summary["cards"]:
            handler._send_cards_chunk(summary["cards"])
        for template_guid, item_uid, quantity in summary["inventory_updates"]:
            handler.push_inventory_to_client(
                qty=quantity, template_guid=template_guid,
                item_id=item_uid)
        for template_guid, chest_uid, remaining in summary["opened"]:
            if remaining:
                # An inventory chest can be a stack; keep the entry
                # with its authoritative remaining count.
                handler.push_inventory_to_client(
                    qty=remaining, template_guid=template_guid,
                    item_id=chest_uid)
            else:
                handler._send_inventory_updated(
                    template_guid, chest_uid, quantity=0)



LEGACY_HANDLERS = {
    6009: handle_getstoreitems_6009,
    6013: handle_redeemcode_6013,
    2127: handle_opencardpack_2127,
    2049: handle_chest_spinning_2049,
    2129: handle_reserved_for_chests_with_a_2129,
}
