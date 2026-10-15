"""Chain and stack resolution, including the game-started chain and rules-port chain completion."""


class ChainMixin:
    """Methods of HCPHandler, mixed into it (see hconnect_server)."""

    def _completed_rules_port_chain(self, session, bstate):
        """Find the native chain item released by a completed continuation."""
        try:
            instance_id = int(
                (bstate or {}).get("completed_chain_instance_id") or 0)
        except (TypeError, ValueError):
            return None
        port = getattr(session, "_rules_port_session", None)
        if port is None:
            return None
        chain = getattr(port, "chain", None)
        if instance_id <= 0 or chain is None:
            return None
        if chain.peek_ability(instance_id) is None:
            return None
        return port, instance_id

    def _resume_completed_rules_port_chain(self, session, bstate,
                                           completed_chain=None):
        """Finish a chain item whose picker continuation just completed.

        The continuation has already applied the authored effects and stored
        ``completed_chain_instance_id``. Resume the existing native resolver
        now so it can consume that marker, finish the card's zone projection,
        and publish the client-facing chain removal sequence exactly once.
        """
        import hconnect_server as _hc
        completed_chain = (completed_chain or
                           self._completed_rules_port_chain(session, bstate))
        if completed_chain is None:
            return False
        port, instance_id = completed_chain
        from rules_port.actions import AbilityResolutionState
        result = port.resolve_top_of_chain(instance_id)
        if result is not AbilityResolutionState.COMPLETED:
            _hc.log_req("    RulesPort completed choice did not finish chain item "
                    f"instance={instance_id} state={result!r}")
        return result is AbilityResolutionState.COMPLETED

    def _priority_context_for(self, phase, bstate=None):
        """Pick the EPriorityContext describing where priority goes when the
        player passes the given phase — drives the client's Pass button label
        (e.g. "Proceed to Next Main Phase" / "Proceed to End Turn"). Mirrors
        the client's PriorityWindowAction.PostUpdate choice for the active player.

        Pass `bstate` so the FirstMainPhase case can pick the correct label
        (ProcedeToCombat when the player can declare an attack, otherwise
        ProcedeToSecondMain) using the already-computed
        player_has_ready_troop flag — the server is authoritative, so it must
        choose the same context the client's PostUpdate would.
        """
        import hconnect_server as _hc
        # A non-empty chain: the player's pass button becomes "Resolve <CardName>"
        # (EPriorityContext.ResolveTopOfChain), mirroring the client's
        # BattleStateBase.RebuildPassButton.
        if bstate is not None and (bstate.get("stack") or []):
            return _hc.game_engine.EPriorityContext.ResolveTopOfChain
        if phase == _hc.game_engine.ETurnPhases.FirstMainPhase:
            if bstate is None or bstate.get("player_has_ready_troop"):
                return _hc.game_engine.EPriorityContext.ProcedeToCombat
            return _hc.game_engine.EPriorityContext.ProcedeToSecondMain
        if phase == _hc.game_engine.ETurnPhases.SecondMainPhase:
            return _hc.game_engine.EPriorityContext.ProceedToEndTurn
        if phase == _hc.game_engine.ETurnPhases.DeclareCombatPriorityWindow:
            return _hc.game_engine.EPriorityContext.ProcedeToCombat
        if phase == _hc.game_engine.ETurnPhases.DeclareAttackPriorityWindow:
            return _hc.game_engine.EPriorityContext.ProcedeToBlockers
        if phase == _hc.game_engine.ETurnPhases.DeclareDefensePriorityWindow:
            return _hc.game_engine.EPriorityContext.ResolveCombat
        if phase == _hc.game_engine.ETurnPhases.Ready:
            return _hc.game_engine.EPriorityContext.Ready
        if phase == _hc.game_engine.ETurnPhases.EndPhase:
            return _hc.game_engine.EPriorityContext.EndPhase
        return _hc.game_engine.EPriorityContext.Normal

    def _push_transaction_ack(self, session):
        """Send an empty 3055 sync packet to the client.

        The client's SessionClient.SubmitTransaction sends only ONE transaction
        at a time and silently DROPS any further transaction while
        m_HasPreviousTransactionBeenRespondedByServer is false
        (Game/Client/SessionClient.cs:45). That flag is reset to true ONLY when
        a NetworkPacketSessionEventArgs (class 255 — the top level of every 3055
        packet) arrives (ClientSessionBase.cs:30). For 3029 transactions the
        server handles WITHOUT pushing 3055 events (SetTurnPhases, stale or
        no-op passes), we must still send an empty sync packet or the next
        transaction — including the Withdraw (QuitGameTransaction) — is silently
        dropped client-side and "nothing happens".
        """
        import hconnect_server as _hc
        try:
            pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
            game = _hc.game_engine.Game(session.session_id, pl_t, _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000))
            pkt = game.make_network_packet(pl_t)  # no events -> empty packet
            dw = _hc.encode_datawrapper(0, 3055, _hc.compress_gzip(_hc.encode_sync_event(pkt)), 1,
                                    "00000000-0000-0000-0000-000000000000")
            self._game_scnt = max(self._game_scnt, self.scnt) + 1
            self.scnt = self._game_scnt
            gs_inst = str(session.server_id)
            self.send({
                "issuer": f"0.0.0.0.ServiceGameSession.{_hc.SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                "target": "ServiceGameSession", "instance": gs_inst, "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
            }, dw)
            self._event_q.append((self.scnt, dw, {}))
            _hc.log_req(f"    Pushed transaction ack (empty 3055, {len(dw)}b)")
        except Exception as e:
            _hc.log_req(f"    Transaction ack failed: {e}")

    def _autopass_game_started_chain(self, session, pl_t, ai_t, bstate,
                                     game):
        """Resolve the mandatory chain created by ``GameStartedEvent``.

        Opening setup must not expose a ResolveTopOfChain pass button.  Use
        the same stack resolver as an ordinary pass, so nested triggers and
        generated cards retain normal ordering and event serialization.  If a
        setup ability genuinely needs player input, leave its pending state in
        place and let the regular prompt path take over.
        """
        import hconnect_server as _hc
        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            # Setup triggers are already registered with the native action
            # stack.  Auto-pass their native priority window so opening-game
            # resolution cannot select or pop an item from the compatibility
            # stack.  The latter remains only a durable event projection.
            if port.chain.is_empty:
                port.rehydrate_projected_chain()
            if not port.chain.is_empty:
                resolved = 0
                for _ in range(256):
                    action = port.action_stack.peek()
                    if action is None:
                        break
                    from rules_port.kernel import PriorityWindowAction
                    if isinstance(action, PriorityWindowAction):
                        if not port.auto_pass_internal_priority(port.player_ids):
                            break
                    elif not port.tick():
                        break
                    resolved += 1
                    if port.chain.is_empty:
                        break
                port.persist()
                if resolved:
                    _hc.log_req(f"    Auto-passed {resolved} native GameStarted "
                            "chain action(s)")
                return resolved
            if bstate.get("stack"):
                # A native session must not silently downgrade setup to the
                # legacy resolver if native rehydration is incomplete.
                raise RuntimeError(
                    "RulesPort GameStarted chain has no native action")
            return 0

        _be = self._checkpoint_engine(session)

        resolved = 0
        had_chain = not _be.stack_empty(bstate)
        # A malformed/cyclic setup effect must not hang the game-start
        # transaction forever.  Normal authored setup chains are much smaller
        # than this, including generated follow-up triggers.
        max_items = 256
        while (not _be.stack_empty(bstate) and
               resolved < max_items):
            if (bstate.get("pending_choice") or
                    bstate.get("pending_deck_search") or
                    bstate.get("pending_trigger") or
                    bstate.get("pending_discard_ability") or
                    bstate.get("pending_conversation")):
                break
            item = _be.stack_pop(bstate)
            _be.stack_reset_passes(bstate)
            self._resolve_stack_item(
                session, pl_t, ai_t, bstate, item, game)
            resolved += 1

        if resolved >= max_items and not _be.stack_empty(bstate):
            _hc.log_req("    GameStarted chain exceeded 256 items; leaving "
                    "remaining items for normal priority")
        elif resolved:
            _hc.log_req(f"    Auto-passed {resolved} GameStarted chain item(s)")

        if (had_chain and _be.stack_empty(bstate) and
                not bstate.get("pending_choice") and
                not bstate.get("pending_deck_search") and
                not bstate.get("pending_trigger") and
                not bstate.get("pending_discard_ability") and
                not bstate.get("pending_conversation")):
            game.push_chain_empty()
        return resolved

    def _suppress_completed_game_started_chain_events(self, game,
                                                       event_start):
        """Hide a fully auto-passed setup chain from the client animation UI.

        GameStarted abilities are resolved before their initial network packet
        is sent.  Sending their push/pop pair in that same packet makes the
        client move a source champion into the chain while it is still
        building the opening board; the remove animation can then be applied
        before the corresponding chain card is registered and leave the
        champion displayed at the bottom of the chain.  The ability's effect
        events remain in the packet; only the transient chain presentation is
        removed.

        ``event_start`` scopes this to events created by GameStarted setup and
        preserves any earlier events in the transaction.
        """
        import hconnect_server as _hc
        chain_event_types = (
            _hc.game_engine.AbilityPushedOnChainSessionEventArgs,
            _hc.game_engine.TopOfChainResolvedSessionEventArgs,
            _hc.game_engine.RemovedTopOfChainSessionEventArgs,
            _hc.game_engine.ChainEmptySessionEventArgs,
        )
        prefix = game.events[:event_start]
        setup_events = game.events[event_start:]
        game.events = prefix + [
            event for event in setup_events
            if not isinstance(event, chain_event_types)
        ]

    # --- rules_port.chain_items host hooks (Practice/PvE projection) -------
    def chain_load(self, session):
        from rules_port import lifecycle
        return lifecycle.load_state(session) or {}

    def chain_save(self, session, state):
        from rules_port import lifecycle
        lifecycle.save_state(session, state)

    def chain_new_game(self, session, state, player_uid, ai_uid):
        return self._fresh_game(session, player_uid, ai_uid, state)

    def chain_send(self, session, game, player_uid, ai_uid):
        self._send_battle_events(session, game, player_uid)

    def chain_resume_continuation(self, session, state, game, player_uid,
                                  ai_uid, continuation):
        import hconnect_server as _hc
        from rules_port.resolution import resume_ability_continuation_parents
        return resume_ability_continuation_parents(
            self, game, session, _hc._db, player_uid, ai_uid, state,
            continuation)

    def chain_card_data(self, game, scid, template_guid):
        return self._card_full_data(game, scid, template_guid, None)

    def chain_dispatch(self, session, game, state, player_uid, ai_uid,
                       event_type, source_uid, owner_id, **event_data):
        return self._dispatch_native_trigger(
            game, session, player_uid, ai_uid, state, event_type, source_uid,
            owner_id, **event_data)

    def chain_state_based(self, session, state, game, player_uid, ai_uid):
        """Apply lethal state-based actions before chain priority returns."""
        import hconnect_server as _hc
        from rules_port.context import EffectContext
        from rules_port.death_effects import state_based_deaths
        state["_rules_port_attached"] = True
        previous = getattr(self, "_current_bstate", None)
        self._current_bstate = state
        try:
            return bool(state_based_deaths(EffectContext.from_rules_port(
                game, session, _hc._db, self, player_uid, ai_uid, state,
                "state_based_death", ability=None)))
        finally:
            if previous is None:
                try:
                    del self._current_bstate
                except AttributeError:
                    pass
            else:
                self._current_bstate = previous

    def chain_mark_warzone_entry(self, session, source_uid):
        """Permanent bookkeeping for a troop/artifact reaching the warzone."""
        import hconnect_server as _hc
        _hc.db_card_set_warzone_arrival(session.session_id, source_uid)
        _hc.db_set_card_resolved_at(
            session.session_id, source_uid,
            self._next_resolve_counter(session))

    def chain_push_empty(self, port, state, item, pending):
        if pending or state.get("stack"):
            return False
        # The compatibility stack empties after each projected item, but
        # RulesPort may still own further native chain items (for example
        # Nerissa's two troop summons followed by the champion ability).
        # Only the final native item empties the client's chain.
        native_chain = getattr(port, "chain", None)
        return len(getattr(native_chain, "_instance_ids", ())) <= 1

    def chain_finalize(self, session, state, game, item, pending,
                       player_uid, ai_uid):
        """Return main-phase priority with the completed chain packet.

        Practice/PvE has one client, so its last chain item must publish the
        active human's GreenLight alongside the chain removal. Tournament PvP
        uses its own two-player priority projection hook.
        """
        import hconnect_server as _hc
        if pending or state.get("stack"):
            return
        port = getattr(session, "_rules_port_session", None)
        native_chain = getattr(port, "chain", None)
        if len(getattr(native_chain, "_instance_ids", ())) > 1:
            return
        active_player = getattr(port, "active_player_id", None)
        if active_player is None or not _hc._same_uid(active_player, player_uid):
            return
        game.push_green_light(
            player_uid, _hc.game_engine.EPriorityContext.Normal)

    def chain_after_item(self, session, state, game, item, player_uid, ai_uid):
        """A resolved spell/ability/trigger can deal the last health."""
        self._check_champion_health(session, player_uid, ai_uid, state)

    def chain_ability_owner(self, state, item, owner_id, player_uid, ai_uid):
        """Normalize a manual/champion ability item's owner for this mode.

        Native PVE descriptors use typed ServicePlayer/AI identities for
        scheduler ownership.  Generated cards and deck mutations require the
        persisted profile owner (player) or the canonical AI owner (0).
        """
        import hconnect_server as _hc
        if state.get("pvp"):
            return owner_id
        player_owner = int((self.user_profile or {}).get("id", 0)) \
            if isinstance(self.user_profile, dict) else 0
        if _hc._same_uid(owner_id, player_uid) or owner_id == player_owner:
            return player_owner
        if _hc._same_uid(owner_id, ai_uid) or owner_id == 0:
            return 0
        return owner_id

    def _resolve_stack_item(self, session, pl_t, ai_t, bstate, item, game):
        """Execute the top of the chain (`item`, already popped) and push the
        resolve chain events onto `game` (TopOfChainResolved + RemovedTopOfChain).

        Item kinds:
          - troop:  CastSpells -> Warzone (came out this turn, summoning sick).
          - trigger: a Deathcry (ability.resolve_stack_trigger).
          - spell:   execute the typed spell graph then CastSpells -> Discard.
          - ability: a champion ability — resolve its typed graph + class-23
                     discard prompt when authored metadata requires one.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        kind = item.get("kind")
        instance_id = int(item.get("instance_id", 1))
        game.push_top_of_chain_resolved(instance_id)
        game.push_removed_top_of_chain(instance_id)
        if kind == "troop":
            uid = int(item.get("source_uid") or 0)
            if uid:
                loc = _hc.db_card_location(session.session_id, uid, conn=_hc._db)
                if loc != "CastSpells":
                    _hc.log_req(f"    Troop {uid} already left the chain "
                            f"(loc={loc}) — skipped")
                    _be.save_state(session, bstate)
                    return game
                # Permanent cast abilities resolve before the card enters
                # Warzone. A cast graph may grant a Deploy ability, which
                # must be present when CardEnteredZoneEvent is dispatched.
                from rules_port.resolution import \
                    resolve_port_played_permanent
                owner_id = int(_hc.db_card_owner_id(
                    session.session_id, uid, conn=_hc._db) or 0)
                resolve_port_played_permanent(
                    game, session, _hc._db, self, pl_t, ai_t, bstate, item,
                    owner_id)
                if bstate.get("resolution_paused"):
                    _be.save_state(session, bstate)
                    return game
                scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(uid))
                _hc.db_card_set_warzone_arrival(session.session_id, uid)
                _hc.db_set_card_resolved_at(session.session_id, uid, self._next_resolve_counter(session))
                crow2 = _hc.db_card_with_template(session.session_id, uid)
                if crow2:
                    owner_id = _hc.db_card_owner_id(
                        session.session_id, uid, conn=_hc._db) or 0
                    owner_sid = pl_t if (owner_id or 0) != 0 else ai_t
                    # Populate the CardDef (cost/atk/def/thresholds/abilities)
                    # so the re-pushed CardUpdated renders the full card — without
                    # it a troop played mid-game (e.g. via a spell) shows zeros.
                    self._card_full_data(game, scid, crow2[0], None)
                    game.push_card_updated(scid, owner_sid, _hc.game_engine.ECardCollections.Warzone,
                                           _hc.game_engine.card_type_from_db(crow2[1]),
                                           template_id=crow2[0])
                    game.push_troop_card_played(scid, owner_sid)
                    # A troop resolving to the warzone from the stack fires its
                    # own CardEnteredZone triggers plus those of other warzone
                    # troops under the same controller (Adamanthian Scrivener).
                    self._dispatch_native_trigger(
                        game, session, pl_t, ai_t, bstate,
                        "CardEnteredZoneEvent", uid, owner_id,
                        event_source_collection="CastSpells",
                        event_destination_collection="warzone")
                    # Normal plays publish CardCastEvent when they enter the
                    # chain. Keep a fallback for legacy/effect-created chain
                    # items, using the same champion-source/card-target shape.
                    if not item.get("card_cast_event_dispatched"):
                        from rules_port.chain_items import dispatch_card_cast
                        dispatch_card_cast(
                            self, session, game, bstate, pl_t, ai_t, uid,
                            owner_id)
        elif kind == "trigger":
            from rules_port.resolution import resolve_port_trigger
            resolve_port_trigger(self, game, session, _hc._db, pl_t, ai_t,
                                 bstate, item)
        elif kind == "spell":
            uid = int(item.get("source_uid") or 0)
            # The spell must still be on the chain (CastSpells) to resolve.  A
            # Countermagic/interrupt already moved it to the graveyard while it
            # waited for the stack — skip its BOM so a countered spell never
            # also draws/buffs/damages (the client's interrupted card fizzles).
            loc_details = _hc.db_card_owner_zone_state(
                session.session_id, uid, conn=_hc._db)
            loc = loc_details[1] if loc_details else "discard"
            if loc != "CastSpells":
                _hc.log_req(f"    Spell {uid} already left the chain "
                        f"(loc={loc}) — countered/interrupted, BOM skipped")
                _be.save_state(session, bstate)
                return game
            # Track actions cast this turn (for ChampionActionsCastThisTurn).
            turn_now = int(bstate.get("turn_number", 1))
            cast_side = "ai" if (loc_details[0] or 0) == 0 else "player"
            cast_turn_key = f"{cast_side}_actions_cast_turn"
            cast_count_key = f"{cast_side}_actions_cast_this_turn"
            if bstate.get(cast_turn_key) != turn_now:
                bstate[cast_count_key] = 0
                bstate[cast_turn_key] = turn_now
            bstate[cast_count_key] = int(bstate.get(cast_count_key, 0)) + 1
            bstate["player_spell_target"] = item.get("target_uid")
            bstate["resolving_source_uid"] = item.get("source_uid")
            # The caster (0 = AI) decides which champion "you"/"opposing"
            # effects target; never assume the human cast the spell.
            bstate["resolving_owner_id"] = (
                loc_details[0] if loc_details else self.user_profile["id"])
            bstate["x_cost"] = int(item.get("x_cost") or 0)
            from rules_port.resolution import resolve_port_played_spell
            resolve_port_played_spell(
                game, session, _hc._db, self, pl_t, ai_t, bstate,
                item.get("ability_guids", []),
                activations=item.get("activations"),
                played_from_hand=bool(item.get("played_from_hand")),
                voided_cost_uids=item.get("voided_cost_uids", ()))
            # A reveal/deck-search or revealed-card selection is a real client
            # continuation.  Do not move the spell to the graveyard or emit
            # ChainEmpty until that continuation has supplied its target; the
            # previous code discarded Oakhenge immediately and left the
            # reveal packet detached from the waiting UI.
            if bstate.get("resolution_paused"):
                _be.save_state(session, bstate)
                return game
            # "When you play an action/... " triggers (e.g. Chimes of the
            # Zodiac's "copy it") fire against the played spell.
            if (item.get("source_uid") and
                    not item.get("card_cast_event_dispatched")):
                # Keep the event source/target identities consistent for AI
                # and player casts. Champion triggers such as Rowdy inspect
                # the cast card through TriggerTarget.
                from rules_port.chain_items import dispatch_card_cast
                cast_owner_id = int(loc_details[0] or 0) if loc_details else 0
                dispatch_card_cast(
                    self, session, game, bstate, pl_t, ai_t,
                    int(item["source_uid"]), cast_owner_id)
            bstate.pop("player_spell_target", None)
            bstate.pop("resolving_source_uid", None)
            bstate.pop("x_cost", None)
            if uid:
                scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(uid))
                # Re-read the card's zone AFTER its BOM resolved: a leaf may
                # have moved it into the deck (Eternal Youth / Chronic Madness
                # "Put this into your deck") — only a spell still in CastSpells
                # goes to the graveyard.
                loc = _hc.db_card_location(session.session_id, uid, conn=_hc._db) or "discard"
                if loc != "deck":
                    # Default: the spent spell goes to the graveyard. A spell
                    # that moved itself into the deck (Eternal Youth's
                    # Escalation "PutThisIntoYourDeck") was already pushed by
                    # the leaf — skip the discard.
                    _hc.db_card_discard_spell(session.session_id, uid)
                    # The spell entered the crypt — "when a card enters an
                    # opposing crypt" triggers (Incantation of Fear) fire here.
                    self._dispatch_native_trigger(
                        game, session, pl_t, ai_t, bstate,
                        "CardEnteredZoneEvent", uid,
                        (loc_details[0] if loc_details else 0),
                        event_source_collection="CastSpells",
                        event_destination_collection="discard")
                    crow2 = _hc.db_card_with_template(session.session_id, uid)
                    if crow2:
                        card_owner = (loc_details[0] if loc_details else 0)
                        owner_sid = pl_t if (card_owner or 0) != 0 else ai_t
                        # Populate the CardDef so the graveyard card renders its
                        # full data (cost/atk/def/thresholds/abilities) — a bare
                        # push_card_updated leaves the grave card blank.
                        self._card_full_data(game, scid, crow2[0], None)
                        game.push_card_updated(scid, owner_sid, _hc.game_engine.ECardCollections.Discard,
                                               _hc.game_engine.card_type_from_db(crow2[1]),
                                               template_id=crow2[0])
        elif kind == "ability":
            ag = item.get("ability_guid", "")
            if (str(ag).lower() ==
                    "f2d6797b-1a24-4c3d-9239-a27a2e0de0ff"):
                from rules_port.context import EffectContext
                from rules_port.tunneling import resolve_surface
                result = resolve_surface(EffectContext.from_rules_port(
                    game, session, _hc._db, self, pl_t, ai_t, bstate, str(ag),
                    ability=None), item.get("source_uid"))
                _hc.log_req(f"    Tunneling Surface {item.get('source_uid')}: "
                        f"{result}")
                ag = ""
            if ag:
                # The activation's chosen target (e.g. Dimmid's Lifedrain troop)
                # must reach the BOM leaves.
                bstate["player_mod_target"] = item.get("target_uid")
                bstate["player_spell_target"] = item.get("target_uid")
                bstate["resolving_ability"] = str(ag)
                # Champion ability context: the source is the champion card and
                # its owner decides which side heals/takes damage (e.g.
                # Dimmid's Lifedrain).  Leaves need resolving_source_uid +
                # resolving_owner_id even though the champion is not a
                # game_cards row.
                src_uid = item.get("source_uid")
                bstate["resolving_source_uid"] = src_uid
                p_scid = getattr(self, "_player_champ_scid", None)
                a_scid = getattr(self, "_ai_champ_scid", None)
                if p_scid is not None and src_uid is not None \
                        and int(src_uid) == int(p_scid.uid.uid64):
                    bstate["resolving_owner_id"] = (
                        self.user_profile["id"] if self.user_profile else 0)
                elif a_scid is not None and src_uid is not None \
                        and int(src_uid) == int(a_scid.uid.uid64):
                    bstate["resolving_owner_id"] = 0
                else:
                    source_owner = _hc.db_card_owner_id(
                        session.session_id, int(src_uid or 0), conn=_hc._db)
                    bstate["resolving_owner_id"] = (
                        int(source_owner) if source_owner is not None else
                        (self.user_profile["id"] if self.user_profile else 0))
                # Multi-target champion powers with an m_VoidTarget (e.g.
                # Bun'jitsu's "Void two ready troops you control") void the
                # chosen troops now and remember their stats so the summoned
                # token's buffs ("+[ATK] equal to the voided troop's [ATK] plus
                # 3") are data-driven.
                ability_event_start = len(game.events)
                ability_player_health_before = int(
                    bstate.get("player_health", game.player_health))
                ability_ai_health_before = int(
                    bstate.get("ai_health", game.ai_health))
                self._resolve_champion_void_targets(
                    game, session, pl_t, ai_t, bstate, str(ag))
                from rules_port.resolution import resolve_port_ability
                target_map = {}
                if item.get("target_uid") is not None:
                    # Champion powers often contain duplicate target-template
                    # entries: the effect mapping may refer to index 1 even
                    # though the activation payload carries the single chosen
                    # target at index 0. Preserve that explicit choice for
                    # every equivalent authored target slot; otherwise the
                    # native resolver falls back to the first legal robot.
                    target_uid = int(item["target_uid"])
                    try:
                        from gamedata import DEFAULT_RECORD_STORE, ability_graph
                        graph = ability_graph(DEFAULT_RECORD_STORE, str(ag).lower())
                        targets = list(getattr(graph, "targets", ()) or ())
                        first_guid = (getattr(targets[0], "guid", None)
                                      if targets else None)
                        for index, spec in enumerate(targets):
                            if index == 0 or getattr(spec, "guid", None) == first_guid:
                                target_map[index] = target_uid
                    except Exception:
                        target_map = {0: target_uid}
                ability_log = resolve_port_ability(
                    self, game, session, _hc._db, pl_t, ai_t, bstate, ag,
                    source_uid=bstate.get("resolving_source_uid"),
                    owner_id=bstate.get("resolving_owner_id", 0),
                    target_map=target_map)
                # Damage/heal leaves normally emit class 38.  Add a fallback
                # for ability implementations that update the battle state
                # directly, so the champion HUD changes immediately rather
                # than waiting for a later phase refresh.
                ability_player_health_after = int(
                    bstate.get("player_health", game.player_health))
                ability_ai_health_after = int(
                    bstate.get("ai_health", game.ai_health))
                game.player_health = ability_player_health_after
                game.ai_health = ability_ai_health_after
                game.push_champion_health_changed_if_missing(
                    pl_t, ability_player_health_before,
                    ability_player_health_after, since=ability_event_start)
                game.push_champion_health_changed_if_missing(
                    ai_t, ability_ai_health_before,
                    ability_ai_health_after, since=ability_event_start)
                _hc.log_req(f"    Champion ability BOM {str(ag)[:8]}: "
                        f"{ability_log} health="
                        f"{ability_player_health_before}->{ability_player_health_after}/"
                        f"{ability_ai_health_before}->{ability_ai_health_after}")
                if self._bom_has_discard(
                        ag, native=bool(bstate.get("_rules_port_attached"))):
                    self._push_discard_prompt(
                        game, session, pl_t, ai_t, bstate, str(ag))
        # Clear transient resolution targets so a later trigger/ability cannot
        # pick up a stale target (e.g. Solitary Exile's Deploy voiding the
        # wrong card because a previous champion power left player_mod_target).
        bstate.pop("player_spell_target", None)
        bstate.pop("player_mod_target", None)
        bstate.pop("resolving_owner_id", None)
        bstate.pop("resolving_ability", None)
        bstate.pop("champion_void_uids", None)
        bstate.pop("champion_voided_stats", None)
        bstate.pop("created_token_uids", None)
        if kind in ("troop", "spell"):
            from rules_port.resolution import \
                restore_played_card_ability_lists
            restore_played_card_ability_lists(
                bstate, item.get("source_uid"))
        if isinstance(bstate.get("ability_lists"), dict):
            bstate["ability_lists"].pop("VoidedCards", None)
        _be.save_state(session, bstate)
        # Refresh both players' HUD (health/resources) after the resolution —
        # a heal from a trigger (e.g. Adamanthian Scrivener) otherwise only
        # shows on the next PlayerUpdated.
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        # State-based effects: when the stack is now empty, any troop whose
        # effective defense (base + mod) is 0 or less dies (Immortal included).
        if _be.stack_empty(bstate):
            from rules_port.context import EffectContext
            from rules_port.death_effects import state_based_deaths
            state_based_deaths(EffectContext.from_rules_port(
                game, session, _hc._db, self, pl_t, ai_t, bstate,
                "state_based_death", ability=None))
            if _be.stack_empty(bstate):
                surface_owner = (self.user_profile["id"]
                                 if bstate.get("turn_player") == _be.PLAYER
                                 else 0)
                if bstate.get("_rules_port_attached"):
                    from rules_port.context import EffectContext
                    from rules_port.tunneling import queue_surfaces
                    queue_surfaces(EffectContext.from_rules_port(
                        game, session, _hc._db, self, pl_t, ai_t, bstate,
                        "", ability=None), surface_owner)
                else:
                    from abilities.framework.effects.counters import queue_tunneling_surfaces
                    queue_tunneling_surfaces(
                        _hc._db, session, self, game, pl_t, ai_t, bstate,
                        surface_owner)
        # Champion state check: a spell/trigger that brought a champion to 0
        # health ends the game right here.
        self._check_champion_health(session, pl_t, ai_t, bstate)
        return game

    def _push_rules_port_chain_priority_sync(self, session, port):
        """Restore native chain priority after the client requests a resync.

        The legacy checkpoint can still describe a main phase while RulesPort
        owns a pending chain item. Replaying its ordinary phase GreenLight
        replaces the client's ResolveTopOfChain button context.
        """
        import hconnect_server as _hc
        chain = getattr(port, "chain", None)
        if chain is None or chain.is_empty:
            return None

        priority = getattr(
            getattr(port, "action_stack", None), "priority_player_id", None)
        if priority is None:
            _hc.log_req("    RequestPrioritySync: native chain has no priority owner")
            return False

        if (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import (
                _pvp_populate_game_state, _send_pvp_packet,
                player_handlers, pvp_load_state,
            )
            state = pvp_load_state(session) or {}
            pids = [int(value) for value in (state.get("pids") or ())]
            my_pid = int(self.client_reck_id)
            if len(pids) != 2 or my_pid not in pids:
                _hc.log_req(
                    "    RequestPrioritySync: no PvP participants for native chain")
                return False
            priority_pid = next((pid for pid in pids if _hc._same_uid(
                priority, _hc.game_engine.UID.make(244, pid))), None)
            if priority_pid is None:
                _hc.log_req(
                    "    RequestPrioritySync: unknown PvP native chain owner")
                return False

            other_pid = next(pid for pid in pids if pid != my_pid)
            target_uid = _hc.game_engine.UID.make(244, my_pid)
            other_uid = _hc.game_engine.UID.make(244, other_pid)
            priority_uid = _hc.game_engine.UID.make(244, priority_pid)
            turn_pid = int(state.get("turn_pid") or priority_pid)
            turn_uid = _hc.game_engine.UID.make(244, turn_pid)
            phase = port.current_turn_phase
            window = _hc.game_engine.Game(
                int(session.session_id), target_uid, other_uid)
            _pvp_populate_game_state(window, state, my_pid, other_pid)
            window.push_green_light(
                priority_uid,
                _hc.game_engine.EPriorityContext.ResolveTopOfChain)
            window.push_turn_phase(phase, turn_uid, priority_uid)
            target_handler = player_handlers.get(my_pid) or self
            _send_pvp_packet(
                target_handler, session, window, target_uid,
                "rules-port-chain-priority-sync")
            _hc.log_req(
                "    RequestPrioritySync: restored PvP native chain window "
                f"priority={priority_pid} phase={phase}")
            return True

        player_uid = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_uid = _hc.game_engine.UID.make(
            _hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        priority_uid = (player_uid if _hc._same_uid(priority, player_uid)
                        else ai_uid if _hc._same_uid(priority, ai_uid) else None)
        if priority_uid is None:
            _hc.log_req(
                "    RequestPrioritySync: unknown Practice native chain owner")
            return False
        checkpoint = self._checkpoint_engine(session).load_state(session)
        self._current_bstate = checkpoint
        game = self._fresh_game(session, player_uid, ai_uid, checkpoint)
        active_owner = getattr(port, "active_player_id", None)
        active_uid = (player_uid if _hc._same_uid(active_owner, player_uid)
                      else ai_uid if _hc._same_uid(active_owner, ai_uid)
                      else player_uid)
        game.push_green_light(
            priority_uid, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
        game.push_turn_phase(port.current_turn_phase, active_uid, priority_uid)
        if _hc._same_uid(priority_uid, player_uid):
            # The human is the response player; restore the legal quick actions
            # and champion powers alongside the ResolveTopOfChain button.  They
            # go after the green light in the same packet: sent before it, the
            # client dropped them when the chain window opened.
            self._push_phase_options_empty(
                session, player_uid, ai_uid, game=game, send=False,
                refresh_warzone=False)
        self._send_battle_events(session, game, player_uid)
        _hc.log_req(
            "    RequestPrioritySync: restored Practice native chain window "
            f"priority={priority_uid!r} phase={port.current_turn_phase}")
        return True
