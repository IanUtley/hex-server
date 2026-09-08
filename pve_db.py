"""Campaign and PVE persistence API.

PVE and PVP share sessions, cards and event storage. This module therefore
owns campaign/FRA-specific queries rather than pretending PVE has a separate
physical database.
"""

from db import (
    db_get_arena_state, db_get_arena_fight_history, db_get_fra_challenge,
    db_get_active_fra_challenges, db_roll_fra_start_challenge,
    db_record_arena_fight, db_update_arena_state, db_clear_fra_challengers,
    db_create_fra_challengers, db_get_fra_challengers,
    db_get_fra_public_base_encounter, db_delete_game_session,
    db_get_player_champion_guid, db_get_champion_guid,
    db_champion_template_health,
)

__all__ = [name for name in globals() if name.startswith("db_")]
