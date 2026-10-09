"""Attack and block declaration, combat damage, champion health and battle end."""

import json
import random


class CombatMixin:
    """Methods of HCPHandler, mixed into it (see hconnect_server)."""

    def _clear_combat_damage(self, session, bstate):
        import hconnect_server as _hc
        from rules_port.lifecycle import clear_combat_damage
        return clear_combat_damage(_hc._db, session.session_id)

    def _announce_champion_board_cards(self, game, session, pl_t, state=None):
        """Re-announce the champions' HUD cards once the client is in battle.

        ``ChampionCardPlayed`` owns champion card-view placement: it is the
        only event that moves that view into the player's ``ChampionsView``,
        which is the defender the client's ``BattleStateDeclareAttackers`` and
        ``UIBattle.OnAttackDeclared`` draw the troop-to-champion line to.  That
        event is sent once, inside the game-start burst, while Unity is still
        entering the battle scene; a view created after that (on demand, at the
        card root) is never moved, so the attack line ends in the middle of the
        board.  Every other card is re-pushed by ``_push_warzone_card_updates``
        on each phase push - this gives the champions the same treatment, once,
        after the client's first transaction proves the battle UI is live.

        The champion CardUpdated carries whatever the champion CardDef holds
        (abilities, counters, spell-point cost mods, live health), so the
        CardDefs are rebuilt for this packet first: a champion re-pushed
        without them wipes the client's champion ability buttons, counters and
        spell-power escalation display.
        """
        import hconnect_server as _hc
        # Both flags are keyed to this game's session id: a handler (and its
        # session object) outlives the game, and the next game needs its own
        # announcement once its own client is in the battle UI.
        game_id = getattr(session, "session_id", None)
        if getattr(session, "_champion_board_announced", None) == game_id:
            return False
        if getattr(session, "_client_in_battle", None) != game_id:
            return False
        entries = [entry for entry in (
            getattr(self, "_player_champ_board", None),
            getattr(self, "_ai_champ_board", None)) if entry]
        if len(entries) != 2:
            return False
        # ``OnChampionCardPlayed`` moves the champion's GoCardView into the
        # ChampionsView. If the first live packet already contains a trigger
        # sourced by that champion, the client queues the trigger's
        # AddToChain animation before this announcement. Appending the
        # announcement would then move the same card view back out of the
        # ChainView, leaving a Resolve button with no visible top item. Put
        # the one-time board setup first so later chain animations own the
        # final card-view position.
        event_start = len(game.events)
        setattr(session, "_champion_board_announced", game_id)
        player_uid = getattr(game, "player_uid", pl_t)
        ai_uid = getattr(game, "ai_uid", None)
        if not isinstance(state, dict):
            state = getattr(self, "_current_bstate", None) or {}
        # Register the champion CardDefs (abilities + persisted counters and
        # spell-point cost mods) and copy the live health onto this Game.  This
        # is the same rebuild the phase walk uses; it deliberately emits no
        # CardUpdated of its own.
        self._push_champions_warm(session, player_uid, ai_uid, state, game)
        for scid, is_ai, name, champ_guid in entries:
            owner = ai_uid if is_ai else player_uid
            # The CardDef's defense is the champion's live health (which is
            # what the HUD reports through ChampionHealth).
            game.push_card_updated(
                scid, owner, _hc.game_engine.ECardCollections.None_,
                _hc.game_engine.ECardTypes.Champion, attack=0,
                defense=int(getattr(
                    game, "ai_health" if is_ai else "player_health", 0) or 0),
                template_id=champ_guid)
            game.push_champion_card_played(
                owner, bool(is_ai), name, scid)
        game.push_player_updated(
            player_uid, champ_id=getattr(self, "_player_champ_scid", None))
        if ai_uid is not None:
            game.push_player_updated(
                ai_uid, champ_id=getattr(self, "_ai_champ_scid", None))
        announcement_events = game.events[event_start:]
        del game.events[event_start:]
        game.events[0:0] = announcement_events
        _hc.log_req("    Re-announced champion board cards (client in battle)",
                level="DEBUG")
        return True

    def _send_battle_events(self, session, game, pl_t):
        """Send a Game's queued events as one 3055 sync packet."""
        import hconnect_server as _hc
        # Serializing this projection is the drain point.  A projection the
        # port has already moved off can still hold publications no packet
        # serialized (Practice builds a fresh Game per packet, and a native
        # phase entry - an AI attack declaration, for example - publishes
        # before the host builds the next one).
        sink = getattr(getattr(session, "_rules_port_session", None),
                       "event_sink", None)
        if sink is not None:
            sink.drain_into(game)
        from rules_port.visibility import (project_visible_hands,
                                           project_visible_top_decks)
        current_state = getattr(self, "_current_bstate", {}) or {}
        project_visible_hands(
            _hc._db, session, self, game, pl_t,
            getattr(game, "ai_uid", _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)),
            current_state)
        project_visible_top_decks(
            _hc._db, session, self, game, pl_t,
            getattr(game, "ai_uid", _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)),
            current_state)
        # Placing the champions on the client's board is a one-shot projection
        # at game start; re-announce it in the first packet the client receives
        # from inside the battle scene, exactly like the warzone CardUpdated
        # re-push, so the attack line has a placed target.
        self._announce_champion_board_cards(game, session, pl_t, current_state)
        if not game.events:
            return False
        # RulesPort transactions are normalized with the decoded numeric UID,
        # whereas the ObjFmt encoder requires the typed UID object.  Keep the
        # ingress representation out of the packet boundary.
        if not isinstance(pl_t, _hc.game_engine.UID):
            pl_t = _hc.game_engine.UID(int(getattr(pl_t, "uid64", pl_t)))
        if not getattr(game, "_visibility_by_uid", None):
            from rules_port.visibility import \
                apply_player_visibility_to_game
            apply_player_visibility_to_game(game, current_state)
        # Unity places warzone cards under CardUpdated/ CardMoved.Controller
        # (the event player id), not from an intrinsic owner field on the
        # card.  A stale projection can therefore make an opponent constant
        # appear on the local side when the layout is rebuilt.  Reconcile
        # Reconcile public permanent-zone events against the authoritative
        # game_cards owner. Underground is included because tunneling emits
        # its CardMoved/CardUpdated pair there; leaving it out lets a stale
        # event player UID make an opponent's tunneled card render locally
        # until the next full board refresh. Hand/deck events retain their
        # visibility projection semantics.
        try:
            from abilities.framework._shared import owner_uid
            for event in game.events:
                collection = getattr(event, "collection", None)
                if collection not in (
                        _hc.game_engine.ECardCollections.Warzone,
                        _hc.game_engine.ECardCollections.Underground):
                    continue
                sid = getattr(event, "session_card_id", None)
                raw_sid = getattr(getattr(sid, "uid", sid), "uid64", sid)
                if raw_sid is None:
                    continue
                owner_id = _hc.db_card_owner_id(
                    session.session_id, int(raw_sid), conn=_hc._db)
                if owner_id is None:
                    continue
                projected_owner = owner_uid(owner_id, game.player_uid,
                                             getattr(game, "ai_uid", None),
                                             current_state)
                if hasattr(event, "player_id"):
                    event.player_id = projected_owner
                if hasattr(event, "controller"):
                    event.controller = projected_owner
        except Exception as exc:
            _hc.log_req(f"    Card owner projection skipped: {exc}")
        pkt = game.make_network_packet(pl_t)
        dw = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt)), 1,
                                "00000000-0000-0000-0000-000000000000")
        self._game_scnt = max(self._game_scnt, self.scnt) + 1
        self.scnt = self._game_scnt
        gs_inst = str(session.server_id)
        self.send({
            "issuer": f"0.0.0.0.ServiceGameSession.{_hc.SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
            "target": "ServiceGameSession", "instance": gs_inst,
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        self._event_q.append((self.scnt, dw, {}))
        return True

    def _push_phase_options_empty(self, session, pl_t, ai_t, game=None,
                                  send=True, refresh_warzone=True):
        """Push a PlayerOptionList with champion abilities + playable hand
        QuickActions (instant-speed — castable in ANY priority window).

        Called for stop phases that are NOT main phases so the champion's
        charge/spell power buttons and QuickActions are always interactive when
        the player has priority. Always carries a fresh PlayerUpdated so the
        client's resource / charge / SP display reflects the real battle state.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        game = game or self._fresh_game(session, pl_t, ai_t, bstate)
        # QuickActions may be played in ANY priority window (your turn or the
        # opponent's turn when a stop hands you priority).
        from pvp_db import db_hand_cards_with_templates
        from rules_port.static_rules import effective_option_projection
        playable = []
        rows = db_hand_cards_with_templates(
            session.session_id, self.user_profile["id"])
        resources = bstate.get("player_resources", 0)
        threshold = bstate.get("player_threshold", {})
        fc = self._warzone_troop_count(session, self.user_profile["id"])
        ec = self._warzone_troop_count(session, 0)
        for card_uid, cost, ct, thresh_json, abilities_json in rows:
            try:
                # One native static scan per hand card: the effective
                # attributes and the effective cost are both projections of
                # the same board view, and scanning twice doubled the cost of
                # every opponent-turn priority window.
                effective_attrs, cost = effective_option_projection(
                    _hc._db, session.session_id, bstate, card_uid)
                effective_attrs = int(effective_attrs or 0)
            except Exception:
                effective_attrs = 0
            if ("QuickAction" not in (ct or "") and not
                    (effective_attrs & _hc.game_engine.ECardAttributes.QuickAction)):
                continue
            ab = []
            if abilities_json:
                try:
                    ab = [g.lower() for g in json.loads(abilities_json)]
                except Exception:
                    pass
            if self._hand_card_playable(session, card_uid, ct, cost, thresh_json,
                                        ab, resources, threshold, True, fc, ec):
                playable.append(_hc.game_engine.SessionCardId(_hc.game_engine.UID(card_uid)))
        game.push_options(pl_t, playable)
        # Attach targeting TargetInstances for played spells (target picker).
        self._add_play_target_options(game, session, pl_t, ai_t)
        player_champ_cid = getattr(self, "_player_champ_scid", None)
        affordable_champ = []
        if player_champ_cid:
            abilities = getattr(self, "_player_champ_abilities", [])
            if abilities:
                champ_uid = (player_champ_cid.uid.to_uint64()
                             if hasattr(player_champ_cid, "uid") else 0)
                champ_targets = self._champion_ability_targets(
                    session, abilities, champ_uid)
                champ_costs = self._champion_ability_costs(
                    session, abilities, champ_uid)
                affordable_champ = self._filter_affordable_abilities(
                    abilities, bstate, _be.current_phase(bstate),
                    target_data=champ_targets, cost_data=champ_costs,
                    session=session)
                game.add_champion_to_options(
                    pl_t, player_champ_cid, affordable_champ,
                    self._discard_costs_for(session, abilities),
                    champ_targets, champ_costs)
        # Warzone-troop QuickAction abilities (e.g. Living Totem's 3-cost activations)
        # are castable in ANY priority window — include them here for non-main phases.
        affordable = self._affordable_troop_abilities(session, bstate)
        if affordable:
            self._add_troop_ability_options(game, pl_t, session, affordable,
                                             bstate)
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        if refresh_warzone:
            self._push_warzone_card_updates(game, session, pl_t, ai_t)
        if send:
            self._send_battle_events(session, game, pl_t)
        _hc.log_req(f"    Phase options (priority window): {len(playable)} playable "
                f"quick actions, champ_ab={len(affordable_champ)}")

    def _push_practice_ability_chain_window(self, session, port, game):
        """Give the single Practice client the chain response window.

        An ability activation that leaves a chain item pending must replace the
        stale main-phase options with a chain-only window plus a
        ResolveTopOfChain green light — the tournament path does the same for
        both participants.  Practice has no client-submitted table state, so
        the identities come from the live projection: the single client is the
        service player and the other side is the server AI.  Deriving them here
        keeps the projection independent of caller-local names (a missing
        binding used to abort it, leaving the chain item unresolved and every
        later Basic Action unplayable).
        """
        import hconnect_server as _hc
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        priority = port.action_stack.priority_player_id
        if not _hc._same_uid(priority, pl_t):
            return False
        self._push_phase_options_empty(session, game.player_uid, game.ai_uid)
        from rules_port.persistence import load_state as _load_state
        chain_game = self._fresh_game(
            session, pl_t, ai_t, _load_state(session, default=lambda: {}))
        # OnTurnPhaseUpdated checks the client's current HasPriority bit. Send
        # GreenLight first so a client that just lost priority to the AI does
        # not request a main-phase resync before this response window arrives.
        chain_game.push_green_light(
            pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
        chain_game.push_turn_phase(
            port.current_turn_phase,
            pl_t if _hc._same_uid(port.active_player_id, pl_t) else ai_t,
            pl_t)
        self._send_battle_events(session, chain_game, pl_t)
        _hc.log_req("    RulesPort Practice ability priority window: "
                f"phase={port.current_turn_phase} priority=player")
        return True

    def _push_attack_options(self, session, pl_t, ai_t):
        """Push a PlayerOptionList marking the player's ready warzone troops
        as attackable (ECardUsage.Attack) during the DeclareAttack phase.

        Only troops that have StartedATurnOnYourSide (survived to this turn,
        i.e. not summoning sick) and are not tapped may attack — the same rule
        the client enforces in Card.HasSummoningSickness() / CanAttack().

        ForceAttack ("Must attack", e.g. Savage Raider) troops are declared as
        attackers HERE, server-side — the authoritative DeclareAttackState in
        the client does the same, and the server must not rely on the player
        remembering to drag them.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        self._auto_declare_force_attackers(session, pl_t, ai_t, bstate, game)
        rows = _hc.db_warzone_troops_basic(session.session_id, self.user_profile["id"])
        from rules_port.static_rules import effective_attributes
        ready = []
        for uid, ct, state in rows:
            state = state or 0
            attrs = int(effective_attributes(
                _hc._db, session.session_id, bstate, int(uid)) or 0)
            # A troop with Speed may attack the turn it comes into play (the
            # client's HasSummoningSickness() exempts Speed) — e.g. a Blaze
            # Elemental summoned by Poca's charge power.
            if (((state & _hc.game_engine.ECardStates.StartedATurnOnYourSide)
                 or ((attrs or 0) & _hc.game_engine.ECardAttributes.Speed))
                    and not (state & _hc.game_engine.ECardStates.Tapped)
                    and not (state & _hc.game_engine.ECardStates.Attacking)
                    and not ((attrs or 0) &
                             (_hc.game_engine.ECardAttributes.CantAttack |
                              _hc.game_engine.ECardAttributes.Defensive))):
                ready.append(_hc.game_engine.SessionCardId(_hc.game_engine.UID(uid)))
        ev = game._make_event(_hc.game_engine.PlayerOptionListSessionEventArgs)
        ev.player_id = pl_t
        for scid in ready:
            opt = game._make_event(_hc.game_engine.PlayerOptionSessionEventArgs)
            opt.card = scid
            opt.state = _hc.game_engine.ECardUsage.Attack
            opt.instances = []
            ev.options.append(opt)
        game._push(ev)
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        self._push_warzone_card_updates(game, session, pl_t, ai_t)
        self._send_battle_events(session, game, pl_t)
        _hc.log_req(f"    Attack options: {len(ready)} ready troop(s)")

    def _auto_declare_force_attackers(self, session, pl_t, ai_t, bstate, game):
        """Server-authoritative "Must attack": every ready player warzone troop
        with ForceAttack is declared as an attacker automatically, exactly like
        the client's DeclareAttackState does on the authoritative side — the
        player cannot forget (or refuse) to attack with them.

        Idempotent: troops already attacking (or already in
        bstate['player_attackers']) are skipped, so re-pushing the DeclareAttack
        options never double-declares.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        from pvp_db import db_card_state_value as db_card_state_raw
        from rules_port.static_rules import effective_attributes
        rows = _hc.db_warzone_troops_basic(session.session_id,
                                       self.user_profile["id"])
        ai_champ_uid = getattr(self, "_ai_champ_scid", None)
        ai_champ_uid64 = ai_champ_uid.uid.to_uint64() if ai_champ_uid else 0
        attackers = {int(k): int(v)
                     for k, v in (bstate.get("player_attackers") or {}).items()}
        combats = []
        for uid, ct, state in rows:
            state = state or 0
            attrs = int(effective_attributes(
                _hc._db, session.session_id, bstate, int(uid)) or 0)
            if not (attrs & _hc.game_engine.ECardAttributes.ForceAttack):
                continue
            if (state & (_hc.game_engine.ECardStates.Attacking |
                         _hc.game_engine.ECardStates.Tapped)):
                continue
            if not (state & _hc.game_engine.ECardStates.StartedATurnOnYourSide) \
                    and not (attrs & _hc.game_engine.ECardAttributes.Speed):
                continue
            if attrs & (_hc.game_engine.ECardAttributes.CantAttack |
                        _hc.game_engine.ECardAttributes.Defensive):
                continue
            u = int(uid)
            if u in attackers:
                continue
            attackers[u] = int(ai_champ_uid64)
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(u))
            combat_id = _hc.game_engine.CombatId(pl_t, u & 0xFFFF)
            port = getattr(session, "_rules_port_session", None)
            if port is not None:
                # ForceAttack declarations must exist in the same native
                # CombatManager as ordinary client declarations.  The event
                # projection below is not a second combat authority.
                port_attacker = port.get_card(u) or u
                port_defender = (port.get_card(ai_champ_uid64)
                                 if ai_champ_uid64 else None)
                port_combat = port.combat_manager.create_attack(
                    combat_id, pl_t, port_defender or ai_champ_uid64)
                port_combat.declare_attacker(port_attacker)
                combat_id = self._wire_combat_id_for_attacker(
                    session, pl_t, u, u & 0xFFFF)
            game.push_attack_declared(
                combat_id, pl_t,
                ai_champ_uid or _hc.game_engine.SessionCardId(ai_t), scid)
            state_bits = (_hc.game_engine.ECardStates.Attacking |
                          _hc.game_engine.ECardStates.HasAttacked)
            if not (attrs & _hc.game_engine.ECardAttributes.Steadfast):
                state_bits |= _hc.game_engine.ECardStates.Tapped
            _hc.db_card_set_attacking_state(session.session_id, u, state_bits)
            card_details = _hc.db_card_zone_details(
                session.session_id, u, conn=_hc._db)
            tpl_guid = card_details[0] if card_details else None
            self._card_full_data(game, scid, tpl_guid)
            pushed_state = db_card_state_raw(session.session_id, u) or state_bits
            game.push_card_updated(
                scid, pl_t, _hc.game_engine.ECardCollections.Warzone,
                _hc.game_engine.ECardTypes.Troop, template_id=tpl_guid,
                state=pushed_state)
            if state_bits & _hc.game_engine.ECardStates.Tapped:
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate, "CardTappedEvent", u,
                    self.user_profile["id"])
            cs = _hc.game_engine.CombatSessionEventArgs()
            cs.player_id = pl_t
            cs.id = combat_id
            cs.attacker = scid
            cs.blockers = []
            combats.append(cs)
            # Fire "when this attacks" triggers (e.g. Chimera Guard Outrider).
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate, "CardAttackedEvent", u,
                self.user_profile["id"])
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate,
                "CardAttackedOrBlockedEvent", u, self.user_profile["id"])
            from rules_port.combat_effects import apply_rage
            from rules_port.context import EffectContext
            apply_rage(EffectContext.from_rules_port(
                game, session, _hc._db, self, pl_t, ai_t, bstate,
                "", ability=None), u)
        if combats:
            bstate["player_attackers"] = {
                str(k): str(v) for k, v in attackers.items()}
            _be.save_state(session, bstate)
            game.push_combat_listing(pl_t, combats)
            _hc.log_req(f"    Auto-declared {len(combats)} ForceAttack attacker(s): "
                    f"{[hex(c.attacker.uid.uid64) for c in combats]}")
        return combats

    def _push_blocker_options(self, session, pl_t, ai_t):
        """Push a PlayerOptionList enabling the player to declare blockers during
        the DeclareDefense phase of the AI's turn.

        The client's BattleStateDeclareBlockers only lets a troop block when
        State.HasUsage(troop, ECardUsage.Defend) is set AND
        State.GetTargetsFor(troop, ResourceId.Blocking) lists at least one
        attacker (BattleStateDeclareBlockers.CanEnterCombat). So every eligible
        (untapped) player warzone troop gets a Defend usage plus a Blocking
        TargetInstance whose targets are the AI's declared attackers
        (bstate['ai_attackers']). ResourceId.Blocking =
        83659505-152d-4ddc-89df-7c29bdfba16d (client ResourceId.cs).
        """
        import hconnect_server as _hc
        if not self._player_can_block(session):
            return False
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        attackers = bstate.get("ai_attackers") or {}
        attacker_scids = [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(u)))
                          for u in attackers]
        # Use the same metadata-driven combat legality as resolution. This
        # includes conditional static rules such as Electroid's Dwarf/Robot
        # control threshold, which is not a persisted keyword bit.
        from rules_port.combat_rules import can_block
        rows = _hc.db_warzone_defender_rows(
            session.session_id, self.user_profile["id"],
            _hc.game_engine.ECardStates.Tapped, conn=_hc._db)
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        ev = game._make_event(_hc.game_engine.PlayerOptionListSessionEventArgs)
        ev.player_id = pl_t
        blocking_id = _hc.game_engine.ResourceId.from_str(
            "83659505-152d-4ddc-89df-7c29bdfba16d")
        for uid, _battrs in rows:
            # This blocker's blockable targets: every attacker it may block.
            blockable = []
            for scid, u in zip(attacker_scids, attackers):
                if can_block(_hc._db, session.session_id, bstate,
                             int(u), int(uid)):
                    blockable.append(scid)
            if not blockable:
                continue
            opt = game._make_event(_hc.game_engine.PlayerOptionSessionEventArgs)
            opt.card = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(uid)))
            opt.state = _hc.game_engine.ECardUsage.Defend
            inst = game._make_event(_hc.game_engine.OptionInstanceSessionEventArgs)
            inst.opt_id = blocking_id
            inst.min_target_counts.append(0)
            inst.max_target_counts.append(len(blockable))
            inst.target_ids.append(blocking_id)
            tgt = game._make_event(_hc.game_engine.TargetInstanceSessionEventArgs)
            tgt.target_index = 0
            tgt.target_id = blocking_id
            tgt.targets = list(blockable)
            inst.target_instances.append(tgt)
            opt.instances.append(inst)
            ev.options.append(opt)
        game._push(ev)
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        self._push_warzone_card_updates(game, session, pl_t, ai_t)
        self._send_battle_events(session, game, pl_t)
        _hc.log_req(f"    Blockers options: {len(rows)} defender troop(s) for {len(attacker_scids)} AI attacker(s)")
        return True

    def _player_can_block(self, session):
        """True if at least one player troop can block an AI attacker.

        A generic untapped troop is not sufficient here: when all attackers
        have Flight, only Flight/SkyGuard troops are legal blockers.  Opening
        DeclareBlockers for an otherwise ineligible board leaves the client
        asking for a block it cannot make.
        """
        import hconnect_server as _hc
        import game_engine as _ge
        from rules_port.persistence import load_state
        bstate = load_state(session)
        attackers = [int(uid) for uid in (bstate.get("ai_attackers") or {})]
        if not attackers:
            # Keep the broad board check for callers that ask before attacker
            # declarations are persisted; the DeclareDefense path itself has
            # the attacker list and applies the full pairwise legality test.
            attackers = None
        rows = _hc.db_warzone_blocker_uids(
            session.session_id, self.user_profile["id"],
            _ge.ECardStates.Tapped, conn=_hc._db)
        from rules_port.combat_rules import can_block
        from rules_port.static_rules import effective_attributes
        if attackers is None:
            return any(not (int(effective_attributes(
                _hc._db, session.session_id, bstate, int(row[0])) or 0) &
                _ge.ECardAttributes.CantBlock) for row in rows)
        return any(can_block(_hc._db, session.session_id, bstate, attacker_uid,
                             int(row[0]))
                   for row in rows for attacker_uid in attackers)

    def _finish_battle_gameend(self, session, won, winners, losers):
        """Persist FRA progress before publishing the terminal event.

        ArenaClient immediately joins the Arena lobby after it receives
        ``GameEnded``.  The FRA result therefore has to advance the persisted
        challenger index before that event is sent.  Campaign and tournament
        sessions retain their existing post-event result handling.
        """
        import hconnect_server as _hc
        import commands as _cmd
        prepared = _hc.campaign.prepare_fra_battle_gameend(
            self, _hc._db, session, won)
        _cmd.push_battle_game_end(
            handler=self, session=session,
            winners=winners, losers=losers)
        if prepared is not None:
            _hc.campaign.publish_fra_battle_gameend(
                self, prepared, _hc.SERVICE_MAIL_UID)
        else:
            _hc.campaign.handle_battle_gameend(
                self, _hc._db, session, won, _hc.SERVICE_MAIL_UID,
                _hc.UID_TYPE["ServiceCampaign"])
        return prepared

    def _check_champion_health(self, session, pl_t, ai_t, bstate):
        """End the game immediately when a champion's health is 0 or less.
        State checks run at EVERY phase (and after chain resolutions), not
        only when combat damage happens — e.g. the AI killing itself with its
        own Fang of the Mountain God must not leave a 0-health champion alive
        until the player's next combat."""
        import hconnect_server as _hc
        ph = int(bstate.get("player_health", 20))
        ah = int(bstate.get("ai_health", 20))
        # Give metadata-defined survival abilities their client-equivalent
        # replacement event before publishing GameEnded.  These abilities
        # resolve immediately (ChampionWouldLose is authored as an
        # ignores-chain event), so re-read health after each dispatch.
        if ph <= 0:
            loss_game = self._fresh_game(session, pl_t, ai_t, bstate)
            from rules_port.context import EffectContext
            from rules_port.death_effects import champion_would_lose
            bstate["_rules_port_attached"] = True
            champion_would_lose(EffectContext.from_rules_port(
                loss_game, session, _hc._db, self, pl_t, ai_t, bstate,
                "", ability=None), self.user_profile["id"])
            if loss_game.events:
                self._send_battle_events(session, loss_game, pl_t)
            ph = int(bstate.get("player_health", 20))
        if ah <= 0:
            loss_game = self._fresh_game(session, pl_t, ai_t, bstate)
            from rules_port.context import EffectContext
            from rules_port.death_effects import champion_would_lose
            bstate["_rules_port_attached"] = True
            champion_would_lose(EffectContext.from_rules_port(
                loss_game, session, _hc._db, self, pl_t, ai_t, bstate,
                "", ability=None), 0)
            if loss_game.events:
                self._send_battle_events(session, loss_game, pl_t)
            ah = int(bstate.get("ai_health", 20))
        if ph <= 0:
            self._finish_battle_gameend(
                session, False, winners=[ai_t], losers=[pl_t])
            _hc.log_req(f"    Game over: player health {ph} <= 0 (AI wins)")
            return True
        if ah <= 0:
            self._finish_battle_gameend(
                session, True, winners=[pl_t], losers=[ai_t])
            _hc.log_req(f"    Game over: AI health {ah} <= 0 (player wins)")
            return True
        return False

    def _push_phase_options(self, session, pl_t, ai_t, phase):
        """Push the PlayerOptionList appropriate for `phase` (used when re-
        granting priority after a chain resolves / at a stop): playable cards for
        main phases, attack options for DeclareAttack, the combat listing at
        damage steps, otherwise just champion abilities + QuickActions."""
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        if phase in (_hc.game_engine.ETurnPhases.FirstMainPhase,
                     _hc.game_engine.ETurnPhases.SecondMainPhase):
            self._push_main_phase_options(session, pl_t, ai_t)
        elif phase == _hc.game_engine.ETurnPhases.DeclareAttack:
            self._push_attack_options(session, pl_t, ai_t)
        elif phase in (_hc.game_engine.ETurnPhases.AssignDamage,
                       _hc.game_engine.ETurnPhases.AssignFirstStrikeDamage):
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            self._push_combat_listing_phase(session, pl_t, ai_t, bstate)
        elif phase == _hc.game_engine.ETurnPhases.DeclareDefense:
            self._push_blocker_options(session, pl_t, ai_t)
        else:
            self._push_phase_options_empty(session, pl_t, ai_t)

    def _ai_pass_declare_defense(self, session, pl_t, ai_t, bstate, game):
        import ai
        return ai.ai_pass_declare_defense(self, session, pl_t, ai_t, bstate, game)

    def _wire_combat_id_for_attacker(self, session, player_uid,
                                     attacker_uid, fallback_serial):
        """Use the native CombatManager ID in every client combat packet.

        The client returns the ID from its current CombatListing in its damage
        order transaction. Rebuilding that ID from a dictionary position or a
        card UID can point at a different native Combat when multiple attacks
        are in flight.
        """
        import hconnect_server as _hc
        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            try:
                attacker = port.get_card(int(attacker_uid)) or int(attacker_uid)
                combat = port.combat_manager.combat_for_attacker(attacker)
                serial = int(combat.combat_id.serial_number) if combat else 0
                if serial > 0:
                    return _hc.game_engine.CombatId(player_uid, serial)
            except (AttributeError, TypeError, ValueError):
                pass
        return _hc.game_engine.CombatId(player_uid, int(fallback_serial))

    def _push_combat_listing_phase(self, session, pl_t, ai_t, bstate):
        """Push the current combat listing (no blockers yet) at a damage step
        so the client's BattleStateAssignDamage can auto-commit and send
        AssignDamageOrderTransaction."""
        import hconnect_server as _hc
        game = _hc.game_engine.Game(session.session_id, pl_t, ai_t)
        self._maybe_attach_rules_port(session, game, bstate)
        attackers = bstate.get("player_attackers") or {}
        combats = []
        for i, u in enumerate(attackers):
            attacker_uid = int(u)
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(attacker_uid))
            combat_id = self._wire_combat_id_for_attacker(
                session, pl_t, attacker_uid, i + 1)
            cs = _hc.game_engine.CombatSessionEventArgs()
            cs.player_id = pl_t
            cs.id = combat_id
            cs.attacker = scid
            cs.blockers = []
            combats.append(cs)
        if combats:
            game.push_combat_listing(pl_t, combats)
        self._send_battle_events(session, game, pl_t)

    def _player_can_attack_troops(self, session, user_id=None):
        import hconnect_server as _hc
        bstate = getattr(self, "_current_bstate", None)
        if (getattr(session, "_rules_port_session", None) is not None or
                (bstate or {}).get("_rules_port_attached")):
            from rules_port.combat_rules import player_has_eligible_attackers
            if bstate is None:
                from rules_port.persistence import load_state
                bstate = load_state(session)
            if user_id is None:
                user_id = (self.user_profile.get("id")
                           if isinstance(self.user_profile, dict) else 0)
            if user_id is None:
                return False
            return player_has_eligible_attackers(
                _hc._db, session.session_id, bstate or {}, int(user_id))
        import ai
        return ai.player_can_attack_troops(self, session, user_id)

    def _ai_can_attack_troops(self, session):
        import hconnect_server as _hc
        bstate = getattr(self, "_current_bstate", None)
        if (getattr(session, "_rules_port_session", None) is not None or
                (bstate or {}).get("_rules_port_attached")):
            from rules_port.combat_rules import player_has_eligible_attackers
            if bstate is None:
                from rules_port.persistence import load_state
                bstate = load_state(session)
            return player_has_eligible_attackers(
                _hc._db, session.session_id, bstate or {}, 0)
        import ai
        return ai.ai_can_attack_troops(self, session)

    def _refresh_native_combat_branch(self, session, battle_state, *,
                                      active_is_ai, active_phase):
        """Refresh the combat-branch fact the native FirstMainPhase reads.

        The checkpoint fact is written at the turn's Prep, so a troop that
        entered play afterwards - the client's own Speed troop, or an AI card
        that resolved inside the human's response window - leaves it stale.
        The native phase graph then leaves First Main straight for Second Main
        and skips DeclareAttack entirely.  Returns True when the persisted
        phase plan was rebuilt and therefore must be re-read by the caller.
        """
        import hconnect_server as _hc
        self._current_bstate = battle_state
        ready = bool(self._ai_can_attack_troops(session) if active_is_ai
                     else self._player_can_attack_troops(session))
        if active_is_ai:
            # The AI driver rebuilds its own plan when it reaches First Main.
            battle_state["player_has_ready_troop"] = ready
            return False
        if active_phase != _hc.game_engine.ETurnPhases.FirstMainPhase:
            # Only First Main may be rebuilt: from Second Main on, swapping the
            # base plan for the combat plan renumbers ``phase_idx`` (Second
            # Main becomes DeclareCombat) and strands the client's next pass.
            return False
        if bool(battle_state.get("player_has_ready_troop")) == ready:
            return False
        from rules_port.lifecycle import build_turn_phases
        from rules_port.persistence import save_state
        battle_state["player_has_ready_troop"] = ready
        battle_state["turn_phases"] = build_turn_phases(battle_state)
        save_state(session, battle_state)
        return True

    def _ai_declare_attackers(self, game, session, ai_t, pl_t, battle_state):
        import ai
        return ai.ai_declare_attackers(self, game, session, ai_t, pl_t, battle_state)

    def _resolve_ai_combat_damage(self, session, pl_t, ai_t, bstate):
        import ai
        return ai.resolve_ai_combat_damage(self, session, pl_t, ai_t, bstate)

    def _resolve_combat_damage(self, session, pl_t, ai_t, bstate,
                               first_strike=False):
        """Resolve player combat through the native RulesPort."""
        import hconnect_server as _hc
        from rules_port.context import EffectContext
        from rules_port.combat_damage import resolve
        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            # CombatManager is authoritative while the session is live; keep
            # the legacy-shaped checkpoint as its persistence/wire projection
            # for the native damage adapter and reconnects.
            def card_uid(value):
                value = getattr(value, "session_card_id", value)
                value = getattr(value, "uid", value)
                return int(getattr(value, "uid64", value))
            declared = {}
            blockers = {}
            order = {}
            for combat in port.combat_manager.combats:
                if combat.attacker is None:
                    continue
                try:
                    attacker_uid = card_uid(combat.attacker)
                    defender_uid = card_uid(combat.defender)
                except (TypeError, ValueError):
                    continue
                declared[str(attacker_uid)] = str(defender_uid)
                blocker_ids = []
                for blocker in combat.blockers:
                    try:
                        blocker_ids.append(card_uid(blocker))
                    except (TypeError, ValueError):
                        continue
                if blocker_ids:
                    blockers[str(attacker_uid)] = [str(uid)
                                                    for uid in blocker_ids]
                    order[str(attacker_uid)] = [str(uid)
                                                for uid in blocker_ids]
            if declared:
                bstate["player_attackers"] = declared
                bstate["ai_blockers"] = blockers
                bstate["player_damage_order"] = order
                from rules_port.persistence import save_state
                save_state(session, bstate)
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        context = EffectContext.from_rules_port(
            game, session, _hc._db, self, pl_t, ai_t, bstate,
            "", ability=None)
        result = resolve(context, first_strike=first_strike,
                         attacker_key="player_attackers",
                         blocker_key="ai_blockers")
        if game.events:
            self._send_battle_events(session, game, pl_t)
        # Combat damage is simultaneous, so the champion's defeat is decided
        # once the whole step (damage + lifedrain) is applied.
        self._check_champion_health(session, pl_t, ai_t, bstate)
        return result

    def _discard_card_to_owner(self, session, pl_t, ai_t, card_uid):
        """Move a card to its OWNER's graveyard.

        Restores user_id = owner_user_id so a stolen card (e.g. Mind Grasp)
        returns to its original owner's graveyard rather than the controller's,
        then updates location to 'discard'. Returns (game_with_events, owner_uid)
        so the caller can send the events targeting the owner. Returns
        (None, None) if the card isn't found.
        """
        from rules_port.host_mutations import discard_card_to_owner
        return discard_card_to_owner(self, session, pl_t, ai_t, card_uid)

    def _sacrifice_troop(self, game, session, pl_t, ai_t, card_uid,
                         expected_owner_id=None):
        """Pay a sacrifice cost (e.g. Abominate's "sacrifice a troop you
        control"): move the troop to its owner's graveyard (no Dead state — a
        sacrifice isn't combat damage). The troop still DIES, so its Deathcry
        triggers (mirrors the client; e.g. Spiritbound Spy -> Phantom)."""
        import hconnect_server as _hc
        row = _hc.db_card_basic(session.session_id, card_uid)
        if not row:
            return False
        tpl_guid, owner_id = row
        # Sacrifice costs are restricted to a troop controlled by the
        # activating player.  Re-check the authoritative row immediately
        # before mutating it: a stale TargetMap or a cross-player UID must
        # never move an opponent's permanent into the wrong graveyard.
        card_row = _hc.db_card_sacrifice_info(
            session.session_id, int(card_uid), conn=_hc._db)
        if not card_row:
            _hc.log_req(f"    Sacrifice rejected missing card {int(card_uid)}")
            return False
        actual_owner, location, card_type = card_row
        if (str(location or "").lower() != "warzone" or
                "troop" not in str(card_type or "").lower()):
            _hc.log_req(
                f"    Sacrifice rejected invalid zone/type uid={int(card_uid)} "
                f"owner={int(actual_owner or 0)} location={location!r} type={card_type!r}")
            return False
        if expected_owner_id is not None and int(actual_owner or 0) != int(expected_owner_id):
            _hc.log_req(
                f"    Sacrifice rejected wrong owner uid={int(card_uid)} "
                f"expected={int(expected_owner_id)} actual={int(actual_owner or 0)}")
            return False
        owner_id = int(actual_owner or 0)
        # Sacrifice is a native death transition: zone events, replacement
        # checks, CardSacrificed, and Deathcry resolution stay inside one
        # RulesPort lifecycle.
        from rules_port.context import EffectContext
        from rules_port.death_effects import kill_troop
        from rules_port.persistence import load_state, save_state
        sacrifice_state = load_state(session)
        sacrifice_state["_rules_port_attached"] = True
        sacrifice_state["_rules_port_native_effect"] = True
        result = kill_troop(
            EffectContext.from_rules_port(
                game, session, _hc._db, self, pl_t, ai_t, sacrifice_state,
                "", ability=None), int(card_uid), cause="sacrifice")
        save_state(session, sacrifice_state)
        return str(result).startswith("killed")

    def _push_discard_prompt(self, game, session, pl_t, ai_t, bstate,
                             ability_guid=None):
        """Push a class-23 AbilityActivationDataRequired prompt so the PLAYER
        picks a hand card to discard (e.g. a Deathcry that forces each opposing
        champion to discard, like Bloatcap). The follow-up
        SetAbilityActivationDataTransaction discards the chosen card."""
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        player_champ_scid = getattr(self, "_player_champ_scid", None)
        resolving_ability = ability_guid or bstate.get("resolving_ability", "")
        # AI-controlled abilities must never open a human card picker.  The
        # responsible owner is carried by the resolver checkpoint; choose a
        # random card from that owner's hand and let the normal discard
        # mutation/event path apply it.
        owner_id = bstate.get("resolving_owner_id")
        human_owner = self.user_profile.get("id") if isinstance(
            self.user_profile, dict) else None
        if owner_id is not None and int(owner_id or 0) != int(human_owner or -1):
            rows = [(row[1],) for row in _hc.db_ordered_zone_rows(
                session.session_id, int(owner_id), "hand", conn=_hc._db)]
            if rows:
                chosen = int(random.choice(rows)[0])
                gd, _owner_uid = self._discard_card_to_owner(
                    session, pl_t, ai_t, chosen)
                if gd is not None:
                    game.events.extend(gd.events)
                bstate["ai_discarded_uid"] = chosen
                _hc.log_req(f"    AI discard resolved without player prompt: {chosen}")
            else:
                bstate["ai_discarded_uid"] = None
            return
        # Ability configuration path (same protocol used by Necrotic Mage's
        # PVE ability): class-23 opens BattleStateConfigureAbility and the
        # selected hand card is returned in SetAbilityActivationData.
        hand = [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(uid))) for (uid,) in
                _hc.db_hand_card_uids(
                    session.session_id,
                    self.user_profile["id"] if self.user_profile else 0,
                    conn=_hc._db)]
        if not hand:
            # The authored discard target allows a best-effort minimum.  An
            # empty hand therefore resolves the Deathcry as a no-op; opening
            # class 23 with min/max=1 and zero targets leaves the client in a
            # picker it cannot answer and strands the native chain.
            _hc.log_req("    Deathcry discard: no legal hand target")
            return "discard: no legal target"
        prompt = self._discard_prompt_data(resolving_ability, bstate=bstate)
        if not prompt or not prompt[1]:
            _hc.log_req("    Deathcry discard: missing metadata prompt target")
            return
        child_ability, target_template = prompt
        source_uid = bstate.get("resolving_source_uid")
        source_card_id = (_hc.game_engine.SessionCardId(_hc.game_engine.UID(
            int(source_uid))) if source_uid else
            (player_champ_scid if player_champ_scid else pl_t))
        options = game._make_event(_hc.game_engine.PlayerOptionListSessionEventArgs)
        options.player_id = pl_t
        opt = game._make_event(_hc.game_engine.PlayerOptionSessionEventArgs)
        opt.card = source_card_id
        opt.state = _hc.game_engine.ECardUsage.Activate
        child = game._make_event(_hc.game_engine.OptionInstanceSessionEventArgs)
        child.opt_id = _hc.game_engine.ResourceId.from_str(child_ability)
        child.target_ids = [_hc.game_engine.ResourceId.from_str(target_template)]
        tgt = game._make_event(_hc.game_engine.TargetInstanceSessionEventArgs)
        tgt.target_index = 0
        tgt.target_id = _hc.game_engine.ResourceId.from_str(target_template)
        tgt.targets = hand
        child.target_instances = [tgt]
        child.min_target_counts = [1]
        child.max_target_counts = [1]
        opt.instances = [child]
        options.options = [opt]
        game._push(options)
        req = _hc.game_engine.AbilityActivationDataRequiredSessionEventArgs()
        req.player_id = pl_t
        req.ability_instance_id = 1
        req.ability_parent_id = 0
        req.source_card_id = source_card_id
        req.ability_template_id = _hc.game_engine.ResourceId.from_str(child_ability)
        req.effect_group_id = 1
        req.effect_instance_ids = [0]
        req.resolve_chain = False
        game._push(req)
        game.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
        bstate["pending_discard_ability"] = child_ability
        bstate["pending_discard_ability_instance_id"] = 1
        bstate["pending_discard_target_template"] = target_template
        bstate["pending_discard_scid"] = None
        _be.save_state(session, bstate)
        _hc.log_req("    Deathcry discard: class-23 prompt pushed (player discards)")

    def _resolve_champion_void_targets(self, game, session, pl_t, ai_t,
                                       bstate, ability_guid):
        """Void the troops a champion power chose via its gamedata m_VoidTarget
        (e.g. Bun'jitsu's "Void two ready troops you control") and remember the
        SUM of the voided troops' ATK/DEF so the summoned-token buff leaves can
        be data-driven ("+[ATK] equal to the voided troops' [ATK] plus 3")."""
        import hconnect_server as _hc
        import json as _j
        uids = bstate.get("champion_void_uids") or []
        if not uids:
            return
        raw_json = _hc.db_ability_raw_json(ability_guid, conn=_hc._db)
        if not raw_json:
            return
        try:
            rec = _j.loads(raw_json)
        except Exception:
            return
        vt = (rec.get("m_VoidTarget") or {})
        if not (vt.get("m_Guid") or ""):
            return
        voided = 0
        voided_uids = []
        stats = {"atk": 0, "def": 0}
        for uid in uids:
            row = _hc.db_card_combat_identity(
                session.session_id, int(uid), conn=_hc._db)
            if not row or row[1] != "warzone":
                continue
            # Bun'jitsu: "+[ATK] equal to the VOIDED TROOPS' [ATK] plus 3" —
            # with two voided troops the summoned token's buff sums BOTH
            # troops' attack/defense, then adds the fixed +3.
            stats["atk"] += int(row[3] or 0)
            stats["def"] += int(row[4] or 0)
            _hc.db_move_card_to_location(
                session.session_id, int(uid), "void", position=0, conn=_hc._db)
            _hc._db.commit()
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(uid)))
            tpl_guid = row[2]
            _tpl, ct, _n, _c, atk, def_, _g = self._card_full_data(
                game, scid, tpl_guid)
            owner = pl_t if (row[0] or 0) != 0 else ai_t
            game.push_card_updated(scid, owner,
                                   _hc.game_engine.ECardCollections.Void, ct,
                                   template_id=tpl_guid, attack=atk,
                                   defense=def_)
            voided += 1
            voided_uids.append(int(uid))
        if stats:
            bstate["champion_voided_stats"] = stats
        if voided_uids:
            bstate.setdefault("ability_lists", {})["VoidedCards"] = \
                list(voided_uids)
        _hc.log_req(f"    Champion void targets: {voided} voided "
                f"(stats={stats})")

    def _dispatch_blocker_events(self, game, session, bstate, runners):
        """Emit the authored block triggers for ``(attacker, blockers)`` pairs.

        ``Session.EmitBlockerEvents``/``AssignBlockers`` queue
        ``CardBlockedEvent`` and ``CardAttackedOrBlockedEvent`` with the blocker
        as their source AND the blocked attacker as their target, then one
        ``CardWasBlockedEvent`` for the attacker.  The target is what
        target-side abilities test ("When this becomes blocked", e.g. Nameless
        Citizen burying the top four cards of each opposing deck).  Only the
        AI's own blocking dispatched these, so a human block never fired the
        blocked attacker's authored abilities.
        """
        import hconnect_server as _hc
        pl_t, ai_t = game.player_uid, game.ai_uid
        for attacker_uid, blocker_uids in runners:
            try:
                attacker_uid = int(attacker_uid)
                blocker_values = [int(value) for value in blocker_uids or ()]
            except (TypeError, ValueError):
                continue
            if not blocker_values:
                continue
            for blocker_uid in blocker_values:
                blocker_owner = _hc.db_card_owner_id(
                    session.session_id, blocker_uid, conn=_hc._db)
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate, "CardBlockedEvent",
                    blocker_uid, blocker_owner, target_card_id=attacker_uid)
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate,
                    "CardAttackedOrBlockedEvent", blocker_uid, blocker_owner,
                    target_card_id=attacker_uid)
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate, "CardWasBlockedEvent",
                attacker_uid,
                _hc.db_card_owner_id(session.session_id, attacker_uid, conn=_hc._db))
