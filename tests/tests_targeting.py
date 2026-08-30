"""Regression tests for the ported targeting layer (client Mechanics ->
Python): target-template extraction, card-filter evaluation, and the class-39
triggered-ability target prompt used by Solitary Exile's Deploy."""

import os
import json
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine

from abilities.framework import triggers
from abilities.framework.targeting import legal_targets, evaluate_card_filter

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db")

EXILE_DEPLOY = "952e3555-5ee1-50de-38f6-25cf34037c67"
EXILE_TARGET = "33b1ecf2-a9de-5bae-9918-9203f43b79aa"
CHAMP_TARGET = "716ff96f-6c1b-0385-401f-53ff496c62af"
PRAIRIE_TARGET = "571b110f-60f1-0210-6bb5-243c0bbb5218"
EXILE_LEAVE = "0180723f-d4d2-ba58-ec1e-f70bdc09a624"
EXILE_TPL = "649d8a2a-d4e2-4220-b747-5d45a486fee3"
PLAIN_TPL = "11111111-1111-1111-1111-111111111111"
TOPN_TPL = "22222222-2222-2222-2222-222222222222"
TOPN_TARGET = "33333333-3333-3333-3333-333333333333"
RESOURCE_TPL = "44444444-4444-4444-4444-444444444444"
PET_TPL = "55555555-5555-5555-5555-555555555555"
PET_TARGET = "66666666-6666-6666-6666-666666666666"


class SessionStub:
    session_id = 1
    server_id = 100


class HandlerStub:
    user_profile = {"id": 5}
    prompted = None

    def _prompt_trigger_targets(self, game, pl_t, ai_t, session, bstate,
                                source_uid, ability_guid, target_template_ids,
                                candidates):
        inst_id = int(bstate.get("_next_instance_id", 1))
        bstate["_next_instance_id"] = inst_id + 1
        bstate["pending_trigger"] = {
            "ability_guid": ability_guid,
            "source_uid": int(source_uid),
            "instance_id": inst_id,
            "target_template_id": (target_template_ids or [None])[0],
        }
        self.prompted = (int(source_uid), ability_guid,
                         list(target_template_ids), list(candidates))

    def _card_full_data(self, game, scid, template_guid, instance_id=None):
        return (template_guid, "Troop", "Card", 1, 1, 1, 0)


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = sqlite3.connect(path)
    src = sqlite3.connect(SRC)
    db.execute("""CREATE TABLE game_cards (
        session_id INTEGER, user_id INTEGER, card_uid INTEGER,
        template_guid TEXT, card_template_id TEXT, location TEXT,
        position INTEGER, card_state INTEGER, card_abilities TEXT,
        card_type TEXT, card_attributes INTEGER, temporary_attributes INTEGER DEFAULT 0, card_attack_mod INTEGER,
        card_defense_mod INTEGER, card_damage INTEGER,
        permanent_buffs TEXT, temporary_buffs TEXT, card_uses TEXT)""")
    db.execute("""CREATE TABLE card_abilities_meta (
        ability_guid TEXT, is_triggered INTEGER, trigger_event_type TEXT,
        game_text TEXT, raw_json TEXT, casting_behavior INTEGER,
        is_manual INTEGER, activation_cost INTEGER, uses_per_game INTEGER,
        uses_per_turn INTEGER, target_template_ids TEXT)""")
    # The AI target chooser inspects the ability BOM to distinguish a normal
    # target from a battle target.  Keep this focused fixture faithful to the
    # production schema instead of letting that lookup fail with
    # "no such table: ability_effects".
    db.execute("""CREATE TABLE ability_effects (
        ability_guid TEXT NOT NULL, effect_guid TEXT NOT NULL,
        effect_order INTEGER NOT NULL DEFAULT 0,
        effect_type TEXT DEFAULT '', param TEXT DEFAULT '',
        effect_group_id INTEGER DEFAULT 0, condition_id TEXT DEFAULT '',
        target_index INTEGER DEFAULT -1, effect_instance_id INTEGER DEFAULT -1,
        contingent_effect_instance_id INTEGER DEFAULT -1,
        secondary_target_index INTEGER DEFAULT -1,
        recalculate_targets INTEGER DEFAULT -1,
        is_optional INTEGER DEFAULT 0, effect_duration TEXT DEFAULT 'Instant',
        output_variables TEXT DEFAULT '{}',
        PRIMARY KEY (ability_guid, effect_guid, effect_order))""")
    db.execute("""CREATE TABLE target_templates (
        template_id TEXT PRIMARY KEY, game_text TEXT,
        is_auto_target INTEGER, is_random_target INTEGER, optional INTEGER,
        explicit INTEGER, player_filter TEXT, collection_flags TEXT,
        min_target_count INTEGER, max_target_count INTEGER, filter_json TEXT)""")
    db.execute("ALTER TABLE target_templates ADD COLUMN target_kind TEXT DEFAULT ''")
    db.execute("""CREATE TABLE card_templates (
        guid TEXT, name TEXT, card_type TEXT, cost INTEGER, attack INTEGER,
        defense INTEGER, attributes INTEGER, abilities_json TEXT,
        threshold_json TEXT, subtype TEXT)""")
    for tid in (EXILE_TARGET, PRAIRIE_TARGET, CHAMP_TARGET):
        for row in src.execute(
                "SELECT * FROM target_templates WHERE template_id=?", (tid,)):
            db.execute("INSERT INTO target_templates VALUES "
                       "(?,?,?,?,?,?,?,?,?,?,?,?)", row)
    for ag in (EXILE_DEPLOY, EXILE_LEAVE):
        for row in src.execute(
                "SELECT ability_guid, is_triggered, trigger_event_type, game_text, "
                "raw_json, casting_behavior, is_manual, activation_cost, "
                "uses_per_game, uses_per_turn, target_template_ids "
                "FROM card_abilities_meta WHERE ability_guid=?", (ag,)):
            db.execute(
                "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        for row in src.execute(
                """SELECT ability_guid, effect_guid, effect_order, effect_type,
                        param, effect_group_id, condition_id, target_index,
                        effect_instance_id, contingent_effect_instance_id,
                        secondary_target_index, recalculate_targets,
                        is_optional, effect_duration, output_variables
                   FROM ability_effects WHERE ability_guid=?
                  ORDER BY effect_order""", (ag,)):
            db.execute("INSERT INTO ability_effects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       row)
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        (EXILE_TPL, "Solitary Exile", "Constant", 3, 0, 0, 0,
         f'["{EXILE_DEPLOY}", "{EXILE_LEAVE}"]', "[]", ""))
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        (PLAIN_TPL, "Plain Troop", "Troop", 1, 1, 1, 0, "[]", "[]", ""))
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        (RESOURCE_TPL, "Wild Shard", "Resource", 0, 0, 0, 0, "[]", "[]", ""))
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        (PET_TPL, "#PET_SPIRIT_STAG#", "Troop", 0, 1, 1, 0, "[]", "[]", ""))
    db.execute(
        "INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (PET_TARGET, "<b>#PET_SPIRIT_STAG#</b>", 1, 0, 0, 0,
         "MultiplePlayers", "Warzone", 1, 1,
         json.dumps({"_t": "Game.Shared.Mechanics.Cards.Filters.IsCardName",
                     "m_CardName": "#PET_SPIRIT_STAG#"}),
         "AbilityTargetTemplate"))
    db.execute(
        "INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (TOPN_TARGET, "Top troop", 1, 0, 0, 0, "MultiplePlayers",
         "Deck|Hand|Champions|Warzone|Discard|Void|CastSpells|Underground|Choosing",
         1, 1, json.dumps({
             "_t": "Game.Shared.Mechanics.Cards.Filters.TopNOfDeck",
             "m_Amount": 1,
             "m_Filter": {
                 "_t": "Game.Shared.Mechanics.Cards.Filters.AndCardFilter",
                 "m_TargetFilters": [
                     {"_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"},
                     {"_t": "Game.Shared.Mechanics.Cards.Filters.IsControlledBy",
                      "m_TestAgainstActivePlayer": 0},
                 ],
             },
         }), "AbilityTargetTemplate"))
    src.close()
    db.commit()
    return db


def add_card(db, uid, owner, tpl, location, abilities="[]", position=0):
    ctype = ("Troop" if tpl == PLAIN_TPL else
             "Resource" if tpl == RESOURCE_TPL else "Constant")
    db.execute(
        "INSERT INTO game_cards VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,'{}','{}','{}')",
        (1, owner, uid, tpl, tpl, location, position, 0, abilities, ctype, 0))
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


def test_legal_targets(db):
    """Deploy 'another target card' offers every warzone card except the exile."""
    add_card(db, 100, 5, EXILE_TPL, "warzone", f'["{EXILE_DEPLOY}", "{EXILE_LEAVE}"]')
    add_card(db, 101, 5, PLAIN_TPL, "warzone")
    add_card(db, 200, 0, PLAIN_TPL, "warzone")
    cands = legal_targets(db, 1, 5, EXILE_TARGET, 100, both_players=True)
    assert 100 not in cands, cands
    assert 101 in cands and 200 in cands, cands


def test_filter_eval(db):
    f = {"_t": "Game.Shared.Mechanics.Cards.Filters.NotCardFilter",
         "m_TargetFilter": {"_t": "Game.Shared.Mechanics.Cards.Filters.IsAbilitySource"}}
    assert evaluate_card_filter({"card_uid": 101, "card_type": "Troop"}, f, 100)
    assert not evaluate_card_filter({"card_uid": 100, "card_type": "Troop"}, f, 100)


def test_placeholder_card_name_targets_only_matching_templates(db):
    """Dynamic card-name placeholders identify their token template, not all
    cards in the target collection (Skylak's pet charge-power regression)."""
    add_card(db, 101, 5, PET_TPL, "warzone")
    add_card(db, 102, 0, PET_TPL, "warzone")
    add_card(db, 103, 5, PLAIN_TPL, "warzone")
    add_card(db, 104, 0, PLAIN_TPL, "warzone")
    cands = legal_targets(db, 1, 5, PET_TARGET, 999, both_players=True)
    assert cands == [101, 102], cands


def test_top_n_of_controller_deck(db):
    """TopNOfDeck uses the nested metadata filter and controller's deck only."""
    # Player's first deck card is not a troop; the second is the only legal
    # result. The opponent also has a troop at the top of their deck, which
    # must not be selected by a source-controlled TopN filter.
    add_card(db, 109, 5, RESOURCE_TPL, "deck", position=1)
    add_card(db, 110, 5, PLAIN_TPL, "deck", position=2)
    add_card(db, 111, 5, PLAIN_TPL, "deck", position=3)
    add_card(db, 210, 7, PLAIN_TPL, "deck", position=1)
    cands = legal_targets(db, 1, 5, TOPN_TARGET, 999, both_players=True)
    assert cands == [110], cands

    # The same metadata must work from the AI/PVE side as well. Use a
    # non-zero opposing owner above so the first assertion also covers PVP.
    add_card(db, 220, 0, PLAIN_TPL, "deck", position=1)
    cands = legal_targets(db, 1, 0, TOPN_TARGET, 999, both_players=True)
    assert cands == [220], cands


def test_attacking_filter(db):
    """Prairie Scout's 'target attacking troop' only offers attacking troops."""
    f = {"_t": "Game.Shared.Mechanics.Cards.Filters.AndCardFilter",
         "m_TargetFilters": [{"_t": "Game.Shared.Mechanics.Cards.Filters.IsAttacking"},
                             {"_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"}]}
    from abilities.framework.targeting import evaluate_card_filter
    from game_engine import ECardStates
    assert evaluate_card_filter(
        {"card_type": "Troop", "state": ECardStates.Attacking}, f, 0)
    assert not evaluate_card_filter(
        {"card_type": "Troop", "state": 0}, f, 0)


def test_champion_targets(db):
    """'Target champion' (IsHero) templates offer the champions, who are not
    game_cards rows — they join the pool via the champions parameter."""
    champs = [(900, 5, "Player", 20), (901, 0, "AI", 20)]
    cands = legal_targets(db, 1, 5, CHAMP_TARGET, 900, both_players=True,
                          champions=champs)
    assert 900 in cands and 901 in cands, cands
    # A troop-only filter excludes champions.
    f = {"_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"}
    db.execute("UPDATE target_templates SET filter_json=? WHERE template_id=?",
               (json.dumps(f), CHAMP_TARGET))
    db.commit()
    cands = legal_targets(db, 1, 5, CHAMP_TARGET, 900, both_players=True,
                          champions=champs)
    assert 900 not in cands and 901 not in cands, cands


def test_deploy_prompt_human(db):
    """Human-controlled deploy trigger prompts (class 39), not chain-push."""
    add_card(db, 100, 5, EXILE_TPL, "warzone", f'["{EXILE_DEPLOY}", "{EXILE_LEAVE}"]')
    add_card(db, 101, 5, PLAIN_TPL, "warzone")
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"_next_instance_id": 1, "resolving_owner_id": 5}
    handler = HandlerStub()
    triggers.resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t,
                              bstate, "CardEnteredZoneEvent", 100, 5)
    assert handler.prompted is not None, "expected a target prompt"
    assert handler.prompted[0] == 100
    assert EXILE_TARGET in handler.prompted[2]
    assert 101 in handler.prompted[3] and 100 not in handler.prompted[3]
    assert not (bstate.get("stack") or []), "prompt must not push a chain item"
    assert bstate.get("pending_trigger"), "pending trigger must be stored"


def test_deploy_auto_ai(db):
    """AI-controlled deploy auto-picks the first legal target and chains it."""
    add_card(db, 300, 0, EXILE_TPL, "warzone", f'["{EXILE_DEPLOY}", "{EXILE_LEAVE}"]')
    add_card(db, 301, 0, PLAIN_TPL, "warzone")
    add_card(db, 302, 5, PLAIN_TPL, "warzone")
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"_next_instance_id": 1, "resolving_owner_id": 0}
    handler = HandlerStub()
    triggers.resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t,
                              bstate, "CardEnteredZoneEvent", 300, 0)
    assert handler.prompted is None
    stack = bstate.get("stack") or []
    assert len(stack) == 1, stack
    assert stack[0]["target_uid"] in (301, 302), stack


def test_class39_wire(db):
    ev = game_engine.TriggeredAbilityActivationDataRequiredSessionEventArgs()
    ev.player_id = game_engine.UID.make(244, 5)
    ev.ability_instance_ids = [7]
    ev.ability_template_ids = [game_engine.ResourceId.from_str(EXILE_DEPLOY)]
    ev.source_card_ids = [game_engine.SessionCardId(game_engine.UID(100))]
    data = ev.to_byte_array()
    assert len(data) > 8
    assert ev.CLASS_ID == 39


if __name__ == "__main__":
    run("legal targets exclude the ability source", test_legal_targets)
    run("card filter Not(IsAbilitySource)", test_filter_eval)
    run("placeholder card names only match their token templates",
        test_placeholder_card_name_targets_only_matching_templates)
    run("TopNOfDeck uses controller and deck order", test_top_n_of_controller_deck)
    run("IsAttacking target filter", test_attacking_filter)
    run("champion targets join the pool", test_champion_targets)
    run("human deploy triggers class-39 prompt", test_deploy_prompt_human)
    run("AI deploy auto-picks + chains", test_deploy_auto_ai)
    run("class-39 event serializes", test_class39_wire)
