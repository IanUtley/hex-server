"""Headless PvP runner for engine smoke checks and FRA AI duels.

Drives a real tournament PvP session (tourney-N) through the production
paths: 3029 PassPriority / ChoosePlay / AcceptStartingHand transactions,
resource + troop plays, attacker/blocker declarations and combat — with two
fake HCPHandlers pushing events to no client.  The goal is to catch crashes
and stuck phases in the PvP state machine (GreenLight sync, phase wrapping,
combat).

Run the original Set 1 smoke runner with ``python3 pvp_autoplay.py [games]``.
Run a selectable FRA duel with ``python3 pvp_autoplay.py --fra
--turn-limit 30 --profile``. To run unattended, pass two deck GUIDs after
``--fra``. Without GUIDs, non-interactive runs use the first two authored
encounters with complete decks; in a terminal, the runner lists those decks
and prompts for both seats. Pass ``--choose-decks`` to request that menu
explicitly. Multiple games run in parallel by default; pass ``--workers 1``
to force serial execution or ``--workers N`` to cap the thread count. Use
``--round-robin`` to run every non-elite FRA deck against every other
non-elite deck once and print the final standings. Add ``--match-limit N``
to run only the first N pairings, which is useful for bounded profiling.
"""

import os
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import random
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from itertools import combinations

# A command-line simulation should never clean up rows in the server's live
# database.  SQLite's online backup includes committed WAL state and gives the
# runner a disposable snapshot with the authored metadata it needs.
_SIMULATION_DB_TEMP = None
_SIMULATION_DB_COPY = None
if (__name__ == "__main__" and
        ("--fra" in sys.argv or "--round-robin" in sys.argv)):
    _source_db = os.environ.get(
        "HEX_DB_PATH", os.path.join(os.path.dirname(__file__), "hconnect.db"))
    if _source_db != ":memory:" and os.path.isfile(_source_db):
        _SIMULATION_DB_TEMP = tempfile.TemporaryDirectory(
            prefix="hex-pvp-simulation-")
        _SIMULATION_DB_COPY = os.path.join(
            _SIMULATION_DB_TEMP.name, "simulation.db")
        _source_connection = sqlite3.connect(_source_db, timeout=30)
        _copy_connection = sqlite3.connect(_SIMULATION_DB_COPY, timeout=30)
        try:
            _source_connection.backup(_copy_connection)
        finally:
            _copy_connection.close()
            _source_connection.close()
        os.environ["HEX_DB_PATH"] = _SIMULATION_DB_COPY

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import game_engine
import game_session as gs
import hconnect_server as hcs
import encoder
from db import _db, log_req, set_log_level
from pvp_db import (
    db_ai_hand_playables, db_clear_session_cards,
    db_copy_template_payload, db_delete_game_session,
    db_hand_resources_with_template, db_insert_generated_card,
    db_set_constructed_guids, db_set_resource_guids,
    db_gem_templates, db_static_card_rows, db_warzone_troop_attributes,
    db_zone_card_count, db_encounter_deck_cards, db_is_champion_template,
    db_warzone_attack_candidates, db_game_champion,
    db_champion_ability_guids,
)
from domain.constants import (CARD_UID_TYPE, PLAYER_UID_TYPE,
                              PLAYER_TRANSACTION_DATA_TYPE,
                              SESSION_CARD_UID_FIELD_TYPE)
from services.tournament_game import (
    pvp_default_state, pvp_load_state, pvp_save_state,
    handle_ready_for_game_setup, handle_ready_for_game_events,
    player_handlers, player_handler_lock, _pvp_fra_view, pvp_shared_port,
)

SET1 = "0382f729-7710-432b-b761-13677982dcd2"
SCRATCH_SESSION_BASE = 987700
AUTOPLAY_PLAYER_IDS = (5, 6)
AUTOPLAY_SERVER_ID_MULTIPLIER = 7
AUTOPLAY_DECK_RESOURCE_COUNT = 12
AUTOPLAY_DECK_SPELL_COUNT = 28
AUTOPLAY_CHAMPION_UID_OFFSET = 9000
AUTOPLAY_PLAYER_UID_STRIDE = 10000
AUTOPLAY_TURN_CAP = 60
AUTOPLAY_GUARD_CAP = 1200
AUTOPLAY_MULLIGAN_ROUNDS = 6
AUTOPLAY_REDRAW_ROUNDS = (0, 1)
# The production PvP adapter indexes transient handlers by player id and
# resolves a session by player id. Parallel simulator runs therefore need
# disjoint identities even though every run has the same two seats. Keep the
# old ids for direct/private callers of _play_one_game; scheduled runs use a
# high disposable range that cannot match a real account in the snapshot.
AUTOPLAY_SIMULATION_PLAYER_BASE = 970000000
AUTOPLAY_SIMULATION_PLAYER_STRIDE = 2
# ``game_cards.id`` is a database-wide primary key, while the legacy helper
# allocates it from MAX(id) within one session. Give each scheduled run and
# seat a disjoint seed range; generated cards then continue above that range
# through the existing per-session allocator.
AUTOPLAY_ROW_ID_CHAMPION_OFFSET = 1000000
AUTOPLAY_ROW_ID_SEAT_STRIDE = 2000000
AUTOPLAY_ROW_ID_RUN_STRIDE = 4000000
AUTOPLAY_ROW_ID_START = 10001
OPENING_SETUP_PHASES = (game_engine.ETurnPhases.PickGoesFirst,
                         game_engine.ETurnPhases.Mulligan)
CARD_POSITION_CHAMPION = 0
DEFAULT_AUTOPLAY_GAMES = 5
FIRST_AUTOPLAY_SEED = 1


def _cleanup(session_id, pids):
    db_clear_session_cards(session_id)
    db_delete_game_session(session_id)
    with player_handler_lock:
        for pid in pids:
            player_handlers.pop(pid, None)


def _autoplay_identity(seed, run_slot=None):
    """Return the disposable session/player identities for one simulation.

    A complete game must remain single-threaded because its two seats share
    one authoritative RulesPort session. Different games can run in parallel
    only when every lookup key is disjoint: the PvP adapter's handler registry
    and ``find_session_by_player`` both use player ids.
    """
    session_id = SCRATCH_SESSION_BASE + int(seed)
    if run_slot is None:
        return session_id, list(AUTOPLAY_PLAYER_IDS)
    slot = int(run_slot)
    first_pid = (AUTOPLAY_SIMULATION_PLAYER_BASE +
                 slot * AUTOPLAY_SIMULATION_PLAYER_STRIDE)
    return session_id, [first_pid, first_pid + 1]


def _make_handler(pid, session, deck_personality=None):
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
    setattr(h, "_player_autopass", False)
    h._pending_player_stops = None
    h._pending_player_draws_first = None
    h._player_champ_scid = None
    h._ai_champ_scid = None
    h._player_champ_guid = None
    h._ai_champ_guid = None
    h._player_starting_health = game_engine.DEFAULT_STARTING_HEALTH
    h._ai_starting_health = game_engine.DEFAULT_STARTING_HEALTH
    setattr(h, "_autoplay_drive_ai_turn", True)
    setattr(h, "_campaign_gameend", lambda *a, **k: None)
    # Native mode exercises the same PvP RulesPort attachment used by HConnect.
    h._application = hcs.ApplicationCommandDispatcher(
        event_publisher=h._publish_application_events)
    h.send = lambda *a, **k: None
    h.send_and_cache = lambda *a, **k: None
    h._push_transaction_ack = lambda *a, **k: None
    # Inferred FRA strategy and the default combat attitude use the same AI
    # configuration path as live encounters.
    import ai as ai_policy
    ai_policy.configure_personality(
        h, deck_personality=deck_personality, campaign_personality=None)
    return h


def _autoplay_health_snapshot(state, pids):
    if not isinstance(state, dict):
        return {}
    health = {}
    for pid in pids:
        key = f"hp_{int(pid)}"
        try:
            health[str(int(pid))] = int(
                state[key] if state.get(key) is not None else 20)
        except (TypeError, ValueError):
            health[str(int(pid))] = 20
    return health


def _autoplay_report_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value) if value is not None else None


def _autoplay_hand_snapshot(session_id, pids):
    if not pids:
        return {}
    marks = ",".join("?" for _ in pids)
    rows = _db.execute(
        "SELECT gc.card_uid, gc.user_id, gc.template_guid, "
        "COALESCE(ct.name, gc.template_guid), gc.position "
        "FROM game_cards gc LEFT JOIN card_templates ct "
        "ON LOWER(ct.guid)=LOWER(gc.template_guid) "
        "WHERE gc.session_id=? AND gc.user_id IN (" + marks + ") "
        "AND LOWER(gc.location)='hand' "
        "ORDER BY gc.user_id, gc.position, gc.card_uid",
        (int(session_id), *(int(pid) for pid in pids))).fetchall()
    return {
        int(row[0]): {
            "card_uid": int(row[0]),
            "player_id": int(row[1]),
            "template_guid": str(row[2] or ""),
            "name": str(row[3] or row[2] or "Unknown"),
            "position": int(row[4] or 0),
        }
        for row in rows
    }


def _autoplay_update_hand_tracker(tracker, session_id, pids, state):
    if tracker is None:
        return {}
    turn_number = _autoplay_report_int(
        state.get("turn_number") if isinstance(state, dict) else None)
    if not isinstance(turn_number, int):
        turn_number = 1
    with tracker["lock"]:
        current = _autoplay_hand_snapshot(session_id, pids)
        cards = tracker["cards"]
        current_uids = set(current)
        for card_uid, card in current.items():
            entry = cards.get(card_uid)
            if entry is None or not entry.get("in_hand"):
                entry = {**card, "entered_turn": turn_number,
                         "in_hand": True}
                cards[card_uid] = entry
            else:
                entry.update(card)
        for card_uid, entry in cards.items():
            if entry.get("in_hand") and card_uid not in current_uids:
                entry["in_hand"] = False
    return current


def _autoplay_finalize_hand(tracker, session_id, pids, state):
    current = _autoplay_update_hand_tracker(
        tracker, session_id, pids, state)
    end_turn = _autoplay_report_int(
        state.get("turn_number") if isinstance(state, dict) else None)
    if not isinstance(end_turn, int):
        end_turn = 1
    hands = {str(int(pid)): [] for pid in pids}
    with tracker["lock"]:
        for card_uid, card in current.items():
            entry = tracker["cards"].get(card_uid, card)
            hands[str(card["player_id"])].append({
                "card_uid": card_uid,
                "template_guid": card["template_guid"],
                "name": card["name"],
                "turn_entered_hand": int(entry.get("entered_turn", end_turn)),
                # Count completed turn boundaries since the card entered hand.
                "turns_in_hand": max(
                    0, end_turn - int(entry.get("entered_turn", end_turn))),
                "position": card["position"],
            })
    return hands


def _autoplay_terminal_result(session, state, pids):
    zero_life = []
    for pid in pids:
        value = state.get(f"hp_{pid}", 20)
        try:
            health = int(value if value is not None else 20)
        except (TypeError, ValueError):
            health = 20
        if health <= 0:
            zero_life.append(int(pid))
    session_ended = str(getattr(session, "state", "") or "").lower() == "ended"
    if not zero_life and not session_ended:
        return None
    if len(zero_life) == 1:
        loser = zero_life[0]
        winner = next((int(pid) for pid in pids if int(pid) != loser), None)
        reason = "champion_at_zero_health"
    elif len(zero_life) > 1:
        loser = winner = None
        reason = "both_champions_at_zero_health"
    else:
        winner = loser = None
        reason = "session_ended"
    return {"reason": reason, "winner_id": winner, "loser_id": loser}


def _autoplay_store_game_end(report, outcome, terminal_result):
    """Store a terminal result in either a detailed or lightweight report."""
    if terminal_result is None:
        return
    if report is not None:
        report["game_end"] = terminal_result
    if outcome is not None:
        outcome["game_end"] = terminal_result


def _seed_deck(session_id, pid, deck, uid_offset=0, row_id_base=0):
    ctr = [uid_offset]
    for pos, card_spec in enumerate(deck):
        if isinstance(card_spec, (tuple, list)):
            tpl, gem_type, gem_abilities = card_spec
        else:
            tpl, gem_type, gem_abilities = card_spec, 0, ()
        ctr[0] += 1
        cu = game_engine.UID.make(CARD_UID_TYPE, ctr[0]).uid64
        card = db_copy_template_payload(tpl)
        if not card:
            raise ValueError(f"unknown autoplay template {tpl}")
        try:
            abilities = [str(value).lower()
                         for value in json.loads(card[1] or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            abilities = []
        for ability_guid in gem_abilities or ():
            value = str(ability_guid).lower()
            if value not in abilities:
                abilities.append(value)
        db_insert_generated_card(
            session_id, pid, cu, tpl, "deck", card[0],
            json.dumps(abilities), card[2],
            AUTOPLAY_ROW_ID_START + int(row_id_base) + pos, position=pos,
            gems=int(gem_type or 0))


def _seed_champion(session_id, pid, champ_guid, uid_offset=0, row_id_base=0):
    ctr = [uid_offset]
    ctr[0] += 1
    cu = game_engine.UID.make(CARD_UID_TYPE, ctr[0]).uid64
    card = db_copy_template_payload(champ_guid)
    is_catalog_champion = db_is_champion_template(champ_guid)
    if not card and not is_catalog_champion:
        raise ValueError(f"unknown autoplay champion {champ_guid}")
    # Authored FRA champions are stored in champion_templates(_extended),
    # not necessarily in card_templates.  The production PvP projection uses
    # this same catalog check and obtains signature abilities from
    # champion_abilities when it builds the champion card.
    card_type, abilities, attributes = (
        ("Champion", "[]", 0) if is_catalog_champion else card)
    db_insert_generated_card(
        session_id, pid, cu, champ_guid, "champions", card_type, abilities,
        attributes, AUTOPLAY_ROW_ID_START + int(row_id_base),
        position=CARD_POSITION_CHAMPION)
    _db.execute(
        "UPDATE game_cards SET is_champion=1 "
        "WHERE session_id=? AND card_uid=?", (session_id, int(cu)))
    return int(cu)


def _transaction(handler, session, inner_bytes, typed_payload=None,
                 profiler=None):
    """Push a 3029 PlayerTransaction through the production handler.

    ``inner_obj`` carries the labelled raw envelope so the handler recovers
    the same typed payload it recovers when ObjFmt decoding of a live request
    fails; without it the harness would only exercise the untyped ingress.
    """
    import game_engine as _ge
    # The 3029 handler reloads the session via find_session_by_player; use the
    # same lookup so our view of the state stays in sync with the DB.
    pid = int(handler.client_reck_id)
    if profiler is None:
        profiler = getattr(handler, "_autoplay_profiler", None)
    session = gs.find_session_by_player(_ge.UID.make(PLAYER_UID_TYPE, pid).to_uint64()) \
        or session
    report = getattr(handler, "_autoplay_report", None)
    report_pids = getattr(handler, "_autoplay_pids", ())
    if str(getattr(session, "state", "") or "").lower() == "ended":
        return session
    before_state = pvp_load_state(session) if report is not None else None
    if (report is not None and
            _autoplay_terminal_result(session, before_state or {}, report_pids)):
        return session
    before_health = _autoplay_health_snapshot(before_state, report_pids)
    before_phase = (before_state.get("phase")
                    if isinstance(before_state, dict) else None)
    before_turn = (before_state.get("turn_number")
                   if isinstance(before_state, dict) else None)
    if os.environ.get("PVP_TRACE"):
        to = session.turn_order
        log_req(f"    [pvp-trace] pre-tx session={session.session_id} "
                f"name={session.session_name} turn_order_type="
                f"{type(to).__name__} keys="
                f"{list(to.keys())[:6] if isinstance(to, dict) else 'n/a'}")
    inner_obj = {"__raw__": inner_bytes}
    if isinstance(typed_payload, dict):
        # Add the same labeled decoder fields that the client request parser
        # supplies. This supports target selections without bypassing the
        # production application dispatcher or PvP RulesPort adapter.
        inner_obj.update(typed_payload)
    transaction_name = (inner_bytes.split(b";", 1)[0].decode(
        "ascii", errors="replace") if isinstance(inner_bytes, bytes)
        else str(inner_bytes))
    port = (getattr(session, "_rules_port_session", None) or
            pvp_shared_port(session))
    if port is not None and profiler is not None:
        # Install this before dispatch. Installing it after the handler
        # returns profiles the next transaction, not the current one.
        port._transaction_profile_callback = (
            lambda kind, elapsed: _record_rules_port_dispatch(
                profiler, kind, elapsed))
    started = time.perf_counter()
    try:
        handler.handle_service_request(
            "ServiceGameSession", str(session.server_id),
            PLAYER_TRANSACTION_DATA_TYPE, 1, 1,
            session.session_id, 0, inner_obj, inner_bytes)
    finally:
        if profiler is not None:
            elapsed = time.perf_counter() - started
            _profile_add(profiler, "rules_transactions", 1)
            _profile_add(profiler, "rules_transaction_seconds", elapsed)
            _record_profile_timing(
                profiler, "rules_transaction_by_kind", transaction_name,
                elapsed)
    cur = gs.find_session_by_player(
        _ge.UID.make(PLAYER_UID_TYPE, int(handler.client_reck_id)).to_uint64()) or session
    if report is not None:
        after_state = pvp_load_state(cur)
        _autoplay_update_hand_tracker(
            getattr(handler, "_autoplay_hand_tracker", None),
            cur.session_id, report_pids, after_state)
        terminal_result = _autoplay_terminal_result(
            cur, after_state or {}, report_pids)
        after_health = _autoplay_health_snapshot(after_state, report_pids)
        phase_before = _autoplay_report_int(before_phase)
        phase_after = _autoplay_report_int(
            after_state.get("phase") if isinstance(after_state, dict) else None)
        health_delta = {
            owner: after_health.get(owner, old) - old
            for owner, old in before_health.items()
        }
        trace = {
            "turn": _autoplay_report_int(before_turn),
            "phase_before": phase_before,
            "phase_after": phase_after,
            "player_id": pid,
            "transaction": transaction_name,
            "health_before": before_health,
            "health_after": after_health,
            "health_delta": health_delta,
        }
        report_lock = getattr(handler, "_autoplay_report_lock", None)
        if report_lock is not None:
            report_lock.acquire()
        try:
            if any(health_delta.values()):
                report.setdefault("health_changes", []).append(trace)
            if terminal_result is not None:
                report["game_end"] = terminal_result
            damage_phases = {
                int(_ge.ETurnPhases.AssignFirstStrikeDamage),
                int(_ge.ETurnPhases.AssignDamage),
            }
            if phase_before in damage_phases:
                report.setdefault("combat_steps", []).append({
                    **trace,
                    "phase_advanced": phase_after != phase_before,
                })
        finally:
            if report_lock is not None:
                report_lock.release()
    port = (getattr(cur, "_rules_port_session", None) or
            pvp_shared_port(cur))
    if port is not None and profiler is not None:
        port._transaction_profile_callback = (
            lambda kind, elapsed: _record_rules_port_dispatch(
                profiler, kind, elapsed))
    if os.environ.get("PVP_TRACE"):
        if isinstance(cur.turn_order, dict) and "turn_player" in cur.turn_order:
            log_req(f"    [pvp-trace] after {inner_bytes[:36]!r} — turn_order "
                    f"CORRUPTED keys={list(cur.turn_order.keys())[:6]}")
        else:
            log_req(f"    [pvp-trace] after {inner_bytes[:36]!r} — ok "
                    f"phase={cur.turn_order.get('phase') if isinstance(cur.turn_order, dict) else '?'}")
    return cur


def _pass_native_priority_window(session, pids, handlers, *, phase=None,
                                 profiler=None):
    """Let each AI answer combat/end-step windows, then pass in priority order."""
    if len(pids) < 2:
        return
    expected_phase = phase
    combat_response_phases = {
        game_engine.ETurnPhases.DeclareCombatPriorityWindow,
        game_engine.ETurnPhases.DeclareAttackPriorityWindow,
        game_engine.ETurnPhases.DeclareDefensePriorityWindow,
        game_engine.ETurnPhases.FirstStrikePriorityWindow,
        game_engine.ETurnPhases.EndPhase,
    }
    for _ in range(64):
        current = gs.find_session_by_player(
            game_engine.UID.make(PLAYER_UID_TYPE, int(pids[0])).to_uint64()
        ) or session
        state = pvp_load_state(current)
        if not isinstance(state, dict):
            return
        current_phase = state.get("phase")
        if expected_phase is None:
            expected_phase = current_phase
        if current_phase != expected_phase:
            return
        priority_pid = state.get("priority_pid")
        if priority_pid is None:
            return
        try:
            priority_pid = int(priority_pid)
        except (TypeError, ValueError):
            return
        handler = handlers.get(priority_pid)
        if handler is None:
            return

        opponent_pid = next(
            (int(pid) for pid in pids if int(pid) != priority_pid), None)
        if (opponent_pid is not None and
                current_phase in combat_response_phases and
                _play_ai_combat_trick_response(
                    handler, current, state, priority_pid, opponent_pid,
                    profiler=profiler)):
            latest = gs.find_session_by_player(
                game_engine.UID.make(
                    PLAYER_UID_TYPE, int(pids[0])).to_uint64()) or current
            next_state = pvp_load_state(latest)
            if not isinstance(next_state, dict):
                return
            if next_state.get("phase") != expected_phase:
                return
            next_priority = next_state.get("priority_pid")
            try:
                if next_priority is None or int(next_priority) == priority_pid:
                    return
            except (TypeError, ValueError):
                return
            continue

        _transaction(handler, current, b"PassPriorityTransaction;",
                     profiler=profiler)

        latest = gs.find_session_by_player(
            game_engine.UID.make(PLAYER_UID_TYPE, int(pids[0])).to_uint64()
        ) or current
        next_state = pvp_load_state(latest)
        if not isinstance(next_state, dict):
            return
        if next_state.get("phase") != current_phase:
            return
        next_priority = next_state.get("priority_pid")
        if next_priority is None:
            return
        try:
            if int(next_priority) == priority_pid:
                return
        except (TypeError, ValueError):
            return


def _mk_uid_bytes(uid):
    import struct
    from binascii import hexlify
    # ObjFmt field: m_UID64;<idx>;<type>;0;<little-endian-hex>;
    return (b"m_UID64;0;" + str(SESSION_CARD_UID_FIELD_TYPE).encode()
            + b";0;" + hexlify(struct.pack("<Q", int(uid))) + b";")


# The stock client submits one Play<CardType>Transaction per card (there is no
# generic play envelope), so the harness must name the same class the client
# would or its plays fall outside the typed RulesPort ingress.  Keyed by
# ``card_templates.card_type``; a combined type uses its first listed token.
_PLAY_TRANSACTION_BY_DB_TYPE = {
    "Champion": b"PlayChampionTransaction",
    "BasicAction": b"PlaySpellTransaction",
    "QuickAction": b"PlaySpellTransaction",
    "Troop": b"PlayTroopTransaction",
    "Artifact": b"PlayArtifactTransaction",
    "Constant": b"PlayArtifactTransaction",
    "Resource": b"PlayResourceTransaction",
}


def _card_play_bytes(card_uid, card_type):
    """Client-shaped play transaction for one hand card of ``card_type``."""
    for token in str(card_type or "").split("|"):
        transaction = _PLAY_TRANSACTION_BY_DB_TYPE.get(token.strip())
        if transaction is not None:
            return transaction + b";m_SessionCardId;" + _mk_uid_bytes(card_uid)
    raise ValueError(f"autoplay cannot play card type {card_type!r}")


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
        for b in blocker_map.get(int(a), ()):
            out += _mk_uid_bytes(b)
    out += _mk_uid_bytes(champ_uid)
    return out


def _fra_encounter_specs(deck_guids=None, *, non_elite_only=False,
                         all_decks=False):
    """Load authored FRA lists as disposable PvP decks.

    Normal duel mode intentionally stops after two complete decks.  Round
    robin mode opts into every complete encounter and applies the authored
    ``is_elite`` flag in SQL so it cannot accidentally include an elite deck.
    """
    query = (
        "SELECT deck_guid, deck_name, champion_guid, "
        "COALESCE(NULLIF(TRIM(ai_deck_personality), ''), 'Default') "
        "FROM fra_encounters "
    )
    if non_elite_only:
        query += "WHERE COALESCE(is_elite, 0)=0 "
    query += "ORDER BY deck_name, deck_guid"
    rows = _db.execute(query).fetchall()
    by_guid = {str(row[0]).lower(): row for row in rows}
    if deck_guids:
        chosen = []
        for guid in deck_guids:
            row = by_guid.get(str(guid).lower())
            if row is None:
                raise ValueError(f"unknown FRA encounter deck {guid!r}")
            chosen.append(row)
    else:
        chosen = rows

    specs = []
    gem_templates = {
        str(name): (int(gem_type or 0),
                    [str(value).lower() for value in
                     json.loads(abilities_json or "[]")])
        for name, gem_type, abilities_json in db_gem_templates(conn=_db)
    }
    for deck_guid, name, champion_guid, personality in chosen:
        cards = []
        authored_cards = db_encounter_deck_cards(deck_guid, conn=_db)
        for card_guid, quantity, gem_json in authored_cards:
            try:
                gem_slots = json.loads(gem_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                gem_slots = []
            for copy_index in range(max(0, int(quantity or 0))):
                socket_names = (gem_slots[copy_index]
                                if copy_index < len(gem_slots) else [])
                if not isinstance(socket_names, list):
                    socket_names = [socket_names]
                gem_type = 0
                gem_abilities = []
                for socket_name in socket_names:
                    gem_data = gem_templates.get(str(socket_name))
                    if not gem_data:
                        continue
                    if not gem_type:
                        gem_type = gem_data[0]
                    for ability_guid in gem_data[1]:
                        if ability_guid not in gem_abilities:
                            gem_abilities.append(ability_guid)
                cards.append((str(card_guid), gem_type, gem_abilities))
        if not cards or not champion_guid:
            continue
        specs.append({"deck_guid": str(deck_guid), "name": str(name),
                      "champion_guid": str(champion_guid), "cards": cards,
                      "ai_deck_personality": personality or "Default"})
        if not all_decks and len(specs) == 2:
            break
    if len(specs) < 2:
        raise ValueError("need two FRA encounter decks with champions and cards")
    return specs


def _choose_fra_deck_guids(input_fn=input, output_fn=print):
    """Prompt for both seats from the authored FRA decks with complete lists."""
    rows = _db.execute(
        "SELECT deck_guid, deck_name, champion_guid FROM fra_encounters "
        "ORDER BY deck_name, deck_guid").fetchall()
    choices = []
    for deck_guid, name, champion_guid in rows:
        cards = db_encounter_deck_cards(deck_guid, conn=_db)
        if cards and champion_guid:
            choices.append((str(deck_guid), str(name),
                            str(champion_guid)))
    if not choices:
        raise ValueError("no FRA encounter decks with champions and cards")

    output_fn("Available FRA decks:")
    for index, (deck_guid, name, champion_guid) in enumerate(choices, 1):
        output_fn(f"  {index}. {name} [champion {champion_guid}; "
                  f"deck {deck_guid}]")

    selected = []
    for seat in (1, 2):
        while True:
            try:
                answer = input_fn(f"Choose deck for seat {seat} "
                                  "(number, name, or GUID): ").strip()
            except EOFError as exc:
                raise ValueError("FRA deck selection ended before both seats were chosen") from exc
            if not answer:
                output_fn("Enter a deck number, name, or GUID.")
                continue
            by_guid = next((choice for choice in choices
                            if answer.casefold() == choice[0].casefold()), None)
            if by_guid is not None:
                selected.append(by_guid[0])
                break
            by_name = [choice for choice in choices
                       if answer.casefold() == choice[1].casefold()]
            if len(by_name) == 1:
                selected.append(by_name[0][0])
                break
            if len(by_name) > 1:
                output_fn("That deck name is ambiguous; choose its number "
                          "or GUID.")
                continue
            try:
                index = int(answer)
            except ValueError:
                index = 0
            if 1 <= index <= len(choices):
                selected.append(choices[index - 1][0])
                break
            output_fn("No matching FRA deck; choose a listed number, name, "
                      "or GUID.")
    return selected


def _ai_evaluator_for_seat(handler, session, state, owner_id, opponent_id,
                           profiler, report):
    """Build the existing hand evaluator with this PvP seat as its AI side."""
    from ai_eval import build_evaluator

    champ_map = state.get("champ_map") or {}
    own_champ = int(champ_map.get(str(owner_id), 0) or 0)
    opp_champ = int(champ_map.get(str(opponent_id), 0) or 0)
    handler.user_profile = {"id": int(owner_id), "name": f"FRA-{owner_id}"}
    handler._ai_champ_scid = (game_engine.SessionCardId(
        game_engine.UID(own_champ)) if own_champ else None)
    handler._player_champ_scid = (game_engine.SessionCardId(
        game_engine.UID(opp_champ)) if opp_champ else None)

    view = dict(state)
    view.update({
        "ai_resources": int(state.get(f"res_{owner_id}", 0) or 0),
        "ai_total_resources": int(
            state.get(f"res_total_{owner_id}", 0) or 0),
        "ai_threshold": state.get(f"thresh_{owner_id}") or {},
        "ai_health": int(state.get(f"hp_{owner_id}", 20) or 0),
        "player_health": int(state.get(f"hp_{opponent_id}", 20) or 0),
    })
    handler._current_bstate = view
    evaluator = build_evaluator(
        handler, session, view,
        game_engine.UID.make(PLAYER_UID_TYPE, owner_id),
        game_engine.UID.make(PLAYER_UID_TYPE, opponent_id),
        ai_owner_id=owner_id, player_owner_id=opponent_id)
    return evaluator


def _record_card_decision(report, evaluator, chosen, choice_reason,
                          only_quick_actions=False):
    if report is None:
        return
    owner_id = int(evaluator.ai_owner_id)
    decisions = report.setdefault("card_decisions", {}).setdefault(
        str(owner_id), {})
    for card in evaluator.hand:
        if only_quick_actions and not card.is_quick_action():
            continue
        key = f"{card.template_guid}:{card.name}"
        entry = decisions.setdefault(key, {
            "template_guid": card.template_guid,
            "name": card.name,
            "seen": 0,
            "selected": 0,
            "played": 0,
            "reasons": Counter(),
        })
        entry["seen"] += 1
        playability = evaluator.is_playable(card)
        if playability == "False":
            if not evaluator.handler._thresholds_met(
                    card.threshold_json, evaluator.threshold):
                reason = "threshold_unavailable"
            elif (card.is_action()
                  and evaluator.has_required_explicit_target(card)
                  and evaluator.choose_action_target(card) is None):
                reason = "required_target_unavailable"
            else:
                reason = "other_legality_or_target_unavailable"
        elif playability == "NeedsResources":
            reason = "needs_resources"
        elif card is chosen:
            reason = choice_reason or "selected"
            entry["selected"] += 1
        elif card.is_resource():
            reason = "resource_slot_used_or_not_selected"
        elif card.is_quick_action():
            reason = (choice_reason if only_quick_actions else
                      "quick_action_not_selected_in_main_phase")
        elif chosen is None:
            reason = "no_preferred_action"
        else:
            reason = "lower_priority_than_selected"
        entry["reasons"][reason] += 1


def _record_resource_decision(report, owner_id, row, played):
    """Account for a resource choice made before the hand evaluator runs."""
    if report is None:
        return
    template_guid = str(row[2])
    card_uid = int(row[1])
    name_row = _db.execute(
        "SELECT name FROM card_templates WHERE guid=?", (template_guid,)
    ).fetchone()
    name = str(name_row[0] if name_row else template_guid)
    key = f"{template_guid}:{name}"
    entry = report.setdefault("card_decisions", {}).setdefault(
        str(int(owner_id)), {}).setdefault(key, {
            "template_guid": template_guid,
            "name": name,
            "seen": 0,
            "selected": 0,
            "played": 0,
            "reasons": Counter(),
        })
    entry["seen"] += 1
    entry["selected"] += 1
    entry["reasons"]["resource_play"] += 1
    if played:
        entry["played"] += 1
    else:
        entry["reasons"]["selected_but_not_played"] += 1


def _record_failed_card_play(report, owner_id, chosen):
    if report is None or chosen is None:
        return
    key = f"{chosen.template_guid}:{chosen.name}"
    entry = report.get("card_decisions", {}).get(str(int(owner_id)), {}).get(key)
    if entry is not None:
        entry["reasons"]["selected_but_not_played"] += 1


def _effective_combat_attributes(session_id, owner_id, battle_state, cards):
    """Refresh evaluator cards with dynamic card attributes for combat."""
    from rules_port.static_rules import effective_stats

    by_uid = {card.card_uid: card for card in cards}
    for row in db_warzone_troop_attributes(session_id, owner_id):
        card_uid = int(row[0])
        card = by_uid.get(card_uid)
        if card is None:
            continue
        card.attributes = int(effective_stats(
            _db, session_id, battle_state, card_uid)[2] or 0)
        card.card_state = int(row[1] or 0)
    return by_uid


def _ai_attack_decision(handler, session, state, owner_id, opponent_id,
                        profiler=None, report=None):
    """Use the shared AI combat valuation for a FRA seat's attack choice."""
    started = time.perf_counter()
    evaluator = _ai_evaluator_for_seat(
        handler, session, state, owner_id, opponent_id, profiler, report)
    import ai as ai_policy

    own_cards = _effective_combat_attributes(
        session.session_id, owner_id, state, evaluator.ai_warzone)
    opposing_cards = _effective_combat_attributes(
        session.session_id, opponent_id, state, evaluator.player_warzone)
    blockers = [card for card in opposing_cards.values()
                if card.is_troop()
                and not (int(card.card_state or 0)
                         & game_engine.ECardStates.Tapped)
                and not card.has_attribute(game_engine.ECardAttributes.CantBlock)]

    eligible = []
    for card_uid, _template, attrs, _card_attrs, card_state, _attack in \
            db_warzone_attack_candidates(session.session_id, owner_id):
        card_uid = int(card_uid)
        card = own_cards.get(card_uid)
        if card is None:
            continue
        card_state = int(card_state or 0)
        attrs = int(attrs or 0) | int(_card_attrs or 0) | int(
            card.attributes or 0)
        if card_state & game_engine.ECardStates.Tapped:
            continue
        if attrs & (game_engine.ECardAttributes.CantAttack |
                    game_engine.ECardAttributes.Defensive):
            continue
        if not (card_state & game_engine.ECardStates.StartedATurnOnYourSide) \
                and not attrs & game_engine.ECardAttributes.Speed:
            continue
        card.attributes = attrs
        eligible.append(card)

    chosen_uids, reason = ai_policy.ai_choose_attackers(
        handler, evaluator, eligible, blockers)
    chosen_uid_set = set(chosen_uids)
    chosen = [card for card in eligible
              if int(card.card_uid) in chosen_uid_set]

    if profiler is not None:
        _profile_add(profiler, "combat_evaluations", 1)
        _profile_add(profiler, "combat_evaluation_seconds",
                     time.perf_counter() - started)
    if report is not None:
        report.setdefault("combat_decisions", []).append({
            "turn": int(state.get("turn", 0) or 0),
            "player_id": int(owner_id),
            "kind": "attack",
            "reason": reason,
            "attackers": [int(card.card_uid) for card in chosen],
            "eligible": [int(card.card_uid) for card in eligible],
            "blockers": [int(card.card_uid) for card in blockers],
        })
    return [int(card.card_uid) for card in chosen], reason


def _ai_block_decision(handler, session, state, owner_id, opponent_id,
                       attacker_uids, profiler=None, report=None):
    """Select legal, value-positive one-to-one blocks with AICombat values."""
    started = time.perf_counter()
    evaluator = _ai_evaluator_for_seat(
        handler, session, state, owner_id, opponent_id, profiler, report)
    from ai_eval import ECardAttributes

    own_cards = _effective_combat_attributes(
        session.session_id, owner_id, state, evaluator.ai_warzone)
    opposing_cards = _effective_combat_attributes(
        session.session_id, opponent_id, state, evaluator.player_warzone)
    blockers = [card for card in own_cards.values()
                if card.is_troop()
                and not (int(card.card_state or 0)
                         & game_engine.ECardStates.Tapped)
                and not card.has_attribute(ECardAttributes.CantBlock)]
    attackers = [opposing_cards[int(uid)] for uid in attacker_uids
                 if int(uid) in opposing_cards]
    attackers.sort(key=lambda card: -card.effective_attack(in_play=True))
    available = list(blockers)
    blocker_map = {}
    details = []
    for attacker in attackers:
        best = None
        for blocker in available:
            if not evaluator._can_block(blocker, attacker):
                continue
            damage, blocker_value, attacker_dies, attacker_value = \
                evaluator._damage_through(attacker, [blocker])
            prevented = max(0, attacker.effective_attack(in_play=True) - damage)
            blocker_dies = attacker.effective_attack(in_play=True) >= \
                blocker.effective_defense(in_play=True) and not \
                blocker.has_attribute(ECardAttributes.Immortal)
            score = (attacker_value if attacker_dies else 0.0) - \
                (blocker_value if blocker_dies else 0.0) + \
                prevented * evaluator.personality.values["DamageParityValue"] / 20.0
            if best is None or score > best[0]:
                best = (score, blocker, damage, attacker_dies, blocker_dies)
        if best is not None and best[0] > 0:
            score, blocker, damage, attacker_dies, blocker_dies = best
            blocker_map[int(attacker.card_uid)] = [int(blocker.card_uid)]
            available.remove(blocker)
            details.append({
                "attacker": int(attacker.card_uid),
                "blocker": int(blocker.card_uid),
                "score": round(float(score), 4),
                "attacker_dies": bool(attacker_dies),
                "blocker_dies": bool(blocker_dies),
                "damage_through": int(damage),
            })

    if profiler is not None:
        _profile_add(profiler, "combat_evaluations", 1)
        _profile_add(profiler, "combat_evaluation_seconds",
                     time.perf_counter() - started)
    if report is not None:
        report.setdefault("combat_decisions", []).append({
            "turn": int(state.get("turn", 0) or 0),
            "player_id": int(owner_id),
            "kind": "block",
            "attackers": [int(uid) for uid in attacker_uids],
            "blocks": details,
        })
    return blocker_map


def _profile_add(profiler, key, amount):
    lock = profiler.get("_lock")
    if lock is None:
        profiler[key] = profiler.get(key, 0) + amount
    else:
        with lock:
            profiler[key] = profiler.get(key, 0) + amount


def _record_profile_timing(profiler, bucket_key, kind, elapsed):
    """Accumulate count and wall time for one profiled operation kind."""
    lock = profiler.get("_lock")

    def record():
        bucket = profiler.setdefault(bucket_key, {})
        item = bucket.setdefault(str(kind), {"count": 0, "seconds": 0.0})
        item["count"] += 1
        item["seconds"] += float(elapsed)

    if lock is None:
        record()
    else:
        with lock:
            record()


def _accepted_ability_activation(history, history_before, source_uid,
                                 ability_guid):
    """Match an accepted activation without depending on player UID flavor."""
    guid = str(ability_guid or "").lower()
    source_uid = int(source_uid or 0)
    for item in list(history or ())[int(history_before or 0):]:
        if str(item.get("kind") or "") != "activate_ability":
            continue
        payload = item.get("payload") or {}
        try:
            same_source = int(payload.get("source_card_id", 0) or 0) == source_uid
        except (TypeError, ValueError):
            same_source = False
        if (same_source and
                str(payload.get("ability_template_id") or "").lower() == guid):
            return True
    return False


def _record_rules_port_dispatch(profiler, kind, elapsed):
    lock = profiler.get("_lock")

    def record():
        profiler["rules_port_dispatches"] += 1
        profiler["rules_port_dispatch_seconds"] += float(elapsed)
        by_kind = profiler["rules_port_by_kind"]
        item = by_kind.setdefault(str(kind), {"count": 0, "seconds": 0.0})
        item["count"] += 1
        item["seconds"] += float(elapsed)

    if lock is None:
        record()
    else:
        with lock:
            record()


def _ai_champion_power_decision(handler, session, state, owner_id,
                                opponent_id, profiler=None, report=None):
    """Ask the shared FRA champion-power policy for a PvP seat's next power.

    The selector expects the active AI on the ``ai_*`` side of its effect
    view.  This function supplies that side-oriented view, legal champion
    records, and the seat's authored champion powers, then restores the fake
    handler's prior view so alternating seats cannot leak state into one
    another.
    """
    owner_id = int(owner_id)
    opponent_id = int(opponent_id)
    champion = db_game_champion(session.session_id, owner_id, conn=_db)
    opponent_champion = db_game_champion(
        session.session_id, opponent_id, conn=_db)
    if not champion:
        return None

    import ai as ai_policy

    champion_uid, champion_guid = int(champion[0]), str(champion[1])
    opponent_champion_uid = (int(opponent_champion[0])
                             if opponent_champion else 0)
    ability_rows = _db.execute(
        "SELECT ability_guid, ability_name FROM champion_abilities "
        "WHERE champion_guid=? ORDER BY ability_guid", (champion_guid,)
    ).fetchall()
    ability_names = {str(guid).lower(): str(name or "")
                     for guid, name in ability_rows}
    ai_t = game_engine.UID.make(PLAYER_UID_TYPE, owner_id)
    pl_t = game_engine.UID.make(PLAYER_UID_TYPE, opponent_id)
    effect_view = _pvp_fra_view(state, opponent_id, owner_id)
    effect_view["phase"] = int(state.get("phase", 0) or 0)
    effect_view["ai_sp_uses"] = dict(
        state.get(f"sp_uses_{owner_id}") or {})
    champion_targets = []
    if opponent_champion_uid:
        champion_targets.append((
            opponent_champion_uid, opponent_id, "Player",
            int(state.get(f"hp_{opponent_id}", 20) or 0)))
    champion_targets.append((
        champion_uid, owner_id, "AI",
        int(state.get(f"hp_{owner_id}", 20) or 0)))

    saved = {}
    sentinel = object()
    names = ("user_profile", "_current_bstate", "_ai_champ_scid",
             "_player_champ_scid", "_ai_champ_ability_guids",
             "_champion_targets")
    for name in names:
        saved[name] = getattr(handler, name, sentinel)
    started = time.perf_counter()
    chosen = None
    try:
        handler.user_profile = {"id": owner_id, "name": f"FRA-{owner_id}"}
        handler._current_bstate = effect_view
        handler._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID(champion_uid))
        handler._player_champ_scid = (
            game_engine.SessionCardId(game_engine.UID(opponent_champion_uid))
            if opponent_champion_uid else None)
        handler._ai_champ_ability_guids = db_champion_ability_guids(
            champion_guid, conn=_db)
        handler._champion_targets = lambda: list(champion_targets)
        chosen = ai_policy.ai_use_champion_ability(
            handler, None, session, ai_t, pl_t, effect_view,
            decision_only=True, ai_owner_id=owner_id,
            player_owner_id=opponent_id)
    finally:
        for name, value in saved.items():
            if value is sentinel:
                try:
                    delattr(handler, name)
                except AttributeError:
                    pass
            else:
                setattr(handler, name, value)
        if profiler is not None:
            _profile_add(profiler, "ai_power_evaluations", 1)
            _profile_add(profiler, "ai_power_evaluation_seconds",
                         time.perf_counter() - started)

    if chosen is not None and not isinstance(chosen, dict):
        log_req("    [pvp-autoplay] discarded malformed champion-power "
                f"decision ({type(chosen).__name__})")
        chosen = None
    if report is not None:
        report.setdefault("ability_decisions", []).append({
            "turn": int(state.get("turn_number", 1) or 1),
            "player_id": owner_id,
            "decision": "selected" if chosen else "none",
            "reason": ("selected_by_fra_power_policy" if chosen else
                       "no_worthwhile_affordable_power"),
            "available_ability_guids": list(ability_names),
            "charges": int(state.get(f"chg_{owner_id}", 0) or 0),
            "spell_points": int(state.get(f"sp_{owner_id}", 0) or 0),
            "ability_guid": (chosen.get("ability_guid") if chosen else None),
            "ability_name": (ability_names.get(
                str(chosen.get("ability_guid") or "").lower(), "")
                if chosen else ""),
            "target_uid": (chosen.get("target_uid") if chosen else None),
            "sacrifice_uids": (list(chosen.get("sacrifice_uids") or [])
                               if chosen else []),
            "charge_cost": (int(chosen.get("charge_cost", 0) or 0)
                            if chosen else 0),
            "spell_cost": (int(chosen.get("spell_cost", 0) or 0)
                           if chosen else 0),
        })
    return chosen


def _champion_power_bytes(source_uid, decision):
    """Build the named activation fields used by the normal PvP ingress."""
    ability_guid = str(decision["ability_guid"]).lower()
    raw = (b"ActivateAbilityTransaction;m_AbilityActivationData;"
           b"SourceCardId;m_SessionCardId;" + _mk_uid_bytes(source_uid)
           + b"AbilityTemplateId;m_Guid;" + ability_guid.encode("ascii")
           + b";")
    target_uid = decision.get("target_uid")
    if target_uid is not None:
        raw += b"TargetMap;" + _mk_uid_bytes(target_uid)
    sacrifice_uids = list(decision.get("sacrifice_uids") or ())
    if sacrifice_uids:
        raw += b"CardsToSacrifice;" + b"".join(
            _mk_uid_bytes(uid) for uid in sacrifice_uids)
    return raw


def _manual_ability_bytes(source_uid, ability_guid, target_uid=None):
    """Client-shaped manual ability activation for a card source."""
    raw = (b"ActivateAbilityTransaction;m_AbilityActivationData;"
           b"SourceCardId;m_SessionCardId;" + _mk_uid_bytes(source_uid)
           + b"AbilityTemplateId;m_Guid;"
           + str(ability_guid).lower().encode("ascii") + b";")
    if target_uid is not None:
        raw += b"TargetMap;" + _mk_uid_bytes(target_uid)
    return raw


def _ai_warzone_ability_decision(handler, session, state, owner_id,
                                opponent_id, profiler=None, report=None,
                                include_non_troops=False,
                                resource_sink=False):
    """Use the FRA manual-warzone-ability chooser for either PvP seat."""
    owner_id = int(owner_id)
    opponent_id = int(opponent_id)
    champion = db_game_champion(session.session_id, owner_id, conn=_db)
    opponent_champion = db_game_champion(
        session.session_id, opponent_id, conn=_db)
    if not champion:
        return None

    import ai as ai_policy

    champion_uid = int(champion[0])
    opponent_champion_uid = (int(opponent_champion[0])
                             if opponent_champion else 0)
    ai_t = game_engine.UID.make(PLAYER_UID_TYPE, owner_id)
    pl_t = game_engine.UID.make(PLAYER_UID_TYPE, opponent_id)
    effect_view = _pvp_fra_view(state, opponent_id, owner_id)
    effect_view["phase"] = int(state.get("phase", 0) or 0)
    champion_targets = []
    if opponent_champion_uid:
        champion_targets.append((
            opponent_champion_uid, opponent_id, "Player",
            int(state.get(f"hp_{opponent_id}", 20) or 0)))
    champion_targets.append((
        champion_uid, owner_id, "AI",
        int(state.get(f"hp_{owner_id}", 20) or 0)))

    saved = {}
    sentinel = object()
    names = ("user_profile", "_current_bstate", "_ai_champ_scid",
             "_player_champ_scid", "_champion_targets")
    for name in names:
        saved[name] = getattr(handler, name, sentinel)
    started = time.perf_counter()
    chosen = None
    try:
        handler.user_profile = {"id": owner_id, "name": f"FRA-{owner_id}"}
        handler._current_bstate = effect_view
        handler._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID(champion_uid))
        handler._player_champ_scid = (
            game_engine.SessionCardId(game_engine.UID(opponent_champion_uid))
            if opponent_champion_uid else None)
        handler._champion_targets = lambda: list(champion_targets)
        chosen = ai_policy.ai_use_warzone_ability(
            handler, None, session, ai_t, pl_t, effect_view,
            include_non_troops=include_non_troops,
            resource_sink=resource_sink,
            decision_only=True, ai_owner_id=owner_id,
            player_owner_id=opponent_id)
    finally:
        for name, value in saved.items():
            if value is sentinel:
                try:
                    delattr(handler, name)
                except AttributeError:
                    pass
            else:
                setattr(handler, name, value)
        if profiler is not None:
            _profile_add(profiler, "ai_manual_ability_evaluations", 1)
            _profile_add(profiler, "ai_manual_ability_evaluation_seconds",
                         time.perf_counter() - started)

    if chosen is not None and not isinstance(chosen, dict):
        log_req("    [pvp-autoplay] discarded malformed warzone ability "
                f"decision ({type(chosen).__name__})")
        chosen = None
    if report is not None:
        report.setdefault("ability_decisions", []).append({
            "turn": int(state.get("turn_number", 1) or 1),
            "player_id": owner_id,
            "kind": "resource_sink" if resource_sink else "warzone_ability",
            "decision": "selected" if chosen else "none",
            "reason": (("selected_by_fra_resource_sink_policy"
                        if resource_sink else
                        "selected_by_fra_warzone_policy") if chosen else
                       ("no_worthwhile_affordable_resource_sink"
                        if resource_sink else
                        "no_worthwhile_affordable_warzone_ability")),
            "ability_guid": (chosen.get("ability_guid") if chosen else None),
            "source_uid": (chosen.get("source_uid") if chosen else None),
            "target_uid": (chosen.get("target_uid") if chosen else None),
            "x_cost": (int(chosen.get("x_cost", 0) or 0) if chosen else 0),
            "resource_cost": (int(chosen.get("resource_cost", 0) or 0)
                              if chosen else 0),
        })
    return chosen


def _ai_tunnel_decision(handler, session, state, owner_id, opponent_id,
                        profiler=None, report=None):
    """Ask the shared FRA hand-tunneling selector for a PvP seat."""
    import ai as ai_policy

    owner_id = int(owner_id)
    opponent_id = int(opponent_id)
    effect_view = _pvp_fra_view(state, opponent_id, owner_id)
    effect_view["phase"] = int(state.get("phase", 0) or 0)
    ai_t = game_engine.UID.make(PLAYER_UID_TYPE, owner_id)
    pl_t = game_engine.UID.make(PLAYER_UID_TYPE, opponent_id)
    started = time.perf_counter()
    chosen = ai_policy.ai_tunnel_hand_troop(
        handler, None, session, ai_t, pl_t, effect_view,
        decision_only=True, ai_owner_id=owner_id)
    if chosen is not None and not isinstance(chosen, dict):
        log_req("    [pvp-autoplay] discarded malformed tunnel decision "
                f"({type(chosen).__name__})")
        chosen = None
    if profiler is not None:
        _profile_add(profiler, "ai_tunnel_evaluations", 1)
        _profile_add(profiler, "ai_tunnel_evaluation_seconds",
                     time.perf_counter() - started)
    if report is not None:
        report.setdefault("ability_decisions", []).append({
            "turn": int(state.get("turn_number", 1) or 1),
            "player_id": owner_id,
            "kind": "tunnel",
            "decision": "selected" if chosen else "none",
            "reason": ("selected_by_fra_tunnel_policy" if chosen else
                       "no_worthwhile_affordable_tunnel"),
            "ability_guid": (chosen.get("ability_guid") if chosen else None),
            "source_uid": (chosen.get("source_uid") if chosen else None),
            "resource_cost": (int(chosen.get("resource_cost", 0) or 0)
                              if chosen else 0),
        })
    return chosen


def _ai_main_decision(handler, session, state, owner_id, opponent_id,
                      profiler=None, report=None, pre_combat=True):
    """Run the shared FRA card and champion-power selection order."""
    started = time.perf_counter()
    evaluator = _ai_evaluator_for_seat(
        handler, session, state, owner_id, opponent_id, profiler, report)
    import ai as ai_policy

    ai_t = game_engine.UID.make(PLAYER_UID_TYPE, int(owner_id))
    pl_t = game_engine.UID.make(PLAYER_UID_TYPE, int(opponent_id))
    card_decision = ai_policy.ai_choose_main_phase_card(
        handler, session, state, ai_t, pl_t, pre_combat=pre_combat,
        stage="early", evaluator=evaluator,
        ai_owner_id=int(owner_id), player_owner_id=int(opponent_id))
    power_decision = None
    if card_decision is None:
        power_decision = _ai_champion_power_decision(
            handler, session, state, owner_id, opponent_id,
            profiler=profiler, report=report)
    warzone_decision = None
    if card_decision is None and power_decision is None:
        warzone_decision = _ai_warzone_ability_decision(
            handler, session, state, owner_id, opponent_id,
            profiler=profiler, report=report)
    if (card_decision is None and power_decision is None
            and warzone_decision is None):
        card_decision = ai_policy.ai_choose_main_phase_card(
            handler, session, state, ai_t, pl_t,
            pre_combat=pre_combat, stage="lockdown",
            evaluator=evaluator, ai_owner_id=int(owner_id),
            player_owner_id=int(opponent_id))
    if (card_decision is None and power_decision is None
            and warzone_decision is None):
        card_decision = ai_policy.ai_choose_main_phase_card(
            handler, session, state, ai_t, pl_t, pre_combat=pre_combat,
            stage="threat", evaluator=evaluator,
            ai_owner_id=int(owner_id), player_owner_id=int(opponent_id))
    tunnel_decision = None
    if (card_decision is None and power_decision is None
            and warzone_decision is None):
        tunnel_decision = _ai_tunnel_decision(
            handler, session, state, owner_id, opponent_id,
            profiler=profiler, report=report)
    if (card_decision is None and power_decision is None
            and warzone_decision is None and tunnel_decision is None):
        card_decision = ai_policy.ai_choose_main_phase_card(
            handler, session, state, ai_t, pl_t, pre_combat=pre_combat,
            stage="board", evaluator=evaluator,
            ai_owner_id=int(owner_id), player_owner_id=int(opponent_id))
    unplayable_board_choice = None
    if (card_decision is not None
            and card_decision.get("reason") == "best_board_builder"
            and evaluator.is_playable(card_decision["card"]) != "True"):
        unplayable_board_choice = card_decision["card"]
        card_decision = None
    resource_sink_decision = None
    if (not pre_combat and card_decision is None
            and power_decision is None and warzone_decision is None
            and tunnel_decision is None):
        resource_sink_decision = _ai_warzone_ability_decision(
            handler, session, state, owner_id, opponent_id,
            profiler=profiler, report=report, include_non_troops=True,
            resource_sink=True)

    chosen = card_decision.get("card") if card_decision else None
    reason = (card_decision.get("reason") if card_decision else
              "champion_power" if power_decision else
              "warzone_ability" if warzone_decision else
              "tunnel" if tunnel_decision else
              "resource_sink" if resource_sink_decision else "pass_no_play")
    _record_card_decision(
        report, evaluator, chosen or unplayable_board_choice,
        "board_builder_not_playable" if unplayable_board_choice else reason)
    if profiler is not None:
        _profile_add(profiler, "ai_evaluations", 1)
        _profile_add(profiler, "ai_evaluation_seconds",
                     time.perf_counter() - started)
    if power_decision is not None:
        return {"kind": "champion_power", "power": power_decision,
                "reason": "champion_power"}
    if warzone_decision is not None:
        return {"kind": "warzone_ability", "ability": warzone_decision,
                "reason": "warzone_ability"}
    if tunnel_decision is not None:
        return {"kind": "tunnel", "ability": tunnel_decision,
                "reason": "tunnel"}
    if resource_sink_decision is not None:
        return {"kind": "resource_sink", "ability": resource_sink_decision,
                "reason": "resource_sink"}
    if chosen is not None:
        return {"kind": "card", "card": chosen,
                "target_uid": card_decision.get("target_uid"),
                "target_uids": list(card_decision.get("target_uids") or (
                    [card_decision["target_uid"]]
                    if card_decision.get("target_uid") is not None else [])),
                "target_map": card_decision.get("target_map"),
                "x_cost": int(card_decision.get("x_cost", 0) or 0),
                "reason": reason}
    return {"kind": "pass", "reason": "pass_no_play"}


def _ai_quick_decision(handler, session, state, owner_id, opponent_id,
                       profiler=None, report=None):
    """Choose a legal quick removal after the opponent makes a play."""
    started = time.perf_counter()
    evaluator = _ai_evaluator_for_seat(
        handler, session, state, owner_id, opponent_id, profiler, report)
    chosen = target_uid = None
    x_cost = 0
    reason = "no_preferred_quick_action"
    for threat in evaluator.threatening_targets():
        candidate, variable_cost, target = evaluator.find_removal_for(
            threat, quick=True)
        if (candidate is not None
                and evaluator.is_playable(candidate) == "True"):
            chosen, x_cost, target_uid = (
                candidate, variable_cost, target)
            reason = "quick_threat_removal"
            break

    _record_card_decision(
        report, evaluator, chosen, reason, only_quick_actions=True)
    if profiler is not None:
        _profile_add(profiler, "ai_evaluations", 1)
        _profile_add(profiler, "ai_evaluation_seconds",
                     time.perf_counter() - started)
    return chosen, target_uid, x_cost, reason


def _play_ai_combat_trick_response(handler, session, state, owner_id,
                                   opponent_id, profiler=None):
    """Submit the normal AI's combat-trick choice through the PvP wire path."""
    started = time.perf_counter()
    report = getattr(handler, "_autoplay_report", None)
    evaluator = _ai_evaluator_for_seat(
        handler, session, state, owner_id, opponent_id,
        profiler, report)
    combat_state = evaluator.bstate
    attackers = {
        int(key): int(value)
        for key, value in (state.get("attackers") or {}).items()
    }
    blockers = {
        int(key): [int(uid) for uid in values or ()]
        for key, values in (state.get("blockers") or {}).items()
    }
    attacking_pid = int(state.get("turn_pid") or 0)
    ai_attacking = attacking_pid == int(owner_id)
    combat_state["ai_attackers"] = attackers if ai_attacking else {}
    combat_state["player_attackers"] = attackers if not ai_attacking else {}
    # ai_play_combat_trick interprets this map relative to the AI's side:
    # blocked own attackers when attacking, or own blockers against attackers
    # when defending. The PvP checkpoint stores both in attacker-keyed form.
    combat_state["ai_blockers"] = blockers

    import ai as ai_policy
    ai_t = game_engine.UID.make(PLAYER_UID_TYPE, int(owner_id))
    pl_t = game_engine.UID.make(PLAYER_UID_TYPE, int(opponent_id))
    decision = ai_policy.ai_choose_combat_trick(
        handler, session, combat_state, ai_t, pl_t,
        evaluator=evaluator)
    chosen = decision["card"] if decision else None
    reason = (decision.get("reason") if decision else
              "no_preferred_combat_trick")
    _record_card_decision(
        report, evaluator, chosen, reason, only_quick_actions=True)
    if profiler is not None:
        _profile_add(profiler, "ai_evaluations", 1)
        _profile_add(profiler, "ai_evaluation_seconds",
                     time.perf_counter() - started)
    if decision is None:
        return False

    target_map = decision.get("target_map") or {}
    target_uid = decision.get("target_uid")
    if not target_map and target_uid is not None:
        target_map = {0: [int(target_uid)]}
    payload = {"ability_data": [{
        "target_map": {
            int(index): [int(uid) for uid in values]
            for index, values in target_map.items()
        },
        "x_cost": 0,
    }]}
    _transaction(
        handler, session,
        _card_play_bytes(chosen.card_uid, chosen.card_type),
        typed_payload=payload, profiler=profiler)
    location = _db.execute(
        "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
        (int(session.session_id), int(chosen.card_uid))).fetchone()
    played = bool(location and str(location[0]).lower() != "hand")
    if report is not None:
        key = f"{chosen.template_guid}:{chosen.name}"
        entry = report["card_decisions"].get(
            str(int(owner_id)), {}).get(key)
        if played and entry is not None:
            entry["played"] += 1
        elif not played:
            _record_failed_card_play(report, owner_id, chosen)
        player_name = (report.get("decks", {}).get(
            str(int(owner_id)), {}).get("name") or f"FRA-{owner_id}")
        report.setdefault("actions", []).append({
            "turn": int(state.get("turn_number", 0) or 0),
            "player_id": int(owner_id),
            "player": player_name,
            "card": chosen.name,
            "action": "combat_trick",
            "reason": reason,
            "target_uid": target_uid,
            "played": played,
        })
    return played


def _play_quick_response(handler, session, session_id, state,
                         owner_id, opponent_id, player_name, turn_number,
                         profiler=None, report=None):
    """Play one threat-removal quick action in the current response window."""
    quick, target_uid, x_cost, reason = _ai_quick_decision(
        handler, session, state, owner_id, opponent_id,
        profiler=profiler, report=report)
    if quick is None:
        return False
    payload = {"ability_data": [{
        "target_map": ({0: [int(target_uid)]}
                       if target_uid is not None else {}),
        "x_cost": int(x_cost or 0),
    }]}
    _transaction(
        handler, session,
        _card_play_bytes(quick.card_uid, quick.card_type),
        typed_payload=payload, profiler=profiler)
    location = _db.execute(
        "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(quick.card_uid))).fetchone()
    played = bool(location and location[0] != "hand")
    if report is not None:
        key = f"{quick.template_guid}:{quick.name}"
        entry = report["card_decisions"].get(
            str(int(owner_id)), {}).get(key)
        if played and entry is not None:
            entry["played"] += 1
        elif not played:
            _record_failed_card_play(report, owner_id, quick)
        report["actions"].append({
            "turn": int(turn_number),
            "player_id": int(owner_id),
            "player": player_name,
            "card": quick.name,
            "action": "quick_response",
            "reason": reason,
            "target_uid": target_uid,
            "played": played,
        })
    return played


def _play_one_game(seed, turns_cap=AUTOPLAY_TURN_CAP, deck_specs=None,
                   profiler=None, report=None, run_slot=None, outcome=None):
    rnd = random.Random(seed)
    session_id, pids = _autoplay_identity(seed, run_slot)
    _cleanup(session_id, pids)
    row_id_run_base = (int(run_slot or 0) * AUTOPLAY_ROW_ID_RUN_STRIDE)

    session = gs.GameSession(session_id, session_id * AUTOPLAY_SERVER_ID_MULTIPLIER,
                             f"tourney-{session_id}", pids[0])
    for pid in pids:
        session.add_player(encoder.make_uid(PLAYER_UID_TYPE, pid), 0)

    # Seed FRA encounter decks when requested; otherwise retain the Set 1
    # smoke fixture used by the original runner.
    shards = db_set_resource_guids(SET1)
    others = db_set_constructed_guids(SET1)
    for i, pid in enumerate(pids):
        if deck_specs:
            spec = deck_specs[i]
            deck = list(spec["cards"])
            champ_guid = spec["champion_guid"]
            rnd.shuffle(deck)
        else:
            rnd.shuffle(others)
            deck = ((shards * AUTOPLAY_DECK_RESOURCE_COUNT)
                    [:AUTOPLAY_DECK_RESOURCE_COUNT]
                    + others[:AUTOPLAY_DECK_SPELL_COUNT])
            champ_guid = "1ae73dcf-e96e-4536-aec3-f53efb5e1c96"
            rnd.shuffle(deck)
        row_id_base = row_id_run_base + i * AUTOPLAY_ROW_ID_SEAT_STRIDE
        _seed_deck(
            session_id, pid, deck,
            uid_offset=i * AUTOPLAY_PLAYER_UID_STRIDE,
            row_id_base=row_id_base)
        champ_uid = _seed_champion(
            session_id, pid, champ_guid,
            uid_offset=i * AUTOPLAY_PLAYER_UID_STRIDE +
            AUTOPLAY_CHAMPION_UID_OFFSET,
            row_id_base=row_id_base + AUTOPLAY_ROW_ID_CHAMPION_OFFSET)
        if report is not None:
            report["champion_uids"][str(pid)] = champ_uid
    _db.commit()

    h1 = _make_handler(
        pids[0], session,
        deck_personality=(deck_specs[0].get("ai_deck_personality")
                          if deck_specs else None))
    h2 = _make_handler(
        pids[1], session,
        deck_personality=(deck_specs[1].get("ai_deck_personality")
                          if deck_specs else None))
    hand_tracker = {"lock": threading.Lock(), "cards": {}}
    if profiler is not None:
        h1._autoplay_profiler = profiler
        h2._autoplay_profiler = profiler
    if report is not None:
        report_lock = (profiler.get("_lock") if profiler is not None
                       else threading.Lock())
        for handler in (h1, h2):
            handler._autoplay_report = report
            handler._autoplay_pids = tuple(pids)
            handler._autoplay_report_lock = report_lock
            handler._autoplay_hand_tracker = hand_tracker
    with player_handler_lock:
        player_handlers[pids[0]] = h1
        player_handlers[pids[1]] = h2
    if report is not None:
        report["decks"] = {
            str(pid): {
                "name": (deck_specs[index]["name"] if deck_specs
                         else "Set 1 smoke deck"),
                "deck_guid": (deck_specs[index]["deck_guid"]
                              if deck_specs else None),
                "champion_guid": (deck_specs[index]["champion_guid"]
                                  if deck_specs else None),
                "ai_deck_personality": (
                    deck_specs[index].get("ai_deck_personality") or "Default"
                    if deck_specs else None),
            }
            for index, pid in enumerate(pids)
        }

    # Ready setup (both players) — persists the coin flip.
    ready_state = {}
    handle_ready_for_game_setup(h1, session, ready_state, player_handlers)
    handle_ready_for_game_setup(h2, session, ready_state, player_handlers)
    state = pvp_load_state(session) or {}
    if not state:
        state = pvp_default_state(pids[0], pids[0])
        state["goes_first_pid"] = pids[0]
        pvp_save_state(session, state)
    events_ready = {}
    handle_ready_for_game_events(h1, session, events_ready, log_req)
    handle_ready_for_game_events(h2, session, events_ready, log_req)
    if report is not None:
        _autoplay_update_hand_tracker(
            hand_tracker, session_id, pids, pvp_load_state(session))

    # Play/Draw pick: winner chooses to play.
    _transaction(h1, session, b"ChoosePlayTransaction;")
    # The user's theory: the OTHER player must also send their pick for the
    # client to leave PickGoesFirst.  Test by having the loser send the
    # complementary pick (Draw) too.
    _transaction(h2, session, b"ChooseDrawTransaction;")

    # Sequential mulligan: ask 1 redraws (player A), ask 2 redraws (player B),
    # ask 3 keeps, ask 4 keeps — exercises the alternating redraw path the
    # real client uses.
    for round_i in range(AUTOPLAY_MULLIGAN_ROUNDS):
        cur = gs.find_session_by_player(
            game_engine.UID.make(PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
        st = pvp_load_state(cur)
        if not st or len(st.get("kept") or []) >= len(pids) \
                or st.get("phase") != game_engine.ETurnPhases.Mulligan:
            break
        ask = st.get("mulligan_pid")
        if ask in pids:
            if round_i in AUTOPLAY_REDRAW_ROUNDS:
                # Ask 1 (A) redraws, ask 2 (B) redraws — alternating redraw.
                _transaction(player_handlers[ask], session,
                             b"MulliganTransaction;")
            else:
                _transaction(player_handlers[ask], session,
                             b"AcceptStartingHand;")
        else:
            break

    state = pvp_load_state(session)
    if state is None:
        state = {}
    log_req(f"    PvP game {seed}: phase={state.get('phase')} "
            f"turn={state.get('turn_pid')}")
    if report is not None:
        report["starting_health"] = _autoplay_health_snapshot(state, pids)

    turns = 0
    observed_turn_number = int(state.get("turn_number", 1) or 1)
    guard = 0
    damage_order_phase_key = None
    try:
        while turns < turns_cap and guard < AUTOPLAY_GUARD_CAP:
            guard += 1
            cur_session = gs.find_session_by_player(
                game_engine.UID.make(PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
            state = pvp_load_state(cur_session)
            if state is None:
                log_req(f"    [pvp-autoplay] STATE LOST at guard {guard} — "
                        f"turn_order keys="
                        f"{list(cur_session.turn_order.keys()) if isinstance(cur_session.turn_order, dict) else type(cur_session.turn_order)}")
                break
            terminal_result = _autoplay_terminal_result(
                cur_session, state, pids)
            if terminal_result is not None:
                _autoplay_store_game_end(report, outcome, terminal_result)
                log_req(f"    [pvp-autoplay] game ended: {terminal_result}")
                break
            # The native scheduler can enter EndTurn and the next StartTurn
            # inside one priority pass, so sampling phase 22 misses completed
            # turns. The persisted turn number advances exactly once at that
            # boundary and remains observable after the scheduler settles.
            current_turn_number = int(
                state.get("turn_number", observed_turn_number) or
                observed_turn_number)
            if current_turn_number > observed_turn_number:
                turns += current_turn_number - observed_turn_number
                observed_turn_number = current_turn_number
            if turns >= turns_cap:
                break
            phase = state.get("phase")
            turn_pid = state.get("turn_pid")
            opp_pid = pids[1] if turn_pid == pids[0] else pids[0]
            damage_phases = (
                game_engine.ETurnPhases.AssignFirstStrikeDamage,
                game_engine.ETurnPhases.AssignDamage)
            if phase not in damage_phases:
                damage_order_phase_key = None
            # Both seats are AIs in this harness, so resolve every persisted
            # UI choice before asking the card policy for another action. A
            # pending prompt clears the native priority owner; continuing into
            # card selection would count repeated rejected transactions as
            # valid AI choices and strand the simulation in the same phase.
            pend_key = next((key for key in (
                "pending_trigger", "pending_choice", "pending_deck_search",
                "pending_discard_ability", "pending_conversation")
                if state.get(key)), None)
            if pend_key is not None:
                pend = state[pend_key]
                if pend_key in ("pending_discard_ability",
                                "pending_conversation"):
                    raise RuntimeError(
                        f"autoplay cannot resolve {pend_key} yet")
                chooser_pid = int(pend.get("owner_id", turn_pid) or turn_pid)
                if pend_key == "pending_trigger":
                    candidates = []
                    if pend.get("source_uid"):
                        src = int(pend["source_uid"])
                        candidates = [r[0] for r in db_static_card_rows(
                            session_id, ("warzone", "CastSpells"))
                                      if r[0] != src]
                    if not candidates:
                        candidates = [r[0] for r in db_static_card_rows(
                            session_id, ("warzone",))[:1]]
                    target_index = int(pend.get("target_index", 0) or 0)
                elif pend_key == "pending_choice":
                    candidates = list(pend.get("choice_uids") or ())
                    continuation = pend.get("continuation") or {}
                    target_index = int(
                        continuation.get("target_index", 0) or 0)
                else:
                    candidates = list(pend.get("candidates") or ())
                    continuation = pend.get("continuation") or {}
                    target_index = int(continuation.get(
                        "target_index", pend.get("target_index", 0)) or 0)

                chooser = player_handlers.get(chooser_pid)
                if chooser is None or not candidates:
                    raise RuntimeError(
                        f"unhandled {pend_key} for pid {chooser_pid}: "
                        f"no handler or legal candidates")
                if (pend_key == "pending_deck_search" and
                        str(pend.get("kind") or "") in
                        ("revealed_troop", "revealed_card")):
                    # The FRA harness owns both seats. Model this UI choice as
                    # a random legal pick, using the run-seeded RNG so the
                    # simulation remains reproducible.
                    chosen_uid = int(rnd.choice(candidates))
                else:
                    chosen_uid = int(candidates[0])
                typed_payload = {
                    "AbilityInstanceId": int(
                        pend.get("instance_id", 1) or 1),
                    "AbilityActivationData": {
                        "TargetMap": {str(target_index): [chosen_uid]}
                    },
                }
                _transaction(
                    chooser, session,
                    b"SetAbilityActivationDataTransaction;" +
                    _mk_uid_bytes(chosen_uid),
                    typed_payload=typed_payload, profiler=profiler)
                latest = gs.find_session_by_player(
                    game_engine.UID.make(
                        PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                after_state = pvp_load_state(latest) or {}
                if after_state.get(pend_key) == pend:
                    raise RuntimeError(
                        f"autoplay could not resolve {pend_key} for pid "
                        f"{chooser_pid} (choice {hex(chosen_uid)})")
                continue
            if state.get("resolution_paused"):
                raise RuntimeError(
                    "autoplay found a paused resolution without a supported "
                    "pending-input marker")
            # The client automatically sends this transaction on entry to
            # AssignFirstStrikeDamage / AssignDamage. Submit it once for each
            # damage-step entry. If an effect leaves priority open in the same
            # phase, pass that live window instead of repeatedly resolving the
            # automatic damage order.
            if phase in damage_phases:
                damage_phase_key = (current_turn_number, int(phase))
                if damage_order_phase_key != damage_phase_key:
                    before_priority = state.get("priority_pid")
                    _transaction(
                        player_handlers[turn_pid], session,
                        b"AssignDamageOrderTransaction;",
                        typed_payload={"assignments": []}, profiler=profiler)
                    damage_order_phase_key = damage_phase_key
                    latest = gs.find_session_by_player(
                        game_engine.UID.make(
                            PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                    after_state = pvp_load_state(latest) or {}
                    terminal_result = _autoplay_terminal_result(
                        latest, after_state, pids)
                    if terminal_result is not None:
                        _autoplay_store_game_end(
                            report, outcome, terminal_result)
                        log_req(
                            "    [pvp-autoplay] game ended during damage "
                            f"resolution: {terminal_result}")
                        break
                    after_turn = _autoplay_report_int(after_state.get(
                        "turn_number", current_turn_number))
                    pending_input = any(after_state.get(key) for key in (
                        "pending_trigger", "pending_choice",
                        "pending_deck_search", "pending_discard_ability"))
                    if (after_state.get("phase") == phase and
                            after_turn == current_turn_number and
                            after_state.get("priority_pid") == before_priority and
                            state.get("stack") == after_state.get("stack") and
                            state.get("passes") == after_state.get("passes") and
                            state.get("stack_passed") ==
                            after_state.get("stack_passed") and
                            not pending_input):
                        raise RuntimeError(
                            "AssignDamageOrder did not advance the native "
                            f"phase or priority (phase={phase}, "
                            f"turn={current_turn_number}, "
                            f"priority={before_priority})")
                    continue

                before_priority = state.get("priority_pid")
                _pass_native_priority_window(
                    session, pids, player_handlers, phase=phase,
                    profiler=profiler)
                latest = gs.find_session_by_player(
                    game_engine.UID.make(
                        PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                after_state = pvp_load_state(latest) or {}
                terminal_result = _autoplay_terminal_result(
                    latest, after_state, pids)
                if terminal_result is not None:
                    _autoplay_store_game_end(report, outcome, terminal_result)
                    log_req(
                        "    [pvp-autoplay] game ended during damage "
                        f"priority: {terminal_result}")
                    break
                after_turn = _autoplay_report_int(after_state.get(
                    "turn_number", current_turn_number))
                pending_input = any(after_state.get(key) for key in (
                    "pending_trigger", "pending_choice",
                    "pending_deck_search", "pending_discard_ability"))
                if (after_state.get("phase") == phase and
                        after_turn == current_turn_number and
                        after_state.get("priority_pid") == before_priority and
                        state.get("stack") == after_state.get("stack") and
                        state.get("passes") == after_state.get("passes") and
                        state.get("stack_passed") ==
                        after_state.get("stack_passed") and
                        not pending_input):
                    raise RuntimeError(
                        "native damage priority window made no progress "
                        f"(phase={phase}, turn={current_turn_number}, "
                        f"priority={before_priority})")
                continue
            if phase in OPENING_SETUP_PHASES:
                # Setup phases are driven above; a stray pass should not loop.
                break
            # Main phases: the active player uses the shared FRA decision
            # path, then the native priority owner(s) pass the live window.
            if phase in (game_engine.ETurnPhases.FirstMainPhase,
                         game_engine.ETurnPhases.SecondMainPhase):
                if deck_specs:
                    h = player_handlers[turn_pid]
                    res_rows = db_hand_resources_with_template(
                        session_id, turn_pid)
                    if res_rows and not state.get(
                            f"res_played_{turn_pid}", 0):
                        resource_row = res_rows[0]
                        _transaction(h, session,
                                     _card_play_bytes(
                                         resource_row[1], "Resource"),
                                     profiler=profiler)
                        resource_location = _db.execute(
                            "SELECT location FROM game_cards WHERE session_id=? "
                            "AND card_uid=?", (session_id,
                                                 int(resource_row[1]))).fetchone()
                        resource_played = bool(
                            resource_location
                            and resource_location[0] != "hand")
                        _record_resource_decision(
                            report, turn_pid, resource_row,
                            resource_played)
                        if report is not None:
                            report["actions"].append({
                                "turn": turns + 1,
                                "player_id": int(turn_pid),
                                "player": deck_specs[pids.index(turn_pid)]["name"],
                                "card": _db.execute(
                                    "SELECT name FROM card_templates WHERE guid=?",
                                    (str(resource_row[2]),)).fetchone()[0],
                                "reason": "resource_play",
                                "played": resource_played,
                            })

                    latest = gs.find_session_by_player(
                        game_engine.UID.make(
                            PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                    state = pvp_load_state(latest) or state

                    decision = _ai_main_decision(
                        h, session, state, turn_pid, opp_pid,
                        profiler=profiler, report=report,
                        pre_combat=(phase ==
                                    game_engine.ETurnPhases.FirstMainPhase))
                    action_selected = decision["kind"] != "pass"
                    if decision["kind"] == "card":
                        chosen = decision["card"]
                        target_uid = decision.get("target_uid")
                        target_uids = [int(uid) for uid in
                                       decision.get("target_uids") or (
                                           [target_uid] if target_uid is not None
                                           else [])]
                        target_map = decision.get("target_map") or {}
                        if not target_map and target_uids:
                            target_map = {0: target_uids}
                        typed_payload = {"ability_data": [{
                            "target_map": {
                                int(index): [int(uid) for uid in values]
                                for index, values in target_map.items()
                            },
                            "x_cost": int(decision.get("x_cost", 0) or 0),
                        }]}
                        _transaction(
                            h, session,
                            _card_play_bytes(chosen.card_uid, chosen.card_type),
                            typed_payload=typed_payload, profiler=profiler)
                        location = _db.execute(
                            "SELECT location FROM game_cards WHERE session_id=? "
                            "AND card_uid=?", (session_id,
                                                 int(chosen.card_uid))).fetchone()
                        played = bool(location and location[0] != "hand")
                        if report is not None:
                            key = f"{chosen.template_guid}:{chosen.name}"
                            entry = report["card_decisions"].get(
                                str(int(turn_pid)), {}).get(key)
                            if entry is not None and played:
                                entry["played"] += 1
                            elif not played:
                                _record_failed_card_play(
                                    report, turn_pid, chosen)
                            report["actions"].append({
                                "turn": turns + 1,
                                "player_id": int(turn_pid),
                                "player": deck_specs[pids.index(turn_pid)]["name"],
                                "card": chosen.name,
                                "reason": decision["reason"],
                                "target_uid": target_uid,
                                "target_uids": target_uids,
                                "played": played,
                            })
                    elif decision["kind"] == "champion_power":
                        power = decision["power"]
                        # The selector resolves this from the champion record;
                        # use the same authoritative row for the transaction
                        # instead of a possibly stale/missing state projection.
                        champion_row = db_game_champion(
                            session_id, turn_pid, conn=_db)
                        source_uid = int(champion_row[0]) if champion_row else 0
                        activation_data = {
                            "target_map": ({
                                0: [int(power["target_uid"])]
                            } if power.get("target_uid") is not None else {}),
                            "cost_target_map": {
                                index: [int(uid)]
                                for index, uid in enumerate(
                                    power.get("sacrifice_uids", ()))
                            },
                        }
                        typed_payload = {
                            "source_card_id": source_uid,
                            "ability_template_id": power["ability_guid"],
                            "activation_data": activation_data,
                            "ability_instance_id": 0,
                        }
                        charge_before = int(
                            state.get(f"chg_{turn_pid}", 0) or 0)
                        spell_before = int(
                            state.get(f"sp_{turn_pid}", 0) or 0)
                        port = getattr(session, "_rules_port_session", None)
                        history_before = (len(port.transaction_history)
                                          if port is not None else 0)
                        _transaction(
                            h, session,
                            _champion_power_bytes(source_uid, power),
                            typed_payload=typed_payload, profiler=profiler)
                        latest = gs.find_session_by_player(
                            game_engine.UID.make(
                                PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                        history_after = getattr(
                            getattr(latest, "_rules_port_session", None),
                            "transaction_history", ())
                        latest_state = pvp_load_state(latest) or state
                        accepted = _accepted_ability_activation(
                            history_after, history_before, source_uid,
                            power["ability_guid"])
                        charge_spent = (charge_before - int(
                            latest_state.get(f"chg_{turn_pid}", 0) or 0))
                        spell_spent = (spell_before - int(
                            latest_state.get(f"sp_{turn_pid}", 0) or 0))
                        activated = bool(
                            accepted or
                            (int(power.get("charge_cost", 0) or 0) > 0 and
                             charge_spent >= int(
                                 power.get("charge_cost", 0) or 0)) or
                            (int(power.get("spell_cost", 0) or 0) > 0 and
                             spell_spent >= int(
                                 power.get("spell_cost", 0) or 0)))
                        if report is not None:
                            power_entry = report.setdefault(
                                "ability_decisions", [])[-1]
                            power_entry["submitted"] = True
                            power_entry["activated"] = bool(activated)
                            report["actions"].append({
                                "turn": turns + 1,
                                "player_id": int(turn_pid),
                                "player": deck_specs[pids.index(turn_pid)]["name"],
                                "action": "champion_power",
                                "ability_guid": power["ability_guid"],
                                "target_uid": power.get("target_uid"),
                                "sacrifice_uids": list(
                                    power.get("sacrifice_uids") or []),
                                "charge_cost": int(
                                    power.get("charge_cost", 0) or 0),
                                "spell_cost": int(
                                    power.get("spell_cost", 0) or 0),
                                "activated": bool(activated),
                            })
                    elif decision["kind"] in (
                            "warzone_ability", "resource_sink"):
                        ability = decision["ability"]
                        source_uid = int(ability["source_uid"])
                        target_uid = ability.get("target_uid")
                        activation_data = {
                            "target_map": ({
                                0: [int(target_uid)]
                            } if target_uid is not None else {}),
                            "cost_target_map": {
                                int(index): [int(uid) for uid in values]
                                for index, values in (
                                    ability.get("cost_target_map") or {}).items()
                            },
                            "x_cost": int(ability.get("x_cost", 0) or 0),
                        }
                        typed_payload = {
                            "source_card_id": source_uid,
                            "ability_template_id": ability["ability_guid"],
                            "activation_data": activation_data,
                            "ability_instance_id": 0,
                        }
                        port = getattr(session, "_rules_port_session", None)
                        history_before = (len(port.transaction_history)
                                          if port is not None else 0)
                        _transaction(
                            h, session,
                            _manual_ability_bytes(
                                source_uid, ability["ability_guid"],
                                target_uid),
                            typed_payload=typed_payload, profiler=profiler)
                        latest = gs.find_session_by_player(
                            game_engine.UID.make(
                                PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                        history_after = getattr(
                            getattr(latest, "_rules_port_session", None),
                            "transaction_history", ())
                        activated = _accepted_ability_activation(
                            history_after, history_before, source_uid,
                            ability["ability_guid"])
                        if report is not None:
                            ability_entry = report.setdefault(
                                "ability_decisions", [])[-1]
                            ability_entry["submitted"] = True
                            ability_entry["activated"] = bool(activated)
                            report["actions"].append({
                                "turn": turns + 1,
                                "player_id": int(turn_pid),
                                "player": deck_specs[pids.index(turn_pid)]["name"],
                                "action": decision["kind"],
                                "ability_guid": ability["ability_guid"],
                                "source_uid": source_uid,
                                "target_uid": target_uid,
                                "x_cost": int(ability.get("x_cost", 0) or 0),
                                "activated": bool(activated),
                            })
                    elif decision["kind"] == "tunnel":
                        ability = decision["ability"]
                        source_uid = int(ability["source_uid"])
                        typed_payload = {
                            "source_card_id": source_uid,
                            "ability_template_id": ability["ability_guid"],
                            "activation_data": {},
                            "ability_instance_id": 0,
                        }
                        port = getattr(session, "_rules_port_session", None)
                        history_before = (len(port.transaction_history)
                                          if port is not None else 0)
                        _transaction(
                            h, session,
                            _manual_ability_bytes(
                                source_uid, ability["ability_guid"]),
                            typed_payload=typed_payload, profiler=profiler)
                        latest = gs.find_session_by_player(
                            game_engine.UID.make(
                                PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                        history_after = getattr(
                            getattr(latest, "_rules_port_session", None),
                            "transaction_history", ())
                        activated = _accepted_ability_activation(
                            history_after, history_before, source_uid,
                            ability["ability_guid"])
                        if report is not None:
                            ability_entry = report.setdefault(
                                "ability_decisions", [])[-1]
                            ability_entry["submitted"] = True
                            ability_entry["activated"] = bool(activated)
                            report["actions"].append({
                                "turn": turns + 1,
                                "player_id": int(turn_pid),
                                "player": deck_specs[pids.index(turn_pid)]["name"],
                                "action": "tunnel",
                                "ability_guid": ability["ability_guid"],
                                "source_uid": source_uid,
                                "resource_cost": int(
                                    ability.get("resource_cost", 0) or 0),
                                "activated": bool(activated),
                            })
                    if action_selected:
                        latest = gs.find_session_by_player(
                            game_engine.UID.make(
                                PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                        response_state = pvp_load_state(latest) or state
                        deck_name = deck_specs[pids.index(opp_pid)]["name"]
                        _play_quick_response(
                            player_handlers[opp_pid], session, session_id,
                            response_state, opp_pid, turn_pid, deck_name,
                            turns + 1, profiler=profiler, report=report)
                    _pass_native_priority_window(
                        session, pids, player_handlers, phase=phase,
                        profiler=profiler)
                    continue

                h = player_handlers[turn_pid]
                res_rows = db_hand_resources_with_template(session_id, turn_pid)
                if res_rows and not db_zone_card_count(
                        session_id, turn_pid, "PlayedResources"):
                    _transaction(h, session,
                                 _card_play_bytes(res_rows[0][1], "Resource"))
                troop = next((row for row in db_ai_hand_playables(
                    session_id, turn_pid, "permanent")
                    if "Troop" in str(row[4])), None)
                if troop:
                    _transaction(h, session,
                                 _card_play_bytes(troop[1], troop[4]))
                _pass_native_priority_window(
                    session, pids, player_handlers, phase=phase,
                    profiler=profiler)
                continue
            # DeclareAttack: the turn player swings with all eligible troops.
            if phase == game_engine.ETurnPhases.DeclareAttack:
                attackers, attack_reason = _ai_attack_decision(
                    player_handlers[turn_pid], session, state, turn_pid,
                    opp_pid, profiler=profiler, report=report)
                champ_map = state.get("champ_map") or {}
                opp_champ = int(champ_map.get(str(opp_pid), 0))
                # DeclareAttack has no pass-priority action. The client ends
                # this phase by committing its attack declaration, including
                # an empty list when it has no legal attackers. Sending passes
                # here is rejected by RulesPort and used to make the harness
                # repeat the same phase until its guard expired.
                attack_session = _transaction(
                    player_handlers[turn_pid], session,
                    _attack_bytes(attackers, opp_champ),
                    typed_payload={"m_Attacks": [{
                        "DefendingCardId": opp_champ,
                        "AttackingCardIds": attackers,
                    }]},
                    profiler=profiler)
                if report is not None:
                    report["actions"].append({
                        "turn": turns + 1,
                        "player_id": int(turn_pid),
                        "player": deck_specs[pids.index(turn_pid)]["name"],
                        "action": "attack",
                        "reason": attack_reason,
                        "attacker_uids": attackers,
                    })
                if attackers:
                    latest = gs.find_session_by_player(
                        game_engine.UID.make(
                            PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                    response_state = pvp_load_state(latest) or state
                    deck_name = deck_specs[pids.index(opp_pid)]["name"]
                    _play_quick_response(
                        player_handlers[opp_pid], session, session_id,
                        response_state, opp_pid, turn_pid, deck_name,
                        turns + 1, profiler=profiler, report=report)
                else:
                    # The empty declaration advances directly through the
                    # native no-combat path. Do not send DeclareAttack passes.
                    post_attack_state = pvp_load_state(attack_session)
                    if (post_attack_state is None or
                            post_attack_state.get("phase") ==
                            game_engine.ETurnPhases.DeclareAttack):
                        raise RuntimeError(
                            "empty attack declaration did not advance out of "
                            "DeclareAttack")
                    continue
                _pass_native_priority_window(
                    session, pids, player_handlers,
                    profiler=profiler)
                continue
            # DeclareDefense: the defender blocks with every eligible troop.
            if phase == game_engine.ETurnPhases.DeclareDefense:
                att_state = state
                attacker_uids = [int(k) for k in
                                 (att_state.get("attackers") or {})]
                blockers = _ai_block_decision(
                    player_handlers[opp_pid], session, state, opp_pid,
                    turn_pid, attacker_uids, profiler=profiler,
                    report=report)
                champ_map = state.get("champ_map") or {}
                opp_champ = int(champ_map.get(str(opp_pid), 0))
                if attacker_uids:
                    _transaction(player_handlers[opp_pid], session,
                                 _defense_bytes(attacker_uids, blockers,
                                                opp_champ),
                                 typed_payload={"m_DefenseDeclarations": [
                                     {"AttackerId": int(attacker),
                                      "DefendingCardIds": [
                                          int(uid) for uid in blocker_list]}
                                     for attacker, blocker_list in
                                     blockers.items()
                                 ]},
                                 profiler=profiler)
                _pass_native_priority_window(
                    session, pids, player_handlers,
                    profiler=profiler)
                continue
            _pass_native_priority_window(
                session, pids, player_handlers, phase=phase,
                profiler=profiler)
    except Exception:
        return turns, traceback.format_exc()
    finally:
        if report is not None or outcome is not None:
            last_session = gs.find_session_by_player(
                game_engine.UID.make(PLAYER_UID_TYPE, pids[0]).to_uint64())
            last_session = last_session or session
            last_state = pvp_load_state(last_session) or {}
            terminal_result = _autoplay_terminal_result(
                last_session, last_state, pids)
            _autoplay_store_game_end(report, outcome, terminal_result)
            if report is not None:
                report["hand_at_end"] = _autoplay_finalize_hand(
                    hand_tracker, session_id, pids, last_state)
                report["turns"] = turns
                report["last_phase"] = str(last_state.get("phase"))
                report["health"] = {
                    str(pid): int(last_state.get(f"hp_{pid}", 20) or 0)
                    for pid in pids
                }
        _cleanup(session_id, pids)
    return turns, None


def _simulation_worker_count(job_count, workers=None):
    """Return a bounded worker count for the requested simulation jobs."""
    if job_count <= 0:
        return 0
    if workers is None:
        return min(job_count, max(1, os.cpu_count() or 1))
    try:
        count = int(workers)
    except (TypeError, ValueError) as exc:
        raise ValueError("workers must be a positive integer") from exc
    if count < 1:
        raise ValueError("workers must be a positive integer")
    return min(job_count, count)


def _run_simulation_job(job):
    """Run one isolated game and turn setup failures into a normal result."""
    seed, kwargs = job
    try:
        return _play_one_game(seed, **kwargs)
    except Exception:
        # _play_one_game owns cleanup after setup succeeds. If seeding or
        # handler construction fails before its main try/finally, clean the
        # same disposable identity here so a failed worker cannot poison the
        # next run.
        try:
            session_id, pids = _autoplay_identity(
                seed, kwargs.get("run_slot"))
            _cleanup(session_id, pids)
        except Exception:
            pass
        return 0, traceback.format_exc()


def _run_simulation_jobs(jobs, workers=None, progress=None):
    """Run complete games concurrently while preserving result order.

    ``progress`` is called from the coordinator thread as each job finishes;
    it receives ``(completed, total, job_index, result)``.  Keeping callbacks
    out of the worker threads makes progress reporting safe for callers that
    also update shared standings or write to a single output stream.
    """
    jobs = list(jobs)
    worker_count = _simulation_worker_count(len(jobs), workers)
    if worker_count <= 1:
        results = []
        for job_index, job in enumerate(jobs):
            result = _run_simulation_job(job)
            results.append(result)
            if progress is not None:
                progress(job_index + 1, len(jobs), job_index, result)
        return results
    with ThreadPoolExecutor(max_workers=worker_count,
                            thread_name_prefix="hex-sim") as executor:
        futures = {
            executor.submit(_run_simulation_job, job): job_index
            for job_index, job in enumerate(jobs)
        }
        if progress is None:
            # Reading in submission order keeps seeded reports and console
            # output stable even though the games themselves run at the same
            # time.
            return [future.result() for future in futures]

        # Progress must be reported in completion order so the count reflects
        # finished matches rather than the first slow job in the queue.
        results = [None] * len(jobs)
        for completed, future in enumerate(as_completed(futures), 1):
            job_index = futures[future]
            result = future.result()
            results[job_index] = result
            progress(completed, len(jobs), job_index, result)
        return results


def main(games=DEFAULT_AUTOPLAY_GAMES, workers=None):
    seeds = list(range(FIRST_AUTOPLAY_SEED,
                       games + FIRST_AUTOPLAY_SEED))
    results = _run_simulation_jobs(
        [(seed, {"run_slot": run_index})
         for run_index, seed in enumerate(seeds)],
        workers=workers)
    ok = 0
    for seed, (turns, err) in zip(seeds, results):
        if err:
            print(f"game {seed}: CRASH after {turns} turns")
            print(err[:3000])
        else:
            ok += 1
            print(f"game {seed}: {turns} turns, no crash")
    print(f"PvP autoplay: {ok}/{games} games completed")


def _json_report(report):
    copied = dict(report)
    copied["card_decisions"] = {
        owner: {
            key: {**entry, "reasons": dict(entry["reasons"])}
            for key, entry in decisions.items()
        }
        for owner, decisions in report.get("card_decisions", {}).items()
    }
    copied["profile"] = {
        key: value for key, value in report.get("profile", {}).items()
        if not key.startswith("_")
    }
    return copied


def _new_simulation_profiler():
    """Create counters shared by the games in one simulation batch."""
    return {
        "_lock": threading.Lock(),
        "rules_transactions": 0,
        "rules_transaction_seconds": 0.0,
        "rules_transaction_by_kind": {},
        "rules_port_dispatches": 0,
        "rules_port_dispatch_seconds": 0.0,
        "rules_port_by_kind": {},
        "ai_evaluations": 0,
        "ai_evaluation_seconds": 0.0,
        "ai_power_evaluations": 0,
        "ai_power_evaluation_seconds": 0.0,
        "ai_manual_ability_evaluations": 0,
        "ai_manual_ability_evaluation_seconds": 0.0,
        "ai_tunnel_evaluations": 0,
        "ai_tunnel_evaluation_seconds": 0.0,
        "combat_evaluations": 0,
        "combat_evaluation_seconds": 0.0,
    }


def main_fra(deck_guids=None, turns_cap=30, show_profile=False,
             report_path=None, runs=1, seed=900, workers=None):
    specs = _fra_encounter_specs(deck_guids)
    profiler = _new_simulation_profiler()
    turn_limit = max(0, int(turns_cap))
    run_count = max(1, int(runs))
    first_seed = int(seed)
    report = {"decks": {}, "runs": [], "actions": [],
              "combat_decisions": [], "health_changes": [],
              "combat_steps": [], "card_decisions": {},
              "ability_decisions": []}
    any_error = False
    run_contexts = []
    run_jobs = []
    for run_index in range(run_count):
        run_seed = first_seed + run_index
        run_report = {"champion_uids": {}, "actions": [],
                      "combat_decisions": [], "health_changes": [],
                      "combat_steps": [], "card_decisions": {},
                      "ability_decisions": []}
        run_contexts.append((run_index, run_seed, run_report))
        run_jobs.append((run_seed, {
            "turns_cap": turn_limit,
            "deck_specs": specs,
            "profiler": profiler,
            "report": run_report,
            "run_slot": run_index,
        }))
    results = _run_simulation_jobs(run_jobs, workers=workers)
    for (run_index, run_seed, run_report), (turns, err) in zip(
            run_contexts, results):
        if not report["decks"]:
            report["decks"] = dict(run_report.get("decks") or {})
        run_summary = {
            "seed": run_seed,
            "turns": turns,
            "health": dict(run_report.get("health") or {}),
            "starting_health": dict(run_report.get("starting_health") or {}),
            "last_phase": run_report.get("last_phase"),
            "game_end": run_report.get("game_end"),
            "hand_at_end": run_report.get("hand_at_end", {}),
        }
        run_summary["life_delta"] = {
            str(pid): int(run_summary["health"].get(str(pid), 0)) -
            int(run_summary["starting_health"].get(str(pid), 0))
            for pid in sorted(run_summary["health"], key=int)
        }
        combat_health_delta = Counter()
        combat_steps = run_report.get("combat_steps", ())
        for step in combat_steps:
            combat_health_delta.update(step.get("health_delta") or {})
        run_summary["combat_phase_transactions"] = len(combat_steps)
        run_summary["combat_phase_advances"] = sum(
            bool(step.get("phase_advanced")) for step in combat_steps)
        run_summary["combat_health_delta"] = {
            str(pid): int(combat_health_delta.get(str(pid), 0))
            for pid in sorted(run_report.get("health", {}), key=int)
        }
        if err:
            any_error = True
            run_summary["error"] = err
            print(f"FRA run seed {run_seed}: stopped after {turns} turns")
            print(err)
        else:
            print(f"FRA run seed {run_seed}: {turns}/{turn_limit} turns; "
                  f"health={run_summary['health']}")
        print(f"  Life delta since game start: {run_summary['life_delta']}")
        if run_summary["game_end"] is not None:
            print(f"  Game ended: {run_summary['game_end']}")
        print(f"  Combat phases: "
              f"{run_summary['combat_phase_advances']}/"
              f"{run_summary['combat_phase_transactions']} advanced; "
              f"net health delta={run_summary['combat_health_delta']}")
        print("  Cards in hand at end:")
        for owner_id, cards in run_summary["hand_at_end"].items():
            deck = run_report.get("decks", {}).get(owner_id, {})
            owner_name = deck.get("name", f"Player {owner_id}")
            print(f"    {owner_name} (seat {owner_id}):")
            if not cards:
                print("      (none)")
            for card in cards:
                print(f"      {card['name']} — "
                      f"{card['turns_in_hand']} turn(s) in hand")
        report["runs"].append(run_summary)
        for action in run_report.get("actions", ()):
            report["actions"].append({
                "run": run_index + 1, "seed": run_seed, **action})
        for decision in run_report.get("combat_decisions", ()):
            report["combat_decisions"].append({
                "run": run_index + 1, "seed": run_seed, **decision})
        for change in run_report.get("health_changes", ()):
            report["health_changes"].append({
                "run": run_index + 1, "seed": run_seed, **change})
        for decision in run_report.get("ability_decisions", ()):
            report["ability_decisions"].append({
                "run": run_index + 1, "seed": run_seed, **decision})
        for step in combat_steps:
            report["combat_steps"].append({
                "run": run_index + 1, "seed": run_seed, **step})
        for owner_id, decisions in run_report.get(
                "card_decisions", {}).items():
            destination = report["card_decisions"].setdefault(owner_id, {})
            for key, entry in decisions.items():
                total = destination.setdefault(key, {
                    "template_guid": entry["template_guid"],
                    "name": entry["name"],
                    "seen": 0,
                    "selected": 0,
                    "played": 0,
                    "reasons": Counter(),
                })
                total["seen"] += entry["seen"]
                total["selected"] += entry["selected"]
                total["played"] += entry["played"]
                total["reasons"].update(entry["reasons"])

    strategy_label = lambda spec: (
        spec.get("ai_deck_personality") or "Default")
    print(f"FRA duel: {specs[0]['name']} ({strategy_label(specs[0])}) vs "
          f"{specs[1]['name']} ({strategy_label(specs[1])}) — "
          f"{run_count} run(s), max {turn_limit} turns each")

    print("AI card decisions by seat (played / selected / seen):")
    for owner_id, deck in report["decks"].items():
        print(f"  {deck['name']} (seat {owner_id})")
        entries = sorted(report["card_decisions"].get(owner_id, {}).values(),
                         key=lambda item: (item["played"], -item["seen"],
                                           item["name"].lower()))
        for entry in entries:
            if entry["played"] >= entry["seen"]:
                continue
            reasons = ", ".join(
                f"{reason}={count}" for reason, count in
                sorted(entry["reasons"].items()))
            print(f"    {entry['name']}: {entry['played']}/"
                  f"{entry['selected']} / {entry['seen']} — {reasons}")

    print("Champion power decisions:")
    if not report["ability_decisions"]:
        print("  (none evaluated)")
    else:
        for item in report["ability_decisions"]:
            ability = item.get("ability_guid")
            if ability:
                label = str(item.get("ability_name") or ability)
            else:
                label = "no power selected"
            outcome = ("activated" if item.get("activated") else
                       "selected" if item.get("decision") == "selected" else
                       "declined")
            target = item.get("target_uid")
            target_text = f" -> {hex(int(target))}" if target else ""
            print(f"  turn {item.get('turn')} seat {item.get('player_id')}: "
                  f"{label} ({outcome}){target_text}")

    if show_profile:
        evaluations = int(profiler["ai_evaluations"])
        power_evaluations = int(profiler["ai_power_evaluations"])
        combat_evaluations = int(profiler["combat_evaluations"])
        port_dispatches = int(profiler["rules_port_dispatches"])
        print("Profile:")
        if evaluations:
            average = profiler["ai_evaluation_seconds"] * 1000 / evaluations
            print(f"  AI evaluator: "
                  f"{profiler['ai_evaluation_seconds'] * 1000:.2f} ms "
                  f"across {evaluations} decisions ({average:.2f} ms/decision)")
        else:
            print("  AI evaluator: 0 decisions")
        if power_evaluations:
            average = profiler["ai_power_evaluation_seconds"] * 1000 / \
                power_evaluations
            print(f"  AI champion-power evaluator: "
                  f"{profiler['ai_power_evaluation_seconds'] * 1000:.2f} ms "
                  f"across {power_evaluations} decisions "
                  f"({average:.2f} ms/decision)")
        else:
            print("  AI champion-power evaluator: 0 decisions")
        manual_ability_evaluations = int(
            profiler["ai_manual_ability_evaluations"])
        if manual_ability_evaluations:
            average = profiler["ai_manual_ability_evaluation_seconds"] * \
                1000 / manual_ability_evaluations
            print(f"  AI warzone-ability evaluator: "
                  f"{profiler['ai_manual_ability_evaluation_seconds'] * 1000:.2f} ms "
                  f"across {manual_ability_evaluations} decisions "
                  f"({average:.2f} ms/decision)")
        else:
            print("  AI warzone-ability evaluator: 0 decisions")
        tunnel_evaluations = int(profiler["ai_tunnel_evaluations"])
        if tunnel_evaluations:
            average = profiler["ai_tunnel_evaluation_seconds"] * 1000 / \
                tunnel_evaluations
            print(f"  AI tunneling evaluator: "
                  f"{profiler['ai_tunnel_evaluation_seconds'] * 1000:.2f} ms "
                  f"across {tunnel_evaluations} decisions "
                  f"({average:.2f} ms/decision)")
        else:
            print("  AI tunneling evaluator: 0 decisions")
        if combat_evaluations:
            average = profiler["combat_evaluation_seconds"] * 1000 / \
                combat_evaluations
            print(f"  AI combat evaluator: "
                  f"{profiler['combat_evaluation_seconds'] * 1000:.2f} ms "
                  f"across {combat_evaluations} decisions "
                  f"({average:.2f} ms/decision)")
        else:
            print("  AI combat evaluator: 0 decisions")
        if port_dispatches:
            print(f"  RulesPort dispatch: "
                  f"{profiler['rules_port_dispatch_seconds'] * 1000:.2f} ms "
                  f"across {port_dispatches} queued transaction dispatches "
                  f"(includes PvP projection callbacks)")
        else:
            print("  RulesPort dispatch: no queued transactions")
        for kind, item in sorted(profiler["rules_port_by_kind"].items()):
            print(f"    {kind}: {item['seconds'] * 1000:.2f} ms "
                  f"across {item['count']}")
        print(f"  End-to-end transaction path: "
              f"{profiler['rules_transaction_seconds'] * 1000:.2f} ms "
              f"across {profiler['rules_transactions']} transactions")
    report["profile"] = {
        key: value for key, value in profiler.items()
        if not key.startswith("_")
    }
    report["max_turns"] = turn_limit
    report["seed"] = first_seed
    if report_path:
        with open(report_path, "w", encoding="utf-8") as output:
            json.dump(_json_report(report), output, indent=2, sort_keys=True)
            output.write("\n")
        print(f"Wrote report: {report_path}")
    return 1 if any_error else 0


def main_fra_round_robin(turns_cap=30, show_profile=False,
                          report_path=None, seed=900, workers=None,
                          match_limit=None):
    """Run one match for every unique pair of non-elite FRA decks."""
    specs = _fra_encounter_specs(non_elite_only=True, all_decks=True)
    if len(specs) < 2:
        raise ValueError("need at least two non-elite FRA decks")

    turn_limit = max(0, int(turns_cap))
    first_seed = int(seed)
    all_pairs = list(combinations(range(len(specs)), 2))
    if match_limit is None:
        pairs = all_pairs
    else:
        match_limit = int(match_limit)
        if match_limit < 1:
            raise ValueError("match_limit must be a positive integer")
        pairs = all_pairs[:match_limit]
    profiler = _new_simulation_profiler()
    standings = {
        spec["deck_guid"]: {
            "deck_guid": spec["deck_guid"],
            "name": spec["name"],
            "points": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "matches": 0,
        }
        for spec in specs
    }
    run_contexts = []
    run_jobs = []
    for match_index, (left_index, right_index) in enumerate(pairs):
        run_seed = first_seed + match_index
        outcome = {}
        run_contexts.append((match_index, run_seed, left_index,
                             right_index, outcome))
        run_jobs.append((run_seed, {
            "turns_cap": turn_limit,
            "deck_specs": [specs[left_index], specs[right_index]],
            "profiler": profiler,
            "run_slot": match_index,
            "outcome": outcome,
        }))

    progress_started = time.monotonic()

    def report_progress(completed, total, job_index, result):
        del job_index
        turns, error = result
        elapsed = time.monotonic() - progress_started
        rate = completed / elapsed * 60 if elapsed > 0 else 0.0
        if error:
            status = f"error: {error.strip().splitlines()[-1]}"
        else:
            status = f"{turns} turns"
        print(f"Round-robin progress: {completed}/{total} matches finished; "
              f"{status}; {rate:.2f} matches/min; "
              f"elapsed {elapsed:.0f}s", flush=True)

    results = _run_simulation_jobs(
        run_jobs, workers=workers, progress=report_progress)
    match_results = []
    errors = []
    for (match_index, run_seed, left_index, right_index, outcome), \
            (turns, err) in zip(run_contexts, results):
        left = specs[left_index]
        right = specs[right_index]
        left_score = standings[left["deck_guid"]]
        right_score = standings[right["deck_guid"]]
        pids = _autoplay_identity(run_seed, match_index)[1]
        game_end = outcome.get("game_end")
        winner_id = game_end.get("winner_id") if game_end else None
        try:
            winner_id = int(winner_id) if winner_id is not None else None
        except (TypeError, ValueError):
            winner_id = None

        match_result = {
            "match": match_index + 1,
            "seed": run_seed,
            "left": left["deck_guid"],
            "right": right["deck_guid"],
            "turns": turns,
            "result": "error" if err else "draw",
        }
        if err:
            errors.append({
                "match": match_index + 1,
                "left": left["name"],
                "right": right["name"],
                "error": err,
            })
            match_result["error"] = err
        elif winner_id == pids[0]:
            left_score["points"] += 1
            left_score["wins"] += 1
            right_score["losses"] += 1
            match_result["result"] = "left_win"
        elif winner_id == pids[1]:
            right_score["points"] += 1
            right_score["wins"] += 1
            left_score["losses"] += 1
            match_result["result"] = "right_win"
        else:
            left_score["draws"] += 1
            right_score["draws"] += 1

        if not err:
            left_score["matches"] = left_score.get("matches", 0) + 1
            right_score["matches"] = right_score.get("matches", 0) + 1
        match_results.append(match_result)

    elapsed_seconds = time.monotonic() - progress_started
    ordered = sorted(
        standings.values(),
        key=lambda item: (-item["points"], -item["wins"], item["losses"],
                          item["name"].casefold(), item["deck_guid"]))
    match_scope = (f"{len(pairs)} matches" if len(pairs) == len(all_pairs)
                   else f"{len(pairs)} of {len(all_pairs)} matches")
    print(f"FRA round-robin: {len(specs)} non-elite decks, "
          f"{match_scope}, max {turn_limit} turns each")
    print(f"Round-robin elapsed: {elapsed_seconds:.2f}s")
    if errors:
        print(f"Round-robin errors: {len(errors)} match(es) were not scored")
        for item in errors[:10]:
            print(f"  match {item['match']}: {item['left']} vs "
                  f"{item['right']} — {item['error'].splitlines()[-1]}")
        if len(errors) > 10:
            print(f"  ... {len(errors) - 10} more error(s)")
    print("Round-robin standings (points; wins-draws-losses):")
    for rank, item in enumerate(ordered, 1):
        point_label = "point" if item["points"] == 1 else "points"
        print(f"  {rank:>2}. {item['name']} — {item['points']} "
              f"{point_label} "
              f"({item['wins']}-{item['draws']}-{item['losses']})")

    if show_profile:
        print("Round-robin profile: "
              f"{profiler['rules_transactions']} RulesPort transactions, "
              f"{profiler['ai_evaluations']} AI evaluations, "
              f"{profiler['combat_evaluations']} combat evaluations")
        print(f"  Rules transaction time: "
              f"{profiler['rules_transaction_seconds']:.2f}s")
        print(f"  AI evaluator time: "
              f"{profiler['ai_evaluation_seconds']:.2f}s")
        print(f"  Champion-power evaluator time: "
              f"{profiler['ai_power_evaluation_seconds']:.2f}s")
        print(f"  Warzone-ability evaluator time: "
              f"{profiler['ai_manual_ability_evaluation_seconds']:.2f}s")
        print(f"  Tunneling evaluator time: "
              f"{profiler['ai_tunnel_evaluation_seconds']:.2f}s")
        print(f"  Combat evaluator time: "
              f"{profiler['combat_evaluation_seconds']:.2f}s")
        if profiler["rules_transaction_by_kind"]:
            print("  Transaction time by kind:")
            for kind, item in sorted(
                    profiler["rules_transaction_by_kind"].items(),
                    key=lambda pair: (-pair[1]["seconds"], pair[0])):
                print(f"    {kind}: {item['seconds']:.2f}s "
                      f"across {item['count']}")
        if profiler["rules_port_dispatches"]:
            print(f"  RulesPort dispatch time: "
                  f"{profiler['rules_port_dispatch_seconds']:.2f}s "
                  f"across {profiler['rules_port_dispatches']}")
            for kind, item in sorted(
                    profiler["rules_port_by_kind"].items(),
                    key=lambda pair: (-pair[1]["seconds"], pair[0])):
                print(f"    {kind}: {item['seconds']:.2f}s "
                      f"across {item['count']}")

    report = {
        "mode": "round_robin",
        "seed": first_seed,
        "max_turns": turn_limit,
        "match_limit": match_limit,
        "total_possible_matches": len(all_pairs),
        "elapsed_seconds": elapsed_seconds,
        "decks": [
            {"deck_guid": spec["deck_guid"], "name": spec["name"]}
            for spec in specs
        ],
        "matches": match_results,
        "standings": ordered,
        "profile": {
            key: value for key, value in profiler.items()
            if not key.startswith("_")
        },
    }
    if report_path:
        with open(report_path, "w", encoding="utf-8") as output:
            json.dump(report, output, indent=2, sort_keys=True)
            output.write("\n")
        print(f"Wrote report: {report_path}")
    return 1 if errors else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("games", nargs="?", type=int,
                        default=DEFAULT_AUTOPLAY_GAMES)
    parser.add_argument("--fra", nargs="*", metavar="DECK_GUID",
                        help="run a duel between FRA decks; in a terminal, "
                             "choose both decks when no GUIDs are supplied")
    parser.add_argument("--choose-decks", action="store_true",
                        help="show the FRA deck chooser (requires --fra and a terminal)")
    parser.add_argument("--turn-limit", type=int, default=30,
                        help="maximum completed turns in FRA mode (default: 30)")
    parser.add_argument("--runs", type=int, default=1,
                        help="number of seeded FRA duels to aggregate")
    parser.add_argument("--seed", type=int, default=900,
                        help="first shuffle seed for FRA mode (default: 900)")
    parser.add_argument("--round-robin", action="store_true",
                        help="run every non-elite FRA deck against every "
                             "other non-elite deck once")
    parser.add_argument("--match-limit", type=int, default=None,
                        help="in round-robin mode, run only the first N "
                             "pairings (useful for profiling)")
    parser.add_argument("--workers", type=int, default=None,
                        help="parallel simulation threads (default: capped "
                             "by CPU count; use 1 for serial execution)")
    parser.add_argument("--profile", action="store_true",
                        help="print AI, RulesPort, and transaction timings")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "OFF"),
        default=None,
        help="logging threshold (default: HEX_LOG_LEVEL or INFO); "
             "progress output is always retained")
    parser.add_argument("--json-report", metavar="PATH",
                        help="write card decisions and actions as JSON")
    args = parser.parse_args()
    if args.log_level is not None:
        set_log_level(args.log_level)
    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.round_robin:
        if args.fra:
            parser.error("--round-robin cannot be combined with FRA deck GUIDs")
        if args.choose_decks:
            parser.error("--round-robin cannot be combined with --choose-decks")
        if args.runs != 1:
            parser.error("--round-robin runs each pairing once; omit --runs")
        if _SIMULATION_DB_COPY:
            print("Simulation database: disposable snapshot")
        raise SystemExit(main_fra_round_robin(
            turns_cap=args.turn_limit, show_profile=args.profile,
            report_path=args.json_report, seed=args.seed,
            workers=args.workers, match_limit=args.match_limit))
    if args.match_limit is not None:
        parser.error("--match-limit requires --round-robin")
    if args.choose_decks and args.fra is None:
        parser.error("--choose-decks requires --fra")
    if args.fra is not None:
        if len(args.fra) not in (0, 2):
            parser.error("--fra accepts either no GUIDs or exactly two")
        if args.choose_decks and args.fra:
            parser.error("--choose-decks cannot be combined with explicit deck GUIDs")
        if args.runs < 1:
            parser.error("--runs must be at least 1")
        deck_guids = args.fra or None
        if args.choose_decks or (not args.fra and sys.stdin.isatty()):
            if not sys.stdin.isatty():
                parser.error("interactive deck selection requires a terminal")
            try:
                deck_guids = _choose_fra_deck_guids()
            except ValueError as exc:
                parser.error(str(exc))
        if _SIMULATION_DB_COPY:
            print("Simulation database: disposable snapshot")
        raise SystemExit(main_fra(
            deck_guids, turns_cap=args.turn_limit,
            show_profile=args.profile, report_path=args.json_report,
            runs=args.runs, seed=args.seed, workers=args.workers))
    main(args.games, workers=args.workers)
