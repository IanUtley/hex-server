"""Profile, economy, inventory, mail and deck persistence APIs.

This is the profile-domain entry point. The functions are re-exported from
the legacy ``db`` facade for now so callers can migrate without a flag day;
new profile/economy code should import this module directly.
"""

from db import (
    db_add_collection, db_add_inventory, db_add_card, db_get_decks,
    db_get_inventory, db_get_or_create_user, db_get_store_item,
    db_get_store_items, db_get_user, db_get_user_by_client_auth_id,
    db_find_user_by_name,
    db_get_user_currency, db_set_user_currency, db_update_resources,
    db_save_deck, db_update_deck, db_record_purchase, db_redeem_code,
    db_send_email, db_find_mail_recipient, db_get_unread_mail_count,
    db_get_mail_list, db_get_sent_mail_list, db_delete_sent_mail,
    db_mark_all_mail_read, db_delete_all_mail, db_get_mail_by_id,
    db_claim_mail, db_mark_mail_read, db_delete_mail, db_claim_mail_for_user,
    db_set_inventory_client_uid, db_primal_pack_for,
    db_next_card_instance_for_user, db_insert_card_instance, db_get_deck_by_id,
    db_user_owns_deck,
    db_get_friends, db_get_pending_friend_requests, db_get_ignored_list,
    db_send_friend_request, db_accept_friend_request,
    db_ignore_friend_request, db_remove_friend, db_ignore_player,
    db_unignore_player,
    display_name_from_identity,
)

__all__ = [name for name in globals() if name.startswith("db_")]
