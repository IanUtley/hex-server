"""Tournament PvP game setup — push initial battle events to both players."""

import random, json, threading, re, time

import game_engine as _ge
from rules_port.persistence import (load_pvp_state, save_pvp_state)
from rules_port.pvp_lifecycle import (phase_is_stop as port_phase_is_stop,
                                      player_auto_passes as port_player_auto_passes,
                                      phase_after_blockers as port_phase_after_blockers,
                                      phase_transition as port_phase_transition,
                                      enter_phase as port_enter_phase,
                                      record_phase_pass as port_record_phase_pass,
                                      set_priority as port_set_priority,
                                      reset_priority_interval as port_reset_priority_interval,
                                      waiting_player_requires_priority as port_waiting_player_requires_priority,
                                      stack_pass_transition as port_stack_pass_transition,
                                      advance_turn_state as port_advance_turn_state,
                                      queue_stack_item as port_queue_stack_item,
                                      default_pvp_state as port_default_pvp_state,
                                      mulligan_transition as port_mulligan_transition,
                                      turn_phase_list as port_turn_phase_list)
from gamedata import DEFAULT_RECORD_STORE, ability_graph, PlayPlan
from application.player_transactions import extract_ability_guid
from db import _db, log_req
from pvp_db import (db_game_session_pids, db_game_champion,
                    db_game_cards_at_location,
                    db_game_deck_cards, db_game_draw_cards, db_game_card_type,
                    db_game_shuffle_deck, db_champion_template_health,
                    db_discard_card, db_delete_game_session,
                    db_card_ability_list, db_ability_effect_type_params,
                    db_card_state_value,
                    db_card_set_attacking_state,
                    db_bulk_blocker_state, db_card_discard_spell,
                    db_get_card_abilities, db_is_champion_template,
                    db_card_uses, db_bump_card_use,
                    db_hand_cards_with_templates,
                    db_card_template_attrs_joined,
                    db_template_by_guid,
                    db_set_card_location, db_set_card_played_to_zone,
                    db_set_card_state_or, db_update_card_state,
                    db_card_location, db_card_basic,
                    db_warzone_troops_with_state,
                    db_warzone_attack_option_rows, db_warzone_blocker_uids,
                    db_card_attribute_rows, db_card_uids_in_zone,
                    db_ability_option_cards, db_card_ability_payload,
                    db_template_ability_payload, db_card_activation_info,
                    db_owned_warzone_card, db_card_zone_details,
                    db_card_position, db_hand_exists,
                    db_hand_card_for_discard, db_deck_top_card,
                    db_hand_count, db_card_play_info, db_cards_with_ability,
                    db_card_chain_info, db_warzone_display_rows,
                    db_template_name, db_target_template_info,
                    db_talent_ability_exists, db_card_zone_projection,
                    db_card_owner_zone_state, db_champion_ability_guids,
                    db_champion_ability_costs, db_champion_ability_thresholds)
from tournament_db import (db_tournament_by_id,
                           db_tournament_player_name_for_session)
from encoder import encode_datawrapper, encode_sync_event, compress_gzip, encode_objfmt_response, client_session_guid
from gamemodes.tournament_engine import (
    player_handlers, player_handler_lock, record_tournament_game_result,
    tournament_id_from_session_name,
)
from domain.constants import DEFAULT_MAX_HAND_SIZE


_ECardCollections = _ge.ECardCollections
_ECardTypes = _ge.ECardTypes
_PVP_INACTIVITY_TIMEOUT_SECONDS = 5 * 60
_RECORD_STORE = DEFAULT_RECORD_STORE


def _pvp_dispatch_triggers(handler, game, session, state, player_uid,
                           ai_uid, event_type, source_card_id,
                           source_owner_id=None, target_card_id=None,
                           **event_data):
    """Dispatch one PvP event through the native RulesPort trigger path.

    Tournament PvP still owns its two-player packet projection, but trigger
    discovery and ability ordering must be the same native implementation as
    Practice.  Keeping this adapter here also prevents a new PvP call site
    from quietly importing the historical trigger scanner.
    """
    from rules_port.triggers import dispatch_native_trigger
    return dispatch_native_trigger(
        db=_db, handler=handler, game=game, session=session,
        player_uid=player_uid, ai_uid=ai_uid, battle_state=state,
        event_type=event_type, source_card_id=source_card_id,
        source_player_id=source_owner_id, target_card_id=target_card_id,
        data=event_data)


def _pvp_resolve_ability(handler, game, session, state, player_uid, ai_uid,
                         ability_guid, source_uid, owner_id, *,
                         target_map=None, variables=None,
                         resume_from_order=None, instance_id=1):
    """Resolve a PvP continuation through the native RulesPort lifecycle."""
    from rules_port.resolution import resolve_port_ability
    return resolve_port_ability(
        handler, game, session, _db, player_uid, ai_uid, state,
        ability_guid, source_uid, owner_id, target_map=target_map,
        variables=variables, resume_from_order=resume_from_order,
        instance_id=instance_id)


def _pvp_resource_charge_points(session, card_uid):
    """Return charge points granted by a resource's current ability BOM.

    Resource charge generation is card data, not a second universal rule.
    Set 1 shards each have a BOM ``chargepoints = 1`` effect, so adding a
    hard-coded base charge as well would double-count them.
    """
    abilities = db_card_ability_list(session.session_id, card_uid)
    total = 0
    for ability_guid in abilities if isinstance(abilities, list) else []:
        if not isinstance(ability_guid, str):
            continue
        effects = db_ability_effect_type_params(ability_guid.lower())
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
    source_uid = int((state.get("champ_map") or {}).get(str(owner_id), 0)
                     or 0)
    if not source_uid:
        champion = (getattr(owner_handler, "_player_champ_scid", None) or
                    getattr(owner_handler, "_ai_champ_scid", None))
        source_uid = (int(champion.uid.uid64) if champion is not None else 0)
    _pvp_dispatch_triggers(
        owner_handler, game, session, state, pl_uid, opp_uid,
        "GainChargeEvent", source_uid,
        owner_id)
    return game


def _pvp_gain_threshold_trigger_game(handler, session, state, owner_id,
                                     color):
    """Build the shared event stream for a PvP threshold gain."""
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
    source_uid = int((state.get("champ_map") or {}).get(str(owner_id), 0)
                     or 0)
    if not source_uid:
        champion = (getattr(owner_handler, "_player_champ_scid", None) or
                    getattr(owner_handler, "_ai_champ_scid", None))
        source_uid = (int(champion.uid.uid64) if champion is not None else 0)
    _pvp_dispatch_triggers(
        owner_handler, game, session, state, pl_uid, opp_uid,
        "GainThresholdEvent", source_uid,
        owner_id, gain_threshold_color=int(color))
    return game


# ── PvP state persistence (session.turn_order / turn_order_json) ────────────
# Mirrors the battle_engine.load_state / save_state pattern but with a PvP-
# specific schema (two human players, no AI).  State lives in the DB so a
# reconnect can resume.

def pvp_default_state(turn_pid, goes_first_pid):
    return port_default_pvp_state(turn_pid, goes_first_pid)


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


# The C# ``AuthoritativeSession`` is a single per-game object shared by both
# participants.  HConnect materializes a fresh ``GameSession`` wrapper per
# request, so storing the native port on that wrapper (``_rules_port_session``)
# gave each connection its own scheduler: the two threads restored separate
# snapshots and clobbered each other's phase/priority (the first shard/card
# play after mulligan was rejected).  Keep one shared port per game session so
# both connections drive the same scheduler, exactly like the client.
_pvp_ports = {}
_pvp_ports_guard = threading.Lock()


def pvp_shared_port(session):
    sid = int(session.session_id)
    with _pvp_ports_guard:
        return _pvp_ports.get(sid)


def set_pvp_shared_port(session, port):
    sid = int(session.session_id)
    with _pvp_ports_guard:
        if port is None:
            _pvp_ports.pop(sid, None)
        else:
            _pvp_ports[sid] = port


def pvp_discard_shared_port(session):
    set_pvp_shared_port(session, None)


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
    mulligan = port_mulligan_transition(state, pids, just_acted_pid)
    if mulligan["action"] == "start_turn":
        # Both players kept.  Keep the native session at Mulligan until it
        # performs the legal Mulligan -> StartGame -> StartTurn transition;
        # the RulesPort phase scheduler owns the remainder of the first turn.
        # A service-side phase loop here can get out of sync with the native
        # priority action (the original source of second-main/discard tennis).
        state["phase"] = int(_ge.ETurnPhases.Mulligan)
        state.pop("priority_pid", None)
        state["passes"] = []
        state.pop("mulligan_pid", None)
        pvp_save_state(session, state)
        native_handler = player_handlers.get(int(pids[0]))
        if native_handler is None:
            log_req("    PvP mulligan complete but no player handler is "
                    "available to attach RulesPort")
            return False
        turn_uid = _ge.UID.make(244, int(state["turn_pid"]))
        opponent_pid = next(int(pid) for pid in pids
                            if int(pid) != int(state["turn_pid"]))
        native_game = _ge.Game(
            int(session.session_id), turn_uid,
            _ge.UID.make(244, opponent_pid))
        port = attach_pvp_rules_port(
            native_handler, session, native_game, state)
        if port is None:
            log_req("    PvP mulligan complete but RulesPort attachment failed")
            return False
        port.begin_pvp_turn()
        live = pvp_load_state(session) or state
        log_req("    PvP mulligan complete — native scheduler reached "
                f"phase {live.get('phase')} for turn player "
                f"{live.get('turn_pid')}")
        # Start the server-side priority watchdog for clock flushing and
        # inactivity expiry. It does not send periodic client events.
        pvp_start_priority_watchdog(session)
        # No dialog is open now; clear any "opponent is mulliganing" state.
        _pvp_push_waiting_on(session, None)
        # The sequential mulligan prompt disabled every client that was not the
        # active mulliganer (_pvp_push_mulligan_prompt ->
        # push_disable_interface).  Nothing else re-enables them, so the player
        # who kept first is left with m_DisabledInput=true and every
        # button-driven action (charge power, Pass) is silently dropped by
        # UIBattle.HandleInputs until the inactivity timeout.  The first turn
        # is live now, so re-enable both clients.
        for pid in pids:
            h = player_handlers.get(int(pid))
            if not h:
                continue
            pt = _ge.UID.make(244, int(pid))
            opp = _ge.UID.make(
                244, int(pids[0]) if int(pid) == int(pids[1]) else int(pids[1]))
            enable = _ge.Game(int(session.session_id), pt, opp)
            enable.push_disable_interface(False)
            _send_pvp_packet(h, session, enable, pt,
                             "mulligan-end-enable-input")
        return False
    # Only one (or neither) has kept.  Ask the other player if they haven't
    # kept yet; otherwise (the other player already kept) re-ask the player
    # who just acted (they must keep or redraw again, one fewer card).
    next_pid = mulligan["next_player"]
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
    # Tell the other client the opponent is mulliganing.
    _pvp_push_waiting_on(session, ask_pid)


def pvp_load_state(session):
    # A live game has ONE authoritative checkpoint shared by both connections.
    # HConnect builds a fresh ``GameSession`` wrapper per request, so reading
    # the wrapper's own ``_rules_port_battle_state``/``turn_order`` makes the
    # two players diverge (a shard's charge/threshold landed in one wrapper's
    # dict while the options refresh read another).  Resolve through the shared
    # port when it exists so every wrapper sees the same dict.
    port = getattr(session, "_rules_port_session", None)
    if port is not None:
        state = getattr(port, "_pvp_state", None)
        if isinstance(state, dict) and state.get("pvp"):
            return state
    shared = getattr(session, "_rules_port_battle_state", None)
    if isinstance(shared, dict) and shared.get("pvp"):
        return shared
    return load_pvp_state(session)


def pvp_save_state(session, state):
    if isinstance(state, dict) and state.get("pvp"):
        # Publish to the shared port so both connections and every per-request
        # wrapper operate on this one dict.
        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            port._pvp_state = state
        # Keep the PvP projection and the RulesPort snapshot on one mutable
        # root.  Without this assignment, SQLiteRulesSnapshot.save() can
        # persist a stale pre-rotation root and overwrite turn_order after a
        # successful native EndTurn transition.
        shared = getattr(session, "_rules_port_battle_state", None)
        if isinstance(shared, dict) and shared is not state:
            if "rules_port" not in state and "rules_port" in shared:
                state["rules_port"] = shared["rules_port"]
            session._rules_port_battle_state = state
    # Keep the champion-card identity as session metadata when a RulesPort
    # transition supplies a reduced PvP state dictionary.  Losing champ_map
    # makes the next reconnect serialize Undefined.0 in PlayerUpdated.
    if isinstance(state, dict) and not state.get("champ_map"):
        existing = getattr(session, "turn_order", None)
        if isinstance(existing, dict) and existing.get("champ_map"):
            state["champ_map"] = dict(existing["champ_map"])
    save_pvp_state(session, state, flush_clock=_pvp_flush_priority_clock)


def project_accepted_pvp_transaction(handler, session, kind, transaction,
                                     *, port=None):
    """Apply the PvP wire/database projection for an accepted intent.

    RulesPort is the sole owner of transaction classification, legality,
    costs, and ordering.  This function is deliberately the only compatibility
    boundary used by the live PvP adapter; it receives a typed, already
    accepted intent and exists only to emit the historical PvP event stream.

    The implementation is kept separate from attachment so the remaining
    legacy projection can be replaced one transaction family at a time
    without reintroducing a second ingress or validation path.
    """
    payload = getattr(transaction, "payload", {}) or {}
    raw = getattr(transaction, "raw_transaction", None)
    if raw is None:
        raw = getattr(getattr(session, "_rules_port_dispatch_command", None),
                      "inner_bytes", b"")
    current = getattr(session, "_rules_port_dispatch_handler", None) or handler
    try:
        my_pid = int(current.client_reck_id)
    except (AttributeError, TypeError, ValueError):
        return False

    # Continuations are already represented by explicit typed RulesPort
    # transaction kinds. Route them directly to their mode projection instead
    # of sending them back through the old all-purpose transaction parser.
    # This is especially important after reconnect, where the raw envelope
    # may not contain the original ability class name.
    if kind == "conversation":
        return bool(_pvp_resolve_conversation(current, session, raw, my_pid))
    if kind == "choice":
        if (pvp_load_state(session) or {}).get("pending_choice"):
            # The native RulesPort continuation carries the selected target in
            # the typed payload (``activation_data.target_map``).  The legacy
            # ``raw`` envelope is empty on this path, so pass the payload
            # through; ``_pvp_resolve_choice`` reads the typed target when the
            # raw scan finds nothing.
            return bool(_pvp_resolve_choice(
                current, session, raw, my_pid, typed_payload=payload))
        return True
    if kind == "discard":
        live = pvp_load_state(session) or {}
        if live.get("pending_discard_ability"):
            return bool(_pvp_resolve_discard_prompt(
                current, session, raw, my_pid))
        try:
            card_uid = int(getattr(payload.get("card_id"), "uid64",
                                   payload.get("card_id")))
        except (TypeError, ValueError):
            return False
        pids = tuple(int(pid) for pid in db_game_session_pids(
            session.session_id))
        if my_pid not in pids:
            return False
        opponent = next((pid for pid in pids if pid != my_pid), my_pid)
        from rules_port.host_mutations import discard_card_to_owner
        projected, _owner = discard_card_to_owner(
            current, session, _ge.UID.make(244, my_pid),
            _ge.UID.make(244, opponent), card_uid)
        if projected is None:
            return False
        _pvp_send_same_events(
            session, projected, _ge.UID.make(244, my_pid),
            _ge.UID.make(244, opponent))
        return True
    if kind == "discard_continuation":
        return bool(_pvp_resolve_discard_prompt(
            current, session, raw, my_pid))
    if kind == "triggered":
        pending = pvp_load_state(session) or {}
        if pending.get("pending_trigger"):
            return bool(_pvp_resolve_trigger_target(
                current, session, raw, my_pid))
        search = pending.get("pending_deck_search") or {}
        search_kind = str(search.get("kind") or "")
        if search_kind == "revealed_troop":
            return bool(_pvp_resolve_revealed_choice(
                current, session, raw, my_pid))
        if search_kind == "shard":
            return bool(_pvp_resolve_shard_choice(
                current, session, raw, my_pid))
        if search_kind == "matching_target":
            return bool(_pvp_resolve_matching_target(
                current, session, raw, my_pid))
        if search:
            return bool(_pvp_resolve_deck_search(
                current, session, raw, my_pid))
        return True
    if kind == "attack":
        return bool(_pvp_declare_attackers(current, session, raw, my_pid))
    if kind == "defense":
        return bool(_pvp_declare_blockers(current, session, raw, my_pid))
    if kind == "ready":
        return True
    if kind == "damage":
        # AssignDamageOrder is an automatic client transaction. Its only
        # mutable payload is blocker order; combat resolution remains the
        # native phase/action-stack boundary.
        live = pvp_load_state(session) or {}
        try:
            import struct
            selected = []
            for match in re.finditer(
                    rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', raw):
                value = struct.unpack(
                    '<Q', bytes.fromhex(match.group(1).decode()))[0]
                if (value & 0xFF) == 1:
                    selected.append(int(value))
            blockers = {int(key): {int(value) for value in values}
                        for key, values in
                        (live.get("blockers") or {}).items()}
            order_map = {}
            for attacker, blocker_set in blockers.items():
                ordered = [uid for uid in selected if uid in blocker_set]
                ordered.extend(uid for uid in blocker_set if uid not in ordered)
                if ordered:
                    order_map[attacker] = ordered
            if order_map:
                live["damage_order"] = {
                    str(key): [str(value) for value in values]
                    for key, values in order_map.items()}
            phase = int(live.get("phase", 0) or 0)
            if phase in (_ge.ETurnPhases.AssignFirstStrikeDamage,
                         _ge.ETurnPhases.AssignDamage):
                _pvp_resolve_combat(
                    session, live,
                    first_strike=(phase == _ge.ETurnPhases.AssignFirstStrikeDamage))
                _pvp_advance_from_damage_step(session, live, phase)
            pvp_save_state(session, live)
            return True
        except (TypeError, ValueError, struct.error) as exc:
            log_req(f"    PvP AssignDamageOrder projection error: {exc}")
            return False
    if kind == "play_resource":
        try:
            card_uid = int(getattr(payload.get("card_id"), "uid64",
                                   payload.get("card_id")))
        except (TypeError, ValueError):
            return False
        pids = db_game_session_pids(session.session_id)
        if len(pids) < 2 or my_pid not in [int(pid) for pid in pids]:
            return False
        crow = db_card_play_info(session.session_id, card_uid, conn=_db)
        if not crow or str(crow[1] or "") != "Resource":
            return False
        opponent = next(int(pid) for pid in pids if int(pid) != my_pid)
        return bool(_pvp_project_resource_play(
            current, session, raw, my_pid, card_uid, crow, crow[2],
            tuple(int(pid) for pid in pids), _ge.UID.make(244, my_pid),
            _ge.UID.make(244, opponent)))
    card_uid = payload.get("card_id")
    if kind in {"play_troop", "play_artifact", "play_spell",
                "play_champion"} and card_uid is not None:
        try:
            card_uid = int(getattr(card_uid, "uid64", card_uid))
            row = db_card_play_info(session.session_id, card_uid, conn=_db)
            if row:
                card_type = str(row[1] or "")
                if kind == "play_spell" or any(
                        name in card_type for name in ("BasicAction", "QuickAction")):
                    return bool(_pvp_play_spell(
                        current, session, card_uid, int(current.client_reck_id),
                        raw, typed_payload=payload, native_port=port))
                return bool(_pvp_play_troop(
                    current, session, card_uid, int(current.client_reck_id), raw,
                    typed_payload=payload, native_port=port))
        except (TypeError, ValueError):
            return False
    # Every transaction kind wired by attach_pvp_rules_port must be handled
    # above. Unknown kinds are rejected here rather than being reinterpreted
    # by the legacy all-purpose dispatcher.
    log_req(f"    PvP RulesPort projection: unsupported kind={kind}")
    return False


def attach_pvp_rules_port(handler, session, game, state):
    """Attach the generic RulesPort scheduler to a two-human PvP session.

    The tournament service remains a projection adapter for the established
    PvP wire/database shape.  Transaction classification, requirements, and
    scheduler ordering are supplied by ``PvpAuthoritativeSession``; the
    adapter callbacks below only apply accepted mutations and publish events.
    """
    if not session:
        return None
    # The caller may have obtained ``state`` from the generic RulesPort
    # snapshot while rebuilding a fresh Game.  That snapshot is scheduler
    # metadata and can lag the tournament turn_order checkpoint (notably
    # during the Mulligan -> FirstMain transition).  PvP's turn_order is the
    # authoritative phase/priority projection; never let a stale generic
    # snapshot rehydrate the native port or reject the first shard/card play.
    authoritative = pvp_load_state(session)
    if isinstance(authoritative, dict) and authoritative.get("pvp"):
        state = authoritative
    if not isinstance(state, dict) or not state.get("pvp"):
        return None
    cached = (pvp_shared_port(session) or
              getattr(session, "_rules_port_session", None))
    if cached is not None:
        # Point this request wrapper at the shared port FIRST so
        # ``pvp_load_state`` resolves the one authoritative checkpoint dict.
        session._rules_port_session = cached
        set_pvp_shared_port(session, cached)
        # A reconnect can materialize a fresh turn_order dictionary while the
        # native PvP session object remains cached. Refresh both its phase /
        # priority view and runtime facts before accepting another request;
        # otherwise validation can use the old participant checkpoint.
        live_state = pvp_load_state(session) or state
        live_state["_rules_port_attached"] = True
        session._rules_port_battle_state = live_state
        cached._pvp_state = live_state
        # Record the request-scoped dispatch identity: the port's projections
        # must attribute cost/resource/threshold changes to THIS handler, not
        # the handler that happened to create the shared port.
        cached._pvp_current_handler = handler
        cached._pvp_current_session = session
        # The shared port is the single live scheduler for this game, so its
        # in-memory phase/priority are authoritative.  A request-scoped
        # wrapper can have loaded ``turn_order`` before the other connection
        # advanced the phase (e.g. the mulligan completion's first-turn
        # drive); syncing the port FROM that stale checkpoint rolled it back
        # to Ready/Prep and rejected the first shard/card play.  Project the
        # port OUT to the checkpoint instead.
        sync_out = getattr(cached, "sync_to_pvp_state", None)
        if callable(sync_out):
            sync_out(live_state)
        sink = getattr(cached, "event_sink", None)
        if sink is not None:
            sink.game = game
        facts = getattr(cached, "runtime_facts", None)
        if facts is not None:
            facts.battle_state = live_state
            facts.client_player_uid = game.player_uid
            try:
                facts.player_owner_id = int(handler.client_reck_id)
                facts.ai_owner_id = int(next(
                    pid for pid in db_game_session_pids(session.session_id)
                    if int(pid) != int(handler.client_reck_id)))
            except (AttributeError, StopIteration, TypeError, ValueError):
                pass
        # Keep the snapshot store pointed at the current wrapper, or persist()
        # would write through a stale request-scoped session object.
        snapshot = getattr(cached, "snapshot_store", None)
        if snapshot is not None:
            snapshot.game_session = session
        return cached
    from rules_port import (GameEngineEventSink, PvpAuthoritativeSession,
                            SQLiteRulesSnapshot, attach_pvp_runtime_facts)
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return None
    # ``session.players`` is legacy transport metadata and may contain the
    # stale packed IDs originally sent by the client.  The game-card/session
    # participant rows are authoritative for live PvP RulesPort ownership.
    player_ids = tuple(_ge.UID.make(244, pid) for pid in pids[:2])
    state["_rules_port_attached"] = True
    session._rules_port_battle_state = state
    port = PvpAuthoritativeSession(
        session.session_id, player_ids,
        seed_z=int(getattr(session, "seed_z", 22222)),
        seed_w=int(getattr(session, "seed_w", 11111)),
        event_sink=GameEngineEventSink(game),
        snapshot=SQLiteRulesSnapshot(session))
    # Rehydrate port-owned scheduler/combat descriptors before wiring the
    # mode projections; the PvP checkpoint remains authoritative for the
    # current phase and priority values.
    port.restore_snapshot(port.snapshot_store.load())
    port.sync_from_pvp_state(state)
    # Resource plays leave the player in the same main phase.  The legacy PvP
    # projection sends GreenLight directly, but without a native priority
    # action the following PassPriority is rejected and the client can apply
    # a stale/default PlayerUpdated while trying to advance the phase.
    # Reconcile an empty main-phase stack from the durable checkpoint; existing
    # chain/priority actions restored above remain untouched.
    priority_uid = port._uid_for_raw_player(state.get("priority_pid"))
    active_uid = port._uid_for_raw_player(state.get("turn_pid"))
    if priority_uid is not None and active_uid is not None:
        port.sync_checkpoint(
            phases=[port.current_turn_phase], phase_idx=0,
            active_player_id=active_uid, client_player_id=priority_uid,
            # Rebuild a native window for every interactive phase after
            # reconnect. ``restore_snapshot`` intentionally restores action
            # descriptors without fabricating action objects; limiting this
            # to First/Second Main left DeclareAttackPriorityWindow (phase
            # 11) with a durable priority owner but no live action.
            ensure_main_priority=True,
            ensure_current_priority=True)
    facts = attach_pvp_runtime_facts(
        port, session, state, player_uid=game.player_uid,
        ai_uid=game.ai_uid)
    facts.client_player_uid = game.player_uid
    # PvP card owners are raw participant ids, unlike Practice's profile/AI
    # pair. The runtime adapter derives these from the typed request.
    facts.player_owner_id = int(handler.client_reck_id)
    facts.ai_owner_id = int(next(pid for pid in pids
                                 if int(pid) != int(handler.client_reck_id)))

    def current_handler():
        # The port is shared by both connections, so the request-scoped
        # handler captured at creation is stale for the other player.  Use the
        # handler recorded for the transaction currently being projected.
        return (getattr(port, "_pvp_current_handler", None)
                or getattr(session, "_rules_port_dispatch_handler", None)
                or handler)

    def raw_transaction():
        command = getattr(session, "_rules_port_dispatch_command", None)
        return getattr(command, "inner_bytes", b"")

    def pvp_projection(_kind, _transaction):
        # All RulesPort-accepted PvP intents cross one named projection seam.
        # The frozen RulesTransaction is intentionally not mutated. The raw
        # command remains available only through the session dispatch context
        # for the historical event projection.
        return project_accepted_pvp_transaction(
            current_handler(), session, _kind, _transaction, port=port)

    def native_activation_resolver(ability):
        """Resolve simple manual abilities through the native port lifecycle.

        The PvP checkpoint uses raw participant ids, while the RulesPort
        transaction uses typed ServicePlayer ids.  Keep that translation at
        this mode boundary and let the shared native effect dispatcher own the
        actual BOM traversal.
        """
        from rules_port.resolution import resolve_port_ability
        owner_id = int(getattr(ability, "metadata", ability).owner_id)
        other_id = next((int(pid) for pid in pids if int(pid) != owner_id),
                        owner_id)
        live = pvp_load_state(session) or {}
        view = _pvp_fra_view(live, owner_id, other_id)
        player_uid = _ge.UID.make(244, owner_id)
        opponent_uid = _ge.UID.make(244, other_id)
        game = _ge.Game(int(session.session_id), player_uid, opponent_uid)
        _pvp_populate_game_state(game, live, owner_id, other_id)
        cost_selections = live.pop("_rules_port_pvp_cost_selections", ())
        if cost_selections:
            _pvp_apply_card_play_costs(
                current_handler(), game, session, live, player_uid,
                opponent_uid, cost_selections, int(ability.source_uid))
        exhausted = live.pop("_rules_port_pvp_exhausted", ())
        if exhausted:
            source_uid = int(ability.source_uid)
            row = db_card_basic(session.session_id, source_uid, conn=_db)
            if row:
                scid = _ge.SessionCardId(_ge.UID(source_uid))
                _tpl, ctype, _name, cost, attack, defense, gems = \
                    current_handler()._card_full_data(game, scid, row[0])
                game.push_card_updated(
                    scid, player_uid, _ge.ECardCollections.Warzone, ctype,
                    template_id=_tpl, state=int(db_card_state_value(
                        session.session_id, source_uid, conn=_db) or
                        _ge.ECardStates.Tapped), cost=cost, attack=attack,
                    defense=defense, gems=gems)
        # Activation prompts and native effect events must share the same
        # recipient-aware PvP projection used by the rest of the packet path.
        port.event_sink.game = game
        result = resolve_port_ability(
            current_handler(), game, session, _db, player_uid, opponent_uid,
            view, ability.ability_template_id, int(ability.source_uid),
            owner_id,
            target_map=dict(getattr(ability.activation, "target_map", {}) or {}),
            instance_id=int(ability.instance_id))
        live["stack"] = view.get("stack") or []
        live["stack_passed"] = []
        _pvp_sync_view_to_state(live, view, owner_id, other_id)
        pvp_save_state(session, live)
        _pvp_send_same_events(session, game, player_uid, opponent_uid)
        session._rules_port_mutation_emitted = True
        return result

    def native_chain_resolver(ability):
        from rules_port.pvp_session import ProjectedChainAbility
        if not isinstance(ability, ProjectedChainAbility):
            return native_activation_resolver(ability)
        state = pvp_load_state(session) or {}
        descriptor = dict(ability.descriptor)
        # The PvP host projection consumes the same persisted descriptor; only
        # its chain ownership has moved to the native action stack.
        stack = state.setdefault("stack", [])
        if not stack or int(stack[-1].get("instance_id", -1)) != int(
                descriptor.get("instance_id", -2)):
            stack.append(descriptor)
        owner_id = int(ability.owner_id >> 8) if (
            isinstance(ability.owner_id, int) and
            (ability.owner_id & 0xFF) == 244) else int(
                getattr(ability.owner_id, "uid64", ability.owner_id))
        if descriptor.get("kind") == "spell":
            _pvp_resolve_native_spell(
                session, state, current_handler(), descriptor)
        elif descriptor.get("kind") == "troop":
            _pvp_resolve_native_permanent(
                session, state, current_handler(), descriptor)
        else:
            _pvp_resolve_chain(
                session, state, current_handler(), owner_id, item=descriptor)
        pvp_save_state(session, state)
        from rules_port.actions import AbilityResolutionState
        if (not bool(getattr(ability, "ignores_chain", False)) and
                any(state.get(key) for key in (
                    "pending_choice", "pending_trigger",
                    "pending_deck_search", "pending_conversation",
                    "pending_discard_ability", "resolution_paused"))):
            # A real chain ability paused on an interactive prompt.  Keep the
            # chain item (do not forget the projected chain) so the client's
            # picker is not torn down; resolution resumes when the prompt is
            # answered.
            session._rules_port_mutation_emitted = True
            return AbilityResolutionState.WAITING_FOR_INPUT
        port.forget_projected_chain(int(ability.instance_id))
        # Triggers or nested effects may have placed another descriptor on
        # the persisted PvP stack. Reify its response window in RulesPort as
        # well, so the next pass cannot fall back to the legacy stack owner.
        remaining = state.get("stack") or []
        if remaining and not any(state.get(key) for key in (
                "pending_choice", "pending_deck_search", "pending_trigger",
                "pending_discard_ability")):
            next_item = remaining[-1]
            next_source = int(next_item.get("source_uid") or 0)
            next_owner = owner_id
            if next_source:
                next_row = db_card_basic(
                    session.session_id, next_source, conn=_db)
                if next_row:
                    next_owner = int(next_row[1])
            next_other = next((int(pid) for pid in pids
                               if int(pid) != next_owner), next_owner)
            port.queue_projected_chain(
                next_item, next_owner,
                first_player_id=_ge.UID.make(244, next_other))
        session._rules_port_mutation_emitted = True
        return AbilityResolutionState.COMPLETED

    def native_activation_cost_payer(ability):
        """Apply the numeric portion of a simple native ability cost."""
        from rules_port.costs import ability_cost_targets, plan_ability_cost
        from rules_port.resources import pay_resource_for_player
        metadata = getattr(ability, "metadata", ability)
        ability_guid = (getattr(metadata, "ability_template_id", None) or
                        getattr(metadata, "ability_guid", None) or
                        getattr(metadata, "runtime_ability_guid", ""))
        owner_id = int(getattr(metadata, "owner_id", 0) or 0)
        state = pvp_load_state(session) or {}
        graph = getattr(metadata, "graph", None)
        cost_selections = []
        if graph is not None:
            costs_by_index = {
                int(cost.index): cost for cost in ability_cost_targets(
                    graph, _db, session.session_id, owner_id,
                    int(getattr(metadata, "source_uid", 0) or 0),
                    battle_state=state)}
            activation_data = getattr(metadata, "activation", None)
            cost_map = getattr(activation_data, "cost_target_map", {}) or {}
            for index, cost in costs_by_index.items():
                selected = cost_map.get(index, cost_map.get(str(index), ()))
                if cost.is_source_auto_target and not selected:
                    selected = (int(metadata.source_uid),)
                selected = tuple(int(value) for value in (selected or ()))
                if (len(selected) < int(cost.minimum) or
                        (int(cost.maximum) > 0 and
                         len(selected) > int(cost.maximum)) or
                        any(value not in set(cost.candidates)
                            for value in selected)):
                    return False
                if selected:
                    from rules_port.costs import cost_type_for_kind
                    cost_selections.append((
                        {"kind": cost.kind, "minimum": cost.minimum,
                         "maximum": cost.maximum,
                         "cost_type": cost_type_for_kind(cost.kind),
                         "auto": cost.is_source_auto_target},
                        selected))
        costs = getattr(metadata, "costs", None)
        activation = getattr(metadata, "activation", None)
        current = int(state.get(f"res_{owner_id}", 0) or 0)
        plan = plan_ability_cost(
            costs, activation, current_resource=current,
            charges=int(state.get(f"chg_{owner_id}", 0) or 0),
            spell_points=int(state.get(f"sp_{owner_id}", 0) or 0),
            health=int(state.get(f"hp_{owner_id}", 20) or 0),
            spell_uses=state.get(f"sp_uses_{owner_id}", {}) or {},
            ability_key=str(ability_guid))
        if plan is None:
            return False
        if plan.resource:
            pay_resource_for_player(state, owner_id, int(plan.resource))
        state[f"chg_{owner_id}"] = max(
            0, int(state.get(f"chg_{owner_id}", 0) or 0) - int(plan.charge_points))
        state[f"sp_{owner_id}"] = max(
            0, int(state.get(f"sp_{owner_id}", 0) or 0) - int(plan.spell_points))
        if plan.life:
            state[f"hp_{owner_id}"] = max(
                0, int(state.get(f"hp_{owner_id}", 20) or 0) - int(plan.life))
        # The PvE payer emits the pool-change events the client HUD listens
        # for.  The native PvP payer only mutated the checkpoint, so a paid
        # charge/spell point never animated (and the charge power button stayed
        # lit).  Record the deltas and project them with the resolution events.
        if (plan.resource or plan.charge_points or plan.spell_points or
                plan.life):
            state.setdefault("_pvp_paid_costs", []).append({
                "owner_id": int(owner_id),
                "resource": int(plan.resource or 0),
                "charge": int(plan.charge_points or 0),
                "spell": int(plan.spell_points or 0),
                "life": int(plan.life or 0),
                "res_new": int(state.get(f"res_{owner_id}", 0) or 0),
                "chg_new": int(state.get(f"chg_{owner_id}", 0) or 0),
                "sp_new": int(state.get(f"sp_{owner_id}", 0) or 0),
                "hp_new": int(state.get(f"hp_{owner_id}", 20) or 0),
            })
        if bool(getattr(costs, "exhausts_card_on_use", False)):
            source_uid = int(getattr(metadata, "source_uid", 0) or 0)
            db_set_card_state_or(session.session_id, source_uid,
                                 _ge.ECardStates.Tapped)
            state.setdefault("_rules_port_pvp_exhausted", []).append(source_uid)
        if cost_selections:
            state["_rules_port_pvp_cost_selections"] = [
                (spec, list(selected)) for spec, selected in cost_selections]
        db_bump_card_use(session.session_id, int(metadata.source_uid),
                         str(ability_guid))
        pvp_save_state(session, state)
        return True

    def native_card_projection(kind, transaction):
        if kind != "activate_ability":
            return pvp_projection(kind, transaction)
        # Activation is fully owned by the native MetadataCardTransaction
        # executor; this callback exists only for the non-activation kinds.
        return False

    from rules_port.card_transactions import MetadataCardTransactionExecutor
    from gamedata import ability_graph as _ability_graph
    native_executor = MetadataCardTransactionExecutor(
        port, graph_loader=lambda guid: _ability_graph(_RECORD_STORE, str(guid).lower()),
        owner_id=0,
        owner_id_resolver=lambda tx: (
            (int(getattr(tx.player_id, "uid64", tx.player_id)) >> 8)
            if (int(getattr(tx.player_id, "uid64", tx.player_id)) & 0xFF) == 244
            else int(getattr(tx.player_id, "uid64", tx.player_id))),
        projection=native_card_projection,
        play_plan_loader=lambda template_guid, source_uid, owner_id:
            PlayPlan.from_card(_RECORD_STORE, template_guid,
                               source_uid=source_uid, owner_id=owner_id))
    port.set_ability_resolver(native_chain_resolver)
    port.set_ability_cost_payer(native_activation_cost_payer)

    def pvp_pass(_transaction):
        # Native manual/triggered abilities use the RulesPort priority action
        # rather than the legacy PvP stack. Rehydrate the native response
        # action if a reconnect restored only the durable descriptor. Once a
        # PvP RulesPort host is attached, do not route a missing action through
        # route_pvp_pass: that would create a second priority/chain authority.
        from rules_port.kernel import PriorityWindowAction
        live = pvp_load_state(session) or {}
        # While an interactive prompt is open (e.g. Corinth's charge-power
        # picker) the only valid client input is the answer.  A stray pass
        # must not advance the phase out from under the picker.
        if any(live.get(key) for key in (
                "pending_choice", "pending_deck_search", "pending_trigger",
                "pending_conversation", "pending_discard_ability")):
            log_req("    PvP pass ignored while an interactive prompt is "
                    "pending")
            return True
        previous_phase = int(live.get("phase", 0) or 0)
        previous_priority = int(live.get("priority_pid", 0) or 0)
        if (port.action_stack.peek() is None and live.get("stack") and
                getattr(port, "rehydrate_projected_chain", None)):
            port.rehydrate_projected_chain()
        if isinstance(port.action_stack.peek(), PriorityWindowAction):
            native_priority_action = port.action_stack.peek()
            player_id = getattr(_transaction, "player_id", None)
            live_phase = int(live.get("phase", 0) or 0)
            if live_phase == int(_ge.ETurnPhases.FirstMainPhase):
                # FirstMainState chooses the next phase using this fact. It
                # must be refreshed before the final pass ticks the native
                # scheduler, otherwise an empty board enters DeclareCombat
                # and can wait for a combat action that cannot exist.
                has_attackers = pvp_turn_has_attackers(
                    session, int(live.get("turn_pid", 0) or 0))
                port.has_legal_attackers = bool(has_attackers)
                port.active_player_skips_attack = not bool(has_attackers)
            # Keep a reconnectable pass record, while the native action queue
            # remains the authority for accepting and ordering the pass.
            raw_player = int(getattr(player_id, "uid64", player_id))
            pass_pid = (raw_player >> 8
                        if (raw_player & 0xFF) == 244 else raw_player)
            responding_to_chain = (
                getattr(native_priority_action, "ability_responding_to", None)
                is not None)
            pass_key = "stack_passed" if responding_to_chain else "passes"
            passed = set(int(value) for value in
                         (live.get(pass_key) or ()))
            passed.add(pass_pid)
            live[pass_key] = sorted(passed)
            if not port.pass_priority_and_drive(player_id):
                return False
            # The native scheduler has now either handed off, resolved the
            # chain, or entered the next phase. Project that result once.
            port.sync_to_pvp_state(live)
            pvp_save_state(session, live)
            # Keep the native scheduler checkpoint in lockstep with the PvP
            # projection.  Without this, the wire handoff can name the next
            # player while a reconnect or the next transaction rehydrates the
            # old PriorityWindowAction owner and rejects that player's pass.
            try:
                port.persist()
            except Exception as exc:
                log_req(f"    PvP RulesPort post-pass persistence failed: {exc}")
            # A chain resolution can suspend on an interactive prompt (for
            # example Corinth's charge power picks a card in the Choosing
            # zone).  The prompt helper already sent the private picker and
            # owns the next green light.  Pushing the ordinary priority
            # handoff/phase options here would immediately tear that picker
            # down, so leave priority with the pending input.
            if any(live.get(key) for key in (
                    "pending_choice", "pending_deck_search", "pending_trigger",
                    "pending_conversation", "pending_discard_ability")):
                pvp_save_state(session, live)
                log_req("    PvP pass paused for pending input; priority "
                        "handoff skipped")
                return True
            # RulesPort owns the queue mutation, but the host still owns the
            # historical client event projection. Without this handoff the
            # next player never receives GreenLight after the first pass and
            # the client remains stuck in Declare Combat.
            next_priority = int(live.get("priority_pid", 0) or 0)
            current_phase = int(live.get("phase", 0) or 0)
            if (next_priority and current_phase == previous_phase and
                    next_priority != previous_priority):
                pids_live = db_game_session_pids(session.session_id)
                for target_pid in pids_live:
                    target_h = player_handlers.get(int(target_pid))
                    if not target_h:
                        continue
                    other_pid = next(
                        (int(value) for value in pids_live
                         if int(value) != int(target_pid)), int(target_pid))
                    target_uid = _ge.UID.make(244, int(target_pid))
                    other_uid = _ge.UID.make(244, other_pid)
                    handoff = _ge.Game(
                        int(session.session_id), target_uid, other_uid)
                    _pvp_populate_game_state(
                        handoff, live, int(target_pid), other_pid)
                    handoff.push_green_light(
                        _ge.UID.make(244, next_priority),
                        _ge.EPriorityContext.Normal)
                    # A same-phase native handoff must rebuild the client's
                    # phase state as well as toggle GreenLight.  The legacy
                    # pass path already does this; omitting it here leaves
                    # the receiving client in an inactive/stale MainPhase
                    # state with no pass button even though the checkpoint
                    # correctly assigns it priority.
                    _pvp_push_turn_phase_with_elapsed(
                        handoff, current_phase,
                        _ge.UID.make(244, int(live.get("turn_pid") or
                                              next_priority)),
                        _ge.UID.make(244, next_priority),
                        _pvp_priority_elapsed_ticks(
                            live, next_priority) // 10_000_000)
                    _send_pvp_packet(
                        target_h, session, handoff, target_uid,
                        "rules-port-priority-handoff")
                pvp_push_phase_options(
                    session, live, pid=next_priority)
                log_req(f"    PvP RulesPort priority handoff: "
                        f"{previous_priority} -> {next_priority} "
                        f"phase={current_phase}")
            return True
        log_req("    PvP RulesPort rejected pass: native priority action missing")
        return False

    def native_phase_entry(phase):
        """Project a native RulesPort phase entry to the PvP wire state.

        ``_pvp_run_phase_start`` remains a packet/state projection: the
        native phase state has already selected the transition and will create
        the priority action immediately after this callback returns.  It no
        longer owns phase progression or pass handling.
        """
        live = pvp_load_state(session) or state
        turn_pid = int(live.get("turn_pid") or 0)
        if not turn_pid:
            return False
        pids_live = db_game_session_pids(session.session_id)
        if len(pids_live) < 2:
            return False
        port_enter_phase(live, phase)
        defender = next((int(pid) for pid in pids_live if int(pid) != turn_pid),
                         turn_pid)
        phase_priority = port.priority_players_for_phase(
            live, int(phase), turn_pid, defender)
        if phase_priority.name == "NONE":
            live.pop("priority_pid", None)
        else:
            live["priority_pid"] = (defender
                                     if phase == _ge.ETurnPhases.DeclareDefense
                                     else turn_pid)
        if phase == _ge.ETurnPhases.Discard:
            try:
                live["discard_required"] = (
                    db_hand_count(session.session_id, turn_pid, conn=_db)
                    > DEFAULT_MAX_HAND_SIZE)
            except (TypeError, ValueError):
                live["discard_required"] = False
        else:
            live.pop("discard_required", None)
        # FirstMainState's branch fact is refreshed at the native boundary;
        # this prevents a stale legacy cursor from reintroducing combat when
        # no legal attacker exists.
        live["_rules_port_attached"] = True
        try:
            port.active_player_skips_attack = not pvp_turn_has_attackers(
                session, turn_pid)
        except Exception:
            port.active_player_skips_attack = False
        pvp_save_state(session, live)
        _pvp_run_phase_start(session, live, phase)
        session._rules_port_mutation_emitted = True
        return True

    def native_turn_boundary(active_player_id):
        """Persist the RulesPort's completed EndTurn rotation for PvP."""
        live = pvp_load_state(session) or state
        try:
            raw = int(getattr(active_player_id, "uid64", active_player_id))
            next_pid = raw >> 8 if (raw & 0xFF) == 244 else raw
        except (TypeError, ValueError):
            return False
        if not next_pid:
            return False
        pids_live = db_game_session_pids(session.session_id)
        boundary = port_advance_turn_state(
            live, pids_live, incoming_player_id=next_pid)
        pvp_save_state(session, live)
        log_req("    PvP RulesPort turn boundary: next turn player "
                f"{boundary['turn_pid']}"
                + (" (bonus)" if boundary["bonus_used"] else ""))
        # Returning the typed identity lets the generic native scheduler keep
        # its active-player cursor aligned if this was a bonus/current-player
        # turn rather than the ordinary alternating handoff.
        return _ge.UID.make(244, int(boundary["turn_pid"]))

    def pvp_hand(kind, transaction):
        current = current_handler()
        if kind == "accept_starting_hand":
            return bool(current._handle_mulligan_keep_transaction(
                session, getattr(session, "_rules_port_dispatch_command", None)))
        if kind == "mulligan":
            return bool(current._handle_mulligan_redraw_transaction(
                session, getattr(session, "_rules_port_dispatch_command", None)))
        return False

    def setup_pick(transaction):
        """Project the typed Play/Draw choice into the PvP setup state."""
        original = getattr(session, "_rules_port_dispatch_command", None)
        if original is None:
            return False
        try:
            handled = bool(current_handler()._handle_choose_pick_transaction(
                session, original))
            if handled:
                session._rules_port_mutation_emitted = True
                # The play/draw choice is made; clear the opponent's wait.
                _pvp_push_waiting_on(session, None)
            return handled
        except Exception as exc:
            log_req(f"    PvP setup mutation failed: {exc}")
            return False

    def cancel_auto_pass(_transaction):
        live = pvp_load_state(session) or {}
        pid = int(current_handler().client_reck_id)
        if int(live.get("autopass_pid", 0) or 0) != pid:
            return True
        live.pop("autopass_pid", None)
        live.pop("autopass_state", None)
        pvp_save_state(session, live)
        return True

    def set_stops(transaction):
        live = pvp_load_state(session) or state
        pid = int(current_handler().client_reck_id)
        payload = getattr(transaction, "payload", {}) or {}
        live[f"stops_self_{pid}"] = list(payload.get("self_phases", ()))
        other = next((int(value) for value in pids if int(value) != pid), None)
        if other is not None:
            live[f"stops_opp_{other}"] = list(
                payload.get("opponent_phases", ()))
        pvp_save_state(session, live)
        return True

    def options(_transaction):
        live = pvp_load_state(session) or state
        # The native scheduler owns the phase.  A client asking for a resync
        # must be answered from the authoritative port, never by echoing a
        # stale checkpoint back (that is how the client and server drifted).
        # Align the checkpoint to the port so the projection and any reconnect
        # agree.
        phase = int(getattr(port, "current_turn_phase",
                            live.get("phase", 0)) or 0)
        live["phase"] = phase
        priority = getattr(port.action_stack, "priority_player_id", None)
        if priority is not None:
            raw_priority = int(getattr(priority, "uid64", priority))
            live["priority_pid"] = (raw_priority >> 8
                                    if (raw_priority & 0xFF) == 244
                                    else raw_priority)
        pvp_save_state(session, live)
        if phase in (int(_ge.ETurnPhases.FirstMainPhase),
                     int(_ge.ETurnPhases.SecondMainPhase)):
            pvp_push_main_phase_options(session, live)
        else:
            pvp_push_phase_options(session, live)
        return True

    # All callbacks are projections. No callback performs a second rules
    # validation path; the port has already accepted the typed transaction.
    port.set_card_transaction_resolver(native_executor)
    port.set_resource_transaction_resolver(lambda tx: pvp_projection(
        "play_resource", tx))
    port.set_setup_transaction_resolver(setup_pick)
    port.set_priority_transaction_resolver(pvp_pass)
    port.set_hand_transaction_resolver(pvp_hand)
    port.set_auto_pass_transaction_resolver(
        lambda tx: set_pvp_auto_pass(current_handler(), session,
                                     (getattr(tx, "payload", {}) or {}).get(
                                         "passing_state", 2)))
    port.set_cancel_auto_pass_transaction_resolver(cancel_auto_pass)
    port.set_priority_sync_resolver(options)
    port.set_turn_phase_resolver(set_stops)
    port.set_turn_boundary_resolver(native_turn_boundary)
    port.set_turn_phase_entry_resolver(native_phase_entry)
    port.set_discard_transaction_resolver(lambda tx: pvp_projection(
        "discard", tx))
    port.set_discard_continuation_resolver(lambda tx: pvp_projection(
        "discard_continuation", tx))
    port.set_triggered_ability_transaction_resolver(lambda tx: pvp_projection(
        "triggered", tx))
    port.set_attack_transaction_resolver(lambda tx: pvp_projection(
        "attack", tx))
    port.set_defense_transaction_resolver(lambda tx: pvp_projection(
        "defense", tx))
    port.set_damage_transaction_resolver(lambda tx: pvp_projection(
        "damage", tx))
    port.set_choice_transaction_resolver(lambda tx: pvp_projection(
        "choice", tx))
    port.set_player_options_resolver(options)
    port.set_ready_card_transaction_resolver(lambda tx: pvp_projection(
        "ready", tx))
    port.set_encounter_mod_resolver(lambda tx: pvp_projection(
        "conversation", tx))
    port.set_quit_game_resolver(lambda _tx: bool(
        pvp_concede(current_handler(), session)))
    port.assert_projection_wiring()
    port.rehydrate_combats()
    # ``restore_snapshot`` runs before the PvP callbacks above exist.  Now
    # that the native chain resolver is wired, rebuild the current projected
    # response window if this handler was materialized by reconnect.
    port.rehydrate_projected_chain()
    port._pvp_current_handler = handler
    port._pvp_current_session = session
    port._pvp_state = state
    session._rules_port_session = port
    set_pvp_shared_port(session, port)
    # ``restore_snapshot`` may have loaded a pre-migration native owner while
    # the PvP checkpoint has the current owner.  Persist the reconciled native
    # snapshot immediately so reconnect cannot resurrect that stale turn
    # owner on the next process or handler attach.
    port.persist()
    log_req(f"    PvP RulesPort attached session={session.session_id}")
    return port


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


def _pvp_emit_paid_cost_events(game, state):
    """Project deferred activation-cost pool changes onto the event stream.

    ``native_activation_cost_payer`` pays the cost while the transaction is
    still being classified, before the mode creates the resolution Game.  The
    deltas are parked in the checkpoint and emitted here so both clients get
    the ChampionChargePointsChanged / resource / spell-point HUD events.
    """
    pending = state.pop("_pvp_paid_costs", None)
    if not pending:
        return
    for paid in pending:
        uid = _ge.UID.make(244, int(paid.get("owner_id") or 0))
        if paid.get("resource"):
            ev = _ge.PlayerCurrentResourcePoolChangedSessionEventArgs()
            ev.player_id = uid
            ev.operation = 2
            ev.delta = int(paid["resource"])
            ev.new_value = int(paid.get("res_new", 0) or 0)
            game._push(ev)
        if paid.get("charge"):
            ev = _ge.ChampionChargePointsChangedSessionEventArgs()
            ev.player_id = uid
            ev.operation = 2
            ev.delta = int(paid["charge"])
            ev.new_value = int(paid.get("chg_new", 0) or 0)
            game._push(ev)
        if paid.get("spell"):
            ev = _ge.ChampionSpellPointsChangedSessionEventArgs()
            ev.player_id = uid
            ev.operation = 2
            ev.delta = int(paid["spell"])
            ev.new_value = int(paid.get("sp_new", 0) or 0)
            game._push(ev)
        if paid.get("life"):
            ev = _ge.ChampionHealthChangedSessionEventArgs()
            ev.player_id = uid
            ev.operation = 2
            ev.delta = int(paid["life"])
            ev.new_value = int(paid.get("hp_new", 0) or 0)
            game._push(ev)


def _pvp_sync_view_to_state(state, view, player_pid, opponent_pid):
    """Persist per-player values changed through a FRA-shaped PvP view.

    Ability resolution uses the same FRA-shaped dictionary in Practice and
    PvP.  It is a view, not a live alias, so resource/threshold/charge changes
    made by a BOM must be copied back before the next legality/options check.
    """
    from rules_port.pvp_view import apply_effect_view
    apply_effect_view(state, view, player_pid, opponent_pid)


def _pvp_resolve_granted_resource_abilities(handler, session, state,
                                            card_uid, owner_pid):
    """Resolve instance-only resource abilities on a shared PvP event view."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return []
    owner_pid = int(owner_pid)
    opponent_pid = next(pid for pid in pids if int(pid) != owner_pid)
    owner_handler = player_handlers.get(owner_pid) or handler
    pl_uid = _ge.UID.make(244, owner_pid)
    opp_uid = _ge.UID.make(244, opponent_pid)
    view = dict(state)
    view.update({
        "pvp": True,
        "player_resources": int(state.get(f"res_{owner_pid}", 0)),
        "player_total_resources": int(
            state.get(f"res_total_{owner_pid}", 0)),
        "player_charges": int(state.get(f"chg_{owner_pid}", 0)),
        "player_threshold": dict(
            state.get(f"thresh_{owner_pid}") or {}),
        "ai_resources": int(state.get(f"res_{opponent_pid}", 0)),
        "ai_total_resources": int(
            state.get(f"res_total_{opponent_pid}", 0)),
        "ai_charges": int(state.get(f"chg_{opponent_pid}", 0)),
        "ai_threshold": dict(
            state.get(f"thresh_{opponent_pid}") or {}),
    })
    owner_handler._current_bstate = view
    game = _ge.Game(int(session.session_id), pl_uid, opp_uid)
    _pvp_populate_game_state(game, state, owner_pid, opponent_pid)
    from rules_port.resources import resolve_granted_resource_abilities
    from rules_port.resolution import resolve_port_ability
    logs = resolve_granted_resource_abilities(
        game, session, _db, owner_handler, pl_uid, opp_uid, view,
        int(card_uid), owner_pid,
        resolver=lambda _handler, _game, _session, _db_conn, _pl_t, _ai_t,
        _bstate, guid, source, owner, _target_map:
        resolve_port_ability(
            _handler, _game, _session, _db_conn, _pl_t, _ai_t, _bstate,
            guid, source, owner, target_map={}))
    _pvp_sync_view_to_state(state, view, owner_pid, opponent_pid)
    pvp_save_state(session, state)
    if logs:
        log_req("    PvP resource granted abilities: " + "; ".join(logs))
    return game.events


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


def _pvp_chain_active(session, state=None):
    """Read chain activity from the active RulesPort authority.

    Tournament checkpoints retain ``stack`` for wire/reconnect compatibility,
    but an attached native session has a separate typed Chain instance. Using
    the mirror for UI or priority decisions can expose a stale Resolve window
    after the native chain has already emptied.
    """
    port = getattr(session, "_rules_port_session", None)
    if port is not None:
        return not getattr(port.chain, "is_empty", True)
    return bool((state or {}).get("stack"))


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


def _pvp_apply_visibility(game, state):
    """Attach persisted player-level visibility to a fresh PvP Game."""
    from rules_port.visibility import \
        apply_player_visibility_to_game
    apply_player_visibility_to_game(game, state or {})


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
        _pvp_apply_visibility(g, state)
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
        view = _pvp_fra_view(state, turn_pid, opp_pid)
        repl_draw = (_pvp_dispatch_triggers(
            draw_h, g, session, view, turn_uid_p, opp_uid_p,
            "CardWouldBeDrawnEvent", None, turn_pid) if draw_h else None)
        repl_zone = (_pvp_dispatch_triggers(
            draw_h, g, session, view, turn_uid_p, opp_uid_p,
            "CardWouldEnterZoneEvent", int(cu), turn_pid) if draw_h else None)
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
        if draw_h:
            _pvp_dispatch_triggers(
                draw_h, g, session, view, turn_uid_p, opp_uid_p,
                "CardEnteredZoneEvent", int(cu), turn_pid)
    except Exception as e:
        log_req(f"    PvP CardEnteredZoneEvent trigger error: {e}")
    # "When you draw" triggers (both sides' cards react — "when you draw" and
    # "when an opposing champion draws").  The client's CardDrawnEvent carries
    # SourceCardId = the drawing champion, TargetCardId = the drawn card.
    champ_map = state.get("champ_map") or {}
    champ_uid = int(champ_map.get(str(turn_pid), 0)) or None
    try:
        # Reuse the same authoritative view that received the zone-entry
        # trigger above; otherwise its stack/health mutations would be lost
        # before the CardDrawnEvent pass.
        if draw_h:
            _pvp_dispatch_triggers(
                draw_h, g, session, view, turn_uid_p, opp_uid_p,
                "CardDrawnEvent", champ_uid, turn_pid,
                target_card_id=int(cu))
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
    # Every phase-start event is built from the active player's point of view.
    # Resolve the opposing pid before the TurnPhaseEvent path below; the first
    # non-StartTurn phase after mulligan also enters that path.
    defender_pid = pids[1] if turn_uid == pids[0] else pids[0]
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
                st_view = _pvp_fra_view(
                    state, turn_uid,
                    pids[1] if turn_uid == pids[0] else pids[0])
                st_view["phase"] = phase
                st_view["_last_turn_phase_event"] = state.get(
                    "_last_turn_phase_event")
                st_view["_rules_port_attached"] = True
                from rules_port.context import EffectContext
                from rules_port.tunneling import advance, queue_surfaces
                tunnel_context = EffectContext.from_rules_port(
                    warm, session, _db, turn_h, turn_uid_p, opp_uid_st,
                    st_view, "", ability=None)
                tunnel_changes = advance(tunnel_context, turn_uid)
                _pvp_dispatch_triggers(
                    turn_h, warm, session, st_view, turn_uid_p, opp_uid_st,
                    "TurnPhaseEvent", None, turn_uid,
                    phase=int(phase))
                state["_last_turn_phase_event"] = st_view.get(
                    "_last_turn_phase_event")
                _pvp_dispatch_triggers(
                    turn_h, warm, session, st_view, turn_uid_p, opp_uid_st,
                    "TurnStartedEvent", None, turn_uid)
                tunnel_surfaces = queue_surfaces(tunnel_context, turn_uid)
                state["_next_instance_id"] = st_view.get(
                    "_next_instance_id", state.get("_next_instance_id", 1))
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
                        c_basic = db_card_basic(
                            session.session_id, cu64, conn=_db)
                        if c_basic:
                            turn_h._card_full_data(warm, c_scid, c_basic[0])
                            cdef = warm.card_defs.get(c_scid)
                            if cdef is not None:
                                cdef.counters = dict(
                                    (state.get("champion_counters") or {}).get(
                                        str(cu64), {}) or {})
                            # Champion re-push carries the CURRENT health as
                            # defense — otherwise the client's champion
                            # representation resets to the template's base
                            # (20), briefly showing 20 HP at each phase change
                            # before the real value re-renders.
                            warm.push_card_updated(
                                c_scid, c_uid, _ge.ECardCollections.Champions,
                                _ge.ECardTypes.Champion,
                                template_id=c_basic[0],
                                defense=int(state.get(f"hp_{cpid}", 20)))
                pvp_save_state(session, state)
                if warm.events:
                    _pvp_send_same_events(session, warm, turn_uid_p, opp_uid_st)
                log_req(f"    PvP StartTurn: TurnStartedEvent fired + "
                        f"tunneling +{len(tunnel_changes)} / "
                        f"surface queued {len(tunnel_surfaces)} + "
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
        from rules_port.resources import begin_turn_resources_for_player
        resource_refill = begin_turn_resources_for_player(state, turn_uid)
        from rules_port.lifecycle import clear_expired_temporary_attributes
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
        wz_rows = db_warzone_troops_with_state(
            session.session_id, turn_uid, conn=_db)
        for wzr in wz_rows:
            wz_uid = int(wzr[0])
            db_update_card_state(
                session.session_id, wz_uid,
                set_bits=_ge.ECardStates.StartedATurnOnYourSide,
                clear_bits=_ge.ECardStates.CameOutThisTurn |
                _ge.ECardStates.Tapped |
                _ge.ECardStates.Attacking |
                _ge.ECardStates.HasAttacked |
                _ge.ECardStates.Blocking |
                _ge.ECardStates.HasBlocked,
                reset_damage=True)
            pstate = db_card_state_value(session.session_id, wz_uid)
            if not pstate:
                pstate = _ge.ECardStates.StartedATurnOnYourSide
            ct_str = db_game_card_type(wzr[1])
            wz_ct = _ge.card_type_from_db(ct_str) if ct_str else _ECardTypes.Troop
            prep_wz.append((_ge.SessionCardId(_ge.UID(wz_uid)), wzr[1],
                            wz_ct, pstate))
        _db.commit()
        log_req(f"    PvP Prep: refilled {turn_uid} to "
                f"{resource_refill.new_value}, readied "
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
    # TurnPhaseEvent is emitted by the client's TurnPhaseState.OnEntry.  PvP
    # constructs one Game packet per viewer, so resolve it once against the
    # active player's handler and mirror the resulting chain events to both.
    if phase != _ge.ETurnPhases.StartTurn:
        phase_h = player_handlers.get(turn_uid)
        if phase_h:
            phase_game = _ge.Game(int(session.session_id),
                                  _ge.UID.make(244, turn_uid),
                                  _ge.UID.make(244, defender_pid))
            _pvp_apply_visibility(phase_game, state)
            phase_view = _pvp_fra_view(state, turn_uid, defender_pid)
            phase_view["phase"] = phase
            phase_view["_last_turn_phase_event"] = state.get(
                "_last_turn_phase_event")
            _pvp_dispatch_triggers(
                phase_h, phase_game, session, phase_view,
                _ge.UID.make(244, turn_uid),
                _ge.UID.make(244, defender_pid), "TurnPhaseEvent", None,
                turn_uid, phase=phase)
            # ``Shifted Paradigm`` and every other metadata-defined
            # end-of-turn ability listens for TurnEndedEvent, not the client
            # phase notification.  Native PvP reaches EndPhase through the
            # RulesPort scheduler, so fire the semantic event here before
            # Discard/EndTurn and let the ordinary projected chain resolve.
            # Mirrors C# ``EndPhaseState.OnEntry``: the event source is the
            # active player's champion card.
            if (phase == _ge.ETurnPhases.EndPhase and
                    not state.get("turn_end_trigger_fired")):
                end_champion_uid = int(champ_map.get(str(turn_uid), 0) or 0)
                _pvp_dispatch_triggers(
                    phase_h, phase_game, session, phase_view,
                    _ge.UID.make(244, turn_uid),
                    _ge.UID.make(244, defender_pid), "TurnEndedEvent",
                    end_champion_uid or None, turn_uid)
                state["turn_end_trigger_fired"] = True
            state["_last_turn_phase_event"] = phase_view.get(
                "_last_turn_phase_event")
            if phase_game.events:
                _pvp_send_same_events(
                    session, phase_game, _ge.UID.make(244, turn_uid),
                    _ge.UID.make(244, defender_pid))
    chain_from_phase_start = _pvp_chain_active(session, state)
    phase_priority_pid = (defender_pid
                          if phase == _ge.ETurnPhases.DeclareDefense
                          else turn_uid)
    if chain_from_phase_start:
        # A phase-entry trigger (notably Corinth's end-of-turn ability) is
        # already a native RulesPort response action by this point.  Its
        # APNAP owner, rather than the phase's ordinary active player, is the
        # GreenLight owner that must be projected to both clients.
        native_port = getattr(session, "_rules_port_session", None)
        native_action = (native_port.action_stack.peek()
                          if native_port is not None else None)
        native_priority = getattr(native_action, "priority_player_id", None)
        if native_priority is not None:
            try:
                raw_priority = int(getattr(native_priority, "uid64",
                                           native_priority))
                phase_priority_pid = (
                    raw_priority >> 8
                    if (raw_priority & 0xFF) == 244 else raw_priority)
            except (TypeError, ValueError):
                pass
    # RulesPort classifies these lifecycle phases as NONE: they are legal
    # phase entries but the client has no action window in them.  Keep the
    # phase event for UI sequencing, while suppressing GreenLight so the
    # transport cannot manufacture a priority request the native scheduler
    # does not own.
    try:
        from rules_port.kernel import TurnPhasePlayers
        from rules_port.pvp_session import PvpAuthoritativeSession
        native_window = PvpAuthoritativeSession.priority_players_for_phase(
            state, int(phase), int(turn_uid), int(defender_pid))
        emits_priority = (chain_from_phase_start or
                          native_window is not TurnPhasePlayers.NONE)
    except (ImportError, TypeError, ValueError):
        emits_priority = phase not in (
            _ge.ETurnPhases.StartGame, _ge.ETurnPhases.StartTurn,
            _ge.ETurnPhases.Ready, _ge.ETurnPhases.Prep,
            _ge.ETurnPhases.Draw)
    if phase not in (3, 4):
        # Start the new priority interval before emitting TurnPhaseUpdated so
        # the event can carry the cumulative time already spent by this
        # player in earlier priority windows.
        if emits_priority:
            state["priority_pid"] = phase_priority_pid
        else:
            state.pop("priority_pid", None)
        pvp_save_state(session, state)
    for pid in pids:
        h = player_handlers.get(pid)
        if not h:
            continue
        is_me = (pid == turn_uid)
        pl_t = _ge.UID.make(244, pid)
        opp_t = _ge.UID.make(244, pids[1] if pid == pids[0] else pids[0])
        g = _ge.Game(int(session.session_id), pl_t, opp_t)
        _pvp_apply_visibility(g, state)
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
        if emits_priority:
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
    if _pvp_chain_active(session, state):
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
    rows = db_warzone_attack_option_rows(
        session.session_id, turn_pid, conn=_db)
    from rules_port.static_rules import effective_attributes
    for uid, cstate, _card_type, attrs in rows:
        cstate = cstate or 0
        attrs = int(attrs or 0) | int(effective_attributes(
            _db, session.session_id, state, int(uid)) or 0)
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
    for uid, cstate, _card_type, attrs in rows:
        cstate = cstate or 0
        attrs = int(attrs or 0) | int(effective_attributes(
            _db, session.session_id, state, int(uid)) or 0)
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
    _pvp_apply_visibility(g, state)
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
        trow = db_card_basic(session.session_id, u, conn=_db)
        tpl_guid = trow[0] if trow else None
        h_card = h  # the turn player's handler
        if tpl_guid:
            h_card._card_full_data(g, scid, tpl_guid)
        pushed_state = db_card_state_value(session.session_id, u) or state_bits
        g.push_card_updated(scid, pl_t, _ge.ECardCollections.Warzone,
                            _ge.ECardTypes.Troop, template_id=tpl_guid,
                            state=pushed_state)
        cs = _ge.CombatSessionEventArgs()
        cs.player_id = pl_t
        cs.id = cid
        cs.attacker = scid
        cs.blockers = []
        combats.append(cs)
        view = _pvp_fra_view(state, turn_pid, opp_pid)
        _pvp_dispatch_triggers(
            h_card, g, session, view, pl_t, opp_t,
            "CardAttackedEvent", int(u), turn_pid)
        _pvp_dispatch_triggers(
            h_card, g, session, view, pl_t, opp_t,
            "CardAttackedOrBlockedEvent", int(u), turn_pid)
        from rules_port.context import EffectContext
        from rules_port.combat_effects import apply_rage
        apply_rage(EffectContext.from_rules_port(
            g, session, _db, h_card, pl_t, opp_t, view,
            "", ability=None), int(u))
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
    from rules_port.combat_rules import can_block
    rows = db_warzone_blocker_uids(
        session.session_id, defender_pid, _ge.ECardStates.Tapped, conn=_db)
    g = _ge.Game(int(session.session_id), pl_t, opp_t)
    _pvp_apply_visibility(g, state)
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
    for (uid,) in rows:
        blockable = []
        for scid, u in zip(attacker_scids, attackers):
            if can_block(_db, session.session_id,
                         _pvp_fra_view(state, turn_pid, defender_pid),
                         int(u), int(uid)):
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
    from rules_port.combat_rules import can_block
    rows = db_warzone_blocker_uids(
        session.session_id, defender_pid, _ge.ECardStates.Tapped, conn=_db)
    count = 0
    view = _pvp_fra_view(state, defender_pid, turn_pid)
    for (uid,) in rows:
        for u in attackers:
            if can_block(_db, session.session_id, view, int(u), int(uid)):
                count += 1
                break
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
    from rules_port.static_rules import effective_attributes
    playable = []
    for cu, cost, ct_name, thresh_json, _ab in \
            db_hand_cards_with_templates(session.session_id, turn_pid):
        # A printed troop/artifact can become Quick through a continuous
        # CardCreated aura (for example, Robots in hand while an underground
        # Saboteur is controlled).  The client uses the effective attribute,
        # not the immutable card_type, to decide whether it is offered in a
        # response window.
        try:
            effective_attrs = int(effective_attributes(
                _db, session.session_id, state, int(cu)) or 0)
        except Exception:
            effective_attrs = 0
        if ("QuickAction" not in (ct_name or "") and not
                (effective_attrs & _ge.ECardAttributes.QuickAction)):
            continue
        if (cost or 0) > resources:
            continue
        if not _pvp_thresholds_met(thresh_json, threshold):
            continue
        try:
            ability_guids = [x.lower() for x in json.loads(_ab or "[]")]
        except Exception:
            ability_guids = []
        trow = db_card_basic(session.session_id, cu, conn=_db)
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
    _pvp_apply_visibility(g, state)
    from pvp_db import db_game_get_hand
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
    rows = db_warzone_display_rows(session.session_id, conn=_db)
    for card_uid, tpl_guid, user_id, cstate, db_ct in rows:
        scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
        if wz_handler:
            wz_handler._card_full_data(g, scid, tpl_guid)
        cdef = g.card_defs.get(scid)
        attrs = cdef.attributes if cdef else 0
        gems = cdef.gems if cdef else 0
        owner = _ge.UID.make(244, user_id)
        ct = _ge.card_type_from_db(db_ct)
        g.push_card_updated(scid, owner, _ge.ECardCollections.Warzone,
                            ct, template_id=tpl_guid,
                            attributes=attrs, state=int(cstate or 0),
                            gems=gems)
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
    from rules_port.combat_rules import player_has_eligible_attackers
    return player_has_eligible_attackers(
        _db, session.session_id,
        battle_state=pvp_load_state(session) or {},
        player_id=turn_pid)


def pvp_phase_is_stop(state, phase, turn_pid, opp_pid):
    """True if EITHER player wants to stop at `phase` during the turn player's
    turn: the turn player's self-stops (their own turn) OR the opponent's
    opponent-stops (the opponent's turn), falling back to the client defaults
    when a player hasn't configured stops.  Mirrors battle_engine.is_self_stop
    / is_opp_stop."""
    return port_phase_is_stop(state, phase, turn_pid, opp_pid)


def pvp_player_auto_passes(state, pid):
    """Whether *pid* has enabled the client's F10 auto-pass mode."""
    return port_player_auto_passes(state, pid)


def _pvp_auto_pass_chain_priority(session, state, pid):
    """Consume a chain response for an F10-enabled player.

    The client normally submits the follow-up PassPriority itself.  PvP can
    hand a GreenLight to an auto-passing client while it is rebuilding its
    chain/priority UI, leaving the opponent's card waiting for a manual
    Resolve click.  The server owns the authoritative two-pass state, so
    consume this response here and use the normal pass route.
    """
    if (not _pvp_chain_active(session, state) or
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
    from rules_port import lifecycle as _be
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
    from rules_port import lifecycle as _be
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
    phase_list = _pvp_turn_phase_list(state, turn_pid, has_ready)
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
        port_enter_phase(state, new_phase)
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
            hand_count = db_hand_count(
                session.session_id, turn_pid, conn=_db)
            if hand_count > DEFAULT_MAX_HAND_SIZE:
                log_req(f"    PvP auto-advance: stopped at Discard "
                        f"(hand {hand_count} > {DEFAULT_MAX_HAND_SIZE})")
                return advanced


def _pvp_turn_phase_list(state, turn_pid, has_ready):
    """Build the active player's phase cycle, including authored extra
    combats scheduled in ThisTurnsData."""
    return port_turn_phase_list(state, turn_pid, has_ready)


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
    from gamedata import PlayPlan
    from gamedata import ability_graph
    from rules_port.costs import card_cost_targets, cost_type_for_kind
    from rules_port.targeting import legal_targets_for
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
    store = _RECORD_STORE
    try:
        play_plan = PlayPlan.from_card(store, tpl_guid, source_uid=card_uid,
                                       owner_id=turn_pid)
    except KeyError:
        return False
    if play_plan.cost_instances:
        for cost_spec in card_cost_targets(
                play_plan, _db, session.session_id, turn_pid, int(card_uid),
                champions=champ_targets, battle_state=state):
            if (not cost_spec.is_source_auto_target and
                    len(cost_spec.candidates) < int(cost_spec.minimum)):
                return False
    for ag in (ability_guids or []):
        graph = ability_graph(store, str(ag).lower())
        if graph is None:
            # A card whose current Records definition is unavailable is not
            # playable; do not silently substitute a stale DB ability shape.
            return False
        if graph.manual or graph.trigger_event_type:
            continue
        instance = next((ability for ability in play_plan.abilities
                         if ability.ability_guid.lower() == str(ag).lower()),
                        None)
        if instance is None:
            return False
        for index in instance.referenced_target_indexes:
            if index < 0 or index >= len(graph.targets):
                continue
            target = graph.targets[index]
            if not target.requires_input or target.minimum < 1:
                continue
            try:
                candidates = legal_targets_for(
                    _db, session.session_id, turn_pid, target, 0,
                    both_players=True, champions=champ_targets,
                    battle_state=state)
            except Exception:
                continue
            if not candidates:
                log_req(f"    PvP options: {ct_name} not playable "
                        f"(target template {target.guid[:8]} "
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
    from pvp_db import db_game_get_hand, db_game_card_type
    from pvp_db import db_card_template_thresholds
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
            from rules_port.static_rules import effective_cost as _ec
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
        ab_json = db_template_ability_payload(tg, conn=_db)
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
    # Options packets can follow a resource packet and may contain state
    # events from card/ability projections.  Hydrate the complete PvP HUD
    # first; a bare Game defaults to health 20/10 and zero charges, which can
    # overwrite the valid resource update on the client.
    _pvp_populate_game_state(g, state, turn_pid, opp_pid)
    champ_map = state.get("champ_map") or {}
    g.player_champion_card_id = _ge.SessionCardId(
        _ge.UID(int(champ_map.get(str(turn_pid), 0)))) if champ_map.get(
            str(turn_pid)) else None
    g.ai_champion_card_id = _ge.SessionCardId(
        _ge.UID(int(champ_map.get(str(opp_pid), 0)))) if champ_map.get(
            str(opp_pid)) else None
    h._current_bstate = state
    _pvp_add_hand_card_updates(g, session, state, turn_pid, pl_t)
    # CardUpdated must precede PlayerOptionList.  The Unity client processes
    # PlayerOptionList immediately and updates the champion HUD button against
    # its current CardRepresentation.  Sending the list first leaves a stale
    # champion/ability cache after a resource grants a charge, making the
    # clickable charge button submit no activation.
    pvp_push_warzone_updates(session, state, game=g)
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
    # Log the final option payload after every contributor has appended to it.
    # The client replaces its PlayerOptions cache on each PlayerOptionList, so
    # the server-side affordability count alone cannot show whether the
    # champion option survived into the 3055 packet.
    for event in g.events:
        if not isinstance(event, _ge.PlayerOptionListSessionEventArgs):
            continue
        option_parts = []
        for option in event.options:
            if not isinstance(option, _ge.PlayerOptionSessionEventArgs):
                continue
            card_uid = getattr(getattr(option.card, "uid", None), "uid64", option.card)
            instance_parts = []
            for instance in option.instances:
                if not isinstance(instance, _ge.OptionInstanceSessionEventArgs):
                    continue
                targets = getattr(instance, "target_instances", ()) or ()
                target_count = len(targets)
                cost_count = sum(
                    1 for target in targets
                    if isinstance(target, _ge.CostInstanceSessionEventArgs)
                )
                instance_parts.append(
                    f"{str(instance.opt_id.guid)[:8]}"
                    f"/targets={target_count}/costs={cost_count}"
                )
            option_parts.append(
                f"card={card_uid}/state={int(option.state)}/"
                f"instances=[{','.join(instance_parts)}]"
            )
        log_req(
            f"    PvP final-options pid={event.player_id} "
            f"options=[{' ; '.join(option_parts)}]"
        )
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
            offered_names.append(db_template_name(_tg, conn=_db) or _tg)
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
    crow = db_card_basic(session.session_id, cu, conn=_db)
    if not crow:
        return
    tpl_guid = crow[0]
    from pve_db import db_talent_ability_costs
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
        all_rids.append(_ge.ResourceId.from_str(ag))
        # Champion ability lists contain both player-activated powers and
        # automatic talents/signature triggers. Keep every ability in the
        # CardDef so the HUD can display it, but only a metadata-marked manual
        # ability may become a PlayerOption. In particular, Wind Whisperer's
        # Channelling trigger is zero-cost and would otherwise make the
        # champion appear activatable whenever it fires on the chain.
        graph = ability_graph(_RECORD_STORE, str(ag).lower())
        if graph is not None and not graph.manual:
            continue
        row = db_champion_ability_costs(str(ag))
        if row is None:
            row = db_talent_ability_costs(str(ag))
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
        # BasicAction powers are legal only in the controller's own main
        # phase, and never while a chain item is waiting to resolve. The
        # client keeps reading the most recent PlayerOptionList after a
        # chain animation, so rechecking this here prevents a stale/refresh
        # packet from making the champion clickable on the stack.
        if _pvp_chain_active(session, state) and casting != 64:
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
    if db_is_champion_template(tpl_guid):
        hp = int(state.get(f"hp_{pid}", 20))
        g.card_defs[champ_scid] = _ge.CardDef(
            "Champion", _ge.ECardTypes.Champion, 0, hp, hp, [], list(all_rids))
    # Target data: for champion abilities with explicit targets (e.g. Dimmid's
    # "Target troop gets Lifedrain") attach legal TargetInstances so the
    # client's target picker shows candidates — mirrors PvE
    # _champion_ability_targets.  Without this CanUseAbility is false and the
    # button is dead.
    from rules_port.targeting import legal_targets_for
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
        graph = ability_graph(_RECORD_STORE, ag.lower())
        if graph is None:
            continue
        entries = []
        for target in graph.targets:
            if not target.requires_input:
                continue
            tid = target.guid
            if target.is_auto or target.target_kind == "PlayerTargetTemplate":
                continue
            try:
                cands = legal_targets_for(
                    _db, session.session_id, pid, target, int(cu),
                    both_players=False, champions=champ_targets,
                    battle_state=state)
            except Exception:
                cands = []
            if not cands:
                continue
            entries.append((tid, cands, target.minimum or 1,
                            target.maximum if target.maximum > 0 else 1))
        if entries:
            target_data[ag] = entries
    # The champion definition must reach the client before the option list.
    # Otherwise the client can cache the activation against an older
    # CardRepresentation and the visible charge button becomes locally
    # unrecognized (no ActivateAbilityTransaction is emitted).  We still
    # append the option to the existing PlayerOptionList below; the Game
    # helper deliberately finds that list rather than requiring it to be the
    # final event.
    if db_is_champion_template(tpl_guid):
        hp = int(state.get(f"hp_{pid}", 20))
        cdef = g.card_defs.get(champ_scid)
        counters = dict((state.get("champion_counters") or {}).get(
            str(int(champ_scid.uid.uid64)), {}) or {})
        if cdef is not None:
            cdef.counters = counters
        g.push_card_updated(
            champ_scid, _ge.UID.make(244, pid),
            # Champion updates in the HUD use the initial ``None_``
            # collection.  ``Game.push_card_updated`` suppresses later
            # ``Champions`` collection updates to prevent duplicate board
            # views, which would otherwise silently drop this ability-cache
            # refresh.
            _ge.ECardCollections.None_, _ge.ECardTypes.Champion,
            template_id=tpl_guid, defense=hp, counters=counters)
        # ``push_options`` already created the list that will own the
        # champion activation.  Move this definition in front of that list in
        # the actual event stream; mutating the list afterward is not enough,
        # because Unity processes events in wire order.
        champion_update = g.events.pop()
        option_index = next(
            (index for index, event in enumerate(g.events)
             if isinstance(event, _ge.PlayerOptionListSessionEventArgs)),
            len(g.events))
        g.events.insert(option_index, champion_update)
    g.add_champion_to_options(pl_t, champ_scid, afford,
                              target_data=target_data or None)
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
    from gamedata import AbilityInstance, PlayPlan
    from rules_port.targeting import legal_targets as _lt
    champ_map = state.get("champ_map") or {}
    champ_targets = []
    for cpid in (state.get("pids") or []):
        cu = int(champ_map.get(str(cpid), 0))
        if cu:
            champ_targets.append((cu, cpid, "Champ",
                                  int(state.get(f"hp_{cpid}", 20))))
    for opt in last_ev.options:
        card_uid = int(opt.card.uid.uid64)
        row = db_card_basic(session.session_id, card_uid, conn=_db)
        if not row:
            continue
        store = _RECORD_STORE
        try:
            plan = PlayPlan.from_card(store, row[0], source_uid=card_uid,
                                      owner_id=turn_pid)
        except KeyError:
            continue
        for ability in plan.abilities:
            graph = ability.graph
            if graph is None or graph.manual or ability.is_triggered:
                continue
            for i in ability.referenced_target_indexes:
                if i >= len(graph.targets):
                    continue
                target = graph.targets[i]
                if not target.requires_input:
                    continue
                tid = target.guid
                trow = db_target_template_info(tid, conn=_db)
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
                inst.opt_id = _ge.ResourceId.from_str(ability.ability_guid)
                minimum = max(0, int(target.minimum))
                maximum = max(minimum, int(target.maximum or minimum or 1))
                inst.min_target_counts.append(minimum)
                inst.max_target_counts.append(maximum)
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
                        inst2.min_target_counts.append(minimum)
                        inst2.max_target_counts.append(maximum)
                        tgt2 = g._make_event(_ge.TargetInstanceSessionEventArgs)
                        tgt2.target_index = len(inst2.target_instances)
                        tgt2.target_id = _ge.ResourceId.from_str(tid)
                        tgt2.targets = list(targets)
                        inst2.target_instances.append(tgt2)
                        break
        # Card-level target costs use the same CostInstance contract as
        # activated abilities.  Preserve their authored order so the client
        # assigns them into the matching XCostData collection.
        try:
            from rules_port.costs import card_cost_targets
            cost_candidates = card_cost_targets(
                plan, _db, session.session_id, turn_pid, int(card_uid),
                champions=champ_targets, battle_state=state)
        except ValueError:
            cost_candidates = ()
        for cost_spec in cost_candidates:
            if cost_spec.is_source_auto_target:
                continue
            cost_guid = cost_spec.guid
            cost_uids = list(cost_spec.candidates)
            if not cost_uids:
                continue
            minimum = int(cost_spec.minimum)
            maximum = int(cost_spec.maximum)
            if maximum < 0:
                maximum = len(cost_uids)
            for inst in opt.instances:
                if str(inst.opt_id.guid) == _ge.PLAY_CARD_ABILITY_TEMPLATE_ID:
                    ci = g._make_event(_ge.CostInstanceSessionEventArgs)
                    ci.min = minimum
                    ci.max = maximum
                    from rules_port.costs import cost_type_for_kind
                    ci.cost_type = cost_type_for_kind(cost_spec.kind)
                    ci.target_template_id = _ge.ResourceId.from_str(cost_guid)
                    ci.targets = [_ge.SessionCardId(_ge.UID(int(uid)))
                                  for uid in cost_uids]
                    inst.target_instances.append(ci)
                    break
        # Variable X cost: attach an XCost CostInstance to the PlayCard option
        # so the client's BattleStateAssignXCost pushes the X slider — only for
        # templates with variable_cost (mirrors PvE _template_has_x_cost).
        if plan.cost.variable:
            for inst in opt.instances:
                if str(inst.opt_id.guid) == _ge.PLAY_CARD_ABILITY_TEMPLATE_ID:
                    ci = g._make_event(_ge.CostInstanceSessionEventArgs)
                    ci.min = plan.cost.variable_minimum
                    ci.max = 0
                    ci.cost_type = 256  # EAbilityCostType.XCostAbilityCostType
                    ci.target_template_id = _ge.ResourceId.invalid()
                    ci.targets = []
                    inst.target_instances.append(ci)
                    break


def _pvp_affordable_troop_abilities(session, state, pid=None):
    """Return {(card_uid, tpl_guid): [ability_guid, ...]} for the priority
    player's cards whose MANUAL abilities are activatable — mirrors PvE
    _affordable_troop_abilities (collection/phase gating, cost, uses limits,
    exhaust-as-cost, legal targets, ability condition)."""
    import json as _js
    from rules_port.conditions import ConditionContext, trigger_condition_met
    from rules_port.triggers import trigger_collection_allows
    from rules_port.targeting import legal_targets_for
    from rules_port.lifecycle import COMBAT_STEPS
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
    rows = db_ability_option_cards(
        session.session_id, ability_pid, conn=_db)
    result = {}
    for card_uid, tpl_guid, card_state, attrs, card_type, card_location in rows:
        card_ability_payload = db_card_ability_payload(
            session.session_id, int(card_uid), conn=_db)
        ab_list = []
        if card_ability_payload:
            try:
                ab_list = _js.loads(card_ability_payload)
            except Exception:
                ab_list = []
        # An explicit empty instance list is meaningful: a ONE-SHOT ability
        # has been consumed and must not be restored from the canonical card
        # template.
        if card_ability_payload is None:
            template_ability_payload = db_template_ability_payload(
                tpl_guid, conn=_db)
            if template_ability_payload:
                try:
                    ab_list = _js.loads(template_ability_payload)
                except Exception:
                    ab_list = []
        if not ab_list:
            continue
        uses = db_card_uses(session.session_id, int(card_uid))
        affordable = []
        for ag in ab_list:
            ag = str(ag)
            graph = ability_graph(_RECORD_STORE, ag.lower())
            if graph is None:
                continue
            casting = 64 if graph.casting_behavior == "QuickAction" else 8
            if not graph.manual:
                continue
            if not trigger_collection_allows(
                    getattr(graph, "trigger_collection_flags", ""),
                    card_location):
                continue
            cost = graph.costs.activation
            upg = graph.costs.uses_per_game
            upt = graph.costs.uses_per_turn
            exh = graph.costs.exhausts_card_on_use
            cond_ctx = ConditionContext(
                _db, session, state, ability_source_uid=int(card_uid),
                ability_source_owner_id=ability_pid)
            if not trigger_condition_met(graph.source.to_dict(), cond_ctx):
                continue
            target_refs = graph.targets
            if target_refs:
                wants_attacking = False
                has_target = False
                from rules_port import lifecycle as _be
                for target in target_refs:
                    target_filter = target.card_filter
                    if hasattr(target_filter, "to_dict"):
                        target_filter = target_filter.to_dict()
                    if "IsAttacking" in json.dumps(target_filter or {}):
                        wants_attacking = True
                    if target.is_auto or target.target_kind in (
                            "PlayerTargetTemplate",
                            "AbilitySourceCardTargetTemplate",
                            "AbilityCreatedTargetTemplate"):
                        has_target = True
                        continue
                    cands = legal_targets_for(
                        _db, session.session_id, ability_pid, target,
                        int(card_uid), champions=champ_targets,
                        battle_state=state)
                    if cands:
                        has_target = True
                if wants_attacking and phase not in COMBAT_STEPS:
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


def _pvp_ability_cost_targets(session, state, pid, source_uid,
                              ability_guid, champ_targets):
    """Return legal cost cards, or None when a required cost is unpayable."""
    from rules_port.costs import ability_cost_targets, cost_type_for_kind
    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        return None
    costs = ability_cost_targets(
        graph, _db, session.session_id, pid, int(source_uid),
        champions=champ_targets, battle_state=state)
    if not costs:
        return []
    out = []
    for cost in costs:
        tid = cost.guid
        cost_type = cost_type_for_kind(cost.kind)
        if not cost.guid:
            return None
        minimum = int(cost.minimum)
        maximum = int(cost.maximum)
        # Gamedata represents "sacrifice this" as an automatic source-card
        # target.  It is a payment target for the option contract, but the
        # client does not repeat the source UID in the submitted TargetMap.
        # Advertise the source as the sole legal candidate and let activation
        # satisfy this automatic payment without requiring it in the wire
        # transaction.
        if cost.is_source_auto_target:
            out.append((tid, cost_type, [int(source_uid)], minimum, maximum))
            continue
        candidates = list(cost.candidates)
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
    from rules_port.costs import ability_cost_targets, cost_type_for_kind
    from rules_port.targeting import legal_targets_for
    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        return None
    selected_uids = [int(uid) for uid in (selected_uids or [])]
    cost_targets = _pvp_ability_cost_targets(
        session, state, pid, source_uid, ability_guid, champ_targets)
    if cost_targets is None:
        return None
    used = set()
    sacrifices = []
    native_costs = ability_cost_targets(
        graph, _db, session.session_id, pid, int(source_uid),
        champions=champ_targets, battle_state=state)
    for cost in native_costs:
        tid, cost_type = cost.guid, cost_type_for_kind(cost.kind)
        candidates, minimum, maximum = next(
            ((values, low, high) for cost_id, _wire, values, low, high
             in cost_targets if str(cost_id).lower() == tid.lower()),
            (None, cost.minimum, cost.maximum))
        if candidates is None:
            return None
        available = ([int(source_uid)] if cost.is_source_auto_target else
                     [uid for uid in selected_uids
                      if uid in {int(c) for c in candidates}
                      and uid not in used])
        if len(available) < int(minimum):
            return None
        limit = len(available) if int(maximum) < 0 else int(maximum)
        chosen = available[:limit]
        used.update(chosen)
        if int(cost_type) == 2:
            sacrifices.extend(chosen)

    legal_effects = set()
    explicit_required = False
    for target in graph.targets:
        if not target.requires_input:
            continue
        explicit_required = explicit_required or target.minimum > 0
        legal_effects.update(legal_targets_for(
            _db, session.session_id, pid, target, int(source_uid),
            champions=champ_targets, battle_state=state))
    effect_selected = [uid for uid in selected_uids
                       if uid not in used and uid in legal_effects]
    if explicit_required and not effect_selected:
        return None
    return (effect_selected[-1] if effect_selected else None, sacrifices)


def _pvp_discard_prompt_data(ability_guid):
    """Return the child/target pair for a controller discard prompt.

    The prompt is derived from the current effect graph.  Missing effect data
    is invalid Records data and must not be inferred from localized text.
    """
    from rules_port.metadata import ability_effect_prompt, ability_cost_prompt
    prompt = ability_effect_prompt(
        ability_guid, "DiscardCardAbilityEffectTemplate")
    if prompt and prompt[1]:
        return prompt
    prompt = ability_cost_prompt(ability_guid, "discard")
    return prompt if prompt and prompt[1] else None


def _pvp_add_troop_ability_options(g, session, state, pl_t, opp_t, pid,
                                   affordable):
    """Append card ability options (ECardUsage.Activate) to the most
    recent PlayerOptionList, one OptionInstance per affordable ability with
    target instances per target template — mirrors PvE _add_troop_ability_options."""
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
    from rules_port.targeting import legal_targets_for
    champ_map = state.get("champ_map") or {}
    champ_targets = []
    for cpid in (state.get("pids") or []):
        ccu = int(champ_map.get(str(cpid), 0))
        if ccu:
            champ_targets.append((ccu, cpid, "Champ",
                                  int(state.get(f"hp_{cpid}", 20))))
    for (card_uid, tpl_guid), abilities in affordable.items():
        scid = _ge.SessionCardId(_ge.UID(int(card_uid)))
        # A hand card may be both normally playable and manually activatable
        # (Tunnel).  Merge the bit into the existing card option because the
        # client stores one ECardUsage value per card and later duplicate
        # entries overwrite the earlier Play state.
        opt = g.get_or_add_card_option(
            last_ev, scid, _ge.ECardUsage.Activate)
        for ag in abilities:
            inst = g._make_event(_ge.OptionInstanceSessionEventArgs)
            inst.opt_id = _ge.ResourceId.from_str(ag)
            graph = ability_graph(_RECORD_STORE, str(ag).lower())
            target_refs = (tuple(target for target in graph.targets
                                 if target.requires_input)
                           if graph is not None else ())
            if target_refs:
                built = []
                for target in target_refs:
                    i, tid = target.index, target.guid
                    if target.is_auto or target.target_kind in (
                            "PlayerTargetTemplate",
                            "AbilitySourceCardTargetTemplate",
                            "AbilityCreatedTargetTemplate"):
                        continue
                    built.append(i)
                    others = legal_targets_for(
                        _db, session.session_id, pid, target,
                        int(card_uid), champions=champ_targets,
                        battle_state=state)
                    if not others:
                        filt = target.filter
                        if hasattr(filt, "to_dict"):
                            filt = filt.to_dict()
                        if not filt:
                            others = [r[0] for r in db_card_uids_in_zone(
                                session.session_id, pid, "warzone", conn=_db)]
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
                        db_game_get_hand(session.session_id, int(pid))]
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
            db_game_get_hand(session.session_id, int(my_pid))]
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
        row = db_hand_card_for_discard(
            session.session_id, card_uid, my_pid, conn=_db)
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
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    state = pvp_load_state(session) or {}
    # Reuse the option calculation as the authoritative legality check.  This
    # covers phase restrictions, attacking-only targets, exhaustion, and use
    # limits when a client submits a stale or hand-crafted activation.
    source_row = db_card_basic(
        session.session_id, source_uid, conn=_db)
    source_key = (int(source_uid), source_row[0] if source_row else "")
    affordable = _pvp_affordable_troop_abilities(
        session, state, pid=my_pid)
    if ability_guid not in affordable.get(source_key, []):
        log_req(f"    PvP troop ability {ability_guid[:8]}: not legal in "
                f"phase {state.get('phase')} — rejected")
        return True
    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        log_req(f"    PvP troop ability {ability_guid[:8]}: missing current "
                "Records ability — rejected")
        return True
    cost = graph.costs.activation
    upg = graph.costs.uses_per_game
    upt = graph.costs.uses_per_turn
    exh = graph.costs.exhausts_card_on_use
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
        crow = db_card_activation_info(
            session.session_id, source_uid, conn=_db)
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
    from rules_port.costs import ability_cost_targets
    native_costs = ability_cost_targets(
        graph, _db, session.session_id, my_pid, int(source_uid),
        champions=champ_targets, battle_state=state)
    for _tid, _cost_type, candidates, minimum, maximum in cost_targets:
        candidate_set = set(candidates)
        cost_ref = next((cost for cost in native_costs
                         if cost.guid.lower() == str(_tid).lower()), None)
        auto_source = bool(cost_ref and cost_ref.is_source_auto_target)
        available = ([int(source_uid)] if auto_source else
                     [uid for uid in selected_uids
                      if int(uid) in candidate_set
                      and int(uid) not in cost_target_uids])
        if len(available) < minimum:
            log_req(f"    PvP troop ability {ability_guid[:8]}: selected "
                    "payment is incomplete — rejected")
            return True
        selected = (available if maximum < 0 else available[:maximum])
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
    target_refs = graph.targets if graph is not None else ()
    legal_effect_targets = set()
    for target in target_refs:
        if target.is_auto or target.target_kind in (
                "PlayerTargetTemplate", "AbilitySourceCardTargetTemplate",
                "AbilityCreatedTargetTemplate"):
            continue
        from rules_port.targeting import legal_targets_for
        legal_effect_targets.update(legal_targets_for(
            _db, session.session_id, my_pid, target, int(source_uid),
            both_players=True, champions=champ_targets, battle_state=state))
    for uid in selected_uids:
        if int(uid) not in cost_target_uids and int(uid) in legal_effect_targets:
            target_uid = int(uid)
            break
    if target_uid is None and selected_uids and target_refs:
        log_req(f"    PvP troop ability {ability_guid[:8]}: no legal "
                "effect target selected — rejected")
        return True
    discard_prompt_data = _pvp_discard_prompt_data(ability_guid)
    # Resource payment is committed only after all card targets have passed
    # validation, so a stale client transaction cannot spend resources while
    # silently doing nothing.
    from rules_port.resources import pay_resource_for_player
    pay_resource_for_player(state, my_pid, cost)
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
        prow = db_owned_warzone_card(
            session.session_id, pay_uid, my_pid, conn=_db)
        if not prow:
            continue
        db_set_card_state_or(session.session_id, pay_uid,
                             _ge.ECardStates.Tapped)
        pay_scid = _ge.SessionCardId(_ge.UID(pay_uid))
        _tpl_pay, ct_pay, _name_pay, cost_pay, atk_pay, def_pay, gem_pay = \
            handler._card_full_data(g, pay_scid, prow[0])
        pay_state = db_card_state_value(
            session.session_id, pay_uid, conn=_db)
        g.push_card_updated(
            pay_scid, my_uid, _ge.ECardCollections.Warzone, ct_pay,
            template_id=prow[0], state=int(pay_state) if pay_state else
            _ge.ECardStates.Tapped, cost=cost_pay, attack=atk_pay,
            defense=def_pay, gems=gem_pay)
        log_req(f"    PvP troop ability {ability_guid[:8]}: exhausted "
                f"cost target {hex(pay_uid)}")
    # m_PutIntoDeckTarget is an ability-level zone operation represented by a
    # target CostInstance in the client protocol.  It is not a payment and
    # therefore must move the selected cards rather than exhaust them.
    for move_uid in sorted(set(deck_target_uids)):
        move_row = db_card_zone_details(
            session.session_id, move_uid, conn=_db)
        if not move_row or move_row[3] != "warzone":
            continue
        owner_pid = int(move_row[2] or my_pid)
        db_set_card_location(
            session.session_id, int(move_uid), "deck",
            extra_set="position=?, card_state=?", extra_params=[0, 0])
        from pvp_db import db_randomly_insert_deck_cards
        db_randomly_insert_deck_cards(
            session.session_id, owner_pid, [int(move_uid)], connection=_db)
        pos = int(db_card_position(
            session.session_id, move_uid, conn=_db) or 0)
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

    # The client treats ordinary manual abilities as chain items unless the
    # authored template explicitly sets IgnoresChain.  Tunnel is one of these
    # abilities: the source remains in the warzone until both players pass,
    # then the BOM moves it to Underground during chain resolution.  Keep the
    # older direct path for interactive child prompts until their continuation
    # protocol is available in the generic chain resolver.
    if not graph.ignores_chain and not discard_prompt_data:
        from rules_port import lifecycle as _be

        inst_id = port_queue_stack_item(state, {
            "kind": "ability",
            "ability_guid": ability_guid,
            "source_uid": int(source_uid),
            "target_uid": target_uid,
        })
        state["stack_passed"] = []
        view["stack"] = state["stack"]
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
        source_scid = _ge.SessionCardId(_ge.UID(int(source_uid)))
        g.push_ability_on_chain(
            source_scid, _ge.ResourceId.from_str(ability_guid),
            ability_instance_id=inst_id,
            target_card_ids=[source_scid], ignores_chain=False)
        champ_map = state.get("champ_map") or {}
        for target_pid in pids:
            t_uid = _ge.UID.make(244, target_pid)
            cu = int(champ_map.get(str(target_pid), 0))
            g.push_player_updated(
                t_uid, champ_id=_ge.SessionCardId(_ge.UID(cu)) if cu else None)
        _pvp_send_same_events(session, g, my_uid, opp_uid)

        # Activated abilities that use the chain follow the same response
        # order as a normal card play: the non-activating player gets the
        # first ResolveTopOfChain/QuickAction window.  Giving the caster the
        # first green light made Tunnel look like it had to be resolved by
        # its controller before the opponent could respond.
        state["priority_pid"] = opp_pid
        pvp_save_state(session, state)
        opp_h = player_handlers.get(opp_pid)
        if opp_h and not pvp_player_auto_passes(state, opp_pid):
            gg = _ge.Game(int(session.session_id), opp_uid, my_uid)
            gg.push_green_light(opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(opp_h, session, gg, opp_uid,
                             "troop-ability-chain-opp-first")
            try:
                pvp_push_phase_options(session, state, pid=opp_pid)
            except Exception as _e:
                log_req(f"    PvP troop ability chain options error: {_e}")
        caster_h = player_handlers.get(my_pid)
        if caster_h:
            caster_game = _ge.Game(int(session.session_id), my_uid, opp_uid)
            caster_game.push_green_light(
                opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(caster_h, session, caster_game, my_uid,
                             "troop-ability-chain-caster-lost")
        _pvp_auto_pass_chain_priority(session, state, opp_pid)
        log_req(f"    PvP troop ability {ability_guid[:8]} pushed on chain "
                f"from {hex(int(source_uid))} (cost {cost}, "
                f"instance={inst_id})")
        return True
    try:
        from rules_port.resolution import resolve_port_ability
        target_map = {}
        if target_uid is not None:
            for index, spec in enumerate(graph.targets):
                if spec.requires_input:
                    target_map[index] = int(target_uid)
                    break
        resolve_port_ability(
            handler, g, session, _db, my_uid, opp_uid, view,
            ability_guid, int(source_uid), my_pid, target_map=target_map)
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
        db_set_card_state_or(
            session.session_id, int(source_uid), _ge.ECardStates.Tapped)
        scid_src = _ge.SessionCardId(_ge.UID(int(source_uid)))
        trow = db_card_basic(
            session.session_id, source_uid, conn=_db)
        if trow:
            _tpl_src, ct_src, _n_src, cost_src, atk_src, def_src, gem_src = \
                handler._card_full_data(g, scid_src, trow[0])
            crow = db_card_state_value(
                session.session_id, source_uid, conn=_db)
            g.push_card_updated(scid_src, my_uid, _ge.ECardCollections.Warzone,
                                ct_src, template_id=trow[0],
                                state=int(crow) if crow
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
        hand_exists = db_hand_exists(
            session.session_id, my_pid, conn=_db)
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
    if (not _pvp_chain_active(session, state) and
            state.get("phase") in (_ge.ETurnPhases.FirstMainPhase,
                                    _ge.ETurnPhases.SecondMainPhase)):
        pvp_push_main_phase_options(session, state)
    log_req(f"    PvP troop ability {ability_guid[:8]} activated on "
            f"{hex(int(source_uid))} (cost {cost}, target="
            f"{hex(target_uid) if target_uid else 'none'})")
    return True


def push_pvp_game_start(handler, session, log_req=log_req):
    """Push initial battle events for a PvP tournament session (tourney-N)."""
    player_pid = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0
    # The authenticated handler identity is authoritative.  A Ready request
    # can carry the other participant's packed UID, so using that captured
    # request value swaps the local player/opponent wire identities.
    wire_pid = player_pid
    player_uid = player_pid
    sess_id = session.session_id.uid64 if hasattr(session.session_id, 'uid64') else int(session.session_id)

    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        log_req("    PvP: need 2 players in session")
        return

    # Requesting player is pl_t (their perspective); the other is opp_t.
    log_req(f"    push_pvp: player_uid={player_uid} pids={pids}")
    wire_opp_pid = pids[1] if pids[0] == player_pid else pids[0]
    if wire_opp_pid == wire_pid:
        wire_opp_pid = player_pid
    if pids[0] == player_pid:
        pl_t = _ge.UID.make(244, wire_pid)
        opp_t = _ge.UID.make(244, wire_opp_pid)
    else:
        pl_t = _ge.UID.make(244, wire_pid)
        opp_t = _ge.UID.make(244, wire_opp_pid)

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
            if pid == player_pid:
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
    mode_data = getattr(session, "encounter_data", {}) or {}
    if mode_data.get("tournament_mode") == "corinth_merry_melee":
        state["corinth_mode"] = True
        state["skip_draw_phase"] = True
        state["starting_hand_size"] = int(
            mode_data.get("starting_hand_size", 4) or 4)
    from rules_port.resources import begin_turn_resources_for_player
    for _pid in pids:
        begin_turn_resources_for_player(state, _pid)
    pvp_save_state(session, state)
    goes_first_wire_pid = wire_pid if goes_first_pid == player_pid else wire_opp_pid
    goes_first_uid = (goes_first_wire_pid << 8) | 244
    log_req(f"    Coin flip: {hex(goes_first_uid)} goes first")

    # 1. GameStarted — local player's champion always at index 0 (left side).
    champ_guids = [None, None]
    champ_names = ["Player 1", "Player 2"]
    for pid in pids:
        idx = 0 if pid == player_pid else 1
        cr = db_game_champion(session.session_id, pid)
        if cr:
            champ_guids[idx] = cr[1]
        signup_name = db_tournament_player_name_for_session(session.session_id, pid)
        if signup_name:
            champ_names[idx] = signup_name
    if champ_guids[0] is None: champ_guids[0] = "00000000-0000-0000-0000-000000000000"
    if champ_guids[1] is None: champ_guids[1] = "00000000-0000-0000-0000-000000000000"

    # --- Packet 1: GameStarted + champions + PlayerUpdated ----------------
    # Event order matches the PvE game-init sequence (hconnect_server.py ~3994).
    game1 = _ge.Game(sess_id, pl_t, opp_t)
    game1.player_champion_card_id = pchamp
    game1.ai_champion_card_id = achamp
    _pvp_populate_game_state(
        game1, state or {}, player_pid,
        pids[1] if player_pid == pids[0] else pids[0])

    # 1. GameStarted — registers turn order, champion names / template IDs.
    game1.push_game_started(champion_names=champ_names,
                            champion_template_ids=champ_guids,
                            player_first=(goes_first_pid == player_pid))
    # Coin flip resolution (class 60): lets the client complete the coin-flip
    # state (m_CoinFlipSkip -> m_CoinFlipDone) so it can process the phases
    # that follow.  Without it neither client gets past the toss.
    game1.push_first_player_dictated(
        _ge.UID.make(244, goes_first_wire_pid))
    log_req(f"    PvP start: pushed GameStarted + FirstPlayerDictated to pid "
            f"{player_uid} (winner {goes_first_pid})")

    # 2. PlayerUpdated — must come before card events so State.Players exists.
    game1.push_player_updated(pl_t, champ_id=pchamp)
    game1.push_player_updated(opp_t, champ_id=achamp)

    # 3. CardUpdated for champions — ECardCollections.None_ (matches PvE).
    for pid in pids:
        is_pl = (pid == player_pid)
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
        is_pl = (pid == player_pid)
        pt = pl_t if is_pl else opp_t
        ch_id = pchamp if is_pl else achamp
        pn = champ_names[0] if is_pl else champ_names[1]
        game1.push_champion_card_played(pt, False, pn, ch_id)

    # Persist champion SCIDs so _pvp_run_phase_start passes valid IDs.
    state = pvp_load_state(session)
    if state:
        champ_map = state.get("champ_map", {})
        for pid in pids:
            is_pl = (pid == player_pid)
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
        is_me = (pid == player_pid)
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
        is_me = (pid == player_pid)
        player_t = pl_t if is_me else opp_t
        game2.push_deck_created(player_t)

    # The stock client emits PreGameEvent after both decks have been created,
    # before PickGoesFirst. Run the same metadata trigger dispatcher for both
    # deck owners and persist the resulting state, guarded so reconnects do
    # not apply deck abilities twice.
    if not state.get("pvp_pregame_done"):
        for owner_pid in pids:
            owner_handler = player_handlers.get(int(owner_pid)) or handler
            owner_handler._current_bstate = state
            _pvp_dispatch_triggers(
                owner_handler, game2, session, state, pl_t, opp_t,
                "PreGameEvent", None, int(owner_pid), zones=("deck",))
        state["pvp_pregame_done"] = True
        pvp_save_state(session, state)

    # PickGoesFirst with correct turn player.  GreenLight must precede the
    # phase in the same packet for the winner, otherwise UIBattle sees local
    # priority before HasPriority is set and immediately requests a resync.
    turn_uid = _ge.UID.make(244, goes_first_pid)
    if player_pid == goes_first_pid:
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
    # All live tournament sessions are RulesPort-owned by default. Internal
    # callers (auto-pass, reconnect repair, and legacy service helpers) may
    # still arrive here without going through the transaction callback. Route
    # those passes through the native action stack as well, instead of
    # allowing a second state["stack"] priority implementation to run.
    native_port = getattr(session, "_rules_port_session", None)
    if native_port is not None:
        from rules_port.kernel import PriorityWindowAction
        live_native = pvp_load_state(session) or {}
        if (native_port.action_stack.peek() is None and
                live_native.get("stack") and
                getattr(native_port, "rehydrate_projected_chain", None)):
            native_port.rehydrate_projected_chain()
        action = native_port.action_stack.peek()
        if not isinstance(action, PriorityWindowAction):
            log_req("    PvP RulesPort rejected internal pass: native priority action missing")
            return False
        player_id = _ge.UID.make(244, int(
            handler.client_reck_id if hasattr(handler, "client_reck_id") else 0))
        if not native_port.pass_priority_and_drive(player_id):
            return False
        next_uid = native_port.action_stack.priority_player_id
        # A native PvP priority window still needs the mode's stop policy.
        # The old projection auto-completed the other player's pass during a
        # main phase when they had neither an explicit opponent stop nor a
        # legal quick action.  The RulesPort handoff above establishes the
        # same priority window, but without this check an empty Second Main
        # phase waits forever for a second client pass.
        if next_uid is not None:
            live_after_pass = pvp_load_state(session) or live_native
            phase = int(live_after_pass.get("phase", 0) or 0)
            raw_waiting = int(getattr(next_uid, "uid64", next_uid))
            waiting_pid = (raw_waiting >> 8
                           if (raw_waiting & 0xFF) == 244 else raw_waiting)
            if phase in (_ge.ETurnPhases.FirstMainPhase,
                         _ge.ETurnPhases.SecondMainPhase):
                try:
                    has_quick_action = bool(_pvp_affordable_troop_abilities(
                        session, live_after_pass, pid=waiting_pid))
                except Exception as exc:
                    has_quick_action = False
                    log_req(f"    PvP native auto-pass quick-action check "
                            f"failed for {waiting_pid}: {exc}")
                if native_port.auto_pass_waiting_player(
                        live_after_pass, next_uid,
                        has_quick_action=has_quick_action):
                    next_uid = native_port.action_stack.priority_player_id
                    log_req(
                        f"    PvP RulesPort auto-completed priority for "
                        f"{waiting_pid} on phase {phase} "
                        f"(no opponent stop or quick action)")
        native_port.sync_to_pvp_state(live_native)
        pvp_save_state(session, live_native)
        return True
    my_pid = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False

    state = pvp_load_state(session)
    if state is None:
        state = pvp_default_state(my_pid, my_pid)
        port_enter_phase(state, 10)
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
        port_set_priority(state, caster_pid)
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
        stack_pass = port_stack_pass_transition(
            state.get("stack_passed"), my_pid, pids)
        if stack_pass["action"] == "duplicate":
            # Already passed — ignore the duplicate.
            return True
        sp = set(stack_pass["passed"])
        other_pid = stack_pass["other_player"]
        if stack_pass["action"] == "handoff":
            # Only one player has passed: hand priority to the other so they
            # can cast a response (quick action) or pass to resolve.
            state["stack_passed"] = sorted(sp)
            port_set_priority(state, other_pid)
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
    passes = port_record_phase_pass(state, my_pid)
    pvp_save_state(session, state)

    if len(passes) < 2:
        waiting_pid = pids[0] if pids[1] in passes else pids[1]
        # If the phase is an OPPONENT-STOP for the waiting player, hand them
        # priority so they can actually act (the client only shows the pass
        # button while holding priority).  Otherwise they have nothing to
        # respond to (no instants in PvP yet) — auto-complete their pass so
        # the turn player's single pass advances the phase ("Continue to
        # Second Main Phase" just works instead of stalling at 1/2).
        # Only stop the waiting player for MANDATORY opponent phases
        # (OPP_ALWAYS_STOPS — DeclareDefense, where they must decide blocks)
        # or phases they EXPLICITLY configured as opponent-stops.  The client's
        # DEFAULT opponent stops (SecondMain, DeclareAttackPriorityWindow,
        # DeclareDefensePriorityWindow) would otherwise force a manual pass
        # from the opponent every turn even though PvP has no instants to
        # respond with — stalling at the turn player's pass.
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
        if port_waiting_player_requires_priority(
                state, int(state["phase"]), waiting_pid,
                has_quick_action=quick_action_wait):
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
                port_set_priority(state, waiting_pid)
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
        passes = port_record_phase_pass(state, waiting_pid)
        pvp_save_state(session, state)
        log_req(f"    PvP pass: auto-completed opponent {waiting_pid}'s pass "
                f"(no opponent stop on phase {state['phase']})")

    # ── both players have passed ──────────────────────────────────────
    old_phase = state["phase"]
    from rules_port import lifecycle as _be
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
    phase_list = _pvp_turn_phase_list(state, turn_pid, has_ready)
    # The client chooses the next combat phase only after Declare Blockers has
    # completed and the DeclareDefensePriorityWindow response window has
    # closed.  Evaluate the live combat here so a Quick Action that grants
    # Swiftstrike to an attacker or blocker is included.
    if old_phase == _ge.ETurnPhases.DeclareDefensePriorityWindow:
        new_phase = pvp_phase_after_blockers(session, state)
        transition = port_phase_transition(
            phase_list, old_phase, after_blockers=new_phase)
    else:
        transition = port_phase_transition(phase_list, old_phase)
    cur_idx = transition["current_index"]
    next_idx = transition["next_index"]
    new_phase = transition["new_phase"]
    if transition["wrapped"]:
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
                if not state.get("turn_end_trigger_fired"):
                    _pvp_dispatch_triggers(
                        end_h, eg, session,
                        _pvp_fra_view(state, turn_pid, end_opp),
                        end_uid, end_opp_uid, "TurnEndedEvent", None,
                        turn_pid)
                    if state.get("stack"):
                        # Hold the current EndTurn until its triggered ability
                        # resolves.  Otherwise the next turn begins with the
                        # trigger still on the stack and a token such as Blaze
                        # Elemental is sacrificed during the next First Main.
                        state["turn_end_trigger_fired"] = True
                # Combat damage and "until end of turn" attributes expire at
                # cleanup, not at the next turn's Prep.  Clear every warzone
                # card because combat can damage either player's troops.
                from rules_port.lifecycle import (
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
                    port_reset_priority_interval(state, turn_pid)
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
        turn_boundary = port_advance_turn_state(state, pids_)
        if turn_boundary["bonus_used"]:
            log_req(f"    PvP: bonus turn for {turn_boundary['turn_pid']}")
        next_idx = 0
        new_phase = phase_list[0]
    elif old_phase != _ge.ETurnPhases.DeclareDefensePriorityWindow:
        new_phase = transition["new_phase"]
    port_enter_phase(state, new_phase)
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
        row = db_hand_card_for_discard(
            session.session_id, card_uid, my_pid, conn=_db)
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
        _pvp_dispatch_triggers(
            h_card, g, session, view, _ge.UID.make(244, owner_pid),
            _ge.UID.make(244, opp_pid), "CardEnteredZoneEvent", card_uid,
            owner_pid)
    except Exception as exc:
        log_req(f"    PvP discard trigger error: {exc}")
    _pvp_sync_view_to_state(state, view, owner_pid, opp_pid)
    state["stack"] = view.get("stack") or state.get("stack") or []
    state["stack_passed"] = []
    state["priority_pid"] = my_pid
    pvp_save_state(session, state)
    _pvp_send_same_events(session, g, my_uid, opp_uid)

    hand_count = db_hand_count(session.session_id, my_pid, conn=_db)
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
    elif hand_count > DEFAULT_MAX_HAND_SIZE:
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
        top = db_deck_top_card(session.session_id, owner_pid, conn=_db)
        if not top:
            break
        card_uid, tpl_guid, instance_id = top
        g = _ge.Game(int(session.session_id), owner_uid, opponent_uid)
        handler._player_draw_card(g, session, owner_uid, owner_pid)
        loc = db_card_location(session.session_id, card_uid)
        if loc == 'hand':
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
def _pvp_project_resource_play(handler, session, inner_bytes, my_pid,
                               played_card_uid, crow, card_name, pids,
                               my_uid, opp_uid):
    """Project an already RulesPort-validated PvP resource play."""
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    db_set_card_played_to_zone(
        session.session_id, int(played_card_uid), "PlayedResources")
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
    resource_choice_ability = None
    ability_guids = []
    if crow[5]:
        try:
            ability_guids = json.loads(crow[5])
        except Exception:
            ability_guids = []
        shard_ability, shard_tpl = handler._shards_of_fate_template(
            ability_guids)
        if not shard_tpl:
            from rules_port.resources import printed_resource_choice_ability
            resource_choice_ability = printed_resource_choice_ability(
                ability_guids)
    is_shards_of_fate = bool(shard_tpl)
    log_req(f"    PvP resource metadata: {card_name} "
            f"abilities={[str(g)[:8] for g in ability_guids]} "
            f"choice={str(resource_choice_ability or '')[:8] or 'none'} "
            f"shards_of_fate={is_shards_of_fate}")

    # Track resources, threshold, and champion charge in PvP state.  Shards of
    # Fate is excluded only from the ordinary-shard threshold path; its
    # selected deck card supplies the threshold after the prompt resolves.
    state = pvp_load_state(session) or {}
    # Resource charge generation is defined by the card's BOM.  Do not add a
    # universal +1 here: Set 1 shards already contain a gain-one-charge leaf.
    charge_grant = _pvp_resource_charge_points(session, played_card_uid)
    # A normal resource can fire GainChargeEvent immediately.  Shards of Fate
    # has a nested deck choice below, so defer its trigger until that choice
    # has completed and the picker is no longer active.
    charge_trigger_game = None
    if charge_grant and not (is_shards_of_fate or resource_choice_ability):
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
    from rules_port.resources import play_resource_for_player
    play_resource_for_player(
        state, my_pid, current_grant, max_grant,
        threshold_color=(shard_color if shard_color and not (
            is_shards_of_fate or resource_choice_ability) else None),
        charge_amount=charge_grant)
    pvp_save_state(session, state)
    # Read the post-payment threshold value for the client event.  This must
    # be reconstructed after play_resource_for_player mutates the per-player
    # state; the old path referenced a variable that was never initialized.
    thresh = _pvp_state_thresholds(state, my_pid)
    threshold_trigger_game = None
    if shard_color and not (is_shards_of_fate or resource_choice_ability):
        threshold_trigger_game = _pvp_gain_threshold_trigger_game(
            handler, session, state, my_pid, shard_color)
    resource_ability_events = _pvp_resolve_granted_resource_abilities(
        handler, session, state, int(played_card_uid), my_pid)
    # The resource is now played — refresh the turn player's options so the
    # second shard no longer highlights.
    if (not is_shards_of_fate and not resource_choice_ability and
            not state.get("stack") and
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
        if shard_color and not (is_shards_of_fate or resource_choice_ability):
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
        if threshold_trigger_game:
            for trigger_event in threshold_trigger_game.events:
                g._push(trigger_event)
        for resource_event in resource_ability_events:
            g._push(resource_event)
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

    # Some clients receive a later phase/options packet after the resource
    # packet.  That packet can contain PlayerUpdated events built from a bare
    # Game and overwrite the just-applied charge/resource values in the HUD.
    # Send one final authoritative player-state snapshot after all resource
    # events so the last PlayerUpdated values are the durable PvP state.
    champ_map = state.get("champ_map", {})
    for pid in pids:
        refresh_handler = player_handlers.get(pid)
        if not refresh_handler:
            continue
        opponent_pid = pids[1] if pid == pids[0] else pids[0]
        player_uid = _ge.UID.make(244, int(pid))
        opponent_uid = _ge.UID.make(244, int(opponent_pid))
        refresh_game = _ge.Game(
            int(session.session_id), player_uid, opponent_uid)
        _pvp_populate_game_state(
            refresh_game, state, int(pid), int(opponent_pid))
        refresh_game.push_player_updated(
            player_uid,
            champ_id=_ge.SessionCardId(
                _ge.UID(int(champ_map.get(str(pid), 0)))))
        refresh_game.push_player_updated(
            opponent_uid,
            champ_id=_ge.SessionCardId(
                _ge.UID(int(champ_map.get(str(opponent_pid), 0)))))
        _send_pvp_packet(
            refresh_handler, session, refresh_game, player_uid,
            "resource-state-refresh")
    log_req("    PvP resource: final player-state refresh pushed")
    if (charge_trigger_game and charge_trigger_game.events
            and state.get("stack")):
        # The resource packet above contains the charge event and the
        # triggered ability entry.  Give the opponent the first response
        # window, matching permanent/spell plays already on the PvP chain.
        _pvp_offer_trigger_response(session, state, my_pid)
        return True
    if resource_choice_ability:
        # Resource events must arrive before the built-in choice picker. The
        # printed ability creates private Choosing-zone cards and the shared
        # prompt helper sends the class-23 activation request to the owner.
        state["priority_pid"] = my_pid
        pvp_save_state(session, state)
        owner_handler = player_handlers.get(my_pid) or handler
        prompt_game = _ge.Game(int(session.session_id), my_uid, opp_uid)
        _pvp_populate_game_state(prompt_game, state, my_pid, opp_pid)
        prompt_view = _pvp_fra_view(state, my_pid, opp_pid)
        _pvp_resolve_ability(
            owner_handler, prompt_game, session, prompt_view, my_uid, opp_uid,
            resource_choice_ability, int(played_card_uid), my_pid,
            target_map={})
        state["stack"] = prompt_view.get("stack") or []
        state["stack_player_passed"] = False
        state["stack_ai_passed"] = False
        _pvp_sync_view_to_state(state, prompt_view, my_pid, opp_pid)
        # The prompt helper persists its private pending state while the
        # resolver is running. Preserve those markers when copying the FRA
        # view back into the authoritative PvP state.
        persisted = pvp_load_state(session) or {}
        for pending_key in ("pending_choice", "resolution_paused"):
            if persisted.get(pending_key):
                state[pending_key] = persisted[pending_key]
        pvp_save_state(session, state)
        if state.get("pending_choice"):
            log_req(f"    PvP resource choice: awaiting picker for pid "
                    f"{my_pid}")
            return True
        if state.pop("pending_gain_charge_pid", None) == my_pid:
            charge_trigger_game = _pvp_gain_charge_trigger_game(
                owner_handler, session, state, my_pid)
            if charge_trigger_game and charge_trigger_game.events:
                _pvp_send_same_events(
                    session, charge_trigger_game, my_uid, opp_uid)
                if state.get("stack"):
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


def pvp_handle_transaction(handler, session, inner_bytes, *, typed_payload=None):
    """Handle a PvP game transaction (card play, ability use, combat).
    Applies the action server-side and pushes events to BOTH players.
    Returns True if handled."""
    if not isinstance(inner_bytes, bytes):
        return False
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    my_pid = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0
    if b"EncounterModDialogTransaction" in inner_bytes:
        return _pvp_resolve_conversation(handler, session, inner_bytes, my_pid)
    # A class-39 answer (deck search, revealed-card choice, or Shards of
    # Fate) is named SetAbilityActivationDataTransaction in the client model,
    # but the serialized transaction contains only AbilityActivationData.
    # Route both spellings before generic ability activation; otherwise the
    # picker response falls through and is misread as a champion activation.
    pending_state = pvp_load_state(session) or {}
    if pending_state.get("pending_choice") and b"m_UID64" in inner_bytes:
        return _pvp_resolve_choice(
            handler, session, inner_bytes, my_pid,
            typed_payload=typed_payload)
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
        if ((state.get("pending_deck_search") or {}).get("kind") ==
                "matching_target"):
            return _pvp_resolve_matching_target(
                handler, session, inner_bytes, my_pid)
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
    # GUID; if it belongs to a player-controlled card in a metadata-allowed
    # collection, activate the card's manual ability (Shift/Tunnel etc.);
    # otherwise it's the champion's charge/spell power.
    if b"m_AbilityActivationData" in inner_bytes:
        ability_guid = extract_ability_guid(inner_bytes)
        if ability_guid:
            champ_owned = db_talent_ability_exists(
                ability_guid, conn=_db)
            src_row = None
            if not champ_owned:
                # Multiple copies share the same ability GUID.  The first
                # card UID in an activation transaction is its source card;
                # do not route every copy to the first matching warzone row.
                card_uids = _pvp_transaction_card_uids(inner_bytes)
                if card_uids:
                    source_matches = db_cards_with_ability(
                        session.session_id, my_pid, ability_guid,
                        card_uid=card_uids[0], conn=_db)
                    src_row = source_matches[0] if source_matches else None
                if src_row is None and not card_uids:
                    # Preserve the unambiguous single-copy case for clients
                    # that omit the source SessionCardId, but never guess
                    # between duplicate ability instances.
                    matches = db_cards_with_ability(
                        session.session_id, my_pid, ability_guid, conn=_db)
                    if len(matches) == 1:
                        src_row = matches[0]
            if src_row:
                return _pvp_activate_troop_ability(
                    handler, session, inner_bytes, my_pid,
                    ability_guid, int(src_row[0]))
        return _pvp_activate_champion_ability(handler, session, inner_bytes,
                                              my_pid)

    # Extract played card UID from the transaction. A RulesPort projection
    # supplies this typed value after validating the request; raw parsing is
    # retained only for the explicit compatibility caller.
    played_card_uid = None
    if isinstance(typed_payload, dict):
        try:
            played_card_uid = int(getattr(
                typed_payload.get("card_id"), "uid64",
                typed_payload.get("card_id")))
        except (TypeError, ValueError):
            played_card_uid = None
    scid_pos = inner_bytes.find(b"m_SessionCardId")
    if played_card_uid is None and scid_pos >= 0:
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
    crow = db_card_play_info(
        session.session_id, played_card_uid, conn=_db)
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
            return _pvp_play_troop(handler, session, played_card_uid, my_pid,
                                   inner_bytes)
        except Exception as e:
            # Never let a card-play crash kill the session thread and
            # disconnect both clients — log and ack so the game keeps going.
            import traceback
            log_req(f"    PvP play exception ({card_name}): {e}")
            _tb = traceback.format_exc()
            for _tl in _tb.splitlines():
                log_req(f"    PvP play TB: {_tl}")
            return True

    return _pvp_project_resource_play(
        handler, session, inner_bytes, my_pid, played_card_uid, crow,
        card_name, pids, my_uid, opp_uid)
def _pvp_resolve_choice(handler, session, inner_bytes, my_pid,
                        typed_payload=None):
    """Resolve a private ChooseAndPlay choice and resume its parent BOM."""
    from rules_port import lifecycle as _be
    from rules_port.choice_effects import (
        extract_card_uids, _play_choice_card, _resolve_choice_card_abilities)
    state = pvp_load_state(session) or {}
    pending = state.get("pending_choice")
    if not pending:
        return False
    selected = extract_card_uids(inner_bytes)
    if not selected and isinstance(typed_payload, dict):
        activation = typed_payload.get("activation_data")
        target_map = (activation.get("target_map")
                      if isinstance(activation, dict) else None)
        if isinstance(target_map, dict):
            for target in target_map.values():
                values = target if isinstance(target, (list, tuple, set)) else (target,)
                for value in values:
                    try:
                        uid = int(value)
                    except (TypeError, ValueError):
                        continue
                    if (uid & 0xFF) == 1:
                        selected.append(uid)
    legal = {int(uid) for uid in pending.get("choice_uids", [])}
    chosen_uid = next((uid for uid in reversed(selected) if int(uid) in legal),
                      None)
    owner_id = int(pending.get("owner_id", 0))
    log_req(f"    PvP choice parse: selected={[hex(int(u)) for u in selected]} "
            f"legal={[hex(int(u)) for u in legal]} chosen="
            f"{hex(int(chosen_uid)) if chosen_uid else None}")
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
    state.pop("pending_choice", None)
    state.pop("resolution_paused", None)
    choice_zone_target = pending.get("kind") == "choice_zone_target"
    choice_zone_copy = pending.get("kind") == "choice_zone_copy"
    if choice_zone_copy:
        # Keep the selected original in Choosing while the child ability
        # copies it to hand; the remaining generated options are discarded
        # only after the copy has resolved.
        state["selected_choice_uid"] = int(chosen_uid)
    view = _pvp_fra_view(state, owner_id, opponent_id)
    view.pop("pending_choice", None)
    view.pop("resolution_paused", None)
    if choice_zone_copy:
        view["choice_copy_to_hand"] = True
    g = _ge.Game(int(session.session_id), pl_t, ai_t)
    _pvp_populate_game_state(g, state, owner_id, opponent_id)
    from rules_port.context import EffectContext
    choice_context = EffectContext.from_rules_port(
        g, session, _db, handler, pl_t, ai_t, view,
        "choice", ability=None)
    if choice_zone_target:
        # The authored child target opened the picker (e.g. Corinth's charge
        # power, "a card in the choice zone").  Resolve that child against the
        # selected token, then resume the enclosing ability so its later
        # effect groups still run.  Mirrors the PvE choice_zone_target
        # continuation; the selected card is only targeted, never "played".
        continuation = pending.get("continuation") or {}
        child_guid = str(continuation.get("ability_guid") or
                         pending.get("ability_guid") or "").lower()
        child_source = int(continuation.get(
            "source_uid", pending.get("source_uid", 0)) or 0)
        child_owner = int(continuation.get("owner_id", owner_id) or owner_id)
        child_targets = {int(key): value for key, value in
                         (continuation.get("target_map") or {}).items()}
        child_targets[int(continuation.get("target_index", 0) or 0)] = \
            int(chosen_uid)
        _pvp_resolve_ability(
            handler, g, session, view, pl_t, ai_t, child_guid,
            child_source, child_owner, target_map=child_targets,
            variables=continuation.get("variables") or {},
            resume_from_order=int(
                continuation.get("resume_effect_order", 0) or 0))
        parent = pending.get("parent") or {}
        parent_guid = str(parent.get("ability_guid") or "").lower()
        if parent_guid and not state.get("pending_choice"):
            _pvp_resolve_ability(
                handler, g, session, view, pl_t, ai_t, parent_guid,
                parent.get("source_uid"),
                int(parent.get("owner_id", owner_id) or owner_id),
                target_map={int(key): value for key, value in
                            (parent.get("target_map") or {}).items()},
                variables=parent.get("variables") or {},
                resume_from_order=int(
                    parent.get("resume_effect_order", 0) or 0))
    else:
        if not choice_zone_copy and not _play_choice_card(
                choice_context, chosen_uid, owner_id):
            log_req(f"    PvP choice card no longer selectable: {chosen_uid}")
            state["pending_choice"] = pending
            state["resolution_paused"] = True
            pvp_save_state(session, state)
            return True
        # The charge-power choices are templates to copy, not cards whose
        # printed abilities should be cast while answering the picker.
        # Resolving those abilities here can require unrelated targets (for
        # example GrantAbility) and abort the parent before its copy-to-hand
        # effect runs.
        if not choice_zone_copy:
            _resolve_choice_card_abilities(
                choice_context, chosen_uid, pending.get("source_uid"),
                owner_id)

        target_map = {int(key): value for key, value in
                      (pending.get("target_map") or {}).items()}
        _pvp_resolve_ability(
            handler, g, session, view, pl_t, ai_t,
            pending["ability_guid"], pending.get("source_uid"), owner_id,
            target_map=target_map, variables=pending.get("variables") or {},
            resume_from_order=int(pending.get("resume_effect_order", 0)))
    if choice_zone_copy:
        view.pop("choice_copy_to_hand", None)
        from rules_port.choice_effects import _clear_choice_zone
        _clear_choice_zone(choice_context)
        state.pop("selected_choice_uid", None)
    state["stack"] = view.get("stack") or []
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False
    _pvp_sync_view_to_state(state, view, owner_id, opponent_id)
    charge_trigger_game = None
    if (not view.get("pending_choice") and
            state.pop("pending_gain_charge_pid", None) == owner_id):
        charge_trigger_game = _pvp_gain_charge_trigger_game(
            handler, session, state, owner_id)
        if charge_trigger_game:
            for trigger_event in charge_trigger_game.events:
                g._push(trigger_event)
    pvp_save_state(session, state)
    _pvp_send_same_events(session, g, pl_t, ai_t)

    if state.get("pending_choice"):
        # The resumed second DoubleChoice prompt was sent privately by the
        # shared prompt helper. Only the selected card's public move was sent
        # above; leave priority in the picker until the next answer.
        log_req(f"    PvP choice selected: {hex(int(chosen_uid))}; "
                "second choice pending")
        return True

    # The chooser answered; clear the opponent's "opponent is choosing" state.
    _pvp_push_waiting_on(session, None)

    if charge_trigger_game and state.get("stack"):
        _pvp_offer_trigger_response(session, state, owner_id)
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


def _pvp_resolve_conversation(handler, session, inner_bytes, my_pid):
    """Resume a metadata BOM after a class-55 encounter conversation."""
    from application.player_transactions import extract_resource_guid
    from rules_port import lifecycle as _be

    state = pvp_load_state(session) or {}
    pending = state.get("pending_conversation")
    if not pending:
        handler._push_transaction_ack(session)
        return True
    conversation_id = extract_resource_guid(inner_bytes, "ConversationId")
    expected = str(pending.get("conversation_id", "")).lower()
    if conversation_id and conversation_id != expected:
        log_req(f"    PvP conversation answer rejected: got {conversation_id}, expected {expected}")
        handler._push_transaction_ack(session)
        return True
    owner_id = int(pending.get("owner_id", 0) or 0)
    if int(my_pid) != owner_id:
        log_req(f"    PvP conversation answer rejected: pid {my_pid} is not owner {owner_id}")
        handler._push_transaction_ack(session)
        return True
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        handler._push_transaction_ack(session)
        return True
    opp_pid = pids[0] if pids[1] == owner_id else pids[1]
    state.pop("pending_conversation", None)
    state.pop("resolution_paused", None)
    pl_t = _ge.UID.make(244, owner_id)
    ai_t = _ge.UID.make(244, opp_pid)
    view = _pvp_fra_view(state, owner_id, opp_pid)
    view.pop("pending_conversation", None)
    view.pop("resolution_paused", None)
    game = _ge.Game(int(session.session_id), pl_t, ai_t)
    _pvp_populate_game_state(game, state, owner_id, opp_pid)
    ability_owner_id = int(pending.get(
        "ability_owner_id", owner_id) or owner_id)
    _pvp_resolve_ability(
        handler, game, session, view, pl_t, ai_t,
        pending.get("ability_guid", ""), pending.get("source_uid"),
        ability_owner_id,
        target_map={int(k): v for k, v in
                    (pending.get("target_map") or {}).items()},
        variables=pending.get("variables") or {},
        resume_from_order=int(pending.get("resume_effect_order", 0)),
    )
    state["stack"] = view.get("stack") or []
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False
    state["stack_passed"] = []
    _pvp_sync_view_to_state(state, view, owner_id, opp_pid)
    persisted = pvp_load_state(session) or {}
    for key in ("pending_trigger", "pending_deck_search", "pending_choice",
                "pending_conversation"):
        if persisted.get(key):
            state[key] = persisted[key]
    pvp_save_state(session, state)
    _pvp_send_same_events(session, game, pl_t, ai_t)

    pending_input = (state.get("pending_conversation") or
                     state.get("pending_choice") or
                     state.get("pending_trigger") or
                     state.get("pending_deck_search"))
    if pending_input:
        log_req("    PvP conversation resumed into another pending input")
        return True

    if _be.stack_empty(state):
        turn_pid = int(state.get("turn_pid") or owner_id)
        state["priority_pid"] = turn_pid
        pvp_save_state(session, state)
        turn_h = player_handlers.get(turn_pid)
        if turn_h:
            turn_uid = _ge.UID.make(244, turn_pid)
            other_uid = _ge.UID.make(244, pids[1] if turn_pid == pids[0] else pids[0])
            resume = _ge.Game(int(session.session_id), turn_uid, other_uid)
            resume.push_chain_empty()
            resume.push_green_light(turn_uid, _ge.EPriorityContext.Normal)
            _send_pvp_packet(turn_h, session, resume, turn_uid,
                             "conversation-chain-empty")
        phase = int(state.get("phase", 0))
        if phase in (_ge.ETurnPhases.FirstMainPhase,
                     _ge.ETurnPhases.SecondMainPhase):
            pvp_push_main_phase_options(session, state)
    else:
        resume_pid = int(state.get("conversation_resume_priority_pid", owner_id) or owner_id)
        next_pid = pids[1] if resume_pid == pids[0] else pids[0]
        state["priority_pid"] = next_pid
        pvp_save_state(session, state)
        next_h = player_handlers.get(next_pid)
        if next_h:
            next_uid = _ge.UID.make(244, next_pid)
            other_uid = _ge.UID.make(244, pids[1] if next_pid == pids[0] else pids[0])
            resume = _ge.Game(int(session.session_id), next_uid, other_uid)
            resume.push_green_light(next_uid,
                                    _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(next_h, session, resume, next_uid,
                             "conversation-chain-next")
    handler._push_transaction_ack(session)
    log_req(f"    PvP conversation resolved: {expected[:8]}")
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
    state.pop("resolution_paused", None)
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
    bstate = state
    g = _ge.Game(int(session.session_id), pl_t, ai_t)
    from rules_port.context import EffectContext
    from rules_port.deck_effects import move_deck_card_to_hand
    bstate["_rules_port_attached"] = True
    move_deck_card_to_hand(EffectContext.from_rules_port(
        g, session, _db, handler, pl_t, ai_t, bstate,
        "move_deck_card_to_hand", ability=None), chosen_uid, owner_id)
    pvp_save_state(session, state)
    _pvp_send_same_events(session, g, pl_t, ai_t)
    log_req(f"    PvP deck-search resolved: {hex(chosen_uid)} -> hand "
            f"(pid {owner_id})")
    return True


def _pvp_resolve_matching_target(handler, session, inner_bytes, my_pid):
    """Resolve a PvP deck target whose typed effect keeps it in the deck.

    This is the PvP counterpart of the FRA continuation: Scheme's selected
    action is never moved to hand, all picker candidates are hidden again,
    and the child BOM is resumed with the selected TargetMap before priority
    is returned to the active player.
    """
    import struct

    state = pvp_load_state(session) or {}
    pend = state.pop("pending_deck_search", None)
    if not pend:
        return False
    state.pop("resolution_paused", None)
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
    candidates = [int(uid) for uid in (pend.get("candidates") or [])]
    owner_id = int(pend.get("owner_id", my_pid) or my_pid)
    pids = db_game_session_pids(session.session_id)
    if (not chosen_uid or chosen_uid not in candidates or
            owner_id not in pids or int(my_pid) != owner_id or len(pids) < 2):
        pvp_save_state(session, state)
        log_req(f"    PvP matching-target invalid choice: "
                f"chosen={chosen_uid} candidates={candidates}")
        handler._push_transaction_ack(session)
        return True

    opp_pid = next(pid for pid in pids if int(pid) != owner_id)
    pl_t = _ge.UID.make(244, owner_id)
    ai_t = _ge.UID.make(244, opp_pid)
    view = _pvp_fra_view(state, owner_id, opp_pid)
    view.pop("pending_deck_search", None)
    view.pop("resolution_paused", None)
    g = _ge.Game(int(session.session_id), pl_t, ai_t)
    _pvp_populate_game_state(g, state, owner_id, opp_pid)
    handler._hide_candidates_to_deck(g, session, pl_t, ai_t, candidates)

    continuation = pend.get("continuation") or {}
    child_guid = str(continuation.get("ability_guid") or "").lower()
    child_source = int(continuation.get("source_uid") or 0)
    child_owner = int(continuation.get("owner_id", owner_id) or owner_id)
    child_targets = {
        int(key): value for key, value in
        (continuation.get("target_map") or {}).items()
    }
    child_targets[int(continuation.get("target_index", 0))] = int(chosen_uid)
    _pvp_resolve_ability(
        handler, g, session, view, pl_t, ai_t,
        child_guid, child_source, child_owner,
        target_map=child_targets,
        variables=continuation.get("variables") or {})

    parent = continuation.get("parent") or {}
    parent_guid = str(parent.get("ability_guid") or "").lower()
    if parent_guid:
        _pvp_resolve_ability(
            handler, g, session, view, pl_t, ai_t,
            parent_guid, child_source,
            int(parent.get("owner_id", child_owner) or child_owner),
            target_map={int(key): value for key, value in
                        (parent.get("target_map") or {}).items()},
            variables=parent.get("variables") or {},
            resume_from_order=int(parent.get("resume_effect_order", 0)))

    state["stack"] = view.get("stack") or []
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False
    _pvp_sync_view_to_state(state, view, owner_id, opp_pid)
    persisted = pvp_load_state(session) or {}
    for key in ("pending_trigger", "pending_deck_search", "pending_choice",
                "pending_conversation"):
        if persisted.get(key):
            state[key] = persisted[key]
    pvp_save_state(session, state)
    _pvp_send_same_events(session, g, pl_t, ai_t)

    if (state.get("pending_trigger") or state.get("pending_deck_search") or
            state.get("pending_choice") or state.get("pending_conversation")):
        handler._push_transaction_ack(session)
        return True

    if state.get("stack"):
        _pvp_offer_trigger_response(session, state, owner_id)
    else:
        state["priority_pid"] = int(state.get("turn_pid") or owner_id)
        state["stack_passed"] = []
        pvp_save_state(session, state)
        priority_pid = int(state["priority_pid"])
        priority_handler = player_handlers.get(priority_pid)
        if priority_handler:
            priority_uid = _ge.UID.make(244, priority_pid)
            other_uid = _ge.UID.make(
                244, next(pid for pid in pids if int(pid) != priority_pid))
            resume = _ge.Game(int(session.session_id), priority_uid, other_uid)
            resume.push_chain_empty()
            resume.push_green_light(priority_uid, _ge.EPriorityContext.Normal)
            _send_pvp_packet(priority_handler, session, resume, priority_uid,
                             "greenlight-after-matching-target")
        if state.get("phase") in (_ge.ETurnPhases.FirstMainPhase,
                                   _ge.ETurnPhases.SecondMainPhase):
            pvp_push_main_phase_options(session, state)
    handler._push_transaction_ack(session)
    log_req(f"    PvP matching target chosen: {hex(int(chosen_uid))}; "
            "created matching cards and restored priority")
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
    from rules_port.context import EffectContext
    from rules_port.deck_effects import move_deck_card_to_hand
    state["_rules_port_attached"] = True
    move_deck_card_to_hand(EffectContext.from_rules_port(
        g, session, _db, handler, pl_t, ai_t, state,
        "move_deck_card_to_hand", ability=None), chosen_uid, owner_id)
    remaining = [cu for cu in revealed if cu != chosen_uid]
    if remaining:
        from pvp_db import db_randomly_insert_deck_cards
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

    chosen_info = db_card_play_info(
        session.session_id, chosen_uid, conn=_db)
    color = (chosen_info[2].split()[0] if chosen_info else "").lower()
    from pvp_db import db_randomly_insert_deck_cards
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
        threshold_trigger_game = _pvp_gain_threshold_trigger_game(
            handler, session, state, owner_id, flag)
        if threshold_trigger_game:
            for trigger_event in threshold_trigger_game.events:
                g._push(trigger_event)

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
    try:
        from rules_port.resolution import resolve_port_trigger
        resolve_port_trigger(handler, g, session, _db, pl_t, ai_t, view, {
            "kind": "trigger", "ability_guid": ag, "source_uid": src,
            "source_owner_uid": owner_id,
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
        _pvp_apply_visibility(g, pvp_load_state(session) or {})
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
    if not getattr(g, "_visibility_by_uid", None):
        from rules_port.visibility import \
            apply_player_visibility_to_game
        apply_player_visibility_to_game(g, pvp_load_state(session) or {})
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


def _pvp_push_waiting_on(session, waiting_pid):
    """Tell each client who the game is waiting on (class 79).

    The acting player receives an invalid id (which clears their own waiting
    state and must not cover their open dialog), while the other client is
    told to wait on the acting player.  Pass ``None`` to clear both.
    """
    pids = [int(pid) for pid in
            (db_game_session_pids(session.session_id) or [])]
    if len(pids) < 2:
        return
    waiting_uid = (_ge.UID.make(244, int(waiting_pid))
                   if waiting_pid else _ge.UID.invalid())
    for pid in pids:
        h = player_handlers.get(pid)
        if not h:
            continue
        opp = next((value for value in pids if value != pid), pid)
        g = _ge.Game(int(session.session_id), _ge.UID.make(244, pid),
                     _ge.UID.make(244, opp))
        # The acting player must not push BattleStateWait over their own
        # picker; send them an explicit clear instead of the matching id.
        if waiting_pid is not None and int(pid) == int(waiting_pid):
            g.push_waiting_on_player(None)
            event_desc = "clear"
        else:
            g.push_waiting_on_player(waiting_uid)
            event_desc = (str(waiting_pid) if waiting_pid else "clear")
        _send_pvp_packet(h, session, g, _ge.UID.make(244, pid),
                         f"waiting-on-player(to={pid},event={event_desc})")


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
    # The checkpoint can predate champ_map (or a RulesPort save can carry a
    # reduced state view).  Champion rows are authoritative and contain the
    # real typed SessionCardId, so never construct a reconnect champion from
    # a missing map entry: UID(..., 0) serializes as Undefined.0 and corrupts
    # the client's PlayerUpdated cache.
    champion_rows = {
        int(owner): row for owner, row in (
            (p, db_game_champion(session.session_id, p))
            for p in (pid, opp_pid))
        if row and row[0]
    }
    champ_map = state.setdefault("champ_map", {})
    for owner, row in champion_rows.items():
        champ_map[str(owner)] = int(row[0])
    if champion_rows:
        pvp_save_state(session, state)
    g = _ge.Game(int(session.session_id), pl_uid, opp_uid)
    g.player_champion_card_id = _ge.SessionCardId(
        _ge.UID(int(champ_map.get(str(pid), 0))))
    g.ai_champion_card_id = _ge.SessionCardId(
        _ge.UID(int(champ_map.get(str(opp_pid), 0))))
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
    champion_names = [
        db_tournament_player_name_for_session(
            session.session_id, game_pid) or f"Player {index + 1}"
        for index, game_pid in enumerate(game_started_pids)]
    g.push_game_started(
        champion_names=champion_names,
        champion_template_ids=champion_template_ids,
        player_first=(goes_first_pid == pid))
    g.push_first_player_dictated(_ge.UID.make(244, goes_first_pid))

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


def _pvp_reassign_priority_after_disconnect(session, disconnected_pid,
                                            survivor_pid):
    """Make a live PvP checkpoint usable by the still-connected player.

    A socket disappearing must not leave the native RulesPort priority action
    owned by that socket.  If the disconnected player owned priority, hand the
    current window to the survivor and rebuild the private options packet.
    If the survivor already owned priority, leave it untouched so their next
    transaction remains valid.
    """
    try:
        disconnected_pid = int(disconnected_pid)
        survivor_pid = int(survivor_pid)
    except (TypeError, ValueError):
        return False
    if not session or survivor_pid <= 0:
        return False
    with pvp_session_lock(session):
        state = pvp_load_state(session) or {}
        if not state.get("pvp"):
            return False
        pids = [int(pid) for pid in db_game_session_pids(session.session_id)]
        if disconnected_pid not in pids or survivor_pid not in pids:
            return False
        old_priority = int(state.get("priority_pid") or 0)
        phase = int(state.get("phase", 0) or 0)
        if old_priority == disconnected_pid and phase >= int(
                _ge.ETurnPhases.FirstMainPhase):
            state["priority_pid"] = survivor_pid
            port_reset_priority_interval(state, survivor_pid)
            pvp_save_state(session, state)
            log_req(f"    PvP priority handed off: {disconnected_pid} -> "
                    f"{survivor_pid} phase={phase}")
        else:
            # Still persist the latest clock before the socket disappears;
            # this prevents the watchdog from charging a stale interval.
            _pvp_flush_priority_clock(state)
            pvp_save_state(session, state)

    survivor_handler = player_handlers.get(survivor_pid)
    if not survivor_handler:
        return True
    state = pvp_load_state(session) or {}
    if int(state.get("priority_pid") or 0) != survivor_pid:
        return True
    opponent_pid = next((pid for pid in pids if pid != survivor_pid),
                        disconnected_pid)
    pl_uid = _ge.UID.make(244, survivor_pid)
    opp_uid = _ge.UID.make(244, opponent_pid)
    game = _ge.Game(int(session.session_id), pl_uid, opp_uid)
    _pvp_populate_game_state(game, state, survivor_pid, opponent_pid)
    _pvp_apply_visibility(game, state)
    priority_uid = _ge.UID.make(244, survivor_pid)
    turn_uid = _ge.UID.make(244, int(state.get("turn_pid") or survivor_pid))
    game.push_green_light(priority_uid, _ge.EPriorityContext.Normal)
    _pvp_push_turn_phase_with_elapsed(
        game, phase, turn_uid, priority_uid,
        _pvp_priority_elapsed_ticks(state, survivor_pid) // 10_000_000)
    _send_pvp_packet(survivor_handler, session, game, pl_uid,
                     "disconnect-priority")
    if phase in (_ge.ETurnPhases.FirstMainPhase,
                 _ge.ETurnPhases.SecondMainPhase):
        pvp_push_main_phase_options(session, state)
    elif phase == _ge.ETurnPhases.DeclareAttack:
        pvp_push_attack_options(session, state)
    elif phase == _ge.ETurnPhases.DeclareDefense:
        pvp_push_blocker_options(session, state)
    elif phase not in (3, 4, 5, 6, 7, 8, 9):
        pvp_push_phase_options(session, state, pid=survivor_pid)
    return True


def notify_pvp_player_disconnected(player_uid, disconnected_handler=None):
    """Reconcile a PvP game after one socket goes offline."""
    import game_session as gs
    try:
        player_uid = int(player_uid)
    except (TypeError, ValueError):
        return False
    # A reconnect can replace the registry entry before the old socket's
    # recv-loop reaches finally/_handle_disconnect.  That old socket must not
    # announce a disconnect for the still-live replacement connection.
    if disconnected_handler is not None:
        with player_handler_lock:
            if player_handlers.get(player_uid) is not disconnected_handler:
                log_req(f"    Ignoring stale PvP disconnect for {player_uid}")
                return False
    session = gs.find_session_by_player(player_uid)
    if (not session or getattr(session, "state", "") == "ended"
            or not str(getattr(session, "session_name", "") or "").startswith(
                "tourney-")):
        return False
    pvp_state = pvp_load_state(session)
    if not pvp_state or not pvp_state.get("pvp"):
        return False
    pids = db_game_session_pids(session.session_id)
    opponent = next((p for p in pids if int(p) != player_uid), None)
    opponent_handler = player_handlers.get(opponent) if opponent is not None else None
    if not opponent_handler or opponent_handler is disconnected_handler:
        return False
    try:
        _pvp_reassign_priority_after_disconnect(session, player_uid, opponent)
        log_req(f"    PvP disconnect reconciled: {player_uid} -> {opponent}")
        return True
    except Exception as exc:
        log_req(f"    PvP disconnect handling failed: {exc}")
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
    # A visible PvP warzone card must never be re-projected with the invalid
    # template used for hidden hand/deck cards. The Device reproduction
    # produced a valid Warzone update followed by an all-zero-template update
    # on the opposing client, which looked like the card tunnelling away.
    # Repair only that impossible combination from the authoritative session
    # row; Underground cards retain their deliberate hidden projection.
    for event in evs:
        if not isinstance(event, _ge.CardUpdatedSessionEventArgs):
            continue
        if event.collection != _ge.ECardCollections.Warzone:
            continue
        if getattr(getattr(event, "card_id", None), "guid", None).int != 0:
            continue
        card_uid = int(event.session_card_id.uid.uid64)
        details = db_card_zone_details(session.session_id, card_uid, conn=_db)
        template_guid = details[0] if details else None
        if template_guid:
            event.card_id = _ge.ResourceId.from_str(template_guid)
            log_req(f"    PvP repaired invalid Warzone template for "
                    f"{hex(card_uid)} -> {template_guid}")
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
        g2._visibility_by_uid = dict(
            getattr(game, "_visibility_by_uid", {}) or {})
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
            tid = tournament_id_from_session_name(session.session_name)
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
            # A timeout can occur while the client still has the interface
            # disabled from a prior transition.  Re-enable input before the
            # local GameOver state is pushed so its Continue button can call
            # the normal client-side tournament transition.
            enable = _ge.Game(int(session.session_id), my_uid,
                              _ge.UID.make(244, loser_pid if pid == winner_pid
                                           else winner_pid))
            enable.push_disable_interface(False)
            _send_pvp_packet(h, session, enable, my_uid,
                             "game-end-enable-input")
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
    # Free the per-session mutation lock and shared port now that the game is
    # over.
    try:
        pvp_discard_session_lock(session)
    except Exception:
        pass
    try:
        pvp_discard_shared_port(session)
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
    # ChampionWouldLoseEvent is a replacement event, not a post-game hook.
    # Resolve it before publishing the match result so authored survival
    # abilities get the same chance in PvP as in the campaign battle path.
    for pid in pids:
        if int(state.get(f"hp_{pid}", 20)) > 0:
            continue
        other = pids[1] if pid == pids[0] else pids[0]
        event_handler = player_handlers.get(pid) or player_handlers.get(other)
        if not event_handler:
            continue
        pl_uid = _ge.UID.make(244, pid)
        opp_uid = _ge.UID.make(244, other)
        game = _ge.Game(int(session.session_id), pl_uid, opp_uid)
        game.player_health = int(state.get(f"hp_{pid}", 20))
        game.ai_health = int(state.get(f"hp_{other}", 20))
        view = _pvp_fra_view(state, pid, other)
        _pvp_dispatch_triggers(
            event_handler, game, session, view, pl_uid, opp_uid,
            "ChampionWouldLoseEvent", int((state.get("champ_map") or {}).get(
                str(pid), 0) or 0), pid)
        if game.events:
            _pvp_send_same_events(session, game, pl_uid, opp_uid)
        _pvp_sync_view_to_state(state, view, pid, other)
        if view.get("stack"):
            state["stack"] = view["stack"]
        pvp_save_state(session, state)
    for pid in pids:
        if int(state.get(f"hp_{pid}", 20)) <= 0:
            other = pids[1] if pid == pids[0] else pids[0]
            _pvp_end_game(session, state, other, pid,
                          f"champion at 0 health")
            return True
    return False


def _pvp_resolve_native_permanent(session, state, handler, item):
    """Resolve a native permanent chain item into its warzone projection."""
    pids = db_game_session_pids(session.session_id)
    source_uid = int(item.get("source_uid") or 0)
    if len(pids) < 2 or not source_uid:
        return False
    stack = state.get("stack") or []
    if stack and int(stack[-1].get("instance_id", -1)) == int(
            item.get("instance_id", -2)):
        stack.pop()
    row = db_card_chain_info(session.session_id, source_uid, conn=_db)
    if not row or db_card_location(session.session_id, source_uid) != "CastSpells":
        return False
    owner_row = db_card_basic(session.session_id, source_uid, conn=_db)
    owner_id = int(owner_row[1]) if owner_row else int(pids[0])
    opponent_id = next((int(pid) for pid in pids if int(pid) != owner_id),
                       owner_id)
    player_uid = _ge.UID.make(244, owner_id)
    opponent_uid = _ge.UID.make(244, opponent_id)
    view = _pvp_fra_view(state, owner_id, opponent_id)
    game = _ge.Game(int(session.session_id), player_uid, opponent_uid)
    _pvp_populate_game_state(game, state, owner_id, opponent_id)
    instance_id = int(item.get("instance_id", 1) or 1)
    game.push_top_of_chain_resolved(instance_id)
    game.push_removed_top_of_chain(instance_id)
    db_set_card_location(
        session.session_id, source_uid, "warzone",
        extra_set="position=?, card_state=(card_state | ?)",
        extra_params=[0, _ge.ECardStates.CameOutThisTurn])
    scid = _ge.SessionCardId(_ge.UID(source_uid))
    _tpl, _ct, _name, _cost, _attack, _defense, gems = \
        handler._card_full_data(game, scid, row[0])
    card_type = _ge.card_type_from_db(row[1])
    cdef = game.card_defs.get(scid)
    game.push_card_updated(
        scid, player_uid, _ge.ECardCollections.Warzone, card_type,
        template_id=row[0], cost=cdef.cost if cdef else 0,
        attack=cdef.attack if cdef else 0,
        defense=cdef.defense if cdef else 0, gems=gems)
    game.push_card_moved(
        scid, player_uid, _ge.ECardCollections.Warzone,
        _ge.ECardLocations.Top, 0)
    if card_type & _ge.ECardTypes.Troop:
        game.push_troop_card_played(scid, player_uid)
    elif card_type & _ge.ECardTypes.Artifact:
        game.push_artifact_card_played(scid, player_uid)
    _pvp_dispatch_triggers(
        handler, game, session, view, player_uid, opponent_uid,
        "CardEnteredZoneEvent", source_uid, owner_id,
        event_destination_collection="warzone")
    view["card_cast_copy_target"] = source_uid
    _pvp_dispatch_triggers(
        handler, game, session, view, player_uid, opponent_uid,
        "CardCastEvent", source_uid, owner_id)
    view.pop("card_cast_copy_target", None)
    state["stack"] = view.get("stack") or []
    state["stack_passed"] = []
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False
    _pvp_sync_view_to_state(state, view, owner_id, opponent_id)
    pending = any(state.get(key) for key in (
        "pending_choice", "pending_deck_search", "pending_trigger",
        "pending_discard_ability"))
    if not pending and not state.get("stack"):
        game.push_chain_empty()
        turn_pid = int(state.get("turn_pid") or owner_id)
        state["priority_pid"] = turn_pid
        game.push_green_light(
            _ge.UID.make(244, turn_pid), _ge.EPriorityContext.Normal)
    elif not pending:
        state["priority_pid"] = opponent_id
    pvp_save_state(session, state)
    _pvp_send_same_events(session, game, player_uid, opponent_uid)
    if not pending and state.get("stack"):
        next_uid = _ge.UID.make(244, int(state["priority_pid"]))
        next_handler = player_handlers.get(int(state["priority_pid"]))
        if next_handler:
            response = _ge.Game(int(session.session_id), next_uid,
                                _ge.UID.make(244, int(owner_id)))
            response.push_green_light(
                next_uid, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(next_handler, session, response, next_uid,
                             "native-permanent-chain-next")
    return True


def _pvp_resolve_native_spell(session, state, handler, item):
    """Resolve one native RulesPort spell chain item and project its zone.

    The chain/priority decision is made by ``PvpAuthoritativeSession``. This
    function is deliberately only the PvP storage and wire projection around
    the shared Records-backed spell resolver.
    """
    pids = db_game_session_pids(session.session_id)
    source_uid = int(item.get("source_uid") or 0)
    if len(pids) < 2 or not source_uid:
        return False
    stack = state.get("stack") or []
    if stack and int(stack[-1].get("instance_id", -1)) == int(
            item.get("instance_id", -2)):
        stack.pop()
    source_row = db_card_basic(session.session_id, source_uid, conn=_db)
    owner_id = int(source_row[1]) if source_row else int(pids[0])
    opponent_id = next((int(pid) for pid in pids if int(pid) != owner_id),
                       owner_id)
    player_uid = _ge.UID.make(244, owner_id)
    opponent_uid = _ge.UID.make(244, opponent_id)
    view = _pvp_fra_view(state, owner_id, opponent_id)
    view["resolving_source_uid"] = source_uid
    view["resolving_owner_id"] = owner_id
    view["player_spell_target"] = item.get("target_uid")
    game = _ge.Game(int(session.session_id), player_uid, opponent_uid)
    _pvp_populate_game_state(game, state, owner_id, opponent_id)
    instance_id = int(item.get("instance_id", 1) or 1)
    game.push_top_of_chain_resolved(instance_id)
    game.push_removed_top_of_chain(instance_id)
    if db_card_location(session.session_id, source_uid) != "CastSpells":
        log_req(f"    Native PvP spell {source_uid} already left CastSpells")
    else:
        game.push_spell_card_played(
            _ge.SessionCardId(_ge.UID(source_uid)), player_uid)
        from rules_port.resolution import resolve_port_played_spell
        resolve_port_played_spell(
            game, session, _db, handler, player_uid, opponent_uid, view,
            item.get("ability_guids", ()),
            activations=item.get("activations") or {})
        persisted = pvp_load_state(session) or {}
        for key in ("pending_choice", "pending_deck_search", "pending_trigger",
                    "pending_discard_ability"):
            if persisted.get(key):
                state[key] = persisted[key]
        if db_card_location(session.session_id, source_uid) != "deck":
            db_card_discard_spell(session.session_id, source_uid)
            row = db_card_chain_info(
                session.session_id, source_uid, conn=_db)
            if row:
                scid = _ge.SessionCardId(_ge.UID(source_uid))
                handler._card_full_data(game, scid, row[0])
                game.push_card_updated(
                    scid, player_uid, _ge.ECardCollections.Discard,
                    _ge.card_type_from_db(row[1]), template_id=row[0])
                game.push_card_moved(
                    scid, player_uid, _ge.ECardCollections.Discard,
                    _ge.ECardLocations.Top, 0)
                _pvp_dispatch_triggers(
                    handler, game, session, view, player_uid, opponent_uid,
                    "CardEnteredZoneEvent", source_uid, owner_id)
    state["stack"] = view.get("stack") or []
    state["stack_passed"] = []
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False
    _pvp_sync_view_to_state(state, view, owner_id, opponent_id)
    pending = any(state.get(key) for key in (
        "pending_choice", "pending_deck_search", "pending_trigger",
        "pending_discard_ability"))
    if not pending and not (state.get("stack") or []):
        game.push_chain_empty()
        turn_pid = int(state.get("turn_pid") or owner_id)
        state["priority_pid"] = turn_pid
        game.push_green_light(
            _ge.UID.make(244, turn_pid), _ge.EPriorityContext.Normal)
    elif not pending:
        next_pid = next((int(pid) for pid in pids
                         if int(pid) != owner_id), owner_id)
        state["priority_pid"] = next_pid
    pvp_save_state(session, state)
    _pvp_send_same_events(session, game, player_uid, opponent_uid)
    if not pending and state.get("stack"):
        next_uid = _ge.UID.make(244, int(state["priority_pid"]))
        next_handler = player_handlers.get(int(state["priority_pid"]))
        if next_handler:
            response = _ge.Game(int(session.session_id), next_uid,
                                _ge.UID.make(244, int(owner_id)))
            response.push_green_light(
                next_uid, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(next_handler, session, response, next_uid,
                             "native-spell-chain-next")
    return True


def _pvp_resolve_chain(session, state, handler, my_pid, item=None):
    """Resolve the top item of the PvP chain/stack.

    The client's "Resolve" button submits a PassPriorityTransaction while the
    chain is non-empty.  This pops the top item, pushes TopOfChainResolved +
    RemovedTopOfChain + the effect's own events (e.g. Adamanthian Scrivener's
    life gain) to BOTH players, persists health, and hands priority on: back
    to the turn player (Normal) when the chain empties, else to the OTHER
    player (ResolveTopOfChain) so they can respond to the next item.
    Returns True when an item was resolved."""
    from rules_port import lifecycle as _be
    if item is None:
        item = _be.stack_pop(state)
    else:
        # RulesPort supplied the authoritative typed chain descriptor. The
        # persisted stack is only a wire/reconnect projection; remove its
        # matching mirror without selecting a different legacy item.
        item = dict(item)
        stack = state.get("stack") or []
        if stack and int(stack[-1].get("instance_id", -1)) == int(
                item.get("instance_id", -2)):
            stack.pop()
    if not item:
        return False
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    kind = item.get("kind")
    # Only champion-ability chain items have an ability GUID.  Keep the
    # post-resolution discard/continuation checks safe for troop/spell items
    # instead of leaking an UnboundLocalError after a normal card resolves.
    ag = ""
    instance_id = int(item.get("instance_id", 1))
    src_uid = int(item.get("source_uid") or 0)
    # Owner of the chain item's source card (triggers belong to their card).
    owner_id = my_pid
    if src_uid:
        orow = db_card_basic(session.session_id, src_uid)
        if orow:
            owner_id = orow[1]
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
    if kind == "trigger":
        try:
            from rules_port.resolution import resolve_port_trigger
            resolve_port_trigger(handler, g, session, _db, pl_t, ai_t, view,
                                 item)
        except Exception as e:
            import traceback
            log_req(f"    PvP chain trigger resolve error: {e}")
            traceback.print_exc()
    elif kind == "troop":
        # A permanent (troop/artifact/constant) resolves from the chain into
        # the warzone: mark CameOutThisTurn, push CardUpdated/CardMoved,
        # fire enters-play triggers — mirrors PvE's troop chain resolution.
        if src_uid:
            loc_row = db_card_location(session.session_id, src_uid)
            if not loc_row or loc_row != "CastSpells":
                log_req(f"    PvP troop {src_uid} already left the chain "
                        f"(loc={loc_row}) — skipped")
                pvp_save_state(session, state)
                return True
            scid = _ge.SessionCardId(_ge.UID(src_uid))
            tw = db_card_chain_info(
                session.session_id, src_uid, conn=_db)
            if tw:
                db_set_card_location(
                    session.session_id, src_uid, "warzone",
                    extra_set="position=?, card_state=(card_state | ?)",
                    extra_params=[0, _ge.ECardStates.CameOutThisTurn])
                _tpl, ct, _name, cost, attack, defense, gems = \
                    handler._card_full_data(g, scid, tw[0])
                ct = _ge.card_type_from_db(tw[1])
                g.push_card_updated(scid, pl_t, _ge.ECardCollections.Warzone,
                                    ct, template_id=tw[0],
                                    cost=cost, attack=attack,
                                    defense=defense, gems=gems)
                g.push_card_moved(scid, pl_t, _ge.ECardCollections.Warzone,
                                  _ge.ECardLocations.Top, 0)
                if ct & _ge.ECardTypes.Troop:
                    g.push_troop_card_played(scid, pl_t)
                elif ct & _ge.ECardTypes.Artifact:
                    g.push_artifact_card_played(scid, pl_t)
                try:
                    # CardEnteredZoneEvent is the native RulesPort trigger
                    # boundary for permanents entering the warzone.  The
                    # host still owns the SQLite/event projection above.
                    _pvp_dispatch_triggers(
                        handler, g, session, view, pl_t, ai_t,
                        "CardEnteredZoneEvent", src_uid, owner_id,
                        event_destination_collection="warzone")
                    # CardCastEvent also covers permanents.  Keep it separate
                    # from CardEnteredZoneEvent so cost-based triggers such
                    # as Jadiim see the card that was actually played.
                    view["card_cast_copy_target"] = src_uid
                    _pvp_dispatch_triggers(
                        handler, g, session, view, pl_t, ai_t,
                        "CardCastEvent", src_uid, owner_id)
                    view.pop("card_cast_copy_target", None)
                except Exception as e:
                    log_req(f"    PvP troop chain enters-play error: {e}")
    elif kind == "spell":
        # A played action resolves its BOM then goes CastSpells -> Discard.
        # A spell that was countered/interrupted already left the chain —
        # skip its BOM so a countered spell never also draws/buffs/damages.
        loc = db_card_location(session.session_id, src_uid) or "discard"
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
                from rules_port.resolution import resolve_port_played_spell
                view["player_spell_target"] = item.get("target_uid")
                view["resolving_source_uid"] = src_uid
                view["resolving_owner_id"] = owner_id
                view["x_cost"] = int(item.get("x_cost") or 0)
                resolve_port_played_spell(
                    g, session, _db, handler, pl_t, ai_t, view,
                    item.get("ability_guids", []),
                    activations=item.get("activations"))
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
                    view["card_cast_copy_target"] = src_uid
                    _pvp_dispatch_triggers(
                        handler, g, session, view, pl_t, ai_t,
                        "CardCastEvent", src_uid, owner_id)
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
                loc = db_card_location(session.session_id, src_uid) or "discard"
                if loc != "deck":
                    db_card_discard_spell(session.session_id, src_uid)
                    scid = _ge.SessionCardId(_ge.UID(src_uid))
                    tw = db_card_chain_info(
                        session.session_id, src_uid, conn=_db)
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
                            _pvp_dispatch_triggers(
                                handler, g, session, view, pl_t, ai_t,
                                "CardEnteredZoneEvent", src_uid, owner_id)
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
        if ag.lower() == "f2d6797b-1a24-4c3d-9239-a27a2e0de0ff":
            from rules_port.tunneling import surface_source_is_underground
            if not surface_source_is_underground(
                    _db, session, item.get("source_uid")):
                log_req(f"    Ignoring stale PvP tunneling Surface "
                        f"source={item.get('source_uid')}")
                ag = ""
        if ag:
            try:
                from rules_port.resolution import resolve_port_ability
                view["player_mod_target"] = item.get("target_uid")
                view["player_spell_target"] = item.get("target_uid")
                view["resolving_ability"] = ag
                view["resolving_source_uid"] = src_uid
                view["resolving_owner_id"] = owner_id
                # Pay-cost HUD events (charge/resource/spell) must reach the
                # client BEFORE any picker this BOM opens.  The client
                # re-evaluates ability options when the charge changes and
                # would otherwise close the chooser it just opened (the picker
                # showed for ~1s then vanished).  Mirrors the client's own
                # ordering where the cost is paid at activation, before the
                # ability resolves.
                cost_game = _ge.Game(int(session.session_id), pl_t, ai_t)
                _pvp_populate_game_state(cost_game, state, owner_id, opp_pid)
                _pvp_emit_paid_cost_events(cost_game, state)
                if cost_game.events:
                    _pvp_send_same_events(session, cost_game, pl_t, ai_t)
                ability_event_start = len(g.events)
                ability_player_health_before = int(view.get("player_health", 20))
                ability_ai_health_before = int(view.get("ai_health", 20))
                target_map = {}
                if item.get("target_uid") is not None:
                    from gamedata import ability_graph, DEFAULT_RECORD_STORE
                    graph = ability_graph(DEFAULT_RECORD_STORE, ag)
                    if graph is not None:
                        for index, spec in enumerate(graph.targets):
                            if spec.requires_input:
                                target_map[index] = int(item["target_uid"])
                                break
                resolve_port_ability(
                    handler, g, session, _db, pl_t, ai_t, view, ag,
                    src_uid, owner_id, target_map=target_map,
                    instance_id=instance_id)
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
                         "pending_choice", "pending_conversation"):
        if persisted.get(_pending_key):
            state[_pending_key] = persisted[_pending_key]

    # Champion abilities can contain a nested DiscardCard leaf just like
    # troop abilities (Blue Sparrow draws, then chooses and discards). The
    # BOM resolver performs the draw, but the discard is a client class-23
    # follow-up rather than an ordinary effect target. Schedule the shared
    # metadata-derived PvP picker after the resolution events are sent.
    pending_discard = False
    if ag:
        discard_prompt = _pvp_discard_prompt_data(ag)
        hand_exists = db_hand_exists(
            session.session_id, owner_id, conn=_db)
        if discard_prompt and hand_exists:
            state["pending_discard_ability"] = discard_prompt[0]
            state["pending_discard_target_template"] = discard_prompt[1]
            state["pending_discard_source_uid"] = int(src_uid)
            state["pending_discard_pid"] = int(owner_id)
            state["priority_pid"] = int(owner_id)
            pending_discard = True
    chain_empty = _be.stack_empty(state)
    pending_revealed_choice = (
        (state.get("pending_deck_search") or {}).get("kind")
        == "revealed_troop")
    pending_trigger = bool(state.get("pending_trigger"))
    pending_choice = bool(state.get("pending_choice"))
    pending_conversation = bool(state.get("pending_conversation"))
    # Shards of Fate / Adaptable Infusion Device sends a private class-39
    # deck picker to the controller.  Its picker packet already owns the
    # next green-light; sending the normal chain-empty/options packet here
    # tears down that UI and leaves PvP priority stranded.
    pending_deck_search = bool(state.get("pending_deck_search"))
    # State-based actions are checked again after any start-of-turn trigger
    # chain resolves.  A tunneled card that reached its threshold while that
    # chain was on top must still surface before normal phase priority returns.
    if (chain_empty and not (pending_revealed_choice or pending_deck_search
                             or pending_trigger or pending_choice
                             or pending_conversation or pending_discard)):
        from rules_port.tunneling import queue_surfaces
        turn_pid = int(state.get("turn_pid") or 0)
        tunnel_handler = player_handlers.get(turn_pid) or handler
        turn_pt = _ge.UID.make(244, turn_pid)
        turn_opp = _ge.UID.make(
            244, pids[1] if turn_pid == pids[0] else pids[0])
        state["_rules_port_attached"] = True
        from rules_port.context import EffectContext
        tunnel_context = EffectContext.from_rules_port(
            g, session, _db, tunnel_handler, turn_pt, turn_opp, state,
            "", ability=None)
        tunnel_surfaces = queue_surfaces(tunnel_context, turn_pid)
        if tunnel_surfaces:
            chain_empty = False
            pvp_save_state(session, state)
            log_req(f"    PvP state-based tunneling: queued surfaces "
                    f"{tunnel_surfaces}")
    _pvp_log_stack(state, "resolve")
    if chain_empty and not (pending_revealed_choice or pending_deck_search
                            or pending_trigger or pending_choice or
                            pending_conversation):
        g.push_chain_empty()
    pvp_save_state(session, state)
    # State-based deaths: when the stack empties, troops at <=0 effective
    # defense die (spell/trigger -X/-X, etc.) — mirrors PvE
    # _resolve_stack_item (hconnect ~3087).  The death events go into the SAME
    # stream so both clients see the graveyard move + Deathcry.
    if chain_empty:
        try:
            from rules_port.context import EffectContext
            from rules_port.death_effects import state_based_deaths
            view["_rules_port_attached"] = True
            state_based_deaths(EffectContext.from_rules_port(
                g, session, _db, handler, pl_t, ai_t, view,
                "state_based_death", ability=None))
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
    # The chain item is only removed once the ability FULLY resolves, and an
    # IgnoresChain ability is never added to the client's chain at all
    # (UIBattle.OnAbilityPushedOnChain plays a card event instead).  Emitting
    # TopOfChainResolved/RemovedTopOfChain for it — or while the BOM is paused
    # on an interactive prompt — would tear down the picker the client just
    # opened.  Mirrors the client: ResolveTopOfChainAction removes the item
    # only on COMPLETED, and only for a real chain entry.
    ignores_chain = False
    if kind == "ability":
        try:
            from rules_port.session import projected_ability_ignores_chain
            ignores_chain = projected_ability_ignores_chain(item)
        except Exception:
            ignores_chain = False
    if not ignores_chain and not any(state.get(key) for key in (
            "pending_choice", "pending_trigger", "pending_deck_search",
            "pending_conversation", "pending_discard_ability",
            "resolution_paused")):
        g.push_top_of_chain_resolved(instance_id)
        g.push_removed_top_of_chain(instance_id)
    _pvp_emit_paid_cost_events(g, state)
    _pvp_send_same_events(session, g, pl_t, ai_t)
    if chain_empty and pending_discard:
        _pvp_push_discard_prompt(
            session, state, int(owner_id), int(opp_pid), int(src_uid))
        pvp_save_state(session, state)
        log_req(f"    PvP chain paused for discard choice: {ag[:8]} "
                f"source={hex(int(src_uid))}")
        return True
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
    if chain_empty and pending_conversation:
        # Class 55 was sent privately to the controller by the conversation
        # effect.  Do not replace it with chain-empty/priority events until
        # EncounterModDialogTransaction resumes the BOM.
        pvp_save_state(session, state)
        log_req("    PvP chain paused for encounter conversation")
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
    from rules_port.pvp_view import to_effect_view
    return to_effect_view(state, attacker_pid, defender_pid)


def _pvp_typed_card_targets(payload, source_uid):
    """Flatten decoded card-play TargetMap values in client order."""
    values = []
    for activation in (payload or {}).get("ability_data") or ():
        if not isinstance(activation, dict):
            continue
        for selected in (activation.get("target_map", {}) or {}).values():
            selected = selected if isinstance(
                selected, (list, tuple, set)) else (selected,)
            for value in selected:
                try:
                    uid = int(getattr(value, "uid64", value))
                except (TypeError, ValueError):
                    continue
                if uid != int(source_uid):
                    values.append(uid)
    return values


def _pvp_play_troop(handler, session, played_card_uid, my_pid, inner_bytes,
                    typed_payload=None, native_port=None):
    """Play a non-resource permanent in PvP: hand -> CastSpells -> Warzone,
    fire enters-play triggers, and push the events to BOTH players.  Returns
    True when handled."""
    from gamedata import PlayPlan
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    crow = db_card_play_info(
        session.session_id, played_card_uid, conn=_db)
    if not crow:
        return False
    tpl_guid, card_type, card_name = crow[0], crow[1], crow[2]
    card_abilities = crow[5]
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
    try:
        play_plan = PlayPlan.from_card(
            _RECORD_STORE, tpl_guid, source_uid=int(played_card_uid),
            owner_id=my_pid)
    except KeyError as exc:
        log_req(f"    PvP REJECTED permanent {card_name}: {exc}")
        return True
    # Decode the one client TargetMap before paying any resource or card cost.
    # Cost targets and effect targets share the wire list, so the PlayPlan
    # partitions them using the authored target templates.
    targets = _pvp_typed_card_targets(typed_payload, played_card_uid)
    if typed_payload is None:
        try:
            targets = handler._extract_transaction_targets(
                inner_bytes, int(played_card_uid))
        except Exception:
            targets = []
    champ_map = state.get("champ_map") or {}
    champ_targets = []
    for cpid in state.get("pids") or pids:
        cuid = int(champ_map.get(str(cpid), 0))
        if cuid:
            champ_targets.append((
                cuid, int(cpid), "Champion",
                int(state.get(f"hp_{cpid}", 20))))
    cost_selection = _pvp_select_card_play_costs(
        handler, session, state, play_plan, int(played_card_uid), my_pid,
        targets, champ_targets)
    if cost_selection is None:
        log_req(f"    PvP REJECTED spell {card_name}: incomplete/illegal "
                "card cost target")
        return True
    cost_selections, cost_uids = cost_selection
    # Defense-in-depth cost check: the client's options are the normal gate,
    # but don't let a drag play an unaffordable card and go negative.
    _trow = db_template_by_guid(tpl_guid)
    cost = play_plan.cost.resource
    if not cost and _trow and not play_plan.cost.variable:
        cost = _trow[3] or 0
    # Effective cost (static cost modifiers) — charge what the client showed.
    try:
        from rules_port.static_rules import effective_cost as _ec
        cost = _ec(_db, session.session_id,
                   _pvp_fra_view(state, my_pid, opp_pid), int(played_card_uid))
    except Exception:
        pass
    available = int(state.get(f"res_{my_pid}", 0))
    if native_port is None and cost > available:
        log_req(f"    PvP REJECTED play {card_name}: cost {cost} > "
                f"resources {available}")
        pvp_push_main_phase_options(session, state)
        return True
    activations, _cost_target_map = play_plan.activation_bundle(targets)
    selected_cost_map = {
        index: tuple(selected)
        for index, (_spec, selected) in enumerate(cost_selections)
        if selected
    }
    plan_errors = (play_plan.validate(
        activations=activations, cost_target_map=selected_cost_map)
                   if native_port is None else ())
    if plan_errors:
        log_req(f"    PvP REJECTED play {card_name}: activation validation: "
                f"{'; '.join(plan_errors)}")
        pvp_push_main_phase_options(session, state)
        return True
    # Pay the cost FIRST (before any resolution) so the resource pool is
    # correct regardless of triggers firing.
    from rules_port.card_transactions import apply_card_play_for_player
    card_transition = apply_card_play_for_player(
        _db, session.session_id, state, int(played_card_uid), my_pid, cost)
    if card_transition is None:
        log_req(f"    PvP REJECTED play {card_name}: card was not in hand")
        return True
    view = _pvp_fra_view(state, my_pid, opp_pid)
    # Move the card onto the CHAIN (CastSpells visual) — mirroring how spells
    # are cast.  Troops/artifacts/constants do NOT resolve instantly: they stay
    # on the stack so the opponent can respond (e.g. Countermagic) before they
    # resolve to the warzone via the both-pass chain flow in _pvp_resolve_chain.
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    _pvp_populate_game_state(g, state, my_pid, opp_pid)
    _pvp_apply_card_play_costs(
        handler, g, session, state, my_uid, opp_uid, cost_selections,
        int(played_card_uid))
    # Register the CardDef FIRST so every CardUpdated carries the full stats
    # (mirrors the spell path).
    _tpl, ct, _n, cost2, atk, def_, gems = \
        handler._card_full_data(g, scid, tpl_guid)
    g.push_card_updated(scid, my_uid, _ge.ECardCollections.CastSpells,
                        ctype_num, template_id=tpl_guid, cost=cost2,
                        attack=atk, defense=def_, gems=gems)
    g.push_card_moved(scid, my_uid, _ge.ECardCollections.CastSpells,
                      _ge.ECardLocations.Top, 0)
    # Push the permanent onto the chain as a "troop" item.
    from rules_port import lifecycle as _be
    inst_id = port_queue_stack_item(state, {
        "kind": "troop", "source_uid": int(played_card_uid),
        "ability_guids": [], "target_uid": None,
        "x_cost": 0,
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
        _tabs = _chj.loads(card_abilities) if card_abilities else []
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
    if native_port is not None:
        native_port.queue_projected_chain(
            {"kind": "troop", "source_uid": int(played_card_uid),
             "ability_guids": [], "target_uid": None,
             "instance_id": int(inst_id), "x_cost": 0},
            my_pid, first_player_id=opp_uid)
        state["priority_pid"] = int(opp_pid)
        pvp_save_state(session, state)
        opp_h = player_handlers.get(opp_pid)
        if opp_h:
            response = _ge.Game(int(session.session_id), opp_uid, my_uid)
            response.push_green_light(
                opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(opp_h, session, response, opp_uid,
                             "native-troop-chain-opp")
            pvp_push_phase_options(session, state, pid=opp_pid)
        caster_h = player_handlers.get(my_pid)
        if caster_h:
            response = _ge.Game(int(session.session_id), my_uid, opp_uid)
            response.push_green_light(
                opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(caster_h, session, response, my_uid,
                             "native-troop-chain-caster")
        session._rules_port_mutation_emitted = True
        return True
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


def _pvp_play_spell(handler, session, played_card_uid, my_pid, inner_bytes,
                    typed_payload=None, native_port=None):
    """Cast a BasicAction/QuickAction spell in PvP: hand -> CastSpells, push
    the spell onto the chain, then resolve its BOM when the chain resolves and
    send CastSpells -> Discard.  A player may cast a QuickAction any time they
    hold priority and can pay the cost.  Pushes events to BOTH players."""
    from rules_port import lifecycle as _be
    from gamedata import PlayPlan
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    crow = db_card_play_info(
        session.session_id, played_card_uid, conn=_db)
    if not crow:
        return False
    tpl_guid, card_type, card_name, _current_grant, _max_grant, ab_json = crow
    try:
        play_plan = PlayPlan.from_card(
            _RECORD_STORE, tpl_guid, source_uid=int(played_card_uid),
            owner_id=my_pid)
    except KeyError as exc:
        log_req(f"    PvP REJECTED spell {card_name}: {exc}")
        return True
    scid = _ge.SessionCardId(_ge.UID(int(played_card_uid)))
    my_uid = _ge.UID.make(244, my_pid)
    opp_uid = _ge.UID.make(244, opp_pid)
    state = pvp_load_state(session) or {}
    targets = _pvp_typed_card_targets(typed_payload, played_card_uid)
    if typed_payload is None:
        try:
            targets = handler._extract_transaction_targets(
                inner_bytes, int(played_card_uid))
        except Exception:
            targets = []
    champ_map = state.get("champ_map") or {}
    champ_targets = []
    for cpid in state.get("pids") or pids:
        cuid = int(champ_map.get(str(cpid), 0))
        if cuid:
            champ_targets.append((
                cuid, int(cpid), "Champion",
                int(state.get(f"hp_{cpid}", 20))))
    cost_selection = _pvp_select_card_play_costs(
        handler, session, state, play_plan, int(played_card_uid), my_pid,
        targets, champ_targets)
    if cost_selection is None:
        log_req(f"    PvP REJECTED spell {card_name}: incomplete/illegal "
                "card cost target")
        return True
    cost_selections, cost_uids = cost_selection
    # Defense-in-depth cost check; pay FIRST so resources are right even if
    # the resolution is interrupted.
    _trow = db_template_by_guid(tpl_guid)
    cost = _trow[3] if _trow else 0
    # Effective cost (static cost modifiers) — charge what the client showed.
    try:
        from rules_port.static_rules import effective_cost as _ec
        cost = _ec(_db, session.session_id,
                   _pvp_fra_view(state, my_pid, opp_pid), int(played_card_uid))
    except Exception:
        pass
    # Read and validate the selected X before mutating resources.  The client
    # enforces this in AreXCostsComplete; the server must reject stale or
    # forged transactions by the same plan boundary.
    x_cost = max((int(item.get("x_cost", 0) or 0)
                  for item in (typed_payload or {}).get("ability_data", ())
                  if isinstance(item, dict)), default=0)
    if typed_payload is None:
        try:
            x_cost = max(0, int(handler._extract_int32_field(
                inner_bytes, "m_ResourceXCost") or 0))
        except Exception:
            x_cost = 0
    if play_plan.cost.variable:
        if x_cost < play_plan.cost.variable_minimum:
            log_req(f"    PvP REJECTED spell {card_name}: X={x_cost} below "
                    f"minimum {play_plan.cost.variable_minimum}")
            return True
    elif x_cost:
        log_req(f"    PvP REJECTED spell {card_name}: non-variable card has X={x_cost}")
        return True
    available = int(state.get(f"res_{my_pid}", 0))
    if native_port is None and cost + x_cost > available:
        log_req(f"    PvP REJECTED spell {card_name}: cost {cost} > "
                f"resources {available}")
        pvp_push_main_phase_options(session, state)
        return True
    # Validate the complete current-record activation before paying or moving
    # the card.  Target and option failures must reject the play atomically.
    activations, _cost_target_map = play_plan.activation_bundle(
        targets, x_cost=x_cost)
    selected_cost_map = {
        index: tuple(selected)
        for index, (_spec, selected) in enumerate(cost_selections)
        if selected
    }
    plan_errors = (play_plan.validate(
        variable_cost=x_cost, activations=activations,
        cost_target_map=selected_cost_map) if native_port is None else ())
    if plan_errors:
        log_req(f"    PvP REJECTED spell {card_name}: activation validation: "
                f"{'; '.join(plan_errors)}")
        pvp_push_main_phase_options(session, state)
        return True
    from rules_port.card_transactions import apply_card_play_for_player
    card_transition = apply_card_play_for_player(
        _db, session.session_id, state, int(played_card_uid), my_pid,
        cost + x_cost)
    if card_transition is None:
        log_req(f"    PvP REJECTED spell {card_name}: card was not in hand")
        return True
    if x_cost:
        log_req(f"    PvP spell {card_name}: X cost {x_cost} paid "
                f"(resources left {state.get(f'res_{my_pid}')})")
    view = _pvp_fra_view(state, my_pid, opp_pid)
    effect_targets = [uid for uid in targets if int(uid) not in cost_uids]
    target_uid = effect_targets[-1] if effect_targets else None
    view["player_spell_target"] = target_uid
    view["resolving_owner_id"] = my_pid
    view["resolving_source_uid"] = int(played_card_uid)
    g = _ge.Game(int(session.session_id), my_uid, opp_uid)
    _pvp_populate_game_state(g, state, my_pid, opp_pid)
    _pvp_apply_card_play_costs(
        handler, g, session, state, my_uid, opp_uid, cost_selections,
        int(played_card_uid))
    _tpl, ct, _n, cost2, atk, def_, gems = \
        handler._card_full_data(g, scid, tpl_guid)
    g.push_card_updated(scid, my_uid, _ge.ECardCollections.CastSpells, ct,
                        template_id=tpl_guid, cost=cost2, attack=atk,
                        defense=def_, gems=gems)
    g.push_card_moved(scid, my_uid, _ge.ECardCollections.CastSpells,
                      _ge.ECardLocations.Top, 0)
    g.push_spell_card_cast(scid, my_uid, free=False)
    ability_guids = [ability.ability_guid for ability in play_plan.abilities
                     if not ability.is_triggered]
    inst_id = port_queue_stack_item(state, {
        "kind": "spell", "source_uid": int(played_card_uid),
        "ability_guids": ability_guids, "target_uid": target_uid,
        "x_cost": x_cost,
        "activations": {
            guid: activation.as_dict()
            for guid, activation in activations.items()
        },
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
    if native_port is not None:
        native_port.queue_projected_chain(
            {"kind": "spell", "source_uid": int(played_card_uid),
             "ability_guids": ability_guids,
             "target_uid": target_uid, "instance_id": int(inst_id),
             "x_cost": x_cost,
             "activations": {
                 guid: activation.as_dict()
                 for guid, activation in activations.items()}},
            my_pid, first_player_id=opp_uid)
        state["priority_pid"] = int(opp_pid)
        pvp_save_state(session, state)
        opp_h = player_handlers.get(opp_pid)
        if opp_h:
            response = _ge.Game(int(session.session_id), opp_uid, my_uid)
            response.push_green_light(
                opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(opp_h, session, response, opp_uid,
                             "native-spell-chain-opp")
            pvp_push_phase_options(session, state, pid=opp_pid)
        caster_h = player_handlers.get(my_pid)
        if caster_h:
            response = _ge.Game(int(session.session_id), my_uid, opp_uid)
            response.push_green_light(
                opp_uid, _ge.EPriorityContext.ResolveTopOfChain)
            _send_pvp_packet(caster_h, session, response, my_uid,
                             "native-spell-chain-caster")
        session._rules_port_mutation_emitted = True
        return True
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


def _pvp_select_card_play_costs(handler, session, state, plan, source_uid,
                                pid, selected_uids, champions):
    """Bind the client TargetMap to the card's authored cost targets."""
    from rules_port.costs import card_cost_targets, cost_type_for_kind

    selected_uids = [int(uid) for uid in (selected_uids or [])]
    used = set()
    selections = []
    if not plan.cost_instances:
        return selections, used
    for native in card_cost_targets(
            plan, _db, session.session_id, pid, int(source_uid),
            champions=champions, battle_state=state):
        spec = {"kind": native.kind, "target_guid": native.guid,
                "cost_type": cost_type_for_kind(native.kind),
                "minimum": native.minimum, "maximum": native.maximum,
                "auto": native.is_source_auto_target}
        candidates = list(native.candidates)
        if native.is_source_auto_target:
            selections.append((spec, candidates))
            continue
        candidate_set = {int(uid) for uid in candidates}
        available = [uid for uid in selected_uids
                     if uid in candidate_set and uid not in used]
        minimum = int(spec["minimum"])
        maximum = int(spec["maximum"])
        if maximum < 0:
            maximum = len(available)
        if len(available) < minimum:
            return None
        chosen = tuple(available[:maximum])
        used.update(chosen)
        selections.append((spec, chosen))
    return selections, used


def _pvp_apply_card_play_costs(handler, game, session, state, pl_t, opp_t,
                               selections, source_uid):
    """Apply card-level CostInstances before the card enters the chain."""
    from pvp_db import db_discard_card, db_randomly_insert_deck_cards
    def push_zone(uid, owner_pid, location):
        row = db_card_zone_projection(
            session.session_id, uid, conn=_db)
        if not row:
            return
        scid = _ge.SessionCardId(_ge.UID(int(uid)))
        handler._card_full_data(game, scid, row[0])
        owner = _ge.UID.make(244, int(owner_pid))
        collection = {
            "hand": _ge.ECardCollections.Hand,
            "deck": _ge.ECardCollections.Deck,
            "discard": _ge.ECardCollections.Discard,
            "void": _ge.ECardCollections.Void,
            "warzone": _ge.ECardCollections.Warzone,
        }.get(location, _ge.ECardCollections.Warzone)
        cdef = game.card_defs.get(scid)
        game.push_card_updated(
            scid, owner, collection, _ge.card_type_from_db(row[1]),
            template_id=row[0], state=int(row[2] or 0),
            cost=cdef.cost if cdef else 0,
            attack=cdef.attack if cdef else 0,
            defense=cdef.defense if cdef else 0,
            nulling=location == "deck")
        game.push_card_moved(
            scid, owner, collection, _ge.ECardLocations.Top, 0)

    for spec, selected in selections:
        kind = spec["kind"]
        for uid in selected:
            uid = int(uid)
            row = db_card_owner_zone_state(
                session.session_id, uid, conn=_db)
            if not row:
                continue
            owner_pid = int(row[0] or 0)
            if kind == "sacrifice":
                handler._sacrifice_troop(game, session, pl_t, opp_t, uid)
                continue
            if kind == "exhaust":
                db_set_card_state_or(
                    session.session_id, uid, _ge.ECardStates.Tapped)
                _db.commit()
                push_zone(uid, owner_pid, row[1])
                continue
            destination = {
                "discard": "discard",
                "void": "void",
                "put_into_deck": "deck",
                "shuffle_into_deck": "deck",
                "put_into_hand": "hand",
            }.get(kind)
            if destination is None:
                if kind == "reveal":
                    scid = _ge.SessionCardId(_ge.UID(uid))
                    owner = _ge.UID.make(244, owner_pid)
                    event = game._make_event(
                        _ge.CardsRevealedSessionEventArgs)
                    event.player_id = owner
                    event.session_card_ids = [scid]
                    event.collections = [_ge.ECardCollections.Warzone]
                    event.owning_players = [owner]
                    event.positions = [0]
                    game._push(event)
                continue
            if destination == "discard":
                db_discard_card(session.session_id, uid, connection=_db)
            elif destination == "hand":
                db_set_card_location(
                    session.session_id, uid, "hand",
                    extra_set="position=?", extra_params=[100])
                _db.commit()
            else:
                db_set_card_location(
                    session.session_id, uid, destination,
                    extra_set="position=?, card_state=?",
                    extra_params=[0, 0])
                _db.commit()
                if destination == "deck":
                    db_randomly_insert_deck_cards(
                        session.session_id, owner_pid, [uid], connection=_db)
            push_zone(uid, owner_pid, destination)
            if destination in ("discard", "void"):
                _pvp_dispatch_triggers(
                    handler, game, session, state, pl_t, opp_t,
                    "CardEnteredZoneEvent", uid, owner_pid,
                    event_source_collection=row[1],
                    event_destination_collection=destination,
                    event_previous_state=int(row[2] or 0))


def _pvp_activate_champion_ability(handler, session, inner_bytes, my_pid):
    """Activate the pid's champion charge/spell power (ActivateAbilityTransaction
    from the champion ability button): extract the ability GUID + chosen target,
    pay the charge/spell cost from the PvP state, push the ability onto the
    chain, and hand the caster priority (ResolveTopOfChain) so the chain
    resolution (both-pass) resolves its BOM through _pvp_resolve_chain."""
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    opp_pid = pids[0] if pids[1] == my_pid else pids[1]
    state = pvp_load_state(session) or {}
    ability_guid = extract_ability_guid(inner_bytes)
    if not ability_guid:
        log_req(f"    PvP champion ability: could not parse GUID "
                f"(pid {my_pid})")
        return False
    champ_map = state.get("champ_map") or {}
    my_champ_uid = int(champ_map.get(str(my_pid), 0))
    if not my_champ_uid:
        return False
    # Affordability: charge/spell cost from champion_abilities / talents.
    from pvp_db import (db_champion_ability_costs,
                        db_champion_ability_thresholds)
    from pve_db import db_talent_ability_costs
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
    from rules_port.resources import (pay_charge_for_player,
                                      pay_spell_points_for_player)
    # Both affordability checks above happen before either transition, so a
    # malformed activation cannot partially consume a champion cost.
    pay_charge_for_player(state, my_pid, cc)
    pay_spell_points_for_player(state, my_pid, effective_sc)
    if sc:
        spell_uses[str(ability_guid)] = int(
            spell_uses.get(str(ability_guid), 0) or 0) + 1
        state[f"sp_uses_{my_pid}"] = spell_uses
    from rules_port import lifecycle as _be
    inst_id = port_queue_stack_item(state, {
        "kind": "ability", "ability_guid": ability_guid,
        "source_uid": my_champ_uid, "target_uid": target_uid,
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
    _pvp_dispatch_triggers(
        handler, g, session, state, my_uid, opp_uid,
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
    if _state_refresh.get("stack"):
        # The charge power is still on the chain. Publish only the legal
        # response-window options; rebuilding main-phase options here leaves
        # the client with a clickable champion/card while the chain waits.
        pvp_push_phase_options(session, _state_refresh, pid=my_pid)
    elif _state_refresh.get("phase") in (_ge.ETurnPhases.FirstMainPhase,
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
    wz_rows = [
        (uid, card_state, attrs)
        for uid, card_state, _card_type, attrs in
        db_warzone_attack_option_rows(
            session.session_id, my_pid, conn=_db)
    ]
    from rules_port.static_rules import effective_attributes
    wz = set()
    for uid, cstate, attrs in wz_rows:
        cstate = int(cstate or 0)
        attrs = int(attrs or 0) | int(effective_attributes(
            _db, session.session_id, state, int(uid)) or 0)
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
    from pvp_db import db_card_set_attacking_state
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
        pushed_state = db_card_state_value(session.session_id, int(u))
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
        view = _pvp_fra_view(state, my_pid, opp_pid)
        _pvp_dispatch_triggers(
            handler, g, session, view, my_uid, opp_uid,
            "CardAttackedEvent", int(u), my_pid)
        _pvp_dispatch_triggers(
            handler, g, session, view, my_uid, opp_uid,
            "CardAttackedOrBlockedEvent", int(u), my_pid)
        from rules_port.context import EffectContext
        from rules_port.combat_effects import apply_rage
        apply_rage(EffectContext.from_rules_port(
            g, session, _db, handler, my_uid, opp_uid, view,
            "", ability=None), int(u))
        # Persist any trigger/rage health changes.
        if view.get("player_health") is not None:
            state[f"hp_{my_pid}"] = int(view["player_health"])
        if view.get("ai_health") is not None:
            state[f"hp_{opp_pid}"] = int(view["ai_health"])
        pvp_save_state(session, state)
    if attackers:
        # One champion-scoped event represents the whole declaration; do not
        # emit one event per attacker because metadata conditions consume the
        # authored NumAttackers TAC value.
        from rules_port.tac import _tac_attr_hash
        _pvp_dispatch_triggers(
            handler, g, session, state, my_uid, opp_uid,
            "CardsAttackedEvent", my_champ or int(my_uid.uid64), my_pid,
            event_tac={_tac_attr_hash("NumAttackers"): len(attackers)})
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
    from rules_port import lifecycle as _be
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
        port_enter_phase(state, new_phase)
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
        port_enter_phase(state, new_phase)
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
    rows = db_card_attribute_rows(
        session.session_id, uids, conn=_db)
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
    return port_phase_after_blockers(
        bool(state.get("attackers") or {}),
        pvp_combat_has_swiftstrike(session, state))


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
    my_wz = set(r[0] for r in db_card_uids_in_zone(
        session.session_id, my_pid, "warzone", conn=_db))
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
            from rules_port.combat_rules import can_block
            if can_block(_db, session.session_id, _pvp_fra_view(state, opp_pid, my_pid),
                         cur, u):
                blockers_map[cur].append(u)
    state["blockers"] = {str(k): [str(b) for b in v]
                         for k, v in blockers_map.items()}
    # Mark each blocker Blocking in the DB so reconnect / HasBlocked logic and
    # the shared resolver's end-of-combat clear work (mirrors PvE
    # db_bulk_blocker_state).
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
    from rules_port import lifecycle as _be
    pids = db_game_session_pids(session.session_id)
    if len(pids) < 2:
        return False
    turn_pid = state.get("turn_pid")
    opp_pid = pids[0] if pids[1] == turn_pid else pids[1]
    try:
        new_phase = _ge.ETurnPhases.DeclareDefensePriorityWindow
    except Exception:
        new_phase = 15
    port_enter_phase(state, new_phase)
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
    from rules_port import lifecycle as _be
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
    port_enter_phase(state, new_phase)
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
    pl_t = _ge.UID.make(244, attacker_pid)
    ai_t = _ge.UID.make(244, defender_pid)
    # The attacker's chosen blocker order (weakest-to-toughest) captured from
    # the AssignDamageOrderTransaction — pass as order_map so damage is
    # assigned in that order (mirrors PvE).
    order_map = {int(k): [int(b) for b in v]
                 for k, v in (state.get("damage_order") or {}).items()}
    try:
        # PvP uses the same native combat algorithm as Practice/PvE. The
        # view adapter supplies the port's player/AI-shaped state keys; do not
        # re-enter the legacy ai.resolve_combat implementation here.
        view["player_attackers"] = attackers
        view["ai_blockers"] = blockers
        view["player_damage_order"] = order_map
        from rules_port.context import EffectContext
        from rules_port.combat_damage import resolve as resolve_native
        native_game = _ge.Game(int(session.session_id), pl_t, ai_t)
        view["_rules_port_attached"] = True
        context = EffectContext.from_rules_port(
            native_game, session, _db, handler, pl_t, ai_t, view,
            "", ability=None)
        view = resolve_native(
            context, first_strike=first_strike,
            attacker_key="player_attackers", blocker_key="ai_blockers")
        if native_game.events:
            _pvp_send_same_events(session, native_game, pl_t, ai_t)
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
        g2 = _ge.Game(int(session.session_id), pl_t, ai_t)
        g2.player_health = int(state.get(f"hp_{attacker_pid}", 20))
        g2.ai_health = int(state.get(f"hp_{defender_pid}", 20))
        from rules_port.context import EffectContext
        from rules_port.death_effects import state_based_deaths
        view["_rules_port_attached"] = True
        state_based_deaths(EffectContext.from_rules_port(
            g2, session, _db, handler, pl_t, ai_t, view,
            "state_based_death", ability=None))
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
    pids = db_game_session_pids(session.session_id)
    my_pid = int(handler.client_reck_id) if hasattr(handler, 'client_reck_id') else 0
    # The authenticated handler identity is authoritative.  A Ready request
    # can carry the other participant's packed UID after setup/reconnect; using
    # that field swaps the clients and makes a valid winner pick look invalid.
    wire_pid = my_pid
    player_uid_val = (wire_pid << 8) | 244
    resp_inner = None

    if len(pids) >= 2:
        opp_pid = pids[0] if pids[1] == my_pid else pids[1]
        wire_opp_pid = opp_pid
        opp_uid_val = (wire_opp_pid << 8) | 244

        # Coin flip — deterministic from session_id + both pids so both
        # 22027 calls (one per player) get the same result.
        sess_id = int(session.session_id) if isinstance(session.session_id, int) else 0
        h = hashlib.md5(f"{sess_id}:{pids[0]}:{pids[1]}".encode()).digest()
        goes_first_pid = pids[0] if h[0] & 1 else pids[1]
        goes_first_wire_pid = goes_first_pid
        goes_first_uid = (goes_first_wire_pid << 8) | 244
        goes_second_pid = pids[1] if goes_first_pid == pids[0] else pids[0]
        goes_second_wire_pid = goes_second_pid
        goes_second_uid = (goes_second_wire_pid << 8) | 244

        # Persist coin-flip winner so push_pvp_game_start reuses it.
        from services.tournament_game import pvp_load_state, pvp_save_state
        state = pvp_load_state(session) or pvp_default_state(goes_first_pid, goes_first_pid)
        state["goes_first_pid"] = goes_first_pid
        pvp_save_state(session, state)

        # TurnOrder: [first, second] — the player at index 0 goes first.
        turn_order_vals = [goes_first_uid, goes_second_uid]

        try:
            from binascii import hexlify as _hx
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
            # After BOTH setups are sent, tell the opponent the coin-flip
            # winner is choosing who goes first.
            try:
                state = pvp_load_state(session) or {}
                _pvp_push_waiting_on(
                    session, int(state.get("goes_first_pid") or 0) or None)
            except Exception as e:
                log_req(f"    PvP pick-goes-first wait push failed: {e}")
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
