"""Replay fetch and game-session player transactions.

Moved out of hconnect_server.HCPHandler._handle_service_request_legacy.
"""

from application.player_transactions import classify_player_transaction
from application.player_transactions import typed_payload_from_decoded
from application.practice_flow import _same_uid
from application.response_encoding import compress_gzip
from application.response_encoding import encode_datawrapper
from application.response_encoding import encode_objfmt_response
from application.response_encoding import make_uid
from db import log_req
from replay_db import db_complete_session_transaction
from replay_db import db_record_session_transaction
from replay_db import db_rules_port_state_snapshot
from replay_db import db_session_state_snapshot
from replay_db import set_session_transaction_trace_ids
import game_session
import hashlib
import json
import os


# ServiceGameSession ReplayFetch (160000)
def handle_servicegamesession_replayfetch_160000(handler, target, instance, data_type, reqid, comp, session_id,
                                                 conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    envelope = inner_obj.get("Envelope", b"{}")
    try:
        env_json = json.loads(envelope.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        env_json = {}
    if isinstance(env_json, dict) and env_json.get("action") == "replayfetch":
        from replay import replay_fetch
        resp_envelope = replay_fetch(env_json)
        _hc.log_req(
            f">>> ReplayFetch session={env_json.get('Session', '')} "
            f"offset={env_json.get('Offset', 0)} size={len(resp_envelope)}"
        )
    else:
        resp_envelope = b"\0\0\0\0\0"
        _hc.log_req(">>> ReplayFetch invalid request")
    resp_inner = _hc.encode_objfmt_response(
        ["Game.Shared.GameReq.GameReq+Response", "System.Byte[]"],
        [("Envelope", "bytes", resp_envelope)],
    )
    resp_body = _hc.compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = _hc.encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = (
        f"0.0.0.0.ServiceGameSession.{_hc.SERVICE_GAME_SESSION_UID}."
        f"{instance}.{resp_reqid}"
    )
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    _hc.log_req(f"    Sent ReplayFetch response ({len(dw_bytes)}b)")


# GameSession Service — PlayerTransaction (3029)
def handle_playertransaction_3029(handler, target, instance, data_type, reqid, comp, session_id,
                                  conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    player_uid = _hc.make_uid(_hc.UID_TYPE["ServicePlayer"], int(handler.client_reck_id))
    session = _hc.game_session.find_session_by_player(player_uid)
    sync_packets_before = int(getattr(
        handler, "_game_sync_send_count", 0))
    handler._ai_turn_depth = 0  # reset turn-advance recursion guard
    transaction_capture_id = None

    # Decode the raw transaction once at the application boundary;
    # execution below consumes typed flags without reparsing bytes.
    transaction = _hc.classify_player_transaction(inner_bytes)
    # Run labeled recovery even when nested ObjFmt parsing failed,
    # then reclassify with the real command flags so combat and
    # continuation-specific raw recovery rules are available.
    decoded_payload = _hc.typed_payload_from_decoded(
        transaction,
        inner_obj if isinstance(inner_obj, dict)
        else {"__raw__": inner_bytes})
    if decoded_payload:
        transaction = _hc.classify_player_transaction(
            inner_bytes, typed_payload=decoded_payload)
    txn_info = transaction.fields
    txn_id_str = txn_info.get("m_TransactionId", "?")
    txn_id_int = transaction.transaction_id
    _hc.set_session_transaction_trace_ids(
        transaction_id=txn_id_int,
        session_id=getattr(session, "session_id", None))
    quit_series = txn_info.get("m_QuitEntireSeries", "?")

    # PassPriorityTransaction.m_TurnPhase — the phase the client thinks it
    # is in. Used to ignore stale auto-passes from phases the server has
    # already auto-advanced past.
    pass_turn_phase = transaction.pass_turn_phase

    # Detect the client's phase-stop preference (SetTurnPhasesTransaction).
    is_set_stops = transaction.is_set_stops
    
    # Detect mulligan keep/redraw by transaction type name
    is_mulligan_keep = transaction.is_mulligan_keep
    is_mulligan_redraw = transaction.is_mulligan_redraw
    
    # Detect DebugCheatTransaction
    is_cheat = transaction.is_cheat

    # Detect priority pass (player clicked Pass Priority).
    is_pass_priority = transaction.is_pass_priority

    # Detect the Play/Draw pick (ChoosePlayTransaction / ChooseDrawTransaction).
    is_choose_pick = transaction.is_choose_pick

    is_discard = transaction.is_discard

    is_ability_activate = transaction.is_ability_activate

    # Follow-up to a class-23 AbilityActivationDataRequired prompt: the
    # player supplied targets (e.g. chose a card to discard) for an
    # ability already resolving.
    is_set_ability_data = transaction.is_set_ability_data

    # Combat: player declared attackers (CommitTroopsToAttackTransaction).
    is_commit_attack = transaction.is_commit_attack

    # Combat: player declared blockers during the AI's turn
    # (CommitTroopsToDefenseTransaction).
    is_commit_defense = transaction.is_commit_defense

    # F10 / "Skip": the client asks to auto-pass priority to the end of
    # its own turn (SetAutoPassTransaction, m_PassingState EndOfTurn=2 /
    # EndPhase=3 / Attack=1) or cancels it (CancelAutoPassTransaction).
    is_set_auto_pass = transaction.is_set_auto_pass
    is_cancel_auto_pass = transaction.is_cancel_auto_pass

    # Combat: player assigned damage order (AssignDamageOrderTransaction) —
    # the client auto-sends this at the AssignDamage step when no combat
    # has blockers (BattleStateAssignDamage auto-commits).
    is_assign_damage = transaction.is_assign_damage

    # Priority resync: the client lost priority tracking (e.g. a
    # TurnPhaseUpdated arrived without a preceding GreenLight) and asks the
    # server to re-send the current greenlight. Must be handled explicitly —
    # falling through to the card-play handler re-pushes MAIN-phase options
    # (making hand cards playable mid-combat) and clobbers attack options.
    is_priority_sync = transaction.is_priority_sync
    is_encounter_mod_dialog = transaction.is_encounter_mod_dialog

    # Preserve the inbound action before any resolver branch mutates
    # the authoritative session. This is intentionally separate from
    # session_events, which is the server-to-client replay stream.
    # Competitive sessions are captured by default. A local
    # practice/encounter trace is opt-in through
    # HEX_CAPTURE_SESSION_TRACES to avoid persistent private game
    # state during ordinary local play.
    is_replay_session = bool(
        session and (session.session_name or "").startswith(
            ("tourney-", "pvp-", "Challenge_")))
    capture_all_sessions = os.environ.get(
        "HEX_CAPTURE_SESSION_TRACES", "").strip().lower() in {
            "1", "true", "yes", "on"}
    if is_replay_session or capture_all_sessions:
        classification = {
            "fields": txn_info,
            "pass_turn_phase": pass_turn_phase,
            "is_set_stops": is_set_stops,
            "is_mulligan_keep": is_mulligan_keep,
            "is_mulligan_redraw": is_mulligan_redraw,
            "is_cheat": is_cheat,
            "is_pass_priority": is_pass_priority,
            "is_choose_pick": is_choose_pick,
            "is_discard": is_discard,
            "is_play_resource": transaction.is_play_resource,
            "is_play_troop": transaction.is_play_troop,
            "is_play_artifact": transaction.is_play_artifact,
            "is_play_spell": transaction.is_play_spell,
            "is_play_champion": transaction.is_play_champion,
            "is_ability_activate": is_ability_activate,
            "is_set_ability_data": is_set_ability_data,
            "is_commit_attack": is_commit_attack,
            "is_commit_defense": is_commit_defense,
            "is_set_auto_pass": is_set_auto_pass,
            "is_cancel_auto_pass": is_cancel_auto_pass,
            "is_assign_damage": is_assign_damage,
            "is_priority_sync": is_priority_sync,
            "is_encounter_mod_dialog": is_encounter_mod_dialog,
        }
        try:
            pre_state_json = _hc.db_session_state_snapshot(
                session.session_id)
            pre_rules_port_json = _hc.db_rules_port_state_snapshot(session)
            transaction_capture_id = _hc.db_record_session_transaction(
                session.session_id, player_uid, reqid, data_type, comp,
                txn_id_int,
                inner_obj.get("__type__", "")
                if isinstance(inner_obj, dict) else "",
                classification, inner_bytes,
                hashlib.sha256(pre_state_json.encode("utf-8")).hexdigest()
                if pre_state_json else "",
                raw_data_wrapper_bytes=raw_data_wrapper_bytes,
                decoded_intent=decoded_payload or {},
                pre_state_json=pre_state_json,
                pre_rules_port_json=pre_rules_port_json)
            _hc.set_session_transaction_trace_ids(
                transaction_row_id=transaction_capture_id)
        except Exception as capture_error:
            # Diagnostics must never make a live transaction fail.
            _hc.log_req(f"    Transaction capture failed: {capture_error}")

    _hc.log_req(f">>> PlayerTransaction (dt=3029) txId={txn_id_str} quit={quit_series} cheat={is_cheat} pass={is_pass_priority}")
    
    # Handle mulligan keep — push AcceptedStartingHand + phase sequence
    handled = False
    # Tournament PvP uses the native two-human RulesPort lifecycle
    # through its mode adapter, rather than the generic
    # AuthoritativeSession (whose turn_player/phase_idx snapshot
    # would overwrite the tournament turn_order schema).  Dispatch it
    # before the generic host: auto-attach is enabled by default, so
    # treating the deliberately absent generic host as an unhandled
    # command would otherwise acknowledge and discard every PvP
    # action before the tournament service sees it.
    if (session and
            (session.session_name or "").startswith("tourney-")):
        from services.tournament_game import (
            pvp_concede, pvp_handle_transaction, route_pvp_pass)
        try:
            # Setup requests use the dedicated PvP protocol handlers.
            # For gameplay, create the PvP-aware generic host first;
            # only use the old adapter if host construction genuinely
            # failed, preserving an explicit rollback boundary.
            if (getattr(session, "_rules_port_session", None) is None
                    and not (is_choose_pick or is_mulligan_keep or
                             is_mulligan_redraw)):
                from rules_port.persistence import load_state as _load_rules_state
                handler._fresh_game(session, player_uid,
                                 next((value for value, _position in
                                       (getattr(session, "players", ()) or ())
                                       if value != player_uid), player_uid),
                                 _load_rules_state(session))
            is_quit_transaction = (
                transaction.is_quit_game or
                "m_QuitEntireSeries" in txn_info or
                "m_Surrendered" in txn_info)
            if (getattr(session, "_rules_port_session", None) is not None
                    and not (is_choose_pick or is_mulligan_keep or
                             is_mulligan_redraw)):
                # The generic RulesPort dispatch below owns gameplay.
                # Leave this branch untouched so setup/rollback only
                # reaches the explicit cases following it.
                pass
            elif is_quit_transaction:
                handled = bool(pvp_concede(handler, session))
            elif is_choose_pick:
                # Play/Draw has a PvP-specific setup transition which
                # draws both hands and opens Mulligan. It is not a
                # gameplay card intent and must retain that protocol
                # projection until the generic PvP host is attached.
                handled = bool(handler._handle_choose_pick_transaction(
                    session, transaction))
            elif is_mulligan_keep:
                handled = bool(handler._handle_mulligan_keep_transaction(
                    session, transaction))
            elif is_mulligan_redraw:
                handled = bool(handler._handle_mulligan_redraw_transaction(
                    session, transaction))
            elif is_pass_priority:
                handled = bool(route_pvp_pass(handler, session))
            else:
                handled = bool(pvp_handle_transaction(
                    handler, session, transaction.inner_bytes,
                    typed_payload=getattr(
                        transaction, "typed_payload", None)))
        except Exception as _pvp_error:
            import traceback
            _hc.log_req(f"    PvP RulesPort adapter exception: {_pvp_error}")
            traceback.print_exc()
            handled = True
        if (handled and not is_pass_priority and
                not is_choose_pick and not is_mulligan_keep and
                not is_mulligan_redraw):
            handler._push_transaction_ack(session)
    if session and not handled:
        # Transaction dispatch precedes most event-building helpers.
        # Ensure Practice/PvP sessions have a host before attempting
        # the typed command.
        if getattr(session, "_rules_port_session", None) is None:
            from rules_port.persistence import load_state
            _rules_state = load_state(session)
            _rules_players = [p for p, _position in
                              (getattr(session, "players", ()) or ())]
            _rules_ai = next((p for p in _rules_players
                              if not _hc._same_uid(p, player_uid)), None)
            if _rules_ai is None:
                # Practice persists the human participant twice, so
                # the session list has no distinct opponent.  Fall
                # back to the canonical server-driven AI identity.
                # Using ``player_uid`` here built ``Game(pl, pl)`` and
                # every RulesPort closure derived from it reported the
                # human as the AI, projecting opponent cards onto the
                # local side.
                _rules_ai = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
            handler._fresh_game(session, player_uid, _rules_ai,
                             _rules_state)
        handled = handler._dispatch_rules_port_transaction(
            session, transaction, player_uid)
    if is_cheat and session and not handled:
        handled = handler._application.dispatch_player_transaction(
            transaction,
            lambda command: handler._handle_debug_cheat_transaction(
                session, command),
        )
    if not handled and session and session.state != "ended":
        native_boundary_intent = any(
            getattr(transaction, name, False) for name in (
                "is_play_resource", "is_play_troop",
                "is_play_artifact", "is_play_spell",
                "is_play_champion", "is_ability_activate",
                "is_activate_triggered_abilities",
                "is_set_ability_data", "is_discard",
                "is_ready_card", "is_commit_attack",
                "is_commit_defense", "is_assign_damage",
                "is_request_player_options", "is_state_checksum",
                "is_encounter_mod_dialog", "is_quit_game",
                "is_mulligan_keep", "is_mulligan_redraw",
                "is_set_stops", "is_set_auto_pass",
                "is_pass_priority", "is_choose_pick",
                "is_cancel_auto_pass", "is_priority_sync",
                "is_tip_window_closed"))
        # The native scheduler is the only ingress for a live
        # PlayerTransaction.  A classified request never falls through
        # to the legacy dispatcher, and neither does an unrecognized
        # one: the port cannot validate an intent it cannot decode, so
        # the request is acknowledged and dropped rather than
        # reinterpreted under a different phase/priority contract.  A
        # transaction class the client actually sends belongs in
        # classify_player_transaction, not in the legacy engine.
        _hc.log_req("    RulesPort boundary: refusing legacy fallback"
                + ("" if native_boundary_intent else
                   " (unclassified transaction)"))
        handler._push_transaction_ack(session)
        handled = True

    # Mono releases its ordinary 3029 request gate on any class-255
    # session packet. Some accepted intents are bookkeeping-only
    # (for example TipWindowClosed and the defunct checksum
    # transaction), and some compatibility projections can return
    # success without publishing events. Count only 3055 packets
    # successfully sent on this connection, from the start of the
    # request, so handler-owned packets are not duplicated and an
    # early packet in the same dispatch still satisfies completion.
    if (session and handled and
            int(getattr(handler, "_game_sync_send_count", 0)) ==
            sync_packets_before):
        _hc.log_req("    PlayerTransaction emitted no 3055; "
                "sending empty sync acknowledgement")
        handler._push_transaction_ack(session)

    # PlayerTransaction is fire-and-forget in the battle client. Its
    # SessionClient advances only after a 3055 sync packet, and the
    # client has no usable 3029 response handler for this server-side
    # transaction path. Sending the legacy 3029 response therefore
    # produces "Command handler not found for data wrapper type 3029"
    # and can strand the next transaction. Every handled path above
    # sends either its event packet or the empty 3055 acknowledgement.

    if transaction_capture_id is not None:
        try:
            post_state_json = _hc.db_session_state_snapshot(
                session.session_id)
            post_rules_port_json = _hc.db_rules_port_state_snapshot(session)
            _hc.db_complete_session_transaction(
                transaction_capture_id,
                hashlib.sha256(post_state_json.encode("utf-8")).hexdigest()
                if post_state_json else "",
                handled, post_state_json=post_state_json,
                post_rules_port_json=post_rules_port_json)
        except Exception as capture_error:
            _hc.log_req(f"    Transaction completion capture failed: {capture_error}")



LEGACY_HANDLERS = {
    160000: handle_servicegamesession_replayfetch_160000,
    3029: handle_playertransaction_3029,
}
