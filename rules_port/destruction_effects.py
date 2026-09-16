"""RulesPort-owned mass-destruction effect operations."""

from __future__ import annotations

import random


def destroy_by_defense(context):
    """Destroy warzone troops using their effective defense value."""
    from pvp_db import db_warzone_troop_uids
    from .static_rules import effective_stats

    rows = db_warzone_troop_uids(
        context.session.session_id, conn=context.db)
    destroyed = 0
    for (uid,) in rows:
        defense = int(effective_stats(
            context.db, context.session.session_id, context.bstate,
            int(uid))[1] or 0)
        if random.random() > 0.10 * defense:
            # The decision (candidate set, effective defense, and random roll)
            # is port-owned; destroy() is the host mutation/event boundary.
            context.destroy(int(uid))
            destroyed += 1
    return f"destroyed {destroyed}/{len(rows)}"
