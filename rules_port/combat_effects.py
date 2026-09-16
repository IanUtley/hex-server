"""Native combat keyword mutations owned by RulesPort."""

from __future__ import annotations

import json


def apply_rage(context, card_uid):
    """Apply authored Rage to an attacker as a permanent attack modifier."""
    from pvp_db import db_card_mutation_field, db_set_card_mutation_field
    from .static_rules import effective_stats

    uid = int(card_uid)
    rage = int(effective_stats(
        context.db, context.session.session_id, context.bstate, uid)[4] or 0)
    if rage <= 0:
        return 0
    raw = db_card_mutation_field(
        context.session.session_id, uid, "permanent_buffs", conn=context.db)
    try:
        buffs = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if not isinstance(buffs, dict):
        buffs = {}
    buffs["atk"] = int(buffs.get("atk", 0) or 0) + rage
    db_set_card_mutation_field(
        context.session.session_id, uid, "permanent_buffs",
        json.dumps(buffs, separators=(",", ":"), sort_keys=True), conn=context.db)
    context.db.commit()
    context._push_modifier_card(uid)
    return rage


def block(context):
    """Port of ``BlockEffectTemplate``.

    Despite the name, the C# leaf relocates the effect's mapped target to
    ``m_DestinationLocation`` (it is the generic "move card to a new zone"
    leaf).  The native branch previously imported a function that did not
    exist, raising ImportError.  Route through the shared zone transition.
    """
    return context.move_card_to_zone()
