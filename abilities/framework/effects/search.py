"""Search and deck-selection operations.

These operations are shared by Deathcry, Deploy, generic ability resolution,
and the PvP activation-data prompt.  They are deliberately independent of a
particular trigger keyword.
"""

import game_engine

from .._shared import card_collection_for_location, owner_uid


def move_deck_card_to_hand(game, session, db, handler, pl_t, ai_t, card_uid,
                           owner_id, bstate=None):
    """Move a searched deck card into its controller's hand.

    This is the shared tail of a ``MoveCardToZone`` effect whose destination is
    Hand.  It also emits the CardMoved/CardDrawn/CardUpdated events expected by
    the client.
    """
    row = db.execute(
        "SELECT template_guid, card_template_id FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session.session_id, int(card_uid))).fetchone()
    if not row:
        return "search deck: card not found"
    db.execute(
        "UPDATE game_cards SET location='hand', position=100 "
        "WHERE session_id=? AND card_uid=?",
        (session.session_id, int(card_uid)))
    db.commit()
    scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
    owner = owner_uid(owner_id, pl_t, ai_t, bstate)
    _tpl, ct, _n, cost, atk, def_, _g = handler._card_full_data(
        game, scid, row[0], row[1])
    game.push_card_moved(scid, owner, game_engine.ECardCollections.Hand,
                         game_engine.ECardLocations.Top, 1)
    game.push_card_drawn(scid, owner, 1)
    game.push_card_updated(scid, owner, game_engine.ECardCollections.Hand,
                           ct, template_id=row[0], cost=cost, attack=atk,
                           defense=def_)
    return f"searched deck card {hex(int(card_uid))} to hand"

