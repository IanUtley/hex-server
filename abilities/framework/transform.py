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

    row = _dbmod._db.execute(
        "SELECT user_id, card_state, location, position FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session.session_id, int(card_uid))).fetchone()
    if not row:
        return
    owner_user_id, old_state = row[0], int(row[1] or 0)
    cur_zone = row[2]
    old_position = int(row[3] or 0)
    trow = _dbmod._db.execute(
        "SELECT card_type, abilities_json, attributes FROM card_templates "
        "WHERE guid=?", (new_template_guid,)).fetchone()
    ctype = trow[0] if trow else "Troop"
    canonical_abilities = trow[1] if trow and trow[1] else "[]"
    canonical_attributes = int(trow[2] or 0) if trow else 0
    if keep_zone:
        new_state = old_state
        new_location = cur_zone
        new_position = old_position
    else:
        new_state = old_state & game_engine.ECardStates.StartedATurnOnYourSide
        new_location = "warzone"
        new_position = 0
    _dbmod._db.execute(
        "UPDATE game_cards SET template_guid=?, card_template_id=?, card_type=?, "
        "card_abilities=?, card_attributes=?, temporary_attributes=0, "
        "temporary_buffs='{}', location=?, position=?, card_state=? "
        "WHERE session_id=? AND card_uid=?",
        (new_template_guid, new_template_guid, ctype, canonical_abilities,
         canonical_attributes, new_location, new_position, new_state,
         session.session_id, int(card_uid)))
    _dbmod._db.commit()
    handler._sync_instance_card_data(session, card_uid, new_template_guid)
    scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
    # _sync_instance_card_data also reapplies attributes granted by the new
    # ability list.  Send the effective instance value, not only the template
    # value, so a transformed card cannot lose a legitimate granted keyword.
    arow = _dbmod._db.execute(
        "SELECT card_attributes FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session.session_id, int(card_uid))).fetchone()
    effective_attributes = (int(arow[0] or 0) if arow
                            else canonical_attributes)
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
    _log(f"    Transformed {hex(card_uid)} -> {name} ({new_template_guid[:8]}) in {new_location}")
    return int(card_uid)
