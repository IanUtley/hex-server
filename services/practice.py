"""Practice match service — mirror match against the player's last saved deck."""

import json, random, struct, io
from binascii import hexlify

import game_engine
from db import (_db, log_req,
    db_get_deck, db_get_last_deck, db_get_champion_guid, db_get_charge_power,
    db_get_champion_ability_guids, db_get_card_abilities, db_get_card_type,
    db_get_card_template_for_instance, db_clear_session_cards, db_move_cards_to_hand,
    db_insert_game_card)
from encoder import encode_objfmt_response, compress_gzip, encode_datawrapper, encode_sync_event


def handle_practice(handler, session, target=None, instance=None, reqid=None,
                    comp=None, session_id=None, conh=None,
                    SERVICE_MAIL_UID=None, SERVICE_GAME_SESSION_UID=None, **_kw):
    """Create a practice mirror-match game session (22031 ReadyToStartGame)."""
    if not (session.session_name or "").startswith("Session-"):
        return False

    uid = handler.user_profile["id"]
    pl_t = game_engine.UID.make(244, int(handler.client_reck_id))
    ai_t = game_engine.UID.make(3, 1000)

    # Use the deck the player selected in deck builder, not "last saved"
    deck_db_id = getattr(session, 'deck_db_id', 0) or 0
    deck_row = db_get_deck(deck_db_id, uid) if deck_db_id else None
    if not deck_row:
        deck_row = db_get_last_deck(uid)
    if not deck_row:
        log_req("    Practice: no deck found")
        return False

    deck_db_id = deck_row[0]
    champ_guid = deck_row[2] or ""
    pve_champ_id = deck_row[3] or 0

    if not champ_guid and pve_champ_id:
        champ_guid = db_get_champion_guid(pve_champ_id)
    if not champ_guid:
        champ_guid = "1d462ffb-0744-4996-804c-ba61b2c5c2f1"
    card_ids = json.loads(deck_row[1] or "[]")
    log_req(f"    Practice: deck {deck_db_id}, {len(card_ids)} cards, champ {champ_guid[:8]}")

    ai_champ_guid = "f8f86969-2e47-4901-8c9e-7fbf8d859e22"
    ai_name = "AI Opponent"
    player_name = handler.user_profile["name"] if handler.user_profile else "Player"

    # Build game
    sess_id = session.session_id.uid64 if hasattr(session.session_id, 'uid64') else int(session.session_id)
    game = game_engine.Game(sess_id, pl_t, ai_t)
    game.turn_number = 1
    game.player_health = 20; game.ai_health = 20
    game.player_charges = 0; game.ai_charges = 0
    game.player_spell_points = 0; game.ai_spell_points = 0
    pchamp = game._new_card_id()
    achamp = game._new_card_id()
    log_req(f"    Champion UIDs: player={hex(pchamp.uid.to_uint64())} ai={hex(achamp.uid.to_uint64())}")
    log_req(f"    Player UIDs: pl_t={hex(pl_t.to_uint64())} ai_t={hex(ai_t.to_uint64())}")

    # Champion card defs — resolve abilities from champion_templates.charge_power
    # and champion_abilities (the champion's charge/spell powers)
    pl_abilities = []
    ai_abilities = []
    cp = db_get_charge_power(champ_guid)
    if cp:
        pl_abilities = [game_engine.ResourceId.from_str(cp)]
    if not pl_abilities:
        pl_abilities = [game_engine.ResourceId.from_str(r) for r in db_get_champion_ability_guids(champ_guid) if r]

    ai_cp = db_get_charge_power(ai_champ_guid)
    if ai_cp:
        ai_abilities = [game_engine.ResourceId.from_str(ai_cp)]
    if not ai_abilities:
        ai_abilities = [game_engine.ResourceId.from_str(r) for r in db_get_champion_ability_guids(ai_champ_guid) if r]

    handler._player_champ_abilities = list(pl_abilities)
    handler._ai_champ_ability_guids = [str(a.guid) for a in ai_abilities]
    handler._player_champ_guid = champ_guid
    handler._ai_champ_guid = ai_champ_guid
    handler._ai_champ_scid = achamp
    handler._player_champ_scid = pchamp

    game.card_defs[pchamp] = game_engine.CardDef("Player", game_engine.ECardTypes.Champion,
                                                 0, 20, 20, [], pl_abilities)
    ai_starting_health = handler._champion_health_by_guid(ai_champ_guid)
    game.card_defs[achamp] = game_engine.CardDef(ai_name, game_engine.ECardTypes.Champion,
                                                 0, ai_starting_health, ai_starting_health, [], ai_abilities)
    game.player_champion_card_id = pchamp
    game.ai_champion_card_id = achamp

    # 1. GameStarted
    game.push_game_started(
        champion_names=[player_name, ai_name],
        champion_template_ids=[champ_guid, ai_champ_guid],
        player_first=True)

    # 2. Player + champion card updates
    game.push_player_updated(pl_t, champ_id=pchamp)
    game.push_player_updated(ai_t, champ_id=achamp)
    game.push_card_updated(pchamp, pl_t, game_engine.ECardCollections.None_,
                          game_engine.ECardTypes.Champion, attack=0, defense=20,
                          template_id=champ_guid)
    game.push_card_updated(achamp, ai_t, game_engine.ECardCollections.None_,
                          game_engine.ECardTypes.Champion, attack=0, defense=ai_starting_health,
                          template_id=ai_champ_guid)

    # 3. Champion played (HUD portraits)
    game.push_champion_card_played(pl_t, False, player_name, pchamp)
    game.push_champion_card_played(ai_t, True, ai_name, achamp)

    # 4. Resolve card IDs to template GUIDs with card types + instance IDs
    tguids = []
    ttypes = []
    tinst = []
    for cid in card_ids:
        if isinstance(cid, str) and len(cid) == 36:
            tguids.append(cid)
            ttypes.append(db_get_card_type(cid))
            tinst.append(None)
        elif isinstance(cid, (int, float)):
            tr = db_get_card_template_for_instance(int(cid), uid)
            if tr:
                tguids.append(tr[0])
                ttypes.append(tr[1] or "Troop")
                tinst.append(int(cid))
    if not tguids:
        log_req("    Practice: no cards resolved")
        return False

    # Shuffle
    shuffled = list(zip(tguids, ttypes, tinst))
    random.shuffle(shuffled)

    # Delete stale game_cards for this session
    db_clear_session_cards(session.session_id)

    # 5. Create deck cards for both players
    pids = []
    aids = []
    for _tg, _ctname, _inst in shuffled:
        pids.append(game._new_card_id())
        aids.append(game._new_card_id())
    # Use same shuffle for AI as player
    ai_shuffled = list(shuffled)
    random.shuffle(ai_shuffled)

    for pos in range(len(pids)):
        pid = pids[pos]
        aid = aids[pos]
        tg, ctname, tinst = shuffled[pos]
        atg, actname, ainst = ai_shuffled[pos]
        ct = game_engine.card_type_from_db(ctname) if ctname else game_engine.ECardTypes.Troop
        act = game_engine.card_type_from_db(actname) if actname else game_engine.ECardTypes.Troop

        puid = pid.uid.to_uint64()
        auid = aid.uid.to_uint64()
        p_ab, p_attr = db_get_card_abilities(tg)
        a_ab, a_attr = db_get_card_abilities(atg)
        db_insert_game_card(session.session_id, uid, puid, tg, 'deck',
                           card_type=ctname, position=pos,
                           abilities_json=p_ab, attributes=p_attr)
        db_insert_game_card(session.session_id, 0, auid, atg, 'deck',
                           card_type=actname, position=pos,
                           abilities_json=a_ab, attributes=a_attr)
        _db.commit()
        # Full CardDef via the shared helper (thresholds, abilities, attrs,
        # instance-persisted data, gems) — same as FRA/campaign.
        handler._card_full_data(game, pid, tg, instance_id=tinst)
        handler._card_full_data(game, aid, atg, instance_id=ainst)
        # Deck card - face down
        game.push_card_updated(pid, pl_t, game_engine.ECardCollections.Deck, ct, nulling=True)
        game.push_card_updated(aid, ai_t, game_engine.ECardCollections.Deck, act, nulling=True)

    # 6. DeckCreated
    game.push_deck_created_with_cards(pl_t, pids)
    game.push_deck_created_with_cards(ai_t, aids)
    _db.commit()

    # 7. Draw opening hands (7 cards)
    drawn_pl = [pid for pid in pids[:7]]
    drawn_ai = [aid for aid in aids[:7]]
    for scid in drawn_pl:
        game.push_card_drawn(scid, pl_t, 0)
        game.push_card_moved(scid, pl_t, game_engine.ECardCollections.Hand, game_engine.ECardLocations.Top, 1)
        cdef = game.card_defs.get(scid)
        tg_idx = pids.index(scid)
        tg, _, _ = shuffled[tg_idx]
        ct = cdef.card_type if cdef else game_engine.ECardTypes.Troop
        game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand, ct, template_id=tg)
    for scid in drawn_ai:
        game.push_card_drawn(scid, ai_t, 0)
        game.push_card_moved(scid, ai_t, game_engine.ECardCollections.Hand, game_engine.ECardLocations.Top, 1)
        cdef = game.card_defs.get(scid)
        atg_idx = aids.index(scid)
        atg, _, _ = ai_shuffled[atg_idx]
        act = cdef.card_type if cdef else game_engine.ECardTypes.Troop
        game.push_card_updated(scid, ai_t, game_engine.ECardCollections.Hand, act, template_id=atg)

    # Update game_cards locations
    db_move_cards_to_hand(session.session_id, [scid.uid.to_uint64() for scid in drawn_pl + drawn_ai])
    _db.commit()

    # 8. SkipSetup in its own packet so client processes it before PreGame
    skip_game = game_engine.Game(sess_id, pl_t, ai_t)
    skip_game.push_skip_setup()
    skip_pkt = skip_game.make_network_packet(pl_t)
    skip_bytes = compress_gzip(encode_sync_event(skip_pkt))
    skip_dw = encode_datawrapper(0, 3055, skip_bytes, 1, "00000000-0000-0000-0000-000000000000")
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{handler.scnt}",
        "target": "ServiceGameSession", "instance": str(session.server_id),
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }, skip_dw)
    log_req(f"    Practice: pushed SkipSetup ({len(skip_dw)}b)")

    # 9. PreGame phase + main game events
    game.push_turn_phase(game_engine.ETurnPhases.PreGame)

    # Push main game events
    pkt = game.make_network_packet(pl_t)
    evt_bytes = compress_gzip(encode_sync_event(pkt))
    evt_dw = encode_datawrapper(0, 3055, evt_bytes, 1, "00000000-0000-0000-0000-000000000000")
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{handler.scnt}",
        "target": "ServiceGameSession", "instance": str(session.server_id),
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }, evt_dw)
    log_req(f"    Practice: pushed game init ({len(evt_dw)}b)")

    # 10. Skip PickGoesFirst (player goes first), go to Mulligan
    game2 = game_engine.Game(sess_id, pl_t, ai_t)
    game2.push_turn_phase(game_engine.ETurnPhases.Mulligan, pl_t, pl_t)
    game2.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
    pkt2 = game2.make_network_packet(pl_t)
    evt2_bytes = compress_gzip(encode_sync_event(pkt2))
    evt2_dw = encode_datawrapper(0, 3055, evt2_bytes, 1, "00000000-0000-0000-0000-000000000000")
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{handler.scnt}",
        "target": "ServiceGameSession", "instance": str(session.server_id),
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }, evt2_dw)
    log_req(f"    Practice: pushed Mulligan ({len(evt2_dw)}b)")

    return True
