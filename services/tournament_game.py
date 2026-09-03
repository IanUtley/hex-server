"""Tournament PvP game setup — push initial battle events to both players."""

import random, json, threading, re, time

import game_engine as _ge
from db import (_db, log_req, db_game_session_pids, db_game_champion,
                db_game_deck_cards, db_game_draw_cards, db_game_card_type,
                db_game_shuffle_deck, db_champion_template_health,
                db_discard_card, db_delete_game_session,
                db_tournament_by_id)
from encoder import encode_datawrapper, encode_sync_event, compress_gzip, encode_objfmt_response, client_session_guid
from gamemodes.tournament_engine import (
    player_handlers, player_handler_lock, record_tournament_game_result,
)


_ECardCollections = _ge.ECardCollections
_ECardTypes = _ge.ECardTypes
_PVP_INACTIVITY_TIMEOUT_SECONDS = 5 * 60


def _pvp_resource_charge_points(session, card_uid):
    """Return charge points granted by a resource's current ability BOM.

    Resource charge generation is card data, not a second universal rule.
    Set 1 shards each have a BOM ``chargepoints = 1`` effect, so adding a
    hard-coded base charge as well would double-count them.
    """
    row = _db.execute(
        "SELECT card_abilities FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(card_uid))).fetchone()
    if not row:
        return 0
    try:
        abilities = json.loads(row[0] or "[]")
    except Exception:
        abilities = []
    total = 0
    for ability_guid in abilities if isinstance(abilities, list) else []:
        if not isinstance(ability_guid, str):
            continue
        effects = _db.execute(
            "SELECT effect_type, param FROM ability_effects "
            "WHERE ability_guid=? ORDER BY effect_order",
            (ability_guid.lower(),)).fetchall()
        for effect_type, param in effects:
            if effect_type != "CardModifierAbilityEffectTemplate":
                continue
            try:
                modifier = json.loads(param or "{}")
            except Exception:
                continue
            if modifier.get("property") != "chargepoints":
                continue
            try:
                amount = int(modifier.get("amount", 0) or 0)
            except (TypeError, ValueError):
                amount = 0
            if amount > 0:
                total += amount
    return total


def _pvp_gain_charge_trigger_game(handler, session, state, owner_id):
    """Build the objective event stream for a PvP charge-gain trigger."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return None
    owner_id = int(owner_id)
    opp_pid = pids[0] if pids[1] == owner_id else pids[1]
    owner_handler = player_handlers.get(owner_id) or handler
    owner_handler._current_bstate = state
    pl_uid = _ge.UID.make(244, owner_id)
    opp_uid = _ge.UID.make(244, opp_pid)
    game = _ge.Game(int(session.session_id), pl_uid, opp_uid)
    _pvp_populate_game_state(game, state, owner_id, opp_pid)
    from abilities.framework.triggers import resolve_gain_charge_triggers
    resolve_gain_charge_triggers(
        _db, owner_handler, game, session, pl_uid, opp_uid, state, owner_id)
    return game


# ── PvP state persistence (session.turn_order / turn_order_json) ────────────
# Mirrors the battle_engine.load_state / save_state pattern but with a PvP-
# specific schema (two human players, no AI).  State lives in the DB so a
# reconnect can resume.

def pvp_default_state(turn_pid, goes_first_pid):
    return {
        "pvp": True,
        "pids": [turn_pid, goes_first_pid] if turn_pid != goes_first_pid else [turn_pid],
        "turn_pid": turn_pid,
        "goes_first_pid": goes_first_pid,
        "turn_number": 1,
        "phase": 3,       # PickGoesFirst
        "passes": [],
        "kept": [],
        "draws_first_pid": 0,
        # Persisted chess-clock accounting.  Values in
        # priority_elapsed_ticks are TimeSpan ticks (100ns); the client gets
        # whole seconds in TurnPhaseUpdated and converts them back to ticks.
        "priority_elapsed_ticks": {},
        "_priority_clock_pid": 0,
        "_priority_clock_started_ns": 0,
        "_priority_window_pid": 0,
        "_priority_window_started_ns": 0,
    }


# ── per-session mutation lock ───────────────────────────────────────────────
# Each client connection runs its own thread (hconnect_server.main spawns one
# thread per socket).  In a PvP game BOTH players' threads mutate the SAME
# session's turn_order_json (load -> mutate -> save via pvp_save_state), so a
# simultaneous pass/card-play from the two clients is a read-modify-write race
# that can silently clobber a priority/phase write.  A per-session lock
# serializes all PvP state mutations for one game, while different sessions
# (practice mode, multiple 1v1s) proceed fully in parallel.  RLock so a thread
# that already holds the lock (e.g. pass -> resolve_chain) can re-enter.

_session_locks = {}
_session_locks_guard = threading.Lock()


def pvp_session_lock(session):
    """Return the per-session RLock guarding PvP state mutations."""
    sid = int(session.session_id)
    with _session_locks_guard:
        lock = _session_locks.get(sid)
        if lock is None:
            lock = threading.RLock()
            _session_locks[sid] = lock
    return lock


def pvp_discard_session_lock(session):
    """Drop the per-session lock when a game ends (frees memory)."""
    sid = int(session.session_id)
    with _session_locks_guard:
        _session_locks.pop(sid, None)


def _pvp_locked(fn):
    """Decorator: run `fn(...)` holding the per-session mutation lock, so both
    players' threads serialize their PvP state read-modify-write cycles for
    THIS session (other sessions stay parallel).  Accepts the session as the
    first positional arg (pvp_mulligan_next(session, ...)) or the second
    (route_pvp_pass(handler, session, ...)) or keyword ``session``."""
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        session = kwargs.get("session")
        if session is None and len(args) > 1 \
                and hasattr(args[1], "session_id"):
            session = args[1]
        elif session is None and len(args) > 0 \
                and hasattr(args[0], "session_id"):
            session = args[0]
        if session is None:
            return fn(*args, **kwargs)
        with pvp_session_lock(session):
            return fn(*args, **kwargs)
    return wrapper


@_pvp_locked
def pvp_mulligan_next(session, state, just_acted_pid):
    """Advance the sequential mulligan: after `just_acted_pid` kept or
    redrew, decide who is asked next and push their Mulligan prompt.

    Rules (mirror the real client's alternating mulligan):
      - a player who has NOT kept is asked to keep/redraw;
      - a player who redrew and whose opponent already kept is asked again
        (keep again or redraw again, one fewer card each time);
      - once BOTH players have kept, the mulligan ends and the game moves to
        StartGame with greenlight to the turn player.
    Returns True when a player was prompted, False when mulligan ended.
    """
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    kept = list(state.get("kept") or [])
    if len(kept) >= 2:
        # Both players kept — mulligan over, advance to StartTurn.
        state["phase"] = 6
        state["passes"] = []
        state.pop("mulligan_pid", None)
        pvp_save_state(session, state)
        for pid in pids:
            h = player_handlers.get(pid)
            if not h:
                continue
            pt = _ge.UID.make(244, pid)
            opp = _ge.UID.make(244, pids[1] if pid == pids[0] else pids[0])
            turn_uid = _ge.UID.make(244, state["turn_pid"])
            g = _ge.Game(int(session.session_id), pt, opp)
            # Mulligan -> StartGame -> StartTurn in ONE packet.  StartGame has
            # no client interaction (m_TurnPhasePlayers=None, UIBattle pushes
            # no state for it) so no pass can ever arrive during it — pushing
            # ONLY StartGame left both clients stuck on "Start Game".  Pushing
            # StartTurn right after gives the client the valid transitions
            # (Mulligan->StartGame->StartTurn) and starts the turn cycle.
            # Active/priority = the TURN player for both clients (NOT each
            # client's self), so both clients agree on who starts.
            g.push_disable_interface(False)
            g.push_turn_phase(_ge.ETurnPhases.StartGame, turn_uid, turn_uid)
            g.push_turn_phase(_ge.ETurnPhases.StartTurn, turn_uid, turn_uid)
            _send_pvp_packet(h, session, g, pt, "mulligan-done")
        # StartTurn/Ready/Prep/Draw are non-interactive — neither client
        # auto-passes them and the opponent can't pass without priority, so
        # the server marches the phase forward itself until the next phase
        # either player has a STOP on (FirstMainPhase by default).  Without
        # this the game sits on "Start Turn" forever.
        pvp_advance_past_non_stops(session, state)
        log_req("    PvP mulligan complete — auto-advanced to "
                f"phase {state['phase']} for turn player {state['turn_pid']}")
        # Start the server-side priority watchdog for clock flushing and
        # inactivity expiry. It does not send periodic client events.
        pvp_start_priority_watchdog(session)
        return False
    # Only one (or neither) has kept.  Ask the other player if they haven't
    # kept yet; otherwise (the other player already kept) re-ask the player
    # who just acted (they must keep or redraw again, one fewer card).
    other_pid = pids[0] if pids[1] == just_acted_pid else pids[1]
    next_pid = other_pid if other_pid not in kept else just_acted_pid
    state["mulligan_pid"] = next_pid
    pvp_save_state(session, state)
    _pvp_push_mulligan_prompt(session, state, next_pid)
    return True


def _pvp_push_mulligan_prompt(session, state, ask_pid):
    """Hand greenlight to the active mulliganer.

    Both clients already entered the Mulligan phase together (the dialog is
    open on each); the client toggles the Keep/Redraw buttons from the
    GreenLight events — the active player gets greenlight, the other loses it.
    Re-pushing the Mulligan phase would NOT reopen a dismissed dialog (the
    client only pushes BattleStateMulligan when the phase CHANGES), so we only
    send greenlight handoffs here."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    opp_pid = pids[0] if pids[1] == ask_pid else pids[1]
    for pid in pids:
        h = player_handlers.get(pid)
        if not h:
            continue
        pt = _ge.UID.make(244, pid)
        opp = _ge.UID.make(244, pids[0] if pid == pids[1] else pids[1])
        g = _ge.Game(int(session.session_id), pt, opp)
        _pvp_populate_game_state(
            g, state, pid, pids[0] if pid == pids[1] else pids[1])
        g.push_player_updated(pt, champ_id=_ge.SessionCardId(
            _ge.UID(int((state.get("champ_map") or {}).get(str(pid), 0))))
            if (state.get("champ_map") or {}).get(str(pid)) else None)
        # Keep the dialog visible for the waiting client, but make the
        # already-answered client's battle UI inert.  BattleStateMulligan can
        # otherwise leave DrawAgain clickable even though this player no
        # longer has priority.
        g.push_disable_interface(pid != ask_pid)
        # GreenLight is a per-client state update, not a server-side broadcast
        # marker.  Every client must receive the same priority owner: the
        # asker gains it and the other client explicitly loses it.  Omitting
        # this event from the waiting client's packet leaves its old
        # HasPriority state untouched and can stall the mulligan UI.
        g.push_green_light(_ge.UID.make(244, ask_pid),
                           _ge.EPriorityContext.Normal)
        _send_pvp_packet(h, session, g, pt, "mulligan")
    state["priority_pid"] = ask_pid
    pvp_save_state(session, state)
    log_req(f"    PvP mulligan: greenlight to pid {ask_pid} "
            f"(opponent {opp_pid} waiting)")


def pvp_load_state(session):
    try:
        data = session.turn_order
        if isinstance(data, dict) and data.get("pvp"):
            return data
    except (ValueError, TypeError):
        pass
    return None


def pvp_save_state(session, state):
    _pvp_flush_priority_clock(state)
    session.turn_order = state
    session._persist()


def _pvp_flush_priority_clock(state, now_ns=None):
    """Accumulate wall-clock time for the currently prioritised player.

    The PvP state is written after priority-changing actions, so this clock is
    deliberately independent of the mutable ``priority_pid`` value.  That
    lets us account for the interval belonging to the previous priority owner
    before starting the new owner's interval.  The watchdog also flushes it
    periodically, limiting time lost if the process stops unexpectedly.
    """
    if not isinstance(state, dict):
        return
    now_ns = int(now_ns if now_ns is not None else time.time_ns())
    try:
        current_pid = int(state.get("priority_pid") or 0)
    except (TypeError, ValueError):
        current_pid = 0
    try:
        clock_pid = int(state.get("_priority_clock_pid") or 0)
    except (TypeError, ValueError):
        clock_pid = 0
    try:
        started_ns = int(state.get("_priority_clock_started_ns") or 0)
    except (TypeError, ValueError):
        started_ns = 0

    elapsed = state.get("priority_elapsed_ticks")
    if not isinstance(elapsed, dict):
        elapsed = {}
        state["priority_elapsed_ticks"] = elapsed

    if clock_pid and started_ns:
        delta_ticks = max(0, now_ns - started_ns) // 100
        if delta_ticks:
            key = str(clock_pid)
            elapsed[key] = int(elapsed.get(key, 0) or 0) + delta_ticks

    # A priority change closes the old interval and starts the new one.  When
    # priority is unset, leave the clock stopped rather than charging either
    # player while the game is in a non-priority setup phase.
    state["_priority_clock_pid"] = current_pid
    state["_priority_clock_started_ns"] = now_ns if current_pid else 0

    # This marker is intentionally NOT refreshed on every state save.  It is
    # the client's five-minute inactivity/turn-phase window, which remains
    # active for as long as the same player retains priority.  The cumulative
    # clock above is flushed frequently; conflating the two would prevent the
    # timeout from ever firing.
    try:
        window_pid = int(state.get("_priority_window_pid") or 0)
        window_started_ns = int(
            state.get("_priority_window_started_ns") or 0)
    except (TypeError, ValueError):
        window_pid = window_started_ns = 0
    if current_pid != window_pid:
        window_pid = current_pid
        window_started_ns = now_ns if current_pid else 0
    elif current_pid and not window_started_ns:
        window_started_ns = now_ns
    state["_priority_window_pid"] = window_pid
    state["_priority_window_started_ns"] = window_started_ns


def _pvp_priority_elapsed_ticks(state, pid, now_ns=None):
    """Return persisted plus currently-running priority time for ``pid``."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return 0
    elapsed = state.get("priority_elapsed_ticks") if isinstance(state, dict) else None
    try:
        total = int((elapsed or {}).get(str(pid), 0) or 0)
    except (AttributeError, TypeError, ValueError):
        total = 0
    try:
        clock_pid = int(state.get("_priority_clock_pid") or 0)
        started_ns = int(state.get("_priority_clock_started_ns") or 0)
    except (TypeError, ValueError):
        clock_pid = started_ns = 0
    if clock_pid == pid and started_ns:
        now_ns = int(now_ns if now_ns is not None else time.time_ns())
        total += max(0, now_ns - started_ns) // 100
    return total


def _pvp_priority_window_elapsed_seconds(state, pid, now_ns=None):
    """Return seconds since ``pid`` most recently gained priority."""
    try:
        pid = int(pid)
        window_pid = int(state.get("_priority_window_pid") or 0)
        started_ns = int(state.get("_priority_window_started_ns") or 0)
    except (TypeError, ValueError):
        return 0
    if pid <= 0 or window_pid != pid or not started_ns:
        return 0
    now_ns = int(now_ns if now_ns is not None else time.time_ns())
    return max(0, now_ns - started_ns) // 1_000_000_000


def _pvp_push_turn_phase_with_elapsed(game, phase, active_uid, priority_uid,
                                      elapsed_seconds):
    """Push TurnPhaseUpdated with the cumulative clock value for reconnect."""
    game.push_turn_phase(phase, active_uid, priority_uid)
    if (game.events and
            isinstance(game.events[-1], _ge.TurnPhaseUpdatedSessionEventArgs)):
        game.events[-1].priority_timer_elapsed = max(0, int(elapsed_seconds))


def _pvp_state_thresholds(state, pid):
    """Return a PlayerUpdated-compatible threshold map from PvP state."""
    out = {}
    for key, value in (state.get(f"thresh_{pid}") or {}).items():
        try:
            out[int(key)] = int(value or 0)
        except (TypeError, ValueError):
            continue
    return out


def _pvp_populate_game_state(game, state, player_pid, opponent_pid):
    """Copy the persisted PvP HUD state onto a newly-created Game.

    PlayerUpdated reads its values from Game, whose defaults are 20 health and
    zero resources/charges.  Every PvP event stream that creates a fresh Game
    must populate it before pushing a PlayerUpdated event.
    """
    game.player_health = int(state.get(f"hp_{player_pid}", 20))
    game.ai_health = int(state.get(f"hp_{opponent_pid}", 20))
    game.player_resources = int(state.get(f"res_{player_pid}", 0))
    game.player_total_resources = int(
        state.get(f"res_total_{player_pid}", 0))
    game.ai_resources = int(state.get(f"res_{opponent_pid}", 0))
    game.ai_total_resources = int(
        state.get(f"res_total_{opponent_pid}", 0))
    game.player_charges = int(state.get(f"chg_{player_pid}", 0))
    game.ai_charges = int(state.get(f"chg_{opponent_pid}", 0))
    game.player_spell_points = int(state.get(f"sp_{player_pid}", 0))
    game.ai_spell_points = int(state.get(f"sp_{opponent_pid}", 0))
    game.player_threshold = _pvp_state_thresholds(state, player_pid)
    game.ai_threshold = _pvp_state_thresholds(state, opponent_pid)
    game.turn_number = int(state.get("turn_number", 1))


def _pvp_sync_view_to_state(state, view, player_pid, opponent_pid):
    """Persist per-player values changed through a FRA-shaped PvP view.

    Ability resolution uses the same FRA-shaped dictionary in Practice and
    PvP.  It is a view, not a live alias, so resource/threshold/charge changes
    made by a BOM must be copied back before the next legality/options check.
    """
    for key, view_key, pid in (
            (f"esc_{player_pid}", "player_escalation_uses", player_pid),
            (f"esc_{opponent_pid}", "ai_escalation_uses", opponent_pid)):
        if view_key in view:
            state[key] = int(view.get(view_key, state.get(key, 0)) or 0)
    for key, view_key, pid in (
            (f"res_{player_pid}", "player_resources", player_pid),
            (f"res_{opponent_pid}", "ai_resources", opponent_pid),
            (f"res_total_{player_pid}", "player_total_resources", player_pid),
            (f"res_total_{opponent_pid}", "ai_total_resources", opponent_pid),
            (f"chg_{player_pid}", "player_charges", player_pid),
            (f"chg_{opponent_pid}", "ai_charges", opponent_pid),
            (f"sp_{player_pid}", "player_spell_points", player_pid),
            (f"sp_{opponent_pid}", "ai_spell_points", opponent_pid)):
        if view_key in view:
            state[key] = int(view.get(view_key, state.get(key, 0)) or 0)
    if "briar_legions_entered" in view:
        state["briar_legions_entered"] = int(
            view.get("briar_legions_entered",
                     state.get("briar_legions_entered", 0)) or 0)
    if "player_threshold" in view:
        state[f"thresh_{player_pid}"] = dict(view.get("player_threshold") or {})
    if "ai_threshold" in view:
        state[f"thresh_{opponent_pid}"] = dict(view.get("ai_threshold") or {})
    if "damaged_opponent_this_turn" in view:
        state["damaged_opponent_this_turn"] = list(
            view.get("damaged_opponent_this_turn") or [])
    if "damaged_opponent_turn" in view:
        state["damaged_opponent_turn"] = int(
            view.get("damaged_opponent_turn") or 0)
    if "bonus_turn_pid" in view:
        state["bonus_turn_pid"] = int(view.get("bonus_turn_pid") or 0)


def _pvp_log_stack(state, label):
    """Log the current chain/stack size + which players have passed it, so the
    server log can be correlated with the CLIENT's resolve requests:
    every chain item on the stack needs BOTH players to pass, and each phase
    transition that happens while the chain is non-empty also re-announces it.
    Expect roughly: plays/passes/resolves ≈ 2 * items-on-chain + 2 * phase."""
    try:
        st = state.get("stack") or []
        sp = state.get("stack_passed") or []
        ph = state.get("phase", "?")
        turn = state.get("turn_pid", "?")
        log_req(f"    PvP stack[{label}]: {len(st)} item(s) phase={ph} "
                f"turn={turn} passed={sp}")
    except Exception as _e:
        log_req(f"    PvP stack[{label}] log error: {_e}")


# ── priority watchdog ──────────────────────────────────────────────────────
# The original implementation periodically re-pushed GreenLight events to
# correct client-side priority drift.  Normal PvP transitions now explicitly
# send the correct priority to both clients, so periodic client events are no
# longer needed and would pollute replay capture.  Keep the daemon only as a
# server-side clock/inactivity watchdog.

_watchdog_sessions = set()
_watchdog_lock = threading.Lock()


def pvp_start_priority_watchdog(session):
    """Start the per-session priority watchdog (idempotent per session)."""
    sid = int(session.session_id)
    with _watchdog_lock:
        if sid in _watchdog_sessions:
            return
        _watchdog_sessions.add(sid)
    threading.Thread(target=_pvp_priority_watchdog_loop,
                     args=(session, sid), daemon=True).start()
    log_req(f"    PvP priority watchdog started for session {sid}")


def _pvp_priority_watchdog_loop(session, sid):
    import time
    import game_session as _gs
    fail_count = 0
    try:
        while True:
            try:
                # The transaction handlers call find_session_by_player, which
                # returns a NEW GameSession object per request.  Load that
                # object only AFTER taking the per-session lock: loading it
                # before the lock lets the watchdog re-save an old snapshot
                # after a phase transition (for example, erasing a resource
                # refill from 2 back to 0).
                with pvp_session_lock(session):
                    fresh = _gs.find_session_by_id(sid)
                    if fresh is None:
                        log_req(f"    PvP priority watchdog stopped for {sid}: "
                                "session not found")
                        return
                    if getattr(fresh, "state", "") == "ended":
                        log_req(f"    PvP priority watchdog stopped for {sid}: "
                                "session ended")
                        return
                    state = pvp_load_state(fresh)
                    if not state or not state.get("pvp"):
                        log_req(f"    PvP priority watchdog stopped for {sid}: "
                                "no PvP state")
                        return
                    pid = state.get("priority_pid")
                    if not pid:
                        log_req(f"    PvP priority watchdog stopped for {sid}: "
                                "no priority owner")
                        return
                    pids = db_game_session_pids(fresh.session_id)
                    if len(pids) < 2:
                        log_req(f"    PvP priority watchdog stopped for {sid}: "
                                f"only {len(pids)} player(s)")
                        return
                    if pid not in pids:
                        log_req(f"    PvP priority watchdog stopped for {sid}: "
                                f"priority owner {pid} is not in session")
                        return
                    # Flush the active priority interval while holding the
                    # same lock used by transactions.  The latest DB snapshot
                    # is now the one being flushed, so resource/phase writes
                    # cannot be overwritten by a stale watchdog snapshot.
                    pvp_save_state(fresh, state)
                    if (_pvp_priority_window_elapsed_seconds(state, pid) >=
                            _PVP_INACTIVITY_TIMEOUT_SECONDS):
                        winner_pid = (pids[1]
                                      if pids[0] == pid else pids[0])
                        log_req(
                            f"    PvP inactivity timeout: pid {pid} "
                            f"exceeded {_PVP_INACTIVITY_TIMEOUT_SECONDS}s "
                            f"of priority in session {sid}")
                        _pvp_end_game(
                            fresh, state, winner_pid, pid,
                            "priority inactivity timeout")
                        return
            except Exception as e:
                log_req(f"    PvP priority watchdog stopped for {sid}: {e}")
                return
            time.sleep(5)
    finally:
        with _watchdog_lock:
            _watchdog_sessions.discard(sid)
        log_req(f"    PvP priority watchdog stopped for session {sid}")


def _pvp_sync_game_state(session):
    """Push PlayerUpdated to both players after a state change so each
    client sees current health / charges / champion."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    state = pvp_load_state(session)
    if not state:
        return
    champ_map = state.get("champ_map", {})
    for pid in pids:
        h = player_handlers.get(pid)
        if not h:
            continue
        pl_uid = _ge.UID.make(244, pid)
        opp = pids[1] if pid == pids[0] else pids[0]
        opp_uid = _ge.UID.make(244, opp)
        g = _ge.Game(int(session.session_id), pl_uid, opp_uid)
        g.player_health = int(state.get(f"hp_{pid}", 20))
        g.ai_health = int(state.get(f"hp_{opp}", 20))
        g.player_resources = int(state.get(f"res_{pid}", 0))
        g.player_total_resources = int(state.get(f"res_total_{pid}", 0))
        g.ai_resources = int(state.get(f"res_{opp}", 0))
        g.ai_total_resources = int(state.get(f"res_total_{opp}", 0))
        _pvp_populate_game_state(g, state, pid, opp)

        for target_pid in pids:
            target_uid = _ge.UID.make(244, target_pid)
            cu = int(champ_map.get(str(target_pid), 0))
            champ_scid = _ge.SessionCardId(_ge.UID(cu)) if cu else None
            g.push_player_updated(target_uid, champ_id=champ_scid)

        if g.events:
            pkt = g.make_network_packet(pl_uid)
            dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                     client_session_guid(h))
            h.scnt += 1
            h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
                    "target": "ServiceGameSession", "instance": str(session.server_id),
                    "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
            log_req(f"    PvP sync: PlayerUpdated pushed to pid {pid}")


def _pvp_run_draw(session, state):
    """The Draw phase: the turn player draws one card (except the play-first
    player on turn 1), fires the draw-related triggers ONCE on an objective
    event stream, and returns (drawer_events, opp_events) for each client's
    packet — or None when nothing was drawn.  Mirrors PvE _player_draw_card:
    CardWouldBeDrawnEvent / CardWouldEnterZoneEvent replacement triggers first,
    then CardMoved+CardDrawn+CardUpdated (full data), then CardDrawnEvent for
    BOTH sides' cards."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return None
    turn_pid = state.get("turn_pid")
    opp_pid = pids[0] if pids[1] == turn_pid else pids[1]
    turn_num = int(state.get("turn_number", 1))
    if not (turn_num > 1 or state.get("draws_first_pid") == turn_pid):
        # Play-first player on turn 1 skips the draw.
        return None
    drawn = db_game_draw_cards(session.session_id, turn_pid, 1)
    if not drawn:
        # Deck-out: a player who must draw from an empty deck loses.
        _pvp_end_game(session, state, opp_pid, turn_pid,
                      "deck empty on draw")
        return None
    cu, tg = drawn[0]
    scid = _ge.SessionCardId(_ge.UID(int(cu)))
    ct_str = db_game_card_type(tg)
    ct = _ge.card_type_from_db(ct_str) if ct_str else _ECardTypes.Troop
    turn_uid_p = _ge.UID.make(244, turn_pid)
    opp_uid_p = _ge.UID.make(244, opp_pid)
    draw_h = player_handlers.get(turn_pid)
    g = _ge.Game(int(session.session_id), turn_uid_p, opp_uid_p)
    g.player_health = int(state.get(f"hp_{turn_pid}", 20))
    g.ai_health = int(state.get(f"hp_{opp_pid}", 20))
    # Replacement triggers: "If you would draw a card..." (The Transcended),
    # "If this would enter a hand..." (Booby Trap).
    try:
        from abilities.framework.triggers import resolve_triggers
        view = _pvp_fra_view(state, turn_pid, opp_pid)
        repl_draw = resolve_triggers(_db, draw_h, g, session, turn_uid_p,
                                     opp_uid_p, view, "CardWouldBeDrawnEvent",
                                     None, turn_pid) if draw_h else None
        repl_zone = resolve_triggers(_db, draw_h, g, session, turn_uid_p,
                                     opp_uid_p, view, "CardWouldEnterZoneEvent",
                                     int(cu), turn_pid) if draw_h else None
        if repl_draw or repl_zone:
            # The draw was replaced by a trigger effect — still send the
            # trigger's events (they may draw/buff), then finish.
            pvp_save_state(session, state)
            return list(g.events), []
    except Exception as e:
        log_req(f"    PvP draw replacement trigger error: {e}")
    # Register the FULL CardDef so the drawn card renders complete.
    _d_tpl, _d_ct, _d_nm, d_cost, d_atk, d_def, _d_gx = \
        (draw_h._card_full_data(g, scid, tg) if draw_h
         else (tg, ct, "", 0, 0, 0, 0))
    # Objective stream: CardMoved + CardDrawn + CardUpdated (full data, incl.
    # the gem_type so socketed-gem abilities highlight on the drawn card).
    g.push_card_moved(scid, turn_uid_p, _ECardCollections.Hand,
                      _ge.ECardLocations.Top, 1)
    g.push_card_drawn(scid, turn_uid_p, 1)
    g.push_card_updated(scid, turn_uid_p, _ECardCollections.Hand,
                        ct, template_id=tg, cost=d_cost, attack=d_atk,
                        defense=d_def, gems=_d_gx)
    # Zone entry is separate from drawing.  Hand-bound triggers such as
    # Reginald's granted ability must fire for whichever player now controls
    # the destination deck, including the opponent after a Reginald transfer.
    view = _pvp_fra_view(state, turn_pid, opp_pid)
    try:
        from abilities.framework.triggers import resolve_triggers
        if draw_h:
            resolve_triggers(_db, draw_h, g, session, turn_uid_p, opp_uid_p,
                             view, "CardEnteredZoneEvent", int(cu), turn_pid)
    except Exception as e:
        log_req(f"    PvP CardEnteredZoneEvent trigger error: {e}")
    # "When you draw" triggers (both sides' cards react — "when you draw" and
    # "when an opposing champion draws").  The client's CardDrawnEvent carries
    # SourceCardId = the drawing champion, TargetCardId = the drawn card.
    champ_map = state.get("champ_map") or {}
    champ_uid = int(champ_map.get(str(turn_pid), 0)) or None
    try:
        from abilities.framework.triggers import resolve_triggers
        # Reuse the same authoritative view that received the zone-entry
        # trigger above; otherwise its stack/health mutations would be lost
        # before the CardDrawnEvent pass.
        if draw_h:
            resolve_triggers(_db, draw_h, g, session, turn_uid_p, opp_uid_p,
                             view, "CardDrawnEvent", champ_uid, turn_pid,
                             extra_target=int(cu))
    except Exception as e:
        log_req(f"    PvP CardDrawnEvent trigger error: {e}")
    # Copy health/stack changes from the draw triggers back into state.
    if view.get("player_health") is not None:
        state[f"hp_{turn_pid}"] = int(view["player_health"])
    if view.get("ai_health") is not None:
        state[f"hp_{opp_pid}"] = int(view["ai_health"])
    state["stack"] = view.get("stack") or []
    state["stack_passed"] = []
    pvp_save_state(session, state)
    # The opponent's variant shows the same Deck -> Hand move but face-down
    # (nulling) — no CardDrawn sound/event for them.
    g2 = _ge.Game(int(session.session_id), turn_uid_p, opp_uid_p)
    g2.events = []
    # The opponent must receive the same trigger/chain events as the drawer.
    # Only the drawn card's private face-up events differ.  Previously g2
    # contained only a face-down Deck -> Hand update, so Twisted Fate's
    # AbilityPushedOnChain event existed in the authoritative stack and in the
    # drawer's packet but was invisible on the other client.
    g2.card_defs = dict(g.card_defs)
    g2.push_card_moved(scid, turn_uid_p, _ECardCollections.Hand,
                       _ge.ECardLocations.Top, 1)
    g2.push_card_updated(scid, turn_uid_p, _ECardCollections.Hand,
                         ct, template_id=tg, nulling=True)
    # Preserve all events that are not the private representation of the
    # drawn card: trigger chain entries, source-card activation flashes, and
    # any resulting zone changes are objective and must reach both clients.
    for event in g.events:
        if (getattr(event, "session_card_id", None) == scid
                and event.__class__.__name__ in (
                    "CardMovedSessionEventArgs",
                    "CardDrawnSessionEventArgs",
                    "CardUpdatedSessionEventArgs")):
            continue
        g2._push(event)
    log_req(f"    PvP draw: pid {turn_pid} drew card {cu} "
            f"({_d_nm or ''})")
    return list(g.events), list(g2.events)


def _pvp_run_phase_start(session, state, phase):
    """Push a phase transition to BOTH players in ONE packet each, with the
    GreenLight to the turn player FIRST.

    Order matters: the client's OnTurnPhaseUpdated fires a spurious
    RequestPrioritySync + state-stack churn (Killed/Popping states) when a
    phase event names it priority player while it does NOT yet hold the
    greenlight (HasPriority false).  Sending the greenlight BEFORE the phase
    in the same packet means the client already has priority when it processes
    the phase, so no sync is requested and the correct BattleState is pushed."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    turn_uid = state["turn_pid"]
    turn_uid_p = _ge.UID.make(244, turn_uid)
    champ_map = state.get("champ_map", {})
    # STARTTURN (phase 6): fire "At the start of your turn" triggers for the
    # turn player and re-push BOTH champions warm — mirrors PvE
    # _advance_to_priority (hconnect ~3249): TurnStartedEvent triggers +
    # _push_champions_warm so the client's State.Cards cache has both
    # ChampionSessionCardIds (prevents KeyNotFoundException in OnTurnPhaseUpdated).
    if phase == _ge.ETurnPhases.StartTurn:
        turn_h = player_handlers.get(turn_uid)
        if turn_h:
            try:
                opp_uid_st = _ge.UID.make(
                    244, pids[1] if turn_uid == pids[0] else pids[0])
                warm = _ge.Game(int(session.session_id), turn_uid_p, opp_uid_st)
                warm.player_health = int(state.get(f"hp_{turn_uid}", 20))
                warm.ai_health = int(state.get(
                    f"hp_{pids[1] if turn_uid == pids[0] else pids[0]}", 20))
                from abilities.framework.triggers import resolve_triggers
                st_view = _pvp_fra_view(
                    state, turn_uid,
                    pids[1] if turn_uid == pids[0] else pids[0])
                resolve_triggers(_db, turn_h, warm, session, turn_uid_p,
                                 opp_uid_st, st_view, "TurnStartedEvent",
                                 None, turn_uid)
                # Persist trigger mutations before re-pushing champion card
                # data; CardUpdated carries the current health as defense and
                # must not overwrite a just-applied Warbot damage event with
                # the old state value.
                if st_view.get("player_health") is not None:
                    state[f"hp_{turn_uid}"] = int(st_view["player_health"])
                if st_view.get("ai_health") is not None:
                    state[f"hp_{pids[1] if turn_uid == pids[0] else pids[0]}"] = \
                        int(st_view["ai_health"])
                # Re-push both champions with their abilities (CardUpdated) so
                # the client cache is warm for the phases that follow.
                for cpid in pids:
                    c_uid = _ge.UID.make(244, cpid)
                    cu64 = int(champ_map.get(str(cpid), 0))
                    if cu64:
                        c_scid = _ge.SessionCardId(_ge.UID(cu64))
                        c_row = _db.execute(
                            "SELECT template_guid FROM game_cards "
                            "WHERE session_id=? AND card_uid=?",
                            (session.session_id, cu64)).fetchone()
                        if c_row:
                            turn_h._card_full_data(warm, c_scid, c_row[0])
                            # Champion re-push carries the CURRENT health as
                            # defense — otherwise the client's champion
                            # representation resets to the template's base
                            # (20), briefly showing 20 HP at each phase change
                            # before the real value re-renders.
                            warm.push_card_updated(
                                c_scid, c_uid, _ge.ECardCollections.Champions,
                                _ge.ECardTypes.Champion,
                                template_id=c_row[0],
                                defense=int(state.get(f"hp_{cpid}", 20)))
                pvp_save_state(session, state)
                if warm.events:
                    _pvp_send_same_events(session, warm, turn_uid_p, opp_uid_st)
                log_req(f"    PvP StartTurn: TurnStartedEvent fired + "
                        f"champions re-pushed for {turn_uid}")
            except Exception as e:
                import traceback
                log_req(f"    PvP StartTurn trigger error: {e}")
                traceback.print_exc()
        if _pvp_check_game_end(session, state):
            return
    # Prep happens once per turn: refill resources + ready/untap the turn
    # player's warzone troops in the DB.  The CardUpdated events go to BOTH
    # players (each pushes them into its own packet below), with the TURN
    # player as the card controller so both screens untap the right troops.
    prep_wz = []   # (scid, template_guid, card_type, state) ready/untapped
    if phase == 8:
        total = int(state.get(f"res_{turn_uid}", 0))
        state[f"res_{turn_uid}"] = int(state.get(f"res_total_{turn_uid}", 0))
        state[f"res_played_{turn_uid}"] = 0
        from abilities.framework._shared import clear_expired_temporary_attributes
        clear_expired_temporary_attributes(
            _db, session.session_id, turn_uid, "start_turn",
            clear_stat_buffs=True)
        clear_expired_temporary_attributes(
            _db, session.session_id, turn_uid, "prep",
            clear_stat_buffs=True)
        # Clear combat states (Tapped, Attacking, HasAttacked, Blocking,
        # HasBlocked) and CameOutThisTurn; set StartedATurnOnYourSide so
        # troops that survived to this turn are no longer summoning sick and
        # can be declared as attackers.  Mirrors the PvE Prep.
        wz_rows = _db.execute(
            "SELECT card_uid, template_guid FROM game_cards "
            "WHERE session_id=? AND user_id=? AND location='warzone'",
            (session.session_id, turn_uid)).fetchall()
        for wzr in wz_rows:
            wz_uid = int(wzr[0])
            _db.execute(
                "UPDATE game_cards SET card_state = (card_state | ?) & ~?, "
                "card_damage = 0 "
                "WHERE session_id=? AND card_uid=?",
                (_ge.ECardStates.StartedATurnOnYourSide,
                 _ge.ECardStates.CameOutThisTurn |
                 _ge.ECardStates.Tapped |
                 _ge.ECardStates.Attacking |
                 _ge.ECardStates.HasAttacked |
                 _ge.ECardStates.Blocking |
                 _ge.ECardStates.HasBlocked,
                 session.session_id, wz_uid))
            from db import db_card_state_raw
            pstate = db_card_state_raw(session.session_id, wz_uid)
            if not pstate:
                pstate = _ge.ECardStates.StartedATurnOnYourSide
            ct_str = db_game_card_type(wzr[1])
            wz_ct = _ge.card_type_from_db(ct_str) if ct_str else _ECardTypes.Troop
            prep_wz.append((_ge.SessionCardId(_ge.UID(wz_uid)), wzr[1],
                            wz_ct, pstate))
        _db.commit()
        log_req(f"    PvP Prep: refilled {turn_uid} to "
                f"{state.get(f'res_total_{turn_uid}')}, readied "
                f"{len(prep_wz)} warzone troop(s)")
    # Draw happens ONCE per turn (before the per-player loop): build the
    # objective draw event stream + trigger events, then splice per-client
    # variants into each packet below.
    pvp_draw_cache = {}
    if phase == 9:
        dr = _pvp_run_draw(session, state)
        if dr is not None:
            for _pid in pids:
                pvp_draw_cache[_pid] = dr
            if _pvp_check_game_end(session, state):
                return
    chain_from_phase_start = bool(state.get("stack"))
    defender_pid = pids[1] if turn_uid == pids[0] else pids[0]
    phase_priority_pid = (defender_pid
                          if phase == _ge.ETurnPhases.DeclareDefense
                          else turn_uid)
    if phase not in (3, 4):
        # Start the new priority interval before emitting TurnPhaseUpdated so
        # the event can carry the cumulative time already spent by this
        # player in earlier priority windows.
        state["priority_pid"] = phase_priority_pid
        pvp_save_state(session, state)
    for pid in pids:
        h = player_handlers.get(pid)
        if not h:
            continue
        is_me = (pid == turn_uid)
        pl_t = _ge.UID.make(244, pid)
        opp_t = _ge.UID.make(244, pids[1] if pid == pids[0] else pids[0])
        g = _ge.Game(int(session.session_id), pl_t, opp_t)
        g.player_health = int(state.get(f"hp_{pid}", 20))
        g.ai_health = int(state.get(f"hp_{pids[1] if pid == pids[0] else pids[0]}", 20))
        g.player_resources = int(state.get(f"res_{pid}", 0))
        g.player_total_resources = int(state.get(f"res_total_{pid}", 0))
        g.ai_resources = int(state.get(f"res_{pids[1] if pid == pids[0] else pids[0]}", 0))
        g.ai_total_resources = int(state.get(f"res_total_{pids[1] if pid == pids[0] else pids[0]}", 0))
        g.player_charges = int(state.get(f"chg_{pid}", 0))
        g.ai_charges = int(state.get(f"chg_{pids[1] if pid == pids[0] else pids[0]}", 0))
        _pvp_populate_game_state(
            g, state, pid, pids[1] if pid == pids[0] else pids[0])

        if phase == 8 and is_me:   # Prep — refill available to the pool
            g.player_resources = int(state.get(f"res_{turn_uid}", 0))
            g.player_total_resources = int(state.get(f"res_total_{turn_uid}", 0))
        if phase == 8 and prep_wz:
            # The ready/untap CardUpdateds reach BOTH clients — the troop's
            # controller is the TURN player (not the receiving player), so the
            # opponent's client untaps the opponent's troops too.
            wz_handler = player_handlers.get(turn_uid)
            for wz_scid, wz_tpl, wz_ct, pstate in prep_wz:
                if wz_handler:
                    wz_handler._card_full_data(g, wz_scid, wz_tpl)
                g.push_card_updated(wz_scid, turn_uid_p,
                                    _ECardCollections.Warzone, wz_ct,
                                    template_id=wz_tpl, state=pstate)
        my_champ_uid = int(champ_map.get(str(pid), 0))
        my_champ = _ge.SessionCardId(_ge.UID(my_champ_uid)) if my_champ_uid else None

        # At DeclareDefense the DEFENDER holds priority (they must decide
        # blocks) even though the TURN player is the active player.  Pushing
        # priority to the turn player here makes the defender's client push
        # BattleStateInactivePriorityWindow instead of BattleStateDeclareBlockers
        # and never show the pass/Skip button — stalling combat.  Mirror PvE:
        # active = turn player, priority = defender at DeclareDefense.
        priority_pid = phase_priority_pid
        prio_uid = _ge.UID.make(244, priority_pid)

        # GreenLight to the PRIORITY player FIRST, then the TurnPhase — so the
        # priority player's client has HasPriority set when it processes the
        # phase (no spurious priority sync), and the other client loses it.
        g.push_green_light(
            prio_uid,
            (_ge.EPriorityContext.ResolveTopOfChain
             if chain_from_phase_start else _ge.EPriorityContext.Normal))
        priority_elapsed_seconds = (
            _pvp_priority_elapsed_ticks(state, priority_pid) // 10_000_000
            if phase not in (3, 4) else 0)
        _pvp_push_turn_phase_with_elapsed(
            g, phase, turn_uid_p, prio_uid, priority_elapsed_seconds)

        # Re-push all warzone cards so the board matches the DB after any
        # state/attribute shift this phase (mirrors PvE _push_warzone_card_updates).
        pvp_push_warzone_updates(session, state, game=g)

        if phase == 8:
            # Push PlayerUpdated for BOTH players so both HUDs see the
            # refilled resource counts (mirrors PvE 3487-3488).
            g.push_player_updated(pl_t, champ_id=my_champ)
            opp_champ_uid = int(champ_map.get(
                str(pids[1] if pid == pids[0] else pids[0]), 0))
            g.push_player_updated(
                _ge.UID.make(244, pids[1] if pid == pids[0] else pids[0]),
                champ_id=_ge.SessionCardId(_ge.UID(opp_champ_uid))
                if opp_champ_uid else None)
        elif phase == 9:
            # Draw phase: the turn player draws one card, with draw triggers
            # fired ONCE on an objective stream (see _pvp_run_draw).  The
            # drawer gets the face-up CardUpdated; the opponent gets the
            # face-down move (their deck counter still drops).
            dr = pvp_draw_cache.get(pid)
            if dr is not None:
                if is_me:
                    for ev in dr[0]:
                        g._push(ev)
                else:
                    for ev in dr[1]:
                        g._push(ev)

        if g.events:
            pkt = g.make_network_packet(pl_t)
            dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                     client_session_guid(h))
            h.scnt += 1
            h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
                    "target": "ServiceGameSession", "instance": str(session.server_id),
                    "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
            log_req(f"    PvP phase {phase} start pushed to pid {pid}")
    if phase == 8:
        # Prep updated the resource pool — persist it.
        pvp_save_state(session, state)
    _pvp_log_stack(state, f"phase-{phase}")
    # The PRIORITY player holds priority in this phase (the greenlight above).
    # At DeclareDefense the defender is the priority player; elsewhere it's the
    # turn player (mirrors the greenlight/phase push at the top of this loop).
    if phase not in (3, 4):
        if phase == _ge.ETurnPhases.DeclareDefense:
            state["priority_pid"] = pids[1] if state.get("turn_pid") == pids[0] \
                else pids[0]
        else:
            state["priority_pid"] = state.get("turn_pid")
        pvp_save_state(session, state)
    # Push the phase-appropriate options for the priority holder (mirrors PvE
    # _push_phase_options): main phases get the full playable list, DeclareAttack
    # the attack options, DeclareDefense the blocker options; every OTHER stop
    # phase gets hand QuickActions + champion powers so instant-speed responses
    # are possible in any priority window.
    if state.get("stack"):
        # A draw trigger created a real chain item during phase start.  The
        # normal phase-9 priority/options path would leave the client in a
        # normal pass window even though the trigger is waiting to resolve.
        pvp_push_phase_options(session, state, pid=state.get("priority_pid"))
    elif phase in (_ge.ETurnPhases.FirstMainPhase,
                 _ge.ETurnPhases.SecondMainPhase):
        pvp_push_main_phase_options(session, state)
    elif phase == _ge.ETurnPhases.DeclareAttack:
        pvp_push_attack_options(session, state)
    elif phase == _ge.ETurnPhases.DeclareDefense:
        pvp_push_blocker_options(session, state)
    elif phase not in (3, 4, 5, 6, 7, 8, 9):
        # Non-main stop phase (combat priority windows, AssignDamage steps,
        # Discard, EndTurn...): the priority player may cast QuickActions and
        # activate champion powers.
        pvp_push_phase_options(session, state)


def pvp_push_attack_options(session, state):
    """Push a PlayerOptionList marking the turn player's READY warzone troops
    as attackable (ECardUsage.Attack) during DeclareAttack, mirroring PvE
    _push_attack_options: a troop may attack if it has StartedATurnOnYourSide
    (survived to this turn, i.e. not summoning sick) OR Speed, is not tapped,
    not already attacking, and lacks CantAttack."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    turn_pid = state.get("turn_pid")
    h = player_handlers.get(turn_pid)
    if not h:
        return
    opp_pid = pids[0] if pids[1] == turn_pid else pids[1]
    pl_t = _ge.UID.make(244, turn_pid)
    opp_t = _ge.UID.make(244, opp_pid)
    ready = []
    rows = _db.execute(
        "SELECT gc.card_uid, gc.card_state, gc.card_type, "
        "(ct.attributes | gc.card_attributes | "
        "COALESCE(gc.temporary_attributes, 0)) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%'",
        (session.session_id, turn_pid)).fetchall()
    for uid, cstate, _card_type, attrs in rows:
        cstate = cstate or 0
        attrs = attrs or 0
        if (((cstate & _ge.ECardStates.StartedATurnOnYourSide)
             or (attrs & _ge.ECardAttributes.Speed))
                and not (cstate & _ge.ECardStates.Tapped)
                and not (cstate & _ge.ECardStates.Attacking)
                and not (attrs & (_ge.ECardAttributes.CantAttack |
                                  _ge.ECardAttributes.Defensive))):
            ready.append(_ge.SessionCardId(_ge.UID(int(uid))))
    # Server-authoritative "Must attack": every ready warzone troop with
    # ForceAttack is declared as an attacker NOW (mirrors PvE
    # _auto_declare_force_attackers) — the player cannot forget/refuse to
    # attack with them.  Idempotent: already-attacking/committed troops are
    # skipped, so re-pushing the options never double-declares.
    champ_map = state.get("champ_map") or {}
    my_champ = int(champ_map.get(str(turn_pid), 0))
    attackers = {int(k): int(v) for k, v in (state.get("attackers") or {}).items()}
    forced = []
    from db import db_card_set_attacking_state, db_card_state_raw
    for uid, cstate, _card_type, attrs in rows:
        cstate = cstate or 0
        attrs = attrs or 0
        if not (attrs & _ge.ECardAttributes.ForceAttack):
            continue
        if (cstate & (_ge.ECardStates.Attacking | _ge.ECardStates.Tapped)):
            continue
        if not (cstate & _ge.ECardStates.StartedATurnOnYourSide) \
                and not (attrs & _ge.ECardAttributes.Speed):
            continue
        if attrs & (_ge.ECardAttributes.CantAttack |
                    _ge.ECardAttributes.Defensive):
            continue
        u = int(uid)
        if u in attackers:
            continue
        attackers[u] = my_champ
        state_bits = (_ge.ECardStates.Attacking |
                      _ge.ECardStates.HasAttacked)
        if not (attrs & _ge.ECardAttributes.Steadfast):
            state_bits |= _ge.ECardStates.Tapped
        db_card_set_attacking_state(session.session_id, u, state_bits)
        forced.append((u, state_bits))
    _db.commit()
    if forced:
        state["attackers"] = {str(k): str(v) for k, v in attackers.items()}
        pvp_save_state(session, state)
    g = _ge.Game(int(session.session_id), pl_t, opp_t)
    _pvp_populate_game_state(g, state, turn_pid, opp_pid)
    g.player_health = int(state.get(f"hp_{turn_pid}", 20))
    g.ai_health = int(state.get(f"hp_{opp_pid}", 20))
    ev = g._make_event(_ge.PlayerOptionListSessionEventArgs)
    ev.player_id = pl_t
    for scid in ready:
        opt = g._make_event(_ge.PlayerOptionSessionEventArgs)
        opt.card = scid
        opt.state = _ge.ECardUsage.Attack
        opt.instances = []
        ev.options.append(opt)
    g._push(ev)
    # AttackDeclared + CardUpdated(state) + triggers + CombatListing for the
    # auto-declared ForceAttack troops — mirrored from PvE, on the SAME
    # objective stream so BOTH clients see the forced attack.
    combats = []
    for i, (u, state_bits) in enumerate(forced):
        scid = _ge.SessionCardId(_ge.UID(u))
        cid = _ge.CombatId(pl_t, i + 1)
        g.push_attack_declared(cid, pl_t,
                               _ge.SessionCardId(_ge.UID(my_champ)) if my_champ
                               else _ge.SessionCardId(opp_t), scid)
        trow = _db.execute(
            "SELECT template_guid FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, u)).fetchone()
        tpl_guid = trow[0] if trow else None
        h_card = h  # the turn player's handler
        if tpl_guid:
            h_card._card_full_data(g, scid, tpl_guid)
        pushed_state = db_card_state_raw(session.session_id, u) or state_bits
        g.push_card_updated(scid, pl_t, _ge.ECardCollections.Warzone,
                            _ge.ECardTypes.Troop, template_id=tpl_guid,
                            state=pushed_state)
        cs = _ge.CombatSessionEventArgs()
        cs.player_id = pl_t
        cs.id = cid
        cs.attacker = scid
        cs.blockers = []
        combats.append(cs)
        from abilities.framework.triggers import resolve_triggers
        view = _pvp_fra_view(state, turn_pid, opp_pid)
        resolve_triggers(_db, h_card, g, session, pl_t, opp_t, view,
                         "CardAttackedEvent", int(u), turn_pid)
        resolve_triggers(_db, h_card, g, session, pl_t, opp_t, view,
                         "CardAttackedOrBlockedEvent", int(u), turn_pid)
        from abilities.framework.keywords.combat import apply_rage_keyword
        apply_rage_keyword(_db, session, h_card, g, pl_t, opp_t, view, int(u))
        if view.get("player_health") is not None:
            state[f"hp_{turn_pid}"] = int(view["player_health"])
        if view.get("ai_health") is not None:
            state[f"hp_{opp_pid}"] = int(view["ai_health"])
        pvp_save_state(session, state)
    if combats:
        g.push_combat_listing(pl_t, combats)
    g.push_player_updated(pl_t, champ_id=_ge.SessionCardId(
        _ge.UID(int(state.get("champ_map", {}).get(str(turn_pid), 0)))))
    g.push_player_updated(opp_t, champ_id=_ge.SessionCardId(
        _ge.UID(int(state.get("champ_map", {}).get(str(opp_pid), 0)))))
    if forced:
        _pvp_send_same_events(session, g, pl_t, opp_t)
    else:
        pkt = g.make_network_packet(pl_t)
        dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                 client_session_guid(h))
        h.scnt += 1
        h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
                "target": "ServiceGameSession", "instance": str(session.server_id),
                "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
    log_req(f"    PvP attack options pushed to {turn_pid}: "
            f"{len(ready)} ready troop(s), {len(forced)} forced attacker(s)")


def pvp_push_blocker_options(session, state):
    """Push a PlayerOptionList enabling the DEFENDER (the non-turn player) to
    declare blockers during DeclareDefense.  The client's
    BattleStateDeclareBlockers only lets a troop block when
    State.HasUsage(troop, ECardUsage.Defend) AND
    State.GetTargetsFor(troop, ResourceId.Blocking) lists the attackers —
    mirrors PvE _push_blocker_options (hconnect ~2375).  Returns the number of
    defender troops that can block this attack (0 => the defender has nothing
    to block with and the phase can auto-advance)."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    turn_pid = state.get("turn_pid")
    defender_pid = pids[0] if pids[1] == turn_pid else pids[1]
    h = player_handlers.get(defender_pid)
    if not h:
        return
    pl_t = _ge.UID.make(244, defender_pid)
    opp_t = _ge.UID.make(244, turn_pid)
    attackers = {int(k): int(v) for k, v in (state.get("attackers") or {}).items()}
    if not attackers:
        return
    attacker_scids = [_ge.SessionCardId(_ge.UID(int(u))) for u in attackers]
    attacker_attrs = {}
    for u in attackers:
        r = _db.execute(
            "SELECT (ct.attributes | gc.card_attributes | "
            "COALESCE(gc.temporary_attributes, 0)) FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session.session_id, int(u))).fetchone()
        attacker_attrs[int(u)] = r[0] if r else 0
    rows = _db.execute(
        "SELECT gc.card_uid, "
        "(ct.attributes | gc.card_attributes | "
        "COALESCE(gc.temporary_attributes, 0)) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND user_id=? AND location='warzone' "
        "AND gc.card_type LIKE '%Troop%' AND (gc.card_state & ?) = 0 "
        "AND (ct.attributes | gc.card_attributes | "
        "COALESCE(gc.temporary_attributes, 0)) & ? = 0",
        (session.session_id, defender_pid, _ge.ECardStates.Tapped,
         _ge.ECardAttributes.CantBlock)).fetchall()
    g = _ge.Game(int(session.session_id), pl_t, opp_t)
    _pvp_populate_game_state(g, state, defender_pid, turn_pid)
    # Carry live health — otherwise the PlayerUpdateds pushed at the end reset
    # both champions to the default 20 during DeclareDefense (the "health flicks
    # to 20 at Declare Blockers" bug).
    g.player_health = int(state.get(f"hp_{defender_pid}", 20))
    g.ai_health = int(state.get(f"hp_{turn_pid}", 20))
    g.player_resources = int(state.get(f"res_{defender_pid}", 0))
    g.ai_resources = int(state.get(f"res_{turn_pid}", 0))
    g.player_total_resources = int(state.get(f"res_total_{defender_pid}", 0))
    g.ai_total_resources = int(state.get(f"res_total_{turn_pid}", 0))
    g.player_charges = int(state.get(f"chg_{defender_pid}", 0))
    g.ai_charges = int(state.get(f"chg_{turn_pid}", 0))
    ev = g._make_event(_ge.PlayerOptionListSessionEventArgs)
    ev.player_id = pl_t
    blocking_id = _ge.ResourceId.from_str(
        "83659505-152d-4ddc-89df-7c29bdfba16d")
    blockable_count = 0
    for uid, battrs in rows:
        can_block_flyers = bool(
            int(battrs or 0) & (_ge.ECardAttributes.Flight |
                                _ge.ECardAttributes.SkyGuard))
        blockable = []
        for scid, u in zip(attacker_scids, attackers):
            if (int(attacker_attrs.get(int(u)) or 0)
                    & _ge.ECardAttributes.CantBeBlocked):
                continue
            if (attacker_attrs[int(u)] & _ge.ECardAttributes.Flight
                    and not can_block_flyers):
                continue
            blockable.append(scid)
        if not blockable:
            continue
        blockable_count += 1
        opt = g._make_event(_ge.PlayerOptionSessionEventArgs)
        opt.card = _ge.SessionCardId(_ge.UID(int(uid)))
        opt.state = _ge.ECardUsage.Defend
        inst = g._make_event(_ge.OptionInstanceSessionEventArgs)
        inst.opt_id = blocking_id
        inst.min_target_counts.append(0)
        inst.max_target_counts.append(len(blockable))
        inst.target_ids.append(blocking_id)
        tgt = g._make_event(_ge.TargetInstanceSessionEventArgs)
        tgt.target_index = 0
        tgt.target_id = blocking_id
        tgt.targets = list(blockable)
        inst.target_instances.append(tgt)
        opt.instances.append(inst)
        ev.options.append(opt)
    g._push(ev)
    g.push_player_updated(pl_t, champ_id=_ge.SessionCardId(
        _ge.UID(int(state.get("champ_map", {}).get(str(defender_pid), 0)))))
    g.push_player_updated(opp_t, champ_id=_ge.SessionCardId(
        _ge.UID(int(state.get("champ_map", {}).get(str(turn_pid), 0)))))
    pkt = g.make_network_packet(pl_t)
    dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                             client_session_guid(h))
    h.scnt += 1
    h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
            "target": "ServiceGameSession", "instance": str(session.server_id),
            "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
    log_req(f"    PvP blocker options pushed to {defender_pid}: "
            f"{len(rows)} defender troop(s) for {len(attackers)} attacker(s), "
            f"{blockable_count} blockable")
    return blockable_count


def _pvp_defender_blockable_count(session, state):
    """Return how many of the defender's warzone troops can actually block at
    least one of the current attackers (mirrors the eligibility logic in
    pvp_push_blocker_options, without pushing any options — used to decide
    whether to auto-pass the defender through DeclareDefense)."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return 0
    turn_pid = state.get("turn_pid")
    defender_pid = pids[0] if pids[1] == turn_pid else pids[1]
    attackers = {int(k): int(v) for k, v in (state.get("attackers") or {}).items()}
    if not attackers:
        return 0
    attacker_scids = [_ge.SessionCardId(_ge.UID(int(u))) for u in attackers]
    attacker_attrs = {}
    for u in attackers:
        r = _db.execute(
            "SELECT (ct.attributes | gc.card_attributes | "
            "COALESCE(gc.temporary_attributes, 0)) FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session.session_id, int(u))).fetchone()
        attacker_attrs[int(u)] = r[0] if r else 0
    rows = _db.execute(
        "SELECT gc.card_uid, "
        "(ct.attributes | gc.card_attributes | "
        "COALESCE(gc.temporary_attributes, 0)) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND user_id=? AND location='warzone' "
        "AND gc.card_type LIKE '%Troop%' AND (gc.card_state & ?) = 0 "
        "AND (ct.attributes | gc.card_attributes | "
        "COALESCE(gc.temporary_attributes, 0)) & ? = 0",
        (session.session_id, defender_pid, _ge.ECardStates.Tapped,
         _ge.ECardAttributes.CantBlock)).fetchall()
    count = 0
    for uid, battrs in rows:
        can_block_flyers = bool(
            int(battrs or 0) & (_ge.ECardAttributes.Flight |
                                _ge.ECardAttributes.SkyGuard))
        for u in attackers:
            if (int(attacker_attrs.get(int(u)) or 0)
                    & _ge.ECardAttributes.CantBeBlocked):
                continue
            if (attacker_attrs[int(u)] & _ge.ECardAttributes.Flight
                    and not can_block_flyers):
                continue
            count += 1
            break  # this troop can block at least one attacker
    return count


def pvp_push_phase_options(session, state, pid=None):
    """Push a PlayerOptionList for a NON-main priority window: hand QuickActions
    (instant-speed — castable in ANY priority window) + champion charge powers,
    so the holding player can respond with quick actions mid-combat.  Mirrors
    PvE _push_phase_options_empty (hconnect ~2180)."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    turn_pid = pid or state.get("priority_pid") or state.get("turn_pid")
    h = player_handlers.get(turn_pid)
    if not h:
        return
    opp_pid = pids[0] if pids[1] == turn_pid else pids[1]
    pl_t = _ge.UID.make(244, turn_pid)
    opp_t = _ge.UID.make(244, opp_pid)
    resources = int(state.get(f"res_{turn_pid}", 0))
    threshold = dict(state.get(f"thresh_{turn_pid}") or {})
    from db import db_hand_quick_actions
    playable = []
    for cu, cost, ct_name, thresh_json, _ab in \
            db_hand_quick_actions(session.session_id, turn_pid):
        if (cost or 0) > resources:
            continue
        if not _pvp_thresholds_met(thresh_json, threshold):
            continue
        try:
            ability_guids = [x.lower() for x in json.loads(_ab or "[]")]
        except Exception:
            ability_guids = []
        trow = _db.execute(
            "SELECT template_guid FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(cu))).fetchone()
        if not trow or not _pvp_card_playable(
                session, state, int(cu), trow[0], ct_name, cost or 0,
                ability_guids, resources, threshold):
            continue
        playable.append(_ge.SessionCardId(_ge.UID(int(cu))))
    g = _ge.Game(int(session.session_id), pl_t, opp_t)
    _pvp_populate_game_state(g, state, turn_pid, opp_pid)
    # CardUpdated must use the current PvP state when dynamic/static card data
    # is rebuilt (not a previous turn or another session's cached view).
    h._current_bstate = state
    # Carry live health/resources — otherwise the PlayerUpdateds pushed below
    # reset both champions to the default 20 during combat priority windows
    # (DeclareCombat / response windows: the "health flicks to 20" bug).
    g.player_health = int(state.get(f"hp_{turn_pid}", 20))
    g.ai_health = int(state.get(f"hp_{opp_pid}", 20))
    g.player_resources = resources
    g.player_total_resources = int(state.get(f"res_total_{turn_pid}", 0))
    g.ai_resources = int(state.get(f"res_{opp_pid}", 0))
    g.ai_total_resources = int(state.get(f"res_total_{opp_pid}", 0))
    g.player_charges = int(state.get(f"chg_{turn_pid}", 0))
    g.ai_charges = int(state.get(f"chg_{opp_pid}", 0))
    g.push_options(pl_t, playable)
    # Response-window QuickActions use the same play-card targeting flow as
    # main-phase spells. Countermagic therefore receives a TargetInstance for
    # the current CastSpells chain card instead of being cast with no target.
    _pvp_add_play_target_options(g, session, state, pl_t, opp_t, turn_pid)
    # Manual troop abilities are legal in combat priority windows too.  The
    # main-phase path already adds these options, but combat used to send only
    # quick actions and champion powers, leaving cards such as Prairie Scout
    # unusable after attackers were declared.
    priority_pid = int(turn_pid)
    affordable = _pvp_affordable_troop_abilities(
        session, state, pid=priority_pid)
    if affordable:
        _pvp_add_troop_ability_options(
            g, session, state, pl_t, opp_t, priority_pid, affordable)
    _pvp_add_champion_options(g, session, state, turn_pid, pl_t)
    _pvp_add_hand_card_updates(g, session, state, turn_pid, pl_t)
    g.push_player_updated(pl_t, champ_id=_ge.SessionCardId(
        _ge.UID(int(state.get("champ_map", {}).get(str(turn_pid), 0)))))
    g.push_player_updated(opp_t, champ_id=_ge.SessionCardId(
        _ge.UID(int(state.get("champ_map", {}).get(str(opp_pid), 0)))))
    pkt = g.make_network_packet(pl_t)
    dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                             client_session_guid(h))
    h.scnt += 1
    h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
            "target": "ServiceGameSession", "instance": str(session.server_id),
            "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
    log_req(f"    PvP phase options pushed to {turn_pid}: "
            f"{len(playable)} quick action(s), {resources} resources")


def _pvp_add_hand_card_updates(g, session, state, pid, player_uid):
    """Refresh the priority player's private hand card representations.

    Dynamic all-zone modifiers (notably Pterobot) change when Dwarves or
    Robots enter/leave the warzone.  The client only changes the displayed
    cost after a CardUpdated, so rebuilding PlayerOptionList alone leaves a
    stale hand cost even when server affordability is already correct.
    """
    h = player_handlers.get(int(pid))
    if h is None or not hasattr(h, "_card_full_data"):
        return
    from db import db_game_get_hand
    h._current_bstate = state
    for card_uid, template_guid in db_game_get_hand(
            session.session_id, int(pid)):
        scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
        try:
            _tpl, ct, name, cost, attack, defense, gem = h._card_full_data(
                g, scid, template_guid)
        except Exception:
            continue
        g.push_card_updated(
            scid, player_uid, _ge.ECardCollections.Hand, ct,
            template_id=template_guid, cost=cost, attack=attack,
            defense=defense, gems=gem, card_name=name)


def pvp_push_warzone_updates(session, state, game=None):
    """Re-push CardUpdateds for ALL warzone cards (both players) so the
    client's card icons/abilities/state always match the DB — mirrors PvE
    _push_warzone_card_updates.  When `game` is given the updates are appended
    to that Game's event stream (to be sent with it); otherwise a fresh stream
    is sent to both players."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    g = game
    if g is None:
        g = _ge.Game(int(session.session_id),
                     _ge.UID.make(244, pids[0]),
                     _ge.UID.make(244, pids[1]))
    wz_handler = player_handlers.get(pids[0]) or player_handlers.get(pids[1])
    if wz_handler is not None:
        wz_handler._current_bstate = state
    rows = _db.execute(
        "SELECT card_uid, template_guid, user_id, card_state, card_type "
        "FROM game_cards "
        "WHERE session_id=? AND location='warzone'",
        (session.session_id,)).fetchall()
    for card_uid, tpl_guid, user_id, cstate, db_ct in rows:
        scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
        if wz_handler:
            wz_handler._card_full_data(g, scid, tpl_guid)
        cdef = g.card_defs.get(scid)
        attrs = cdef.attributes if cdef else 0
        owner = _ge.UID.make(244, user_id)
        ct = _ge.card_type_from_db(db_ct)
        g.push_card_updated(scid, owner, _ge.ECardCollections.Warzone,
                            ct, template_id=tpl_guid,
                            attributes=attrs, state=int(cstate or 0))
    if game is None and g.events:
        _pvp_send_same_events(session, g,
                              _ge.UID.make(244, pids[0]),
                              _ge.UID.make(244, pids[1]))
        log_req(f"    PvP warzone re-pushed: {len(rows)} troop(s)")


def pvp_turn_has_attackers(session, turn_pid):
    """True if the turn player controls a warzone troop ELIGIBLE to attack:
    a troop, untapped, no Can't-Attack attribute, and not summoning sick
    (StartedATurnOnYourSide OR Speed/haste).  Mirrors ai.player_can_attack_troops
    and drives whether the turn enters the combat phase list — with no eligible
    attackers the combat steps are skipped entirely (FirstMain -> SecondMain),
    exactly like the PvE turn."""
    from ai import player_can_attack_troops
    # The handler argument is unused by the eligibility query (only user_id).
    return player_can_attack_troops(None, session, turn_pid)


def pvp_phase_is_stop(state, phase, turn_pid, opp_pid):
    """True if EITHER player wants to stop at `phase` during the turn player's
    turn: the turn player's self-stops (their own turn) OR the opponent's
    opponent-stops (the opponent's turn), falling back to the client defaults
    when a player hasn't configured stops.  Mirrors battle_engine.is_self_stop
    / is_opp_stop."""
    import battle_engine as _be
    self_stops = set(_be.SELF_ALWAYS_STOPS)
    self_stops.update(state.get(f"stops_self_{turn_pid}") or _be.SELF_DEFAULT_STOPS)
    opp_stops = set(_be.OPP_ALWAYS_STOPS)
    opp_stops.update(state.get(f"stops_opp_{opp_pid}") or _be.OPP_DEFAULT_STOPS)
    # SetAutoPass is a client-side request to keep passing through this
    # player's configured stops.  It must not remove the other player's
    # stops: the other client still needs to receive priority at its own
    # configured window.  Mandatory opponent stops remain mandatory unless
    # the player who would receive them is the one auto-passing.
    if pvp_player_auto_passes(state, turn_pid):
        self_stops = set(_be.SELF_ALWAYS_STOPS)
    if pvp_player_auto_passes(state, opp_pid):
        opp_stops = set(_be.OPP_ALWAYS_STOPS)
    return phase in self_stops or phase in opp_stops


def pvp_player_auto_passes(state, pid):
    """Whether *pid* has enabled the client's F10 auto-pass mode."""
    try:
        return int(state.get("autopass_pid", 0)) == int(pid)
    except (TypeError, ValueError):
        return False


def _pvp_auto_pass_chain_priority(session, state, pid):
    """Consume a chain response for an F10-enabled player.

    The client normally submits the follow-up PassPriority itself.  PvP can
    hand a GreenLight to an auto-passing client while it is rebuilding its
    chain/priority UI, leaving the opponent's card waiting for a manual
    Resolve click.  The server owns the authoritative two-pass state, so
    consume this response here and use the normal pass route.
    """
    if (not state.get("stack") or
            not pvp_player_auto_passes(state, pid) or
            int(state.get("autopass_state", 2) or 2) != 2):
        return False
    if int(state.get("priority_pid") or 0) != int(pid):
        return False
    h = player_handlers.get(int(pid))
    if h is None:
        return False
    log_req(f"    PvP F10: server auto-passing chain priority for {pid}")
    route_pvp_pass(h, session)
    return True


@_pvp_locked
def set_pvp_auto_pass(handler, session, passing_state=2):
    """Enable F10 auto-pass for one PvP client and consume its current pass.

    The client will send the subsequent passes itself whenever a GreenLight is
    handed back to it, including ResolveTopOfChain.  Record which player's
    configured stops should be ignored, then consume the current pass
    immediately so the server can advance through the current stop too.
    """
    pid = int(handler.client_reck_id)
    state = pvp_load_state(session)
    if not state:
        return False
    state["autopass_pid"] = pid
    state["autopass_state"] = int(passing_state or 2)
    pvp_save_state(session, state)
    current_priority = state.get("priority_pid")
    route_pvp_pass(handler, session)
    log_req(f"    PvP SetAutoPass: pid={pid} state={passing_state} "
            f"priority_was={current_priority}")
    return True


def _pvp_auto_pass_opponent_stop(session, state, turn_pid, opp_pid):
    """Hand an opponent-only configured stop to the opponent during F10.

    The normal phase walker pushes priority to the active player first.  When
    that player is auto-passing, an opponent stop must consume the active
    player's pass before the opponent can receive priority; otherwise the
    walker appears to stop on the active player's screen (notably at Second
    Main, which is in the opponent defaults too).
    """
    import battle_engine as _be
    phase = int(state.get("phase", 0))
    if not pvp_player_auto_passes(state, turn_pid):
        return False
    if phase in _be.SELF_ALWAYS_STOPS or phase in _be.OPP_ALWAYS_STOPS:
        return False
    opp_stops = set(state.get(f"stops_opp_{opp_pid}")
                    or _be.OPP_DEFAULT_STOPS)
    if phase not in opp_stops:
        return False
    h = player_handlers.get(turn_pid)
    if not h:
        return False
    route_pvp_pass(h, session)
    log_req(f"    PvP F10: passed active priority at opponent stop "
            f"phase {phase} to {opp_pid}")
    return True


def pvp_advance_past_non_stops(session, state):
    """Auto-advance the PvP phase one step at a time (pushing each phase and
    running its start-of-phase logic — Prep resource, Draw) until the next
    phase that is a STOP for either player, at which point the both-pass
    cycle takes over.

    Non-interactive phases like Ready/Prep/Draw are marched through
    server-side (neither client auto-passes them and the opponent can't pass
    without a priority handoff), but a phase is NEVER auto-passed if the turn
    player has a self-stop on it or the opponent has an opponent-stop on it —
    the player's configured stops are respected.  Returns True if it advanced
    at least one phase."""
    import battle_engine as _be
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    turn_pid = state.get("turn_pid")
    opp_pid = pids[0] if pids[1] == turn_pid else pids[1]
    if _pvp_auto_pass_opponent_stop(session, state, turn_pid, opp_pid):
        return True
    # The current phase was just entered by the caller.  If either player has
    # a stop on it, don't auto-advance — let the both-pass cycle handle it.
    if pvp_phase_is_stop(state, int(state.get("phase", 6)), turn_pid, opp_pid):
        log_req(f"    PvP auto-advance: phase {state.get('phase')} is a "
                f"player stop — not auto-passing")
        return False
    # Combat steps only when the turn player controls a troop ELIGIBLE to
    # attack (untapped, not summoning sick) — mirrors the PvE build_turn_phases
    # / ai.player_can_attack_troops.  With no eligible attackers the turn skips
    # DeclareAttack/DeclareDefense/AssignDamage entirely (FirstMain -> SecondMain).
    has_ready = pvp_turn_has_attackers(session, turn_pid)
    phase_list = (_be.COMBAT_TURN_PHASES if has_ready
                  else _be.BASE_TURN_PHASES)
    try:
        cur = phase_list.index(int(state.get("phase", 6)))
    except ValueError:
        cur = 0
    advanced = False
    while True:
        cur += 1
        if cur >= len(phase_list):
            log_req("    PvP advance: reached the end of the phase list")
            return advanced
        new_phase = phase_list[cur]
        state["phase"] = new_phase
        state["passes"] = []
        pvp_save_state(session, state)
        # _pvp_run_phase_start pushes the TurnPhase + GreenLight to both in one
        # packet each (greenlight first, so the client never sees the phase
        # without priority).
        log_req(f"    PvP auto-advance: phase {new_phase} to both")
        _pvp_run_phase_start(session, state, new_phase)
        advanced = True
        if _pvp_auto_pass_opponent_stop(session, state, turn_pid, opp_pid):
            return advanced
        if pvp_phase_is_stop(state, new_phase, turn_pid, opp_pid):
            log_req(f"    PvP auto-advance: stopped at phase {new_phase} "
                    f"(player stop)")
            return advanced
        # Discard (21): stop only when the turn player's hand exceeds the max
        # hand size (7) — mirror PvE: hand fits -> auto-advance.
        if new_phase == _ge.ETurnPhases.Discard:
            hc = _db.execute(
                "SELECT COUNT(*) FROM game_cards WHERE session_id=? "
                "AND user_id=? AND location='hand'",
                (session.session_id, turn_pid)).fetchone()
            if int(hc[0] or 0) > 7:
                log_req(f"    PvP auto-advance: stopped at Discard "
                        f"(hand {hc[0]} > 7)")
                return advanced


def _pvp_thresholds_met(thresh_json, player_threshold):
    """Check the PvP player's threshold counts (state thresh_<pid>, a dict of
    shard-flag -> count) against a card's threshold_json requirement
    ({"list":[2,2]} = TWO Ruby, indices 0=Colorless 1=Blood 2=Ruby 3=Sapphire
    4=Wild 5=Diamond -> flags 0/4/8/16/32/64).  Mirrors the PvE
    _thresholds_met (hconnect_server) using the PvP state's threshold dict."""
    if not thresh_json:
        return True
    try:
        import json as _j
        req = _j.loads(thresh_json)
        req_list = req.get("list", []) if isinstance(req, dict) else []
        if not req_list:
            return True
        shard_fmt = {0: 0, 1: 4, 2: 8, 3: 16, 4: 32, 5: 64}
        need = {}
        for s in req_list:
            flag = shard_fmt.get(int(s), int(s))
            need[flag] = need.get(flag, 0) + 1
        for flag, count in need.items():
            val = player_threshold.get(flag)
            if val is None:
                val = player_threshold.get(str(flag), 0)
            if int(val or 0) < count:
                return False
        return True
    except Exception:
        return True


def _pvp_card_playable(session, state, card_uid, tpl_guid, ct_name, cost,
                       ability_guids, resources, threshold):
    """A PvP hand card is playable iff affordable + thresholds met + every
    explicit target template of its non-manual abilities has a legal candidate
    (mirrors PvE _hand_card_playable + _card_target_requirements_met — makes
    Countermagic unplayable with nothing on the chain)."""
    import json as _js
    from db import db_ability_meta_targets
    from abilities.framework.targeting import legal_targets, ZONE_MAP
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return True
    turn_pid = state.get("priority_pid") or state.get("turn_pid")
    champ_map = state.get("champ_map") or {}
    champ_targets = []
    for cpid in (state.get("pids") or []):
        ccu = int(champ_map.get(str(cpid), 0))
        if ccu:
            champ_targets.append((ccu, cpid, "Champ",
                                  int(state.get(f"hp_{cpid}", 20))))
    for ag in (ability_guids or []):
        meta = db_ability_meta_targets(ag)
        if not meta or not meta[0]:
            continue
        if meta[4]:  # is_manual — a warzone activation, not a play cost
            continue
        try:
            tpl_ids = _js.loads(meta[0])
        except Exception:
            continue
        for tid in tpl_ids:
            tid = str(tid)
            trow = _db.execute(
                "SELECT filter_json, target_kind, is_auto_target, "
                "collection_flags, min_target_count FROM target_templates "
                "WHERE template_id=?", (tid,)).fetchone()
            if not trow:
                continue
            kind = trow[1] or ""
            auto = int(trow[2] or 0)
            flags = trow[3] or ""
            minc = int(trow[4] or 1)
            if auto or minc < 1:
                continue
            if kind == "PlayerTargetTemplate":
                continue
            if not flags or flags.strip().lower() in ("none", ""):
                continue
            zones = [ZONE_MAP.get(z, z.lower())
                     for z in flags.split("|") if z]
            if not zones:
                continue
            try:
                candidates = legal_targets(
                    _db, session.session_id, turn_pid, tid, 0,
                    both_players=True, champions=champ_targets)
            except Exception:
                continue
            if not candidates:
                log_req(f"    PvP options: {ct_name} not playable "
                        f"(target template {tid[:8]} zone {flags} "
                        f"has no legal target)")
                return False
    return True


def pvp_push_main_phase_options(session, state):
    """Push the PlayerOptionList (golden playable-card outlines) for the turn
    player at a main phase, so the client lets them click cards.  Affordability
    is computed from the PvP state's resource count (res_<pid>) and each card's
    template cost, plus THRESHOLD requirements (thresh_<pid>) — a card like
    Emberspire Witch (2 Ruby) is not highlighted until the player has the
    thresholds."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    turn_pid = state.get("turn_pid")
    h = player_handlers.get(turn_pid)
    if not h:
        return
    opp_pid = pids[0] if pids[1] == turn_pid else pids[1]
    pl_t = _ge.UID.make(244, turn_pid)
    opp_t = _ge.UID.make(244, opp_pid)
    resources = int(state.get(f"res_{turn_pid}", 0))
    threshold = dict(state.get(f"thresh_{turn_pid}") or {})
    resource_played = int(state.get(f"res_played_{turn_pid}", 0))
    from db import (db_game_get_hand, db_game_card_type, db_template_by_guid,
                    db_card_template_thresholds)
    playable = []
    for cu, tg in db_game_get_hand(session.session_id, turn_pid):
        scid = _ge.SessionCardId(_ge.UID(int(cu)))
        ct_name = db_game_card_type(tg)
        if str(ct_name or "").split("|", 1)[0] == "Resource":
            if not resource_played:
                playable.append(scid)
            continue
        t = db_template_by_guid(tg)
        cost = t[3] if t else 0
        # Effective cost (static/temporary cost modifiers, e.g. Fury of the
        # Mountain God's -1 per damage) — mirrors PvE effective_cost.
        try:
            from abilities.framework.statics import effective_cost as _ec
            cost = _ec(_db, session.session_id,
                       _pvp_fra_view(state, turn_pid, opp_pid), int(cu))
        except Exception:
            pass
        if cost > resources:
            continue
        srow = db_card_template_thresholds(tg)
        thresh_json = srow[0] if srow else None
        if not _pvp_thresholds_met(thresh_json, threshold):
            log_req(f"    PvP options: {ct_name} {cu} not playable "
                    f"(thresholds unmet: {thresh_json})")
            continue
        # Explicit target-template availability (Countermagic needs a
        # CastSpells target, etc.) — mirrors PvE _card_target_requirements_met.
        import json as _js
        ab_json = None
        trow_ab = _db.execute(
            "SELECT abilities_json FROM card_templates WHERE guid=?",
            (tg,)).fetchone()
        if trow_ab and trow_ab[0]:
            ab_json = trow_ab[0]
        ability_guids = []
        if ab_json:
            try:
                ability_guids = [x.lower() for x in _js.loads(ab_json)]
            except Exception:
                ability_guids = []
        if not _pvp_card_playable(session, state, int(cu), tg, ct_name,
                                  cost, ability_guids, resources, threshold):
            continue
        playable.append(scid)
    g = _ge.Game(int(session.session_id), pl_t, opp_t)
    h._current_bstate = state
    _pvp_add_hand_card_updates(g, session, state, turn_pid, pl_t)
    g.push_options(pl_t, playable)
    # Attach targeting TargetInstances to the playable cards so the client
    # opens the target picker for targeted spells (mirrors PvE
    # _add_play_target_options) — without this they fizzle with no target.
    _pvp_add_play_target_options(g, session, state, pl_t, opp_t, turn_pid)
    # Warzone-troop manual abilities (e.g. Shift): light up as ECardUsage.Activate.
    affordable = _pvp_affordable_troop_abilities(
        session, state, pid=turn_pid)
    if affordable:
        _pvp_add_troop_ability_options(g, session, state, pl_t, opp_t,
                                       turn_pid, affordable)
    # Champion charge/spell powers: add the champion card to the options so
    # the client's charge ability buttons light up (CanActivateAbility ->
    # State.CanUseAbility needs the champion in PlayerOptions.m_Targets).
    _pvp_add_champion_options(g, session, state, turn_pid, pl_t)
    # Re-push all warzone cards so attribute/state shifts render (mirrors PvE
    # _push_warzone_card_updates inside the options packet).
    pvp_push_warzone_updates(session, state, game=g)
    pkt = g.make_network_packet(pl_t)
    dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                             client_session_guid(h))
    h.scnt += 1
    h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
            "target": "ServiceGameSession", "instance": str(session.server_id),
            "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
    try:
        offered_uids = {
            int(getattr(card.uid, "uid64", card.uid)) for card in playable
        }
        offered_names = []
        for _cu, _tg in db_game_get_hand(session.session_id, turn_pid):
            if int(_cu) not in offered_uids:
                continue
            _name_row = _db.execute(
                "SELECT name FROM card_templates WHERE guid=?", (_tg,)
            ).fetchone()
            offered_names.append(_name_row[0] if _name_row else _tg)
    except Exception:
        offered_names = []
    log_req(f"    PvP main-phase options pushed to {turn_pid} "
            f"({len(playable)} playable, {resources} resources; "
            f"offered={offered_names})")


def _pvp_add_champion_options(g, session, state, pid, pl_t):
    """Append the pid's champion to the most recent PlayerOptionList with its
    AFFORDABLE charge/spell abilities, so the client's champion ability buttons
    appear and light up (CanActivateAbility -> State.CanUseAbility requires the
    champion in PlayerOptions.m_Targets).  Affordability mirrors PvE
    _filter_affordable_abilities but reads charges/thresholds from the PvP
    state (chg_<pid> / thresh_<pid>) instead of a battle_engine bstate."""
    champ_map = state.get("champ_map") or {}
    cu = int(champ_map.get(str(pid), 0))
    if not cu:
        return
    champ_scid = _ge.SessionCardId(_ge.UID(cu))
    crow = _db.execute(
        "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, cu)).fetchone()
    if not crow:
        return
    tpl_guid = crow[0]
    from db import db_champion_ability_guids, db_champion_ability_costs, \
        db_champion_ability_thresholds, db_talent_ability_costs
    all_guids = db_champion_ability_guids(tpl_guid)
    if not all_guids:
        return
    charges = int(state.get(f"chg_{pid}", 0))
    spell_points = int(state.get(f"sp_{pid}", 0))
    spell_uses = dict(state.get(f"sp_uses_{pid}") or {})
    threshold = dict(state.get(f"thresh_{pid}") or {})
    phase = int(state.get("phase", 0))
    # The champion CardDef's abilities list drives the HUD buttons: it must
    # contain EVERY charge/spell power so the player always sees what they need
    # (e.g. "2 Diamond" / "2 Ruby"), greyed out until affordable.  Only the
    # AFFORDABLE abilities are offered as activatable options below.
    afford = []
    all_rids = []
    for ag in all_guids:
        row = db_champion_ability_costs(str(ag))
        if row is None:
            row = db_talent_ability_costs(str(ag))
        all_rids.append(_ge.ResourceId.from_str(ag))
        # Keep unknown abilities in CardDef so the HUD can display them, but
        # never make an ability with missing cost metadata playable.
        if row is None:
            continue
        cc = int(row[0] or 0)
        sc = int(row[1] or 0)
        effective_sc = (sc + int(spell_uses.get(str(ag), 0) or 0)
                        if sc else 0)
        activatable_phases = int(row[2] or 0) if len(row) > 2 else 0
        casting = int(row[3] or 0) if len(row) > 3 else 64
        if charges < cc or spell_points < effective_sc:
            continue
        # BasicAction powers require the controller's own turn.  The phase
        # bitmask comes from gamedata (do not hardcode First/Second Main).
        if casting != 64 and state.get("turn_pid") != pid:
            continue
        if activatable_phases and not (activatable_phases & (1 << phase)):
            continue
        reqs = db_champion_ability_thresholds(str(ag))
        if reqs:
            from game_engine import SHARD_TO_FLAG
            ok = True
            for color, qty in reqs:
                flag = SHARD_TO_FLAG.get(str(color).lower(), 0)
                if flag:
                    # threshold dict keys are STRINGS after the JSON round-trip
                    # (thresh_<pid> in the persisted state) — check both the int
                    # and string forms, else the lookup always returns 0.
                    _tv = threshold.get(flag)
                    if _tv is None:
                        _tv = threshold.get(str(flag), 0)
                    if int(_tv or 0) < qty:
                        ok = False
                        break
            if not ok:
                continue
        afford.append(_ge.ResourceId.from_str(ag))
    # All-Abilities champion CardDef is pushed EVERY time so the HUD shows the
    # charge powers (greyed when unaffordable).  Only `afford` is placed in
    # PlayerOptionList: those instances are what the client treats as playable.
    from db import db_is_champion_template
    if db_is_champion_template(tpl_guid):
        hp = int(state.get(f"hp_{pid}", 20))
        g.card_defs[champ_scid] = _ge.CardDef(
            "Champion", _ge.ECardTypes.Champion, 0, hp, hp, [], list(all_rids))
    # Target data: for champion abilities with explicit targets (e.g. Dimmid's
    # "Target troop gets Lifedrain") attach legal TargetInstances so the
    # client's target picker shows candidates — mirrors PvE
    # _champion_ability_targets.  Without this CanUseAbility is false and the
    # button is dead.
    import json as _js
    from abilities.framework.targeting import legal_targets as _lt
    champ_map = state.get("champ_map") or {}
    champ_targets = []
    for cpid in (state.get("pids") or []):
        ccu = int(champ_map.get(str(cpid), 0))
        if ccu:
            champ_targets.append((ccu, cpid, "Champ",
                                  int(state.get(f"hp_{cpid}", 20))))
    target_data = {}
    for rid in all_rids:
        ag = str(rid.guid)
        row = _db.execute(
            "SELECT target_template_ids FROM champion_abilities "
            "WHERE ability_guid=?", (ag,)).fetchone()
        if not row or not row[0]:
            continue
        try:
            tpls = _js.loads(row[0])
        except Exception:
            continue
        entries = []
        for tid in tpls:
            tid = str(tid)
            trow = _db.execute(
                "SELECT target_kind, is_auto_target, min_target_count, "
                "max_target_count FROM target_templates WHERE template_id=?",
                (tid,)).fetchone()
            if not trow:
                continue
            kind = trow[0] or ""
            auto = int(trow[1] or 0)
            if auto or kind == "PlayerTargetTemplate":
                continue
            try:
                cands = _lt(_db, session.session_id, pid, tid, int(cu),
                            both_players=False, champions=champ_targets)
            except Exception:
                cands = []
            if not cands:
                # Fall back to the controller's warzone troops so the picker
                # always has a pool.
                cands = [r[0] for r in _db.execute(
                    "SELECT card_uid FROM game_cards WHERE session_id=? "
                    "AND user_id=? AND location='warzone' ORDER BY position",
                    (session.session_id, pid)).fetchall()]
            entries.append((tid, cands,
                            int(trow[2] or 1), int(trow[3] or 1)))
        if entries:
            target_data[ag] = entries
    g.add_champion_to_options(pl_t, champ_scid, afford,
                              target_data=target_data or None)
    # Push the champion CardUpdated AFTER the options are added (the client's
    # add_champion_to_options needs a PlayerOptionList as the last event), so
    # State.Cards[champ].Abilities carries the charge powers and the HUD
    # buttons render.
    if db_is_champion_template(tpl_guid):
        hp = int(state.get(f"hp_{pid}", 20))
        g.push_card_updated(
            champ_scid, _ge.UID.make(244, pid),
            _ge.ECardCollections.Champions, _ge.ECardTypes.Champion,
            template_id=tpl_guid, defense=hp)
    log_req(f"    PvP champion options added for {pid}: "
            f"{[str(a.guid)[:8] for a in all_rids]} (charges {charges}, "
            f"affordable {len(afford)})")


def _pvp_add_play_target_options(g, session, state, pl_t, opp_t, turn_pid):
    """Attach targeting TargetInstances to the most recent PlayerOptionList so
    the client opens the target picker for played spells — mirrors PvE
    _add_play_target_options.  Without this a targeted spell (e.g. Bravery
    "+1/+1 target troop") is played with no target and no effect."""
    import json as _js
    if not g.events:
        return
    # CardUpdated/PlayerUpdated events may be appended while the packet is
    # being assembled.  The option list is the owning event; relying on the
    # final event silently drops manual abilities on those refresh paths.
    last_ev = next(
        (event for event in reversed(g.events)
         if isinstance(event, _ge.PlayerOptionListSessionEventArgs)), None)
    if last_ev is None:
        return
    from db import db_get_card_abilities, db_ability_meta_targets, \
        db_card_template_field, db_target_template_text, db_card_template_thresholds
    from abilities.framework.targeting import legal_targets as _lt
    champ_map = state.get("champ_map") or {}
    champ_targets = []
    for cpid in (state.get("pids") or []):
        cu = int(champ_map.get(str(cpid), 0))
        if cu:
            champ_targets.append((cu, cpid, "Champ",
                                  int(state.get(f"hp_{cpid}", 20))))
    for opt in last_ev.options:
        card_uid = int(opt.card.uid.uid64)
        row = _db.execute(
            "SELECT template_guid FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, card_uid)).fetchone()
        if not row:
            continue
        ab_json, _attrs = db_get_card_abilities(row[0])
        ab = []
        if ab_json:
            try:
                ab = [x.lower() for x in _js.loads(ab_json)]
            except Exception:
                pass
        for ag in ab:
            meta = db_ability_meta_targets(ag)
            if not meta or not meta[0]:
                continue
            if meta[4]:  # is_manual — hand cards don't activate manual abilities
                continue
            try:
                tpl_ids = _js.loads(meta[0])
            except Exception:
                continue
            for i, tid in enumerate(tpl_ids):
                tid = str(tid)
                trow = _db.execute(
                    "SELECT filter_json, target_kind, is_auto_target "
                    "FROM target_templates WHERE template_id=?",
                    (tid,)).fetchone()
                if not trow:
                    continue
                kind = trow[1] or ""
                auto = int(trow[2] or 0)
                if auto:
                    continue
                targets = []
                if kind == "PlayerTargetTemplate":
                    cu = int(champ_map.get(str(turn_pid), 0))
                    targets = [_ge.SessionCardId(_ge.UID(cu))] if cu else []
                else:
                    fj = trow[0] or "{}"
                    if not fj or fj.strip() == "{}":
                        continue
                    try:
                        cands = _lt(_db, session.session_id, turn_pid, tid, 0,
                                    both_players=True, champions=champ_targets)
                        targets = [_ge.SessionCardId(_ge.UID(int(u)))
                                   for u in cands]
                    except Exception:
                        targets = []
                if not targets:
                    continue
                inst = g._make_event(_ge.OptionInstanceSessionEventArgs)
                inst.opt_id = _ge.ResourceId.from_str(ag)
                inst.min_target_counts.append(1)
                inst.max_target_counts.append(1)
                inst.target_ids.append(_ge.ResourceId.from_str(tid))
                tgt = g._make_event(_ge.TargetInstanceSessionEventArgs)
                tgt.target_index = i
                tgt.target_id = _ge.ResourceId.from_str(tid)
                tgt.targets = list(targets)
                inst.target_instances.append(tgt)
                opt.instances.append(inst)
                # Also attach the picker to the PlayCard option so the client's
                # CanUseAbility finds the target on the built-in PlayCard
                # ability (the play-card flow keys on PlayCardAbilityTemplateId).
                for inst2 in opt.instances:
                    if str(inst2.opt_id.guid) == _ge.PLAY_CARD_ABILITY_TEMPLATE_ID:
                        inst2.target_ids.append(_ge.ResourceId.from_str(tid))
                        inst2.min_target_counts.append(1)
                        inst2.max_target_counts.append(1)
                        tgt2 = g._make_event(_ge.TargetInstanceSessionEventArgs)
                        tgt2.target_index = len(inst2.target_instances)
                        tgt2.target_id = _ge.ResourceId.from_str(tid)
                        tgt2.targets = list(targets)
                        inst2.target_instances.append(tgt2)
                        break
        # Variable X cost: attach an XCost CostInstance to the PlayCard option
        # so the client's BattleStateAssignXCost pushes the X slider — only for
        # templates with variable_cost (mirrors PvE _template_has_x_cost).
        vc = _db.execute(
            "SELECT variable_cost FROM card_templates WHERE guid=?",
            (row[0],)).fetchone()
        if vc and int(vc[0] or 0) > 0:
            for inst in opt.instances:
                if str(inst.opt_id.guid) == _ge.PLAY_CARD_ABILITY_TEMPLATE_ID:
                    ci = g._make_event(_ge.CostInstanceSessionEventArgs)
                    ci.min = 0
                    ci.max = 0
                    ci.cost_type = 256  # EAbilityCostType.XCostAbilityCostType
                    ci.target_template_id = _ge.ResourceId.invalid()
                    ci.targets = []
                    inst.target_instances.append(ci)
                    break


def _pvp_affordable_troop_abilities(session, state, pid=None):
    """Return {(card_uid, tpl_guid): [ability_guid, ...]} for the priority
    player's
    warzone troops whose MANUAL abilities are activatable — mirrors PvE
    _affordable_troop_abilities (is_manual, phase gating, cost, uses limits,
    exhaust-as-cost, legal targets, ability condition)."""
    import json as _js
    from db import db_card_uses
    from abilities.framework.targeting import (
        legal_targets as _lt, target_uses_both_players as _both_players)
    from abilities.framework.condition_engine import (
        ConditionContext, trigger_condition_met)
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return {}
    turn_pid = int(state.get("turn_pid") or 0)
    ability_pid = int(pid if pid is not None else turn_pid)
    phase = int(state.get("phase", 0))
    resources = int(state.get(f"res_{ability_pid}", 0))
    champ_map = state.get("champ_map") or {}
    champ_targets = []
    for cpid in (state.get("pids") or []):
        ccu = int(champ_map.get(str(cpid), 0))
        if ccu:
            champ_targets.append((ccu, cpid, "Champ",
                                  int(state.get(f"hp_{cpid}", 20))))
    rows = _db.execute(
        "SELECT gc.card_uid, gc.template_guid, gc.card_state, "
        "(ct.attributes | gc.card_attributes), ct.card_type "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone'",
        (session.session_id, ability_pid)).fetchall()
    result = {}
    for card_uid, tpl_guid, card_state, attrs, card_type in rows:
        ab_row = _db.execute(
            "SELECT card_abilities FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(card_uid))).fetchone()
        ab_list = []
        if ab_row and ab_row[0]:
            try:
                ab_list = _js.loads(ab_row[0])
            except Exception:
                ab_list = []
        # An explicit empty instance list is meaningful: a ONE-SHOT ability
        # has been consumed and must not be restored from the canonical card
        # template.  Only repair genuinely missing legacy instance data.
        if ab_row is None or ab_row[0] is None:
            trow = _db.execute(
                "SELECT abilities_json FROM card_templates WHERE guid=?",
                (tpl_guid,)).fetchone()
            if trow and trow[0]:
                try:
                    ab_list = _js.loads(trow[0])
                except Exception:
                    ab_list = []
        if not ab_list:
            continue
        uses = db_card_uses(session.session_id, int(card_uid))
        affordable = []
        for ag in ab_list:
            ag = str(ag)
            m = _db.execute(
                "SELECT casting_behavior, is_manual, activation_cost, "
                "uses_per_game, uses_per_turn, exhausts_on_use "
                "FROM card_abilities_meta WHERE ability_guid=?", (ag,)).fetchone()
            if not m:
                continue
            casting, manual, cost, upg, upt, exh = m
            if not manual:
                continue
            raw_row = _db.execute(
                "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
                (ag,)).fetchone()
            if raw_row and raw_row[0]:
                try:
                    cond_ctx = ConditionContext(
                        _db, session, state, ability_source_uid=int(card_uid),
                        ability_source_owner_id=ability_pid)
                    if not trigger_condition_met(raw_row[0], cond_ctx):
                        continue
                except Exception:
                    pass
            trow2 = _db.execute(
                "SELECT target_template_ids FROM card_abilities_meta "
                "WHERE ability_guid=?", (ag,)).fetchone()
            tids = []
            if trow2 and trow2[0]:
                try:
                    tids = _js.loads(trow2[0])
                except Exception:
                    tids = []
            if tids:
                wants_attacking = False
                has_target = False
                import battle_engine as _be
                for tid in tids:
                    tid = str(tid)
                    tt = _db.execute(
                        "SELECT filter_json, target_kind, is_auto_target "
                        "FROM target_templates WHERE template_id=?",
                        (tid,)).fetchone()
                    if tt and tt[0] and "IsAttacking" in tt[0]:
                        wants_attacking = True
                    kind = (tt[1] if tt else "") or ""
                    auto = int(tt[2] or 0) if tt else 0
                    if auto or kind in ("PlayerTargetTemplate",
                                        "AbilitySourceCardTargetTemplate",
                                        "AbilityCreatedTargetTemplate"):
                        has_target = True
                        continue
                    cands = _lt(_db, session.session_id, ability_pid, tid,
                                int(card_uid),
                                both_players=_both_players(_db, tid),
                                champions=champ_targets, battle_state=state)
                    if cands:
                        has_target = True
                if wants_attacking and phase not in _be.COMBAT_STEPS:
                    continue
                if not has_target:
                    continue
            if exh:
                cstate = card_state or 0
                if cstate & _ge.ECardStates.Tapped:
                    continue
                # The client only applies summoning sickness to troops:
                # Card.HasSummoningSickness() => IsTroop() && ... .  A
                # non-creature artifact such as Hex Engine may therefore be
                # activated on the turn it enters play; it still cannot be
                # activated while tapped.
                is_troop = "Troop" in str(card_type or "").split("|")
                if (is_troop
                        and not (cstate & _ge.ECardStates.StartedATurnOnYourSide)
                        and not ((attrs or 0) & _ge.ECardAttributes.Speed)):
                    continue
            if casting != 64:
                if ability_pid != turn_pid or phase not in (
                                 _ge.ETurnPhases.FirstMainPhase,
                                 _ge.ETurnPhases.SecondMainPhase):
                    continue
            if (cost or 0) > resources:
                continue
            # Card costs such as Ingenuity Engine's "exhaust one or more
            # Dwarves and/or Robots" live in m_ExhaustTarget, not in the
            # ability's effect target list.  They must have a legal payment
            # target before the activation is offered.
            cost_targets = _pvp_ability_cost_targets(
                session, state, ability_pid, int(card_uid), ag, champ_targets)
            if cost_targets is None:
                continue
            used = int(uses.get(ag, 0))
            if upg and used >= upg:
                continue
            if upt and used >= upt:
                continue
            affordable.append(ag)
        if affordable:
            result[(int(card_uid), tpl_guid)] = affordable
    return result


_PVP_ABILITY_COST_FIELD_TYPES = {
    "m_VoidTarget": 16,
    "m_SacrificeTarget": 2,
    "m_ExhaustTarget": 1,
    "m_DiscardTarget": 8,
    "m_RevealTarget": 64,
    "m_PutIntoDeckTarget": 32,
    "m_PutIntoDeckTarget2": 32,
    "m_PutIntoHandTarget": 128,
    "m_ShuffleIntoDeckTarget": 4,
}


def _pvp_ability_cost_templates(ability_guid):
    """Read card-payment target templates from an ability's raw metadata."""
    row = _db.execute(
        "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
        (str(ability_guid).lower(),)).fetchone()
    if not row or not row[0]:
        return []
    raw = row[0]
    out = []
    for field, cost_type in _PVP_ABILITY_COST_FIELD_TYPES.items():
        match = re.search(
            rf'"{field}"\s*:\s*\{{[^}}]*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"',
            raw)
        if match:
            guid = match.group(1).lower()
            if guid != "00000000-0000-0000-0000-000000000000":
                out.append((guid, cost_type))
            continue
        match = re.search(rf'"{field}"\s*:\s*\[(.*?)\]', raw)
        if match:
            for guid in re.findall(
                    r'"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', match.group(1)):
                guid = guid.lower()
                if guid != "00000000-0000-0000-0000-000000000000":
                    out.append((guid, cost_type))
    return out


def _pvp_ability_cost_targets(session, state, pid, source_uid,
                              ability_guid, champ_targets):
    """Return legal cost cards, or None when a required cost is unpayable."""
    from abilities.framework.targeting import legal_targets as _lt
    costs = _pvp_ability_cost_templates(ability_guid)
    if not costs:
        return []
    out = []
    for tid, cost_type in costs:
        trow = _db.execute(
            "SELECT min_target_count, max_target_count, target_kind, "
            "is_auto_target "
            "FROM target_templates WHERE template_id=?", (tid,)).fetchone()
        minimum = int(trow[0] or 1) if trow else 1
        maximum = int(trow[1] or 1) if trow else 1
        target_kind = (trow[2] if trow else "") or ""
        is_auto = bool(int(trow[3] or 0)) if trow else False
        # Gamedata represents "sacrifice this" as an automatic source-card
        # target.  It is a payment target for the option contract, but the
        # client does not repeat the source UID in the submitted TargetMap.
        # Advertise the source as the sole legal candidate and let activation
        # satisfy this automatic payment without requiring it in the wire
        # transaction.
        if is_auto and target_kind == "AbilitySourceCardTargetTemplate":
            out.append((tid, cost_type, [int(source_uid)], minimum, maximum))
            continue
        candidates = _lt(
            _db, session.session_id, pid, tid, int(source_uid),
            both_players=False, champions=champ_targets,
            battle_state=state)
        if len(candidates) < minimum:
            return None
        # Gamedata uses Int32.MaxValue for an open-ended "one or more"
        # payment.  The client needs the effective maximum for this choice,
        # not an unbounded value that can leave its picker waiting forever.
        maximum = min(maximum, len(candidates))
        out.append((tid, cost_type, [int(uid) for uid in candidates],
                    minimum, maximum))
    return out


def _pvp_select_champion_activation_targets(session, state, pid, source_uid,
                                            ability_guid, selected_uids,
                                            champ_targets):
    """Split a champion activation's payment cards from its effect target."""
    import json as _json
    from abilities.framework.targeting import (
        legal_targets as _lt, target_uses_both_players)
    selected_uids = [int(uid) for uid in (selected_uids or [])]
    cost_targets = _pvp_ability_cost_targets(
        session, state, pid, source_uid, ability_guid, champ_targets)
    if cost_targets is None:
        return None
    used = set()
    sacrifices = []
    for tid, cost_type, candidates, minimum, maximum in cost_targets:
        trow = _db.execute(
            "SELECT target_kind, is_auto_target FROM target_templates "
            "WHERE template_id=?", (str(tid),)).fetchone()
        auto_source = bool(trow and int(trow[1] or 0)) and \
            (trow[0] or "") == "AbilitySourceCardTargetTemplate"
        available = ([int(source_uid)] if auto_source else
                     [uid for uid in selected_uids
                      if uid in {int(c) for c in candidates}
                      and uid not in used])
        if len(available) < int(minimum):
            return None
        chosen = available[:int(maximum)]
        used.update(chosen)
        if int(cost_type) == 2:
            sacrifices.extend(chosen)

    row = _db.execute(
        "SELECT target_template_ids FROM card_abilities_meta "
        "WHERE ability_guid=?", (ability_guid,)).fetchone()
    if not row or not row[0]:
        row = _db.execute(
            "SELECT target_template_ids FROM champion_abilities "
            "WHERE ability_guid=?", (ability_guid,)).fetchone()
    if not row or not row[0]:
        row = _db.execute(
            "SELECT target_template_ids FROM talent_abilities "
            "WHERE ability_guid=? LIMIT 1", (ability_guid,)).fetchone()
    try:
        target_templates = _json.loads(row[0]) if row and row[0] else []
    except (TypeError, ValueError, _json.JSONDecodeError):
        target_templates = []
    cost_ids = {str(tid).lower() for tid, _ctype in
                _pvp_ability_cost_templates(ability_guid)}
    legal_effects = set()
    explicit_required = False
    for tid in target_templates:
        tid = str(tid).lower()
        if tid in cost_ids:
            continue
        trow = _db.execute(
            "SELECT target_kind, is_auto_target, min_target_count "
            "FROM target_templates WHERE template_id=?", (tid,)).fetchone()
        kind = (trow[0] if trow else "") or ""
        auto = int(trow[1] or 0) if trow else 0
        if auto or kind in ("PlayerTargetTemplate",
                            "AbilitySourceCardTargetTemplate",
                            "AbilityCreatedTargetTemplate"):
            continue
        explicit_required = True
        legal_effects.update(_lt(
            _db, session.session_id, pid, tid, int(source_uid),
            both_players=target_uses_both_players(_db, tid),
            champions=champ_targets, battle_state=state))
    effect_selected = [uid for uid in selected_uids
                       if uid not in used and uid in legal_effects]
    if explicit_required and not effect_selected:
        return None
    return (effect_selected[-1] if effect_selected else None, sacrifices)


def _pvp_discard_prompt_data(ability_guid):
    """Return the child/target pair for a controller discard prompt.

    The normal case is a materialized DiscardCard BOM leaf.  Wretched
    Wrangler's extracted record stores the discard in the serialized ability
    contract, so use the generic authored hand-card target as the prompt
    contract when the leaf is absent.
    """
    from abilities import bom_leaf_prompt_data
    prompt = bom_leaf_prompt_data(
        _db, ability_guid, "DiscardCardAbilityEffectTemplate")
    if prompt and prompt[1]:
        return prompt
    row = _db.execute(
        "SELECT game_text FROM card_abilities_meta WHERE ability_guid=?",
        (str(ability_guid).lower(),)).fetchone()
    if not (row and re.match(
            r"^\s*(?:\[[^]]+\]\s*)*discard\s+(?:a|one)\s+card\b",
            row[0] or "", re.IGNORECASE)):
        return None
    target = _db.execute(
        "SELECT template_id FROM target_templates "
        "WHERE lower(game_text)=? AND lower(filter_json) LIKE ? "
        "ORDER BY template_id LIMIT 1",
        ("a card from your hand", "%hand%"),).fetchone()
    return (str(ability_guid).lower(), target[0]) if target else None


def _pvp_add_troop_ability_options(g, session, state, pl_t, opp_t, pid,
                                   affordable):
    """Append warzone-troop ability options (ECardUsage.Activate) to the most
    recent PlayerOptionList, one OptionInstance per affordable ability with
    target instances per target template — mirrors PvE _add_troop_ability_options."""
    import json as _js
    if not g.events:
        return
    # Card/PlayerUpdated events can be appended while a packet is assembled.
    # Find the owning option list instead of assuming it is the final event;
    # otherwise manual abilities disappear from some refresh packets.
    last_ev = next(
        (event for event in reversed(g.events)
         if isinstance(event, _ge.PlayerOptionListSessionEventArgs)), None)
    if last_ev is None:
        return
    from abilities.framework.targeting import (
        legal_targets as _lt, target_uses_both_players as _both_players)
    champ_map = state.get("champ_map") or {}
    champ_targets = []
    for cpid in (state.get("pids") or []):
        ccu = int(champ_map.get(str(cpid), 0))
        if ccu:
            champ_targets.append((ccu, cpid, "Champ",
                                  int(state.get(f"hp_{cpid}", 20))))
    for (card_uid, tpl_guid), abilities in affordable.items():
        scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
        opt = g._make_event(_ge.PlayerOptionSessionEventArgs)
        opt.card = scid
        opt.state = _ge.ECardUsage.Activate
        for ag in abilities:
            inst = g._make_event(_ge.OptionInstanceSessionEventArgs)
            inst.opt_id = _ge.ResourceId.from_str(ag)
            mrow = _db.execute(
                "SELECT target_template_ids FROM card_abilities_meta "
                "WHERE ability_guid=?", (ag,)).fetchone()
            tpls = []
            if mrow and mrow[0]:
                try:
                    tpls = _js.loads(mrow[0])
                except Exception:
                    tpls = []
            if tpls:
                built = []
                for i, tid in enumerate(tpls):
                    tid = str(tid)
                    tt = _db.execute(
                        "SELECT target_kind, is_auto_target "
                        "FROM target_templates WHERE template_id=?",
                        (tid,)).fetchone()
                    kind = (tt[0] if tt else "") or ""
                    auto = int(tt[1] or 0) if tt else 0
                    if auto or kind in ("PlayerTargetTemplate",
                                        "AbilitySourceCardTargetTemplate",
                                        "AbilityCreatedTargetTemplate"):
                        continue
                    built.append(i)
                    others = _lt(_db, session.session_id, pid, tid,
                                 int(card_uid),
                                 both_players=_both_players(_db, tid),
                                 champions=champ_targets,
                                 battle_state=state)
                    if not others:
                        tt2 = _db.execute(
                            "SELECT filter_json FROM target_templates "
                            "WHERE template_id=?", (tid,)).fetchone()
                        filt = (tt2[0] if tt2 else "") or ""
                        if filt.strip() in ("", "{}"):
                            others = [r[0] for r in _db.execute(
                                "SELECT card_uid FROM game_cards WHERE session_id=? "
                                "AND user_id=? AND location='warzone' ORDER BY position",
                                (session.session_id, pid)).fetchall()]
                    tgt = g._make_event(_ge.TargetInstanceSessionEventArgs)
                    tgt.target_index = i
                    tgt.target_id = _ge.ResourceId.from_str(tid)
                    tgt.targets = [_ge.SessionCardId(_ge.UID(int(u)))
                                   for u in others]
                    # The client matches the picker to the ability through
                    # TargetIds as well as TargetInstances.  Without this
                    # field the Prairie Scout option can be visible but never
                    # opens a target picker.
                    inst.target_ids.append(_ge.ResourceId.from_str(tid))
                    inst.target_instances.append(tgt)
                if built:
                    inst.min_target_counts = [1] * len(built)
                    inst.max_target_counts = [1] * len(built)
                else:
                    inst.min_target_counts = []
                    inst.max_target_counts = []
            # A nested discard effect is a child ability.  Mirror the PvE
            # option contract: advertise that child as a separate option
            # instance on the same source card, with its hand targets.  The
            # child is not a target of the parent (Stargazer's parent targets
            # are two automatic "You" entries).
            discard_prompt = _pvp_discard_prompt_data(ag)
            child_instance = None
            if discard_prompt and discard_prompt[1]:
                child_ability, discard_target = discard_prompt
                hand = [_ge.SessionCardId(_ge.UID(int(r[0]))) for r in
                        _db.execute(
                            "SELECT card_uid FROM game_cards "
                            "WHERE session_id=? AND user_id=? "
                            "AND location='hand' ORDER BY position",
                            (session.session_id, int(pid))).fetchall()]
                if hand:
                    child = g._make_event(
                        _ge.OptionInstanceSessionEventArgs)
                    child.opt_id = _ge.ResourceId.from_str(child_ability)
                    child.target_ids.append(
                        _ge.ResourceId.from_str(discard_target))
                    child.min_target_counts = [1]
                    child.max_target_counts = [1]
                    child_target = g._make_event(
                        _ge.TargetInstanceSessionEventArgs)
                    child_target.target_index = 0
                    child_target.target_id = _ge.ResourceId.from_str(
                        discard_target)
                    child_target.targets = hand
                    child.target_instances.append(child_target)
                    child_instance = child
            cost_targets = _pvp_ability_cost_targets(
                session, state, pid, int(card_uid), ag, champ_targets)
            for tid, cost_type, candidates, minimum, maximum in (
                    cost_targets or []):
                cost_ev = g._make_event(_ge.CostInstanceSessionEventArgs)
                cost_ev.min_target_count = minimum
                cost_ev.max_target_count = maximum
                cost_ev.cost_type = cost_type
                cost_ev.targets = [
                    _ge.SessionCardId(_ge.UID(int(uid)))
                    for uid in candidates]
                cost_ev.target_template_id = _ge.ResourceId.from_str(tid)
                inst.target_instances.append(cost_ev)
            # Keep the parent option first; the child discard option follows
            # it, just as in the PvE option builder.
            opt.instances.append(inst)
            if child_instance is not None:
                opt.instances.append(child_instance)
        last_ev.options.append(opt)


def _pvp_push_discard_prompt(session, state, my_pid, opp_pid, source_uid):
    """Send Stargazer's nested DiscardACard prompt to only its controller.

    The client predictively clears PlayerOptions when Stargazer is activated,
    so the child option advertised in the normal main-phase list no longer
    exists by the time the draw has completed.  Republish only the child
    option, with the post-draw hand as its target list, immediately before the
    class-23 activation request.  This is the same contract used by the PvE
    triggered-ability path: class 23 starts configuration, while the option's
    TargetInstance makes configuration open the hand-card picker.  Do not send
    class 39 here; that is the triggered-ability chooser and is a different UI
    path.
    """
    h = player_handlers.get(int(my_pid))
    if not h:
        return False
    my_uid = _ge.UID.make(244, int(my_pid))
    opp_uid = _ge.UID.make(244, int(opp_pid))
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    child = state.get("pending_discard_ability")
    target_id = state.get("pending_discard_target_template")
    if not child or not target_id:
        log_req("    PvP discard prompt: missing metadata target")
        return False

    # PredictivePushOnChain() clears the client's PlayerOptions cache as soon
    # as the parent ability is activated.  The child therefore has to be
    # re-published after the draw, using the current hand (not the hand that
    # was present when the parent option was first advertised).
    hand = [_ge.SessionCardId(_ge.UID(int(row[0]))) for row in
            _db.execute(
                "SELECT card_uid FROM game_cards "
                "WHERE session_id=? AND user_id=? AND location='hand' "
                "ORDER BY position",
                (session.session_id, int(my_pid))).fetchall()]
    if not hand:
        log_req("    PvP discard prompt: no hand target after draw")
        return False
    option_list = g._make_event(_ge.PlayerOptionListSessionEventArgs)
    option_list.player_id = my_uid
    option = g._make_event(_ge.PlayerOptionSessionEventArgs)
    option.card = _ge.SessionCardId(_ge.UID(int(source_uid)))
    option.state = _ge.ECardUsage.Activate
    child_option = g._make_event(_ge.OptionInstanceSessionEventArgs)
    child_option.opt_id = _ge.ResourceId.from_str(child)
    child_option.target_ids.append(_ge.ResourceId.from_str(target_id))
    child_option.min_target_counts = [1]
    child_option.max_target_counts = [1]
    target = g._make_event(_ge.TargetInstanceSessionEventArgs)
    target.target_index = 0
    target.target_id = _ge.ResourceId.from_str(target_id)
    target.targets = hand
    child_option.target_instances.append(target)
    option.instances.append(child_option)
    option_list.options.append(option)
    g._push(option_list)

    req = g._make_event(_ge.AbilityActivationDataRequiredSessionEventArgs)
    req.player_id = my_uid
    req.ability_instance_id = 1
    req.ability_parent_id = 0
    req.source_card_id = _ge.SessionCardId(_ge.UID(int(source_uid)))
    req.ability_template_id = _ge.ResourceId.from_str(child)
    req.effect_group_id = 1
    req.effect_instance_ids = [0]
    req.resolve_chain = False
    g._push(req)
    g.push_green_light(my_uid, _ge.EPriorityContext.Normal)
    _send_pvp_packet(h, session, g, my_uid, "discard-prompt")
    return True


def _pvp_resolve_discard_prompt(handler, session, inner_bytes, my_pid):
    """Resolve the card selected for a pending class-23 discard prompt."""
    state = pvp_load_state(session) or {}
    child = state.get("pending_discard_ability")
    if not child or int(state.get("pending_discard_pid", -1)) != int(my_pid):
        return False
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return True
    card_uids = _pvp_transaction_card_uids(inner_bytes)
    card_uid = card_uids[-1] if card_uids else None
    row = None
    if card_uid is not None:
        row = _db.execute(
            "SELECT user_id, COALESCE(owner_user_id, user_id), template_guid, "
            "card_template_id FROM game_cards WHERE session_id=? AND card_uid=? "
            "AND user_id=? AND location='hand'",
            (session.session_id, int(card_uid), int(my_pid))).fetchone()
    if not row:
        log_req(f"    PvP discard prompt rejected: uid={card_uid} "
                f"pid={my_pid}")
        return True
    owner_pid = int(row[1])
    if owner_pid not in pids:
        owner_pid = int(my_pid)
    db_discard_card(session.session_id, int(card_uid),
                    owner_user_id=owner_pid, connection=_db)
    opp_pid = pids[0] if pids[1] == int(my_pid) else pids[1]
    my_uid = _ge.UID.make(244, int(my_pid))
    opp_uid = _ge.UID.make(244, int(opp_pid))
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    _pvp_populate_game_state(g, state, int(my_pid), int(opp_pid))
    scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
    card_handler = player_handlers.get(int(my_pid)) or handler
    tpl_guid = row[2]
    _tpl, card_type, _name, cost, attack, defense, gems = \
        card_handler._card_full_data(g, scid, tpl_guid, row[3])
    owner_uid = _ge.UID.make(244, owner_pid)
    g.push_card_updated(scid, owner_uid, _ge.ECardCollections.Discard,
                        card_type, template_id=tpl_guid, cost=cost,
                        attack=attack, defense=defense, gems=gems)
    g.push_card_moved(scid, owner_uid, _ge.ECardCollections.Discard,
                      _ge.ECardLocations.Top, 0)
    for pid in pids:
        uid = _ge.UID.make(244, int(pid))
        cuid = int((state.get("champ_map") or {}).get(str(pid), 0))
        g.push_player_updated(uid, champ_id=(
            _ge.SessionCardId(_ge.UID(cuid)) if cuid else None))
    state.pop("pending_discard_ability", None)
    state.pop("pending_discard_target_template", None)
    state.pop("pending_discard_source_uid", None)
    state.pop("pending_discard_pid", None)
    state["priority_pid"] = int(my_pid)
    pvp_save_state(session, state)
    _pvp_send_same_events(session, g, my_uid, opp_uid)

    # Restore the normal priority/phase view to both clients.  Only the
    # controller receives the private main-phase options below.
    phase = int(state.get("phase", _ge.ETurnPhases.FirstMainPhase))
    turn_uid = _ge.UID.make(244, int(state.get("turn_pid", my_pid)))
    for pid in pids:
        recipient = _ge.UID.make(244, int(pid))
        other = _ge.UID.make(244, int(opp_pid if int(pid) == int(my_pid)
                                       else my_pid))
        h = player_handlers.get(int(pid))
        if not h:
            continue
        gp = _ge.Game(int(session.session_id), recipient, other)
        gp.push_green_light(my_uid, _ge.EPriorityContext.Normal)
        _pvp_push_turn_phase_with_elapsed(
            gp, phase, turn_uid, my_uid,
            _pvp_priority_elapsed_ticks(state, int(my_pid)) // 10_000_000)
        _send_pvp_packet(h, session, gp, recipient, "discard-priority")
    if phase in (_ge.ETurnPhases.FirstMainPhase,
                 _ge.ETurnPhases.SecondMainPhase):
        pvp_push_main_phase_options(session, state)
    log_req(f"    PvP discard prompt resolved: {card_uid} -> discard")
    return True


def _pvp_activate_troop_ability(handler, session, inner_bytes, my_pid,
                                ability_guid, source_uid):
    """Activate a manual ability on a warzone troop (e.g. Shift): pay the
    resource cost, bump usage, resolve the BOM on the shared event stream to
    BOTH players, and apply exhaust-as-cost — mirrors PvE
    _activate_troop_ability."""
    from db import db_card_uses, db_bump_card_use
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    state = pvp_load_state(session) or {}
    # Reuse the option calculation as the authoritative legality check.  This
    # covers phase restrictions, attacking-only targets, exhaustion, and use
    # limits when a client submits a stale or hand-crafted activation.
    source_row = _db.execute(
        "SELECT template_guid FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (session.session_id, int(source_uid))).fetchone()
    source_key = (int(source_uid), source_row[0] if source_row else "")
    affordable = _pvp_affordable_troop_abilities(
        session, state, pid=my_pid)
    if ability_guid not in affordable.get(source_key, []):
        log_req(f"    PvP troop ability {ability_guid[:8]}: not legal in "
                f"phase {state.get('phase')} — rejected")
        return True
    m = _db.execute(
        "SELECT activation_cost, uses_per_game, uses_per_turn, exhausts_on_use "
        "FROM card_abilities_meta WHERE ability_guid=?", (ability_guid,)).fetchone()
    cost = int(m[0] or 0) if m else 0
    upg = int(m[1] or 0) if m else 0
    upt = int(m[2] or 0) if m else 0
    exh = int(m[3] or 0) if m else 0
    resources = int(state.get(f"res_{my_pid}", 0))
    uses = db_card_uses(session.session_id, int(source_uid))
    used = int(uses.get(ability_guid, 0))
    if cost > resources:
        log_req(f"    PvP troop ability {ability_guid[:8]}: need {cost} "
                f"resources, have {resources}")
        return True
    if upg and used >= upg:
        log_req(f"    PvP troop ability {ability_guid[:8]}: uses_per_game "
                f"exhausted ({used})")
        return True
    if upt and used >= upt:
        log_req(f"    PvP troop ability {ability_guid[:8]}: uses_per_turn "
                f"exhausted ({used})")
        return True
    if exh:
        crow = _db.execute(
            "SELECT gc.card_state, (ct.attributes | gc.card_attributes), "
            "ct.card_type "
            "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session.session_id, int(source_uid))).fetchone()
        cstate = int(crow[0]) if crow else 0
        cattrs = int(crow[1]) if crow else 0
        card_type = crow[2] if crow else ""
        is_troop = "Troop" in str(card_type or "").split("|")
        if (cstate & _ge.ECardStates.Tapped
                or (is_troop
                    and not (cstate & _ge.ECardStates.StartedATurnOnYourSide)
                    and not (cattrs & _ge.ECardAttributes.Speed))):
            log_req(f"    PvP troop ability {ability_guid[:8]}: cannot "
                    f"exhaust {hex(source_uid)} (sick/tapped)")
            return True
    # Extract every selected Card UID.  A transaction can contain both the
    # payment selection (e.g. Ingenuity Engine's ExhaustTarget) and a normal
    # effect target, so treating only the last UID as the target loses the
    # payment choice.
    if hasattr(handler, "_extract_transaction_targets"):
        selected_uids = handler._extract_transaction_targets(
            inner_bytes, int(source_uid))
    else:
        selected_uids = []
        if isinstance(inner_bytes, bytes):
            for m_du in re.finditer(
                    rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                    inner_bytes):
                try:
                    import struct as _st
                    uid64 = _st.unpack(
                        '<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                    if ((uid64 & 0xFF) == 1
                            and int(uid64) != int(source_uid)):
                        selected_uids.append(int(uid64))
                except Exception:
                    continue

    # Validate and separate card-payment targets from effect targets using
    # the same metadata that built the option packet.  The cost target is not
    # an effect target: this distinction is what lets the client select a bot
    # to tap while the BOM continues to resolve its self-targeted effects.
    champ_targets = []
    champ_map = state.get("champ_map") or {}
    for cpid in state.get("pids") or pids:
        cuid = int(champ_map.get(str(cpid), 0))
        if cuid:
            champ_targets.append((
                cuid, int(cpid), "Champion",
                int(state.get(f"hp_{cpid}", 20))))
    cost_targets = _pvp_ability_cost_targets(
        session, state, my_pid, int(source_uid), ability_guid, champ_targets)
    if cost_targets is None:
        log_req(f"    PvP troop ability {ability_guid[:8]}: missing legal "
                "payment target — rejected")
        return True
    cost_target_uids = []
    exhausted_target_uids = []
    deck_target_uids = []
    sacrifice_target_uids = set()
    for _tid, _cost_type, candidates, minimum, maximum in cost_targets:
        candidate_set = set(candidates)
        tt = _db.execute(
            "SELECT target_kind, is_auto_target FROM target_templates "
            "WHERE template_id=?", (str(_tid),)).fetchone()
        auto_source = bool(tt and int(tt[1] or 0)) and \
            (tt[0] or "") == "AbilitySourceCardTargetTemplate"
        available = ([int(source_uid)] if auto_source else
                     [uid for uid in selected_uids
                      if int(uid) in candidate_set
                      and int(uid) not in cost_target_uids])
        if len(available) < minimum:
            log_req(f"    PvP troop ability {ability_guid[:8]}: selected "
                    "payment is incomplete — rejected")
            return True
        selected = available[:maximum]
        cost_target_uids.extend(selected)
        if int(_cost_type) == 32:  # metadata m_PutIntoDeckTarget
            deck_target_uids.extend(selected)
        else:
            exhausted_target_uids.extend(selected)
        if int(_cost_type) == 2:
            sacrifice_target_uids.update(int(uid) for uid in selected)

    # The remaining selected card, if any, is an explicit effect target.  Do
    # not consider source/auto target templates here; those are resolved by
    # the BOM from the source card and must not consume the payment target.
    target_uid = None
    import json as _target_json
    from abilities.framework.targeting import legal_targets as _legal_targets
    target_row = _db.execute(
        "SELECT target_template_ids FROM card_abilities_meta "
        "WHERE ability_guid=?", (ability_guid,)).fetchone()
    target_templates = []
    if target_row and target_row[0]:
        try:
            target_templates = _target_json.loads(target_row[0])
        except Exception:
            target_templates = []
    legal_effect_targets = set()
    for tid in target_templates:
        tt = _db.execute(
            "SELECT target_kind, is_auto_target FROM target_templates "
            "WHERE template_id=?", (str(tid),)).fetchone()
        kind = (tt[0] if tt else "") or ""
        auto = int(tt[1] or 0) if tt else 0
        if auto or kind in ("PlayerTargetTemplate",
                            "AbilitySourceCardTargetTemplate",
                            "AbilityCreatedTargetTemplate"):
            continue
        legal_effect_targets.update(_legal_targets(
            _db, session.session_id, my_pid, str(tid), int(source_uid),
            both_players=True, champions=champ_targets,
            battle_state=state))
    for uid in selected_uids:
        if int(uid) not in cost_target_uids and int(uid) in legal_effect_targets:
            target_uid = int(uid)
            break
    if target_uid is None and not cost_targets and selected_uids:
        # Preserve the legacy single-target activation behavior for abilities
        # without card-payment targets.
        target_uid = int(selected_uids[-1])
        if target_templates:
            if target_uid not in legal_effect_targets:
                log_req(f"    PvP troop ability {ability_guid[:8]}: target "
                        f"{target_uid} is not metadata-legal — rejected")
                return True
    discard_prompt_data = _pvp_discard_prompt_data(ability_guid)
    # Resource payment is committed only after all card targets have passed
    # validation, so a stale client transaction cannot spend resources while
    # silently doing nothing.
    state[f"res_{my_pid}"] = resources - cost
    db_bump_card_use(session.session_id, int(source_uid), ability_guid)
    my_uid = _ge.UID.make(244, my_pid)
    opp_uid = _ge.UID.make(244, opp_pid)
    view = _pvp_fra_view(state, my_pid, opp_pid)
    view["player_mod_target"] = target_uid if target_uid else int(source_uid)
    view["player_transform_target"] = target_uid if target_uid else int(source_uid)
    view["player_spell_target"] = target_uid
    view["resolving_ability"] = ability_guid
    view["resolving_source_uid"] = int(source_uid)
    view["resolving_owner_id"] = my_pid
    view["player_shift_source"] = int(source_uid)
    view["player_shift_target"] = target_uid
    # Expose metadata-selected payments to the BOM variable layer.  This is
    # used by Construction Plans to count the troops exhausted by this
    # activation, while keeping the normal effect target separate.
    view["ability_lists"] = {
        "ExhaustedCards": list(exhausted_target_uids),
    }
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    _pvp_populate_game_state(g, state, my_pid, opp_pid)

    # Pay card costs before resolving the effect.  The client-visible
    # CardUpdated is emitted on the same event stream as the ability so both
    # players see the selected bot become tapped.
    for pay_uid in sorted(set(cost_target_uids)):
        if pay_uid in deck_target_uids:
            continue
        if pay_uid in sacrifice_target_uids:
            # Automatic source-card sacrifices ("Sacrifice this") are not
            # present in the client's TargetMap, but are still real costs.
            # Move the card before resolving the BOM; its source UID remains
            # in the resolution context so the draw/effect can complete.
            handler._sacrifice_troop(g, session, my_uid, opp_uid, pay_uid)
            log_req(f"    PvP troop ability {ability_guid[:8]}: sacrificed "
                    f"cost target {hex(pay_uid)}")
            continue
        prow = _db.execute(
            "SELECT template_guid, card_type FROM game_cards "
            "WHERE session_id=? AND card_uid=? AND user_id=? "
            "AND location='warzone'",
            (session.session_id, pay_uid, my_pid)).fetchone()
        if not prow:
            continue
        _db.execute(
            "UPDATE game_cards SET card_state = card_state | ? "
            "WHERE session_id=? AND card_uid=?",
            (_ge.ECardStates.Tapped, session.session_id, pay_uid))
        _db.commit()
        pay_scid = _ge.SessionCardId(_ge.UID(pay_uid))
        _tpl_pay, ct_pay, _name_pay, cost_pay, atk_pay, def_pay, gem_pay = \
            handler._card_full_data(g, pay_scid, prow[0])
        pay_state = _db.execute(
            "SELECT card_state FROM game_cards WHERE session_id=? "
            "AND card_uid=?", (session.session_id, pay_uid)).fetchone()
        g.push_card_updated(
            pay_scid, my_uid, _ge.ECardCollections.Warzone, ct_pay,
            template_id=prow[0], state=int(pay_state[0]) if pay_state else
            _ge.ECardStates.Tapped, cost=cost_pay, attack=atk_pay,
            defense=def_pay, gems=gem_pay)
        log_req(f"    PvP troop ability {ability_guid[:8]}: exhausted "
                f"cost target {hex(pay_uid)}")
    # m_PutIntoDeckTarget is an ability-level zone operation represented by a
    # target CostInstance in the client protocol.  It is not a payment and
    # therefore must move the selected cards rather than exhaust them.
    for move_uid in sorted(set(deck_target_uids)):
        move_row = _db.execute(
            "SELECT template_guid, card_template_id, user_id, location "
            "FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(move_uid))).fetchone()
        if not move_row or move_row[3] != "warzone":
            continue
        owner_pid = int(move_row[2] or my_pid)
        _db.execute(
            "UPDATE game_cards SET location='deck', position=0, card_state=? "
            "WHERE session_id=? AND card_uid=?",
            (0, session.session_id, int(move_uid)))
        _db.commit()
        from db import db_randomly_insert_deck_cards
        db_randomly_insert_deck_cards(
            session.session_id, owner_pid, [int(move_uid)], connection=_db)
        pos_row = _db.execute(
            "SELECT position FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(move_uid))).fetchone()
        pos = int(pos_row[0]) if pos_row else 0
        move_scid = _ge.SessionCardId(_ge.UID(int(move_uid)))
        move_owner = _ge.UID.make(244, owner_pid)
        _tpl_move, ct_move, _name_move, cost_move, atk_move, def_move, gems_move = \
            handler._card_full_data(g, move_scid, move_row[0], move_row[1])
        g.push_card_moved(move_scid, move_owner,
                          _ge.ECardCollections.Deck,
                          _ge.ECardLocations.Unknown, 0)
        g.push_card_updated(move_scid, move_owner,
                            _ge.ECardCollections.Deck, ct_move,
                            template_id=move_row[0], cost=cost_move,
                            attack=atk_move, defense=def_move, gems=gems_move,
                            state=0, nulling=True)
        log_req(f"    PvP troop ability {ability_guid[:8]}: put "
                f"{hex(move_uid)} into deck (pos {pos})")
    try:
        from abilities import resolve_effect as _re_eff
        fn = _re_eff(ability_guid)
        if fn:
            fn(g, session, _db, handler, my_uid, opp_uid, view,
               ability_guid, None)
    except Exception as e:
        import traceback
        log_req(f"    PvP troop ability resolve error: {e}")
        traceback.print_exc()
    handler._remove_one_shot_ability(
        session, int(source_uid), ability_guid, g, my_uid, opp_uid, state)
    # DiscardCard is a leaf placeholder in the shared BOM executor.  The
    # actual hand choice is requested by a class-23 child-ability prompt after
    # the draw has been emitted, rather than guessing the first card here.
    # Exhaust-as-cost: tap the source.
    if exh:
        _db.execute(
            "UPDATE game_cards SET card_state = card_state | ? "
            "WHERE session_id=? AND card_uid=?",
            (_ge.ECardStates.Tapped, session.session_id, int(source_uid)))
        _db.commit()
        scid_src = _ge.SessionCardId(_ge.UID(int(source_uid)))
        trow = _db.execute(
            "SELECT template_guid FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(source_uid))).fetchone()
        if trow:
            _tpl_src, ct_src, _n_src, cost_src, atk_src, def_src, gem_src = \
                handler._card_full_data(g, scid_src, trow[0])
            crow = _db.execute(
                "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, int(source_uid))).fetchone()
            g.push_card_updated(scid_src, my_uid, _ge.ECardCollections.Warzone,
                                ct_src, template_id=trow[0],
                                state=int(crow[0]) if crow
                                else _ge.ECardStates.Tapped, cost=cost_src,
                                attack=atk_src, defense=def_src, gems=gem_src)
    # Persist health/stack, push the resource deduction + events to both.
    if view.get("player_health") is not None:
        state[f"hp_{my_pid}"] = int(view["player_health"])
    if view.get("ai_health") is not None:
        state[f"hp_{opp_pid}"] = int(view["ai_health"])
    state["stack"] = view.get("stack") or []
    state["stack_passed"] = []
    _pvp_sync_view_to_state(state, view, my_pid, opp_pid)
    pvp_save_state(session, state)
    g.player_resources = int(state.get(f"res_{my_pid}", 0))
    g.player_total_resources = int(state.get(f"res_total_{my_pid}", 0))
    g.ai_resources = int(state.get(f"res_{opp_pid}", 0))
    g.ai_total_resources = int(state.get(f"res_total_{opp_pid}", 0))
    ev_spent = _ge.PlayerCurrentResourcePoolChangedSessionEventArgs()
    ev_spent.player_id = my_uid
    ev_spent.operation = 2
    ev_spent.delta = cost
    ev_spent.new_value = g.player_resources
    g._push(ev_spent)
    champ_map = state.get("champ_map") or {}
    for target_pid in pids:
        t_uid = _ge.UID.make(244, target_pid)
        cu = int(champ_map.get(str(target_pid), 0))
        g.push_player_updated(t_uid, champ_id=_ge.SessionCardId(
            _ge.UID(cu)) if cu else None)
    _pvp_send_same_events(session, g, my_uid, opp_uid)
    if _pvp_check_game_end(session, state):
        return True
    if discard_prompt_data:
        hand_exists = _db.execute(
            "SELECT 1 FROM game_cards WHERE session_id=? AND user_id=? "
            "AND location='hand' LIMIT 1",
            (session.session_id, my_pid)).fetchone()
        if hand_exists:
            state["pending_discard_ability"] = discard_prompt_data[0]
            state["pending_discard_target_template"] = discard_prompt_data[1]
            state["pending_discard_source_uid"] = int(source_uid)
            state["pending_discard_pid"] = int(my_pid)
            state["priority_pid"] = int(my_pid)
            pvp_save_state(session, state)
            _pvp_push_discard_prompt(
                session, state, my_pid, opp_pid, int(source_uid))
            log_req(f"    PvP nested discard prompt: {ability_guid[:8]} "
                    f"source={hex(int(source_uid))}")
            return True
    # The player keeps priority to resolve any chain / continue.
    state["priority_pid"] = my_pid
    pvp_save_state(session, state)
    turn_h = player_handlers.get(my_pid)
    if turn_h:
        gg = _ge.Game(int(session.session_id), my_uid, opp_uid)
        ctx = (_ge.EPriorityContext.ResolveTopOfChain
               if state.get("stack") else _ge.EPriorityContext.Normal)
        gg.push_green_light(my_uid, ctx)
        # Reassert the phase after the ability transaction.  The client can
        # have just left BattleStateConfigureAbility/AssignCardsAsCost while
        # processing this packet; without a fresh phase event it may retain
        # activatable buttons but lose the Continue/Pass button.
        _pvp_push_turn_phase_with_elapsed(
            gg, int(state.get("phase", 0)),
            _ge.UID.make(244, int(state.get("turn_pid") or my_pid)),
            my_uid,
            _pvp_priority_elapsed_ticks(state, my_pid) // 10_000_000)
        _send_pvp_packet(turn_h, session, gg, my_uid, "troop-ability")
    if (not state.get("stack") and
            state.get("phase") in (_ge.ETurnPhases.FirstMainPhase,
                                    _ge.ETurnPhases.SecondMainPhase)):
        pvp_push_main_phase_options(session, state)
    log_req(f"    PvP troop ability {ability_guid[:8]} activated on "
            f"{hex(int(source_uid))} (cost {cost}, target="
            f"{hex(target_uid) if target_uid else 'none'})")
    return True


def push_pvp_game_start(handler, session, log_req=log_req):
    """Push initial battle events for a PvP tournament session (tourney-N)."""
    player_uid = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0
    sess_id = session.session_id.uid64 if hasattr(session.session_id, 'uid64') else int(session.session_id)

    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        log_req("    PvP: need 2 players in session")
        return

    # Requesting player is pl_t (their perspective); the other is opp_t.
    log_req(f"    push_pvp: player_uid={player_uid} pids={pids}")
    if pids[0] == player_uid:
        pl_t = _ge.UID.make(244, pids[0])
        opp_t = _ge.UID.make(244, pids[1])
    else:
        pl_t = _ge.UID.make(244, pids[1])
        opp_t = _ge.UID.make(244, pids[0])

    # Shuffle both players' decks before drawing.
    for pid in pids:
        db_game_shuffle_deck(session.session_id, pid)

    # Champion card IDs + gamedata starting health (e.g. Dimmid = 19).
    champ_health = {}
    pchamp = None
    achamp = None
    for pid in pids:
        champ_row = db_game_champion(session.session_id, pid)
        if champ_row:
            cid = _ge.SessionCardId(_ge.UID(int(champ_row[0])))
            hp = db_champion_template_health(champ_row[1]) or 20
            champ_health[pid] = hp
            ct = _ge.CardDef("Champion", _ECardTypes.Champion, 0, hp, hp, [], [])
            if pid == player_uid:
                pchamp = cid
            else:
                achamp = cid
    if not pchamp:
        pchamp = _ge.SessionCardId(_ge.UID.make(1, 0))
    if not achamp:
        achamp = _ge.SessionCardId(_ge.UID.make(1, 0))

    # Coin flip — read from session state (set in handle_ready_for_game_setup).
    state = pvp_load_state(session)
    goes_first_pid = (state or {}).get("goes_first_pid", pids[0])
    # This is a new battle setup, so neither player has consumed their one
    # resource play yet.  Do this explicitly instead of relying only on the
    # Prep phase reset: the first-main options are the first packet that tells
    # the client which hand cards to outline, and a stale setup state must not
    # hide resources while the transaction handler still accepts them.
    if state is None:
        # Seed the complete participant list. Passing the same player twice to
        # pvp_default_state makes its compact default representation omit the
        # opponent, leaving the first-main options stream without the normal
        # two-player state context.
        state = pvp_default_state(pids[0], goes_first_pid)
        state["pids"] = list(pids)
    for _pid in pids:
        state[f"res_played_{_pid}"] = 0
    pvp_save_state(session, state)
    goes_first_uid = (goes_first_pid << 8) | 244  # raw uid64 for ServicePlayer type
    log_req(f"    Coin flip: {hex(goes_first_uid)} goes first")

    # 1. GameStarted — local player's champion always at index 0 (left side).
    champ_guids = [None, None]
    champ_names = ["Player 1", "Player 2"]
    for pid in pids:
        idx = 0 if pid == player_uid else 1
        cr = db_game_champion(session.session_id, pid)
        if cr:
            champ_guids[idx] = cr[1]
        sr = _db.execute(
            "SELECT player_name FROM tournament_signups "
            "WHERE tournament_id=(SELECT id FROM tournaments WHERE session_id=? LIMIT 1) AND player_uid=?",
            (session.session_id, pid)).fetchone()
        if sr:
            champ_names[idx] = sr[0]
    if champ_guids[0] is None: champ_guids[0] = "00000000-0000-0000-0000-000000000000"
    if champ_guids[1] is None: champ_guids[1] = "00000000-0000-0000-0000-000000000000"

    # --- Packet 1: GameStarted + champions + PlayerUpdated ----------------
    # Event order matches the PvE game-init sequence (hconnect_server.py ~3994).
    game1 = _ge.Game(sess_id, pl_t, opp_t)
    game1.player_champion_card_id = pchamp
    game1.ai_champion_card_id = achamp
    _pvp_populate_game_state(
        game1, state or {}, player_uid,
        pids[1] if player_uid == pids[0] else pids[0])

    # 1. GameStarted — registers turn order, champion names / template IDs.
    game1.push_game_started(champion_names=champ_names,
                            champion_template_ids=champ_guids,
                            player_first=(goes_first_pid == player_uid))
    # Coin flip resolution (class 60): lets the client complete the coin-flip
    # state (m_CoinFlipSkip -> m_CoinFlipDone) so it can process the phases
    # that follow.  Without it neither client gets past the toss.
    game1.push_first_player_dictated(
        _ge.UID.make(244, goes_first_pid))
    log_req(f"    PvP start: pushed GameStarted + FirstPlayerDictated to pid "
            f"{player_uid} (winner {goes_first_pid})")

    # 2. PlayerUpdated — must come before card events so State.Players exists.
    game1.push_player_updated(pl_t, champ_id=pchamp)
    game1.push_player_updated(opp_t, champ_id=achamp)

    # 3. CardUpdated for champions — ECardCollections.None_ (matches PvE).
    for pid in pids:
        is_pl = (pid == player_uid)
        pt = pl_t if is_pl else opp_t
        ch_id = pchamp if is_pl else achamp
        cr = db_game_champion(session.session_id, pid)
        tg = cr[1] if cr else "00000000-0000-0000-0000-000000000000"
        hp = champ_health.get(pid, 20)
        handler._card_full_data(game1, ch_id, tg)
        game1.push_card_updated(ch_id, pt, _ECardCollections.None_,
                                _ECardTypes.Champion, attack=0, defense=hp,
                                template_id=tg)
        try:
            _cd = game1.card_defs.get(ch_id)
            _ab = list(_cd.abilities) if _cd else []
            log_req(f"    PvP champ CardUpdated {ch_id}: abilities="
                    f"{[str(a.guid)[:8] for a in _ab]}")
        except Exception:
            pass

    # 4. ChampionCardPlayed — populates HUD portraits (AFTER CardUpdated per PvE).
    for pid in pids:
        is_pl = (pid == player_uid)
        pt = pl_t if is_pl else opp_t
        ch_id = pchamp if is_pl else achamp
        pn = champ_names[0] if is_pl else champ_names[1]
        game1.push_champion_card_played(pt, False, pn, ch_id)

    # Persist champion SCIDs so _pvp_run_phase_start passes valid IDs.
    state = pvp_load_state(session)
    if state:
        champ_map = state.get("champ_map", {})
        for pid in pids:
            is_pl = (pid == player_uid)
            ch = pchamp if is_pl else achamp
            champ_map[str(pid)] = ch.uid.uid64
            state[f"hp_{pid}"] = champ_health.get(pid, 20)
        state["champ_map"] = champ_map
        pvp_save_state(session, state)
        # The shared ability framework (abilities/framework/bom.py _deal_damage,
        # resolution.py _champion_uids, etc.) maps champion targets via the
        # handler's _player_champ_scid / _ai_champ_scid — PvE sets these at
        # battle init, PvP never did, so champion-targeting effects (Burn on a
        # champion) resolved as "no card" and dealt no damage.  Set them here.
        try:
            handler._player_champ_scid = pchamp
            handler._ai_champ_scid = achamp
            handler._player_champ_guid = champ_guids[0] if champ_guids else None
            handler._ai_champ_guid = champ_guids[1] if len(champ_guids) > 1 else None
        except Exception:
            pass

    pkt1 = game1.make_network_packet(pl_t)
    dw1 = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt1)), 1,
                             client_session_guid(handler))
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{handler.scnt}",
        "target": "ServiceGameSession", "instance": str(session.server_id),
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }, dw1)

    # --- Packet 2: Deck / hand / PreGame phase ---------------------------
    # (No separate GameStarted — it's in packet 1 with champion data.)
    game2 = _ge.Game(sess_id, pl_t, opp_t)
    game2.player_champion_card_id = pchamp
    game2.ai_champion_card_id = achamp
    game2.turn_number = 1

    # Push deck cards face-down + DeckCreated for both players.
    for pid in pids:
        is_me = (pid == player_uid)
        player_t = pl_t if is_me else opp_t
        deck_cards = db_game_deck_cards(session.session_id, pid)
        for cu, tg in deck_cards:
            scid = _ge.SessionCardId(_ge.UID(int(cu)))
            handler._card_full_data(game2, scid, tg)
            ct_str = db_game_card_type(tg)
            ct = _ge.card_type_from_db(ct_str) if ct_str else _ECardTypes.Troop
            game2.push_card_updated(scid, player_t, _ECardCollections.Deck,
                                    ct, template_id=tg, nulling=True)
        # DeckCreated populates the deck UI zone (hand/deck counters).
    # DeckCreated for both players.
    for pid in pids:
        is_me = (pid == player_uid)
        player_t = pl_t if is_me else opp_t
        game2.push_deck_created(player_t)

    # PickGoesFirst with correct turn player.  GreenLight must precede the
    # phase in the same packet for the winner, otherwise UIBattle sees local
    # priority before HasPriority is set and immediately requests a resync.
    turn_uid = _ge.UID.make(244, goes_first_pid)
    if player_uid == goes_first_pid:
        game2.push_green_light(turn_uid, _ge.EPriorityContext.Normal)
    game2.push_turn_phase(_ge.ETurnPhases.PickGoesFirst, turn_uid, turn_uid)

    pkt2 = game2.make_network_packet(pl_t)
    dw2 = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt2)), 1,
                             client_session_guid(handler))
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{handler.scnt}",
        "target": "ServiceGameSession", "instance": str(session.server_id),
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }, dw2)
    log_req(f"    Pushed PvP game setup ({len(dw1)}+{len(dw2)}b)")

    log_req(f"    Pushed PickGoesFirst phase ({len(dw2)}b)")


@_pvp_locked
def route_pvp_pass(handler, session):
    """Handle a PassPriority in a tournament PvP session.

    When both players pass the current phase, the server atomically
    advances: pushes the new phase to BOTH players, runs start-of-phase
    logic, then gives GreenLight to the turn player.  No intermediate
    green-light passthrough — that broke client state.

    Phase progression reuses the battle engine's per-turn phase lists
    (BASE_TURN_PHASES / COMBAT_TURN_PHASES), so PvP wraps at EndTurn, switches
    the turn player and runs the same start-of-phase logic (Prep resources,
    Draw, GreenLight) as the AI path.
    """
    if not (session.session_name or "").startswith("tourney-"):
        return False
    my_pid = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False

    state = pvp_load_state(session)
    if state is None:
        state = pvp_default_state(my_pid, my_pid)
        state["phase"] = 10
        pvp_save_state(session, state)

    # Never auto-pass during Mulligan / PickGoesFirst.
    if state["phase"] in (3, 4):
        log_req(f"    PvP: ignoring pass in phase {state['phase']} (mulligan/setup)")
        return True

    # Instant-speed response window: after the turn player plays a card, the
    # OPPONENT gets priority to respond with a quick action.  When the opponent
    # passes, hand priority BACK to the caster (don't advance the phase) so they
    # can continue or, if the chain is non-empty, resolve it.
    resp_wait = state.get("response_waiting_pid")
    if resp_wait == my_pid:
        caster_pid = state.get("response_caster_pid") or state.get("turn_pid")
        state.pop("response_waiting_pid", None)
        state.pop("response_caster_pid", None)
        state["priority_pid"] = caster_pid
        pvp_save_state(session, state)
        # If a chain item is pending (e.g. Adamanthian Scrivener's enters-play
        # trigger), the opponent's response pass counts as their stack pass —
        # so the caster is ONE pass from resolving the top item.  Seed
        # stack_passed with the opponent so the caster's Resolve pass triggers
        # _pvp_resolve_chain (mirrors the both-pass stack rule).
        if state.get("stack"):
            sp = set(state.get("stack_passed") or [])
            sp.add(my_pid)  # the opponent just passed the stack
            state["stack_passed"] = sorted(sp)
            pvp_save_state(session, state)
        caster_h = player_handlers.get(caster_pid)
        if caster_h:
            caster_uid = _ge.UID.make(244, caster_pid)
            opp_uid = _ge.UID.make(244, my_pid)
            gg = _ge.Game(int(session.session_id), caster_uid, opp_uid)
            ctx = (_ge.EPriorityContext.ResolveTopOfChain
                   if state.get("stack") else _ge.EPriorityContext.Normal)
            gg.push_green_light(caster_uid, ctx)
            _send_pvp_packet(caster_h, session, gg, caster_uid,
                             "response-window-close")
        _st2 = pvp_load_state(session) or {}
        if _st2.get("phase") in (_ge.ETurnPhases.FirstMainPhase,
                                 _ge.ETurnPhases.SecondMainPhase) \
                and not state.get("stack"):
            pvp_push_main_phase_options(session, _st2)
        log_req(f"    PvP response window: {my_pid} passed — priority back to "
                f"caster {caster_pid}"
                + (", caster 1 pass from resolving chain"
                   if state.get("stack") else ""))
        return True

    # Chain/stack resolution: while the chain is non-empty, the client's pass
    # button is "Resolve" — but BOTH players must pass for the top item to
    # resolve (mirrors PvE stack_set_pass / stack_both_passed).  The first
    # passer hands priority to the OTHER player (ResolveTopOfChain) so they
    # can respond; only when both have passed does the item resolve.
    if state.get("stack"):
        sp = set(state.get("stack_passed") or [])
        if my_pid in sp:
            # Already passed — ignore the duplicate.
            return True
        sp.add(my_pid)
        other_pid = pids[0] if pids[1] == my_pid else pids[1]
        if len(sp) < 2:
            # Only one player has passed: hand priority to the other so they
            # can cast a response (quick action) or pass to resolve.
            state["stack_passed"] = sorted(sp)
            state["priority_pid"] = other_pid
            pvp_save_state(session, state)
            _pvp_log_stack(state, f"pass-1/2 by {my_pid}")
            if _pvp_auto_pass_chain_priority(session, state, other_pid):
                return True
            other_h = player_handlers.get(other_pid)
            if other_h:
                my_uid = _ge.UID.make(244, my_pid)
                other_uid = _ge.UID.make(244, other_pid)
                gg = _ge.Game(int(session.session_id), other_uid, my_uid)
                gg.push_green_light(other_uid,
                                    _ge.EPriorityContext.ResolveTopOfChain)
                _send_pvp_packet(other_h, session, gg, other_uid,
                                 "chain-respond")
                # Offer the responding player their quick actions / champion
                # powers so they can actually respond (e.g. Countermagic the
                # troop/spell on the stack, or cast an interrupt).
                try:
                    pvp_push_phase_options(session, state, pid=other_pid)
                except Exception as e:
                    log_req(f"    PvP chain-respond options error: {e}")
                log_req(f"    PvP chain: {my_pid} passed 1/2 — priority to "
                        f"{other_pid} to respond")
            return True
        # Both players passed — resolve the top item.
        state["stack_passed"] = []
        pvp_save_state(session, state)
        _pvp_log_stack(state, f"pass-2/2 by {my_pid}")
        return _pvp_resolve_chain(session, state, handler, my_pid)

    # Record this player's pass.
    passes = state.get("passes") or []
    if my_pid not in passes:
        passes.append(my_pid)
    state["passes"] = passes
    pvp_save_state(session, state)

    if len(passes) < 2:
        waiting_pid = pids[0] if pids[1] in passes else pids[1]
        # If the phase is an OPPONENT-STOP for the waiting player, hand them
        # priority so they can actually act (the client only shows the pass
        # button while holding priority).  Otherwise they have nothing to
        # respond to (no instants in PvP yet) — auto-complete their pass so
        # the turn player's single pass advances the phase ("Continue to
        # Second Main Phase" just works instead of stalling at 1/2).
        import battle_engine as _be
        # Only stop the waiting player for MANDATORY opponent phases
        # (OPP_ALWAYS_STOPS — DeclareDefense, where they must decide blocks)
        # or phases they EXPLICITLY configured as opponent-stops.  The client's
        # DEFAULT opponent stops (SecondMain, DeclareAttackPriorityWindow,
        # DeclareDefensePriorityWindow) would otherwise force a manual pass
        # from the opponent every turn even though PvP has no instants to
        # respond with — stalling at the turn player's pass.
        _opp_stops = set(_be.OPP_ALWAYS_STOPS)
        _explicit_opp = state.get(f"stops_opp_{waiting_pid}")
        if _explicit_opp:
            _opp_stops.update(_explicit_opp)
        # A QuickAction permanent ability is a real response option during an
        # opponent's main phase too.  The previous auto-complete path only
        # considered configured opponent stops, so the active player's pass
        # advanced the phase before the other client could activate cards such
        # as Construction Plans: Ingenuity Engine.
        quick_action_wait = False
        if int(state.get("phase", -1)) in (
                _ge.ETurnPhases.FirstMainPhase,
                _ge.ETurnPhases.SecondMainPhase):
            try:
                quick_action_wait = bool(_pvp_affordable_troop_abilities(
                    session, state, pid=waiting_pid))
            except Exception:
                quick_action_wait = False
        if ((int(state["phase"]) in _opp_stops or quick_action_wait)
                and not pvp_player_auto_passes(state, waiting_pid)):
            waiting_h = player_handlers.get(waiting_pid)
            if waiting_h:
                waiting_uid = _ge.UID.make(244, waiting_pid)
                other_pid = pids[1] if waiting_pid == pids[0] else pids[0]
                other_uid = _ge.UID.make(244, other_pid)
                g = _ge.Game(int(session.session_id), waiting_uid, other_uid)
                # A normal GreenLight only toggles the client's priority bit.
                # If an earlier animation/state transition left the priority
                # window missing, the client can show activatable abilities
                # but no Continue button.  Re-assert the current phase after
                # GreenLight so the client rebuilds its phase state, matching
                # the reconnect snapshot path.
                g.push_green_light(waiting_uid, _ge.EPriorityContext.Normal)
                state["priority_pid"] = waiting_pid
                pvp_save_state(session, state)
                _pvp_push_turn_phase_with_elapsed(
                    g, int(state["phase"]),
                    _ge.UID.make(244, int(state.get("turn_pid") or waiting_pid)),
                    waiting_uid,
                    _pvp_priority_elapsed_ticks(state, waiting_pid) // 10_000_000)
                # Champion charge powers must stay interactive in ANY priority
                # window (mirrors PvE _push_phase_options_empty): push an
                # empty options list carrying the champion so the buttons
                # remain lit on the opponent's screen during their stop.
                g.push_options(waiting_uid, [])
                affordable_wait = _pvp_affordable_troop_abilities(
                    session, state, pid=waiting_pid)
                if affordable_wait:
                    _pvp_add_troop_ability_options(
                        g, session, state, waiting_uid, other_uid,
                        waiting_pid, affordable_wait)
                _pvp_add_champion_options(g, session, state, waiting_pid,
                                          waiting_uid)
                _send_pvp_packet(waiting_h, session, g, waiting_uid,
                                 "pass-handoff")
                # GreenLight is local client state.  The waiting client must
                # gain it, but the player who just passed must also receive
                # the same owner so it immediately loses priority and clears
                # its options.  Relying on the server-side watchdog for the
                # second half leaves the two clients disagreeing during the
                # handoff and can strand the UI between priority windows.
                passed_h = player_handlers.get(my_pid)
                if passed_h:
                    passed_uid = _ge.UID.make(244, my_pid)
                    passed_game = _ge.Game(
                        int(session.session_id), passed_uid, waiting_uid)
                    passed_game.push_green_light(
                        waiting_uid, _ge.EPriorityContext.Normal)
                    _pvp_push_turn_phase_with_elapsed(
                        passed_game, int(state["phase"]),
                        _ge.UID.make(
                            244, int(state.get("turn_pid") or waiting_pid)),
                        waiting_uid,
                        _pvp_priority_elapsed_ticks(
                            state, waiting_pid) // 10_000_000)
                    _send_pvp_packet(passed_h, session, passed_game,
                                     passed_uid, "pass-handoff-lost")
                log_req(f"    PvP pass: {len(passes)}/2 — priority handed to "
                        f"{waiting_pid} ({'quick action' if quick_action_wait else 'opponent stop'} "
                        f"on phase {state['phase']})")
            else:
                log_req(f"    PvP pass: {len(passes)}/2 — waiting for opponent")
            return True
        # No opponent-stop: the waiting player has nothing to respond to —
        # auto-complete their pass and advance.
        if waiting_pid not in passes:
            passes.append(waiting_pid)
        state["passes"] = passes
        pvp_save_state(session, state)
        log_req(f"    PvP pass: auto-completed opponent {waiting_pid}'s pass "
                f"(no opponent stop on phase {state['phase']})")

    # ── both players have passed ──────────────────────────────────────
    old_phase = state["phase"]
    import battle_engine as _be
    # Decide the phase list for this turn: combat steps only when the turn
    # player controls a ready troop (mirrors build_turn_phases).  CRITICAL:
    # once we are PAST the first combat phase (>= DeclareAttack=12) we must
    # stay on COMBAT_TURN_PHASES even if the attacker's troops are now tapped
    # (they declared attacks) — otherwise passing DeclareDefense wraps to a NEW
    # turn (phase 7) instead of resolving Swiftstrike/AssignDamage damage, so
    # combat is skipped, the champion takes no damage, and a fresh turn starts.
    turn_pid = state.get("turn_pid")
    old_phase = int(old_phase)
    if old_phase >= _ge.ETurnPhases.DeclareAttack \
            and old_phase != _ge.ETurnPhases.SecondMainPhase:
        has_ready = True
    else:
        has_ready = pvp_turn_has_attackers(session, turn_pid)
    phase_list = (_be.COMBAT_TURN_PHASES if has_ready
                  else _be.BASE_TURN_PHASES)
    try:
        cur_idx = phase_list.index(old_phase)
    except ValueError:
        cur_idx = 0
    # The client chooses the next combat phase only after Declare Blockers has
    # completed and the DeclareDefensePriorityWindow response window has
    # closed.  Evaluate the live combat here so a Quick Action that grants
    # Swiftstrike to an attacker or blocker is included.
    if old_phase == _ge.ETurnPhases.DeclareDefensePriorityWindow:
        new_phase = pvp_phase_after_blockers(session, state)
        next_idx = phase_list.index(new_phase)
    else:
        next_idx = cur_idx + 1
    if next_idx >= len(phase_list):
        pids_ = db_game_session_pids(session.session_id)
        # EndTurn passed: fire "At the end of your turn" triggers for the
        # outgoing turn player, then switch the turn player, wrap to StartTurn.
        # Mirrors PvE (hconnect ~3266): TurnEndedEvent + temporary_attributes
        # expiration + warzone re-push.
        try:
            end_h = player_handlers.get(turn_pid)
            if end_h:
                end_opp = pids_[0] if pids_[1] == turn_pid else pids_[1]
                end_opp_uid = _ge.UID.make(244, end_opp)
                end_uid = _ge.UID.make(244, turn_pid)
                eg = _ge.Game(int(session.session_id), end_uid, end_opp_uid)
                eg.player_health = int(state.get(f"hp_{turn_pid}", 20))
                eg.ai_health = int(state.get(f"hp_{end_opp}", 20))
                from abilities.framework.triggers import resolve_triggers
                if not state.get("turn_end_trigger_fired"):
                    resolve_triggers(
                        _db, end_h, eg, session, end_uid, end_opp_uid,
                        _pvp_fra_view(state, turn_pid, end_opp),
                        "TurnEndedEvent", None, turn_pid)
                    if state.get("stack"):
                        # Hold the current EndTurn until its triggered ability
                        # resolves.  Otherwise the next turn begins with the
                        # trigger still on the stack and a token such as Blaze
                        # Elemental is sacrificed during the next First Main.
                        state["turn_end_trigger_fired"] = True
                # Combat damage and "until end of turn" attributes expire at
                # cleanup, not at the next turn's Prep.  Clear every warzone
                # card because combat can damage either player's troops.
                from abilities.framework._shared import (
                    clear_combat_damage, clear_expired_temporary_attributes)
                clear_combat_damage(_db, session.session_id)
                clear_expired_temporary_attributes(
                    _db, session.session_id, turn_pid, "end_turn",
                    clear_stat_buffs=True)
                # CardUpdated rebuilds the CardDef after card_damage is
                # cleared, so both clients immediately see healed defense.
                pvp_push_warzone_updates(session, state)
                ev_view = _pvp_fra_view(state, turn_pid, end_opp)
                if ev_view.get("player_health") is not None:
                    state[f"hp_{turn_pid}"] = int(ev_view["player_health"])
                if ev_view.get("ai_health") is not None:
                    state[f"hp_{end_opp}"] = int(ev_view["ai_health"])
                pvp_save_state(session, state)
                if eg.events:
                    _pvp_send_same_events(session, eg, end_uid, end_opp_uid)
                log_req(f"    PvP TurnEndedEvent fired for {turn_pid}")
                if state.get("stack"):
                    state["passes"] = []
                    state["priority_pid"] = turn_pid
                    pvp_save_state(session, state)
                    end_uid = _ge.UID.make(244, turn_pid)
                    end_opp_uid = _ge.UID.make(244, end_opp)
                    eg_priority = _ge.Game(
                        int(session.session_id), end_uid, end_opp_uid)
                    eg_priority.push_green_light(
                        end_uid, _ge.EPriorityContext.ResolveTopOfChain)
                    _send_pvp_packet(end_h, session, eg_priority, end_uid,
                                     "turn-ended-chain")
                    pvp_push_phase_options(session, state, pid=turn_pid)
                    log_req(f"    PvP EndTurn chain held priority for "
                            f"{turn_pid}; turn remains at phase {old_phase}")
                    return True
        except Exception as e:
            import traceback
            log_req(f"    PvP TurnEndedEvent error: {e}")
            traceback.print_exc()
        # Fresh turn: reset the "resource already played this turn" flag for
        # both players (the new turn player can play one again).
        # F10 EndOfTurn belongs only to the outgoing turn.  If it leaks across
        # the boundary, the next turn can skip FirstMain/DeclareAttack stops
        # and appear to jump straight into combat.
        state.pop("autopass_pid", None)
        state.pop("autopass_state", None)
        state.pop("turn_end_trigger_fired", None)
        bonus_pid = int(state.pop("bonus_turn_pid", 0) or 0)
        if bonus_pid in pids_:
            state["turn_pid"] = bonus_pid
            log_req(f"    PvP: bonus turn for {bonus_pid}")
        else:
            state["turn_pid"] = pids_[0] if pids_[1] == turn_pid else pids_[1]
        state["turn_number"] = int(state.get("turn_number", 1)) + 1
        state.pop("damaged_opponent_this_turn", None)
        state.pop("damaged_opponent_turn", None)
        state.pop("attackers", None)
        state.pop("blockers", None)
        for _pid in pids_:
            state[f"res_played_{_pid}"] = 0
        next_idx = 0
        new_phase = phase_list[0]
    elif old_phase != _ge.ETurnPhases.DeclareDefensePriorityWindow:
        new_phase = phase_list[next_idx]
    state["phase"] = new_phase
    state["passes"] = []
    pvp_save_state(session, state)
    log_req(f"    PvP: both passed phase {old_phase} → {new_phase} "
            f"(idx {cur_idx}->{next_idx} of {len(phase_list)}, "
            f"list={'C' if has_ready else 'B'}, turn={state.get('turn_pid')})")

    # Leaving AssignDamage resolves the declared combat — through the SAME
    # shared resolver the AI path uses, then events go to both players.
    # Leaving AssignFirstStrikeDamage resolves the Swiftstrike step first
    # (only FirstStrike/DualStrike combatants deal; casualties removed before
    # the normal step) — mirrors PvE's two-step resolution.
    if old_phase == _ge.ETurnPhases.AssignFirstStrikeDamage:
        _pvp_resolve_combat(session, state, first_strike=True)
    elif old_phase == _ge.ETurnPhases.AssignDamage:
        _pvp_resolve_combat(session, state, first_strike=False)
    # A champion may have died from combat or a lingering effect — end the
    # game before pushing any further phase.
    if _pvp_check_game_end(session, state):
        return True

    # Push the new phase to BOTH players — _pvp_run_phase_start sends the
    # TurnPhase + GreenLight together in one packet each (greenlight first so
    # the client holds priority when it processes the phase; a phase event
    # naming the player as priority without a greenlight triggers a spurious
    # RequestPrioritySync + state-stack churn).
    _pvp_run_phase_start(session, state, new_phase)
    # Auto-pass non-stop phases (Ready/Prep/Draw...) — but NEVER a phase that
    # either player has configured a stop on (self-stop for the turn player or
    # opponent-stop for the opponent); those wait for the both-pass cycle.
    # Every new turn starts at StartTurn, so this also marches each turn to
    # its first real interaction.
    pvp_advance_past_non_stops(session, state)
@_pvp_locked
def pvp_concede(handler, session):
    """End a tournament PvP game when one player explicitly concedes."""
    if not session or not (session.session_name or "").startswith("tourney-"):
        return False
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    try:
        loser_pid = int(handler.client_reck_id)
    except (AttributeError, TypeError, ValueError):
        return False
    if loser_pid not in pids:
        return False
    winner_pid = pids[0] if pids[1] == loser_pid else pids[1]
    state = pvp_load_state(session)
    _pvp_end_game(session, state, winner_pid, loser_pid, "player conceded")
    return True


def _pvp_transaction_card_uids(inner_bytes):
    """Extract Card SessionCardIds from a client transaction."""
    if not isinstance(inner_bytes, bytes):
        return []
    import struct
    out = []
    for match in re.finditer(
            rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
            inner_bytes):
        try:
            uid = struct.unpack('<Q', bytes.fromhex(match.group(1).decode()))[0]
            if (uid & 0xFF) == 1:
                out.append(int(uid))
        except (TypeError, ValueError, struct.error):
            continue
    return out


@_pvp_locked
def pvp_handle_discard(handler, session, inner_bytes):
    """Resolve one normal hand-discard transaction in tournament PvP.

    This must stay out of HCPHandler's human-vs-AI discard path: that path
    loads a battle_engine state and can replace the persisted PvP state with a
    Practice-style turn, which is how a discard previously jumped back to
    First Main and could produce a false deck-out victory.
    """
    if not session or not (session.session_name or '').startswith('tourney-'):
        return False
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    my_pid = int(handler.client_reck_id)
    state = pvp_load_state(session) or {}
    if (int(state.get('phase', -1)) != _ge.ETurnPhases.Discard or
            int(state.get('turn_pid', -1)) != my_pid):
        log_req(f"    PvP discard rejected: pid {my_pid} phase="
                f"{state.get('phase')} turn={state.get('turn_pid')}")
        return True

    card_uids = _pvp_transaction_card_uids(inner_bytes)
    card_uid = card_uids[-1] if card_uids else None
    row = None
    if card_uid is not None:
        row = _db.execute(
            "SELECT user_id, COALESCE(owner_user_id, user_id), "
            "template_guid, card_template_id "
            "FROM game_cards WHERE session_id=? AND card_uid=? "
            "AND user_id=? AND location='hand'",
            (session.session_id, card_uid, my_pid)).fetchone()
    if not row:
        log_req(f"    PvP discard ignored: no hand card for pid {my_pid} "
                f"uid={card_uid}")
        return True

    card_controller, card_owner, tpl_guid, instance_id = row
    # PvP owners are ServicePlayer ids.  A stolen card still returns to its
    # original owner's discard, but never let malformed ownership point an
    # event at the Practice AI UID.
    owner_pid = int(card_owner)
    if owner_pid not in pids:
        owner_pid = my_pid
    db_discard_card(session.session_id, card_uid, owner_user_id=owner_pid,
                    connection=_db)

    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    my_uid = _ge.UID.make(244, my_pid)
    opp_uid = _ge.UID.make(244, opp_pid)
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    _pvp_populate_game_state(g, state, my_pid, opp_pid)
    scid = _ge.SessionCardId(_ge.UID(card_uid))
    owner_uid = _ge.UID.make(244, owner_pid)
    h_card = player_handlers.get(my_pid) or handler
    if h_card:
        _tpl, ct, _name, cost, atk, defense, gems = h_card._card_full_data(
            g, scid, tpl_guid, instance_id)
        g.push_card_updated(
            scid, owner_uid, _ge.ECardCollections.Discard,
            ct, attack=atk, defense=defense, cost=cost,
            template_id=_tpl, gems=gems)
    g.push_card_moved(scid, owner_uid, _ge.ECardCollections.Discard,
                      _ge.ECardLocations.Top, 0)

    # A crypt-entry trigger may legitimately put an item on the chain.  Keep
    # it on the shared PvP state and broadcast its events just like a normal
    # card resolution.
    view = _pvp_fra_view(state, owner_pid, opp_pid)
    try:
        from abilities.framework.triggers import resolve_triggers
        resolve_triggers(_db, h_card, g, session,
                         _ge.UID.make(244, owner_pid),
                         _ge.UID.make(244, opp_pid), view,
                         "CardEnteredZoneEvent", card_uid, owner_pid)
    except Exception as exc:
        log_req(f"    PvP discard trigger error: {exc}")
    _pvp_sync_view_to_state(state, view, owner_pid, opp_pid)
    state["stack"] = view.get("stack") or state.get("stack") or []
    state["stack_passed"] = []
    state["priority_pid"] = my_pid
    pvp_save_state(session, state)
    _pvp_send_same_events(session, g, my_uid, opp_uid)

    hand_count = _db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='hand'", (session.session_id, my_pid)).fetchone()[0]
    log_req(f"    PvP discarded card {card_uid} (hand={hand_count})")
    if state.get("stack"):
        for pid in pids:
            h = player_handlers.get(pid)
            if not h:
                continue
            recipient = _ge.UID.make(244, pid)
            other = _ge.UID.make(244, pids[0] if pids[1] == pid else pids[1])
            gp = _ge.Game(int(session.session_id), recipient, other)
            gp.push_green_light(my_uid, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(h, session, gp, recipient, "discard-chain")
        pvp_push_phase_options(session, state, pid=my_pid)
    elif hand_count > 7:
        gp = _ge.Game(int(session.session_id), my_uid, opp_uid)
        gp.push_green_light(my_uid, _ge.EPriorityContext.Normal)
        _send_pvp_packet(handler, session, gp, my_uid, "discard-more")
    else:
        # A successful final discard is equivalent to the turn player passing
        # the Discard phase.  route_pvp_pass performs the normal cleanup and
        # phase transition while preserving the PvP state machine.
        route_pvp_pass(handler, session)
    return True


@_pvp_locked
def pvp_debug_draw(handler, session, count):
    """Draw debug cards in PvP and send private/objective variants to both clients."""
    if not session or not (session.session_name or '').startswith('tourney-'):
        return 0
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return 0
    owner_pid = int(handler.client_reck_id)
    if owner_pid not in pids:
        return 0
    opp_pid = pids[0] if pids[1] == owner_pid else pids[1]
    owner_uid = _ge.UID.make(244, owner_pid)
    opponent_uid = _ge.UID.make(244, opp_pid)
    drawn_count = 0
    for _ in range(max(0, int(count))):
        top = _db.execute(
            "SELECT card_uid, template_guid, card_template_id "
            "FROM game_cards WHERE session_id=? AND user_id=? "
            "AND location='deck' ORDER BY position LIMIT 1",
            (session.session_id, owner_pid)).fetchone()
        if not top:
            break
        card_uid, tpl_guid, instance_id = top
        g = _ge.Game(int(session.session_id), owner_uid, opponent_uid)
        handler._player_draw_card(g, session, owner_uid, owner_pid)
        loc = _db.execute(
            "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(card_uid))).fetchone()
        if loc and loc[0] == 'hand':
            drawn_count += 1
        if not g.events:
            continue
        g2 = _ge.Game(int(session.session_id), owner_uid, opponent_uid)
        g2.events = []
        g2.card_defs = dict(g.card_defs)
        scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
        _tpl2, ct2, _name2, _cost2, _atk2, _def2, _gems2 = \
            handler._card_full_data(g2, scid, tpl_guid, instance_id)
        g2.push_card_moved(scid, owner_uid, _ge.ECardCollections.Hand,
                           _ge.ECardLocations.Top, 1)
        g2.push_card_updated(scid, owner_uid, _ge.ECardCollections.Hand,
                             ct2, template_id=_tpl2, nulling=True)
        for event in g.events:
            if (getattr(event, 'session_card_id', None) == scid and
                    event.__class__.__name__ in (
                        'CardMovedSessionEventArgs',
                        'CardDrawnSessionEventArgs',
                        'CardUpdatedSessionEventArgs')):
                continue
            g2._push(event)
        _send_pvp_packet(handler, session, g, owner_uid, 'debug-draw')
        other_h = player_handlers.get(opp_pid)
        if other_h:
            _send_pvp_packet(other_h, session, g2, opponent_uid,
                             'debug-draw-opponent')

    state = pvp_load_state(session) or {}
    if state.get('stack'):
        state['priority_pid'] = owner_pid
        state['stack_passed'] = []
        pvp_save_state(session, state)
        for pid in pids:
            h = player_handlers.get(pid)
            if not h:
                continue
            recipient = _ge.UID.make(244, pid)
            other = _ge.UID.make(244, pids[0] if pids[1] == pid else pids[1])
            gp = _ge.Game(int(session.session_id), recipient, other)
            gp.push_green_light(owner_uid,
                                _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(h, session, gp, recipient, 'debug-draw-priority')
        pvp_push_phase_options(session, state, pid=owner_pid)
    elif int(state.get('phase', -1)) in (
            _ge.ETurnPhases.FirstMainPhase, _ge.ETurnPhases.SecondMainPhase):
        pvp_push_main_phase_options(session, state)
    return drawn_count


@_pvp_locked
def pvp_handle_transaction(handler, session, inner_bytes):
    """Handle a PvP game transaction (card play, ability use, combat).
    Applies the action server-side and pushes events to BOTH players.
    Returns True if handled."""
    if not isinstance(inner_bytes, bytes):
        return False
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    my_pid = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0
    # A class-39 answer (deck search, revealed-card choice, or Shards of
    # Fate) is named SetAbilityActivationDataTransaction in the client model,
    # but the serialized transaction contains only AbilityActivationData.
    # Route both spellings before generic ability activation; otherwise the
    # picker response falls through and is misread as a champion activation.
    pending_state = pvp_load_state(session) or {}
    if pending_state.get("pending_choice") and b"m_UID64" in inner_bytes:
        return _pvp_resolve_choice(handler, session, inner_bytes, my_pid)
    is_ability_data = b"AbilityActivationData" in inner_bytes
    if (b"SetAbilityActivationDataTransaction" in inner_bytes or
            (is_ability_data and
             (pending_state.get("pending_trigger") or
              pending_state.get("pending_deck_search") or
              pending_state.get("pending_discard_ability")))):
        state = pending_state
        if state.get("pending_discard_ability"):
            return _pvp_resolve_discard_prompt(
                handler, session, inner_bytes, my_pid)
        if state.get("pending_trigger"):
            return _pvp_resolve_trigger_target(handler, session, inner_bytes,
                                               my_pid)
        if (state.get("pending_deck_search") or {}).get("kind") == \
                "revealed_troop":
            return _pvp_resolve_revealed_choice(handler, session,
                                                inner_bytes, my_pid)
        if (state.get("pending_deck_search") or {}).get("kind") == "shard":
            return _pvp_resolve_shard_choice(handler, session, inner_bytes,
                                             my_pid)
        return _pvp_resolve_deck_search(handler, session, inner_bytes, my_pid)
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    my_uid = _ge.UID.make(244, my_pid)
    opp_uid = _ge.UID.make(244, opp_pid)

    # Combat declarations arrive as transactions during the combat phases and
    # carry no m_SessionCardId — route them BEFORE the card-play parsing.
    if b"CommitTroopsToAttackTransaction" in inner_bytes:
        return _pvp_declare_attackers(handler, session, inner_bytes, my_pid)
    if b"CommitTroopsToDefenseTransaction" in inner_bytes:
        return _pvp_declare_blockers(handler, session, inner_bytes, my_pid)
    # The client AUTO-sends AssignDamageOrderTransaction when it enters the
    # AssignDamage / AssignFirstStrikeDamage steps (BattleStateAssignDamage
    # auto-commits with no blockers).  Combat damage in PvP is resolved by
    # _pvp_resolve_combat when both players pass AssignDamage — this
    # transaction carries no card to play, so just consume it (ack) instead of
    # letting it fall into the human-vs-AI fallback, which would load a
    # battle_engine state and CLOBBER the PvP turn_order (game drops into the
    # AI path and someone gets a bogus victory screen).
    if b"AssignDamageOrderTransaction" in inner_bytes:
        # The transaction carries the attacker's chosen blocker order
        # (weakest-to-toughest, m_AssignedDamageOrder -> DamageAssignment
        # CombatId/ordered CardIds).  Store it on the PvP state so
        # _pvp_resolve_combat passes it to ai.resolve_combat as order_map
        # (mirrors PvE hconnect 9224-9250).  Combat damage itself resolves on
        # the phase pass.
        state = pvp_load_state(session) or {}
        try:
            import struct as _st
            seq = []
            for m_du in re.finditer(
                    rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                    inner_bytes):
                v = _st.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                if (v & 0xFF) == 1:
                    seq.append(int(v))
            blockers = {int(k): set(int(b) for b in (v or []))
                        for k, v in (state.get("blockers") or {}).items()}
            order_map = {}
            for att, bset in blockers.items():
                ordered = [u for u in seq if u in bset]
                ordered += [b for b in bset if b not in ordered]
                if ordered:
                    order_map[att] = ordered
            if order_map:
                state["damage_order"] = {str(k): [str(b) for b in v]
                                         for k, v in order_map.items()}
                pvp_save_state(session, state)
                log_req(f"    PvP AssignDamageOrder: stored blocker order "
                        f"for {len(order_map)} combat(s) — pid {my_pid}")
            else:
                log_req(f"    PvP AssignDamageOrder consumed (no blocker "
                        f"order parsed) — pid {my_pid}")
        except Exception as e:
            log_req(f"    PvP AssignDamageOrder parse error: {e}")
        # The client auto-submits AssignDamageOrder when it enters the
        # damage-assignment step.  With the order (or lack of blockers)
        # recorded, resolve the damage step immediately instead of waiting for
        # a further manual pass — the attacker's client enters
        # BattleStateAssignDamage and, with no blockers to order, has nothing
        # left to do, so it would otherwise hang there forever.
        try:
            cur_ph = int(state.get("phase", 0))
            if cur_ph in (_ge.ETurnPhases.AssignFirstStrikeDamage,
                          _ge.ETurnPhases.AssignDamage):
                _pvp_resolve_combat(
                    session, state,
                    first_strike=(cur_ph == _ge.ETurnPhases.AssignFirstStrikeDamage))
                # Advance past the just-resolved damage step to the next combat
                # phase (Swiftstrike -> AssignDamage, AssignDamage -> SecondMain).
                _pvp_advance_from_damage_step(session, state, cur_ph)
        except Exception as e:
            import traceback
            log_req(f"    PvP AssignDamageOrder auto-resolve error: {e}")
            traceback.print_exc()
        return True
    # Ability activation (ActivateAbilityTransaction): extract the ability
    # GUID; if it belongs to a warzone troop the player controls, activate the
    # troop's manual ability (Shift etc.); otherwise it's the champion's
    # charge/spell power.
    if b"m_AbilityActivationData" in inner_bytes:
        import re as _rre2
        ability_guid = None
        m = _rre2.search(
            rb'AbilityTemplateId;[^;]*;[^;]*;[^;]*;[^;]*;[^;]*;[^;]*;'
            rb'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
            rb'[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', inner_bytes)
        if not m:
            aidx = inner_bytes.find(b"AbilityTemplateId")
            if aidx >= 0:
                m2 = _rre2.search(
                    rb'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                    rb'[0-9a-fA-F]{4}-[0-9a-fA-F]{12})',
                    inner_bytes[aidx:aidx + 300])
                if m2:
                    m = m2
        if m:
            ability_guid = m.group(1).decode().lower()
        if ability_guid:
            champ_owned = _db.execute(
                "SELECT 1 FROM talent_abilities WHERE ability_guid=? "
                "LIMIT 1", (ability_guid,)).fetchone()
            src_row = None
            if not champ_owned:
                # Multiple copies share the same ability GUID.  The first
                # card UID in an activation transaction is its source card;
                # do not route every copy to the first matching warzone row.
                card_uids = _pvp_transaction_card_uids(inner_bytes)
                if card_uids:
                    src_row = _db.execute(
                        "SELECT card_uid FROM game_cards "
                        "WHERE session_id=? AND user_id=? AND location='warzone' "
                        "AND card_uid=? AND card_abilities LIKE ?",
                        (session.session_id, my_pid, int(card_uids[0]),
                         f'%"{ability_guid}"%')).fetchone()
                if src_row is None and not card_uids:
                    # Preserve the unambiguous single-copy case for clients
                    # that omit the source SessionCardId, but never guess
                    # between duplicate ability instances.
                    matches = _db.execute(
                        "SELECT card_uid FROM game_cards "
                        "WHERE session_id=? AND user_id=? AND location='warzone' "
                        "AND card_abilities LIKE ?",
                        (session.session_id, my_pid,
                         f'%"{ability_guid}"%')).fetchall()
                    if len(matches) == 1:
                        src_row = matches[0]
            if src_row:
                return _pvp_activate_troop_ability(
                    handler, session, inner_bytes, my_pid,
                    ability_guid, int(src_row[0]))
        return _pvp_activate_champion_ability(handler, session, inner_bytes,
                                              my_pid)

    # Extract played card UID from the transaction.
    played_card_uid = None
    scid_pos = inner_bytes.find(b"m_SessionCardId")
    if scid_pos >= 0:
        uid_pos = inner_bytes.find(b"m_UID64", scid_pos)
        if uid_pos >= 0:
            rest = inner_bytes[uid_pos + 7:]
            parts = rest.split(b";", 6)
            if len(parts) >= 4:
                try:
                    import struct
                    hex_val = parts[4].decode("ascii", errors="replace")
                    played_card_uid = struct.unpack('<Q', bytes.fromhex(hex_val))[0]
                except Exception:
                    pass
    if not played_card_uid:
        return False

    # Look up the card in DB.
    crow = _db.execute(
        "SELECT gc.template_guid, ct.card_type, ct.name, "
        "ct.current_resources_granted, ct.max_resources_granted, "
        "ct.abilities_json FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session.session_id, int(played_card_uid))).fetchone()
    if not crow:
        return False
    card_type = crow[1]
    card_name = crow[2]
    is_resource = (card_type == 'Resource')

    if not is_resource:
        # Route by permanence: troops/artifacts/constants resolve to the
        # warzone; BasicAction/QuickAction spells go onto the chain and
        # resolve their BOM (a player may cast a QuickAction any time they
        # hold priority and can pay the cost).
        ctype_num = _ge.card_type_from_db(card_type)
        try:
            if ctype_num & (_ge.ECardTypes.BasicAction | _ge.ECardTypes.QuickAction):
                return _pvp_play_spell(handler, session, played_card_uid,
                                       my_pid, inner_bytes)
            return _pvp_play_troop(handler, session, played_card_uid, my_pid)
        except Exception as e:
            # Never let a card-play crash kill the session thread and
            # disconnect both clients — log and ack so the game keeps going.
            import traceback
            log_req(f"    PvP play exception ({card_name}): {e}")
            _tb = traceback.format_exc()
            for _tl in _tb.splitlines():
                log_req(f"    PvP play TB: {_tl}")
            return True

    # ── resource play ────────────────────────────────────────────────
    _db.execute("UPDATE game_cards SET location='PlayedResources', position=9999 "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, int(played_card_uid)))
    _db.commit()
    log_req(f"    PvP resource play: {card_name} by pid {my_pid}")

    # Resource templates carry their own current/maximum grants.  Most basic
    # shards grant both, while Shards of Fate grants only maximum resources
    # and then asks the player to choose a Standard resource for its threshold.
    current_grant = int(crow[3] or 0)
    max_grant = int(crow[4] or 0)
    if not current_grant and not max_grant:
        # Keep old/imported resource rows playable while the data migration is
        # being applied; normal Set 1 rows have explicit values.
        current_grant = max_grant = 1
    shard_ability = shard_tpl = None
    if crow[5]:
        try:
            ability_guids = json.loads(crow[5])
        except Exception:
            ability_guids = []
        shard_ability, shard_tpl = handler._shards_of_fate_template(
            ability_guids)
    is_shards_of_fate = bool(shard_tpl)

    # Track resources, threshold, and champion charge in PvP state.  Shards of
    # Fate is excluded only from the ordinary-shard threshold path; its
    # selected deck card supplies the threshold after the prompt resolves.
    state = pvp_load_state(session) or {}
    key = f"res_{my_pid}"
    state[key] = state.get(key, 0) + current_grant
    state[f"res_total_{my_pid}"] = state.get(f"res_total_{my_pid}", 0) + max_grant
    state[f"res_played_{my_pid}"] = 1
    # Resource charge generation is defined by the card's BOM.  Do not add a
    # universal +1 here: Set 1 shards already contain a gain-one-charge leaf.
    charge_grant = _pvp_resource_charge_points(session, played_card_uid)
    state[f"chg_{my_pid}"] = state.get(f"chg_{my_pid}", 0) + charge_grant
    # A normal resource can fire GainChargeEvent immediately.  Shards of Fate
    # has a nested deck choice below, so defer its trigger until that choice
    # has completed and the picker is no longer active.
    charge_trigger_game = None
    if charge_grant and not is_shards_of_fate:
        charge_trigger_game = _pvp_gain_charge_trigger_game(
            handler, session, state, my_pid)
    elif charge_grant:
        state["pending_gain_charge_pid"] = my_pid
    # Threshold colour from the shard name ("Ruby Shard" -> Ruby=8).
    shard_color = None
    col_map = {'Ruby': _ge.ECardShards.Ruby, 'Sapphire': _ge.ECardShards.Sapphire,
               'Blood': _ge.ECardShards.Blood, 'Diamond': _ge.ECardShards.Diamond,
               'Wild': _ge.ECardShards.Wild}
    if card_name:
        shard_color = col_map.get(card_name.split()[0])
    thresh_key = f"thresh_{my_pid}"
    thresh = dict(state.get(thresh_key) or {})
    if shard_color and not is_shards_of_fate:
        # Keys become strings after the JSON round-trip through the DB, so
        # look up BOTH the int and string forms — otherwise a second same-color
        # shard reads 0 (int 8 vs str '8') and the threshold never exceeds 1.
        cur = thresh.get(shard_color)
        if cur is None:
            cur = thresh.get(str(shard_color), 0)
        thresh[shard_color] = int(cur or 0) + 1
    state[thresh_key] = thresh
    pvp_save_state(session, state)
    # The resource is now played — refresh the turn player's options so the
    # second shard no longer highlights.
    if (not is_shards_of_fate and not state.get("stack") and
            state.get("phase") in (_ge.ETurnPhases.FirstMainPhase,
                                    _ge.ETurnPhases.SecondMainPhase)):
        pvp_push_main_phase_options(session, state)
    champ_map = state.get("champ_map", {})

    # Push card events + resource/threshold/charge/PlayerUpdated for BOTH
    # players in one packet each.
    for pid in pids:
        h = player_handlers.get(pid)
        if not h:
            continue
        is_me = (pid == my_pid)
        pl_uid = my_uid if is_me else opp_uid
        other_uid = opp_uid if is_me else my_uid
        g = _ge.Game(int(session.session_id), pl_uid, other_uid)
        _pvp_populate_game_state(
            g, state, pid, pids[1] if pid == pids[0] else pids[0])
        scid = _ge.SessionCardId(_ge.UID(int(played_card_uid)))
        # Real health/resource values from the PvP state (a bare Game defaults
        # to 20/20 and 0/0, which made every client show its own champion
        # gain 1 health and wiped the resource bar).
        g.player_health = int(state.get(f"hp_{pid}", 20))
        g.ai_health = int(state.get(f"hp_{pids[1] if pid == pids[0] else pids[0]}", 20))
        g.player_resources = int(state.get(f"res_{pid}", 0))
        g.player_total_resources = int(state.get(f"res_total_{pid}", 0))
        g.ai_resources = int(state.get(f"res_{pids[1] if pid == pids[0] else pids[0]}", 0))
        g.ai_total_resources = int(state.get(f"res_total_{pids[1] if pid == pids[0] else pids[0]}", 0))
        g.player_charges = int(state.get(f"chg_{pid}", 0))
        g.ai_charges = int(state.get(f"chg_{pids[1] if pid == pids[0] else pids[0]}", 0))

        # Rebuild the instance definition so the client retains any current
        # ability list (including a granted Gain-a-charge ability).
        _rtpl, rct, _rn, rcost, ratk, rdef, _rgem = \
            handler._card_full_data(g, scid, crow[0])
        g.push_card_updated(scid, my_uid, _ECardCollections.PlayedResources,
                            rct, template_id=_rtpl, cost=rcost,
                            attack=ratk, defense=rdef, nulling=False)
        g.push_resource_card_played(scid, my_uid, free=False)
        my_uid_p = _ge.UID.make(244, my_pid)
        # Current + total resource pool display.
        if current_grant:
            ev_cur = _ge.PlayerCurrentResourcePoolChangedSessionEventArgs()
            ev_cur.player_id = my_uid_p
            ev_cur.operation = 1
            ev_cur.delta = current_grant
            ev_cur.new_value = int(state.get(f"res_{my_pid}", 0))
            g._push(ev_cur)
        if max_grant:
            ev_tot = _ge.PlayerTotalResourcePoolChangedSessionEventArgs()
            ev_tot.player_id = my_uid_p
            ev_tot.operation = 1
            ev_tot.delta = max_grant
            ev_tot.new_value = int(state.get(f"res_total_{my_pid}", 0))
            g._push(ev_tot)
        # Threshold gem for the played shard's colour.
        if shard_color and not is_shards_of_fate:
            ev_th = _ge.PlayerResourceThresholdChangedSessionEventArgs()
            ev_th.player_id = my_uid_p
            ev_th.color = shard_color
            ev_th.operation = 1
            ev_th.delta = 1
            ev_th.new_value = int(thresh.get(shard_color, 0))
            g._push(ev_th)
        # Champion charge generated by the resource's BOM.
        ev_chg = _ge.ChampionChargePointsChangedSessionEventArgs()
        ev_chg.player_id = my_uid_p
        ev_chg.operation = 1
        ev_chg.delta = charge_grant
        ev_chg.new_value = int(state.get(f"chg_{my_pid}", 0))
        g._push(ev_chg)
        if charge_trigger_game:
            for trigger_event in charge_trigger_game.events:
                g._push(trigger_event)
        # PlayerUpdated for both — health / charges / resources.
        for target_pid in pids:
            target_uid = _ge.UID.make(244, target_pid)
            cu = int(champ_map.get(str(target_pid), 0))
            champ_scid = _ge.SessionCardId(_ge.UID(cu)) if cu else None
            g.push_player_updated(target_uid, champ_id=champ_scid)

        if g.events:
            try:
                _cls2 = [getattr(type(_e), "CLASS_ID", 0) for _e in g.events]
                log_req(f"    PvP resource-audit -> pid {pid}: "
                        f"classes={_cls2}")
            except Exception as _e2:
                log_req(f"    PvP resource-audit error: {_e2}")
            pkt = g.make_network_packet(pl_uid)
            dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                     client_session_guid(h))
            h.scnt += 1
            h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
                    "target": "ServiceGameSession", "instance": str(session.server_id),
                    "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
            log_req(f"    PvP resource: pushed to pid {pid}")
    if (charge_trigger_game and charge_trigger_game.events
            and state.get("stack")):
        # The resource packet above contains the charge event and the
        # triggered ability entry.  Give the opponent the first response
        # window, matching permanent/spell plays already on the PvP chain.
        _pvp_offer_trigger_response(session, state, my_pid)
        return True
    if is_shards_of_fate:
        # Resource events must arrive before the class-39 deck picker.  The
        # picker itself re-grants priority to the chooser, so do not send the
        # ordinary post-card greenlight here.
        state["priority_pid"] = my_pid
        pvp_save_state(session, state)
        prompt_game = _ge.Game(int(session.session_id), my_uid, opp_uid)
        _pvp_populate_game_state(prompt_game, state, my_pid, opp_pid)
        result = handler._resolve_shards_of_fate(
            prompt_game, session, my_uid, opp_uid, state,
            int(played_card_uid), shard_ability, shard_tpl, my_pid)
        if "awaiting" in str(result):
            log_req(f"    PvP Shards of Fate: awaiting threshold choice "
                    f"for pid {my_pid}")
            return True
        # No eligible Standard resource remained.  Resume priority rather
        # than leaving the turn waiting for a prompt that was not sent.
        state["priority_pid"] = my_pid
        pvp_save_state(session, state)

        # No picker remains, so a charge trigger deferred above can now be
        # put on the shared PvP chain and offered to the opponent.
        if state.pop("pending_gain_charge_pid", None) == my_pid:
            charge_trigger_game = _pvp_gain_charge_trigger_game(
                handler, session, state, my_pid)
            if charge_trigger_game and charge_trigger_game.events:
                _pvp_send_same_events(
                    session, charge_trigger_game, my_uid, opp_uid)
                if state.get("stack"):
                    _pvp_offer_trigger_response(session, state, my_pid)
                    return True

    # The client clears its LOCAL greenlight after playing a card
    # (BattleStatePlayCard.LoseGreenLight) — the server must re-grant
    # priority to the turn player or nobody can act/pass afterwards.
    turn_h = player_handlers.get(my_pid)
    if turn_h:
        gg = _ge.Game(int(session.session_id), my_uid, opp_uid)
        gg.push_green_light(my_uid, _ge.EPriorityContext.Normal)
        _send_pvp_packet(turn_h, session, gg, my_uid, "greenlight-after-play")
    state["priority_pid"] = my_pid
    pvp_save_state(session, state)
    return True


def _pvp_resolve_choice(handler, session, inner_bytes, my_pid):
    """Resolve a private ChooseAndPlay choice and resume its parent BOM."""
    import battle_engine as _be
    from abilities.framework.effects.choices import (
        CHOOSE_AND_PLAY_ABILITY, extract_card_uids, play_choice_card,
        resolve_choice_card_abilities)
    state = pvp_load_state(session) or {}
    pending = state.get("pending_choice")
    if not pending:
        return False
    selected = extract_card_uids(inner_bytes)
    legal = {int(uid) for uid in pending.get("choice_uids", [])}
    chosen_uid = next((uid for uid in reversed(selected) if int(uid) in legal),
                      None)
    owner_id = int(pending.get("owner_id", 0))
    if owner_id != int(my_pid) or chosen_uid is None:
        log_req(f"    PvP choice answer invalid: pid={my_pid} "
                f"chosen={chosen_uid} owner={owner_id}")
        return True

    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return True
    opponent_id = next(pid for pid in pids if int(pid) != owner_id)
    pl_t = _ge.UID.make(244, owner_id)
    ai_t = _ge.UID.make(244, opponent_id)
    view = _pvp_fra_view(state, owner_id, opponent_id)
    view.pop("pending_choice", None)
    view.pop("resolution_paused", None)
    g = _ge.Game(int(session.session_id), pl_t, ai_t)
    _pvp_populate_game_state(g, state, owner_id, opponent_id)
    if not play_choice_card(g, session, _db, handler, pl_t, ai_t, view,
                            chosen_uid, owner_id):
        log_req(f"    PvP choice card no longer selectable: {chosen_uid}")
        state["pending_choice"] = pending
        state["resolution_paused"] = True
        pvp_save_state(session, state)
        return True
    resolve_choice_card_abilities(
        g, session, _db, handler, pl_t, ai_t, view, chosen_uid,
        pending.get("source_uid"), owner_id)

    from abilities.framework.resolution import resolve_ability
    target_map = {int(key): value for key, value in
                  (pending.get("target_map") or {}).items()}
    resolve_ability(
        handler, g, session, _db, pl_t, ai_t, view,
        pending["ability_guid"], pending.get("source_uid"), owner_id,
        target_map=target_map, variables=pending.get("variables") or {},
        resume_from_order=int(pending.get("resume_effect_order", 0)))
    state["stack"] = view.get("stack") or []
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False
    _pvp_sync_view_to_state(state, view, owner_id, opponent_id)
    pvp_save_state(session, state)
    _pvp_send_same_events(session, g, pl_t, ai_t)

    if state.get("pending_choice"):
        # The resumed second DoubleChoice prompt was sent privately by the
        # shared prompt helper. Only the selected card's public move was sent
        # above; leave priority in the picker until the next answer.
        log_req(f"    PvP choice selected: {hex(int(chosen_uid))}; "
                "second choice pending")
        return True

    g2 = _ge.Game(int(session.session_id), pl_t, ai_t)
    g2.push_chain_empty()
    state["priority_pid"] = int(state.get("turn_pid") or owner_id)
    pvp_save_state(session, state)
    turn_pid = int(state["priority_pid"])
    turn_handler = player_handlers.get(turn_pid)
    if turn_handler is not None:
        turn_uid = _ge.UID.make(244, turn_pid)
        other_uid = _ge.UID.make(244, opponent_id if turn_pid == owner_id
                                  else owner_id)
        g2 = _ge.Game(int(session.session_id), turn_uid, other_uid)
        g2.push_green_light(turn_uid, _ge.EPriorityContext.Normal)
        _send_pvp_packet(turn_handler, session, g2, turn_uid,
                         "greenlight-after-choice")
    if state.get("phase") in (_ge.ETurnPhases.FirstMainPhase,
                               _ge.ETurnPhases.SecondMainPhase):
        pvp_push_main_phase_options(session, state)
    log_req(f"    PvP choice selected: {hex(int(chosen_uid))}; "
            "choice sequence complete")
    return True


def _pvp_resolve_deck_search(handler, session, inner_bytes, my_pid):
    """Resolve a PvP "search your deck" pick (Darkspire Priestess's Deathcry):
    move the player's chosen matching deck card into their hand and push the
    objective CardMoved / CardDrawn / CardUpdated stream to both players."""
    import struct
    state = pvp_load_state(session) or {}
    pend = state.pop("pending_deck_search", None)
    if not pend:
        return False
    chosen_uid = None
    if isinstance(inner_bytes, bytes):
        for m_du in re.finditer(
                rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                inner_bytes):
            try:
                uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                if (uid64 & 0xFF) == 1:
                    chosen_uid = int(uid64)
            except Exception:
                continue
    if not chosen_uid or chosen_uid not in pend["candidates"]:
        pvp_save_state(session, state)
        log_req(f"    PvP deck-search invalid choice: "
                f"chosen={chosen_uid} candidates={pend['candidates']}")
        return True
    owner_id = int(pend["owner_id"])
    pids = db_game_session_pids(session.session_id)
    pl_t = _ge.UID.make(244, owner_id)
    opp_pid = [p for p in pids if p != owner_id][0]
    ai_t = _ge.UID.make(244, opp_pid)
    bstate = {"pvp": True, "pids": list(pids)}
    g = _ge.Game(int(session.session_id), pl_t, ai_t)
    from abilities.framework.effects.search import move_deck_card_to_hand
    move_deck_card_to_hand(g, session, _db, handler, pl_t, ai_t,
                           chosen_uid, owner_id, bstate)
    pvp_save_state(session, state)
    _pvp_send_same_events(session, g, pl_t, ai_t)
    log_req(f"    PvP deck-search resolved: {hex(chosen_uid)} -> hand "
            f"(pid {owner_id})")
    return True


def _pvp_resolve_revealed_choice(handler, session, inner_bytes, my_pid):
    """Resolve an explicit choice from a private SourceRevealed prompt."""
    import re as _re
    import struct as _st
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    state = pvp_load_state(session) or {}
    pend = state.pop("pending_deck_search", None)
    if not pend or pend.get("kind") != "revealed_troop":
        return False
    chosen_uid = None
    if isinstance(inner_bytes, bytes):
        for m_du in _re.finditer(
                rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                inner_bytes):
            try:
                uid64 = _st.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                if (uid64 & 0xFF) == 1:
                    chosen_uid = int(uid64)
            except Exception:
                continue
    candidates = [int(c) for c in (pend.get("candidates") or [])]
    revealed = [int(c) for c in (pend.get("revealed_cards") or [])]
    owner_id = int(pend.get("owner_id", my_pid))
    if not chosen_uid or chosen_uid not in candidates:
        pvp_save_state(session, state)
        log_req(f"    PvP revealed choice invalid: chosen={chosen_uid} "
                f"candidates={candidates}")
        return True
    opp_pid = pids[0] if pids[1] == owner_id else pids[1]
    pl_t = _ge.UID.make(244, owner_id)
    ai_t = _ge.UID.make(244, opp_pid)
    g = _ge.Game(int(session.session_id), pl_t, ai_t)
    _pvp_populate_game_state(g, state, owner_id, opp_pid)
    from abilities.framework.effects.search import move_deck_card_to_hand
    move_deck_card_to_hand(g, session, _db, handler, pl_t, ai_t,
                           chosen_uid, owner_id, state)
    remaining = [cu for cu in revealed if cu != chosen_uid]
    if remaining:
        from db import db_randomly_insert_deck_cards
        db_randomly_insert_deck_cards(
            session.session_id, owner_id, remaining, connection=_db)
    handler._hide_candidates_to_deck(
        g, session, pl_t, ai_t,
        remaining)
    _pvp_send_same_events(session, g, pl_t, ai_t)
    pvp_save_state(session, state)
    log_req(f"    PvP revealed choice resolved: {hex(chosen_uid)} -> hand "
            f"(pid {owner_id})")
    state["priority_pid"] = owner_id
    pvp_save_state(session, state)
    chooser = player_handlers.get(owner_id)
    if chooser:
        g2 = _ge.Game(int(session.session_id), pl_t, ai_t)
        _pvp_populate_game_state(g2, state, owner_id, opp_pid)
        g2.push_green_light(pl_t, _ge.EPriorityContext.Normal)
        _send_pvp_packet(chooser, session, g2, pl_t,
                         "greenlight-after-revealed-choice")
    if state.get("phase") in (_ge.ETurnPhases.FirstMainPhase,
                               _ge.ETurnPhases.SecondMainPhase):
        pvp_push_main_phase_options(session, state)
    return True


def _pvp_resolve_shard_choice(handler, session, inner_bytes, my_pid):
    """Resolve the PvP Shards of Fate deck choice.

    The selected Standard resource remains in the deck; only its threshold is
    granted.  The candidate cards are then hidden back into the deck for both
    clients and the turn player's normal priority is restored.
    """
    import struct
    state = pvp_load_state(session) or {}
    pend = state.pop("pending_deck_search", None)
    if not pend:
        return False
    chosen_uid = None
    if isinstance(inner_bytes, bytes):
        for m_du in re.finditer(
                rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                inner_bytes):
            try:
                uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                if (uid64 & 0xFF) == 1:
                    chosen_uid = int(uid64)
            except Exception:
                continue
    candidates = [int(c) for c in (pend.get("candidates") or [])]
    if not chosen_uid or chosen_uid not in candidates:
        pvp_save_state(session, state)
        log_req(f"    PvP Shards of Fate invalid choice: "
                f"chosen={chosen_uid} candidates={candidates}")
        return True

    owner_id = int(pend["owner_id"])
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2 or owner_id not in pids:
        pvp_save_state(session, state)
        return True
    opp_pid = [p for p in pids if p != owner_id][0]
    pl_t = _ge.UID.make(244, owner_id)
    ai_t = _ge.UID.make(244, opp_pid)
    g = _ge.Game(int(session.session_id), pl_t, ai_t)
    _pvp_populate_game_state(g, state, owner_id, opp_pid)

    row = _db.execute(
        "SELECT ct.name FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session.session_id, int(chosen_uid))).fetchone()
    color = (row[0].split()[0] if row else "").lower()
    from db import db_randomly_insert_deck_cards
    db_randomly_insert_deck_cards(
        session.session_id, owner_id, pend.get("candidates") or [])
    flag = _ge.SHARD_TO_FLAG.get(color, 0)
    thresh_key = f"thresh_{owner_id}"
    thresh = dict(state.get(thresh_key) or {})
    cur = thresh.get(flag)
    if cur is None:
        cur = thresh.get(str(flag), 0)
    if flag:
        thresh[flag] = int(cur or 0) + 1
        state[thresh_key] = thresh
        g.player_threshold = dict(thresh)
        ev_th = _ge.PlayerResourceThresholdChangedSessionEventArgs()
        ev_th.player_id = pl_t
        ev_th.color = flag
        ev_th.operation = 1
        ev_th.delta = 1
        ev_th.new_value = int(thresh[flag])
        g._push(ev_th)

    # The selected card is not moved into hand or PlayedResources.  All
    # presented candidates, including the selected one, return face-down to
    # the deck in the clients' views.
    handler._hide_candidates_to_deck(g, session, pl_t, ai_t, candidates)
    champ_map = state.get("champ_map", {})
    for target_pid in pids:
        target_uid = _ge.UID.make(244, target_pid)
        cu = int(champ_map.get(str(target_pid), 0))
        champ_scid = _ge.SessionCardId(_ge.UID(cu)) if cu else None
        g.push_player_updated(target_uid, champ_id=champ_scid)
    if state.pop("pending_gain_charge_pid", None) == owner_id:
        charge_trigger_game = _pvp_gain_charge_trigger_game(
            handler, session, state, owner_id)
        if charge_trigger_game:
            for trigger_event in charge_trigger_game.events:
                g._push(trigger_event)
    _pvp_send_same_events(session, g, pl_t, ai_t)
    pvp_save_state(session, state)
    log_req(f"    PvP Shards of Fate resolved: gained {color} threshold "
            f"(chosen {hex(int(chosen_uid))}, pid {owner_id})")

    if state.get("stack"):
        _pvp_offer_trigger_response(session, state, owner_id)
        return True

    state["priority_pid"] = owner_id
    pvp_save_state(session, state)
    turn_h = player_handlers.get(owner_id)
    if turn_h:
        g2 = _ge.Game(int(session.session_id), pl_t, ai_t)
        _pvp_populate_game_state(g2, state, owner_id, opp_pid)
        g2.push_green_light(pl_t, _ge.EPriorityContext.Normal)
        _send_pvp_packet(turn_h, session, g2, pl_t,
                         "greenlight-after-shard")
    if state.get("phase") in (_ge.ETurnPhases.FirstMainPhase,
                              _ge.ETurnPhases.SecondMainPhase):
        pvp_push_main_phase_options(session, state)
    return True


def _pvp_resolve_trigger_target(handler, session, inner_bytes, my_pid):
    """Resolve a PvP triggered-ability target choice (Solitary Exile's Deploy,
    Adamanthian Scrivener, ...): read the chosen card from the transaction,
    pop the pending trigger from the PvP state, resolve the ability BOM with
    that target, and push the objective events to both players."""
    import re as _re
    import struct as _st
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    state = pvp_load_state(session) or {}
    pend = state.get("pending_trigger")
    if not pend:
        return False
    chosen_uid = None
    if isinstance(inner_bytes, bytes):
        for m_du in _re.finditer(
                rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                inner_bytes):
            try:
                uid64 = _st.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                if (uid64 & 0xFF) == 1:
                    chosen_uid = int(uid64)
            except Exception:
                continue
    state.pop("pending_trigger", None)
    pvp_save_state(session, state)
    ag = str(pend["ability_guid"])
    src = int(pend["source_uid"])
    owner_id = int(pend.get("owner_id", my_pid))
    opp_pid = pids[0] if pids[1] == owner_id else pids[1]
    pl_t = _ge.UID.make(244, owner_id)
    ai_t = _ge.UID.make(244, opp_pid)
    # Build the shared-resolver view for the trigger BOM (owner-aware leaves).
    view = _pvp_fra_view(state, owner_id, opp_pid)
    view["pending_trigger"] = None
    view["resolving_owner_id"] = owner_id
    view["player_mod_target"] = chosen_uid
    view["player_spell_target"] = chosen_uid
    view["resolving_source_uid"] = src
    g = _ge.Game(int(session.session_id), pl_t, ai_t)
    _pvp_populate_game_state(g, state, owner_id, opp_pid)
    from abilities.framework.triggers import resolve_stack_trigger
    try:
        resolve_stack_trigger(handler, g, session, _db, pl_t, ai_t, view, {
            "kind": "trigger", "ability_guid": ag, "source_uid": src,
            "target_uid": chosen_uid,
            "instance_id": int(pend.get("instance_id", 1)),
        })
    except Exception as e:
        import traceback
        log_req(f"    PvP trigger resolve error: {e}")
        traceback.print_exc()
    # Copy health changes back into the PvP state.
    if view.get("player_health") is not None:
        state[f"hp_{owner_id}"] = int(view["player_health"])
    if view.get("ai_health") is not None:
        state[f"hp_{opp_pid}"] = int(view["ai_health"])
    _pvp_sync_view_to_state(state, view, owner_id, opp_pid)
    pvp_save_state(session, state)
    _pvp_send_same_events(session, g, pl_t, ai_t)
    # A resolved trigger (e.g. Lifedrain) can kill a champion.
    if _pvp_check_game_end(session, state):
        return True

    # The target-choice transaction completes the same chain item that
    # _pvp_resolve_chain normally finishes.  Because this path returns early
    # from the normal resolver, it must explicitly restore the ordinary
    # priority/phase/options handoff; otherwise both clients leave the target
    # picker but neither receives a usable next green light.
    turn_pid = int(state.get("turn_pid") or owner_id)
    state["priority_pid"] = turn_pid
    state["passes"] = []
    state["stack_passed"] = []
    pvp_save_state(session, state)
    turn_h = player_handlers.get(turn_pid)
    if turn_h:
        turn_uid = _ge.UID.make(244, turn_pid)
        other_pid = pids[1] if pids[0] == turn_pid else pids[0]
        other_uid = _ge.UID.make(244, other_pid)
        resume = _ge.Game(int(session.session_id), turn_uid, other_uid)
        _pvp_populate_game_state(resume, state, turn_pid, other_pid)
        resume.push_chain_empty()
        resume.push_green_light(turn_uid, _ge.EPriorityContext.Normal)
        _pvp_push_turn_phase_with_elapsed(
            resume, int(state.get("phase", 0)), turn_uid, turn_uid,
            _pvp_priority_elapsed_ticks(state, turn_pid) // 10_000_000)
        _send_pvp_packet(turn_h, session, resume, turn_uid,
                         "trigger-target-chain-empty")
    phase = int(state.get("phase", 0))
    if phase in (_ge.ETurnPhases.FirstMainPhase,
                 _ge.ETurnPhases.SecondMainPhase):
        pvp_push_main_phase_options(session, state)
    elif phase == _ge.ETurnPhases.DeclareAttack:
        pvp_push_attack_options(session, state)
    elif phase == _ge.ETurnPhases.DeclareDefense:
        pvp_push_blocker_options(session, state)
    log_req(f"    PvP trigger resolved: {ag[:8]} -> "
            f"{hex(chosen_uid) if chosen_uid else 'none'}; "
            f"priority -> {turn_pid}")
    return True


def _push_to_both_players(session, handler, events_fn, log_req=log_req):
    """Push game events to BOTH players in a tourney session using *events_fn*."""
    pids = db_game_session_pids(session.session_id)
    for pid in pids:
        h = player_handlers.get(pid)
        if not h:
            continue
        pl_t = _ge.UID.make(244, pid)
        opp_t = _ge.UID.make(244, pids[1] if pid == pids[0] else pids[0])
        g = _ge.Game(int(session.session_id), pl_t, opp_t)
        events_fn(g, pl_t)
        if g.events:
            pkt = g.make_network_packet(pl_t)
            dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                     client_session_guid(h))
            h.scnt += 1
            try:
                h.send({
                    "issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
                    "target": "ServiceGameSession",
                    "instance": str(session.server_id),
                    "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
                }, dw)
                log_req(f"    PvP: pushed to pid {pid}")
            except OSError:
                log_req(f"    PvP: failed to push to pid {pid} (disconnected)")


def _send_pvp_packet(h, session, g, pl_uid, label):
    """Serialize a Game's queued events and send the 3055 packet to one
    player's handler.  Returns True when the send succeeded (or there was
    nothing to send), False when the client is disconnected."""
    if not g.events:
        return True
    pkt = g.make_network_packet(pl_uid)
    dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                            client_session_guid(h))
    h.scnt += 1
    try:
        h.send({
            "issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
            "target": "ServiceGameSession", "instance": str(session.server_id),
            "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
        }, dw)
        log_req(f"    PvP {label}: pushed to pid {int(pl_uid.uid64) >> 8}")
        return True
    except OSError:
        log_req(f"    PvP {label}: failed to push to pid {int(pl_uid.uid64) >> 8} (disconnected)")
        return False


def _pvp_push_reconnect_snapshot(handler, session, pid):
    """Restore the persisted PvP view for a reconnecting client.

    The normal start packet cannot be reused here: it shuffles/deals the
    decks.  ``game_cards`` and ``turn_order_json`` are the authoritative
    mid-game state, so rebuild only client representations and current HUD /
    priority data from those rows.
    """
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2 or int(pid) not in pids:
        return False
    pid = int(pid)
    opp_pid = pids[1] if pids[0] == pid else pids[0]
    state = pvp_load_state(session) or {}
    # Close the live priority interval before rebuilding the client view.  A
    # reconnect may happen while no card/phase transaction is in flight, so
    # relying only on the last action would under-count the active player's
    # clock.
    with pvp_session_lock(session):
        latest_state = pvp_load_state(session)
        if latest_state:
            state = latest_state
            pvp_save_state(session, state)
    pl_uid = _ge.UID.make(244, pid)
    opp_uid = _ge.UID.make(244, opp_pid)
    g = _ge.Game(int(session.session_id), pl_uid, opp_uid)
    g.player_champion_card_id = _ge.SessionCardId(
        _ge.UID(int((state.get("champ_map") or {}).get(str(pid), 0))))
    g.ai_champion_card_id = _ge.SessionCardId(
        _ge.UID(int((state.get("champ_map") or {}).get(str(opp_pid), 0))))
    _pvp_populate_game_state(g, state, pid, opp_pid)
    handler._current_bstate = state

    # JoinDisconnectedGame creates only the local Player on the client.  Add
    # the opponent before GameStarted so ClientSessionBase can construct both
    # Player objects and UIBattle can build m_PlayerIndices before DeckCreated
    # and CardUpdated events are replayed.
    player_added_inner = encode_objfmt_response(
        ["Game.Shared.Network.GameSession.PlayerAddedEventArgs",
         "Game.Shared.UID", "Game.Shared.PlayerState", "System.Int32"],
        [("RoutingPlayerId", "uid", int(opp_uid.uid64)),
         ("PlayerState", "struct", ("Game.Shared.PlayerState", [
             ("PlayerId", "uid", int(opp_uid.uid64)),
             ("PlayerPosition", "int", 1),
         ]))])
    player_added_dw = encode_datawrapper(
        0, 3050, compress_gzip(player_added_inner), 1,
        client_session_guid(handler))
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{handler.scnt}",
        "target": "ServiceGameSession", "instance": str(session.server_id),
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }, player_added_dw)
    log_req(f"    PvP reconnect: added opponent pid {opp_pid}")

    # Rebuild the client-side turn-order/player-index state.  Use the
    # persisted first-player identity, rather than allowing the helper to
    # randomize the order during reconnect.
    goes_first_pid = int(state.get("goes_first_pid", state.get("turn_pid", pid)))
    if goes_first_pid not in pids:
        goes_first_pid = pid
    game_started_pids = [goes_first_pid,
                         pids[1] if goes_first_pid == pids[0] else pids[0]]
    champion_template_ids = []
    for game_pid in game_started_pids:
        champion_row = db_game_champion(session.session_id, game_pid)
        champion_template_ids.append(
            champion_row[1] if champion_row and champion_row[1]
            else "00000000-0000-0000-0000-000000000000")
    player_champion_row = db_game_champion(session.session_id, pid)
    opponent_champion_row = db_game_champion(session.session_id, opp_pid)
    handler._player_champ_scid = g.player_champion_card_id
    handler._ai_champ_scid = g.ai_champion_card_id
    handler._player_champ_guid = (
        player_champion_row[1] if player_champion_row else None)
    handler._ai_champ_guid = (
        opponent_champion_row[1] if opponent_champion_row else None)
    g.push_game_started(
        champion_names=["Player 1", "Player 2"],
        champion_template_ids=champion_template_ids,
        player_first=(goes_first_pid == pid))
    g.push_first_player_dictated(_ge.UID.make(244, goes_first_pid))

    from db import db_game_cards_at_location

    # PlayerUpdated must precede CardUpdated so the client has valid player
    # entries when it handles champion/zone state.
    g.push_player_updated(pl_uid, champ_id=g.player_champion_card_id)
    g.push_player_updated(opp_uid, champ_id=g.ai_champion_card_id)

    # Public champions, warzone, discard and void cards.
    for card_uid, template_guid, owner, card_type, card_state, _abilities, _attrs \
            in db_game_cards_at_location(session.session_id, "champion"):
        scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
        try:
            handler._card_full_data(g, scid, template_guid)
        except Exception:
            pass
        g.push_card_updated(
            scid, _ge.UID.make(244, int(owner)), _ECardCollections.None_,
            _ge.card_type_from_db(card_type), template_id=template_guid,
            state=int(card_state or 0))

    # CardUpdated introduces the champion representation, but the client only
    # builds the champion HUD/portrait/ability buttons from this follow-up
    # event.  Reconnect must replay it just like the initial PvP setup does.
    g.push_champion_card_played(
        pl_uid, False, "Player 1", g.player_champion_card_id)
    g.push_champion_card_played(
        opp_uid, False, "Player 2", g.ai_champion_card_id)

    pvp_push_warzone_updates(session, state, game=g)
    for zone, collection in (("discard", _ECardCollections.Discard),
                             ("void", _ECardCollections.Void)):
        for card_uid, template_guid, owner, card_type, card_state, _abilities, _attrs \
                in db_game_cards_at_location(session.session_id, zone):
            scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
            try:
                handler._card_full_data(g, scid, template_guid)
            except Exception:
                pass
            g.push_card_updated(
                scid, _ge.UID.make(244, int(owner)), collection,
                _ge.card_type_from_db(card_type), template_id=template_guid,
                state=int(card_state or 0))

    # Rebuild both deck counters while keeping all deck identities hidden.
    for owner_pid in pids:
        owner_uid = _ge.UID.make(244, owner_pid)
        for card_uid, template_guid, owner, card_type, card_state, _abilities, _attrs \
                in db_game_cards_at_location(session.session_id, "deck",
                                             user_id=owner_pid):
            scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
            try:
                handler._card_full_data(g, scid, template_guid)
            except Exception:
                pass
            g.push_card_updated(
                scid, owner_uid, _ECardCollections.Deck,
                _ge.card_type_from_db(card_type), template_id=template_guid,
                nulling=True)
        g.push_deck_created(owner_uid)

        for card_uid, template_guid, owner, card_type, card_state, _abilities, _attrs \
                in db_game_cards_at_location(session.session_id, "hand",
                                             user_id=owner_pid):
            scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
            try:
                handler._card_full_data(g, scid, template_guid)
            except Exception:
                pass
            g.push_card_updated(
                scid, owner_uid, _ECardCollections.Hand,
                _ge.card_type_from_db(card_type), template_id=template_guid,
                state=int(card_state or 0), nulling=owner_pid != pid)

    turn_pid = int(state.get("turn_pid", pid))
    priority_pid = int(state.get("priority_pid", turn_pid))
    phase = int(state.get("phase", _ge.ETurnPhases.FirstMainPhase))
    priority_uid = _ge.UID.make(244, priority_pid)
    turn_uid = _ge.UID.make(244, turn_pid)
    priority_elapsed_seconds = (
        _pvp_priority_elapsed_ticks(state, priority_pid) // 10_000_000)
    g.push_green_light(priority_uid, _ge.EPriorityContext.Normal)
    _pvp_push_turn_phase_with_elapsed(
        g, phase, turn_uid, priority_uid, priority_elapsed_seconds)
    g.push_reconnect_done()
    _send_pvp_packet(handler, session, g, pl_uid, "reconnect-snapshot")

    # Recreate only the current priority holder's legal choices.  These are
    # private and therefore must not be sent to the other client.
    if priority_pid == pid:
        if phase in (_ge.ETurnPhases.FirstMainPhase,
                     _ge.ETurnPhases.SecondMainPhase):
            pvp_push_main_phase_options(session, state)
        elif phase == _ge.ETurnPhases.DeclareAttack:
            pvp_push_attack_options(session, state)
        elif phase == _ge.ETurnPhases.DeclareDefense:
            pvp_push_blocker_options(session, state)
        elif phase not in (3, 4, 5, 6, 7, 8, 9):
            pvp_push_phase_options(session, state, pid=pid)
    log_req(f"    PvP reconnect snapshot restored for pid {pid} "
            f"(phase={phase}, priority={priority_pid})")
    return True


def _pvp_raw_player_id(player_uid):
    """Return the persisted Reckoning id from a raw or typed player UID."""
    value = int(player_uid or 0)
    return (value >> 8) if (value & 0xff) == 244 else value


def notify_pvp_player_disconnected(player_uid, disconnected_handler=None):
    """Tell the remaining PvP client that its opponent went offline."""
    import game_session as gs
    try:
        player_uid = int(player_uid)
    except (TypeError, ValueError):
        return False
    session = gs.find_session_by_player(player_uid)
    if not session or getattr(session, "state", "") == "ended":
        return False
    pids = db_game_session_pids(session.session_id)
    opponent = next((p for p in pids if int(p) != player_uid), None)
    opponent_handler = player_handlers.get(opponent) if opponent is not None else None
    if not opponent_handler or opponent_handler is disconnected_handler:
        return False
    inner = encode_objfmt_response(
        ["Game.Client.Network.GameSession.PlayerDisconnectedResponse"], [])
    dw = encode_datawrapper(0, 3033, compress_gzip(inner), 1,
                            client_session_guid(opponent_handler))
    opponent_handler.scnt += 1
    try:
        opponent_handler.send({
            "issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{opponent_handler.scnt}",
            "target": "ServiceGameSession", "instance": str(session.server_id),
            "reqid": 0, "c": 0, "conh": 0, "sid": opponent_handler.sid,
        }, dw)
        log_req(f"    PvP disconnect notification: {player_uid} -> {opponent}")
        return True
    except OSError:
        return False


def _pvp_send_same_events(session, game, pl_t, ai_t):
    """Send the identical combat event stream to both players.  The events are
    objective — each card/player is referenced by its own UID — so the same
    payload renders correctly from either client's perspective.  The CardDefs
    registered on `game` (via _card_full_data) are ALSO carried to each
    player's fresh Game: CardUpdated events read the CardDef for cost/atk/def/
    abilities/attributes/gems, so without them a re-pushed card (e.g. a troop
    moving onto the opponent's chain) would render blank on the non-controller's
    client.  player_health/resources are copied too so any PlayerUpdated in the
    stream reports the real values, not the 20/0 defaults."""
    pids = [int(pl_t.uid64) >> 8, int(ai_t.uid64) >> 8]
    evs = list(game.events)
    card_defs = dict(game.card_defs)
    # DEBUG: per-player event-class audit — confirms both clients receive the same
    # CardUpdated(64)/CardMoved(22)/AbilityOnChain/Played events from a troop play,
    # so we can see if the OPPONENT's packet is missing the CardUpdated that would
    # introduce the played card to their client.
    try:
        _cls = []
        for _ev in evs:
            _c = getattr(type(_ev), "CLASS_ID", 0)
            _cls.append(_c)
        def uid_val(u):
            u = getattr(u, "uid", u)
            u = getattr(u, "uid64", u)
            return int(u)
        _cards = [uid_val(c) for c in card_defs]
        log_req(f"    PvP send-audit: {len(evs)} events, classes={_cls} "
                f"defs={[hex(x) for x in _cards]}")
    except Exception as _e:
        log_req(f"    PvP send-audit error: {_e}")
    health = game.player_health
    ai_health = game.ai_health
    p_res = game.player_resources
    p_tot = game.player_total_resources
    ai_res = game.ai_resources
    ai_tot = game.ai_total_resources
    p_chg = game.player_charges
    ai_chg = game.ai_charges
    for pid in pids:
        h = player_handlers.get(pid)
        if not h:
            continue
        opp = pids[1] if pid == pids[0] else pids[0]
        g2 = _ge.Game(int(session.session_id),
                      _ge.UID.make(244, pid), _ge.UID.make(244, opp))
        g2.events = [ev for ev in evs]
        g2.card_defs = dict(card_defs)
        g2.player_health = health
        g2.ai_health = ai_health
        g2.player_resources = p_res
        g2.player_total_resources = p_tot
        g2.ai_resources = ai_res
        g2.ai_total_resources = ai_tot
        g2.player_charges = p_chg
        g2.ai_charges = ai_chg
        _send_pvp_packet(h, session, g2, _ge.UID.make(244, pid), "combat")


def _pvp_end_game(session, state, winner_pid, loser_pid, reason=""):
    """Publish the tournament result before ending the client game.

    The clients can transition to the tournament lobby as soon as they receive
    GameEnded.  Persist and publish the match first so that the lobby's cached
    TournamentInfo already contains the completed match when that transition
    starts.  The result publisher also sends the delayed rdata refresh used to
    cover the separate ServicePlayer/chat delivery paths.
    """
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    winner_uid = _ge.UID.make(244, winner_pid)
    loser_uid = _ge.UID.make(244, loser_pid)
    tournament_complete = False
    try:
        record_tournament_game_result(session, winner_pid, loser_pid)
        try:
            tid = int(str(session.session_name)[len("tourney-"):])
            tournament_complete = str(
                (db_tournament_by_id(tid) or {}).get("status", "")
            ).lower() == "complete"
        except (TypeError, ValueError):
            tournament_complete = False
    except Exception as exc:
        log_req(f"    WARN: tournament result recording failed: {exc}")
    for pid in pids:
        h = player_handlers.get(pid)
        if not h:
            continue
        my_uid = _ge.UID.make(244, pid)
        try:
            import commands as _cmd
            _cmd.push_battle_game_end(h, session, [winner_uid], [loser_uid])
        except Exception as e:
            # Fall back to sending the raw packet if the helper path fails.
            from domain.events import make_game_ended_packet
            nw = make_game_ended_packet(int(session.session_id), my_uid,
                                        [winner_uid], [loser_uid])
            dw = encode_datawrapper(0, 3055,
                                    compress_gzip(encode_sync_event(nw)), 1,
                                    client_session_guid(h))
            h.scnt += 1
            try:
                h.send({
                    "issuer": f"0.0.0.0.ServiceGameSession.246."
                              f"{session.session_id}.{h.scnt}",
                    "target": "ServiceGameSession",
                    "instance": str(session.server_id),
                    "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
                }, dw)
            except OSError:
                log_req(f"    PvP game end: failed to push to pid {pid} "
                        f"(disconnected)")
            log_req(f"    PvP game end fallback sent to pid {pid}: "
                      f"winner {winner_pid} loser {loser_pid} ({reason})")
    try:
        session.set_state("ended")
    except Exception:
        pass
    # Free the per-session mutation lock now that the game is over.
    try:
        pvp_discard_session_lock(session)
    except Exception:
        pass
    if tournament_complete:
        db_delete_game_session(session.session_id)
        log_req(f"    Tournament complete: cleaned PvP session {session.session_id}")
    log_req(f"    PvP GAME OVER: pid {winner_pid} beats {loser_pid} "
            f"({reason}) — session ended")


def _pvp_check_game_end(session, state):
    """After combat/chain resolution, if either champion's health is <= 0 the
    game ends (mirrors PvE _check_champion_health).  Returns True when ended."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    for pid in pids:
        if int(state.get(f"hp_{pid}", 20)) <= 0:
            other = pids[1] if pid == pids[0] else pids[0]
            _pvp_end_game(session, state, other, pid,
                          f"champion at 0 health")
            return True
    return False


def _pvp_resolve_chain(session, state, handler, my_pid):
    """Resolve the top item of the PvP chain/stack.

    The client's "Resolve" button submits a PassPriorityTransaction while the
    chain is non-empty.  This pops the top item, pushes TopOfChainResolved +
    RemovedTopOfChain + the effect's own events (e.g. Adamanthian Scrivener's
    life gain) to BOTH players, persists health, and hands priority on: back
    to the turn player (Normal) when the chain empties, else to the OTHER
    player (ResolveTopOfChain) so they can respond to the next item.
    Returns True when an item was resolved."""
    import battle_engine as _be
    item = _be.stack_pop(state)
    if not item:
        return False
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    kind = item.get("kind")
    instance_id = int(item.get("instance_id", 1))
    src_uid = int(item.get("source_uid") or 0)
    # Owner of the chain item's source card (triggers belong to their card).
    owner_id = my_pid
    if src_uid:
        orow = _db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, src_uid)).fetchone()
        if orow:
            owner_id = orow[0]
    opp_pid = pids[0] if pids[1] == owner_id else pids[1]
    pl_t = _ge.UID.make(244, owner_id)
    ai_t = _ge.UID.make(244, opp_pid)
    view = _pvp_fra_view(state, owner_id, opp_pid)
    view["resolving_owner_id"] = owner_id
    view["resolving_source_uid"] = src_uid
    view["player_mod_target"] = item.get("target_uid")
    view["player_spell_target"] = item.get("target_uid")
    g = _ge.Game(int(session.session_id), pl_t, ai_t)
    _pvp_populate_game_state(g, state, owner_id, opp_pid)
    g.push_top_of_chain_resolved(instance_id)
    g.push_removed_top_of_chain(instance_id)
    if kind == "trigger":
        from abilities.framework.triggers import resolve_stack_trigger
        try:
            resolve_stack_trigger(handler, g, session, _db, pl_t, ai_t, view, item)
        except Exception as e:
            import traceback
            log_req(f"    PvP chain trigger resolve error: {e}")
            traceback.print_exc()
    elif kind == "troop":
        # A permanent (troop/artifact/constant) resolves from the chain into
        # the warzone: mark CameOutThisTurn, push CardUpdated/CardMoved,
        # fire enters-play triggers — mirrors PvE's troop chain resolution.
        if src_uid:
            loc_row = _db.execute(
                "SELECT location FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, src_uid)).fetchone()
            if not loc_row or loc_row[0] != "CastSpells":
                log_req(f"    PvP troop {src_uid} already left the chain "
                        f"(loc={loc_row[0] if loc_row else None}) — skipped")
                pvp_save_state(session, state)
                return True
            scid = _ge.SessionCardId(_ge.UID(src_uid))
            tw = _db.execute(
                "SELECT gc.template_guid, ct.card_type, ct.cost FROM game_cards gc "
                "JOIN card_templates ct ON ct.guid=gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (session.session_id, src_uid)).fetchone()
            if tw:
                _db.execute(
                    "UPDATE game_cards SET location='warzone', position=0, "
                    "card_state=(card_state | ?) WHERE session_id=? AND card_uid=?",
                    (_ge.ECardStates.CameOutThisTurn, session.session_id, src_uid))
                _db.commit()
                handler._card_full_data(g, scid, tw[0])
                ct = _ge.card_type_from_db(tw[1])
                cdef = g.card_defs.get(scid)
                g.push_card_updated(scid, pl_t, _ge.ECardCollections.Warzone,
                                    ct, template_id=tw[0],
                                    cost=cdef.cost if cdef else 0,
                                    attack=cdef.attack if cdef else 0,
                                    defense=cdef.defense if cdef else 0)
                g.push_card_moved(scid, pl_t, _ge.ECardCollections.Warzone,
                                  _ge.ECardLocations.Top, 0)
                if ct & _ge.ECardTypes.Troop:
                    g.push_troop_card_played(scid, pl_t)
                elif ct & _ge.ECardTypes.Artifact:
                    g.push_artifact_card_played(scid, pl_t)
                from abilities.framework.triggers import resolve_enters_play_triggers
                try:
                    resolve_enters_play_triggers(
                        _db, handler, g, session, pl_t, ai_t, view,
                        src_uid, owner_id, tw[2])
                    # CardCastEvent also covers permanents.  Keep it separate
                    # from CardEnteredZoneEvent so cost-based triggers such
                    # as Jadiim see the card that was actually played.
                    view["card_cast_copy_target"] = src_uid
                    from abilities.framework.triggers import resolve_triggers
                    resolve_triggers(_db, handler, g, session, pl_t, ai_t,
                                     view, "CardCastEvent", src_uid, owner_id)
                    view.pop("card_cast_copy_target", None)
                except Exception as e:
                    log_req(f"    PvP troop chain enters-play error: {e}")
    elif kind == "spell":
        # A played action resolves its BOM then goes CastSpells -> Discard.
        # A spell that was countered/interrupted already left the chain —
        # skip its BOM so a countered spell never also draws/buffs/damages.
        loc_row = _db.execute(
            "SELECT location FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, src_uid)).fetchone()
        loc = loc_row[0] if loc_row else "discard"
        if loc != "CastSpells":
            log_req(f"    PvP spell {src_uid} already left the chain "
                    f"(loc={loc}) — countered/interrupted, BOM skipped")
        else:
            # The client receives SpellCardCast when the card enters
            # CastSpells and SpellCardPlayed only after it resolves.  A
            # countered spell never reaches this branch and therefore never
            # receives the played event.
            g.push_spell_card_played(
                _ge.SessionCardId(_ge.UID(src_uid)), pl_t)
            try:
                from abilities import resolve_played_spell as _rp_spell
                view["player_spell_target"] = item.get("target_uid")
                view["resolving_source_uid"] = src_uid
                view["resolving_owner_id"] = owner_id
                view["x_cost"] = int(item.get("x_cost") or 0)
                _rp_spell(g, session, _db, handler, pl_t, ai_t, view,
                          item.get("ability_guids", []))
                view.pop("x_cost", None)
            except Exception as e:
                import traceback
                log_req(f"    PvP chain spell resolve error: {e}")
                _tb = traceback.format_exc()
                for _tl in _tb.splitlines():
                    log_req(f"    PvP spell TB: {_tl}")
            # "When you play an action/..." triggers fire against the played
            # spell (e.g. Chimes of the Zodiac's "copy it").
            if src_uid:
                try:
                    from abilities.framework.triggers import resolve_triggers
                    view["card_cast_copy_target"] = src_uid
                    resolve_triggers(_db, handler, g, session, pl_t, ai_t,
                                     view, "CardCastEvent", src_uid, owner_id)
                    view.pop("card_cast_copy_target", None)
                except Exception as e:
                    import traceback
                    log_req(f"    PvP CardCast trigger error: {e}")
                    traceback.print_exc()
            view.pop("player_spell_target", None)
            view.pop("resolving_source_uid", None)
            # The spent spell goes to the graveyard (unless a leaf moved it
            # into the deck — e.g. Eternal Youth's escalation "put this into
            # your deck").
            if src_uid:
                loc_row2 = _db.execute(
                    "SELECT location FROM game_cards "
                    "WHERE session_id=? AND card_uid=?",
                    (session.session_id, src_uid)).fetchone()
                loc = loc_row2[0] if loc_row2 else "discard"
                if loc != "deck":
                    from db import db_card_discard_spell
                    db_card_discard_spell(session.session_id, src_uid)
                    scid = _ge.SessionCardId(_ge.UID(src_uid))
                    tw = _db.execute(
                        "SELECT gc.template_guid, ct.card_type FROM game_cards gc "
                        "JOIN card_templates ct ON ct.guid=gc.template_guid "
                        "WHERE gc.session_id=? AND gc.card_uid=?",
                        (session.session_id, src_uid)).fetchone()
                    if tw:
                        handler._card_full_data(g, scid, tw[0])
                        g.push_card_updated(
                            scid, pl_t, _ge.ECardCollections.Discard,
                            _ge.card_type_from_db(tw[1]), template_id=tw[0])
                        g.push_card_moved(scid, pl_t,
                                          _ge.ECardCollections.Discard,
                                          _ge.ECardLocations.Top, 0)
                        # "When a card enters an opposing crypt" triggers
                        # (e.g. Incantation of Fear) fire here — mirrors PvE
                        # hconnect ~2982.
                        try:
                            from abilities.framework.triggers import resolve_triggers
                            resolve_triggers(_db, handler, g, session, pl_t,
                                             ai_t, view, "CardEnteredZoneEvent",
                                             src_uid, owner_id)
                        except Exception as e:
                            import traceback
                            log_req(f"    PvP spell-crypt trigger error: {e}")
                            traceback.print_exc()
    elif kind == "ability":
        # Champion charge/spell power on the chain (e.g. Dimmid's Lifedrain):
        # resolve its BOM through the same resolver the PvE "ability" path
        # uses.  The source is the champion card — not a game_cards row — so
        # resolving_source_uid / resolving_owner_id carry the owner's pid.
        ag = str(item.get("ability_guid") or "")
        if ag:
            try:
                from abilities import resolve_effect as _re_eff
                view["player_mod_target"] = item.get("target_uid")
                view["player_spell_target"] = item.get("target_uid")
                view["resolving_ability"] = ag
                view["resolving_source_uid"] = src_uid
                view["resolving_owner_id"] = owner_id
                ability_event_start = len(g.events)
                ability_player_health_before = int(view.get("player_health", 20))
                ability_ai_health_before = int(view.get("ai_health", 20))
                fn = _re_eff(ag)
                if fn:
                    fn(g, session, _db, handler, pl_t, ai_t, view, ag, None)
                else:
                    log_req(f"    PvP chain ability {ag[:8]}: "
                            f"no BOM resolver, skipped")
                ability_player_health_after = int(
                    view.get("player_health", ability_player_health_before))
                ability_ai_health_after = int(
                    view.get("ai_health", ability_ai_health_before))
                g.player_health = ability_player_health_after
                g.ai_health = ability_ai_health_after
                g.push_champion_health_changed_if_missing(
                    pl_t, ability_player_health_before,
                    ability_player_health_after, since=ability_event_start)
                g.push_champion_health_changed_if_missing(
                    ai_t, ability_ai_health_before,
                    ability_ai_health_after, since=ability_event_start)
            except Exception as e:
                import traceback
                log_req(f"    PvP chain ability resolve error: {e}")
                traceback.print_exc()
            view.pop("player_mod_target", None)
            view.pop("player_spell_target", None)
            view.pop("resolving_ability", None)
            view.pop("resolving_source_uid", None)
    # Persist health + remaining stack (the view aliases state's stack, but
    # copy back explicitly so nothing is lost).
    state["stack"] = view.get("stack") or []
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False
    state["stack_passed"] = []
    state["_next_instance_id"] = view.get("_next_instance_id", 1)
    if view.get("player_health") is not None:
        state[f"hp_{owner_id}"] = int(view["player_health"])
    if view.get("ai_health") is not None:
        state[f"hp_{opp_pid}"] = int(view["ai_health"])
    _pvp_sync_view_to_state(state, view, owner_id, opp_pid)
    # Interactive BOM prompts persist their continuation through the handler
    # (the PvP prompt packet is private to the choosing client).  Reload just
    # those continuation markers so this resolver does not immediately send
    # the ordinary chain-empty/priority packet over the picker.
    persisted = pvp_load_state(session) or {}
    for _pending_key in ("pending_trigger", "pending_deck_search",
                         "pending_choice"):
        if persisted.get(_pending_key):
            state[_pending_key] = persisted[_pending_key]
    chain_empty = _be.stack_empty(state)
    pending_revealed_choice = (
        (state.get("pending_deck_search") or {}).get("kind")
        == "revealed_troop")
    pending_trigger = bool(state.get("pending_trigger"))
    pending_choice = bool(state.get("pending_choice"))
    # Shards of Fate / Adaptable Infusion Device sends a private class-39
    # deck picker to the controller.  Its picker packet already owns the
    # next green-light; sending the normal chain-empty/options packet here
    # tears down that UI and leaves PvP priority stranded.
    pending_deck_search = bool(state.get("pending_deck_search"))
    _pvp_log_stack(state, "resolve")
    if chain_empty and not (pending_revealed_choice or pending_deck_search
                            or pending_trigger or pending_choice):
        g.push_chain_empty()
    pvp_save_state(session, state)
    # State-based deaths: when the stack empties, troops at <=0 effective
    # defense die (spell/trigger -X/-X, etc.) — mirrors PvE
    # _resolve_stack_item (hconnect ~3087).  The death events go into the SAME
    # stream so both clients see the graveyard move + Deathcry.
    if chain_empty:
        try:
            from abilities.framework.kill_troop import state_based_deaths
            state_based_deaths(g, session, _db, handler, pl_t, ai_t, view)
        except Exception as e:
            log_req(f"    PvP state-based deaths error: {e}")
        # Copy health back again (deaths can heal via triggers).
        if view.get("player_health") is not None:
            state[f"hp_{owner_id}"] = int(view["player_health"])
        if view.get("ai_health") is not None:
            state[f"hp_{opp_pid}"] = int(view["ai_health"])
        state["stack"] = view.get("stack") or []
        _pvp_sync_view_to_state(state, view, owner_id, opp_pid)
        pvp_save_state(session, state)
    _pvp_send_same_events(session, g, pl_t, ai_t)
    if chain_empty and pending_revealed_choice:
        # The private picker packet was sent by _prompt_revealed_choice.  Do
        # not follow it with the ordinary chain-empty greenlight/options
        # packet; that would replace the client's ConfigureAbility state
        # before the player can answer it.
        pvp_save_state(session, state)
        log_req("    PvP chain paused for revealed-card choice")
        return True
    if chain_empty and pending_deck_search:
        # _prompt_deck_search already pushed the private picker and greenlight
        # to the choosing player.  Wait for SetAbilityActivationDataTransaction
        # before announcing chain-empty or re-pushing phase options.
        pvp_save_state(session, state)
        log_req("    PvP chain paused for deck-search choice")
        return True
    if chain_empty and pending_trigger:
        # _prompt_trigger_targets already sent the private PlayerOptionList,
        # class-39 activation request, and green light.  A normal chain-empty
        # refresh here would immediately replace BattleStateConfigureAbility,
        # which is why the target cursor appeared briefly and then vanished.
        pvp_save_state(session, state)
        log_req("    PvP chain paused for triggered target choice")
        return True
    if chain_empty and pending_choice:
        # The private picker packet was sent by _prompt_choice_cards. Do not
        # replace it with a chain-empty/normal-priority packet.
        pvp_save_state(session, state)
        log_req("    PvP chain paused for card choice")
        return True
    # Chain damage can kill a champion (e.g. burn / Lifedrain) — end the game
    # properly instead of continuing into the next priority handoff.
    if _pvp_check_game_end(session, state):
        return True
    if chain_empty:
        turn_pid = state.get("turn_pid")
        turn_h = player_handlers.get(turn_pid)
        state["priority_pid"] = turn_pid
        pvp_save_state(session, state)
        if turn_h:
            turn_pt = _ge.UID.make(244, turn_pid)
            turn_opp = _ge.UID.make(
                244, pids[1] if turn_pid == pids[0] else pids[0])
            gg = _ge.Game(int(session.session_id), turn_pt, turn_opp)
            gg.push_green_light(turn_pt, _ge.EPriorityContext.Normal)
            _pvp_push_turn_phase_with_elapsed(
                gg, int(state.get("phase", 0)), turn_pt, turn_pt,
                _pvp_priority_elapsed_ticks(state, turn_pid) // 10_000_000)
            _send_pvp_packet(turn_h, session, gg, turn_pt, "chain-empty")
        # Mirror PvE (hconnect ~8880): re-announce the current phase + push the
        # phase-appropriate options so the client rebuilds its state and the
        # stale "Continue to Second Main Phase <Card>" pass-button tail clears.
        _cur_phase = int(state.get("phase", 0))
        if _cur_phase in (_ge.ETurnPhases.FirstMainPhase,
                          _ge.ETurnPhases.SecondMainPhase):
            pvp_push_main_phase_options(session, state)
        elif _cur_phase == _ge.ETurnPhases.DeclareAttack:
            pvp_push_attack_options(session, state)
        elif _cur_phase == _ge.ETurnPhases.DeclareDefense:
            pvp_push_blocker_options(session, state)
        elif _cur_phase not in (3, 4, 5, 6, 7, 8, 9):
            pvp_push_phase_options(session, state, pid=turn_pid)
        log_req(f"    PvP chain resolved ({kind}) — empty, priority to "
                f"turn player {turn_pid}, phase {_cur_phase} options re-pushed")
    else:
        other_pid = pids[1] if my_pid == pids[0] else pids[0]
        state["priority_pid"] = other_pid
        pvp_save_state(session, state)
        if _pvp_auto_pass_chain_priority(session, state, other_pid):
            return True
        other_h = player_handlers.get(other_pid)
        if other_h:
            other_pt = _ge.UID.make(244, other_pid)
            other_opp = _ge.UID.make(
                244, pids[1] if other_pid == pids[0] else pids[0])
            gg = _ge.Game(int(session.session_id), other_pt, other_opp)
            gg.push_green_light(other_pt, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(other_h, session, gg, other_pt, "chain-next")
        log_req(f"    PvP chain resolved ({kind}) — "
                f"{len(state.get('stack') or [])} item(s) left, priority to "
                f"{other_pid}")
    return True


def _pvp_fra_view(state, attacker_pid, defender_pid):
    """Translate the PvP battle state into the FRA-shaped view resolve_combat
    understands (attacker = 'player', defender = 'ai').  ``pvp`` stays set so
    the owner mappings use pid-based UIDs, and pvp_health_map lets heal
    triggers write to the right health key.  The chain/stack is ALIASED to the
    persisted PvP state (not a transient copy), so triggers pushed onto the
    stack survive the function call and can be resolved later by a pass."""
    import battle_engine as _be
    return {
        "pvp": True,
        "pids": list(state.get("pids") or []),
        "champ_map": state.get("champ_map") or {},
        "pvp_health_map": {attacker_pid: "player_health",
                           defender_pid: "ai_health"},
        "player_health": int(state.get(f"hp_{attacker_pid}", 20)),
        "ai_health": int(state.get(f"hp_{defender_pid}", 20)),
        "player_max_health": int(state.get(f"hp_{attacker_pid}", 20)),
        "ai_max_health": int(state.get(f"hp_{defender_pid}", 20)),
        "turn_number": int(state.get("turn_number", 1)),
        "damaged_opponent_this_turn": list(
            state.get("damaged_opponent_this_turn") or []),
        "damaged_opponent_turn": int(
            state.get("damaged_opponent_turn", 0) or 0),
        # Escalation is per player for the whole game, not per resolver view.
        # The old literal zero reset Ragefire whenever a new view was built.
        "player_escalation_uses": int(
            state.get(f"esc_{attacker_pid}", 0)),
        "ai_escalation_uses": int(
            state.get(f"esc_{defender_pid}", 0)),
        # Resource pool from the PvP state (shards played + refill at Prep).
        "player_resources": int(state.get(f"res_{attacker_pid}", 0)),
        "ai_resources": int(state.get(f"res_{defender_pid}", 0)),
        "player_total_resources": int(state.get(f"res_total_{attacker_pid}", 0)),
        "ai_total_resources": int(state.get(f"res_total_{defender_pid}", 0)),
        "player_threshold": _pvp_state_thresholds(state, attacker_pid),
        "ai_threshold": _pvp_state_thresholds(state, defender_pid),
        "player_charges": int(state.get(f"chg_{attacker_pid}", 0)),
        "ai_charges": int(state.get(f"chg_{defender_pid}", 0)),
        "player_spell_points": int(state.get(f"sp_{attacker_pid}", 0)),
        "ai_spell_points": int(state.get(f"sp_{defender_pid}", 0)),
        "briar_legions_entered": int(
            state.get("briar_legions_entered", 0)),
        # Chain/stack aliased to the persisted state so trigger pushes land in
        # the DB-persisted dict (pvp_save_state persists session.turn_order).
        "stack": state.setdefault("stack", []),
        "stack_player_passed": state.get("stack_player_passed", False),
        "stack_ai_passed": state.get("stack_ai_passed", False),
        "_next_instance_id": state.get("_next_instance_id", 1),
    }


def _pvp_play_troop(handler, session, played_card_uid, my_pid):
    """Play a non-resource permanent in PvP: hand -> CastSpells -> Warzone,
    fire enters-play triggers, and push the events to BOTH players.  Returns
    True when handled."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    crow = _db.execute(
        "SELECT gc.template_guid, ct.card_type, ct.name, ct.abilities_json "
        "FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session.session_id, int(played_card_uid))).fetchone()
    if not crow:
        return False
    tpl_guid, card_type, card_name = crow[0], crow[1], crow[2]
    ctype_num = _ge.card_type_from_db(card_type)
    is_permanent = bool(ctype_num & (_ge.ECardTypes.Troop |
                                     _ge.ECardTypes.Artifact |
                                     _ge.ECardTypes.Constant))
    if not is_permanent:
        log_req(f"    PvP: {card_name} is an action — not yet resolved")
        return False
    scid = _ge.SessionCardId(_ge.UID(int(played_card_uid)))
    my_uid = _ge.UID.make(244, my_pid)
    opp_uid = _ge.UID.make(244, opp_pid)
    state = pvp_load_state(session) or {}
    # Defense-in-depth cost check: the client's options are the normal gate,
    # but don't let a drag play an unaffordable card and go negative.
    from db import db_template_by_guid
    _trow = db_template_by_guid(tpl_guid)
    cost = _trow[3] if _trow else 0
    # Effective cost (static cost modifiers) — charge what the client showed.
    try:
        from abilities.framework.statics import effective_cost as _ec
        cost = _ec(_db, session.session_id,
                   _pvp_fra_view(state, my_pid, opp_pid), int(played_card_uid))
    except Exception:
        pass
    available = int(state.get(f"res_{my_pid}", 0))
    if cost > available:
        log_req(f"    PvP REJECTED play {card_name}: cost {cost} > "
                f"resources {available}")
        pvp_push_main_phase_options(session, state)
        return True
    # Pay the cost FIRST (before any resolution) so the resource pool is
    # correct regardless of triggers firing.
    state[f"res_{my_pid}"] = available - cost
    view = _pvp_fra_view(state, my_pid, opp_pid)
    # Move the card onto the CHAIN (CastSpells visual) — mirroring how spells
    # are cast.  Troops/artifacts/constants do NOT resolve instantly: they stay
    # on the stack so the opponent can respond (e.g. Countermagic) before they
    # resolve to the warzone via the both-pass chain flow in _pvp_resolve_chain.
    _db.execute(
        "UPDATE game_cards SET location='CastSpells', position=0 "
        "WHERE session_id=? AND card_uid=?",
        (session.session_id, int(played_card_uid)))
    _db.commit()
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    _pvp_populate_game_state(g, state, my_pid, opp_pid)
    # Register the CardDef FIRST so every CardUpdated carries the full stats
    # (mirrors the spell path).
    _tpl, ct, _n, cost2, atk, def_, _gx = \
        handler._card_full_data(g, scid, tpl_guid)
    g.push_card_updated(scid, my_uid, _ge.ECardCollections.CastSpells,
                        ctype_num, template_id=tpl_guid, cost=cost2,
                        attack=atk, defense=def_)
    g.push_card_moved(scid, my_uid, _ge.ECardCollections.CastSpells,
                      _ge.ECardLocations.Top, 0)
    # Push the permanent onto the chain as a "troop" item.
    import battle_engine as _be
    inst_id = int(state.get("_next_instance_id", 1))
    state["_next_instance_id"] = inst_id + 1
    _be.stack_push(state, {
        "kind": "troop", "source_uid": int(played_card_uid),
        "ability_guids": [], "target_uid": None,
        "instance_id": inst_id, "x_cost": 0,
    })
    # The card's presence on the chain: the client populates ChainView ONLY
    # from AbilityPushedOnChain (GoChainView has no CastSpells zone mapping, and
    # resources are the only cards moved there explicitly).  OnAbilityPushedOnChain
    # is gated on TemplateManager.Abilities.ContainsKey(AbilityTemplateId), so the
    # template id MUST be a valid client ability template — a card template GUID
    # fails the gate and the chain stays empty for BOTH players.  Use the troop's
    # OWN first ability GUID when it has one (a real AbilityTemplate), else the
    # client's built-in PlayCardAbilityTemplateId (always registered, semantically
    # "cast this card").  The card rendered is still the ACTUAL card instance
    # (SourceCardId -> non-clone AddAbilityInstanceWithSource path), with its real
    # stats/buffs — the ability id is only the client's chain-render key.
    import json as _chj
    _chain_tpl = _ge.PLAY_CARD_ABILITY_TEMPLATE_ID
    try:
        _tabs = _chj.loads(crow[3]) if len(crow) > 3 and crow[3] else []
        if _tabs:
            _chain_tpl = str(_tabs[0]).lower()
    except Exception:
        pass
    g.push_ability_on_chain(scid, _ge.ResourceId.from_str(_chain_tpl),
                            ability_instance_id=inst_id)
    state["stack"] = view.get("stack") or []
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False
    state["stack_passed"] = []
    pvp_save_state(session, state)
    # Reflect the cost paid on BOTH clients (objective UIDs).
    g.player_resources = int(state.get(f"res_{my_pid}", 0))
    g.player_total_resources = int(state.get(f"res_total_{my_pid}", 0))
    g.ai_resources = int(state.get(f"res_{opp_pid}", 0))
    g.ai_total_resources = int(state.get(f"res_total_{opp_pid}", 0))
    ev_spent = _ge.PlayerCurrentResourcePoolChangedSessionEventArgs()
    ev_spent.player_id = my_uid
    ev_spent.operation = 2
    ev_spent.delta = cost
    ev_spent.new_value = g.player_resources
    g._push(ev_spent)
    champ_map = state.get("champ_map") or {}
    for target_pid in pids:
        target_uid = _ge.UID.make(244, target_pid)
        cu = int(champ_map.get(str(target_pid), 0))
        champ_scid = _ge.SessionCardId(_ge.UID(cu)) if cu else None
        g.push_player_updated(target_uid, champ_id=champ_scid)
    _pvp_send_same_events(session, g, my_uid, opp_uid)
    log_req(f"    PvP troop play: {card_name} by pid {my_pid} (paid {cost}, "
            f"stack={len(state.get('stack') or [])} item(s))")
    _pvp_log_stack(state, f"troop-play {card_name}")
    # The card is on the stack.  Priority passes to the OPPONENT FIRST (they
    # get the response window / Resolve on the chain item), then back to the
    # caster — mirroring the PvP flow where the non-actor responds to a cast
    # before the actor resolves it.  The both-pass stack rule in route_pvp_pass
    # hands priority to the other player after the first pass; seeding the
    # opponent as priority here means THEY click Resolve first.
    opp_h = player_handlers.get(opp_pid)
    if opp_h and not pvp_player_auto_passes(state, opp_pid):
        gg = _ge.Game(int(session.session_id), opp_uid, my_uid)
        gg.push_green_light(opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
        _send_pvp_packet(opp_h, session, gg, opp_uid, "troop-on-stack-opp")
        # Offer the opponent their quick actions so they can respond first.
        try:
            pvp_push_phase_options(session, state, pid=opp_pid)
        except Exception as _e:
            log_req(f"    PvP troop-on-stack opp options error: {_e}")
    # Explicitly clear the caster's local green-light as well.  The play-card
    # UI normally does this optimistically, but a second authoritative packet
    # prevents the activating client from retaining a stale priority state
    # when the opponent is the first responder.
    caster_h = player_handlers.get(my_pid)
    if caster_h:
        caster_game = _ge.Game(int(session.session_id), my_uid, opp_uid)
        caster_game.push_green_light(opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
        _send_pvp_packet(caster_h, session, caster_game, my_uid,
                         "troop-on-stack-caster-lost")
    state["priority_pid"] = opp_pid
    pvp_save_state(session, state)
    _pvp_log_stack(state, f"troop-play-{card_name}-opp-first")
    _pvp_auto_pass_chain_priority(session, state, opp_pid)
    return True


def _pvp_offer_opponent_response(session, state, caster_pid):
    """After the turn player (caster_pid) plays a card in a main phase, offer
    the OPPONENT a priority window to respond with a quick action (e.g. Burn).
    Hands ResolveTopOfChain + quick-action options to the opponent and marks
    state so route_pvp_pass returns priority to the caster when the opponent
    passes (or after the opponent's response chain resolves)."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    opp_pid = pids[0] if pids[1] == caster_pid else pids[1]
    opp_h = player_handlers.get(opp_pid)
    caster_uid = _ge.UID.make(244, caster_pid)
    opp_uid = _ge.UID.make(244, opp_pid)
    # Record that we are waiting on the opponent's response; when they pass,
    # route_pvp_pass will hand priority back to the caster.
    state["response_waiting_pid"] = opp_pid
    state["response_caster_pid"] = caster_pid
    state["priority_pid"] = opp_pid
    pvp_save_state(session, state)
    if opp_h:
        # Greenlight first (opponent HAS priority), then their quick-action
        # options — so the client shows the response window immediately.
        g = _ge.Game(int(session.session_id), opp_uid, caster_uid)
        g.push_green_light(opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
        _send_pvp_packet(opp_h, session, g, opp_uid, "response-window")
        # Offer the opponent their quick actions + champion powers so Burn etc.
        # light up.
        pvp_push_phase_options(session, state, pid=opp_pid)
    log_req(f"    PvP response window: offering {opp_pid} priority to respond "
            f"to {caster_pid}'s play")


def _pvp_offer_trigger_response(session, state, caster_pid):
    """Offer the opponent priority for a newly-created trigger chain."""
    _pvp_offer_opponent_response(session, state, caster_pid)
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    opp_pid = pids[0] if pids[1] == caster_pid else pids[1]
    caster_h = player_handlers.get(caster_pid)
    if caster_h:
        caster_uid = _ge.UID.make(244, caster_pid)
        opp_uid = _ge.UID.make(244, opp_pid)
        g = _ge.Game(int(session.session_id), caster_uid, opp_uid)
        g.push_green_light(opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
        _send_pvp_packet(caster_h, session, g, caster_uid,
                         "trigger-on-stack-caster-lost")
    _pvp_auto_pass_chain_priority(session, state, opp_pid)


def _pvp_play_spell(handler, session, played_card_uid, my_pid, inner_bytes):
    """Cast a BasicAction/QuickAction spell in PvP: hand -> CastSpells, push
    the spell onto the chain, then resolve its BOM when the chain resolves and
    send CastSpells -> Discard.  A player may cast a QuickAction any time they
    hold priority and can pay the cost.  Pushes events to BOTH players."""
    import json as _js
    import battle_engine as _be
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    crow = _db.execute(
        "SELECT gc.template_guid, ct.card_type, ct.name, ct.abilities_json "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session.session_id, int(played_card_uid))).fetchone()
    if not crow:
        return False
    tpl_guid, card_type, card_name, ab_json = crow
    scid = _ge.SessionCardId(_ge.UID(int(played_card_uid)))
    my_uid = _ge.UID.make(244, my_pid)
    opp_uid = _ge.UID.make(244, opp_pid)
    state = pvp_load_state(session) or {}
    # Defense-in-depth cost check; pay FIRST so resources are right even if
    # the resolution is interrupted.
    from db import db_template_by_guid
    _trow = db_template_by_guid(tpl_guid)
    cost = _trow[3] if _trow else 0
    # Effective cost (static cost modifiers) — charge what the client showed.
    try:
        from abilities.framework.statics import effective_cost as _ec
        cost = _ec(_db, session.session_id,
                   _pvp_fra_view(state, my_pid, opp_pid), int(played_card_uid))
    except Exception:
        pass
    available = int(state.get(f"res_{my_pid}", 0))
    if cost > available:
        log_req(f"    PvP REJECTED spell {card_name}: cost {cost} > "
                f"resources {available}")
        pvp_push_main_phase_options(session, state)
        return True
    state[f"res_{my_pid}"] = available - cost
    # Variable X cost: the X the player chose in the client's X-cost dialog
    # travels as xCostData.m_ResourceXCost on the play transaction.
    try:
        x_cost = handler._extract_int32_field(inner_bytes, "m_ResourceXCost")
        x_cost = max(0, int(x_cost or 0))
    except Exception:
        x_cost = 0
    if x_cost:
        state[f"res_{my_pid}"] = max(0, int(state.get(f"res_{my_pid}", 0)) - x_cost)
        log_req(f"    PvP spell {card_name}: X cost {x_cost} paid "
                f"(resources left {state.get(f'res_{my_pid}')})")
    view = _pvp_fra_view(state, my_pid, opp_pid)
    # The client's targeting picker sends the chosen target in the transaction.
    try:
        targets = handler._extract_transaction_targets(
            inner_bytes, int(played_card_uid))
    except Exception:
        targets = []
    target_uid = targets[-1] if targets else None
    view["player_spell_target"] = target_uid
    view["resolving_owner_id"] = my_pid
    view["resolving_source_uid"] = int(played_card_uid)
    _db.execute(
        "UPDATE game_cards SET location='CastSpells', position=0 "
        "WHERE session_id=? AND card_uid=?",
        (session.session_id, int(played_card_uid)))
    _db.commit()
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    _pvp_populate_game_state(g, state, my_pid, opp_pid)
    _tpl, ct, _n, cost2, atk, def_, _gx = \
        handler._card_full_data(g, scid, tpl_guid)
    g.push_card_updated(scid, my_uid, _ge.ECardCollections.CastSpells, ct,
                        template_id=tpl_guid, cost=cost2, attack=atk,
                        defense=def_)
    g.push_card_moved(scid, my_uid, _ge.ECardCollections.CastSpells,
                      _ge.ECardLocations.Top, 0)
    g.push_spell_card_cast(scid, my_uid, free=False)
    try:
        ability_guids = [x.lower() for x in _js.loads(ab_json)] if ab_json else []
    except Exception:
        ability_guids = []
    inst_id = int(state.get("_next_instance_id", 1))
    state["_next_instance_id"] = inst_id + 1
    _be.stack_push(state, {
        "kind": "spell", "source_uid": int(played_card_uid),
        "ability_guids": ability_guids, "target_uid": target_uid,
        "instance_id": inst_id, "x_cost": x_cost,
    })
    # Chain entry: must carry a VALID client ability template (the client's
    # OnAbilityPushedOnChain is gated on TemplateManager.Abilities.ContainsKey).
    # Use the spell's first ability GUID (a real AbilityTemplate), else the
    # client built-in PlayCardAbilityTemplateId.
    _chain_tpl2 = (_ge.PLAY_CARD_ABILITY_TEMPLATE_ID
                   if not ability_guids else ability_guids[0])
    g.push_ability_on_chain(scid,
                            _ge.ResourceId.from_str(_chain_tpl2),
                            ability_instance_id=inst_id)
    state["stack"] = view.get("stack") or []
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False
    state["stack_passed"] = []
    pvp_save_state(session, state)
    # Reflect the cost paid on BOTH clients' resource displays (the event
    # carries the caster's absolute UID, so each client renders "You spent"
    # vs "Opponent spent" from its own perspective).
    g.player_health = int(state.get(f"hp_{my_pid}", 20))
    g.ai_health = int(state.get(f"hp_{opp_pid}", 20))
    g.player_resources = int(state.get(f"res_{my_pid}", 0))
    g.player_total_resources = int(state.get(f"res_total_{my_pid}", 0))
    g.ai_resources = int(state.get(f"res_{opp_pid}", 0))
    g.ai_total_resources = int(state.get(f"res_total_{opp_pid}", 0))
    ev_spent = _ge.PlayerCurrentResourcePoolChangedSessionEventArgs()
    ev_spent.player_id = my_uid
    ev_spent.operation = 2
    ev_spent.delta = cost + x_cost
    ev_spent.new_value = g.player_resources
    g._push(ev_spent)
    champ_map = state.get("champ_map") or {}
    for _tpid in pids:
        _tuid = _ge.UID.make(244, _tpid)
        cu = int(champ_map.get(str(_tpid), 0))
        champ_scid = _ge.SessionCardId(_ge.UID(cu)) if cu else None
        g.push_player_updated(_tuid, champ_id=champ_scid)
    _pvp_send_same_events(session, g, my_uid, opp_uid)
    log_req(f"    PvP spell cast: {card_name} by pid {my_pid} (paid {cost}, "
            f"target={hex(target_uid) if target_uid else None}, "
            f"stack={len(state.get('stack') or [])})")
    _pvp_log_stack(state, f"spell-cast {card_name}")
    # The card is on the stack.  Priority passes to the OPPONENT FIRST (they get
    # the response window / Resolve on the spell), then back to the caster —
    # mirroring the PvP flow where the non-actor responds to a cast first.
    try:
        opp_h = player_handlers.get(opp_pid)
        if opp_h and not pvp_player_auto_passes(state, opp_pid):
            gg = _ge.Game(int(session.session_id), opp_uid, my_uid)
            gg.push_green_light(opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(opp_h, session, gg, opp_uid, "chain-priority-opp")
            try:
                pvp_push_phase_options(session, state, pid=opp_pid)
            except Exception:
                pass
        state["priority_pid"] = opp_pid
        pvp_save_state(session, state)
        _pvp_log_stack(state, f"spell-cast-{card_name}-opp-first")
        _pvp_auto_pass_chain_priority(session, state, opp_pid)
    except Exception as e:
        # Never let a post-play refresh (greenlight/options) kill the session
        # thread and disconnect both clients — log and return True so the
        # transaction is acked and the game keeps running.
        import traceback
        log_req(f"    PvP spell post-refresh error: {e}")
        traceback.print_exc()
    return True


def _pvp_activate_champion_ability(handler, session, inner_bytes, my_pid):
    """Activate the pid's champion charge/spell power (ActivateAbilityTransaction
    from the champion ability button): extract the ability GUID + chosen target,
    pay the charge/spell cost from the PvP state, push the ability onto the
    chain, and hand the caster priority (ResolveTopOfChain) so the chain
    resolution (both-pass) resolves its BOM through _pvp_resolve_chain."""
    import re as _rre
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    state = pvp_load_state(session) or {}
    ability_guid = None
    if isinstance(inner_bytes, bytes):
        m = _rre.search(
            rb'AbilityTemplateId;[^;]*;[^;]*;[^;]*;[^;]*;[^;]*;[^;]*;'
            rb'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
            rb'[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', inner_bytes)
        if not m:
            aidx = inner_bytes.find(b"AbilityTemplateId")
            if aidx >= 0:
                m2 = _rre.search(
                    rb'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                    rb'[0-9a-fA-F]{4}-[0-9a-fA-F]{12})',
                    inner_bytes[aidx:aidx + 300])
                if m2:
                    m = m2
        if m:
            ability_guid = m.group(1).decode().lower()
    if not ability_guid:
        log_req(f"    PvP champion ability: could not parse GUID "
                f"(pid {my_pid})")
        return False
    champ_map = state.get("champ_map") or {}
    my_champ_uid = int(champ_map.get(str(my_pid), 0))
    if not my_champ_uid:
        return False
    # Affordability: charge/spell cost from champion_abilities / talents.
    from db import (db_champion_ability_costs, db_talent_ability_costs,
                    db_champion_ability_thresholds)
    row = db_champion_ability_costs(ability_guid)
    if row is None:
        row = db_talent_ability_costs(ability_guid)
    cc = int(row[0] or 0) if row else 0
    sc = int(row[1] or 0) if row else 0
    spell_points = int(state.get(f"sp_{my_pid}", 0))
    spell_uses = dict(state.get(f"sp_uses_{my_pid}") or {})
    effective_sc = (sc + int(spell_uses.get(str(ability_guid), 0) or 0)
                    if sc else 0)
    activatable_phases = int(row[2] or 0) if row and len(row) > 2 else 0
    casting = int(row[3] or 0) if row and len(row) > 3 else 64
    phase = int(state.get("phase", 0))
    # The client normally hides BasicAction powers outside the controller's
    # main phases, but stale option packets can still submit an activation.
    # Enforce the same gamedata-derived restriction on the server so a power
    # such as Dimmid's cannot be used during Declare Attackers.
    if casting != 64 and state.get("turn_pid") != my_pid:
        log_req(f"    PvP champion ability {ability_guid[:8]}: not "
                f"{my_pid}'s turn in phase {phase} — rejected")
        return True
    if activatable_phases and not (activatable_phases & (1 << phase)):
        log_req(f"    PvP champion ability {ability_guid[:8]}: phase "
                f"{phase} not in mask {activatable_phases:#x} — rejected")
        return True
    charges = int(state.get(f"chg_{my_pid}", 0))
    if charges < cc:
        log_req(f"    PvP champion ability {ability_guid[:8]}: need "
                f"{cc} charges, have {charges} — rejected")
        return True
    if spell_points < effective_sc:
        log_req(f"    PvP champion ability {ability_guid[:8]}: need "
                f"{effective_sc} spell points, have {spell_points} — rejected")
        return True
    reqs = db_champion_ability_thresholds(ability_guid)
    threshold = dict(state.get(f"thresh_{my_pid}") or {})
    if reqs:
        from game_engine import SHARD_TO_FLAG
        for color, qty in reqs:
            flag = SHARD_TO_FLAG.get(str(color).lower(), 0)
            if flag:
                # thresh_<pid> keys are STRINGS after the JSON round-trip.
                _tv = threshold.get(flag)
                if _tv is None:
                    _tv = threshold.get(str(flag), 0)
                if int(_tv or 0) < qty:
                    log_req(f"    PvP champion ability {ability_guid[:8]}: "
                            f"threshold {color} {qty} unmet — rejected")
                    return True
    # A champion transaction can contain both card-payment selections and an
    # effect target.  Separate them using the authored target templates; a
    # sacrifice target must not become the +4/+4 target.
    all_uids = _pvp_transaction_card_uids(inner_bytes)
    champ_targets = []
    for cpid in state.get("pids") or pids:
        cuid = int(champ_map.get(str(cpid), 0))
        if cuid:
            champ_targets.append((
                cuid, int(cpid), "Champion",
                int(state.get(f"hp_{cpid}", 20))))
    selection = _pvp_select_champion_activation_targets(
        session, state, my_pid, my_champ_uid, ability_guid, all_uids,
        champ_targets)
    if selection is None:
        log_req(f"    PvP champion ability {ability_guid[:8]}: missing/illegal "
                "payment or effect target — rejected")
        return True
    target_uid, sacrifice_uids = selection
    if all_uids:
        # Multi-target void powers need the complete selected list in the
        # resolver, while target_uid remains the ordinary effect target.
        state["champion_void_uids"] = all_uids
    state[f"chg_{my_pid}"] = charges - cc
    state[f"sp_{my_pid}"] = spell_points - effective_sc
    if sc:
        spell_uses[str(ability_guid)] = int(
            spell_uses.get(str(ability_guid), 0) or 0) + 1
        state[f"sp_uses_{my_pid}"] = spell_uses
    import battle_engine as _be
    inst_id = int(state.get("_next_instance_id", 1))
    state["_next_instance_id"] = inst_id + 1
    _be.stack_push(state, {
        "kind": "ability", "ability_guid": ability_guid,
        "source_uid": my_champ_uid, "target_uid": target_uid,
        "instance_id": inst_id,
    })
    pvp_save_state(session, state)
    my_uid = _ge.UID.make(244, my_pid)
    opp_uid = _ge.UID.make(244, opp_pid)
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    _pvp_populate_game_state(g, state, my_pid, opp_pid)
    for sacrifice_uid in sacrifice_uids:
        handler._sacrifice_troop(
            g, session, my_uid, opp_uid, int(sacrifice_uid))
    g.player_charges = int(state.get(f"chg_{my_pid}", 0))
    g.push_ability_on_chain(_ge.SessionCardId(_ge.UID(my_champ_uid)),
                            _ge.ResourceId.from_str(ability_guid),
                            ability_instance_id=inst_id)
    # CardActivatedEvent is distinct from charge-point gain.  It drives
    # metadata-defined passives such as Lorenzo's "when you use a charge
    # power, copy that ability" and carries the original activation so the
    # CopyAbility leaf can put the correct ability back on the chain.
    state["activated_ability_guid"] = ability_guid
    state["activated_source_uid"] = my_champ_uid
    state["activated_target_uid"] = target_uid
    from abilities.framework.triggers import resolve_triggers
    resolve_triggers(_db, handler, g, session, my_uid, opp_uid, state,
                     "CardActivatedEvent", my_champ_uid, my_pid)
    state.pop("activated_ability_guid", None)
    state.pop("activated_source_uid", None)
    state.pop("activated_target_uid", None)
    # Refresh HUD (charges) + hand/deck counts for both players.
    g.player_health = int(state.get(f"hp_{my_pid}", 20))
    g.ai_health = int(state.get(f"hp_{opp_pid}", 20))
    g.player_resources = int(state.get(f"res_{my_pid}", 0))
    g.player_total_resources = int(state.get(f"res_total_{my_pid}", 0))
    g.ai_resources = int(state.get(f"res_{opp_pid}", 0))
    g.ai_total_resources = int(state.get(f"res_total_{opp_pid}", 0))
    g.player_spell_points = int(state.get(f"sp_{my_pid}", 0))
    g.ai_spell_points = int(state.get(f"sp_{opp_pid}", 0))
    if effective_sc:
        ev = _ge.ChampionSpellPointsChangedSessionEventArgs()
        ev.player_id = my_uid
        ev.operation = 2
        ev.delta = effective_sc
        ev.new_value = int(state.get(f"sp_{my_pid}", 0))
        g._push(ev)
    for target_pid in pids:
        t_uid = _ge.UID.make(244, target_pid)
        cu = int(champ_map.get(str(target_pid), 0))
        g.push_player_updated(t_uid,
                              champ_id=_ge.SessionCardId(_ge.UID(cu)) if cu
                              else None)
    _pvp_send_same_events(session, g, my_uid, opp_uid)
    log_req(f"    PvP champion ability activated: {ability_guid[:8]} by "
            f"pid {my_pid} (charges {charges}->{state.get(f'chg_{my_pid}')}, "
            f"target={hex(target_uid) if target_uid else None})")
    # The caster holds priority to resolve the chain.
    turn_h = player_handlers.get(my_pid)
    if turn_h:
        gg = _ge.Game(int(session.session_id), my_uid, opp_uid)
        gg.push_green_light(my_uid, _ge.EPriorityContext.ResolveTopOfChain)
        _send_pvp_packet(turn_h, session, gg, my_uid, "ability-chain-priority")
    state["priority_pid"] = my_pid
    pvp_save_state(session, state)
    _state_refresh = pvp_load_state(session) or {}
    if _state_refresh.get("phase") in (_ge.ETurnPhases.FirstMainPhase,
                                       _ge.ETurnPhases.SecondMainPhase):
        pvp_push_main_phase_options(session, _state_refresh)
    return True


def _pvp_declare_attackers(handler, session, inner_bytes, my_pid):
    """Record the turn player's declared attackers and push the combat
    listing to both players."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    state = pvp_load_state(session) or {}
    if state.get("turn_pid") != my_pid:
        return False
    attacker_uids = []
    if isinstance(inner_bytes, bytes):
        for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                                inner_bytes):
            try:
                import struct as _st
                uid64 = _st.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                if (uid64 & 0xFF) == 1:
                    attacker_uids.append(int(uid64))
            except Exception:
                continue
    # Treat the transaction as untrusted input.  The client normally only
    # includes cards marked with ECardUsage.Attack, but a stale/forged
    # transaction must not turn Constants, Artifacts, or summoning-sick cards
    # into attackers.  This also keeps the server rule identical to the list
    # offered by pvp_push_attack_options above.
    wz_rows = _db.execute(
        "SELECT gc.card_uid, gc.card_state, "
        "(ct.attributes | gc.card_attributes | "
        "COALESCE(gc.temporary_attributes, 0)) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%'",
        (session.session_id, my_pid)).fetchall()
    wz = set()
    for uid, cstate, attrs in wz_rows:
        cstate = int(cstate or 0)
        attrs = int(attrs or 0)
        if ((cstate & (_ge.ECardStates.Tapped |
                       _ge.ECardStates.Attacking)) or
                attrs & (_ge.ECardAttributes.CantAttack |
                         _ge.ECardAttributes.Defensive) or
                not ((cstate & _ge.ECardStates.StartedATurnOnYourSide) or
                     attrs & _ge.ECardAttributes.Speed)):
            continue
        wz.add(int(uid))
    attacker_uids = [u for u in attacker_uids if u in wz]
    champ_map = state.get("champ_map") or {}
    my_champ = int(champ_map.get(str(my_pid), 0))
    # MERGE the manually-committed attackers with what's ALREADY declared in
    # state — which includes auto-declared ForceAttack troops (Savage Raider
    # "must attack") that the server pushed at DeclareAttack.  Without this
    # merge, a CommitTroopsToAttackTransaction carrying only the manually
    # selected UIDs overwrites the forced attacker, it vanishes, and combat is
    # skipped -> the forced troop never attacks / deals no damage.
    existing = {int(k): int(v)
                for k, v in (state.get("attackers") or {}).items()}
    merged = dict(existing)
    for u in attacker_uids:
        merged[u] = my_champ
    attackers = list(merged.keys())
    state["attackers"] = {str(u): str(v) for u, v in merged.items()}
    pvp_save_state(session, state)
    my_uid = _ge.UID.make(244, my_pid)
    opp_uid = _ge.UID.make(244, opp_pid)
    # Mark each attacker Attacking|HasAttacked (+Tapped UNLESS Steadfast) in
    # the DB and build ONE objective event stream (AttackDeclared + CombatListing
    # + CardUpdated with the new state + "when this attacks" trigger events)
    # that goes to BOTH players — mirrors PvE _auto_declare_force_attackers /
    # the CommitTroopsToAttack handler.  Only the NEWLY committed attackers get
    # events here; auto-declared ForceAttack troops already got theirs when the
    # phase opened (in pvp_push_attack_options), so no duplicate push (mirrors
    # PvE: `new_attackers = [u for u in attackers if u not in existing]`).
    new_attackers = [u for u in attacker_uids if u not in existing]
    from db import db_card_set_attacking_state, db_card_state_raw, \
        db_card_template_attrs_joined
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    g.player_health = int(state.get(f"hp_{my_pid}", 20))
    g.ai_health = int(state.get(f"hp_{opp_pid}", 20))
    combats = []
    for i, u in enumerate(new_attackers):
        cid = _ge.CombatId(my_uid, i + 1)
        scid = _ge.SessionCardId(_ge.UID(u))
        g.push_attack_declared(cid, my_uid,
                               _ge.SessionCardId(_ge.UID(my_champ)) if my_champ
                               else _ge.SessionCardId(opp_uid), scid)
        trow = db_card_template_attrs_joined(session.session_id, int(u))
        tpl_guid = trow[0] if trow else None
        attrs = (trow[1] if trow and trow[1] else 0) | \
                (trow[2] if trow and trow[2] else 0)
        cstate = (_ge.ECardStates.Attacking |
                  _ge.ECardStates.HasAttacked)
        if not (attrs & _ge.ECardAttributes.Steadfast):
            cstate |= _ge.ECardStates.Tapped
        db_card_set_attacking_state(session.session_id, int(u), cstate)
        pushed_state = db_card_state_raw(session.session_id, int(u))
        if not pushed_state:
            pushed_state = cstate
        handler._card_full_data(g, scid, tpl_guid)
        g.push_card_updated(scid, my_uid, _ge.ECardCollections.Warzone,
                            _ge.ECardTypes.Troop, template_id=tpl_guid,
                            state=pushed_state)
        cs = _ge.CombatSessionEventArgs()
        cs.player_id = my_uid
        cs.id = cid
        cs.attacker = scid
        cs.blockers = []
        combats.append(cs)
        # "When this attacks" triggers + Rage.
        from abilities.framework.triggers import resolve_triggers
        view = _pvp_fra_view(state, my_pid, opp_pid)
        resolve_triggers(_db, handler, g, session, my_uid, opp_uid, view,
                         "CardAttackedEvent", int(u), my_pid)
        resolve_triggers(_db, handler, g, session, my_uid, opp_uid, view,
                         "CardAttackedOrBlockedEvent", int(u), my_pid)
        from abilities.framework.keywords.combat import apply_rage_keyword
        apply_rage_keyword(_db, session, handler, g, my_uid, opp_uid, view,
                           int(u))
        # Persist any trigger/rage health changes.
        if view.get("player_health") is not None:
            state[f"hp_{my_pid}"] = int(view["player_health"])
        if view.get("ai_health") is not None:
            state[f"hp_{opp_pid}"] = int(view["ai_health"])
        pvp_save_state(session, state)
    _db.commit()
    if combats:
        g.push_combat_listing(my_uid, combats)
    _pvp_send_same_events(session, g, my_uid, opp_uid)
    log_req(f"    PvP attack: pid {my_pid} declared {len(attackers)} attacker(s)")
    # No attackers declared (e.g. every troop is summoning sick): skip the
    # remaining combat steps straight to SecondMainPhase instead of leaving
    # the game stuck waiting for passes through DeclareDefense/AssignDamage.
    if not attackers:
        pvp_skip_to_second_main(session, state)
        return True
    # Attackers WERE declared: the combat now moves to the defender.  Advance
    # through DeclareAttackPriorityWindow (13, response window) into
    # DeclareDefense (14) and hand the DEFENDER priority + blocker options so
    # they can set up blockers.  The responder (attacker) gets a QuickAction
    # window at 13; the defender acts at 14.
    pvp_advance_to_declare_defense(session, state)
    return True


def pvp_advance_to_declare_defense(session, state):
    """Advance the PvP phase from DeclareAttack (12) through
    DeclareAttackPriorityWindow (13) to DeclareDefense (14), handing priority
    to the DEFENDER at 14 (mirrors PvE: after attackers are declared the
    defender is the one who must act/block).  Returns True if it reached
    DeclareDefense."""
    import battle_engine as _be
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    turn_pid = state.get("turn_pid")
    opp_pid = pids[0] if pids[1] == turn_pid else pids[1]
    phase_list = _be.COMBAT_TURN_PHASES
    try:
        cur = phase_list.index(int(state.get("phase", 12)))
    except ValueError:
        cur = phase_list.index(12)
    while True:
        cur += 1
        if cur >= len(phase_list):
            return False
        new_phase = phase_list[cur]
        state["phase"] = new_phase
        state["passes"] = []
        pvp_save_state(session, state)
        log_req(f"    PvP post-attack: phase {new_phase} to both")
        _pvp_run_phase_start(session, state, new_phase)
        if new_phase == _ge.ETurnPhases.DeclareDefense:
            # The defender holds priority and sees the blocker options.
            state["priority_pid"] = opp_pid
            pvp_save_state(session, state)
            # If the defender has NO eligible blockers (or nothing blocks any
            # attacker), they have nothing to do at DeclareDefense — auto-pass
            # them (emit an empty BlockersAssigned) and advance to the
            # responder window, mirroring PvE ai_pass_declare_defense.
            blockable = _pvp_defender_blockable_count(session, state)
            if blockable <= 0:
                log_req(f"    PvP post-attack: defender {opp_pid} has "
                        f"{blockable} blocker(s) — auto-passing DeclareDefense")
                try:
                    pvp_push_empty_blockers(session, state)
                except Exception as e:
                    log_req(f"    PvP empty-blockers error: {e}")
                continue
            log_req(f"    PvP post-attack: at DeclareDefense, priority to "
                    f"defender {opp_pid} ({blockable} blocker(s) available)")
            return True
        if new_phase == _ge.ETurnPhases.DeclareDefensePriorityWindow:
            # The defender auto-passed (no blockers); enter the responder
            # window and hand priority to the TURN player so combat can
            # proceed to AssignFirstStrikeDamage.  Stop here and let the
            # normal pass cycle carry it forward.
            state["priority_pid"] = turn_pid
            pvp_save_state(session, state)
            log_req(f"    PvP post-attack: at DeclareDefensePriorityWindow, "
                    f"priority to turn player {turn_pid} (defender had no "
                    f"blockers)")
            return True
    return False


def pvp_push_empty_blockers(session, state):
    """Push an empty BlockersAssigned for each attacker (the defender declines
    to block) to BOTH players — mirrors PvE ai_pass_declare_defense."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    turn_pid = state.get("turn_pid")
    opp_pid = pids[0] if pids[1] == turn_pid else pids[1]
    attackers = {int(k): int(v) for k, v in (state.get("attackers") or {}).items()}
    if not attackers:
        return
    my_uid = _ge.UID.make(244, turn_pid)
    opp_uid = _ge.UID.make(244, opp_pid)
    my_champ = int((state.get("champ_map") or {}).get(str(turn_pid), 0))
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    g.player_health = int(state.get(f"hp_{turn_pid}", 20))
    g.ai_health = int(state.get(f"hp_{opp_pid}", 20))
    for i, u in enumerate(attackers):
        cid = _ge.CombatId(my_uid, i + 1)
        g.push_blockers_assigned(
            cid, _ge.SessionCardId(_ge.UID(int(u))),
            _ge.SessionCardId(_ge.UID(my_champ)) if my_champ
            else _ge.SessionCardId(opp_uid), [])
    _pvp_send_same_events(session, g, my_uid, opp_uid)
    log_req(f"    PvP empty blockers assigned for "
            f"{len(attackers)} attacker(s)")


def pvp_skip_to_second_main(session, state):
    """Advance an empty attack through the client-safe path to SecondMain.

    DeclareAttackPriorityWindow is the only intermediate phase needed here:
    the client permits that priority window to transition directly to
    SecondMainPhase when CombatManager has no combats.  Do not push blocker or
    damage phases for an empty attack; those states can make the client emit
    spurious damage transactions for a combat that does not exist.
    """
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    attack_priority = _ge.ETurnPhases.DeclareAttackPriorityWindow
    second_main = _ge.ETurnPhases.SecondMainPhase
    # From DeclareAttack the client's state machine requires the attack
    # priority window before it accepts SecondMain.  If this helper is called
    # after that point, the direct destination is already valid.
    phases_to_push = []
    if int(state.get("phase", 12)) < attack_priority:
        phases_to_push.append(attack_priority)
    phases_to_push.append(second_main)
    for new_phase in phases_to_push:
        state["phase"] = new_phase
        state["passes"] = []
        pvp_save_state(session, state)
        log_req(f"    PvP skip combat: phase {new_phase} to both")
        _pvp_run_phase_start(session, state, new_phase)


def pvp_combat_has_swiftstrike(session, state):
    """Return whether the current PVP combat has a FirstStrike/DualStrike
    combatant.

    The client checks both sides of every combat, not only the attackers.  Use
    the live joined attributes so a Quick Action's temporary Swiftstrike grant
    is visible when this is called after the blocker response window.
    """
    uids = set()
    for uid in (state.get("attackers") or {}):
        try:
            uids.add(int(uid))
        except (TypeError, ValueError):
            continue
    for blockers in (state.get("blockers") or {}).values():
        for uid in (blockers or []):
            try:
                uids.add(int(uid))
            except (TypeError, ValueError):
                continue
    if not uids:
        return False
    marks = ",".join("?" * len(uids))
    rows = _db.execute(
        "SELECT ct.attributes | gc.card_attributes | "
        "COALESCE(gc.temporary_attributes, 0) "
        "FROM game_cards gc JOIN card_templates ct "
        "ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid IN (%s)" % marks,
        [session.session_id] + list(uids)).fetchall()
    swiftstrike = (_ge.ECardAttributes.FirstStrike |
                   _ge.ECardAttributes.DualStrike)
    return any(int(row[0] or 0) & swiftstrike for row in rows)


def pvp_phase_after_blockers(session, state):
    """Select the first phase after the blocker response window.

    This must be evaluated at the end of DeclareDefensePriorityWindow, after
    both players had their Quick Action opportunity.  In particular, do not
    cache the Swiftstrike result when attackers are declared or blockers are
    assigned: a temporary keyword grant may arrive during that window.
    """
    if not (state.get("attackers") or {}):
        return _ge.ETurnPhases.SecondMainPhase
    if not pvp_combat_has_swiftstrike(session, state):
        return _ge.ETurnPhases.AssignDamage
    return _ge.ETurnPhases.AssignFirstStrikeDamage


def _pvp_declare_blockers(handler, session, inner_bytes, my_pid):
    """Record the defender's declared blockers and push BlockersAssigned to
    both players."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    state = pvp_load_state(session) or {}
    attackers = {int(k): int(v) for k, v in (state.get("attackers") or {}).items()}
    if not attackers:
        return False
    my_wz = set(r[0] for r in _db.execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location='warzone'", (session.session_id, my_pid)))
    all_uids = []
    if isinstance(inner_bytes, bytes):
        for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                                inner_bytes):
            try:
                import struct as _st
                uid64 = _st.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                if (uid64 & 0xFF) == 1:
                    all_uids.append(int(uid64))
            except Exception:
                continue
    blockers_map = {}
    cur = None
    for u in all_uids:
        if u in attackers:
            cur = u
            blockers_map.setdefault(cur, [])
        elif cur is not None and u in my_wz:
            from abilities.framework.statics import can_block
            if can_block(_db, session.session_id, _pvp_fra_view(state, opp_pid, my_pid),
                         cur, u):
                blockers_map[cur].append(u)
    state["blockers"] = {str(k): [str(b) for b in v]
                         for k, v in blockers_map.items()}
    # Mark each blocker Blocking in the DB so reconnect / HasBlocked logic and
    # the shared resolver's end-of-combat clear work (mirrors PvE
    # db_bulk_blocker_state).
    from db import db_bulk_blocker_state
    db_bulk_blocker_state(session.session_id,
                          [int(b) for bs in blockers_map.values() for b in bs])
    pvp_save_state(session, state)
    champ_map = state.get("champ_map") or {}
    opp_champ = int(champ_map.get(str(opp_pid), 0))
    opp_uid = _ge.UID.make(244, opp_pid)
    my_uid = _ge.UID.make(244, my_pid)
    for pid in pids:
        h = player_handlers.get(pid)
        if not h:
            continue
        pl_uid = _ge.UID.make(244, pid)
        g = _ge.Game(int(session.session_id), pl_uid,
                     _ge.UID.make(244, pids[1] if pid == pids[0] else pids[0]))
        player_champ = _ge.SessionCardId(_ge.UID(opp_champ)) if opp_champ \
            else _ge.SessionCardId(opp_uid)
        combats = []
        for u in attackers:
            cid = _ge.CombatId(opp_uid, u & 0xFFFF)
            blockers = [_ge.SessionCardId(_ge.UID(int(b)))
                        for b in blockers_map.get(u, [])]
            g.push_blockers_assigned(cid, _ge.SessionCardId(_ge.UID(u)),
                                     player_champ, blockers)
            cs = _ge.CombatSessionEventArgs()
            cs.player_id = opp_uid
            cs.id = cid
            cs.attacker = _ge.SessionCardId(_ge.UID(u))
            cs.blockers = blockers
            combats.append(cs)
        if combats:
            g.push_combat_listing(opp_uid, combats)
        _send_pvp_packet(h, session, g, pl_uid, "defense")
    log_req(f"    PvP defense: pid {my_pid} declared "
            f"{sum(len(v) for v in blockers_map.values())} blocker(s)")
    # The defender's decision is made — advance out of DeclareDefense (14)
    # into DeclareDefensePriorityWindow (15), a response window, and hand
    # priority to the TURN player so combat can proceed to AssignDamage.  The
    # normal pass cycle then carries both players through 15 -> AssignFirst
    # StrikeDamage (16) -> ... -> AssignDamage (18) -> SecondMain (19).
    # (Without this the game sat on DeclareDefense forever once the defender
    # declared — even declaring NO blockers.)
    _pvp_advance_past_declare_defense(session, state)
    return True


def _pvp_advance_past_declare_defense(session, state):
    """Advance the PvP phase from DeclareDefense (14) to
    DeclareDefensePriorityWindow (15), pushing the phase to both players and
    handing priority to the turn (attacker) player.  Returns True on success."""
    import battle_engine as _be
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    turn_pid = state.get("turn_pid")
    opp_pid = pids[0] if pids[1] == turn_pid else pids[1]
    try:
        new_phase = _ge.ETurnPhases.DeclareDefensePriorityWindow
    except Exception:
        new_phase = 15
    state["phase"] = new_phase
    state["passes"] = []
    state["priority_pid"] = turn_pid
    pvp_save_state(session, state)
    log_req(f"    PvP post-blockers: phase {new_phase} to both "
            f"(priority to turn player {turn_pid})")
    _pvp_run_phase_start(session, state, new_phase)
    return True


def _pvp_advance_from_damage_step(session, state, just_resolved):
    """After resolving a damage step (AssignFirstStrikeDamage=16 or
    AssignDamage=18), advance to the next phase and push it to both players.
    16 -> AssignDamage (18); 18 -> SecondMainPhase (19).  Resolves combat so
    the opponent doesn't get stuck in a dead BattleStateAssignDamage."""
    import battle_engine as _be
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    turn_pid = state.get("turn_pid")
    if just_resolved == _ge.ETurnPhases.AssignFirstStrikeDamage:
        new_phase = _ge.ETurnPhases.AssignDamage
    elif just_resolved == _ge.ETurnPhases.AssignDamage:
        new_phase = _ge.ETurnPhases.SecondMainPhase
    else:
        return
    state["phase"] = new_phase
    state["passes"] = []
    state["priority_pid"] = turn_pid
    pvp_save_state(session, state)
    log_req(f"    PvP post-damage: phase {new_phase} to both "
            f"(priority to turn player {turn_pid})")
    _pvp_run_phase_start(session, state, new_phase)
    # A champion may have died from the resolved damage.
    _pvp_check_game_end(session, state)


def _pvp_resolve_combat(session, state, first_strike=False):
    """Resolve the declared PvP combat through the SAME shared resolver the
    AI path uses (ai.resolve_combat), then push the identical event stream to
    both players.  ``first_strike=True`` is the Swiftstrike damage step (only
    FirstStrike/DualStrike combatants deal; casualties removed first)."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return
    attackers = {int(k): int(v) for k, v in (state.get("attackers") or {}).items()}
    if not attackers:
        state.pop("attackers", None)
        state.pop("blockers", None)
        return
    attacker_pid = state.get("turn_pid")
    defender_pid = pids[0] if pids[1] == attacker_pid else pids[1]
    blockers = {int(k): [int(b) for b in v]
                for k, v in (state.get("blockers") or {}).items()}
    view = _pvp_fra_view(state, attacker_pid, defender_pid)
    handler = player_handlers.get(attacker_pid)
    if not handler:
        return
    import ai
    pl_t = _ge.UID.make(244, attacker_pid)
    ai_t = _ge.UID.make(244, defender_pid)
    # The attacker's chosen blocker order (weakest-to-toughest) captured from
    # the AssignDamageOrderTransaction — pass as order_map so damage is
    # assigned in that order (mirrors PvE).
    order_map = {int(k): [int(b) for b in v]
                 for k, v in (state.get("damage_order") or {}).items()}
    try:
        view = ai.resolve_combat(
            handler, session, pl_t, ai_t, view, attackers, blockers,
            pl_t, ai_t, "pvp_attackers",
            send_events=lambda game, p, a, bstate:
                _pvp_send_same_events(session, game, p, a),
            first_strike=first_strike, order_map=order_map or None)
    except Exception as e:
        log_req(f"    PvP combat resolve error: {e}")
        import traceback
        traceback.print_exc()
        return
    # Copy the authoritative health back into the PvP state.  Keep the
    # attackers/blockers through the FIRST-STRIKE step — the normal step still
    # needs them; only the final (non-first-strike) resolution clears them.
    state[f"hp_{attacker_pid}"] = int(view.get("player_health", 20))
    state[f"hp_{defender_pid}"] = int(view.get("ai_health", 20))
    if not first_strike:
        state.pop("attackers", None)
        state.pop("blockers", None)
        state.pop("damage_order", None)
    pvp_save_state(session, state)
    log_req(f"    PvP combat resolved: {attacker_pid} hp "
            f"{state[f'hp_{attacker_pid}']} / {defender_pid} hp "
            f"{state[f'hp_{defender_pid}']}")
    # State-based deaths after combat (survivors at <=0 effective defense
    # from damage + statics die, e.g. a 0/1 that took 1).  Events ride the
    # same stream so both clients see the graveyard moves + Deathcries.
    try:
        from abilities.framework.kill_troop import state_based_deaths
        g2 = _ge.Game(int(session.session_id), pl_t, ai_t)
        g2.player_health = int(state.get(f"hp_{attacker_pid}", 20))
        g2.ai_health = int(state.get(f"hp_{defender_pid}", 20))
        state_based_deaths(g2, session, _db, handler, pl_t, ai_t, view)
        if g2.events:
            _pvp_send_same_events(session, g2, pl_t, ai_t)
    except Exception as e:
        log_req(f"    PvP post-combat state-based deaths error: {e}")
    if view.get("player_health") is not None:
        state[f"hp_{attacker_pid}"] = int(view["player_health"])
    if view.get("ai_health") is not None:
        state[f"hp_{defender_pid}"] = int(view["ai_health"])
    _pvp_sync_view_to_state(state, view, attacker_pid, defender_pid)
    pvp_save_state(session, state)
    # Combat damage can kill a champion — end the game properly instead of
    # letting the session limp on / fall into the human-vs-AI fallback.
    _pvp_check_game_end(session, state)


@_pvp_locked
def handle_ready_for_game_setup(handler, session, pvp_ready, player_handlers):
    """Post-process a 22027 response for tournament PvP sessions."""
    if not session or not (session.session_name or "").startswith("tourney-"):
        return None, False
    import io, struct, hashlib
    from binascii import hexlify as _hx
    rows = _db.execute("SELECT DISTINCT user_id FROM game_cards WHERE session_id=?", (session.session_id,)).fetchall()
    pids = [r[0] for r in rows]
    my_pid = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0
    player_uid_val = (my_pid << 8) | 244
    resp_inner = None

    if len(pids) >= 2:
        opp_pid = pids[0] if pids[1] == my_pid else pids[1]
        opp_uid_val = (opp_pid << 8) | 244

        # Coin flip — deterministic from session_id + both pids so both
        # 22027 calls (one per player) get the same result.
        sess_id = int(session.session_id) if isinstance(session.session_id, int) else 0
        h = hashlib.md5(f"{sess_id}:{pids[0]}:{pids[1]}".encode()).digest()
        goes_first_pid = pids[0] if h[0] & 1 else pids[1]
        goes_first_uid = (goes_first_pid << 8) | 244
        goes_second_pid = pids[1] if goes_first_pid == pids[0] else pids[0]
        goes_second_uid = (goes_second_pid << 8) | 244

        # Persist coin-flip winner so push_pvp_game_start reuses it.
        from services.tournament_game import pvp_load_state, pvp_save_state
        state = pvp_load_state(session) or pvp_default_state(goes_first_pid, goes_first_pid)
        state["goes_first_pid"] = goes_first_pid
        pvp_save_state(session, state)

        # TurnOrder: [first, second] — the player at index 0 goes first.
        turn_order_vals = [goes_first_uid, goes_second_uid]

        try:
            from binascii import hexlify as _hx
            player_uid_val = (my_pid << 8) | 244
            opp_hex = _hx(struct.pack("<Q", opp_uid_val)).decode("ascii")
            opp_name = b"Opponent"

            from encoder import encode_objfmt_response
            resp_inner = encode_objfmt_response(
                ["Game.Client.Network.LoadBalancer.ReadyForGameSetupResponse",
                 "Game.Shared.SessionState", "Game.Shared.UID",
                 "Game.Shared.ResourceId", "System.Guid",
                 "System.Collections.Generic.List`1#Game.Shared.PlayerState",
                 "Game.Shared.PlayerState",
                 "System.Collections.Generic.List`1#Game.Shared.UID",
                 "System.UInt64", "System.Int32",
                 "Game.Shared.Network.LoadBalancer.EReadyForGameSetupError",
                 "System.String"],
                [("SessionState","struct",("Game.Shared.SessionState",[
                   ("SessionId","uid",int(session.session_id)),
                   ("SessionName","string",session.session_name or "tourney-0"),
                   ("MinimumPlayerCount","int",2),("MaximumPlayerCount","int",2),
                   ("EncounterData","class","Game.Shared.SessionStateEncounterData"),
                   ("JoinInsteadOfReconnect","bool",False)])),
                 ("DeckId","uid",player_uid_val),
                 ("DeckTemplateId","struct",("Game.Shared.ResourceId",[
                   ("guid","guid","00000000-0000-0000-0000-000000000000")])),
                 ("OpponentsInfo","playerstate_coll",("System.Collections.Generic.List`1#Game.Shared.PlayerState",
                    [(opp_hex, 1, b"")])),
                 ("TurnOrder","uidlist",("System.Collections.Generic.List`1#Game.Shared.UID",0,
                    turn_order_vals)),
                 ("seedZ","ulong",22222),("seedW","ulong",11111),
                 ("Error","enum1",("Game.Shared.Network.LoadBalancer.EReadyForGameSetupError",0)),
                 ("ErrorMessage","string","")])
            log_req(f"    OpponentsInfo: my_pid={my_pid} opp_pid={opp_pid} opp_uid_val={opp_uid_val:#x} data={len(resp_inner)}b")
        except Exception as e:
            import traceback
            log_req(f"    OpponentsInfo encoding FAILED: {e}\n{traceback.format_exc()}")
            resp_inner = None

    # --- PvP ready handling ---
    game_started = False
    try:
        pvp_ready.setdefault(session.session_id, {"handlers": {}})
        pr = pvp_ready[session.session_id]
        puid = handler.client_reck_id if hasattr(handler, 'client_reck_id') else 0
        if puid not in pr["handlers"]:
            pr["handlers"][puid] = []
        pr["handlers"][puid].append(handler)
        log_req(f"    PvP ready: {len(pr['handlers'])}/2 for tourney session {session.session_id}")
    except Exception as e:
        import traceback
        log_req(f"    PvP ready section FAILED: {e}\n{traceback.format_exc()}")

    log_req(f"    handle_ready: opp_resp={resp_inner is not None}")
    return resp_inner, game_started


def handle_ready_for_game_events(handler, session, pvp_events_ready, log_req=log_req):
    """Called from 22029 handler. When both players are ready for events, pushes game start."""
    if not session or not (session.session_name or "").startswith("tourney-"):
        return False
    try:
        pvp_events_ready.setdefault(session.session_id, {"handlers": {}})
        pr = pvp_events_ready[session.session_id]
        puid = handler.client_reck_id if hasattr(handler, 'client_reck_id') else 0
        if puid not in pr["handlers"]:
            pr["handlers"][puid] = []
        pr["handlers"][puid].append(handler)
        log_req(f"    Events ready: {len(pr['handlers'])}/2 for tourney session {session.session_id}")
        if len(pr["handlers"]) >= 2:
            all_handlers = [h for hl in pr["handlers"].values() for h in hl]
            for h in all_handlers:
                try:
                    push_pvp_game_start(h, session)
                except Exception as e:
                    import traceback
                    log_req(f"    push_pvp_game_start FAILED: {e}\n{traceback.format_exc()}")
            del pvp_events_ready[session.session_id]
            return True
    except Exception as e:
        import traceback
        log_req(f"    handle_events_ready FAILED: {e}\n{traceback.format_exc()}")
    return False


def handle_join_disconnected_game(handler, target, instance, reqid, comp,
                                   session_id, conh, inner_obj, inner_bytes,
                                   log_req=log_req, **_kw):
    """Handle DT 22023 — JoinDisconnectedGame for tournament PvP.
    
    The client sends this after receiving 25060 (TournamentSessionStart)
    and transitioning to Battle. Returns a valid JoinDisconnectedGameResponse
    so the client adds the local player and sends ReadyToContinueGame.
    """
    import struct
    from binascii import unhexlify

    log_req(f">>> JoinDisconnectedGame (dt=22023)")

    # Extract PlayerId from inner bytes
    player_uid_val = 0
    if isinstance(inner_bytes, bytes):
        pos = inner_bytes.find(b"PlayerId")
        if pos >= 0:
            rest = inner_bytes[pos:]
            idx = rest.find(b"m_UID64")
            if idx >= 0:
                rest2 = rest[idx + 7:]
                parts = rest2.split(b";", 6)
                if len(parts) >= 5:
                    try:
                        hex_val = parts[4].decode("ascii")
                        player_uid_val = struct.unpack("<Q", unhexlify(hex_val))[0]
                    except (ValueError, struct.error):
                        pass

    player_uid = player_uid_val
    player_pid = _pvp_raw_player_id(player_uid_val)
    log_req(f"    PlayerId UID={player_uid:#x} raw_pid={player_pid}")

    # Find session for this player
    import game_session as gs
    session = gs.find_session_by_player(player_pid)
    if session and (getattr(session, "state", "") == "ended"
                    or not str(getattr(session, "session_name", "") or "").startswith("tourney-")):
        session = None
    if not session:
        log_req(f"    No session found for player {player_uid:#x}")
        resp_inner = encode_objfmt_response(
            ["Game.Client.Network.LoadBalancer.JoinDisconnectedGameResponse",
             "Game.Shared.UID"],
            [("RoutingPlayerId", "uid", 0)])
    else:
        # Replace the disconnected socket in the live PvP registry before
        # ReadyToContinue arrives; all subsequent priority/options pushes then
        # target the new connection.
        with player_handler_lock:
            player_handlers[player_pid] = handler
        resp_inner = encode_objfmt_response(
            ["Game.Client.Network.LoadBalancer.JoinDisconnectedGameResponse",
             "Game.Shared.UID"],
            [("RoutingPlayerId", "uid", player_uid)])

    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 22023, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceLoadBalancer.252.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent JoinDisconnectedGameResponse ({len(dw_bytes)}b)")


def handle_ready_to_continue_game(handler, target, instance, reqid, comp,
                                   session_id, conh, inner_obj, inner_bytes,
                                   log_req=log_req, **_kw):
    """Handle DT 22025 — ReadyToContinueGame after join."""
    import struct
    from binascii import unhexlify

    log_req(f">>> ReadyToContinueGame (dt=22025)")

    player_uid_val = 0
    if isinstance(inner_bytes, bytes):
        pos = inner_bytes.find(b"PlayerId")
        if pos >= 0:
            rest = inner_bytes[pos:]
            idx = rest.find(b"m_UID64")
            if idx >= 0:
                rest2 = rest[idx + 7:]
                parts = rest2.split(b";", 6)
                if len(parts) >= 5:
                    try:
                        hex_val = parts[4].decode("ascii")
                        player_uid_val = struct.unpack("<Q", unhexlify(hex_val))[0]
                    except (ValueError, struct.error):
                        pass

    player_pid = _pvp_raw_player_id(player_uid_val)
    import game_session as gs
    session = gs.find_session_by_player(player_pid)
    if session and getattr(session, "state", "") != "ended":
        try:
            pvp_start_priority_watchdog(session)
            _pvp_push_reconnect_snapshot(handler, session, player_pid)
        except Exception as exc:
            log_req(f"    PvP reconnect snapshot failed for {player_pid}: {exc}")
    else:
        log_req(f"    No active PvP session for reconnecting pid {player_pid}")
    # The client calls this overload with a null response callback.  A 22025
    # response is therefore rejected as an unsolicited command and leaves the
    # reconnect UI darkened.  The 3055 snapshot above is the only packet this
    # fire-and-forget request needs.
    log_req(f"    Processed ReadyToContinueGame for raw pid {player_pid}")
