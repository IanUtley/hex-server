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
