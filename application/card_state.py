"""Card templates, instance data, ability uses, draws and deck-out for the live game."""

from __future__ import annotations

import json
import re
import struct

import game_engine


class CardStateMixin:
    """Methods of HCPHandler, mixed into it (see hconnect_server)."""

    @staticmethod
    def _champion_health_by_guid(guid):
        """Look up a champion template's starting health by its GUID."""
        import hconnect_server as _hc
        if not guid:
            return 20
        return _hc.db_champion_template_health(guid)

    def _template_by_guid(self, template_guid):
        """Fetch card template data for a template GUID.
        Returns (guid, card_type, name, cost, attack, defense) or None."""
        import hconnect_server as _hc
        if not template_guid:
            return None
        return _hc.db_template_by_guid(template_guid)

    def _resolve_card_ref(self, card_ref, user_id=None):
        """Resolve a game_cards.card_template_id to card template info.

        card_template_id is either an integer card_instances.instance_id
        (player/FRA cards) or a template GUID string (AI encounter deck cards).
        Returns (template_guid, card_type_name, name, cost, attack, defense).
        """
        import hconnect_server as _hc
        if card_ref is None:
            return None, None, None, 0, 0, 0
        if isinstance(card_ref, str):
            t = self._template_by_guid(card_ref)
            if t:
                return t
            return None, None, None, 0, 0, 0
        if user_id is None and self.user_profile:
            user_id = self.user_profile.get("id")
        if user_id is None:
            return None, None, None, 0, 0, 0
        row = _hc.db_card_instance_display(
            user_id, card_ref, conn=_hc._db)
        if row:
            return row[0], row[1], row[2], row[3] or 0, row[4] or 0, row[5] or 0
        return None, None, None, 0, 0, 0

    def _card_full_data(self, game, scid: game_engine.SessionCardId,
                        template_guid, instance_id=None,
                        instance_projection=None):
        """Resolve full card data for a session card and prepare it for display.

        Fills game.card_defs[scid] (thresholds, abilities) so a subsequent
        push_card_updated renders the complete card (cost, atk/def, shards,
        abilities, gems). Returns (tpl_guid, card_type, name, cost, attack,
        defense, gem_type).

        Instance-aware: the card's CURRENT abilities come from game_cards
        (Shift moves abilities between cards, and PowerShifted triggers grant
        +atk/+def), not the template's printed list — so a Prep re-push keeps a
        shifted Lifedrain / granted ability / buff instead of reverting it.
        """
        import hconnect_server as _hc
        # This is the hot CardUpdated path. Keep the instance fields in the
        # historical ``db_card_instance_full`` order, but fetch the owner,
        # zone, and current template GUID in the same indexed lookup. Printed
        # fields come from the process-wide Records cache; a missing record is
        # expected for champion/generated/legacy rows and uses the DB fallback.
        joined = None
        irow = None
        if scid is not None:
            joined = (instance_projection if instance_projection is not None
                      else _hc.db_card_instance_dynamic_projection(
                          game.session_id.uid64, scid.uid.uid64, conn=_hc._db))
            irow = joined
        t = None
        records_template = None
        if joined:
            joined_guid = str(joined[13] or "")
            requested_guid = str(template_guid or "")
            if (joined_guid and
                    (not requested_guid or
                     joined_guid.lower() == requested_guid.lower())):
                records_template = _hc._records_card_projection(joined_guid)
                if records_template:
                    t = records_template[:6]
        if t is None:
            records_template = _hc._records_card_projection(template_guid)
            if records_template:
                t = records_template[:6]
        if t is None:
            t = self._template_by_guid(template_guid)
        if t:
            tpl_guid, ct_name, name, cost, atk, def_ = t
            ct = _hc.game_engine.card_type_from_db(ct_name)
        else:
            # Champion templates — PvP champs in champion_templates_extended,
            # PvE champs (and some PvP) in champion_templates.  Try both.
            from pvp_db import db_champion_ability_guids
            if _hc.db_is_champion_template(template_guid):
                tpl_guid, ct_name, name, cost, atk, def_ = (
                    template_guid, "Champion", "Champion", 0, 0, 20)
                ct = _hc.game_engine.ECardTypes.Champion
            else:
                tpl_guid = "00000000-0000-0000-0000-000000000000"
                ct_name = "Troop"
                ct, name, cost, atk, def_ = _hc.game_engine.ECardTypes.Troop, "Card", 0, 0, 0
        shards = []
        attributes = _hc.game_engine.ECardAttributes.Unknown
        lethal = False
        srow = None
        # Instance-persisted abilities + power/toughness buffs (Shift /
        # PowerShiftedEvent). A bare template read would drop them on every
        # re-push, so read them from the card's game_cards row.
        inst_abilities_json = None
        atk_mod = def_mod = dmg = 0
        perm_atk = perm_def = 0
        temp_atk = temp_def = 0
        inst_attrs = 0
        temp_attrs = 0
        persisted_int_attrs = {}
        threshold_override = None
        subtype_override = None
        instance_damage_shield = False
        orig_tpl = None
        card_location = None
        card_owner_id = None
        card_instance_ref = None
        persisted_gem = _hc._NO_PERSISTED_GEM
        if scid is not None:
            if irow:
                inst_abilities_json = irow[0]
                atk_mod = irow[1] or 0
                def_mod = irow[2] or 0
                dmg = irow[3] or 0
                orig_tpl = irow[4] or None
                inst_attrs = int(irow[9] or 0)
                temp_attrs = int(irow[10] or 0)
                try:
                    pb = json.loads(irow[5] or "{}")
                    perm_atk = int(pb.get("atk", 0) or 0)
                    perm_def = int(pb.get("def", 0) or 0)
                    raw_int_attrs = pb.get("int_attrs", {})
                    if isinstance(raw_int_attrs, dict):
                        persisted_int_attrs = {
                            str(k): int(v or 0)
                            for k, v in raw_int_attrs.items()
                            if int(v or 0) != 0
                        }
                    if isinstance(pb.get("thresholds"), list):
                        threshold_override = [int(value) for value in
                                              pb["thresholds"]]
                    if "subtype" in pb:
                        subtype_override = str(pb.get("subtype") or "")
                    instance_damage_shield = bool(pb.get("damage_shields"))
                except Exception:
                    pass
                try:
                    tb = json.loads(irow[6] or "{}")
                    temp_atk = int(tb.get("atk", 0) or 0)
                    temp_def = int(tb.get("def", 0) or 0)
                    instance_damage_shield = instance_damage_shield or bool(
                        tb.get("damage_shields"))
                except Exception:
                    pass
            card_owner_id = irow[11] if irow else None
            card_location = irow[12] if irow else None
            if irow and len(irow) > 14:
                card_instance_ref = irow[14]
            if irow and len(irow) > 15:
                persisted_gem = irow[15]
        native_bstate = getattr(self, "_current_bstate", None)
        native_cost = None
        if (scid is not None and isinstance(native_bstate, dict) and
                native_bstate.get("_rules_port_attached")):
            from rules_port.static_rules import effective_cost
            native_cost = effective_cost(
                _hc._db, game.session_id.uid64, native_bstate, scid.uid.uid64)
            cost_mod = 0
        else:
            cost_mod = int(irow[7] or 0) if irow else 0
            cost_mod_json = irow[8] if irow else None
            if (scid is not None and cost_mod_json and
                    str(cost_mod_json).strip() not in ("[]", "{}", "")):
                from abilities.framework.cost_mod import cost_mod_delta
                cost_mod += cost_mod_delta(_hc._db, game.session_id.uid64,
                                           scid.uid.uid64, cost_mod_json)
            elif scid is not None and card_location != "warzone":
            # Some all-zone dynamic reductions are represented by a
            # CardCreatedEvent in gamedata.  Cards that started in the deck
            # never receive that event during this match, but their displayed
            # and charged cost still needs to reflect the metadata formula
            # (Pterobot is the canonical example).
                try:
                    from abilities.framework.cost_mod import dynamic_cost_mod_delta
                    cost_mod += dynamic_cost_mod_delta(
                        _hc._db, game.session_id.uid64, scid.uid.uid64)
                except Exception:
                    pass
        if tpl_guid != "00000000-0000-0000-0000-000000000000":
            records_template = (records_template if records_template and
                                str(records_template[0]).lower() ==
                                str(tpl_guid).lower() else None)
            if records_template:
                lethal = bool(records_template[10])
                shards = list(records_template[6])
                srow = (None, json.dumps(list(records_template[7])),
                        records_template[8])
            else:
                lethal = bool(_hc.db_card_template_lethal(tpl_guid))
                srow = _hc.db_card_template_thresholds(tpl_guid)
            if srow:
                if srow[0]:
                    try:
                        td = json.loads(srow[0])
                        shard_flags = {0:0, 1:4, 2:8, 3:16, 4:32, 5:64}
                        shards = [shard_flags.get(s, s) for s in td.get('list', [])]
                    except Exception:
                        pass
                if srow[2]:
                    try:
                        attributes = int(srow[2])
                    except Exception:
                        pass
            elif ct == _hc.game_engine.ECardTypes.Champion:
                # Champion abilities live in champion_abilities, not card_templates.
                from pvp_db import db_champion_ability_guids
                carow = db_champion_ability_guids(tpl_guid)
                if carow:
                    srow = (None, json.dumps(carow), None)
        if threshold_override is not None:
            shards = list(threshold_override)
        try:
            if records_template:
                subtype = records_template[9] or ""
            else:
                subtype = _hc.db_template_subtype(tpl_guid, conn=_hc._db) or ""
        except Exception:
            subtype = ""
        if subtype_override is not None:
            subtype = subtype_override
        # Ability list: the instance's persisted list wins (Shift moves
        # abilities between cards).  An empty list is authoritative for
        # ordinary cards: a consumed ONE-SHOT must stay gone when the card is
        # rebuilt for CardUpdated.  Champions are the exception because their
        # game_cards rows are seeded with [] while their signature powers live
        # in champion_abilities rather than card_templates.
        ab_src = inst_abilities_json
        if (not ab_src or str(ab_src).strip() in ("null", "{}")
                or (str(ab_src).strip() == "[]"
                    and ct == _hc.game_engine.ECardTypes.Champion)):
            ab_src = srow[1] if srow else None
        ability_guids = []
        abilities = []
        if ab_src:
            try:
                parsed = json.loads(ab_src)
                abilities = [_hc.game_engine.ResourceId.from_str(g) for g in parsed]
                ability_guids = [g.lower() for g in parsed]
            except Exception:
                pass
        # Socketed-gem abilities come from the current deck socket, not from
        # the denormalized decks.gem_abilities cache.  The cache can predate a
        # deck edit (which was how a Speed gem retained an old Rage ability).
        # Call through the production class explicitly: focused test stubs
        # intentionally provide only the older helper surface, while the
        # live handler still receives the same method implementation.
        gem_instance_id = (instance_id if instance_id is not None
                           else card_instance_ref)
        gem_type = _hc.HCPHandler._card_gem_type(
            self, game, scid, gem_instance_id, persisted_gem=persisted_gem)
        display_gems = int(gem_type or 0)
        if (display_gems > 0x3FF and
                not (display_gems & _hc.EMPTY_SOCKET_GEMS)):
            # Older deck saves lost the packed-format flag but retained the
            # socket payload. Restore the marker in the client projection so
            # its socket renderer decodes every slot instead of treating the
            # combined value as one legacy gem enum.
            display_gems |= _hc.EMPTY_SOCKET_GEMS
        if gem_type:
            gem_guids = _hc.db_gem_ability_guids(gem_type, conn=_hc._db)
            for gem_guid in gem_guids:
                gem_guid = str(gem_guid).lower()
                if gem_guid not in ability_guids:
                    ability_guids.append(gem_guid)
                    abilities.append(_hc.game_engine.ResourceId.from_str(gem_guid))
            # Persist the merged list for the active game instance so combat
            # statics and subsequent pushes see the same gem ability.
            if scid is not None and gem_guids:
                try:
                    current = json.loads(inst_abilities_json or "[]")
                except Exception:
                    current = []
                current = [str(g).lower() for g in current]
                merged = list(current)
                if not merged:
                    merged = list(ability_guids)
                else:
                    for gem_guid in gem_guids:
                        if str(gem_guid).lower() not in merged:
                            merged.append(str(gem_guid).lower())
                if merged != current:
                    _hc.db_set_card_abilities(
                        game.session_id.uid64, scid.uid.uid64,
                        json.dumps(merged), conn=_hc._db)
                    # _card_full_data() is also called immediately before the
                    # battle state is persisted.  That save uses the separate
                    # game_session connection, so leave the shared connection
                    # transaction closed or it can deadlock on its own write
                    # (especially during the opening hand when socketed cards
                    # are re-pushed).
                    _hc._db.commit()
        # Encounter scene statics are continuous effects. Apply them silently
        # when a card is materialized in the relevant zone, using the granted
        # ability's typed target filter. Beast Crossing therefore affects only
        # Wild troops in the warzone, including troops created later, without
        # ever placing those grants on the chain.
        if scid is not None and card_location == "warzone":
            from abilities.framework.targeting import evaluate_card_filter
            card_subtype = subtype or ""
            # Scene target filters can inspect dynamic IntAttr markers (for
            # example, Taming Dire Toad only grants Untamed to non-Tamed
            # Dire Toads).  Reconstruct those markers from the card's current
            # ability list before evaluating the scene filter.
            scene_int_attrs = {}
            scene_int_attrs.update(persisted_int_attrs)
            for _ag in ability_guids:
                for _eg, _et, _ep in _hc.db_ability_effect_rows(_ag, conn=_hc._db):
                    if _et != "CardModifierAbilityEffectTemplate":
                        continue
                    try:
                        _pd = json.loads(_ep or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        _pd = {}
                    if str(_pd.get("property", "")).lower() != "intattr":
                        continue
                    _attr = str(_pd.get("attribute") or "")
                    if not _attr and "untamed" in str(_pd.get("text") or "").lower():
                        _attr = "Untamed"
                    if _attr:
                        scene_int_attrs[_attr] = 1
            if scene_int_attrs.get("Tamed", 0) > 0:
                scene_int_attrs.pop("Untamed", None)
            scene_added = []
            for scene_ag in ((getattr(self, "_scene_global_ability_guids", []) or [])
                             + (getattr(self, "_scene_targeted_ability_guids", []) or [])):
                target_ids_json = _hc.db_ability_target_template_ids(
                    scene_ag, conn=_hc._db)
                try:
                    target_ids = json.loads(target_ids_json or "[]")
                except (TypeError, ValueError):
                    target_ids = []
                scene_ag = str(scene_ag).lower()
                # Targeted encounter passives retain the setup card's
                # controller.  This is important for filters such as
                # IsControlledBy (the Taming Dire Toad passive means the
                # controller's Dire Toads, not every Dire Toad in play).
                scene_card = {
                    "card_uid": scid.uid.uid64,
                    "card_type": ct_name,
                    "location": card_location,
                    "shards": shards,
                    "name": name,
                    "user_id": card_owner_id,
                    "subtype": card_subtype,
                    "attributes": int(attributes | inst_attrs | temp_attrs),
                    "int_attrs": scene_int_attrs,
                }
                if scene_ag in (getattr(self, "_scene_targeted_ability_owners", {}) or {}):
                    scene_card["src_owner_id"] = (
                        self._scene_targeted_ability_owners[scene_ag])
                qualifies = False
                for target_id in target_ids:
                    filter_json = _hc.db_target_template_filter(
                        target_id, conn=_hc._db)
                    try:
                        filter_data = json.loads(filter_json or "{}")
                    except (TypeError, ValueError):
                        filter_data = {}
                    if evaluate_card_filter(scene_card, filter_data,
                                            scid.uid.uid64):
                        qualifies = True
                        break
                if qualifies and scene_ag not in ability_guids:
                    ability_guids.append(scene_ag)
                    abilities.append(_hc.game_engine.ResourceId.from_str(scene_ag))
                    scene_added.append(scene_ag)
            if scene_added:
                _hc.db_set_card_abilities(
                    game.session_id.uid64, scid.uid.uid64,
                    json.dumps(ability_guids), conn=_hc._db)
                _hc._db.commit()
        # Effective attributes = static template attributes + any attributes
        # granted by the card's CURRENT passives. Attributes render as icons on
        # the card and must be present the whole time (not computed only during
        # combat). E.g. Gemsoul Feeder's "This gets Lifedrain in all zones"
        # passive (44605164) grants SpiritDrain via a CardModifier
        # AttributeModifier.
        # Instance-persisted attributes (apply_attribute_grant writes the
        # template attrs + any granted keywords into game_cards.card_attributes,
        # e.g. Inner Conflict's permanent "can't attack or block"). Without
        # this OR the icons vanish on the next CardUpdated re-push.
        attributes |= inst_attrs | temp_attrs
        # Permanent + this-turn buffs (apply_card_stat_mod) also contribute.
        atk += atk_mod + perm_atk + temp_atk
        def_ += def_mod + perm_def + temp_def
        # Cost modifiers (static card_cost_mod + dynamic formula entries).
        cost = (int(native_cost) if native_cost is not None else
                max(0, cost + cost_mod))
        # Continuous static abilities (WhileCardInPlay / Permanent): dynamic
        # self-bonuses ("+2/+2 for each card in your hand"), auras ("Troops
        # you control have +2/+2") and zone-wide cost modifiers ("Your
        # artifacts in all zones have cost -1").  Computed from gamedata so the
        # displayed card and its playable cost always match the current board.
        if scid is not None and tpl_guid != "00000000-0000-0000-0000-000000000000":
            try:
                from rules_port.static_rules import effective_deltas
                bstate = getattr(self, "_current_bstate", None)
                if bstate is None:
                    try:
                        raw_state = _hc.db_session_turn_order(
                            game.session_id.uid64, conn=_hc._db)
                        data = json.loads(raw_state) if raw_state else {}
                        bstate = data if isinstance(data, dict) else {}
                    except Exception:
                        bstate = {}
                    self._current_bstate = bstate
                static = effective_deltas(_hc._db, game.session_id.uid64, bstate,
                                          scid.uid.uid64)
                atk += static["atk"]
                def_ += static["def"]
                if native_cost is None:
                    cost = max(0, cost + static["cost_mod"])
                attributes |= static["attrs"]
            except Exception:
                pass
        # TEMPORARY combat damage: show the defense reduced by damage taken
        # (the client colors values below the template base red). The troop
        # "heals" when card_damage is cleared at its controller's Prep.
        def_ = max(0, def_ - dmg)
        shards = [int(shard) for shard in shards if shard is not None]
        game.card_defs[scid] = _hc.game_engine.CardDef(
            name, ct, cost, atk, def_, shards, abilities, attributes, lethal,
            subtype=subtype)
        if scid is not None:
            from rules_port.ability_usage import remaining_uses_per_game
            ability_uses = remaining_uses_per_game(
                _hc._db, game.session_id.uid64, scid.uid.uid64, ability_guids,
                getattr(self, "_current_bstate", None) or {},
                champion=ct == _hc.game_engine.ECardTypes.Champion)
            game.card_defs[scid].uses_per_game_counts = {
                _hc.game_engine.ResourceId.from_str(guid): int(value)
                for guid, value in ability_uses.items()
            }
        cooldowns = {}
        if scid is not None:
            try:
                from pvp_db import db_card_cooldown_counts
                cooldowns.update(db_card_cooldown_counts(
                    game.session_id.uid64, scid.uid.uid64, conn=_hc._db))
            except Exception:
                pass
            state = getattr(self, "_current_bstate", None) or {}
            prefix = f"{int(scid.uid.uid64)}:"
            for key, value in (state.get("champion_ability_cooldowns") or {}).items():
                if str(key).startswith(prefix):
                    guid = str(key).split(":", 1)[1].lower()
                    try:
                        remaining = int(value or 0)
                    except (TypeError, ValueError):
                        continue
                    if remaining > 0:
                        cooldowns[guid] = remaining
        game.card_defs[scid].cooldown_counts = {
            _hc.game_engine.ResourceId.from_str(guid): int(value)
            for guid, value in cooldowns.items() if int(value or 0) > 0
        }
        # Tunneling is a card IntAttr in the template TAC, not an
        # ECardAttributes bit. Keep it on CardDef so every CardUpdated carries
        # the same value the original client reads from CardTemplate.tac.
        try:
            if ((getattr(getattr(game, "session", None),
                         "_rules_port_session", None) is not None) or
                    (getattr(self, "_current_bstate", None) or {}).get(
                        "_rules_port_attached")):
                from rules_port.tunneling import tunneling_value
            else:
                from abilities.framework.effects.counters import tunneling_value
            game.card_defs[scid].tunneling = tunneling_value(
                _hc._db, tpl_guid, persisted_int_attrs)
        except Exception:
            game.card_defs[scid].tunneling = int(
                persisted_int_attrs.get("Tunneling", 0) or 0)
        game.card_defs[scid].damage_shield = instance_damage_shield
        # IntAttr modifiers are transmitted separately from keyword
        # attributes. Preserve metadata-defined Untamed on the final card
        # definition (the definition above replaces any earlier placeholder)
        # so the Taming Sphere's IntAttrFilter can select this Dire Toad.
        if scid is not None:
            untamed = False
            for _ag in ability_guids:
                _has_untamed = any(
                    _et == "CardModifierAbilityEffectTemplate" and
                    "untamed" in str(_ep or "").lower()
                    for _eg, _et, _ep in _hc.db_ability_effect_rows(
                        _ag, conn=_hc._db))
                if _has_untamed:
                    untamed = True
                    break
            int_attrs = dict(persisted_int_attrs)
            if untamed:
                int_attrs.setdefault("Untamed", 1)
            # Taming is a persistent marker and overrides the scene's static
            # non-Tamed -> Untamed grant for this card instance.
            if int_attrs.get("Tamed", 0) > 0:
                int_attrs.pop("Untamed", None)
            game.card_defs[scid].int_attrs = int_attrs
        # Card.EscalationCount is per card instance (C# starts it at one and
        # Session.Escalate increments the selected card). Carry the persisted
        # count into CardUpdated for every card, regardless of its text.
        if scid is not None:
            from rules_port.statistics import card_escalation_count
            game.card_defs[scid].escalation = card_escalation_count(
                _hc._db, game.session_id.uid64,
                getattr(self, "_current_bstate", None) or {},
                scid.uid.uid64)
        # Counters + voided-card relationships must survive every re-push: the
        # client renders counter badges and the exile's voided-card link from
        # CardUpdated, so carry them on the CardDef (push_card_updated reads
        # them by default).
        try:
            from abilities.framework.effects.counters import card_counters_full
            c_counts, c_guids = card_counters_full(
                _hc._db, game.session_id.uid64, scid.uid.uid64)
            if c_counts:
                cc = {}
                for cname, cnum in c_counts.items():
                    g = c_guids.get(cname)
                    if g:
                        cc[str(g)] = int(cnum)
                game.card_defs[scid].counters = cc
        except Exception:
            pass
        try:
            cbstate = getattr(self, "_current_bstate", None) or {}
            vby = cbstate.get("voided_by") or {}
            linked = [int(u) for u in vby.get(str(scid.uid.uid64), [])]
            if linked:
                game.card_defs[scid].related_cards = [
                    _hc.game_engine.SessionCardId(_hc.game_engine.UID(u))
                    for u in linked]
        except Exception:
            pass
        # The card's ORIGINAL (pre-transform) template — read from the
        # game_cards row (per-instance, lasts the whole game), NOT derived here.
        # push_card_updated sends it as CardUpdated.OrigTemplate so the client's
        # examine panel can show the original card ("Transformed From <Name>").
        game.card_defs[scid].orig_template = orig_tpl
        # Carry the socketed gem on the CardDef so EVERY later push_card_updated
        # (warzone entry, trigger "shake", phase re-push) renders the gem even
        # when the caller omits gems= — e.g. Shamed Gladiator must keep its gem
        # once it resolves to the board.
        if scid is not None and game.card_defs.get(scid) is not None:
            game.card_defs[scid].gems = display_gems
            # Rage display: CardUpdated.rage drives the client's Rage icon, and
            # it was never populated (always 0).  Use the same authoritative
            # static-ability evaluation as combat (template rage_value + granted
            # "Rage 1 in all zones" gems, gated by the ability's threshold
            # condition) so the card shows the Rage it actually has.
            try:
                from rules_port.static_rules import effective_stats
                bstate = getattr(self, "_current_bstate", None) or {}
                _atk, _def, _attrs, _flags, rage = effective_stats(
                    _hc._db, game.session_id.uid64, bstate, scid.uid.uid64)
                game.card_defs[scid].rage = int(rage or 0)
            except Exception:
                game.card_defs[scid].rage = 0
        return tpl_guid, ct, name, cost, atk, def_, display_gems

    def _revert_to_template(self, session, card_uid, template_guid, user_id=None):
        """Reversion: reset a card instance to its ORIGINAL template's canonical
        data.

        A transformed card (e.g. Spiritbound Spy -> Phantom) keeps the same
        card_uid but its template data changed; Reversion restores the instance
        to the template it was CREATED from (original_template_guid), resetting
        template_guid / card_type / card_attributes / card_abilities / stat mods
        / uses as though it were a fresh card. When `template_guid` is given and
        differs, it is used instead (an explicit Revert-to-X effect).
        """
        import hconnect_server as _hc
        from pvp_db import db_card_original_template, db_card_template_full, db_card_revert_to_template
        # The instance's original identity wins; fall back to the given template.
        original = db_card_original_template(session.session_id, card_uid) or ""
        target = original if original else (template_guid or "")
        if not target:
            return False
        trow = db_card_template_full(target)
        if not trow:
            return False
        ctype, cost, atk, def_, thresh_json, abilities_json, attributes = trow
        abilities_json = abilities_json or "[]"
        try:
            import json as _rj
            ability_guids = [g.lower() for g in _rj.loads(abilities_json)]
        except Exception:
            ability_guids = []
        attributes = int(attributes or 0)
        db_card_revert_to_template(session.session_id, card_uid, attributes,
                                   abilities_json, target, ctype)
        _hc.log_req(f"    Revert {hex(card_uid)} to template {target[:8]} "
                f"(type={ctype} attrs={attributes} abilities={ability_guids})")
        return True

    def _card_ability_list(self, session, card_uid):
        """Current ability GUID list for a card instance (from card_abilities)."""
        import hconnect_server as _hc
        return _hc.db_card_ability_list(session.session_id, card_uid)

    def _sync_instance_card_data(self, session, card_uid, template_guid,
                                 commit=True):
        """Populate a card instance's ability/attribute/uses data from its template.

        card_templates is the CANONICAL source; game_cards.card_abilities /
        card_attributes / card_uses are the per-instance working copy. This is
        called when a card instance is created (and by Reversion to reset it).
        Uses is reset to {} (a fresh instance has no usage history).
        """
        import hconnect_server as _hc
        if not template_guid:
            return
        ab_json, attrs = _hc.db_get_card_abilities(template_guid)
        ab_json = ab_json or "[]"
        attrs = int(attrs or 0)
        try:
            ability_guids = [g.lower() for g in json.loads(ab_json)]
        except Exception:
            ability_guids = []
        _hc.db_card_sync_abilities(
            session.session_id, card_uid, ab_json, attrs, template_guid,
            commit=commit)
        return attrs

    def _battle_setup_template_data(self, template_guid, cache):
        """Return canonical setup data for a card template, using *cache*.

        Battle deck construction creates many copies of the same templates.  A
        fresh instance still needs the template's working ability/attribute
        snapshot, but resolving that snapshot once per copy causes a SELECT and
        an UPDATE for every card.  Keep the metadata local to one battle setup
        (rather than on the handler, which serves multiple sessions) and reuse
        it for every copy.

        Returns ``(abilities_json, effective_attributes, ability_guids,
        threshold_json)`` or ``None`` when the template is missing.
        """
        import hconnect_server as _hc
        if not template_guid:
            return None
        key = str(template_guid).lower()
        if key in cache:
            return cache[key]
        row = _hc.db_card_template_thresholds(template_guid)
        if not row:
            cache[key] = None
            return None
        threshold_json, abilities_json, attributes = row
        abilities_json = abilities_json or "[]"
        try:
            ability_guids = [str(g).lower() for g in json.loads(abilities_json)]
        except Exception:
            ability_guids = []
        effective_attributes = int(attributes or 0)
        result = (abilities_json, effective_attributes, ability_guids,
                  threshold_json)
        cache[key] = result
        return result

    def _card_uses(self, session, card_uid):
        """Return the per-ability usage dict for a card instance."""
        import hconnect_server as _hc
        return _hc.db_card_uses(session.session_id, card_uid)

    def _bump_card_use(self, session, card_uid, ability_guid):
        """Increment a card instance's usage of an ability (UsesPerGame/Turn)."""
        import hconnect_server as _hc
        return _hc.db_bump_card_use(session.session_id, card_uid, ability_guid)

    def _champion_ability_uses(self, bstate):
        """Per-ability activation counts for the champion's synthetic powers."""
        uses = (bstate or {}).get("champion_ability_uses")
        return uses if isinstance(uses, dict) else {}

    def _champion_ability_available(self, bstate, ability_guid, graph=None,
                                    source_uid=None):
        """Whether an authored ``m_UsesPerGame`` champion power still has a use.

        ONE-SHOT talent powers (``uses_per_game=1``) belong to the synthetic
        champion SessionCardId, which has no ``game_cards`` row to hold
        ``card_uses``; the count lives in the shared battle state instead.
        """
        import hconnect_server as _hc
        limit = int(getattr(getattr(graph, "costs", None),
                            "uses_per_game", 0) or 0)
        guid = str(ability_guid or "").lower()
        if source_uid is None:
            champion = getattr(self, "_player_champ_scid", None)
            if isinstance(champion, _hc.game_engine.SessionCardId):
                source_uid = int(champion.uid.uid64)
        from rules_port.cooldowns import champion_cooldown_key, champion_cooldown
        cooldown_key = champion_cooldown_key(source_uid, guid)
        if cooldown_key and champion_cooldown(bstate, source_uid, guid) > 0:
            return False
        uses = self._champion_ability_uses(bstate)
        used = int(uses.get(cooldown_key, uses.get(guid, 0)) or 0)
        if limit > 0 and used >= limit:
            return False
        per_turn = int(getattr(getattr(graph, "costs", None),
                               "uses_per_turn", 0) or 0)
        if per_turn > 0 and source_uid is not None:
            from rules_port.runtime_helpers import (
                champion_ability_use_key, champion_ability_uses_this_turn)
            turn_key = champion_ability_use_key(source_uid, guid)
            if champion_ability_uses_this_turn(bstate, turn_key) >= per_turn:
                return False
        return True

    def _consume_champion_ability_use(self, bstate, ability_guid, graph=None,
                                      source_uid=None):
        """Record one activation of a champion power in the battle state."""
        import hconnect_server as _hc
        guid = str(ability_guid or "").lower()
        if source_uid is None:
            champion = getattr(self, "_player_champ_scid", None)
            if isinstance(champion, _hc.game_engine.SessionCardId):
                source_uid = int(champion.uid.uid64)
        from rules_port.cooldowns import (champion_cooldown_key,
                                          set_champion_cooldown)
        key = champion_cooldown_key(source_uid, guid) or guid
        if graph is None:
            # Callers may omit the graph; resolve the authored costs here so a
            # ONE-SHOT activation still consumes its per-game use.
            from gamedata import DEFAULT_RECORD_STORE, ability_graph
            graph = ability_graph(DEFAULT_RECORD_STORE, guid)
        costs = getattr(graph, "costs", None)
        per_game = int(getattr(costs, "uses_per_game", 0) or 0)
        uses = bstate.setdefault("champion_ability_uses", {})
        if per_game > 0:
            uses[key] = int(uses.get(key, uses.get(guid, 0)) or 0) + 1
        cooldown = int(getattr(getattr(graph, "costs", None),
                               "cooldown", 0) or 0)
        if cooldown > 0:
            set_champion_cooldown(bstate, source_uid, guid, cooldown)
        per_turn = int(getattr(getattr(graph, "costs", None),
                               "uses_per_turn", 0) or 0)
        if per_turn > 0 and source_uid is not None:
            from rules_port.runtime_helpers import (
                champion_ability_use_key,
                record_champion_ability_use_this_turn)
            record_champion_ability_use_this_turn(
                bstate, champion_ability_use_key(source_uid, guid))
        return int(uses.get(key, 0) or 0)

    def _remove_one_shot_ability(self, session, card_uid, ability_guid,
                                  game, pl_t, ai_t, bstate=None):
        """Consume a one-shot ability on this individual card instance.

        ``uses_per_game=1`` is the extracted representation of ONE-SHOT.
        Usage counters still gate activation, but the client also needs the
        current per-instance ability list updated or it will continue to show
        the used power on the card.  Recompute effective attributes and push a
        complete CardUpdated so both the server and client agree.
        """
        import hconnect_server as _hc
        meta = _hc.db_ability_activation_metadata(
            ability_guid, conn=_hc._db)
        if not meta or int(meta[1] or 0) != 1:
            return False
        row = _hc.db_card_mutation_snapshot(
            session.session_id, int(card_uid), conn=_hc._db)
        if not row:
            return False
        abilities = self._card_ability_list(session, card_uid)
        ag = str(ability_guid).lower()
        if ag not in abilities:
            return False
        abilities.remove(ag)
        base_attrs = int(_hc.db_template_attributes(row[0], conn=_hc._db) or 0)
        attributes = base_attrs
        _hc.db_set_card_abilities_and_attributes(
            session.session_id, int(card_uid), json.dumps(abilities),
            attributes, conn=_hc._db)
        _hc._db.commit()
        if game is not None:
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(card_uid)))
            _tpl, ct, _name, cost, atk, defense, gems = self._card_full_data(
                game, scid, row[0])
            if (bstate or {}).get("pvp"):
                owner = _hc.game_engine.UID.make(244, int(row[1]))
            else:
                owner = pl_t if int(row[1] or 0) != 0 else ai_t
            from abilities.framework._shared import card_collection_for_location
            game.push_card_updated(
                scid, owner, card_collection_for_location(row[2]), ct,
                template_id=_tpl, cost=cost, attack=atk, defense=defense,
                gems=gems, attributes=attributes, state=int(row[3] or 0))
        _hc.log_req(f"    One-shot ability {ag[:8]} removed from {hex(int(card_uid))}")
        return True

    def _apply_power_shifted_triggers(self, session, target_uid, game,
                                      pl_t, ai_t, bstate):
        """Dispatch the authored PowerShiftedEvent through the shared BOM."""
        import hconnect_server as _hc
        target_owner = _hc.db_card_owner_id(
            session.session_id, int(target_uid), conn=_hc._db)
        if target_owner is None:
            return ""
        return self._dispatch_native_trigger(
            game, session, pl_t, ai_t, bstate, "PowerShiftedEvent",
            int(target_uid), target_owner, target_card_id=int(target_uid))

    def _shift_ability_between(self, session, pl_t, ai_t, source_uid, target_uid,
                               ability_guid, game, bstate=None):
        """Move a granted ability from one card instance to another (ShiftPower).

        Removes `ability_guid` from the source's card_abilities, adds it to the
        target's, then recomputes both cards' effective attributes (the shifted
        ability's BOM may grant an attribute, e.g. Lifedrain -> SpiritDrain).
        Persists to game_cards and pushes CardUpdateds for both cards. The
        template remains the canonical source; a Reversion resets either card.
        """
        import hconnect_server as _hc
        # Load both instances' current ability lists.
        srow = _hc.db_card_mutation_info(
            session.session_id, int(source_uid), conn=_hc._db)
        trow = _hc.db_card_mutation_info(
            session.session_id, int(target_uid), conn=_hc._db)
        if not srow or not trow:
            return False
        src_ab = [a for a in self._card_ability_list(session, source_uid) if a != ability_guid]
        tgt_ab = self._card_ability_list(session, target_uid)
        if ability_guid not in tgt_ab:
            tgt_ab.append(ability_guid)
        # Recompute effective attributes from the (possibly changed) ability lists.
        # Include the template's static attributes.
        for row, ab, uid in ((srow, src_ab, source_uid), (trow, tgt_ab, target_uid)):
            base = int(_hc.db_template_attributes(row[0], conn=_hc._db) or 0)
            eff = base
            _hc.db_set_card_abilities_and_attributes(
                session.session_id, int(uid), json.dumps(ab), eff, conn=_hc._db)
        _hc._db.commit()
        # A power was shifted ONTO the target: resolve any of its own
        # PowerShiftedEvent triggers (e.g. Deepgaze Acolyte +1/+1). Persists
        # the buff (card_attack_mod / card_defense_mod); _card_full_data below
        # folds it into the pushed stats.
        self._apply_power_shifted_triggers(
            session, target_uid, game, pl_t, ai_t, bstate or {})
        # Push CardUpdated for both cards so the client updates the ability
        # icons + attribute icons. Build the CardDef from the template via
        # _card_full_data so the pushed card keeps its threshold pips / text
        # (a bare CardDef with shards=[] wipes the color-cost pips), then
        # override the (shifted) ability list and effective attributes.
        for row, uid, ab in ((srow, source_uid, src_ab), (trow, target_uid, tgt_ab)):
            scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(uid)))
            _tpl, ct, _name, _c, _a, _d, _gem = self._card_full_data(game, scid, row[0])
            cdef = game.card_defs[scid]
            cdef.abilities = [_hc.game_engine.ResourceId.from_str(a) for a in ab]
            base = 0
            st_val = _hc.db_template_attributes(row[0], conn=_hc._db)
            if st_val:
                base = int(st_val)
            cdef.attributes = base
            owner = pl_t if row[1] != 0 else ai_t
            # NOTE: _card_full_data already folded in the persisted
            # card_attack_mod / card_defense_mod (written by
            # _apply_power_shifted_triggers), so we must NOT add atk_mod/def_mod
            # again here — that double-counts the PowerShifted +1/+1 (the client
            # showed 4/3, then the correct 3/2 at the next phase, looking like
            # the buff was lost).
            # Carry the card's persisted state (StartedATurnOnYourSide, Tapped,
            # Attacking, ...) so the CardUpdated doesn't wipe it on the client —
            # a bare state=None would make the troops look like they just came
            # into play (summoning sickness) and unattackable.
            cstate = _hc.db_card_state_value(
                session.session_id, int(uid), conn=_hc._db)
            game.push_card_updated(scid, owner, _hc.game_engine.ECardCollections.Warzone, ct,
                                   template_id=row[0], attributes=cdef.attributes,
                                   state=cstate)
        _hc.log_req(f"    ShiftPower: {ability_guid[:8]} {hex(source_uid)} -> {hex(target_uid)} "
                f"(src_ab={src_ab} tgt_ab={tgt_ab})")
        return True

    def _activate_troop_ability(self, session, pl_t, ai_t, bstate, source_uid,
                                ability_guid, inner_bytes):
        """Activate a manual ability on a player-controlled card.

        Validates the cost/phase/uses gates, pays the resource cost, bumps the
        instance's usage, records the Shift source+target in battle state, then
        resolves the ability's BOM (ability_effects). The BOM's
        TACAbilityEffectTemplate leaf dispatches to ShiftPower generically via
        the serialized TAC (never hardcoded).
        """
        import hconnect_server as _hc
        graph = _hc._records_ability_graph(self, ability_guid)
        if graph is None:
            _hc.log_req(f"    Troop ability {ability_guid[:8]}: missing current "
                    "Records ability")
            return
        from rules_port.triggers import trigger_collection_allows
        source_location = _hc.db_card_location(
            session.session_id, int(source_uid), conn=_hc._db)
        source_row = (source_location,) if source_location is not None else None
        if not source_row or not trigger_collection_allows(
                graph.trigger_collection_flags, source_row[0]):
            _hc.log_req(f"    Troop ability {ability_guid[:8]}: source collection "
                    f"{source_row[0] if source_row else None} is not allowed")
            return
        from gamedata import AbilityInstance, ActivationData
        ability = AbilityInstance.from_graph(
            graph, source_uid=int(source_uid), owner_id=self.user_profile["id"],
            responsible_player_id=self.user_profile["id"], store=self._play_plan_store)
        casting = 64 if graph.casting_behavior == "QuickAction" else 8
        cost = int(ability.costs.activation or 0)
        upg = int(ability.costs.uses_per_game or 0)
        upt = int(ability.costs.uses_per_turn or 0)
        exh = bool(ability.costs.exhausts_card_on_use)
        variable_x = int(ability.costs.variable_activation or 0)
        variable_min = int(ability.costs.variable_minimum or 0)
        resources = bstate.get("player_resources", 0)
        x_cost = 0
        if variable_x:
            x_cost = self._extract_int32_field(inner_bytes, "m_ResourceXCost")
            x_cost = max(0, int(x_cost or 0))
            if x_cost < variable_min:
                _hc.log_req(f"    Troop ability {ability_guid[:8]}: X={x_cost} below minimum {variable_min}")
                return
        from pvp_db import (db_card_ability_use_counts,
                            db_card_cooldown_counts,
                            db_record_card_ability_use,
                            db_set_card_cooldown)
        used_game, used_turn = db_card_ability_use_counts(
            session.session_id, source_uid, ability_guid,
            bstate.get("turn_number", 1), conn=_hc._db)
        if cost + x_cost > resources:
            _hc.log_req(f"    Troop ability {ability_guid[:8]}: need {cost + x_cost} resources, have {resources}")
            return
        if upg and used_game >= upg:
            _hc.log_req(f"    Troop ability {ability_guid[:8]}: uses_per_game exhausted ({used_game})")
            return
        if upt and used_turn >= upt:
            _hc.log_req(f"    Troop ability {ability_guid[:8]}: uses_per_turn exhausted ({used_turn})")
            return
        if (graph.costs.cooldown and int(db_card_cooldown_counts(
                session.session_id, source_uid, conn=_hc._db).get(
                    ability_guid, 0) or 0) > 0):
            _hc.log_req(f"    Troop ability {ability_guid[:8]}: cooldown active")
            return
        if exh:
            # Authoritative exhaust-as-cost gate (mirrors _affordable_troop_abilities).
            activation_info = _hc.db_card_activation_info(
                session.session_id, int(source_uid), conn=_hc._db)
            cstate = int(activation_info[0] or 0) if activation_info else 0
            cattrs = int(activation_info[1] or 0) if activation_info else 0
            if (cstate & _hc.game_engine.ECardStates.Tapped
                    or (not (cstate & _hc.game_engine.ECardStates.StartedATurnOnYourSide)
                        and not (cattrs & _hc.game_engine.ECardAttributes.Speed))):
                _hc.log_req(f"    Troop ability {ability_guid[:8]}: cannot exhaust "
                        f"{hex(source_uid)} (summoning sick/tapped)")
                return
        # Ability conditions gate activation (client CanActivateAbilityBase:
        # TriggerCondition.IsValid). E.g. Droo's Colossal Walker only activates
        # while exhausted.
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
                ability_source_uid=int(source_uid),
                ability_source_owner_id=self.user_profile["id"])
            if not trigger_condition_met(graph.source.to_dict(), cond_ctx):
                _hc.log_req(f"    Troop ability {ability_guid[:8]}: ability condition not met")
                return
        if bstate.get("_rules_port_attached"):
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        phase = _be.current_phase(bstate)
        if casting != 64 and (
                bstate.get("turn_player") != _be.PLAYER
                or phase not in (_hc.game_engine.ETurnPhases.FirstMainPhase,
                                  _hc.game_engine.ETurnPhases.SecondMainPhase)
                or not _be.stack_empty(bstate)):
            _hc.log_req(f"    Troop ability {ability_guid[:8]}: basic action not legal now")
            return
        # Extract every selected card from the transaction. A manual ability
        # can carry both payment targets (Concubunny's ready Shin'hare) and a
        # separate effect target, so the selected cards must be split by their
        # metadata target templates instead of treating the last UID as the
        # effect target.
        selected_uids = []
        if isinstance(inner_bytes, bytes):
            for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                    if (uid64 & 0xFF) == 1:
                        uid64 = int(uid64)
                        if uid64 != source_uid:
                            selected_uids.append(uid64)
                except Exception:
                    continue

        # Validate and split metadata-declared card payment targets first.
        # This is the same distinction used by the PvP activation path: an
        # ExhaustTarget is a cost, not an effect target, and must be paid
        # before the BOM resolves.  The live target filter is re-evaluated so
        # a stale transaction cannot exhaust a troop that became tapped after
        # the option list was sent.
        from rules_port.builder import AbilityBuilder
        from rules_port.costs import (
            AbilityCostTarget, validate_cost_target_selection,
        )
        builder = AbilityBuilder.from_graph(
            graph, store=getattr(self, "_play_plan_store", None))
        cost_selections = []
        cost_target_uids = []
        used_cost_uids = set()
        for cost_ref in builder.cost_targets:
            if cost_ref.target is None:
                _hc.log_req(f"    REJECTED troop ability {ability_guid[:8]}: "
                        f"missing payment target template {cost_ref.guid}")
                return
            minimum = cost_ref.minimum
            maximum = cost_ref.maximum
            auto_source = cost_ref.is_source_auto_target
            if auto_source:
                candidates = [int(source_uid)]
            else:
                candidates = builder.target_candidates(
                    _hc._db, session.session_id, self.user_profile["id"],
                    cost_ref.target, int(source_uid), both_players=False,
                    champions=self._champion_targets(), battle_state=bstate)
            candidate_set = {int(uid) for uid in candidates}
            available = ([int(source_uid)] if auto_source else
                         [int(uid) for uid in selected_uids
                          if int(uid) in candidate_set and
                          int(uid) not in used_cost_uids])
            cost_target = AbilityCostTarget(
                index=cost_ref.index, kind=cost_ref.kind,
                guid=cost_ref.guid, minimum=minimum, maximum=maximum,
                is_source_auto_target=auto_source,
                candidates=tuple(candidates),
                allow_best_effort_minimum=cost_ref.allow_best_effort_minimum)
            if (len(available) < minimum and
                    not cost_target.allow_best_effort_minimum):
                _hc.log_req(f"    REJECTED troop ability {ability_guid[:8]}: "
                        f"missing/illegal payment target for {cost_ref.guid}")
                return
            chosen = available if maximum < 0 else available[:maximum]
            chosen = validate_cost_target_selection(
                _hc._db, session.session_id, self.user_profile["id"],
                int(source_uid), cost_target, chosen,
                champions=self._champion_targets(), battle_state=bstate)
            if chosen is None:
                _hc.log_req(f"    REJECTED troop ability {ability_guid[:8]}: "
                        f"invalid metadata payment target {cost_ref.guid}")
                return
            used_cost_uids.update(chosen)
            cost_target_uids.extend(chosen)
            cost_selections.append(({"kind": cost_ref.kind}, tuple(chosen)))

        # Work out which target templates are effect targets (as opposed to
        # automatic source/player targets or card-cost targets), then validate
        # the transaction against the same gamedata filter used to build the
        # picker. This keeps activation data-driven and prevents an invalid or
        # missing target from silently resolving against the source card.
        explicit_templates = builder.targets(
            requires_input=True, include_costs=False)
        if explicit_templates:
            # Taming Sphere has one required explicit target. For abilities
            # with several templates, accept the selected card if it is legal
            # for any effect target, while excluding cards already consumed as
            # payment targets.
            target_uid = None
            for target in explicit_templates:
                candidates = builder.target_candidates(
                    _hc._db, session.session_id, self.user_profile["id"],
                    target, int(source_uid),
                    champions=self._champion_targets(),
                    battle_state=bstate)
                for candidate in selected_uids:
                    if (int(candidate) not in used_cost_uids and
                            int(candidate) in {int(c) for c in candidates}):
                        target_uid = int(candidate)
                        break
                if target_uid is not None:
                    break
            valid_target = target_uid is not None
            if not valid_target:
                _hc.log_req(f"    REJECTED troop ability {ability_guid[:8]}: "
                        f"missing/illegal metadata target {target_uid}")
                return
        else:
            # Any card UIDs in a cost TargetMap (sacrifice/void/etc.) are not
            # effect targets consumed by the BOM resolver.
            target_uid = None

        # Expose payment selections to metadata-driven variables such as
        # Construction Plans, then pay them before resolving the ability.
        if cost_target_uids:
            bstate.setdefault("ability_lists", {})["ExhaustedCards"] = [
                int(uid) for uid in cost_target_uids
                if any(spec["kind"] == "exhaust" and int(uid) in selected
                       for spec, selected in cost_selections)]

        # Pay the resource cost only after target validation succeeds.
        if not self._card_costs_valid(
                session, cost_selections, self.user_profile["id"]):
            _hc.log_req(f"    REJECTED troop ability {ability_guid[:8]}: invalid card cost")
            return
        from rules_port.resources import apply_resource_change
        resource_change = apply_resource_change(
            bstate, "player", "currentresource", -(cost + x_cost))
        if variable_x:
            bstate["x_cost"] = x_cost
        # Leaves read the resolved target from bstate; self-targeting manual
        # abilities (Living Totem, Ascetic Aspirant) default to the source.
        bstate["player_mod_target"] = target_uid if target_uid else source_uid
        bstate["player_transform_target"] = target_uid if target_uid else source_uid
        bstate["player_spell_target"] = target_uid
        bstate["resolving_ability"] = ability_guid
        bstate["resolving_source_uid"] = source_uid
        bstate["resolving_owner_id"] = self.user_profile["id"]
        bstate["player_shift_source"] = source_uid
        bstate["player_shift_target"] = target_uid
        # Bump the instance's usage of this ability (UsesPerGame/Turn limits).
        if upg or upt:
            db_record_card_ability_use(
                session.session_id, source_uid, ability_guid,
                bstate.get("turn_number", 1), conn=_hc._db,
                uses_per_game=bool(upg), uses_per_turn=bool(upt))
        if graph.costs.cooldown:
            db_set_card_cooldown(
                session.session_id, source_uid, ability_guid,
                graph.costs.cooldown, conn=_hc._db)
        _be.save_state(session, bstate)
        # Resolve the typed ability through RulesPort.
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        if cost_selections:
            self._apply_card_play_costs(
                game, session, bstate, pl_t, ai_t, cost_selections,
                expected_owner_id=self.user_profile["id"])
        from rules_port.resolution import resolve_port_ability
        log = resolve_port_ability(
            self, game, session, _hc._db, pl_t, ai_t, bstate, ability_guid,
            source_uid=source_uid,
            owner_id=bstate.get("resolving_owner_id", 0),
            target_map=({0: int(target_uid)}
                        if target_uid is not None else {}))
        self._remove_one_shot_ability(
            session, source_uid, ability_guid, game, pl_t, ai_t, bstate)
        # Manual troop abilities resolve directly rather than as stack items.
        # Apply state-based effects here as well, so a stat reduction that
        # lowers a troop's effective defense to zero sends it to the crypt
        # before the player receives another priority window.
        if _be.stack_empty(bstate):
            from rules_port.context import EffectContext
            from rules_port.death_effects import state_based_deaths
            state_based_deaths(EffectContext.from_rules_port(
                game, session, _hc._db, self, pl_t, ai_t, bstate,
                "state_based_death", ability=None))
        _be.save_state(session, bstate)
        # Exhaust-as-cost: tap the source card now that the ability resolved
        # (e.g. Prairie Scout's [ACT] ... — the tap is part of the cost).
        if exh:
            _hc.db_update_card_state(
                session.session_id, int(source_uid),
                set_bits=_hc.game_engine.ECardStates.Tapped, conn=_hc._db)
            _hc._db.commit()
            scid_src = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(source_uid)))
            source_details = _hc.db_card_zone_details(
                session.session_id, int(source_uid), conn=_hc._db)
            _tpl, ct2, _n, _c, atk, def_, _g = self._card_full_data(
                game, scid_src, source_details[0] if source_details else None)
            current_state = _hc.db_card_state_value(
                session.session_id, int(source_uid), conn=_hc._db)
            game.push_card_updated(scid_src, pl_t, _hc.game_engine.ECardCollections.Warzone,
                                   ct2, template_id=_tpl, attack=atk, defense=def_,
                                   state=int(current_state or _hc.game_engine.ECardStates.Tapped))
            # Exhausting an activated troop emits the data-defined tap event.
            # Granted triggers such as Spider Nest listen for this event.
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate, "CardTappedEvent",
                int(source_uid), self.user_profile["id"])
        # Push resource change + player update + greenlight so the player keeps
        # priority after activating the ability.
        ec = _hc.game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
        ec.player_id = pl_t
        ec.operation = 2
        ec.delta = cost + x_cost
        ec.new_value = bstate.get("player_resources", 0)
        game._push(ec)
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        # A discard leaf is a client-side class-23 follow-up, not an ordinary
        # main-phase target.  Push the picker after the draw/resource events
        # and hold the normal GreenLight/options until the selected card has
        # actually moved to discard.
        discard_prompted = False
        if self._ability_requires_discard(ability_guid):
            self._push_discard_prompt(
                game, session, pl_t, ai_t, bstate, ability_guid)
            discard_prompted = bool(bstate.get("pending_discard_ability"))
        if (not discard_prompted and
                not bstate.get("pending_choice") and
                not bstate.get("pending_deck_search") and
                not bstate.get("pending_trigger") and
                not bstate.get("pending_conversation")):
            game.push_green_light(pl_t, self._priority_context_for(
                _be.current_phase(bstate), bstate))
        if game.events:
            self._send_battle_events(session, game, pl_t)
        if variable_x:
            bstate.pop("x_cost", None)
            _be.save_state(session, bstate)
        _hc.log_req(f"    Troop ability {ability_guid[:8]} activated on {hex(source_uid)} "
                f"(cost {cost}+{x_cost}, target={hex(target_uid) if target_uid else 'none'}): {log}")

    def _ai_draw_card(self, game, session, ai_t, battle_state):
        import hconnect_server as _hc
        import ai
        if battle_state.get("_rules_port_attached"):
            from rules_port.context import EffectContext
            battle_state.pop("_rules_port_deck_out_owner", None)
            context = EffectContext.from_rules_port(
                game, session, _hc._db, self, game.player_uid, ai_t,
                battle_state, "", ability=None)
            context.draw(1, owner=0)
            from rules_port.persistence import save_state
            save_state(session, battle_state)
            return bool(battle_state.pop("_rules_port_deck_out_owner", None))
        return ai.ai_draw_card(self, game, session, ai_t, battle_state)

    def _move_deck_to_hand(self, game, session, pl_t, card_uid, tpl_guid, instance_id=None):
        """Move a specific card from the deck to the player's hand (pushes into
        `game`). Used by !addcard."""
        import hconnect_server as _hc
        _hc.db_move_card_to_location(
            session.session_id, int(card_uid), "hand", position=100, conn=_hc._db)
        _hc._db.commit()
        scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(int(card_uid)))
        tpl_guid, ct, name, cost, atk, def_, gem_type = self._card_full_data(
            game, scid, tpl_guid, instance_id)
        game.push_card_drawn(scid, pl_t, 1)
        game.push_card_updated(scid, pl_t, _hc.game_engine.ECardCollections.Hand, ct,
                               attack=atk, defense=def_, cost=cost,
                               template_id=tpl_guid, gems=gem_type)
        _hc.log_req(f"    Added card {card_uid} ({name}) from deck to hand")

    def _player_draw_card(self, game, session, pl_t, owner_id=None):
        """Draw the top card of the human's deck into hand (pushes into `game`)."""
        import hconnect_server as _hc
        _be = self._checkpoint_engine(session)
        is_pvp = (session and (session.session_name or "").startswith("tourney-"))
        if is_pvp:
            from services.tournament_game import pvp_load_state, pvp_save_state
            bstate = pvp_load_state(session)
            if not bstate:
                _hc.log_req("    PvP debug draw ignored: no PvP state")
                return False
        else:
            bstate = _be.load_state(session)
        self._current_bstate = bstate
        # Practice/campaign uses the local profile id; tournament PvP uses the
        # ServicePlayer/reckoning pid stored in game_cards.  The latter cannot
        # be inferred from user_profile (it is a different identifier).
        if owner_id is None:
            if bstate.get("pvp"):
                owner_id = int(pl_t.uid64) >> 8
            else:
                owner_id = self.user_profile["id"]
        owner_id = int(owner_id)
        if bstate.get("pvp"):
            pids = [int(p) for p in (bstate.get("pids") or [])]
            opponent_id = next((p for p in pids if p != owner_id), None)
            ai_t = (_hc.game_engine.UID.make(244, opponent_id)
                    if opponent_id is not None else _hc.game_engine.UID.make(244, owner_id))
            draw_pl_t = _hc.game_engine.UID.make(244, owner_id)
        else:
            ai_t = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
            draw_pl_t = pl_t
        if bstate.get("_rules_port_attached"):
            from rules_port.context import EffectContext
            bstate.pop("_rules_port_deck_out_owner", None)
            context = EffectContext.from_rules_port(
                game, session, _hc._db, self, draw_pl_t, ai_t, bstate,
                "", ability=None)
            context.draw(1, owner=owner_id)
            if is_pvp:
                from services.tournament_game import pvp_save_state
                pvp_save_state(session, bstate)
            else:
                _be.save_state(session, bstate)
            return bool(bstate.pop("_rules_port_deck_out_owner", None))
        turn = bstate.get("turn_number", 1)
        if bstate.get("player_draws_turn") != turn:
            bstate["player_draws_this_turn"] = 0
            bstate["player_draws_turn"] = turn
        bstate["player_draws_this_turn"] = int(bstate.get("player_draws_this_turn", 0)) + 1
        # Replacement: "If you would draw a card..." (The Transcended) and
        # "If this would enter a hand..." (Booby Trap) — a resolved trigger
        # replaces the draw with its own effect.
        repl_draw = self._dispatch_native_trigger(
            game, session, draw_pl_t, ai_t, bstate,
            "CardWouldBeDrawnEvent", None, owner_id)
        row = _hc.db_deck_top_card_details(
            session.session_id, owner_id, conn=_hc._db)
        if not row:
            if is_pvp:
                _hc.log_req(f"    PvP debug draw: pid {owner_id} deck empty")
                return False
            # Deck-out: a player who must draw with an empty deck loses.
            self._finish_battle_gameend(
                session, False, winners=[ai_t], losers=[pl_t])
            _hc.log_req("    Game over: player deck empty on draw (AI wins)")
            return True
        repl_zone = self._dispatch_native_trigger(
            game, session, draw_pl_t, ai_t, bstate,
            "CardWouldEnterZoneEvent", row[1], owner_id)
        if repl_draw or repl_zone:
            if is_pvp:
                from services.tournament_game import pvp_save_state
                pvp_save_state(session, bstate)
            else:
                _be.save_state(session, bstate)
            _hc.log_req(f"    Player draw replaced (would-draw/would-enter trigger)")
            return
        scid = _hc.game_engine.SessionCardId(_hc.game_engine.UID(row[1]))
        _hc.db_move_card_row_to_hand(session.session_id, row[0], conn=_hc._db)
        _hc._db.commit()
        tpl_guid, ct, name, cost, atk, def_, gem_type = self._card_full_data(game, scid, row[3], row[2])
        game.push_card_drawn(scid, draw_pl_t, 1)
        game.push_card_updated(scid, draw_pl_t, _hc.game_engine.ECardCollections.Hand, ct,
                               attack=atk, defense=def_, cost=cost,
                               template_id=tpl_guid, gems=gem_type)
        # Entering a hand is its own authoritative zone event.  This is
        # distinct from CardDrawnEvent: cards such as Reginald trigger from
        # entering Hand even when the card was put there by an effect rather
        # than a normal draw.  The source owner is the actual deck owner, so
        # the trigger follows the card if control of the deck changed.
        self._dispatch_native_trigger(
            game, session, draw_pl_t, ai_t, bstate,
            "CardEnteredZoneEvent", row[1], owner_id,
            event_source_collection="deck",
            event_destination_collection="hand")
        # Fire "when you draw" triggers.  The client's CardDrawnEvent carries
        # SourceCardId = the drawing CHAMPION and TargetCardId = the drawn
        # card, and the trigger conditions test them per m_TriggerTest (e.g.
        # Twisted Fate's "when you draw a card, bury..." checks IsHero on the
        # source and IsType on the target).
        champ_uid = None
        if bstate.get("pvp"):
            for pid, cuid in (bstate.get("champ_map") or {}).items():
                if int(pid) == owner_id:
                    champ_uid = int(cuid)
                    break
        else:
            champ_scid = (getattr(self, "_player_champ_scid", None)
                          if owner_id else getattr(self, "_ai_champ_scid", None))
            champ_uid = (int(champ_scid.uid.uid64)
                         if champ_scid is not None else None)
        self._dispatch_native_trigger(
            game, session, draw_pl_t, ai_t, bstate,
            "CardDrawnEvent", champ_uid, owner_id,
            target_card_id=row[1])
        if is_pvp:
            from services.tournament_game import pvp_save_state
            pvp_save_state(session, bstate)
        else:
            _be.save_state(session, bstate)
        _hc.log_req(f"    Player drew card {row[1]} ({name})")

    def _rules_port_deck_out(self, game, session, owner_id, pl_t, ai_t,
                             battle_state):
        """Publish the mode-specific result for a native deck-out rule."""
        import hconnect_server as _hc
        owner_id = int(owner_id or 0)
        if battle_state.get("pvp"):
            from services.tournament_game import _pvp_end_game
            pids = [int(pid) for pid in (battle_state.get("pids") or ())]
            if len(pids) >= 2:
                loser = owner_id
                winner = next((pid for pid in pids if pid != loser), None)
                if winner is not None:
                    _pvp_end_game(session, battle_state, winner, loser,
                                  reason="deck-out")
            return True
        if owner_id:
            self._finish_battle_gameend(
                session, False, winners=[ai_t], losers=[pl_t])
            _hc.log_req("    Game over: player deck empty on native draw")
        else:
            import commands as _cmd
            _cmd.push_battle_game_end(
                handler=self, session=session,
                winners=[pl_t], losers=[ai_t])
            if hasattr(self, "_campaign_gameend"):
                self._campaign_gameend(session, won=True)
            _hc.log_req("    Game over: AI deck empty on native draw")
        return True

    def _ai_play_resource(self, game, session, ai_t, battle_state):
        import ai
        return ai.ai_play_resource(self, game, session, ai_t, battle_state)

    def _ai_play_troop(self, game, session, ai_t, battle_state):
        import ai
        return ai.ai_play_troop(self, game, session, ai_t, battle_state)

    def _resolve_ai_mulligan(self, session, game, ai_t):
        import ai
        return ai.resolve_ai_mulligan(self, session, game, ai_t)

    def _card_type_of(self, card_template_id):
        """Return the card_templates.card_type for a card_template_id."""
        import hconnect_server as _hc
        s = str(card_template_id)
        try:
            if "-" in s:
                template = _hc.db_template_by_guid(s)
                return template[1] if template else None
            row = _hc.db_card_instance_display(
                self.user_profile["id"] if self.user_profile else 0,
                int(s), conn=_hc._db)
            return row[1] if row else None
        except Exception:
            return None
