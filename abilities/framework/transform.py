"""Transform an existing card into a new template (e.g. Spiritbound Spy -> Phantom).

The card KEEPS its card_uid but template_guid / card_type / base stats / abilities /
attributes are copied from the NEW card template. Persistent stat mods carry over.
"""

import game_engine


def transform_card(handler, game, session, pl_t, ai_t, card_uid, new_template_guid,
                   keep_zone=False, bstate=None):
    """Transform an existing card instance into *new_template_guid*.

    With ``keep_zone=True`` the card stays in its current zone (used by
    all-zones transforms like Incantation of Righteousness -> Sentinels of
    Light); otherwise it re-enters the warzone."""
    from ._shared import _log
    import db as _dbmod

    from pvp_db import (db_card_owner_location_position,
                        db_card_owner_zone_state,
                        db_copy_template_payload, db_transform_card_instance,
                        db_card_attribute_value)
    row = db_card_owner_location_position(
        session.session_id, int(card_uid), conn=_dbmod._db)
    if not row:
        return
    owner_user_id, cur_zone, old_position = row[0], row[1], int(row[2] or 0)
    state_row = db_card_owner_zone_state(
        session.session_id, int(card_uid), conn=_dbmod._db)
    old_state = int(state_row[2] or 0) if state_row else 0
    trow = db_copy_template_payload(new_template_guid, conn=_dbmod._db)
    ctype = trow[0] if trow else "Troop"
    canonical_abilities = trow[1] if trow and trow[1] else "[]"
    canonical_attributes = int(trow[2] or 0) if trow else 0
    if keep_zone:
        new_state = old_state
        new_location = cur_zone
        new_position = old_position
    else:
        # Transforming does not untap the card.  Preserve the complete
        # instance state (Tapped/Attacking/StartedATurn...) across a zone-
        # preserving transform such as Caterpillar -> Cocoon; only the
        # template and combat representation change.
        new_state = old_state
        new_location = "warzone"
        new_position = 0
    db_transform_card_instance(
        session.session_id, int(card_uid), new_template_guid, ctype,
        canonical_abilities, canonical_attributes, new_location, new_position,
        new_state, conn=_dbmod._db)
    _dbmod._db.commit()
    handler._sync_instance_card_data(session, card_uid, new_template_guid)
    scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
    # _sync_instance_card_data also reapplies attributes granted by the new
    # ability list.  Send the effective instance value, not only the template
    # value, so a transformed card cannot lose a legitimate granted keyword.
    effective_attributes = int(db_card_attribute_value(
        session.session_id, int(card_uid), "card_attributes",
        conn=_dbmod._db) or canonical_attributes)
    tpl_guid, ct, name, cost, atk, def_, gem = handler._card_full_data(
        game, scid, new_template_guid, None)
    # CardUpdated refreshes the cached CardRepresentation, but the client
    # uses CardTransformed to run the transform animation and replace the
    # card's template in its live view.  Sending only CardUpdated/CardMoved
    # leaves a transform such as Caterpillar -> Cocoon looking like a stale
    # card until another full refresh arrives.
    game.push_card_transformed(scid, new_template_guid, gems=gem)
    from ._shared import owner_uid
    owner = owner_uid(owner_user_id, pl_t, ai_t, bstate)
    zone = game_engine.ECardCollections.Warzone
    if keep_zone:
        zone = {
            "hand": game_engine.ECardCollections.Hand,
            "deck": game_engine.ECardCollections.Deck,
            "discard": game_engine.ECardCollections.Discard,
            "void": game_engine.ECardCollections.Void,
            "CastSpells": game_engine.ECardCollections.CastSpells,
            "warzone": game_engine.ECardCollections.Warzone,
        }.get(cur_zone, game_engine.ECardCollections.Warzone)
    game.push_card_updated(scid, owner, zone, ct,
                           attack=atk, defense=def_, cost=cost,
                           template_id=tpl_guid, gems=gem, state=new_state,
                           # CardUpdated is authoritative for the client
                           # representation.  Without the new template's
                           # attributes, a keyword such as Defensive remains
                           # in the client's cached CardRepresentation.
                           attributes=effective_attributes)
    game.push_card_moved(scid, owner, zone,
                         game_engine.ECardLocations.Top, new_position)
    if bstate is not None:
        from .triggers import resolve_triggers

        # The transformed card can have a self trigger, while other cards
        # listen for the separate "another card transforms" event. Both are
        # authored event types and use the normal trigger-condition path.
        resolve_triggers(
            _dbmod._db, handler, game, session, pl_t, ai_t, bstate,
            "CardTransformedEvent", int(card_uid),
            source_owner_uid=owner_user_id,
            event_source_collection=cur_zone,
            event_destination_collection=new_location,
            event_previous_state=old_state)
        resolve_triggers(
            _dbmod._db, handler, game, session, pl_t, ai_t, bstate,
            "CardTransformsEvent", int(card_uid),
            source_owner_uid=owner_user_id,
            event_source_collection=cur_zone,
            event_destination_collection=new_location,
            event_previous_state=old_state)
    _log(f"    Transformed {hex(card_uid)} -> {name} ({new_template_guid[:8]}) in {new_location}")
    return int(card_uid)
