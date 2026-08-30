"""PvP combat parity: the tournament path must resolve through the SAME shared
ai.resolve_combat as the FRA/AI path, with pid-based ownership, producing the
identical outcome (who lives/dies, lifesteal heals) and objective events."""

import os
import json
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine
import ai
import db as dbmod
import services.tournament_game as tournament_game


TPL_ATT = "11111111-1111-1111-1111-111111111111"
TPL_BLK = "22222222-2222-2222-2222-222222222222"


def make_db(att_owner, blk_owner):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE game_cards (
        session_id INTEGER, user_id INTEGER, card_uid INTEGER,
        template_guid TEXT, card_template_id TEXT, location TEXT,
        position INTEGER, card_state INTEGER, card_abilities TEXT,
        card_type TEXT, card_attributes INTEGER, temporary_attributes INTEGER,
        card_attack_mod INTEGER, card_defense_mod INTEGER, card_cost_mod INTEGER,
        cost_mod_json TEXT DEFAULT '[]', card_damage INTEGER,
        permanent_buffs TEXT DEFAULT '{}', temporary_buffs TEXT DEFAULT '{}',
        card_uses TEXT DEFAULT '{}', resolved_at INTEGER DEFAULT 0,
        original_template_guid TEXT DEFAULT '')""")
    db.execute("""CREATE TABLE card_templates (
        guid TEXT, name TEXT, card_type TEXT, cost INTEGER, attack INTEGER,
        defense INTEGER, attributes INTEGER, abilities_json TEXT,
        threshold_json TEXT, subtype TEXT)""")
    db.execute("""CREATE TABLE card_abilities_meta (
        ability_guid TEXT, is_triggered INTEGER, trigger_event_type TEXT,
        game_text TEXT, raw_json TEXT, casting_behavior INTEGER,
        is_manual INTEGER, activation_cost INTEGER, uses_per_game INTEGER,
        uses_per_turn INTEGER, target_template_ids TEXT,
        exhausts_on_use INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE card_counter_templates (
        template_id TEXT PRIMARY KEY, name TEXT, description TEXT)""")
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TPL_ATT, "Lifestealer", "Troop", 3, 3, 3,
         int(game_engine.ECardAttributes.SpiritDrain), "[]", "[]", ""))
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TPL_BLK, "Wall Troop", "Troop", 3, 3, 3, 0, "[]", "[]", ""))
    for uid, owner, tpl in ((101, att_owner, TPL_ATT),
                            (102, blk_owner, TPL_BLK)):
        db.execute(
            "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,"
            "card_template_id,location,position,card_state,card_abilities,"
            "card_type,card_attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (1, owner, uid, tpl, tpl, "warzone", 0, 0, "[]", "Troop", 0))
    db.commit()
    return db


class SessionStub:
    session_id = 1
    server_id = 100
    turn_order = {}

    def _persist(self):
        pass


class HandlerStub:
    user_profile = {"id": 5}

    def __init__(self, db):
        self._db = db

    def _fresh_game(self, session, pl_t, ai_t, bstate):
        g = game_engine.Game(session.session_id, pl_t, ai_t)
        g.player_health = bstate.get("player_health", 20)
        g.ai_health = bstate.get("ai_health", 10)
        g.player_resources = bstate.get("player_resources", 0)
        g.player_total_resources = bstate.get("player_total_resources", 0)
        g.player_charges = bstate.get("player_charges", 0)
        g.ai_charges = bstate.get("ai_charges", 0)
        g.player_threshold = dict(bstate.get("player_threshold", {}))
        g.ai_threshold = dict(bstate.get("ai_threshold", {}))
        return g

    def _card_full_data(self, game, scid, template_guid, instance_id=None):
        row = self._db.execute(
            "SELECT card_type, name, cost, attack, defense, attributes "
            "FROM card_templates WHERE guid=?",
            (template_guid,)).fetchone()
        if not row:
            return (template_guid, "Troop", "Card", 0, 0, 0, 0)
        ct = game_engine.card_type_from_db(row[0])
        game.card_defs[scid] = game_engine.CardDef(
            row[1], ct, row[2] or 0, row[3] or 0, row[4] or 0, [], [],
            attributes=row[5] or 0)
        return (template_guid, row[0], row[1], row[2] or 0,
                row[3] or 0, row[4] or 0, 0)

    def _resolve_stack_item(self, *a, **k):
        return None


def resolve(owner_a, owner_d, pvp):
    db = make_db(owner_a, owner_d)
    ai._db = db
    if pvp:
        pl_t = game_engine.UID.make(244, owner_a)
        ai_t = game_engine.UID.make(244, owner_d)
        bstate = {
            "pvp": True, "pids": [owner_a, owner_d],
            "champ_map": {str(owner_a): 9001, str(owner_d): 9002},
            "pvp_health_map": {owner_a: "player_health",
                               owner_d: "ai_health"},
            "player_health": 16, "ai_health": 20,
            "player_max_health": 19, "ai_max_health": 20,
            "turn_number": 1,
        }
    else:
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        bstate = {"player_health": 16, "ai_health": 20,
                  "player_max_health": 19, "ai_max_health": 20,
                  "turn_number": 1}
    attackers = {101: 9002 if pvp else 0}
    blockers = {101: [102]}
    handler = HandlerStub(db)
    captured = {}

    def _capture(game, pl_t, ai_t, bstate):
        captured["game"] = game

    ai.resolve_combat(
        handler, SessionStub(), pl_t, ai_t, bstate, attackers, blockers,
        pl_t, ai_t, "pvp_attackers" if pvp else "player_attackers",
        send_events=_capture)
    loc_a = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=101").fetchone()[0]
    loc_b = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=102").fetchone()[0]
    owner_uids = []
    for ev in captured.get("game", type("X", (), {"events": []})).events:
        if ev.__class__.__name__ == "CardUpdatedSessionEventArgs":
            owner_uids.append(int(ev.controller.uid64))
    db.close()
    return loc_a, loc_b, bstate, owner_uids


def test_parity():
    fra = resolve(5, 0, pvp=False)
    pvp = resolve(1001, 1002, pvp=True)
    # Both combatants die in both modes (3/3 lifestealer vs 3/3 blocker).
    assert fra[0] == "discard" and fra[1] == "discard", fra[:2]
    assert pvp[0] == "discard" and pvp[1] == "discard", pvp[:2]
    # Lifesteal heals the ATTACKER's controller 3 in both modes.
    assert fra[2]["player_health"] == 19, fra[2]
    assert fra[2]["ai_health"] == 20, fra[2]
    assert pvp[2]["player_health"] == 19, pvp[2]
    assert pvp[2]["ai_health"] == 20, pvp[2]
    # PvP CardUpdated owner UIDs must encode the cards' real pids (1001/1002),
    # not collapse both sides onto the "player" UID.
    assert pvp[3] and all(u >> 8 in (1001, 1002) for u in pvp[3]), pvp[3]
    assert set(u >> 8 for u in pvp[3]) == {1001, 1002}, pvp[3]
    print("PASS PvP combat parity with FRA")


def test_phase_selection_after_blockers():
    """PVP chooses combat phases after the blocker response window closes."""
    db = make_db(1001, 1002)
    previous_db = tournament_game._db
    tournament_game._db = db

    class Session:
        session_id = 1

    state = {
        "attackers": {"101": "9002"},
        "blockers": {"101": ["102"]},
    }
    try:
        # No live Swiftstrike means the first-strike steps are omitted.
        assert tournament_game.pvp_phase_after_blockers(Session(), state) == \
            game_engine.ETurnPhases.AssignDamage

        # A temporary Quick Action grant on a blocker is seen at this point,
        # so the first-strike steps are retained.
        db.execute(
            "UPDATE game_cards SET temporary_attributes=? WHERE card_uid=102",
            (int(game_engine.ECardAttributes.FirstStrike),))
        db.commit()
        assert tournament_game.pvp_phase_after_blockers(Session(), state) == \
            game_engine.ETurnPhases.AssignFirstStrikeDamage

        state["attackers"] = {}
        assert tournament_game.pvp_phase_after_blockers(Session(), state) == \
            game_engine.ETurnPhases.SecondMainPhase
    finally:
        tournament_game._db = previous_db
        db.close()
    print("PASS PvP combat phase skipping")


def test_pvp_state_view_preserves_escalation_and_charges():
    """Resolver views and HUD events must use persisted per-player values."""
    state = {
        "pids": [1001, 1002],
        "esc_1001": 1,
        "esc_1002": 3,
        "chg_1001": 4,
        "chg_1002": 2,
        "sp_1001": 1,
        "sp_1002": 0,
        "thresh_1001": {"4": 2},
        "thresh_1002": {"8": 1},
    }
    view = tournament_game._pvp_fra_view(state, 1001, 1002)
    assert view["player_escalation_uses"] == 1
    assert view["ai_escalation_uses"] == 3
    assert view["player_charges"] == 4
    assert view["ai_charges"] == 2

    g = game_engine.Game(
        1, game_engine.UID.make(244, 1001), game_engine.UID.make(244, 1002))
    tournament_game._pvp_populate_game_state(g, state, 1001, 1002)
    g.push_player_updated(game_engine.UID.make(244, 1001))
    g.push_player_updated(game_engine.UID.make(244, 1002))
    assert [ev.charges for ev in g.events[-2:]] == [4, 2]
    print("PASS PvP escalation/charge state preservation")


def test_pvp_main_options_offer_resource_until_turn_played():
    """First-main options must outline hand resources before the one-per-turn
    resource play, then remove them after that play is recorded."""
    db = make_db(1001, 1002)
    resource_tpl = "33333333-3333-3333-3333-333333333333"
    artifact_tpl = "44444444-4444-4444-4444-444444444444"
    db.executemany(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(resource_tpl, "Sapphire Shard", "Resource", 0, 0, 0, 0,
          "[]", "[]", ""),
         (artifact_tpl, "Zero Artifact", "Artifact", 0, 0, 0, 0,
          "[]", "[]", "")])
    for uid, template, card_type, pos in (
            (201, resource_tpl, "Resource", 0),
            (202, artifact_tpl, "Artifact", 1)):
        db.execute(
            "INSERT INTO game_cards (session_id,user_id,card_uid,"
            "template_guid,card_template_id,location,position,card_state,"
            "card_abilities,card_type,card_attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (1, 1001, uid, template, template, "hand", pos, 0, "[]",
             card_type, 0))
    db.commit()

    previous = {
        "db": tournament_game._db,
        "dbmod": dbmod._db,
        "pids": tournament_game.db_game_session_pids,
        "handlers": tournament_game.player_handlers,
        "playable": tournament_game._pvp_card_playable,
        "play_targets": tournament_game._pvp_add_play_target_options,
        "troops": tournament_game._pvp_affordable_troop_abilities,
        "champions": tournament_game._pvp_add_champion_options,
        "warzone": tournament_game.pvp_push_warzone_updates,
        "encode": (tournament_game.encode_datawrapper,
                    tournament_game.encode_sync_event,
                    tournament_game.compress_gzip,
                    tournament_game.client_session_guid),
        "packet": game_engine.Game.make_network_packet,
    }

    class Session:
        session_id = 1
        server_id = 100

    class Handler:
        scnt = 0
        sid = "test"

        def send(self, *_args, **_kwargs):
            pass

    captured = {}
    try:
        tournament_game._db = db
        dbmod._db = db
        tournament_game.db_game_session_pids = lambda _sid: [1001, 1002]
        tournament_game.player_handlers = {1001: Handler()}
        tournament_game._pvp_card_playable = lambda *args: True
        tournament_game._pvp_add_play_target_options = lambda *args: None
        tournament_game._pvp_affordable_troop_abilities = lambda *args, **kwargs: {}
        tournament_game._pvp_add_champion_options = lambda *args: None
        tournament_game.pvp_push_warzone_updates = lambda *args, **kwargs: None
        tournament_game.encode_datawrapper = lambda *_args: b""
        tournament_game.encode_sync_event = lambda value: value
        tournament_game.compress_gzip = lambda value: value
        tournament_game.client_session_guid = lambda _handler: ""

        def capture_packet(game, _player):
            captured["events"] = list(game.events)
            return b""

        game_engine.Game.make_network_packet = capture_packet
        state = {"pvp": True, "pids": [1001, 1002], "turn_pid": 1001,
                 "res_played_1001": 0, "champ_map": {}}
        tournament_game.pvp_push_main_phase_options(Session(), state)
        options = next(ev for ev in captured["events"]
                       if isinstance(ev, game_engine.PlayerOptionListSessionEventArgs))
        offered = {int(opt.card.uid.uid64) for opt in options.options}
        assert {201, 202} <= offered, offered

        state["res_played_1001"] = 1
        tournament_game.pvp_push_main_phase_options(Session(), state)
        options = next(ev for ev in captured["events"]
                       if isinstance(ev, game_engine.PlayerOptionListSessionEventArgs))
        offered = {int(opt.card.uid.uid64) for opt in options.options}
        assert 201 not in offered and 202 in offered, offered
    finally:
        tournament_game._db = previous["db"]
        dbmod._db = previous["dbmod"]
        tournament_game.db_game_session_pids = previous["pids"]
        tournament_game.player_handlers = previous["handlers"]
        tournament_game._pvp_card_playable = previous["playable"]
        tournament_game._pvp_add_play_target_options = previous["play_targets"]
        tournament_game._pvp_affordable_troop_abilities = previous["troops"]
        tournament_game._pvp_add_champion_options = previous["champions"]
        tournament_game.pvp_push_warzone_updates = previous["warzone"]
        (tournament_game.encode_datawrapper,
         tournament_game.encode_sync_event,
         tournament_game.compress_gzip,
         tournament_game.client_session_guid) = previous["encode"]
        game_engine.Game.make_network_packet = previous["packet"]
        db.close()
    print("PASS PvP resource option visibility")


def test_pvp_activation_summoning_sickness_only_applies_to_troops():
    """The client gates exhaust-on-use abilities by HasSummoningSickness.

    Card.HasSummoningSickness() is IsTroop() && ... in the client, so a
    non-creature artifact such as Hex Engine can activate on the turn it is
    played while a troop with the same exhaust-on-use ability cannot.
    """
    db = make_db(1001, 1002)
    ability = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    artifact_tpl = "55555555-5555-5555-5555-555555555555"
    troop_tpl = "66666666-6666-6666-6666-666666666666"
    db.executemany(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(artifact_tpl, "Test Engine", "Artifact", 4, 0, 0, 0,
          json.dumps([ability]), "[]", "Engine"),
         (troop_tpl, "Test Troop", "Troop", 4, 2, 2, 0,
          json.dumps([ability]), "[]", "")])
    db.execute(
        "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ability, 0, None, "[ACT] Gain 2.", "{}", 64, 1, 0, 0, 0,
         "[]", 1))
    for uid, template, card_type in ((301, artifact_tpl, "Artifact"),
                                     (302, troop_tpl, "Troop")):
        db.execute(
            "INSERT INTO game_cards (session_id,user_id,card_uid,"
            "template_guid,card_template_id,location,position,card_state,"
            "card_abilities,card_type,card_attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (1, 1001, uid, template, template, "warzone", uid, 0,
             json.dumps([ability]), card_type, 0))
    db.commit()

    previous_db = tournament_game._db
    previous_pids = tournament_game.db_game_session_pids
    try:
        tournament_game._db = db
        tournament_game.db_game_session_pids = lambda _sid: [1001, 1002]

        class Session:
            session_id = 1

        state = {"pvp": True, "pids": [1001, 1002], "turn_pid": 1001,
                 "phase": game_engine.ETurnPhases.FirstMainPhase,
                 "res_1001": 10, "champ_map": {}}
        affordable = tournament_game._pvp_affordable_troop_abilities(
            Session(), state, pid=1001)
        assert ability in affordable[(301, artifact_tpl)], affordable
        assert (302, troop_tpl) not in affordable, affordable
    finally:
        tournament_game._db = previous_db
        tournament_game.db_game_session_pids = previous_pids
        db.close()
    print("PASS PvP artifact activation ignores troop summoning sickness")


def test_pvp_hand_refresh_pushes_current_dynamic_cost():
    """A board change must refresh the private hand cost shown to the client."""
    db = make_db(1001, 1002)
    ptero_tpl = "55555555-5555-5555-5555-555555555555"
    robot_tpl = "66666666-6666-6666-6666-666666666666"
    db.executemany(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(ptero_tpl, "Pterobot", "Troop", 7, 4, 4, 0, "[]", "[]", "Robot Dinosaur"),
         (robot_tpl, "Worker Bot", "Troop|Artifact", 1, 1, 1, 0, "[]", "[]", "Robot")])
    db.execute(
        "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,"
        "card_template_id,location,position,card_state,card_abilities,"
        "card_type,card_attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, 1001, 301, ptero_tpl, ptero_tpl, "hand", 0, 0, "[]", "Troop", 0))
    db.execute(
        "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,"
        "card_template_id,location,position,card_state,card_abilities,"
        "card_type,card_attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, 1001, 302, robot_tpl, robot_tpl, "warzone", 0, 0, "[]",
         "Troop|Artifact", 0))
    db.commit()

    previous = (tournament_game._db, dbmod._db,
                tournament_game.player_handlers)

    class Session:
        session_id = 1

    class Handler:
        def _card_full_data(self, game, scid, template_guid):
            row = db.execute(
                "SELECT name, card_type, cost, attack, defense "
                "FROM card_templates WHERE guid=?", (template_guid,)
            ).fetchone()
            cost = int(row[2])
            if template_guid == ptero_tpl:
                robot_count = db.execute(
                    "SELECT COUNT(*) FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid=gc.template_guid "
                    "WHERE gc.session_id=1 AND gc.user_id=1001 "
                    "AND gc.location='warzone' AND ct.subtype LIKE '%Robot%'"
                ).fetchone()[0]
                cost -= robot_count
            ct = game_engine.card_type_from_db(row[1])
            game.card_defs[scid] = game_engine.CardDef(
                row[0], ct, cost, row[3], row[4], [], [])
            return template_guid, ct, row[0], cost, row[3], row[4], 0

    try:
        tournament_game._db = db
        dbmod._db = db
        tournament_game.player_handlers = {1001: Handler()}
        g = game_engine.Game(1, game_engine.UID.make(244, 1001),
                             game_engine.UID.make(244, 1002))
        tournament_game._pvp_add_hand_card_updates(
            g, Session(), {"pvp": True, "pids": [1001, 1002]},
            1001, game_engine.UID.make(244, 1001))
        updates = [ev for ev in g.events
                   if isinstance(ev, game_engine.CardUpdatedSessionEventArgs)]
        ptero = next(ev for ev in updates if int(ev.session_card_id.uid.uid64) == 301)
        assert ptero.cost == 6, ptero.cost
    finally:
        tournament_game._db, dbmod._db, tournament_game.player_handlers = previous
        db.close()
    print("PASS PvP dynamic hand cost refresh")


def test_mulligan_priority_is_sent_to_both_clients():
    """A mulligan handoff must update priority state on both clients."""
    class Session:
        session_id = 1
        server_id = 100
        turn_order = {}

        def _persist(self):
            pass

    session = Session()
    previous_pids = tournament_game.db_game_session_pids
    previous_handlers = tournament_game.player_handlers
    previous_populate = tournament_game._pvp_populate_game_state
    previous_send = tournament_game._send_pvp_packet
    packets = []
    try:
        tournament_game.db_game_session_pids = lambda _sid: [1001, 1002]
        tournament_game.player_handlers = {1001: object(), 1002: object()}
        tournament_game._pvp_populate_game_state = lambda *args: None
        tournament_game._send_pvp_packet = lambda h, s, g, uid, label: \
            packets.append(g)
        state = {"pvp": True, "pids": [1001, 1002], "champ_map": {}}
        tournament_game._pvp_push_mulligan_prompt(session, state, 1002)
    finally:
        tournament_game.db_game_session_pids = previous_pids
        tournament_game.player_handlers = previous_handlers
        tournament_game._pvp_populate_game_state = previous_populate
        tournament_game._send_pvp_packet = previous_send

    assert len(packets) == 2
    for game in packets:
        greenlights = [
            ev for ev in game.events
            if isinstance(ev, game_engine.GreenLightSessionEventArgs)
        ]
        assert len(greenlights) == 1
        assert int(greenlights[0].player_id.uid64) == ((1002 << 8) | 244)
    assert state["priority_pid"] == 1002
    print("PASS PvP mulligan priority broadcast")


def test_pvp_quick_action_handoff_updates_both_clients():
    """A quick-action response window must set the same priority owner on
    both clients, not leave the passer dependent on the heartbeat."""
    class Session:
        session_id = 1
        server_id = 100
        session_name = "tourney-test"
        turn_order = {
            "pvp": True,
            "pids": [1001, 1002],
            "turn_pid": 1001,
            "phase": game_engine.ETurnPhases.SecondMainPhase,
            "passes": [],
            "champ_map": {},
        }

        def _persist(self):
            pass

    class Handler:
        def __init__(self, pid):
            self.client_reck_id = pid

    session = Session()
    handlers = {1001: Handler(1001), 1002: Handler(1002)}
    packets = []
    previous = (
        tournament_game.db_game_session_pids,
        tournament_game.player_handlers,
        tournament_game._pvp_affordable_troop_abilities,
        tournament_game._pvp_add_troop_ability_options,
        tournament_game._pvp_add_champion_options,
        tournament_game._send_pvp_packet,
    )
    try:
        tournament_game.db_game_session_pids = lambda _sid: [1001, 1002]
        tournament_game.player_handlers = handlers
        tournament_game._pvp_affordable_troop_abilities = (
            lambda _session, _state, pid=None: {"quick": ["ability"]}
            if pid == 1002 else {})
        tournament_game._pvp_add_troop_ability_options = lambda *args: None
        tournament_game._pvp_add_champion_options = lambda *args: None
        tournament_game._send_pvp_packet = (
            lambda h, _session, game, player_uid, label:
            packets.append((h.client_reck_id, label, list(game.events))) or True)
        assert tournament_game.route_pvp_pass(handlers[1001], session)
    finally:
        (tournament_game.db_game_session_pids,
         tournament_game.player_handlers,
         tournament_game._pvp_affordable_troop_abilities,
         tournament_game._pvp_add_troop_ability_options,
         tournament_game._pvp_add_champion_options,
         tournament_game._send_pvp_packet) = previous

    assert [pid for pid, _label, _events in packets] == [1002, 1001], packets
    for pid, _label, events in packets:
        green = next(ev for ev in events
                     if isinstance(ev, game_engine.GreenLightSessionEventArgs))
        assert int(green.player_id.uid64) == ((1002 << 8) | 244), pid
    assert session.turn_order["priority_pid"] == 1002
    print("PASS PvP quick-action priority broadcast")


def test_constant_is_not_offered_as_pvp_attacker():
    """PvP attack options must contain troops only, not Constants."""
    db = make_db(1001, 1002)
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("33333333-3333-3333-3333-333333333333", "Incantation", "Constant",
         2, 0, 0, 0, "[]", "[]", ""))
    db.execute(
        "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,"
        "card_template_id,location,position,card_state,card_abilities,"
        "card_type,card_attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, 1001, 103, "33333333-3333-3333-3333-333333333333",
         "33333333-3333-3333-3333-333333333333", "warzone", 0,
         int(game_engine.ECardStates.StartedATurnOnYourSide), "[]",
         "Constant", 0))
    db.execute(
        "UPDATE game_cards SET card_state=? WHERE card_uid=101",
        (int(game_engine.ECardStates.StartedATurnOnYourSide),))
    db.commit()

    class Session:
        session_id = 1
        server_id = 100

    class Handler:
        scnt = 0
        sid = "test"

        def send(self, *_args, **_kwargs):
            pass

    previous_db = tournament_game._db
    previous_pids = tournament_game.db_game_session_pids
    previous_handlers = tournament_game.player_handlers
    previous_encode = (
        tournament_game.encode_datawrapper,
        tournament_game.encode_sync_event,
        tournament_game.compress_gzip,
        tournament_game.client_session_guid,
    )
    original_packet = game_engine.Game.make_network_packet
    captured = {}
    try:
        tournament_game._db = db
        tournament_game.db_game_session_pids = lambda _sid: [1001, 1002]
        tournament_game.player_handlers = {1001: Handler()}
        tournament_game.encode_datawrapper = lambda *_args: b""
        tournament_game.encode_sync_event = lambda *_args: b""
        tournament_game.compress_gzip = lambda value: value
        tournament_game.client_session_guid = lambda _handler: ""
        def capture_packet(game, _player):
            captured["events"] = list(game.events)
            return b""

        game_engine.Game.make_network_packet = capture_packet
        state = {
            "pvp": True,
            "pids": [1001, 1002],
            "turn_pid": 1001,
            "champ_map": {"1001": 9001, "1002": 9002},
        }
        tournament_game.pvp_push_attack_options(Session(), state)
    finally:
        game_engine.Game.make_network_packet = original_packet
        tournament_game._db = previous_db
        tournament_game.db_game_session_pids = previous_pids
        tournament_game.player_handlers = previous_handlers
        (tournament_game.encode_datawrapper,
         tournament_game.encode_sync_event,
         tournament_game.compress_gzip,
         tournament_game.client_session_guid) = previous_encode
        db.close()

    options = [ev for ev in captured["events"]
               if isinstance(ev, game_engine.PlayerOptionListSessionEventArgs)]
    assert options
    offered = {
        int(opt.card.uid.uid64)
        for opt in options[0].options
    }
    assert offered == {101}, offered
    print("PASS PvP Constants are not attackers")


def test_pvp_steadfast_attacker_stays_untapped():
    """The PvP commit handler must preserve Steadfast on an attacker."""
    db = make_db(1001, 1002)
    uid = 0x101  # Card UID type byte 1, as used by the wire transaction.
    db.execute(
        "UPDATE game_cards SET card_uid=?, card_state=?, card_attributes=? "
        "WHERE card_uid=101",
        (uid, int(game_engine.ECardStates.StartedATurnOnYourSide),
         int(game_engine.ECardAttributes.Steadfast)))
    db.commit()

    class Session:
        session_id = 1
        server_id = 100
        turn_order = {}

        def _persist(self):
            pass

    session = Session()
    session.turn_order = {
        "pvp": True,
        "pids": [1001, 1002],
        "turn_pid": 1001,
        "champ_map": {"1001": 9001, "1002": 9002},
        "attackers": {},
    }
    previous = {
        "db": tournament_game._db,
        "db_helper": dbmod._db,
        "pids": tournament_game.db_game_session_pids,
        "send": tournament_game._pvp_send_same_events,
        "advance": tournament_game.pvp_advance_to_declare_defense,
    }
    try:
        tournament_game._db = db
        dbmod._db = db
        tournament_game.db_game_session_pids = lambda _sid: [1001, 1002]
        tournament_game._pvp_send_same_events = lambda *_args: None
        tournament_game.pvp_advance_to_declare_defense = lambda *_args: True
        wire_uid = uid.to_bytes(8, "little").hex().encode("ascii")
        inner = (b"CommitTroopsToAttackTransaction;m_UID64;0;0;0;" +
                 wire_uid + b";")
        assert tournament_game._pvp_declare_attackers(
            HandlerStub(db), session, inner, 1001)
        state = db.execute(
            "SELECT card_state FROM game_cards WHERE card_uid=?", (uid,)
        ).fetchone()[0]
        assert state & game_engine.ECardStates.Attacking
        assert state & game_engine.ECardStates.HasAttacked
        assert not (state & game_engine.ECardStates.Tapped), state
    finally:
        tournament_game._db = previous["db"]
        dbmod._db = previous["db_helper"]
        tournament_game.db_game_session_pids = previous["pids"]
        tournament_game._pvp_send_same_events = previous["send"]
        tournament_game.pvp_advance_to_declare_defense = previous["advance"]
        db.close()
    print("PASS PvP Steadfast attackers stay untapped")


if __name__ == "__main__":
    test_parity()
    test_constant_is_not_offered_as_pvp_attacker()
    test_phase_selection_after_blockers()
    test_pvp_state_view_preserves_escalation_and_charges()
    test_pvp_main_options_offer_resource_until_turn_played()
    test_pvp_activation_summoning_sickness_only_applies_to_troops()
    test_pvp_hand_refresh_pushes_current_dynamic_cost()
    test_mulligan_priority_is_sent_to_both_clients()
    test_pvp_quick_action_handoff_updates_both_clients()
    test_pvp_steadfast_attacker_stays_untapped()
