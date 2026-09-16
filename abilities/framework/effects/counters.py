"""Card-counter persistence and client projection."""

import json
import re
import base64
import struct

import game_engine


# These are client BuiltInResources rather than ordinary Records entries.
# Keep the IDs here so the server can maintain the same hidden counter and
# state-based surface behavior without inventing a card-specific rule.
TUNNELING_COUNTER_GUID = "def75520-0b8b-447f-8705-b34e71043890"
# The Records graph contains the client keyword trigger as well.  Tunneling
# counters are advanced by the shared turn-boundary service below so that the
# same rule works for PvP and PvE; resolving this graph as an ordinary trigger
# would add a second counter and put a pointless ability on the chain.
TUNNELING_ABILITY_GUID = "a4fc4440-4f02-4f40-b786-214ad0205dad"
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
    """Return the current metadata-defined Tunneling value for a card.

    The base card value is stored in the CardTemplate TAC, not in the
    normalized card-template table.  Instance modifiers are persisted in the
    same ``int_attrs`` map used by the generic CardModifier executor.  This
    keeps the rule data-driven and avoids parsing a card name or game text.
    """
    values = persisted_int_attrs or {}
    for key, value in values.items():
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
        marker = _tac_hash("Tunneling")
        offset = raw.find(marker)
        if offset >= 0 and offset + 8 <= len(raw):
            return max(0, int(struct.unpack_from("<i", raw, offset + 4)[0]))
    except (AttributeError, TypeError, ValueError, struct.error,
            base64.binascii.Error):
        pass
    return 0


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
        from pvp_db import db_counter_template_id
        return db_counter_template_id(name, conn=db)
    except Exception:
        return None


def card_counters(db, session_id, card_uid):
    return dict(card_counters_full(db, session_id, card_uid)[0])


def card_counters_full(db, session_id, card_uid):
    from pvp_db import db_card_mutation_field
    payload = db_card_mutation_field(session_id, int(card_uid),
                                     "permanent_buffs", conn=db)
    if payload is None:
        return {}, {}
    data, counters = _counters_payload(payload)
    guids = data.get("counter_guids")
    if not isinstance(guids, dict):
        guids = {}
    return dict(counters), dict(guids)


def counter_is_secret(counter_guid):
    """Whether the client CardCounterTemplate hides this counter from opponents."""
    # Tunneling progress is public game information even though the card's
    # identity remains hidden while it is Underground.
    if str(counter_guid or "").lower() == TUNNELING_COUNTER_GUID:
        return False
    try:
        from gamedata import DEFAULT_RECORD_STORE
        record = DEFAULT_RECORD_STORE.get(
            "CardCounterTemplate", str(counter_guid or "").lower())
        return bool(record and int(record.field("m_Secret", 0) or 0))
    except (AttributeError, TypeError, ValueError):
        return False


def add_card_counter(db, session_id, card_uid, name, amount=1):
    from pvp_db import db_card_mutation_field, db_set_card_mutation_field
    payload = db_card_mutation_field(session_id, int(card_uid),
                                     "permanent_buffs", conn=db)
    if payload is None:
        return 0
    data, counters = _counters_payload(payload)
    key = (name or "").lower()
    counters[key] = int(counters.get(key, 0)) + int(amount)
    data["counters"] = counters
    guids = data.get("counter_guids")
    if not isinstance(guids, dict):
        guids = {}
    if key not in guids:
        guid = counter_guid_for_name(db, key)
        if not guid and key == "tunneling":
            guid = TUNNELING_COUNTER_GUID
        if guid:
            guids[key] = guid
    data["counter_guids"] = guids
    db_set_card_mutation_field(session_id, int(card_uid), "permanent_buffs",
                               json.dumps(data), conn=db)
    db.commit()
    return counters[key]


def increment_tunneling_counters(db, session, handler, game, pl_t, ai_t,
                                 bstate, owner_id):
    """Add one public Tunneling counter to each owned underground card."""
    owner_id = int(owner_id or 0)
    from pvp_db import db_underground_card_rows, db_card_mutation_field
    rows = db_underground_card_rows(session.session_id, owner_id, conn=db)
    changed = []
    for card_uid, template_guid in rows:
        payload = db_card_mutation_field(session.session_id, int(card_uid),
                                         "permanent_buffs", conn=db)
        try:
            saved = json.loads(payload or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            saved = {}
        persisted = saved.get("int_attrs", {})
        threshold = tunneling_value(db, template_guid, persisted)
        if threshold <= 0:
            continue
        old = card_counters(db, session.session_id, int(card_uid)).get(
            "tunneling", 0)
        new = add_card_counter(db, session.session_id, int(card_uid),
                               "tunneling", 1)
        push_card_counters(game, session, db, handler, pl_t, ai_t,
                           int(card_uid), bstate, changed_counter="tunneling",
                           old_value=old)
        changed.append((int(card_uid), old, new, threshold))
    return changed


def queue_tunneling_surfaces(db, session, handler, game, pl_t, ai_t, bstate,
                             owner_id):
    """Queue thresholded underground cards as normal free Surface abilities."""
    import battle_engine as _be

    owner_id = int(owner_id or 0)
    if not _be.stack_empty(bstate):
        return []
    from pvp_db import db_underground_card_rows, db_card_mutation_field
    rows = db_underground_card_rows(session.session_id, owner_id, conn=db)
    queued = []
    for card_uid, template_guid in rows:
        # State-based actions are serialized.  Once one card has been put on
        # the chain, stop here and let its resolution re-enter this function;
        # otherwise several hidden Surface entries can be emitted together
        # and the client presents a card-back/Resolve prompt for each one.
        if not _be.stack_empty(bstate):
            break
        payload = db_card_mutation_field(session.session_id, int(card_uid),
                                         "permanent_buffs", conn=db)
        try:
            saved = json.loads(payload or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            saved = {}
        threshold = tunneling_value(db, template_guid,
                                    saved.get("int_attrs", {}))
        count = card_counters(db, session.session_id, int(card_uid)).get(
            "tunneling", 0)
        if threshold <= 0 or count < threshold:
            continue
        old = count
        # The original state-based action resets the counter before creating
        # the Surface ability, so a response/cancel cannot make it requeue.
        add_card_counter(db, session.session_id, int(card_uid), "tunneling",
                         -old)
        push_card_counters(game, session, db, handler, pl_t, ai_t,
                           int(card_uid), bstate, changed_counter="tunneling",
                           old_value=old)
        instance_id = int(bstate.get("_next_instance_id", 1))
        bstate["_next_instance_id"] = instance_id + 1
        _be.stack_push(bstate, {
            "kind": "ability", "ability_guid": SURFACE_ABILITY_GUID,
            "source_uid": int(card_uid), "target_uid": int(card_uid),
            "source_owner_uid": owner_id, "instance_id": instance_id,
        })
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        game.push_ability_on_chain(
            scid, game_engine.ResourceId.from_str(SURFACE_ABILITY_GUID),
            ability_instance_id=instance_id, target_card_ids=[scid],
            ignores_chain=False)
        queued.append(int(card_uid))
    return queued


def surface_source_is_underground(db, session, card_uid):
    """Return whether a persisted Surface item still has a legal source."""
    if card_uid is None:
        return False
    try:
        card_uid = int(card_uid)
    except (TypeError, ValueError):
        return False
    from pvp_db import db_card_location
    return str(db_card_location(session.session_id, card_uid, conn=db) or "").lower() == "underground"


def _champion_uid_owner(handler, bstate, champion_uid):
    """Return the controller id for a champion SessionCardId.

    Champions are deliberately not rows in ``game_cards``.  Keep their
    counters in the persisted battle state instead, while resolving ownership
    from the same PvE handler/PvP ``champ_map`` data used by targeting.
    """
    try:
        target = int(champion_uid)
    except (TypeError, ValueError):
        return None
    if (bstate or {}).get("pvp"):
        for pid, uid in ((bstate or {}).get("champ_map") or {}).items():
            try:
                if int(uid) == target:
                    return int(pid)
            except (TypeError, ValueError):
                continue
    profile = getattr(handler, "user_profile", None)
    profile_id = profile.get("id", 0) if isinstance(profile, dict) else 0
    for attr, owner in (("_player_champ_scid", profile_id),
                        ("_ai_champ_scid", 0)):
        champ = getattr(handler, attr, None)
        if champ is None:
            continue
        try:
            uid = int(champ.uid.uid64)
        except AttributeError:
            try:
                uid = int(champ)
            except (TypeError, ValueError):
                continue
        if uid == target:
            return int(owner)
    return None


def is_champion_target(handler, bstate, champion_uid):
    """Whether *champion_uid* identifies one of the live champion cards."""
    return _champion_uid_owner(handler, bstate, champion_uid) is not None


def champion_counter_counts(bstate, champion_uid):
    """Return counters on a champion keyed by counter-template GUID."""
    try:
        uid = str(int(champion_uid))
    except (TypeError, ValueError):
        return {}
    values = ((bstate or {}).get("champion_counters") or {}).get(uid, {})
    if not isinstance(values, dict):
        return {}
    return {str(guid).lower(): int(count or 0)
            for guid, count in values.items()
            if int(count or 0) > 0}


def set_champion_counter(bstate, champion_uid, counter_guid, value):
    """Persist one champion counter in the session's battle-state JSON."""
    try:
        uid = str(int(champion_uid))
    except (TypeError, ValueError):
        return 0
    guid = str(counter_guid or "").lower()
    if not guid:
        return 0
    if bstate is None:
        return 0
    all_counters = bstate.setdefault("champion_counters", {})
    values = all_counters.setdefault(uid, {})
    new_value = max(0, int(value or 0))
    if new_value:
        values[guid] = new_value
    else:
        values.pop(guid, None)
        if not values:
            all_counters.pop(uid, None)
    return new_value


def change_champion_counter(bstate, champion_uid, counter_guid, amount,
                            operation="add"):
    """Apply an add/remove/set operation and return ``(old, new)``."""
    old = champion_counter_counts(bstate, champion_uid).get(
        str(counter_guid or "").lower(), 0)
    operation = str(operation or "add").lower()
    if operation == "set":
        new = int(amount or 0)
    elif operation in ("remove", "subtract"):
        new = old - int(amount or 0)
    else:
        new = old + int(amount or 0)
    return old, set_champion_counter(bstate, champion_uid, counter_guid, new)


def push_champion_counter(game, session, handler, pl_t, ai_t, bstate,
                          champion_uid, counter_guid, old_value, new_value):
    """Project a persisted champion counter through normal card events."""
    from .._shared import owner_uid

    owner_id = _champion_uid_owner(handler, bstate, champion_uid)
    if owner_id is None:
        return
    scid = game_engine.SessionCardId(game_engine.UID(int(champion_uid)))
    target = int(champion_uid)
    player_champ = getattr(handler, "_player_champ_scid", None)
    player_champ_uid = None
    ai_champ_uid = None
    try:
        player_champ_uid = int(player_champ.uid.uid64) if player_champ else None
    except AttributeError:
        player_champ_uid = None
    try:
        ai_champ = getattr(handler, "_ai_champ_scid", None)
        ai_champ_uid = int(ai_champ.uid.uid64) if ai_champ else None
    except AttributeError:
        ai_champ_uid = None
    if target == player_champ_uid:
        template_id = getattr(handler, "_player_champ_guid", None)
    elif target == ai_champ_uid:
        template_id = getattr(handler, "_ai_champ_guid", None)
    else:
        template_id = None
    cdef = game.card_defs.get(scid)
    # A fresh Game object (reconnects and PvP phase packets) may not have a
    # CardDef yet.  Materialize it through the existing authoritative helper
    # before publishing the counter update so abilities/stats are retained.
    if cdef is None:
        guid = template_id
        loader = getattr(handler, "_card_full_data", None)
        if callable(loader) and guid:
            try:
                loader(game, scid, guid)
                cdef = game.card_defs.get(scid)
            except Exception:
                cdef = None
    if cdef is not None:
        cdef.counters[str(counter_guid).lower()] = int(new_value)
        secret_guids = getattr(cdef, "_secret_counter_guids", set()) or set()
        if counter_is_secret(counter_guid):
            secret_guids.add(str(counter_guid).lower())
        cdef._secret_counter_guids = secret_guids
    player_uid = owner_uid(owner_id, pl_t, ai_t, bstate)
    health_key = ((bstate or {}).get("pvp_health_map") or {}).get(owner_id)
    if health_key:
        health = int(bstate.get(health_key, 20))
    elif (bstate or {}).get("pvp"):
        health = int(bstate.get(f"hp_{owner_id}", 20))
    else:
        health = int(bstate.get("player_health" if owner_id else
                               "ai_health", 20))
    game.push_card_updated(
        scid, player_uid, game_engine.ECardCollections.Champions,
        game_engine.ECardTypes.Champion, template_id=template_id,
        defense=health, counters={str(counter_guid).lower(): int(new_value)},
        secret_counter_guids=({str(counter_guid).lower()}
                              if counter_is_secret(counter_guid) else set()),
        secret_counter_owner_uid=player_uid)
    game.push_card_counters_changed(
        scid, game_engine.ResourceId.from_str(counter_guid),
        int(new_value), int(old_value), private_player_uid=player_uid)


def push_card_counters(game, session, db, handler, pl_t, ai_t, target_uid,
                       bstate=None, changed_counter=None, old_value=None):
    from .._shared import card_collection_for_location, owner_uid
    if target_uid is None:
        return
    counts, guids = card_counters_full(db, session.session_id, int(target_uid))
    from pvp_db import db_card_source_info, db_card_owner_id
    trow = db_card_source_info(session.session_id, int(target_uid), conn=db)
    if not trow:
        return
    scid = game_engine.SessionCardId(game_engine.UID(int(target_uid)))
    _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(
        game, scid, trow[0])
    orow = db_card_owner_id(session.session_id, int(target_uid), conn=db)
    owner = owner_uid(orow if orow is not None else 0, pl_t, ai_t, bstate)
    encoded = {}
    for name, count in counts.items():
        guid = guids.get(name) or counter_guid_for_name(db, name)
        if guid:
            encoded[guid] = int(count)
    game.push_card_updated(scid, owner, card_collection_for_location(trow[2]),
                           ct, template_id=trow[0], attack=atk, defense=def_,
                           counters=encoded, nulling=(trow[2] == "deck"),
                           secret_counter_guids={
                               str(guid).lower() for guid in encoded
                               if counter_is_secret(guid)},
                           secret_counter_owner_uid=owner)
    if changed_counter is not None and old_value is not None:
        key = str(changed_counter).lower()
        guid = guids.get(key) or counter_guid_for_name(db, key)
        if guid:
            game.push_card_counters_changed(
                scid, game_engine.ResourceId.from_str(guid),
                int(counts.get(key, 0)), int(old_value),
                private_player_uid=(owner if counter_is_secret(guid) else None))


def remove_card_counters(db, session_id, card_uid, name=None):
    from pvp_db import db_card_mutation_field, db_set_card_mutation_field
    payload = db_card_mutation_field(session_id, int(card_uid),
                                     "permanent_buffs", conn=db)
    if payload is None:
        return
    data, counters = _counters_payload(payload)
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
    db_set_card_mutation_field(session_id, int(card_uid), "permanent_buffs",
                               json.dumps(data), conn=db)
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
