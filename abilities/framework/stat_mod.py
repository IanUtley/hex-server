"""Apply permanent or this-turn ATK/DEF modifiers to card instances.

Buffs live in two JSON columns on game_cards:
  - permanent_buffs  {"atk": N, "def": N}  — persist for the whole game
  - temporary_buffs  {"atk": N, "def": N}  — cleared at the owner's next turn
So "this turn" buffs (Guard Dog) wear off automatically at the Prep refresh.
"""

import json

import game_engine


def _load_buffs(raw):
    try:
        d = json.loads(raw or "{}")
    except (ValueError, TypeError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    out = dict(d)  # preserve counters/counter_guids/anything else
    out["atk"] = int(d.get("atk", 0) or 0)
    out["def"] = int(d.get("def", 0) or 0)
    return out


def apply_card_stat_mod(game, session, db, handler, pl_t, ai_t, card_uid,
                        atk_d, def_d, this_turn=False, bstate=None):
    """Apply an ATK/DEF modifier to a card instance, push CardUpdated.

    ``this_turn=True`` writes to temporary_buffs (worn off at next turn start);
    ``False`` writes to permanent_buffs.  Returns the card's new EFFECTIVE
    defense (<= 0 means it should die).
    """
    from ._shared import (_log, _card_state_of, owner_uid,
                          card_collection_for_location)

    row = db.execute(
        "SELECT template_guid, user_id, location FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(card_uid))).fetchone()
    if not row:
        return None
    tpl_guid, owner_id, location = row
    col = "temporary_buffs" if this_turn else "permanent_buffs"
    cur_row = db.execute(
        f"SELECT {col} FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(card_uid))).fetchone()
    cur = _load_buffs(cur_row[0] if cur_row else "{}")
    cur["atk"] += int(atk_d or 0)
    cur["def"] += int(def_d or 0)
    db.execute(
        f"UPDATE game_cards SET {col}=? WHERE session_id=? AND card_uid=?",
        (json.dumps(cur), session.session_id, int(card_uid)))
    db.commit()
    scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
    _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(game, scid, tpl_guid)
    owner = owner_uid(owner_id, pl_t, ai_t, bstate)
    card_def = game.card_defs.get(scid)
    attributes = (card_def.attributes if card_def is not None
                  else game_engine.ECardAttributes.Unknown)
    game.push_card_updated(scid, owner, card_collection_for_location(location), ct,
                           template_id=tpl_guid, attack=atk, defense=def_,
                           attributes=attributes,
                           state=_card_state_of(db, session, card_uid),
                           # Deck cards are face-down to every client. A
                           # stat change must not turn the targeted deck card
                           # face-up for the opponent (or leave a stale full
                           # representation in the client cache).
                           nulling=(location == "deck"))
    _log(f"    CardModifier {hex(card_uid)}: {atk_d:+}ATK/{def_d:+}DEF"
         f"{' (turn)' if this_turn else ''} -> now {atk}/{def_}")
    return def_


def clear_turn_stat_mods(db, session, card_uid=None):
    """Reset this-turn stat modifiers (called at the owner's next turn start)."""
    if card_uid is not None:
        db.execute(
            "UPDATE game_cards SET temporary_buffs='{}' "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(card_uid)))
    else:
        db.execute(
            "UPDATE game_cards SET temporary_buffs='{}' "
            "WHERE session_id=?", (session.session_id,))
    db.commit()
