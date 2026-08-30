"""Keyword lifecycle entry points.

Deploy, Inspire, and Deathcry share the CardEnteredZone/AsEntersPlay event
pipeline.  This facade gives callers a keyword-oriented home while the
existing trigger engine remains the compatibility implementation.
"""

from ..triggers import resolve_enters_play_triggers


def resolve_deploy_and_inspire(db, handler, game, session, pl_t, ai_t, bstate,
                               card_uid, owner_id, event_uid=0):
    """Resolve Deploy and Inspire abilities for an entering troop."""
    return resolve_enters_play_triggers(
        db, handler, game, session, pl_t, ai_t, bstate, card_uid,
        owner_id, event_uid)

