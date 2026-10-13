"""Prompts for triggers, deck search, revealed cards and conversations, and the resolution of the answers."""

from collections.abc import Mapping
from typing import Any
from typing import cast
import json
import random
import re
import struct


class PendingChoicesMixin:
    """Methods of HCPHandler, mixed into it (see hconnect_server)."""

    def _resolve_shards_of_fate(self, game, session, pl_t, ai_t, bstate,
                                played_card_uid, ability_guid, shard_tpl,
                                owner_id):
        """Shards of Fate: the controller chooses a Standard resource in their
        deck and gains its threshold.  A HUMAN gets the class-39 choosing
        prompt (the shard stays in the deck); the AI auto-picks a random one."""
        import hconnect_server as _hc
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        from rules_port.targeting import legal_targets as _lt
        candidates = _lt(_hc._db, session.session_id, owner_id, shard_tpl,
                         int(played_card_uid or 0), both_players=False,
                         champions=[])
        candidates = [int(c) for c in candidates]
        if not candidates:
            _hc.log_req(f"    Shards of Fate: no Standard resource left in deck "
                    f"(owner {owner_id})")
            return "shards of fate: no standard resource in deck"
        if owner_id != 0:
            # Do not expose the deck's current shard order in the picker.
            random.shuffle(candidates)
            return self._prompt_deck_search(
                game, session, pl_t, ai_t, bstate, ability_guid,
                int(played_card_uid or 0), owner_id, candidates, kind="shard")
        # AI: random Standard resource from its deck -> gain its threshold.
        import random as _rnd
        chosen = _rnd.choice(candidates)
        from rules_port.resources import (
            apply_resource_change, card_template_threshold_flags)
        provided = card_template_threshold_flags(
            _hc._db, session.session_id, int(chosen))
        threshold_colors = sorted({
            int(flag) for flag in _hc.game_engine.SHARD_TO_FLAG.values()
            if provided & int(flag)})
        if threshold_colors:
            th = bstate.setdefault("ai_threshold", {})
            changes = []
            for flag in threshold_colors:
                changes.append(apply_resource_change(
                    bstate, "ai", "threshold", 1, color=flag))
            _be.save_state(session, bstate)
            game.ai_threshold = dict(th)
            for flag, change in zip(threshold_colors, changes):
                ev_th = _hc.game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                ev_th.player_id = ai_t
                ev_th.color = flag
                ev_th.operation = 1
                ev_th.delta = 1
                ev_th.new_value = change.new_value
                game._push(ev_th)
                from rules_port.triggers import dispatch_native_trigger
                champion = getattr(self, "_ai_champ_scid", None)
                if champion is not None:
                    dispatch_native_trigger(
                        db=_hc._db, handler=self, game=game, session=session,
                        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                        event_type="GainThresholdEvent",
                        source_card_id=int(champion.uid.uid64),
                        source_player_id=owner_id,
                        data={"gain_threshold_color": int(flag)})
            game.push_player_updated(
                ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        _hc.log_req(f"    Shards of Fate (AI): gained threshold flags "
                f"{threshold_colors}")
        return f"shards of fate: AI gained {threshold_colors} threshold flags"

    def _prompt_optional_trigger(self, game, pl_t, ai_t, session, bstate,
                                 source_uid, ability_guid, target_template_ids,
                                 candidates, owner_id=None,
                                 trigger_target_uid=None):
        """Prompt a human controller for an authored optional trigger.

        Optional triggers use the same class-39 continuation as triggered
        target choices.  Keeping the packet construction in one path is
        important: an optional trigger may also have an explicit target, in
        which case the client first asks whether to opt in and then opens the
        authored target picker.
        """
        return self._prompt_trigger_targets(
            game, pl_t, ai_t, session, bstate, source_uid, ability_guid,
            target_template_ids, candidates, owner_id=owner_id,
            trigger_target_uid=trigger_target_uid, optional=True)

    def _prompt_trigger_targets(self, game, pl_t, ai_t, session, bstate,
                                source_uid, ability_guid, target_template_ids,
                                candidates, owner_id=None,
                                trigger_target_uid=None, optional=False):
        """Prompt for an optional trigger and/or its explicit targets.

        Mirrors the client's WaitForTriggeredAbilitiesAction: a
        PlayerOptionList carrying the legal candidates (when authored target
        input exists) plus the class-39
        TriggeredAbilityActivationDataRequired event.  The follow-up
        SetAbilityActivationDataTransaction resolves the trigger, or cancels
        it when an optional ability is declined.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        inst_id = int(bstate.get("_next_instance_id", 1))
        bstate["_next_instance_id"] = inst_id + 1
        prompt_owner_id = owner_id
        if prompt_owner_id is None:
            prompt_owner_id = bstate.get("resolving_owner_id")
        if prompt_owner_id is None:
            prompt_owner_id = _hc.db_card_owner_id(
                session.session_id, source_uid, conn=_hc._db)
        if prompt_owner_id is None:
            prompt_owner_id = 0
        prompt_owner_id = int(prompt_owner_id)
        # Resolve every explicit target template of this trigger.  The client
        # keys target instances by (card, ability) and clears the stored list
        # for each OptionInstance with the same opt_id (PlayerOptions.cs), so
        # all of them must ride ONE OptionInstance or only the last is offered
        # (Rowdy Ringmaster's Deploy asked for the friendly troop but never the
        # opposing one, so its battle had no defender).  ``candidates`` is the
        # already-computed pool for the first template; additional templates
        # are computed here with the same evaluator.
        all_target_ids = []
        if target_template_ids:
            try:
                all_target_ids = json.loads(
                    _hc.db_ability_target_template_ids(
                        ability_guid, conn=_hc._db) or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                all_target_ids = []
        if candidates and all(isinstance(c, (list, tuple, set))
                              for c in candidates):
            candidate_sets = [list(c) for c in candidates]
        elif candidates:
            candidate_sets = [list(candidates)]
        else:
            candidate_sets = []
        entries = []
        for position, tid in enumerate(target_template_ids or ()):
            index = next(
                (i for i, known in enumerate(all_target_ids)
                 if str(known).lower() == str(tid).lower()), position)
            if position < len(candidate_sets) and candidate_sets[position]:
                uids = [int(u) for u in candidate_sets[position]]
            else:
                try:
                    from rules_port.targeting import legal_targets
                    uids = [int(u) for u in legal_targets(
                        _hc._db, session.session_id, prompt_owner_id, tid,
                        source_uid, both_players=True,
                        champions=self._champion_targets(),
                        battle_state=bstate)]
                except Exception:
                    uids = []
            minimum, maximum = self._plan_target_bounds(tid)
            entries.append((index, tid, uids, int(minimum), int(maximum)))
        target_index = entries[0][0] if entries else 0
        bstate["pending_trigger"] = {
            "ability_guid": ability_guid,
            "source_uid": int(source_uid),
            "owner_id": prompt_owner_id,
            "instance_id": inst_id,
            "target_index": target_index,
            "target_template_id": (entries[0][1] if entries
                                   else (target_template_ids or [None])[0]),
            "target_entries": [
                {"index": index, "template_id": tid,
                 "minimum": minimum, "maximum": maximum}
                for index, tid, _uids, minimum, maximum in entries],
            "trigger_target_uid": (None if trigger_target_uid is None
                                   else int(trigger_target_uid)),
            "optional": bool(optional),
        }
        # PvP: the trigger choice lives in the PvP state (not the battle
        # state), and the prompt goes only to the choosing player via the
        # tournament packet path — same pattern as _prompt_deck_search.
        if bstate.get("pvp"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state, _send_pvp_packet)
            from gamemodes.tournament_engine import player_handlers
            from pvp_db import db_game_session_pids
            pend = bstate["pending_trigger"]
            state = pvp_load_state(session) or {}
            state["pending_trigger"] = pend
            pvp_save_state(session, state)
            pids = db_game_session_pids(session.session_id)
            chooser_pid = int(pend.get("owner_id", 0)) or int(pl_t.uid64 >> 8)
            if chooser_pid not in pids:
                chooser_pid = pids[0] if pids else 0
            opp_pid = [p for p in pids if p != chooser_pid][0] \
                if len(pids) > 1 else 0
            chooser = _hc.game_engine.UID.make(244, chooser_pid)
            g2 = _hc.game_engine.Game(int(session.session_id), chooser,
                                  _hc.game_engine.UID.make(244, opp_pid))
            g2.events = [ev for ev in game.events]
            # The prompt must NOT ride the shared event stream sent to both
            # players by _pvp_play_troop — strip it here so only the chooser
            # sees the option list / class-39 prompt.
            strip_types = (
                "PlayerOptionListSessionEventArgs",
                "TriggeredAbilityActivationDataRequiredSessionEventArgs",
                "GreenLightSessionEventArgs",
            )
            game.events = [ev for ev in game.events
                           if ev.__class__.__name__ not in strip_types]
            h = player_handlers.get(chooser_pid)
            if h is not None:
                # Build the actual private picker packet.  The shared event
                # stream only contains the chain-resolution events; merely
                # labelling this as a prompt leaves PvP clients with no
                # PlayerOptionList or class-39 request to drive the picker.
                ev = g2._make_event(_hc.game_engine.PlayerOptionListSessionEventArgs)
                ev.player_id = chooser
                opt = g2._make_event(_hc.game_engine.PlayerOptionSessionEventArgs)
                opt.card = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(source_uid)))
                opt.state = _hc.game_engine.ECardUsage.Activate
                opt_inst = g2._make_event(_hc.game_engine.OptionInstanceSessionEventArgs)
                opt_inst.opt_id = _hc.game_engine.ResourceId.from_str(ability_guid)
                for index, tid, uids, minimum, maximum in entries:
                    opt_inst.target_ids.append(
                        _hc.game_engine.ResourceId.from_str(tid))
                    tgt = g2._make_event(
                        _hc.game_engine.TargetInstanceSessionEventArgs)
                    # Preserve the authored index. ConfigureAbility uses it
                    # when it serializes this choice into TargetMap.
                    tgt.target_index = index
                    tgt.target_id = _hc.game_engine.ResourceId.from_str(tid)
                    tgt.targets = [_hc.game_engine.SessionCardId(
                        _hc.game_engine.UID(int(u))) for u in uids]
                    opt_inst.target_instances.append(tgt)
                    opt_inst.min_target_counts.append(minimum)
                    opt_inst.max_target_counts.append(maximum)
                opt.instances.append(opt_inst)
                ev.options.append(opt)
                g2._push(ev)
                ev39 = g2._make_event(
                    _hc.game_engine.TriggeredAbilityActivationDataRequiredSessionEventArgs)
                ev39.player_id = chooser
                ev39.ability_instance_ids = [inst_id]
                ev39.ability_template_ids = [
                    _hc.game_engine.ResourceId.from_str(ability_guid)]
                ev39.source_card_ids = [
                    _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(source_uid)))]
                g2._push(ev39)
                g2.push_green_light(chooser, _hc.game_engine.EPriorityContext.Normal)
                _send_pvp_packet(h, session, g2, chooser, "trigger")
            _hc.log_req(f"    PvP trigger prompt: {ability_guid[:8]} -> pid "
                    f"{chooser_pid} targets={len(entries)}")
            return
        _be.save_state(session, bstate)
        # PlayerOptionList: trigger card + the ability as an option + the
        # legal candidates as a TargetInstance keyed by the target template.
        ev = game._make_event(_hc.game_engine.PlayerOptionListSessionEventArgs)
        ev.player_id = pl_t
        opt = game._make_event(_hc.game_engine.PlayerOptionSessionEventArgs)
        opt.card = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(source_uid)))
        opt.state = _hc.game_engine.ECardUsage.Activate
        inst = game._make_event(_hc.game_engine.OptionInstanceSessionEventArgs)
        inst.opt_id = _hc.game_engine.ResourceId.from_str(ability_guid)
        # ConfigureAbility matches TargetInstances through this parallel list.
        # PvP otherwise receives candidates but cannot open the target picker.
        for index, tid, uids, minimum, maximum in entries:
            inst.target_ids.append(_hc.game_engine.ResourceId.from_str(tid))
            tgt = game._make_event(_hc.game_engine.TargetInstanceSessionEventArgs)
            # The explicit target may follow auto targets such as "this".
            tgt.target_index = index
            tgt.target_id = _hc.game_engine.ResourceId.from_str(tid)
            tgt.targets = [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(u)))
                           for u in uids]
            inst.target_instances.append(tgt)
            inst.min_target_counts.append(minimum)
            inst.max_target_counts.append(maximum)
        opt.instances.append(inst)
        ev.options.append(opt)
        game._push(ev)
        # Class 39 — TriggeredAbilityActivationDataRequired.
        ev39 = _hc.game_engine.TriggeredAbilityActivationDataRequiredSessionEventArgs()
        ev39.player_id = pl_t
        ev39.ability_instance_ids = [inst_id]
        ev39.ability_template_ids = [_hc.game_engine.ResourceId.from_str(ability_guid)]
        ev39.source_card_ids = [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(source_uid)))]
        game._push(ev39)
        # The picker needs priority to commit (the client's
        # BattleStateTriggeredAbilities -> BattleStateConfigureAbility flow).
        game.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
        _hc.log_req(f"    Trigger target prompt: {ability_guid[:8]} source={hex(source_uid)} "
                f"targets={len(entries)} inst={inst_id}")

    def _queue_conversation_prompt(self, game, session, pl_t, ai_t, bstate,
                                   conversation_id):
        """Publish class-55 conversation UI and persist its BOM continuation.

        The client implementation of ConversationAbilityEffectTemplate does
        not resolve the next effect immediately.  It queues an encounter mod,
        enters WaitForConversationAction, and resumes only after the client
        submits EncounterModDialogTransaction.  Keep that same boundary in
        both battle modes; the PvP state is copied to its tournament-owned
        JSON by the existing prompt pattern.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)

        parent = bstate.get("_choice_parent") or {}
        ability_guid = str(parent.get("ability_guid") or
                           bstate.get("resolving_ability") or "").lower()
        source_uid = bstate.get("resolving_source_uid")
        owner_id = int(parent.get(
            "owner_id", bstate.get("resolving_owner_id", 0)) or 0)
        # The original client always applies an encounter conversation to its
        # UserPlayer.  In PvE the source can be an AI encounter card, so keep
        # the ability owner for resumption but route the answer to the human
        # controller.
        answer_owner_id = owner_id
        if not bstate.get("pvp"):
            answer_owner_id = int(
                self.user_profile["id"] if self.user_profile else owner_id)
        resume_order = int(parent.get(
            "resume_effect_order",
            int(bstate.get("resolving_effect_order", 0)) + 1))
        if bstate.get("_rules_port_attached"):
            from rules_port.abilities import AbilityContinuation
        else:
            from rules_port.builder import AbilityContinuation
        pending = AbilityContinuation.from_state(
            bstate, ability_guid=ability_guid, source_uid=source_uid,
            owner_id=answer_owner_id,
            target_map=(parent.get("target_map") or
                        bstate.get("ability_target_map") or {}),
            variables=(parent.get("variables") or
                       bstate.get("ability_variables") or {}),
            resume_effect_order=resume_order).to_dict()
        pending.update({"kind": "conversation",
                        "conversation_id": str(conversation_id).lower(),
                        "ability_owner_id": owner_id})
        event_player = pl_t
        if bstate.get("pvp"):
            from services.tournament_game import pvp_load_state, pvp_save_state
            state = pvp_load_state(session) or {}
            state["pending_conversation"] = pending
            state["resolution_paused"] = True
            state["conversation_resume_priority_pid"] = int(
                state.get("priority_pid", answer_owner_id) or answer_owner_id)
            pvp_save_state(session, state)
            event_player = _hc.game_engine.UID.make(244, owner_id)
        else:
            bstate["pending_conversation"] = pending
            bstate["resolution_paused"] = True
            _be.save_state(session, bstate)

        event = game._make_event(
            _hc.game_engine.EncounterModDialogSessionEventArgs)
        event.player_id = event_player
        event.conversation_template_id = _hc.game_engine.ResourceId.from_str(
            str(conversation_id))
        # Conversation UI is controller-private in PvP.  PvE has one human
        # controller, so leaving this unset preserves the normal event path.
        if bstate.get("pvp"):
            event._private_player_uid = event_player
        game._push(event)
        _hc.log_req(f"    Conversation queued: {str(conversation_id)[:8]} "
                f"ability={ability_guid[:8]} owner={answer_owner_id}")
        return f"conversation: awaiting {str(conversation_id).lower()}"

    def _resolve_pending_conversation(self, session, transaction):
        """Resume a BOM after the client closes class-55 conversation UI."""
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        pending_state = _be.load_state(session)
        pending = pending_state.get("pending_conversation")
        conversation_id = _hc.extract_resource_guid(
            transaction.inner_bytes, "ConversationId")
        if not pending:
            # FRA challenge conversations are queued directly by battle
            # setup, rather than by a BOM ability continuation.  The stock
            # client only sends the conversation GUID when its authored
            # answer (the row's answer_text) is clicked, so record that as
            # accepting the current challenge before acknowledging the
            # class-55 transaction.
            if conversation_id:
                from services.arena import (
                    record_fra_challenge_conversation_answer)
                if record_fra_challenge_conversation_answer(
                        self.user_profile["id"], conversation_id):
                    _hc.log_req(
                        "    FRA challenge answer accepted: "
                        f"{conversation_id[:8]}")
            self._push_transaction_ack(session)
            return True
        if (conversation_id and conversation_id != str(
                pending.get("conversation_id", "")).lower()):
            _hc.log_req(f"    Conversation answer rejected: got {conversation_id}, "
                    f"expected {pending.get('conversation_id')}")
            self._push_transaction_ack(session)
            return True
        owner_id = int(pending.get("owner_id", 0) or 0)
        if owner_id != int(self.user_profile["id"] if self.user_profile else 0):
            _hc.log_req(f"    Conversation answer rejected for owner {owner_id}")
            self._push_transaction_ack(session)
            return True

        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        pending_state.pop("pending_conversation", None)
        pending_state.pop("resolution_paused", None)
        game = self._fresh_game(session, pl_t, ai_t, pending_state)
        from rules_port.resolution import resolve_port_ability
        ability_owner_id = int(pending.get(
            "ability_owner_id", owner_id) or owner_id)
        resolve_port_ability(
            self, game, session, _hc._db, pl_t, ai_t, pending_state,
            pending.get("ability_guid", ""), pending.get("source_uid"),
            ability_owner_id, target_map={int(k): v for k, v in
                                  (pending.get("target_map") or {}).items()},
            variables=pending.get("variables") or {},
            resume_from_order=int(pending.get("resume_effect_order", 0)),
        )
        _be.save_state(session, pending_state)

        pending_input = (pending_state.get("pending_conversation") or
                         pending_state.get("pending_choice") or
                         pending_state.get("pending_trigger") or
                         pending_state.get("pending_deck_search") or
                         pending_state.get("pending_discard_ability"))
        if pending_input:
            # The prompt helper owns any replacement picker.  The current
            # packet still carries public mutations and/or class 55 itself.
            self._send_battle_events(session, game, pl_t)
            return True

        if _be.stack_empty(pending_state):
            game.push_chain_empty()
            phase = _be.current_phase(pending_state)
            game.push_green_light(
                pl_t, self._priority_context_for(phase, pending_state))
            self._send_battle_events(session, game, pl_t)
            if phase in (_hc.game_engine.ETurnPhases.FirstMainPhase,
                         _hc.game_engine.ETurnPhases.SecondMainPhase):
                self._push_main_phase_options(session, pl_t, ai_t)
        else:
            game.push_green_light(
                pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
            self._send_battle_events(session, game, pl_t)
        self._push_transaction_ack(session)
        _hc.log_req(f"    Conversation resolved: "
                f"{pending.get('conversation_id', '')[:8]}")
        return True

    def _prompt_deck_search(self, game, session, pl_t, ai_t, bstate,
                            ability_guid, source_uid, owner_id, candidates,
                            kind="search", continuation=None):
        """Ask a HUMAN controller to pick which matching deck card a
        "search your deck" effect puts into their hand (e.g. Darkspire
        Priestess's Deathcry).  The AI auto-picks a random match instead.

        Mirrors the client's SearchYourDeck flow: a PlayerOptionList carrying
        every matching deck card plus the class-39
        TriggeredAbilityActivationDataRequired event.  The follow-up
        SetAbilityActivationDataTransaction resolves with the chosen card.

        FRA: the prompt rides on the shared ``game`` event stream.  PvP: the
        prompt is sent only to the choosing player and the pending choice is
        persisted in the PvP state for the tournament transaction handler.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        inst_id = int(bstate.get("_next_instance_id", 1))
        bstate["_next_instance_id"] = inst_id + 1
        # The class-39 prompt must reference the nested SEARCH ability (e.g.
        # 37955055) whose own target template drives the choosing window — the
        # top deathcry's "You" template makes the client find no targets and
        # instantly commit.
        search_ability, search_template = self._deck_search_ability(ability_guid)
        prompt_ability = search_ability or ability_guid
        pend = {
            "ability_guid": prompt_ability,
            "source_uid": int(source_uid),
            "owner_id": int(owner_id),
            "instance_id": inst_id,
            "candidates": [int(u) for u in (candidates or [])],
            "kind": kind,
        }
        if continuation:
            pend["continuation"] = continuation
        # The TargetInstance's target_id MUST be the "search your deck" target
        # template (Choosing in its CollectionFlags + the Darkspire filter) —
        # the client's BattleStateTarget only accepts candidates whose
        # collection is covered by the template.  The deathcry's own template
        # ("You"/Warzone) left the picker with a bare cursor.
        target_id = search_template or prompt_ability

        def build_prompt(g, player_uid):
            # Present temporary candidates in the client's Choosing zone.  The
            # database cards remain in the deck; the follow-up resolver moves
            # every candidate back to the deck view.  The TargetInstance still
            # carries the real search template and candidate IDs, which is
            # what makes this a selectable deck-search prompt rather than a
            # generic hand-card click.
            for cu in pend["candidates"]:
                c_scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(cu)))
                c_row = _hc.db_card_zone_details(
                    session.session_id, int(cu), conn=_hc._db)
                if not c_row:
                    continue
                c_tpl = c_row[0]
                _tpl2, ct2, _n2, cost2, atk2, def2, _g2 = self._card_full_data(
                    g, c_scid, c_tpl, c_row[1])
                g.push_card_updated(c_scid, player_uid,
                                    _hc.game_engine.ECardCollections.Choosing, ct2,
                                    template_id=c_tpl, cost=cost2, attack=atk2,
                                    defense=def2)
            ev = g._make_event(_hc.game_engine.PlayerOptionListSessionEventArgs)
            ev.player_id = player_uid
            opt = g._make_event(_hc.game_engine.PlayerOptionSessionEventArgs)
            opt.card = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(source_uid)))
            opt.state = _hc.game_engine.ECardUsage.Activate
            inst = g._make_event(_hc.game_engine.OptionInstanceSessionEventArgs)
            inst.opt_id = _hc.game_engine.ResourceId.from_str(prompt_ability)
            tgt = g._make_event(_hc.game_engine.TargetInstanceSessionEventArgs)
            tgt.target_index = 0
            tgt.target_id = _hc.game_engine.ResourceId.from_str(target_id)
            # ConfigureAbility matches target instances through this parallel
            # target-template list.  Without it the client can render the
            # candidates but keep the outer BattleStateTarget alive after a
            # selection because it cannot associate the chosen card with the
            # nested search ability's target.
            inst.target_ids.append(_hc.game_engine.ResourceId.from_str(target_id))
            tgt.targets = [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(u)))
                           for u in pend["candidates"]]
            inst.target_instances.append(tgt)
            inst.min_target_counts = [1]
            inst.max_target_counts = [1]
            opt.instances.append(inst)
            ev.options.append(opt)
            g._push(ev)
            ev39 = _hc.game_engine.TriggeredAbilityActivationDataRequiredSessionEventArgs()
            ev39.player_id = player_uid
            ev39.ability_instance_ids = [inst_id]
            ev39.ability_template_ids = [
                _hc.game_engine.ResourceId.from_str(prompt_ability)]
            ev39.source_card_ids = [
                _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(source_uid)))]
            g._push(ev39)
            g.push_green_light(player_uid, _hc.game_engine.EPriorityContext.Normal)

        if bstate.get("pvp"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state, _send_pvp_packet)
            from gamemodes.tournament_engine import player_handlers
            from pvp_db import db_game_session_pids
            state = pvp_load_state(session) or {}
            state["pending_deck_search"] = pend
            pvp_save_state(session, state)
            h = player_handlers.get(int(owner_id))
            pids = db_game_session_pids(session.session_id)
            opp_pid = [p for p in pids if p != int(owner_id)][0]
            chooser = _hc.game_engine.UID.make(244, int(owner_id))
            g2 = _hc.game_engine.Game(int(session.session_id), chooser,
                                  _hc.game_engine.UID.make(244, opp_pid))
            build_prompt(g2, chooser)
            if h is not None:
                _send_pvp_packet(h, session, g2, chooser, "deck-search")
            return (f"deck search: awaiting {len(pend['candidates'])} "
                    f"candidates for pid {owner_id}")
        bstate["pending_deck_search"] = pend
        _be.save_state(session, bstate)
        build_prompt(game, pl_t)
        _hc.log_req(f"    Deck-search prompt: {ability_guid[:8]} source={hex(source_uid)} "
                f"candidates={len(pend['candidates'])} inst={inst_id}")
        return f"deck search: awaiting {len(pend['candidates'])} candidates"

    def _prompt_choice_cards(self, game, session, pl_t, ai_t, bstate,
                             pending):
        """Publish the client's built-in ChooseAndPlay card picker.

        DoubleChoice creates real Choice-card instances in the Choosing zone,
        then advertises the built-in ability that plays one of them for free.
        PvP choice cards and their picker are private to the controller; the
        selected card's eventual PlayedResources move is public.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        from abilities.framework.effects.choices import (
            CHOICE_COPY_ABILITY, CHOICE_TARGET_TEMPLATE)
        _hc.log_req("    PvP choice prompt DEBUG: "
                f"kind={pending.get('kind')} "
                f"source={pending.get('source_uid')} "
                f"parent={pending.get('parent', {}).get('ability_guid')} "
                f"parent_resume="
                f"{pending.get('parent', {}).get('resume_effect_order')} "
                f"child={pending.get('continuation', {}).get('ability_guid')}",
                level="DEBUG")

        def build_prompt(target_game, player_uid):
            ev = target_game._make_event(
                _hc.game_engine.PlayerOptionListSessionEventArgs)
            ev.player_id = player_uid
            opt = target_game._make_event(
                _hc.game_engine.PlayerOptionSessionEventArgs)
            opt.card = _hc.game_engine.SessionCardId(
                _hc.game_engine.UID(int(pending["source_uid"])))
            opt.state = _hc.game_engine.ECardUsage.Activate
            inst = target_game._make_event(
                _hc.game_engine.OptionInstanceSessionEventArgs)
            inst.opt_id = _hc.game_engine.ResourceId.from_str(
                CHOICE_COPY_ABILITY)
            inst.target_ids.append(_hc.game_engine.ResourceId.from_str(
                CHOICE_TARGET_TEMPLATE))
            inst.min_target_counts = [1]
            inst.max_target_counts = [1]
            target = target_game._make_event(
                _hc.game_engine.TargetInstanceSessionEventArgs)
            target.target_index = 0
            target.target_id = _hc.game_engine.ResourceId.from_str(
                CHOICE_TARGET_TEMPLATE)
            target.targets = [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(uid)))
                              for uid in pending["choice_uids"]]
            inst.target_instances.append(target)
            opt.instances.append(inst)
            ev.options.append(opt)
            target_game._push(ev)
            # DoubleChoice is resolved by the client's normal triggered-ability
            # configuration flow: PlayerOptionList supplies the generated cards,
            # then class 23 tells UIBattle to push BattleStateUseTriggeredAbility
            # for the built-in ChooseAndPlay ability. Without this event the
            # options only update the cache; no picker state is pushed.
            req = target_game._make_event(
                _hc.game_engine.AbilityActivationDataRequiredSessionEventArgs)
            req.player_id = player_uid
            req.ability_instance_id = int(pending.get("instance_id", 1))
            req.ability_parent_id = 0
            req.source_card_id = _hc.game_engine.SessionCardId(
                _hc.game_engine.UID(int(pending["source_uid"])))
            req.ability_template_id = _hc.game_engine.ResourceId.from_str(
                CHOICE_COPY_ABILITY)
            req.effect_group_id = 1
            # ChooseAndPlay has one explicit target effect (the generated
            # card in Choosing).  The client uses this effect-instance list
            # while building BattleStateConfigureAbility; an empty list can
            # leave the class-23 state with no selectable threshold even
            # though the PlayerOptionList contains the two choice cards.
            req.effect_instance_ids = [0]
            req.resolve_chain = False
            target_game._push(req)
            target_game.push_green_light(
                player_uid, _hc.game_engine.EPriorityContext.Normal)

        bstate["pending_choice"] = pending
        bstate["resolution_paused"] = True
        if bstate.get("pvp"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state, _send_pvp_packet)
            from gamemodes.tournament_engine import player_handlers
            from pvp_db import db_game_session_pids
            state = pvp_load_state(session) or {}
            state["pending_choice"] = pending
            state["resolution_paused"] = True
            pvp_save_state(session, state)
            chooser_id = int(pending["owner_id"])
            pids = db_game_session_pids(session.session_id)
            opponent_id = next((pid for pid in pids if int(pid) != chooser_id), 0)
            chooser = _hc.game_engine.UID.make(244, chooser_id)
            private = _hc.game_engine.Game(
                int(session.session_id), chooser,
                _hc.game_engine.UID.make(244, opponent_id))
            choice_ids = ({int(uid) for uid in pending["choice_uids"]} |
                          {int(uid) for uid in
                           (bstate.pop("private_choice_uids", []) or [])})
            # The generated cards and picker are controller-private. Keep the
            # chain-resolution events in the shared game stream.
            private.events = []
            for event in list(game.events):
                card_id = getattr(event, "session_card_id", None)
                card_uid = getattr(getattr(card_id, "uid", None), "uid64", None)
                if card_uid is not None and int(card_uid) in choice_ids:
                    private.events.append(event)
            game.events = [
                event for event in game.events
                if not (
                    event.__class__.__name__ in (
                        "PlayerOptionListSessionEventArgs",
                        "GreenLightSessionEventArgs")
                    or (getattr(getattr(
                        getattr(event, "session_card_id", None), "uid", None),
                        "uid64", None) in choice_ids))]
            build_prompt(private, chooser)
            prompt_handler = player_handlers.get(chooser_id)
            # Tell the opponent this player is choosing a card FIRST, so the
            # picker packet below is the last thing the chooser receives and
            # nothing can cover it.
            from services.tournament_game import _pvp_push_waiting_on
            _pvp_push_waiting_on(session, chooser_id)
            if prompt_handler is not None:
                _send_pvp_packet(prompt_handler, session, private, chooser,
                                 "choice")
            return
        _be.save_state(session, bstate)
        build_prompt(game, pl_t)

    def _resolve_pending_choice(self, session, pl_t, ai_t, inner_bytes,
                                ability_guid=None, typed_payload=None):
        """Play a selected Choice token and resume its parent BOM."""
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        from abilities.framework.effects.choices import (
            CHOOSE_AND_PLAY_ABILITY, CHOICE_COPY_ABILITY, extract_card_uids)
        from rules_port.choice_effects import (
            _play_choice_card as play_choice_card,
            _resolve_choice_card_abilities as resolve_choice_card_abilities)
        from rules_port.resolution import (
            resolve_port_ability,
            resume_ability_continuation_parents)
        bstate = _be.load_state(session)
        pending = bstate.get("pending_choice")
        if not pending:
            return False
        if (ability_guid and str(ability_guid).lower() not in (
                CHOOSE_AND_PLAY_ABILITY, CHOICE_COPY_ABILITY)):
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
        chosen_uid = next((uid for uid in reversed(selected)
                           if int(uid) in {
                               int(value) for value in
                               pending.get("choice_uids", [])}), None)
        if chosen_uid is None:
            _hc.log_req("    Choice answer invalid: no legal choice card")
            self._push_transaction_ack(session)
            return True
        owner_id = int(pending.get("owner_id", self.user_profile["id"]))
        if owner_id != int(self.user_profile["id"]):
            _hc.log_req(f"    Choice answer rejected for owner {owner_id}")
            self._push_transaction_ack(session)
            return True
        bstate.pop("pending_choice", None)
        bstate.pop("resolution_paused", None)
        g = self._fresh_game(session, pl_t, ai_t, bstate)
        if pending.get("kind") == "choice_zone_target":
            # The picker was opened by the authored child target, not by a
            # summon.  Let that child play and resolve the selected token,
            # then resume the enclosing ability after its ActivateAbility
            # effect.  This preserves one picker for all generated options.
            continuation = pending.get("continuation") or {}
            child_guid = str(continuation.get("ability_guid") or "").lower()
            child_source = int(continuation.get(
                "source_uid", pending.get("source_uid", 0)) or 0)
            child_owner = int(continuation.get(
                "owner_id", owner_id) or owner_id)
            child_targets = _hc._numeric_target_map(
                continuation.get("target_map"))
            child_targets[int(continuation.get("target_index", 0))] = \
                int(chosen_uid)
            resolve_port_ability(
                self, g, session, _hc._db, pl_t, ai_t, bstate,
                child_guid, child_source, child_owner,
                target_map=child_targets,
                variables=continuation.get("variables") or {},
                resume_from_order=int(
                    continuation.get("resume_effect_order", 0) or 0))
            parent = pending.get("parent") or {}
            parent_guid = str(parent.get("ability_guid") or "").lower()
            if parent_guid and not bstate.get("resolution_paused"):
                resolve_port_ability(
                    self, g, session, _hc._db, pl_t, ai_t, bstate,
                    parent_guid, parent.get("source_uid"),
                    int(parent.get("owner_id", owner_id) or owner_id),
                    target_map=_hc._numeric_target_map(parent.get("target_map")),
                    variables=parent.get("variables") or {},
                    resume_from_order=int(
                        parent.get("resume_effect_order", 0) or 0))
            # The child and its enclosing activation are resolved.  Release the
            # paused-item hold and mark the chain item's BOM done so the next
            # pass only finishes the card instead of reopening this picker.
            if not bstate.get("resolution_paused"):
                completed = int(bstate.pop(
                    "paused_chain_instance_id", 0) or 0)
                if completed:
                    bstate["completed_chain_instance_id"] = completed
            _be.save_state(session, bstate)
            projected = False
            if bstate.get("pending_choice"):
                self._send_battle_events(session, g, pl_t)
            else:
                completed_chain = self._completed_rules_port_chain(
                    session, bstate)
                if completed_chain is not None:
                    # Publish the selected card's effects before the finish
                    # pass removes the waiting chain item.
                    self._send_battle_events(session, g, pl_t)
                    finished_chain = self._resume_completed_rules_port_chain(
                        session, bstate, completed_chain)
                    if (finished_chain and
                            not bstate.get("resolution_paused")):
                        # Completing the item can expose the next native
                        # chain item (a trigger the chosen card raised) or end
                        # the paused phase; re-project that priority window
                        # instead of leaving the client on a stale pass button.
                        projected = self._advance_rules_port_to_priority(
                            session, pl_t, ai_t, bstate)
                else:
                    # Non-chain choice effects still need the legacy empty
                    # chain/priority projection. A paused RulesPort chain item
                    # emits those after its TopOfChainResolved and
                    # RemovedTopOfChain events in the shared chain resolver.
                    g.push_chain_empty()
                    g.push_green_light(
                        pl_t, _hc.game_engine.EPriorityContext.Normal)
                    self._send_battle_events(session, g, pl_t)
                if not projected:
                    if _be.current_phase(bstate) in (
                            _hc.game_engine.ETurnPhases.FirstMainPhase,
                            _hc.game_engine.ETurnPhases.SecondMainPhase):
                        self._push_main_phase_options(session, pl_t, ai_t)
            if not projected:
                self._push_transaction_ack(session)
            _hc.log_req(f"    Choice selected: {hex(int(chosen_uid))} "
                    f"for {child_guid[:8]} target")
            return True
        from rules_port.context import EffectContext
        bstate["_rules_port_attached"] = True
        # Mark the lifecycle phase while TurnStarted triggers resolve so
        # temporary current-resource gains can survive the later Prep refill.
        _previous_phase = bstate.get("phase")
        bstate["phase"] = _hc.game_engine.ETurnPhases.StartTurn
        choice_context = EffectContext.from_rules_port(
            g, session, _hc._db, self, pl_t, ai_t, bstate,
            "", ability=None)
        if not play_choice_card(choice_context, chosen_uid, owner_id):
            _hc.log_req(f"    Choice answer rejected: {hex(int(chosen_uid))}")
            bstate["pending_choice"] = pending
            bstate["resolution_paused"] = True
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            return True
        resolve_choice_card_abilities(
            choice_context, chosen_uid, pending.get("source_uid"), owner_id)
        target_map = _hc._numeric_target_map(pending.get("target_map"))
        resolve_port_ability(
            self, g, session, _hc._db, pl_t, ai_t, bstate,
            pending["ability_guid"], pending.get("source_uid"), owner_id,
            target_map=target_map, variables=pending.get("variables") or {},
            resume_from_order=int(pending.get("resume_effect_order", 0)))
        _be.save_state(session, bstate)
        if bstate.get("pending_choice"):
            # The resumed BOM has opened its second choice. The prompt helper
            # already supplied the new PlayerOptionList and green light.
            self._send_battle_events(session, g, pl_t)
        else:
            g.push_chain_empty()
            g.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
            self._send_battle_events(session, g, pl_t)
            if _be.current_phase(bstate) in (
                    _hc.game_engine.ETurnPhases.FirstMainPhase,
                    _hc.game_engine.ETurnPhases.SecondMainPhase):
                self._push_main_phase_options(session, pl_t, ai_t)
        self._push_transaction_ack(session)
        _hc.log_req(f"    Choice selected: {hex(int(chosen_uid))} "
                f"for {pending['ability_guid'][:8]}")
        return True

    def _push_private_revealed_cards(self, session, bstate, owner_id, rows,
                                     pl_t, ai_t):
        """Send a controller-only CardsRevealed packet for a PvP look effect.

        ``CardsRevealedSessionEventArgs`` is recipient-scoped by delivery, not
        by the ``player_id`` field inside the event.  A shared PvP packet would
        therefore disclose a "look at" effect to the opponent.  The reveal
        visibility comes from the extracted RevealCards effect metadata; this
        helper only handles the protocol delivery and card definitions.
        """
        import hconnect_server as _hc
        if not (bstate or {}).get("pvp"):
            return False
        from services.tournament_game import _send_pvp_packet
        from gamemodes.tournament_engine import player_handlers
        chooser_pid = int(owner_id)
        pids = _hc.db_game_session_pids(session.session_id)
        if chooser_pid not in pids:
            return False
        opponent_pid = next((p for p in pids if int(p) != chooser_pid), 0)
        chooser = _hc.game_engine.UID.make(244, chooser_pid)
        opponent = _hc.game_engine.UID.make(244, opponent_pid)
        private = _hc.game_engine.Game(int(session.session_id), chooser, opponent)
        ids = []
        positions = []
        for card_uid, position, template_guid, _user_id, card_state in rows:
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(card_uid)))
            _tpl, ctype, _name, cost, attack, defense, _gem = \
                self._card_full_data(private, scid, template_guid)
            private.push_card_updated(
                scid, chooser, _hc.game_engine.ECardCollections.Deck, ctype,
                state=int(card_state or 0), template_id=template_guid,
                cost=cost, attack=attack, defense=defense, nulling=False)
            ids.append(scid)
            positions.append(int(position or 0))
        ev = private._make_event(_hc.game_engine.CardsRevealedSessionEventArgs)
        ev.player_id = chooser
        ev.session_card_ids = ids
        ev.collections = [_hc.game_engine.ECardCollections.Deck] * len(ids)
        ev.owning_players = [chooser] * len(ids)
        ev.positions = positions
        private._push(ev)
        handler = player_handlers.get(chooser_pid)
        if handler is not None:
            _send_pvp_packet(handler, session, private, chooser,
                             "private-reveal")
        bstate["private_reveal_sent"] = True
        return True

    def _prompt_revealed_choice(self, game, session, pl_t, ai_t, bstate,
                                ability_guid, source_uid, owner_id,
                                candidates, revealed_cards, optional=False,
                                continuation=None):
        """Prompt for an explicit choice from cards just revealed by BOM data.

        Oakhenge's child target is a SourceRevealedTargetTemplate filtered by
        IsTroop.  The client turns the reveal into a selectable target when
        the same packet carries a PlayerOptionList plus the class-23
        activation-data request for the revealing ability instance; it then
        merges the two into one revealed-card picker.  PvP sends the reveal
        and picker only to the revealing player; the shared chain stream is
        stripped of the sensitive class-51 event.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        inst_id = int(bstate.get("_next_instance_id", 1))
        bstate["_next_instance_id"] = inst_id + 1
        pend = {
            "ability_guid": str(ability_guid),
            "source_uid": int(source_uid),
            "owner_id": int(owner_id),
            "instance_id": inst_id,
            "candidates": [int(u) for u in (candidates or [])],
            "revealed_cards": [int(u) for u in (revealed_cards or [])],
            "kind": "revealed_card",
            "optional": bool(optional),
        }
        if continuation:
            pend["continuation"] = continuation
        # The continuation must follow the authored child effect.  In
        # particular, Starsphere moves the selected card to the top of the
        # deck; Oakhenge-style effects move their selected card to hand.
        move_row = next((row[2] for row in _hc.db_ability_effect_rows(
            ability_guid, conn=_hc._db)
                         if row[1] == "MoveCardToZoneEffectTemplate"), None)
        try:
            move_spec = json.loads(move_row or "{}") if move_row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            move_spec = {}
        pend["move_destination"] = str(
            move_spec.get("destination") or "Hand").lower()
        pend["move_location"] = str(
            move_spec.get("location") or "").lower()

        def build_prompt(g, player_uid):
            rows = _hc.db_card_positions(
                session.session_id, pend["revealed_cards"], conn=_hc._db)
            # The client only turns a reveal into a picker when the same
            # packet carries the class-23 activation-data request for the
            # revealing ability instance: NetworkPacketSessionEventArgs.
            # CombineRevealAndChoose drops the plain Coverflow and turns the
            # revealed cards that are not legal targets into the picker's
            # AdditionalTargets.  Without the shared instance id the client
            # keeps the informational Coverflow -- whose OK button cannot
            # select a card -- and no troop picker ever opens.
            #
            # The reveal leaf normally already emitted CardsRevealed into this
            # same Game/packet, so reuse that event instead of queueing a
            # second Coverflow.  A controller-private PvP reveal is delivered
            # in its own packet and is not repeated here.
            if not (bstate or {}).get("private_reveal_sent"):
                revealed_ids = {int(u) for u in pend["revealed_cards"]}
                reveal_event = None
                for event in getattr(g, "events", ()):
                    if not isinstance(
                            event,
                            _hc.game_engine.CardsRevealedSessionEventArgs):
                        continue
                    event_ids = {
                        int(card.uid.uid64)
                        for card in (event.session_card_ids or [])
                    }
                    if event_ids == revealed_ids:
                        reveal_event = event
                        break
                if reveal_event is None:
                    reveal_event = g._make_event(
                        _hc.game_engine.CardsRevealedSessionEventArgs)
                    reveal_event.player_id = player_uid
                    reveal_event.session_card_ids = [
                        _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(cu)))
                        for cu, _pos in rows]
                    reveal_event.collections = (
                        [_hc.game_engine.ECardCollections.Deck] * len(rows))
                    reveal_event.owning_players = [player_uid] * len(rows)
                    reveal_event.positions = [
                        int(pos or 0) for _cu, pos in rows]
                    g._push(reveal_event)
                reveal_event.ability_instance_id = inst_id

            opt_list = g._make_event(
                _hc.game_engine.PlayerOptionListSessionEventArgs)
            opt_list.player_id = player_uid
            opt = g._make_event(_hc.game_engine.PlayerOptionSessionEventArgs)
            opt.card = _hc.game_engine.SessionCardId(
                _hc.game_engine.UID(int(source_uid)))
            opt.state = _hc.game_engine.ECardUsage.Activate
            inst = g._make_event(_hc.game_engine.OptionInstanceSessionEventArgs)
            inst.opt_id = _hc.game_engine.ResourceId.from_str(ability_guid)
            target_ids_json = _hc.db_ability_target_template_ids(
                ability_guid, conn=_hc._db)
            try:
                target_id = json.loads(target_ids_json or "[]")[0]
            except (TypeError, ValueError, IndexError, json.JSONDecodeError):
                target_id = "00000000-0000-0000-0000-000000000000"
            inst.target_ids.append(_hc.game_engine.ResourceId.from_str(target_id))
            tgt = g._make_event(_hc.game_engine.TargetInstanceSessionEventArgs)
            tgt.target_index = 0
            tgt.target_id = _hc.game_engine.ResourceId.from_str(target_id)
            tgt.targets = [
                _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(u)))
                for u in pend["candidates"]]
            inst.target_instances.append(tgt)
            inst.min_target_counts = [0 if optional else 1]
            inst.max_target_counts = [1]
            opt.instances.append(inst)
            opt_list.options.append(opt)
            g._push(opt_list)

            # Class 23 is the client's activation-data picker for one ability
            # instance (UIBattle.OnAbilityActivationDataRequested pushes
            # BattleStateUseTriggeredAbility -> BattleStateConfigureAbility).
            # The class-39 triggered-ability request would instead show the
            # triggered-ability stack and answer with a different transaction.
            req = g._make_event(
                _hc.game_engine.AbilityActivationDataRequiredSessionEventArgs)
            req.player_id = player_uid
            req.ability_instance_id = inst_id
            req.ability_parent_id = 0
            req.source_card_id = _hc.game_engine.SessionCardId(
                _hc.game_engine.UID(int(source_uid)))
            req.ability_template_id = _hc.game_engine.ResourceId.from_str(
                ability_guid)
            req.effect_group_id = 1
            # Read back by the client only as the picker's effect scope; the
            # revealed-card choice is the ability's first effect instance.
            req.effect_instance_ids = [0]
            req.resolve_chain = False
            g._push(req)
            g.push_green_light(player_uid, _hc.game_engine.EPriorityContext.Normal)

        if bstate.get("pvp"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state, _send_pvp_packet)
            from gamemodes.tournament_engine import player_handlers
            from pvp_db import db_game_session_pids
            state = pvp_load_state(session) or {}
            state["pending_deck_search"] = pend
            pvp_save_state(session, state)
            pids = db_game_session_pids(session.session_id)
            chooser_pid = int(owner_id)
            opp_pid = [p for p in pids if p != chooser_pid][0] \
                if len(pids) > 1 else 0
            chooser = _hc.game_engine.UID.make(244, chooser_pid)
            private = _hc.game_engine.Game(
                int(session.session_id), chooser,
                _hc.game_engine.UID.make(244, opp_pid))
            build_prompt(private, chooser)
            # The reveal was initially appended to the shared resolution
            # game. Remove it before the caller broadcasts that game.
            game.events = [ev for ev in game.events
                           if ev.__class__.__name__ !=
                           "CardsRevealedSessionEventArgs"]
            h = player_handlers.get(chooser_pid)
            if h is not None:
                _send_pvp_packet(h, session, private, chooser,
                                 "revealed-choice")
            _hc.log_req(f"    PvP revealed-choice prompt: {ability_guid[:8]} "
                    f"-> pid {chooser_pid} candidates={len(candidates or [])}")
            return "revealed choice: awaiting player"

        bstate["pending_deck_search"] = pend
        _be.save_state(session, bstate)
        build_prompt(game, pl_t)
        _hc.log_req(f"    Revealed-choice prompt: {ability_guid[:8]} "
                f"source={hex(source_uid)} candidates={len(candidates or [])}")
        return "revealed choice: awaiting player"

    def _resolve_pending_trigger_target(self, session, pl_t, ai_t, inner_bytes,
                                        ability_guid=None):
        """Handle the player's target choice for a triggered ability with
        explicit targets (e.g. Solitary Exile's Deploy "Void another target
        card").

        The client answers the class-39 prompt with a SetAbilityActivationData
        transaction, which on the wire looks like an AbilityActivationData
        transaction — so this is called from BOTH the ability-activation branch
        and the set-ability-data branch.  Extracts the chosen card from the
        TargetMap, pushes the trigger onto the chain, and resolves it with that
        target.  Returns True when a pending trigger was consumed.
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        from abilities.framework._shared import owner_uid
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        pend = bstate.get("pending_trigger")
        if not pend:
            return False
        if ability_guid and str(pend.get("ability_guid", "")).lower() != str(ability_guid).lower():
            return False
        chosen_uid = pend.get("selected_uid")
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
        bstate.pop("pending_trigger")
        ag = pend["ability_guid"]
        src = int(pend["source_uid"])
        inst_id = int(pend["instance_id"])
        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            owner_id = int(pend.get("owner_id", 0) or 0)
            target_index = int(pend.get("target_index", 0) or 0)

            def normalize_target_map(raw):
                """Normalize a decoded TargetMap to ``{index: [uids]}``.

                The activation record may carry ``{index: [uid, ...]}`` or
                ``{index: {"session_card_ids": [...]}}``; a triggered ability
                with two chosen targets must forward both so the resolver's
                authored target indexes line up.
                """
                result = {}
                if not isinstance(raw, Mapping):
                    return result
                for key, value in raw.items():
                    try:
                        index = int(key)
                    except (TypeError, ValueError):
                        continue
                    values = value
                    if isinstance(values, Mapping):
                        values = (values.get("session_card_ids") or
                                  values.get("SessionCardIds") or ())
                    if not isinstance(values, (list, tuple, set)):
                        values = (values,) if values is not None else ()
                    uids = []
                    for item in values:
                        while isinstance(item, Mapping):
                            nested = next((item[field] for field in (
                                "uid64", "UID", "m_UID64", "value")
                                if field in item), None)
                            if nested is None or nested is item:
                                break
                            item = nested
                        try:
                            uid = int(item)
                        except (TypeError, ValueError):
                            continue
                        if uid > 0 and (uid & 0xFF) == 1:
                            uids.append(uid)
                    if uids:
                        result[index] = uids
                return result

            activation_data = pend.get("activation_data")
            activation_data = (dict(activation_data)
                               if isinstance(activation_data, Mapping) else {})
            target_map = normalize_target_map(
                activation_data.get("target_map"))
            # ``chosen_uid`` is the single-pick legacy signal; only fill its
            # slot when the fuller TargetMap did not already name it.
            if chosen_uid is not None and target_index not in target_map:
                target_map[target_index] = [int(chosen_uid)]
            if chosen_uid is None:
                primary = target_map.get(target_index)
                if not primary:
                    primary = next(iter(target_map.values()), None)
                if primary:
                    chosen_uid = int(primary[-1])
            activation_data["target_map"] = {
                str(index): uids for index, uids in target_map.items()}
            selected_uids = [uid for uids in target_map.values()
                             for uid in uids]
            descriptor = {
                "kind": "trigger",
                "ability_guid": ag,
                "source_uid": src,
                "source_owner_uid": owner_id,
                "target_uid": (None if chosen_uid is None
                               else int(chosen_uid)),
                "trigger_target_uid": pend.get("trigger_target_uid"),
                "activation_data": activation_data,
                "instance_id": inst_id,
            }
            queue_chain = getattr(port, "queue_projected_chain", None)
            if not callable(queue_chain):
                return False
            queue_chain(
                descriptor, owner_id,
                first_player_id=(getattr(port, "active_player_id", None)
                                 or pl_t))
            _be.save_state(session, bstate)
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            from gamedata import DEFAULT_RECORD_STORE, ability_graph
            graph = ability_graph(DEFAULT_RECORD_STORE, str(ag).lower())
            target_cards = [_hc.game_engine.SessionCardId(
                _hc.game_engine.UID(int(uid))) for uid in selected_uids]
            g.push_ability_on_chain(
                _hc.game_engine.SessionCardId(_hc.game_engine.UID(src)),
                _hc.game_engine.ResourceId.from_str(ag),
                ability_instance_id=inst_id,
                target_card_ids=target_cards,
                ignores_chain=bool(graph and graph.ignores_chain))
            event_sink = getattr(port, "event_sink", None)
            if event_sink is not None:
                # ``queue_projected_chain`` starts APNAP priority with the
                # active player, which is commonly the Practice AI here.  Do
                # not project a human GreenLight until that server-owned
                # response has gone through the same native pass/drive path
                # used by ordinary chain continuations.
                self._advance_rules_port_to_priority(
                    session, pl_t, ai_t, bstate)
            else:
                g.push_green_light(
                    pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
                self._send_battle_events(session, g, pl_t)
            self._push_transaction_ack(session)
            _hc.log_req(f"    RulesPort trigger target chosen: {ag[:8]} "
                    f"target={hex(chosen_uid) if chosen_uid else 'none'}")
            return True
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
        return True

    def _resolve_rules_port_trigger_continuation(self, session, transaction):
        """Consume a typed RulesPort trigger-target continuation."""
        import hconnect_server as _hc
        payload = transaction.payload.get("activation_data", {}) or {}
        _be = self._checkpoint_engine(session)
        state = _be.load_state(session)
        pending = state.get("pending_trigger")
        if not pending:
            # The class-39 "choose a card in your deck" picker shares this
            # continuation envelope.  Its resolver decodes the chosen card
            # from the original transaction bytes.
            if state.get("pending_deck_search"):
                original = getattr(
                    session, "_rules_port_dispatch_command", None)
                inner_bytes = (getattr(original, "inner_bytes", b"")
                               if original is not None else b"")
                pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
                ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
                handled = bool(self._resolve_pending_deck_search(
                    session, pl_t, ai_t, inner_bytes))
                if handled:
                    session._rules_port_mutation_emitted = True
                return handled
            return False

        def activation_record(value):
            """Select this prompt's AbilityActivationData record."""
            values = value if isinstance(value, (list, tuple)) else (value,)
            first = None
            wanted_instance = int(pending.get("instance_id", 0) or 0)
            wanted_guid = str(pending.get("ability_guid") or "").lower()
            for candidate in values:
                if not isinstance(candidate, Mapping):
                    continue
                if first is None:
                    first = candidate
                try:
                    instance = int(candidate.get(
                        "ability_instance_id", candidate.get("instance_id", 0)) or 0)
                except (TypeError, ValueError):
                    instance = 0
                guid = str(candidate.get("ability_template_id",
                                        candidate.get("ability_guid", "")) or "").lower()
                if ((wanted_instance and instance == wanted_instance) or
                        (wanted_guid and guid == wanted_guid)):
                    return dict(candidate)
            return dict(first or {})

        def flag(value, default=False):
            if value is None:
                return default
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        activation = activation_record(payload)
        if pending.get("optional"):
            # The client sets either OptedIn=false or Disable=true when the
            # optional trigger is declined. Missing fields are accepted for
            # older clients that submit the target envelope without the opt-in
            # members; an explicit false is the only decline signal.
            declined = (flag(activation.get("disable")) or
                        ("opted" in activation and
                         not flag(activation.get("opted"))))
            pending["activation_data"] = activation
            if declined:
                instance_id = int(pending.get("instance_id", 1) or 1)
                state.pop("pending_trigger", None)
                _be.save_state(session, state)
                pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
                ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
                game = self._fresh_game(session, pl_t, ai_t, state)
                # This RulesPort bridge handles Practice/PvE prompts, where
                # pending.owner_id is the raw game_cards.user_id, not the
                # ServicePlayer UID instance. The prompted controller is the
                # connected player, so use its already-canonical protocol UID.
                game.push_ability_cancelled(instance_id, pl_t)
                port = getattr(session, "_rules_port_session", None)
                event_sink = getattr(port, "event_sink", None)
                native_scheduler = port is not None and event_sink is not None
                if native_scheduler:
                    # The trigger prompt temporarily owns the client UI, but
                    # its answer must return through the native scheduler.
                    # In Practice the active AI is first in the response
                    # queue; let it pass before projecting the next human
                    # window instead of sending a GreenLight for a player
                    # that does not yet own native priority.
                    self._advance_rules_port_to_priority(
                        session, pl_t, ai_t, state)
                else:
                    if _be.stack_empty(state):
                        game.push_chain_empty()
                        phase = _be.current_phase(state)
                        game.push_green_light(
                            pl_t, self._priority_context_for(phase, state))
                    else:
                        game.push_green_light(
                            pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
                    self._send_battle_events(session, game, pl_t)
                self._push_transaction_ack(session)
                if (not native_scheduler and _be.stack_empty(state) and
                        _be.current_phase(state) in (
                            _hc.game_engine.ETurnPhases.FirstMainPhase,
                            _hc.game_engine.ETurnPhases.SecondMainPhase)):
                    self._push_main_phase_options(session, pl_t, ai_t)
                _hc.log_req(f"    Optional trigger declined: "
                        f"{pending.get('ability_guid', '')[:8]}")
                session._rules_port_mutation_emitted = True
                return True
            pending.pop("optional", None)
            _be.save_state(session, state)

        # Forward the whole decoded activation record (including the complete
        # TargetMap) so a triggered ability that advertises more than one
        # chosen target resolves with every authored index, not just the
        # primary slot.
        if activation:
            pending["activation_data"] = dict(activation)
            _be.save_state(session, state)

        found = []
        def walk(value):
            if isinstance(value, Mapping):
                for item in value.values():
                    walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)
            else:
                try:
                    number = int(value)
                    if number > 0 and (number & 0xFF) == 1:
                        found.append(number)
                except (TypeError, ValueError):
                    pass

        # Trigger pickers retain the authored target index, which can be
        # nonzero when earlier target slots are auto-filled (for example,
        # target 0 is "this" and the player selects target 1). Prefer that
        # slot so unrelated activation values cannot be mistaken for the
        # selected card.
        target_index = int(pending.get("target_index", 0) or 0)
        target_map = (activation.get("target_map", {})
                      if isinstance(activation, Mapping) else {})
        if isinstance(target_map, Mapping):
            selected = target_map.get(
                target_index, target_map.get(str(target_index)))
            walk(selected)
        if not found:
            walk(activation if activation else payload)
        if found:
            pending["selected_uid"] = found[-1]
            _be.save_state(session, state)
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        handled = self._resolve_pending_trigger_target(
            session, pl_t, ai_t, b"",
            ability_guid=pending.get("ability_guid"))
        if handled:
            session._rules_port_mutation_emitted = True
        return handled

    def _resolve_rules_port_discard_continuation(self, session, transaction):
        """Consume a typed RulesPort class-23 discard continuation."""
        import hconnect_server as _hc
        activation_data = transaction.payload.get("activation_data", {}) or {}
        target_map = (activation_data.get("target_map", {})
                      if isinstance(activation_data, Mapping) else {})

        def target_card_uids(values):
            if isinstance(values, Mapping):
                values = (values.get("session_card_ids") or
                          values.get("SessionCardIds") or ())
            if not isinstance(values, (list, tuple, set)):
                values = (values,) if values is not None else ()
            result = []
            for value in values:
                while isinstance(value, Mapping):
                    nested = next((value[key] for key in (
                        "uid64", "UID", "m_UID64", "value")
                        if key in value), None)
                    if nested is None or nested is value:
                        break
                    value = nested
                try:
                    uid = int(cast(Any, value))
                except (TypeError, ValueError):
                    continue
                if uid > 0 and (uid & 0xFF) == 1:
                    result.append(uid)
            return result

        _be = self._checkpoint_engine(session)
        state = _be.load_state(session)
        native_continuation = state.get("pending_discard_continuation")
        if native_continuation:
            from gamedata import DEFAULT_RECORD_STORE, ability_graph
            from rules_port.resolution import resolve_port_ability
            parent = dict(native_continuation)
            guid = str(parent.get("ability_guid") or "").lower()
            graph = ability_graph(DEFAULT_RECORD_STORE, guid)
            if graph is None:
                raise RuntimeError(
                    f"RulesPort discard continuation ability missing: {guid}")
            target_guid = str(
                state.get("pending_discard_target_template") or "").lower()
            target_index = next((index for index, target in enumerate(
                graph.targets) if str(target.guid).lower() == target_guid), None)
            if target_index is None:
                raise RuntimeError(
                    "RulesPort discard continuation target missing: " +
                    target_guid)

            # AbilityActivationData.TargetMap stores choices by the target's
            # metadata index. Do not recursively collect every card UID in the
            # envelope: that also sees SourceCardId and can mistake the
            # Stargazer itself (or another unrelated card) for the discard.
            selected = (target_map.get(target_index,
                                       target_map.get(str(target_index), ()))
                        if isinstance(target_map, Mapping) else ())
            selected_uids = target_card_uids(selected)

            from pvp_db import db_rules_port_hand_card
            hand_owner_id = (self.user_profile.get("id", 0)
                             if isinstance(self.user_profile, dict) else 0)

            def owned_hand_cards(uids):
                valid = []
                for uid in dict.fromkeys(uids):
                    row = db_rules_port_hand_card(
                        session.session_id, uid, hand_owner_id)
                    if row:
                        valid.append((uid, row))
                return valid

            valid_targets = owned_hand_cards(selected_uids)
            wire_uids = ()
            if not selected_uids:
                # Some Mono builds omit TargetMap from the decoded wrapper but
                # still serialize the selected SessionCardId in the original
                # SetAbilityActivationData payload. Recover only explicitly
                # labelled card UIDs, exclude the source, and accept the
                # fallback only when it names exactly one card in this
                # authenticated player's hand.
                original = getattr(
                    session, "_rules_port_dispatch_command", None)
                raw = getattr(original, "inner_bytes", b"")
                from rules_port.wire import extract_session_card_uids
                wire_uids = extract_session_card_uids(
                    raw, exclude=(parent.get("source_uid", 0),))
                valid_targets = owned_hand_cards(wire_uids)
            submitted_uids = selected_uids or list(wire_uids)
            if len(submitted_uids) != 1 or len(valid_targets) != 1:
                _hc.log_req(
                    "    RulesPort discard continuation rejected: "
                    f"target_index={target_index} selected={selected_uids} "
                    f"wire_cards={list(wire_uids)} "
                    f"valid_hand_cards={[uid for uid, _row in valid_targets]}")
                return False
            chosen_uid, _row = valid_targets[0]

            target_map = dict(parent.get("target_map") or {})
            target_map[target_index] = (chosen_uid,)
            state.pop("pending_discard_continuation", None)
            state.pop("pending_discard_continuation_instance_id", None)
            for key in ("pending_discard_ability", "pending_discard_ability_instance_id",
                        "pending_discard_target_template", "pending_discard_scid"):
                state.pop(key, None)
            state.pop("resolution_paused", None)
            state["rules_port_resume_effect_order"] = int(
                parent.get("resume_effect_order", 0) or 0)
            _be.save_state(session, state)
            pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
            game = self._fresh_game(session, pl_t, ai_t, state)
            result = resolve_port_ability(
                self, game, session, _hc._db, pl_t, ai_t, state, guid,
                parent.get("source_uid"), int(parent.get("owner_id", 0) or 0),
                target_map=target_map,
                variables=parent.get("variables") or {},
                resume_from_order=int(parent.get("resume_effect_order", 0) or 0),
                instance_id=int(native_continuation.get(
                    "instance_id", 1) or 1))
            from rules_port.resolution import \
                resume_ability_continuation_parents
            resume_ability_continuation_parents(
                self, game, session, _hc._db, pl_t, ai_t, state,
                native_continuation)
            _be.save_state(session, state)
            completed_chain = self._completed_rules_port_chain(session, state)
            if completed_chain is None and not state.get("resolution_paused"):
                game.push_green_light(
                    pl_t, _hc.game_engine.EPriorityContext.Normal)
            self._send_battle_events(session, game, pl_t)
            finished_chain = False
            if completed_chain is not None:
                finished_chain = self._resume_completed_rules_port_chain(
                    session, state, completed_chain)
            session._rules_port_mutation_emitted = True
            _hc.log_req(f"    RulesPort discard continuation: {chosen_uid} -> {result}")
            # The Deathcry that opened this picker fired during the AI's turn
            # (Bloatcap's "each opposing champion chooses and discards a
            # card"), which paused its phase loop so the picker survived.
            # Resume that turn where it stopped, exactly like an answered
            # deck-search prompt.
            ai_idx = state.get("ai_turn_phase_idx")
            if ai_idx is not None and not state.get("resolution_paused"):
                self._push_transaction_ack(session)
                state.pop("ai_turn_phase_idx", None)
                _be.save_state(session, state)
                _hc.log_req(f"    Discard answered — resuming AI turn at idx {ai_idx}")
                if getattr(session, "_rules_port_session", None) is not None:
                    # The continuation resolved the chain item directly, so
                    # its ResolveTopOfChainAction is still the native stack
                    # top.  The AI driver only runs from a native phase
                    # window ("phase N was not entered by RulesPort"), so
                    # tick the scheduler back to that window first; the
                    # projection then resumes the AI (or hands a newly
                    # queued trigger's window to the client).
                    self._advance_rules_port_to_priority(
                        session, pl_t, ai_t, state)
                else:
                    self._run_ai_turn(session, pl_t, ai_t, state,
                                      start_idx=int(ai_idx))
                return True
            # A picker continuation that finishes its chain item can expose
            # the next native chain item (a trigger the draw/discard raised,
            # e.g. Mysterious Rune's "when a resource enters your hand") or
            # end the paused phase.  Re-project the native priority window;
            # an empty acknowledgement left the client on a Normal pass
            # button while the trigger stayed on the chain.
            if not (finished_chain and not state.get("resolution_paused") and
                    self._advance_rules_port_to_priority(
                        session, pl_t, ai_t, state)):
                self._push_transaction_ack(session)
            return True
        if not state.get("pending_discard_ability"):
            return False
        legacy_uids = []
        if isinstance(target_map, Mapping):
            for selected in target_map.values():
                legacy_uids.extend(target_card_uids(selected))
        if not legacy_uids:
            original = getattr(
                session, "_rules_port_dispatch_command", None)
            from rules_port.wire import extract_session_card_uids
            legacy_uids = list(extract_session_card_uids(
                getattr(original, "inner_bytes", b""),
                exclude=(state.get("resolving_source_uid", 0),)))
        hand_owner_id = (self.user_profile.get("id", 0)
                         if isinstance(self.user_profile, dict) else 0)
        valid_legacy_uids = []
        from pvp_db import db_rules_port_hand_card
        for uid in dict.fromkeys(legacy_uids):
            if db_rules_port_hand_card(
                    session.session_id, uid, hand_owner_id):
                valid_legacy_uids.append(uid)
        if len(legacy_uids) == 1 and len(valid_legacy_uids) == 1:
            state["pending_discard_scid"] = valid_legacy_uids[0]
            _be.save_state(session, state)
        else:
            _hc.log_req(
                "    RulesPort legacy discard continuation rejected: "
                f"selected={legacy_uids} valid_hand_cards={valid_legacy_uids}")
            return False
        handled = self._handle_set_ability_data_transaction(
            session, type("_Continuation", (), {"inner_bytes": b""})())
        # The continuation resolver already emitted the authoritative card and
        # priority events.  Mark that fact so the outer RulesPort dispatcher
        # does not broadcast the same projection a second time.
        if handled:
            session._rules_port_mutation_emitted = True
        return handled

    def _resolve_pending_deck_search(self, session, pl_t, ai_t, inner_bytes,
                                     ability_guid=None):
        """Handle the player's card choice from a "search your deck" class-39
        prompt (e.g. Darkspire Priestess's Deathcry): move the chosen matching
        deck card into the controller's hand and re-grant priority.

        The client answers with a SetAbilityActivationDataTransaction that
        carries the deathcry's ability GUID — route it here BEFORE the manual
        troop / champion ability paths so it never activates a champion power
        instead (the bug that put Poca on the chain)."""
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        pend = bstate.get("pending_deck_search")
        if not pend:
            return False
        if ability_guid and str(pend.get("ability_guid", "")).lower() != str(ability_guid).lower():
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
        if pend.get("kind") in ("revealed_troop", "revealed_card"):
            return self._resolve_pending_revealed_choice(
                session, pl_t, ai_t, bstate, pend, chosen_uid)
        bstate.pop("pending_deck_search")
        if pend.get("kind") == "shard":
            # Shards of Fate ("Choose a Standard resource in your deck. Gain
            # the thresholds it provides."): the chosen shard STAYS in the
            # deck — only its threshold is gained.
            return self._resolve_pending_shard_choice(
                session, pl_t, ai_t, bstate, pend, chosen_uid, inner_bytes)
        if pend.get("kind") == "matching_target":
            return self._resolve_pending_matching_target(
                session, pl_t, ai_t, bstate, pend, chosen_uid)
        if chosen_uid and chosen_uid in pend["candidates"]:
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            if bstate.get("_rules_port_attached"):
                # A pending picker is a continuation of the original native
                # ability.  Keep the actual zone mutation in RulesPort; this
                # handler is only responsible for decoding the client's
                # choice and projecting the resulting events.
                from rules_port.context import EffectContext
                from rules_port.deck_effects import move_deck_card_to_hand
                native_context = EffectContext.from_rules_port(
                    g, session, _hc._db, self, pl_t, ai_t, bstate,
                    str(pend.get("ability_guid") or "").lower())
                move_deck_card_to_hand(
                    native_context, chosen_uid, pend["owner_id"])
            else:
                from abilities.framework.effects.search import move_deck_card_to_hand
                move_deck_card_to_hand(g, session, _hc._db, self, pl_t, ai_t,
                                       chosen_uid, pend["owner_id"], bstate)
            # Hide the unchosen candidates again (back to the face-down deck)
            # so the Choosing zone empties when the pick resolves.
            self._hide_candidates_to_deck(
                g, session, pl_t, ai_t,
                [cu for cu in pend["candidates"] if int(cu) != int(chosen_uid)])
            self._send_battle_events(session, g, pl_t)
            _hc.log_req(f"    Deck-search chosen: {hex(chosen_uid)} -> hand "
                    f"(owner {pend['owner_id']})")
            # Resume the turn that paused for this prompt (the deathcry fired
            # during combat).  The AI turn resumes where it stopped; a player
            # combat pause advances off AssignDamage; otherwise just re-grant
            # priority as a normal triggered prompt would.
            ai_idx = bstate.get("ai_turn_phase_idx")
            if ai_idx is not None:
                bstate.pop("ai_turn_phase_idx", None)
                _be.save_state(session, bstate)
                _hc.log_req(f"    Deck-search answered — resuming AI turn at idx {ai_idx}")
                self._run_ai_turn(session, pl_t, ai_t, bstate, start_idx=ai_idx)
            elif _be.current_phase(bstate) in (
                    _hc.game_engine.ETurnPhases.AssignDamage,
                    _hc.game_engine.ETurnPhases.AssignFirstStrikeDamage):
                _be.advance_phase(bstate)
                _be.save_state(session, bstate)
                self._advance_to_priority(session, pl_t, ai_t, bstate)
            else:
                g2 = self._fresh_game(session, pl_t, ai_t, bstate)
                g2.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
                self._send_battle_events(session, g2, pl_t)
                self._push_main_phase_options(session, pl_t, ai_t)
        else:
            _hc.log_req(f"    Deck-search answer invalid: "
                    f"chosen={hex(chosen_uid) if chosen_uid else 'none'} "
                    f"candidates={pend['candidates']}")
        _be.save_state(session, bstate)
        self._push_transaction_ack(session)
        return True

    def _resolve_pending_matching_target(self, session, pl_t, ai_t, bstate,
                                         pend, chosen_uid):
        """Resume a typed deck target that does not move the selected card.

        The class-39 picker temporarily presents legal deck cards in the
        client's Choosing collection, but the database cards never leave the
        deck.  Re-enter the selected child ability with its TargetMap so its
        StoreTargets/CreateToken BOM runs normally, then continue the parent
        activation (if the child was invoked from one).
        """
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)

        bstate.pop("resolution_paused", None)
        candidates = [int(uid) for uid in (pend.get("candidates") or [])]
        if not chosen_uid or int(chosen_uid) not in candidates:
            _hc.log_req(f"    Matching-target answer invalid: "
                    f"chosen={hex(int(chosen_uid)) if chosen_uid else 'none'} "
                    f"candidates={candidates}")
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            return True

        g = self._fresh_game(session, pl_t, ai_t, bstate)
        # All presented cards were only a client-side Choosing projection.
        # Return every one to the face-down deck before publishing the
        # generated copies.  No CardDrawn/CardMoved-to-Hand event is emitted.
        self._hide_candidates_to_deck(g, session, pl_t, ai_t, candidates)

        continuation = pend.get("continuation") or {}
        from rules_port.resolution import (
            resolve_port_ability,
            resume_ability_continuation_parents)
        child_guid = str(continuation.get("ability_guid") or "").lower()
        child_source = int(continuation.get("source_uid") or 0)
        child_owner = int(continuation.get("owner_id", pend.get("owner_id", 0)) or 0)
        child_targets = _hc._numeric_target_map(continuation.get("target_map"))
        child_targets[int(continuation.get("target_index", 0))] = \
            int(chosen_uid)
        resolve_port_ability(
            self, g, session, _hc._db, pl_t, ai_t, bstate,
            child_guid, child_source, child_owner,
            target_map=child_targets,
            variables=continuation.get("variables") or {},
            resume_from_order=int(
                continuation.get("resume_effect_order", 0) or 0),
            instance_id=int(continuation.get(
                "ability_instance_id", continuation.get("instance_id", 1))
                or 1))
        resume_ability_continuation_parents(
            self, g, session, _hc._db, pl_t, ai_t, bstate, continuation)
        _be.save_state(session, bstate)
        pending_input = (bstate.get("pending_choice") or
                         bstate.get("pending_trigger") or
                         bstate.get("pending_deck_search") or
                         bstate.get("pending_conversation"))
        if pending_input:
            self._send_battle_events(session, g, pl_t)
        elif bstate.get("ai_turn_phase_idx") is not None:
            resume_idx = int(bstate.pop("ai_turn_phase_idx"))
            _be.save_state(session, bstate)
            self._send_battle_events(session, g, pl_t)
            self._run_ai_turn(session, pl_t, ai_t, bstate,
                              start_idx=resume_idx)
        else:
            phase = _be.current_phase(bstate)
            if _be.stack_empty(bstate):
                g.push_chain_empty()
                if bstate.get("turn_player") == _be.PLAYER:
                    if phase in (_hc.game_engine.ETurnPhases.FirstMainPhase,
                                 _hc.game_engine.ETurnPhases.SecondMainPhase):
                        bstate["turn_phases"] = _be.build_turn_phases(bstate)
                        _be.save_state(session, bstate)
                    if phase is not None:
                        g.push_turn_phase(phase, pl_t, pl_t)
                    self._push_phase_options(session, pl_t, ai_t, phase)
                g.push_green_light(
                    pl_t, self._priority_context_for(phase, bstate))
            else:
                g.push_green_light(
                    pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
            self._send_battle_events(session, g, pl_t)
        self._push_transaction_ack(session)
        _hc.log_req(f"    Matching deck target chosen: {hex(int(chosen_uid))}; "
                f"created matching cards and resumed priority")
        return True

    def _resolve_pending_revealed_choice(self, session, pl_t, ai_t, bstate,
                                         pend, chosen_uid):
        """Finish a SourceRevealed choice using its child effect metadata."""
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        bstate.pop("pending_deck_search", None)
        bstate.pop("resolution_paused", None)
        candidates = [int(u) for u in (pend.get("candidates") or [])]
        revealed = [int(u) for u in (pend.get("revealed_cards") or [])]
        if chosen_uid and int(chosen_uid) in candidates:
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            native_continuation = pend.get("continuation")
            if native_continuation:
                # The reveal itself only changed the client projection.  Put
                # every presented card back behind the deck projection, then
                # let the typed MoveCardToZone effect consume the selected
                # target and continue the same RulesPort ability instance.
                self._hide_candidates_to_deck(
                    g, session, pl_t, ai_t, revealed)
                target_map = _hc._numeric_target_map(
                    native_continuation.get("target_map"))
                target_map[int(native_continuation.get("target_index", 0))] = \
                    int(chosen_uid)
                from rules_port.resolution import (
                    resolve_port_ability,
                    resume_ability_continuation_parents)
                bstate.pop("pending_deck_search", None)
                bstate.pop("resolution_paused", None)
                resolve_port_ability(
                    self, g, session, _hc._db, pl_t, ai_t, bstate,
                    str(native_continuation.get("ability_guid") or "").lower(),
                    int(native_continuation.get("source_uid") or 0),
                    int(native_continuation.get("owner_id", pend.get("owner_id", 0)) or 0),
                    target_map=target_map,
                    variables=native_continuation.get("variables") or {},
                    resume_from_order=int(native_continuation.get(
                        "resume_effect_order", 0)),
                    instance_id=int(native_continuation.get(
                        "ability_instance_id", pend.get("instance_id", 1))))
                resume_ability_continuation_parents(
                    self, g, session, _hc._db, pl_t, ai_t, bstate,
                    native_continuation)
                _be.save_state(session, bstate)
                self._send_battle_events(session, g, pl_t)
                completed = self._resume_completed_rules_port_chain(
                    session, bstate)
                if completed:
                    # Finishing the chain item can end the paused phase: a
                    # trigger resolved during Draw advances to First Main.
                    # Re-project the native priority window, or the client
                    # keeps the stale ResolveTopOfChain button and its next
                    # click passes the phase the player never saw.
                    if not self._advance_rules_port_to_priority(
                            session, pl_t, ai_t, bstate):
                        self._push_transaction_ack(session)
                else:
                    self._push_transaction_ack(session)
                return True
            destination = str(pend.get("move_destination") or "hand").lower()
            location = str(pend.get("move_location") or "").lower()
            if destination == "deck" and location == "top":
                from abilities.framework._shared import state_after_zone_exit
                chosen = int(chosen_uid)
                # Keep the card in the deck, but make it the next card drawn.
                # Shift existing positions first so the ordering is unambiguous
                # even when the revealed card was already at position zero.
                _hc.db_move_card_to_deck_top(
                    session.session_id, chosen, state_after_zone_exit(0),
                    conn=_hc._db)
                _hc._db.commit()
                row = _hc.db_card_zone_details(
                    session.session_id, chosen, conn=_hc._db)
                if row:
                    scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(chosen))
                    owner = pl_t if (row[2] or 0) != 0 else ai_t
                    _tpl, ct, _name, cost, atk, defense, _guid = \
                        self._card_full_data(g, scid, row[0], row[1])
                    g.push_card_updated(
                        scid, owner, _hc.game_engine.ECardCollections.Deck, ct,
                        template_id=row[0], cost=cost, attack=atk,
                        defense=defense, state=0, nulling=True)
                _hc.log_req(f"    Revealed choice: {hex(chosen)} -> deck top "
                        f"(owner {pend['owner_id']})")
            else:
                from abilities.framework.effects.search import move_deck_card_to_hand
                move_deck_card_to_hand(g, session, _hc._db, self, pl_t, ai_t,
                                       int(chosen_uid), pend["owner_id"], bstate)
                _hc.log_req(f"    Revealed choice: {hex(int(chosen_uid))} -> hand "
                        f"(owner {pend['owner_id']})")
            remaining = [cu for cu in revealed if cu != int(chosen_uid)]
            if remaining:
                from pvp_db import db_randomly_insert_deck_cards
                db_randomly_insert_deck_cards(
                    session.session_id, int(pend["owner_id"]), remaining)
            # Hide every other revealed card.  The cards were presented by
            # CardsRevealed, not moved in the database, so they all remain in
            # the deck until this continuation resolves.
            self._hide_candidates_to_deck(
                g, session, pl_t, ai_t,
                remaining)
            self._send_battle_events(session, g, pl_t)
        elif pend.get("optional"):
            # An optional SourceRevealed target may be declined.  Return the
            # revealed cards to the face-down deck without applying the child
            # MoveCardToZone effect.  The legacy client serializes a declined
            # picker using the source card ID rather than a zero target, so
            # accept any non-candidate value as the decline sentinel.
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            # The client-side authoritative move back to Deck with an
            # unknown location inserts the card at a random position.  Do the
            # same in the server DB before hiding it; leaving the old position
            # (often zero) makes a declined card remain visually on top and
            # can make a later draw disagree with what the client showed.
            from pvp_db import db_randomly_insert_deck_cards
            db_randomly_insert_deck_cards(
                session.session_id, int(pend["owner_id"]), revealed)
            self._hide_candidates_to_deck(g, session, pl_t, ai_t, revealed)
            self._send_battle_events(session, g, pl_t)
            _hc.log_req("    Revealed choice declined; returned cards to deck")
        else:
            _hc.log_req(f"    Revealed choice invalid: "
                    f"chosen={hex(int(chosen_uid)) if chosen_uid else 'none'} "
                    f"candidates={candidates}")
        _be.save_state(session, bstate)
        g2 = self._fresh_game(session, pl_t, ai_t, bstate)
        g2.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
        self._send_battle_events(session, g2, pl_t)
        self._push_main_phase_options(session, pl_t, ai_t)
        self._push_transaction_ack(session)
        return True

    def _resolve_pending_shard_choice(self, session, pl_t, ai_t, bstate, pend,
                                      chosen_uid, inner_bytes):
        """Resolve Shards of Fate's pick: gain the chosen Standard resource's
        threshold (the shard remains in the deck) and hide the unchosen
        candidates again."""
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        if chosen_uid and chosen_uid in pend["candidates"]:
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            from rules_port.resources import card_template_threshold_flags
            provided = card_template_threshold_flags(
                _hc._db, session.session_id, int(chosen_uid))
            threshold_colors = sorted({
                int(flag) for flag in _hc.game_engine.SHARD_TO_FLAG.values()
                if provided & int(flag)})
            from pvp_db import db_randomly_insert_deck_cards
            db_randomly_insert_deck_cards(
                session.session_id, int(pend["owner_id"]), pend["candidates"])
            if threshold_colors:
                th = bstate.setdefault("player_threshold", {})
                for flag in threshold_colors:
                    th[flag] = int(th.get(flag, th.get(str(flag), 0)) or 0) + 1
                _be.save_state(session, bstate)
                g.player_threshold = dict(th)
                for flag in threshold_colors:
                    ev_th = _hc.game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                    ev_th.player_id = pl_t
                    ev_th.color = flag
                    ev_th.operation = 1
                    ev_th.delta = 1
                    ev_th.new_value = th[flag]
                    g._push(ev_th)
                    from rules_port.triggers import dispatch_native_trigger
                    champion = getattr(self, "_player_champ_scid", None)
                    if champion is not None:
                        dispatch_native_trigger(
                            db=_hc._db, handler=self, game=g, session=session,
                            player_uid=pl_t, ai_uid=ai_t,
                            battle_state=bstate,
                            event_type="GainThresholdEvent",
                            source_card_id=int(champion.uid.uid64),
                            source_player_id=int(pend["owner_id"]),
                            data={"gain_threshold_color": int(flag)})
                g.push_player_updated(
                    pl_t, champ_id=getattr(self, "_player_champ_scid", None))
            # Hide EVERY candidate again (back to the face-down deck) — the
            # chosen shard stays in the deck, and all of them must be rendered
            # nullable so nothing is left face-up on top after the Choosing
            # window closes.
            self._hide_candidates_to_deck(g, session, pl_t, ai_t,
                                          pend["candidates"])
            self._send_battle_events(session, g, pl_t)
            _hc.log_req(f"    Shards of Fate: gained threshold flags "
                    f"{threshold_colors} "
                    f"(chosen {hex(int(chosen_uid))})")
        else:
            _hc.log_req(f"    Shards of Fate: invalid pick "
                    f"chosen={hex(chosen_uid) if chosen_uid else 'none'}")
        _be.save_state(session, bstate)
        # Re-grant the player priority + fresh main-phase options (the resource
        # play consumed the window; the pick resumes the turn).
        g2 = self._fresh_game(session, pl_t, ai_t, bstate)
        g2.push_green_light(pl_t, _hc.game_engine.EPriorityContext.Normal)
        self._send_battle_events(session, g2, pl_t)
        self._push_main_phase_options(session, pl_t, ai_t)
        self._push_transaction_ack(session)
        return True

    def _hide_candidates_to_deck(self, game, session, pl_t, ai_t, uids):
        """Move deck-search / Shards-of-Fate candidates from the client's
        Choosing zone back to the deck, rendered face-down (nullable).  A bare
        CardUpdated without the CardMoved leaves the client's cache showing the
        cards face-up on top of the deck."""
        import hconnect_server as _hc
        from abilities.framework._shared import state_after_zone_exit
        for cu in uids:
            c_scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(cu)))
            c_row = _hc.db_card_zone_details(
                session.session_id, int(cu), conn=_hc._db)
            if not c_row:
                continue
            _hc.db_move_card_to_deck(
                session.session_id, int(cu), state_after_zone_exit(0),
                conn=_hc._db)
            owner = pl_t if (c_row[2] or 0) != 0 else ai_t
            _tpl2, ct2, _n2, _c2, _a2, _d2, _g2 = self._card_full_data(
                game, c_scid, c_row[0], c_row[1])
            game.push_card_updated(c_scid, owner,
                                   _hc.game_engine.ECardCollections.Deck, ct2,
                                   template_id=c_row[0], cost=_c2, attack=_a2,
                                   defense=_d2, state=0, nulling=True)
