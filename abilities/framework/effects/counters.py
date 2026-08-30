"""Card-counter persistence and client projection."""

import json
import re

import game_engine


def _counters_payload(card_row):
    try:
        data = json.loads(card_row or "{}")
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    counters = data.get("counters")
    if not isinstance(counters, dict):
        counters = {}
    return data, counters


def counter_guid_for_name(db, name):
    """Return the gamedata counter-template GUID for a counter name."""
    if not name:
        return None
    try:
        row = db.execute(
            "SELECT template_id FROM card_counter_templates "
            "WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def card_counters(db, session_id, card_uid):
    return dict(card_counters_full(db, session_id, card_uid)[0])


def card_counters_full(db, session_id, card_uid):
    row = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (session_id, int(card_uid))).fetchone()
    if not row:
        return {}, {}
    data, counters = _counters_payload(row[0])
    guids = data.get("counter_guids")
    if not isinstance(guids, dict):
        guids = {}
    return dict(counters), dict(guids)


def add_card_counter(db, session_id, card_uid, name, amount=1):
    row = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (session_id, int(card_uid))).fetchone()
    if not row:
        return 0
    data, counters = _counters_payload(row[0])
    key = (name or "").lower()
    counters[key] = int(counters.get(key, 0)) + int(amount)
    data["counters"] = counters
    guids = data.get("counter_guids")
    if not isinstance(guids, dict):
        guids = {}
    if key not in guids:
        guid = counter_guid_for_name(db, key)
        if guid:
            guids[key] = guid
    data["counter_guids"] = guids
    db.execute(
        "UPDATE game_cards SET permanent_buffs=? WHERE session_id=? "
        "AND card_uid=?", (json.dumps(data), session_id, int(card_uid)))
    db.commit()
    return counters[key]


def push_card_counters(game, session, db, handler, pl_t, ai_t, target_uid,
                       bstate=None, changed_counter=None, old_value=None):
    from .._shared import card_collection_for_location, owner_uid
    if target_uid is None:
        return
    counts, guids = card_counters_full(db, session.session_id, int(target_uid))
    trow = db.execute(
        "SELECT template_guid, location FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (session.session_id, int(target_uid))).fetchone()
    if not trow:
        return
    scid = game_engine.SessionCardId(game_engine.UID(int(target_uid)))
    _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(
        game, scid, trow[0])
    orow = db.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target_uid))).fetchone()
    owner = owner_uid(orow[0] if orow else 0, pl_t, ai_t, bstate)
    encoded = {}
    for name, count in counts.items():
        guid = guids.get(name) or counter_guid_for_name(db, name)
        if guid:
            encoded[guid] = int(count)
    game.push_card_updated(scid, owner, card_collection_for_location(trow[1]),
                           ct, template_id=trow[0], attack=atk, defense=def_,
                           counters=encoded, nulling=(trow[1] == "deck"))
    if changed_counter is not None and old_value is not None:
        key = str(changed_counter).lower()
        guid = guids.get(key) or counter_guid_for_name(db, key)
        if guid:
            game.push_card_counters_changed(
                scid, game_engine.ResourceId.from_str(guid),
                int(counts.get(key, 0)), int(old_value))


def remove_card_counters(db, session_id, card_uid, name=None):
    row = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (session_id, int(card_uid))).fetchone()
    if not row:
        return
    data, counters = _counters_payload(row[0])
    if name is None:
        counters = {}
    else:
        counters.pop((name or "").lower(), None)
    data["counters"] = counters
    guids = data.get("counter_guids")
    if isinstance(guids, dict):
        if name is None:
            guids = {}
        else:
            guids.pop((name or "").lower(), None)
        data["counter_guids"] = guids
    db.execute(
        "UPDATE game_cards SET permanent_buffs=? WHERE session_id=? "
        "AND card_uid=?", (json.dumps(data), session_id, int(card_uid)))
    db.commit()


def counter_name_from_text(text):
    """Extract a counter name from legacy effect parameters."""
    low = (text or "").lower()
    matches = list(re.finditer(r'([a-z][a-z\- ]*?)\s+counters?', low))
    if not matches:
        return None
    name = matches[-1].group(1).strip()
    for prefix in ("remove all ", "add an ", "add a ", "remove a ",
                   "remove an ", "all ", "add ", "remove ", "a ", "an "):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for stop in (" on ", " to ", " from ", " in "):
        idx = name.find(stop)
        if idx > 0:
            name = name[:idx]
    return name.strip()
