"""Profile/reward output projection mixin for HConnect connections."""


def bind_runtime_globals(namespace):
    """Bind legacy protocol/DB symbols after the server module is initialized."""
    globals().update(namespace)


class ProfileStreamMixin:
    def _handle_chat_command(self, cmd: str, room: str, username: str) -> str:
        import commands
        return commands.handle_command(self, cmd, room, username)

    def push_profile_stream(self):
        p = self.user_profile
        username = display_name_from_identity(p["name"])
        gold = p["gold"]
        platinum = p["platinum"]
        
        ident = encode_objfmt_response(
            ["Game.Shared.Profile.Network+Ident",
             "System.UInt64", "System.UInt64"],
             [("AuthId", "ulong", int(self.client_auth_id)),
              ("ReckId", "ulong", int(self.client_reck_id))]
        )
        args = encode_objfmt_response(
            ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
             "System.Byte[]", "System.Boolean"],
            [("Data", "bytes", ident),
             ("done", "bool", False)]
        )
        compressed = compress_gzip(args)
        dw = encode_datawrapper(0, 2210, compressed, 1, "00000000-0000-0000-0000-000000000000")
        issuer = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.0"
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(f">>> PUSH Ident (dt=2210, auth={self.client_auth_id}, reck={self.client_reck_id}) dw_sz={len(dw)}")

        # Push server-configured feature strings. PlayerProfile handles these
        # as individual strings in the ProfileStream (dt=2210), e.g.
        # ``allowcon`` enables the developer console and ``allowreplay``
        # enables the replay UI hook.
        for feature_flag in PROFILE_FEATURE_FLAGS:
            flag_inner = encode_objfmt_string(feature_flag)
            flag_profile = encode_objfmt_response(
                ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
                 "System.Byte[]", "System.Boolean"],
                [("Data", "bytes", flag_inner),
                 ("done", "bool", False)]
            )
            flag_compressed = compress_gzip(flag_profile)
            flag_dw = encode_datawrapper(
                0, 2210, flag_compressed, 1,
                "00000000-0000-0000-0000-000000000000")
            issuer_flag = (
                f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
                f"ServicePlayer.{self.client_uid}.{self.scnt}"
            )
            self.scnt += 1
            self.send({
                "issuer": issuer_flag, "target": "ServiceProfile", "instance": "Shared",
                "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
            }, flag_dw)
            log_req(f">>> PUSH {feature_flag} (dt=2210) dw_sz={len(flag_dw)}")

        # Push any unopened treasure chests so they survive a re-login.
        # The client collects List<chest_bits> from the profile stream and
        # feeds them to CreateLocalTreasureCache (PlayerProfile.cs).
        self._push_chests_stream(p)

        now_str = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
        
        # Build inventory items from DB (only purchased items, no stardust/chests)
        inv_items = []
        item_id = 1
        
        # Add purchased items from DB
        purchased = db_get_inventory(p["id"])
        for tguid, qty in purchased:
            inv_items.append((tguid, item_id, qty))
            # Store client item UID in the DB so we can reference it later
            from profile_db import db_set_inventory_client_uid
            db_set_inventory_client_uid(p["id"], tguid, item_id)
            item_id += 1

        # Add unopened chests as inventory items. The client expects the chest
        # to be BOTH a chest_bits entry (m_InventoryChests, via the chest
        # stream push above) AND an inventory_bits entry with the
        # CommonTreasureChest template, keyed by the same InventoryId so the
        # pack list can match them up (see UIPackListViewModel.DoUpdateCardPackList
        # and UIPackContentViewModel.openPackResponseHandler).
            from profile_db import db_get_unopened_chests
        chest_rows = db_get_unopened_chests(p["id"])
        for crow in chest_rows:
            # Named promotional chests retain their inventory template; old
            # standard rows have no template and continue using the generic
            # CommonTreasureChest item.
            chest_template = crow[1] if len(crow) > 1 and crow[1] else \
                "a9ae9af2-e27a-48e0-9cd2-490d252fffe4"
            inv_items.append((chest_template, 9000 + crow[0], 1))
        
        inv_count = len(inv_items)
        
        # Load decks from DB
        deck_data = []
        if self.user_profile:
            db_decks = db_get_decks(self.user_profile["id"])
            for dk in db_decks:
                deck_uid = dk["id"]
                deck_uid64 = (deck_uid << 8) | 17
                # Match deck to champion by name
                champ_id = 0
                dname = dk.get("name", "")
                from profile_db import db_get_champion_deck_match
                for c_row in db_get_champion_deck_match(self.user_profile["id"]):
                    if dname.startswith(c_row[1]):
                        champ_id = c_row[0]
                        break
                # Pre-resolve card IDs to template GUIDs
                import json as _json
                try:
                    card_ids = _json.loads(dk.get("cards", "[]"))
                except:
                    card_ids = []
                card_guids = []  # CardsInDeck kept empty in profile push
                deck_data.append((deck_uid64, dname, deck_uid, champ_id, dk.get("cards", "[]"), card_guids))
        deck_count = len(deck_data)
        log(f">>> Profile push: {deck_count} decks from DB")
        
        # Load champions from DB
        champ_data = []
        if self.user_profile:
            from profile_db import db_get_user_champions
            import json as _json
            db_champs = db_get_user_champions(self.user_profile["id"])
            for c in db_champs:
                champ_id = c[0]
                champ_uid64 = (champ_id << 8) | 12  # UID.Type.Champion=12
                # LastDeckID must be the RAW DB deck id, NOT a pre-encoded UID:
                # the client's GetDeck(ulong) wraps it in new UID(Deck, id)
                # (=(id<<8)|17) before looking up its DeckList, which is keyed
                # by (db_id<<8)|17. Sending the encoded UID shifts it twice.
                # We deliberately push LastDeckID=0: the Globe champion select
                # (UIGlobeArenaPanelViewModel.SelectDeck) LAUNCHES the campaign
                # when LastDeckID is set+valid, else it opens the deck editor —
                # pushing 0 lets the player edit their champion deck from the
                # Champion Select / Globe screen. The DB value is kept for
                # battle deck selection (updated when they pick a deck).
                try:
                    champion_talents = _json.loads(c[9] or "[]")
                    if not isinstance(champion_talents, list):
                        champion_talents = []
                except (TypeError, ValueError):
                    champion_talents = []
                champ_data.append((champ_uid64, c[1], champ_id, c[5], c[6], c[3], c[2], c[4],
                                   c[8] or 0, 0, champion_talents, c[10] or ""))
        champ_count = len(champ_data)
        log(f">>> Profile push: {champ_count} champions from DB")

        # ReckoningBits.Cards is a List<card_instance_bits>, not an inventory
        # collection.  Build it from the persisted instances so the client
        # receives one entry per owned copy (with the raw instance Id that
        # card_instance_bits/CardId expects).
        profile_cards = []
        if self.user_profile:
            card_rows = db_profile_card_instances(
                self.user_profile["id"], conn=_db)
            profile_cards = [
                (r[0], r[1] or "", r[5], r[2] or 0, r[3] or 0, r[4] or 0)
                for r in card_rows
            ]
        log(f">>> Profile push: {len(profile_cards)} card instances from DB")
        reck = encode_objfmt_response(
             ["Game.Shared.Domain.reckoning_bits",
              "System.UInt64", "System.String", "System.Int32", "System.Int32",
              "System.Int32",
              "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
              "Game.Shared.Domain.inventory_bits",
              "Game.Shared.ResourceId",
              "System.Guid",
              "System.DateTime",
              "System.Collections.Generic.List`1#Game.Shared.Domain.champion_bits",
              "Game.Shared.Domain.champion_bits",
              "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
              "Game.Shared.Domain.card_instance_bits",
              "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits",
              "Game.Shared.Domain.deck_bits",
              "Game.Shared.Domain.authentication_bits",
              "System.Int32",
              "System.Collections.Generic.List`1#Game.Shared.Domain.buyback_inventory_bits",
              "System.DateTime", "System.DateTime",
              "System.UInt64", "System.Boolean",
              "System.Int32", "System.DateTime", "System.Int32",
              "System.UInt64",
              # Pre-register types added dynamically by champlist/decklist
              "Game.Shared.Mechanics.EChampionClass",
              "Game.Shared.Mechanics.ERace",
              "Game.Shared.Mechanics.EGender",
              "Game.Shared.Mechanics.EDeckLock",
              "Game.Shared.Mechanics.EDeckPersonality",
              "System.Collections.Generic.Dictionary`2#System.UInt64!Game.Shared.Mechanics.EGemTypesNew",
              "Game.Shared.ResourceId", "System.Guid",
              "System.Collections.Generic.List`1#Game.Shared.ResourceId"],
             [("ReckID",     "ulong",   int(self.client_reck_id)),
              ("Name",       "string",  username),
              ("ExperiencePoints", "int", p.get("experience", 0)),
              ("Gold",       "int",     gold),
              ("Platinum",   "int",     platinum),
              ("InventoryIds", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", inv_count, inv_items)),
               ("Champions", "champlist", ("System.Collections.Generic.List`1#Game.Shared.Domain.champion_bits", champ_count, champ_data)),
              # The client receives owned cards as separate card_collection
              # objects in the profile stream and merges them into this set
              # before GetUserProfileInfoResponse.  Keep the reckoning_bits
              # field empty to match that login path.
              ("Cards",     "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
               ("Decks",     "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0)),
              ("Profile",    "class", "Game.Shared.Domain.authentication_bits"),
              ("EloRank",    "int",     1500),
              ("BuybackInventoryIds", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.buyback_inventory_bits", 0)),
              ("LastLogin",  "datetime", now_str),
              ("LastDisconnect", "datetime", now_str),
              ("AITournamentFlags", "ulong", 0),
              ("CanDisableProfanityFilter", "bool", True),
              ("Level",      "int",     1),
              ("XpGainTimer","datetime", now_str),
              ("XpGain",     "int",     p.get("daily_bonus_xp", 0)),
              ("ProfileId",  "ulong",   int(self.client_auth_id))]
        )
        log(f">>> reck raw ({len(reck)}b) hex={hexlify(reck[:200]).decode()}...")
        # Push EncodedDecks BEFORE reckoning_bits done=true
        if deck_count > 0:
                from encoded_decks import encode_encoded_decks
                db_decks = db_get_decks(self.user_profile["id"])
                ed_bytes = encode_encoded_decks(
                    db_decks, self.user_profile["id"], conn=_db)
                with open("/tmp/encoded_decks.bin", "wb") as f:
                    f.write(ed_bytes)
                ed_inner = encode_objfmt_response(
                    ["Game.Shared.Profile.Network+EncodedDecks", "System.Byte[]"],
                    [("Data", "bytes", ed_bytes)])
                ed_profile = encode_objfmt_response(
                    ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
                     "System.Byte[]", "System.Boolean"],
                    [("Data", "bytes", ed_inner),
                     ("done", "bool", False)])
                ed_compressed = compress_gzip(ed_profile)
                ed_dw = encode_datawrapper(0, 2210, ed_compressed, 1, "00000000-0000-0000-0000-000000000000")
                issuer_ed = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{self.scnt}"
                self.scnt += 1
                self.send({
                    "issuer": issuer_ed, "target": "ServiceProfile", "instance": "Shared",
                    "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                }, ed_dw)
                log_req(f">>> PUSH EncodedDecks (dt=2210) {deck_count} decks, dw_sz={len(ed_dw)}")

        # The original profile stream sends card_collection objects in
        # manageable chunks. HandleProfileStream buffers these and appends
        # every card to reckoning_bits.Cards immediately before loading the
        # PlayerProfile collection cache.
        CARD_COLLECTION_CHUNK = 500
        for start in range(0, len(profile_cards), CARD_COLLECTION_CHUNK):
            chunk = profile_cards[start:start + CARD_COLLECTION_CHUNK]
            card_collection = encode_objfmt_response(
                ["Game.Shared.Domain.card_collection",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                 "Game.Shared.Domain.card_instance_bits",
                 "System.UInt64", "Game.Shared.ResourceId", "System.Guid",
                 "System.Boolean", "System.String"],
                [("Cards", "cardlist", (
                    "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                    len(chunk), chunk))]
            )
            card_profile = encode_objfmt_response(
                ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
                 "System.Byte[]", "System.Boolean"],
                [("Data", "bytes", card_collection),
                 ("done", "bool", False)]
            )
            card_dw = encode_datawrapper(
                0, 2210, compress_gzip(card_profile), 1,
                "00000000-0000-0000-0000-000000000000")
            issuer_cards = (
                f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
                f"ServicePlayer.{self.client_uid}.{self.scnt}")
            self.scnt += 1
            self.send({
                "issuer": issuer_cards, "target": "ServiceProfile",
                "instance": "Shared", "reqid": 0, "c": 0, "conh": 0,
                "sid": self.sid,
            }, card_dw)
            log_req(
                f">>> PUSH card_collection (dt=2210) {len(chunk)} cards, "
                f"dw_sz={len(card_dw)}")

        args2 = encode_objfmt_response(
            ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
             "System.Byte[]", "System.Boolean"],
            [("Data", "bytes", reck),
             ("done", "bool", True)]
        )
        compressed2 = compress_gzip(args2)
        dw2 = encode_datawrapper(0, 2210, compressed2, 1, "00000000-0000-0000-0000-000000000000")
        issuer2 = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.1"
        self.scnt += 1
        self.send({
            "issuer": issuer2, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw2)
        log_req(f">>> PUSH reckoning_bits + done (dt=2210, reck={self.client_reck_id}) dw_sz={len(dw2)}")

        args3 = encode_login_stream_done()
        compressed3 = compress_gzip(args3)
        dw3 = encode_datawrapper(0, 2211, compressed3, 1, "00000000-0000-0000-0000-000000000000")
        issuer3 = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.2"
        self.scnt += 1
        self.send({
            "issuer": issuer3, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw3)
        log_req(f">>> PUSH LoginStreamDone (dt=2211) dw_sz={len(dw3)}")

        # The client normally requests 60007 during login, but older service
        # initialization can drop that request.  Push the event as well so the
        # mail counter is initialized from the authoritative unread rows.
        from services.mail import push_unread_notification
        push_unread_notification(self)

        # Flag inventory + social push for after client is ready
        self._inventory_pending = True
        self._social_pending = True
        self.push_iconoclast_banned_cards()

    def push_iconoclast_banned_cards(self):
        """Publish the client-side Iconoclast ban list (Profile event 2214)."""
        from objfmt_builder import ObjFmtBuilder
        from tournament_db import db_tournament_banned_card_guids

        banned = sorted(db_tournament_banned_card_guids(4, conn=_db))
        builder = ObjFmtBuilder(
            "Game.Shared.Network.Profile.BannedCardListEventArgs")
        builder.field_enum(
            "SetFormat", "Game.Shared.Mechanics.ESetFormat",
            ICONOCLAST_SET_FORMAT)
        builder.field_enum(
            "PlayFormat", "Game.Shared.Mechanics.EPlayFormat",
            ICONOCLAST_PLAY_FORMAT)
        builder.field_resource_id_list("BannedCards", banned)
        payload = compress_gzip(builder.finish(1))
        packet = encode_datawrapper(
            0, PROFILE_BANNED_CARD_LIST_EVENT, payload, 1,
            "00000000-0000-0000-0000-000000000000")
        issuer = (
            f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
            f"ServicePlayer.{self.client_uid}.{self.scnt}")
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, packet)
        log_req(f">>> PUSH Iconoclast BannedCardList (dt=2214) "
                 f"{len(banned)} cards, dw_sz={len(packet)}")

    def _push_chests_stream(self, profile):
        """Push unopened treasure chests in the login profile stream.

        Sends a standalone List<chest_bits> wrapped in ProfileStreamEventArgs
        (dt=2210) so the client's HandleProfileStream buffers it and calls
        CreateLocalTreasureCache once the stream is done.
        """
        from encoder import encode_chest_list
        if not profile:
            return
        rows = db_get_unopened_chests_full(profile["id"], conn=_db)
        # Promo/named chests are reconstructed from their inventory_bits
        # template during the reckoning profile push.  Sending them through
        # the generic chest stream first would create a duplicate key when
        # ProcessNonStandardChests handles that same inventory item.
        rows = [r for r in rows if not r[3]]
        if not rows:
            return
        chest_map = {"Common": 0, "Uncommon": 1, "Rare": 2,
                     "Legendary": 3, "Primal": 4, "Promo": 5}
        chests = [(chest_map.get(r[2], 0), 0, r[1], 9000 + r[0]) for r in rows]
        inner = encode_chest_list(chests)
        profile_args = encode_objfmt_response(
            ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
             "System.Byte[]", "System.Boolean"],
            [("Data", "bytes", inner),
             ("done", "bool", False)]
        )
        compressed = compress_gzip(profile_args)
        dw = encode_datawrapper(0, 2210, compressed, 1, "00000000-0000-0000-0000-000000000000")
        issuer = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{self.scnt}"
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(f">>> PUSH Chests stream (dt=2210) {len(chests)} chests, dw_sz={len(dw)}")

    def push_cards_to_client(self):
        """Push card instances from DB to the client via CardsAdded event (2205), chunked."""
        if not self.user_profile:
            return
        p = self.user_profile
        rows = db_profile_card_instances(p["id"], conn=_db)
        if not rows:
            return
        all_cards = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]

        CHUNK = 500
        for start in range(0, len(all_cards), CHUNK):
            chunk = all_cards[start:start + CHUNK]
            self._send_cards_chunk(chunk)

    def push_opened_cards_via_generic(self, cards):
        """Push newly opened cards via ProfileGenericUpdate (2211)."""
        if not self.user_profile or not cards:
            return
        from objfmt_builder import ObjFmtBuilder

        # Inner: ProfileGenericBatchUpdate with Cards list
        b = ObjFmtBuilder("Game.Shared.ProfileGenericBatchUpdate")
        list_idx, _ = b.begin_list("Cards",
            "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", len(cards))
        for i, (guid, name, cost, atk, def_, cid, is_ext) in enumerate(cards):
            b.begin_element(i, "Game.Shared.Domain.card_instance_bits", 6)
            b.card_fields(guid, cid, is_ext)
        batch_bytes = b.finish(1)

        # Wrap in ProfileGenericUpdateEventArgs → Message → Data
        b2 = ObjFmtBuilder("Game.Shared.Network.Profile.ProfileGenericUpdateEventArgs")
        msg_idx, _ = b2.begin_list("Message", "Game.Shared.ProfileGenericMessage", 1)
        b2.begin_element(0, "Game.Shared.ProfileGenericMessage", 1)
        b2.field_bytes("Data", batch_bytes)
        args = b2.finish(1)

        compressed = compress_gzip(args)
        dw = encode_datawrapper(0, 2211, compressed, 1)
        issuer = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{self.scnt}"
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(f">>> PUSH Cards via GenericUpdate (dt=2211) {len(cards)} cards, dw_sz={len(dw)}")

    def push_display_rewards(self, rewards):
        """Push ProfileGenericDisplayRewards so the client shows a reward popup.

        This is the same profile-generic channel used by the live client for
        CARD/GOLD/PLAT rewards.  Collection/card-instance persistence is done
        by the campaign service before this event is emitted.
        """
        if not self.user_profile or not rewards:
            return
        from objfmt_builder import ObjFmtBuilder

        b = ObjFmtBuilder("Game.Shared.ProfileGenericDisplayRewards")
        b.begin_list(
            "Rewards",
            "System.Collections.Generic.List`1#Game.Shared.Profile.Network+RewardResult",
            len(rewards),
        )
        for i, reward in enumerate(rewards):
            b.begin_element(i, "Game.Shared.Profile.Network+RewardResult", 6)
            b.field_str("Id", str(reward.get("id", "")))
            b.field_str("Template", str(reward.get("template", "")))
            b.field_int("Quantity", int(reward.get("quantity", 1) or 1))
            b.field_str("Type", str(reward.get("type", "CARD")))
            b.field_ulong("LedgerID", int(reward.get("ledger_id", 0) or 0))
            b.field_bool("Boa", bool(reward.get("boa", False)))
        reward_bytes = b.finish(1)

        wrapper = ObjFmtBuilder(
            "Game.Shared.Network.Profile.ProfileGenericUpdateEventArgs")
        wrapper.begin_list("Message", "Game.Shared.ProfileGenericMessage", 1)
        wrapper.begin_element(0, "Game.Shared.ProfileGenericMessage", 1)
        wrapper.field_bytes("Data", reward_bytes)
        args = wrapper.finish(1)

        compressed = compress_gzip(args)
        dw = encode_datawrapper(0, 2211, compressed, 1)
        issuer = (
            f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
            f"ServicePlayer.{self.client_uid}.{self.scnt}"
        )
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(f">>> PUSH DisplayRewards via GenericUpdate (dt=2211) "
                 f"{len(rewards)} reward(s), dw_sz={len(dw)}")

    def _send_cards_chunk(self, cards):
        ctype_names = [
            "Game.Shared.Network.Profile.CardsAddedEventArgs",
            "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
            "Game.Shared.Domain.card_instance_bits",
            "Game.Shared.ResourceId", "System.Guid", "System.UInt64",
            "System.Boolean", "System.String",
        ]
        def ft(tn):
            if tn not in ctype_names: ctype_names.append(tn)
            return ctype_names.index(tn)

        csizes = [] ; cbuf = io.BytesIO() ; w = lambda s: cbuf.write(s.encode("utf-8"))
        sep = lambda: cbuf.write(b";") ; lf = lambda: cbuf.write(b"\n")

        csizes.append(0)
        w(""); sep(); w("0"); sep(); w(str(ft(ctype_names[0]))); sep(); w("1"); sep()
        fc = cbuf.tell(); csizes.append(0)
        w("CardBits"); sep(); w("1"); sep(); w(str(ft(ctype_names[1]))); sep(); w("0"); sep()
        w(str(len(cards))); sep()

        for i, (guid, name, cost, atk, def_, cid, is_ext) in enumerate(cards):
            fe = cbuf.tell(); csizes.append(0) ; eidx = len(csizes)-1
            w(str(i)); sep(); w(str(eidx)); sep(); w(str(ft(ctype_names[2]))); sep(); w("6"); sep()
            f1 = cbuf.tell(); csizes.append(0)
            w("Id"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<Q", cid)).decode("ascii")); sep()
            csizes[-1] = cbuf.tell() - f1
            f2 = cbuf.tell(); csizes.append(0) ; tidx = len(csizes)-1
            w("TemplateID"); sep(); w(str(tidx)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
            gs = cbuf.tell(); csizes.append(0) ; gidx = len(csizes)-1
            w("guid"); sep(); w(str(gidx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
            w("36"); sep(); cbuf.write(guid.encode())
            csizes[gidx] = cbuf.tell() - gs ; csizes[tidx] = cbuf.tell() - f2
            f4 = cbuf.tell(); csizes.append(0)
            w("IsFoil"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
            w("0") ; csizes[-1] = cbuf.tell() - f4
            f5 = cbuf.tell(); csizes.append(0)
            w("IsExtended"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
            w("1" if is_ext else "0") ; csizes[-1] = cbuf.tell() - f5
            f7 = cbuf.tell(); csizes.append(0)
            w("IsNotTradeable"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
            w("0") ; csizes[-1] = cbuf.tell() - f7
            f8 = cbuf.tell(); csizes.append(0)
            w("EscrowStatus"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
            enc = b"Clean"; w(str(len(enc))); sep(); cbuf.write(enc)
            csizes[-1] = cbuf.tell() - f8 ; csizes[eidx] = cbuf.tell() - fe

        csizes[1] = cbuf.tell() - fc ; csizes[0] = cbuf.tell()
        w(";".join(ctype_names)); lf()
        for i, s in enumerate(csizes):
            if i > 0: w(";")
            w(str(s))
        resp_inner = cbuf.getvalue()
        compressed = compress_gzip(resp_inner)
        dw = encode_datawrapper(0, 2205, compressed, 1, "00000000-0000-0000-0000-000000000000")
        issuer = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{self.scnt}"
        self.scnt += 1
        self.send({"issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid}, dw)
        log_req(f">>> PUSH CardsAdded (dt=2205) {len(cards)} cards, dw_sz={len(dw)}")

    def _send_inventory_updated(self, template_guid, inventory_id, quantity=0):
        """Push the authoritative quantity for one inventory item.

        PlayerProfile removes an item when InventoryUpdated carries quantity
        zero (or a non-minimum claim date).  The fixed client checks the
        latter on this event rather than checking ``ev.Quantity`` directly,
        so consumed items must carry a non-minimum ClaimDate.  This is
        required for direct-opening chests because OpenChestResponse itself
        only contains reward IDs.
        """
        from objfmt_builder import ObjFmtBuilder

        b = ObjFmtBuilder(
            "Game.Shared.Network.Profile.InventoryUpdatedEventArgs")
        b.field_resource_id("ItemId", template_guid or
                            "00000000-0000-0000-0000-000000000000")
        b.field_int("Quantity", int(quantity))
        # UID.Type.InventoryItem is 11 in the client UID enum.
        b.field_uid("ItemInstanceUid", make_uid(11, int(inventory_id)))
        # PlayerProfile.HandleInventoryUpdate removes a cached item when the
        # event's ClaimDate is greater than DateTime.MinValue.  A zero
        # quantity alone is not sufficient in the client implementation.
        claim_date = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
        b.field_datetime("ClaimDate", claim_date)
        body = b.finish(4)
        compressed = compress_gzip(body)
        dw = encode_datawrapper(
            0, 2207, compressed, 1,
            "00000000-0000-0000-0000-000000000000")
        issuer = (
            f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
            f"ServicePlayer.{self.client_uid}.{self.scnt}")
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(
            f">>> PUSH InventoryUpdated (dt=2207) item={inventory_id} "
            f"quantity={quantity}, dw_sz={len(dw)}")

    def push_inventory_to_client(self, qty=1, template_guid="", item_id=1001):
        """Push an inventory item to the client via ProfileGenericUpdate (dt=2211).

        Structure the client expects (PlayerProfile.HandleProfileGenericUpdate):
            ProfileGenericUpdateEventArgs.Message  (single ProfileGenericMessage)
                .Data = ObjFmt bytes of ProfileGenericBatchUpdate
                          .Items = List<inventory_bits>
                          .GoldDelta = int
        """
        # Inner: ProfileGenericBatchUpdate with Items list + GoldDelta
        batch_bytes = encode_objfmt_response(
            ["Game.Shared.ProfileGenericBatchUpdate",
             "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
             "Game.Shared.Domain.inventory_bits", "System.UInt64",
             "Game.Shared.ResourceId", "System.Guid", "System.Boolean",
             "System.Int32", "System.DateTime", "System.String"],
            [("Items", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", 1,
                                [(template_guid, item_id, qty)])),
             ("GoldDelta", "int", 0)]
        )

        # Wrap in ProfileGenericUpdateEventArgs → Message (single) → Data
        args = encode_objfmt_response(
            ["Game.Shared.Network.Profile.ProfileGenericUpdateEventArgs",
             "Game.Shared.ProfileGenericMessage", "System.Byte[]"],
            [("Message", "struct", ("Game.Shared.ProfileGenericMessage", [("Data", "bytes", batch_bytes)]))]
        )

        compressed = compress_gzip(args)
        dw = encode_datawrapper(0, 2211, compressed, 1, "00000000-0000-0000-0000-000000000000")
        issuer = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.99"
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(f">>> PUSH Inventory item (dt=2211) template={template_guid}")
        # Store client item UID so we can push quantity updates later
        if self.user_profile and template_guid:
            db_set_inventory_client_uid(
                self.user_profile["id"], template_guid, item_id, conn=_db)
            _db.commit()


