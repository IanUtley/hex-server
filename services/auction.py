"""Auction House service stubs — return empty data so the client doesn't spin."""

from encoder import encode_objfmt_response, compress_gzip, encode_datawrapper


def handle_query(handler, target, instance, reqid, comp, session_id, conh,
                 inner_obj, inner_bytes, log_req, **_kw):
    """dt=50004 — Auction.Query: return empty search results."""
    log_req(">>> Auction.Query stub (50004)")

    # Each AuctionQueryResult has Total, Offset, Count, Listings
    empty_result = ("Game.Shared.Escrow.Messages.Auction+AuctionQueryResult",
                    [("Total", "uint", 0),
                     ("Offset", "int", 0),
                     ("Count", "int", 0),
                     ("Listings", "intlist",
                      ("System.Collections.Generic.List`1#"
                       "Game.Shared.Escrow.Messages.Auction+AuctionItem",
                       0, []))])

    resp_inner = encode_objfmt_response(
        ["Game.Shared.Escrow.Messages.Auction+Query+Response",
         "Game.Shared.Escrow.Messages.Auction+AuctionQueryResult",
         "System.Collections.Generic.List`1#"
         "Game.Shared.Escrow.Messages.Auction+AuctionItem"],
        [("CardRes", "struct", empty_result),
         ("InvenRes", "struct", empty_result)])

    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 50004, resp_body, comp, session_id)
    issuer = (f"0.0.0.0.ServiceEscrow."
              f"{handler.client_uid}") if hasattr(handler, "client_uid") else ""
    handler.scnt += 1
    handler.send({
        "issuer": issuer, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent Auction.Query stub ({len(dw_bytes)}b)")


def handle_query_items_info(handler, target, instance, reqid, comp, session_id,
                            conh, inner_obj, inner_bytes, log_req, **_kw):
    """dt=50010 — Auction.QueryAuctionItemsInfo: return empty inventory/chest lists."""
    log_req(">>> Auction.QueryAuctionItemsInfo stub (50010)")

    resp_inner = encode_objfmt_response(
        ["Game.Shared.Escrow.Messages.Auction+QueryAuctionItemsInfo+Response",
         "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.chest_bits"],
        [("InventoryRes", "intlist",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
           0, [])),
         ("ChestRes", "intlist",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.chest_bits",
           0, [])),
         ])

    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 50010, resp_body, comp, session_id)
    issuer = (f"0.0.0.0.ServiceEscrow."
              f"{handler.client_uid}") if hasattr(handler, "client_uid") else ""
    handler.scnt += 1
    handler.send({
        "issuer": issuer, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent Auction.QueryAuctionItemsInfo stub ({len(dw_bytes)}b)")
