"""Server-side implementation of the client's ``DebugCheatTransaction``.

The original Hex client sends these actions as normal 3029 transactions.  The
private server does not have the original authoritative-session implementation,
so this module applies the useful state changes to our DB/PvP state and emits
the same family of session events used by the normal game paths.

This is deliberately a transaction dispatcher rather than another set of chat
commands.  Chat commands can call server helpers directly; these requests need
to decode the client's ObjFmt fields and, in PvP, mutate the pid-specific state
for the player named by ``m_PlayerId``.
"""

from __future__ import annotations

import json
import re
import struct

import game_engine as _ge
from db import _db, log_req


ACTION_NAMES = {
    0: "AddCard",
    1: "AddCardWithCost",
    2: "AddCardToTopOfDeck",
    3: "AddResources",
    4: "AddCharges",
    5: "AddAll",
    6: "AddThreshold",
    7: "AddAllThreshold",
    8: "BuryCard",
    9: "DestroyCard",
    10: "DiscardHand",
    11: "DrawCard",
    12: "SetLife",
    13: "PlayCard",
    14: "Play",
    15: "SetEquipment",
    16: "NoTimers",
    17: "AICommand",
    18: "RandomizeWarzone",
    19: "SetDefaultGems",
    20: "PublishEvent",
    21: "AddSpellPoints",
    22: "TransformChampion",
    23: "TransformMercenary",
    24: "SetTalent",
    25: "RemoveTalent",
    26: "ClearTalents",
    27: "Nuke",
}

_GUID_RE = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_UID_RE = re.compile(rb"m_UID64;[^;]*;[^;]*;[^;]*;([0-9a-fA-F]{16});")


def _enum(handler, raw, field):
    try:
        return handler._extract_enum_int(raw, field)
    except Exception:
        return None


def _int32(handler, raw, field, default=None):
    try:
        value = handler._extract_int32_field(raw, field)
        return default if value is None else int(value)
    except Exception:
        return default


def _uid_after(raw, field):
    """Read a UID/SessionCardId's UInt64 child after a named field.

    UID and SessionCardId both contain an ``m_UID64`` member.  Taking the last
    one in the field segment handles the nested object shape used by the
    client while avoiding unrelated later fields.
    """
    if not isinstance(raw, bytes):
        return None
    pos = raw.find(field.encode("ascii"))
    if pos < 0:
        return None
    end = len(raw)
    for marker in (b"CardTemplateId", b"ThresholdColor", b"Count", b"SessionCardId",
                   b"IventoryItemId", b"DestPlayer", b"EquipmentSlot", b"defaultGems"):
        if marker == field.encode("ascii"):
            continue
        candidate = raw.find(marker, pos + len(field))
        if candidate >= 0:
            end = min(end, candidate)
    matches = list(_UID_RE.finditer(raw[pos:end]))
    if not matches:
        return None
    try:
        return struct.unpack("<Q", bytes.fromhex(matches[-1].group(1).decode()))[0]
    except Exception:
        return None


def _resource_id(raw, field):
    """Extract a ResourceId's nested m_Guid/guid string."""
    if not isinstance(raw, bytes):
        return None
    pos = raw.find(field.encode("ascii"))
    if pos < 0:
        return None
    end = len(raw)
    for marker in (b"ThresholdColor", b"Count", b"SessionCardId", b"IventoryItemId",
                   b"DestPlayer", b"EquipmentSlot", b"defaultGems", b"Key", b"Value"):
        if marker == field.encode("ascii"):
            continue
        candidate = raw.find(marker, pos + len(field))
        if candidate >= 0:
            end = min(end, candidate)
    segment = raw[pos:end]
    for marker in (b"m_Guid", b"guid"):
        mpos = segment.find(marker)
        if mpos < 0:
            continue
        match = _GUID_RE.search(segment[mpos:])
        if match:
            return match.group(0).decode("ascii").lower()
    match = _GUID_RE.search(segment)
    return match.group(0).decode("ascii").lower() if match else None


def _pvp(session):
    return bool(session and (session.session_name or "").startswith("tourney-"))


def _pvp_context(session):
    from services.tournament_game import pvp_load_state
    state = pvp_load_state(session)
    if not state:
        return None, None
    pids = [int(p) for p in (state.get("pids") or [])]
    return state, pids


def _target_pid(handler, session, raw):
    """Return the transaction's player pid, constrained to this game."""
    state, pids = _pvp_context(session) if _pvp(session) else (None, None)
    submitted = _uid_after(raw, "m_PlayerId")
    if submitted is not None:
        submitted_pid = int(submitted) >> 8
        if pids and submitted_pid in pids:
            return submitted_pid
    if pids:
        current = int(handler.client_reck_id)
        return current if current in pids else pids[0]
    return int(getattr(handler, "user_profile", {}).get("id", handler.client_reck_id))


def _opponent_pid(state, pid):
    return next((int(p) for p in (state.get("pids") or []) if int(p) != int(pid)), None)


def _owner_uid(pid):
    return _ge.UID.make(244, int(pid))


def _champion_scid(state, pid):
    value = int((state.get("champ_map") or {}).get(str(pid), 0) or 0)
    return _ge.SessionCardId(_ge.UID(value)) if value else None


def _new_card_uid(session_id):
    row = _db.execute(
        "SELECT COALESCE(MAX(card_uid >> 8), 0) FROM game_cards WHERE session_id=?",
        (int(session_id),),
    ).fetchone()
    return _ge.UID.make(1, int(row[0] or 0) + 1).uid64


def _template(guid):
    if not guid:
        return None
    return _db.execute(
        "SELECT guid, card_type, name, cost, attack, defense, abilities_json "
        "FROM card_templates WHERE guid=?", (str(guid).lower(),)
    ).fetchone()


def _find_card_template(guid):
    """Return a template, tolerating a ResourceId that was not decoded."""
    row = _template(guid)
    if row:
        return row
    return None


def _game(handler, session, state, pid):
    if state and state.get("pvp"):
        other = _opponent_pid(state, pid) or pid
        game = _ge.Game(int(session.session_id), _owner_uid(pid), _owner_uid(other))
        from services.tournament_game import _pvp_populate_game_state
        _pvp_populate_game_state(game, state, pid, other)
        return game, _owner_uid(pid), _owner_uid(other)
    pl = _ge.UID.make(244, int(handler.client_reck_id))
    return _ge.Game(int(session.session_id), pl, _ge.UID.make(3, 1000)), pl, _ge.UID.make(3, 1000)


def _send(handler, session, game, player_uid, pvp=False):
    if not game.events:
        return
    if pvp:
        from services.tournament_game import _pvp_send_same_events
        _pvp_send_same_events(session, game, game.player_uid, game.ai_uid)
    else:
        handler._send_battle_events(session, game, player_uid)


def _sync_player(handler, session, state, pid, game):
    """Push a complete HUD update after a cheat changes player state."""
    if state and state.get("pvp"):
        other = _opponent_pid(state, pid) or pid
        from services.tournament_game import _pvp_populate_game_state
        _pvp_populate_game_state(game, state, pid, other)
        for target in (pid, other):
            game.push_player_updated(_owner_uid(target), champ_id=_champion_scid(state, target))
    else:
        game.push_player_updated(game.player_uid, champ_id=getattr(handler, "_player_champ_scid", None))


def _apply_pool_change(state, pid, field, delta):
    key = f"{field}_{pid}"
    state[key] = max(0, int(state.get(key, 0)) + int(delta))
    if field == "res":
        total = f"res_total_{pid}"
        state[total] = max(0, int(state.get(total, 0)) + int(delta))
    return state[key]


def _emit_pool_events(game, pid, state, field, delta):
    uid = _owner_uid(pid)
    operation = 1 if int(delta) >= 0 else 2
    if field == "res":
        current = int(state.get(f"res_{pid}", 0))
        total = int(state.get(f"res_total_{pid}", 0))
        ev = _ge.PlayerCurrentResourcePoolChangedSessionEventArgs()
        ev.player_id, ev.operation, ev.delta, ev.new_value = uid, operation, int(delta), current
        game._push(ev)
        ev2 = _ge.PlayerTotalResourcePoolChangedSessionEventArgs()
        ev2.player_id, ev2.operation, ev2.delta, ev2.new_value = uid, operation, int(delta), total
        game._push(ev2)
    elif field == "chg":
        ev = _ge.ChampionChargePointsChangedSessionEventArgs()
        ev.player_id, ev.operation, ev.delta, ev.new_value = uid, operation, int(delta), int(state.get(f"chg_{pid}", 0))
        game._push(ev)
    elif field == "sp":
        ev = _ge.ChampionSpellPointsChangedSessionEventArgs()
        ev.player_id, ev.operation, ev.delta, ev.new_value = uid, operation, int(delta), int(state.get(f"sp_{pid}", 0))
        game._push(ev)


def _add_card_requirements(state, pid, tpl):
    """Mirror the original cheat's minimum cost/threshold setup for PvP."""
    if not state or not state.get("pvp"):
        return 0, {}
    extra = max(0, int(tpl[3] or 0) - int(state.get(f"res_{pid}", 0)))
    if extra:
        _apply_pool_change(state, pid, "res", extra)
    added = {}
    row = _db.execute(
        "SELECT threshold_json FROM card_templates WHERE guid=?", (tpl[0],)
    ).fetchone()
    try:
        values = json.loads(row[0] or "{}").get("values", []) if row else []
    except (TypeError, ValueError):
        values = []
    # PlayerUpdated serializes thresholds in this exact order.  Colorless is
    # not a threshold color, so skip index 0.
    colors = (_ge.ECardShards.Blood, _ge.ECardShards.Ruby,
              _ge.ECardShards.Sapphire, _ge.ECardShards.Wild,
              _ge.ECardShards.Diamond)
    thresholds = state.setdefault(f"thresh_{pid}", {})
    for index, color in enumerate(colors, 1):
        required = int(values[index] or 0) if index < len(values) else 0
        current = thresholds.get(color, thresholds.get(str(color), 0))
        if required > int(current or 0):
            thresholds[color] = required
            added[color] = required - int(current or 0)
    return extra, added


def _insert_card(handler, session, state, pid, guid, location):
    tpl = _find_card_template(guid)
    if not tpl:
        log_req(f"    Debug cheat: unknown card template {guid!r}")
        return None
    if "Champion" in str(tpl[1]):
        log_req(f"    Debug cheat: refusing to create champion card {guid}")
        return None
    card_uid = _new_card_uid(session.session_id)
    owner = int(pid)
    row = _db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location=?", (session.session_id, owner, location)
    ).fetchone()
    position = int(row[0] or 0)
    if location == "deck":
        _db.execute("UPDATE game_cards SET position=position+1 WHERE session_id=? AND user_id=? AND location='deck'", (session.session_id, owner))
        position = 0
    _db.execute(
        "INSERT INTO game_cards (user_id, session_id, card_uid, card_template_id, location, position, "
        "is_champion, card_type, template_guid, owner_user_id, original_template_guid) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (owner, session.session_id, int(card_uid), 0, location, position, tpl[1], tpl[0], owner, tpl[0]),
    )
    _db.commit()
    return int(card_uid), tpl


def _card_event(handler, session, state, pid, card_uid, collection, location, index=0):
    game, me, _ = _game(handler, session, state, pid)
    scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
    row = _db.execute("SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?", (session.session_id, int(card_uid))).fetchone()
    guid = row[0] if row else None
    tpl, ct, _name, cost, atk, defense, gems = handler._card_full_data(game, scid, guid)
    game.push_card_updated(scid, _owner_uid(pid) if state and state.get("pvp") else me,
                           collection, ct, template_id=tpl, cost=cost, attack=atk, defense=defense, gems=gems)
    game.push_card_moved(scid, _owner_uid(pid) if state and state.get("pvp") else me, collection, location, index)
    return game, me


def _move_to_zone(handler, session, state, pid, card_uid, zone, collection):
    row = _db.execute("SELECT id FROM game_cards WHERE session_id=? AND card_uid=?", (session.session_id, int(card_uid))).fetchone()
    if not row:
        return None
    _db.execute("UPDATE game_cards SET location=?, position=0 WHERE id=?", (zone, row[0]))
    _db.commit()
    return _card_event(handler, session, state, pid, card_uid, collection, _ge.ECardLocations.Top)


def _draw(handler, session, state, pid, count):
    game, me, _ = _game(handler, session, state, pid)
    drawn = 0
    for _ in range(max(0, int(count))):
        before = _db.execute("SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? AND location='deck' ORDER BY position LIMIT 1", (session.session_id, pid)).fetchone()
        if not before:
            break
        result = handler._player_draw_card(
            game, session, _owner_uid(pid) if state and state.get("pvp") else me,
            owner_id=pid)
        if result is False:
            break
        drawn += 1
        # _player_draw_card appends to the same Game; its PvP state save is
        # authoritative, so do not rebuild or overwrite it here.
    return game, drawn


def _add_card(handler, session, state, pid, guid, to_deck=False, play=False):
    inserted = _insert_card(handler, session, state, pid, guid, "deck" if to_deck else "hand")
    if not inserted:
        return None
    card_uid, tpl = inserted
    game, me, _ = _game(handler, session, state, pid)
    owner_uid = _owner_uid(pid) if state and state.get("pvp") else me
    scid = _ge.SessionCardId(_ge.UID(card_uid))
    full = handler._card_full_data(game, scid, tpl[0])
    if to_deck:
        game.push_card_updated(scid, owner_uid, _ge.ECardCollections.Deck, full[1], template_id=full[0], cost=full[3], attack=full[4], defense=full[5], gems=full[6])
        game.push_card_moved(scid, owner_uid, _ge.ECardCollections.Deck, _ge.ECardLocations.Top, 0)
    else:
        game.push_card_updated(scid, owner_uid, _ge.ECardCollections.Hand, full[1], template_id=full[0], cost=full[3], attack=full[4], defense=full[5], gems=full[6])
        game.push_card_drawn(scid, owner_uid, 1)
        game.push_card_moved(scid, owner_uid, _ge.ECardCollections.Hand, _ge.ECardLocations.Top, 1)
    if state and state.get("pvp"):
        from services.tournament_game import pvp_save_state
        pvp_save_state(session, state)
    return game, me, card_uid, tpl


def _set_life(handler, session, state, pid, value, game):
    if state and state.get("pvp"):
        state[f"hp_{pid}"] = max(0, int(value))
        from services.tournament_game import pvp_save_state
        pvp_save_state(session, state)
        _sync_player(handler, session, state, pid, game)
        return
    # Practice's regular health path is represented in the battle state; the
    # HUD refresh below is sufficient for the server's cheat operation.
    import battle_engine
    bstate = battle_engine.load_state(session)
    bstate["player_health"] = max(0, int(value))
    battle_engine.save_state(session, bstate)
    game.player_health = bstate["player_health"]
    game.push_player_updated(game.player_uid, champ_id=getattr(handler, "_player_champ_scid", None))


def _handle(handler, session, raw):
    action = _enum(handler, raw, "DebugAction")
    if action is None:
        log_req("    Debug cheat: missing DebugAction")
        return True
    action = int(action)
    name = ACTION_NAMES.get(action, f"Unknown({action})")
    state, pids = _pvp_context(session) if _pvp(session) else (None, None)
    pid = _target_pid(handler, session, raw)
    count = _int32(handler, raw, "Count", 1)
    guid = _resource_id(raw, "CardTemplateId")
    target_card = _uid_after(raw, "SessionCardId")
    log_req(f"    Debug cheat: action={name} pid={pid} count={count} card={guid} target={target_card}")

    if state and not pids:
        log_req("    Debug cheat ignored: PvP state has no players")
        return True

    if action in (0, 1, 2, 14):
        result = _add_card(handler, session, state, pid, guid, to_deck=(action == 2), play=(action == 14))
        if result:
            game, me, card_uid, tpl = result
            if action in (1, 14):
                if state and state.get("pvp"):
                    # AddCardWithCost is intended to make the card playable;
                    # give the target enough resources/thresholds, as the
                    # original client authoritative session did.
                    extra, _thresholds = _add_card_requirements(state, pid, tpl)
                    if extra:
                        _emit_pool_events(game, pid, state, "res", extra)
                    from services.tournament_game import pvp_save_state
                    pvp_save_state(session, state)
                    _sync_player(handler, session, state, pid, game)
                elif action == 1:
                    import battle_engine
                    bstate = battle_engine.load_state(session)
                    cost = int(tpl[3] or 0)
                    bstate["player_resources"] = max(
                        int(bstate.get("player_resources", 0)), cost)
                    bstate["player_total_resources"] = max(
                        int(bstate.get("player_total_resources", 0)), cost)
                    try:
                        values = json.loads(
                            (_db.execute(
                                "SELECT threshold_json FROM card_templates WHERE guid=?",
                                (tpl[0],)).fetchone() or ["{}"]) [0] or "{}"
                        ).get("values", [])
                    except (TypeError, ValueError, IndexError):
                        values = []
                    colors = (_ge.ECardShards.Blood, _ge.ECardShards.Ruby,
                              _ge.ECardShards.Sapphire, _ge.ECardShards.Wild,
                              _ge.ECardShards.Diamond)
                    thresholds = bstate.setdefault("player_threshold", {})
                    for index, color in enumerate(colors, 1):
                        if index < len(values):
                            thresholds[color] = max(
                                int(thresholds.get(color, 0)), int(values[index] or 0))
                    battle_engine.save_state(session, bstate)
                    game.player_resources = int(bstate["player_resources"])
                    game.player_total_resources = int(bstate["player_total_resources"])
                    game.player_threshold = dict(thresholds)
                    game.push_player_updated(me, champ_id=getattr(handler, "_player_champ_scid", None))
                if action == 14:
                    if state and state.get("pvp") and pid == int(handler.client_reck_id):
                        from services.tournament_game import (
                            _pvp_play_spell, _pvp_play_troop, pvp_save_state)
                        _add_card_requirements(state, pid, tpl)
                        pvp_save_state(session, state)
                        ctype = _ge.card_type_from_db(tpl[1])
                        if ctype & (_ge.ECardTypes.BasicAction | _ge.ECardTypes.QuickAction):
                            _pvp_play_spell(handler, session, card_uid, pid, raw)
                        elif ctype & (_ge.ECardTypes.Troop | _ge.ECardTypes.Artifact | _ge.ECardTypes.Constant):
                            _pvp_play_troop(handler, session, card_uid, pid)
                        else:
                            log_req(f"    Debug cheat Play: unsupported card type {tpl[1]}")
                        return True
                    log_req("    Debug cheat Play: PvE/unowned-player play remains acknowledgement-only")
            _send(handler, session, game, me, bool(state and state.get("pvp")))
        return True

    if action == 13 and target_card:
        if state and state.get("pvp") and pid == int(handler.client_reck_id):
            from services.tournament_game import pvp_handle_transaction, pvp_save_state
            row = _db.execute(
                "SELECT ct.cost FROM game_cards gc JOIN card_templates ct "
                "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.card_uid=?",
                (session.session_id, int(target_card)),
            ).fetchone()
            extra = max(0, int(row[0] or 0) - int(state.get(f"res_{pid}", 0))) if row else 0
            if extra:
                _apply_pool_change(state, pid, "res", extra)
                pvp_save_state(session, state)
            # The cheat transaction exposes the public DataMember name
            # ``SessionCardId``; ordinary Play* transactions expose the
            # backing member ``m_SessionCardId``.  Reuse the normal PvP play
            # router after normalizing that one wire field.
            play_raw = raw.replace(b"SessionCardId", b"m_SessionCardId", 1)
            if pvp_handle_transaction(handler, session, play_raw):
                return True
        log_req(f"    Debug cheat PlayCard: target {target_card} was acknowledged but not playable in this session")
        return True

    if action == 11:
        game, drawn = _draw(handler, session, state, pid, count)
        _send(handler, session, game, _owner_uid(pid) if state else game.player_uid, bool(state and state.get("pvp")))
        log_req(f"    Debug cheat DrawCard: drew {drawn}/{max(0, count)} for pid {pid}")
        return True

    if action in (3, 4, 21):
        field = {3: "res", 4: "chg", 21: "sp"}[action]
        if state and state.get("pvp"):
            _apply_pool_change(state, pid, field, count)
            from services.tournament_game import pvp_save_state
            pvp_save_state(session, state)
            game, me, _ = _game(handler, session, state, pid)
            _emit_pool_events(game, pid, state, field, count)
            _sync_player(handler, session, state, pid, game)
            _send(handler, session, game, me, True)
        else:
            import battle_engine
            bstate = battle_engine.load_state(session)
            key = {"res": "player_resources", "chg": "player_charges", "sp": "player_spell_points"}[field]
            bstate[key] = max(0, int(bstate.get(key, 0)) + count)
            if field == "res":
                bstate["player_total_resources"] = max(0, int(bstate.get("player_total_resources", 0)) + count)
            battle_engine.save_state(session, bstate)
            game, me, _ = _game(handler, session, state, pid)
            game.player_resources = int(bstate.get("player_resources", 0))
            game.player_total_resources = int(bstate.get("player_total_resources", 0))
            game.player_charges = int(bstate.get("player_charges", 0))
            game.player_spell_points = int(bstate.get("player_spell_points", 0))
            game.push_player_updated(me, champ_id=getattr(handler, "_player_champ_scid", None))
            _send(handler, session, game, me)
        return True

    if action in (5, 6, 7):
        if state and state.get("pvp"):
            if action == 5:
                for field in ("res", "chg", "sp"):
                    _apply_pool_change(state, pid, field, count)
            if action in (5, 6, 7):
                colors = [int(_enum(handler, raw, "ThresholdColor"))] if action == 6 and _enum(handler, raw, "ThresholdColor") is not None else [4, 8, 16, 32, 64]
                thresholds = state.setdefault(f"thresh_{pid}", {})
                for color in colors:
                    thresholds[str(color)] = max(0, int(thresholds.get(str(color), thresholds.get(color, 0))) + count)
            from services.tournament_game import pvp_save_state
            pvp_save_state(session, state)
            game, me, _ = _game(handler, session, state, pid)
            _sync_player(handler, session, state, pid, game)
            _send(handler, session, game, me, True)
        else:
            import battle_engine
            bstate = battle_engine.load_state(session)
            if action == 5:
                for key in ("player_resources", "player_total_resources",
                            "player_charges", "player_spell_points"):
                    bstate[key] = max(0, int(bstate.get(key, 0)) + count)
            colors = ([int(_enum(handler, raw, "ThresholdColor"))]
                      if action == 6 and _enum(handler, raw, "ThresholdColor") is not None
                      else [4, 8, 16, 32, 64])
            thresholds = bstate.setdefault("player_threshold", {})
            for color in colors:
                thresholds[color] = max(0, int(thresholds.get(color, thresholds.get(str(color), 0))) + count)
            battle_engine.save_state(session, bstate)
            game, me, _ = _game(handler, session, state, pid)
            game.player_resources = int(bstate.get("player_resources", 0))
            game.player_total_resources = int(bstate.get("player_total_resources", 0))
            game.player_charges = int(bstate.get("player_charges", 0))
            game.player_spell_points = int(bstate.get("player_spell_points", 0))
            game.player_threshold = dict(thresholds)
            game.push_player_updated(me, champ_id=getattr(handler, "_player_champ_scid", None))
            _send(handler, session, game, me)
        return True

    if action in (8, 10):
        rows = _db.execute(
            "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? AND location=? ORDER BY position",
            (session.session_id, pid, "deck" if action == 8 else "hand"),
        ).fetchall()
        targets = rows[:max(0, count)] if action == 8 else rows
        game = None
        for (card_uid,) in targets:
            moved = _move_to_zone(handler, session, state, pid, card_uid, "discard", _ge.ECardCollections.Discard)
            if moved:
                game = moved[0]
        if game:
            _send(handler, session, game, _owner_uid(pid) if state else game.player_uid, bool(state and state.get("pvp")))
        return True

    if action == 9 and target_card:
        moved = _move_to_zone(handler, session, state, pid, target_card, "discard", _ge.ECardCollections.Discard)
        if moved:
            _send(handler, session, moved[0], _owner_uid(pid) if state else moved[1], bool(state and state.get("pvp")))
        return True

    if action == 12:
        game, me, _ = _game(handler, session, state, pid)
        _set_life(handler, session, state, pid, count, game)
        _send(handler, session, game, me, bool(state and state.get("pvp")))
        return True

    if action == 27:
        owners = pids if state and pids else [pid]
        game = None
        for owner in owners:
            rows = _db.execute("SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? AND is_champion=0 AND location!='void'", (session.session_id, owner)).fetchall()
            for (card_uid,) in rows:
                moved = _move_to_zone(handler, session, state, owner, card_uid, "void", _ge.ECardCollections.Void)
                if moved:
                    game = moved[0]
            wild = _db.execute("SELECT guid FROM card_templates WHERE lower(name)='wild shard' LIMIT 1").fetchone()
            if wild:
                for _ in range(10):
                    made = _insert_card(handler, session, state, owner, wild[0], "deck")
                    if made:
                        game = game or _game(handler, session, state, owner)[0]
        if game:
            _send(handler, session, game, game.player_uid, bool(state and state.get("pvp")))
        return True

    # These actions alter client-only/session features or depend on campaign
    # systems that do not exist in the PvP state machine.  They are still
    # deliberately acknowledged by the caller so they cannot wedge the next
    # transaction.  Keep the log explicit for future implementation.
    log_req(f"    Debug cheat {name}: acknowledged, no server state handler yet")
    return True


def handle_debug_cheat(handler, session, raw):
    """Apply one DebugCheatTransaction and return whether it was consumed."""
    try:
        if _pvp(session):
            from services.tournament_game import pvp_session_lock
            with pvp_session_lock(session):
                return _handle(handler, session, raw)
        return _handle(handler, session, raw)
    except Exception as exc:
        log_req(f"    Debug cheat failed: {exc}")
        import traceback
        traceback.print_exc()
        return True
