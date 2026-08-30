"""Service handler dispatch table for Hex TCG private server.

Data types are routed to handler functions via ``_SERVICE_TABLE``. Each handler
receives the full context::

    def handle_foo(dt: int, handler, target, instance, reqid, comp, session_id,
                   conh, inner_obj, inner_bytes, log_req):

To add a new service, register it here.  Only same-line merge conflicts with the
dict entries are expected when two contributors add different services simultaneously.
"""

# fmt: off
_SERVICE_TABLE = {
    # Mail
    60001: "services.mail.handle_send_mail",
    60007: "services.mail.handle_get_unread",
    60002: "services.mail.handle_receive",
    60005: "services.mail.handle_mark_read",
    60004: "services.mail.handle_send",
    60006: "services.mail.handle_claim",
    60003: "services.mail.handle_delete",
    60008: "services.mail.handle_mark_sent_delete",

    # Profile
    9001:  "services.profile.handle_ping",
    80000: "services.profile.handle_get_profile",
    2043:  "services.profile.handle_get_card_list",
    2091:  "services.profile.handle_get_decks",
    2081:  "services.profile.handle_create_deck",
    2089:  "services.profile.handle_save_deck",
    2095:  "services.profile.handle_get_deck_info",
    2187:  "services.profile.handle_delete_deck",
    2037:  "services.profile.handle_create_champion",
    2185:  "services.profile.handle_delete_champion",
    2033:  "services.profile.handle_get_champions",
    2035:  "services.profile.handle_delete_champion",
    2127:  "services.profile.handle_open_card_pack",
    2049:  "services.profile.handle_spin_wheel",

    # Escrow / Store
    6011: "services.store.handle_purchase",
    6013: "services.store.handle_redeem",
    6009: "services.store.handle_get_items",

    # Auction House
    50004: "services.auction.handle_query",
    50010: "services.auction.handle_query_items_info",

    # Matchmaking
    70022: "services.matchmaking.handle_ladder_find_match",

    # LoadBalancer
    22013: "services.game_session.handle_start_session",
    22019: "services.game_session.handle_join_session",
    22015: "services.game_session.handle_find_session",
    22017: "services.game_session.handle_ready_setup",
    22021: "services.game_session.handle_ready_events",
    22029: "services.game_session.handle_start_encounter",
    22027: "services.game_session.handle_ready_to_start",
    22031: "services.game_session.handle_leave_session",
    22025: "services.game_session.handle_leave_session",
    3027:  "services.game_session.handle_end_session",

    # Campaign
    110000: "services.campaign.handle_request",
    150000: "services.campaign.handle_fra",

    # Battle transactions
    3029: "services.battle.handle_transaction",

    # Arena
    10001: "services.arena.handle_request",
    10003: "services.arena.handle_request",
    10005: "services.arena.handle_request",
    10007: "services.arena.handle_request",
    10009: "services.arena.handle_request",
    10011: "services.arena.handle_request",
    10013: "services.arena.handle_request",
    10019: "services.arena.handle_request",
    10027: "services.arena.handle_request",
    10029: "services.arena.handle_request",
    10033: "services.arena.handle_request",

    # Social / Friends
    2149: "services.social.handle_add_friend",
    2157: "services.social.handle_accept_friend_request",
    2159: "services.social.handle_ignore_friend_request",
    2161: "services.social.handle_remove_friend",
    2163: "services.social.handle_ignore_player",
    2165: "services.social.handle_unignore_player",
}
# fmt: on


def dispatch(data_type: int):
    """Return the fully-qualified handler module path for *data_type*, or None."""
    return _SERVICE_TABLE.get(data_type)
