"""Shared game-session, card and PVP persistence API.

Practice, campaign battles and tournament PVP all use the same game-session
and card tables. This module groups that shared storage boundary; it is not a
second database file.
"""

from db import (
    connect,
    db_next_session_instance, db_save_session, db_get_session,
    db_get_session_by_name, db_get_sessions, db_remove_session,
    db_cleanup_ended_sessions, db_game_session_pids, db_game_champion,
    db_game_deck_cards, db_game_draw_cards, db_game_get_hand,
    db_game_card_type, db_game_shuffle_deck, db_insert_game_card,
    db_clear_session_cards, db_move_cards_to_hand, db_delete_game_session,
    db_get_card_type, db_set_card_location, db_discard_card,
    db_champion_template_health,
)

__all__ = [name for name in globals() if name.startswith("db_")]
