"""Player transaction handlers (mulligan, choose, pass priority, attack, defend, ability activation and more)."""

import json
import re
import struct


class TransactionsMixin:
    """Methods of HCPHandler, mixed into it (see hconnect_server)."""

    def _handle_priority_sync_transaction(self, session, transaction):
        """Reconcile a client's lost priority state without changing game state."""
        import hconnect_server as _hc
        if not session:
            return False

        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            chain_sync = self._push_rules_port_chain_priority_sync(
                session, port)
            if chain_sync is not None:
                if not chain_sync:
                    self._push_transaction_ack(session)
                return True

        # PvP tournament path.
        if (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import pvp_load_state
            pvp_state = pvp_load_state(session)
            if pvp_state:
                my_pid = int(self.client_reck_id)
                my_uid = _hc.game_engine.UID.make(244, my_pid)
                pvp_pids = [int(k) for k in (pvp_state.get("pids") or [])
                            if int(k) != my_pid]
                opp_pid = pvp_pids[0] if pvp_pids else my_pid
                opp_uid = _hc.game_engine.UID.make(244, opp_pid)
                game_ps = _hc.game_engine.Game(int(session.session_id), my_uid, opp_uid)
                phase = pvp_state.get("phase", 3)
                turn_uid = _hc.game_engine.UID.make(244, pvp_state["turn_pid"])
                if phase == _hc.game_engine.ETurnPhases.Mulligan:
                    if pvp_state.get("mulligan_pid") == my_pid:
                        game_ps.push_green_light(
                            my_uid, _hc.game_engine.EPriorityContext.Normal)
                        pkt_ps = game_ps.make_network_packet(my_uid)
                        dw_ps = _hc.encode_datawrapper(
                            0, 3055,
                            _hc.compress_gzip(_hc.encode_sync_event(pkt_ps)), 1,
                            _hc.client_session_guid(self))
                        self.scnt += 1
                        self.send({
                            "issuer": f"0.0.0.0.ServiceGameSession.246."
                                      f"{session.session_id}.{self.scnt}",
                            "target": "ServiceGameSession",
                            "instance": str(session.server_id),
                            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                        }, dw_ps)
                        _hc.log_req(f"    RequestPrioritySync (PvP): Mulligan greenlight to actor {my_pid}")
                    else:
                        self._push_transaction_ack(session)
                        _hc.log_req(f"    RequestPrioritySync (PvP): Mulligan — {my_pid} not the actor, acked")
                elif phase == _hc.game_engine.ETurnPhases.PickGoesFirst:
                    winner_pid = pvp_state.get(
                        "goes_first_pid", pvp_state["turn_pid"])
                    if my_pid == winner_pid:
                        winner_uid = _hc.game_engine.UID.make(244, winner_pid)
                        game_ps.push_turn_phase(
                            _hc.game_engine.ETurnPhases.PickGoesFirst,
                            winner_uid, winner_uid)
                        game_ps.push_green_light(
                            my_uid, _hc.game_engine.EPriorityContext.Normal)
                        pkt_ps = game_ps.make_network_packet(my_uid)
                        dw_ps = _hc.encode_datawrapper(
                            0, 3055,
                            _hc.compress_gzip(_hc.encode_sync_event(pkt_ps)), 1,
                            _hc.client_session_guid(self))
                        self.scnt += 1
                        self.send({
                            "issuer": f"0.0.0.0.ServiceGameSession.246."
                                      f"{session.session_id}.{self.scnt}",
                            "target": "ServiceGameSession",
                            "instance": str(session.server_id),
                            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                        }, dw_ps)
                        _hc.log_req(f"    RequestPrioritySync (PvP): PickGoesFirst greenlight to winner {my_pid}")
                    else:
                        self._push_transaction_ack(session)
                        _hc.log_req(f"    RequestPrioritySync (PvP): PickGoesFirst — {my_pid} is not the winner, acked")
                else:
                    game_ps.push_turn_phase(phase, turn_uid, my_uid)
                    game_ps.push_green_light(
                        my_uid, _hc.game_engine.EPriorityContext.Normal)
                    pkt_ps = game_ps.make_network_packet(my_uid)
                    dw_ps = _hc.encode_datawrapper(
                        0, 3055,
                        _hc.compress_gzip(_hc.encode_sync_event(pkt_ps)), 1,
                        _hc.client_session_guid(self))
                    self.scnt += 1
                    self.send({
                        "issuer": f"0.0.0.0.ServiceGameSession.246."
                                  f"{session.session_id}.{self.scnt}",
                        "target": "ServiceGameSession",
                        "instance": str(session.server_id),
                        "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                    }, dw_ps)
                    _hc.log_req(f"    RequestPrioritySync (PvP): re-sent GreenLight for phase {phase}")
            return True

        _be = self._checkpoint_engine(session)
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        try:
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            phase = _be.current_phase(bstate)
        except Exception:
            bstate, phase = None, None
        if bstate and phase is not None and bstate.get("turn_player") == _be.PLAYER:
            game = self._fresh_game(session, pl_t, ai_t, bstate)
            game.push_green_light(pl_t, self._priority_context_for(phase, bstate))
            game.push_player_updated(
                pl_t, champ_id=getattr(self, "_player_champ_scid", None))
            game.push_player_updated(
                ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
            self._send_battle_events(session, game, pl_t)
            if phase == _hc.game_engine.ETurnPhases.DeclareAttack:
                self._push_attack_options(session, pl_t, ai_t)
            elif phase in (_hc.game_engine.ETurnPhases.FirstMainPhase,
                           _hc.game_engine.ETurnPhases.SecondMainPhase):
                self._push_main_phase_options(session, pl_t, ai_t)
            else:
                self._push_phase_options_empty(session, pl_t, ai_t)
            _hc.log_req(f"    RequestPrioritySync: re-sent GreenLight for phase {phase}")
        elif (bstate and phase is not None
              and bstate.get("turn_player") == _be.AI
              and bstate.get("ai_turn_phase_idx") is not None):
            # During the AI's turn there is no human-side battle state in the
            # sense used by the old player-turn branch above: the AI drives the
            # phase loop server-side and only records ``ai_turn_phase_idx``
            # while waiting for the human at an opponent stop.  If the client
            # loses its local priority bit (for example after combat damage),
            # LocalPlayer requests a priority resync.  A bare 3055 ack leaves
            # the client with no Pass window and strands the AI turn.
            # Re-announce the held phase with the human as priority, then send
            # the same context/options contract as the normal AI-stop path.
            held = _be.ai_held_phase_context(bstate)
            if held is not None:
                held_phase, held_idx, held_phases = held
                # The resume cursor is written at the stop boundary and is
                # safer than a stale phase_idx left by a competing packet.
                bstate["turn_phases"] = list(held_phases)
                bstate["phase_idx"] = held_idx
                _be.save_state(session, bstate)
                phase = held_phase
            game = self._fresh_game(session, pl_t, ai_t, bstate)
            game.push_turn_phase(phase, ai_t, pl_t)
            context = (_hc.game_engine.EPriorityContext.Normal
                       if phase == _hc.game_engine.ETurnPhases.DeclareDefense
                       else _hc.game_engine.EPriorityContext.ResolveTopOfChain)
            game.push_green_light(pl_t, context)
            self._send_battle_events(session, game, pl_t)
            if phase == _hc.game_engine.ETurnPhases.DeclareDefense:
                self._push_blocker_options(session, pl_t, ai_t)
            else:
                self._push_phase_options_empty(session, pl_t, ai_t)
            _hc.log_req(f"    RequestPrioritySync: re-sent AI-stop GreenLight "
                    f"for phase {phase}")
        else:
            self._push_transaction_ack(session)
            _hc.log_req("    RequestPrioritySync: no active battle, acked")
        return True

    def _handle_choose_pick_transaction(self, session, transaction):
        """Resolve Play/Draw selection and enter the Mulligan phase."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        is_set_ability_data = transaction.is_set_ability_data
        is_ability_activate = transaction.is_ability_activate
        # PvP: chose Play or Draw — draw hands, then push Mulligan.
        if ((session.session_name or "").startswith("tourney-")
                and not is_set_ability_data
                and not is_ability_activate):
            from services.tournament_game import (pvp_load_state, pvp_save_state,
                                                  db_game_session_pids, _push_to_both_players)
            state = pvp_load_state(session)
            my_pid = int(self.client_reck_id)
            chose_draw = b"ChooseDrawTransaction" in inner_bytes
            pids = db_game_session_pids(session.session_id)
            if state and pids:
                winner_pid = state["goes_first_pid"]
                loser_pid = next(p for p in pids if p != winner_pid)
                # ONLY the coin-flip winner decides who plays/draws
                # first.  The loser's client never receives the
                # PickGoesFirst GreenLight (only the winner is the
                # active player), so it never shows the Play/Draw
                # dialog and never submits a choose transaction.
                # Requiring a pick from BOTH players therefore stalls
                # forever after the winner chooses ("chose to go first,
                # then nothing happened").  Ignore any non-winner pick
                # and resolve on the winner's first choose.
                if my_pid != winner_pid:
                    _hc.log_req(f"    PvP choose: {my_pid} (non-winner) "
                            f"pick ignored — waiting on winner "
                            f"{winner_pid}")
                    self._push_transaction_ack(session)
                    return True
                if state.get("pick_resolved"):
                    # Already resolved — this is a stale/re-sent choose
                    # from the winner; just ack to avoid double-drawing.
                    _hc.log_req(f"    PvP choose: already resolved "
                            f"(draws_first {state['draws_first_pid']}) "
                            f"— stale pick from {my_pid}")
                    self._push_transaction_ack(session)
                    return True
                # The winner picks Play -> winner plays first and the
                # loser draws first; Draw -> winner draws first and the
                # loser plays first.  The player who draws first goes
                # SECOND (the client reorders its player list so the
                # draws-first player is last / the other is "starting"),
                # so the actual first turn belongs to play_first_pid.
                from services.tournament_game import pvp_session_lock
                with pvp_session_lock(session):
                    draw_first_pid = winner_pid if chose_draw else loser_pid
                    play_first_pid = loser_pid if chose_draw else winner_pid
                    state["draws_first_pid"] = draw_first_pid
                    state["play_first_pid"] = play_first_pid
                    # The first turn goes to the play-first player, NOT the
                    # coin-flip winner — if the winner picked Draw, the
                    # loser opens the game.
                    state["turn_pid"] = play_first_pid
                    state["pids"] = [play_first_pid, draw_first_pid]
                    state["pick_resolved"] = True
                    # Advance the persisted PvP phase from PickGoesFirst(3)
                    # to Mulligan(4) BEFORE any events go out. Otherwise a
                    # client priority sync during the handoff can re-push
                    # PickGoesFirst and restore the wrong setup UI.
                    state["phase"] = 4
                    # Mulligan is an ACTIVE-player phase in C#. The player
                    # who plays first gets the first decision; the coin
                    # winner may have chosen Draw and therefore be second.
                    state["mulligan_pid"] = play_first_pid
                    state["mulligan_count"] = {}
                    pvp_save_state(session, state)

                    # Corinth starts with four shards; ordinary PvP starts
                    # with the constructed-game hand size.
                    opening_hand_size = int(
                        (getattr(session, "encounter_data", {}) or {}).get(
                            "starting_hand_size", 7) or 7)
                    for apid in pids:
                        _hc.db_game_draw_cards(session.session_id, apid,
                                           opening_hand_size)
                    _hc.log_req(f"    PvP choose: drew hands for both "
                            f"({opening_hand_size})")

                # Push hand + Mulligan to each player.
                hands = {
                    int(apid): _hc.db_game_get_hand(session.session_id, apid)
                    for apid in pids
                }
                hand_uids = [int(card_uid)
                             for hand in hands.values()
                             for card_uid, _template_guid in hand]
                hand_projections = _hc.db_card_instance_dynamic_projections(
                    session.session_id, hand_uids, conn=_hc._db)
                for apid in pids:
                    h = _hc.player_handlers.get(apid)
                    if not h: continue
                    aopp = pids[0] if pids[1] == apid else pids[1]
                    pl = _hc.game_engine.UID.make(244, apid)
                    op = _hc.game_engine.UID.make(244, aopp)
                    g = _hc.game_engine.Game(int(session.session_id), pl, op)
                    for idx, (cu, tg) in enumerate(hands[int(apid)]):
                        scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(cu)))
                        # Populate the CardDef (cost/atk/def/thresholds/
                        # abilities/gems) so the client renders the hand
                        # and the Mulligan keep/redraw dialog correctly.
                        _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                            self._card_full_data(
                                g, scid, tg,
                                instance_projection=hand_projections.get(int(cu)))
                        g.push_card_updated(scid, pl, _hc.game_engine.ECardCollections.Hand,
                                            ct, template_id=tg, gems=gem_type)
                    for idx, (cu, tg) in enumerate(hands[int(aopp)]):
                        scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(cu)))
                        _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                            self._card_full_data(
                                g, scid, tg,
                                instance_projection=hand_projections.get(int(cu)))
                        g.push_card_updated(scid, op, _hc.game_engine.ECardCollections.Hand, ct,
                                            template_id=tg, nulling=True, gems=gem_type)
                    # Tell each client who draws first and who plays
                    # first.  Order matters: the Draw event first, then
                    # the Play event — the client derives the mulligan
                    # dialog line ("Your opponent chose to Play/Draw")
                    # from the LAST event that names the OPPONENT; each
                    # player's OWN pick resets it to Unknown.  With
                    # Draw-then-Play the picker's own client shows no
                    # opponent message and the opponent's client shows
                    # the correct "chose to Play/Draw".
                    g.push_player_wishes_to_draw_first(_hc.game_engine.UID.make(244, draw_first_pid))
                    g.push_player_wishes_to_play_first(_hc.game_engine.UID.make(244, play_first_pid))
                    # MulliganState uses the same active-player contract as
                    # C#: only play_first_pid starts with the dialog and
                    # priority. Both clients receive the same global phase
                    # owner; the waiting player does not get a temporary
                    # local priority that would open a second dialog.
                    mulligan_uid = _hc.game_engine.UID.make(
                        244, play_first_pid)
                    g.push_disable_interface(apid != play_first_pid)
                    g.push_green_light(
                        mulligan_uid, _hc.game_engine.EPriorityContext.Normal)
                    g.push_turn_phase(_hc.game_engine.ETurnPhases.Mulligan,
                                      mulligan_uid, mulligan_uid)
                    pkt = g.make_network_packet(pl)
                    dw = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt)), 1,
                                            "00000000-0000-0000-0000-000000000000")
                    h.scnt += 1
                    h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
                            "target": "ServiceGameSession", "instance": str(session.server_id),
                            "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
                    _hc.log_req(f"    PvP choose: hand to pid {apid}")
            else:
                _push_to_both_players(session, self, lambda g, pt:
                    [g.push_turn_phase(_hc.game_engine.ETurnPhases.Mulligan, pt, pt),
                     g.push_green_light(pt, _hc.game_engine.EPriorityContext.Normal)], _hc.log_req)
            return True
        else:
            # The coin-flip winner chose Play or Draw. FRA can resolve this
            # branch for the AI as well as accepting the player's transaction;
            # the latter is only legal when the player won the toss.
            ai_choice = bool(getattr(transaction, "ai_choice", False))
            coin_winner_is_player = getattr(
                self, "_pve_coin_winner_is_player", True)
            if not coin_winner_is_player and not ai_choice:
                _hc.log_req("    PvE choose: ignored player pick; AI won coin toss")
                self._push_transaction_ack(session)
                return True
            chose_draw = b"ChooseDrawTransaction" in inner_bytes
            if (coin_winner_is_player and
                    getattr(self, "_pve_forced_draw_first", False)):
                if not chose_draw:
                    _hc.log_req("    PvE choose: Weight rejected Play; forcing Draw")
                chose_draw = True
                self._pve_forced_draw_first = False
            # A draw-first player draws on turn 1. The play/draw choice is
            # made by the coin-toss winner, so invert the meaning when the AI
            # won: AI Draw means the human plays first, AI Play means the
            # human draws first.
            self._pending_player_draws_first = (
                coin_winner_is_player == chose_draw)
            self._pending_play_first_is_player = (
                coin_winner_is_player != chose_draw)
            encounter_data = dict(session.encounter_data or {})
            encounter_data["_pve_ai_mulligan_done"] = False
            encounter_data["_pve_player_mulliganed"] = False
            encounter_data["_pve_play_first_is_player"] = bool(
                self._pending_play_first_is_player)
            encounter_data["_pve_player_draws_first"] = bool(
                self._pending_player_draws_first)
            if (session.session_name or "").startswith("camp_"):
                encounter_data["_campaign_mulligan_redraws"] = 0
            session.encounter_data = encounter_data
            session._persist()
            pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
            game = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
            # Draw the opening hands NOW — like PvP, PreGame and
            # PickGoesFirst show empty hands; each player draws after the
            # Play/Draw pick. Campaign uses the class/talent opening-hand
            # value for the player; the AI keeps the normal seven cards.
            # Campaign/PvE uses the campaign mulligan rule: the first redraw
            # replaces the opening hand at the same size.  Keep this counter
            # in the persisted session metadata so it survives the separate
            # transaction that carries the redraw request.  PvP retains its
            # existing one-fewer-card mulligan rule.
            if not _hc.db_game_get_hand(session.session_id,
                                    self.user_profile["id"]):
                _hc.db_game_draw_cards(session.session_id,
                                   self.user_profile["id"],
                                   self._starting_hand_size(session))
                _hc.db_game_draw_cards(session.session_id, 0, 7)
            player_hand = _hc.db_game_get_hand(session.session_id,
                                           self.user_profile["id"])
            ai_hand = _hc.db_game_get_hand(session.session_id, 0)
            opening_hand_projections = _hc.db_card_instance_dynamic_projections(
                session.session_id,
                [int(card_uid) for hand in (player_hand, ai_hand)
                 for card_uid, _template_guid in hand],
                conn=_hc._db)
            for idx, (cu, tg) in enumerate(player_hand):
                scid = _hc.game_engine.SessionCardId(
                    _hc.game_engine.UID(int(cu)))
                _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                    self._card_full_data(
                        game, scid, tg,
                        instance_projection=opening_hand_projections.get(int(cu)))
                game.push_card_updated(
                    scid, pl_t, _hc.game_engine.ECardCollections.Hand,
                    ct, template_id=tg, gems=gem_type)
            for idx, (cu, tg) in enumerate(ai_hand):
                scid = _hc.game_engine.SessionCardId(
                    _hc.game_engine.UID(int(cu)))
                _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                    self._card_full_data(
                        game, scid, tg,
                        instance_projection=opening_hand_projections.get(int(cu)))
                game.push_card_updated(
                    scid, ai_t, _hc.game_engine.ECardCollections.Hand,
                    ct, template_id=tg, nulling=True, gems=gem_type)
            if ai_choice:
                if chose_draw:
                    game.push_player_wishes_to_draw_first(ai_t)
                else:
                    game.push_player_wishes_to_play_first(ai_t)
            play_first_is_player = bool(
                self._pending_play_first_is_player)
            ai_mulligan_done = _hc._push_pve_mulligan_start(
                game, pl_t, ai_t, play_first_is_player,
                lambda: self._resolve_ai_mulligan(session, game, ai_t))
            if ai_mulligan_done:
                encounter_data = dict(session.encounter_data or {})
                encounter_data["_pve_ai_mulligan_done"] = True
                session.encounter_data = encounter_data
                session._persist()
            self._sync_rules_port_setup_checkpoint(
                session, _hc.game_engine.ETurnPhases.Mulligan, pl_t)
            if game.events:
                pkt = game.make_network_packet(pl_t)
                dw = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt)), 1,
                                        "00000000-0000-0000-0000-000000000000")
                self._game_scnt = max(self._game_scnt, self.scnt) + 1
                self.scnt = self._game_scnt
                gs_inst = str(session.server_id)
                self.send({
                    "issuer": f"0.0.0.0.ServiceGameSession.{_hc.SERVICE_GAME_SESSION_UID}.{gs_inst}.{self.scnt}",
                    "target": "ServiceGameSession", "instance": gs_inst,
                    "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                }, dw)
                _hc.log_req(f"    Pushed Mulligan phase ({len(dw)}b)")
            return True

    def _handle_mulligan_keep_transaction(self, session, transaction):
        """Resolve an accepted opening hand for PvP or Practice."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        handled = False
        if (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state,
                pvp_mulligan_next, _pvp_push_mulligan_prompt,
                db_game_session_pids)
            # Track which player kept
            state = pvp_load_state(session)
            my_pid = int(self.client_reck_id)
            if state:
                from services.tournament_game import pvp_session_lock
                with pvp_session_lock(session):
                    # A keep can race with a retry or phase handoff. Reload
                    # under the session lock so a stale snapshot cannot roll
                    # the durable phase back after the first turn has begun.
                    state = pvp_load_state(session) or state
                    kept = state.get("kept") or []
                    mulligan_counts = state.get("mulligan_count") or {}
                    mulliganed = bool(int(mulligan_counts.get(
                        str(my_pid), mulligan_counts.get(my_pid, 0)) or 0))
                    # The client can resend AcceptStartingHand while
                    # the first response is still animating.  Treat a
                    # duplicate as an idempotent acknowledgement: an
                    # extra AcceptedStartingHand/priority packet can
                    # pop the visible Mulligan dialog on the opponent.
                    if my_pid in kept:
                        _hc.log_req(f"    PvP: duplicate keep from "
                                f"{my_pid}; ignored")
                    else:
                        kept.append(my_pid)
                        state["kept"] = kept
                        pvp_save_state(session, state)
                        # Tell BOTH clients the keeper's hand is
                        # accepted. The opponent also needs the event
                        # to recompute the keeper's deck count.
                        pl_t = _hc.game_engine.UID.make(244, my_pid)
                        pids = db_game_session_pids(session.session_id)
                        other_pid = pids[0] if pids[1] == my_pid else pids[1]
                        other_uid = _hc.game_engine.UID.make(244, other_pid)
                        for _keep_pid in pids:
                            _keep_h = _hc.player_handlers.get(_keep_pid)
                            if not _keep_h:
                                continue
                            g = _hc.game_engine.Game(int(session.session_id), pl_t, other_uid)
                            # The keeper has finished their decision.  Disable
                            # both clients while the other player's mulligan
                            # decision is pending; the next prompt enables
                            # only its asker.
                            g.push_disable_interface(True)
                            g.push_accepted_starting_hand(
                                pl_t, mulliganed=mulliganed)
                            pkt = g.make_network_packet(pl_t)
                            dw = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt)), 1,
                                                     "00000000-0000-0000-0000-000000000000")
                            _keep_h.scnt += 1
                            _keep_h.send({
                                "issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{_keep_h.scnt}",
                                "target": "ServiceGameSession",
                                "instance": str(session.server_id),
                                "reqid": 0, "c": 0, "conh": 0, "sid": _keep_h.sid,
                            }, dw)
                        _hc.log_req(f"    PvP: player {my_pid} kept hand (pushed to both)")
                        from services.tournament_game import pvp_mulligan_next
                        pvp_mulligan_next(session, state, my_pid)
            return True
        else:
            pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
            # The selected Fortune is consumed when the human finishes the
            # opening-hand decision.  ReadyToStartGame has its own local
            # campaign-id variable, so derive it again here rather than
            # relying on that unrelated handler scope.
            try:
                camp_id = int((session.session_name or "camp_0").split("_")[-1]
                              or 0) if (session.session_name or "").startswith(
                                  "camp_") else 0
            except (TypeError, ValueError):
                camp_id = 0
        if not handled:
            game = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
            game.player_resources = 0
            game.player_total_resources = 0
            game.max_hand_size = self._max_hand_size(session)
            setup_encounter = dict(session.encounter_data or {})
            ai_mulligan_done = bool(setup_encounter.get(
                "_pve_ai_mulligan_done", False))
            player_mulliganed = bool(setup_encounter.get(
                "_pve_player_mulliganed", False))
            play_first_is_player = bool(setup_encounter.get(
                "_pve_play_first_is_player",
                getattr(self, "_pending_play_first_is_player", True)))
            player_draws_first = setup_encounter.get(
                "_pve_player_draws_first",
                getattr(self, "_pending_player_draws_first", None))
            # Get cards in hand
            rows = _hc.db_hand_cards_raw(session.session_id, self.user_profile["id"])
            hand_cards = []  # list of (scid, cost)
            for r in rows:
                scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(r[0]))
                instance_id = r[1]
                # Resolve template data via the stored template_guid — one
                # path for both instance-based (FRA) and GUID (campaign/AI)
                # cards.
                t = self._template_by_guid(r[2])
                if t:
                    tpl_guid, ct_name, _name, cost, atk, def_ = t
                    ct = _hc.game_engine.card_type_from_db(ct_name)
                else:
                    tpl_guid = "00000000-0000-0000-0000-000000000000"
                    ct = _hc.game_engine.ECardTypes.Troop
                    cost, atk, def_ = 0, 0, 0
                hand_cards.append((scid, cost))
                # Full card data from the template via _card_full_data so
                # the re-pushed hand card keeps its thresholds, abilities,
                # ATTRIBUTES (e.g. Flight) and any stat mods — a bare
                # CardDef("Card", ...) without attributes wipes the Flight
                # keyword the client renders from the attributes bitmask.
                _tpl_g, ct, _nm, _c, _a, _d, gem_type = self._card_full_data(
                    game, scid, r[2], instance_id)
                game.push_card_updated(scid, pl_t, _hc.game_engine.ECardCollections.Hand, ct,
                    template_id=r[2], gems=gem_type)

            # The human kept: push the starting-hand acceptance + the AI's
            # mulligan decision, then enter the StartGame phase. The server
            # pushes ONE phase at a time; StartTurn/Ready/Prep/Draw/
            # FirstMainPhase are advanced one-by-one as the client (auto-)
            # passes, driven by the 3029 PassPriority handler.
            if (session.session_name or "").startswith("camp_"):
                _hc.campaign.apply_starting_hand_talents(
                    self, _hc._db, session, game, pl_t,
                    getattr(self, "_campaign_starting_hand_effects", []))
            game.push_accepted_starting_hand(
                pl_t, mulliganed=player_mulliganed)
            # A human keep passes priority to the AI if it has not already
            # completed its active-first mulligan during setup.
            if not ai_mulligan_done:
                game.push_green_light(
                    ai_t, _hc.game_engine.EPriorityContext.Normal)
                game.push_turn_phase(
                    _hc.game_engine.ETurnPhases.Mulligan, ai_t, ai_t)
                self._resolve_ai_mulligan(session, game, ai_t)
            setup_encounter = dict(session.encounter_data or {})
            for transient_key in (
                    "_pve_ai_mulligan_done",
                    "_pve_player_mulliganed",
                    "_pve_play_first_is_player",
                    "_pve_player_draws_first"):
                setup_encounter.pop(transient_key, None)
            session.encounter_data = setup_encounter
            session._persist()
            first_turn_uid = pl_t if play_first_is_player else ai_t
            game.push_turn_phase(_hc.game_engine.ETurnPhases.StartGame,
                                 first_turn_uid, first_turn_uid)
            # Initialise the DB-backed battle state with the player who won the
            # subsequent Play/Draw choice. (The choice can belong to the AI.)
            # The client shows NO interaction during StartGame (no BattleState
            # is pushed for it), so a pass can never arrive — the server
            # advances StartGame -> StartTurn itself below.
            from rules_port import lifecycle as _be
            bstate = _be.default_state(
                turn_player=_be.PLAYER if play_first_is_player else _be.AI)
            setup_encounter = session.encounter_data or {}
            fra_start_state = (setup_encounter.get(
                "_fra_starting_battle_state", {})
                if isinstance(setup_encounter, dict) else {})
            bstate["player_resources"] = int(
                fra_start_state.get("player_resources", 0) or 0)
            bstate["player_total_resources"] = int(
                fra_start_state.get("player_total_resources", 0) or 0)
            bstate["ai_resources"] = int(
                fra_start_state.get("ai_resources", 0) or 0)
            bstate["ai_total_resources"] = int(
                fra_start_state.get("ai_total_resources", 0) or 0)
            for side in ("player", "ai"):
                thresholds = fra_start_state.get(f"{side}_threshold", {})
                bstate[f"{side}_threshold"] = {
                    int(color): int(value or 0)
                    for color, value in (thresholds or {}).items()
                }
            bstate["ai_resource_played_this_turn"] = False
            bstate["ai_resource_plays_this_turn"] = 0
            bstate["player_resource_plays_this_turn"] = 0
            bstate["player_charges"] = int(fra_start_state.get(
                "player_charges", getattr(self, "_player_starting_charges", 0)) or 0)
            bstate["ai_charges"] = int(fra_start_state.get(
                "ai_charges", getattr(self, "_ai_starting_charges", 0)) or 0)
            # Champion starting health (persisted for combat + reconnect).
            bstate["player_health"] = int(fra_start_state.get(
                "player_health", getattr(self, "_player_starting_health", 20)) or 20)
            bstate["ai_health"] = int(fra_start_state.get(
                "ai_health", getattr(self, "_ai_starting_health", 10)) or 10)
            # Attach before opening GameStarted triggers.  Otherwise the first
            # gameplay event would be resolved by the compatibility scanner,
            # leaving the session with a mixed native/legacy lifecycle from
            # the very first chain item.
            self._maybe_attach_rules_port(session, game, bstate)
            # A re-entrant attach refreshes this fresh seed with the shared
            # checkpoint created during setup, before PreGame modifiers ran.
            # That snapshot has no charge count, so re-apply the PreGame seed
            # (e.g. Fury's +2 charges) here or the starting charges are lost.
            bstate["player_charges"] = int(fra_start_state.get(
                "player_charges", getattr(self, "_player_starting_charges", 0)) or 0)
            bstate["ai_charges"] = int(fra_start_state.get(
                "ai_charges", getattr(self, "_ai_starting_charges", 0)) or 0)
            # Reapplying the challenge snapshot after RulesPort attachment
            # mirrors the charge restoration above and keeps the saved
            # round-zero resource/health mods from being overwritten by the
            # setup checkpoint created before they were applied.
            for key in ("player_resources", "player_total_resources",
                        "ai_resources", "ai_total_resources",
                        "player_health", "ai_health"):
                if key in fra_start_state:
                    bstate[key] = int(fra_start_state.get(key, 0) or 0)
            for side in ("player", "ai"):
                key = f"{side}_threshold"
                if key in fra_start_state:
                    bstate[key] = {
                        int(color): int(value or 0)
                        for color, value in
                        (fra_start_state.get(key) or {}).items()
                    }
            native_port = getattr(session, "_rules_port_session", None)
            if native_port is not None:
                # The setup packet contains the client's non-interactive
                # StartGame phase, but the cached native scheduler is still
                # sitting on its Mulligan priority action. Leave that action
                # before invoking either the human or AI turn driver.
                if native_port.begin_first_turn(first_turn_uid):
                    _hc.log_req(
                        "    RulesPort began first turn: "
                        f"active={native_port.active_player_id!r}")
            # Fortune readings are opponent-owned encounter effects. Resolve
            # the selected card through the normal metadata/BOM path so its
            # target templates choose the player's champion for
            # EachOpposingChampion, while EachChampion effects still affect
            # both champions. Sapphire's opening-hand modifier was applied
            # before the hand was dealt above.
            fortune_ability = getattr(
                self, "_campaign_fortune_ability_guid", None)
            fortune_guid = getattr(self, "_campaign_fortune_guid", None)
            if fortune_ability:
                self._current_bstate = bstate
                try:
                    from rules_port.resolution import resolve_port_ability
                    fortune_source = int(self._ai_champ_scid.uid.uid64)
                    fortune_log = resolve_port_ability(
                        self, game, session, _hc._db, pl_t, ai_t, bstate,
                        fortune_ability, fortune_source, 0, target_map={})
                    _hc.log_req(f"    Fortune {fortune_guid}: {fortune_log}")
                except Exception as exc:
                    _hc.log_req(f"    Fortune {fortune_guid} error: {exc}")
                finally:
                    for key in ("player_mod_target", "player_spell_target",
                                "resolving_target_uid", "resolving_effect_guid"):
                        bstate.pop(key, None)
                    _hc.campaign.consume_fortune(_hc._db, camp_id, fortune_guid)
                    self._campaign_fortune_guid = None
                    self._campaign_fortune_ability_guid = None
            game.player_threshold = dict(bstate.get("player_threshold", {}))
            game.player_charges = bstate.get("player_charges", 0)
            game.player_resources = bstate.get("player_resources", 0)
            game.player_total_resources = bstate.get("player_total_resources", 0)
            game.ai_threshold = dict(bstate.get("ai_threshold", {}))
            game.ai_charges = bstate.get("ai_charges", 0)
            game.ai_resources = bstate.get("ai_resources", 0)
            game.ai_total_resources = bstate.get("ai_total_resources", 0)
            game.player_health = bstate.get("player_health", 20)
            game.ai_health = bstate.get("ai_health", 10)
            # Apply any phase stops the client configured before the battle
            # state existed (SetTurnPhasesTransaction during setup), or the
            # player's saved preferences from previous battles.
            if getattr(self, "_pending_player_stops", None) is not None:
                pending_stops = self._pending_player_stops
                if (isinstance(pending_stops, (tuple, list)) and
                        len(pending_stops) >= 2):
                    bstate["player_self_stops"], bstate["player_opp_stops"] = pending_stops[:2]
                self._pending_player_stops = None
            else:
                saved_s, saved_o = self._load_player_stops(self.user_profile["id"])
                if saved_s is not None:
                    bstate["player_self_stops"] = saved_s
                if saved_o is not None:
                    bstate["player_opp_stops"] = saved_o
            # A draw-first player draws a card on their first turn.
            if player_draws_first is not None:
                bstate["player_draws_first_turn"] = bool(player_draws_first)
            self._pending_player_draws_first = None
            self._pending_play_first_is_player = None
            bstate["turn_player"] = (
                _be.PLAYER if play_first_is_player else _be.AI)
            _be.save_state(session, bstate)
            # "At the start of the game" triggers for both players'
            # opening-hand / warzone cards (e.g. Princess Victoria).
            game_started_event_start = len(game.events)
            # One dispatch: trigger discovery already scans both sides, so
            # dispatching once per owner ran every start-of-game ability twice
            # (Zuba's Expendable Lives granted its Deathcry twice, and Early
            # Sprouts' resources were applied twice).
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate,
                "GameStartedEvent", None, self.user_profile["id"],
                zones=("hand", "warzone"))
            # GameStarted triggers are mandatory setup work, not a priority
            # window.  Drain their normal chain in LIFO order before the
            # StartGame packet is sent; otherwise _advance_to_priority leaves
            # the client waiting for a manual pass during the opening flow.
            # A real choice (target, revealed card, or discard) still pauses
            # here and is sent to the client normally.
            self._autopass_game_started_chain(
                session, pl_t, ai_t, bstate, game)
            if (_be.stack_empty(bstate) and
                    not bstate.get("pending_choice") and
                    not bstate.get("pending_deck_search") and
                    not bstate.get("pending_trigger") and
                    not bstate.get("pending_discard_ability") and
                    not bstate.get("pending_conversation")):
                self._suppress_completed_game_started_chain_events(
                    game, game_started_event_start)
            # GameStarted abilities may change the DB-backed battle state.  In
            # particular, encounter-scene battleboard cards such as Savage
            # Lord's Resource Rich grant both current and permanent resources.
            # Persist after resolving them so reconnects and the next phase
            # load the same 1/1 state that was sent in the setup packet.
            _be.save_state(session, bstate)
            # Send the StartGame packet (one phase per packet).
            if game.events:
                pkt = game.make_network_packet(pl_t)
                dw = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt)), 1, "00000000-0000-0000-0000-000000000000")
                self._game_scnt = max(self._game_scnt, self.scnt) + 1
                self.scnt = self._game_scnt
                gs_inst = str(session.server_id)
                self.send({
                    "issuer": f"0.0.0.0.ServiceGameSession.{_hc.SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                    "target": "ServiceGameSession", "instance": gs_inst, "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                }, dw)
                self._event_q.append((self.scnt, dw, {}))
                _hc.log_req(f"    Pushed StartGame phase after keep ({len(dw)}b)")
            # Auto-pass non-stop phases (StartTurn -> Ready -> Prep -> Draw)
            # until the next stop phase (FirstMainPhase).  PvP sessions
            # advance via route_pvp_pass instead — calling the AI-turn
            # path here overwrites the PvP state with a human-vs-AI
            # battle state (turn_player etc.) and desyncs GreenLight.
            if not (session.session_name or "").startswith("tourney-"):
                if bstate.get("turn_player") == _be.AI:
                    self._run_ai_turn(session, pl_t, ai_t, bstate)
                else:
                    self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_discard_transaction(self, session, transaction, *, card_uid=None):
        """Process a discard selection and acknowledge it."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        is_set_ability_data = transaction.is_set_ability_data
        is_ability_activate = transaction.is_ability_activate
        if ((session.session_name or "").startswith("tourney-")
                and not is_set_ability_data
                and not is_ability_activate):
            # Tournament PvP has its own persisted two-player phase
            # state.  Never let a discard transaction fall through to
            # the human-vs-AI battle_engine path: that would overwrite
            # turn_order_json and can jump the clients back to First
            # Main or even emit a false deck-out victory.
            from services.tournament_game import pvp_handle_discard
            handled = pvp_handle_discard(self, session, inner_bytes)
            if handled:
                self._push_transaction_ack(session)
                return True
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        gd = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
        card_uids = []
        if card_uid is not None:
            try:
                card_uids = [int(getattr(card_uid, "uid64", card_uid))]
            except (TypeError, ValueError):
                card_uids = []
        elif isinstance(inner_bytes, bytes):
            for m in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    card_uid = struct.unpack('<Q', bytes.fromhex(m.group(1).decode()))[0]
                    # Only Card-type UIDs (the champion/player UIDs are
                    # type 0/3 and must NOT be discarded).
                    if (card_uid & 0xFF) != 1:
                        continue
                except Exception:
                    continue
                card_uids.append(int(card_uid))
        for card_uid in card_uids:
            try:
                # Typed RulesPort discard IDs and raw legacy IDs share the
                # same owner/hand validation in the domain helper.
                if (int(card_uid) & 0xFF) != 1:
                    continue
                gd_owner, _owner_uid = self._discard_card_to_owner(
                    session, pl_t, ai_t, int(card_uid))
                if gd_owner:
                    gd.events.extend(gd_owner.events)
                _hc.log_req(f"    Discarded card {card_uid}")
            except Exception:
                continue
        _hc._db.commit()
        if gd.events:
            self._send_battle_events(session, gd, pl_t)
        # Discard down to the max hand size (7): each DiscardTransaction
        # moves ONE card, so stay in the Discard phase until the hand fits.
        # Only then advance to EndTurn.
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        hand_count = _hc.db_hand_card_count(session.session_id, self.user_profile["id"])
        if bstate.get("pending_discard_native"):
            # Stargazer-style discard is a temporary native discard window,
            # not an end-of-turn hand-limit discard. Restore the phase and
            # clear the continuation after exactly one DiscardTransaction.
            return_phase_idx = bstate.pop("pending_discard_return_phase_idx", bstate.get("phase_idx", 0))
            bstate.pop("pending_discard_native", None)
            bstate.pop("pending_discard_ability", None)
            bstate.pop("pending_discard_ability_instance_id", None)
            bstate.pop("pending_discard_target_template", None)
            bstate.pop("pending_discard_scid", None)
            bstate["phase_idx"] = return_phase_idx
            _be.save_state(session, bstate)
            self._advance_to_priority(session, pl_t, ai_t, bstate)
            return True
        if (hand_count > self._max_hand_size(session) and
                not self._hand_size_unlimited(session, bstate)):
            g2 = self._fresh_game(session, pl_t, ai_t, bstate)
            g2.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
            g2.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
            g2.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
            self._send_battle_events(session, g2, pl_t)
            _hc.log_req(f"    Still over hand limit ({hand_count} cards) — re-granted priority to discard more")
        else:
            _be.advance_phase(bstate)
            _be.save_state(session, bstate)
            self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_mulligan_redraw_transaction(self, session, transaction):
        """Redraw the opening hand for PvP or Practice."""
        import hconnect_server as _hc
        handled = False
        # PvP tournament: use reckoning id for game_cards.user_id.
        if (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import (
                db_game_session_pids, pvp_load_state, pvp_save_state,
                pvp_session_lock)
            import random as _rp
            my_pid = int(self.client_reck_id)
            pl_t = _hc.game_engine.UID.make(244, my_pid)
            pids = db_game_session_pids(session.session_id)
            opp_pid = pids[0] if pids[1] == my_pid else pids[1]
            opp_t = _hc.game_engine.UID.make(244, opp_pid)
            from pvp_db import (db_rules_port_hand_rows,
                                db_rules_port_redraw_hand)
            hand_rows = db_rules_port_hand_rows(session.session_id, my_pid)
            redraw_count = len(hand_rows)
            draw_count = max(0, redraw_count - 1)
            _hc.log_req(f"    PvP mulligan redraw: {redraw_count} -> {draw_count} cards")
            # Save old card_uids so we can push them back to deck.
            old_scids = [_hc.game_engine.UID(row[1]) for row in hand_rows]
            _old_rows, new_rows = db_rules_port_redraw_hand(
                session.session_id, my_pid, draw_count)
            with pvp_session_lock(session):
                state = pvp_load_state(session)
                if state is not None:
                    mulligan_counts = dict(state.get("mulligan_count") or {})
                    key = str(my_pid)
                    mulligan_counts[key] = int(
                        mulligan_counts.get(key,
                                            mulligan_counts.get(my_pid, 0)) or 0) + 1
                    state["mulligan_count"] = mulligan_counts
                    pvp_save_state(session, state)
            # Push the updated hand to both clients. A priority handoff is
            # sent only after the player keeps or is auto-accepted at zero.
            for pid in pids:
                h = _hc.player_handlers.get(pid)
                if not h: continue
                is_me = (pid == my_pid)
                p_uid = _hc.game_engine.UID.make(244, pid)
                o_uid = _hc.game_engine.UID.make(244, opp_pid if is_me else my_pid)
                g = _hc.game_engine.Game(int(session.session_id), p_uid, o_uid)
                from rules_port.host_mutations import project_mulligan_cards
                project_mulligan_cards(
                    self, g, pl_t, _old_rows, new_rows,
                    new_player_uid=p_uid, reveal_new=is_me)
                # A C# MulliganTransaction redraw does not pass priority.
                # Keep the redrawer enabled and leave the opponent waiting;
                # only AcceptStartingHand hands priority to the other player.
                g.push_disable_interface(pid != my_pid)
                # The client uses this event to clear the local
                # BattleStateMulligan animation and refresh the hand
                # count.  CardMoved/CardUpdated alone can leave the
                # redraw state showing the old hand.
                g.push_player_mulliganed_hand(pl_t, draw_count)
                pkt = g.make_network_packet(p_uid)
                dw = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt)), 1,
                                        "00000000-0000-0000-0000-000000000000")
                h.scnt += 1
                h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
                        "target": "ServiceGameSession", "instance": str(session.server_id),
                        "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
                _hc.log_req(f"    PvP mulligan: pushed redrawn hand to pid {pid}")
            if draw_count == 0:
                # C# auto-accepts a zero-card hand and passes priority.
                return self._handle_mulligan_keep_transaction(
                    session, transaction)
            # Redrawing retains the active player's priority and Mulligan UI.
            handled = True
        if not handled:
            pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
            import random as _random
            import json as _redraw_json
            game2 = _hc.game_engine.Game(session.session_id, pl_t, ai_t)

            # Fetch deck's active_gems ONCE (not per card)
            deck_gems = {}
            arena_lookup = _hc.db_get_arena_state(self.user_profile["id"])
            deck_lookup_id = self._resolve_fra_deck_id(
                arena_lookup["deck_id"]) or 0
            from pvp_db import db_deck_active_gems
            active_gems = db_deck_active_gems(deck_lookup_id)
            if active_gems:
                try:
                    import json as _gem_json
                    deck_gems = _gem_json.loads(active_gems)
                except Exception:
                    pass

            # Count cards in hand
            from pvp_db import db_rules_port_hand_rows
            hand_rows = db_rules_port_hand_rows(
                session.session_id, self.user_profile["id"])
            redraw_count = len(hand_rows)
            is_campaign = (session.session_name or "").startswith("camp_")
            if is_campaign:
                encounter_data = dict(session.encounter_data or {})
                try:
                    redraws = max(0, int(encounter_data.get(
                        "_campaign_mulligan_redraws", 0) or 0))
                except (TypeError, ValueError):
                    redraws = 0
                # The first campaign redraw is penalty-free.  Preserve the
                # normal shrinking-hand behavior if a client attempts a
                # second redraw before accepting its hand.
                draw_count = (redraw_count if redraws == 0 else
                              max(0, redraw_count - 1))
                encounter_data["_campaign_mulligan_redraws"] = redraws + 1
                session.encounter_data = encounter_data
                session._persist()
            else:
                draw_count = max(0, redraw_count - 1)

            # Persist the redraw atomically; this RulesPort domain operation
            # returns the old/new rows for the client event projection below.
            from pvp_db import db_rules_port_redraw_hand
            old_rows, new_cards = db_rules_port_redraw_hand(
                session.session_id, self.user_profile["id"], draw_count)
            encounter_data = dict(session.encounter_data or {})
            encounter_data["_pve_player_mulliganed"] = True
            session.encounter_data = encounter_data
            session._persist()
            from rules_port.host_mutations import project_mulligan_cards
            project_mulligan_cards(
                self, game2, pl_t, old_rows, new_cards,
                move_new=False, draw_new=True, update_old_first=False)

            game2.push_player_mulliganed_hand(pl_t, len(new_cards))

            if game2.events:
                pkt = game2.make_network_packet(pl_t)
                dw = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt)), 1, "00000000-0000-0000-0000-000000000000")
                self._game_scnt = max(self._game_scnt, self.scnt) + 1
                self.scnt = self._game_scnt
                gs_inst = str(session.server_id)
                self.send({
                    "issuer": f"0.0.0.0.ServiceGameSession.{_hc.SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                    "target": "ServiceGameSession", "instance": gs_inst, "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                }, dw)
                self._event_q.append((self.scnt, dw, {}))
                _hc.log_req(f"    Mulligan redraw: {redraw_count} back, {draw_count} drawn")
            if draw_count == 0:
                # AuthoritativeSessionBase.Mulligan accepts the hand and
                # passes priority immediately when the reduced size is zero.
                return self._handle_mulligan_keep_transaction(
                    session, transaction)
            handled = True
            # MulliganTransaction retains priority in C#. The AI is asked only
            # after the human accepts, if the AI has not already gone first.
        return True

    def _handle_generic_player_transaction(self, session, transaction,
                                      session_id, comp, conh,
                                      *, from_rules_port=False):
        """Handle a non-specialized player transaction after classification."""
        import hconnect_server as _hc
        # A typed request that has entered RulesPort must never be silently
        # reinterpreted by this compatibility dispatcher.  The one permitted
        # call is the explicit domain-mutation adapter above, which keeps the
        # persistence/event projection separate from RulesPort validation and
        # ordering.
        if (not from_rules_port and
                getattr(session, "_rules_port_dispatch_command", None) is
                transaction):
            return False
        inner_bytes = transaction.inner_bytes
        typed_payload = getattr(transaction, "typed_payload", None)
        txn_info = transaction.fields
        is_quit = "m_QuitEntireSeries" in txn_info
        is_surr = "m_Surrendered" in txn_info
        is_pass_priority = transaction.is_pass_priority
        player_uid = _hc.make_uid(_hc.UID_TYPE["ServicePlayer"], int(self.client_reck_id))
        gs_instance = str(session.server_id)
        handled = False
        gs_instance = str(session.server_id)
        # Only end game on explicit quit/concede. The presence of the
        # m_QuitEntireSeries / m_Surrendered field marks a
        # QuitGameTransaction (the Withdraw/Concede button sends
        # m_QuitEntireSeries=false for a normal concede, so we must NOT
        # gate on the boolean value — the field's presence is the signal).
        is_quit = "m_QuitEntireSeries" in txn_info
        is_surr = "m_Surrendered" in txn_info
        # PvP tournament: route concessions through the shared end-game
        # path so both players receive the GameEnded banner.
        if not handled and session and (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import (
                pvp_concede, pvp_handle_transaction,
            )
            try:
                if is_quit or is_surr:
                    handled = pvp_concede(self, session)
                else:
                    handled = pvp_handle_transaction(
                        self, session, inner_bytes,
                        typed_payload=getattr(transaction, "typed_payload", None))
            except Exception as _e:
                # Never let a PvP transaction exception kill this thread
                # (it would RST both clients).  Log, ack, and continue.
                import traceback
                _hc.log_req(f"    PvP txn exception: {_e}")
                traceback.print_exc()
                handled = True
        # PvP transactions are fully handled by tournament_game (cards,
        # combat, resources, deck search).  The human-vs-AI fallback
        # below must NOT run for them — it loads a battle_engine state
        # (turn_player etc.) into turn_order_json, clobbering the PvP
        # state machine and desyncing GreenLight.
        if handled and session and (session.session_name or "").startswith("tourney-"):
            if not is_pass_priority:
                self._push_transaction_ack(session)
                return True
        if not is_quit and not is_surr:
            _hc.log_req(f"    Normal transaction — processing card play/pass")

            # Try to identify which card was played from the transaction
            played_card_uid = None
            if isinstance(inner_bytes, bytes):
                scid_pos = inner_bytes.find(b"m_SessionCardId")
                if scid_pos >= 0:
                    # Find the UID value after m_SessionCardId struct
                    uid_pos = inner_bytes.find(b"m_UID64", scid_pos)
                    if uid_pos >= 0:
                        rest = inner_bytes[uid_pos + 7:]
                        parts = rest.split(b";", 6)
                        if len(parts) >= 4:
                            try:
                                hex_val = parts[4].decode("ascii", errors="replace")
                                played_card_uid = struct.unpack('<Q', bytes.fromhex(hex_val))[0]
                            except: pass

            # Find the card in DB to get its type and template GUID
            is_resource_play = False
            is_troop_play = False
            shard_color = None
            shard_ability = None
            shard_tpl = None
            resource_choice_ability = None
            played_card_type = None
            crow = None
            cur_grant = 0
            max_grant = 0
            if played_card_uid:
                from pvp_db import db_rules_port_card_projection
                crow = db_rules_port_card_projection(
                    session.session_id, int(played_card_uid))
                if crow:
                    played_card_type = crow[1]
                    cur_grant = int(crow[4] or 0)
                    max_grant = int(crow[5] or 0)
                    if crow[1] == 'Resource':
                        is_resource_play = True
                        # Validate the authored per-turn allowance before the
                        # compatibility projection moves the card out of the
                        # hand. This keeps an extra-resource card in hand
                        # when a stale client submits a now-illegal play.
                        import battle_engine as _resource_be
                        _resource_state = _resource_be.load_state(session)
                        from rules_port.resources import (
                            can_play_resource, resource_play_limit)
                        _resource_limit = resource_play_limit(
                            _hc._db, session.session_id, _resource_state,
                            int(self.user_profile.get("id", 0)))
                        if not can_play_resource(
                                _resource_state, "player",
                                limit=_resource_limit):
                            _hc.log_req(
                                "    Rejected resource play: per-turn allowance used")
                            return True
                        shard_color = crow[2].split()[0]
                        # Shards of Fate ("Choose a Standard resource in
                        # your deck. Gain the thresholds it provides.")
                        # is data-driven: an ability chain targeting a
                        # Standard RESOURCE in the DECK.  It must NOT
                        # behave like a basic shard (+1 max/current
                        # resource, +1 charge, fixed threshold).
                        if crow[3]:
                            try:
                                _pl_ags = json.loads(crow[3])
                            except Exception:
                                _pl_ags = []
                            shard_ability, shard_tpl = \
                                self._shards_of_fate_template(_pl_ags)
                            if not shard_tpl:
                                from abilities.framework.resources import (
                                    printed_resource_choice_ability)
                                resource_choice_ability = \
                                    printed_resource_choice_ability(_pl_ags)
                    elif _hc.game_engine.card_type_from_db(crow[1]) & (
                            _hc.game_engine.ECardTypes.Troop |
                            _hc.game_engine.ECardTypes.Artifact |
                            _hc.game_engine.ECardTypes.Constant |
                            _hc.game_engine.ECardTypes.BasicAction |
                            _hc.game_engine.ECardTypes.QuickAction):
                        is_troop_play = True
                    # Remove this card from the hand by setting its position beyond the deck
                    from pvp_db import db_set_card_played_to_zone
                    db_set_card_played_to_zone(session.session_id, int(played_card_uid), 'PlayedResources')
                    _hc.log_req(f"    Played {crow[2]} (uid={played_card_uid}), moving to resource zone")

            # Track resources in the DB battle state. Playing a basic
            # threshold gives: +1 total, +1 current, +1 threshold colour,
            # +1 champion charge. All state lives in turn_order_json.
            import battle_engine as _be
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
            charge_delta = 0
            if is_resource_play:
                from rules_port.resources import resource_play_count
                old_charge = int(bstate.get("player_charges", 0) or 0)
                bstate["player_resource_plays_this_turn"] = (
                    resource_play_count(bstate, "player") + 1)
                bstate["player_resource_played_this_turn"] = True
                if shard_tpl:
                    # Shards of Fate: grants max/current resources from
                    # the template (m_MaxResourcesGranted=1,
                    # m_CurrentResourcesGranted=0 — it increases MAX
                    # mana only), plus one champion charge; the controller also
                    # chooses a Standard resource and gains its
                    # threshold.  The chosen shard stays in the deck.
                    _be.save_state(session, bstate)
                    g_tmp = self._fresh_game(session, pl_t, ai_t, bstate)
                    bstate["player_total_resources"] = (
                        bstate.get("player_total_resources", 0) + max_grant)
                    bstate["player_resources"] = (
                        bstate.get("player_resources", 0) + cur_grant)
                    bstate["player_charges"] = (
                        bstate.get("player_charges", 0) + 1)
                    g_tmp.player_total_resources = bstate.get(
                        "player_total_resources", 0)
                    g_tmp.player_resources = bstate.get(
                        "player_resources", 0)
                    self._send_battle_events(session, g_tmp, pl_t)
                    _be.save_state(session, bstate)
                    _hc.log_req(f"    Shards of Fate: resource play consumed; "
                            f"+{max_grant} max/+{cur_grant} current, "
                            f"printed threshold ability pending")
                else:
                    bstate["player_total_resources"] = (
                        bstate.get("player_total_resources", 0) + max_grant)
                    bstate["player_resources"] = (
                        bstate.get("player_resources", 0) + cur_grant)
                    bstate["player_charges"] = bstate.get("player_charges", 0) + 1
                    if shard_color and not resource_choice_ability:
                        color_map = {'Ruby': _hc.game_engine.ECardShards.Ruby,
                                     'Sapphire': _hc.game_engine.ECardShards.Sapphire,
                                     'Blood': _hc.game_engine.ECardShards.Blood,
                                     'Diamond': _hc.game_engine.ECardShards.Diamond,
                                     'Wild': _hc.game_engine.ECardShards.Wild}
                        color = color_map.get(shard_color)
                        if color is not None:
                            th = bstate.setdefault("player_threshold", {})
                            th[color] = th.get(color, 0) + 1
                charge_delta = max(
                    0, int(bstate.get("player_charges", 0) or 0) - old_charge)
                _be.save_state(session, bstate)
                _hc.log_req(f"    Resource: tot={bstate.get('player_total_resources',0)} th={dict(bstate.get('player_threshold',{}))} chg={bstate.get('player_charges',0)}")

            g3 = self._fresh_game(session, pl_t, ai_t, bstate)

            # Send ResourceCardPlayed + resource/threshold/charge updates
            if is_resource_play and played_card_uid and crow:
                scid_played = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(played_card_uid)))
                # Move card to PlayedResources zone first (CardUpdated then ResourceCardPlayed)
                g3.push_card_updated(scid_played, pl_t, _hc.game_engine.ECardCollections.PlayedResources,
                                    _hc.game_engine.ECardTypes.Resource, template_id=crow[0])
                g3.push_resource_card_played(scid_played, pl_t, free=False)
                if max_grant or cur_grant:
                    # Update current resources display
                    ev_cur = _hc.game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                    ev_cur.player_id = pl_t; ev_cur.operation = 1
                    ev_cur.delta = cur_grant
                    ev_cur.new_value = bstate.get("player_resources", 0)
                    g3._push(ev_cur)
                    # Update total resources display
                    ev_tot = _hc.game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
                    ev_tot.player_id = pl_t; ev_tot.operation = 1
                    ev_tot.delta = max_grant
                    ev_tot.new_value = bstate.get("player_total_resources", 0)
                    g3._push(ev_tot)
                if not shard_tpl and not resource_choice_ability:
                    # Update threshold display for the played shard's color
                    if shard_color:
                        col_map = {'Ruby': 8, 'Sapphire': 16, 'Blood': 4, 'Diamond': 64, 'Wild': 32}
                        shard_val = col_map.get(shard_color, 32)
                        thresh_count = bstate["player_threshold"].get(shard_val, 0)
                        ev_th = _hc.game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                        ev_th.player_id = pl_t; ev_th.color = shard_val; ev_th.operation = 1; ev_th.delta = 1
                        ev_th.new_value = thresh_count
                        g3._push(ev_th)
                        from rules_port.triggers import dispatch_native_trigger
                        champion = getattr(self, "_player_champ_scid", None)
                        if champion is not None:
                            previous_color = bstate.get("gain_threshold_color")
                            bstate["gain_threshold_color"] = int(shard_val)
                            try:
                                dispatch_native_trigger(
                                    db=_hc._db, handler=self, game=g3,
                                    session=session, player_uid=pl_t,
                                    ai_uid=ai_t, battle_state=bstate,
                                    event_type="GainThresholdEvent",
                                    source_card_id=int(champion.uid.uid64),
                                    source_player_id=(
                                        self.user_profile["id"]
                                        if self.user_profile else 0),
                                    data={"gain_threshold_color": int(shard_val)})
                            finally:
                                if previous_color is None:
                                    bstate.pop("gain_threshold_color", None)
                                else:
                                    bstate["gain_threshold_color"] = previous_color
                # Playing any resource, including Shards of Fate, grants a
                # champion charge point.
                if charge_delta:
                    ev_chg = _hc.game_engine.ChampionChargePointsChangedSessionEventArgs()
                    ev_chg.player_id = pl_t; ev_chg.operation = 1
                    ev_chg.delta = charge_delta
                    ev_chg.new_value = bstate.get("player_charges", 0)
                    g3._push(ev_chg)
                    from rules_port.triggers import dispatch_native_trigger
                    champion = getattr(self, "_player_champ_scid", None)
                    if champion is not None:
                        for _ in range(max(0, int(charge_delta))):
                            dispatch_native_trigger(
                                db=_hc._db, handler=self, game=g3,
                                session=session, player_uid=pl_t,
                                ai_uid=ai_t, battle_state=bstate,
                                event_type="GainChargeEvent",
                                source_card_id=int(champion.uid.uid64),
                                source_player_id=(
                                    self.user_profile["id"]
                                    if self.user_profile else 0))
                # A resource can carry an instance-only, non-triggered
                # ability granted while it was in the deck (for example
                # Fruitful Foresight's Gain [L1][R1]).  Resolve those
                # metadata-defined additions after the normal resource grant.
                from abilities.framework.resources import (
                    resolve_granted_resource_abilities,
                    resolve_printed_resource_abilities)
                resource_owner = (self.user_profile["id"]
                                  if self.user_profile else 0)
                resource_logs = resolve_granted_resource_abilities(
                    g3, session, _hc._db, self, pl_t, ai_t, bstate,
                    int(played_card_uid), resource_owner)
                if resource_logs:
                    _hc.log_req("    Resource granted abilities: " +
                            "; ".join(resource_logs))
                if resource_choice_ability:
                    # Choice resources carry their threshold effect in the
                    # printed BOM (rather than in the resource name).  Run it
                    # through the normal resolver so the client receives the
                    # generated Choosing cards and class-23 picker.
                    from rules_port.resolution import resolve_port_ability
                    resolve_port_ability(
                        self, g3, session, _hc._db, pl_t, ai_t, bstate,
                        resource_choice_ability, int(played_card_uid),
                        resource_owner, target_map={})
                    _hc.log_req(f"    Resource choice ability: "
                            f"{resource_choice_ability[:8]}")
                printed_logs = resolve_printed_resource_abilities(
                    g3, session, _hc._db, self, pl_t, ai_t, bstate,
                    int(played_card_uid), resource_owner,
                    skip_guids=({resource_choice_ability}
                                if resource_choice_ability else ()))
                if printed_logs:
                    _hc.log_req("    Resource printed abilities: " +
                            "; ".join(printed_logs))
                # Resource-triggered abilities are added to the authoritative
                # chain in ``bstate``. Persist that mutation before the later
                # option/packet helpers reload the session; otherwise the
                # client sees the trigger animation, but the next pass loads
                # an empty stack and advances FirstMain into combat.
                _be.save_state(session, bstate)

            if not is_troop_play:
                g3.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))

            # Troop/artifact play: push to the stack (CastSpells), then
            # auto-resolve since the AI opponent always passes.
            if is_troop_play and played_card_uid and crow:
                tid = int(played_card_uid)
                scid_played = _hc.game_engine.SessionCardId(_hc.game_engine.UID(tid))
                # Get instance id for gem lookup
                from pvp_db import db_rules_port_card_instance_id
                inst_id = db_rules_port_card_instance_id(session.session_id, tid)
                # _card_full_data fills g3.card_defs with cost/atk/def/
                # thresholds/abilities/gems and returns full stats.
                tpl_guid, ct_n, nm, cost, atk, def_, gem = self._card_full_data(
                    g3, scid_played, crow[0], inst_id)
                play_plan = self._card_play_plan(
                    crow[0], tid, self.user_profile["id"]
                    if self.user_profile else 0,
                    session=session)
                if play_plan is None:
                    self._push_transaction_ack(session)
                    return True
                targets = self._extract_transaction_targets(
                    inner_bytes, played_card_uid)
                cost_selection = self._card_play_cost_selections(
                    session, play_plan, tid, targets)
                if cost_selection is None:
                    _hc.log_req(f"    REJECTED play {crow[2]}: incomplete/illegal "
                            "card cost target")
                    g3 = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
                    g3.push_player_updated(pl_t, champ_id=getattr(
                        self, "_player_champ_scid", None))
                    self._send_battle_events(session, g3, pl_t)
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                cost_selections, cost_uids = cost_selection
                ability_data = ((typed_payload or {}).get("ability_data") or ()) \
                    if isinstance(typed_payload, dict) else ()
                x_cost = self._extract_int32_field(
                    inner_bytes, "m_ResourceXCost")
                x_cost = max(0, int(x_cost or 0))
                if play_plan.cost.variable:
                    if x_cost < play_plan.cost.variable_minimum:
                        _hc.log_req(f"    REJECTED play {crow[2]}: X={x_cost} "
                                f"below minimum {play_plan.cost.variable_minimum}")
                        self._push_transaction_ack(session)
                        handled = True
                        return True
                elif x_cost:
                    _hc.log_req(f"    REJECTED play {crow[2]}: non-variable card "
                            f"has X={x_cost}")
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                x_payment = x_cost * play_plan.cost.variable_multiplier
                if cost + x_payment > bstate.get("player_resources", 0):
                    # Defense-in-depth: the client's playability is the
                    # normal gate, but a drag shouldn't be able to play
                    # an unaffordable card (e.g. an undiscounted Fury of
                    # the Mountain God) and push resources negative.
                    _hc.log_req(f"    REJECTED play {crow[2]}: cost {cost}+{x_payment} > "
                            f"resources {bstate.get('player_resources', 0)}")
                    g3 = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
                    g3.push_player_updated(pl_t, champ_id=getattr(
                        self, "_player_champ_scid", None))
                    self._send_battle_events(session, g3, pl_t)
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                # Build and validate the complete current-record activation
                # before mutating resources or moving the card.  This is the
                # single card-play boundary for target/option/X data; leaves
                # must never discover a missing target after payment.
                activations, _cost_target_map = play_plan.activation_bundle(
                    targets, x_cost=x_cost, ability_data=ability_data)
                ability_cost_selection = self._ability_card_play_cost_selections(
                    session, play_plan, tid, activations,
                    owner_id=self.user_profile["id"], battle_state=bstate)
                if ability_cost_selection is None:
                    _hc.log_req(f"    REJECTED play {crow[2]}: invalid ability cost target")
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                ability_cost_selections, ability_cost_uids = ability_cost_selection
                cost_selections.extend(ability_cost_selections)
                cost_uids.update(ability_cost_uids)
                selected_cost_map = {
                    int(spec.get("index", index)): tuple(selected)
                    for index, (spec, selected) in enumerate(cost_selections)
                    if selected and not spec.get("ability_guid")
                }
                plan_errors = play_plan.validate(
                    variable_cost=x_cost,
                    activations=activations,
                    cost_target_map=selected_cost_map)
                if plan_errors:
                    _hc.log_req(f"    REJECTED play {crow[2]}: "
                            f"activation validation: {'; '.join(plan_errors)}")
                    g3 = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
                    g3.push_player_updated(pl_t, champ_id=getattr(
                        self, "_player_champ_scid", None))
                    self._send_battle_events(session, g3, pl_t)
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                if not self._card_costs_valid(
                        session, cost_selections, self.user_profile["id"]):
                    _hc.log_req(f"    REJECTED play {crow[2]}: invalid card cost")
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                bstate["player_resources"] = (
                    bstate.get("player_resources", 0) - cost - x_payment)
                # Move from hand to CastSpells in DB
                from pvp_db import db_set_card_played_to_zone
                db_set_card_played_to_zone(session.session_id, tid, 'CastSpells')
                # Push chain events
                voided_cost_uids = self._apply_card_play_costs(
                    g3, session, bstate, pl_t, ai_t, cost_selections,
                    source_uid=tid,
                    expected_owner_id=self.user_profile["id"],
                    scrounge=any(getattr(ability, "is_scrounge", False)
                                 for ability in play_plan.cast_abilities),
                    defer_voided_cards=True)
                g3.push_card_updated(scid_played, pl_t, _hc.game_engine.ECardCollections.CastSpells,
                                    _hc.game_engine.card_type_from_db(played_card_type),
                                    template_id=crow[0], cost=cost, attack=atk, defense=def_, gems=gem)
                g3.push_troop_card_played(scid_played, pl_t)
                # Opponent (AI) gets priority after the card has been
                # registered on the client chain.  The chain animation must
                # precede GreenLight; otherwise the client can enter the
                # response window while ChainView is still empty.
                perm = _hc.game_engine.card_type_from_db(played_card_type) & (
                    _hc.game_engine.ECardTypes.Troop |
                    _hc.game_engine.ECardTypes.Artifact |
                    _hc.game_engine.ECardTypes.Constant)
                if perm:
                    # Permanents go on the STACK like spells (so they can
                    # be countered/interrupted) instead of resolving
                    # instantly.  The card stays in CastSpells; when BOTH
                    # players pass, _resolve_stack_item moves it to the
                    # warzone (CameOutThisTurn / summoning sick), re-pushes
                    # the full CardUpdated/CardMoved, and fires its
                    # enters-play + Deploy triggers.
                    effect_targets = [uid for uid in targets
                                      if int(uid) not in cost_uids]
                    target_uid = effect_targets[-1] if effect_targets else None
                    _be.stack_push(bstate, {
                        "kind": "troop", "source_uid": int(tid),
                        "ability_guids": [ability.ability_guid
                                          for ability in
                                          play_plan.cast_abilities],
                        "target_uid": target_uid,
                        "instance_id": 1, "x_cost": x_cost,
                        "voided_cost_uids": list(voided_cost_uids or ()),
                        "activations": {
                            guid: activation.as_dict()
                            for guid, activation in activations.items()
                        },
                        "played_from_hand": True,
                    })
                    # The troop's presence on the chain is driven by the
                    # CastSpells CardMove/UpdateCard of the actual card
                    # above. The chain event still needs a real client
                    # AbilityTemplate; a card-template GUID is not one and
                    # makes UIBattle silently skip the chain picture.
                    g3.push_ability_on_chain(
                        scid_played,
                        _hc.game_engine.ResourceId.from_str(
                            _hc.game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID))
                    _be.save_state(session, bstate)
                    _hc.log_req(f"    Troop {crow[2]} on the stack — "
                            f"resolves on both-pass (AI auto-passes)")
                else:
                    # BasicAction / QuickAction spell: cast and PUSH onto
                    # the chain (CastSpells = the visual stack). The BOM
                    # (draw / buff / etc.) executes when the chain resolves
                    # (both players pass) — ONE chain item, sub-effects
                    # bundled. Card-level costs were already paid and bound
                    # above; only the remaining IDs are effect targets.
                    effect_targets = [uid for uid in targets
                                      if int(uid) not in cost_uids]
                    target_uid = effect_targets[-1] if effect_targets else None
                    _hc.log_req(f"    Spell {played_card_uid}: targets={[hex(t) for t in targets]} "
                            f"costs={[hex(t) for t in cost_uids]} "
                            f"target={hex(target_uid) if target_uid else None}")
                    ability_guids = [ability.ability_guid
                                     for ability in play_plan.cast_abilities]
                    _be.stack_push(bstate, {
                        "kind": "spell", "source_uid": int(tid),
                        "ability_guids": ability_guids, "target_uid": target_uid,
                        "instance_id": 1, "x_cost": x_cost,
                        "voided_cost_uids": list(voided_cost_uids or ()),
                        "activations": {
                            guid: activation.as_dict()
                            for guid, activation in activations.items()
                        },
                    })
                    g3.push_ability_on_chain(
                        scid_played,
                        _hc.game_engine.ResourceId.from_str(
                            _hc.game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID),
                        target_card_ids=(
                            [_hc.game_engine.SessionCardId(
                                _hc.game_engine.UID(int(uid)))
                             for uid in effect_targets]))
                    _be.save_state(session, bstate)
                # Both card-play branches now have a visible chain item.
                g3.push_green_light(
                    ai_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
                _be.save_state(session, bstate)
                g3.player_resources = bstate["player_resources"]
                ec = _hc.game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                ec.player_id = pl_t; ec.operation = 2; ec.delta = cost + x_payment
                ec.new_value = bstate["player_resources"]
                g3._push(ec)
                g3.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
                _hc.log_req(f"    Troop cost={cost}+{x_cost} — resources left={bstate['player_resources']}")
                if g3.events:
                    pkt3 = g3.make_network_packet(pl_t)
                    dw3 = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt3)), 1, "00000000-0000-0000-0000-000000000000")
                    self._game_scnt = max(self._game_scnt, self.scnt) + 1
                    self.scnt = self._game_scnt
                    self.send({
                        "issuer": f"0.0.0.0.ServiceGameSession.{_hc.SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                        "target": "ServiceGameSession", "instance": gs_instance,
                        "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                    }, dw3)
                    self._event_q.append((self.scnt, dw3, {}))
                    _hc.log_req(f"    Pushed permanent to chain ({len(dw3)}b); "
                            "waiting for both players to pass")
                handled = True

            # Get updated hand cards (skip the played one), resolving via
            # template_guid so it works for instance and GUID cards.
            rows3 = _hc.db_hand_cards_full(session.session_id, self.user_profile["id"])

            hand3 = []
            for r3 in rows3:
                scid3 = _hc.game_engine.SessionCardId(_hc.game_engine.UID(r3[0]))
                c3 = 0
                ct3 = r3[1] or ''
                thresh_json3 = None
                ab_guids3 = []
                t = self._template_by_guid(r3[2])
                if t:
                    c3 = t[3]
                    ct3 = t[1] or ''
                    srow = _hc.db_card_template_thresholds(r3[2])
                    if srow:
                        thresh_json3 = srow[0]
                        if srow[1]:
                            try:
                                ab_guids3 = [g.lower() for g in json.loads(srow[1])]
                            except Exception:
                                pass
                hand3.append((scid3, c3, ct3, thresh_json3, ab_guids3))

            # Filter: resources + threshold + valid troop target.
            fc3 = self._warzone_troop_count(session, self.user_profile["id"])
            ec3 = self._warzone_troop_count(session, 0)
            playable3 = []
            from rules_port.static_rules import effective_cost
            from rules_port.resources import can_play_resource, resource_play_limit
            resource_limit = resource_play_limit(
                _hc._db, session.session_id, bstate,
                int(self.user_profile.get("id", 0)))
            resource_slots_used = not can_play_resource(
                bstate, "player", limit=resource_limit)
            for s, c, ct, thresh_json, ab_guids in hand3:
                # Recompute instance-level cost when rebuilding options after
                # a resource play; opening-hand discounts must remain active.
                try:
                    c = effective_cost(_hc._db, session.session_id, bstate,
                                       s.uid.uid64)
                except Exception:
                    pass
                if self._hand_card_playable(session, s.uid.uid64, ct, c,
                                            thresh_json, ab_guids,
                                            bstate.get("player_resources", 0),
                                            bstate.get("player_threshold", {}),
                                            resource_slots_used,
                                            fc3, ec3):
                    playable3.append(s)
            if (not bstate.get("pending_choice") and
                    not bstate.get("pending_trigger") and
                    not bstate.get("pending_deck_search") and
                    not bstate.get("pending_conversation")):
                # A class-39 trigger target prompt is pending — the
                # prompt packet already carries the picker + priority;
                # pushing the normal post-play options would clobber it.
                chain_pending = not _be.stack_empty(bstate)
                if chain_pending:
                    # A permanent is still on CastSpells.  Do not publish the
                    # main-phase hand options here: that lets the player cast
                    # another troop/resource before the chain has resolved.
                    # Send the card/resource events first, then expose only
                    # quick responses while the chain is pending.
                    if g3.events:
                        pkt3 = g3.make_network_packet(pl_t)
                        dw3 = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt3)), 1, "00000000-0000-0000-0000-000000000000")
                        self.scnt += 1
                        self.send({
                            "issuer": f"0.0.0.0.ServiceGameSession.{_hc.SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                            "target": "ServiceGameSession", "instance": gs_instance,
                            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                        }, dw3)
                    self._push_phase_options_empty(session, pl_t, ai_t)
                    g_chain = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
                    g_chain.push_green_light(
                        pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
                    self._send_battle_events(session, g_chain, pl_t)
                    _hc.log_req("    Chain pending after player play; "
                            "main-phase card options withheld")
                else:
                    g3.push_options(pl_t, playable3)
                    self._add_play_target_options(g3, session, pl_t, ai_t)
                    _hc.log_req(f"    Post-play options: {len(playable3)} playable, thresh={dict(bstate.get('player_threshold',{}))} resources={bstate.get('player_resources',0)}")
                    player_champ_cid3 = getattr(self, "_player_champ_scid", None)
                    if player_champ_cid3:
                        abilities3 = getattr(self, "_player_champ_abilities", [])
                        if abilities3:
                            champ_uid3 = (
                                player_champ_cid3.uid.to_uint64()
                                if hasattr(player_champ_cid3, "uid") else 0)
                            champ_targets3 = self._champion_ability_targets(
                                session, abilities3, champ_uid3)
                            champ_costs3 = self._champion_ability_costs(
                                session, abilities3, champ_uid3)
                            g3.add_champion_to_options(
                                pl_t, player_champ_cid3,
                                self._filter_affordable_abilities(
                                    abilities3, bstate,
                                    _be.current_phase(bstate),
                                    target_data=champ_targets3,
                                    cost_data=champ_costs3),
                                self._discard_costs_for(session, abilities3),
                                champ_targets3, champ_costs3)
                    # Warzone-troop manual abilities (e.g. Shift) also refresh
                    # after playing a card (a fresh troop just resolved).
                    affordable3 = self._affordable_troop_abilities(session, bstate)
                    if affordable3:
                        self._add_troop_ability_options(
                            g3, pl_t, session, affordable3, bstate)
                    # Re-grant priority so the player keeps the Pass Priority
                    # button visible after playing a card.
                    g3.push_green_light(pl_t, self._priority_context_for(_be.current_phase(bstate), bstate))
                    if g3.events:
                        pkt3 = g3.make_network_packet(pl_t)
                        dw3 = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt3)), 1, "00000000-0000-0000-0000-000000000000")
                        self.scnt += 1
                        self.send({
                            "issuer": f"0.0.0.0.ServiceGameSession.{_hc.SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                            "target": "ServiceGameSession", "instance": gs_instance,
                            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                        }, dw3)
                        _hc.log_req(f"    Pushed fresh GreenLight + options ({len(dw3)}b)")
            elif (bstate.get("pending_choice") or
                  bstate.get("pending_deck_search") or
                  bstate.get("pending_conversation")) and g3.events:
                # A resource picker (or Shards of Fate deck picker) is
                # pending: send only the resource
                # card played + PlayerUpdated events — the prompt packet
                # already carried the picker and priority.
                pkt3 = g3.make_network_packet(pl_t)
                dw3 = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt3)), 1, "00000000-0000-0000-0000-000000000000")
                self.scnt += 1
                self.send({
                    "issuer": f"0.0.0.0.ServiceGameSession.{_hc.SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                    "target": "ServiceGameSession", "instance": gs_instance,
                    "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                }, dw3)
                _hc.log_req(f"    Pushed resource-play events (choice pending) ({len(dw3)}b)")
        elif not handled:
            _hc.log_req(f"    === PlayerTransaction — ending game ===")
            self._finish_battle_gameend(
                session, False,
                [_hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)],   # AI wins
                [_hc.game_engine.UID(player_uid)])     # player loses
            _hc.log_req(f"    Pushed GameEnded (3055) — marked as loss")
        return True

    def _handle_debug_cheat_transaction(self, session, transaction):
        """Execute a client DebugCheatTransaction and acknowledge it."""
        import hconnect_server as _hc
        from debug_cheats import handle_debug_cheat
        _hc.log_req("    Debug cheat — dispatching server-side action")
        handled = handle_debug_cheat(self, session, transaction.inner_bytes)
        if handled:
            self._push_transaction_ack(session)
        return True

    def _handle_encounter_mod_dialog_transaction(self, session, transaction):
        """Route the class-55 conversation acknowledgement to its owner."""
        if (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import pvp_handle_transaction
            handled = pvp_handle_transaction(self, session,
                                              transaction.inner_bytes,
                                              typed_payload=getattr(
                                                  transaction, "typed_payload", None))
            if not handled:
                self._push_transaction_ack(session)
            return True
        return self._resolve_pending_conversation(session, transaction)

    def _handle_set_ability_data_transaction(self, session, transaction):
        """Resolve target data supplied for a pending ability prompt."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        if bstate.get("pending_trigger"):
            # The player answered a class-39 triggered-ability target
            # prompt (e.g. Solitary Exile's Deploy "Void another target
            # card").  Extract the chosen target (last Card-type UID in
            # the TargetMap), push the trigger onto the chain with that
            # target, and let the normal pass flow resolve it.
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
            pend = bstate.pop("pending_trigger")
            ag = pend["ability_guid"]
            src = int(pend["source_uid"])
            inst_id = int(pend["instance_id"])
            _be.stack_push(bstate, {
                "kind": "trigger", "ability_guid": ag,
                "source_uid": src, "target_uid": chosen_uid,
                "instance_id": inst_id,
            })
            _be.save_state(session, bstate)
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            g.push_ability_on_chain(
                _hc.game_engine.SessionCardId(_hc.game_engine.UID(src)),
                _hc.game_engine.ResourceId.from_str(ag),
                ability_instance_id=inst_id)
            g.push_green_light(pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
            self._send_battle_events(session, g, pl_t)
            self._push_transaction_ack(session)
            _hc.log_req(f"    Trigger target chosen: {ag[:8]} "
                    f"target={hex(chosen_uid) if chosen_uid else 'none'}")
            handled = True
        if bstate.get("pending_deck_search"):
            # The player answered a "search your deck" class-39 prompt
            # (e.g. Darkspire Priestess's Deathcry) — move the chosen
            # matching deck card into the controller's hand.
            handled = self._resolve_pending_deck_search(
                session, pl_t, ai_t, inner_bytes)
        if bstate.get("pending_discard_ability"):
            # The chosen card is the LAST Card-type UID in the transaction
            # (the TargetMap embeds it after the source card id).
            discard_card_uid = None
            if isinstance(inner_bytes, bytes):
                for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                    try:
                        uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                        if (uid64 & 0xFF) == 1:
                            discard_card_uid = int(uid64)
                    except Exception:
                        continue
            if not discard_card_uid:
                discard_card_uid = bstate.get("pending_discard_scid")
            row = None
            if discard_card_uid:
                from pvp_db import db_rules_port_hand_card
                row = db_rules_port_hand_card(
                    session.session_id, discard_card_uid,
                    self.user_profile["id"])
            if row and discard_card_uid is not None:
                # Discard to the card's OWNER (a Mind Grasp steal returns
                # to the AI's graveyard; a player card to the player's).
                gd_owner, owner_uid = self._discard_card_to_owner(
                    session, pl_t, ai_t, int(discard_card_uid))
                gd = gd_owner if gd_owner else _hc.game_engine.Game(session.session_id, pl_t, ai_t)
                tpl_d = "00000000-0000-0000-0000-000000000000"
                # Generic post-ability rule: re-grant priority to the
                # activating player after the discard resolves (the class-23
                # prompt consumed the priority window).
                gd.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
                self._send_battle_events(session, gd, pl_t)
                self._push_main_phase_options(session, pl_t, ai_t)
                _hc.log_req(f"    Discard (class-23 follow-up): {discard_card_uid} owner={owner_uid} ({tpl_d})")
            else:
                _hc.log_req(f"    Discard (class-23 follow-up): no hand card uid={discard_card_uid}")
            bstate.pop("pending_discard_ability", None)
            bstate.pop("pending_discard_ability_instance_id", None)
            bstate.pop("pending_discard_target_template", None)
            bstate.pop("pending_discard_scid", None)
            # The discard is the terminal continuation of Stargazer's
            # draw-then-discard BOM.  Clear the resume cursor and port
            # activation checkpoint so the next activation starts at effect
            # zero and cannot inherit the previous player's discard state.
            bstate.pop("rules_port_resume_effect_order", None)
            bstate.pop("resolution_paused", None)
            try:
                port = getattr(session, "_rules_port_session", None)
                if port is not None:
                    port.pending_activation = None
                    port.persist()
            except Exception:
                pass
            _be.save_state(session, bstate)
        return True

    def _handle_ability_activate_transaction(self, session, transaction):
        """Resolve a PvE card or champion ability activation."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        is_set_ability_data = transaction.is_set_ability_data
        is_ability_activate = transaction.is_ability_activate
        handled = False
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        # Extract AbilityTemplateId (GUID) from the transaction
        ability_guid = _hc.extract_ability_guid(inner_bytes)
        _hc.log_req(f"    Ability activation: guid={ability_guid}")

        # A DoubleChoice effect exposes the client's built-in
        # ChooseAndPlay ability. Consume that selection before normal
        # champion/troop ability routing can mistake the generated Choice
        # card for an ordinary activation.
        if ability_guid:
            _choice_be = self._checkpoint_engine(session)
            if _choice_be.load_state(session).get("pending_choice"):
                if self._resolve_pending_choice(
                        session, pl_t, ai_t, inner_bytes, ability_guid):
                    return True

        # A triggered-ability target response (e.g. Solitary Exile's
        # Deploy "Void another target card") arrives as an
        # AbilityActivationData transaction too — the client submits a
        # SetAbilityActivationDataTransaction for the class-39 prompt.
        # Route it to the pending-trigger follow-up BEFORE the manual
        # troop-ability path so the trigger resolves with the chosen
        # target instead of being rejected by the activation gates.
        if ability_guid and (
                self._resolve_pending_trigger_target(
                    session, pl_t, ai_t, inner_bytes, ability_guid)
                or self._resolve_pending_deck_search(
                    session, pl_t, ai_t, inner_bytes, ability_guid)):
            handled = True

        # Troop/artifact manual ability (e.g. Shift): the source is a
        # warzone troop the player controls, not the champion. Champion
        # abilities live in talent_abilities; card abilities live in
        # card_abilities_meta and are gated on the instance's ability
        # list. Route here BEFORE the champion path.
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        if ability_guid and not handled:
            # A GUID can be present in both the card-ability index and
            # champion metadata.  The transaction's SourceCardId is the
            # authoritative discriminator: when it identifies a matching
            # player-controlled warzone card carrying this ability, route it
            # through the card-ability resolver first.  Otherwise a card power
            # such as Crown of the Primals can enter the champion path, where
            # its selected source/target cards are mistaken for champion
            # payment targets and voided.
            from pvp_db import (db_rules_port_talent_ability_exists,
                                db_rules_port_cards_with_ability)
            champ_owned = db_rules_port_talent_ability_exists(ability_guid)
            # The same ability GUID can be present on multiple card instances
            # (for example, two Howling Braves).  The option list is per
            # source card, so route by SourceCardId rather than selecting the
            # first matching row.
            source_uid = None
            if isinstance(inner_bytes, bytes):
                scid_pos = inner_bytes.find(b"SourceCardId")
                if scid_pos >= 0:
                    uid_pos = inner_bytes.find(b"m_UID64", scid_pos)
                    if uid_pos >= 0:
                        rest = inner_bytes[uid_pos + 7:]
                        parts = rest.split(b";", 6)
                        if len(parts) >= 5:
                            try:
                                source_uid = struct.unpack(
                                    '<Q', bytes.fromhex(
                                        parts[4].decode("ascii")))[0]
                                if (source_uid & 0xFF) != 1:
                                    source_uid = None
                            except (ValueError, TypeError, struct.error):
                                source_uid = None
            src_row = None
            if source_uid is not None:
                rows = db_rules_port_cards_with_ability(
                    session.session_id, self.user_profile["id"], ability_guid,
                    source_uid)
                src_row = rows[0] if rows else None
            if src_row is None and source_uid is None:
                # Older clients may omit the source field. Preserve the
                # unambiguous single-copy case, but never guess between
                # multiple copies of the same ability.
                matches = db_rules_port_cards_with_ability(
                    session.session_id, self.user_profile["id"], ability_guid)
                if len(matches) == 1:
                    src_row = matches[0]
            if src_row or not champ_owned:
                if src_row:
                    self._activate_troop_ability(
                        session, pl_t, ai_t, bstate,
                        int(src_row[0]), ability_guid, inner_bytes)
                    if (not bstate.get("stack") and
                            not bstate.get("pending_choice") and
                            not bstate.get("pending_deck_search") and
                            not bstate.get("pending_trigger") and
                            not bstate.get("pending_discard_ability") and
                            _be.current_phase(bstate) in (
                                _hc.game_engine.ETurnPhases.FirstMainPhase,
                                _hc.game_engine.ETurnPhases.SecondMainPhase,
                                _hc.game_engine.ETurnPhases.DeclareCombatPriorityWindow,
                                _hc.game_engine.ETurnPhases.DeclareAttackPriorityWindow,
                                _hc.game_engine.ETurnPhases.DeclareDefensePriorityWindow,
                                _hc.game_engine.ETurnPhases.FirstStrikePriorityWindow)):
                        # A manual troop ability can change the resources or
                        # board targets available to a QuickAction.  Rebuild
                        # the option list in the same priority window; leaving
                        # the pre-activation list in place makes cards such as
                        # Strength of the Redwood remain hidden after Howling
                        # Brave generates a resource.
                        phase = _be.current_phase(bstate)
                        if phase in (_hc.game_engine.ETurnPhases.FirstMainPhase,
                                     _hc.game_engine.ETurnPhases.SecondMainPhase):
                            self._push_main_phase_options(session, pl_t, ai_t)
                        else:
                            self._push_phase_options_empty(session, pl_t, ai_t)
                    self._push_transaction_ack(session)
                    handled = True

        # If a class-23 discard prompt is pending and this activation is
        # the DiscardACard follow-up, this is the player's card choice —
        # discard it and finish, do NOT re-run the draw/class-23 flow.
        if not handled:
            bstate = _be.load_state(session)
            self._current_bstate = bstate
        if (bstate.get("pending_discard_ability") and
                ability_guid == bstate.get("pending_discard_ability")):
            # Extract the chosen card (last Card-type UID in the TargetMap).
            chosen_uid = None
            if isinstance(inner_bytes, bytes):
                for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                    try:
                        uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                        if (uid64 & 0xFF) == 1:
                            chosen_uid = int(uid64)
                    except Exception:
                        continue
            if not chosen_uid:
                chosen_uid = bstate.get("pending_discard_scid")
            row = None
            if chosen_uid:
                from pvp_db import db_rules_port_hand_card
                row = db_rules_port_hand_card(
                    session.session_id, chosen_uid, self.user_profile["id"])
            if row and chosen_uid is not None:
                # Discard to the card's OWNER (a Mind Grasp steal returns
                # to the AI's graveyard; a player card to the player's).
                gd_owner, owner_uid = self._discard_card_to_owner(
                    session, pl_t, ai_t, int(chosen_uid))
                tpl_d = "00000000-0000-0000-0000-000000000000"
                # _discard_card_to_owner returns a Game that carries only
                # the card events (no PlayerUpdated fields), so overlay the
                # live resources/health onto it for the PlayerUpdated below.
                # A bare Game would default them to 0/20, wiping the UI.
                if gd_owner:
                    gd = gd_owner
                    gd.player_resources = bstate.get("player_resources", 0)
                    gd.player_total_resources = bstate.get("player_total_resources", 0)
                    gd.player_threshold = dict(bstate.get("player_threshold", {}))
                    gd.player_charges = bstate.get("player_charges", 0)
                    gd.player_spell_points = bstate.get("player_spell_points", 0)
                    gd.player_health = bstate.get("player_health", 20)
                    gd.ai_health = bstate.get("ai_health", 10)
                else:
                    gd = self._fresh_game(session, pl_t, ai_t, bstate)
                gd.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
                # Re-grant priority so the player can keep playing after the
                # discard resolves (the class-23 prompt consumed the window).
                gd.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
                self._send_battle_events(session, gd, pl_t)
                self._push_main_phase_options(session, pl_t, ai_t)
                _hc.log_req(f"    Discard (class-23 follow-up): {chosen_uid} owner={owner_uid} ({tpl_d})")
            else:
                _hc.log_req(f"    Discard (class-23 follow-up): no hand card uid={chosen_uid}")
            bstate.pop("pending_discard_ability", None)
            bstate.pop("pending_discard_ability_instance_id", None)
            bstate.pop("pending_discard_target_template", None)
            bstate.pop("pending_discard_scid", None)
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            handled = True
            return True  # skip the rest of this handler

        # If the ability's BOM includes a discard, the client prompted the
        # player to pick a hand card (TargetInstance attached to the
        # option); the chosen card arrives in the activation's TargetMap.
        # Extract its card_uid so we can move it to discard after the draw.
        discard_card_uid = None
        if ability_guid and not handled and isinstance(inner_bytes, bytes):
            needs_discard = self._bom_has_discard(ability_guid)
            if needs_discard:
                # The TargetMap embeds the chosen card's SessionCardId as a
                # m_UID64 with UID type 1 (Card). It appears after the
                # SourceCardId; take the LAST such UID in the transaction.
                for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                    try:
                        uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                        if (uid64 & 0xFF) == 1:
                            discard_card_uid = int(uid64)
                    except Exception:
                        continue
                # Fall back to auto-pick if no target was found in the
                # transaction (e.g. client didn't attach a TargetMap).
                if not discard_card_uid:
                    from pvp_db import db_rules_port_first_hand_card
                    discard_card_uid = db_rules_port_first_hand_card(
                        session.session_id, self.user_profile["id"])
                _hc.log_req(f"    Ability discard target: card_uid={discard_card_uid}")

        # Look up cost and deduct from battle state. Costs live on the
        # top-level granted ability in talent_abilities (the BOM's head);
        # ability_effects expands it into the leaf effect chain.
        # Guarded by `not handled`: a warzone-troop manual ability (e.g.
        # Shift) was already resolved by _activate_troop_ability (it pays
        # RESOURCE cost). Falling through here would re-resolve it and
        # push a second transaction ack.
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        if ability_guid and not handled:
            from db import db_talent_ability_costs
            talent_cost = db_talent_ability_costs(ability_guid)
            trow = talent_cost[:2] if talent_cost else None
            if trow is None:
                # Champion signature charge powers aren't talents; their
                # costs come from champion_abilities (gamedata seed).
                from pvp_db import db_champion_ability_costs
                crow = db_champion_ability_costs(ability_guid)
                trow = (crow[0], crow[1]) if crow else None
            cc = trow[0] if trow else 0
            sc = trow[1] if trow else 0
            charges = bstate.get("player_charges", 0)
            sp = bstate.get("player_spell_points", 0)
            # Spell-power escalation: each use permanently bumps that
            # spell's SP cost by +1 (mirrors the client's
            # IncrementSpellPointCostModifier, Session.cs:1154).
            # Applies ONLY to spell powers (spell_cost > 0) — a charge
            # power must not accumulate an SP surcharge.
            sp_uses = bstate.get("player_sp_uses", {}) or {}
            used = int(sp_uses.get(ability_guid, 0))
            eff_sc = sc + (used if sc > 0 else 0)
            graph = _hc._records_ability_graph(self, ability_guid)
            player_champ_scid = getattr(self, "_player_champ_scid", None)
            if not isinstance(player_champ_scid, _hc.game_engine.SessionCardId):
                _hc.log_req("    Champion ability rejected: player champion card id is unavailable")
                self._push_transaction_ack(session)
                return True
            champion_uid = int(player_champ_scid.uid.uid64)
            available = self._champion_ability_available(
                bstate, ability_guid, graph, source_uid=champion_uid)
            if (charges >= cc and sp >= eff_sc and available
                    and self._champion_thresholds_met(ability_guid, bstate)):
                # ONE-SHOT (m_UsesPerGame) powers are spent on activation;
                # the option list reads this count so the ability disappears.
                self._consume_champion_ability_use(
                    bstate, ability_guid, graph, source_uid=champion_uid)
                from rules_port.resources import apply_resource_change
                charge_change = apply_resource_change(
                    bstate, "player", "chargepoints", -cc)
                bstate["player_spell_points"] = sp - eff_sc
                if sc > 0:
                    sp_uses[ability_guid] = used + 1
                    bstate["player_sp_uses"] = sp_uses
                g = self._fresh_game(session, pl_t, ai_t, bstate)
                # Push charge/SP deduction events
                ev_chg = _hc.game_engine.ChampionChargePointsChangedSessionEventArgs()
                ev_chg.player_id = pl_t; ev_chg.operation = 2; ev_chg.delta = cc
                ev_chg.new_value = charge_change.new_value; g._push(ev_chg)
                if eff_sc:
                    ev_sp = _hc.game_engine.ChampionSpellPointsChangedSessionEventArgs()
                    ev_sp.player_id = pl_t; ev_sp.operation = 2; ev_sp.delta = eff_sc
                    ev_sp.new_value = bstate["player_spell_points"]; g._push(ev_sp)
                # Keep the escalated SP cost in the authoritative state and
                # CardDef.  Do not emit CardUpdated for the champion here:
                # the client treats a Champions collection update as a
                # location change and can leave a giant copy of the champion
                # behind when this ability is also pushed onto the chain.
                if player_champ_scid and sp_uses.get(ability_guid):
                    cdef = g.card_defs.get(player_champ_scid)
                    if cdef is not None:
                        for ag2, uses2 in sp_uses.items():
                            if int(uses2) > 0:
                                cdef.spell_point_cost_mods[_hc.game_engine.ResourceId.from_str(ag2)] = int(uses2)
                # Push the ability onto the CHAIN (the client's stack). The
                # BOM (draw / discard / etc.) executes when the chain
                # resolves (both players pass). One chain item per top-level
                # ability — its sub-effects (e.g. Soothsaying's draw +
                # discard) are bundled and resolve together, so a counterspell
                # has a single entry to cancel.
                champ_scid = player_champ_scid
                # The ability's chosen target (last Card-type UID in the
                # transaction, e.g. the troop for Dimmid's Lifedrain).
                target_uid = None
                all_target_uids = []
                if isinstance(inner_bytes, bytes):
                    for m_du in re.finditer(
                            rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                            inner_bytes):
                        try:
                            uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                            if (uid64 & 0xFF) == 1:
                                all_target_uids.append(int(uid64))
                        except Exception:
                            continue
                selection = self._select_champion_activation_targets(
                    session, bstate, ability_guid,
                    champ_scid.uid.to_uint64(), all_target_uids)
                if selection is None:
                    _hc.log_req(f"    Champion ability {ability_guid[:8]}: "
                            "missing/illegal payment or effect target")
                    return True
                target_uid, sacrifice_uids = selection
                for sacrifice_uid in sacrifice_uids:
                    self._sacrifice_troop(
                        g, session, pl_t, ai_t, sacrifice_uid,
                        expected_owner_id=self.user_profile.get("id")
                        if isinstance(self.user_profile, dict) else None)
                if all_target_uids:
                    # Multi-target champion powers (e.g. Bun'jitsu's
                    # "Void two ready troops you control") carry every
                    # chosen card; the resolver voids them and reads
                    # their stats for the summoned-token buffs.
                    bstate["champion_void_uids"] = all_target_uids
                bstate["player_mod_target"] = target_uid
                # Clear the previous PlayerOptionList before the chain event
                # reaches the client. AbilityPushedOnChain renders the
                # champion; CanActivateAbility reads the cached options.
                g.push_options(pl_t, [])
                _be.stack_push(bstate, {
                    "kind": "ability", "ability_guid": str(ability_guid),
                    "source_uid": champ_scid.uid.to_uint64(),
                    "target_uid": target_uid,
                    "instance_id": 1,
                })
                g.push_ability_on_chain(champ_scid,
                                        _hc.game_engine.ResourceId.from_str(str(ability_guid)))
                _be.save_state(session, bstate)
                # Push PlayerUpdated for BOTH players so the client refreshes
                # hand AND deck counts (OnPlayerUpdated -> BattleAnimationSetCardCounts).
                g.player_resources = bstate.get("player_resources", 0)
                g.player_total_resources = bstate.get("player_total_resources", 0)
                g.player_threshold = dict(bstate.get("player_threshold", {}))
                g.player_charges = bstate.get("player_charges", 0)
                g.player_spell_points = bstate.get("player_spell_points", 0)
                g.ai_resources = bstate.get("ai_resources", 0)
                g.ai_total_resources = bstate.get("ai_total_resources", 0)
                g.ai_threshold = dict(bstate.get("ai_threshold", {}))
                g.ai_charges = bstate.get("ai_charges", 0)
                g.ai_spell_points = bstate.get("ai_spell_points", 0)
                g.push_player_updated(pl_t, champ_id=player_champ_scid)
                g.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
                # The chain card is added by a client animation queued from
                # AbilityPushedOnChain.  If ResolveTopOfChain is sent in this
                # same packet, UIBattle.CheckForMissingPriorityWindowState()
                # runs before that animation registers the card and pushes an
                # inactive priority state.  The client then displays Zuba on
                # the chain but has no usable priority.  Normal is still the
                # correct priority handoff; the server's pass handler resolves
                # the pending chain item, and avoids that client-side race.
                g.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
                self._send_battle_events(session, g, pl_t)
                # While the chain is non-empty expose only instant-speed
                # responses. Main-phase cards and BasicAction champion powers
                # return after the chain empties.
                self._push_phase_options_empty(session, pl_t, ai_t)
                _hc.log_req(f"    Ability activated on chain: charges {charges}->{bstate['player_charges']}, SP {sp}->{bstate['player_spell_points']}")
            elif not available:
                _hc.log_req(f"    Ability {str(ability_guid)[:8]} already used "
                        "(ONE-SHOT / UsesPerGame)")
            else:
                _hc.log_req(f"    Cannot afford ability: need {cc} charges/{sc} SP, have {charges}/{sp}")
        elif not handled:
            _hc.log_req("    Ability activation: could not parse GUID")
        if not handled:
            # Always respond so the client isn't blocked (champion
            # abilities; troop abilities ack inside _activate_troop_ability).
            self._push_transaction_ack(session)
        return True

    def _handle_commit_defense_transaction(self, session, transaction,
                                            *, declarations=None,
                                            authoritative_port=None):
        """Declare the player's blockers for the current combat."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        if authoritative_port is not None:
            from rules_port.persistence import load_state, save_state
            load_checkpoint = load_state
            save_checkpoint = save_state
        else:
            import battle_engine as _be
            load_checkpoint = _be.load_state
            save_checkpoint = _be.save_state
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        bstate = load_checkpoint(session)
        self._current_bstate = bstate
        attackers = {int(k): int(v) for k, v in (bstate.get("ai_attackers") or {}).items()}
        from pvp_db import db_warzone_card_uids, db_card_state_value as db_card_state_raw
        player_troops = set(db_warzone_card_uids(session.session_id, self.user_profile["id"]))
        all_uids = []
        if declarations is not None:
            for attacker, blockers in declarations:
                try:
                    all_uids.append(int(attacker))
                    all_uids.extend(int(blocker) for blocker in blockers)
                except (TypeError, ValueError):
                    continue
        elif isinstance(inner_bytes, bytes):
            for m in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    u = struct.unpack('<Q', bytes.fromhex(m.group(1).decode()))[0]
                    if (u & 0xFF) == 1:
                        all_uids.append(int(u))
                except Exception:
                    continue
        blockers_map = {}
        if declarations is not None:
            for attacker, blockers in declarations:
                try:
                    blockers_map[int(attacker)] = [int(blocker)
                                                   for blocker in blockers]
                except (TypeError, ValueError):
                    continue
        cur_attacker = None
        for u in all_uids:
            if declarations is not None:
                continue
            if u in attackers:
                cur_attacker = u
                blockers_map.setdefault(cur_attacker, [])
            elif cur_attacker is not None and u in player_troops:
                blockers_map[cur_attacker].append(u)
        if authoritative_port is not None:
            # RulesPort has already validated and staged blocker assignment
            # against its shared Combat objects.  Project that accepted map
            # directly; a second legacy can_block pass could discard a valid
            # blocker when its SQLite-derived state lagged the port snapshot.
            port_blockers = {}
            port_attackers = {}
            current_ai_attackers = {
                int(value) for value in (bstate.get("ai_attackers") or {})
            }
            for combat in authoritative_port.combat_manager.combats:
                attacker = combat.attacker
                if attacker is None:
                    continue
                attacker_id = int(getattr(
                    getattr(attacker, "session_card_id", attacker),
                    "uid64", getattr(attacker, "session_card_id", attacker)))
                if current_ai_attackers and attacker_id not in current_ai_attackers:
                    continue
                port_attackers[attacker_id] = combat
                port_blockers[attacker_id] = []
                for blocker in combat.blockers:
                    value = getattr(blocker, "session_card_id", blocker)
                    value = getattr(value, "uid64", value)
                    port_blockers[attacker_id].append(int(value))
            if port_attackers:
                attackers = {
                    uid: int((bstate.get("ai_attackers") or {}).get(
                        str(uid), (bstate.get("ai_attackers") or {}).get(uid, 0)))
                    for uid in port_attackers
                }
                blockers_map = port_blockers
        else:
            # Legacy-only compatibility path: retain the metadata-driven
            # blocker legality checks for unclassified requests.
            from rules_port.combat_rules import can_block
            for u in list(blockers_map):
                blockers_map[u] = [b for b in blockers_map[u]
                                   if can_block(_hc._db, session.session_id,
                                                bstate, u, b)]
        bstate["ai_blockers"] = {str(k): [str(b) for b in v]
                                 for k, v in blockers_map.items()}
        block_uids = {b for v in blockers_map.values() for b in v}
        if block_uids:
            _hc.db_bulk_blocker_state(session.session_id, list(block_uids))
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        player_champ_scid = getattr(self, "_player_champ_scid", None) or _hc.game_engine.SessionCardId(pl_t)
        combats = []
        for u in attackers:
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(u))
            combat_id = self._wire_combat_id_for_attacker(
                session, ai_t, u, u & 0xFFFF)
            blockers = [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(b)))
                        for b in blockers_map.get(u, [])]
            game.push_blockers_assigned(combat_id, scid, player_champ_scid, blockers)
            cs = _hc.game_engine.CombatSessionEventArgs()
            cs.player_id = ai_t
            cs.id = combat_id
            cs.attacker = scid
            cs.blockers = blockers
            combats.append(cs)
        self._dispatch_blocker_events(
            game, session, bstate,
            [(u, blockers_map.get(u, [])) for u in attackers])
        if combats:
            game.push_combat_listing(pl_t, combats)
        self._send_battle_events(session, game, pl_t)
        block_summary = ", ".join(
            hex(int(k)) + "->" + (hex(int(v[0])) if v else "none")
            for k, v in blockers_map.items())
        _hc.log_req(f"    CommitDefense: {len(attackers)} attacker(s), "
                f"blocks={block_summary}")
        if authoritative_port is not None:
            # The native defense transaction already staged the blockers and
            # consumed the defender's DeclareDefense priority before this
            # compatibility projection ran.  Drive that completed native
            # action into DeclareDefensePriorityWindow; re-entering the AI
            # turn loop here re-published DeclareDefense and left the client
            # submitting a second (often empty) defense transaction.
            save_checkpoint(session, bstate)
            self._advance_to_priority(session, pl_t, ai_t, bstate)
            return True
        start_idx = bstate.get("ai_turn_phase_idx", 0)
        bstate.pop("ai_turn_phase_idx", None)
        save_checkpoint(session, bstate)
        self._run_ai_turn(session, pl_t, ai_t, bstate, start_idx=start_idx)
        return True

    def _handle_commit_attack_transaction(self, session, transaction,
                                          *, declarations=None,
                                          authoritative_port=None):
        """Declare the player's attackers for the current combat."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        if authoritative_port is not None:
            from rules_port.persistence import load_state, save_state
            from rules_port.lifecycle import advance_checkpoint_phase
            load_checkpoint = load_state
            save_checkpoint = save_state
            advance_checkpoint = advance_checkpoint_phase
        else:
            import battle_engine as _be
            load_checkpoint = _be.load_state
            save_checkpoint = _be.save_state
            advance_checkpoint = _be.advance_phase
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        bstate = load_checkpoint(session)
        self._current_bstate = bstate
        # Extract the attacking card UIDs: every Card-type SessionCardId
        # in the transaction that is in the player's warzone. The
        # defender (AI champion) is not in the player's warzone.
        attacker_uids = []
        if declarations is not None:
            for _defender, attackers in declarations:
                try:
                    attacker_uids.extend(int(attacker) for attacker in attackers)
                except (TypeError, ValueError):
                    continue
        elif isinstance(inner_bytes, bytes):
            for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                    if (uid64 & 0xFF) == 1:  # Card type
                        attacker_uids.append(int(uid64))
                except Exception:
                    continue
        # RulesPort has already validated and staged these declarations.  In
        # that live path the host is only projecting the accepted decision;
        # re-running the old legality loop here used to silently discard
        # attackers when its legacy state view lagged the port view.
        from pvp_db import db_warzone_card_uids, db_card_state_value as db_card_state_raw
        from rules_port.static_rules import effective_attributes
        wz_uids = set(db_warzone_card_uids(
            session.session_id, self.user_profile["id"]))
        attackers = []
        if authoritative_port is not None:
            staged = {
                int(getattr(combat.attacker, "session_card_id",
                            getattr(combat.attacker, "uid", combat.attacker)))
                for combat in authoritative_port.combat_manager.combats
                if combat.attacker is not None
            }
            attackers = [uid for uid in attacker_uids if uid in staged]
        else:
            for uid in attacker_uids:
                if uid not in wz_uids:
                    continue
                state = int(db_card_state_raw(session.session_id, int(uid)) or 0)
                attrs = int(effective_attributes(
                    _hc._db, session.session_id, bstate, int(uid)) or 0)
                if (state & (_hc.game_engine.ECardStates.Tapped |
                             _hc.game_engine.ECardStates.Attacking)):
                    continue
                if attrs & (_hc.game_engine.ECardAttributes.CantAttack |
                            _hc.game_engine.ECardAttributes.Defensive):
                    continue
                if not ((state & _hc.game_engine.ECardStates.StartedATurnOnYourSide)
                        or attrs & _hc.game_engine.ECardAttributes.Speed):
                    continue
                attackers.append(uid)
        # Persist declared attackers in battle state (DB) so combat
        # resolution and a reconnect can reconstruct them.
        ai_champ_uid = getattr(self, "_ai_champ_scid", None)
        ai_champ_uid64 = ai_champ_uid.uid.to_uint64() if ai_champ_uid else 0
        # Merge with any server-auto-declared ForceAttack ("Must
        # attack") attackers so a commit (or pass) never un-declares
        # them — e.g. Savage Raider is already attacking.
        existing = {int(k)
                    for k in (bstate.get("player_attackers") or {})}
        merged = existing | set(attackers)
        bstate["player_attackers"] = {
            str(u): str(ai_champ_uid64) for u in merged}
        save_checkpoint(session, bstate)
        _hc.log_req(f"    CommitTroopsToAttack: {len(attackers)} new attacker(s) -> "
                f"{[hex(u) for u in attackers]} (merged, total {len(merged)})")
        # Push the combat events: one AttackDeclared (class 27) per
        # attacker + a CombatListing (62) with a CombatSession (63) per
        # attacker (no blockers yet — the AI will pass DeclareDefense).
        # Also mark each attacker Attacking | HasAttacked | Tapped (the
        # client renders the tap + attack line from these states) and
        # persist in card_state for reconnect.
        game = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
        ai_champ_scid = ai_champ_uid if ai_champ_uid else _hc.game_engine.SessionCardId(ai_t)
        combats = []
        # Only the newly committed attackers get events here — the
        # auto-declared ForceAttack troops already got their
        # AttackDeclared / CombatListing when the phase opened.
        new_attackers = [u for u in attackers if u not in existing]
        for i, u in enumerate(new_attackers):
            cid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(u))
            combat_id = self._wire_combat_id_for_attacker(
                session, pl_t, u, u & 0xFFFF)
            game.push_attack_declared(combat_id, pl_t, ai_champ_scid, cid)
            from pvp_db import db_card_state_value as db_card_state_raw
            trow = _hc.db_card_template_attrs_joined(session.session_id, int(u))
            tpl_guid = trow[0] if trow else None
            # Effective attributes = template static + instance-granted
            # (a SHIFTED Steadfast lives in card_attributes, not the
            # template — checking only ct.attributes would tap it).
            attrs = (trow[1] if trow and trow[1] else 0) | (trow[2] if trow and trow[2] else 0)
            # Mark the troop as attacking (and tapped UNLESS Steadfast —
            # Steadfast troops don't tap when attacking). Persist + push.
            state = (_hc.game_engine.ECardStates.Attacking |
                     _hc.game_engine.ECardStates.HasAttacked)
            if not (attrs & _hc.game_engine.ECardAttributes.Steadfast):
                state |= _hc.game_engine.ECardStates.Tapped
            from rules_port.host_mutations import apply_attacking_card_state
            apply_attacking_card_state(session, int(u), state)
            from pvp_db import db_card_state_value
            pushed_state = db_card_state_value(session.session_id, int(u))
            if not pushed_state:
                pushed_state = state
            from rules_port.host_mutations import project_attacking_card
            # Continuous attack-time keywords (for example Emberleaf
            # Duelist's "while this is attacking" Swiftstrike) are derived
            # from the authoritative state. Include them in the attack
            # CardUpdated so the client cache sees the same attributes used
            # by combat resolution.
            from rules_port.static_rules import effective_attributes
            attack_attributes = effective_attributes(
                _hc._db, session.session_id, bstate, int(u))
            project_attacking_card(self, game, cid, pl_t, tpl_guid,
                                   pushed_state, attack_attributes)
            # Attacking normally exhausts the troop.  Emit the same typed tap
            # event used by activated and AI attacks so data-defined effects
            # such as Spider Nest's granted "when this exhausts" ability fire
            # for player-declared attackers too.  Steadfast attackers do not
            # become tapped and therefore must not emit this event.
            if state & _hc.game_engine.ECardStates.Tapped:
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate, "CardTappedEvent",
                    int(u), self.user_profile["id"])
            cs = _hc.game_engine.CombatSessionEventArgs()
            cs.player_id = pl_t
            cs.id = combat_id
            cs.attacker = cid
            cs.blockers = []
            combats.append(cs)
            # Fire "when this attacks" triggers (e.g. Chimera Guard
            # Outrider: +[ATK] equal to this troop's [DEF] this turn).
            for event_type in ("CardAttackedEvent",
                               "CardAttackedOrBlockedEvent"):
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate, event_type, u,
                    self.user_profile["id"])
            # Rage X: when this attacks it gets a permanent attack modifier.
            if authoritative_port is not None:
                from rules_port.context import EffectContext
                from rules_port.combat_effects import apply_rage
                apply_rage(EffectContext.from_rules_port(
                    game, session, _hc._db, self, pl_t, ai_t, bstate,
                    "", ability=None), u)
            else:
                from abilities.framework.keywords.combat import apply_rage_keyword
                apply_rage_keyword(_hc._db, session, self, game, pl_t, ai_t,
                                   bstate, u)
        if attackers:
            # The client raises one champion-scoped CardsAttackedEvent after
            # the complete attack declaration, carrying the total attacker
            # count for metadata conditions such as "three or more".
            player_champ = (getattr(self, "_player_champ_scid", None) or
                            _hc.game_engine.SessionCardId(pl_t))
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate, "CardsAttackedEvent",
                int(player_champ.uid.uid64), self.user_profile["id"],
                attacker_count=len(attackers),
                attacker_uids=list(attackers))
        _hc._db.commit()
        game.push_combat_listing(pl_t, combats)
        self._send_battle_events(session, game, pl_t)
        # Advance to DeclareAttackPriorityWindow (the next stop).
        advance_checkpoint(bstate)
        save_checkpoint(session, bstate)
        self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_assign_damage_transaction(self, session, transaction,
                                          *, assignments=None,
                                          native_combat=False):
        """Resolve the client's combat damage assignment transaction."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        if native_combat:
            from rules_port.persistence import (current_phase, load_state,
                                                save_state)
            from rules_port.lifecycle import advance_checkpoint_phase
            load_checkpoint = load_state
            save_checkpoint = save_state
            current_checkpoint_phase = current_phase
            advance_checkpoint = advance_checkpoint_phase
        else:
            import battle_engine as _be
            load_checkpoint = _be.load_state
            save_checkpoint = _be.save_state
            current_checkpoint_phase = _be.current_phase
            advance_checkpoint = _be.advance_phase
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        bstate = load_checkpoint(session)
        self._current_bstate = bstate
        cur_phase = current_checkpoint_phase(bstate)
        native_phase_advanced = False
        if cur_phase == _hc.game_engine.ETurnPhases.AssignDamage:
            n_att = len(bstate.get("player_attackers") or {})
            # The AssignDamageOrderTransaction carries the player's
            # chosen blocker order (weakest-to-toughest) per combat:
            # m_AssignedDamageOrder -> DamageAssignment(CombatId,
            # ordered CardIds). Extract every card UID in stream order,
            # then order each attacker's blockers by their first
            # occurrence so resolve_combat assigns damage in that order.
            if assignments is not None:
                seq = []
                for _combat_id, card_ids in assignments:
                    try:
                        seq.extend(int(card_id) for card_id in card_ids)
                    except (TypeError, ValueError):
                        continue
            elif isinstance(inner_bytes, bytes):
                seq = []
                for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                    try:
                        v = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                        if (v & 0xFF) == 1:
                            seq.append(int(v))
                    except Exception:
                        continue
                blockers = {int(k): set(int(b) for b in (v or []))
                            for k, v in (bstate.get("ai_blockers") or {}).items()}
                order_map = {}
                for att, bset in blockers.items():
                    ordered = [u for u in seq if u in bset]
                    ordered += [b for b in bset if b not in ordered]
                    if ordered:
                        order_map[att] = ordered
                if order_map:
                    bstate["player_damage_order"] = {str(k): [str(b) for b in v]
                                                     for k, v in order_map.items()}
                    save_checkpoint(session, bstate)
            _hc.log_req(f"    AssignDamageOrder (AssignDamage step): resolving {n_att} attackers")
            if native_combat:
                bstate = self._resolve_combat_damage(
                    session, pl_t, ai_t, bstate)
                # Combat damage is resolved by the host projection, but the
                # native scheduler still owns the phase cursor.  Its priority
                # action is intentionally waiting for this transaction, so a
                # normal ``tick()`` cannot advance it.  Clear that consumed
                # window and perform the native transition before asking the
                # RulesPort driver to project the next input window;
                # advancing only the compatibility checkpoint leaves the
                # port at AssignDamage and makes Mono submit empty orders in
                # a loop.
                port = getattr(session, "_rules_port_session", None)
                if port is not None:
                    port.action_stack.clear()
                    port.advance_turn_phase()
                    phases = list(bstate.get("turn_phases") or ())
                    try:
                        bstate["phase_idx"] = phases.index(
                            port.current_turn_phase)
                    except ValueError:
                        pass
                    native_phase_advanced = True
            else:
                bstate = self._resolve_combat_damage(session, pl_t, ai_t, bstate)
            save_checkpoint(session, bstate)
            # If the AI's champion is dead, end the game (player wins).
            if bstate.get("ai_health", 10) <= 0:
                _hc.log_req(f"    AI defeated! ai_health={bstate['ai_health']}")
                self._finish_battle_gameend(
                    session, True, winners=[pl_t], losers=[ai_t])
                handled = True
                self._push_transaction_ack(session)
                return True
        elif cur_phase == _hc.game_engine.ETurnPhases.AssignFirstStrikeDamage:
            # Swiftstrike step: FirstStrike/DualStrike combatants deal
            # damage now; casualties are removed before the normal step.
            _hc.log_req("    AssignDamageOrder (Swiftstrike step): "
                    "resolving first-strike damage")
            if native_combat:
                bstate = self._resolve_combat_damage(
                    session, pl_t, ai_t, bstate, first_strike=True)
                port = getattr(session, "_rules_port_session", None)
                if port is not None:
                    port.action_stack.clear()
                    port.advance_turn_phase()
                    phases = list(bstate.get("turn_phases") or ())
                    try:
                        bstate["phase_idx"] = phases.index(
                            port.current_turn_phase)
                    except ValueError:
                        pass
                    native_phase_advanced = True
            else:
                bstate = self._resolve_combat_damage(
                    session, pl_t, ai_t, bstate, first_strike=True)
            save_checkpoint(session, bstate)
        else:
            _hc.log_req(f"    AssignDamageOrder ({cur_phase}): no damage yet, advancing")
        if bstate.get("pending_deck_search"):
            # A combat-death deck-search prompt is awaiting the player's
            # answer — do NOT advance to the next phase yet (the phase
            # packet would tear the client's target picker down).  The
            # SetAbilityActivationData answer advances the turn.
            save_checkpoint(session, bstate)
            self._push_transaction_ack(session)
            _hc.log_req("    AssignDamage: paused for deck-search answer")
            handled = True
            return True
        # Advance to the next phase (SecondMain after AssignDamage).  Native
        # RulesPort already performed that transition above; applying the
        # compatibility cursor a second time would skip the next phase.
        if not native_phase_advanced:
            advance_checkpoint(bstate)
        save_checkpoint(session, bstate)
        self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_set_auto_pass_transaction(self, session, transaction):
        """Record and apply the client's one-turn auto-pass choice."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        import re
        handled = False
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        # Tournament PvP has its own persisted two-player priority
        # state.  Do not feed its SetAutoPass through battle_engine:
        # that is the PvE/AI state machine and can advance a stale
        # turn_order, causing the other client to miss its resolve
        # priority.  Record F10 on the PvP state and let the normal
        # PvP pass route hand GreenLight between the clients.
        # m_PassingState: 1=Attack, 2=EndOfTurn, 3=EndPhase.
        st = 2
        if isinstance(inner_bytes, bytes):
            m = re.search(rb'm_PassingState;\d+;\d+;\d+;([0-9A-Fa-f]+);', inner_bytes)
            if m:
                try:
                    st = int(m.group(1), 16)
                except Exception:
                    pass
        if (session.session_name or "").startswith("tourney-"):
            try:
                from services.tournament_game import set_pvp_auto_pass
                if set_pvp_auto_pass(self, session, st):
                    self._push_transaction_ack(session)
                    handled = True
            except Exception as exc:
                _hc.log_req(f"    PvP SetAutoPass exception: {exc}")
        if handled:
            pass
        else:
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            bstate["player_autopass"] = st
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            _hc.log_req(f"    SetAutoPass: state={st} (player auto-passes to end of turn)")
            self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_cancel_auto_pass_transaction(self, session, transaction):
        """Clear the client's one-turn auto-pass choice."""
        import hconnect_server as _hc
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        handled = False
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        if (session.session_name or "").startswith("tourney-"):
            try:
                from services.tournament_game import pvp_load_state, pvp_save_state
                pstate = pvp_load_state(session)
                if pstate is not None:
                    if int(pstate.get("autopass_pid", 0)) == int(self.client_reck_id):
                        pstate.pop("autopass_pid", None)
                        pstate.pop("autopass_state", None)
                        pvp_save_state(session, pstate)
                    self._push_transaction_ack(session)
                    _hc.log_req("    PvP CancelAutoPass: auto-pass cleared")
                    handled = True
            except Exception as exc:
                _hc.log_req(f"    PvP CancelAutoPass exception: {exc}")
        if not handled:
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            bstate.pop("player_autopass", None)
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            _hc.log_req("    CancelAutoPass: auto-pass cleared")
        return True

    def _handle_set_turn_phases_transaction(self, session, transaction):
        """Persist client phase stops and acknowledge the transaction."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        self_stops = self._extract_enum_list(inner_bytes, "m_SelfTurnPhases", "m_OpponentTurnPhases")
        opp_stops = self._extract_enum_list(inner_bytes, "m_OpponentTurnPhases", None)
        # Persist for future battles.
        if self_stops is not None or opp_stops is not None:
            self._save_player_stops(self.user_profile["id"], self_stops, opp_stops)
        try:
            raw_to = session.turn_order
            if isinstance(raw_to, dict) and "turn_player" in raw_to:
                if getattr(session, "_rules_port_session", None) is not None:
                    from rules_port import lifecycle as _bse
                else:
                    import battle_engine as _bse
                bstate = _bse.load_state(session)
                if self_stops is not None:
                    bstate["player_self_stops"] = self_stops
                if opp_stops is not None:
                    bstate["player_opp_stops"] = opp_stops
                _bse.save_state(session, bstate)
                # If the AI turn is paused at an opponent-stop phase that
                # the player just removed from their stops, resume the AI
                # turn automatically instead of waiting for a pass.
                resume = bstate.get("ai_turn_phase_idx")
                if resume is not None:
                    paused_phase = _bse.TURN_PHASES[resume - 1]
                    if not _bse.is_opp_stop(bstate, paused_phase):
                        start_idx = resume
                        bstate.pop("ai_turn_phase_idx", None)
                        _bse.save_state(session, bstate)
                        _hc.log_req(f"    Stops updated — phase {paused_phase} no longer a stop; resuming AI turn")
                        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
                        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
                        self._run_ai_turn(session, pl_t, ai_t, bstate, start_idx=start_idx)
            elif (session.session_name or "").startswith("tourney-"):
                # PvP: persist each player's stops into the PvP state,
                # keyed by their reckoning pid, so the auto-advance can
                # respect "stop on Ready (self or opponent)" etc.
                from services.tournament_game import (pvp_load_state,
                                                      pvp_save_state,
                                                      pvp_session_lock)
                with pvp_session_lock(session):
                    _pstate = pvp_load_state(session)
                    if _pstate:
                        my_pid = int(self.client_reck_id)
                        if self_stops is not None:
                            _pstate[f"stops_self_{my_pid}"] = self_stops
                        if opp_stops is not None:
                            _pstate[f"stops_opp_{my_pid}"] = opp_stops
                        pvp_save_state(session, _pstate)
                self._pending_player_stops = (self_stops, opp_stops)
            else:
                self._pending_player_stops = (self_stops, opp_stops)
        except (ValueError, TypeError):
            self._pending_player_stops = (self_stops, opp_stops)
        _hc.log_req(f"    Captured phase stops: self={self_stops} opp={opp_stops}")
        # No 3055 events are pushed for SetTurnPhases — send an empty
        # sync packet so the client's one-at-a-time transaction pipeline
        # (m_HasPreviousTransactionBeenRespondedByServer) is unblocked;
        # otherwise the next transaction (incl. Withdraw) is dropped.
        self._push_transaction_ack(session)
        return True

    def _handle_pass_priority_transaction(self, session, transaction):
        """Resolve a priority pass through the gameplay service boundary."""
        import hconnect_server as _hc
        inner_bytes = transaction.inner_bytes
        pass_turn_phase = transaction.pass_turn_phase
        handled = False
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        # A pass arriving during setup (PreGame/PickGoesFirst/Mulligan,
        # before the player has kept their opening hand) has no turn-cycle
        # battle state yet — session.turn_order is still the default [].
        # Treat it as a no-op pass: acknowledge the transaction but do
        # NOT advance the turn, or it would corrupt the setup flow. The
        # client normally sends no transaction during PreGame (the
        # priority window auto-completes), but this is defensive.
        try:
            raw_to = session.turn_order
            setup_in_progress = not (isinstance(raw_to, dict) and "turn_player" in raw_to)
        except (ValueError, TypeError):
            setup_in_progress = True
        if setup_in_progress:
            from services.tournament_game import route_pvp_pass
            try:
                _rpv = route_pvp_pass(self, session)
            except Exception as _e:
                import traceback
                _hc.log_req(f"    PvP pass exception: {_e}")
                traceback.print_exc()
                _rpv = True
            if not _rpv:
                _hc.log_req("    Pass during setup phase — no-op (battle not started)")
            self._push_transaction_ack(session)
            handled = True
        else:
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            _hc.log_req(f"    Pass priority: turn={bstate.get('turn_player')} priority=player "
                    f"phase={_be.current_phase(bstate)} (client={pass_turn_phase})")

            # The client also auto-passes non-stop phases, which race with
            # the server's own auto-advance. Ignore a stale pass whose
            # phase no longer matches the server's current phase.
            # A pending AI card can survive a phase-list rebuild (for example
            # when the AI gains an attack phase after its main phase). The
            # client still submits the phase it was shown when it received
            # priority; do not strand that chain item as a stale pass.
            pending_ai_chain = (
                not _be.stack_empty(bstate)
                and bstate.get("turn_player") == _be.AI
                and bstate.get("ai_turn_phase_idx") is not None)
            # A paused AI stop stores the resume cursor one phase past the
            # stop. If a late packet left phase_idx at an earlier AI phase,
            # reconcile it only when that cursor identifies the exact phase
            # shown by the client. This prevents a legitimate post-combat pass
            # from being discarded while still rejecting arbitrary stale
            # passes.
            if (pass_turn_phase is not None
                    and pass_turn_phase != _be.current_phase(bstate)
                    and bstate.get("turn_player") == _be.AI
                    and bstate.get("ai_turn_phase_idx") is not None):
                held = _be.ai_held_phase_context(bstate)
                if held is not None and held[0] == pass_turn_phase:
                    _held_phase, held_idx, held_phases = held
                    bstate["turn_phases"] = list(held_phases)
                    bstate["phase_idx"] = held_idx
                    _be.save_state(session, bstate)
                    _hc.log_req(f"    Reconciled stale AI phase_idx to held stop "
                            f"phase {pass_turn_phase}")
            if (pass_turn_phase is not None
                    and pass_turn_phase != _be.current_phase(bstate)
                    and not pending_ai_chain):
                _hc.log_req(f"    Ignored stale pass (client phase {pass_turn_phase} != {_be.current_phase(bstate)})")
                self._push_transaction_ack(session)
                handled = True
            elif (_be.stack_empty(bstate) and
                  bstate.get("turn_player") == _be.AI):
                # The human passed during the AI's turn (an opponent-stop
                # phase). Resume the AI turn from the next phase.
                start_idx = bstate.get("ai_turn_phase_idx", 0)
                bstate.pop("ai_turn_phase_idx", None)
                _be.save_state(session, bstate)
                _hc.log_req(f"    Human passed during AI turn — resuming AI at idx {start_idx}")
                self._run_ai_turn(session, pl_t, ai_t, bstate, start_idx=start_idx)
                handled = True
            elif _be.current_phase(bstate) in (_hc.game_engine.ETurnPhases.EndTurn,
                                               _hc.game_engine.ETurnPhases.Discard):
                # Defensive: end the human's turn and hand it to the AI.
                g_end2 = self._fresh_game(session, pl_t, ai_t, bstate)
                self._dispatch_native_trigger(
                    g_end2, session, pl_t, ai_t, bstate,
                    "TurnEndedEvent", None, self.user_profile["id"])
                self._clear_combat_damage(session, bstate)
                self._clear_expired_temporary_attributes(
                    session, bstate, self.user_profile["id"], "end_turn",
                    clear_stat_buffs=True)
                from pvp_db import db_rules_port_warzone_projection
                for cu, tpl, card_user_id, card_state in db_rules_port_warzone_projection(
                        session.session_id):
                    scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(cu))
                    _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(
                        g_end2, scid, tpl)
                    g_end2.push_card_updated(
                        scid, pl_t if card_user_id else ai_t,
                        _hc.game_engine.ECardCollections.Warzone, ct,
                        template_id=tpl,
                        state=int(card_state or 0))
                self._send_battle_events(session, g_end2, pl_t)
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
                _be.save_state(session, bstate)
                if next_player == _be.PLAYER:
                    self._advance_to_priority(session, pl_t, ai_t, bstate)
                    _hc.log_req("    Bonus turn kept player in control")
                else:
                    self._run_ai_turn(session, pl_t, ai_t, bstate)
                    _hc.log_req("    Turn passed to AI; AI turn played out")
            else:
                # A non-empty chain: passing resolves the top of the chain
                # instead of advancing the phase (troops/spells/abilities/
                # triggers). After resolving, if the chain is still non-empty
                # re-grant priority (the player can respond to the next
                # item); when it empties, push ChainEmpty and advance.
                if not _be.stack_empty(bstate):
                    _be.stack_set_pass(bstate, _be.PLAYER, True)
                    if not _be.stack_both_passed(bstate):
                        # The AI hasn't passed yet — it auto-passes, so
                        # count it now and resolve.
                        _be.stack_set_pass(bstate, _be.AI, True)
                    if _be.stack_both_passed(bstate):
                        item = _be.stack_pop(bstate)
                        _be.stack_reset_passes(bstate)
                        if item is None:
                            _hc.log_req("    RulesPort rejected empty chain item after both passes")
                            self._push_transaction_ack(session)
                            return True
                        # A FRESH game seeded from battle state so any
                        # PlayerUpdated pushed during resolution (e.g.
                        # after a heal) reports the real health/resources.
                        gs = self._fresh_game(session, pl_t, ai_t, bstate)
                        self._resolve_stack_item(session, pl_t, ai_t, bstate, item, gs)
                        if (_be.stack_empty(bstate) and
                                not bstate.get("pending_choice") and
                                not bstate.get("pending_trigger") and
                                not bstate.get("pending_deck_search") and
                                not bstate.get("pending_conversation")):
                            gs.push_chain_empty()
                        cur_phase = _be.current_phase(bstate)
                        pending_input = (bstate.get("pending_choice") or
                                         bstate.get("pending_trigger") or
                                         bstate.get("pending_deck_search") or
                                         bstate.get("pending_conversation"))
                        paused_ai_idx = (
                            bstate.get("ai_turn_phase_idx")
                            if (_be.stack_empty(bstate) and not pending_input and
                                bstate.get("turn_player") == _be.AI)
                            else None)
                        if paused_ai_idx is not None:
                            # The chain interrupted an AI phase. Once the
                            # human resolves the item, continue the AI from
                            # its saved cursor; granting another human
                            # greenlight here strands the turn between phases.
                            bstate.pop("ai_turn_phase_idx", None)
                            _be.save_state(session, bstate)
                            self._send_battle_events(session, gs, pl_t)
                            self._run_ai_turn(
                                session, pl_t, ai_t, bstate,
                                start_idx=int(paused_ai_idx))
                        else:
                            # Priority to the active player for the next item
                            # (or the next phase). ResolveTopOfChain keeps the
                            # pass button labelled "Resolve <Card>".
                            if (_be.stack_empty(bstate) and not pending_input and
                                    bstate.get("turn_player") == _be.PLAYER):
                                if cur_phase in (
                                        _hc.game_engine.ETurnPhases.FirstMainPhase,
                                        _hc.game_engine.ETurnPhases.SecondMainPhase):
                                    # A troop may have entered during this chain
                                    # (notably a Speed troop). Recompute before
                                    # rebuilding the green light so the next
                                    # prompt immediately offers ProceedToCombat.
                                    if cur_phase == _hc.game_engine.ETurnPhases.FirstMainPhase:
                                        bstate["player_has_ready_troop"] = self._player_can_attack_troops(session)
                                    bstate["turn_phases"] = _be.build_turn_phases(bstate)
                                    _be.save_state(session, bstate)
                                # The chain fully emptied on the player's turn:
                                # re-announce the current phase with the player as
                                # priority player so the client pushes a FRESH state.
                                # Without this the prior state's cached button tail
                                # (the resolved card's name, set only under
                                # ResolveTopOfChain) lingers — the pass button shows
                                # "Continue to Second Main Phase <CardName>".
                                if cur_phase is not None:
                                    gs.push_turn_phase(cur_phase, pl_t, pl_t)
                                    self._push_phase_options(
                                        session, pl_t, ai_t, cur_phase)
                            if not pending_input:
                                gs.push_green_light(pl_t, self._priority_context_for(
                                    cur_phase, bstate))
                            self._send_battle_events(session, gs, pl_t)
                        _hc.log_req(f"    Resolved chain item {item.get('kind')} "
                                f"({item.get('ability_guid', '')[:8] or hex(item.get('source_uid') or 0)})")
                    else:
                        self._push_transaction_ack(session)
                    handled = True
                else:
                    # The player passed a stop phase: advance one phase, then
                    # auto-pass non-stop phases until the next stop phase.
                    bstate["player_passed"] = True
                    bstate["ai_passed"] = True
                    # Passing the FirstMainPhase decides whether combat
                    # happens. Recompute AFTER any troop the player played
                    # this main phase has resolved to the warzone — a Speed
                    # (haste) troop can attack the turn it enters, so the
                    # decision is made here, not just at Prep.
                    if (_be.current_phase(bstate) ==
                            _hc.game_engine.ETurnPhases.FirstMainPhase and
                            bstate.get("turn_player") == _be.PLAYER):
                        bstate["player_has_ready_troop"] = self._player_can_attack_troops(session)
                        bstate["turn_phases"] = _be.build_turn_phases(bstate)
                        _be.save_state(session, bstate)
                        _hc.log_req(f"    FirstMain pass: combat={bstate['player_has_ready_troop']} "
                                f"phases={len(bstate['turn_phases'])}")
                    # If the pass ends the AssignDamage step, resolve the
                    # combat damage (unblocked attackers hit the champion)
                    # before moving on. AssignDamageOrderTransaction is the
                    # normal path; a plain pass here is the fallback.
                    if (_be.current_phase(bstate) in (
                            _hc.game_engine.ETurnPhases.AssignDamage,
                            _hc.game_engine.ETurnPhases.AssignFirstStrikeDamage)):
                        fs = (_be.current_phase(bstate) ==
                              _hc.game_engine.ETurnPhases.AssignFirstStrikeDamage)
                        bstate = self._resolve_combat_damage(
                            session, pl_t, ai_t, bstate, first_strike=fs)
                        _be.save_state(session, bstate)
                        if bstate.get("ai_health", 10) <= 0:
                            _hc.log_req(f"    AI defeated via pass at AssignDamage! ai_health={bstate['ai_health']}")
                            self._finish_battle_gameend(
                                session, True, winners=[pl_t], losers=[ai_t])
                            self._push_transaction_ack(session)
                            return True
                        if bstate.get("pending_deck_search"):
                            # Pause for the deck-search answer; the
                            # follow-up advances the phase.
                            self._push_transaction_ack(session)
                            _hc.log_req("    Pass at AssignDamage: paused for deck-search answer")
                            handled = True
                            return True
                    _be.advance_phase(bstate)
                    _be.save_state(session, bstate)
                    self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True
