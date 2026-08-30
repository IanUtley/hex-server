"""Headless two-player PvP autoplay smoke runner.

Drives a real tournament PvP session (tourney-N) through the production
paths: 3029 PassPriority / ChoosePlay / AcceptStartingHand transactions,
resource + troop plays, attacker/blocker declarations and combat — with two
fake HCPHandlers pushing events to no client.  The goal is to catch crashes
and stuck phases in the PvP state machine (GreenLight sync, phase wrapping,
combat).

Run: python3 pvp_autoplay.py [games]
"""

import os
import random
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import game_engine
import game_session as gs
import hconnect_server as hcs
import battle_engine as be
import encoder
from db import _db, log_req
from services.tournament_game import (
    pvp_default_state, pvp_load_state, pvp_save_state,
    handle_ready_for_game_setup, handle_ready_for_game_events,
    player_handlers,
)

SET1 = "0382f729-7710-432b-b761-13677982dcd2"
SCRATCH_BASE = 987700


def _cleanup(session_id, pids):
    _db.execute("DELETE FROM game_cards WHERE session_id=?", (session_id,))
    _db.execute("DELETE FROM game_sessions WHERE session_id=?",
                (session_id,))
    for pid in pids:
        player_handlers.pop(pid, None)
    _db.commit()


def _make_handler(pid, session):
    h = object.__new__(hcs.HCPHandler)
    h.user_profile = {"id": pid, "name": f"P{pid}"}
    h.client_reck_id = pid
    h.sid = f"pvp-{pid}"
    h.scnt = 0
    h.ccnt = 0
    h._game_scnt = 0
    h._event_q = []
    h._svc_scnt = {}
    h.client_uid = f"pvp-{pid}"
    h._ai_turn_depth = 0
    h._current_bstate = None
    h._player_autopass = False
    h._pending_player_stops = None
    h._pending_player_draws_first = None
    h._player_champ_scid = None
    h._ai_champ_scid = None
    h._player_champ_guid = None
    h._ai_champ_guid = None
    h._player_starting_health = 20
    h._ai_starting_health = 20
    h._autoplay_drive_ai_turn = True
    h._campaign_gameend = lambda *a, **k: None
    h.send = lambda *a, **k: None
    h.send_and_cache = lambda *a, **k: None
    h._push_transaction_ack = lambda *a, **k: None
    return h


def _seed_deck(session_id, pid, deck, uid_offset=0):
    ctr = [uid_offset]
    for pos, tpl in enumerate(deck):
        ctr[0] += 1
        cu = game_engine.UID.make(1, ctr[0]).uid64
        row = _db.execute(
            "SELECT card_type, attributes, abilities_json FROM card_templates "
            "WHERE guid=?", (tpl,)).fetchone()
        _db.execute(
            "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,"
            "card_template_id,location,position,card_state,card_abilities,"
            "card_type,card_attributes,card_attack_mod,card_defense_mod,"
            "card_cost_mod,card_damage,permanent_buffs,temporary_buffs,"
            "card_uses,resolved_at,original_template_guid,temporary_attributes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,'{}','{}','{}',0,?,0)",
            (session_id, pid, cu, tpl, tpl, "deck", pos, 0,
             row[2] or '[]', row[0], row[1] or 0, tpl))


def _seed_champion(session_id, pid, champ_guid, uid_offset=0):
    ctr = [uid_offset]
    ctr[0] += 1
    cu = game_engine.UID.make(1, ctr[0]).uid64
    row = _db.execute(
        "SELECT card_type, attributes, abilities_json FROM card_templates "
        "WHERE guid=?", (champ_guid,)).fetchone()
    _db.execute(
        "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,"
        "card_template_id,location,position,card_state,card_abilities,"
        "card_type,card_attributes,card_attack_mod,card_defense_mod,"
        "card_cost_mod,card_damage,permanent_buffs,temporary_buffs,"
        "card_uses,resolved_at,original_template_guid,temporary_attributes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,'{}','{}','{}',0,?,0)",
        (session_id, pid, cu, champ_guid, champ_guid, "champions", 0, 0,
         row[2] or '[]', row[0], row[1] or 0, champ_guid))
    return int(cu)


def _transaction(handler, session, inner_bytes):
    """Push a 3029 PlayerTransaction through the production handler."""
    import game_engine as _ge
    # The 3029 handler reloads the session via find_session_by_player; use the
    # same lookup so our view of the state stays in sync with the DB.
    pid = int(handler.client_reck_id)
    session = gs.find_session_by_player(_ge.UID.make(244, pid).to_uint64()) \
        or session
    if os.environ.get("PVP_TRACE"):
        to = session.turn_order
        log_req(f"    [pvp-trace] pre-tx session={session.session_id} "
                f"name={session.session_name} turn_order_type="
                f"{type(to).__name__} keys="
                f"{list(to.keys())[:6] if isinstance(to, dict) else 'n/a'}")
    handler.handle_service_request(
        "ServiceGameSession", str(session.server_id), 3029, 1, 1,
        session.session_id, 0, {}, inner_bytes)
    cur = gs.find_session_by_player(
        _ge.UID.make(244, int(handler.client_reck_id)).to_uint64()) or session
    if os.environ.get("PVP_TRACE"):
        if isinstance(cur.turn_order, dict) and "turn_player" in cur.turn_order:
            log_req(f"    [pvp-trace] after {inner_bytes[:36]!r} — turn_order "
                    f"CORRUPTED keys={list(cur.turn_order.keys())[:6]}")
        else:
            log_req(f"    [pvp-trace] after {inner_bytes[:36]!r} — ok "
                    f"phase={cur.turn_order.get('phase') if isinstance(cur.turn_order, dict) else '?'}")
    return cur


def _mk_uid_bytes(uid):
    import struct
    from binascii import hexlify
    # ObjFmt field: m_UID64;<idx>;<type>;0;<little-endian-hex>;
    return (b"m_UID64;0;" + str(7).encode()
            + b";0;" + hexlify(struct.pack("<Q", int(uid))) + b";")


def _card_play_bytes(card_uid):
    # Minimal transaction: the parser only looks for m_SessionCardId ->
    # m_UID64 (the hex UID is in parts[4] of the split).
    return b"PlayCardTransaction;m_SessionCardId;" + _mk_uid_bytes(card_uid)


def _attack_bytes(attacker_uids, champ_uid):
    out = b"CommitTroopsToAttackTransaction;"
    for u in attacker_uids:
        out += _mk_uid_bytes(u)
    out += _mk_uid_bytes(champ_uid)
    return out


def _defense_bytes(attacker_uids, blocker_map, champ_uid):
    out = b"CommitTroopsToDefenseTransaction;"
    for a in attacker_uids:
        out += _mk_uid_bytes(a)
    for b in blocker_map:
        out += _mk_uid_bytes(b)
    out += _mk_uid_bytes(champ_uid)
    return out


def _play_one_game(seed, turns_cap=60):
    rnd = random.Random(seed)
    session_id = SCRATCH_BASE + seed
    pids = [5, 6]
    _cleanup(session_id, pids)

    session = gs.GameSession(session_id, session_id * 7,
                             f"tourney-{session_id}", pids[0])
    for pid in pids:
        session.add_player(encoder.make_uid(244, pid), 0)

    # Seed both players' decks from Set 1 (12 shards + 28 cards each).
    shards = [r[0] for r in _db.execute(
        "SELECT DISTINCT guid FROM card_templates WHERE set_guid=? "
        "AND card_type='Resource'", (SET1,)).fetchall()]
    others = [r[0] for r in _db.execute(
        "SELECT guid FROM card_templates WHERE set_guid=? "
        "AND card_type!='Resource' AND is_pve=0 AND no_pvp=0", (SET1,)).fetchall()]
    for i, pid in enumerate(pids):
        rnd.shuffle(others)
        deck = (shards * 12)[:12] + others[:28]
        rnd.shuffle(deck)
        _seed_deck(session_id, pid, deck, uid_offset=i * 10000)
        _seed_champion(session_id, pid, "1ae73dcf-e96e-4536-aec3-f53efb5e1c96",
                       uid_offset=i * 10000 + 9000)
    _db.commit()

    h1 = _make_handler(pids[0], session)
    h2 = _make_handler(pids[1], session)
    player_handlers[pids[0]] = h1
    player_handlers[pids[1]] = h2

    # Ready setup (both players) — persists the coin flip.
    handle_ready_for_game_setup(h1, session, {}, player_handlers)
    handle_ready_for_game_setup(h2, session, {}, player_handlers)
    state = pvp_load_state(session)
    if not state:
        state = pvp_default_state(pids[0], pids[0])
        state["goes_first_pid"] = pids[0]
        pvp_save_state(session, state)
    handle_ready_for_game_events(h1, session, {}, log_req)
    handle_ready_for_game_events(h2, session, {}, log_req)

    # Play/Draw pick: winner chooses to play.
    _transaction(h1, session, b"ChoosePlayTransaction;")
    # The user's theory: the OTHER player must also send their pick for the
    # client to leave PickGoesFirst.  Test by having the loser send the
    # complementary pick (Draw) too.
    _transaction(h2, session, b"ChooseDrawTransaction;")

    # Sequential mulligan: ask 1 redraws (player A), ask 2 redraws (player B),
    # ask 3 keeps, ask 4 keeps — exercises the alternating redraw path the
    # real client uses.
    for round_i in range(6):
        cur = gs.find_session_by_player(
            game_engine.UID.make(244, pids[0]).to_uint64()) or session
        st = pvp_load_state(cur)
        if not st or len(st.get("kept") or []) >= 2 \
                or st.get("phase") != 3:
            break
        ask = st.get("mulligan_pid")
        if ask in pids:
            if round_i in (0, 1):
                # Ask 1 (A) redraws, ask 2 (B) redraws — alternating redraw.
                _transaction(player_handlers[ask], session,
                             b"MulliganTransaction;")
            else:
                _transaction(player_handlers[ask], session,
                             b"AcceptStartingHand;")
        else:
            break

    state = pvp_load_state(session)
    log_req(f"    PvP game {seed}: phase={state.get('phase')} "
            f"turn={state.get('turn_pid')}")

    turns = 0
    guard = 0
    try:
        while turns < turns_cap and guard < 1200:
            guard += 1
            cur_session = gs.find_session_by_player(
                game_engine.UID.make(244, pids[0]).to_uint64()) or session
            state = pvp_load_state(cur_session)
            if state is None:
                log_req(f"    [pvp-autoplay] STATE LOST at guard {guard} — "
                        f"turn_order keys="
                        f"{list(cur_session.turn_order.keys()) if isinstance(cur_session.turn_order, dict) else type(cur_session.turn_order)}")
                break
            phase = state.get("phase")
            turn_pid = state.get("turn_pid")
            opp_pid = pids[1] if turn_pid == pids[0] else pids[0]
            # A triggered ability is awaiting a target: answer it (choose the
            # first legal candidate) before doing anything else.
            pend = state.get("pending_trigger")
            if pend:
                chooser_pid = int(pend.get("owner_id", turn_pid))
                candidates = []
                if pend.get("source_uid"):
                    src = int(pend["source_uid"])
                    candidates = [r[0] for r in _db.execute(
                        "SELECT card_uid FROM game_cards WHERE session_id=? "
                        "AND card_uid<>? AND location IN ('warzone','CastSpells')",
                        (session_id, src)).fetchall()]
                if not candidates:
                    candidates = [r[0] for r in _db.execute(
                        "SELECT card_uid FROM game_cards WHERE session_id=? "
                        "AND location='warzone' LIMIT 1",
                        (session_id,)).fetchall()]
                if candidates:
                    _transaction(player_handlers[chooser_pid], session,
                                 b"SetAbilityActivationDataTransaction;" +
                                 _mk_uid_bytes(candidates[0]))
                    continue
            if phase in (3, 4):
                # Setup phases are driven above; a stray pass should not loop.
                break
            if phase == game_engine.ETurnPhases.EndTurn:
                turns += 1
            # Main phases: the turn player plays a resource then an affordable
            # troop; the opponent passes immediately.
            if phase in (game_engine.ETurnPhases.FirstMainPhase,
                         game_engine.ETurnPhases.SecondMainPhase):
                _transaction(player_handlers[opp_pid], session,
                             b"PassPriorityTransaction;")
                h = player_handlers[turn_pid]
                res = _db.execute(
                    "SELECT gc.card_uid FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid=gc.template_guid "
                    "WHERE gc.session_id=? AND gc.user_id=? "
                    "AND gc.location='hand' AND ct.card_type='Resource' "
                    "ORDER BY gc.position LIMIT 1",
                    (session_id, turn_pid)).fetchone()
                if res and not _db.execute(
                        "SELECT 1 FROM game_cards WHERE session_id=? "
                        "AND user_id=? AND location='PlayedResources'",
                        (session_id, turn_pid)).fetchone():
                    _transaction(h, session, _card_play_bytes(res[0]))
                    _transaction(player_handlers[opp_pid], session,
                                 b"PassPriorityTransaction;")
                troop = _db.execute(
                    "SELECT gc.card_uid, ct.cost, ct.threshold_json FROM "
                    "game_cards gc JOIN card_templates ct "
                    "ON ct.guid=gc.template_guid WHERE gc.session_id=? "
                    "AND gc.user_id=? AND gc.location='hand' "
                    "AND ct.card_type LIKE '%Troop%' "
                    "ORDER BY gc.position LIMIT 1",
                    (session_id, turn_pid)).fetchone()
                if troop:
                    _transaction(h, session, _card_play_bytes(troop[0]))
                    _transaction(player_handlers[opp_pid], session,
                                 b"PassPriorityTransaction;")
                _transaction(h, session, b"PassPriorityTransaction;")
                continue
            # DeclareAttack: the turn player swings with all eligible troops.
            if phase == game_engine.ETurnPhases.DeclareAttack:
                rows = _db.execute(
                    "SELECT gc.card_uid, ct.attributes, gc.card_attributes, "
                    "gc.card_state FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid=gc.template_guid "
                    "WHERE gc.session_id=? AND gc.user_id=? "
                    "AND gc.location='warzone' AND gc.card_type LIKE '%Troop%'",
                    (session_id, turn_pid)).fetchall()
                attackers = []
                for cu, t_a, c_a, cstate in rows:
                    cstate = cstate or 0
                    attrs = (t_a or 0) | (c_a or 0)
                    if (cstate & game_engine.ECardStates.Tapped) \
                            or (attrs & game_engine.ECardAttributes.CantAttack):
                        continue
                    if not (cstate & game_engine.ECardStates.StartedATurnOnYourSide) \
                            and not (attrs & game_engine.ECardAttributes.Speed):
                        continue
                    attackers.append(int(cu))
                champ_map = state.get("champ_map") or {}
                my_champ = int(champ_map.get(str(turn_pid), 0))
                if attackers:
                    _transaction(player_handlers[turn_pid], session,
                                 _attack_bytes(attackers, my_champ))
                _transaction(player_handlers[opp_pid], session,
                             b"PassPriorityTransaction;")
                _transaction(player_handlers[turn_pid], session,
                             b"PassPriorityTransaction;")
                continue
            # DeclareDefense: the defender blocks with every eligible troop.
            if phase == game_engine.ETurnPhases.DeclareDefense:
                att_state = state
                attacker_uids = [int(k) for k in
                                 (att_state.get("attackers") or {})]
                blocker_rows = _db.execute(
                    "SELECT gc.card_uid, ct.attributes, gc.card_attributes, "
                    "gc.card_state, ct.attack, ct.defense, gc.card_defense_mod, "
                    "gc.card_damage FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid=gc.template_guid "
                    "WHERE gc.session_id=? AND gc.user_id=? "
                    "AND gc.location='warzone' AND gc.card_type LIKE '%Troop%'",
                    (session_id, opp_pid)).fetchall()
                avail = []
                for cu, t_a, c_a, cstate, atk, bdef, dmod, dmg in blocker_rows:
                    cstate = cstate or 0
                    attrs = (t_a or 0) | (c_a or 0)
                    if (cstate & game_engine.ECardStates.Tapped) \
                            or (attrs & game_engine.ECardAttributes.CantBlock):
                        continue
                    avail.append((int(cu), atk or 0,
                                  (bdef or 0) + (dmod or 0) - (dmg or 0)))
                # Best single blocker per attacker: the cheapest that survives
                # the hit, else the biggest (trades), else chump the biggest
                # threat.  One blocker per attacker — damage gets through.
                blockers = []
                att_stats = {}
                for a_uid in attacker_uids:
                    row = _db.execute(
                        "SELECT ct.attack FROM game_cards gc "
                        "JOIN card_templates ct ON ct.guid=gc.template_guid "
                        "WHERE gc.session_id=? AND gc.card_uid=?",
                        (session_id, int(a_uid))).fetchone()
                    att_stats[int(a_uid)] = int(row[0] or 0) if row else 0
                for a_uid in sorted(attacker_uids,
                                    key=lambda u: -att_stats.get(int(u), 0)):
                    a_atk = att_stats.get(int(a_uid), 0)
                    if not avail:
                        break
                    survivors = [b for b in avail if b[2] > a_atk]
                    if survivors:
                        pick = min(survivors, key=lambda b: b[2])
                    else:
                        pick = max(avail, key=lambda b: b[1])
                    blockers.append(pick[0])
                    avail.remove(pick)
                champ_map = state.get("champ_map") or {}
                opp_champ = int(champ_map.get(str(opp_pid), 0))
                if attacker_uids and blockers:
                    _transaction(player_handlers[opp_pid], session,
                                 _defense_bytes(attacker_uids, blockers,
                                                opp_champ))
                _transaction(player_handlers[opp_pid], session,
                             b"PassPriorityTransaction;")
                _transaction(player_handlers[turn_pid], session,
                             b"PassPriorityTransaction;")
                continue
            # Both players pass the phase (the non-turn player passes first so
            # the turn player's pass completes the pair).
            _transaction(player_handlers[opp_pid], session,
                         b"PassPriorityTransaction;")
            _transaction(player_handlers[turn_pid], session,
                         b"PassPriorityTransaction;")
    except Exception:
        return turns, traceback.format_exc()
    finally:
        _cleanup(session_id, pids)
    return turns, None


def main(games=5):
    ok = 0
    for seed in range(1, games + 1):
        turns, err = _play_one_game(seed)
        if err:
            print(f"game {seed}: CRASH after {turns} turns")
            print(err[:3000])
        else:
            ok += 1
            print(f"game {seed}: {turns} turns, no crash")
    print(f"PvP autoplay: {ok}/{games} games completed")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
