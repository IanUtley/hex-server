"""Store / Escrow service handlers: GetStoreItems, PurchaseItem, RedeemCode."""

import json, gzip, os

import db as _db_module
from db import _db, log_req, db_get_store_items
from db import (db_update_resources, db_record_purchase, db_add_inventory,
                db_save_deck, db_redeem_code, db_send_email)
from encoder import encode_objfmt_response, compress_gzip, encode_datawrapper, encode_store_response

# Store deck data — loaded from JSON files in Hex root
_ROOT = os.path.dirname(os.path.dirname(__file__))

_STORE_DECK_CARDS = {}
_path = os.path.join(_ROOT, "starter_deck_cards.json")
if os.path.exists(_path):
    with open(_path) as f:
        _STORE_DECK_CARDS = json.load(f)

_STARTER_DECKS = {}
_path2 = os.path.join(_ROOT, "generated", "starter_decks.json")
if os.path.exists(_path2):
    with open(_path2) as f:
        _STARTER_DECKS = json.load(f)


def _grant_deck_to_player(user_id, cards, deck_name, handler=None, conn=None):
    """Grant cards to collection + card_instances, create deck, push to client.
    
    The deck's cards column stores instance IDs (integer), not template GUIDs,
    so the client's deck loader can resolve all 23 fields of card_instance_bits.
    """
    connection = conn or _db
    max_id = connection.execute(
        "SELECT COALESCE(MAX(instance_id), 5000) + 1 FROM card_instances "
        "WHERE user_id=?", (user_id,)).fetchone()[0]
    cid = max_id if max_id else 5001
    instance_ids = []
    for card_guid, count in cards:
        existing = connection.execute(
            "SELECT quantity FROM collections WHERE user_id=? AND card_template_id=?",
            (user_id, card_guid)).fetchone()
        if existing:
            connection.execute(
                "UPDATE collections SET quantity=quantity+? "
                "WHERE user_id=? AND card_template_id=?",
                (count, user_id, card_guid))
        else:
            connection.execute(
                "INSERT INTO collections (user_id, card_template_id, quantity) "
                "VALUES (?,?,?)", (user_id, card_guid, count))
        for _ in range(count):
            _db.execute(
                "INSERT OR IGNORE INTO card_instances "
                "(user_id, instance_id, template_guid) VALUES (?,?,?)",
                (user_id, cid, card_guid))
            instance_ids.append(cid)
            cid += 1
    cards_json = json.dumps(instance_ids)
    deck_db_id = db_save_deck(user_id, deck_name, cards_json, conn=conn)
    log_req(f"    Granted {deck_name}: {len(instance_ids)} cards, deck_id={deck_db_id}")
    if handler is not None:
        handler.push_cards_to_client()
    if conn is None:
        connection.commit()
    return deck_db_id


def apply_purchase(conn, user_id, item_id, quantity):
    """Apply a store purchase using the caller-owned transaction."""
    row = conn.execute(
        "SELECT name, price, currency, template_guid, store_tab "
        "FROM store_items WHERE id=?", (int(item_id),)).fetchone()
    if not row:
        item_name, cost, currency_type, template_guid, store_tab = (
            "Unknown", 100, "Gold", "", "")
    else:
        item_name, cost, currency_type, template_guid, store_tab = row
    balance_column = "platinum" if currency_type == "Platinum" else "gold"
    balance = conn.execute(
        f"SELECT {balance_column} FROM users WHERE id=?", (user_id,)
    ).fetchone()[0]
    remaining = balance - (int(cost) * int(quantity))
    conn.execute(
        f"UPDATE users SET {balance_column}=? WHERE id=?",
        (remaining, user_id))

    granted_list = []
    deck_granted = False
    if template_guid:
        granted_list = [(template_guid, 1000 + int(item_id), int(quantity))]
    if store_tab == "collectordeck" and template_guid:
        deck_data = _STORE_DECK_CARDS.get(template_guid)
        if deck_data:
            _grant_deck_to_player(
                user_id, deck_data["cards"], deck_data.get("name", item_name),
                conn=conn)
            deck_granted = True
        elif item_name:
            race = item_name.replace(" Starter Deck", "").replace(
                " Starter", "").strip()
            if race in _STARTER_DECKS:
                _grant_deck_to_player(
                    user_id, _STARTER_DECKS[race]["cards"],
                    f"{race} Starter Deck", conn=conn)
                deck_granted = True
    db_record_purchase(
        user_id, item_name, template_guid, int(cost) * int(quantity),
        currency_type, conn=conn)
    if template_guid:
        db_add_inventory(user_id, template_guid, int(quantity), conn=conn)
        conn.execute(
            "UPDATE player_inventory SET client_item_uid=? "
            "WHERE user_id=? AND template_guid=? AND client_item_uid=0",
            (1000 + int(item_id), user_id, template_guid))
    return {
        "remaining": remaining,
        "currency": currency_type,
        "item_name": item_name,
        "template_guid": template_guid,
        "quantity": int(quantity),
        "item_id": int(item_id),
        "deck_granted": deck_granted,
        "granted_list": granted_list,
    }


def apply_redeem(conn, user_id, redeem_code):
    """Apply a redemption and its system email in one transaction."""
    result = db_redeem_code(redeem_code, conn=conn)
    if result is None:
        return {"gold": 0, "platinum": 0, "redeemed": False}
    row = conn.execute(
        "SELECT gold, platinum FROM users WHERE id=?", (user_id,)).fetchone()
    new_gold = row[0] + result["gold"]
    new_platinum = row[1] + result["platinum"]
    db_update_resources(
        user_id, gold=new_gold, platinum=new_platinum, conn=conn)
    parts = []
    if result["gold"] > 0:
        parts.append(f"{result['gold']:,} Gold")
    if result["platinum"] > 0:
        parts.append(f"{result['platinum']:,} Platinum")
    reward_desc = ", ".join(parts)
    db_send_email(
        user_id, f"Code Redeemed: {redeem_code}",
        f"You have successfully redeemed code '{redeem_code}' and "
        f"received {reward_desc}.\n\nThank you for playing Hex: Shards of Fate!",
        "SYSTEM", conn=conn)
    return {
        "gold": result["gold"], "platinum": result["platinum"],
        "redeemed": True,
    }


def handle_get_items(handler, target, instance, reqid, comp, session_id, conh,
                     SERVICE_MAIL_UID, **_kw):
    """ServiceEscrow — GetStoreItems (6009)."""
    items = db_get_store_items()
    log_req(f">>> Respond GetStoreItems -> {len(items)} items")
    resp_inner = encode_store_response(items)
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 6009, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceEscrow.{SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent GetStoreItems response ({len(dw_bytes)}b)")


def handle_purchase(handler, target, instance, reqid, comp, session_id, conh,
                    inner_obj, SERVICE_MAIL_UID, **_kw):
    """ServiceEscrow — PurchaseItem (6011)."""
    quantity = inner_obj.get("Quanity", 1)
    item_id = inner_obj.get("Id", 1)
    log_req(f">>> PurchaseItem: qty={quantity} id={item_id}")

    from application.commands import PurchaseStoreItemCommand
    result = handler._application.execute(PurchaseStoreItemCommand(
        user_id=handler.user_profile["id"],
        item_id=int(item_id),
        quantity=int(quantity),
    )).value
    item_name = result["item_name"]
    currency_type = result["currency"]
    template_guid = result["template_guid"]
    remaining = result["remaining"]
    granted_list = result["granted_list"]
    p = handler.user_profile
    if currency_type == "Platinum":
        p["platinum"] = remaining
    else:
        p["gold"] = remaining

    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Escrow.PurchaseItemResponse",
         "System.Int32", "System.String",
         "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
         "Game.Shared.Domain.inventory_bits",
         "Game.Shared.ResourceId", "System.Guid", "System.DateTime",
         "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits"],
        [("RemainingCurrency", "int", remaining),
         ("TransactionCurrencyType", "string", currency_type),
         ("PurchasedInventory", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", 0)),
         ("PurchasedDeckBits", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0)),
         ("PurchasedCards", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
         ("GrantedInventory", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
           len(granted_list), granted_list)),
         ("GrantedDeckBits", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0)),
         ("GrantedCards", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
         ("ConsumedInventory", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", 0)),
         ("ConsumedCards", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
         ("CurrencyInventory", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", 0))]
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 6011, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceEscrow.{SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send_and_cache({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes, 6011, reqid, target, instance)
    if result["deck_granted"]:
        handler.push_cards_to_client()
    if template_guid:
        handler.push_inventory_to_client(
            qty=result["quantity"], template_guid=template_guid,
            item_id=1000 + result["item_id"])
    log_req(f"    Sent PurchaseItem response: remaining={remaining} {currency_type}")


def handle_redeem(handler, target, instance, reqid, comp, session_id, conh,
                  inner_obj, SERVICE_MAIL_UID, **_kw):
    """ServiceEscrow — RedeemCode (6013)."""
    redeem_code = inner_obj.get("RedeemCode", "")
    log_req(f">>> RedeemCode: code='{redeem_code}'")

    p = handler.user_profile
    from application.commands import RedeemCodeCommand
    result = handler._application.execute(RedeemCodeCommand(
        user_id=p["id"], code=redeem_code)).value
    gold_delta = result["gold"]
    plat_delta = result["platinum"]
    if result["redeemed"]:
        p["gold"] += gold_delta
        p["platinum"] += plat_delta
        log_req(f"    RedeemCode success: gold+{gold_delta} plat+{plat_delta}")
    else:
        log_req(f"    RedeemCode invalid: {redeem_code}")
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Escrow.RedeemCodeResponse",
         "System.Collections.Generic.List`1#Game.Shared.ResourceId",
         "Game.Shared.ResourceId", "System.Guid", "System.Int32",
         "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits"],
        [("ItemTemplateIds", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)),
         ("GoldDelta", "int", gold_delta),
         ("PlatinumDelta", "int", plat_delta),
         ("CardBits", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
         ("StarterDecksBits", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0))]
    )

    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 6013, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceEscrow.{SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send_and_cache({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes, 6013, reqid, target, instance)
    log_req(f"    Sent RedeemCode response ({len(dw_bytes)}b)")
