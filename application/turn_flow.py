"""Turn phases, priority advancement, rules-port turn steps and per-player stops."""

from dataclasses import replace
from typing import Any
from typing import cast
import json
import re
import struct


class TurnFlowMixin:
    """Methods of HCPHandler, mixed into it (see hconnect_server)."""

    def _dispatch_native_trigger(self, game, session, pl_t, ai_t, bstate,
                               event_type, source_card_id=None,
                               source_player_id=None, target_card_id=None,
                               target_player_id=None, **data):
        """Emit a gameplay event through the attached rules authority."""
        import hconnect_server as _hc
        from rules_port.triggers import dispatch_native_trigger
        return dispatch_native_trigger(
            db=_hc._db, handler=self, game=game, session=session,
            player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
            event_type=event_type, source_card_id=source_card_id,
            source_player_id=source_player_id,
            target_card_id=target_card_id,
            target_player_id=target_player_id, data=data)

    def _clear_expired_temporary_attributes(self, session, bstate, owner_id,
                                             boundary, clear_stat_buffs=False):
        import hconnect_server as _hc
        from rules_port.lifecycle import clear_expired_temporary_attributes
        return clear_expired_temporary_attributes(
            _hc._db, session.session_id, owner_id, boundary,
            clear_stat_buffs=clear_stat_buffs,
            battle_state=bstate, handler=self)

    def _dispatch_rules_port_transaction(self, session, command, player_uid):
        """Serialize tournament PvP dispatch under the per-session lock.

        Both clients run on their own connection thread and mutate the same
        native RulesPort object.  Without this lock a mulligan completion's
        first-turn phase drive (which holds the lock) can interleave with the
        other client's transaction, which then reads a mid-drive checkpoint and
        rolls the durable phase back — the first shard/card play is rejected by
        ``MainPhaseRequirement``/``PlayerHasPriorityRequirement``.
        """
        if session is not None and str(
                getattr(session, "session_name", "") or "").startswith(
                    "tourney-"):
            from services.tournament_game import pvp_session_lock
            with pvp_session_lock(session):
                return self._dispatch_rules_port_transaction_locked(
                    session, command, player_uid)
        return self._dispatch_rules_port_transaction_locked(
            session, command, player_uid)

    def _dispatch_rules_port_transaction_locked(self, session, command, player_uid):
        """Resolve one explicitly typed command through the native host."""
        import hconnect_server as _hc
        if session is None:
            return False
        session = cast(_hc.game_session.GameSession, session)
        port = getattr(session, "_rules_port_session", None)
        payload = getattr(command, "typed_payload", None)
        # Setup, mulligan, pass-priority, and priority-sync commands now enter
        # the same typed RulesPort normalization path as gameplay commands.
        # If a legacy client omits required typed fields, normalization still
        # safely returns None and the outer dispatcher can report that decode
        # gap without mutating state.
        if port is None:
            _hc.log_req("    RulesPort skipped: no attached session host")
            return False
        # The port is shared by both connections.  Pin the dispatch identity
        # for the whole locked transaction so every projection (resource,
        # threshold, charge, cost) is attributed to THIS player.
        if session is not None and str(
                getattr(session, "session_name", "") or "").startswith(
                    "tourney-"):
            try:
                port._pvp_current_handler = self
                port._pvp_current_session = session
            except (AttributeError, TypeError):
                pass
        # The shared battle checkpoint keeps ``phase_idx`` at the beginning
        # of the turn while the client is in the pre-game dialogs.  Mulligan
        # and play/draw requests are nevertheless authoritative phase
        # transitions, so hydrate the port from the transaction family before
        # applying its phase requirements.  Without this, the first Mulligan
        # request is rejected as StartTurn and the client remains dialog-less.
        setup_phase = None
        if (getattr(command, "is_mulligan_keep", False) or
                getattr(command, "is_mulligan_redraw", False)):
            setup_phase = _hc.game_engine.ETurnPhases.Mulligan
        elif getattr(command, "is_choose_pick", False):
            setup_phase = _hc.game_engine.ETurnPhases.PickGoesFirst
        # Older in-flight PVE sessions could already have shown Mulligan while
        # their native checkpoint still said NotPlaying. Repair only that
        # pre-turn checkpoint when native history confirms the preceding
        # Play/Draw choice and the same player still owns active and priority.
        # New setup packets save phases before transmission, so this only
        # bridges sessions created before that fix.
        if (setup_phase == _hc.game_engine.ETurnPhases.Mulligan and
                port.current_turn_phase == _hc.game_engine.ETurnPhases.NotPlaying):
            try:
                from rules_port.persistence import load_state as _load_setup_state
                setup_state = _load_setup_state(session)
                setup_player = port.coerce_transaction_player_id(player_uid)
                native_state = (setup_state.get("rules_port", {})
                                if isinstance(setup_state, dict) else {})
                history = (native_state.get("transaction_history", ())
                           if isinstance(native_state, dict) else ())
                chose_first = any(
                    isinstance(entry, dict) and entry.get("kind") in (
                        "choose_play_first", "choose_draw_first")
                    for entry in history)
                if (isinstance(setup_state, dict) and
                        not setup_state.get("pvp") and
                        not setup_state.get("turn_phases") and
                        chose_first and
                        _hc._same_uid(port.active_player_id, setup_player) and
                        _hc._same_uid(port.action_stack.priority_player_id,
                                  setup_player)):
                    self._sync_rules_port_setup_checkpoint(
                        session, setup_phase, setup_player)
            except Exception as exc:
                _hc.log_req(f"    RulesPort setup checkpoint recovery failed: {exc}")
        if setup_phase is not None:
            port.current_turn_phase = setup_phase
        # Clear orphaned continuation markers before any fresh transaction,
        # not only ability activations.  A prior AI discard must never consume
        # the player's next shard/card transaction.
        try:
            from rules_port import lifecycle as _be_clean
            clean_state = _be_clean.load_state(session)
            if (not clean_state.get("pending_discard_ability") and
                    not getattr(port, "pending_activation", None)):
                for key in ("rules_port_resume_effect_order", "resolution_paused",
                            "pending_discard_target_template", "pending_discard_scid"):
                    clean_state.pop(key, None)
                _be_clean.save_state(session, clean_state)
        except Exception:
            pass
        # Class-23 discard replies are serialized by Mono as a fresh
        # ActivateAbilityTransaction (child GUID) even though they continue
        # the waiting parent.  The port's in-memory pending marker may be
        # absent after a reconnect, so consult the authoritative battle
        # checkpoint before normalization and force the continuation shape.
        if getattr(command, "is_ability_activate", False):
            try:
                from rules_port import lifecycle as _be_wire
                wire_state = _be_wire.load_state(session)
                if wire_state.get("pending_discard_ability"):
                    # Mono emits the discard selection as a child
                    # ActivateAbilityTransaction.  The child GUID is only a
                    # UI helper; RulesPort must resume the waiting parent
                    # instance with the selected hand card.
                    pending = getattr(port, "pending_activation", None)
                    if not isinstance(pending, dict):
                        pending = {}
                    continuation_payload = dict(payload or {})
                    if pending.get("ability_instance_id") is not None:
                        continuation_payload["ability_instance_id"] = int(
                            pending["ability_instance_id"])
                    continuation_payload.setdefault("activation_data", {})
                    command = replace(command, is_ability_activate=False,
                                      is_set_ability_data=True,
                                      typed_payload=continuation_payload)
                    payload = continuation_payload
                    _hc.log_req("    RulesPort discard continuation: forcing SetAbilityActivationData "
                            f"instance={continuation_payload.get('ability_instance_id')}")
                elif not getattr(port, "pending_activation", None):
                    # A completed AI discard continuation must not poison the
                    # next player's fresh activation with its resume offset
                    # or paused marker (otherwise Stargazer draws nothing and
                    # appears to jump zones).
                    for key in ("rules_port_resume_effect_order",
                                "resolution_paused", "pending_discard_target_template",
                                "pending_discard_scid"):
                        wire_state.pop(key, None)
                    _be_wire.save_state(session, wire_state)
            except Exception:
                pass
        # Choice-card selections can also arrive as a child
        # ActivateAbilityTransaction. Treat them as continuations while the
        # metadata choice checkpoint is pending.
        if getattr(command, "is_ability_activate", False):
            try:
                from rules_port import lifecycle as _be_choice_wire
                if _be_choice_wire.load_state(session).get("pending_choice"):
                    command = replace(command, is_ability_activate=False,
                                      is_set_ability_data=True)
                    _hc.log_req("    RulesPort choice continuation: forcing SetAbilityActivationData")
            except Exception:
                pass
        # Class-23 discard continuations are consumed by the legacy hand
        # discard bridge below. They do not represent a live RulesPort
        # AbilityInstance, so do not reject them as missing port state.
        try:
            from rules_port import lifecycle as _be_discard_bridge
            if (getattr(command, "is_set_ability_data", False) and
                    _be_discard_bridge.load_state(session).get(
                        "pending_discard_ability")):
                payload = dict(payload or {})
                payload["_discard_continuation"] = True
                command = replace(command, typed_payload=payload)
        except Exception:
            pass
        # Trigger target selections emitted by the legacy metadata trigger
        # bridge are likewise persisted as a battle-state checkpoint, not as
        # a hydrated RulesPort AbilityInstance. Let the existing triggered
        # ability resolver consume these selections instead of rejecting them
        # on the port queue requirements after a reconnect/reattach.
        try:
            from rules_port import lifecycle as _be_trigger_bridge
            if getattr(command, "is_activate_triggered_abilities", False):
                _bridge_state = _be_trigger_bridge.load_state(session)
                # The "choose a card in your deck" picker is a class-39 prompt
                # too, so its answer arrives in this envelope (Scheme).  Mark
                # both pending prompts before normalization: otherwise the
                # non-triggered ability is reclassified as a fresh manual
                # activation and rejected by AbilityCanBeActivated.
                if (_bridge_state.get("pending_trigger") or
                        _bridge_state.get("pending_deck_search")):
                    payload = dict(payload or {})
                    payload["_triggered_continuation"] = True
                    command = replace(command, typed_payload=payload)
            elif getattr(command, "is_set_ability_data", False):
                _bridge_state = _be_trigger_bridge.load_state(session)
                if (_bridge_state.get("pending_trigger") or
                        _bridge_state.get("pending_deck_search")):
                    # Class-23 target/search answers are persisted host
                    # continuations rather than hydrated RulesPort ability
                    # instances. Route them through the continuation
                    # projection instead of trying to resume a missing native
                    # activation instance.
                    payload = dict(payload or {})
                    payload["_triggered_continuation"] = True
                    command = replace(command, typed_payload=payload)
        except Exception:
            pass
        facts = getattr(port, "runtime_facts", None)
        projected_game = None
        if facts is not None:
            # The PlayerId nested in PlayerTransactionRequestArgs is the
            # client/session namespace, while Game.player_uid is the service
            # projection namespace.  Preserve the request identity for
            # owner-side validation instead of treating the human as AI.
            facts.client_player_uid = player_uid
            # The port event sink owns the current Game projection; this
            # dispatch helper itself does not have a local ``game`` variable.
            projected_game = getattr(getattr(port, "event_sink", None),
                                     "game", None)
            def _valid_champion(value, fallback):
                try:
                    uid = getattr(value, "uid", value)
                    if int(getattr(uid, "uid64", uid) or 0) > 0:
                        return value
                except (TypeError, ValueError):
                    pass
                return fallback
            facts.player_champion_card_id = _valid_champion(
                getattr(projected_game, "player_champion_card_id", None),
                getattr(self, "_player_champ_scid", None))
            facts.ai_champion_card_id = _valid_champion(
                getattr(projected_game, "ai_champion_card_id", None),
                getattr(self, "_ai_champ_scid", None))
            # Synthetic champion cards are not in game_cards. Preserve the
            # exact ability catalog projected on each champion so a talent
            # charge power can pass the same source-card gate as a signature
            # champion ability.
            def _champion_ability_guids(card_id, fallback):
                card_def = getattr(projected_game, "card_defs", {}).get(card_id)
                values = tuple(str(value.guid).lower() for value in
                               (getattr(card_def, "abilities", ()) or ()))
                if values:
                    return values
                return tuple(str(getattr(value, "guid", value)).lower()
                             for value in (fallback or ()))
            facts.player_champion_ability_guids = _champion_ability_guids(
                facts.player_champion_card_id,
                getattr(self, "_player_champ_abilities", ()))
            facts.ai_champion_ability_guids = _champion_ability_guids(
                facts.ai_champion_card_id,
                getattr(self, "_ai_champ_ability_guids", ()))
        payload_required = any(getattr(command, name, False) for name in (
            "is_discard", "is_ready_card", "is_play_resource",
            "is_play_troop", "is_play_artifact", "is_play_spell",
            "is_play_champion", "is_ability_activate",
            "is_activate_triggered_abilities", "is_set_ability_data",
            "is_commit_attack", "is_commit_defense", "is_assign_damage",
            "is_set_stops", "is_set_auto_pass", "is_quit_game",
            "is_encounter_mod_dialog"))
        if payload is None and payload_required:
            _hc.log_req("    RulesPort skipped: decoder supplied no typed payload")
            # These transaction classes are inside the RulesPort migration
            # boundary.  Do not let a malformed/unsupported nested ObjFmt
            # envelope fall through to the legacy rules handler, where it
            # could mutate state without the port's phase/priority checks.
            self._push_transaction_ack(session)
            setattr(session, "_rules_port_dispatch_command", None)
            return True
        from rules_port.wire import (submit_classified_transaction,
                                     normalize_player_transaction)
        # Choice-card prompts are metadata continuations and therefore do not
        # have a live RulesPort AbilityInstance. Mark them for the dedicated
        # continuation resolver instead of applying SetAbility requirements.
        if (getattr(command, "is_set_ability_data", False) and
                isinstance(payload, dict)):
            try:
                from rules_port import lifecycle as _be_choice
                if _be_choice.load_state(session).get("pending_choice"):
                    payload = dict(payload)
                    payload["_choice_continuation"] = True
                    command = replace(command, typed_payload=payload)
            except Exception:
                pass
        # Mono's class-23 response commonly carries only the selected target
        # and omits AbilityInstanceId. Recover it only from an authoritative
        # pending continuation; never guess for a fresh activation.
        if (getattr(command, "is_set_ability_data", False) and
                isinstance(payload, dict) and
                payload.get("ability_instance_id") in (None, "")):
            continuation_instance = None
            pending = getattr(port, "pending_activation", None)
            if isinstance(pending, dict):
                continuation_instance = pending.get("ability_instance_id")
            try:
                if continuation_instance is None:
                    from rules_port import lifecycle as _be_cont
                    cont_state = _be_cont.load_state(session)
                    for key in ("pending_trigger", "pending_deck_search"):
                        marker = cont_state.get(key)
                        if (isinstance(marker, dict) and
                                marker.get("instance_id") is not None):
                            continuation_instance = marker.get("instance_id")
                            break
                    if (continuation_instance is None and
                            cont_state.get("pending_discard_ability")):
                        continuation_instance = cont_state.get(
                            "pending_discard_ability_instance_id", 1)
            except Exception:
                continuation_instance = None
            if continuation_instance is not None:
                payload = dict(payload)
                payload["ability_instance_id"] = int(continuation_instance)
                command = replace(command, typed_payload=payload)
                _hc.log_req("    RulesPort continuation instance recovered="
                        f"{payload['ability_instance_id']}")
        # Some client builds serialize a manual/quick ability activation using
        # the (misspelled) ActivateTriggeredAbiliesTransaction envelope.  The
        # envelope name is not authoritative: metadata marks whether the
        # ability is actually triggered.  Reclassify a single non-triggered
        # ability before normalization so QuickActions (for example Scheme)
        # are validated as ordinary activations rather than requiring a
        # pending triggered-ability queue.
        if (getattr(command, "is_activate_triggered_abilities", False) and
                isinstance(payload, dict)):
            try:
                ability_guid = str(payload.get("ability_template_id") or "").lower()
                activation = payload.get("activation_data")
                triggered = (_hc.db_ability_is_triggered(
                    ability_guid, conn=_hc._db) if ability_guid else None)
                if (triggered is False and
                        payload.get("source_card_id") is not None and
                        isinstance(activation, (dict, tuple, list))):
                    command = replace(command,
                                      is_ability_activate=True,
                                      is_activate_triggered_abilities=False,
                                      typed_payload=payload)
                    _hc.log_req("    RulesPort reclassified non-triggered ability "
                            f"{ability_guid} from triggered envelope")
            except Exception as exc:
                _hc.log_req(f"    RulesPort ability envelope check failed: {exc}")
        setattr(session, "_rules_port_dispatch_command", command)
        session._rules_port_mutation_emitted = False
        # Mono can submit the same ability activation twice on a rapid
        # double-click.  Once the first request has been accepted, the second
        # must be an idempotent acknowledgement; letting it fall through to
        # the legacy handler pays the cost/opens a second discard continuation
        # and leaves the RulesPort scheduler stranded.
        if getattr(command, "is_ability_activate", False):
            try:
                history = list(getattr(port, "transaction_history", ()) or ())
                last = history[-1] if history else None
                lp = (last or {}).get("payload", {})
                if (last and (last or {}).get("kind") == "activate_ability" and
                        (getattr(port, "pending_activation", None) or
                         bool(getattr(port, "action_stack", None) and port.action_stack.count)) and
                        int(lp.get("source_card_id", -1)) == int((payload or {}).get("source_card_id", -2)) and
                        str(lp.get("ability_template_id", "")).lower() ==
                        str((payload or {}).get("ability_template_id", "")).lower()):
                    _hc.log_req("    RulesPort ignored duplicate ability activation")
                    self._push_transaction_ack(session)
                    setattr(session, "_rules_port_dispatch_command", None)
                    return True
            except (TypeError, ValueError, AttributeError):
                pass
        if not submit_classified_transaction(
                port, command, player_uid, payload=payload):
            rejected = normalize_player_transaction(
                command, player_uid, current_phase=port.current_turn_phase,
                payload=payload)
            coerce_player = getattr(port, "coerce_transaction_player_id", None)
            intent_player = (coerce_player(player_uid)
                             if callable(coerce_player) else player_uid)
            if rejected is None:
                # The typed decoder could not build an intent (for example a
                # play transaction whose nested payload never decoded).  The
                # port has nothing to explain, so name the boundary directly.
                _hc.log_req("    RulesPort reject: unclassified transaction"
                        f" phase={port.current_turn_phase!r}"
                        f" player={player_uid!r}")
            _hc.log_req("    RulesPort boundary diagnostic:"
                    f" intent_phase="
                    f"{getattr(rejected, 'phase', None)!r}"
                    f" native_phase={port.current_turn_phase!r}"
                    f" player={player_uid!r}"
                    f" coerced={intent_player!r}"
                    f" in_session="
                    f"{_hc._uid_in(intent_player, port.player_ids)}"
                    f" terminated={port.terminated}"
                    f" native_priority="
                    f"{port.action_stack.priority_player_id!r}"
                    f" active={port.active_player_id!r}")
            if getattr(rejected, "kind", "") == "pass_priority":
                try:
                    action = port.action_stack.peek()
                    _hc.log_req(
                        "    RulesPort priority diagnostic: "
                        f"phase={port.current_turn_phase!r} "
                        f"stack_priority={port.action_stack.priority_player_id!r} "
                        f"action_priority={getattr(action, 'priority_player_id', None)!r} "
                        f"queue={list(getattr(action, '_priority_queue', ()) or ())!r} "
                        f"chain={list(getattr(port.chain, '_instance_ids', ()) or ())!r}")
                except Exception as diag_exc:
                    _hc.log_req(f"    RulesPort priority diagnostic failed: {diag_exc}")
            if (getattr(command, "is_ability_activate", False) and
                    projected_game is not None):
                try:
                    source = (payload or {}).get("source_card_id")
                    guid = (payload or {}).get("ability_template_id")
                    card = facts.get_card(source) if facts is not None else None
                    _hc.log_req(f"    RulesPort ability validation: source={source} guid={guid} "
                            f"card={card} owner={getattr(card, 'owner_id', None)} "
                            f"location={getattr(card, 'location', None)} abilities={getattr(card, 'abilities', None)}")
                except Exception as diag_exc:
                    _hc.log_req(f"    RulesPort ability diagnostic failed: {diag_exc}")
            setattr(session, "_rules_port_dispatch_command", None)
            # A decoder-owned typed card/ability intent is already inside the
            # migration boundary.  Do not let its RulesPort validation or
            # mutation rejection fall through to the legacy dispatcher: doing
            # so executes the same client request twice under different
            # phase/priority rules.
            # The empty acknowledgement also releases Mono's one-request
            # client gate while leaving authoritative state unchanged.
            rules_port_intent = any(
                getattr(command, name, False) for name in (
                    "is_play_resource", "is_play_troop", "is_play_artifact",
                    "is_play_spell", "is_play_champion",
                    "is_ability_activate", "is_activate_triggered_abilities",
                    "is_set_ability_data", "is_discard", "is_ready_card",
                    "is_commit_attack", "is_commit_defense", "is_assign_damage",
                    "is_request_player_options", "is_state_checksum",
                    "is_encounter_mod_dialog", "is_quit_game",
                    "is_mulligan_keep", "is_mulligan_redraw",
                    "is_set_stops", "is_set_auto_pass",
                    "is_pass_priority", "is_choose_pick",
                    "is_cancel_auto_pass", "is_priority_sync",
                    "is_tip_window_closed"))
            if rules_port_intent:
                options_resynced = (
                    getattr(command, "is_ability_activate", False) and
                    self._refresh_options_after_rejected_ability(
                        session, port, projected_game, player_uid))
                if not options_resynced:
                    self._push_transaction_ack(session)
                return True
            return False
        handled = port.handle_transaction()
        # ``handle_transaction`` only enqueues/mutates the port state.  Mirror
        # the C# session tick so a newly-created ability enters its action
        # stack immediately and can emit its activation checkpoint/continuation
        # in this same 3055 response.
        if handled:
            # A live HConnect session still owns the persisted battle phase.
            # On reconnect/reattach the port may have an empty action stack
            # even though the legacy state is sitting at a priority stop.  If
            # we call tick() first, AuthoritativeSession interprets that empty
            # stack as permission to advance FirstMain -> DeclareCombat before
            # it consumes the just-submitted activation.  Consume the queued
            # transaction first, then tick only the action stack created by
            # that transaction; never let the migration scheduler advance the
            # legacy phase on its own.
            for _ in range(16):
                if port.action_stack.count == 0:
                    break
                if not port.tick():
                    break
            # ``ResolveTopOfChainAction`` persists when it removes the chain
            # item, but the action stack itself is popped by the surrounding
            # ``GameActionStack.update`` immediately afterwards. Persist the
            # post-tick snapshot as well; otherwise a reconnect (or the next
            # transaction's reattach) sees a completed resolver still on the
            # stack and the client can remain unable to play another card.
            try:
                port.persist()
            except Exception as exc:
                _hc.log_req(f"    RulesPort post-tick persistence failed: {exc}")
        _hc.log_req(
            f"    RulesPort handled={handled} transaction={command.__class__.__name__}",
            level="DEBUG")
        if handled:
            game = getattr(getattr(port, "event_sink", None), "game", None)
            emitted = bool(getattr(session, "_rules_port_mutation_emitted", False))
            session._rules_port_mutation_emitted = False
            # While an interactive prompt is open (class-23 picker, triggered
            # target, deck search, ...) the prompt owns the client's UI.  The
            # scheduler's post-resolution events (GreenLight/TurnPhaseUpdated/
            # AbilityPushedOnChain) would replace it, so suppress them until
            # the prompt is answered.
            prompt_pending = False
            if (session.session_name or "").startswith("tourney-"):
                try:
                    from rules_port.persistence import load_state
                    _live = load_state(session) or {}
                    prompt_pending = any(_live.get(key) for key in (
                        "pending_choice", "pending_trigger",
                        "pending_deck_search", "pending_conversation",
                        "pending_discard_ability", "resolution_paused"))
                except Exception:
                    prompt_pending = False
            if game is not None and game.events:
                _hc.log_req(
                    "    RulesPort post-tick event sink: "
                    f"emitted={emitted} prompt_pending={prompt_pending} "
                    f"classes="
                    f"{[getattr(type(ev), 'CLASS_ID', 0) for ev in game.events]}")
            if game is not None and not emitted and not prompt_pending:
                    self._send_battle_events(session, game, player_uid)
            # A manual champion activation leaves a chain item pending.  The
            # native resolver emits the mutation packet, but the PvP adapter
            # still owns the client-facing response window.  Project the
            # current ResolveTopOfChain GreenLight to both clients or the
            # charge is paid while neither client receives a pass window.
            if (getattr(command, "is_ability_activate", False) and
                    (session.session_name or "").startswith("tourney-")):
                try:
                    from services.tournament_game import (
                        _pvp_populate_game_state, _send_pvp_packet,
                        player_handlers, pvp_load_state,
                    )
                    live_pvp = pvp_load_state(session) or {}
                    if live_pvp.get("stack") or not port.chain.is_empty:
                        pids = [int(value) for value in
                                (live_pvp.get("pids") or ())]
                        priority_pid = int(live_pvp.get("priority_pid") or 0)
                        if len(pids) == 2 and priority_pid in pids:
                            phase = port.current_turn_phase
                            turn_pid = int(live_pvp.get("turn_pid") or
                                            priority_pid)
                            for target_pid in pids:
                                target_h = player_handlers.get(target_pid)
                                if not target_h:
                                    continue
                                other_pid = next(pid for pid in pids
                                                 if pid != target_pid)
                                target_uid = _hc.game_engine.UID.make(244, target_pid)
                                other_uid = _hc.game_engine.UID.make(244, other_pid)
                                window = _hc.game_engine.Game(
                                    int(session.session_id), target_uid,
                                    other_uid)
                                _pvp_populate_game_state(
                                    window, live_pvp, target_pid, other_pid)
                                # UIBattle checks its local HasPriority bit
                                # when TurnPhaseUpdated arrives. Send the
                                # matching GreenLight first so a phase update
                                # cannot trigger a stale priority resync.
                                window.push_green_light(
                                    _hc.game_engine.UID.make(244, priority_pid),
                                    _hc.game_engine.EPriorityContext.ResolveTopOfChain)
                                window.push_turn_phase(
                                    phase,
                                    _hc.game_engine.UID.make(244, turn_pid),
                                    _hc.game_engine.UID.make(244, priority_pid))
                                _send_pvp_packet(
                                    target_h, session, window, target_uid,
                                    "rules-port-ability-priority")
                            _hc.log_req(
                                f"    PvP RulesPort ability priority window: "
                                f"pid={priority_pid} phase={phase}")
                except Exception as exc:
                    _hc.log_req(
                        f"    PvP RulesPort ability priority projection failed: {exc}")
            # Practice/PvE has the same native chain semantics as tournament
            # PvP, but only one client projection.  The activation tick emits
            # the charge/card updates; replace the stale main-phase options
            # with a chain-only response window while the ability is pending.
            if (getattr(command, "is_ability_activate", False) and
                    not (session.session_name or "").startswith("tourney-") and
                    getattr(port, "chain", None) is not None and
                    not port.chain.is_empty):
                try:
                    self._push_practice_ability_chain_window(session, port, game)
                except Exception as exc:
                    _hc.log_req(
                        f"    RulesPort Practice ability priority projection failed: {exc}")
            # A completed manual ability returns to the same main-phase
            # priority window.  The legacy ability handler rebuilt the
            # PlayerOptionList at that boundary, but the RulesPort action
            # resolver only emits its mutation/GreenLight events.  Without a
            # fresh option list Unity keeps the pre-activation list (or an
            # empty chain list), making every card appear unplayable until a
            # later phase packet happens to refresh it.
            #
            # Keep this generic and conservative: a pending continuation,
            # port chain item, or legacy trigger stack means the client is
            # still in a response/chain checkpoint and must not receive main
            # phase card options yet.
            if any(getattr(command, name, False) for name in (
                    "is_ability_activate", "is_play_resource",
                    "is_play_troop", "is_play_artifact", "is_play_spell",
                    "is_play_champion", "is_set_ability_data")):
                try:
                    from rules_port.persistence import load_state
                    settled_state = load_state(session)
                    main_phase = port.current_turn_phase in (
                        _hc.game_engine.ETurnPhases.FirstMainPhase,
                        _hc.game_engine.ETurnPhases.SecondMainPhase)
                    settled = (
                        main_phase and
                        not getattr(port, "pending_activation", None) and
                        getattr(port, "chain", None) is not None and
                        port.chain.is_empty and
                        not settled_state.get("stack") and
                        not any(settled_state.get(key) for key in (
                            "pending_choice", "pending_trigger",
                            "pending_deck_search", "pending_discard_ability",
                            "pending_conversation", "resolution_paused")))
                    if settled:
                        _hc.log_req("    RulesPort post-ability main-phase options "
                                "refresh (settled)")
                        if (session.session_name or "").startswith(
                                "tourney-"):
                            from services.tournament_game import (
                                pvp_load_state,
                                pvp_push_main_phase_options)
                            pvp_push_main_phase_options(
                                session, pvp_load_state(session) or {})
                        elif game is not None:
                            self._push_main_phase_options(
                                session, game.player_uid, game.ai_uid)
                except Exception as exc:
                    _hc.log_req(f"    RulesPort option refresh failed: {exc}")
        else:
            from rules_port.wire import describe_transaction
            described = normalize_player_transaction(
                command, player_uid, current_phase=port.current_turn_phase,
                payload=payload)
            _hc.log_req("    RulesPort handler reject: "
                    f"{describe_transaction(described, port)} "
                    f"phase={getattr(port, 'current_turn_phase', None)} "
                    f"priority={getattr(getattr(port, 'action_stack', None), 'priority_player_id', None)} "
                    f"action={type(getattr(getattr(port, 'action_stack', None), 'peek', lambda: None)()).__name__}")
            if getattr(command, "is_ability_activate", False):
                self._refresh_options_after_rejected_ability(
                    session, port, projected_game, player_uid)
        setattr(session, "_rules_port_dispatch_command", None)
        return bool(handled)

    def _publish_application_events(self, events):
        """Publish events after an application command has committed.

        Session removal currently needs only an audit/log notification. Game
        commands will extend this callback to route committed domain events
        to the appropriate client event projection.
        """
        import hconnect_server as _hc
        for event in events:
            _hc.log_req(f"    Committed application event: {event!r}")

    def _refresh_options_after_rejected_ability(
            self, session, port, projected_game, request_player):
        """Restore the authoritative main-phase options after a stale click.

        Unity enables manual abilities from its cached PlayerOptionList. If a
        player submits an ability after its cost became unaffordable, RulesPort
        rejects the activation; replace the cached list while the same player
        still owns an ordinary main-phase priority window.
        """
        import hconnect_server as _hc
        if (projected_game is None or
                (session.session_name or "").startswith("tourney-")):
            return False
        try:
            coerce_player = getattr(
                port, "coerce_transaction_player_id", None)
            intent_player = (coerce_player(request_player)
                             if callable(coerce_player) else request_player)
            main_phase = port.current_turn_phase in (
                _hc.game_engine.ETurnPhases.FirstMainPhase,
                _hc.game_engine.ETurnPhases.SecondMainPhase)
            action = port.action_stack.peek()
            player_has_priority = (
                _hc._same_uid(intent_player, port.action_stack.priority_player_id)
                and _hc._same_uid(intent_player, port.active_player_id))
            from rules_port.lifecycle import load_state
            battle_state = load_state(session)
            waiting_for_input = (
                bool(getattr(port, "pending_activation", None)) or
                any(battle_state.get(key) for key in (
                    "pending_choice", "pending_trigger", "pending_deck_search",
                    "pending_discard_ability", "pending_conversation",
                    "resolution_paused")))
            if not (
                    main_phase and player_has_priority and
                    type(action).__name__ == "PriorityWindowAction" and
                    getattr(getattr(port, "chain", None), "is_empty", True) and
                    not waiting_for_input):
                return False
            player_uid = getattr(projected_game, "player_uid", None)
            ai_uid = getattr(projected_game, "ai_uid", None)
            if player_uid is None or ai_uid is None:
                return False
            sent = self._push_main_phase_options(
                session, player_uid, ai_uid)
            if sent:
                _hc.log_req("    RulesPort rejected stale ability click; "
                        "refreshed main-phase options")
            return bool(sent)
        except Exception as refresh_exc:
            _hc.log_req("    RulesPort rejected ability option refresh failed: "
                    f"{refresh_exc}")
            return False

    def _hand_card_playable(self, session, card_uid, ct, cost, thresh_json,
                            ability_guids, resources, threshold, resource_played,
                            friendly_count, enemy_count):
        """A hand card is playable iff:
          - Resource: not already played one this turn.
          - Any other type: affordable (cost <= resources) AND thresholds met.
          - Its troop-target requirement has valid targets (e.g. a buff needing
            a friendly troop when you have none is not playable).
          - Its additional sacrifice cost (e.g. Abominate "sacrifice a troop you
            control") can be paid — not playable with no eligible troops."""
        import hconnect_server as _hc
        if ct == 'Resource':
            return not resource_played
        if cost is not None and cost > resources:
            if cost - self._mobilize_discount(session, card_uid) > resources:
                return False
        if not self._thresholds_met(thresh_json, threshold):
            from rules_port.static_rules import champion_int_attribute
            battle_state = getattr(self, "_current_bstate", None)
            if not battle_state:
                try:
                    from rules_port import lifecycle as _lifecycle
                    battle_state = _lifecycle.load_state(session)
                except Exception:
                    battle_state = {}
            owner = (int(self.user_profile.get("id", 0) or 0)
                     if isinstance(self.user_profile, dict) else 0)
            if champion_int_attribute(
                    battle_state, owner,
                    "CanIgnoreCardsThresholds") <= 0:
                return False
        card_details = _hc.db_card_zone_details(
            session.session_id, card_uid, conn=_hc._db)
        play_plan = self._card_play_plan(
            card_details[0] if card_details else "", card_uid,
            self.user_profile["id"] if self.user_profile else 0,
            session=session)
        if play_plan is None:
            return False
        # Zone-bound explicit targets (e.g. Countermagic's CastSpells-only
        # "Interrupt target card") must have a legal candidate to be playable.
        if not self._card_target_requirements_met(
                session, play_plan,
                battle_state=getattr(self, "_current_bstate", None)):
            return False
        # Every non-automatic card-level cost must have enough legal targets
        # before the card is offered.  This mirrors the client's
        # GetPotentialCostsForAbility/AreXCostsComplete gates.
        if play_plan.cost_instances:
            from rules_port.costs import card_cost_targets
            for cost in card_cost_targets(
                    play_plan, _hc._db, session.session_id,
                    self.user_profile["id"] if self.user_profile else 0,
                    card_uid, champions=self._champion_targets(),
                    battle_state=getattr(self, "_current_bstate", None)):
                if (not cost.is_source_auto_target and
                        len(cost.candidates) < cost.minimum):
                    return False
        return True

    def _push_main_phase_options(self, session, pl_t, ai_t, game=None,
                                 refresh_warzone=True):
        """Push the playable-card options (golden outlines) for a main phase.

        Computes which cards in the player's hand are affordable from the
        DB-backed battle state and pushes a PlayerOptionList on its own packet.
        Shards: playable if resource not yet played this turn.
        Troops: playable if cost <= available resources AND all threshold
        requirements are met.
        """
        import hconnect_server as _hc
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        from abilities.framework._shared import owner_uid
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        # A card/resource mutation can arrive after the port session was
        # reattached with an empty action stack.  Recreate the client-facing
        # main-phase priority checkpoint before emitting options; otherwise
        # the subsequent PassPriority has no PriorityWindowAction to consume.
        port = getattr(session, "_rules_port_session", None)
        native_chain = getattr(port, "chain", None)
        chain_pending = (
            not getattr(native_chain, "is_empty", True)
            if native_chain is not None else bool(bstate.get("stack")))
        waiting_for_input = (
            bool(getattr(port, "pending_activation", None)) or
            any(bstate.get(key) for key in (
                "pending_choice", "pending_trigger", "pending_deck_search",
                "pending_conversation", "pending_discard_ability",
                "resolution_paused")))
        if waiting_for_input:
            # A picker owns the client's UI state. Leave its options intact.
            return
        if chain_pending:
            # Ordinary cards are illegal until the chain empties. Replace any
            # stale main-phase list with the current priority-window options.
            self._push_phase_options_empty(
                session, pl_t, ai_t,
                refresh_warzone=refresh_warzone)
            return
        if (port is not None and
                getattr(port, "action_stack", None) is not None and
                port.action_stack.count == 0 and
                port.current_turn_phase in (
                    _hc.game_engine.ETurnPhases.FirstMainPhase,
                    _hc.game_engine.ETurnPhases.SecondMainPhase)):
            from collections import deque
            from rules_port.kernel import PriorityWindowAction, TurnPhasePlayers
            priority = PriorityWindowAction(TurnPhasePlayers.ACTIVE)
            port.action_stack.push(priority)
            # The host drives the AI internally; expose one client-facing
            # pass and let the normal phase/AI driver continue afterwards.
            priority._priority_queue = deque([pl_t])
            port.action_stack.priority_player_id = pl_t
        resources = bstate.get("player_resources", 0)
        threshold = bstate.get("player_threshold", {})
        from rules_port.resources import can_play_resource, resource_play_limit
        resource_limit = resource_play_limit(
            _hc._db, session.session_id, bstate,
            int(self.user_profile.get("id", 0)))
        resource_played = not can_play_resource(
            bstate, "player", limit=resource_limit)
        friendly_count = self._warzone_troop_count(session, self.user_profile["id"])
        enemy_count = self._warzone_troop_count(session, 0)
        from pvp_db import (db_hand_cards_with_templates,
                            db_deck_top_card_details,
                            db_card_play_info,
                            db_card_template_thresholds)
        playable = []
        abilities = []
        owner_id = int(self.user_profile["id"])
        rows = list(db_hand_cards_with_templates(
            session.session_id, owner_id))
        from rules_port.static_rules import player_int_attributes
        player_attrs = player_int_attributes(
            _hc._db, session.session_id, bstate, owner_id)
        if int(player_attrs.get("CanPlayTopOfDeck", 0) or 0) > 0:
            top = db_deck_top_card_details(
                session.session_id, owner_id, conn=_hc._db)
            if top:
                top_uid = int(top[1])
                info = db_card_play_info(
                    session.session_id, top_uid, conn=_hc._db)
                if info:
                    threshold_row = db_card_template_thresholds(
                        info[0], conn=_hc._db)
                    top_template = _hc.db_template_by_guid(info[0])
                    rows.append((
                        top_uid, top_template[3] if top_template else 0, info[1],
                        threshold_row[0] if threshold_row else None,
                        info[5]))
        # Enlightened Seeker: "You can't play cards." — a continuous static
        # ability; no hand card is playable while the controller has one in play.
        from rules_port.static_rules import controller_flags
        if "cant_play_cards" in controller_flags(
                _hc._db, session.session_id, bstate, self.user_profile["id"]):
            rows = []
        for card_uid, cost, ct, thresh_json, abilities_json in rows:
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(card_uid))
            # Use the card's EFFECTIVE cost (base + permanent/triggered cost
            # modifiers, e.g. Fury of the Mountain God's "-1 per damage dealt"
            # while in hand) — the raw template cost would wrongly hide a
            # discounted card.
            from rules_port.static_rules import effective_cost
            try:
                cost = effective_cost(_hc._db, session.session_id, bstate, card_uid)
            except Exception:
                pass
            ability_guids = []
            if abilities_json:
                try:
                    ability_guids = [g.lower() for g in json.loads(abilities_json)]
                except Exception:
                    pass
            if self._hand_card_playable(session, card_uid, ct, cost, thresh_json,
                                        ability_guids, resources, threshold,
                                        resource_played, friendly_count, enemy_count):
                playable.append(scid)
            else:
                _hc.log_req(f"    Playability: skip "
                        f"{_hc.card_label(session.session_id, card_uid)} "
                        f"type {ct} "
                        f"(cost {cost} > resources {resources} / thresholds / no troop target)")
        game2 = game or self._fresh_game(session, pl_t, ai_t, bstate)
        game2.push_options(pl_t, playable)
        # Attach targeting TargetInstances so the client opens the target picker
        # for played spells (BattleStatePlayCard -> CanUseAbility).
        self._add_play_target_options(game2, session, pl_t, ai_t)
        # Include champion ability options (charge/spell powers) so the
        # client's PlayerOptions.m_Targets is populated and CanActivateAbility
        # returns true for abilities the champion can afford.
        player_champ_cid = getattr(self, "_player_champ_scid", None)
        if player_champ_cid:
            abilities = getattr(self, "_player_champ_abilities", [])
            if abilities:
                champ_uid = (player_champ_cid.uid.to_uint64()
                             if hasattr(player_champ_cid, "uid") else 0)
                champ_targets = self._champion_ability_targets(
                    session, abilities, champ_uid)
                champ_costs = self._champion_ability_costs(
                    session, abilities, champ_uid)
                game2.add_champion_to_options(
                    pl_t, player_champ_cid,
                    self._filter_affordable_abilities(abilities, bstate,
                                                      _be.current_phase(bstate),
                                                      target_data=champ_targets,
                                                      cost_data=champ_costs,
                                                      session=session),
                    self._discard_costs_for(session, abilities),
                    champ_targets, champ_costs)
        # Warzone-troop manual abilities (e.g. Shift): light up as Activate.
        affordable = self._affordable_troop_abilities(session, bstate)
        if affordable:
            self._add_troop_ability_options(game2, pl_t, session, affordable,
                                             bstate)
        # Always carry a fresh PlayerUpdated so the client's resource/charge/SP
        # display reflects the real battle state (a bare Game defaults them to
        # 0, wiping the UI).
        game2.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game2.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        if refresh_warzone:
            self._push_warzone_card_updates(game2, session, pl_t, ai_t)
        # Options are sometimes delivered as the packet immediately after a
        # choice continuation. Repeat the authoritative priority event in the
        # same packet so the client's one-request UI state cannot remain
        # stranded on the finished choice dialog.
        game2.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
        sent = self._send_battle_events(session, game2, pl_t)
        if sent:
            _hc.log_req(f"    Pushed main-phase options ({len(playable)} playable, champ_ab={len(abilities) if player_champ_cid and abilities else 0}, troop_ab={sum(len(v) for v in affordable.values())})")
        return sent

    def _extract_transaction_targets(self, inner_bytes, exclude_uid):
        """Extract Card-type UIDs from a 3029 transaction's TargetMap (the
        picked targets), excluding the source/played card."""
        targets = []
        if not isinstance(inner_bytes, bytes):
            return targets
        for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
            try:
                uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                if (uid64 & 0xFF) == 1 and uid64 != int(exclude_uid or 0):
                    targets.append(int(uid64))
            except Exception:
                continue
        return targets

    def _max_hand_size(self, session):
        """Return the effective maximum hand size for the active battle.

        Client ``Player.MaximumHandSize``: ten in PvE/PvE Arena, seven in PvP
        (tournament) sessions.  Campaign class/talent metadata can raise or
        lower the PvE value through ``MaximumHandSizeModifiers``.
        """
        try:
            if (session.session_name or "").startswith("tourney-"):
                return 7
        except Exception:
            pass
        return int(getattr(self, "_campaign_max_hand_size", 10))

    def _starting_hand_size(self, session):
        """Return the effective opening-hand count for the active battle."""
        try:
            if (session.session_name or "").startswith("camp_"):
                return int(getattr(self, "_campaign_starting_hand_size", 7))
        except Exception:
            pass
        return 7

    def _hand_size_unlimited(self, session, bstate):
        """Whether a champion passive currently removes the hand limit.

        Champions have no ``game_cards`` row, so ``champ_guid_map`` carries
        their template GUIDs for the continuous-static scan.  Games started
        before that key existed are repaired here from the handler's champion
        identities so an in-progress battle picks the flag up immediately.
        """
        import hconnect_server as _hc
        state = bstate if isinstance(bstate, dict) else {}
        if not state.get("champ_guid_map"):
            guid_map = {}
            owner = 0
            if isinstance(self.user_profile, dict):
                owner = int(self.user_profile.get("id", 0) or 0)
            player_guid = getattr(self, "_player_champ_guid", None)
            ai_guid = getattr(self, "_ai_champ_guid", None)
            if player_guid:
                guid_map[str(owner)] = player_guid
            if ai_guid:
                guid_map["0"] = ai_guid
            if guid_map:
                state["champ_guid_map"] = guid_map
        from rules_port.static_rules import hand_size_unlimited
        return hand_size_unlimited(_hc._db, session.session_id, state)

    def _next_resolve_counter(self, session):
        """Increment and return the resolve counter from battle state.
        Used to stamp resolved_at on cards entering the warzone."""
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        c = bstate.get("resolve_counter", 0) + 1
        bstate["resolve_counter"] = c
        _be.save_state(session, bstate)
        return c

    def _fresh_game(self, session, pl_t, ai_t, bstate):
        """A fresh Game event-builder fully populated from the DB battle state.

        Fresh-per-push is the thread-safe pattern: a Game holds only transient
        event/def data; the authoritative state is in the DB. This helper copies
        EVERY live value (both players: resources, threshold, health, charges,
        spell points) into the fresh Game so any PlayerUpdated it emits reports
        the real numbers — not the 20/0 defaults of a bare Game(). Use this for
        any phase/priority path that pushes PlayerUpdated.
        """
        import hconnect_server as _hc
        game = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
        game.max_hand_size = self._max_hand_size(session)
        game.player_resources = bstate.get("player_resources", 0)
        game.player_total_resources = bstate.get("player_total_resources", 0)
        game.player_threshold = dict(bstate.get("player_threshold", {}))
        game.player_charges = bstate.get("player_charges", 0)
        game.player_spell_points = bstate.get("player_spell_points", 0)
        game.ai_resources = bstate.get("ai_resources", 0)
        game.ai_total_resources = bstate.get("ai_total_resources", 0)
        game.ai_threshold = dict(bstate.get("ai_threshold", {}))
        game.ai_charges = bstate.get("ai_charges", 0)
        game.ai_spell_points = bstate.get("ai_spell_points", 0)
        game.player_health = bstate.get("player_health", 20)
        game.ai_health = bstate.get("ai_health", 10)
        # Carry the champion SessionCardIds onto this fresh Game so a
        # push_player_updated that falls back to player_champion_card_id /
        # ai_champion_card_id (i.e. no explicit champ_id) still emits a VALID
        # champion id — without this it serializes UID type 0 (undefined),
        # the client stores SessionCardId.Invalid as ChampionSessionCardId,
        # and UIBattle.OnTurnPhaseUpdated KeyNotFound-crashes on it.
        game.player_champion_card_id = getattr(self, "_player_champ_scid", None)
        game.ai_champion_card_id = getattr(self, "_ai_champ_scid", None)
        # Deck sleeves are battle state, not event state: re-apply them to every
        # fresh projection so a PlayerUpdated for either side always carries the
        # sleeve the client's card backs depend on.
        for key, uid, cached in (
                ("player_deck_sleeve_id", pl_t, "_player_deck_sleeve_guid"),
                ("ai_deck_sleeve_id", ai_t, "_ai_deck_sleeve_guid")):
            sleeve = bstate.get(key) or getattr(self, cached, None)
            if sleeve:
                game.set_deck_sleeve(uid, sleeve)
        # Visibility permissions are persisted in battle state, but each
        # network checkpoint uses a fresh Game projection. Re-apply the
        # persisted map here or an underground Subterranean Spy appears to
        # lose opponent-hand visibility at the next phase transition.
        from rules_port.visibility import (
            apply_player_visibility_to_game, project_visible_hands)
        apply_player_visibility_to_game(game, bstate)
        # A fresh packet buffer must carry full opposing-hand definitions when
        # an underground Subterranean Spy permission is already active. The
        # persisted flag alone updates PlayerUpdated but leaves Unity's card
        # cache as black/nulling rectangles until a full CardUpdated arrives.
        project_visible_hands(
            _hc._db, session, self, game, pl_t, ai_t, bstate)
        # Attach the semantic rules host on every normal game build,
        # including Practice.  Combat-only construction is too late for
        # card-play transactions and leaves the dispatcher without a host.
        self._maybe_attach_rules_port(session, game, bstate)
        # _maybe_attach_rules_port can repair a partial legacy checkpoint (or
        # replace an attach-time projection with the shared authoritative
        # dictionary). Refresh the projection after that boundary; otherwise
        # this fresh Game would retain the pre-repair zero-health value in its
        # PlayerUpdated event even though the checkpoint is now valid.
        game.player_health = bstate.get("player_health", 20)
        game.ai_health = bstate.get("ai_health", 10)
        return game

    def _push_warzone_card_updates(self, game, session, pl_t, ai_t):
        """Re-push CardUpdateds for ALL warzone cards (both players) so the
        client's card icons/abilities/state always match the DB (MVC: model ->
        view). Called at the end of every phase/options push, mirroring the
        PlayerUpdated refresh — a shifted ability/attribute that changed the
        DB row is reflected on the board immediately.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        from abilities.framework._shared import owner_uid
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        voided_by = (bstate or {}).get("voided_by") or {}
        # A card that is the SOURCE of a pending chain item is currently shown
        # on the chain (CastSpells visual); re-pushing its warzone CardUpdated
        # here makes the client yank the chain image while the Resolve button
        # stays — the "empty chain" the user saw.  Leave it alone until the
        # chain item resolves (the resolution re-pushes it anyway).
        chain_sources = {int(i.get("source_uid") or 0)
                         for i in ((bstate or {}).get("stack") or [])
                         if i.get("source_uid")}
        rows = _hc.db_warzone_display_rows(session.session_id, conn=_hc._db)
        projections = _hc.db_card_instance_dynamic_projections(
            session.session_id, [row[0] for row in rows], conn=_hc._db)
        for card_uid, tpl_guid, user_id, cstate, _card_type in rows:
            if int(card_uid) in chain_sources:
                continue
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(card_uid)))
            _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(
                game, scid, tpl_guid,
                instance_projection=projections.get(int(card_uid)))
            cdef = game.card_defs.get(scid)
            attrs = cdef.attributes if cdef else 0
            owner = owner_uid(user_id, pl_t, ai_t, bstate)
            related = [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(v)))
                       for v in (voided_by.get(str(int(card_uid))) or [])]
            game.push_card_updated(scid, owner, _hc.game_engine.ECardCollections.Warzone, ct,
                                   template_id=tpl_guid, attributes=attrs,
                                   state=int(cstate or 0), related_cards=related)
        self._push_hand_cost_updates(game, session, pl_t, bstate)

    def _push_hand_cost_updates(self, game, session, pl_t, bstate):
        """Show the current effective cost of the player's hand cards.

        Static cost changes that depend on the board (Pterobot: "cost -1 for
        each Dwarf and/or Robot you control") were charged correctly but the
        card in hand kept showing its printed cost.  Push a hand card whenever
        its effective cost differs from the cost the client last saw.
        """
        import hconnect_server as _hc
        if not self.user_profile:
            return
        from pvp_db import db_hand_cards_with_templates
        from rules_port.static_rules import effective_cost
        sent = getattr(self, "_hand_cost_sent", None)
        if sent is None or sent.get("_session") != session.session_id:
            sent = self._hand_cost_sent = {"_session": session.session_id}
        rows = db_hand_cards_with_templates(
            session.session_id, self.user_profile["id"])
        in_hand = set()
        for card_uid, base_cost, ct, _thresh, _abilities in rows:
            card_uid = int(card_uid)
            in_hand.add(card_uid)
            try:
                cost = int(effective_cost(_hc._db, session.session_id, bstate,
                                          card_uid))
            except Exception:
                continue
            if cost == sent.get(card_uid, int(base_cost or 0)):
                continue
            basic = _hc.db_card_basic(session.session_id, card_uid)
            if not basic or not basic[0]:
                continue
            tpl_guid = basic[0]
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(card_uid))
            _tpl, ctype, _n, _c, attack, defense, gems = self._card_full_data(
                game, scid, tpl_guid)
            game.push_card_updated(
                scid, pl_t, _hc.game_engine.ECardCollections.Hand, ctype,
                template_id=tpl_guid, cost=cost, attack=attack,
                defense=defense, gems=gems)
            sent[card_uid] = cost
        for card_uid in [uid for uid in sent if uid != "_session"
                         and uid not in in_hand]:
            sent.pop(card_uid, None)

    def _apply_rules_port_prep(self, session, projection_game, bstate):
        """Project the native Prep lifecycle for the active controller."""
        import hconnect_server as _hc
        from rules_port.lifecycle import (build_turn_phases,
                                          clear_expired_temporary_attributes,
                                          ready_cards_for_turn, reset_armor)
        from rules_port.resources import project_resource_change

        pl_t, ai_t = projection_game.player_uid, projection_game.ai_uid
        is_player = bstate.get("turn_player") == "player"
        side = "player" if is_player else "ai"
        owner_id = (self.user_profile.get("id", 0)
                    if is_player and isinstance(self.user_profile, dict) else 0)
        current = int(bstate.get(f"{side}_resources", 0) or 0)
        total = int(bstate.get(f"{side}_total_resources", 0) or 0)
        bonus = int(bstate.pop(f"start_turn_resource_bonus_{side}", 0) or 0)
        refill = total + max(0, bonus) - current
        if refill:
            project_resource_change(
                projection_game, session, bstate, pl_t, ai_t, side,
                "currentresource", refill)
        clear_expired_temporary_attributes(
            _hc._db, session.session_id, owner_id, "start_turn",
            clear_stat_buffs=True, battle_state=bstate, handler=self)
        reset_armor(_hc._db, session.session_id, bstate)
        changes = ready_cards_for_turn(_hc._db, session.session_id, owner_id)
        recipient = pl_t if is_player else ai_t
        for uid, template_guid, card_owner, previous_state, new_state in changes:
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(uid)))
            _tpl, card_type, _name, _cost, _atk, _def, _gem = \
                self._card_full_data(projection_game, scid, template_guid)
            projection_game.push_card_updated(
                scid, recipient, _hc.game_engine.ECardCollections.Warzone,
                card_type, template_id=template_guid, state=int(new_state))
            if (int(previous_state) & _hc.game_engine.ECardStates.Tapped and
                    not (int(new_state) & _hc.game_engine.ECardStates.Tapped)):
                from rules_port.triggers import dispatch_native_trigger
                dispatch_native_trigger(
                    db=_hc._db, handler=self, game=projection_game,
                    session=session, player_uid=pl_t, ai_uid=ai_t,
                    battle_state=bstate, event_type="CardReadiedEvent",
                    source_card_id=int(uid), source_player_id=owner_id,
                    data={"event_previous_state": int(previous_state)})
        from rules_port.cooldowns import decrement_and_project_ready_cooldowns
        decrement_and_project_ready_cooldowns(
            _hc._db, session, self, projection_game, owner_id, pl_t, ai_t, bstate)
        clear_expired_temporary_attributes(
            _hc._db, session.session_id, owner_id, "prep",
            clear_stat_buffs=True, battle_state=bstate, handler=self)
        # Recompute the combat branch after Prep for whichever side is
        # active.  Previously this only ran for the human, leaving the AI's
        # stale ``player_has_ready_troop`` value from its prior main phase;
        # a troop such as Brightmoon Brave was ready on the next turn but
        # RulesPort still selected SecondMainPhase.
        self._current_bstate = bstate
        bstate["player_has_ready_troop"] = bool(
            self._player_can_attack_troops(session)
            if is_player else self._ai_can_attack_troops(session))
        bstate["turn_phases"] = build_turn_phases(bstate)
        _hc._db.commit()
        # The phase-entry resolver contract is "update the projection and
        # publish its wire events".  Readiness (and the resource refill above)
        # belong to Prep, but the events stayed queued until the drive stopped
        # at the next priority window, so Unity only untapped the troops when
        # Main began.  Publish them here, as StartTurn already does.
        if projection_game.events:
            self._send_battle_events(session, projection_game, pl_t)
        return changes

    def _apply_rules_port_draw(self, session, projection_game, bstate):
        """Project the native Draw phase for whichever side is active."""
        from rules_port.lifecycle import should_draw_for_turn
        side = "ai" if bstate.get("turn_player") == "ai" else "player"
        if not should_draw_for_turn(bstate, side):
            return False
        if side == "ai":
            return self._ai_draw_card(
                projection_game, session, projection_game.ai_uid, bstate)
        owner_id = (self.user_profile.get("id", 0)
                    if isinstance(self.user_profile, dict) else None)
        return self._player_draw_card(
            projection_game, session, projection_game.player_uid, owner_id)

    def _apply_rules_port_start_turn(self, session, projection_game,
                                     bstate, *, emit_events=True,
                                     resolve_phase_triggers=True,
                                     reset_cards=True):
        """Apply card-owned StartTurn rules from the RulesPort lifecycle.

        The port owns phase entry; this adapter owns only the runtime
        projection of the already-selected active player.  Keeping this
        callback here lets the same port transition perform card aging,
        Tunneling, and Surface queuing for both Practice and PvP.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        from rules_port.tunneling import advance, queue_surfaces

        pl_t, ai_t = projection_game.player_uid, projection_game.ai_uid
        active_owner = (self.user_profile.get("id", 0)
                        if bstate.get("turn_player") == _be.PLAYER
                        and isinstance(self.user_profile, dict) else 0)
        game = projection_game or self._fresh_game(session, pl_t, ai_t, bstate)
        if reset_cards:
            self._reset_active_cards_for_turn(
                game, session, pl_t, ai_t, bstate, active_owner)
        bstate["_rules_port_attached"] = True
        from rules_port.context import EffectContext
        context = EffectContext.from_rules_port(
            game, session, _hc._db, self, pl_t, ai_t, bstate,
            "", ability=None)
        active_champion_uid = context.champion_card_uid(active_owner)
        changes = advance(context, active_owner)
        # Mark the lifecycle phase while TurnStarted triggers resolve so
        # temporary current-resource gains (e.g. Lithe Lyricist's +1) are
        # recorded as a start-turn bonus that Prep reapplies after its refill.
        _previous_phase = bstate.get("phase")
        if resolve_phase_triggers:
            from rules_port.triggers import dispatch_native_trigger
            bstate["phase"] = _hc.game_engine.ETurnPhases.StartTurn
            dispatch_native_trigger(
                db=_hc._db, handler=self, game=game, session=session,
                player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                event_type="TurnPhaseEvent", source_card_id=None,
                source_player_id=active_owner,
                data={"phase": int(_hc.game_engine.ETurnPhases.StartTurn)})
            dispatch_native_trigger(
                db=_hc._db, handler=self, game=game, session=session,
                player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                # StartTurnState enqueues TurnStartedEvent with the active
                # champion as SourceCardId. TriggerSource targets (including
                # Mindpyre Wraith's reveal ability) depend on this identity.
                event_type="TurnStartedEvent",
                source_card_id=active_champion_uid,
                source_player_id=active_owner)
        # CardCounterTemplate "Stealth": the client's built-in TurnStarted
        # trigger removes one stealth counter from the active champion.  Run
        # it after the authored TurnStarted triggers so an "if you are
        # Stealth" check still sees the counter that is being removed.
        from rules_port.stealth import advance as stealth_advance
        stealth_changes = stealth_advance(context, active_owner)
        from rules_port.zone_effects import lifebound as lifebound_returns
        lifebound_changes = lifebound_returns(context, active_owner)
        replenished = self._replenish_other_resources(
            game, session, bstate, pl_t, ai_t, active_owner)
        surfaces = queue_surfaces(context, active_owner)
        if _previous_phase is None:
            bstate.pop("phase", None)
        else:
            bstate["phase"] = _previous_phase
        if emit_events:
            self._send_battle_events(session, game, pl_t)
        return {"tunneling": changes, "surfaces": surfaces,
                "stealth": stealth_changes,
                "lifebound": lifebound_changes,
                "replenished": replenished}

    def _replenish_other_resources(self, game, session, bstate, pl_t, ai_t,
                                   active_owner):
        """Session.ResetActiveResourcePool: ReplenishResourcesEachTurn.

        The active player's pool refills at Prep; a non-active champion with
        this flag also refills at the start of the opposing turn.
        """
        import hconnect_server as _hc
        from rules_port.static_rules import champion_int_attribute
        changed = []
        for participant in (bstate.get("champ_map") or {}):
            try:
                owner = int(participant)
            except (TypeError, ValueError):
                continue
            if owner == int(active_owner or 0):
                continue
            if champion_int_attribute(
                    bstate, owner, "ReplenishResourcesEachTurn") <= 0:
                continue
            if champion_int_attribute(
                    bstate, owner, "CantReplenishResources") > 0:
                continue
            if bstate.get("pvp"):
                key, total_key = f"res_{owner}", f"res_total_{owner}"
                old = int(bstate.get(key, 0) or 0)
                total = int(bstate.get(total_key, 0) or 0)
                if total <= old:
                    continue
                bstate[key] = total
                from rules_port.persistence import save_state
                from rules_port.runtime_helpers import owner_uid
                save_state(session, bstate)
                event = _hc.game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                event.player_id = owner_uid(owner, pl_t, ai_t, bstate)
                event.operation = 1
                event.delta = total - old
                event.new_value = total
                game._push(event)
                changed.append((owner, total))
            else:
                side = "player" if owner else "ai"
                total = int(bstate.get(f"{side}_total_resources", 0) or 0)
                old = int(bstate.get(f"{side}_resources", 0) or 0)
                if total <= old:
                    continue
                from rules_port.resources import project_resource_change
                change = project_resource_change(
                    game, session, bstate, pl_t, ai_t, side,
                    "currentresource", total - old)
                changed.append((owner, change.new_value))
        return changed

    def _reset_active_cards_for_turn(self, game, session, pl_t, ai_t,
                                     bstate, active_owner_id):
        """Mirror ``Session.ResetActiveCards`` before TurnStarted triggers.

        Unity clears ``CameOutThisTurn`` from every warzone card, then marks
        only the active player's existing cards with
        ``StartedATurnOnYourSide``.  This happens before TurnStartedEvent, so
        troops summoned by a start-of-turn trigger retain CameOutThisTurn and
        are summoning-sick.  Doing this in Prep instead incorrectly marks
        those freshly summoned tokens as old troops (and the client receives
        the wrong CardUpdated state).
        """
        import hconnect_server as _hc
        if bstate.get("_rules_port_attached") or \
                getattr(session, "_rules_port_session", None) is not None:
            from rules_port.lifecycle import prime_cards_for_turn
            from rules_port.runtime_helpers import owner_uid
            changed = prime_cards_for_turn(
                _hc._db, session.session_id, active_owner_id)
            for card_uid, tpl_guid, owner_id, new_state, _card_type in changed:
                scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(card_uid))
                _tpl, ct, _name, _cost, _atk, _def, _gem = \
                    self._card_full_data(game, scid, tpl_guid)
                cdef = game.card_defs.get(scid)
                game.push_card_updated(
                    scid, owner_uid(owner_id, pl_t, ai_t, bstate),
                    _hc.game_engine.ECardCollections.Warzone, ct,
                    template_id=tpl_guid,
                    attributes=(cdef.attributes if cdef else 0),
                    state=int(new_state))
            if changed:
                _hc.log_req(f"    RulesPort ResetActiveCards: active="
                        f"{int(active_owner_id or 0)} updated {len(changed)} "
                        "warzone card(s)")
            return len(changed)

        from abilities.framework._shared import owner_uid

        # Session.PrimeCard clears these transient flags for the active
        # player's warzone before the CameOut/Started pass.  Tapped is left
        # alone here; readiness (including CantReadyAutomatically) belongs to
        # the subsequent Prep checkpoint.
        prime_mask = (
            _hc.game_engine.ECardStates.Blocking |
            _hc.game_engine.ECardStates.Attacking |
            _hc.game_engine.ECardStates.Damaged |
            _hc.game_engine.ECardStates.Healed |
            _hc.game_engine.ECardStates.HasAttacked |
            _hc.game_engine.ECardStates.HasBlocked |
            _hc.game_engine.ECardStates.ZoneChangeReplacement |
            _hc.game_engine.ECardStates.Activated |
            _hc.game_engine.ECardStates.VoidsIfDestroyed |
            _hc.game_engine.ECardStates.CameOutThisTurn |
            _hc.game_engine.ECardStates.StartedATurnOnYourSide)
        rows = _hc.db_warzone_display_rows(session.session_id, conn=_hc._db)
        changed = 0
        for card_uid, tpl_guid, owner_id, old_state, _card_type in rows:
            old_state = int(old_state or 0)
            # ResetCameOutThisTurn applies to both sides.  PrimeCard and
            # SetStartedATurnOnOwnersSide apply only to the active side.
            new_state = old_state & ~_hc.game_engine.ECardStates.CameOutThisTurn
            if int(owner_id or 0) == int(active_owner_id or 0):
                new_state = ((new_state & ~prime_mask) |
                             _hc.game_engine.ECardStates.StartedATurnOnYourSide)
            if new_state == old_state:
                continue
            _hc.db_set_card_state_exact(
                session.session_id, int(card_uid), new_state, conn=_hc._db)
            changed += 1
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(card_uid)))
            _tpl, ct, _name, _cost, _atk, _def, _gem = \
                self._card_full_data(game, scid, tpl_guid)
            cdef = game.card_defs.get(scid)
            game.push_card_updated(
                scid,
                owner_uid(owner_id, pl_t, ai_t, bstate),
                _hc.game_engine.ECardCollections.Warzone,
                ct,
                template_id=tpl_guid,
                attributes=(cdef.attributes if cdef else 0),
                state=int(new_state))
        _hc._db.commit()
        if changed:
            _hc.log_req(f"    ResetActiveCards: active={int(active_owner_id or 0)} "
                    f"updated {changed} warzone card(s)")
        return changed

    def _push_champions_warm(self, session, pl_t, ai_t, bstate, game):
        """Rebuild both champion CardDefs for the next packet.

        UIBattle.OnTurnPhaseUpdated reads State.Cards[State.Players[p].ChampionSessionCardId]
        for the OPPONENT at StartTurn (and for combat phases). If the AI champion
        isn't in the cache, the client throws KeyNotFoundException, desyncs
        priority and sends RequestPrioritySyncTransaction. The initial battle
        packet already seeds both entries with CardUpdated(None) before
        ChampionCardPlayed moves them into the champion HUD. Sending another
        CardUpdated with collection Champions here makes the client run its
        normal card-location animation on the champion card, which can leave a
        full-size card on the battlefield. Re-register the CardDefs for any
        later PlayerUpdated/option work, but do not emit another CardUpdated.
        Also carry the persisted health into the Game so any PlayerUpdated from
        this object reports the live health (not the 20 default).
        """
        import hconnect_server as _hc
        game.ai_health = bstate.get("ai_health", 10)
        game.player_health = bstate.get("player_health", 20)
        champion_counters = bstate.get("champion_counters") or {}
        ai_champ = getattr(self, "_ai_champ_scid", None)
        if ai_champ:
            ai_cdef = _hc.game_engine.CardDef(
                "AI", _hc.game_engine.ECardTypes.Champion, 0,
                bstate.get("ai_health", 10), bstate.get("ai_health", 10),
                [], [_hc.game_engine.ResourceId.from_str(g) for g in getattr(self, "_ai_champ_ability_guids", [])])
            ai_cdef.counters = dict(champion_counters.get(
                str(int(ai_champ.uid.uid64)), {}) or {})
            game.card_defs[ai_champ] = ai_cdef
        pl_champ = getattr(self, "_player_champ_scid", None)
        if pl_champ:
            pl_abilities = getattr(self, "_player_champ_abilities", [])
            cdef = _hc.game_engine.CardDef(
                "Player", _hc.game_engine.ECardTypes.Champion, 0,
                bstate.get("player_health", 17), bstate.get("player_health", 17),
                [], list(pl_abilities))
            cdef.counters = dict(champion_counters.get(
                str(int(pl_champ.uid.uid64)), {}) or {})
            # Carry the persisted spell-power escalation (player_sp_uses) onto
            # the champion CardDef so the client's button shows the INCREASED
            # SP cost (e.g. Soothsaying 4->5 after one use) — a re-pushed
            # champion without these mods wipes the escalation display.
            for ag, uses in (bstate.get("player_sp_uses", {}) or {}).items():
                if int(uses) > 0:
                    cdef.spell_point_cost_mods[_hc.game_engine.ResourceId.from_str(ag)] = int(uses)
            game.card_defs[pl_champ] = cdef

    def _advance_to_priority(self, session, pl_t, ai_t, bstate):
        """Auto-advance the human's turn through non-stop phases.

        The server pushes ONE phase per packet. If the current phase is NOT a
        stop for the turn player (and not PickGoesFirst/Mulligan, which can
        never be skipped), the server auto-passes it — pushes the phase and
        immediately advances. It stops when it reaches a stop phase (player gets
        priority + GreenLight) or the turn ends (handed to the AI). Returns
        True if the player now holds priority.
        """
        import hconnect_server as _hc
        if bstate.get("_rules_port_attached"):
            return self._advance_rules_port_to_priority(
                session, pl_t, ai_t, bstate)
        import battle_engine as _be
        while True:
            phase = _be.current_phase(bstate)
            if self._check_champion_health(session, pl_t, ai_t, bstate):
                return False
            # Start of the player's turn: reset the combat decision and mirror
            # Unity's ResetActiveCards checkpoint before any TurnStarted
            # triggers. Prep later readies eligible troops. Also re-push both champions so the
            # client's OnTurnPhaseUpdated (State.Cards[ChampionSessionCardId])
            # doesn't KeyNotFound at StartTurn / combat phases.
            if phase == _hc.game_engine.ETurnPhases.StartTurn and bstate.get("turn_player") == _be.PLAYER:
                bstate["player_has_ready_troop"] = False
                bstate.pop("player_attackers", None)
                bstate["turn_phases"] = _be.BASE_TURN_PHASES
                _be.save_state(session, bstate)
                warm = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
                start_result = self._apply_rules_port_start_turn(
                    session, warm, bstate, emit_events=False,
                    resolve_phase_triggers=False)
                tunnel_changes = start_result["tunneling"]
                tunnel_surfaces = start_result["surfaces"]
                stealth_changes = start_result.get("stealth") or []
                self._push_champions_warm(session, pl_t, ai_t, bstate, warm)
                self._send_battle_events(session, warm, pl_t)
                _hc.log_req("    Player turn start: combat decision deferred to Prep; "
                        f"tunneling +{len(tunnel_changes)} / "
                        f"surface queued {len(tunnel_surfaces)} / "
                        f"stealth removed {len(stealth_changes)}; champions re-pushed")
            # End of turn: hand over to the AI.
            if phase == _hc.game_engine.ETurnPhases.EndTurn:
                # "At the end of your turn" triggers for the player's cards.
                g_end = self._fresh_game(session, pl_t, ai_t, bstate)
                bstate["_rules_port_attached"] = True
                from rules_port.triggers import dispatch_native_trigger
                dispatch_native_trigger(
                    db=_hc._db, handler=self, game=g_end, session=session,
                    player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                    event_type="TurnEndedEvent", source_card_id=None,
                    source_player_id=self.user_profile["id"])
                # "Until end of turn" attribute grants (e.g. Dimmid's
                # Lifedrain) expire NOW — the controller's turn is ending —
                # and the warzone cards are re-pushed so the client drops the
                # badges immediately instead of at the next Ready step.
                # Cleanup order matters: remove combat damage while the
                # outgoing turn's temporary defense bonuses still apply.
                self._clear_combat_damage(session, bstate)
                self._clear_expired_temporary_attributes(
                    session, bstate, self.user_profile["id"], "end_turn",
                    clear_stat_buffs=True)
                for wzr in _hc.db_warzone_display_rows(
                        session.session_id, conn=_hc._db):
                    cu, tpl, card_user_id, card_state, _card_type = wzr
                    scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(cu))
                    _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(
                        g_end, scid, tpl)
                    g_end.push_card_updated(
                        scid, pl_t if card_user_id else ai_t,
                        _hc.game_engine.ECardCollections.Warzone,
                        ct, template_id=tpl,
                        state=int(card_state or 0))
                self._send_battle_events(session, g_end, pl_t)
                # End-of-turn triggers are ordinary stack items.  Do not hand
                # the turn to the opponent while they are still waiting for
                # both players to pass; the client must receive the chain and
                # priority checkpoint first (e.g. Verdant Wyldeboar).
                if not _be.stack_empty(bstate):
                    _be.save_state(session, bstate)
                    # We are already at the EndTurn stop.  Re-entering
                    # _advance_to_priority here leaves the phase unchanged
                    # and recursively re-discovers the same trigger chain;
                    # emit the priority checkpoint directly instead.
                    self._push_phase_options_empty(session, pl_t, ai_t)
                    g_chain = self._fresh_game(session, pl_t, ai_t, bstate)
                    # The preceding EndTurn packet may have contained only
                    # card updates.  Re-announce the actual EndTurn phase so
                    # the client's subsequent pass is not tagged with the
                    # previous phase number and rejected as stale.
                    g_chain.push_turn_phase(
                        _be.current_phase(bstate), pl_t, pl_t)
                    g_chain.push_green_light(
                        pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
                    self._send_battle_events(session, g_chain, pl_t)
                    _hc.log_req("    EndTurn paused: waiting for pending trigger chain")
                    return False
                if getattr(self, "_autoplay_drive_ai_turn", False):
                    # Headless harness drives the AI turn itself (with proper
                    # resume at opponent stops).  Leave turn_player as PLAYER
                    # and let the harness hand over.
                    _be.save_state(session, bstate)
                    return False
                if bstate.get("_rules_port_attached"):
                    from rules_port.lifecycle import complete_turn
                    next_player = complete_turn(bstate)
                else:
                    next_player = _be.next_turn_player(bstate)
                    bstate["turn_player"] = next_player
                    bstate["player_passed"] = False
                    bstate["ai_passed"] = False
                    bstate["phase_idx"] = 0
                    bstate["turn_phases"] = _be.BASE_TURN_PHASES
                    bstate[f"{next_player}_resource_played_this_turn"] = False
                    bstate[f"{next_player}_resource_plays_this_turn"] = 0
                # F10 auto-pass is "to the end of MY turn" — clear it now so the
                # AI's own turn isn't auto-advanced.
                bstate.pop("player_autopass", None)
                _be.save_state(session, bstate)
                if next_player == _be.PLAYER:
                    self._advance_to_priority(session, pl_t, ai_t, bstate)
                    _hc.log_req("    Auto-advance reached EndTurn; bonus turn kept player in control")
                else:
                    self._run_ai_turn(session, pl_t, ai_t, bstate)
                    _hc.log_req("    Auto-advance reached EndTurn; turn passed to AI")
                return False
            # No attackers declared: skip the remaining combat steps straight
            # to the Second Main Phase.  Declaration happens at/after
            # DeclareAttack, so DeclareAttackPriorityWindow and beyond are
            # skippable; DeclareCombatPriorityWindow / DeclareAttack still
            # give the player the chance to commit attackers first.
            if (phase in _be.COMBAT_STEPS and
                    _be.COMBAT_STEPS.index(phase) >= 2 and
                    bstate.get("turn_player") == _be.PLAYER and
                    not (bstate.get("player_attackers") or {})):
                _be.skip_to_phase(bstate, _hc.game_engine.ETurnPhases.SecondMainPhase)
                _be.save_state(session, bstate)
                _hc.log_req("    No attackers declared — skipped combat steps to SecondMain")
                continue
            # Swiftstrike damage steps only occur when an attacking or blocking
            # troop has Swiftstrike (FirstStrike) — otherwise skip straight to
            # the normal AssignDamage step.
            if phase in (_hc.game_engine.ETurnPhases.AssignFirstStrikeDamage,
                         _hc.game_engine.ETurnPhases.FirstStrikePriorityWindow):
                from ai import combat_has_swiftstrike
                if not combat_has_swiftstrike(_hc._db, session, bstate):
                    _be.skip_to_phase(bstate, _hc.game_engine.ETurnPhases.AssignDamage)
                    _be.save_state(session, bstate)
                    _hc.log_req("    No Swiftstrike combatants — skipped swiftstrike steps")
                    continue
            # Discard with a fitting hand: the client auto-passes it with a
            # no-op animation (no transaction), so skip it server-side.
            if phase == _hc.game_engine.ETurnPhases.Discard:
                max_hand = self._max_hand_size(session)
                hand_count = _hc.db_hand_card_count(session.session_id, self.user_profile["id"])
                if hand_count <= max_hand:
                    bstate["player_passed"] = True
                    bstate["ai_passed"] = True
                    _be.advance_phase(bstate)
                    _be.save_state(session, bstate)
                    _hc.log_req("    Auto-advanced past Discard (hand fits)")
                    continue
                # Hand over the limit: the player must discard, so it's a stop.
                game = self._fresh_game(session, pl_t, ai_t, bstate)
                game.push_turn_phase(phase, pl_t, pl_t)
                game.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
                game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
                self._send_battle_events(session, game, pl_t)
                _hc.log_req(f"    Stopped at phase {phase} (discard required); turn={bstate.get('turn_player')} priority=player")
                return True
            # Stop phase (incl. PickGoesFirst/Mulligan): grant priority + wait.
            if _be.is_self_stop(bstate, phase):
                # F10 / "Skip" auto-pass: the player asked to pass all priority
                # to the end of their own turn, so advance through stops instead
                # of granting priority (mandatory setup phases still stop).
                if bstate.get("player_autopass") and phase not in (
                        _hc.game_engine.ETurnPhases.PickGoesFirst,
                        _hc.game_engine.ETurnPhases.Mulligan,
                        _hc.game_engine.ETurnPhases.StartGame):
                    game = self._fresh_game(session, pl_t, ai_t, bstate)
                    game.push_turn_phase(phase, pl_t, pl_t)
                    game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
                    game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
                    self._push_warzone_card_updates(game, session, pl_t, ai_t)
                    self._send_battle_events(session, game, pl_t)
                    _hc.log_req(f"    Auto-passed stop phase {phase} (F10)")
                    bstate["player_passed"] = True
                    bstate["ai_passed"] = True
                    _be.advance_phase(bstate)
                    _be.save_state(session, bstate)
                    continue
                # A fully-populated fresh Game so PlayerUpdated reports the live
                # resources/health/charges/SP (not the bare defaults).
                game = self._fresh_game(session, pl_t, ai_t, bstate)
                bstate["_rules_port_attached"] = True
                from rules_port.triggers import dispatch_native_trigger
                dispatch_native_trigger(
                    db=_hc._db, handler=self, game=game, session=session,
                    player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                    event_type="TurnPhaseEvent", source_card_id=None,
                    source_player_id=self.user_profile["id"],
                    data={"phase": int(phase)})
                game.push_turn_phase(phase, pl_t, pl_t)
                game.push_green_light(pl_t, self._priority_context_for(phase, bstate))
                game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
                # Keep the AI player representation fresh (State.Players[ai]
                # needs a recent PlayerUpdated with a valid ChampionId for
                # BattleStateDeclareAttackers.GetTargetChampion to resolve the
                # opponent portrait as the attack-line target).
                game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
                self._push_warzone_card_updates(game, session, pl_t, ai_t)
                self._send_battle_events(session, game, pl_t)
                _hc.log_req(f"    STOP phase {phase} PlayerUpdated: res={game.player_resources}/{game.player_total_resources} chg={game.player_charges} sp={game.player_spell_points}")
                # Push PlayerOptionList for every stop phase so the client knows
                # what is interactable. Main phases: full playable cards + champion
                # abilities. DeclareAttack: warzone troops with ECardUsage.Attack.
                # Other phases: champion abilities only (or empty).
                if phase in (_hc.game_engine.ETurnPhases.FirstMainPhase,
                             _hc.game_engine.ETurnPhases.SecondMainPhase):
                    self._push_main_phase_options(session, pl_t, ai_t)
                elif phase == _hc.game_engine.ETurnPhases.DeclareAttack:
                    self._push_attack_options(session, pl_t, ai_t)
                elif phase in (_hc.game_engine.ETurnPhases.AssignDamage,
                               _hc.game_engine.ETurnPhases.AssignFirstStrikeDamage):
                    # Give the client the combat listing so BattleStateAssignDamage
                    # can auto-commit (no blockers) and send AssignDamageOrderTransaction.
                    self._push_combat_listing_phase(session, pl_t, ai_t, bstate)
                else:
                    self._push_phase_options_empty(session, pl_t, ai_t)
                _hc.log_req(f"    Stopped at phase {phase}; turn={bstate.get('turn_player')} priority=player "
                        f"(context={self._priority_context_for(phase, bstate)})")
                return True
            # Non-stop phase: push it and auto-advance.
            game = self._fresh_game(session, pl_t, ai_t, bstate)
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate, "TurnPhaseEvent", None,
                self.user_profile["id"], phase=int(phase))
            if phase == _hc.game_engine.ETurnPhases.Draw:
                # The human draws a card at the Draw phase — except on their very
                # first turn when they chose to play first.
                if bstate.get("turn_number", 1) > 1 or bstate.get("player_draws_first_turn"):
                    if self._player_draw_card(game, session, pl_t):
                        # Deck-out: the player drew from an empty deck and lost.
                        return False
            elif phase == _hc.game_engine.ETurnPhases.Prep:
                # Prep: refill current resources to max for the turn.
                native_prep = bstate.get("_rules_port_attached")
                if native_prep:
                    from rules_port.resources import apply_resource_change
                    prep_side = ("player" if bstate.get("turn_player")
                                 == "player" else "ai")
                    prep_bonus = int(bstate.pop(
                        f"start_turn_resource_bonus_{prep_side}", 0) or 0)
                    apply_resource_change(
                        bstate, prep_side, "currentresource",
                        int(bstate.get(f"{prep_side}_total_resources", 0) or 0)
                        + max(0, prep_bonus) -
                        int(bstate.get(f"{prep_side}_resources", 0) or 0))
                else:
                    bstate["player_resources"] = bstate.get(
                        "player_total_resources", 0)
                self._clear_expired_temporary_attributes(
                    session, bstate, self.user_profile["id"], "start_turn",
                    clear_stat_buffs=True)
                # Clear summoning sickness from warzone troops: they now have
                # StartedATurnOnYourSide (survived to this turn). Persist the
                # state in game_cards.card_state (DB) so a reconnect and the
                # combat-phase decision can reconstruct it.
                if native_prep:
                    from rules_port.lifecycle import (ready_cards_for_turn,
                                                      reset_armor)
                    reset_armor(_hc._db, session.session_id, bstate)
                    ready_changes = ready_cards_for_turn(
                        _hc._db, session.session_id, self.user_profile["id"])
                    for uid, tpl_guid, owner_id, previous_state, pstate in ready_changes:
                        scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(uid))
                        self._card_full_data(game, scid, tpl_guid)
                        tpl = self._template_by_guid(tpl_guid)
                        ct = (_hc.game_engine.card_type_from_db(tpl[1]) if tpl
                              else _hc.game_engine.ECardTypes.Troop)
                        game.push_card_updated(
                            scid, pl_t, _hc.game_engine.ECardCollections.Warzone,
                            ct, template_id=tpl_guid, state=int(pstate))
                        if (previous_state & _hc.game_engine.ECardStates.Tapped and
                                not (pstate & _hc.game_engine.ECardStates.Tapped)):
                            self._dispatch_native_trigger(
                                game, session, pl_t, ai_t, bstate,
                                "CardReadiedEvent", uid, owner_id)
                    from rules_port.cooldowns import decrement_and_project_ready_cooldowns
                    decrement_and_project_ready_cooldowns(
                        _hc._db, session, self, game,
                        self.user_profile["id"], pl_t, ai_t, bstate)
                    self._clear_expired_temporary_attributes(
                        session, bstate, self.user_profile["id"], "prep",
                        clear_stat_buffs=True)
                    wz_rows = ()
                else:
                    wz_rows = _hc.db_warzone_troops_with_state(
                        session.session_id, self.user_profile["id"], conn=_hc._db)
                for wzr in wz_rows:
                    scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(wzr[0]))
                    previous_state = int(_hc.db_card_state_value(
                        session.session_id, int(wzr[0]), conn=_hc._db) or 0)
                    # Ready/untap: clear combat states.  CameOutThisTurn and
                    # StartedATurnOnYourSide were already resolved at
                    # StartTurn, before TurnStarted triggers; keeping the
                    # former here would incorrectly age freshly summoned
                    # tokens.
                    import game_engine as _ge
                    attrs_row = _hc.db_warzone_card_state_attributes(
                        session.session_id, int(wzr[0]), conn=_hc._db)
                    clear_mask = (_ge.ECardStates.Tapped |
                                  _ge.ECardStates.Attacking |
                                  _ge.ECardStates.HasAttacked |
                                  _ge.ECardStates.Blocking |
                                  _ge.ECardStates.HasBlocked)
                    if (attrs_row and int(attrs_row[1] or 0)
                            & _ge.ECardAttributes.CantReadyAutomatically):
                        clear_mask &= ~_ge.ECardStates.Tapped
                    # A permanent that survived from an earlier turn must be
                    # marked as having started a turn on its controller's
                    # side.  Tokens created during the current turn retain
                    # CameOutThisTurn and remain summoning-sick; older
                    # tokens (including Battle Hoppers) must become
                    # attackable even if a reconnect or an earlier phase
                    # packet omitted the StartTurn marker.
                    aged_state = previous_state & ~clear_mask
                    if not (aged_state & _ge.ECardStates.CameOutThisTurn):
                        aged_state |= _ge.ECardStates.StartedATurnOnYourSide
                    _hc.db_reset_warzone_troop(
                        session.session_id, int(wzr[0]), clear_mask, conn=_hc._db)
                    if aged_state != previous_state:
                        _hc.db_set_card_state_exact(
                            session.session_id, int(wzr[0]), aged_state, conn=_hc._db)
                    self._card_full_data(game, scid, wzr[1])
                    tpl = self._template_by_guid(wzr[1])
                    ct = _hc.game_engine.card_type_from_db(tpl[1]) if tpl else _hc.game_engine.ECardTypes.Troop
                    pstate = _hc.db_card_state_value(session.session_id, int(wzr[0]))
                    if pstate is None:
                        pstate = 0
                    game.push_card_updated(scid, pl_t, _hc.game_engine.ECardCollections.Warzone, ct,
                                          template_id=wzr[1], state=pstate)
                    if (previous_state & _ge.ECardStates.Tapped and
                            not (pstate & _ge.ECardStates.Tapped)):
                        self._dispatch_native_trigger(
                            game, session, pl_t, ai_t, bstate,
                            "CardReadiedEvent", int(wzr[0]),
                            self.user_profile["id"],
                            event_previous_state=previous_state)
                _hc._db.commit()
                # "AfterCardsReadyOnPlayersTurn" effects (e.g. Nazhk's
                # CantReadyAutomatically) must survive through this ready
                # step, then expire for the rest of the turn.
                self._clear_expired_temporary_attributes(
                    session, bstate, self.user_profile["id"], "prep",
                    clear_stat_buffs=True)
                # Prep is the first point at which troops have been readied;
                # seed the combat decision before the First Main green light
                # is emitted so its button says ProceedToCombat when legal.
                bstate["player_has_ready_troop"] = self._player_can_attack_troops(session)
                bstate["turn_phases"] = _be.build_turn_phases(bstate)
            elif phase == _hc.game_engine.ETurnPhases.DeclareDefense:
                # The AI is the defender and never blocks yet: it passes priority
                # through the DeclareDefense step with no blockers. Push the phase
                # plus a BlockersAssigned event (empty blocker list per attacker)
                # so the client renders the AI declining to block, then advance.
                bstate = self._ai_pass_declare_defense(session, pl_t, ai_t, bstate, game)
            # Re-sync the Game's live values from bstate: the game was built by
            # _fresh_game BEFORE the phase ran (Prep refills resources, Draw
            # draws), so without this the PlayerUpdated pushes the pre-phase
            # values and the client flickers to 0/0 at turn start.
            game.player_resources = bstate.get("player_resources", 0)
            game.player_total_resources = bstate.get("player_total_resources", 0)
            game.player_threshold = dict(bstate.get("player_threshold", {}))
            game.player_charges = bstate.get("player_charges", 0)
            game.player_spell_points = bstate.get("player_spell_points", 0)
            game.player_health = bstate.get("player_health", 20)
            game.ai_resources = bstate.get("ai_resources", 0)
            game.ai_total_resources = bstate.get("ai_total_resources", 0)
            game.ai_threshold = dict(bstate.get("ai_threshold", {}))
            game.ai_charges = bstate.get("ai_charges", 0)
            game.ai_spell_points = bstate.get("ai_spell_points", 0)
            game.ai_health = bstate.get("ai_health", 10)
            game.player_health = bstate.get("player_health", 20)
            game.push_turn_phase(phase, pl_t, pl_t)
            game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
            game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
            self._push_warzone_card_updates(game, session, pl_t, ai_t)
            self._send_battle_events(session, game, pl_t)
            _hc.log_req(f"    Auto-passed phase {phase} (turn={bstate.get('turn_player')} priority=player)")
            # A trigger/card went onto the chain this phase (e.g. Twisted
            # Fate's "when you draw" during the Draw phase): the phase must NOT
            # advance while the chain is non-empty — grant priority here so the
            # player resolves the chain first; the pass handler then continues
            # the turn (the player's pass after the chain empties advances it).
            if not _be.stack_empty(bstate):
                bstate["player_passed"] = False
                bstate["ai_passed"] = False
                _be.save_state(session, bstate)
                self._push_phase_options_empty(session, pl_t, ai_t)
                g_chain = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
                g_chain.push_green_light(
                    pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
                self._send_battle_events(session, g_chain, pl_t)
                _hc.log_req(f"    Phase {phase}: chain pending — priority to "
                        f"player (phase held)")
                return True
            bstate["player_passed"] = True
            bstate["ai_passed"] = True
            _be.advance_phase(bstate)
            _be.save_state(session, bstate)

    def _project_rules_port_priority(self, session, pl_t, ai_t, bstate):
        """Project the native scheduler's current input window to the client.

        RulesPort owns the phase/action transition. This method only builds
        the existing wire/UI projection after the scheduler has stopped at a
        client input, or resumes the server AI when the native queue gives it
        priority.
        """
        import hconnect_server as _hc
        from rules_port.kernel import PriorityWindowAction
        from rules_port.persistence import load_state

        port = getattr(session, "_rules_port_session", None)
        if port is None:
            return False
        # Reaching a human projection means the client has answered a
        # transaction from a battle UI state, so its board is live (see
        # ``_announce_champion_board_cards``).
        setattr(session, "_client_in_battle",
                getattr(session, "session_id", None))
        _hc.trace_rules_port(_hc.log_req, "project-before", port, bstate)
        current = load_state(session, default=lambda: dict(bstate))
        # An authored prompt that already owns the client's UI state must not
        # be replaced by the ordinary phase/priority projection.  A trigger
        # with an explicit target (e.g. Wakizashi Ambusher's Deploy), a card
        # choice, deck search, discard or conversation prompt was pushed by
        # its own private PlayerOptionList/class-39 packet plus green light;
        # re-announcing the phase, the board and the phase options here makes
        # the client pop BattleStateConfigureAbility/BattleStateTarget before
        # the player can answer.  PvP pauses at the same boundary, so wait for
        # the answering SetAbilityActivationData/choice transaction instead.
        pending_input = [key for key in (
            "pending_choice", "pending_trigger", "pending_deck_search",
            "pending_conversation", "pending_discard_ability")
            if current.get(key)]
        if pending_input:
            _hc.log_req("    RulesPort projection held for pending client input: "
                    + ",".join(pending_input))
            return True
        phases = list(current.get("turn_phases") or ())
        phase = port.current_turn_phase
        try:
            current["phase_idx"] = phases.index(phase)
        except ValueError:
            pass
        current["turn_player"] = (
            "player" if _hc._same_uid(port.active_player_id, pl_t) else "ai")
        bstate.update(current)
        action = port.action_stack.peek()
        if not isinstance(action, PriorityWindowAction):
            return False
        priority = action.priority_player_id
        if (_hc._same_uid(port.active_player_id, ai_t) and
                _hc._same_uid(priority, ai_t)):
            try:
                ai_idx = phases.index(phase)
            except ValueError:
                ai_idx = int(current.get("phase_idx", 0) or 0)
            self._run_ai_turn(session, pl_t, ai_t, bstate,
                              start_idx=ai_idx)
            _hc.trace_rules_port(_hc.log_req, "project-after-ai", port, bstate)
            return False
        if _hc._same_uid(priority, ai_t):
            # During a human attack the human remains the native active
            # player, but DeclareDefense belongs to the defending AI.  This
            # is a real server-owned input window, not stale AI priority.
            if phase == _hc.game_engine.ETurnPhases.DeclareDefense:
                import ai
                from rules_port.persistence import save_state

                response_game = self._fresh_game(
                    session, pl_t, ai_t, current)
                if getattr(port, "event_sink", None) is not None:
                    port.event_sink.game = response_game
                self._current_bstate = current
                current = ai.ai_pass_declare_defense(
                    self, session, pl_t, ai_t, current, response_game) or current

                # The compatibility AI selector writes the persisted combat
                # assignment and emits the client events.  RulesPort still
                # owns the live combat objects used by AssignDamage, so copy
                # the same typed cards into those objects before consuming
                # the native DeclareDefense priority.
                assignments = current.get("ai_blockers") or {}
                for combat in port.combat_manager.combats:
                    attacker = getattr(combat, "attacker", None)
                    attacker_id = getattr(
                        getattr(attacker, "session_card_id", attacker),
                        "uid64", getattr(attacker, "session_card_id", attacker))
                    try:
                        if attacker_id is None:
                            continue
                        blocker_ids = assignments.get(
                            str(int(cast(Any, attacker_id))), ())
                    except (TypeError, ValueError):
                        blocker_ids = ()
                    blockers = [port.get_card(int(blocker_id))
                                for blocker_id in blocker_ids]
                    if any(blocker is None for blocker in blockers):
                        _hc.log_req(
                            "    RulesPort AI defense could not hydrate "
                            f"blockers for attacker={attacker_id!r}")
                        return False
                    combat.declare_blockers(blockers)

                save_state(session, current)
                port.persist()
                self._send_battle_events(session, response_game, pl_t)
                if not port.pass_player_priority(ai_t):
                    _hc.log_req(
                        "    RulesPort AI defense priority pass rejected: "
                        f"priority={port.action_stack.priority_player_id!r}")
                    return False
                port.drive_until_input(max_steps=64)
                port.persist()
                _hc.trace_rules_port(_hc.log_req, "project-after-ai-defense",
                                 port, current)
                return self._project_rules_port_priority(
                    session, pl_t, ai_t, current)
            _hc.log_req(
                f"    RulesPort ignored stale AI priority: active="
                f"{port.active_player_id!r} ai={ai_t!r} phase={phase!r}")
            return False
        if not _hc._same_uid(priority, pl_t):
            return False

        game = self._fresh_game(session, pl_t, ai_t, bstate)
        game.push_turn_phase(
            phase,
            pl_t if _hc._same_uid(port.active_player_id, pl_t) else ai_t,
            pl_t)
        priority_context = self._priority_context_for(phase, bstate)
        native_chain = getattr(port, "chain", None)
        if (native_chain is not None and
                not getattr(native_chain, "is_empty", True)):
            priority_context = _hc.game_engine.EPriorityContext.ResolveTopOfChain
        game.push_green_light(pl_t, priority_context)
        game.push_player_updated(
            pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(
            ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        # The attack options packet applies force attackers and their triggers,
        # then includes the authoritative board refresh. Avoid projecting the
        # pre-declaration board here; it would immediately be rebuilt.
        if phase != _hc.game_engine.ETurnPhases.DeclareAttack:
            self._push_warzone_card_updates(game, session, pl_t, ai_t)
        self._send_battle_events(session, game, pl_t)
        _hc.trace_rules_port(_hc.log_req, "project-human", port, bstate)
        if phase in (_hc.game_engine.ETurnPhases.FirstMainPhase,
                     _hc.game_engine.ETurnPhases.SecondMainPhase):
            self._push_main_phase_options(
                session, pl_t, ai_t, refresh_warzone=False)
        elif phase == _hc.game_engine.ETurnPhases.DeclareAttack:
            self._push_attack_options(session, pl_t, ai_t)
        elif phase in (_hc.game_engine.ETurnPhases.AssignDamage,
                       _hc.game_engine.ETurnPhases.AssignFirstStrikeDamage):
            self._push_combat_listing_phase(session, pl_t, ai_t, bstate)
        else:
            self._push_phase_options_empty(
                session, pl_t, ai_t, refresh_warzone=False)
        return True

    def _advance_rules_port_to_priority(self, session, pl_t, ai_t, bstate):
        """Drive native phase/action state until a real client input exists."""
        import hconnect_server as _hc
        port = getattr(session, "_rules_port_session", None)
        if port is None:
            return False
        _hc.trace_rules_port(_hc.log_req, "drive-before", port, bstate)
        if (port.current_turn_phase == _hc.game_engine.ETurnPhases.StartTurn and
                port.action_stack.count == 0 and
                int(getattr(port, "total_turns_taken", 0) or 0) == 0):
            # A newly-created checkpoint names StartTurn before its native
            # state has run. Materialize that state once so StartTurn effects
            # and the following phase transitions remain RulesPort-owned.
            port.materialize_current_phase()
        port.drive_until_input(max_steps=64)
        # A resolver can queue server-owned chain responses (a troop's Deploy
        # trigger) while it finishes.  Give the AI the same follow-up passes
        # the transaction pass handler would, or the client is left staring at
        # a window that belongs to the server actor.
        if not (session.session_name or "").startswith("tourney-"):
            for _ in range(8):
                action = port.action_stack.peek()
                if not _hc._is_practice_chain_follow_up(action, pl_t, True):
                    break
                if not port.pass_player_priority(
                        action.priority_player_id):
                    break
                port.drive_until_input(max_steps=64)
        _hc.trace_rules_port(_hc.log_req, "drive-after", port, bstate)
        return self._project_rules_port_priority(
            session, pl_t, ai_t, bstate)

    def _run_ai_turn(self, session, pl_t, ai_t, battle_state, start_idx=0):
        import ai
        return ai.run_ai_turn(self, session, pl_t, ai_t, battle_state, start_idx)

    def _save_player_stops(self, user_id, self_stops, opp_stops):
        """Persist a player's phase-stop preferences (survive across battles)."""
        import json as _j
        from pvp_db import db_card_save_player_stops
        db_card_save_player_stops(user_id, _j.dumps(self_stops), _j.dumps(opp_stops))

    def _load_player_stops(self, user_id):
        """Load a player's saved phase-stop preferences, or (None, None)."""
        from pvp_db import db_card_load_player_stops
        raw = db_card_load_player_stops(user_id)
        if not raw or not raw[0]:
            return None, None
        import json as _j
        try:
            return _j.loads(raw[0]), _j.loads(raw[1]) if raw[1] else None
        except Exception:
            return None, None
