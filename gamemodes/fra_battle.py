"""FRA battle setup and reconnect snapshots for the arena and campaign encounters."""

import json


class FraBattleMixin:
    """Methods of HCPHandler, mixed into it (see hconnect_server)."""

    def _apply_fra_battle_modifications(self, game, session, pl_t, ai_t,
                                        bstate, modifications,
                                        player_deck_cards, ai_deck_cards,
                                        drain_setup_stack):
        """Apply round-zero FRA mods through the same live card/rules state.

        Arena's campaign service owns selection and persistence. This method
        projects its typed result into the authoritative PvE session before
        the initial PreGame packet is sent.
        """
        import hconnect_server as _hc
        from pvp_db import db_insert_game_card, db_game_cards_at_location_scalar
        from rules_port.resources import apply_resource_change

        owners = {
            0: ("ai", 0, ai_t, ai_deck_cards),
            2: ("player", int(self.user_profile["id"]), pl_t,
                player_deck_cards),
        }
        new_decks = {0: False, 2: False}
        template_cache = {}
        collection_values = {
            "deck": _hc.game_engine.ECardCollections.Deck,
            "hand": _hc.game_engine.ECardCollections.Hand,
            "warzone": _hc.game_engine.ECardCollections.Warzone,
            "castspells": _hc.game_engine.ECardCollections.CastSpells,
            "playedresources": _hc.game_engine.ECardCollections.PlayedResources,
            "underground": _hc.game_engine.ECardCollections.Underground,
            "discard": _hc.game_engine.ECardCollections.Discard,
            "crypt": _hc.game_engine.ECardCollections.Discard,
            "void": _hc.game_engine.ECardCollections.Void,
        }

        def _owner_targets(target):
            target = int(target or 0)
            return (tuple(owners) if target == 1 else
                    (target,) if target in owners else ())

        def _push_player_state():
            game.player_resources = int(bstate.get("player_resources", 0) or 0)
            game.player_total_resources = int(
                bstate.get("player_total_resources", 0) or 0)
            game.player_charges = int(bstate.get("player_charges", 0) or 0)
            game.player_threshold = {
                _hc.game_engine.ECardShards(int(color)): int(value or 0)
                for color, value in (bstate.get("player_threshold") or {}).items()
                if int(value or 0) > 0
            }
            game.ai_resources = int(bstate.get("ai_resources", 0) or 0)
            game.ai_total_resources = int(
                bstate.get("ai_total_resources", 0) or 0)
            game.ai_charges = int(bstate.get("ai_charges", 0) or 0)
            game.ai_threshold = {
                _hc.game_engine.ECardShards(int(color)): int(value or 0)
                for color, value in (bstate.get("ai_threshold") or {}).items()
                if int(value or 0) > 0
            }

        shared_grant_cache = {}

        def _shared_all_player_grant(ability_guids):
            """Whether one source grants the same permanent aura to both sides.

            EncounterModAddCard targets All by creating one copy per owner.
            For an authored permanent GrantAbility whose auto target spans
            MultiplePlayers, that duplicates the same grant on every target.
            Project a single source for that metadata shape; keep per-owner
            copies for every other card ability.
            """
            key = tuple(dict.fromkeys(
                str(value).lower() for value in (ability_guids or ()) if value))
            if key in shared_grant_cache:
                return shared_grant_cache[key]
            if not key:
                shared_grant_cache[key] = False
                return False
            from gamedata import DEFAULT_RECORD_STORE, ability_graph
            result = True
            for ability_guid in key:
                graph = ability_graph(DEFAULT_RECORD_STORE, ability_guid)
                if graph is None or not graph.effects:
                    result = False
                    break
                for effect in graph.effects:
                    try:
                        target_index = int(effect.target_index)
                    except (TypeError, ValueError):
                        result = False
                        break
                    if (effect.concrete_type != "GrantAbilityEffectTemplate" or
                            str(effect.duration or "").lower() != "permanent" or
                            target_index < 0 or target_index >= len(graph.targets)):
                        result = False
                        break
                    target = graph.targets[target_index]
                    if (not target.is_auto or
                            str(target.player_filter or "").lower() !=
                            "multipleplayers"):
                        result = False
                        break
                if not result:
                    break
            shared_grant_cache[key] = result
            return result

        for modification in modifications or ():
            if int(modification.get("round_to_apply", 0) or 0) != 0:
                continue
            wire_type = str(modification.get("wire_type") or "")
            target_owners = _owner_targets(modification.get("target_player"))
            if not target_owners:
                continue

            if wire_type.endswith("EncounterModAddChampionHealth"):
                amount = int(modification.get("amount", 0) or 0)
                absolute = bool(modification.get("absolute", False))
                for target in target_owners:
                    side = owners[target][0]
                    key = f"{side}_health"
                    old = int(bstate.get(key, getattr(
                        game, f"{side}_health", 20)) or 0)
                    bstate[key] = max(1, amount if absolute else old + amount)
                continue

            if wire_type.endswith("EncounterModAddResource"):
                absolute = bool(modification.get("absolute", False))
                for target in target_owners:
                    side = owners[target][0]
                    total_key = f"{side}_total_resources"
                    current_key = f"{side}_resources"
                    charge_key = f"{side}_charges"
                    total_old = int(bstate.get(total_key, 0) or 0)
                    resource_amount = int(
                        modification.get("max_resource_value", 0) or 0)
                    resource_delta = (resource_amount - total_old
                                      if absolute else resource_amount)
                    apply_resource_change(
                        bstate, side, "totalresource", resource_delta)
                    apply_resource_change(
                        bstate, side, "currentresource", resource_delta)
                    charge_amount = int(
                        modification.get("charge_value", 0) or 0)
                    charge_delta = (charge_amount - int(
                        bstate.get(charge_key, 0) or 0)
                        if absolute else charge_amount)
                    apply_resource_change(
                        bstate, side, "chargepoints", charge_delta)
                    thresholds = modification.get("threshold_values", ())
                    for threshold in thresholds if isinstance(
                            thresholds, (list, tuple)) else ():
                        color = int(threshold.get("color_flags", 0) or 0)
                        desired = int(threshold.get("threshold", 0) or 0)
                        old = int((bstate.get(f"{side}_threshold") or {}).get(
                            color, (bstate.get(f"{side}_threshold") or {}).get(
                                str(color), 0)) or 0)
                        apply_resource_change(
                            bstate, side, "threshold",
                            desired - old if absolute else desired,
                            color=color)
                _push_player_state()
                continue

            if not wire_type.endswith("EncounterModAddCard"):
                # EncounterModDrawCards is applied after the opening hand is
                # dealt; all other unsupported round-zero mod classes remain
                # visible in the shared setup result and log for follow-up.
                if wire_type.endswith("EncounterModDrawCards"):
                    bstate["fra_refill_hands_after_draw"] = True
                else:
                    _hc.log_req(f"    FRA encounter mod skipped: {wire_type}")
                continue

            template_guid = str(modification.get("card_guid") or "").lower()
            if not template_guid or not _hc.db_template_exists(
                    template_guid, conn=_hc._db):
                _hc.log_req(f"    FRA card mod skipped: invalid template {template_guid}")
                continue
            amount = max(0, int(modification.get("amount", 1) or 0))
            collection = str(modification.get("collection", "Deck") or "Deck")
            collection_key = collection.lower()
            location_by_collection = {
                "deck": "deck", "hand": "hand", "warzone": "warzone",
                "castspells": "CastSpells", "playedresources": "PlayedResources",
                "underground": "underground", "discard": "discard",
                "crypt": "discard", "void": "void",
            }
            location = location_by_collection.get(collection_key)
            if location is None:
                _hc.log_req(f"    FRA card mod skipped: unsupported collection {collection}")
                continue
            row = _hc.db_template_card_info(template_guid, conn=_hc._db)
            if not row:
                _hc.log_req(f"    FRA card mod skipped: template row missing {template_guid}")
                continue
            setup_data = self._battle_setup_template_data(
                template_guid, template_cache)
            if setup_data:
                abilities_json, attributes, ability_guids, _threshold = setup_data
            else:
                abilities_json, attributes, ability_guids = "[]", 0, []
            if isinstance(abilities_json, str):
                try:
                    ability_guids = json.loads(abilities_json)
                except (TypeError, ValueError, json.JSONDecodeError):
                    ability_guids = []
            ability_guids = [str(value).lower() for value in
                             (ability_guids or []) if value]
            card_target_owners = target_owners
            if (len(target_owners) > 1 and
                    _shared_all_player_grant(ability_guids)):
                card_target_owners = (target_owners[0],)
                _hc.log_req("    FRA shared all-player grant projected once: "
                        f"card={template_guid[:8]} copies={len(target_owners)}")
            card_type_name = str(row[0] or "Unknown")
            card_type = _hc.game_engine.card_type_from_db(card_type_name)

            for owner_key in card_target_owners:
                _side, owner_id, owner_uid, deck_cards = owners[owner_key]
                for _ in range(amount):
                    scid = game._new_card_id()
                    card_uid = int(scid.uid.uid64)
                    position = _hc.db_zone_card_count(
                        session.session_id, owner_id, location, conn=_hc._db)
                    db_insert_game_card(
                        session.session_id, owner_id, card_uid, template_guid,
                        location, card_type=card_type_name, position=position,
                        abilities_json=json.dumps(ability_guids),
                        attributes=int(attributes or 0), conn=_hc._db)
                    _tpl, _ctype, _name, _cost, _attack, _defense, _gems = \
                        self._card_full_data(game, scid, template_guid)
                    collection_value = collection_values[collection_key]
                    game.push_card_updated(
                        scid, owner_uid, collection_value,
                        card_type, template_id=template_guid,
                        nulling=(location == "deck" or
                                 (location == "underground" and owner_id == 0)))

                    self._dispatch_native_trigger(
                        game, session, pl_t, ai_t, bstate,
                        "CardCreatedEvent", card_uid, owner_id)
                    drain_setup_stack(bstate)

                    if location == "CastSpells":
                        from rules_port.chain_items import (
                            dispatch_card_cast, resolve_card_item)
                        dispatch_card_cast(
                            self, session, game, bstate, pl_t, ai_t,
                            card_uid, owner_id)
                        resolve_card_item(
                            self, session, _hc._db, game, bstate,
                            {"kind": "spell", "source_uid": card_uid,
                             "ability_guids": ability_guids}, pl_t, ai_t)
                        drain_setup_stack(bstate)
                    elif location == "warzone":
                        if card_type & _hc.game_engine.ECardTypes.Artifact:
                            game.push_artifact_card_played(scid, owner_uid)
                        elif card_type & _hc.game_engine.ECardTypes.Troop:
                            game.push_troop_card_played(scid, owner_uid)
                        self._dispatch_native_trigger(
                            game, session, pl_t, ai_t, bstate,
                            "CardEnteredZoneEvent", card_uid, owner_id,
                            event_source_collection="Deck",
                            event_destination_collection="warzone")
                        drain_setup_stack(bstate)
                    elif location == "deck":
                        game.push_card_updated(
                            scid, owner_uid, _hc.game_engine.ECardCollections.Deck,
                            card_type, nulling=True)
                        deck_cards.append(scid)
                        new_decks[owner_key] = True
            if bool(modification.get("shuffle")) and collection_key == "deck":
                import random as _fra_random
                for owner_key in target_owners:
                    _side, owner_id, _owner_uid, deck_cards = owners[owner_key]
                    ordered = db_game_cards_at_location_scalar(
                        session.session_id, "deck", user_id=owner_id, conn=_hc._db)
                    _fra_random.shuffle(ordered)
                    _hc._db.executemany(
                        "UPDATE game_cards SET position=? WHERE session_id=? "
                        "AND card_uid=?",
                        [(index, session.session_id, int(uid))
                         for index, uid in enumerate(ordered)])

            _hc._db.commit()

        # Keep the final authoritative deck list/count in the same setup
        # packet after any challenge-injected cards were appended.
        for owner_key, changed in new_decks.items():
            if not changed:
                continue
            _side, _owner_id, owner_uid, deck_cards = owners[owner_key]
            sleeve = (getattr(self, "_ai_deck_sleeve_guid", "")
                      if owner_key == 0 else
                      getattr(self, "_player_deck_sleeve_guid", ""))
            game.push_deck_created_with_cards(
                owner_uid, deck_cards, sleeve_guid=sleeve)

        _push_player_state()
        for key, uid, template_guid in (
                ("player_health", self._player_champ_scid,
                 getattr(self, "_player_champ_guid", "")),
                ("ai_health", self._ai_champ_scid,
                 getattr(self, "_ai_champ_guid", ""))):
            health = int(bstate.get(key, 20) or 20)
            if key == "player_health":
                game.player_health = health
            else:
                game.ai_health = health
            card_def = game.card_defs.get(uid)
            if card_def is not None:
                card_def.defense = health
            owner_uid = pl_t if key == "player_health" else ai_t
            game.push_card_updated(
                uid, owner_uid, _hc.game_engine.ECardCollections.None_,
                _hc.game_engine.ECardTypes.Champion, defense=health,
                template_id=template_guid)
        game.push_player_updated(
            pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(
            ai_t, champ_id=getattr(self, "_ai_champ_scid", None))

        # The following Mulligan keep transaction builds a fresh state. Save
        # the mod-adjusted start values there so setup bonuses survive it.
        encounter_data = dict(session.encounter_data or {})
        encounter_data["_fra_starting_battle_state"] = {
            key: bstate.get(key) for key in (
                "player_health", "ai_health", "player_resources",
                "player_total_resources", "ai_resources",
                "ai_total_resources", "player_charges", "ai_charges",
                "player_threshold", "ai_threshold")}
        session.encounter_data = encounter_data
        persist = getattr(session, "_persist", None)
        if callable(persist):
            persist()

    def _resolve_fra_deck_id(self, arena_deck_id):
        """The practice/FRA deck: the arena-assigned deck when it is valid for
        this player, otherwise the player's most recently saved deck.  NEVER a
        hardcoded id — each player's own collection is used."""
        import hconnect_server as _hc
        uid = self.user_profile["id"] if self.user_profile else None
        if uid and arena_deck_id:
            if _hc.db_find_deck_owner(arena_deck_id, conn=_hc._db) == uid:
                return arena_deck_id
        if uid:
            row = _hc.db_latest_deck_id_for_user(uid, conn=_hc._db)
            if row:
                return row
        return None

    def _push_fra_reconnect_snapshot(self, session, player_pid):
        """Rebuild the saved FRA board for a client returning to the match.

        This only projects persisted ``game_cards`` and the saved turn
        checkpoint. It never deals, shuffles, advances the phase, or replays
        the rejected card transaction.
        """
        import hconnect_server as _hc
        profile = self.user_profile if isinstance(self.user_profile, dict) else {}
        try:
            owner_id = int(profile.get("id") or 0)
            player_pid = int(player_pid)
        except (TypeError, ValueError):
            return False
        if owner_id <= 0 or player_pid <= 0:
            return False

        from rules_port.persistence import load_state as load_rules_state
        state = load_rules_state(session)
        if not isinstance(state, dict) or "turn_player" not in state:
            return False

        def checkpoint_champion(champion_owner_id):
            """Read a synthetic champion identity from the saved checkpoint."""
            champion_uids = state.get("champ_map")
            champion_guids = state.get("champ_guid_map")
            if (not isinstance(champion_uids, dict) or
                    not isinstance(champion_guids, dict)):
                return None
            keys = (str(champion_owner_id), champion_owner_id)
            uid = next((champion_uids[key] for key in keys
                        if key in champion_uids), None)
            guid = next((champion_guids[key] for key in keys
                         if key in champion_guids), None)
            if isinstance(guid, dict):
                guid = guid.get("m_Guid", guid.get("guid"))
            try:
                uid = int(uid)
            except (TypeError, ValueError):
                return None
            if uid <= 0 or not guid:
                return None
            return uid, str(guid)

        # FRA champions are synthetic SessionCardIds and intentionally have
        # no game_cards rows. Prefer a row if another session provides one,
        # then restore the saved UID/template pair from the battle checkpoint.
        player_champion = (_hc.db_game_champion(
            session.session_id, owner_id, conn=_hc._db) or
            checkpoint_champion(owner_id))
        ai_champion = (_hc.db_game_champion(
            session.session_id, 0, conn=_hc._db) or checkpoint_champion(0))
        if not player_champion or not ai_champion:
            _hc.log_req("    FRA reconnect snapshot aborted: missing champion "
                    "rows and checkpoint identities")
            return False
        player_champ_uid, player_champ_guid = player_champion
        ai_champ_uid, ai_champ_guid = ai_champion
        try:
            player_champ_uid = int(player_champ_uid)
            ai_champ_uid = int(ai_champ_uid)
        except (TypeError, ValueError):
            return False
        if (player_champ_uid <= 0 or ai_champ_uid <= 0 or
                (player_champ_uid & 0xff) == 0 or (ai_champ_uid & 0xff) == 0):
            _hc.log_req("    FRA reconnect snapshot aborted: invalid champion UID")
            return False

        player_uid = _hc.game_engine.UID.make(244, player_pid)
        ai_uid = _hc.game_engine.UID.make(_hc.game_engine.SERVICE_AI_UID_TYPE, 1000)
        player_champ_scid = _hc.game_engine.SessionCardId(
            _hc.game_engine.UID(player_champ_uid))
        ai_champ_scid = _hc.game_engine.SessionCardId(
            _hc.game_engine.UID(ai_champ_uid))
        player_name = str(profile.get("name") or "Player")
        player_champ_name = _hc.db_template_name(
            player_champ_guid, conn=_hc._db) or player_name
        ai_name = _hc.db_template_name(ai_champ_guid, conn=_hc._db) or "AI Opponent"
        player_sleeve = state.get("player_deck_sleeve_id") or ""
        ai_sleeve = state.get("ai_deck_sleeve_id") or ""
        self._player_champ_scid = player_champ_scid
        self._ai_champ_scid = ai_champ_scid
        self._player_champ_board = (
            player_champ_scid, False, player_champ_name, player_champ_guid)
        self._ai_champ_board = (
            ai_champ_scid, True, ai_name, ai_champ_guid)

        def send_sync(game, label):
            if not game.events:
                return
            packet = game.make_network_packet(player_uid)
            body = _hc.encode_datawrapper(
                0, 3055,
                _hc.compress_gzip(_hc.encode_sync_event(packet)), 1,
                "00000000-0000-0000-0000-000000000000")
            self._game_scnt = max(
                int(getattr(self, "_game_scnt", 0) or 0),
                int(getattr(self, "scnt", 0) or 0)) + 1
            self.scnt = self._game_scnt
            self.send({
                "issuer": (f"0.0.0.0.ServiceGameSession."
                           f"{_hc.SERVICE_GAME_SESSION_UID}.{session.session_id}."
                           f"{self.scnt}"),
                "target": "ServiceGameSession",
                "instance": str(session.server_id),
                "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
            }, body)
            _hc.log_req(f"    FRA reconnect: sent {label}")

        # JoinDisconnectedGame adds only the reconnecting client locally. Add
        # the AI before GameStarted so Unity has both Player objects when it
        # handles the champion, deck, and board snapshot that follows.
        added = _hc.encode_objfmt_response(
            ["Game.Shared.Network.GameSession.PlayerAddedEventArgs",
             "Game.Shared.UID", "Game.Shared.PlayerState", "System.Int32"],
            [("RoutingPlayerId", "uid", int(ai_uid.uid64)),
             ("PlayerState", "struct", ("Game.Shared.PlayerState", [
                 ("PlayerId", "uid", int(ai_uid.uid64)),
                 ("PlayerPosition", "int", 1),
             ]))])
        added_dw = _hc.encode_datawrapper(
            0, 3050, _hc.compress_gzip(added), 1, _hc.client_session_guid(self))
        self.scnt += 1
        self.send({
            "issuer": (f"0.0.0.0.ServiceGameSession.246."
                       f"{session.session_id}.{self.scnt}"),
            "target": "ServiceGameSession",
            "instance": str(session.server_id),
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, added_dw)

        # The original coin toss/Play-or-Draw choice has already happened.
        # Carry its resulting first player into GameStarted and dictate it so
        # reconnect does not restart the toss UI.
        if "player_draws_first_turn" in state:
            player_first = not bool(state.get("player_draws_first_turn"))
        else:
            turn_number = max(1, int(state.get("turn_number", 1) or 1))
            active_is_player = state.get("turn_player") != "ai"
            player_first = (active_is_player if turn_number % 2 else
                            not active_is_player)
        first_uid = player_uid if player_first else ai_uid
        started = _hc.game_engine.Game(session.session_id, player_uid, ai_uid)
        started.push_game_started(
            champion_names=[player_name, ai_name],
            champion_template_ids=[player_champ_guid, ai_champ_guid],
            player_first=player_first,
            sleeve_template_ids=[player_sleeve, ai_sleeve])
        started.push_first_player_dictated(first_uid)
        send_sync(started, "GameStarted and first-player state")

        # _fresh_game attaches/rehydrates RulesPort from the saved checkpoint.
        # Its eager hidden-hand projection belongs after GameStarted, so clear
        # those transient events and let _send_battle_events add them to the
        # complete board snapshot below.
        game = self._fresh_game(session, player_uid, ai_uid, state)
        game.events.clear()
        state = getattr(session, "_rules_port_battle_state", None) or state
        self._current_bstate = state
        game.push_player_updated(
            player_uid, deck_sleeve_id=player_sleeve,
            champ_id=player_champ_scid)
        game.push_player_updated(
            ai_uid, deck_sleeve_id=ai_sleeve, champ_id=ai_champ_scid)
        # _send_battle_events prepends the one-time champion CardUpdated and
        # ChampionCardPlayed announcements ahead of the existing event list.
        # Send the player records in their own packet first so the client's
        # State.Players cache contains both controllers before it builds those
        # champion/card views (BuildSleeve indexes that cache directly).
        send_sync(game, "player state")

        # Rebuild hidden deck counters and card membership without exposing
        # any deck identities. Hand and public zones come from their saved
        # rows below; no card is drawn during reconnect.
        for owner, owner_uid, sleeve in (
                (owner_id, player_uid, player_sleeve),
                (0, ai_uid, ai_sleeve)):
            deck_rows = _hc.db_game_cards_at_location(
                session.session_id, "deck", user_id=owner, conn=_hc._db)
            deck_ids = []
            for card_uid, template_guid, _owner, card_type, card_state, _ags, _attrs in deck_rows:
                scid = _hc.game_engine.SessionCardId(
                    _hc.game_engine.UID(int(card_uid)))
                self._card_full_data(game, scid, template_guid)
                game.push_card_updated(
                    scid, owner_uid, _hc.game_engine.ECardCollections.Deck,
                    _hc.game_engine.card_type_from_db(card_type),
                    template_id=template_guid, state=int(card_state or 0),
                    nulling=True)
                deck_ids.append(scid)
            game.push_deck_created_with_cards(owner_uid, deck_ids, sleeve)

        for owner, owner_uid in ((owner_id, player_uid), (0, ai_uid)):
            hand_rows = _hc.db_game_cards_at_location(
                session.session_id, "hand", user_id=owner, conn=_hc._db)
            for card_uid, template_guid, _owner, card_type, card_state, _ags, _attrs in hand_rows:
                scid = _hc.game_engine.SessionCardId(
                    _hc.game_engine.UID(int(card_uid)))
                self._card_full_data(game, scid, template_guid)
                game.push_card_updated(
                    scid, owner_uid, _hc.game_engine.ECardCollections.Hand,
                    _hc.game_engine.card_type_from_db(card_type),
                    template_id=template_guid, state=int(card_state or 0),
                    nulling=owner != owner_id)

        # Warzone is projected through the same owner/state/ability path used
        # by a normal phase refresh. Restore the remaining visible zones from
        # their current card rows, including an unresolved spell on CastSpells.
        self._push_warzone_card_updates(game, session, player_uid, ai_uid)
        for zone, collection in (
                ("discard", _hc.game_engine.ECardCollections.Discard),
                ("void", _hc.game_engine.ECardCollections.Void),
                ("CastSpells", _hc.game_engine.ECardCollections.CastSpells),
                ("PlayedResources", _hc.game_engine.ECardCollections.PlayedResources),
                ("underground", _hc.game_engine.ECardCollections.Underground)):
            for card_uid, template_guid, owner, card_type, card_state, _ags, _attrs in \
                    _hc.db_game_cards_at_location(
                        session.session_id, zone, conn=_hc._db):
                scid = _hc.game_engine.SessionCardId(
                    _hc.game_engine.UID(int(card_uid)))
                self._card_full_data(game, scid, template_guid)
                controller = (player_uid if int(owner) == owner_id else ai_uid)
                game.push_card_updated(
                    scid, controller, collection,
                    _hc.game_engine.card_type_from_db(card_type),
                    template_id=template_guid, state=int(card_state or 0),
                    nulling=(zone == "underground" and
                             int(owner) != owner_id))
        from rules_port.visibility import project_visible_underground
        project_visible_underground(
            _hc._db, session, self, game, player_uid, ai_uid, state)

        # Restore the visible chain from its persisted descriptors. The
        # CastSpells CardUpdateds above restore the card views; these events
        # restore which abilities are waiting to resolve and their targets.
        for descriptor in state.get("stack", ()) or ():
            if not isinstance(descriptor, dict):
                continue
            try:
                source_uid = int(descriptor.get("source_uid") or 0)
            except (TypeError, ValueError):
                continue
            if source_uid <= 0:
                continue
            kind = str(descriptor.get("kind") or "").lower()
            ability_guid = descriptor.get("ability_guid")
            if kind in ("troop", "spell"):
                ability_guid = _hc.game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID
            if not ability_guid:
                guids = descriptor.get("ability_guids") or ()
                ability_guid = guids[0] if guids else None
            if not ability_guid:
                continue
            target_uids = []
            target_map = descriptor.get("target_map")
            activation_data = descriptor.get("activation_data") or {}
            if not target_map and isinstance(activation_data, dict):
                target_map = activation_data.get("target_map")
            if isinstance(target_map, dict):
                for values in target_map.values():
                    if not isinstance(values, (list, tuple)):
                        values = [values]
                    for value in values:
                        try:
                            target_uids.append(int(getattr(value, "uid64", value)))
                        except (TypeError, ValueError):
                            continue
            for key in ("target_uid", "trigger_target_uid"):
                value = descriptor.get(key)
                if value:
                    try:
                        target_uids.append(int(getattr(value, "uid64", value)))
                    except (TypeError, ValueError):
                        pass
            targets = list(dict.fromkeys(
                uid for uid in target_uids if uid > 0))
            try:
                instance_id = int(descriptor.get("instance_id", 1) or 1)
            except (TypeError, ValueError):
                instance_id = 1
            game.push_ability_on_chain(
                _hc.game_engine.SessionCardId(_hc.game_engine.UID(source_uid)),
                _hc.game_engine.ResourceId.from_str(str(ability_guid)),
                ability_instance_id=instance_id,
                target_card_ids=[_hc.game_engine.SessionCardId(
                    _hc.game_engine.UID(uid)) for uid in targets],
                ignores_chain=bool(descriptor.get("ignores_chain", False)))

        port = getattr(session, "_rules_port_session", None)
        fallback_side = ("ai" if state.get("turn_player") == "ai"
                         else "player")
        fallback_active = ai_uid if fallback_side == "ai" else player_uid

        def participant_uid(value, default):
            if value is None:
                return default
            try:
                value = int(getattr(value, "uid64", value))
            except (TypeError, ValueError):
                return default
            if value in (int(player_uid.uid64), player_pid):
                return player_uid
            if value in (int(ai_uid.uid64), 0):
                return ai_uid
            return default

        active_uid = participant_uid(
            getattr(port, "active_player_id", None), fallback_active)
        priority_default = active_uid
        priority_action = getattr(getattr(port, "action_stack", None),
                                  "peek", lambda: None)()
        priority_uid = participant_uid(
            getattr(priority_action, "priority_player_id", None),
            priority_default)
        if port is not None:
            phase = getattr(port, "current_turn_phase", None)
        else:
            from rules_port.persistence import current_phase
            phase = current_phase(state)
        if phase is None:
            phase = state.get("phase")
        try:
            phase = int(phase)
        except (TypeError, ValueError):
            phase = int(_hc.game_engine.ETurnPhases.FirstMainPhase)

        game.push_green_light(priority_uid, _hc.game_engine.EPriorityContext.Normal)
        game.push_turn_phase(phase, active_uid, priority_uid)
        game.push_reconnect_done()
        # _send_battle_events re-announces the valid champion SessionCardIds
        # before the rest of the board and reapplies viewer-specific visibility.
        session._client_in_battle = session.session_id
        session._champion_board_announced = None
        self._send_battle_events(session, game, player_uid)

        pending_input = bool(
            getattr(port, "pending_activation", None) or
            any(state.get(key) for key in (
                "pending_choice", "pending_trigger", "pending_deck_search",
                "pending_discard_ability", "pending_conversation",
                "resolution_paused")))
        if int(priority_uid.uid64) == int(player_uid.uid64):
            if state.get("stack"):
                self._push_phase_options_empty(session, player_uid, ai_uid)
            elif not pending_input:
                self._push_phase_options(session, player_uid, ai_uid, phase)
        _hc.log_req(
            f"    FRA reconnect snapshot restored for pid={player_pid} "
            f"session={session.session_id} phase={phase} "
            f"priority={int(priority_uid.uid64)}")
        return True
