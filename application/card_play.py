"""Card play planning: costs, targets, troop abilities and the main-phase options built from them."""

# Sentinel for 'no persisted gem'; compared by identity, so it is shared with hconnect_server.
_NO_PERSISTED_GEM = object()

import json
import sqlite3
import struct


class CardPlayMixin:
    """Methods of HCPHandler, mixed into it (see hconnect_server)."""

    def _extract_enum_int(self, inner_bytes, field):
        """Extract the integer value of an enum field from 3029 inner_bytes.

        Enums serialize as `field;...;value__;<idx>;<size>;<type>;0;<hex>;`.
        """
        idx = inner_bytes.find(field.encode())
        if idx < 0:
            return None
        v_idx = inner_bytes.find(b"value__", idx)
        if v_idx < 0:
            return None
        rest = inner_bytes[v_idx + len(b"value__") + 1:]
        parts = rest.split(b";", 6)
        if len(parts) >= 4:
            try:
                return struct.unpack('<I', bytes.fromhex(parts[3].decode("ascii", errors="replace")))[0]
            except Exception:
                pass
        return None

    def _extract_int32_field(self, inner_bytes, field):
        """Extract a plain System.Int32 field from 3029 inner_bytes.

        Int32s serialize as `field;<idx>;<type>;0;<8-hex-le>;` (see
        objfmt_builder.field_int).  Used for the client's
        AbilityActivationData.xCostData.m_ResourceXCost — the X the player
        chose in the X-cost dialog when playing a variable-cost spell.
        """
        idx = inner_bytes.find(field.encode())
        if idx < 0:
            return None
        rest = inner_bytes[idx + len(field) + 1:]
        parts = rest.split(b";", 4)
        if len(parts) >= 4:
            try:
                return struct.unpack('<i', bytes.fromhex(parts[3].decode("ascii", errors="replace")))[0]
            except Exception:
                pass
        return None

    def _extract_enum_list(self, inner_bytes, field, next_field):
        """Best-effort parse of a List<ETurnPhases> field from 3029 inner_bytes."""
        import re as _re
        idx = inner_bytes.find(field.encode())
        if idx < 0:
            return None
        end = inner_bytes.find(next_field.encode(), idx) if next_field else len(inner_bytes)
        seg = inner_bytes[idx:end]
        vals = []
        for m in _re.finditer(rb"value__;\d+;\d+;\d+;([0-9A-Fa-f]{8});", seg):
            try:
                vals.append(struct.unpack('<I', bytes.fromhex(m.group(1).decode("ascii")))[0])
            except Exception:
                pass
        return vals

    def _card_troop_requirements(self, ability_guids):
        """Set of troop-target requirements for a card's abilities, driven by
        the gamedata target-template FILTER (not card text): {'friendly',
        'enemy','any'}.  Champion (PlayerTargetTemplate / IsHero filter)
        targets always exist and never gate playability."""
        import hconnect_server as _hc
        from pvp_db import db_ability_meta_targets
        reqs = set()
        for ag in (ability_guids or []):
            meta = db_ability_meta_targets(ag)
            if meta and meta[4]:  # is_manual — warzone activation, skip
                continue
            if not meta or not meta[0]:
                continue
            try:
                tpl_ids = json.loads(meta[0])
            except Exception:
                continue
            for tid in tpl_ids:
                trow = _hc.db_target_template_definition(tid, conn=_hc._db)
                if not trow:
                    continue
                kind = trow[1] or ""
                fj = trow[0] or "{}"
                if kind == "PlayerTargetTemplate":
                    continue  # auto champion target — always available
                if "IsHero" in fj:
                    continue  # champion targets always exist in play
                if "IsTroop" not in fj:
                    continue
                # A target such as Prophecy's "the next troop in your deck"
                # is an automatic deck search, not a requirement for a troop
                # already on the battlefield.  Only a Warzone-only target
                # should make a card unplayable when the board is empty.
                zones = {z.strip().lower() for z in (trow[2] or "").split("|")
                         if z.strip()}
                if zones != {"warzone"}:
                    continue
                if "IsControlledBy" in fj and "IsNotControlledBy" not in fj:
                    reqs.add("friendly")
                elif "IsNotControlledBy" in fj:
                    reqs.add("enemy")
                else:
                    reqs.add("any")
        return reqs

    def _card_target_requirements_met(self, session, play_plan,
                                      battle_state=None):
        """A hand card is only playable when every EXPLICIT target template of
        its non-manual abilities has at least one legal candidate.  This is
        what makes Countermagic ("Interrupt target card" — CollectionFlags
        CastSpells) unplayable with nothing on the chain: its only legal
        targets are cards in CastSpells, so with an empty chain the card is
        not offered.  Auto targets and player-champion targets never gate.
        """
        import hconnect_server as _hc
        # Choice cards such as Engulf have a wrapper followed by alternative
        # child abilities.  Only one branch must have a legal target; the
        # client does not require unavailable alternatives to be playable.
        choice_card = any(
            int(getattr(ability.graph, "ability_index", -1)) < 0 and
            str(getattr(ability.graph, "game_text", "")).strip().lower()
            in {"choose one:", "choose one"}
            for ability in play_plan.abilities if ability.graph is not None)
        requirements = []
        for ability in play_plan.abilities:
            if (ability.graph is None or ability.graph.manual or
                    ability.is_triggered):
                continue
            has_explicit_target = False
            targets_available = True
            from rules_port.targeting import legal_targets_for
            graph = ability.graph
            for index in ability.referenced_target_indexes:
                if index < 0 or index >= len(graph.targets):
                    continue
                target = graph.targets[index]
                if not target.requires_input or target.minimum < 1:
                    continue
                has_explicit_target = True
                candidates = legal_targets_for(
                    _hc._db, session.session_id, self.user_profile["id"], target,
                    # Playability is asking whether the card can be cast from
                    # hand.  The source is not yet in a gameplay collection;
                    # preserve the pre-port target query's source-less
                    # semantics while still using RulesPort filtering.
                    0, both_players=True,
                    champions=self._champion_targets(),
                    battle_state=battle_state)
                if not candidates:
                    targets_available = False
            if has_explicit_target:
                requirements.append(targets_available)
        if requirements and ((choice_card and not any(requirements)) or
                             (not choice_card and not all(requirements))):
            _hc.log_req("    Playability: no legal target for required card ability")
            return False
        return True

    def _warzone_troop_count(self, session, user_id):
        import hconnect_server as _hc
        return _hc.db_warzone_troop_count(session.session_id, user_id)

    def _card_play_plan(self, template_guid, card_uid=0, owner_id=None,
                        session=None):
        """Return the Records-backed plan for this live card instance.

        The current Records snapshot is the only supported card-data source.
        The instance ability list is included because permanent grants can add
        abilities to cards in hand (for example, Graven Geist grants Scrounge).
        A missing record is invalid card data, not a request to use the old
        database-materialized rules path.
        """
        if not template_guid:
            return None
        try:
            source_uid = int(getattr(card_uid, "uid64", card_uid) or 0)
        except (TypeError, ValueError):
            source_uid = 0
        profile = self.user_profile if isinstance(self.user_profile, dict) else {}
        resolved_owner_id = int(
            owner_id if owner_id is not None else profile.get("id", 0) or 0)
        instance_ability_guids = tuple(
            self._card_ability_list(session, source_uid)
            if session is not None and source_uid else ())
        store = getattr(self, "_play_plan_store", None)
        if store is None:
            from gamedata import DEFAULT_RECORD_STORE
            store = DEFAULT_RECORD_STORE
            self._play_plan_store = store
        cache = getattr(self, "_play_plan_cache", None)
        if cache is None:
            cache = {}
            self._play_plan_cache = cache
        key = (str(template_guid).lower(), source_uid, resolved_owner_id,
               instance_ability_guids)
        if key not in cache:
            from gamedata import PlayPlan
            try:
                cache[key] = PlayPlan.from_card(
                    store, key[0], source_uid=source_uid or None,
                    owner_id=resolved_owner_id,
                    instance_ability_guids=instance_ability_guids)
            except KeyError as exc:
                raise RuntimeError(
                    f"card template {key[0]} is missing from the current Records "
                    "snapshot") from exc
        return cache[key]

    def _plan_target_bounds(self, template_guid):
        """Read min/max directly from the current target record."""
        store = getattr(self, "_play_plan_store", None)
        if store is None:
            from gamedata import DEFAULT_RECORD_STORE
            store = DEFAULT_RECORD_STORE
            self._play_plan_store = store
        target = store.get("AbilityTargetTemplate", template_guid)
        if target is None:
            return 1, 1
        minimum = max(0, int(target.minimum))
        maximum = int(target.maximum)
        if maximum <= 0:
            maximum = max(1, minimum)
        return minimum, maximum

    def _valid_targets_for_template(self, session, pl_t, ai_t, tid,
                                    battle_state=None):
        """Compute the valid SessionCardId targets for an AbilityTargetTemplate,
        driven by the gamedata target template (kind + card filter): champions
        come from the filter's IsHero, troops from the filter, and
        PlayerTargetTemplate ('You') resolves to the controller's champion."""
        import hconnect_server as _hc
        from rules_port.targeting import legal_targets
        store = getattr(self, "_play_plan_store", None)
        if store is None:
            from gamedata import DEFAULT_RECORD_STORE
            store = DEFAULT_RECORD_STORE
            self._play_plan_store = store
        target = store.get("AbilityTargetTemplate", str(tid).lower())
        if target is None:
            return None
        spec = target.target_spec
        kind = spec.target_kind
        if spec.is_auto:
            # Auto targets (e.g. a shard's 'You') resolve automatically server-
            # side — never attach a hand-play target picker for them.
            return None
        if kind == "PlayerTargetTemplate":
            champ = getattr(self, "_player_champ_scid", None)
            return [champ] if champ else []
        # RecordObject filters can implement a zero-length protocol object and
        # therefore evaluate false even when a real filter is present.  Test
        # for None explicitly; otherwise every metadata-targeted troop spell
        # is incorrectly treated as having no legal target.
        if spec.card_filter is None:
            return None
        if battle_state is None:
            battle_state = getattr(self, "_current_bstate", None)
        candidates = legal_targets(
            _hc._db, session.session_id, self.user_profile["id"], tid, 0,
            both_players=True, champions=self._champion_targets(),
            battle_state=battle_state)
        return [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(u)))
                for u in candidates]

    def _play_ability_targets(self, session, play_plan, battle_state=None):
        """For a hand card's abilities return [(ability_guid, index, template_id,
        [SessionCardId])] for each targeting template with computable targets."""
        result = []
        for ability in play_plan.abilities:
            graph = ability.graph
            if graph is None or graph.manual or ability.is_triggered:
                continue
            for index in ability.referenced_target_indexes:
                if index >= len(graph.targets):
                    continue
                target = graph.targets[index]
                if not target.requires_input:
                    continue
                targets = self._valid_targets_for_template(
                    session, None, None, target.guid,
                    battle_state=battle_state)
                if targets is not None and targets:
                    result.append((ability.ability_guid, index, target.guid,
                                   targets))
        return result

    @staticmethod
    def _play_target_bounds(play_plan, ability_guid, index):
        """Return the authored (minimum, maximum) for one target template.

        The client reads min/max by the position of the template in the
        ability's TargetTemplateIds list, so each TargetInstance added to one
        OptionInstance must carry its authored bounds.
        """
        minimum, maximum = 1, 1
        for ability in play_plan.abilities:
            if ability.ability_guid != ability_guid or index >= len(ability.targets):
                continue
            target = ability.targets[index]
            minimum = max(0, target.minimum)
            maximum = max(minimum, target.maximum or minimum or 1)
            break
        return minimum, maximum

    def _add_play_target_options(self, game, session, pl_t, ai_t):
        """Attach targeting TargetInstances to the most recent PlayerOptionList.

        The client's play-card flow (BattleStatePlayCard.NextStep) only opens the
        target picker for a played card's ability when
        State.CanUseAbility(cardId, abilityId) is true, i.e. the ability has an
        OptionInstance + TargetInstance in PlayerOptions.m_Targets. Without this,
        a targeted spell (e.g. Bravery "+1/+1 target troop") is played with no
        target and no effect."""
        import hconnect_server as _hc
        if not game.events:
            return
        last_ev = game.events[-1]
        if not isinstance(last_ev, _hc.game_engine.PlayerOptionListSessionEventArgs):
            return
        for opt in last_ev.options:
            card_uid = opt.card.uid.uid64
            card_details = _hc.db_card_zone_details(
                session.session_id, card_uid, conn=_hc._db)
            if not card_details:
                continue
            play_plan = self._card_play_plan(card_details[0], card_uid,
                                             self.user_profile["id"]
                                             if self.user_profile else 0,
                                             session=session)
            if play_plan is None:
                continue
            # PlayerOptions.Update keys target instances by (card, ability) and
            # CLEARS the stored list each time it sees another OptionInstance
            # with the same opt_id (PlayerOptions.cs:117-129).  Emitting one
            # OptionInstance per target template therefore drops every target
            # but the last: Survival of the Fittest ("Target troop you control
            # battles target opposing troop") prompted only for the opposing
            # troop, so the faction TargetMap was missing index 0 and the
            # pending battle never resolved.  Group every template of one
            # ability into a single OptionInstance, exactly like the manual
            # activation path above and the client's authored
            # AbilityTargetTemplateIds order.
            grouped: dict[str, dict[str, list]] = {}
            for ag, idx, tid, targets in self._play_ability_targets(
                    session, play_plan,
                    battle_state=getattr(self, "_current_bstate", None)):
                grouped.setdefault(ag, {"targets": [], "costs": []})[
                    "targets"].append((idx, tid, targets))
            from rules_port.costs import (
                ability_cost_targets, card_cost_targets, cost_type_for_kind)
            owner_id = self.user_profile["id"] if self.user_profile else 0
            for ability in play_plan.cast_abilities:
                graph = ability.graph
                if graph is None or not graph.additional_cost_targets:
                    continue
                costs = ability_cost_targets(
                    graph, _hc._db, session.session_id, owner_id, card_uid,
                    champions=self._champion_targets(),
                    battle_state=getattr(self, "_current_bstate", None))
                # The client only opens an optional/additional-cost picker
                # when every required part of that ability payment is
                # currently affordable.
                if any(not cost.is_source_auto_target and
                       len(cost.candidates) < cost.minimum
                       for cost in costs):
                    continue
                if costs:
                    grouped.setdefault(
                        ability.ability_guid.lower(),
                        {"targets": [], "costs": []})["costs"].extend(costs)
            for ag, group in grouped.items():
                # Troop abilities are NOT playable from hand.  Only the
                # card itself may be played (cost/threshold gate).
                # Manual abilities activate from the warzone, not hand.
                inst = game._make_event(_hc.game_engine.OptionInstanceSessionEventArgs)
                inst.opt_id = _hc.game_engine.ResourceId.from_str(ag)
                entries = group["targets"]
                for idx, tid, targets in entries:
                    min_count, max_count = self._play_target_bounds(
                        play_plan, ag, idx)
                    inst.min_target_counts.append(min_count)
                    inst.max_target_counts.append(max_count)
                    inst.target_ids.append(_hc.game_engine.ResourceId.from_str(tid))
                    tgt = game._make_event(_hc.game_engine.TargetInstanceSessionEventArgs)
                    tgt.target_index = idx
                    tgt.target_id = _hc.game_engine.ResourceId.from_str(tid)
                    tgt.targets = list(targets)
                    inst.target_instances.append(tgt)
                for cost in group["costs"]:
                    if cost.is_source_auto_target or not cost.candidates:
                        continue
                    minimum = cost.minimum
                    maximum = (len(cost.candidates) if cost.maximum < 0
                               else cost.maximum)
                    ci = game._make_event(
                        _hc.game_engine.CostInstanceSessionEventArgs)
                    _hc.HCPHandler._set_cost_instance_bounds(
                        ci, minimum, maximum)
                    ci.cost_type = cost_type_for_kind(cost.kind)
                    ci.target_template_id = _hc.game_engine.ResourceId.from_str(
                        cost.guid)
                    ci.targets = [_hc.game_engine.SessionCardId(
                        _hc.game_engine.UID(int(uid))) for uid in cost.candidates]
                    inst.target_instances.append(ci)
                opt.instances.append(inst)
                # Also attach the picker to the PlayCard option itself so the
                # client's CanUseAbility finds the target whether it checks the
                # card's ability or the built-in PlayCard ability (the play-card
                # flow keys on BuiltInResources.PlayCardAbilityTemplateId).
                for inst2 in opt.instances:
                    if str(inst2.opt_id.guid) != _hc.game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID:
                        continue
                    for idx, tid, targets in entries:
                        min_count, max_count = self._play_target_bounds(
                            play_plan, ag, idx)
                        inst2.target_ids.append(
                            _hc.game_engine.ResourceId.from_str(tid))
                        inst2.min_target_counts.append(min_count)
                        inst2.max_target_counts.append(max_count)
                        tgt2 = game._make_event(
                            _hc.game_engine.TargetInstanceSessionEventArgs)
                        tgt2.target_index = idx
                        tgt2.target_id = _hc.game_engine.ResourceId.from_str(tid)
                        tgt2.targets = list(targets)
                        inst2.target_instances.append(tgt2)
                    break
            # Additional-cost sacrifice (e.g. Abominate "sacrifice a troop you
            # control"): attach a SacrificeAbilityCostType CostInstance to the
            # PlayCard instance so the client's BattleStateAssignXCost prompts for
            # the sacrificed troop BEFORE the effect target. Without it the client
            # skips the cost and only asks for the single effect target.
            cost_candidates = card_cost_targets(
                play_plan, _hc._db, session.session_id,
                self.user_profile["id"] if self.user_profile else 0,
                card_uid, champions=self._champion_targets(),
                battle_state=getattr(self, "_current_bstate", None))
            for cost in cost_candidates:
                if cost.is_source_auto_target:
                    continue
                cost_guid = cost.guid
                cost_uids = cost.candidates
                if not cost_uids:
                    continue
                minimum = cost.minimum
                maximum = cost.maximum
                if maximum < 0:
                    maximum = len(cost_uids)
                # Find the PlayCard instance — its opt_id MUST match the
                # client's built-in PlayCard ability, or it cannot see this
                # CostInstance.
                for inst in opt.instances:
                    if (str(inst.opt_id.guid) ==
                            _hc.game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID):
                        ci = game._make_event(
                            _hc.game_engine.CostInstanceSessionEventArgs)
                        _hc.HCPHandler._set_cost_instance_bounds(
                            ci, minimum, maximum)
                        ci.cost_type = cost_type_for_kind(cost.kind)
                        ci.target_template_id = _hc.game_engine.ResourceId.from_str(
                            cost_guid)
                        ci.targets = [_hc.game_engine.SessionCardId(
                            _hc.game_engine.UID(int(uid))) for uid in cost_uids]
                        inst.target_instances.append(ci)
                        break
            # Variable X cost (e.g. Burn to the Ground "Deal X damage"): attach
            # an XCostAbilityCostType CostInstance to the PlayCard instance so
            # the client's BattleStateAssignXCost pushes the X slider
            # (BattleStateResourceXCost).  Without this the cost list is empty
            # and the dialog auto-commits at X=0 without ever showing.
            has_x_cost = play_plan.cost.variable
            if has_x_cost:
                for inst in opt.instances:
                    if str(inst.opt_id.guid) == _hc.game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID:
                        ci = game._make_event(_hc.game_engine.CostInstanceSessionEventArgs)
                        minimum = play_plan.cost.variable_minimum
                        _hc.HCPHandler._set_cost_instance_bounds(ci, minimum, 0)
                        ci.cost_type = 256  # EAbilityCostType.XCostAbilityCostType
                        ci.target_template_id = _hc.game_engine.ResourceId.invalid()
                        ci.targets = []
                        inst.target_instances.append(ci)
                        break

    def _cost_targets_available(self, session, tid, count):
        """True if the player controls at least `count` cards that can pay a
        cost targeting template `tid` (e.g. Abominate's "a troop you control").
        Artifacts/other kinds are resolved from the template's game text."""
        import hconnect_server as _hc
        from pvp_db import db_target_template_text, db_warzone_troop_count
        text = db_target_template_text(tid)
        if "artifact" in text.lower():
            n = _hc.db_zone_card_count(
                session.session_id, self.user_profile["id"], "warzone",
                "Artifact", conn=_hc._db)
        else:
            n = db_warzone_troop_count(session.session_id, self.user_profile["id"])
        return n >= count

    def _card_play_cost_selections(self, session, play_plan, source_uid,
                                   selected_uids, client_cost_map=None):
        """Bind a card-play TargetMap to its authored CostInstances.

        ``client_cost_map`` carries the decoder's already-separated cost
        selections (XCostData.CardsToSacrifice -> ability_data
        cost_target_map).  Client picks are validated and used as-is; the
        TargetMap positional/best-effort fallback only applies to costs the
        client did not name.
        """
        import hconnect_server as _hc
        from rules_port.costs import (
            card_cost_targets, cost_type_for_kind,
            validate_cost_target_selection,
        )
        selected_uids = [int(uid) for uid in (selected_uids or [])]
        named_costs = {
            int(index): [int(uid) for uid in selected]
            for index, selected in (client_cost_map or {}).items()
            if selected
        }
        used = set()
        selections = []
        if not play_plan.cost_instances:
            return selections, used
        def trace(reason, cost, available):
            # Rejection-only trace: selected names the client's TargetMap,
            # candidates the legal cost pool, so a refused play can be
            # replayed without the raw ObjFmt envelope.
            _hc.log_req(f"    Card cost selection reject: "
                    f"card={_hc.card_label(session.session_id, source_uid)} "
                    f"kind={cost.kind} guid={cost.guid} "
                    f"reason={reason} "
                    f"selected={[_hc.card_label(session.session_id, uid) for uid in selected_uids]} "
                    f"available={[_hc.card_label(session.session_id, uid) for uid in available]} "
                    f"candidates({len(cost.candidates)})="
                    f"{[_hc.card_label(session.session_id, uid) for uid in list(cost.candidates)[:8]]} "
                    f"minimum={cost.minimum} maximum={cost.maximum} "
                    f"source_auto={cost.is_source_auto_target} "
                    f"best_effort={cost.allow_best_effort_minimum}")

        for cost in card_cost_targets(
                play_plan, _hc._db, session.session_id,
                self.user_profile["id"] if self.user_profile else 0,
                source_uid, champions=self._champion_targets(),
                battle_state=getattr(self, "_current_bstate", None)):
            spec = {"kind": cost.kind, "target_guid": cost.guid,
                    "index": int(cost.index),
                    "cost_type": cost_type_for_kind(cost.kind),
                    "minimum": cost.minimum, "maximum": cost.maximum,
                    "auto": cost.is_source_auto_target,
                    "allow_best_effort_minimum":
                        cost.allow_best_effort_minimum}
            candidates = cost.candidates
            if cost.is_source_auto_target:
                selections.append((spec, candidates))
                continue
            if not candidates:
                trace("no-candidates", cost, ())
                return None
            named = named_costs.get(int(cost.index))
            if named:
                # The client named this cost's payment (XCostData
                # CardsToSacrifice).  Never steal an effect target to pay it.
                chosen = validate_cost_target_selection(
                    _hc._db, session.session_id,
                    self.user_profile["id"] if self.user_profile else 0,
                    source_uid, cost, tuple(named),
                    champions=self._champion_targets(),
                    battle_state=getattr(self, "_current_bstate", None))
                if chosen is None:
                    trace("client-selection-invalid", cost, tuple(named))
                    return None
                used.update(chosen)
                selections.append((spec, chosen))
                continue
            candidate_set = {int(uid) for uid in candidates}
            available = [uid for uid in selected_uids
                         if uid in candidate_set and uid not in used]
            minimum = cost.minimum
            maximum = cost.maximum
            if maximum < 0:
                maximum = len(available)
            if len(available) < minimum:
                # Older Mono clients sometimes omit the CostInstance's
                # TargetMap entry for a sacrifice while still submitting the
                # effect target.  Authored optional/best-effort costs may be
                # completed server-side from the same legal candidate set;
                # never consume the effect target when selecting that fallback.
                if (int(spec.get("cost_type", 0)) == 2 and
                        spec.get("allow_best_effort_minimum", False)):
                    available = [uid for uid in candidates
                                 if uid not in used and uid not in selected_uids]
                if len(available) < minimum:
                    trace("selected<minimum", cost, available)
                    return None
            chosen = tuple(available[:maximum])
            chosen = validate_cost_target_selection(
                _hc._db, session.session_id,
                self.user_profile["id"] if self.user_profile else 0,
                source_uid, cost, chosen,
                champions=self._champion_targets(),
                battle_state=getattr(self, "_current_bstate", None))
            if chosen is None:
                trace(f"validate-rejected({[hex(int(uid)) for uid in available[:maximum]]})",
                      cost, available)
                return None
            used.update(chosen)
            selections.append((spec, chosen))
        return selections, used

    def _ability_card_play_cost_selections(
            self, session, play_plan, source_uid, activations, *,
            owner_id=None, champions=None, battle_state=None):
        """Validate additional costs authored on a cast ability graph."""
        import hconnect_server as _hc
        from rules_port.costs import (
            ability_cost_targets, cost_type_for_kind,
            validate_cost_target_selection,
        )
        owner_id = (int(owner_id) if owner_id is not None else
                    int(self.user_profile["id"])
                    if self.user_profile else 0)
        champions = (self._champion_targets() if champions is None
                     else champions)
        battle_state = (getattr(self, "_current_bstate", None)
                        if battle_state is None else battle_state)
        activation_map = {str(key).lower(): value
                          for key, value in (activations or {}).items()}
        selections = []
        used = set()
        for ability in play_plan.cast_abilities:
            graph = ability.graph
            if graph is None or not graph.additional_cost_targets:
                continue
            activation = activation_map.get(ability.ability_guid.lower())
            if activation is None:
                activation = ability.activation
            cost_map = getattr(activation, "cost_target_map", {}) or {}
            if (ability.is_scrounge and not getattr(activation, "opted", False)
                    and not cost_map):
                # Declining optional Scrounge is a valid card play and has no
                # payment rows to apply.
                continue
            for cost in ability_cost_targets(
                    graph, _hc._db, session.session_id, owner_id, source_uid,
                    champions=champions, battle_state=battle_state):
                selected = cost_map.get(cost.index,
                                        cost_map.get(str(cost.index), ()))
                if cost.is_source_auto_target and not selected:
                    selected = (() if source_uid is None else (int(source_uid),))
                chosen = validate_cost_target_selection(
                    _hc._db, session.session_id, owner_id, source_uid, cost,
                    selected, champions=champions,
                    battle_state=battle_state)
                if chosen is None:
                    return None
                spec = {
                    "kind": cost.kind,
                    "target_guid": cost.guid,
                    "index": int(cost.index),
                    "cost_type": cost_type_for_kind(cost.kind),
                    "minimum": cost.minimum,
                    "maximum": cost.maximum,
                    "auto": cost.is_source_auto_target,
                    "allow_best_effort_minimum":
                        cost.allow_best_effort_minimum,
                    "ability_guid": ability.ability_guid.lower(),
                }
                selections.append((spec, chosen))
                used.update(chosen)
        return selections, used

    def _card_costs_valid(self, session, selections, expected_owner_id=None):
        """Validate card costs before any resource/effect mutation."""
        import hconnect_server as _hc
        for spec, selected in selections or ():
            if spec.get("kind") != "sacrifice":
                continue
            for uid in selected:
                row = _hc.db_card_sacrifice_info(
                    session.session_id, int(uid), conn=_hc._db)
                if not row:
                    return False
                owner_id, location, card_type = row
                if (str(location or "").lower() != "warzone" or
                        "troop" not in str(card_type or "").lower()):
                    return False
                if (expected_owner_id is not None and
                        int(owner_id or 0) != int(expected_owner_id)):
                    return False
        return True

    def _apply_card_play_costs(self, game, session, bstate, pl_t, ai_t,
                               selections, source_uid=None, scrounge=False,
                               expected_owner_id=None,
                               defer_voided_cards=False):
        """Apply card-level CostInstances before a card enters the chain."""
        import hconnect_server as _hc
        from pvp_db import db_discard_card, db_randomly_insert_deck_cards

        if defer_voided_cards:
            # Card-play void costs are carried by the pending chain item.
            # Clear a leftover flat list from older resolutions before any
            # event trigger can observe it.
            lists = bstate.get("ability_lists")
            if isinstance(lists, dict):
                lists.pop("VoidedCards", None)
                if not lists:
                    bstate.pop("ability_lists", None)

        def emit_trigger(event_type, source_uid, owner_id=None, **data):
            from rules_port.triggers import dispatch_native_trigger
            return dispatch_native_trigger(
                db=_hc._db, handler=self, game=game, session=session,
                player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                event_type=event_type, source_card_id=source_uid,
                source_player_id=owner_id,
                target_card_id=data.pop("target_card_id", None),
                data=data)

        def push_zone(uid, owner_id, location):
            row = _hc.db_card_zone_projection(
                session.session_id, int(uid), conn=_hc._db)
            if not row:
                return
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(uid)))
            self._card_full_data(game, scid, row[0])
            from abilities.framework._shared import owner_uid
            owner = owner_uid(owner_id, pl_t, ai_t, bstate)
            collection = {
                "hand": _hc.game_engine.ECardCollections.Hand,
                "deck": _hc.game_engine.ECardCollections.Deck,
                "discard": _hc.game_engine.ECardCollections.Discard,
                "void": _hc.game_engine.ECardCollections.Void,
                "warzone": _hc.game_engine.ECardCollections.Warzone,
            }.get(location, _hc.game_engine.ECardCollections.Warzone)
            cdef = game.card_defs.get(scid)
            game.push_card_updated(
                scid, owner, collection, _hc.game_engine.card_type_from_db(row[1]),
                template_id=row[0], state=int(row[2] or 0),
                cost=cdef.cost if cdef else 0,
                attack=cdef.attack if cdef else 0,
                defense=cdef.defense if cdef else 0,
                nulling=location == "deck")

        voided_uids = []
        cost_list_names = {
            "sacrifice": "SacrificedCards",
            "discard": "DiscardedCards",
            "void": "VoidedCards",
            "put_into_deck": "CardsPutIntoDeck",
            "shuffle_into_deck": "CardsPutIntoDeck",
            "put_into_hand": "CardsPutIntoHand",
        }

        def record_cost_card(kind, uid):
            list_name = cost_list_names.get(str(kind).lower())
            if not list_name:
                return
            # Card-play costs resolve later from a chain descriptor. Keep
            # these voided-card results with that descriptor instead of in
            # the session-wide list, where another response or later Scrounge
            # could accidentally reuse them.
            if defer_voided_cards and list_name == "VoidedCards":
                return
            values = bstate.setdefault("ability_lists", {}).setdefault(
                list_name, [])
            values.append(int(uid))

        source_owner_id = expected_owner_id
        if source_owner_id is None and source_uid is not None:
            source_owner = _hc.db_card_owner_id(
                session.session_id, int(source_uid), conn=_hc._db)
            if source_owner is not None:
                source_owner_id = int(source_owner)

        if not self._card_costs_valid(session, selections, source_owner_id):
            _hc.log_req(f"    Card cost rejected before mutation: owner={source_owner_id}")
            return False

        for spec, selected in selections:
            kind = spec["kind"]
            for uid in selected:
                uid = int(uid)
                row = _hc.db_card_owner_zone_state(
                    session.session_id, uid, conn=_hc._db)
                if not row:
                    continue
                owner_id = int(row[0] or 0)
                if kind == "sacrifice":
                    if not self._sacrifice_troop(
                        game, session, pl_t, ai_t, uid,
                        expected_owner_id=source_owner_id):
                        return False
                    record_cost_card(kind, uid)
                    continue
                if kind == "exhaust":
                    _hc.db_update_card_state(
                        session.session_id, uid,
                        set_bits=_hc.game_engine.ECardStates.Tapped, conn=_hc._db)
                    _hc._db.commit()
                    push_zone(uid, owner_id, row[1])
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
                        event = game._make_event(
                            _hc.game_engine.CardsRevealedSessionEventArgs)
                        event.player_id = (pl_t if owner_id else ai_t)
                        event.session_card_ids = [
                            _hc.game_engine.SessionCardId(_hc.game_engine.UID(uid))]
                        event.collections = [_hc.game_engine.ECardCollections.Warzone]
                        event.owning_players = [event.player_id]
                        event.positions = [0]
                        game._push(event)
                    continue
                if destination == "discard":
                    db_discard_card(session.session_id, uid, connection=_hc._db)
                elif destination == "hand":
                    _hc.db_move_card_to_location(
                        session.session_id, uid, "hand", position=100, conn=_hc._db)
                    _hc._db.commit()
                else:
                    _hc.db_move_card_to_location(
                        session.session_id, uid, destination,
                        reset_state=True, conn=_hc._db)
                    _hc._db.commit()
                    if destination == "void":
                        voided_uids.append(uid)
                    if destination == "deck":
                        db_randomly_insert_deck_cards(
                            session.session_id, owner_id, [uid], connection=_hc._db)
                push_zone(uid, owner_id, destination)
                record_cost_card(kind, uid)
                if destination in ("discard", "void"):
                    emit_trigger(
                        "CardEnteredZoneEvent", uid, owner_id,
                        event_source_collection=row[1],
                        event_destination_collection=destination,
                        event_previous_state=int(row[2] or 0))
                if destination == "discard":
                    emit_trigger(
                        "CardDiscardedEvent", uid, owner_id,
                        event_source_collection=row[1],
                        event_destination_collection="discard")

        # Scrounge fires after the void costs have been paid. The client also
        # exposes those exact instances through the active ability's
        # VoidedCards list, so downstream metadata effects can use them. The
        # queued chain item owns that list for card plays.
        if scrounge and voided_uids and source_uid is not None:
            emit_trigger(
                "CardScroungedEvent", int(source_uid), source_owner_id,
                target_card_id=int(source_uid),
                event_tac={"voided_cards": list(voided_uids)})
        return tuple(int(uid) for uid in voided_uids)

    @staticmethod
    def _set_cost_instance_bounds(cost_instance, minimum, maximum):
        """Populate class-66 bounds for both event-schema revisions."""
        if hasattr(cost_instance, "min_target_count"):
            cost_instance.min_target_count = int(minimum)
            cost_instance.max_target_count = int(maximum)
        else:
            cost_instance.min = int(minimum)
            cost_instance.max = int(maximum)

    @staticmethod
    def _ability_x_cost_metadata(ability_guid):
        """Read variable activation cost fields from the current graph."""
        import hconnect_server as _hc
        from gamedata import DEFAULT_RECORD_STORE, ability_graph
        store = getattr(_hc.HCPHandler._ability_x_cost_metadata,
                        "_record_store", None)
        if store is None:
            store = DEFAULT_RECORD_STORE
            setattr(_hc.HCPHandler._ability_x_cost_metadata, "_record_store", store)
        graph = ability_graph(store, str(ability_guid).lower())
        if graph is None:
            return 0, 0
        return (int(graph.costs.variable_activation or 0),
                int(graph.costs.variable_minimum or 0))

    @staticmethod
    def _template_has_x_cost(tpl_guid):
        """True if a card template has a variable X cost — data-driven from the
        gamedata card field m_VariableCost (card_templates.variable_cost), e.g.
        Burn to the Ground "1X" = 1 base + X.  These cards must show the
        client's X-cost dialog and the chosen X is paid as extra resources.
        """
        from gamedata import DEFAULT_RECORD_STORE
        card = DEFAULT_RECORD_STORE.get("CardTemplate", str(tpl_guid).lower())
        return bool(card and card.has_variable_cost)

    def _resolve_gem_abilities(self, active_gems):
        """Bake a deck's socketed gems into per-instance ability lists at DECK
        SAVE time: {instance_id: [ability guids]} from gem_templates, e.g.
        Shamed Gladiator's Minor Blood Orb of Hatred (gem 5) -> "Rage 1 in all
        zones".  The game later copies these onto game_cards.card_abilities so
        the card is updated with the gem's ability."""
        import hconnect_server as _hc
        out = {}
        for inst, gem in (active_gems or {}).items():
            try:
                gem = int(gem)
            except (TypeError, ValueError):
                continue
            if gem <= 0:
                continue
            abilities = _hc.db_gem_ability_guids(gem, conn=_hc._db)
            if abilities:
                out[str(inst)] = abilities
        return out

    def _card_gem_type(self, game, scid, instance_id=None,
                       persisted_gem=_NO_PERSISTED_GEM):
        """Resolve the authoritative socketed gem for one card instance.

        The deck stores FRA instance ids, while mirrored/AI cards retain the
        gem on their game_cards row.  Keeping this lookup in one place lets
        Practice and PvP derive abilities from the current socket instead of
        trusting a possibly stale decks.gem_abilities cache.
        """
        import hconnect_server as _hc
        if instance_id is None and scid is not None:
            card_details = _hc.db_card_zone_details(
                game.session_id.uid64, scid.uid.uid64, conn=_hc._db)
            if card_details and card_details[1]:
                try:
                    instance_id = int(card_details[1])
                except (TypeError, ValueError):
                    pass
        has_persisted_gem = persisted_gem is not _hc._NO_PERSISTED_GEM
        try:
            gem_type = int(persisted_gem or 0) if has_persisted_gem else 0
        except (TypeError, ValueError):
            gem_type = 0
        if instance_id is not None:
            try:
                # This is a display lookup.  Do not initialize arena_state
                # from inside card encoding: that adds a write/commit to a
                # client-facing turn transition and can hit SQLite's writer
                # lock while the game state is being advanced.
                arena = _hc.db_get_arena_state(
                    self.user_profile["id"], initialize=False)
                deck_id = self._resolve_fra_deck_id(arena["deck_id"]) or 0
                active_gems_json = _hc.db_deck_active_gems(deck_id, conn=_hc._db)
                if active_gems_json:
                    try:
                        gems = json.loads(active_gems_json)
                        gem_type = int(gems.get(str(instance_id), 0) or 0)
                    except Exception:
                        pass
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                # A socketed gem is optional display metadata.  If another
                # process briefly owns SQLite's write lock, continue with the
                # card's persisted game_cards gem (the fallback below) rather
                # than aborting the entire turn transition.
                gem_type = 0
        if (not gem_type and not has_persisted_gem and scid is not None):
            try:
                persisted_gem = _hc.db_card_gem_type(
                    game.session_id.uid64, scid.uid.uid64, conn=_hc._db)
                gem_type = int(persisted_gem or 0)
            except Exception:
                # Older focused fixtures may not carry the optional gem
                # column; the deck lookup above is still authoritative.
                gem_type = 0
        return gem_type

    def _deck_search_ability(self, ability_guid):
        """Find the nested "search your deck" ability + its target template by
        following the ActivateAbility BOM chain to the MoveCardToZone-from-deck
        leaf — e.g. Darkspire Priestess 9853659b -> ... -> 37955055, whose
        target template 0ad94887 has Choosing in its collection flags and the
        Darkspire filter.  The class-39 prompt must reference THIS ability (not
        the top deathcry): the client's ConfigureAbility resolves targets
        against the prompt ability's OWN AbilityTargetTemplateIds, and only
        accepts candidates whose collection is covered by that template.
        Returns (ability_guid, target_template_id) or (None, None)."""
        import hconnect_server as _hc
        from gamedata.records import reference_guid

        def contains(node, type_name, field=None, wanted=None):
            if isinstance(node, dict):
                if str(node.get("_t", "")).rsplit(".", 1)[-1] == type_name:
                    if field is None:
                        return True
                    value = node.get(field)
                    if wanted is None or str(value).lower() == str(wanted).lower():
                        return True
                return any(contains(child, type_name, field, wanted)
                           for child in node.values())
            if isinstance(node, list):
                return any(contains(child, type_name, field, wanted)
                           for child in node)
            return False

        seen = set()
        stack = [str(ability_guid).lower()]
        while stack:
            ag = stack.pop()
            if ag in seen:
                continue
            seen.add(ag)
            graph = _hc._records_ability_graph(self, ag)
            if graph is None:
                continue
            for target in graph.targets:
                if ("Choosing" in (target.collection_flags or "") and
                        "deck" in (target.collection_flags or "").lower()):
                    return ag, str(target.guid)
            for effect in graph.effects:
                if effect.concrete_type not in (
                        "ActivateAbilityEffectTemplate",
                        "ActivateTriggeredAbilityEffectTemplate"):
                    continue
                child = reference_guid(
                    effect.template.field("m_AbilityToInvoke")
                    if effect.template is not None else None)
                if child:
                    stack.append(child.lower())
        return None, None

    def _shards_of_fate_template(self, ability_guids):
        """Data-driven Shards of Fate detection: walk an ability's
        ActivateAbility chain and return (ability_guid, target_template_id)
        when a target template filters a Standard RESOURCE in the DECK
        ("Choose a Standard resource in your deck. Gain the thresholds it
        provides.").  Returns (None, None) for ordinary resources."""
        import hconnect_server as _hc
        from gamedata.records import reference_guid

        def contains(node, type_name, field=None, wanted=None):
            if isinstance(node, dict):
                if str(node.get("_t", "")).rsplit(".", 1)[-1] == type_name:
                    if field is None:
                        return True
                    value = node.get(field)
                    if wanted is None or str(value).lower() == str(wanted).lower():
                        return True
                return any(contains(child, type_name, field, wanted)
                           for child in node.values())
            if isinstance(node, list):
                return any(contains(child, type_name, field, wanted)
                           for child in node)
            return False

        seen = set()
        stack = [str(a).lower() for a in (ability_guids or [])]
        while stack:
            ag = stack.pop()
            if ag in seen:
                continue
            seen.add(ag)
            graph = _hc._records_ability_graph(self, ag)
            if graph is None:
                continue
            for target in graph.targets:
                card_filter = target.card_filter
                if hasattr(card_filter, "to_dict"):
                    card_filter = card_filter.to_dict()
                if (contains(card_filter, "IsSubType", "m_SubType", "Standard")
                        and contains(card_filter, "IsResource")
                        and contains(card_filter, "InZone", "m_Collection", "Deck")):
                    return ag, str(target.guid)
            for effect in graph.effects:
                if effect.concrete_type not in (
                        "ActivateAbilityEffectTemplate",
                        "ActivateTriggeredAbilityEffectTemplate"):
                    continue
                child = reference_guid(
                    effect.template.field("m_AbilityToInvoke")
                    if effect.template is not None else None)
                if child:
                    stack.append(child.lower())
        return None, None

    def _mobilize_discount(self, session, card_uid):
        """Session.CheckMobilize's affordable reduction for one hand card."""
        import hconnect_server as _hc
        from rules_port.static_rules import mobilize_discount
        owner = int(self.user_profile.get("id", 0) or 0) \
            if isinstance(self.user_profile, dict) else 0
        try:
            return mobilize_discount(
                _hc._db, session.session_id, owner, int(card_uid))
        except Exception:
            return 0

    def _handle_rules_port_resource(self, session, transaction):
        """Apply an ordinary resource play without the legacy card dispatcher.

        RulesPort has already validated ownership, zone, phase, priority, and
        the once-per-turn gate. This method owns the resource state/event
        projection and delegates any authored resource BOM to the metadata
        resolver, including chooser continuations.
        """
        import hconnect_server as _hc
        from rules_port import lifecycle as _be
        card_uid = int((getattr(transaction, "payload", {}) or {}).get("card_id", 0))
        if not card_uid or not self.user_profile:
            return False
        from pvp_db import db_rules_port_card_projection
        row = db_rules_port_card_projection(session.session_id, card_uid)
        if not row or row[4] != "Resource":
            return False
        if int(row[2] or 0) != int(self.user_profile.get("id", 0)):
            return False
        source_location = str(row[1] or "").lower()
        if source_location == "deck":
            from pvp_db import db_deck_top_card_details
            from rules_port.static_rules import player_int_attributes
            top = db_deck_top_card_details(
                session.session_id, int(self.user_profile["id"]), conn=_hc._db)
            allowed = player_int_attributes(
                _hc._db, session.session_id, _be.load_state(session),
                int(self.user_profile["id"])).get("CanPlayTopOfDeck", 0)
            if not top or int(top[1]) != card_uid or int(allowed or 0) <= 0:
                return False
        elif source_location != "hand":
            return False
        try:
            ability_guids = json.loads(row[5]) if row[5] else []
        except (TypeError, ValueError):
            return False
        shard_ability = shard_tpl = resource_choice_ability = None
        if ability_guids:
            shard_ability, shard_tpl = self._shards_of_fate_template(
                ability_guids)
            if not shard_tpl:
                from rules_port.resources import printed_resource_choice_ability
                resource_choice_ability = printed_resource_choice_ability(
                    ability_guids)
        bstate = _be.load_state(session)
        from rules_port.resources import can_play_resource, resource_play_limit
        resource_limit = resource_play_limit(
            _hc._db, session.session_id, bstate,
            int(self.user_profile.get("id", 0)))
        if not can_play_resource(
                bstate, "player", limit=resource_limit):
            return False
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        from domain.constants import PLAYED_CARD_POSITION
        from rules_port.zone_effects import move_card_to_zone
        from pvp_db import db_card_owner_zone_state
        prior_card_zone = db_card_owner_zone_state(
            session.session_id, card_uid, conn=_hc._db)
        if not move_card_to_zone(
                _hc._db, session.session_id, card_uid, "PlayedResources",
                owner_id=int(self.user_profile["id"]),
                expected_location=source_location,
                position=PLAYED_CARD_POSITION):
            return False
        current_grant = int(row[6] or 0)
        max_grant = int(row[7] or 0)
        from rules_port.cast_stats import record_card_cast
        record_card_cast(
            bstate, int(self.user_profile.get("id", 0)), resource=True,
            card_uid=card_uid, card_type="Resource")
        from rules_port.resources import (apply_resource_change, play_resource,
                                          resource_threshold_grants)
        threshold_changes = []
        if not shard_tpl and not resource_choice_ability:
            for flag, amount in resource_threshold_grants(
                    _hc._db, session.session_id, int(self.user_profile.get("id", 0)),
                    ability_guids, bstate, source_uid=card_uid):
                change = apply_resource_change(
                    bstate, "player", "threshold", amount, color=flag)
                threshold_changes.append((flag, amount, change))
        resource_play = play_resource(
            bstate, "player", current_grant, max_grant,
            additional_plays=max(0, resource_limit - 1))
        charge_delta = max(
            0, int(resource_play.charge.new_value) -
            int(resource_play.charge.old_value))
        _be.save_state(session, bstate)
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        from rules_port import chain_items as _chain_items
        _chain_items.dispatch_card_cast(
            self, session, game, bstate, pl_t, ai_t, card_uid,
            int(self.user_profile.get("id", 0)))
        _chain_items.dispatch_card_zone_transition(
            self, session, game, bstate, pl_t, ai_t, card_uid,
            int(self.user_profile.get("id", 0)), source_location,
            "PlayedResources",
            int(prior_card_zone[2] or 0) if prior_card_zone else 0)
        # The printed parent ability below invokes the authored target and
        # threshold effect.  Keep the resource play to one activation; the
        # manual picker would replace that ability's continuation.
        scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(card_uid))
        game.push_card_updated(
            scid, pl_t, _hc.game_engine.ECardCollections.PlayedResources,
            _hc.game_engine.ECardTypes.Resource, template_id=row[0])
        game.push_resource_card_played(scid, pl_t, free=False)
        if current_grant or max_grant:
            current = _hc.game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
            current.player_id = pl_t; current.operation = 1
            current.delta = current_grant
            current.new_value = bstate["player_resources"]
            game._push(current)
            total = _hc.game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
            total.player_id = pl_t; total.operation = 1
            total.delta = max_grant
            total.new_value = bstate["player_total_resources"]
            game._push(total)
        if threshold_changes:
            from rules_port.triggers import dispatch_native_trigger
            for flag, amount, change in threshold_changes:
                threshold = _hc.game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                threshold.player_id = pl_t; threshold.color = flag
                threshold.operation = 1; threshold.delta = amount
                threshold.new_value = change.new_value
                game._push(threshold)
                old_threshold_color = bstate.get("gain_threshold_color")
                bstate["gain_threshold_color"] = int(flag)
                try:
                    dispatch_native_trigger(
                        db=_hc._db, handler=self, game=game, session=session,
                        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                        event_type="GainThresholdEvent",
                        source_card_id=int(self._player_champ_scid.uid.uid64),
                        source_player_id=int(self.user_profile.get("id", 0)),
                        data={"gain_threshold_color": int(flag)})
                finally:
                    if old_threshold_color is None:
                        bstate.pop("gain_threshold_color", None)
                    else:
                        bstate["gain_threshold_color"] = old_threshold_color
        if charge_delta:
            charge = _hc.game_engine.ChampionChargePointsChangedSessionEventArgs()
            charge.player_id = pl_t; charge.operation = 1
            charge.delta = charge_delta
            charge.new_value = bstate["player_charges"]
            game._push(charge)
            from rules_port.triggers import dispatch_native_trigger
            for _ in range(charge_delta):
                dispatch_native_trigger(
                    db=_hc._db, handler=self, game=game, session=session,
                    player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                    event_type="GainChargeEvent",
                    source_card_id=int(self._player_champ_scid.uid.uid64),
                    source_player_id=int(self.user_profile.get("id", 0)))
        from rules_port.resources import (
            resolve_granted_resource_abilities,
        )
        resource_owner = int(self.user_profile.get("id", 0))
        granted_logs = resolve_granted_resource_abilities(
            game, session, _hc._db, self, pl_t, ai_t, bstate,
            card_uid, resource_owner)
        if granted_logs:
            _hc.log_req("    RulesPort resource grants: " + "; ".join(granted_logs))
        if resource_choice_ability:
            from rules_port.resolution import resolve_port_ability
            resolve_port_ability(
                self, game, session, _hc._db, pl_t, ai_t, bstate,
                resource_choice_ability, card_uid, resource_owner,
                target_map={})
        from rules_port.resources import resolve_printed_resource_abilities
        printed_logs = resolve_printed_resource_abilities(
            game, session, _hc._db, self, pl_t, ai_t, bstate,
            card_uid, resource_owner,
            skip_guids=({resource_choice_ability}
                        if resource_choice_ability else ()))
        if printed_logs:
            _hc.log_req("    RulesPort resource abilities: " + "; ".join(printed_logs))
        _be.save_state(session, bstate)
        waiting_for_input = any(bstate.get(key) for key in (
            "pending_choice", "pending_trigger", "pending_deck_search",
            "pending_conversation", "pending_discard_ability",
            "resolution_paused"))
        if not waiting_for_input and _be.stack_empty(bstate):
            # Keep the resource/charge mutation, fresh affordability list, and
            # priority window in one sync packet. Unity enables abilities from
            # its cached PlayerOptionList, so a separate follow-up packet leaves
            # a short window where an unaffordable power can still be clicked.
            self._push_main_phase_options(
                session, pl_t, ai_t, game=game)
        elif _be.stack_empty(bstate):
            # The resource event packet already carried the picker/continuation;
            # do not overwrite it with ordinary main-phase options.
            self._send_battle_events(session, game, pl_t)
            _hc.log_req("    RulesPort resource waiting for continuation")
        else:
            # A threshold/charge trigger placed an item on the chain.  Expose
            # only response options and the resolve checkpoint in the same
            # packet as the resource mutation.
            self._push_phase_options_empty(
                session, pl_t, ai_t, game=game, send=False)
            game.push_green_light(
                pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
            self._send_battle_events(session, game, pl_t)
        _hc.log_req(f"    RulesPort resource applied uid={card_uid} "
                f"resources={bstate['player_resources']} "
                f"threshold={bstate.get('player_threshold', {})}")
        return True

    def _handle_rules_port_card_play(self, session, transaction):
        """Cast a typed troop/artifact/spell through the RulesPort seam.

        The port has already performed phase, priority, ownership, threshold,
        and typed X-cost checks. This operation owns the shared card-play
        transition (payment, CastSpells projection, and one chain item); the
        existing metadata resolver only runs later when that chain item
        resolves.
        """
        import hconnect_server as _hc
        from rules_port import lifecycle as _be
        if not isinstance(self.user_profile, dict):
            return False
        owner_id = int(self.user_profile.get("id", 0) or 0)
        payload = getattr(transaction, "payload", {}) or {}
        card_uid = int(payload.get("card_id") or 0)
        kind = str(getattr(transaction, "kind", ""))

        def reject(reason):
            _hc.log_req(f"    Card play reject: "
                    f"card={_hc.card_label(session.session_id, card_uid)} "
                    f"kind={kind or '-'} reason={reason}")
            return False

        if not card_uid or kind not in {
                "play_troop", "play_artifact", "play_spell",
                "play_champion"}:
            return reject("untyped-intent")
        from pvp_db import db_rules_port_card_projection
        row = db_rules_port_card_projection(session.session_id, card_uid)
        if not row or int(row[2] or 0) != owner_id:
            return reject(f"missing-or-not-owner(row_owner="
                          f"{int(row[2]) if row else '-'})")
        source_location = str(row[1] or "").lower()
        if source_location == "deck":
            from pvp_db import db_deck_top_card_details
            from rules_port.static_rules import player_int_attributes
            owner = owner_id
            top = db_deck_top_card_details(
                session.session_id, owner, conn=_hc._db)
            allowed = player_int_attributes(
                _hc._db, session.session_id, _be.load_state(session), owner).get(
                    "CanPlayTopOfDeck", 0)
            if not top or int(top[1]) != card_uid or int(allowed or 0) <= 0:
                return reject("deck-top-not-allowed")
        elif source_location != "hand":
            return reject(f"not-in-hand(location={source_location or '-'})")
        if row[4] == "Resource":
            return reject("resource-card")
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        pl_t = _hc.game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(card_uid))
        _tpl, _ct, _name, cost, attack, defense, gem = self._card_full_data(
            game, scid, row[0], None)
        from rules_port.static_rules import effective_cost
        try:
            cost = int(effective_cost(_hc._db, session.session_id, bstate, card_uid))
        except Exception:
            cost = int(cost or 0)
        play_plan = self._card_play_plan(
            row[0], card_uid, owner_id, session=session)
        if play_plan is None:
            return reject(f"no-play-plan(template={row[0]})")
        ability_data = payload.get("ability_data") or ()
        targets = []
        client_cost_uids = {}
        x_cost = 0
        cast_ability_guids = {
            ability.ability_guid.lower()
            for ability in play_plan.cast_abilities
        }
        for activation in ability_data:
            if not isinstance(activation, dict):
                continue
            raw_ability_guid = activation.get(
                "ability_template_id", activation.get("ability_guid", ""))
            if isinstance(raw_ability_guid, dict):
                raw_ability_guid = raw_ability_guid.get(
                    "guid", raw_ability_guid.get("m_Guid", ""))
            activation_is_cast_ability = (
                str(raw_ability_guid or "").lower() in cast_ability_guids)
            x_cost = max(x_cost, int(activation.get("x_cost", 0) or 0))
            target_map = activation.get("target_map", {})
            if isinstance(target_map, dict):
                for selected in target_map.values():
                    if not isinstance(selected, (list, tuple, set)):
                        selected = (selected,)
                    for value in selected:
                        try:
                            uid = int(getattr(value, "uid64", value))
                        except (TypeError, ValueError):
                            continue
                        # TargetMap is already typed by the ObjFmt decoder;
                        # retain both SessionCardId and PlayerId targets.
                        # Only the source card itself is excluded.
                        if uid != card_uid:
                            targets.append(uid)
            # XCostData.CardsToSacrifice arrives separately from TargetMap
            # (the decoder normalized it to cost_target_map). Card-template
            # costs are kept here; ability graph costs stay with their
            # ability_template_id and are bound by PlayPlan below.
            if activation_is_cast_ability:
                continue
            cost_map = activation.get("cost_target_map")
            if isinstance(cost_map, dict):
                for index, selected in cost_map.items():
                    try:
                        index = int(index)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(selected, (list, tuple, set)):
                        selected = (selected,)
                    for value in selected:
                        try:
                            uid = int(getattr(value, "uid64", value))
                        except (TypeError, ValueError):
                            continue
                        if uid not in client_cost_uids.setdefault(index, []):
                            client_cost_uids[index].append(uid)
        if not x_cost:
            # ``XCostData`` is a nested record the generic ObjFmt walker can
            # stop before, so the typed activation can lose the value the
            # player chose in the X-cost dialog.  Recover the labelled Int32
            # from the raw envelope (the same seam the legacy card-play and
            # tournament paths use) so a variable-cost card cannot resolve
            # for 0 and deal no damage.
            inner_bytes = getattr(transaction, "inner_bytes", None)
            if isinstance(inner_bytes, (bytes, bytearray)):
                x_cost = max(0, int(self._extract_int32_field(
                    bytes(inner_bytes), "m_ResourceXCost") or 0))
        cost_selection = self._card_play_cost_selections(
            session, play_plan, card_uid, targets,
            client_cost_map=client_cost_uids)
        if cost_selection is None:
            return reject("cost-selection")
        cost_selections, cost_uids = cost_selection
        if play_plan.cost.variable:
            if x_cost < int(play_plan.cost.variable_minimum or 0):
                return reject(f"x-cost {x_cost} < minimum "
                              f"{int(play_plan.cost.variable_minimum or 0)}")
        elif x_cost:
            return reject(f"x-cost {x_cost} on fixed-cost card")
        playing_for_free = bool(payload.get("playing_for_free", False))
        x_payment = x_cost * play_plan.cost.variable_multiplier
        payment = 0 if playing_for_free else cost + x_payment
        mobilized = []
        for activation in ability_data:
            if not isinstance(activation, dict):
                continue
            selected = activation.get("cards_to_mobilize")
            if selected is None and isinstance(
                    activation.get("x_cost_data"), dict):
                selected = activation["x_cost_data"].get("cards_to_mobilize")
            if selected is None:
                continue
            if not isinstance(selected, (list, tuple, set)):
                selected = (selected,)
            for value in selected:
                try:
                    uid = int(getattr(value, "uid64", value))
                except (TypeError, ValueError):
                    continue
                if uid not in mobilized:
                    mobilized.append(uid)
        if mobilized and not playing_for_free:
            # Session.CheckMobilize: each mobilized card is a ready troop the
            # player controls, at most the card's Mobilize value, and each one
            # tapped this way reduces the resource cost by two.
            from rules_port.static_rules import mobilize_payment
            reduced = mobilize_payment(
                _hc._db, session.session_id, owner_id, card_uid, payment,
                mobilized)
            if reduced is None:
                return reject("mobilize-payment")
            payment = reduced
        resources = int(bstate.get("player_resources", 0) or 0)
        if payment > resources:
            return reject(f"cost {payment} > pool {resources}")
        selected_cost_map = {
            int(spec.get("index", position)): tuple(selected)
            for position, (spec, selected) in enumerate(cost_selections)
            if selected
        }
        activations, _cost_target_map = play_plan.activation_bundle(
            targets, x_cost=x_cost, cost_target_map=selected_cost_map,
            ability_data=ability_data)
        ability_cost_selection = self._ability_card_play_cost_selections(
            session, play_plan, card_uid, activations, owner_id=owner_id,
            battle_state=bstate)
        if ability_cost_selection is None:
            return reject("ability-cost-selection")
        ability_cost_selections, ability_cost_uids = ability_cost_selection
        cost_selections.extend(ability_cost_selections)
        cost_uids.update(ability_cost_uids)
        validation_errors = play_plan.validate(
            variable_cost=x_cost,
            activations=activations,
            cost_target_map=selected_cost_map)
        if validation_errors:
            return reject(f"plan-validate {list(validation_errors)}")
        if not self._card_costs_valid(
                session, cost_selections, owner_id):
            return reject(f"cost-invalid "
                          f"{[(spec.get('kind'), [hex(u) for u in chosen]) for spec, chosen in cost_selections]}")
        if mobilized:
            from pvp_db import db_update_card_state
            for uid in mobilized:
                db_update_card_state(
                    session.session_id, int(uid),
                    set_bits=_hc.game_engine.ECardStates.Tapped, conn=_hc._db)
            _hc._db.commit()
        from rules_port.card_transactions import apply_card_play
        from pvp_db import db_card_state_value
        previous_state = int(db_card_state_value(
            session.session_id, card_uid, conn=_hc._db) or 0)
        transition = apply_card_play(
            _hc._db, session.session_id, bstate, card_uid,
            owner_id, payment,
            destination="CastSpells",
            expected_location=source_location)
        if transition is None:
            return reject("zone-transition")
        resource_change = transition.resource_change
        from rules_port.cast_stats import record_card_cast
        record_card_cast(
            bstate, owner_id, resource=False,
            card_uid=card_uid, card_type=row[4], resource_cost=cost)
        voided_cost_uids = self._apply_card_play_costs(
            game, session, bstate, pl_t, ai_t, cost_selections,
            source_uid=card_uid,
            expected_owner_id=owner_id,
            scrounge=any(getattr(ability, "is_scrounge", False)
                         for ability in play_plan.cast_abilities),
            defer_voided_cards=True)
        card_type = _hc.game_engine.card_type_from_db(row[4])
        game.push_card_updated(
            scid, pl_t, _hc.game_engine.ECardCollections.CastSpells, card_type,
            template_id=row[0], cost=cost, attack=attack, defense=defense,
            gems=gem)
        game.push_troop_card_played(scid, pl_t)
        for uid in mobilized:
            from pvp_db import db_card_zone_details
            mugid = int(uid)
            mscid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(mugid))
            details = db_card_zone_details(
                session.session_id, mugid, conn=_hc._db)
            mtpl, mct, _mname, mcost, matk, mdef, mgem = \
                self._card_full_data(
                    game, mscid, details[0] if details else None)
            game.push_card_updated(
                mscid, pl_t, _hc.game_engine.ECardCollections.Warzone, mct,
                template_id=mtpl, cost=mcost, attack=matk, defense=mdef,
                gems=mgem, state=int(_hc.game_engine.ECardStates.Tapped))
        perm = card_type & (_hc.game_engine.ECardTypes.Troop |
                            _hc.game_engine.ECardTypes.Artifact |
                            _hc.game_engine.ECardTypes.Constant)
        # The client keys its chain view by a unique ability instance id (C#
        # ``Session.m_NextAbilityInstanceId``).  A hardcoded id collides with a
        # still-pending projected chain item, and ``queue_projected_chain``
        # then silently reuses the old ability instead of queueing this card.
        inst_id = int(bstate.get("_next_instance_id", 1) or 1)
        bstate["_next_instance_id"] = inst_id + 1
        if perm:
            effect_targets = [uid for uid in targets if uid not in cost_uids]
            from rules_port.card_transactions import \
                automatic_instance_ability_guids
            ability_guids = automatic_instance_ability_guids(
                _hc._db, session.session_id, card_uid,
                [ability.ability_guid
                 for ability in play_plan.cast_abilities])
            chain_descriptor = {
                "kind": "troop", "source_uid": card_uid,
                "ability_guids": ability_guids,
                "target_uid": (effect_targets[-1]
                                if effect_targets else None),
                "instance_id": inst_id, "x_cost": x_cost,
                "voided_cost_uids": list(voided_cost_uids or ()),
                "activations": {
                    guid: activation.as_dict()
                    for guid, activation in activations.items()
                },
                "played_from_hand": source_location == "hand",
                "card_cast_event_dispatched": True,
            }
            _be.stack_push(bstate, chain_descriptor)
        else:
            effect_targets = [uid for uid in targets if uid not in cost_uids]
            from rules_port.card_transactions import \
                automatic_instance_ability_guids
            ability_guids = automatic_instance_ability_guids(
                _hc._db, session.session_id, card_uid,
                [ability.ability_guid
                 for ability in play_plan.cast_abilities])
            chain_descriptor = {
                "kind": "spell", "source_uid": card_uid,
                "ability_guids": ability_guids,
                "target_uid": (effect_targets[-1]
                                if effect_targets else None),
                "instance_id": inst_id, "x_cost": x_cost,
                "voided_cost_uids": list(voided_cost_uids or ()),
                "played_from_hand": source_location == "hand",
                "activations": {
                    guid: activation.as_dict()
                    for guid, activation in activations.items()
                },
                "card_cast_event_dispatched": True,
            }
            _be.stack_push(bstate, chain_descriptor)
        native_port = getattr(session, "_rules_port_session", None)
        if native_port is not None and hasattr(native_port, "queue_projected_chain"):
            native_port.queue_projected_chain(
                chain_descriptor, owner_id,
                first_player_id=pl_t)
        game.push_ability_on_chain(
            scid, _hc.game_engine.ResourceId.from_str(
                _hc.game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID),
            target_card_ids=(
                [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(uid)))
                 for uid in effect_targets]
                if perm else None),
            ability_instance_id=inst_id)
        from rules_port.chain_items import (
            dispatch_card_cast, dispatch_card_zone_transition)
        # C# MoveCard enqueues zone events before CastSpell enqueues
        # CardCastEvent. The underlying card item is already queued, so any
        # listeners are pushed above it with their native instance IDs.
        dispatch_card_zone_transition(
            self, session, game, bstate, pl_t, ai_t, card_uid, owner_id,
            source_location, "CastSpells", previous_state)
        dispatch_card_cast(
            self, session, game, bstate, pl_t, ai_t, card_uid, owner_id)
        # The cast trigger queued above can put the AI first on the chain. No
        # client transaction follows for the AI's own response, so consume its
        # passes here, as the pass handler does for the Practice window.
        if not (session.session_name or "").startswith("tourney-"):
            from application.practice_flow import _is_practice_chain_follow_up
            for _follow_up in range(8):
                follow_up = native_port.action_stack.peek()
                follow_priority = getattr(follow_up, "priority_player_id", None)
                if not _is_practice_chain_follow_up(follow_up, pl_t, True):
                    break
                if not native_port.pass_player_priority(follow_priority):
                    break
                native_port.drive_until_input(max_steps=64)
        _be.save_state(session, bstate)
        holder = getattr(native_port.action_stack.peek(), "priority_player_id", None)
        game.push_green_light(
            pl_t if holder is None or _hc._same_uid(holder, pl_t) else ai_t,
            _hc.game_engine.EPriorityContext.ResolveTopOfChain)
        if payment:
            event = _hc.game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
            event.player_id = pl_t; event.operation = 2
            event.delta = payment
            event.new_value = resource_change.new_value
            game._push(event)
        game.player_resources = resource_change.new_value
        game.push_player_updated(
            pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        self._send_battle_events(session, game, pl_t)
        # The initial packet contains the chain item and AI GreenLight. The
        # follow-up checkpoint exposes only response options until both sides
        # pass; it must not reopen main-phase card options.
        self._push_phase_options_empty(session, pl_t, ai_t)
        chain_game = self._fresh_game(session, pl_t, ai_t, bstate)
        chain_game.push_green_light(
            pl_t, _hc.game_engine.EPriorityContext.ResolveTopOfChain)
        self._send_battle_events(session, chain_game, pl_t)
        _hc.log_req(f"    RulesPort card play queued {row[3]} uid={card_uid} "
                f"payment={payment} x={x_cost}")
        return True

    def _affordable_troop_abilities(self, session, bstate):
        """Return {(card_uid, scid): [ability_guid, ...]} for player cards whose
        MANUAL abilities are activatable.

        Gating (mirrors the client's CanActivateAbilityBase + the champion
        talent gating in _filter_affordable_abilities):
          - is_manual (player-activated, not a trigger)
          - source card is in a metadata-allowed collection
          - casting_behavior matches the current phase (QuickAction=64 any
            window; BasicAction=8 only main phases)
          - activation_cost <= available resources
          - UsesPerGame / UsesPerTurn not exhausted for THIS instance
            (tracked in game_cards.card_uses)
        """
        import hconnect_server as _hc
        if bstate.get("_rules_port_attached"):
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        # This helper is also used while rebuilding option lists after a
        # priority resync.  During the AI's own turn the human must not retain
        # activation options from the previous player stop; the AI-turn
        # marker is set only when an opponent stop has explicitly handed
        # priority back to the human.
        if (bstate.get("turn_player") == _be.AI
                and bstate.get("ai_turn_phase_idx") is None):
            return {}
        phase = _be.current_phase(bstate)
        resources = bstate.get("player_resources", 0)
        rows = _hc.db_ability_option_cards(
            session.session_id, self.user_profile["id"], conn=_hc._db)
        result = {}
        from rules_port.triggers import trigger_collection_allows
        for card_uid, tpl_guid, card_state, attrs, _card_type, card_location in rows:
            ab_list = self._card_ability_list(session, card_uid)
            if not ab_list:
                continue
            affordable = []
            for ag in ab_list:
                graph = _hc._records_ability_graph(self, ag)
                if graph is None:
                    continue
                if not graph.manual:
                    continue
                # Manual abilities may be authored for Hand as well as
                # Warzone (Tunnel is the canonical example). Keep the
                # collection gate metadata-driven, matching the client's
                # Card.PassesCollectionFlagRequirements behavior.
                if not trigger_collection_allows(
                        graph.trigger_collection_flags, card_location):
                    continue
                # Ability conditions gate manual activation (client's
                # CanActivateAbilityBase: TriggerCondition.IsValid). E.g.
                # Droo's Colossal Walker "While this is exhausted: ..." has a
                # RequiresSourcePassesFilterCondition(IsTapped) trigger
                # condition — it is only activatable while the troop is tapped.
                if graph.ability_condition or graph.trigger_condition:
                    if bstate.get("_rules_port_attached"):
                        from rules_port.condition_context import ConditionContext
                        from rules_port.conditions import trigger_condition_met
                    else:
                        from abilities.framework.condition_engine import (
                            ConditionContext,
                            trigger_condition_met,
                        )
                    cond_ctx = ConditionContext(
                        _hc._db, session, bstate,
                        ability_source_uid=card_uid,
                        ability_source_owner_id=self.user_profile["id"])
                    if not trigger_condition_met(graph.source.to_dict(), cond_ctx):
                        continue
                # The ability must have at least one legal target for its
                # explicit target templates, and attacking-target abilities
                # (Prairie Scout's "target attacking troop") are only
                # activatable during combat steps — after attackers were
                # declared, until the second main phase.
                from rules_port.targeting import legal_targets_for
                from rules_port.costs import ability_cost_targets
                cost_guids = {str(guid).lower() for _kind, guid in
                              graph.additional_cost_targets or ()}
                target_refs = [(index, target) for index, target in
                               enumerate(graph.targets)
                               if str(target.guid).lower() not in cost_guids]
                if target_refs:
                    wants_attacking = False
                    has_target = False
                    explicit_target_missing = False
                    for _target_index, target in target_refs:
                        target_filter = target.card_filter
                        if hasattr(target_filter, "to_dict"):
                            target_filter = target_filter.to_dict()
                        if "IsAttacking" in json.dumps(target_filter or {}):
                            wants_attacking = True
                        if not target.requires_input:
                            # Auto targets (e.g. Incubation Slave's 'You' —
                            # "Remove all egg counters from this and sacrifice
                            # it") resolve automatically; no picker, always
                            # available.
                            has_target = True
                            continue
                        cands = legal_targets_for(
                            _hc._db, session.session_id, self.user_profile["id"],
                            target, card_uid,
                            champions=self._champion_targets(),
                            battle_state=bstate)
                        if cands:
                            has_target = True
                        else:
                            explicit_target_missing = True
                    if wants_attacking and phase not in _be.COMBAT_STEPS:
                        continue
                    # An automatic source/player target does not satisfy a
                    # separate explicit target requirement.  Taming Sphere,
                    # for example, has an automatic "You" target followed by
                    # a mandatory Untamed-troop target; it must stay dark when
                    # no legal Untamed troop exists.
                    if explicit_target_missing:
                        continue
                    if not has_target:
                        continue
                if graph.costs.exhausts_card_on_use:
                    # Exhaust-as-cost (e.g. Prairie Scout): the card must be
                    # able to pay the tap — not already tapped, and not
                    # summoning sick (entered this turn without Speed).
                    cstate = card_state or 0
                    if cstate & _hc.game_engine.ECardStates.Tapped:
                        continue
                    if (not (cstate & _hc.game_engine.ECardStates.StartedATurnOnYourSide)
                            and not ((attrs or 0) & _hc.game_engine.ECardAttributes.Speed)):
                        continue
                casting = 64 if graph.casting_behavior == "QuickAction" else 8
                if casting != 64:
                    # BasicAction etc. — main phases only; QuickAction(64) any.
                    # Main phase is meaningful only on this player's turn,
                    # and a non-empty chain cannot be interrupted by a Basic
                    # activation. This prevents Taming Sphere appearing while
                    # the AI has handed back a stale priority window.
                    if bstate.get("turn_player") != _be.PLAYER:
                        continue
                    if phase not in (_hc.game_engine.ETurnPhases.FirstMainPhase,
                                     _hc.game_engine.ETurnPhases.SecondMainPhase):
                        continue
                    if not _be.stack_empty(bstate):
                        continue
                cost = int(graph.costs.activation or 0)
                if cost > resources:
                    continue
                if graph.costs.variable_activation and resources < int(
                        graph.costs.variable_minimum or 0):
                    continue
                payment_unavailable = False
                for cost in ability_cost_targets(
                        graph, _hc._db, session.session_id,
                        self.user_profile["id"], card_uid,
                        champions=self._champion_targets(),
                        battle_state=bstate):
                    if cost.is_source_auto_target:
                        continue
                    if len(cost.candidates) < cost.minimum:
                        payment_unavailable = True
                        break
                if payment_unavailable:
                    continue
                from pvp_db import (db_card_ability_use_counts,
                                    db_card_cooldown_counts)
                used_game, used_turn = db_card_ability_use_counts(
                    session.session_id, card_uid, ag,
                    bstate.get("turn_number", 1), conn=_hc._db)
                if (graph.costs.cooldown and
                        int(db_card_cooldown_counts(
                            session.session_id, card_uid, conn=_hc._db).get(
                                ag, 0) or 0) > 0):
                    continue
                if graph.costs.uses_per_game and used_game >= graph.costs.uses_per_game:
                    continue
                if graph.costs.uses_per_turn and used_turn >= graph.costs.uses_per_turn:
                    continue
                affordable.append(ag)
            if affordable:
                result[(card_uid, tpl_guid)] = affordable
        return result

    def _add_troop_ability_options(self, game, pl_t, session, affordable,
                                   bstate=None):
        """Append card ability options (ECardUsage.Activate) to the
        most recent PlayerOptionList, one OptionInstance per affordable ability.

        Each ability carries one TargetInstance PER target template id, keyed by
        the ability's own m_AbilityTargetTemplateIds. The client's target picker
        (GoCardView.GetTargetsFor) matches a TargetInstance whose TemplateId
        equals the ability's target template id for index i — using Invalid does
        NOT match a real template id, so the picker would show zero valid
        targets (this is why Soothsaying's discard option used the exact
        DISCARD_TARGET_TEMPLATE). min/max_target_counts mirrors the number of
        target templates so BattleStateConfigureAbility gets matching lists.
        """
        import hconnect_server as _hc
        # CardUpdated/PlayerUpdated events may be appended while this packet
        # is assembled.  The option list owns the option metadata; it is not
        # necessarily the final event in the Game.  Requiring events[-1]
        # silently drops manual hand activations such as Tunnel.
        option_list = next(
            (event for event in reversed(game.events)
             if isinstance(event, _hc.game_engine.PlayerOptionListSessionEventArgs)),
            None)
        if option_list is None:
            return
        for (card_uid, tpl_guid), abilities in affordable.items():
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(card_uid)))
            # Default candidate pool: the player's warzone troops.  Abilities
            # with explicit target templates get the pool filtered through the
            # gamedata card filter (e.g. Prairie Scout's IsAttacking+IsTroop).
            from rules_port.targeting import legal_targets_for
            from rules_port.costs import ability_cost_targets, cost_type_for_kind
            # A hand card such as Tunnel can also be normally playable.  The
            # client caches one usage value per card, so merge Activate into
            # the existing Play option instead of appending a second option
            # that would overwrite Play.
            opt = game.get_or_add_card_option(
                option_list, scid, _hc.game_engine.ECardUsage.Activate)
            for ag in abilities:
                inst = game._make_event(_hc.game_engine.OptionInstanceSessionEventArgs)
                inst.opt_id = _hc.game_engine.ResourceId.from_str(ag)
                graph = _hc._records_ability_graph(self, ag)
                cost_guids = {str(guid).lower() for _kind, guid in
                              (graph.additional_cost_targets or ())} \
                    if graph is not None else set()
                target_refs = ([(index, target) for index, target in
                                enumerate(graph.targets)
                                if target.requires_input and
                                str(target.guid).lower() not in cost_guids]
                               if graph is not None else [])
                if target_refs:
                    built = []
                    min_counts = []
                    max_counts = []
                    for i, target in target_refs:
                        tid = target.guid
                        built.append(i)
                        min_counts.append(target.minimum)
                        max_counts.append(max(0, target.maximum)
                                          if target.maximum > 0 else 0)
                        inst.target_ids.append(
                            _hc.game_engine.ResourceId.from_str(tid))
                        others = legal_targets_for(
                            _hc._db, session.session_id, self.user_profile["id"],
                            target, int(card_uid),
                            champions=self._champion_targets(),
                            battle_state=bstate)
                        if not others:
                            # Auto/self templates with no real filter (e.g.
                            # Living Totem "this") fall back to the full
                            # friendly pool; restricting filters (e.g. Prairie
                            # Scout's IsAttacking) keep their empty pool so the
                            # ability is never offered without a valid target.
                            filt = target.card_filter
                            if hasattr(filt, "to_dict"):
                                filt = filt.to_dict()
                            if not filt:
                                others = [r[0] for r in _hc.db_card_uids_in_zone(
                                    session.session_id, self.user_profile["id"],
                                    "warzone", conn=_hc._db)]
                        tgt = game._make_event(_hc.game_engine.TargetInstanceSessionEventArgs)
                        tgt.target_index = i
                        tgt.target_id = _hc.game_engine.ResourceId.from_str(tid)
                        tgt.targets = [_hc.game_engine.SessionCardId(_hc.game_engine.UID(int(u))) for u in others]
                        inst.target_instances.append(tgt)
                    inst.min_target_counts = min_counts if built else []
                    inst.max_target_counts = max_counts if built else []
                if graph is not None:
                    for cost in ability_cost_targets(
                            graph, _hc._db, session.session_id,
                            self.user_profile["id"], int(card_uid),
                            champions=self._champion_targets(),
                            battle_state=bstate):
                        if cost.is_source_auto_target:
                            continue
                        candidates = cost.candidates
                        if len(candidates) < cost.minimum:
                            continue
                        ci = game._make_event(
                            _hc.game_engine.CostInstanceSessionEventArgs)
                        minimum = cost.minimum
                        maximum = cost.maximum
                        if maximum < 0:
                            maximum = len(candidates)
                        _hc.HCPHandler._set_cost_instance_bounds(
                            ci, minimum, maximum)
                        ci.cost_type = cost_type_for_kind(cost.kind)
                        ci.target_template_id = _hc.game_engine.ResourceId.from_str(
                            cost.guid)
                        ci.targets = [_hc.game_engine.SessionCardId(
                            _hc.game_engine.UID(int(uid))) for uid in candidates]
                        inst.target_instances.append(ci)
                variable_x, variable_min = _hc.HCPHandler._ability_x_cost_metadata(ag)
                if variable_x:
                    ci = game._make_event(_hc.game_engine.CostInstanceSessionEventArgs)
                    _hc.HCPHandler._set_cost_instance_bounds(ci, variable_min, 0)
                    ci.cost_type = 256  # EAbilityCostType.XCostAbilityCostType
                    ci.target_template_id = _hc.game_engine.ResourceId.invalid()
                    ci.targets = []
                    inst.target_instances.append(ci)
                opt.instances.append(inst)
                discard_prompt = self._discard_prompt_data(ag, bstate=bstate)
                if discard_prompt:
                    child_ability, discard_target = discard_prompt
                    hand = [
                        _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(uid)))
                        for (uid,) in _hc.db_card_uids_in_zone(
                            session.session_id, self.user_profile["id"],
                            "hand", conn=_hc._db)]
                    if hand:
                        child = game._make_event(
                            _hc.game_engine.OptionInstanceSessionEventArgs)
                        child.opt_id = _hc.game_engine.ResourceId.from_str(
                            child_ability)
                        child.target_ids.append(
                            _hc.game_engine.ResourceId.from_str(discard_target))
                        child.min_target_counts = [1]
                        child.max_target_counts = [1]
                        child_target = game._make_event(
                            _hc.game_engine.TargetInstanceSessionEventArgs)
                        child_target.target_index = 0
                        child_target.target_id = _hc.game_engine.ResourceId.from_str(
                            discard_target)
                        child_target.targets = hand
                        child.target_instances.append(child_target)
                        opt.instances.append(child)

    @staticmethod
    def _champion_starting_health(race_name, cls_name):
        """Look up a champion's starting health from champion_class_data."""
        if not race_name or not cls_name:
            return 20
        from pvp_db import db_champion_template_health_by_class
        return db_champion_template_health_by_class(race_name, cls_name)

    def _filter_affordable_abilities(self, abilities, bstate, phase=None, *,
                                     target_data=None, cost_data=None,
                                     session=None):
        """Filter champion abilities to only those activatable in the current phase
        and affordable (charges/SP >= cost). Pass `phase` (an ETurnPhases value) to
        also gate on activatable_phases (a bitmask where bit N = 1<<N).

        QuickAction abilities (casting_behavior=64) ignore the phase gate — they
        may be activated in ANY priority window (mirrors the client's
        CanActivateAbilityBase, which skips the main-phase check for QuickAction).

        ``target_data``/``cost_data`` are the exact per-ability selection pools
        the caller passes to ``add_champion_to_options``.  Offering an ability
        whose authored payment or explicit target has no legal card makes the
        champion card light up for an activation that can never be completed
        (Bunoshi's "sacrifice a troop"), so the offer gate reads the same pools
        the client's picker will.
        """
        import hconnect_server as _hc
        if bstate.get("_rules_port_attached"):
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        charges = bstate.get("player_charges", 0)
        sp = bstate.get("player_spell_points", 0)
        phase_bit = (1 << phase) if phase is not None else 0
        chain_pending = bool(bstate.get("stack"))
        # Session.CanActivateChargeAbilitiesAsQuick: a warzone card with this
        # flag lets the player treat charge powers as QuickActions.
        quick_charges = False
        if session is not None:
            try:
                from rules_port.static_rules import owner_has_card_int_attr
                owner = (int(self.user_profile.get("id", 0) or 0)
                         if isinstance(self.user_profile, dict) else 0)
                quick_charges = owner_has_card_int_attr(
                    _hc._db, session.session_id, owner,
                    "YouCanActivateYourChargePowersAsThoughTheyWereQuick")
            except Exception:
                quick_charges = False
        # Spell-power escalation: each use permanently bumps that spell's SP cost
        # by +1 (mirrors Session.cs:1154 IncrementSpellPointCostModifier).
        # Only spell powers (spell_cost > 0) escalate; charge powers don't.
        sp_uses = bstate.get("player_sp_uses", {}) or {}
        # Use the shared runtime DB helper here.  Importing the PVE helper
        # binds a second connection seam and makes live/test DB swaps observe
        # different state.
        from db import db_talent_ability_costs
        afford = []
        for aid in (abilities or []):
            # Champion ability lists contain both player-activated powers and
            # automatic talent triggers.  The latter must remain available to
            # the trigger dispatcher, but they are not client-activatable
            # abilities and must not light up the champion card in an option
            # packet.  Use the typed Records graph as the authority so this
            # stays data-driven for talents and signature powers alike.
            graph = _hc._records_ability_graph(self, str(aid.guid))
            if graph is not None and not graph.manual:
                continue
            # A spent ONE-SHOT power must stop lighting up the champion card
            # (the client keeps offering it because champions are not
            # game_cards rows and cannot carry ``card_uses``).
            try:
                player_champ_uid = int(self._player_champ_scid.uid.uid64)
            except (AttributeError, TypeError, ValueError):
                player_champ_uid = None
            if not self._champion_ability_available(
                    bstate, aid.guid, graph, source_uid=player_champ_uid):
                continue
            payable = self._champion_payment_available(
                str(aid.guid), graph, target_data, cost_data)
            row = db_talent_ability_costs(str(aid.guid))
            if row is None:
                # Champion signature charge powers (champion_abilities) aren't
                # talents — their costs/casting come from the gamedata seed.
                from pvp_db import db_champion_ability_costs
                row = db_champion_ability_costs(str(aid.guid))
            if row:
                cc, sc, aphases, casting = row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0
                if casting != 64:
                    if not (cc > 0 and quick_charges):
                        # BasicAction abilities cannot be activated while a
                        # chain item is resolving. The source champion still
                        # appears on the chain, but its BasicAction button must
                        # not remain clickable from the previous option list.
                        if chain_pending:
                            continue
                        # BasicAction etc.: only on the PLAYER's own main
                        # phases. During the opponent's turn there is no player
                        # main phase, so a BasicAction charge power must not be
                        # offered even if the AI happens to be in FirstMain.
                        if bstate.get("turn_player") != _be.PLAYER:
                            continue
                        if phase is not None and not (aphases & phase_bit):
                            continue  # not activatable (QuickAction exempt)
                eff_sc = sc + (int(sp_uses.get(str(aid.guid), 0)) if sc > 0 else 0)
                if (charges >= cc and sp >= eff_sc
                        and payable
                        and self._champion_thresholds_met(str(aid.guid), bstate)):
                    afford.append(aid)
            else:
                # Unknown cost — include only if phase gating passes (or no
                # phase given) and any gamedata threshold is met.
                if (payable and (phase is None or phase_bit)
                        and self._champion_thresholds_met(str(aid.guid), bstate)):
                    afford.append(aid)
        return afford

    def _champion_payment_available(self, ability_guid, graph, target_data,
                                    cost_data):
        """Whether a champion ability's authored card costs and explicit
        targets can be satisfied from the pools the client is offered.

        Mirrors the client's own activation rule: a non-optional effect target
        that is neither auto/random nor ``m_AllowBestEffortMinimumTargetCount``
        must have at least its minimum legal cards
        (``Session.CanActivateAbility``), and a sacrifice/exhaust/void cost
        needs its payment cards.  With no pools supplied there is nothing to
        check, so the ability stays offered.
        """
        if target_data is None and cost_data is None:
            return True
        ag = str(ability_guid)
        specs = {str(target.guid).lower(): target
                 for target in (getattr(graph, "targets", ()) or ())}

        def relaxed(target_guid):
            spec = specs.get(str(target_guid).lower())
            return bool(spec is not None and (
                getattr(spec, "optional", False) or
                getattr(spec, "allow_best_effort_minimum", False)))

        for tid, _ctype, candidates, minimum, _maximum in (
                (cost_data or {}).get(ag) or ()):
            if relaxed(tid):
                continue
            if len(candidates or ()) < int(minimum or 0):
                return False
        for tid, candidates, minimum, _maximum, _index in (
                (target_data or {}).get(ag) or ()):
            if relaxed(tid):
                continue
            # The authored minimum decides (0 = an "up to N" target the player
            # may skip); the client compares CountLegalTargets against exactly
            # this value.
            if len(candidates or ()) < int(minimum or 0):
                return False
        return True

    def _champion_thresholds_met(self, ability_guid, bstate):
        """True if the champion's threshold requirements (from gamedata
        champion_abilities.thresholds_json, e.g. Dimmid's [DIAMOND][DIAMOND])
        are met by the player's current threshold counts."""
        import hconnect_server as _hc
        from pvp_db import db_champion_ability_thresholds
        reqs = db_champion_ability_thresholds(ability_guid)
        if not reqs:
            return True
        th = bstate.get("player_threshold", {}) or {}
        # The AI activates its own champion powers with its own thresholds.
        if getattr(self, "_resolving_ai_champion", False):
            th = bstate.get("ai_threshold", {}) or {}
        for color, qty in reqs:
            flag = _hc.game_engine.SHARD_TO_FLAG.get(str(color).lower(), 0)
            if flag and int(th.get(flag, 0) or 0) < qty:
                return False
        return True

    def _bom_has_discard(self, ability_guid, *, native=False):
        """Whether an ability graph contains an authored discard effect."""
        from rules_port.metadata import ability_has_effect
        return ability_has_effect(
            ability_guid, "DiscardCardAbilityEffectTemplate")

    def _discard_prompt_data(self, ability_guid, *, bstate=None):
        """Return ``(ability_guid, hand_target_template)`` for a discard.

        Only a dedicated discard BOM leaf opens the class-23 follow-up prompt.
        An ability-level discard payment is a normal typed CostInstance and is
        paid with the other authored activation costs.
        """
        from rules_port.metadata import ability_effect_prompt
        prompt = ability_effect_prompt(
            ability_guid, "DiscardCardAbilityEffectTemplate")
        if prompt and prompt[1]:
            return prompt
        return None

    def _ability_requires_discard(self, ability_guid):
        """Return whether activation requires the controller to discard.

        The typed BOM/ability cost graph is the sole source of this payment.
        """
        return self._discard_prompt_data(ability_guid) is not None

    def _discard_costs_for(self, session, ability_ids):
        """Return {ability_guid: (hand SessionCardId list, target_template_id str)}
        for the abilities the class-23 discard prompt needs target instances for.

        Only the invoked discard child ability gets a target entry. The granted
        ability must NOT carry a target instance, otherwise the client shows
        the targeting arrow on click (before activation); the class-23 prompt
        happens after the draw instead."""
        import hconnect_server as _hc
        if not ability_ids:
            return {}
        hand_uids = _hc.db_game_cards_at_location_scalar(session.session_id, "hand", self.user_profile["id"])
        hand = [_hc.game_engine.SessionCardId(_hc.game_engine.UID(r)) for r in hand_uids]
        if not hand:
            return {}
        out = {}
        for aid in ability_ids:
            prompt = self._discard_prompt_data(str(aid.guid))
            if prompt and prompt[1]:
                child_ability, target_template = prompt
                out[child_ability] = (list(hand), target_template)
        return out

    def _champion_ability_targets(self, session, ability_ids, champ_uid=0):
        """Return {ability_guid: [(target_template_id, [candidate card uids],
        min, max, target_template_index)]}
        for champion abilities with explicit targets (e.g. Dimmid's "Target
        troop gets Lifedrain this turn").  Candidates come from the gamedata
        target template (collection + card filter)."""
        import hconnect_server as _hc
        if not ability_ids:
            return {}
        from rules_port.targeting import legal_targets_for
        out = {}
        for aid in ability_ids:
            ag = str(aid.guid)
            graph = _hc._records_ability_graph(self, ag)
            if graph is None:
                continue
            cost_tids = {str(guid).lower() for _kind, guid in
                         graph.additional_cost_targets or ()}
            for target_index, target in enumerate(graph.targets):
                if (not target.requires_input or
                        str(target.guid).lower() in cost_tids):
                    continue
                tid = target.guid
                cands = legal_targets_for(
                    _hc._db, session.session_id, self.user_profile["id"],
                    target, int(champ_uid or 0),
                    champions=self._champion_targets())
                mn = target.minimum
                mx = target.maximum if target.maximum > 0 else -1
                out.setdefault(ag, []).append(
                    (tid, cands, mn, mx, target_index))
        return out

    def _ability_cost_templates(self, ability_guid):
        """Return payment targets from the current typed AbilityGraph."""
        import hconnect_server as _hc
        from rules_port.costs import cost_type_for_kind
        graph = _hc._records_ability_graph(self, ability_guid)
        if graph is None:
            return []
        return [(guid, cost_type_for_kind(kind))
                for kind, guid in graph.additional_cost_targets or ()]

    def _current_ability_graph(self, ability_guid):
        from gamedata import ability_graph
        store = getattr(self, "_play_plan_store", None)
        if store is None:
            from gamedata import DEFAULT_RECORD_STORE
            store = DEFAULT_RECORD_STORE
            self._play_plan_store = store
        return ability_graph(store, str(ability_guid).lower())

    def _current_target_spec(self, target_guid):
        store = getattr(self, "_play_plan_store", None)
        if store is None:
            from gamedata import DEFAULT_RECORD_STORE
            store = DEFAULT_RECORD_STORE
            self._play_plan_store = store
        target = store.get("AbilityTargetTemplate", str(target_guid).lower())
        return target.target_spec if target is not None else None

    def _champion_ability_costs(self, session, ability_ids, champ_uid=0):
        """{ability_guid: [(target_template_id, EAbilityCostType, [candidate
        card uids], min, max)]} for the champion ability's CARD costs — the
        client's BattleStateAssignXCost prompts for these (e.g. Bun'jitsu's
        "Void two ready troops you control")."""
        import hconnect_server as _hc
        from rules_port.costs import ability_cost_targets, cost_type_for_kind
        out = {}
        for aid in (ability_ids or []):
            ag = str(aid.guid)
            graph = _hc._records_ability_graph(self, ag)
            if graph is None:
                continue
            for cost in ability_cost_targets(
                    graph, _hc._db, session.session_id,
                    self.user_profile["id"], int(champ_uid or 0),
                    champions=self._champion_targets()):
                out.setdefault(ag, []).append(
                    (cost.guid, cost_type_for_kind(cost.kind), cost.candidates,
                     cost.minimum, cost.maximum))
        return out

    def _select_champion_activation_targets(self, session, bstate,
                                            ability_guid, champ_uid,
                                            selected_uids):
        """Separate champion card-payment targets from effect targets.

        Champion powers use the same AbilityTargetTemplate contracts as card
        abilities.  The client submits all selected cards in one TargetMap,
        so a sacrifice target must be removed from the effect-target pool
        before the chain item is created.
        """
        import hconnect_server as _hc
        from rules_port.costs import (ability_cost_targets,
                                      cost_type_for_kind,
                                      validate_cost_target_selection)
        from rules_port.targeting import legal_targets_for
        graph = _hc._records_ability_graph(self, ability_guid)
        if graph is None:
            return None
        selected_uids = [int(uid) for uid in (selected_uids or [])]
        used = set()
        sacrifices = []
        for cost in ability_cost_targets(
                graph, _hc._db, session.session_id, self.user_profile["id"],
                int(champ_uid), champions=self._champion_targets(),
                battle_state=bstate):
            tid = cost.guid
            cost_type = cost_type_for_kind(cost.kind)
            minimum = cost.minimum
            maximum = cost.maximum
            if cost.is_source_auto_target:
                candidates = [int(champ_uid)]
                available = candidates
            else:
                candidates = cost.candidates
                available = [uid for uid in selected_uids
                             if uid in {int(c) for c in candidates}
                             and uid not in used]
            if len(available) < minimum:
                return None
            limit = len(available) if maximum < 0 else maximum
            chosen = available[:limit]
            chosen = validate_cost_target_selection(
                _hc._db, session.session_id, self.user_profile["id"],
                int(champ_uid), cost, chosen,
                champions=self._champion_targets(), battle_state=bstate)
            if chosen is None:
                return None
            used.update(chosen)
            if int(cost_type) == 2:
                sacrifices.extend(chosen)

        effect_candidates = []
        explicit_required = False
        cost_tids = {str(guid).lower() for _kind, guid in
                     graph.additional_cost_targets or ()}
        for target in graph.targets:
            if (not target.requires_input or
                    str(target.guid).lower() in cost_tids):
                continue
            explicit_required = explicit_required or bool(
                target.minimum if target.minimum is not None else 1)
            effect_candidates.extend(int(uid) for uid in legal_targets_for(
                _hc._db, session.session_id, self.user_profile["id"], target,
                int(champ_uid), champions=self._champion_targets(),
                battle_state=bstate))
        legal_effects = set(effect_candidates)
        effect_selected = [uid for uid in selected_uids
                           if uid not in used and uid in legal_effects]
        if explicit_required and not effect_selected:
            return None
        return (effect_selected[-1] if effect_selected else None, sacrifices)

    @staticmethod
    def _thresholds_met(thresh_json, player_threshold):
        """Check if the player's thresholds meet the card's requirements.

        thresh_json is a JSON string like {"list":[0,1]} where indices map:
        0=Colorless, 1=Blood, 2=Ruby, 3=Sapphire, 4=Wild, 5=Diamond.
        Shard flags in player_threshold use bitmask values: Blood=4, Ruby=8,
        Sapphire=16, Wild=32, Diamond=64.
        The list can repeat a color ({"list":[2,2]} = TWO Ruby), so each
        color's required count is its number of occurrences.
        Returns True if all card thresholds are met.

        Handles both int and string keys (JSON serialization can convert
        int dict keys to strings across save/load cycles).
        """
        if not thresh_json:
            return True
        try:
            import json as _j
            req = _j.loads(thresh_json)
            req_list = req.get("list", [])
            if not req_list:
                return True
            shard_fmt = {0:0, 1:4, 2:8, 3:16, 4:32, 5:64}
            need = {}
            for s in req_list:
                flag = shard_fmt.get(s, s)
                need[flag] = need.get(flag, 0) + 1
            for flag, count in need.items():
                # Try int key first, then string key (JSON converts int→str)
                val = player_threshold.get(flag)
                if val is None:
                    val = player_threshold.get(str(flag), 0)
                if (val or 0) < count:
                    return False
            return True
        except Exception:
            return True

    def _champion_targets(self):
        """[(card_uid, user_id, name, health)] for both champions — the client
        can target champions (IsHero filters), so they join targeting pools."""
        out = []
        p = getattr(self, "_player_champ_scid", None)
        if p is not None:
            hp = getattr(self, "_player_starting_health", 20)
            if getattr(self, "_current_bstate", None):
                hp = self._current_bstate.get("player_health", hp)
            out.append((int(p.uid.uid64),
                        self.user_profile["id"] if self.user_profile else 0,
                        "Player", hp))
        a = getattr(self, "_ai_champ_scid", None)
        if a is not None:
            hp = getattr(self, "_ai_starting_health", 20)
            if getattr(self, "_current_bstate", None):
                hp = self._current_bstate.get("ai_health", hp)
            out.append((int(a.uid.uid64), 0, "AI", hp))
        return out
