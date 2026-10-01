"""Native port of the client's Replica card modification.

``Card.HandleReplicaMods`` turns a created/transformed card into a Replica:
the permanent-data ``IsReplica``/``OriginalIsReplica`` markers are set, the
card type gains Artifact, troops gain the Robot subtype, the Replica subtype
is always added, thresholds are cleared and Unique is removed.  The
modification lives in the persisted card row and permanent-buffs payload, so
type/subtype/threshold projections observe the same view the client does.
"""

from __future__ import annotations

import json
import threading

_REPLICA_LOCK = threading.RLock()


def replica_projection(db, session_id, uid):
    """Return ``(card_type_bits, subtype)`` from the persisted replica row.

    Callers use this after :func:`apply_replica_mods` so the client's
    ``CardUpdated`` renders the Replica type/subtype instead of the base
    template.
    """
    import game_engine
    from pvp_db import (db_card_mutation_field, db_card_source_info)
    info = db_card_source_info(session_id, int(uid), conn=db)
    if not info:
        return None, ""
    card_type = game_engine.card_type_from_db(info[1])
    try:
        buffs = json.loads(db_card_mutation_field(
            session_id, int(uid), "permanent_buffs", conn=db) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    subtype = str(buffs.get("subtype") or "") if isinstance(buffs, dict) else ""
    return card_type, subtype


def project_replica(context, uid):
    """Push the post-replica type/subtype ``CardUpdated`` for one card."""
    import game_engine
    from pvp_db import db_card_source_info
    from .runtime_helpers import card_collection_for_location, owner_uid
    info = db_card_source_info(
        context.session.session_id, int(uid), conn=context.db)
    if not info:
        return False
    card_type, subtype = replica_projection(
        context.db, context.session.session_id, int(uid))
    scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
    _tpl, _rendered, _name, cost, attack, defense, gems = \
        context.handler._card_full_data(context.game, scid, info[0])
    recipient = owner_uid(info[3], context.player_uid, context.ai_uid,
                          context.bstate)
    context.game.push_card_updated(
        scid, recipient, card_collection_for_location(info[2]), card_type,
        template_id=info[0], cost=cost, attack=attack, defense=defense,
        gems=gems, sub_type=subtype)
    return True


def apply_replica_mods(context, uid, template_guid=None):
    """Project ``Card.HandleReplicaMods`` onto one session card."""
    from pvp_db import (db_apply_replica_mods, db_card_mutation_field,
                        db_card_source_info, db_card_template_threshold_subtype,
                        db_copy_template_payload)
    uid = int(uid)
    info = db_card_source_info(context.session.session_id, uid,
                               conn=context.db)
    if not info:
        return False
    guid = str(template_guid or info[0] or "").lower()
    payload = db_copy_template_payload(guid, conn=context.db)
    card_type = str((payload[0] if payload else info[1]) or "Troop")
    with _REPLICA_LOCK:
        try:
            buffs = json.loads(db_card_mutation_field(
                context.session.session_id, uid, "permanent_buffs",
                conn=context.db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            buffs = {}
        parts = [part for part in card_type.split("|") if part]
        if "Artifact" not in parts:
            parts.append("Artifact")
        row = db_card_template_threshold_subtype(
            context.session.session_id, uid, conn=context.db)
        subtype = str((row[1] if row else "") or "")
        subtype_parts = [part for part in subtype.split() if part]
        if "Troop" in parts and "Robot" not in subtype_parts:
            subtype_parts.append("Robot")
        if "Replica" not in subtype_parts:
            subtype_parts.append("Replica")
        attrs = buffs.get("int_attrs")
        if not isinstance(attrs, dict):
            attrs = {}
        attrs["IsReplica"] = 1
        attrs["OriginalIsReplica"] = 1
        buffs["int_attrs"] = attrs
        buffs["subtype"] = " ".join(subtype_parts)
        buffs["thresholds"] = []
        db_apply_replica_mods(
            context.session.session_id, uid, "|".join(parts),
            json.dumps(buffs), conn=context.db)
    return True
