"""Combat keyword adapters such as Rage and socketed combat modifiers."""

from ..statics import apply_rage


def apply_rage_keyword(db, session, handler, game, pl_t, ai_t, bstate, uid):
    """Apply the card's metadata-derived Rage value after it attacks."""
    return apply_rage(db, session, handler, game, pl_t, ai_t, bstate, uid)

