"""RulesPort-owned underground Tunneling lifecycle."""

from __future__ import annotations

import base64
import json
import struct

import game_engine

from .counter_effects import TUNNELING_COUNTER_GUID, change_counter

SURFACE_ABILITY_GUID = "f2d6797b-1a24-4c3d-9239-a27a2e0de0ff"


def _tac_hash(name):
    import hashlib
    digest = bytearray(hashlib.md5(str(name).encode("ascii")).digest()[:4])
    if digest[0] == 0:
        digest[0] = 1
    if digest[3] == 0:
        digest[3] = 1
    return bytes(reversed(digest))


def tunneling_value(db, template_guid, persisted_int_attrs=None):
    """Read the authored Tunneling value and apply instance int attributes."""
    for key, value in (persisted_int_attrs or {}).items():
        if str(key).lower() == "tunneling":
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0
    try:
        from gamedata import DEFAULT_RECORD_STORE
        record = DEFAULT_RECORD_STORE.get("CardTemplate", str(template_guid))
        tac = record.field("m_SerializedTAC", {}) if record else {}
        data = tac.get("data", "") if isinstance(tac, dict) else ""
        raw = base64.b64decode(data, validate=True)
        offset = raw.find(_tac_hash("Tunneling"))
        if offset >= 0 and offset + 8 <= len(raw):
            return max(0, int(struct.unpack_from("<i", raw, offset + 4)[0]))
    except (AttributeError, TypeError, ValueError, struct.error,
            base64.binascii.Error):
        pass
    return 0


def surface_source_is_underground(db, session, card_uid):
    """Return whether a queued Surface source still has a legal location."""
    if card_uid is None:
        return False
    try:
        card_uid = int(card_uid)
    except (TypeError, ValueError):
        return False
    from pvp_db import db_card_location
    return str(db_card_location(
        session.session_id, card_uid, conn=db) or "").lower() == "underground"


def resolve_surface(context, card_uid):
    """Resolve the client-built-in Surface ability through free card play.

    Surface is not an authored Records graph.  Its client definition grants
    the buried card Speed for the turn and plays that same card for free, so
    it must enter the normal free-play chain rather than the metadata resolver.
    """
    if not surface_source_is_underground(context.db, context.session, card_uid):
        return "surface: source is no longer underground"
    from pvp_db import db_card_chain_info, db_card_owner_zone_state
    try:
        card_uid = int(card_uid)
    except (TypeError, ValueError):
        return "surface: invalid source"
    row = db_card_chain_info(
        context.session.session_id, card_uid, conn=context.db)
    owner_zone = db_card_owner_zone_state(
        context.session.session_id, card_uid, conn=context.db)
    if not row or not owner_zone:
        return "surface: source not found"
    from .host_mutations import queue_free_played_card
    return queue_free_played_card(
        context.handler, context.game, context.session, context.db,
        context.player_uid, context.ai_uid, context.bstate, card_uid,
        int(owner_zone[0] or 0), row[0], row[1])


def _saved_int_attrs(db, session_id, card_uid):
    from pvp_db import db_card_mutation_field
    try:
        value = json.loads(db_card_mutation_field(
            session_id, int(card_uid), "permanent_buffs", conn=db) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        value = {}
    return value.get("int_attrs", {}) if isinstance(value, dict) else {}


def advance(context, owner_id):
    """Increment each owned underground card's public Tunneling counter."""
    from pvp_db import db_underground_card_rows

    owner_id = int(owner_id or 0)
    changed = []
    rows = db_underground_card_rows(
        context.session.session_id, owner_id, conn=context.db)
    for card_uid, template_guid in rows:
        persisted = _saved_int_attrs(context.db,
                                     context.session.session_id, card_uid)
        threshold = tunneling_value(context.db, template_guid, persisted)
        if threshold <= 0:
            continue
        # Ordinary cards store counters in permanent_buffs; use the same
        # typed mutation primitive as every other native counter operation.
        from pvp_db import db_card_mutation_field
        try:
            payload = json.loads(db_card_mutation_field(
                context.session.session_id, int(card_uid),
                "permanent_buffs", conn=context.db) or "{}")
            old = int((payload.get("counters", {}) or {}).get(
                "tunneling", 0) or 0) if isinstance(payload, dict) else 0
        except (TypeError, ValueError, json.JSONDecodeError):
            old = 0
        _old, new = change_counter(
            context, int(card_uid), "tunneling", TUNNELING_COUNTER_GUID,
            1, "add")
        changed.append((int(card_uid), old, new, threshold))
    return changed


def queue_surfaces(context, owner_id):
    """Queue at most one thresholded underground card for native resolution."""
    from . import chain
    from pvp_db import db_underground_card_rows, db_card_mutation_field

    owner_id = int(owner_id or 0)
    queued = []
    if not chain.empty(context.bstate):
        return queued
    for card_uid, template_guid in db_underground_card_rows(
            context.session.session_id, owner_id, conn=context.db):
        if not chain.empty(context.bstate):
            break
        threshold = tunneling_value(
            context.db, template_guid,
            _saved_int_attrs(context.db, context.session.session_id, card_uid))
        try:
            payload = json.loads(db_card_mutation_field(
                context.session.session_id, int(card_uid),
                "permanent_buffs", conn=context.db) or "{}")
            count = int((payload.get("counters", {}) or {}).get(
                "tunneling", 0) or 0) if isinstance(payload, dict) else 0
        except (TypeError, ValueError, json.JSONDecodeError):
            count = 0
        if threshold <= 0 or count < threshold:
            continue
        change_counter(context, int(card_uid), "tunneling",
                       TUNNELING_COUNTER_GUID, 0, "set")
        instance_id = int(context.bstate.get("_next_instance_id", 1))
        context.bstate["_next_instance_id"] = instance_id + 1
        descriptor = {
            "kind": "ability", "ability_guid": SURFACE_ABILITY_GUID,
            "source_uid": int(card_uid), "target_uid": int(card_uid),
            "source_owner_uid": owner_id, "instance_id": instance_id,
        }
        chain.push(context.bstate, descriptor)
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        context.game.push_ability_on_chain(
            scid, game_engine.ResourceId.from_str(SURFACE_ABILITY_GUID),
            ability_instance_id=instance_id, target_card_ids=[scid],
            ignores_chain=False)
        # The native scheduler owns chain ordering and the response window;
        # the compatibility descriptor above is only its durable projection.
        # Registering nowhere else left the Surface on the client's chain with
        # no native item to resolve, so the buried card never surfaced even
        # though the counter had already been spent.
        port = getattr(context.session, "_rules_port_session", None)
        register = getattr(port, "queue_projected_chain", None)
        if callable(register):
            # C# responds to an authored chain item with the active player
            # first; the Surface belongs to the player whose turn it is.
            register(descriptor, owner_id,
                     first_player_id=(getattr(port, "active_player_id", None)
                                      or owner_id))
        queued.append(int(card_uid))
    return queued
