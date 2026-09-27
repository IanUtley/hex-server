"""Regression tests for the continuous static-ability layer
(abilities/framework/statics.py): dynamic self-bonuses, auras, zone-wide cost
modifiers, threshold-gated keywords, damage/block semantics and the global
flags (Emberspire Witch / Te'talca / Enlightened Seeker)."""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

# Bind this process's test database before any runtime import
# opens ``db``; the live ``hconnect.db`` is never opened.
SRC = fresh_database()

import game_engine

from abilities.framework.statics import (
    can_block,
    effective_deltas,
    effective_stats,
    global_flags,
)


LIGHT = "fb84ad94-e6ed-f04b-353d-eda325e0ae43"   # +2/+2 per card in your hand
SOUL = "d60496f7-9c0c-f6f2-9e1b-ae889a675112"     # troops you control +2/+2
TECH = "259bf351-2bd5-304b-1305-b8ef10e416d2"     # artifacts all zones cost -1
ROCK = "52e69d5a-a8dc-6de6-23bd-55f0db89239e"     # 3+ artifacts: +2/+2
WALL = "6c34dee2-146a-0058-f98c-207116b57542"     # +DEF = crypt troops' DEF
OZAWA = "dd4df22b-0d17-d1fc-ce22-f1a5512bb688"    # +X/+X where X is health
DANDELION = "a404baa7-70a6-5e11-caf2-e3ce454887c8"  # WILD WILD WILD: keywords
EMBER = "ab91b642-b63f-484c-5d49-782c96e06e22"    # champions can't gain health
TE_TALCA = "f3714bac-9f28-a1fa-aed6-485e8478b1de"  # cards/effects double damage
HARVESTER = "223f5d1a-1b5c-6ab5-63ce-e88d348e1a79"  # unblockable except art/blood
AIR_SUP = "555d8419-a849-6cbc-79c6-2f04b417fa09"    # troops w/ Flight +1/+1
OATH = "df329e4c-7c33-4bb4-e1d3-bbffa277fc00"       # same-name troops +2/+2
HIGH_TOMB = "6ac287a1-da4a-0d14-5ff0-de0329393fbb"  # +1/+1 per card in all crypts
ENDBRINGER = "69f6aafa-89e8-687b-bea5-db5ef08d8a25"  # Orcs Rage 2 per champ <=10hp
GORTEZUMA = "b24b07cf-3da5-0014-fa56-8936700c3f52"  # self Invincible if opponent <=10
GORTEZUMA_COND = "180773b3-b10b-5633-f60c-42eb3556fd9d"
ELECTROID = "e0c9f434-86e1-20a8-47c0-2a1c5f27f5ff"
ELECTROID_COND = "c96dccfd-f714-4508-a3a6-f21779918aae"
FOREMAN = "155ecb0a-a471-42d6-b754-04961add4d67"           # champion passive
FOREMAN_PASSIVE = "ccbd7f1a-0531-e6d9-7ca6-6a511ea61f85"   # no maximum hand size

TPL_PLAIN = "11111111-1111-1111-1111-111111111111"
TPL_SOUL = "22222222-2222-2222-2222-222222222222"
TPL_TECH = "33333333-3333-3333-3333-333333333333"
TPL_ROCK = "44444444-4444-4444-4444-444444444444"
TPL_ART = "66666666-6666-6666-6666-666666666666"
TPL_BLOOD = "77777777-7777-7777-7777-777777777777"


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = sqlite3.connect(path)
    src = sqlite3.connect(SRC)
    for ddl in (
        """CREATE TABLE game_cards (
            session_id INTEGER, user_id INTEGER, card_uid INTEGER,
            template_guid TEXT, card_template_id TEXT, location TEXT,
            position INTEGER, card_state INTEGER, card_abilities TEXT,
            card_type TEXT, card_attributes INTEGER, temporary_attributes INTEGER DEFAULT 0, card_attack_mod INTEGER,
            card_defense_mod INTEGER, card_cost_mod INTEGER,
            cost_mod_json TEXT DEFAULT '[]', card_damage INTEGER,
            permanent_buffs TEXT, temporary_buffs TEXT, card_uses TEXT,
            resolved_at INTEGER)""",
        """CREATE TABLE card_templates (
            guid TEXT, name TEXT, card_type TEXT, cost INTEGER, attack INTEGER,
            defense INTEGER, attributes INTEGER, abilities_json TEXT,
            threshold_json TEXT, subtype TEXT)""",
        """CREATE TABLE card_abilities_meta (
            ability_guid TEXT, is_triggered INTEGER, trigger_event_type TEXT,
            game_text TEXT, raw_json TEXT, casting_behavior INTEGER,
            is_manual INTEGER, activation_cost INTEGER, uses_per_game INTEGER,
            uses_per_turn INTEGER, target_template_ids TEXT)""",
        """CREATE TABLE ability_effects (
            ability_guid TEXT, effect_guid TEXT, effect_order INTEGER,
            effect_type TEXT, param TEXT)""",
        """CREATE TABLE ability_effect_conditions (
            condition_id TEXT PRIMARY KEY, name TEXT, condition_json TEXT)""",
        """CREATE TABLE target_templates (
            template_id TEXT PRIMARY KEY, game_text TEXT, is_auto_target INTEGER,
            is_random_target INTEGER, optional INTEGER, explicit INTEGER,
            player_filter TEXT, collection_flags TEXT, min_target_count INTEGER,
            max_target_count INTEGER, filter_json TEXT)""",
        "ALTER TABLE target_templates ADD COLUMN target_kind TEXT DEFAULT ''",
        """CREATE TABLE champion_abilities (
            champion_guid TEXT, champion_name TEXT, ability_guid TEXT,
            ability_name TEXT, charge_cost INTEGER, spell_cost INTEGER,
            threshold_colors TEXT, game_text TEXT, casting_behavior INTEGER,
            thresholds_json TEXT, target_template_ids TEXT)""",
    ):
        db.execute(ddl)

    for row in src.execute(
            "SELECT * FROM champion_abilities WHERE champion_guid=?",
            (FOREMAN,)):
        db.execute(
            "INSERT INTO champion_abilities VALUES (%s)"
            % ",".join("?" * len(row)), tuple(row))

    def copy_ability(ag):
        m = src.execute(
            "SELECT ability_guid, is_triggered, trigger_event_type, game_text, "
            "raw_json, casting_behavior, is_manual, activation_cost, "
            "uses_per_game, uses_per_turn, target_template_ids "
            "FROM card_abilities_meta WHERE ability_guid=?", (ag,)).fetchone()
        db.execute("INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?)", m)
        for e in src.execute(
                "SELECT ability_guid, effect_guid, effect_order, effect_type, "
                "param FROM ability_effects WHERE ability_guid=?", (ag,)):
            db.execute("INSERT INTO ability_effects VALUES (?,?,?,?,?)", e)
        for tid in json.loads(m[10] or "[]") or []:
            for r in src.execute(
                    "SELECT * FROM target_templates WHERE template_id=?", (tid,)):
                db.execute("INSERT OR IGNORE INTO target_templates VALUES "
                           "(?,?,?,?,?,?,?,?,?,?,?,?)", r)

    for ag in (LIGHT, SOUL, TECH, ROCK, WALL, OZAWA, DANDELION, EMBER,
               TE_TALCA, HARVESTER, AIR_SUP, OATH, HIGH_TOMB, ENDBRINGER,
               GORTEZUMA, ELECTROID, FOREMAN_PASSIVE):
        copy_ability(ag)
    for cid in ("1b5793b0", "d4a01cea", "72c15be6", GORTEZUMA_COND,
                ELECTROID_COND):
        for r in src.execute(
                "SELECT condition_id, name, condition_json "
                "FROM ability_effect_conditions WHERE condition_id LIKE ?",
                (cid + "%",)):
            db.execute("INSERT OR IGNORE INTO ability_effect_conditions "
                       "VALUES (?,?,?)", r)

    def tpl(guid, name, ctype, cost, atk, de, attrs, ab, thresh="[]",
            subtype=""):
        db.execute("INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (guid, name, ctype, cost, atk, de, attrs,
                    json.dumps(ab), thresh, subtype))

    tpl(TPL_PLAIN, "Plain Troop", "Troop", 1, 1, 1, 0, [])
    tpl(TPL_SOUL, "Soul Armaments", "Constant", 6, 0, 0, 0, [SOUL])
    tpl(TPL_TECH, "Technical Genius", "Troop", 4, 2, 2, 0, [TECH])
    tpl(TPL_ROCK, "Rocket Ranger", "Troop", 3, 2, 2, 0, [ROCK])
    tpl(TPL_ART, "Gear Artifact", "Artifact", 2, 0, 0, 0, [])
    tpl(TPL_BLOOD, "Blood Reaver", "Troop", 2, 2, 1, 0, [],
        '{"values": [0, 1, 0, 0, 0, 0], "list": [1]}', "Reaver")
    tpl("11111111-1111-1111-1111-111111111112", "Lightning Armada", "Troop",
        5, 3, 3, 0, [LIGHT])
    tpl("11111111-1111-1111-1111-111111111113", "Wall of Corpses", "Troop",
        4, 0, 4, 0, [WALL])
    tpl("11111111-1111-1111-1111-111111111114", "Ozawa, Cosmic Elder",
        "Troop", 8, 1, 1, 0, [OZAWA])
    tpl("11111111-1111-1111-1111-111111111115", "Dandelion Sprite", "Troop",
        3, 1, 1, 0, [DANDELION])
    tpl("11111111-1111-1111-1111-111111111116", "Emberspire Witch", "Troop",
        5, 3, 3, 0, [EMBER])
    tpl("11111111-1111-1111-1111-111111111117", "Te'talca, High Cleric",
        "Troop", 9, 4, 5, 0, [TE_TALCA])
    tpl("11111111-1111-1111-1111-111111111118", "Corrupt Harvester", "Troop",
        5, 3, 3, 0, [HARVESTER])
    tpl("11111111-1111-1111-1111-111111111119", "Air Superiority", "Constant",
        4, 0, 0, 0, [AIR_SUP])
    tpl("11111111-1111-1111-1111-11111111111a", "Oath of Valor", "Constant",
        3, 0, 0, 0, [OATH])
    tpl("11111111-1111-1111-1111-11111111111b", "High Tomb Lord", "Troop",
        6, 3, 3, 0, [HIGH_TOMB])
    tpl("11111111-1111-1111-1111-11111111111c", "Endbringer", "Troop",
        7, 4, 4, 0, [ENDBRINGER])
    tpl("11111111-1111-1111-1111-11111111111d", "Orc Grunt", "Troop",
        2, 2, 2, 0, [], subtype="Orc")
    tpl("11111111-1111-1111-1111-11111111111e", "Sky Falcon", "Troop",
        2, 1, 1, int(game_engine.ECardAttributes.Flight), [])
    tpl("11111111-1111-1111-1111-11111111111f", "Ground Pouncer", "Troop",
        2, 3, 3, int(game_engine.ECardAttributes.Flight), [])
    tpl("88888888-8888-8888-8888-888888888888", "Other Troop", "Troop",
        1, 1, 1, 0, [])
    tpl("11111111-1111-1111-1111-111111111120", "Gortezuma", "Troop",
        2, 2, 2, 0, [GORTEZUMA])
    tpl("11111111-1111-1111-1111-111111111121", "Electroid", "Troop|Artifact",
        1, 3, 4, 0, [ELECTROID], subtype="Robot")
    src.close()
    db.commit()
    return db


def card(db, uid, owner, tpl, loc, ab="[]", state=0, ctype=None):
    if ctype is None:
        ctype = db.execute(
            "SELECT card_type FROM card_templates WHERE guid=?", (tpl,)).fetchone()[0]
    db.execute(
        "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,"
        "card_template_id,location,position,card_state,card_abilities,card_type,"
        "card_attributes,card_attack_mod,card_defense_mod,card_cost_mod,"
        "cost_mod_json,card_damage,permanent_buffs,temporary_buffs,card_uses,"
        "resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, owner, uid, tpl, tpl, loc, 0, state, ab, ctype, 0, 0, 0, 0,
         "[]", 0, "{}", "{}", "{}", 0))
    db.commit()


def run(name, fn):
    db = make_db()
    try:
        fn(db)
        print(f"PASS {name}")
    except AssertionError as e:
        print(f"FAIL {name}: {e}")
    except Exception as e:
        import traceback
        print(f"ERROR {name}: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        db.close()


def test_idle_transaction_watchdog(db):
    """The shared connection autocommits, and an explicit transaction left
    open is rolled back by the watchdog so it cannot pin SQLite's write lock."""
    import time as _time
    import db as dbmod

    conn = dbmod.connect(":memory:")
    try:
        assert not conn.in_transaction
        # A plain write must not leave an implicit transaction open.
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        assert not conn.in_transaction, \
            "write left an implicit transaction open (autocommit off?)"
        # An explicit BEGIN is held until it looks idle, then rolled back.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t VALUES (2)")
        assert conn.in_transaction
        assert not dbmod.rollback_idle_transaction(conn, max_idle_seconds=999)
        with dbmod._idle_tx_guard:
            dbmod._idle_tx_seen[id(conn)] = _time.monotonic() - 1000
        assert dbmod.rollback_idle_transaction(conn, max_idle_seconds=30)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        with dbmod._idle_tx_guard:
            dbmod._idle_tx_seen.pop(id(conn), None)
        conn.close()


def test_lightning_armada(db):
    card(db, 101, 5, "11111111-1111-1111-1111-111111111112", "warzone",
         json.dumps([LIGHT]))
    card(db, 102, 5, TPL_PLAIN, "hand")
    d = effective_deltas(db, 1, {}, 101)
    assert d["atk"] == 2 and d["def"] == 2, d
    card(db, 103, 5, TPL_PLAIN, "hand")
    d = effective_deltas(db, 1, {}, 101)
    assert d["atk"] == 4 and d["def"] == 4, d


def test_soul_armaments_aura(db):
    card(db, 101, 5, TPL_SOUL, "warzone", json.dumps([SOUL]), ctype="Constant")
    card(db, 102, 5, TPL_PLAIN, "warzone")
    d = effective_deltas(db, 1, {}, 102)
    assert d["atk"] == 2 and d["def"] == 2, d
    want = game_engine.ECardAttributes.SpellShield | game_engine.ECardAttributes.Steadfast
    assert d["attrs"] & want == want, bin(d["attrs"])


def test_technical_genius_zone_cost(db):
    card(db, 101, 5, TPL_TECH, "warzone", json.dumps([TECH]))
    card(db, 102, 5, TPL_ART, "hand")
    d = effective_deltas(db, 1, {}, 102)
    assert d["cost_mod"] == -1, d


def test_rocket_ranger_condition(db):
    card(db, 101, 5, TPL_ROCK, "warzone", json.dumps([ROCK]))
    card(db, 102, 5, TPL_ART, "warzone")
    card(db, 103, 5, TPL_ART, "warzone")
    assert effective_deltas(db, 1, {}, 101)["atk"] == 0  # only 2 artifacts
    card(db, 104, 5, TPL_ART, "warzone")
    assert effective_deltas(db, 1, {}, 101)["atk"] == 2  # 3+ artifacts


def test_wall_of_corpses_sum(db):
    card(db, 101, 5, "11111111-1111-1111-1111-111111111113", "warzone",
         json.dumps([WALL]))
    card(db, 102, 5, TPL_PLAIN, "discard")   # def 1
    card(db, 103, 5, TPL_PLAIN, "discard")   # def 1
    d = effective_deltas(db, 1, {}, 101)
    assert d["def"] == 2, d


def test_ozawa_health(db):
    card(db, 101, 5, "11111111-1111-1111-1111-111111111114", "warzone",
         json.dumps([OZAWA]))
    d = effective_deltas(db, 1, {"player_health": 15}, 101)
    assert d["atk"] == 15 and d["def"] == 15, d


def test_dandelion_threshold_keywords(db):
    card(db, 101, 5, "11111111-1111-1111-1111-111111111115", "warzone",
         json.dumps([DANDELION]))
    bstate = {"player_threshold": {game_engine.ECardShards.Wild: 2}}
    assert effective_deltas(db, 1, bstate, 101)["attrs"] == 0
    bstate["player_threshold"] = {game_engine.ECardShards.Wild: 3}
    attrs = effective_deltas(db, 1, bstate, 101)["attrs"]
    want = game_engine.ECardAttributes.Flight | game_engine.ECardAttributes.SpellShield
    assert attrs & want == want, bin(attrs)


def test_ember_cant_gain_health(db):
    card(db, 101, 0, "11111111-1111-1111-1111-111111111116", "warzone",
         json.dumps([EMBER]))
    assert "cant_gain_health" in global_flags(db, 1, {})


def test_champion_passive_lifts_max_hand_size(db):
    """Construct Foreman's champion passive removes the hand limit.

    "Champions have no maximum hand size." is a WhileCardInPlay
    UnlimitedHandSize intattr authored on the champion itself.  A champion has
    no ``game_cards`` row, so the continuous-static scan must read the
    checkpoint's ``champ_guid_map``; without it the AI discarded down to 7
    every end of turn.
    """
    from rules_port.static_rules import hand_size_unlimited
    bstate = {"champ_map": {"5": 257, "0": 513},
              "champ_guid_map": {"5": "00000000-0000-0000-0000-000000000001",
                                 "0": FOREMAN}}
    assert hand_size_unlimited(db, 1, bstate) is True
    assert hand_size_unlimited(
        db, 1, {"champ_map": {"5": 257, "0": 513}}) is False


def test_ember_cant_gain_health_reaches_the_native_heal_path(db):
    """The authored champion constraint must reach RulesPort's heal leaf.

    Emberspire Witch's "Champions can't gain health." is a WhileCardInPlay
    CantGainHealth intattr on an AllChampions target.  Champions are synthetic
    SessionCardIds with no ``game_cards`` row, so the per-card static
    projection never landed that leaf and Dragon Guard Stalwart's "Gain 1
    health" charge power healed while she was in play.
    """
    from rules_port.context import EffectContext
    from rules_port.effects import _heal_hero
    from rules_port.static_rules import global_flags
    from tests.tests_combat import HandlerStub, SessionStub

    heal = {"property": "healhero", "amount": 1, "duration": "Instant"}
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    game.player_health = 12
    bstate = {"player_health": 12, "resolving_owner_id": 5,
              "resolving_source_uid": 101}
    context = EffectContext.from_rules_port(
        game, SessionStub(), db, HandlerStub(db), pl_t, ai_t, bstate,
        "19eb6023-c496-cfdf-abab-9a7e09e7fdae")

    card(db, 101, 5, "11111111-1111-1111-1111-111111111116", "warzone",
         json.dumps([EMBER]))
    assert "cant_gain_health" in global_flags(db, 1, {})
    result = _heal_hero(context, None, heal)
    assert result.startswith("prevented"), result
    assert bstate["player_health"] == 12, result
    assert game.player_health == 12, result

    # The same leaf still heals once the constraint has left play.
    db.execute("DELETE FROM game_cards WHERE card_uid=101")
    db.commit()
    assert "cant_gain_health" not in global_flags(db, 1, {})
    assert _heal_hero(context, None, heal) == "healed player 12->13"
    assert bstate["player_health"] == 13


def test_unblockable_except(db):
    card(db, 101, 5, "11111111-1111-1111-1111-111111111118", "warzone",
         json.dumps([HARVESTER]))
    card(db, 102, 0, TPL_PLAIN, "warzone")       # ordinary troop: can't block
    card(db, 103, 0, TPL_ART, "warzone")         # Artifact card: not a troop
    card(db, 104, 0, TPL_BLOOD, "warzone")       # blood troop: can block
    assert not can_block(db, 1, {}, 101, 102)
    assert not can_block(db, 1, {}, 101, 103)    # CanBlock -> NotATroop
    assert can_block(db, 1, {}, 101, 104)


def test_te_talca_double_damage_flag(db):
    card(db, 101, 5, "11111111-1111-1111-1111-111111111117", "warzone",
         json.dumps([TE_TALCA]))
    from abilities.framework.statics import controller_flags
    assert "double_damage" in controller_flags(db, 1, {}, 5)


def test_air_superiority_flight_aura(db):
    card(db, 101, 5, "11111111-1111-1111-1111-111111111119", "warzone",
         json.dumps([AIR_SUP]), ctype="Constant")
    card(db, 102, 5, "11111111-1111-1111-1111-11111111111e", "warzone")  # Flight
    card(db, 103, 5, TPL_PLAIN, "warzone")                                # no Flight
    d = effective_deltas(db, 1, {}, 102)
    assert d["atk"] == 1 and d["def"] == 1, d
    assert effective_deltas(db, 1, {}, 103)["atk"] == 0


def test_oath_of_valor_stored_name(db):
    card(db, 101, 5, "11111111-1111-1111-1111-11111111111a", "warzone",
         json.dumps([OATH]), ctype="Constant")
    card(db, 102, 5, TPL_PLAIN, "warzone")
    card(db, 103, 5, TPL_PLAIN, "warzone")
    # Oath remembers "Plain Troop" (its StoreName trigger recorded it).
    bstate = {"stored_names": {OATH: ["Plain Troop"]}}
    d = effective_deltas(db, 1, bstate, 102)
    assert d["atk"] == 2 and d["def"] == 2, d
    # A different-named card is not buffed (name mismatch).
    card(db, 104, 5, "88888888-8888-8888-8888-888888888888", "warzone")
    assert effective_deltas(db, 1, bstate, 104)["atk"] == 0


def test_high_tomb_lord_both_crypts(db):
    card(db, 101, 5, "11111111-1111-1111-1111-11111111111b", "warzone",
         json.dumps([HIGH_TOMB]))
    card(db, 102, 5, TPL_PLAIN, "discard")   # own crypt
    card(db, 103, 0, TPL_PLAIN, "discard")   # opponent crypt (user 0)
    d = effective_deltas(db, 1, {}, 101)
    assert d["atk"] == 2 and d["def"] == 2, d


def test_endbringer_scaled_rage(db):
    card(db, 101, 5, "11111111-1111-1111-1111-11111111111c", "warzone",
         json.dumps([ENDBRINGER]))
    card(db, 102, 5, "11111111-1111-1111-1111-11111111111d", "warzone")  # Orc
    bstate = {"ai_health": 9, "player_health": 25}
    d = effective_deltas(db, 1, bstate, 102)
    assert d["rage"] == 2, d
    bstate["ai_health"] = 7
    bstate["player_health"] = 8
    d = effective_deltas(db, 1, bstate, 102)
    assert d["rage"] == 4, d
    assert d["attrs"] & game_engine.ECardAttributes.Rage


def test_gortezuma_invincible_uses_opponent_health(db):
    card(db, 101, 5, "11111111-1111-1111-1111-111111111120", "warzone",
         json.dumps([GORTEZUMA]))
    bstate = {"player_health": 20, "ai_health": 20}
    assert not (effective_deltas(db, 1, bstate, 101)["attrs"] &
                game_engine.ECardAttributes.Immortal)
    bstate["ai_health"] = 10
    assert (effective_deltas(db, 1, bstate, 101)["attrs"] &
            game_engine.ECardAttributes.Immortal)
    pvp_state = {"pvp": True, "pids": [5, 9], "hp_5": 20, "hp_9": 20}
    assert not (effective_deltas(db, 1, pvp_state, 101)["attrs"] &
                game_engine.ECardAttributes.Immortal)
    pvp_state["hp_9"] = 10
    assert (effective_deltas(db, 1, pvp_state, 101)["attrs"] &
            game_engine.ECardAttributes.Immortal)


def test_flight_block_legality(db):
    card(db, 101, 5, "11111111-1111-1111-1111-11111111111f", "warzone")  # flyer
    card(db, 102, 0, TPL_PLAIN, "warzone")
    card(db, 103, 0, "11111111-1111-1111-1111-11111111111e", "warzone")  # flyer
    assert not can_block(db, 1, {}, 101, 102)
    assert can_block(db, 1, {}, 101, 103)


def test_electroid_combat_threshold_is_continuous(db):
    """Electroid's authored condition must gate both attack and block.

    The condition is a continuous static modifier, so four qualifying troops
    (including Electroid itself) remove both CantAttack and CantBlock without
    any card-name rule in the combat path.
    """
    card(db, 101, 5, "11111111-1111-1111-1111-111111111121", "warzone",
         json.dumps([ELECTROID]),
         state=int(game_engine.ECardStates.StartedATurnOnYourSide))
    attrs = effective_stats(db, 1, {}, 101)[2]
    assert attrs & game_engine.ECardAttributes.CantAttack
    assert attrs & game_engine.ECardAttributes.CantBlock

    for uid, subtype in ((102, "Dwarf"), (103, "Dwarf"), (104, "Robot")):
        guid = f"11111111-1111-1111-1111-{uid:012x}"
        db.execute(
            "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
            (guid, f"Qualifying {uid}", "Troop", 1, 1, 1, 0,
             "[]", "[]", subtype))
        card(db, uid, 5, guid, "warzone")
    attrs = effective_stats(db, 1, {}, 101)[2]
    assert not (attrs & game_engine.ECardAttributes.CantAttack)
    assert not (attrs & game_engine.ECardAttributes.CantBlock)


def test_damage_threshold_variable(db):
    """'Deal 1 damage to target troop for each [BLOOD] you have.' — the leaf
    amount is 0 and the value comes from the SourcePlayerThresholdAbilityVariable."""
    from abilities.framework.statics import _leaf_numeric_value
    src = sqlite3.connect(SRC)
    raw = src.execute(
        "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
        ("ea663b3b-a052-ce57-d890-ffdfddb88518",)).fetchone()[0]
    src.close()
    pm = {"property": "damage", "amount": 0,
          "text": "Deal 1 damage to target troop for each [BLOOD] you have."}
    bstate = {"player_threshold": {game_engine.ECardShards.Blood: 3}}
    amount = _leaf_numeric_value(db, 1, bstate, pm, raw, 5, 0, "damage")
    assert amount == 3, amount


def test_count_list_attribute_uses_gamedata_list_name(db):
    """CountListAttrAbilityVariable counts its m_ListAttrName payload.

    Construction Plans names the variable separately from the
    ``ExhaustedCards`` list populated by its m_ExhaustTarget payment.
    """
    from abilities.framework.statics import _variable_value
    raw = json.dumps({"m_Variables": [{
        "_t": "Game.Shared.Mechanics.Abilities.CountListAttrAbilityVariable",
        "m_Name": "AForEachTroopExhaustedThisWay",
        "m_ListAttrName": "ExhaustedCards",
        "m_DefaultValue": 0,
    }]})
    value = _variable_value(
        db, 1, {"ability_lists": {"ExhaustedCards": [101, 102]}},
        raw, "AForEachTroopExhaustedThisWay", 5, 101)
    assert value == 2, value


def test_damage_esc_variable(db):
    """'Deal ESC:2 damage' reads the active source card's escalation count."""
    from abilities.framework.statics import _leaf_numeric_value
    src = sqlite3.connect(SRC)
    raw = src.execute(
        "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
        ("36dc9fbf-c870-1796-a9e9-a3f84994d934",)).fetchone()[0]
    src.close()
    pm = {"property": "damage", "amount": 0, "text": "Deal ESC:2 damage"}
    for count, expected in ((1, 2), (2, 4), (3, 6)):
        bstate = {"escalation_counts_by_card": {"820801": count}}
        amount = _leaf_numeric_value(
            db, 1, bstate, pm, raw, 5, 820801, "damage")
        assert amount == expected, (count, amount)


def test_champion_damage(db):
    """'Deal 2 damage to target champion' — champions are not game_cards rows;
    _deal_damage maps the champion SessionCardId to the champion's health."""
    from abilities.framework.bom import _deal_damage

    class H:
        user_profile = {"id": 5}
        _player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        _ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))

        def _card_full_data(self, game, scid, tpl, inst=None):
            return (tpl, "Troop", "Card", 1, 1, 1, 0)

    class S:
        session_id = 1
        server_id = 100

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"player_health": 20, "ai_health": 20}
    _deal_damage(game, S(), db, H(), pl_t, ai_t, bstate,
                 int(H._ai_champ_scid.uid.uid64), 5)
    assert bstate["ai_health"] == 15, bstate
    _deal_damage(game, S(), db, H(), pl_t, ai_t, bstate,
                 int(H._player_champ_scid.uid.uid64), 3)
    assert bstate["player_health"] == 17, bstate


def test_resource_properties(db):
    """Comet Strike: 'Each champion gains 10 [DIAMOND].' — threshold property
    applies to both champions' thresholds."""
    from abilities.framework.bom import _apply_resource_property

    class H:
        user_profile = {"id": 5}
        _player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        _ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))

    class S:
        session_id = 1

        def _persist(self, conn=None):
            """Native resource transitions persist their checkpoint here."""

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"player_resources": 3, "player_total_resources": 3,
              "player_threshold": {}, "ai_threshold": {}}
    pm = {"property": "threshold", "amount": 10,
          "text": "Each champion gains 10 [DIAMOND]."}
    _apply_resource_property(game, S(), db, H(), pl_t, ai_t, bstate, pm, None)
    assert bstate["player_threshold"][game_engine.ECardShards.Diamond] == 10
    assert bstate["ai_threshold"][game_engine.ECardShards.Diamond] == 10
    pm2 = {"property": "totalresource", "amount": 10,
           "text": "Each champion gains [L0][R10]."}
    _apply_resource_property(game, S(), db, H(), pl_t, ai_t, bstate, pm2, None)
    assert bstate["player_total_resources"] == 13
    assert bstate["ai_total_resources"] == 10


def test_temporary_attribute_grant(db):
    """Dimmid's Lifedrain charge power is "this turn" — the grant must land in
    temporary_attributes (cleared at the next Ready step) and still show up in
    effective stats for combat, but not persist in card_attributes."""
    from abilities.framework._shared import apply_attribute_grant
    from abilities.framework.statics import effective_stats

    class H:
        user_profile = {"id": 5}

        def _card_full_data(self, game, scid, tpl, inst=None):
            return (tpl, "Troop", "Card", 1, 1, 1, 0)

    class S:
        session_id = 1
        server_id = 100

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    card(db, 101, 5, TPL_PLAIN, "warzone")
    apply_attribute_grant(game, S(), db, H(), pl_t, ai_t, 101, "<b>Lifedrain</b>",
                          temporary=True)
    row = db.execute(
        "SELECT card_attributes, temporary_attributes FROM game_cards "
        "WHERE card_uid=101").fetchone()
    assert row[0] == 0, row                       # not permanent
    assert row[1] & game_engine.ECardAttributes.SpiritDrain, row
    _atk, _def, attrs, _flags, _rage = effective_stats(db, 1, {}, 101)
    assert attrs & game_engine.ECardAttributes.SpiritDrain


def test_end_of_turn_clear_removes_temp_attrs(db):
    """The EndTurn clear (temporary_attributes = 0) drops 'this turn'
    attribute grants from effective stats — Dimmid's Lifedrain must not
    persist past the turn it was granted."""
    from abilities.framework._shared import apply_attribute_grant
    from abilities.framework.statics import effective_stats

    class H:
        user_profile = {"id": 5}

        def _card_full_data(self, game, scid, tpl, inst=None):
            return (tpl, "Troop", "Card", 1, 1, 1, 0)

    class S:
        session_id = 1

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    card(db, 101, 5, TPL_PLAIN, "warzone")
    apply_attribute_grant(game, S(), db, H(), pl_t, ai_t, 101,
                          "<b>Lifedrain</b>", temporary=True)
    _atk, _def, attrs, _flags, _rage = effective_stats(db, 1, {}, 101)
    assert attrs & game_engine.ECardAttributes.SpiritDrain
    # End-of-turn clear (same SQL the EndTurn handlers run).
    db.execute(
        "UPDATE game_cards SET temporary_attributes = 0 "
        "WHERE session_id=? AND user_id=5 AND location='warzone'", (1,))
    db.commit()
    _atk, _def, attrs, _flags, _rage = effective_stats(db, 1, {}, 101)
    assert not (attrs & game_engine.ECardAttributes.SpiritDrain)


def test_end_of_turn_cleanup_clears_damage_before_buffs(db):
    """Cleanup removes marked combat damage before temporary DEF expires."""
    from abilities.framework._shared import (
        clear_combat_damage, clear_expired_temporary_attributes)
    from abilities.framework.statics import effective_stats

    card(db, 101, 5, TPL_PLAIN, "warzone")
    db.execute(
        "UPDATE game_cards SET card_damage=1, temporary_buffs=? "
        "WHERE card_uid=101", (json.dumps({"def": 1}),))
    db.commit()

    # This is the production cleanup order used by PvE and PvP.
    clear_combat_damage(db, 1)
    clear_expired_temporary_attributes(
        db, 1, 5, "end_turn", clear_stat_buffs=True)
    _atk, defense, _attrs, _flags, _rage = effective_stats(db, 1, {}, 101)
    assert defense == 1, defense
    assert db.execute(
        "SELECT card_damage, temporary_buffs FROM game_cards "
        "WHERE card_uid=101").fetchone() == (0, "{}")


def test_beginning_of_owners_turn_uses_source_controller(db):
    """A native attribute effect expires on its source controller's turn."""
    from rules_port.context import EffectContext
    from rules_port.effects import _card_modifier
    from rules_port.lifecycle import clear_expired_temporary_attributes

    class H:
        user_profile = {"id": 5}

        def _card_full_data(self, game, scid, tpl, inst=None):
            return (tpl, "Troop", "Card", 1, 1, 1, 0)

    class S:
        session_id = 1

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)

    class G:
        def push_card_updated(self, *args, **kwargs):
            pass

    game = G()
    card(db, 101, 0, TPL_PLAIN, "warzone")
    bstate = {"resolving_source_uid": 201, "resolving_owner_id": 5,
              "resolving_target_uid": 101}
    card(db, 201, 5, TPL_PLAIN, "warzone")
    context = EffectContext.from_rules_port(
        game, S(), db, H(), pl_t, ai_t, bstate,
        "6ddc0f5f-9caa-96e8-a48d-f3d6cd212050")
    result = _card_modifier(context, {"param": json.dumps({
        "property": "attribute", "attribute_flags": "Defensive",
        "duration": "BeginningOfOwnersTurn"})})
    assert "attribute grant" in result, result
    defensive = int(game_engine.ECardAttributes.Defensive)
    assert db.execute(
        "SELECT temporary_attributes FROM game_cards WHERE card_uid=101"
    ).fetchone()[0] & defensive
    buffs = json.loads(db.execute(
        "SELECT temporary_buffs FROM game_cards WHERE card_uid=101"
    ).fetchone()[0])
    assert buffs["__attribute_expirations"][str(defensive)] == {
        "owner": 5, "boundary": "start_turn"}
    clear_expired_temporary_attributes(db, 1, 0, "start_turn")
    assert db.execute(
        "SELECT temporary_attributes FROM game_cards WHERE card_uid=101"
    ).fetchone()[0] & defensive
    clear_expired_temporary_attributes(db, 1, 5, "start_turn")
    assert not (db.execute(
        "SELECT temporary_attributes FROM game_cards WHERE card_uid=101"
    ).fetchone()[0] & defensive)


def test_surfaced_speed_expires_at_end_of_turn_not_prep(db):
    """A "for the turn" grant issued at StartTurn must survive that turn's Prep.

    A Tunneling Surface resolves at StartTurn and grants Speed for the turn,
    but the Prep that follows in the same turn used to clear the grant because
    a temporary attribute without an authored expiry expires at the affected
    card's next owner boundary.  The grant now records its EndTurn boundary.
    """
    from pvp_db import db_add_temporary_attributes
    from rules_port.lifecycle import clear_expired_temporary_attributes

    speed = int(game_engine.ECardAttributes.Speed)
    card(db, 101, 5, TPL_PLAIN, "warzone")
    db_add_temporary_attributes(1, 101, speed, conn=db, owner_id=5,
                                boundary="end_turn")
    db.commit()

    def temporary_attributes():
        return int(db.execute(
            "SELECT temporary_attributes FROM game_cards WHERE card_uid=101"
        ).fetchone()[0] or 0)

    assert temporary_attributes() & speed
    clear_expired_temporary_attributes(db, 1, 5, "start_turn",
                                       clear_stat_buffs=True)
    clear_expired_temporary_attributes(db, 1, 5, "prep", clear_stat_buffs=True)
    assert temporary_attributes() & speed
    clear_expired_temporary_attributes(db, 1, 5, "end_turn",
                                       clear_stat_buffs=True)
    assert not (temporary_attributes() & speed)


def test_combat_has_swiftstrike(db):
    """The first-strike damage steps only occur when an attacking or blocking
    troop has Swiftstrike (FirstStrike) or DualStrike — mirrors the client's
    Card.CaresAboutCombatPhase(FirstStrike)."""
    from ai import combat_has_swiftstrike

    class S:
        session_id = 1

    assert not combat_has_swiftstrike(db, S(), {})
    card(db, 101, 5, TPL_PLAIN, "warzone")
    bstate = {"player_attackers": {"101": "0"}}
    assert not combat_has_swiftstrike(db, S(), bstate)
    db.execute(
        "UPDATE card_templates SET attributes=? WHERE guid=?",
        (int(game_engine.ECardAttributes.FirstStrike), TPL_PLAIN))
    db.commit()
    assert combat_has_swiftstrike(db, S(), bstate)
    # A blocker with DualStrike also counts (client treats DualStrike as
    # caring about the FirstStrike phase too).
    db.execute(
        "UPDATE card_templates SET attributes=? WHERE guid=?",
        (0, TPL_PLAIN))
    db.execute(
        "UPDATE card_templates SET attributes=? WHERE guid=?",
        (int(game_engine.ECardAttributes.DualStrike), TPL_ART))
    card(db, 102, 0, TPL_ART, "warzone")
    bstate2 = {"player_attackers": {"101": "0"},
               "ai_blockers": {"101": [102]}}
    assert combat_has_swiftstrike(db, S(), bstate2)


def test_can_block_rejects_cantblock(db):
    """A troop with 'can't attack or block' (CantBlock, e.g. Inner Peace /
    Inner Conflict) must not be able to block — mirrors the client's
    Card.CanBlock() which returns CantBlock for that attribute."""
    from abilities.framework.statics import can_block
    card(db, 101, 5, TPL_PLAIN, "warzone")        # attacker
    card(db, 102, 0, TPL_PLAIN, "warzone")        # blocker
    db.execute(
        "UPDATE card_templates SET attributes=? WHERE guid=?",
        (int(game_engine.ECardAttributes.CantBlock), TPL_PLAIN))
    db.commit()
    assert not can_block(db, 1, {}, 101, 102)
    # A tapped blocker also can't block (client CanBlock: IsTapped -> Exhausted).
    db.execute(
        "UPDATE card_templates SET attributes=? WHERE guid=?", (0, TPL_PLAIN))
    db.execute(
        "UPDATE game_cards SET card_state=? WHERE card_uid=102",
        (int(game_engine.ECardStates.Tapped),))
    db.commit()
    assert not can_block(db, 1, {}, 101, 102)
    db.execute(
        "UPDATE game_cards SET card_state=? WHERE card_uid=102", (0,))
    db.commit()
    assert can_block(db, 1, {}, 101, 102)


def test_client_seed_refresh_repairs_the_typed_modifier_projection(db):
    """An existing database is refreshed to the checked-in Records snapshot.

    ``seed_database`` is insert-if-missing and only runs for a fresh database,
    so a database seeded before the extractor emitted typed modifier fields
    kept a lossy ``ability_effects.param`` row: Minor Ruby of Zeal's
    Swiftstrike leaf read as an empty IntAttr, so a Hired Horn Hunter socketed
    with it never gained Swiftstrike even while its controller held a Ruby
    threshold.  The refresh upserts the seed-owned columns and is
    fingerprint-gated, so a normal start is a no-op.
    """
    # This case needs the full snapshot-seeded database rather than the
    # minimal fixture the harness passes in.
    import os
    import shutil
    import static
    from rules_port.static_rules import effective_attributes

    gem = "45e3a0b2-7001-857c-66d5-256eca98b093"
    hunter = "318ac376-9807-46f1-b8cb-6ddb965f2e53"
    socket = "30560d1c-175a-0611-9a2e-92db365a0513"
    first_strike = int(game_engine.ECardAttributes.FirstStrike)
    ruby = int(game_engine.SHARD_TO_FLAG["ruby"])
    stale_param = ('{"property": "intattr", "attribute": "", "operation": '
                   '"Add", "amount": 0, "duration": "Permanent", '
                   '"text": "<b>Swiftstrike</b>"}')
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy2(SRC, path)
    db = sqlite3.connect(path)
    try:
        # Emulate a database seeded before the snapshot gained typed modifier
        # fields and the snapshot fingerprint.
        db.execute("DROP TABLE IF EXISTS client_seed_meta")
        db.execute(
            "UPDATE ability_effects SET param=?, condition_id='' "
            "WHERE ability_guid=?", (stale_param, gem))
        db.execute(
            "INSERT INTO game_cards (session_id, user_id, card_uid, "
            "template_guid, card_template_id, location, position, card_state, "
            "card_abilities, card_type, card_attributes, card_attack_mod, "
            "card_defense_mod, card_cost_mod, card_damage, permanent_buffs, "
            "temporary_buffs, card_uses, resolved_at, original_template_guid) "
            "VALUES (1,0,7001,?,?,'warzone',0,0,?,'Troop',0,0,0,0,0,'{}',"
            "'{}','{}',0,?)",
            (hunter, hunter, json.dumps([socket, gem]), hunter))
        db.commit()
        armed = {"ai_threshold": {ruby: 1}}
        assert not effective_attributes(db, 1, armed, 7001) & first_strike
        assert static.refresh_client_seed(db) > 0
        assert effective_attributes(db, 1, armed, 7001) & first_strike
        # The authored condition still gates the grant.
        assert not effective_attributes(
            db, 1, {"ai_threshold": {}}, 7001) & first_strike
        # Fingerprint-gated: the repaired snapshot is a no-op on the next start.
        assert static.refresh_client_seed(db) == 0
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    run("Lightning Armada scales with hand size", test_lightning_armada)
    run("Soul Armaments auras troops you control", test_soul_armaments_aura)
    run("Technical Genius reduces artifact cost in all zones", test_technical_genius_zone_cost)
    run("Rocket Ranger gates on 3+ artifacts", test_rocket_ranger_condition)
    run("Wall of Corpses sums crypt defense", test_wall_of_corpses_sum)
    run("Ozawa scales with champion health", test_ozawa_health)
    run("Dandelion Sprite needs WILD x3", test_dandelion_threshold_keywords)
    run("Emberspire Witch blocks health gain", test_ember_cant_gain_health)
    run("Construct Foreman lifts the maximum hand size",
        test_champion_passive_lifts_max_hand_size)
    run("Emberspire Witch blocks the native heal path",
        test_ember_cant_gain_health_reaches_the_native_heal_path)
    run("Harvester only blocked by artifact/blood", test_unblockable_except)
    run("Te'talca grants double damage flag", test_te_talca_double_damage_flag)
    run("Air Superiority buffs only Flyers", test_air_superiority_flight_aura)
    run("Oath of Valor uses stored name", test_oath_of_valor_stored_name)
    run("High Tomb Lord counts both crypts", test_high_tomb_lord_both_crypts)
    run("Idle transaction watchdog rolls back open transaction",
        test_idle_transaction_watchdog)
    run("Endbringer scales Rage with champions", test_endbringer_scaled_rage)
    run("Gortezuma checks opposing champion health",
        test_gortezuma_invincible_uses_opponent_health)
    run("Flight needs a flyer to block", test_flight_block_legality)
    run("Electroid gates combat on Dwarf/Robot control",
        test_electroid_combat_threshold_is_continuous)
    run("Damage scales with Blood threshold", test_damage_threshold_variable)
    run("CountListAttr uses gamedata list name",
        test_count_list_attribute_uses_gamedata_list_name)
    run("Damage scales with ESC", test_damage_esc_variable)
    run("Damage maps champion targets", test_champion_damage)
    run("Resource properties hit both champions", test_resource_properties)
    run("This-turn attribute grants are temporary", test_temporary_attribute_grant)
    run("End-of-turn clear drops this-turn attributes", test_end_of_turn_clear_removes_temp_attrs)
    run("End-of-turn cleanup clears damage before buffs",
        test_end_of_turn_cleanup_clears_damage_before_buffs)
    run("Source-owned durations expire on source turn", test_beginning_of_owners_turn_uses_source_controller)
    run("Surfaced Speed expires at end of turn, not at Prep",
        test_surfaced_speed_expires_at_end_of_turn_not_prep)
    run("Swiftstrike phases need FirstStrike/DualStrike", test_combat_has_swiftstrike)
    run("CantBlock troops cannot block", test_can_block_rejects_cantblock)
    run("Client-seed refresh repairs typed modifier rows",
        test_client_seed_refresh_repairs_the_typed_modifier_projection)
