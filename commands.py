"""Chat/debug commands for the Hex private server.

All commands receive the handler instance (self) for DB access, event sending, etc.
"""
import struct as _struct
import json as _json
import sys as _sys

import game_engine
import game_session
import hconnect_server
import encoder
import campaign
from encoder import encode_datawrapper, compress_gzip, encode_sync_event


def reload_runtime_modules():
    """Reload server modules used by the HConnect SIGUSR1 hook."""
    import importlib, ai as aim
    import gamemodes.tournament_engine as te, gamemodes.tournament_server as ts
    import services.tournament_game as tg, services.chat as sch
    import services.arena as arena_service, services.mail as mail_service
    import encoder as en, db as dbm
    import abilities.framework.bom as ability_bom
    import abilities.framework.targeting as ability_targeting
    import abilities.framework.resolution as ability_resolution
    import abilities.framework.triggers as ability_triggers
    import abilities as abilities_pkg, ability as ability_compat
    importlib.reload(dbm); importlib.reload(aim); importlib.reload(en)
    # Campaign handlers are imported by the live HConnect module and must be
    # refreshed here as well; otherwise SIGUSR1 would leave campaign.py
    # changes stale until a full process restart.
    importlib.reload(campaign)
    # tournament_game imports the trigger dispatcher lazily, but Python
    # retains the already-loaded abilities.framework.triggers module. Refresh
    # the framework first, then public abilities and game services.
    importlib.reload(ability_targeting)
    importlib.reload(ability_resolution)
    importlib.reload(ability_bom)
    importlib.reload(ability_triggers)
    importlib.reload(abilities_pkg)
    importlib.reload(ability_compat)
    importlib.reload(te); importlib.reload(ts)
    importlib.reload(tg); importlib.reload(sch)
    importlib.reload(arena_service); importlib.reload(mail_service)
    # When launched as ``python hconnect_server.py``, the live module is
    # ``__main__``. Rebind its tournament globals after reloading.
    hc = _sys.modules.get("__main__")
    if hc is None or not hasattr(hc, "player_handlers"):
        import hconnect_server as hc
    hc.tournament_server = ts
    hc.campaign = campaign
    hc.player_handlers = te.player_handlers
    hc.player_handler_lock = te.player_handler_lock
    hc.player_decks = te.player_decks
    hc.push_tournament_room_data = te.push_tournament_room_data
    hc.build_tournament_desc_json = te.build_tournament_desc_json
    hc.build_waiting_room_data = te.build_waiting_room_data
    hc.build_tournament_info_data = te.build_tournament_info_data
    hc.uid_instance = te.uid_instance
    hc.start_waiting_room_game = te.start_waiting_room_game
    hc._encode_enter_tournament_error = te._encode_enter_tournament_error
    hc._make_deck_data = te._make_deck_data
    return "Tournament, AI, ability, Arena, and Mail modules reloaded + globals rebound"


def _chat_card_link(name, template_guid):
    """Return the client's clickable card-link markup for a template."""
    return (f"[url=OnClick(OnClickLinkedCard::{template_guid}|0|0::"
            f"CardLink_Tooltip);][{name}][/url]")


def handle_command(handler, cmd: str, room: str, username: str) -> str:
    # The profile flag controls both the client's console UI and the server
    # endpoint.  Do not rely on the client hiding the backtick console: a
    # client can still submit a chat command directly.
    if "allowcon" not in getattr(hconnect_server, "PROFILE_FEATURE_FLAGS", ()):
        return "Developer console is disabled"
    parts = cmd.strip().split()
    if not parts:
        return ("Commands: !help !game_end !encounter !hand !zones !playable !gencard "
                "!update !threshold !resource !pass !phase !draw !discard "
                "!addcard")

    action = parts[0].lower()
    args = parts[1:]
    import sys
    print(f"  [CMD DEBUG] action={action} args={args}", file=sys.stderr, flush=True)

    # !game_end and !encounter operate on the campaign layer — handle them
    # before the session gate so they work from the panorama too.
    if action == "game_end":
        try:
            return _cmd_game_end(handler, args)
        except Exception as e:
            return f"Error: {e}"
    if action == "encounter":
        try:
            return _cmd_encounter(handler, args)
        except Exception as e:
            return f"Error: {e}"
    if action == "challenge":
        try:
            return _cmd_challenge(handler, args)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"Error: {e}"
    if action == "reload":
        return reload_runtime_modules()

    player_uid = encoder.make_uid(hconnect_server.UID_TYPE["ServicePlayer"], int(handler.client_reck_id))
    session = game_session.find_session_by_player(player_uid)
    if not session:
        return "No active game"

    pl_t = game_engine.UID.make(244, int(handler.client_reck_id))
    ai_t = game_engine.UID.make(3, 1000)
    # Tournament game_cards are keyed by the ServicePlayer/reckoning id;
    # campaign/practice cards use the local profile id.  Debug commands must
    # use the same owner key as the active game or they can create a phantom
    # tournament participant when a card is generated.
    is_tourney = session and (session.session_name or "").startswith("tourney-")
    command_owner_id = (int(handler.client_reck_id) if is_tourney
                        else handler.user_profile["id"])

    try:
        result = _dispatch(handler, action, args, session, pl_t, ai_t, room, username)
    except Exception as e:
        return f"Error: {e}"
    # Refresh playability for Practice/campaign commands.  Tournament PvP has
    # a separate two-player state machine; loading the PvE battle state here
    # would read a default state and push stale options/priority back to one
    # client after a debug command.
    if not is_tourney:
        try:
            import battle_engine as _be
            bstate = _be.load_state(session)
            phase = _be.current_phase(bstate)
            if bstate.get("turn_player") == _be.PLAYER:
                if phase in (game_engine.ETurnPhases.FirstMainPhase,
                             game_engine.ETurnPhases.SecondMainPhase):
                    handler._push_main_phase_options(session, pl_t, ai_t)
                else:
                    handler._push_phase_options_empty(session, pl_t, ai_t)
        except Exception:
            pass
    return result


def _send_chat(handler, msg, room, username):
    """Send a chat message as the server."""
    from datetime import datetime
    now = datetime.now().strftime("[%H:%M]")
    echo = _json.dumps({"action": "rchat", "room": room, "rflg": "",
                         "user": f"Server {now}", "msg": msg, "flags": "", "icon": ""})
    handler.scnt += 1
    handler.send({"issuer": "Session", "target": "chat", "sid": handler.sid},
                  body=echo.encode("utf-8"))


def _send_game_events(handler, game, session, pl_t):
    """Send a network packet from game events."""
    if not game.events:
        return
    SVC_GS = hconnect_server.SERVICE_GAME_SESSION_UID
    pkt = game.make_network_packet(pl_t)
    dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                             "00000000-0000-0000-0000-000000000000")
    handler.scnt += 1
    headers = {
        "issuer": f"0.0.0.0.ServiceGameSession.{SVC_GS}.{session.session_id}.{handler.scnt}",
        "target": "ServiceGameSession", "instance": str(session.server_id),
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }
    handler.send(headers, dw)
    handler._event_q.append((handler.scnt, dw, headers))
    if len(handler._event_q) > 100:
        handler._event_q = handler._event_q[-50:]


def _refresh_pvp_debug_options(tournament_game, session, state):
    """Rebuild the current PvP option projection after a debug state change."""
    phase = int(state.get("phase", 0))
    if state.get("stack"):
        tournament_game.pvp_push_phase_options(
            session, state, pid=state.get("priority_pid"))
    elif phase in (game_engine.ETurnPhases.FirstMainPhase,
                   game_engine.ETurnPhases.SecondMainPhase):
        tournament_game.pvp_push_main_phase_options(session, state)
    elif phase == game_engine.ETurnPhases.DeclareAttack:
        tournament_game.pvp_push_attack_options(session, state)
    elif phase == game_engine.ETurnPhases.DeclareDefense:
        tournament_game.pvp_push_blocker_options(session, state)
    elif phase not in (3, 4, 5, 6, 7, 8, 9):
        tournament_game.pvp_push_phase_options(
            session, state, pid=state.get("priority_pid"))


def _cmd_encounter(handler, args):
    """Launch an encounter battle: !encounter <encounter_guid>.

    Skips the panorama conversation flow and pushes a gamestarted notification
    directly so the client transitions to the battle scene. Works from the
    panorama or anywhere else.
    """
    if not args:
        return "Usage: !encounter <guid>  — see ENCOUNTERS.md for GUIDs"
    db = hconnect_server._db
    encounter_guid = args[0]

    # Find the player's champion and campaign
    uid = handler.user_profile["id"]
    champ = db.execute(
        "SELECT id, last_deck_id FROM champions WHERE user_id=? AND is_deleted=0 ORDER BY id DESC LIMIT 1",
        (uid,)).fetchone()
    if not champ:
        return "No champion found — create one first"
    champ_id, deck_db_id = champ[0], champ[1]
    deck_uid64 = (deck_db_id << 8) | 17 if deck_db_id else 0

    camp = db.execute(
        "SELECT id FROM campaigns WHERE champion_id=? ORDER BY id DESC LIMIT 1",
        (champ_id,)).fetchone()
    camp_id = camp[0] if camp else 0

    import campaign
    campaign._launch_encounter(handler, db, camp_id, champ_id, encounter_guid,
                               deck_uid64, 0, "00000000-0000-0000-0000-000000000000",
                               "ServiceCampaign", str(hconnect_server.UID_TYPE["ServiceCampaign"]),
                               0, hconnect_server.SERVICE_MAIL_UID)
    return f"Launched encounter {encounter_guid} (camp={camp_id})"


def _cmd_game_end(handler, args):
    """End the current battle: !game_end victory|defeat.

    Pushes the battle GameEnded event (shows the Victory/Defeat screen in the
    client) AND the campaign gameendnotify (updates campaign state).
    """
    db = hconnect_server._db
    result = args[0].lower() if args else "victory"
    won = result in ("win", "won", "victory", "winlose", "true", "1")
    out = []

    # 1) Battle GameEnded event so the client leaves the battle UI.
    player_uid = encoder.make_uid(hconnect_server.UID_TYPE["ServicePlayer"],
                                  int(handler.client_reck_id))
    session = game_session.find_session_by_player(player_uid)
    if session:
        try:
            _push_battle_game_end(handler, session, won)
            out.append(f"GameEnded pushed to session {session.session_id} ({result})")
        except Exception as e:
            out.append(f"GameEnded error: {e}")
    else:
        out.append("No active battle session")

    # 2) Campaign gameendnotify — updates campaign state (reveals quest NPC on a win).
    camp_row = db.execute(
        "SELECT c.id FROM campaigns c JOIN champions ch ON c.champion_id=ch.id "
        "WHERE ch.user_id=? ORDER BY c.id DESC LIMIT 1",
        (handler.user_profile["id"],)
    ).fetchone()
    if not camp_row:
        out.append("No active campaign for this player")
    else:
        camp_id = camp_row[0]
        msg = campaign.push_gameendnotify(
            handler, db, camp_id, won, 0, "00000000-0000-0000-0000-000000000000",
            "ServiceCampaign", str(hconnect_server.UID_TYPE["ServiceCampaign"]), 0,
            hconnect_server.SERVICE_MAIL_UID)
        out.append(f"Campaign {camp_id}: {msg}")
    return "; ".join(out)


def _cmd_challenge(handler, args):
    """Challenge a friend to a duel: !challenge <player_name>"""
    import sys as _sys
    _hcs = _sys.modules.get("hconnect_server") or _sys.modules.get("__main__")
    if not args:
        return "Usage: !challenge <player_name>"

    db = _hcs._db
    opp_name = " ".join(args)
    my_name = handler.user_profile.get("name", "Unknown") if handler.user_profile else "Unknown"
    my_id = handler.user_profile["id"] if handler.user_profile else 0

    # Look up opponent
    opp_row = db.execute(
        "SELECT id, name FROM users WHERE LOWER(name)=LOWER(?) LIMIT 1",
        (opp_name,)).fetchone()
    if not opp_row:
        return f"Player '{opp_name}' not found"

    opp_id = opp_row[0]
    opp_name = opp_row[1]

    # Check opponent is online via realtime dict from sys.modules
    live_active = _hcs._active_clients
    active = live_active.get(opp_id, [])
    if not active:
        return f"{opp_name} is not online"

    opp_handler = active[0][0]

    # Get challenger's deck
    my_deck = db.execute(
        "SELECT id FROM decks WHERE user_id=? AND is_deleted=0 ORDER BY id DESC LIMIT 1",
        (my_id,)).fetchone()
    my_deck_id = my_deck[0] if my_deck else 0
    my_deck_uid64 = (my_deck_id << 8) | 17 if my_deck_id else 0

    # Get opponent's deck
    opp_deck = db.execute(
        "SELECT id FROM decks WHERE user_id=? AND is_deleted=0 ORDER BY id DESC LIMIT 1",
        (opp_id,)).fetchone()
    opp_deck_id = opp_deck[0] if opp_deck else 0
    opp_deck_uid64 = (opp_deck_id << 8) | 17 if opp_deck_id else 0

    # Create game session
    import game_session as gs
    my_uid = encoder.make_uid(_hcs.UID_TYPE["ServicePlayer"], int(handler.client_reck_id))
    session_name = f"Challenge_{my_name}_vs_{opp_name}"
    session = gs.create_encounter_session(session_name, {}, my_uid)
    session.add_player(encoder.make_uid(_hcs.UID_TYPE["ServicePlayer"], opp_id), 1)
    session.set_state("joined")
    sess_uid = int(session.session_id)
    room_id = int(session_id_fallback(session))

    # Push 25072 + 25060 to both players
    from encoder import encode_objfmt_response
    _challenge_push_25072_25060(handler, room_id, sess_uid, session_name, my_deck_uid64, my_id, my_name)
    _challenge_push_25072_25060(opp_handler, room_id, sess_uid, session_name, opp_deck_uid64, opp_id, opp_name)

    return f"Challenged {opp_name}! Game session {sess_uid} created."


def session_id_fallback(session):
    """Get a numeric session ID for tournament push compatibility."""
    if hasattr(session, 'session_id') and hasattr(session.session_id, 'uid64'):
        return int(session.session_id.uid64)
    return int(session.session_id)


def _challenge_push_25072_25060(h, room_id, sess_uid, session_name, deck_uid64, player_id, player_name):
    """Push DeckConstructionStarted (25072) + TournamentSessionStart (25060) to one player."""
    import sys as _sys
    _hcs = _sys.modules.get("hconnect_server") or _sys.modules.get("__main__")
    from encoder import encode_objfmt_response, compress_gzip, encode_datawrapper

    # 25072 — sets CurrentTournament
    dcs_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.DeckConstructionStartedEventArgs",
         "Game.Shared.Tournaments.TournamentInfo",
         "Game.Shared.Domain.deck_bits"],
        [("TournamentID", "ulong", room_id),
         ("TournamentInfo", "struct",
          ("Game.Shared.Tournaments.TournamentInfo",
           [("TournamentID", "ulong", room_id)])),
         ("my_Deck", "class", "Game.Shared.Domain.deck_bits"),
         ("timeForSideboarding", "long", 0),
         ("PlayerID", "ulong", player_id)])

    dcs_body = compress_gzip(dcs_inner)
    dcs_dw = encode_datawrapper(0, 25072, dcs_body, 1,
                                "00000000-0000-0000-0000-000000000000")
    h.scnt += 1
    h.send({
        "issuer": str(_hcs.SERVICE_MAIL_UID),
        "target": "ServicePlayer", "instance": h.sid or "0",
        "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
    }, dcs_dw)

    # 25060 — transition to Battle
    enc_flags = 1024 | 4096 | 8192  # IsImmortalPvP | IsStandardPvP | IsDuelingPit
    evt_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.TournamentSessionStartEventArgs",
         "Game.Shared.SessionState",
         "Game.Shared.SessionStateEncounterData",
         "Game.Shared.UID"],
        [("SessionState", "struct",
          ("Game.Shared.SessionState",
           [("SessionId", "uid", sess_uid),
            ("SessionName", "string", session_name),
            ("MinimumPlayerCount", "int", 2),
            ("MaximumPlayerCount", "int", 2),
            ("EncounterData", "struct",
             ("Game.Shared.SessionStateEncounterData",
              [("SessionFlags", "int", enc_flags),
               ("IsVirtualTournament", "bool", True),
               ("TournamentID", "ulong", room_id),
               ])),
            ("JoinInsteadOfReconnect", "bool", True)])),
         ("DeckId", "uid", deck_uid64),
         ("Forced", "bool", True)])

    evt_body = compress_gzip(evt_inner)
    evt_dw = encode_datawrapper(0, 25060, evt_body, 1,
                                "00000000-0000-0000-0000-000000000000")
    h.scnt += 1
    h.send({
        "issuer": str(_hcs.SERVICE_MAIL_UID),
        "target": "ServicePlayer", "instance": h.sid or "0",
        "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
    }, evt_dw)


def _push_battle_game_end(handler, session, won):
    """Push a GameEndedSessionEventArgs (class 2) event for the current battle.

    Winner/loser UID lists wrapped in a NetworkPacketSessionEventArgs pushed on
    the 3055 channel so the client shows the Victory/Defeat screen.
    """
    pl_uid = game_engine.UID.make(244, int(handler.client_reck_id))
    ai_uid = game_engine.UID.make(3, 1000)
    if won:
        push_battle_game_end(handler, session, [pl_uid], [ai_uid])
    else:
        push_battle_game_end(handler, session, [ai_uid], [pl_uid])


def push_battle_game_end(handler, session, winners, losers):
    """Encode and send a GameEnded event for a battle session on the 3055 channel."""
    pl_uid = game_engine.UID.make(244, int(handler.client_reck_id))
    nw = game_engine.make_game_ended_packet(session.session_id, pl_uid,
                                                winners, losers)
    ge_bytes = compress_gzip(encode_sync_event(nw))
    ge_dw = encode_datawrapper(0, 3055, ge_bytes, 1,
                               "00000000-0000-0000-0000-000000000000")
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceGameSession.{hconnect_server.SERVICE_GAME_SESSION_UID}.{session.session_id}.{handler.scnt}",
        "target": "ServiceGameSession", "instance": str(session.server_id),
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }, ge_dw)
    session.set_state("ended")


def _push_card_update(handler, db, session, pl_t, card_id, user_id=None, **overrides):
    """Fetch card info from DB and send a CardUpdated event.

    Works for both player and AI cards (user_id=0 for the AI). Template data
    resolves via game_cards.template_guid — one path for instance-based and
    GUID cards.
    """
    if user_id is None:
        user_id = handler.user_profile["id"]
    row = db.execute(
        "SELECT gc.card_template_id, gc.location, gc.template_guid FROM game_cards gc "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.card_uid=?",
        (session.session_id, user_id, card_id)).fetchone()
    if not row:
        return
    instance_id = row[0]
    zone_str = row[1] or 'Deck'
    ZONE_MAP = {'deck': 1, 'hand': 2, 'void': 32, 'discard': 16, 'warzone': 8,
                 'playedresources': 64, 'underground': 256, 'champions': 4}
    zone_val = ZONE_MAP.get(zone_str.lower(), 1)
    tpl_guid = "00000000-0000-0000-0000-000000000000"
    ct = game_engine.ECardTypes.Troop
    cost, atk, def_ = 0, 0, 0
    if row[2]:
        trow = db.execute(
            "SELECT ct.card_type, ct.cost, ct.attack, ct.defense "
            "FROM card_templates ct WHERE ct.guid=?", (row[2],)).fetchone()
        if trow:
            tpl_guid = row[2]
            ct = game_engine.card_type_from_db(trow[0])
            cost, atk, def_ = trow[1] or 0, trow[2] or 0, trow[3] or 0
    # Fetch thresholds, abilities, and gems
    shards = []
    abilities = []
    gem_type = 0
    if tpl_guid != "00000000-0000-0000-0000-000000000000":
        srow = db.execute("SELECT threshold_json, abilities_json FROM card_templates WHERE guid=?", (tpl_guid,)).fetchone()
        if srow:
            if srow[0]:
                try:
                    td = _json.loads(srow[0])
                    shard_flags_map = {0:0, 1:4, 2:8, 3:16, 4:32, 5:64}
                    raw_list = td.get('list', [])
                    shards = [shard_flags_map.get(s, s) for s in raw_list]
                except: pass
            if srow[1]:
                try:
                    abilities = [game_engine.ResourceId.from_str(g) for g in _json.loads(srow[1])]
                except: pass
        # Fetch gems from deck
        arena_k = hconnect_server.db_get_arena_state(handler.user_profile["id"])
        deck_k_id = handler._resolve_fra_deck_id(arena_k["deck_id"]) or 0
        gem_row = db.execute("SELECT active_gems FROM decks WHERE id=?", (deck_k_id,)).fetchone()
        if gem_row and gem_row[0]:
            try:
                gems = _json.loads(gem_row[0])
                gem_type = int(gems.get(str(instance_id), 0)) if gems else 0
            except: pass
    scid = game_engine.SessionCardId(game_engine.UID(card_id))
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(session.session_id, pl_t, ai_t)
    game.card_defs[scid] = game_engine.CardDef("Card", ct, cost, atk, def_, shards, abilities)
    kwargs = {'attack': atk, 'defense': def_, 'cost': cost, 'template_id': tpl_guid, 'gems': gem_type}
    kwargs.update(overrides)
    attr_override = kwargs.pop('attributes', None)
    state_val = kwargs.pop('state', game_engine.ECardStates.None_)
    collection_override = kwargs.pop('collection_override', None)
    if collection_override is not None:
        zone_val = collection_override
    game.push_card_updated(scid, pl_t, zone_val, ct, state=state_val, **kwargs)
    if attr_override is not None and game.events:
        game.events[-1].attributes = attr_override
    _send_game_events(handler, game, session, pl_t)


def _dispatch(handler, action, args, session, pl_t, ai_t, room, username):
    db = hconnect_server._db
    # Tournament game_cards are keyed by the ServicePlayer/reckoning id;
    # campaign/practice cards use the local profile id.  Keep debug commands
    # on the same owner key as the active game.
    is_tourney = session and (session.session_name or "").startswith("tourney-")
    command_owner_id = (int(handler.client_reck_id) if is_tourney
                        else handler.user_profile["id"])

    if action == "draw":
        count = max(1, int(args[0]) if args else 1)
        if is_tourney:
            from services.tournament_game import pvp_debug_draw
            drew = pvp_debug_draw(handler, session, count)
            return f"Drew {drew} cards"
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        drew = 0
        for _ in range(count):
            r = db.execute(
                "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? AND location='deck'",
                (session.session_id, command_owner_id)).fetchone()
            if not r or r[0] == 0:
                break
            handler._player_draw_card(game, session, pl_t, command_owner_id)
            drew += 1
        _send_game_events(handler, game, session, pl_t)
        return f"Drew {drew} cards"

    elif action == "phase":
        pn = args[0] if args else "StartTurn"
        pv = getattr(game_engine.ETurnPhases, pn, game_engine.ETurnPhases.StartTurn)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.push_turn_phase(pv, pl_t, pl_t)
        _send_game_events(handler, game, session, pl_t)
        return f"Phase set to {pn}"

    elif action == "addcard":
        # Draw the next copy of a card (by name or card_uid) that is still in the
        # deck to hand.
        if not args:
            return "Usage: addcard <name|id>"
        a = args[0]
        al = a.lower()
        target = None
        try:
            uid_int = int(al)
            row = db.execute(
                "SELECT gc.card_uid, gc.template_guid, gc.card_template_id FROM game_cards gc "
                "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='deck' AND gc.card_uid=? LIMIT 1",
                (session.session_id, command_owner_id, uid_int)).fetchone()
            if row:
                target = row
        except ValueError:
            rows = db.execute(
                "SELECT gc.card_uid, gc.template_guid, gc.card_template_id FROM game_cards gc "
                "JOIN card_templates ct ON ct.guid = gc.template_guid "
                "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='deck' "
                "AND LOWER(ct.name) LIKE ? ORDER BY gc.position LIMIT 1",
                (session.session_id, command_owner_id, "%" + al + "%")).fetchall()
            if rows:
                target = rows[0]
        if not target:
            return f"No copy of '{a}' left in deck"
        card_uid, tpl_guid, card_tpl_id = target
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        handler._move_deck_to_hand(game, session, pl_t, card_uid, tpl_guid, card_tpl_id)
        _send_game_events(handler, game, session, pl_t)
        return f"Added {a} to hand"

    elif action == "discard":
        # Trigger the discard effect directly: pick a random hand card and move
        # it to the discard zone (DiscardCardAbilityEffectTemplate behaviour).
        import random as _rnd
        hand_rows = db.execute(
            "SELECT card_uid, template_guid FROM game_cards WHERE session_id=? AND user_id=? "
            "AND location='hand' ORDER BY position",
            (session.session_id, handler.user_profile["id"])).fetchall()
        if not hand_rows:
            return "No cards in hand"
        row = _rnd.choice(hand_rows)
        card_uid, tpl_guid = row[0], row[1]
        # Discard to the card's OWNER (a Mind Grasp steal returns to the AI's
        # graveyard — user_id is the controller, owner_user_id the true owner).
        owner_row = db.execute(
            "SELECT owner_user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, card_uid)).fetchone()
        owner_uid = owner_row[0] if owner_row else handler.user_profile["id"]
        from db import db_discard_card
        db_discard_card(session.session_id, card_uid, owner_user_id=owner_uid)
        owner_player_uid = ai_t if owner_uid == 0 else pl_t
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        scid = game_engine.SessionCardId(game_engine.UID(card_uid))
        # Populate the CardDef with full thresholds + active gems so the graveyard
        # view renders the card completely (not just name/type/stats).
        tpl_d, ct_d, name_d, cost_d, atk_d, def_d, gem_d = handler._card_full_data(
            game, scid, tpl_guid, None)
        game.push_card_discarded(scid, owner_player_uid)
        game.push_card_updated(scid, owner_player_uid, game_engine.ECardCollections.Discard,
                               game_engine.card_type_from_db(ct_d) if ct_d else game_engine.ECardTypes.Troop,
                               attack=atk_d, defense=def_d, cost=cost_d,
                               template_id=tpl_d, gems=gem_d)
        game.push_card_moved(scid, owner_player_uid, game_engine.ECardCollections.Discard,
                             game_engine.ECardLocations.Top, 0)
        game.push_player_updated(pl_t, champ_id=getattr(handler, "_player_champ_scid", None))
        game.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
        _send_game_events(handler, game, session, pl_t)
        return f"Discarded a card ({len(hand_rows)} in hand)"

    elif action == "pass":
        TURN_PHASES = [
            game_engine.ETurnPhases.FirstMainPhase,
            game_engine.ETurnPhases.DeclareCombatPriorityWindow,
            game_engine.ETurnPhases.DeclareAttack,
            game_engine.ETurnPhases.DeclareAttackPriorityWindow,
            game_engine.ETurnPhases.DeclareDefense,
            game_engine.ETurnPhases.DeclareDefensePriorityWindow,
            game_engine.ETurnPhases.AssignFirstStrikeDamage,
            game_engine.ETurnPhases.FirstStrikePriorityWindow,
            game_engine.ETurnPhases.AssignDamage,
            game_engine.ETurnPhases.SecondMainPhase,
            game_engine.ETurnPhases.EndPhase,
            game_engine.ETurnPhases.Discard,
            game_engine.ETurnPhases.EndTurn,
        ]
        if not hasattr(session, 'current_phase_idx'):
            session.current_phase_idx = 0
        else:
            session.current_phase_idx += 1
        idx = session.current_phase_idx % len(TURN_PHASES)
        phase = TURN_PHASES[idx]
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.push_turn_phase(phase, pl_t, pl_t)
        _send_game_events(handler, game, session, pl_t)
        return f"Phase: {idx}.{phase}"

    elif action == "hand":
        target = args[0].lower() if args else "me"
        user_id = handler.user_profile["id"] if target != "opp" else 0
        rows = db.execute(
            "SELECT gc.card_uid, ct.name, ct.cost, ct.card_type FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='hand' ORDER BY gc.position",
            (session.session_id, user_id)).fetchall()
        lines = [f"{r[1]} [{r[0]}]" for r in rows]
        return f"{target} hand: " + ", ".join(lines)

    elif action == "aihand":
        # Reveal all AI hand cards to the player (push CardUpdated with nulling=False)
        rows = db.execute(
            "SELECT gc.card_uid, gc.template_guid FROM game_cards gc "
            "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='hand' ORDER BY gc.position",
            (session.session_id,)).fetchall()
        if not rows:
            return "AI hand is empty"
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        lines = []
        for uid, tpl in rows:
            scid = game_engine.SessionCardId(game_engine.UID(uid))
            t = handler._template_by_guid(tpl)
            ct = game_engine.card_type_from_db(t[1]) if t else game_engine.ECardTypes.Troop
            handler._card_full_data(game, scid, tpl)
            game.push_card_updated(scid, ai_t, game_engine.ECardCollections.Hand, ct,
                                   template_id=tpl, nulling=False)
            lines.append(f"{t[2] if t else 'Card'} [{uid}]")
        _send_game_events(handler, game, session, pl_t)
        return f"AI hand: " + ", ".join(lines)

    elif action == "playable":
        filter_ids = set()
        filter_names = []
        for a in args:
            try:
                filter_ids.add(int(a))
            except ValueError:
                filter_names.append(a.lower())
        name_filter = " ".join(filter_names) if filter_names else ""
        rows = db.execute(
            "SELECT gc.card_uid, ct.name FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.user_id=? AND gc.position < 100 ORDER BY gc.position LIMIT 7",
            (session.session_id, handler.user_profile["id"])).fetchall()
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        playable = []
        for row in rows:
            scid = game_engine.SessionCardId(game_engine.UID(row[0]))
            name = row[1] or ""
            uid = row[0]
            if not args or uid in filter_ids or (name_filter and name_filter in name.lower()):
                playable.append(scid)
        game.push_options(pl_t, playable)
        _send_game_events(handler, game, session, pl_t)
        lbl = f" (filter: {name_filter})" if name_filter else " (all)"
        return f"Playable: {len(playable)}/{len(rows)} cards" + lbl

    elif action == "threshold":
        target = args[0].lower() if args else ""
        if target in ("me", "opp"):
            vals = [int(a) for a in args[1:7]]
        else:
            target = "me"
            vals = [int(a) for a in args[0:6]]
        while len(vals) < 6:
            vals.append(0)
        if (session.session_name or "").startswith("tourney-"):
            from db import db_game_session_pids
            from services import tournament_game as _tg
            state = _tg.pvp_load_state(session) or {}
            pids = db_game_session_pids(session.session_id)
            changed_pid = (int(handler.client_reck_id) if target == "me"
                           else next((int(pid) for pid in pids
                                      if int(pid) != int(handler.client_reck_id)),
                                     int(handler.client_reck_id)))
            state[f"thresh_{changed_pid}"] = {
                flag: count for flag, count in zip(
                    [1, 4, 8, 16, 32, 64], vals) if count
            }
            _tg.pvp_save_state(session, state)
            _tg._pvp_sync_game_state(session)
            _refresh_pvp_debug_options(_tg, session, state)
            return f"Thresholds: {vals} for {target}"

        uid = pl_t if target == "me" else ai_t
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        thresholds = {
            game_engine.ECardShards.Colorless: vals[0],
            game_engine.ECardShards.Blood: vals[1],
            game_engine.ECardShards.Ruby: vals[2],
            game_engine.ECardShards.Sapphire: vals[3],
            game_engine.ECardShards.Wild: vals[4],
            game_engine.ECardShards.Diamond: vals[5],
        }
        import battle_engine as _be
        bstate = _be.load_state(session)
        bstate["player_threshold" if target == "me" else "ai_threshold"] = thresholds
        _be.save_state(session, bstate)
        game.player_threshold = dict(bstate.get("player_threshold") or {})
        game.ai_threshold = dict(bstate.get("ai_threshold") or {})
        game.push_player_updated(uid, champ_id=getattr(handler, "_player_champ_scid" if target == "me" else "_ai_champ_scid", None))
        for cs_val, count in zip([1, 4, 8, 16, 32, 64], vals):
            if count > 0:
                ev = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                ev.player_id = uid; ev.color = cs_val; ev.operation = 1
                ev.delta = count; ev.new_value = count
                game._push(ev)
        _send_game_events(handler, game, session, pl_t)
        return f"Thresholds: {vals} for {target}"

    elif action == "charge":
        target = args[0].lower() if args else ""
        if target in ("me", "opp"):
            val = int(args[1]) if len(args) > 1 else 0
        else:
            target = "me"
            val = int(args[0]) if args else 0
        uid = pl_t if target == "me" else ai_t
        if (session.session_name or "").startswith("tourney-"):
            from db import db_game_session_pids
            from services import tournament_game as _tg
            state = _tg.pvp_load_state(session) or {}
            pids = db_game_session_pids(session.session_id)
            changed_pid = (int(handler.client_reck_id) if target == "me"
                           else next((int(pid) for pid in pids
                                      if int(pid) != int(handler.client_reck_id)),
                                     int(handler.client_reck_id)))
            state[f"chg_{changed_pid}"] = val
            _tg.pvp_save_state(session, state)
            _tg._pvp_sync_game_state(session)
            _refresh_pvp_debug_options(_tg, session, state)
            return f"Charges set to {val} for {target}"

        # Persist to battle state so playability/affordability reflects it.
        import battle_engine as _be
        bstate = _be.load_state(session)
        bstate["player_charges" if target == "me" else "ai_charges"] = val
        _be.save_state(session, bstate)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.player_charges = bstate.get("player_charges", 0)
        game.ai_charges = bstate.get("ai_charges", 0)
        ev = game_engine.ChampionChargePointsChangedSessionEventArgs()
        ev.player_id = uid; ev.operation = 0; ev.delta = val; ev.new_value = val
        game._push(ev)
        game.push_player_updated(uid, champ_id=getattr(handler, "_player_champ_scid" if target == "me" else "_ai_champ_scid", None))
        _send_game_events(handler, game, session, pl_t)
        return f"Charges set to {val} for {target}"
    elif action == "spellpoints":
        target = args[0].lower() if args else ""
        if target in ("me", "opp"):
            val = int(args[1]) if len(args) > 1 else 0
        else:
            target = "me"
            val = int(args[0]) if args else 0
        uid = pl_t if target == "me" else ai_t
        import battle_engine as _be
        bstate = _be.load_state(session)
        bstate["player_spell_points" if target == "me" else "ai_spell_points"] = val
        _be.save_state(session, bstate)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.player_spell_points = bstate.get("player_spell_points", 0)
        ev = game_engine.ChampionSpellPointsChangedSessionEventArgs()
        ev.player_id = uid; ev.operation = 0; ev.delta = val; ev.new_value = val
        game._push(ev)
        game.push_player_updated(uid, champ_id=getattr(handler, "_player_champ_scid" if target == "me" else "_ai_champ_scid", None))
        _send_game_events(handler, game, session, pl_t)
        return f"Spell points set to {val} for {target}"
    elif action == "health":
        target = args[0].lower() if args else ""
        if target in ("me", "opp"):
            val = int(args[1]) if len(args) > 1 else 0
        else:
            target = "me"
            val = int(args[0]) if args else 0
        uid = pl_t if target == "me" else ai_t
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        ev = game_engine.ChampionHealthChangedSessionEventArgs()
        ev.player_id = uid; ev.old_damage_value = val; ev.new_damage_value = val
        game._push(ev)
        _send_game_events(handler, game, session, pl_t)
        return f"Health set to {val} for {target}"
    elif action == "resource":
        target = args[0].lower() if args else ""
        if target in ("me", "opp"):
            avail = int(args[1]) if len(args) > 1 else 0
            maxr = int(args[2]) if len(args) > 2 else avail
        else:
            target = "me"
            avail = int(args[0]) if args else 0
            maxr = int(args[1]) if len(args) > 1 else avail
        uid = pl_t if target == "me" else ai_t
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.push_player_updated(uid, champ_id=getattr(handler, "_player_champ_scid" if target == "me" else "_ai_champ_scid", None))
        ev_c = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
        ev_c.player_id = uid; ev_c.operation = 0; ev_c.delta = avail; ev_c.new_value = avail
        game._push(ev_c)
        ev_t = game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
        ev_t.player_id = uid; ev_t.operation = 0; ev_t.delta = maxr; ev_t.new_value = maxr
        game._push(ev_t)
        _send_game_events(handler, game, session, pl_t)
        return f"Resources: {avail}/{maxr} for {target}"

    elif action == "gencard":
        if not args:
            return "Usage: gencard <name>"
        name = " ".join(args).lower()
        trow = db.execute(
            "SELECT guid, card_type, cost, attack, defense, abilities_json, attributes "
            "FROM card_templates WHERE LOWER(name) LIKE ? LIMIT 1",
            ("%" + name + "%",)).fetchone()
        if not trow:
            return f"No card template matching '{name}'"
        tpl_guid, card_type_str, cost, atk, def_, ab_json, attrs = trow
        # Card UIDs are (instance << 8) | CardType.  Adding one to the raw
        # value can change the UID type (e.g. Card -> Player), which corrupts
        # the client's card cache.  Allocate the next instance with the real
        # Card UID type instead.
        max_instance = db.execute(
            "SELECT COALESCE(MAX(card_uid >> 8), 0) FROM game_cards "
            "WHERE session_id=?", (session.session_id,)).fetchone()[0]
        max_cuid = game_engine.UID.make(1, int(max_instance or 0) + 1).uid64
        db.execute(
            "INSERT INTO game_cards (session_id, user_id, card_uid, template_guid, "
            "card_template_id, location, position, card_type, card_abilities, "
            "card_attributes, owner_user_id, original_template_guid) "
            "VALUES (?, ?, ?, ?, ?, 'hand', 0, ?, ?, ?, ?, ?)",
            (session.session_id, command_owner_id, max_cuid, tpl_guid,
             tpl_guid, card_type_str, ab_json or "[]", attrs or 0,
             command_owner_id, tpl_guid))
        db.commit()
        # Sync per-instance data from the template (card_type, abilities, attributes,
        # original_template_guid) — ensures all columns are valid regardless of
        # which path created the row.
        handler._sync_instance_card_data(session, max_cuid, tpl_guid)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        scid = game_engine.SessionCardId(game_engine.UID(max_cuid))
        ct = game_engine.card_type_from_db(card_type_str)
        # Populate CardDef with full card data (abilities, thresholds, etc.)
        handler._card_full_data(game, scid, tpl_guid)
        game.push_card_drawn(scid, pl_t, 1)
        game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand,
                                ct, attack=atk or 0, defense=def_ or 0, cost=cost or 0,
                                template_id=tpl_guid)
        _send_game_events(handler, game, session, pl_t)
        return f"Generated card: {name} ({tpl_guid})"

    elif action == "help":
        lines = [
            "=== Commands ===",
            "!game_end victory|defeat — end the campaign battle (test win/loss)",
            "!hand — list cards in hand (name [id])",
            "!playable [id|name ...] — set golden outlines (no args = all)",
            "!gencard <name> — generate a copy of a card template to your hand",
            "!addcard <name|id> — draw the next copy of that card from your deck",
            "!threshold [me|opp] C B R S W D — set 6 threshold counts",
            "!resource [me|opp] <current> <maximum> — set resources",
            "!charge [me|opp] <N> — set champion charges",
            "!spellpoints [me|opp] <N> — set champion spell points",
            "!health [me|opp] <N> — set champion health",
            "!pass — advance turn phase",
            "!phase <Name> — jump to phase",
            "!draw N — draw N cards",
            "!zones — list cards by zone",
            "!move <id> <zone> — move card to zone",
            "!state <id> <flags> — set card state (Tapped|Attacking|...)",
            "!attr <id> <flags> — set card attributes (Flight|Speed|...)",
            "!update <id> — resend CardUpdated for a card",
            "!help — this list",
        ]
        for line in lines:
            _send_chat(handler, line, room, username)
        return ""

    elif action == "zones":
        target = args[0].lower() if args else "me"
        # For PvP sessions, game_cards.user_id stores the reckoning id
        # (not the small profile id), so use client_reck_id directly.
        is_tourney = session and (session.session_name or "").startswith("tourney-")
        if is_tourney:
            my_pid = int(handler.client_reck_id)
            if target == "opp":
                rows = db.execute("SELECT DISTINCT user_id FROM game_cards WHERE session_id=? AND user_id!=?",
                                  (session.session_id, my_pid)).fetchall()
                user_id = rows[0][0] if rows else 0
            else:
                user_id = my_pid
        else:
            user_id = handler.user_profile["id"] if target != "opp" else 0
        all_rows = db.execute(
            "SELECT gc.card_uid, gc.location, ct.name, ct.guid FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.user_id=? ORDER BY gc.location, gc.position",
            (session.session_id, user_id)).fetchall()
        by_zone = {}
        for r in all_rows:
            zone_name = r[1] or 'Deck'
            card_link = _chat_card_link(r[2], r[3])
            by_zone.setdefault(zone_name, []).append(f"{card_link} [{r[0]}]")
        for z, cards in by_zone.items():
            if cards:
                _send_chat(handler, f"{target} {z} ({len(cards)}): {', '.join(cards)}", room, username)
        return f"{len(by_zone)} zones listed ({target})" if by_zone else f"No cards in session ({target})"

    elif action == "move":
        if len(args) < 2:
            return "Usage: !move <card_id> <zone>"
        card_id = int(args[0])
        zone_name = " ".join(args[1:])
        ZONE_MAP = {'deck': 1, 'hand': 2, 'champions': 4, 'warzone': 8,
                     'discard': 16, 'void': 32, 'playedresources': 64,
                     'castspells': 128, 'underground': 256}
        zone_val = ZONE_MAP.get(zone_name.lower(), 1)
        db.execute("UPDATE game_cards SET location=? WHERE session_id=? AND card_uid=?",
                    (zone_name, session.session_id, card_id))
        db.commit()
        # CardMoved first (animation), then CardUpdated (updates cache to new zone)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        scid = game_engine.SessionCardId(game_engine.UID(card_id))
        game.push_card_moved(scid, pl_t, zone_val, game_engine.ECardLocations.Unknown, 0)
        _send_game_events(handler, game, session, pl_t)
        _push_card_update(handler, db, session, pl_t, card_id, collection_override=zone_val)
        return f"Moved {card_id} to {zone_name} (zone={zone_val})"

    elif action == "update":
        if not args:
            return "Usage: !update <card_id>"
        card_id = int(args[0])
        _push_card_update(handler, db, session, pl_t, card_id)
        return f"Updated card {card_id}"

    elif action == "state":
        if len(args) < 2: return "Usage: !state <card_id> <flags>  (Tapped|Blocking|Attacking|Damaged|Healed|Dead|HasAttacked|HasBlocked|EffectExpired|Activated)"
        card_id = int(args[0])
        state_val = 0
        unknown = []
        for flag in args[1:]:
            for sub_flag in flag.split('|'):
                sub_flag = sub_flag.strip()
                if not sub_flag: continue
                val = getattr(game_engine.ECardStates, sub_flag, None)
                if val is not None:
                    state_val |= val
                else:
                    unknown.append(sub_flag)
        if unknown: return f"Unknown state flags: {', '.join(unknown)}"
        _push_card_update(handler, db, session, pl_t, card_id, state=state_val)
        return f"State set to {state_val} for card {card_id}"
    elif action in ("attr", "attributes"):
        if len(args) < 2: return "Usage: !attr <card_id> <flags>  (Flight|Speed|SkyGuard|Crush|Steadfast|Invincible|SpellShield|Unique|LifeDrain)"
        card_id = int(args[0])
        attr_val = 0
        unknown = []
        for flag in args[1:]:
            for sub_flag in flag.split('|'):
                sub_flag = sub_flag.strip()
                if not sub_flag: continue
                val = getattr(game_engine.ECardAttributes, sub_flag, None)
                if val is not None:
                    attr_val |= val
                else:
                    unknown.append(sub_flag)
        if unknown: return f"Unknown attribute flags: {', '.join(unknown)}"
        _push_card_update(handler, db, session, pl_t, card_id, attributes=attr_val)
        return f"Attributes set to {attr_val} for card {card_id}"
    return "Unknown command. !help for list."
