"""Regression tests for the latest live-test card issues:

  * Twisted Fate — "When you draw a card, bury the top card of each opposing
    champion's deck."  The client's CardDrawnEvent source is the drawing
    CHAMPION and the target is the drawn card; the trigger conditions test
    them per m_TriggerTest (IsHero on the source, IsType on the target).
  * Shards of Fate — "Choose a Standard resource in your deck. Gain the
    thresholds it provides."  Playing it must NOT behave like a basic shard
    (+1 max/current resource); it chooses a shard from the deck instead.
  * Incubation Slave — its manual ability (auto 'You' target, no picker)
    removes all egg counters, sacrifices the slave, and summons one
    Spiderspawn per counter removed.
  * Bun'jitsu's charge power — the champion ability BOM is re-seeded from
    gamedata; it summons an exhausted Abomination buffed by the voided troop's
    stats (+3).
"""

import json
import os
import sqlite3
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine

from tests.tests_combat import (
    make_db, add_card, HandlerStub, SessionStub, TPL_ENFORCER,
)

SRC = os.environ.get(
    "HEX_TEST_SOURCE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db"),
)


def _copy_card(db, guid):
    """Data-driven fixture helper: copy a card template + its abilities +
    effects + referenced target templates from the live gamedata DB."""
    src = sqlite3.connect(SRC)
    row = src.execute(
        "SELECT guid, name, card_type, cost, attack, defense, attributes, "
        "abilities_json, threshold_json, subtype, variable_cost, "
        "variable_cost_minimum, rage_value "
        "FROM card_templates WHERE guid=?", (guid,)).fetchone()
    if row:
        db.execute(
            "INSERT OR REPLACE INTO card_templates VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        for ag in json.loads(row[7] or "[]"):
            _copy_ability(db, ag)
    src.close()
    db.commit()


def _copy_ability(db, ag):
    src = sqlite3.connect(SRC)
    m = src.execute(
        "SELECT ability_guid, is_triggered, trigger_event_type, game_text, "
        "raw_json, casting_behavior, is_manual, activation_cost, "
        "uses_per_game, uses_per_turn, target_template_ids, exhausts_on_use "
        "FROM card_abilities_meta WHERE ability_guid=?", (ag,)).fetchone()
    if m:
        db.execute(
            "INSERT OR REPLACE INTO card_abilities_meta VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)", m)
        target_ids = set(json.loads(m[10] or "[]"))
        # Card cost targets (for example Bun'jitsu's m_VoidTarget) are not
        # included in m_AbilityTargetTemplateIds.  They are still queried by
        # the production cost-prompt path, so copy their metadata into this
        # focused fixture as well.
        try:
            raw = json.loads(m[4] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
        for field in (
                "m_VoidTarget", "m_SacrificeTarget", "m_ExhaustTarget",
                "m_DiscardTarget", "m_RevealTarget",
                "m_PutIntoDeckTarget", "m_PutIntoDeckTarget2",
                "m_PutIntoHandTarget", "m_ShuffleIntoDeckTarget",
                "m_ExhaustTargets", "m_DiscardTargets"):
            value = raw.get(field)
            values = value if isinstance(value, list) else [value]
            for target in values:
                if isinstance(target, dict):
                    tid = target.get("m_Guid")
                    if tid and tid != "00000000-0000-0000-0000-000000000000":
                        target_ids.add(str(tid).lower())
        for tid in target_ids:
            tt = src.execute(
                "SELECT template_id, game_text, is_auto_target, "
                "is_random_target, optional, explicit, player_filter, "
                "collection_flags, min_target_count, max_target_count, "
                "filter_json, target_kind FROM target_templates "
                "WHERE template_id=?", (tid,)).fetchone()
            if tt:
                db.execute(
                    "INSERT OR REPLACE INTO target_templates VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)", tt)
        for e in src.execute(
                "SELECT ability_guid, effect_guid, effect_order, effect_type, "
                "param, effect_group_id, condition_id, target_index, "
                "effect_instance_id, contingent_effect_instance_id, "
                "secondary_target_index, recalculate_targets, is_optional, "
                "effect_duration, output_variables FROM ability_effects "
                "WHERE ability_guid=? ORDER BY effect_order", (ag,)):
            db.execute(
                "INSERT OR REPLACE INTO ability_effects VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", e)
        for e in src.execute(
                "SELECT param FROM ability_effects WHERE ability_guid=? "
                "AND effect_type='ActivateAbilityEffectTemplate'", (ag,)):
            if e[0]:
                _copy_ability(db, e[0].lower())
        # The gated branches' conditions (ability_effect_conditions) drive the
        # resolution engine's per-effect gating — copy any referenced by this
        # ability's effect rows so e.g. Spawn of Othuyeg's "if there are ten or
        # more cards in opposing crypts" branches evaluate correctly.
        for cid_row in src.execute(
                "SELECT DISTINCT condition_id FROM ability_effects "
                "WHERE ability_guid=? AND condition_id IS NOT NULL "
                "AND condition_id != ''", (ag,)):
            cid = cid_row[0]
            ccond = src.execute(
                "SELECT condition_id, name, condition_json "
                "FROM ability_effect_conditions WHERE condition_id=?",
                (cid,)).fetchone()
            if ccond:
                db.execute(
                    "INSERT OR REPLACE INTO ability_effect_conditions "
                    "VALUES (?,?,?)", ccond)
    src.close()
    db.commit()


def test_twisted_fate_buries_on_player_draw(db):
    """The player draws a card -> their Twisted Fate (warzone) fires and buries
    the top card of the AI's deck.  The trigger source is the drawing champion
    (IsHero) and the target is the drawn card (IsType)."""
    from abilities.framework.triggers import resolve_triggers
    _copy_card(db, "4b6df816-7a67-419c-896b-d30bd959bc02")  # Twisted Fate
    add_card(db, 101, 5, "4b6df816-7a67-419c-896b-d30bd959bc02")  # player TF
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
        (json.dumps(["42bfcf2f-5059-01ed-a341-11aeeee49326"]),))
    add_card(db, 201, 0, "14909185-1070-48df-9508-61d5a9650bd2",
             loc="deck")  # AI deck card (Darkspire Priestess)
    add_card(db, 202, 5, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd",
             loc="deck")  # the drawn card (player's Shamed Gladiator)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    champ_uid = int(handler._player_champ_scid.uid.uid64)
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardDrawnEvent", champ_uid, 5, extra_target=202)
    # The trigger does NOT ignore the chain (m_IgnoresChain=0): resolve the
    # pushed stack item like the server's _resolve_stack_item would.
    from abilities.framework.triggers import resolve_stack_trigger
    for item in list(bstate.get("stack") or []):
        bstate["stack"].remove(item)
        resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                              bstate, item)
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=201").fetchone()[0]
    assert loc == "discard", loc  # AI top deck card buried
    # The buried card must render face-up in the discard (CardUpdated pushed
    # with the Discard collection) — a bare CardMoved leaves the client's
    # crypt empty, which reads as "not burying".
    upd = [e for e in game.events
           if isinstance(e, game_engine.CardUpdatedSessionEventArgs)
           and int(e.session_card_id.uid.uid64) == 201]
    assert upd and upd[0].collection == game_engine.ECardCollections.Discard, upd
    # The drawn card itself is not buried (only the opponent's top card is).
    loc2 = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=202").fetchone()[0]
    assert loc2 == "deck", loc2


def test_malfunctioning_war_bot_hits_a_random_champion_at_turn_start(db):
    """Malfunctioning War Bot's data-defined TurnStarted trigger must deal
    one damage when its controller's turn begins in a two-player game."""
    from abilities.framework.triggers import resolve_triggers

    _copy_card(db, "20b38d5f-ec11-4928-b10e-ee4d77df83bf")
    add_card(db, 301, 1001,
             "20b38d5f-ec11-4928-b10e-ee4d77df83bf", loc="warzone")
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=?",
        (json.dumps(["278c9761-f7e0-f42c-5f8f-065770b6e63e"]), 301))
    db.commit()

    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    handler._champion_targets = lambda: [
        (10001, 1001, "Controller", 20),
        (10002, 1002, "Opponent", 20),
    ]
    bstate = {
        "pvp": True,
        "pids": [1001, 1002],
        "champ_map": {"1001": 10001, "1002": 10002},
        "pvp_health_map": {1001: "player_health", 1002: "ai_health"},
        "player_health": 20,
        "ai_health": 20,
        "turn_number": 1,
        "stack": [],
    }

    log = resolve_triggers(
        db, handler, game, SessionStub(), pl_t, ai_t, bstate,
        "TurnStartedEvent", None, 1001)

    assert "278c9761" in log, log
    assert sorted((bstate["player_health"], bstate["ai_health"])) == [19, 20], bstate
    assert any(isinstance(ev, game_engine.ChampionHealthChangedSessionEventArgs)
               for ev in game.events)


def test_argus_hand_trigger_fires_at_turn_start(db):
    """Argus's hand-based start-of-turn trigger must be discovered in PvP."""
    from abilities.framework.triggers import resolve_triggers, resolve_stack_trigger

    argus = "e9b37ccc-6f20-4f8f-8a93-90a85179f5b3"
    trigger = "2fdfc7c6-2fa0-2eb8-9d71-ac2952359a0b"
    _copy_card(db, argus)
    add_card(db, 302, 1001, argus, loc="hand")
    add_card(db, 303, 1001,
             "8554b2c8-cf48-467d-bf55-ab45e306ce43", loc="deck")
    db.execute(
        "UPDATE game_cards SET card_abilities=?, card_type=? WHERE card_uid=?",
        (json.dumps([trigger]), "Troop|Artifact", 302))
    db.commit()

    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {
        "pvp": True,
        "pids": [1001, 1002],
        "champ_map": {"1001": 10001, "1002": 10002},
        "player_health": 20,
        "ai_health": 20,
        "turn_number": 1,
        "stack": [],
    }

    log = resolve_triggers(
        db, handler, game, SessionStub(), pl_t, ai_t, bstate,
        "TurnStartedEvent", None, 1001)
    assert "2fdfc7c6" in log, log
    # Argus's extracted trigger ignores the chain, so the reveal and modifier
    # resolve immediately rather than creating a stack item.
    assert db.execute(
        "SELECT card_cost_mod FROM game_cards WHERE card_uid=?", (302,)
    ).fetchone()[0] == -1
    assert db.execute(
        "SELECT card_cost_mod FROM game_cards WHERE card_uid=?", (303,)
    ).fetchone()[0] == 0
    assert bstate["revealed_cards"] == [302], bstate
    revealed = [event for event in game.events
                if isinstance(event, game_engine.CardsRevealedSessionEventArgs)]
    assert revealed and [int(card.uid.uid64)
                         for card in revealed[-1].session_card_ids] == [302]
    assert revealed[-1].collections == [game_engine.ECardCollections.Hand]

    # A later turn-start reveal must apply another permanent -1 modifier.
    resolve_triggers(
        db, handler, game, SessionStub(), pl_t, ai_t, bstate,
        "TurnStartedEvent", None, 1001)
    assert db.execute(
        "SELECT card_cost_mod FROM game_cards WHERE card_uid=?", (302,)
    ).fetchone()[0] == -2


def test_charge_bot_deploy_gains_a_charge(db):
    """Charge Bot's data-defined Deploy modifier must update the controller's
    charge pool and emit the client-visible charge event."""
    from abilities.framework.triggers import resolve_triggers, resolve_stack_trigger

    tpl = "7325706e-6bf1-4ca4-8d6b-5da13ac069f4"
    ag = "271c887a-2a70-629e-51a5-dd4a6acd3bcc"
    _copy_card(db, tpl)
    add_card(db, 311, 1001, tpl, loc="warzone")
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=?",
        (json.dumps([ag]), 311))
    db.commit()

    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {
        "pvp": True, "pids": [1001, 1002],
        "champ_map": {"1001": 10001, "1002": 10002},
        "pvp_health_map": {1001: "player_health", 1002: "ai_health"},
        "player_health": 20, "ai_health": 20,
        "player_charges": 0, "ai_charges": 0, "stack": [],
    }
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardEnteredZoneEvent", 311, 1001)
    for item in list(bstate.get("stack") or []):
        bstate["stack"].remove(item)
        resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                              bstate, item)

    assert bstate["player_charges"] == 1, bstate
    assert any(isinstance(ev, game_engine.ChampionChargePointsChangedSessionEventArgs)
               for ev in game.events)


def test_ingenuity_engine_exhaust_cost_is_encoded_as_a_card_picker(db):
    """Ingenuity Engine exposes its m_ExhaustTarget payment cards to PvP's
    option packet, so the client can choose a Robot instead of opening an
    empty X-cost state."""
    import services.tournament_game as tg

    engine_tpl = "82623da8-02c8-4743-ae26-fc72455b2685"
    engine_ag = "0d326c26-c90c-5cf5-500b-48a3ca4cc4b6"
    robot_tpl = "ce57cae9-c573-4098-97a6-8637711aef26"  # Worker Bot
    cost_tid = "8768a77b-d9a4-7df7-ee7a-42cd9ebd17e9"
    _copy_card(db, engine_tpl)
    _copy_card(db, robot_tpl)
    src = sqlite3.connect(SRC)
    cost_row = src.execute(
        "SELECT template_id, game_text, is_auto_target, is_random_target, "
        "optional, explicit, player_filter, collection_flags, "
        "min_target_count, max_target_count, filter_json, target_kind "
        "FROM target_templates WHERE template_id=?", (cost_tid,)).fetchone()
    src.close()
    db.execute("INSERT OR REPLACE INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               cost_row)
    add_card(db, 321, 1001, engine_tpl, loc="warzone")
    add_card(db, 322, 1001, robot_tpl, loc="warzone")
    # Blocking is a combat marker, not Tapped.  A blocker remains a legal
    # exhaust payment for Ingenuity Engine.
    db.execute("UPDATE game_cards SET card_state=? WHERE card_uid=322",
               (game_engine.ECardStates.Blocking,))
    db.commit()

    state = {"pvp": True, "pids": [1001, 1002],
             "champ_map": {"1001": 10001, "1002": 10002},
             "hp_1001": 20, "hp_1002": 20}
    old_db = tg._db
    tg._db = db
    try:
        pl_t = game_engine.UID.make(244, 1001)
        opp_t = game_engine.UID.make(244, 1002)
        game = game_engine.Game(1, pl_t, opp_t)
        game.push_options(pl_t, [game_engine.SessionCardId(
            game_engine.UID(321))])
        tg._pvp_add_troop_ability_options(
            game, SessionStub(), state, pl_t, opp_t, 1001,
            {(321, engine_tpl): [engine_ag]})
        opt = game.events[-1].options[-1]
        inst = opt.instances[0]
        costs = [x for x in inst.target_instances
                 if isinstance(x, game_engine.CostInstanceSessionEventArgs)]
        assert costs and [int(x.uid.uid64) for x in costs[0].targets] == [322], costs
        assert costs[0].cost_type == 1
    finally:
        tg._db = old_db


def test_crazed_squirrel_titan_ai_battles_a_legal_opposing_troop(db):
    """Crazed Titan's [This, opposing troop] target reaches Battle2Cards."""
    from abilities.framework.triggers import resolve_stack_trigger, resolve_triggers

    titan_tpl = "4523c2f4-8aba-4f3b-a974-108a40b3d5fb"
    target_tpl = "7325706e-6bf1-4ca4-8d6b-5da13ac069f4"  # Charge Bot, 1 DEF
    _copy_card(db, titan_tpl)
    _copy_card(db, target_tpl)
    add_card(db, 331, 1002, titan_tpl, loc="warzone")
    add_card(db, 332, 1001, target_tpl, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=331",
               (json.dumps(["f28e7b5b-9ab4-ee29-19ce-a45e2044fe72"]),))
    db.commit()

    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {
        "pvp": True, "pids": [1001, 1002],
        "champ_map": {"1001": 10001, "1002": 10002},
        "pvp_health_map": {1001: "player_health", 1002: "ai_health"},
        "player_health": 20, "ai_health": 20, "stack": [],
    }
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardEnteredZoneEvent", 331, 1002)
    for item in list(bstate.get("stack") or []):
        bstate["stack"].remove(item)
        resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                              bstate, item)
    location = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=332").fetchone()[0]
    assert location == "discard", location


def test_crazed_squirrel_titan_respects_verdant_wyldeboar_buff(db):
    """Battle2Cards must use effective stats, not printed defense/attack."""
    from abilities.framework.triggers import resolve_stack_trigger, resolve_triggers

    titan_tpl = "4523c2f4-8aba-4f3b-a974-108a40b3d5fb"
    boar_tpl = "15bfdb99-7a05-46f8-9da3-d90b213eaa19"
    titan_ag = "f28e7b5b-9ab4-ee29-19ce-a45e2044fe72"
    _copy_card(db, titan_tpl)
    _copy_card(db, boar_tpl)
    add_card(db, 351, 1002, titan_tpl, loc="warzone")
    add_card(db, 352, 1001, boar_tpl, loc="warzone")
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=351",
        (json.dumps([titan_ag]),))
    db.execute(
        "UPDATE game_cards SET permanent_buffs=? WHERE card_uid=352",
        (json.dumps({"atk": 4, "def": 4}),))
    db.commit()

    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {
        "pvp": True, "pids": [1001, 1002],
        "champ_map": {"1001": 10001, "1002": 10002},
        "pvp_health_map": {1001: "player_health", 1002: "ai_health"},
        "player_health": 20, "ai_health": 20, "stack": [],
    }
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardEnteredZoneEvent", 351, 1002)
    for item in list(bstate.get("stack") or []):
        bstate["stack"].remove(item)
        resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                              bstate, item)
    rows = db.execute(
        "SELECT location, card_damage FROM game_cards "
        "WHERE card_uid IN (351, 352) ORDER BY card_uid").fetchall()
    assert rows == [("discard", 0), ("warzone", 4)], rows


def test_oakhenge_moves_revealed_troop_to_hand_with_its_template(db):
    """Oakhenge must hand over the selected troop's card identity."""
    from abilities.framework.resolution import resolve_ability

    oak_tpl = "f42da1e5-159c-41d2-9664-2e64be20257e"
    caterpillar_tpl = "4a8bca1b-db0f-4c14-b3cf-70502fd411ba"
    oak_ag = "200337da-9971-cf13-7e98-011a78b9ac64"
    _copy_card(db, oak_tpl)
    _copy_card(db, caterpillar_tpl)
    add_card(db, 361, 5, oak_tpl, loc="CastSpells")
    add_card(db, 362, 5, caterpillar_tpl, loc="deck")
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"resolving_source_uid": 361, "resolving_owner_id": 5,
              "player_health": 20, "ai_health": 20}

    resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t, bstate,
                    oak_ag, 361, 5, {})

    row = db.execute(
        "SELECT location, template_guid, card_template_id "
        "FROM game_cards WHERE card_uid=362").fetchone()
    assert row == ("hand", caterpillar_tpl, caterpillar_tpl), row
    updated = [ev for ev in game.events
               if isinstance(ev, game_engine.CardUpdatedSessionEventArgs)
               and int(ev.session_card_id.uid.uid64) == 362]
    assert updated and str(updated[-1].card_id.guid) == caterpillar_tpl, updated
    assert updated[-1].nulling is False, updated[-1]
    assert any(isinstance(ev, game_engine.CardDrawnSessionEventArgs)
               and int(ev.session_card_id.uid.uid64) == 362
               for ev in game.events)


def test_spam_bot_charge_power_targets_one_robot_and_one_stat(db):
    """S.P.A.M. Bot's charge power must resolve its two random branches
    against one chosen Robot, not every Robot and not both stats."""
    from abilities.framework.resolution import resolve_ability

    spam_ag = "d9b0ebb0-74ca-b6da-da1b-3523d9fc7da4"
    robot_tpl = "00c0456e-a081-48c8-81a6-e719a26eb6f8"
    robot_target_tid = "cfe1dd4d-b808-60d8-9c0b-d61525ab9677"
    _copy_card(db, robot_tpl)
    _copy_ability(db, spam_ag)
    src = sqlite3.connect(SRC)
    target = src.execute(
        "SELECT template_id, game_text, is_auto_target, is_random_target, "
        "optional, explicit, player_filter, collection_flags, "
        "min_target_count, max_target_count, filter_json, target_kind "
        "FROM target_templates WHERE template_id=?", (robot_target_tid,)
    ).fetchone()
    src.close()
    db.execute("INSERT OR REPLACE INTO target_templates VALUES "
               "(?,?,?,?,?,?,?,?,?,?,?,?)", target)
    db.commit()

    add_card(db, 701, 5, robot_tpl)
    add_card(db, 702, 5, robot_tpl)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    handler = HandlerStub(db)

    for roll, expected in ((1, (1, 0)), (2, (0, 1))):
        db.execute(
            "UPDATE game_cards SET permanent_buffs='{}', temporary_buffs='{}' "
            "WHERE card_uid IN (701, 702)")
        db.commit()
        bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
        game = game_engine.Game(1, pl_t, ai_t)
        with mock.patch("random.randint", return_value=roll):
            resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t,
                            bstate, spam_ag, 900, 5, {1: 701})
        buffs = db.execute(
            "SELECT card_uid, permanent_buffs FROM game_cards "
            "WHERE card_uid IN (701, 702) ORDER BY card_uid").fetchall()
        chosen = json.loads(buffs[0][1] or "{}")
        untouched = json.loads(buffs[1][1] or "{}")
        assert (chosen.get("atk", 0), chosen.get("def", 0)) == expected, buffs
        assert (untouched.get("atk", 0), untouched.get("def", 0)) == (0, 0), buffs


def test_jadiim_triggers_for_a_one_cost_permanent(db):
    """Jadiim's CardCastEvent trigger must also see a one-cost troop.

    Actions already use the spell-resolution CardCastEvent path; permanent
    plays resolve through the troop path and must emit the same event.
    """
    from abilities.framework.triggers import resolve_stack_trigger, resolve_triggers

    jadiim_tpl = "7319c52f-6de0-41e3-bdb8-047099606d63"
    caterpillar_tpl = "4a8bca1b-db0f-4c14-b3cf-70502fd411ba"
    jadiim_ag = "e1a610bb-d643-f7a8-a4c0-5fcf124932de"
    _copy_card(db, jadiim_tpl)
    _copy_card(db, caterpillar_tpl)
    add_card(db, 710, 5, jadiim_tpl, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=?",
               (json.dumps([jadiim_ag]), 710))
    add_card(db, 711, 5, caterpillar_tpl, loc="warzone")
    db.commit()

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1,
              "stack": [], "_next_instance_id": 1}

    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardCastEvent", 711, 5)
    assert len(bstate["stack"]) == 1, bstate
    item = bstate["stack"].pop()
    resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                          bstate, item)
    buffs = json.loads(db.execute(
        "SELECT temporary_buffs FROM game_cards WHERE card_uid=710"
    ).fetchone()[0] or "{}")
    assert buffs.get("atk", 0) == 1 and buffs.get("def", 0) == 1, buffs


def test_verdant_wyldeboar_deck_move_clears_battle_state(db):
    """A Wyldeboar returned to deck is hidden and cannot redraw tapped."""
    from abilities.framework.triggers import resolve_stack_trigger, resolve_triggers

    tpl = "15bfdb99-7a05-46f8-9da3-d90b213eaa19"
    ag = "28f0058e-6b3d-2b05-c718-9f5b649158fa"
    _copy_card(db, tpl)
    add_card(db, 341, 1001, tpl, loc="warzone",
             state=(game_engine.ECardStates.Tapped
                    | game_engine.ECardStates.Attacking
                    | game_engine.ECardStates.StartedATurnOnYourSide))
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=341",
               (json.dumps([ag]),))
    db.commit()
    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"pvp": True, "pids": [1001, 1002], "stack": [],
              "player_health": 20, "ai_health": 20}
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "TurnEndedEvent", None, 1001)
    for item in list(bstate.get("stack") or []):
        bstate["stack"].remove(item)
        resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                              bstate, item)
    row = db.execute(
        "SELECT location, card_state FROM game_cards WHERE card_uid=341").fetchone()
    assert row == ("deck", 0), row
    moved = [e for e in game.events
             if isinstance(e, game_engine.CardMovedSessionEventArgs)
             and int(e.session_card_id.uid.uid64) == 341]
    updated = [e for e in game.events
               if isinstance(e, game_engine.CardUpdatedSessionEventArgs)
               and int(e.session_card_id.uid.uid64) == 341]
    assert moved and moved[-1].location == game_engine.ECardLocations.Unknown
    assert updated and updated[-1].nulling is True and updated[-1].state == 0, [
        (e.nulling, e.state, e.collection) for e in updated]


def test_cocoon_transform_replaces_client_attributes(db):
    """Transforming Cocoon to Butterfly removes the cached Defensive flag."""
    from abilities.framework.transform import transform_card

    cocoon = "da7d22de-d162-4d40-8f19-59112da8054a"
    butterfly = "3d833f8b-5475-425c-98ff-0defca7e4d8d"
    _copy_card(db, cocoon)
    _copy_card(db, butterfly)
    add_card(db, 351, 1001, cocoon, loc="warzone")

    class TransformHandler(HandlerStub):
        def _sync_instance_card_data(self, session, card_uid, template_guid):
            attrs = db.execute(
                "SELECT attributes FROM card_templates WHERE guid=?",
                (template_guid,)).fetchone()[0]
            db.execute(
                "UPDATE game_cards SET card_attributes=? WHERE card_uid=?",
                (attrs, int(card_uid)))
            db.commit()

    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    import db as dbmod
    old_db = dbmod._db
    dbmod._db = db
    try:
        transform_card(TransformHandler(db), game, SessionStub(), pl_t, ai_t,
                       351, butterfly, keep_zone=True,
                       bstate={"pvp": True, "pids": [1001, 1002]})
    finally:
        dbmod._db = old_db
    transformed = [e for e in game.events
                   if isinstance(e, game_engine.CardTransformedSessionEventArgs)]
    assert transformed and str(transformed[-1].card_template_id.guid).lower() == butterfly
    updated = [e for e in game.events
               if isinstance(e, game_engine.CardUpdatedSessionEventArgs)]
    assert updated and updated[-1].attributes == game_engine.ECardAttributes.Flight


def test_twisted_fate_buries_opposing_drawer_deck(db):
    """An opponent's Twisted Fate must NOT trigger when this player draws.

    The printed "you draw" condition is owner-scoped; the opposing player's
    deck must remain untouched.
    """
    from abilities.framework.triggers import resolve_triggers
    from abilities.framework.triggers import resolve_stack_trigger
    _copy_card(db, "4b6df816-7a67-419c-896b-d30bd959bc02")  # Twisted Fate
    add_card(db, 111, 0, "4b6df816-7a67-419c-896b-d30bd959bc02")  # AI TF
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=111",
        (json.dumps(["42bfcf2f-5059-01ed-a341-11aeeee49326"]),))
    add_card(db, 211, 5, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd",
             loc="deck")  # player's deck card: Twisted Fate's target
    add_card(db, 212, 0, "14909185-1070-48df-9508-61d5a9650bd2",
             loc="deck")  # opponent's deck card: must remain
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    champ_uid = int(handler._player_champ_scid.uid.uid64)
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardDrawnEvent", champ_uid, 5, extra_target=211)
    for item in list(bstate.get("stack") or []):
        bstate["stack"].remove(item)
        resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                              bstate, item)
    assert not bstate.get("stack")
    assert db.execute(
        "SELECT location FROM game_cards WHERE card_uid=211").fetchone()[0] == "deck"
    assert db.execute(
        "SELECT location FROM game_cards WHERE card_uid=212").fetchone()[0] == "deck"


def test_twisted_fate_buries_opposing_pvp_deck(db):
    """Both PvP owners are non-zero ids; the owner's draw still selects the
    other player's deck, without collapsing both players into one side."""
    from abilities.framework.triggers import resolve_triggers, resolve_stack_trigger
    _copy_card(db, "4b6df816-7a67-419c-896b-d30bd959bc02")  # Twisted Fate
    add_card(db, 121, 1001, "4b6df816-7a67-419c-896b-d30bd959bc02")
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=121",
        (json.dumps(["42bfcf2f-5059-01ed-a341-11aeeee49326"]),))
    add_card(db, 221, 1001, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd",
             loc="deck")  # TF controller's deck: must remain
    add_card(db, 222, 1002, "14909185-1070-48df-9508-61d5a9650bd2",
             loc="deck")  # opposing drawer's deck: target
    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    handler._champion_targets = lambda: [
        (10001, 1001, "TF Player", 20),
        (10002, 1002, "Drawer", 20),
    ]
    bstate = {
        "pvp": True,
        "pids": [1001, 1002],
        "champ_map": {"1001": 10001, "1002": 10002},
        "player_health": 20, "ai_health": 20, "turn_number": 1,
        "stack": [],
    }
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardDrawnEvent", 10001, 1001, extra_target=221)
    for item in list(bstate.get("stack") or []):
        bstate["stack"].remove(item)
        resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                              bstate, item)
    assert db.execute(
        "SELECT location FROM game_cards WHERE card_uid=222").fetchone()[0] == "discard"


def test_countermagic_modifies_all_same_name_opposing_cards(db):
    """Countermagic's second effect is a permanent, all-zone modifier.

    The selected opposing spell is countered into the graveyard, and every
    opposing copy of that card name in hand, deck, graveyard, and the chain
    receives the +2 cost modifier.  A same-name card controlled by the caster
    is not affected.
    """
    from abilities import resolve_played_spell

    counter_guid = "16c354dd-50a7-45fb-b4e6-309d27cb6575"
    eternal_guid = "ec78ce90-4343-4e2f-a1b9-acadee12c2b0"
    eternal_alt_guid = "faa5833e-a0e0-e740-c3d4-bf53bd5d15fc"
    _copy_card(db, counter_guid)       # Countermagic
    _copy_card(db, eternal_guid)       # Eternal Youth
    _copy_card(db, eternal_alt_guid)   # same name, alternate template

    add_card(db, 301, 5, counter_guid, loc="hand")
    add_card(db, 302, 0, eternal_guid, loc="CastSpells")  # selected target
    add_card(db, 303, 0, eternal_guid, loc="discard")
    add_card(db, 304, 0, eternal_guid, loc="hand")
    add_card(db, 305, 0, eternal_alt_guid, loc="deck")
    add_card(db, 306, 5, eternal_guid, loc="discard")  # caster's copy
    db.execute(
        "UPDATE game_cards SET card_type='BasicAction' "
        "WHERE card_uid IN (301,302,303,304,305,306)")
    db.commit()

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {
        "player_health": 20,
        "ai_health": 20,
        "turn_number": 1,
        "pvp": False,
        "stack": [],
        "player_spell_target": 302,
        "resolving_source_uid": 301,
        "resolving_owner_id": 5,
    }
    resolve_played_spell(
        game, SessionStub(), db, handler, pl_t, ai_t, bstate,
        ["ecd8264c-306a-1d07-f685-0c8b2ef3d3bf"])

    mods = dict(db.execute(
        "SELECT card_uid, card_cost_mod FROM game_cards "
        "WHERE card_uid BETWEEN 302 AND 306").fetchall())
    assert mods[302] == 2, mods
    assert mods[303] == 2, mods
    assert mods[304] == 2, mods
    assert mods[305] == 2, mods
    assert mods[306] == 0, mods
    assert db.execute(
        "SELECT location FROM game_cards WHERE card_uid=302").fetchone()[0] == "discard"


def test_shards_of_fate_detection_and_ai_threshold(db):
    """Shards of Fate is detected data-driven (a target template filtering a
    Standard resource in the deck) and the AI gains the chosen shard's
    threshold.  The +1 max resource grant lives in the resource-play paths
    (ai_play_resource / the player play handler), not in this method."""
    import hconnect_server as hcs
    import db as dbmod
    _copy_card(db, "00e13fdf-b2c3-4fe7-a064-ce4481b24e8d")  # Shards of Fate
    _copy_card(db, "133705cb-cdb1-42fe-b6c5-45f24c43b7cb")  # Blood Shard
    old_db = dbmod._db
    old_hcs_db = hcs._db
    dbmod._db = db
    hcs._db = db
    try:
        h = object.__new__(hcs.HCPHandler)
        ag, tpl = h._shards_of_fate_template(
            ["0240c6cc-57d4-04f3-6e52-00edb986726c"])
        assert tpl == "b5664968-c53c-ccaa-e49e-c7479d155907", tpl
        # AI deck holds a Blood Shard; AI plays Shards of Fate.
        add_card(db, 301, 0, "133705cb-cdb1-42fe-b6c5-45f24c43b7cb",
                 loc="deck")
        db.execute(
            "UPDATE game_cards SET card_type='Resource' WHERE card_uid=301")
        db.commit()
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        game = game_engine.Game(1, pl_t, ai_t)
        bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
        h._ai_champ_scid = game_engine.SessionCardId(game_engine.UID.make(3, 1000))
        h.user_profile = {"id": 5}
        out = h._resolve_shards_of_fate(
            game, SessionStub(), pl_t, ai_t, bstate, 0, ag, tpl, 0)
        assert "blood" in out, out
        assert bstate.get("ai_threshold", {}).get(4, 0) == 1, bstate
        # The chosen shard stays in the deck.
        loc = db.execute(
            "SELECT location FROM game_cards WHERE card_uid=301").fetchone()[0]
        assert loc == "deck", loc
        # No +1 resource was granted.
        assert bstate.get("ai_total_resources", 0) == 0, bstate
    finally:
        dbmod._db = old_db
        hcs._db = old_hcs_db


def test_resource_grant_columns_from_gamedata(db):
    """The resource-grant fields are populated from the gamedata template:
    basic shards grant 1/1 current/max; Shards of Fate grants 0/1 (it
    increases MAX mana only).  The play paths use these instead of a blanket
    +1/+1."""
    import hconnect_server as hcs
    import db as dbmod
    _copy_card(db, "00e13fdf-b2c3-4fe7-a064-ce4481b24e8d")  # Shards of Fate
    _copy_card(db, "8554b2c8-cf48-467d-bf55-ab45e306ce43")  # Sapphire Shard
    old_db = dbmod._db
    old_hcs_db = hcs._db
    dbmod._db = db
    hcs._db = db
    try:
        from db import db_ensure_resource_grants
        db_ensure_resource_grants(db)
        row = db.execute(
            "SELECT current_resources_granted, max_resources_granted "
            "FROM card_templates WHERE guid='00e13fdf-b2c3-4fe7-a064-ce4481b24e8d'"
        ).fetchone()
        assert row == (0, 1), row  # Shards of Fate: max only
        row2 = db.execute(
            "SELECT current_resources_granted, max_resources_granted "
            "FROM card_templates WHERE guid='8554b2c8-cf48-467d-bf55-ab45e306ce43'"
        ).fetchone()
        assert row2 == (1, 1), row2  # basic shard: current + max
    finally:
        dbmod._db = old_db
        hcs._db = old_hcs_db


def test_chlorophyllia_plays_wild_shard_from_deck(db):
    """The nested PlayCard effect selects the gamedata target, then applies
    the same resource/threshold/charge events as a normal Wild Shard play."""
    from abilities import resolve_played_spell

    chlorophyllia = "59396337-eaf2-4be8-a14f-785067c6ca40"
    wild_shard = "cd41bd00-7585-4762-a721-6163bdaee3c3"
    _copy_card(db, chlorophyllia)
    _copy_card(db, wild_shard)
    add_card(db, 701, 5, chlorophyllia, loc="CastSpells")
    add_card(db, 702, 5, wild_shard, loc="deck")
    handler = HandlerStub(db)
    session = SessionStub()
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {
        "player_resources": 0, "player_total_resources": 0,
        "player_charges": 0, "player_threshold": {},
        "ai_resources": 0, "ai_total_resources": 0,
        "ai_charges": 0, "ai_threshold": {},
        "resolving_source_uid": 701, "resolving_owner_id": 5,
    }
    resolve_played_spell(
        game, session, db, handler, pl_t, ai_t, bstate,
        ["e99736cb-7e6f-3237-8b4f-509ad8c6bee6"])
    assert db.execute(
        "SELECT location FROM game_cards WHERE card_uid=702").fetchone()[0] \
        == "PlayedResources"
    assert bstate["player_resources"] == 1
    assert bstate["player_total_resources"] == 1
    assert bstate["player_threshold"].get(32) == 1
    assert bstate["player_charges"] == 1
    assert any(isinstance(e, game_engine.ResourceCardPlayedSessionEventArgs)
               for e in game.events)


def test_chlorophyllia_pvp_view_persists_resource_state(db):
    """PvP's FRA-shaped resolver view must copy the free shard's resource
    changes back to the pid-keyed tournament state."""
    from abilities.framework.bom import _leaf_play_card
    from services.tournament_game import _pvp_sync_view_to_state

    chlorophyllia = "59396337-eaf2-4be8-a14f-785067c6ca40"
    wild_shard = "cd41bd00-7585-4762-a721-6163bdaee3c3"
    _copy_card(db, chlorophyllia)
    _copy_card(db, wild_shard)
    add_card(db, 801, 1001, chlorophyllia, loc="CastSpells")
    add_card(db, 802, 1001, wild_shard, loc="deck")
    handler = HandlerStub(db)
    session = SessionStub()
    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    view = {
        "pvp": True, "pids": [1001, 1002],
        "player_resources": 0, "player_total_resources": 0,
        "player_charges": 0, "player_threshold": {},
        "ai_resources": 0, "ai_total_resources": 0,
        "ai_charges": 0, "ai_threshold": {},
        "resolving_source_uid": 801, "resolving_owner_id": 1001,
    }
    _leaf_play_card(
        game, session, db, handler, pl_t, ai_t, view,
        "29bce00b-d2a3-0503-31ee-33a70e607e18", None)
    state = {
        "res_1001": 0, "res_total_1001": 0, "chg_1001": 0,
        "thresh_1001": {}, "res_1002": 0, "res_total_1002": 0,
        "chg_1002": 0, "thresh_1002": {},
    }
    _pvp_sync_view_to_state(state, view, 1001, 1002)
    assert state["res_1001"] == 1
    assert state["res_total_1001"] == 1
    assert state["chg_1001"] == 1
    assert state["thresh_1001"].get(32) == 1
    assert db.execute(
        "SELECT location FROM game_cards WHERE card_uid=802").fetchone()[0] \
        == "PlayedResources"


def test_shards_randomly_reinsert_into_deck(db):
    """Shards-of-Fate candidates are randomly reinserted while non-shards
    retain their relative deck order."""
    import db as dbmod
    old_db = dbmod._db
    dbmod._db = db
    try:
        for uid in range(500, 510):
            add_card(db, uid, 5, "00e13fdf-b2c3-4fe7-a064-ce4481b24e8d",
                     loc="deck")
        db.executemany(
            "UPDATE game_cards SET position=? WHERE card_uid=?",
            [(uid - 500, uid) for uid in range(500, 510)])
        db.commit()
        candidates = [500, 503, 506, 508]
        with mock.patch("random.shuffle", side_effect=lambda seq: seq.reverse()):
            ordered = dbmod.db_randomly_insert_deck_cards(
                1, 5, candidates)
        rows = db.execute(
            "SELECT card_uid, position FROM game_cards "
            "WHERE session_id=1 AND user_id=5 ORDER BY position").fetchall()
        positions = {uid: pos for uid, pos in rows}
        non_shards = [uid for uid, _pos in rows if uid not in candidates]
        assert non_shards == [501, 502, 504, 505, 507, 509], non_shards
        assert set(positions[uid] for uid in candidates) == {6, 7, 8, 9}, positions
        assert ordered == [508, 506, 503, 500], ordered
    finally:
        dbmod._db = old_db


def test_incubation_slave_egg_summon_and_sacrifice(db):
    """Incubation Slave's manual ability (cost 6, auto 'You' target): remove
    all egg counters, sacrifice the slave, summon one Spiderspawn per egg."""
    from abilities.framework.resolution import resolve_ability
    _copy_card(db, "a77f395f-41b1-45e0-9f8e-7f63286a8797")  # Incubation Slave
    _copy_card(db, "a9ebe40e-ef30-4c9e-b4dd-1b414dc35d0c")  # Spiderspawn
    add_card(db, 401, 5, "a77f395f-41b1-45e0-9f8e-7f63286a8797")
    db.execute(
        "UPDATE game_cards SET permanent_buffs=? WHERE card_uid=401",
        (json.dumps({"counters": {"egg": 3},
                     "counter_guids": {"egg": "b55ea635-a2a9-ba00-4af2-0a097b2566ee"}}),))
    db.commit()
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1,
              "resolving_source_uid": 401, "resolving_owner_id": 5,
              "player_mod_target": 401}
    out = resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t,
                          bstate, "66e7b30e-1bdf-4401-59fc-20cf9788f96f",
                          401, 5, {})
    assert "summon" in out, out
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=401").fetchone()[0]
    assert loc == "discard", loc  # sacrificed
    spiders = db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=1 AND user_id=5 "
        "AND template_guid='a9ebe40e-ef30-4c9e-b4dd-1b414dc35d0c' "
        "AND location='warzone'").fetchone()[0]
    assert spiders == 3, spiders
    buffs = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE card_uid=401"
    ).fetchone()[0]
    assert '"egg":' not in (buffs or "{}"), buffs


def test_bunjitsu_charge_power_summon_and_buff(db):
    """Bun'jitsu's charge power (re-seeded from gamedata): summon an exhausted
    Abomination and buff it with the voided troop's ATK/DEF + 3."""
    from abilities.framework.resolution import resolve_ability
    _copy_ability(db, "32d0d36a-55fd-2cff-0d3d-341319536a57")
    _copy_card(db, "c776499e-53c1-4526-9be4-acba62050d06")  # Abomination
    _copy_card(db, "2b575216-e5a9-421b-988c-badf120d7443")  # Bun'jitsu troop
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1,
              "resolving_source_uid": 900, "resolving_owner_id": 5,
              "champion_voided_stats": {"atk": 2, "def": 2}}
    out = resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t,
                          bstate, "32d0d36a-55fd-2cff-0d3d-341319536a57",
                          900, 5, {})
    assert "abomination" in out.lower(), out
    row = db.execute(
        "SELECT permanent_buffs, card_state, location "
        "FROM game_cards WHERE template_guid='c776499e-53c1-4526-9be4-acba62050d06' "
        "AND session_id=1 ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None, "Abomination not summoned"
    buffs = json.loads(row[0] or "{}")
    assert buffs.get("atk", 0) == 5 and buffs.get("def", 0) == 5, row  # 2 + 3
    assert row[2] == "warzone", row
    assert int(row[1] or 0) & game_engine.ECardStates.Tapped, row  # exhausted


def test_blood_cauldron_ai_pays_sacrifice_cost(db):
    """Blood Cauldron Ritualist's AI charge power must pay its authored
    sacrifice-a-troop cost before targeting a different troop for +4/+4."""
    import ai
    import db as db_module

    ability_guid = "6249cb76-e4ce-45f2-c9fd-5bbe87159112"
    sacrifice_template = "38e37324-0d8f-69ec-60f1-f4695087e5c4"
    src = sqlite3.connect(SRC)
    db.execute("""CREATE TABLE champion_abilities (
        champion_guid TEXT, champion_name TEXT, ability_guid TEXT,
        ability_name TEXT, charge_cost INTEGER, spell_cost INTEGER,
        threshold_colors TEXT, game_text TEXT, casting_behavior INTEGER,
        thresholds_json TEXT, target_template_ids TEXT,
        PRIMARY KEY (champion_guid, ability_guid))""")
    champion_row = src.execute(
        "SELECT * FROM champion_abilities WHERE ability_guid=?",
        (ability_guid,)).fetchone()
    src.close()
    db.execute("INSERT INTO champion_abilities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               champion_row)
    _copy_ability(db, ability_guid)
    _copy_card(db, TPL_ENFORCER)
    _copy_card(db, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd")
    add_card(db, 101, 0, TPL_ENFORCER)
    add_card(db, 102, 0, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd")

    handler = HandlerStub(db)
    handler._ai_champ_ability_guids = [ability_guid]
    handler._champion_thresholds_met = lambda _ag, _state: True
    # HandlerStub does not carry HCPHandler's metadata parser; the production
    # parser supplies this same target template from raw_json.
    handler._ability_cost_templates = lambda _ag: [(sacrifice_template, 2)]
    old_db, old_ai_db = db_module._db, ai._db
    db_module._db = db
    ai._db = db
    try:
        session = SessionStub()
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        game = game_engine.Game(1, pl_t, ai_t)
        bstate = {
            "ai_charges": 3, "ai_health": 12, "player_health": 20,
            "ai_threshold": {4: 1}, "turn_number": 1,
        }
        assert ai.ai_use_champion_ability(
            handler, game, session, ai_t, pl_t, bstate)
        locations = {
            uid: db.execute(
                "SELECT location FROM game_cards WHERE card_uid=?", (uid,)
            ).fetchone()[0]
            for uid in (101, 102)
        }
        assert list(locations.values()).count("discard") == 1, locations
        assert bstate["ai_charges"] == 0, bstate
    finally:
        db_module._db, ai._db = old_db, old_ai_db


def test_discard_positions_append_and_snapshot_order(db):
    """Discard entries get a stable per-player append position used by reconnects."""
    import db as db_module

    add_card(db, 501, 5, TPL_ENFORCER, loc="discard")
    add_card(db, 502, 5, TPL_ENFORCER, loc="discard")
    db.execute("UPDATE game_cards SET position=? WHERE card_uid=?", (2, 501))
    db.execute("UPDATE game_cards SET position=? WHERE card_uid=?", (4, 502))
    add_card(db, 503, 5, TPL_ENFORCER, loc="hand")
    add_card(db, 504, 5, TPL_ENFORCER, loc="hand")
    db.commit()

    db_module.db_discard_card(1, 503, connection=db)
    db_module.db_discard_card(1, 504, connection=db)
    positions = db.execute(
        "SELECT card_uid, position FROM game_cards "
        "WHERE session_id=1 AND user_id=5 AND location='discard' "
        "ORDER BY position, id").fetchall()
    assert positions == [(501, 2), (502, 4), (503, 5), (504, 6)], positions

    old_db = db_module._db
    db_module._db = db
    try:
        snapshot = db_module.db_game_cards_at_location(1, "discard", user_id=5)
    finally:
        db_module._db = old_db
    assert [row[0] for row in snapshot] == [501, 502, 503, 504], snapshot


def main():
    tests = [
        ("Twisted Fate buries on player draw",
         test_twisted_fate_buries_on_player_draw),
        ("Twisted Fate buries opposing drawer deck",
         test_twisted_fate_buries_opposing_drawer_deck),
        ("Twisted Fate buries opposing PvP deck",
         test_twisted_fate_buries_opposing_pvp_deck),
        ("Malfunctioning War Bot turn-start damage",
         test_malfunctioning_war_bot_hits_a_random_champion_at_turn_start),
        ("Argus reveals itself and gets cost reduction",
         test_argus_hand_trigger_fires_at_turn_start),
        ("Charge Bot Deploy gains a charge",
         test_charge_bot_deploy_gains_a_charge),
        ("Ingenuity Engine exhaust cost picker",
         test_ingenuity_engine_exhaust_cost_is_encoded_as_a_card_picker),
        ("Crazed Squirrel Titan target resolution",
         test_crazed_squirrel_titan_ai_battles_a_legal_opposing_troop),
        ("Crazed Squirrel Titan respects Wyldeboar buff",
         test_crazed_squirrel_titan_respects_verdant_wyldeboar_buff),
        ("Oakhenge preserves revealed troop identity",
         test_oakhenge_moves_revealed_troop_to_hand_with_its_template),
        ("S.P.A.M. Bot charge power targets one Robot/stat",
         test_spam_bot_charge_power_targets_one_robot_and_one_stat),
        ("Jadiim triggers for a one-cost permanent",
         test_jadiim_triggers_for_a_one_cost_permanent),
        ("Verdant Wyldeboar deck state reset",
         test_verdant_wyldeboar_deck_move_clears_battle_state),
        ("Cocoon transform attributes",
         test_cocoon_transform_replaces_client_attributes),
        ("Countermagic modifies all same-name opposing cards",
         test_countermagic_modifies_all_same_name_opposing_cards),
        ("Shards of Fate detection + AI threshold",
         test_shards_of_fate_detection_and_ai_threshold),
        ("Incubation Slave egg summon + sacrifice",
         test_incubation_slave_egg_summon_and_sacrifice),
        ("Bun'jitsu charge power summon + buff",
         test_bunjitsu_charge_power_summon_and_buff),
        ("Blood Cauldron AI sacrifice payment",
         test_blood_cauldron_ai_pays_sacrifice_cost),
        ("Discard positions survive reconnect ordering",
         test_discard_positions_append_and_snapshot_order),
        ("Resource grant columns from gamedata",
         test_resource_grant_columns_from_gamedata),
        ("Chlorophyllia plays a Wild Shard",
         test_chlorophyllia_plays_wild_shard_from_deck),
        ("Chlorophyllia PvP state projection",
         test_chlorophyllia_pvp_view_persists_resource_state),
        ("Shards randomly reinsert into deck",
         test_shards_randomly_reinsert_into_deck),
    ]
    failed = 0
    for name, fn in tests:
        db = make_db()
        try:
            fn(db)
            print(f"PASS {name}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            db.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
