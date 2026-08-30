"""Regression tests for the LifeSteal deck (God's deck): every unique card's
ability resolves data-driven against the real seeded ability metadata.

The tests copy the relevant card_abilities_meta / ability_effects / template
rows from hconnect.db into a temp DB, stub the handler, and drive the same
resolver entry points the server uses.

Run:  python3 tests_deck_abilities.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine
import db as dbmod

from abilities.framework import triggers
from abilities.framework.bom import _LEAFS
from abilities import resolve_played_spell

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db")

TPL = {
    "scrivener": "2ce8233b-c5bd-4be0-86c6-a17021b071ee",
    "paladin": "6655d75a-562c-46d2-be7e-26d3d4e2ea1a",
    "incantation": "4113287c-f5d5-495e-a517-90b84e076450",
    "sentinel": "f0e3cf6c-bcb4-4488-9d97-ec37b4f94b61",
    "kraken": "e3a0ca9c-c21c-45d3-a36d-c4d5c03e445d",
    "outrider": "9d5b36f9-6dda-4699-8aea-ad0a908ba2e1",
    "angel": "674679f6-af3d-41d2-9e20-ae68d1816c71",
    "totem": "8b6d5e83-79a0-425e-b105-228e8e92d824",
    "plain_troop": "11111111-1111-1111-1111-111111111111",
    "exile": "649d8a2a-d4e2-4220-b747-5d45a486fee3",
    "eternal_youth": "ec78ce90-4343-4e2f-a1b9-acadee12c2b0",
    "inner_conflict": "27f4a397-1aca-4910-9508-e91871d44284",
    "prairie": "6e54f6f0-e630-40f9-9df3-e567b31605ea",
    "burn": "609a5ce4-24a4-4470-98d1-e64b8a8a4531",
    "gladiator": "33f03766-e38e-4a77-ac1e-bbc78a55ddbb",
}

ABILITIES = [
    "e6e77180-238a-a5db-08da-16f07cb67836",  # Scrivener
    "8a06bd0d-b743-08ae-5397-9ae295df3f18",  # Paladin
    "3cf80f54-bb4b-a285-6bfe-f65bd75f0b76",  # Incantation
    "759e8464-7980-279a-1935-626e00c13f99",  # Kraken inspire
    "93fed6ce-39e4-03d2-4024-f18517c36709",  # Outrider attack
    "0d22faf5-a934-0983-ca9d-9d0a11636891",  # Angel draw
    "7357e5b2-3819-f851-4f40-8b97349f3792",  # Totem +1/+1
    "4cd98e94-c38b-f50a-afb7-c81438c93126",  # Totem Flight
    "9b85495d-fd29-a90e-9ccf-723bf2b85ae6",  # Eternal Youth heal
    "30aba84c-1ee1-83f6-de7e-8f1796cb9975",  # Inner Conflict
    "952e3555-5ee1-50de-38f6-25cf34037c67",  # Exile deploy void
    "0180723f-d4d2-ba58-ec1e-f70bdc09a624",  # Exile leaves return
    "615f9a45-325b-7b84-22c0-7721bfa68ded",  # Prairie Scout target attacking
    "81712882-30ed-c365-1d90-211966640219",  # Burn champion-or-troop
    "b95fdd81-2eca-f2cb-b28b-c5ec70307ca0",  # Shamed Gladiator deploy "you"
]


class SessionStub:
    session_id = 1
    server_id = 100


class HandlerStub:
    user_profile = {"id": 5}

    @staticmethod
    def _next_resolve_counter(session):
        return 1

    def _card_full_data(self, game, scid, template_guid, instance_id=None):
        if template_guid is None:
            return (None, "Troop", "Card", 0, 0, 0, 0)
        row = self._db.execute(
            "SELECT name, card_type, cost, attack, defense, attributes, abilities_json "
            "FROM card_templates WHERE guid=?", (template_guid,)).fetchone()
        if not row:
            return (template_guid, "Troop", "Card", 0, 0, 0, 0)
        name, ctype, cost, atk, def_, attrs, ab_json = row
        irow = self._db.execute(
            "SELECT card_attack_mod, card_defense_mod, card_attributes, card_abilities "
            "FROM game_cards WHERE session_id=? AND card_uid=?",
            (1, scid.uid.uid64)).fetchone()
        atk_mod = def_mod = inst_attrs = 0
        inst_ab = []
        if irow:
            atk_mod, def_mod, inst_attrs, iab = irow[0] or 0, irow[1] or 0, irow[2] or 0, irow[3] or "[]"
            try:
                inst_ab = json.loads(iab)
            except Exception:
                inst_ab = []
        try:
            ab = json.loads(ab_json or "[]")
        except Exception:
            ab = []
        ct = game_engine.card_type_from_db(ctype)
        game.card_defs[scid] = game_engine.CardDef(
            name, ct, cost or 0, (atk or 0) + atk_mod, (def_ or 0) + def_mod,
            [], [game_engine.ResourceId.from_str(g) for g in inst_ab or ab],
            attributes=(attrs or 0) | inst_attrs)
        return (template_guid, ctype, name, cost or 0,
                (atk or 0) + atk_mod, (def_ or 0) + def_mod, 0)

    def _sync_instance_card_data(self, session, card_uid, new_template_guid):
        row = self._db.execute(
            "SELECT card_type, attributes, abilities_json FROM card_templates WHERE guid=?",
            (new_template_guid,)).fetchone()
        if row:
            self._db.execute(
                "UPDATE game_cards SET template_guid=?, card_template_id=?, card_type=?, "
                "card_attributes=?, card_abilities=? WHERE session_id=? AND card_uid=?",
                (new_template_guid, new_template_guid, row[0], row[1], row[2],
                 1, int(card_uid)))
            self._db.commit()


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
        card_defense_mod INTEGER, card_cost_mod INTEGER DEFAULT 0,
        cost_mod_json TEXT DEFAULT '[]', card_damage INTEGER,
        permanent_buffs TEXT, temporary_buffs TEXT, card_uses TEXT,
        resolved_at INTEGER, original_template_guid TEXT DEFAULT '')""")
    db.execute("""CREATE TABLE card_abilities_meta (
        ability_guid TEXT, is_triggered INTEGER, trigger_event_type TEXT,
        game_text TEXT, raw_json TEXT, casting_behavior INTEGER,
        is_manual INTEGER, activation_cost INTEGER, uses_per_game INTEGER,
        uses_per_turn INTEGER, target_template_ids TEXT,
        exhausts_on_use INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE ability_effects (
        ability_guid TEXT, effect_guid TEXT, effect_order INTEGER,
        effect_type TEXT, param TEXT, effect_group_id INTEGER DEFAULT 0,
        condition_id TEXT DEFAULT '', target_index INTEGER DEFAULT -1,
        effect_instance_id INTEGER DEFAULT -1,
        contingent_effect_instance_id INTEGER DEFAULT -1,
        secondary_target_index INTEGER DEFAULT -1,
        recalculate_targets INTEGER DEFAULT -1,
        is_optional INTEGER DEFAULT 0,
        effect_duration TEXT DEFAULT 'Instant',
        output_variables TEXT DEFAULT '{}')""")
    db.execute("""CREATE TABLE ability_effect_conditions (
        condition_id TEXT PRIMARY KEY, name TEXT, condition_json TEXT)""")
    db.execute("""CREATE TABLE target_templates (
        template_id TEXT PRIMARY KEY, game_text TEXT, is_auto_target INTEGER,
        is_random_target INTEGER, optional INTEGER, explicit INTEGER,
        player_filter TEXT, collection_flags TEXT, min_target_count INTEGER,
        max_target_count INTEGER, filter_json TEXT)""")
    db.execute("ALTER TABLE target_templates ADD COLUMN target_kind TEXT DEFAULT ''")
    db.execute("""CREATE TABLE card_counter_templates (
        template_id TEXT PRIMARY KEY, name TEXT, description TEXT)""")
    db.execute("""CREATE TABLE decks (
        id INTEGER PRIMARY KEY, user_id INTEGER, deck_name TEXT, cards TEXT,
        active_gems TEXT DEFAULT '{}', last_saved TEXT, created_at TEXT,
        pvp_champion_guid TEXT DEFAULT '')""")
    db.execute("""CREATE TABLE arena_state (
        user_id INTEGER PRIMARY KEY, deck_id INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
        challenger_index INTEGER DEFAULT 0, fight_history TEXT DEFAULT '[]',
        gold_earned INTEGER DEFAULT 0, chests_earned INTEGER DEFAULT 0,
        sacks_earned INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE champion_abilities (
        champion_guid TEXT, champion_name TEXT, ability_guid TEXT,
        ability_name TEXT, charge_cost INTEGER, spell_cost INTEGER,
        threshold_colors TEXT, game_text TEXT, casting_behavior INTEGER,
        thresholds_json TEXT, target_template_ids TEXT)""")
    for target_id in (
            "571b110f-60f1-0210-6bb5-243c0bbb5218",
            "4bfd42fa-d682-f72f-4aed-7471eb52fe76"):
        for row in src.execute(
                "SELECT * FROM target_templates WHERE template_id=?",
                (target_id,)):
            db.execute(
                "INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                row)
    for tid in ("ffccbb0c-8382-83cc-1fe3-67f52ed0ba60",   # champion or troop
                "eb7e48cd-1c85-813f-6635-d43f50cf7809"):  # You
        for row in src.execute(
                "SELECT * FROM target_templates WHERE template_id=?", (tid,)):
            db.execute(
                "INSERT OR IGNORE INTO target_templates VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?)", row)
    db.execute(
        "INSERT INTO decks (id, user_id, deck_name, cards, active_gems, "
        "last_saved, pvp_champion_guid) VALUES (6, 5, 'Orcs', '[]', "
        "'{\"6515\": 5}', '2026-08-12 12:37:00', '4848068e-15fd-4f1d-8009-c136d9821a6d')")
    db.execute("INSERT INTO arena_state (user_id, deck_id) VALUES (5, 0)")
    db.execute(
        "INSERT INTO champion_abilities (champion_guid, champion_name, "
        "ability_guid, ability_name, charge_cost, spell_cost, "
        "threshold_colors, game_text, casting_behavior, thresholds_json, "
        "target_template_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("4848068e-15fd-4f1d-8009-c136d9821a6d", "Poca, The Conflagrater",
         "3687a2ea-0000-0000-0000-000000000000", "Summon a Blaze Elemental",
         4, 0, "Ruby", "Summon a <b>Blaze Elemental</b>.", 8, "{}",
         '["eb7e48cd-1c85-813f-6635-d43f50cf7809"]'))
    for row in src.execute(
            "SELECT * FROM ability_effect_conditions "
            "WHERE condition_id='d35818e3-7209-9c38-241f-6b5e2322d1c9'"):
        db.execute(
            "INSERT INTO ability_effect_conditions VALUES (?,?,?)", row)
    for row in src.execute(
            "SELECT * FROM card_counter_templates "
            "WHERE template_id='12a1bb1f-6308-650c-4d75-35a12cb4c5cd'"):
        db.execute(
            "INSERT INTO card_counter_templates VALUES (?,?,?)", row)
    db.execute("""CREATE TABLE card_templates (
        guid TEXT, name TEXT, card_type TEXT, cost INTEGER, attack INTEGER,
        defense INTEGER, attributes INTEGER, abilities_json TEXT,
        threshold_json TEXT, subtype TEXT)""")
    for guid in list(TPL.values()) + [TPL["sentinel"]]:
        for row in src.execute(
                "SELECT guid, name, card_type, cost, attack, defense, attributes, "
                "abilities_json, threshold_json, subtype "
                "FROM card_templates WHERE guid=?", (guid,)):
            db.execute(
                "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)", row)
    for ag in ABILITIES:
        for row in src.execute(
                "SELECT ability_guid, is_triggered, trigger_event_type, game_text, "
                "raw_json, casting_behavior, is_manual, activation_cost, "
                "uses_per_game, uses_per_turn, target_template_ids, exhausts_on_use "
                "FROM card_abilities_meta WHERE ability_guid=?",
                (ag,)):
            db.execute(
                "INSERT INTO card_abilities_meta VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?)", row)
        for row in src.execute(
                "SELECT ability_guid, effect_guid, effect_order, effect_type, param, "
                "effect_group_id, condition_id, target_index, effect_instance_id, "
                "contingent_effect_instance_id, secondary_target_index, "
                "recalculate_targets, is_optional, effect_duration, output_variables "
                "FROM ability_effects WHERE ability_guid=?", (ag,)):
            db.execute(
                "INSERT INTO ability_effects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row)
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TPL["plain_troop"], "Plain Troop", "Troop", 1, 1, 1, 0, "[]", "[]", ""))
    src.close()
    db.commit()
    return db


def add_card(db, uid, owner, tpl_key_or_guid, location, counters=None):
    tpl = TPL.get(tpl_key_or_guid, tpl_key_or_guid)
    row = db.execute(
        "SELECT card_type, attributes, abilities_json FROM card_templates WHERE guid=?",
        (tpl,)).fetchone()
    ctype, attrs, ab = row
    pb = '{"counters": %s}' % (json.dumps(counters or {}),)
    db.execute(
        "INSERT INTO game_cards VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,'[]',0,?,?,?,0,'')",
        (1, owner, uid, tpl, tpl, location, 0, 0, ab, ctype, attrs, pb, "{}", "{}"))
    db.commit()


def new_game(db):
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"player_health": 20, "ai_health": 10, "_next_instance_id": 1,
              "player_threshold": {}}
    return pl_t, ai_t, game, bstate


def drain_stack(db, handler, game, session, pl_t, ai_t, bstate):
    stack = bstate.get("stack") or []
    while stack:
        item = stack.pop()
        triggers.resolve_stack_trigger(handler, game, session, db, pl_t, ai_t,
                                       bstate, item)


def run(name, fn):
    db = make_db()
    dbmod._db = db
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


def test_heal_chain(db):
    add_card(db, 100, 5, "scrivener", "warzone")
    add_card(db, 101, 5, "paladin", "warzone")
    add_card(db, 102, 5, "incantation", "warzone")
    pl_t, ai_t, game, bstate = new_game(db)
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    triggers.resolve_enters_play_triggers(
        db, handler, game, session, pl_t, ai_t, bstate, 100, 5, 1)
    drain_stack(db, handler, game, session, pl_t, ai_t, bstate)
    assert bstate["player_health"] == 21, bstate["player_health"]
    pal = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE card_uid=101").fetchone()[0]
    assert '"atk": 1' in pal and '"def": 1' in pal, pal
    inc = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE card_uid=102").fetchone()[0]
    assert '"incantation": 1' in inc, inc


def test_lifesteal_heal_fires_healed_trigger(db):
    """Lifelink healing routes through _apply_health_gain so 'when you gain
    health' triggers (Righteous Paladin, Incantation of Righteousness) fire —
    the combat lifelink path must not bypass them."""
    add_card(db, 101, 5, "paladin", "warzone")
    pl_t, ai_t, game, bstate = new_game(db)
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    from abilities.framework.triggers import _apply_health_gain
    _apply_health_gain(game, bstate, pl_t, ai_t, 2, 5,
                       db=db, handler=handler, session=session)
    drain_stack(db, handler, game, session, pl_t, ai_t, bstate)
    assert bstate["player_health"] == 22, bstate["player_health"]
    pal = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE card_uid=101").fetchone()[0]
    assert '"atk": 1' in pal and '"def": 1' in pal, pal


def test_dimmid_starting_health(db):
    """Dimmid's starting health comes from the champion table (19), and the
    LifeSteal deck resolves to Dimmid."""
    src = sqlite3.connect(SRC)
    pguid = src.execute(
        "SELECT pvp_champion_guid FROM decks WHERE id=4").fetchone()
    hp = src.execute(
        "SELECT starting_health FROM champion_template_data WHERE guid=?",
        (pguid[0],)).fetchone()
    src.close()
    assert pguid[0] == "0c0ba840-cba0-4e33-a379-4d16aeaf9a73", pguid
    assert hp[0] == 19, hp


def test_prairie_scout_activation_gating(db):
    """Prairie Scout's 'target attacking troop' ability must NOT be activatable
    in a main phase with no attackers, and MUST be activatable during a combat
    step once a troop is actually attacking."""
    import battle_engine as be
    from hconnect_server import HCPHandler

    class PrairieHandler:
        user_profile = {"id": 5}

        def __init__(self, conn):
            self._db = conn

        def _champion_targets(self):
            return [(int(self._player_champ_scid.uid.uid64), 5, "Player", 20),
                    (int(self._ai_champ_scid.uid.uid64), 0, "AI", 20)]

        def _card_ability_list(self, session, card_uid):
            row = self._db.execute(
                "SELECT card_abilities FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, card_uid)).fetchone()
            try:
                return [g.lower() for g in json.loads(row[0] or "[]")]
            except Exception:
                return []

        def _card_uses(self, session, card_uid):
            return {}

        def _champion_targets(self):
            return []

    add_card(db, 101, 5, "prairie", "warzone")
    h = PrairieHandler(db)
    # Main phase, no attacking troop -> not offered.
    bstate = be.default_state()
    bstate["player_resources"] = 10
    bstate["phase_idx"] = be.BASE_TURN_PHASES.index(
        game_engine.ETurnPhases.FirstMainPhase)
    aff = HCPHandler._affordable_troop_abilities(h, SessionStub(), bstate)
    assert not any(uid == 101 for uid, _t in aff), aff
    # Combat step with a ready troop that is attacking -> offered.
    bstate2 = be.default_state()
    bstate2["player_resources"] = 10
    bstate2["turn_phases"] = be.COMBAT_TURN_PHASES
    bstate2["phase_idx"] = be.COMBAT_TURN_PHASES.index(
        game_engine.ETurnPhases.DeclareAttackPriorityWindow)
    db.execute(
        "UPDATE game_cards SET card_state = card_state | ? | ? "
        "WHERE card_uid=101",
        (game_engine.ECardStates.StartedATurnOnYourSide,
         game_engine.ECardStates.Attacking))
    db.commit()
    aff = HCPHandler._affordable_troop_abilities(h, SessionStub(), bstate2)
    assert (101, TPL["prairie"]) in aff, aff


def test_pregame_health_counts_only_heals(db):
    """Dimmid's Lifedrain charge power (an attribute-grant BOM) must not add
    +1 starting health; only real PreGame heal talents (healhero leaves with a
    pregame_* condition) contribute."""
    from abilities.framework.conditions import _apply_bom_health
    src = sqlite3.connect(SRC)
    lifedrain = _apply_bom_health(
        src, "3ccc7773-0697-86ac-ab88-b40379aed08b")
    shard_attuned = _apply_bom_health(
        src, "59b3f27b-b534-5245-e254-3c84f179783d")
    src.close()
    assert lifedrain == 0, lifedrain
    assert shard_attuned == 1, shard_attuned


def test_escalation_preview_value(db):
    """Eternal Youth's CardDef carries the escalation multiplier (uses + 1) so
    the client previews 'Gain 4 health', then 'Gain 8' after one cast."""
    import battle_engine as be
    import db as dbmod
    import hconnect_server as hmod
    from hconnect_server import HCPHandler
    dbmod._db = db
    hmod._db = db
    add_card(db, 101, 5, "eternal_youth", "hand")

    class EH:
        user_profile = {"id": 5}
        _current_bstate = {"player_escalation_uses": 0}

        def _template_by_guid(self, tg):
            return db.execute(
                "SELECT guid, card_type, name, cost, attack, defense "
                "FROM card_templates WHERE guid=?", (tg,)).fetchone()

        def _granted_attributes(self, ags):
            return 0

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    eh = EH()
    game = game_engine.Game(1, pl_t, ai_t)
    scid = game_engine.SessionCardId(game_engine.UID(101))
    HCPHandler._card_full_data(eh, game, scid, TPL["eternal_youth"])
    assert game.card_defs[scid].escalation == 1
    eh._current_bstate = {"player_escalation_uses": 2}
    game2 = game_engine.Game(1, pl_t, ai_t)
    HCPHandler._card_full_data(eh, game2, scid, TPL["eternal_youth"])
    assert game2.card_defs[scid].escalation == 3


def test_void_uses_trigger_target(db):
    """Solitary Exile's Deploy target (resolving_target_uid) must win over a
    stale player_mod_target left by an earlier champion power."""
    from abilities.framework.bom import _LEAFS
    add_card(db, 101, 5, "plain_troop", "warzone")   # stale mod target (own)
    add_card(db, 102, 0, "plain_troop", "warzone")   # the chosen opponent card
    pl_t, ai_t, game, bstate = new_game(db)
    handler = HandlerStub()
    handler._db = db
    bstate["player_mod_target"] = 101                # stale from prior power
    bstate["resolving_target_uid"] = 102             # what the player chose
    _LEAFS["VoidCardAbilityEffectTemplate"](
        game, SessionStub(), db, handler, pl_t, ai_t, bstate, "e", None)
    loc1 = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=101").fetchone()[0]
    loc2 = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=102").fetchone()[0]
    assert loc1 == "warzone", loc1
    assert loc2 == "void", loc2


def test_counters_survive_stat_mod(db):
    """apply_card_stat_mod must preserve counters stored in permanent_buffs —
    _load_buffs previously dropped unknown keys, erasing Incantation counters."""
    add_card(db, 100, 5, "incantation", "warzone")
    db.execute(
        "UPDATE game_cards SET permanent_buffs=? WHERE card_uid=100",
        ('{"counters": {"incantation": 4}, '
         '"counter_guids": {"incantation": "12a1bb1f-6308-650c-4d75-35a12cb4c5cd"}}',))
    db.commit()
    pl_t, ai_t, game, bstate = new_game(db)
    from abilities.framework.stat_mod import apply_card_stat_mod
    handler = HandlerStub()
    handler._db = db
    apply_card_stat_mod(game, SessionStub(), db, handler, pl_t, ai_t,
                        100, 1, 1)
    pb = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE card_uid=100").fetchone()[0]
    assert '"incantation": 4' in pb, pb
    assert '"atk": 1' in pb, pb


def test_deck_stat_mod_is_nulled(db):
    """A hidden deck-card update must not reveal its full representation."""
    add_card(db, 100, 5, "plain_troop", "deck")
    pl_t, ai_t, game, bstate = new_game(db)
    from abilities.framework.stat_mod import apply_card_stat_mod
    handler = HandlerStub()
    handler._db = db

    apply_card_stat_mod(game, SessionStub(), db, handler, pl_t, ai_t,
                        100, 1, 1)

    scid = game_engine.SessionCardId(game_engine.UID(100))
    updates = [event for event in game.events
               if isinstance(event, game_engine.CardUpdatedSessionEventArgs)
               and event.session_card_id.uid.uid64 == scid.uid.uid64]
    assert updates and updates[-1].nulling is True, updates


def test_cardupdated_carries_counters_and_related(db):
    """Every CardUpdated for a card with counters or voided-card links must
    carry them (so badges / the exile relationship don't vanish on re-push)."""
    import hconnect_server as hmod
    import db as dbmod
    import hconnect_server as hmod
    from hconnect_server import HCPHandler
    dbmod._db = db
    hmod._db = db
    hmod._db = db
    add_card(db, 101, 5, "incantation", "warzone")
    add_card(db, 102, 5, "exile", "warzone")
    db.execute(
        "UPDATE game_cards SET permanent_buffs=? WHERE card_uid=101",
        ('{"counters": {"incantation": 3}, '
         '"counter_guids": {"incantation": "12a1bb1f-6308-650c-4d75-35a12cb4c5cd"}}',))
    db.commit()

    class EH:
        user_profile = {"id": 5}
        _current_bstate = {"voided_by": {"102": [16385]}}

        def _template_by_guid(self, tg):
            return db.execute(
                "SELECT guid, card_type, name, cost, attack, defense "
                "FROM card_templates WHERE guid=?", (tg,)).fetchone()

        def _granted_attributes(self, ags):
            return 0

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    eh = EH()
    game = game_engine.Game(1, pl_t, ai_t)
    scid101 = game_engine.SessionCardId(game_engine.UID(101))
    scid102 = game_engine.SessionCardId(game_engine.UID(102))
    HCPHandler._card_full_data(eh, game, scid101, TPL["incantation"])
    HCPHandler._card_full_data(eh, game, scid102, TPL["exile"])
    assert game.card_defs[scid101].counters, game.card_defs[scid101].counters
    assert [int(r.uid.uid64) for r in game.card_defs[scid102].related_cards] == [16385]
    game.push_card_updated(scid101, pl_t, game_engine.ECardCollections.Warzone,
                           game_engine.ECardTypes.Constant,
                           template_id=TPL["incantation"])
    ev = game.events[-1]
    assert ev.counter_counts and ev.counter_counts[0] == 3, ev.counter_counts


def test_spell_heal_targets_caster(db):
    """Eternal Youth's 'Gain ESC:4 health' must heal the CASTER even when a
    stale resolving_owner_id=0 (left by an earlier AI-card trigger) is in the
    battle state — the source card's owner is authoritative."""
    add_card(db, 101, 5, "eternal_youth", "hand")
    pl_t, ai_t, game, bstate = new_game(db)
    handler = HandlerStub()
    handler._db = db
    bstate["player_health"] = 19
    bstate["ai_health"] = 20
    bstate["resolving_owner_id"] = 0          # stale from an AI trigger
    bstate["resolving_source_uid"] = 101
    bstate["resolving_ability"] = "9b85495d-fd29-a90e-9ccf-723bf2b85ae6"
    bstate["player_spell_target"] = None
    _LEAFS["CardModifierAbilityEffectTemplate"](
        game, SessionStub(), db, handler, pl_t, ai_t, bstate, "e",
        '{"text": "Gain ESC:4 health.", "property": "healhero", "amount": 0}')
    assert bstate["player_health"] == 23, bstate
    assert bstate["ai_health"] == 20, bstate


def test_burn_champion_playable(db):
    """Burn ('Deal 3 damage to target champion or troop') must be playable even
    with no troops on the board — champions always exist as targets, and the
    target pool must include both champions."""
    import db as dbmod
    import hconnect_server as hmod
    from hconnect_server import HCPHandler
    dbmod._db = db
    hmod._db = db

    class Stub:
        user_profile = {"id": 5}
        _player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        _ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))

        def __init__(self, conn):
            self._db = conn

        def _champion_targets(self):
            return [(int(self._player_champ_scid.uid.uid64), 5, "Player", 20),
                    (int(self._ai_champ_scid.uid.uid64), 0, "AI", 20)]

    s = Stub(db)
    reqs = HCPHandler._card_troop_requirements(
        s, ["81712882-30ed-c365-1d90-211966640219"])
    assert reqs == set(), reqs
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    targets = HCPHandler._valid_targets_for_template(
        s, SessionStub(), pl_t, ai_t, "ffccbb0c-8382-83cc-1fe3-67f52ed0ba60")
    uids = [int(t.uid.uid64) for t in targets]
    assert int(s._player_champ_scid.uid.uid64) in uids, uids
    assert int(s._ai_champ_scid.uid.uid64) in uids, uids


def test_you_damage_targets_champion(db):
    """Shamed Gladiator's Deploy 'This deals 2 damage to you' must hit the
    controller's CHAMPION, not the source troop ('You' target template)."""
    import db as dbmod
    dbmod._db = db
    from abilities.framework.bom import _champion_target_uid

    class H:
        _player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        _ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))

    bstate = {"resolving_ability": "b95fdd81-2eca-f2cb-b28b-c5ec70307ca0",
              "resolving_owner_id": 5}
    uid = _champion_target_uid(H(), bstate, db, SessionStub())
    assert uid == int(H._player_champ_scid.uid.uid64), uid


def test_auto_target_no_picker(db):
    """A shard's 'You' target is an auto PlayerTargetTemplate — it must NOT
    attach a hand-play target picker (no targeting crosshair when playing a
    resource)."""
    import db as dbmod
    import hconnect_server as hmod
    from hconnect_server import HCPHandler
    dbmod._db = db
    hmod._db = db

    class Stub:
        user_profile = {"id": 5}
        _player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        _ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))

        def __init__(self, conn):
            self._db = conn

        def _champion_targets(self):
            return []

    s = Stub(db)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    targets = HCPHandler._valid_targets_for_template(
        s, SessionStub(), pl_t, ai_t, "eb7e48cd-1c85-813f-6635-d43f50cf7809")
    assert targets is None, targets


def test_gem_survives_repush(db):
    """A socketed gem must survive re-pushes that omit the instance id (the
    trigger 'shake', phase re-pushes) — _card_full_data recovers it from the
    card row, so Shamed Gladiator keeps its gem in the warzone."""
    import db as dbmod
    import hconnect_server as hmod
    from hconnect_server import HCPHandler
    dbmod._db = db
    hmod._db = db
    db.execute(
        "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,"
        "card_template_id,location,position,card_state,card_abilities,"
        "card_type,card_attributes) VALUES (1,5,101,?,?, 'warzone',0,0,"
        "'[]','Troop',0)", (TPL["gladiator"], 6515))
    db.commit()

    class Stub:
        user_profile = {"id": 5}
        _current_bstate = {}

        def __init__(self, conn):
            self._db = conn

        def _template_by_guid(self, tg):
            return db.execute(
                "SELECT guid, card_type, name, cost, attack, defense "
                "FROM card_templates WHERE guid=?", (tg,)).fetchone()

        def _granted_attributes(self, ags):
            return 0

    Stub._resolve_fra_deck_id = HCPHandler._resolve_fra_deck_id
    s = Stub(db)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    g = game_engine.Game(1, pl_t, ai_t)
    scid = game_engine.SessionCardId(game_engine.UID(101))
    _tpl, ct, nm, cost, atk, de, gem = HCPHandler._card_full_data(
        s, g, scid, TPL["gladiator"], None)
    assert gem == 5, gem


def test_poca_ability_no_picker(db):
    """Poca's 'Summon a Blaze Elemental' has a 'You' auto target — it must NOT
    attach a champion-ability target picker."""
    import db as dbmod
    import hconnect_server as hmod
    from hconnect_server import HCPHandler
    dbmod._db = db
    hmod._db = db

    class Stub:
        user_profile = {"id": 5}

        def __init__(self, conn):
            self._db = conn

        def _champion_targets(self):
            return []

        def _ability_cost_templates(self, ability_guid):
            # Poca's charge power has no card-payment target (its only target
            # is the gamedata "You" auto-target).  The production target
            # builder calls this helper to exclude void/sacrifice/etc. costs;
            # this minimal test handler has no cost metadata, so report none.
            return []

    s = Stub(db)
    rid = game_engine.ResourceId.from_str(
        "3687a2ea-0000-0000-0000-000000000000")
    out = HCPHandler._champion_ability_targets(s, SessionStub(), [rid])
    assert out == {}, out


def test_incantation_transform(db):
    add_card(db, 100, 5, "scrivener", "warzone")
    add_card(db, 102, 5, "incantation", "warzone", counters={"incantation": 4})
    pl_t, ai_t, game, bstate = new_game(db)
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    triggers.resolve_enters_play_triggers(
        db, handler, game, session, pl_t, ai_t, bstate, 100, 5, 1)
    drain_stack(db, handler, game, session, pl_t, ai_t, bstate)
    tpl = db.execute(
        "SELECT template_guid FROM game_cards WHERE card_uid=102").fetchone()[0]
    assert tpl == TPL["sentinel"], tpl
    pb = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE card_uid=102").fetchone()[0]
    assert "incantation" not in pb, pb


def test_incantation_gate_does_not_transform_below_five(db):
    add_card(db, 100, 5, "scrivener", "warzone")
    add_card(db, 102, 5, "incantation", "warzone", counters={"incantation": 3})
    pl_t, ai_t, game, bstate = new_game(db)
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    triggers.resolve_enters_play_triggers(
        db, handler, game, session, pl_t, ai_t, bstate, 100, 5, 1)
    drain_stack(db, handler, game, session, pl_t, ai_t, bstate)
    row = db.execute(
        "SELECT template_guid, location FROM game_cards WHERE card_uid=102"
    ).fetchone()
    assert row == (TPL["incantation"], "warzone"), row


def test_kraken_inspire(db):
    add_card(db, 200, 5, "kraken", "warzone")
    add_card(db, 201, 5, "totem", "warzone")
    pl_t, ai_t, game, bstate = new_game(db)
    bstate.update({"pvp": True, "pids": [5, 1006]})
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    triggers.resolve_enters_play_triggers(
        db, handler, game, session, pl_t, ai_t, bstate, 201, 5)
    attrs = db.execute(
        "SELECT card_attributes FROM game_cards WHERE card_uid=201").fetchone()[0]
    assert attrs & game_engine.ECardAttributes.Steadfast, attrs


def test_outrider_attack(db):
    add_card(db, 300, 5, "outrider", "warzone")
    pl_t, ai_t, game, bstate = new_game(db)
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    triggers.resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                              "CardAttackedEvent", 300, 5)
    drain_stack(db, handler, game, session, pl_t, ai_t, bstate)
    tb = db.execute(
        "SELECT temporary_buffs FROM game_cards WHERE card_uid=300").fetchone()[0]
    assert '"atk": 4' in tb, tb


def test_angel_draw(db):
    add_card(db, 400, 5, "angel", "hand")
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_threshold"] = {game_engine.ECardShards.Diamond: 1}
    bstate["player_draws_this_turn"] = 1
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    # CardDrawnEvent's source is the drawing champion and its target is the
    # card drawn.  Angel's TriggerTarget condition and play effect depend on
    # that distinction.
    triggers.resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                              "CardDrawnEvent", 999999, 5, extra_target=400)
    drain_stack(db, handler, game, session, pl_t, ai_t, bstate)
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=400").fetchone()[0]
    assert loc == "warzone", loc


def test_angel_draw_gate(db):
    add_card(db, 400, 5, "angel", "hand")
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_threshold"] = {game_engine.ECardShards.Diamond: 1}
    bstate["player_draws_this_turn"] = 2
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    triggers.resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                              "CardDrawnEvent", 999999, 5, extra_target=400)
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=400").fetchone()[0]
    assert loc == "hand", loc


def test_eternal_youth(db):
    add_card(db, 500, 5, "eternal_youth", "hand")
    pl_t, ai_t, game, bstate = new_game(db)
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    resolve_played_spell(game, session, db, handler, pl_t, ai_t, bstate,
                         [ABILITIES[8]])
    assert bstate["player_health"] == 24, bstate["player_health"]
    resolve_played_spell(game, session, db, handler, pl_t, ai_t, bstate,
                         [ABILITIES[8]])
    assert bstate["player_health"] == 32, bstate["player_health"]


def test_totem_manual(db):
    add_card(db, 600, 5, "totem", "warzone")
    pl_t, ai_t, game, bstate = new_game(db)
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    bstate["player_mod_target"] = 600
    fn = _LEAFS["CardModifierAbilityEffectTemplate"]
    for effect_guid, param in (
            ("7357e5b2-3819-f851-4f40-8b97349f3792", '{"property": "attack", "amount": 1, "duration": "Permanent"}'),
            ("7357e5b2-3819-f851-4f40-8b97349f3792", '{"property": "defense", "amount": 1, "duration": "Permanent"}'),
            ("4cd98e94-c38b-f50a-afb7-c81438c93126", '{"property": "attribute", "amount": 0, "text": "<b>Flight</b>", "duration": "Permanent"}')):
        fn(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param)
    pb = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE card_uid=600").fetchone()[0]
    assert '"atk": 1' in pb and '"def": 1' in pb, pb
    attrs = db.execute(
        "SELECT card_attributes FROM game_cards WHERE card_uid=600").fetchone()[0]
    assert attrs & game_engine.ECardAttributes.Flight, attrs


def test_inner_conflict(db):
    add_card(db, 700, 5, "plain_troop", "warzone")
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_spell_target"] = 700
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    resolve_played_spell(game, session, db, handler, pl_t, ai_t, bstate,
                         [ABILITIES[9]])
    attrs = db.execute(
        "SELECT card_attributes FROM game_cards WHERE card_uid=700").fetchone()[0]
    assert attrs & game_engine.ECardAttributes.CantAttack, attrs
    assert attrs & game_engine.ECardAttributes.CantBlock, attrs


def test_exile_void_return(db):
    add_card(db, 800, 5, "exile", "warzone")
    add_card(db, 801, 5, "plain_troop", "warzone")
    pl_t, ai_t, game, bstate = new_game(db)
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    bstate["resolving_target_uid"] = 801
    bstate["resolving_source_uid"] = 800
    bstate["player_spell_target"] = 801
    _LEAFS["VoidCardAbilityEffectTemplate"](game, session, db, handler,
                                            pl_t, ai_t, bstate, "v", None)
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=801").fetchone()[0]
    assert loc == "void", loc
    triggers.resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                              "CardExitedZoneEvent", 800, 5)
    drain_stack(db, handler, game, session, pl_t, ai_t, bstate)
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=801").fetchone()[0]
    assert loc == "warzone", loc


def test_voiding_exile_returns_voided_cards(db):
    """Voiding a Solitary Exile fires its 'when this leaves play' trigger,
    which returns every card it had voided (the client fires CardExitedZoneEvent
    on the zone exit — the void leaf now does the same)."""
    add_card(db, 800, 5, "exile", "warzone")     # first exile
    add_card(db, 801, 0, "plain_troop", "void")  # card the first exile voided
    add_card(db, 900, 5, "exile", "warzone")     # second exile
    pl_t, ai_t, game, bstate = new_game(db)
    session = SessionStub()
    handler = HandlerStub()
    handler._db = db
    bstate["voided_by"] = {"800": [801]}
    bstate["resolving_source_uid"] = 900
    bstate["resolving_target_uid"] = 800
    bstate["player_spell_target"] = 800
    _LEAFS["VoidCardAbilityEffectTemplate"](
        game, session, db, handler, pl_t, ai_t, bstate, "v", None)
    drain_stack(db, handler, game, session, pl_t, ai_t, bstate)
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=801").fetchone()[0]
    assert loc == "warzone", loc
    loc_exile = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=800").fetchone()[0]
    assert loc_exile == "void", loc_exile


if __name__ == "__main__":
    run("heal chain (Scrivener -> Paladin/Incantation)", test_heal_chain)
    run("lifesteal heal fires healed triggers", test_lifesteal_heal_fires_healed_trigger)
    run("Dimmid starts at 19 from champion table", test_dimmid_starting_health)
    run("Prairie Scout gated to combat + attacking troop", test_prairie_scout_activation_gating)
    run("PreGame health counts only heal leaves", test_pregame_health_counts_only_heals)
    run("Escalation preview scales with uses", test_escalation_preview_value)
    run("Void uses the chosen trigger target", test_void_uses_trigger_target)
    run("Counters survive permanent stat mods", test_counters_survive_stat_mod)
    run("Deck stat-mod updates stay hidden", test_deck_stat_mod_is_nulled)
    run("CardUpdated carries counters and related cards", test_cardupdated_carries_counters_and_related)
    run("Spell heal goes to the caster not the AI", test_spell_heal_targets_caster)
    run("Burn playable vs champion targets", test_burn_champion_playable)
    run("'You' damage hits the champion", test_you_damage_targets_champion)
    run("Auto targets never show a picker", test_auto_target_no_picker)
    run("Gem survives instance-less re-push", test_gem_survives_repush)
    run("Poca auto target never shows a picker", test_poca_ability_no_picker)
    run("Incantation transforms at 5 counters", test_incantation_transform)
    run("Incantation gate blocks premature transform",
        test_incantation_gate_does_not_transform_below_five)
    run("Kraken Guard Inspire grants Steadfast", test_kraken_inspire)
    run("Chimera Guard Outrider attack buff", test_outrider_attack)
    run("Angel of Dawn plays free on first draw", test_angel_draw)
    run("Angel of Dawn gate blocks second draw", test_angel_draw_gate)
    run("Eternal Youth escalation heals 4 then 8", test_eternal_youth)
    run("Living Totem manual +1/+1 and Flight", test_totem_manual)
    run("Inner Conflict can't attack/block", test_inner_conflict)
    run("Solitary Exile void + return", test_exile_void_return)
    run("Voiding an exile returns its voided cards", test_voiding_exile_returns_voided_cards)
