"""Regression tests for the Shamed Gladiator vs Darkspire Priestess combat.

The user's Orc-deck game reported "Darkspire Priestess blocked Shamed
Gladiator and neither died".  The shared resolve_combat must kill both (2/2 vs
2/1), push the two CardUpdated+CardMoved deaths to the discard, resolve the
priestess's Deathcry AFTER all combat damage (not mid-fight), and never treat
the gladiator's Deploy (enters-play) trigger as a Deathcry.
"""

import json
import os
import random
import sqlite3
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine
import ai

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db")

TPL_GLADIATOR = "b7172b6a-ef85-4fef-91e1-81975b4ce7cd"
TPL_PRIESTESS = "14909185-1070-48df-9508-61d5a9650bd2"
TPL_ENFORCER = "d790e8b9-a000-475e-8350-d11be117d6bc"

AG_DEPLOY = "b95fdd81-2eca-f2cb-b28b-c5ec70307ca0"
AG_DEATHCRY = "9853659b-89f4-1e16-f940-67bdb37f5729"


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE game_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER, user_id INTEGER, card_uid INTEGER,
        template_guid TEXT, card_template_id TEXT, location TEXT,
        position INTEGER, card_state INTEGER, card_abilities TEXT,
        card_type TEXT, card_attributes INTEGER, is_champion INTEGER DEFAULT 0,
        temporary_attributes INTEGER DEFAULT 0,
        card_attack_mod INTEGER, card_defense_mod INTEGER, card_cost_mod INTEGER,
        cost_mod_json TEXT DEFAULT '[]', card_damage INTEGER,
        permanent_buffs TEXT DEFAULT '{}', temporary_buffs TEXT DEFAULT '{}',
        card_uses TEXT DEFAULT '{}', resolved_at INTEGER DEFAULT 0,
        original_template_guid TEXT DEFAULT '')""")
    db.execute("""CREATE TABLE card_templates (
        guid TEXT, name TEXT, card_type TEXT, cost INTEGER, attack INTEGER,
        defense INTEGER, attributes INTEGER, abilities_json TEXT,
        threshold_json TEXT, subtype TEXT, variable_cost INTEGER DEFAULT 0,
        variable_cost_minimum INTEGER DEFAULT 0, rage_value INTEGER DEFAULT 0)""")
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
    db.execute("""CREATE TABLE gem_templates (
        gem_type INTEGER PRIMARY KEY, gem_type_name TEXT, name TEXT,
        abilities_json TEXT DEFAULT '[]')""")
    db.execute("""CREATE TABLE target_templates (
        template_id TEXT PRIMARY KEY, game_text TEXT DEFAULT '',
        is_auto_target INTEGER DEFAULT 0, is_random_target INTEGER DEFAULT 0,
        optional INTEGER DEFAULT 0, explicit INTEGER DEFAULT 0,
        player_filter TEXT DEFAULT '', collection_flags TEXT DEFAULT '',
        min_target_count INTEGER DEFAULT 1, max_target_count INTEGER DEFAULT 1,
        filter_json TEXT DEFAULT '{}', target_kind TEXT DEFAULT '')""")
    src = sqlite3.connect(SRC)
    for trow in src.execute(
            "SELECT template_id, game_text, is_auto_target, is_random_target, "
            "optional, explicit, player_filter, collection_flags, "
            "min_target_count, max_target_count, filter_json, target_kind "
            "FROM target_templates WHERE template_id IN "
            "('eb7e48cd-1c85-813f-6635-d43f50cf7809', "
            "'0ad94887-419c-9e99-7946-74c4f72cdd2e', "
            "'c35dd13a-71e2-b244-847a-d887a0666210', "
            "'190a4d8c-7c2c-10d0-6429-99c5aeb0791f')").fetchall():
        db.execute("INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   trow)
    for tpl, name, atk, deff, abjson in (
            (TPL_GLADIATOR, "Shamed Gladiator", 2, 2,
             json.dumps([AG_DEPLOY])),
            (TPL_PRIESTESS, "Darkspire Priestess", 2, 1,
             json.dumps([AG_DEATHCRY])),
            (TPL_ENFORCER, "Darkspire Enforcer", 3, 2, "[]")):
        db.execute(
            "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tpl, name, "Troop", 2, atk, deff, 0, abjson, "[]", "", 0, 0, 0))
    # Copy the gamedata for the two abilities + the deathcry BOM chain from the
    # live DB so the fixtures are data-driven (not hand-written).
    bom_abilities = [AG_DEATHCRY,
                     "7361162f-ed16-fc00-16dd-22517295282b",
                     "79e4a481-fc29-14c0-4524-5db861bd6f0b",
                     "cb21843f-2eae-6bd0-e47d-d13b6006e4af",
                     "37955055-a070-b24e-ada8-ad94e04c1e39"]
    src_meta = src.execute(
        "SELECT ability_guid, is_triggered, trigger_event_type, game_text, "
        "raw_json, casting_behavior, is_manual, activation_cost, "
        "uses_per_game, uses_per_turn, target_template_ids, exhausts_on_use "
        "FROM card_abilities_meta WHERE ability_guid IN (%s)"
        % ",".join("?" * (1 + len(bom_abilities))),
        [AG_DEPLOY] + bom_abilities).fetchall()
    for row in src_meta:
        db.execute(
            "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            row)
    marks = ",".join("?" * len(bom_abilities))
    for row in src.execute(
            "SELECT ability_guid, effect_guid, effect_order, effect_type, "
            "param, effect_group_id, condition_id, target_index, "
            "effect_instance_id, contingent_effect_instance_id, "
            "secondary_target_index, recalculate_targets, is_optional, "
            "effect_duration, output_variables "
            "FROM ability_effects WHERE ability_guid IN (%s) "
            "ORDER BY ability_guid, effect_order" % marks,
            bom_abilities).fetchall():
        db.execute("INSERT INTO ability_effects VALUES "
                   "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    for row in src.execute(
            "SELECT condition_id, name, condition_json "
            "FROM ability_effect_conditions WHERE condition_id IN "
            "('55b776fe-3814-5024-7188-f5a2365379d1', "
            "'efa80b85-6c74-65d5-ce38-96e0ad626901')").fetchall():
        db.execute("INSERT INTO ability_effect_conditions VALUES (?,?,?)", row)
    src.close()
    db.commit()
    return db


def add_card(db, uid, owner, tpl, loc="warzone", state=0):
    db.execute(
        "INSERT INTO game_cards (session_id, user_id, card_uid, template_guid, "
        "card_template_id, location, position, card_state, card_abilities, "
        "card_type, card_attributes, card_attack_mod, card_defense_mod, "
        "card_cost_mod, card_damage, permanent_buffs, temporary_buffs, "
        "card_uses, resolved_at, original_template_guid) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, owner, uid, tpl, tpl, loc, 0, state, "[]", "Troop", 0,
         0, 0, 0, 0, "{}", "{}", "{}", 0, tpl))
    db.commit()


class SessionStub:
    session_id = 1
    server_id = 100
    turn_order = {}
    session_name = ""

    def _persist(self):
        pass

    def set_state(self, state):
        self.state = state


class HandlerStub:
    user_profile = {"id": 5}
    client_reck_id = 5
    scnt = 0
    sid = "hcp-test"

    def __init__(self, db):
        self._db = db
        self._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        self._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))

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

    def _champion_targets(self):
        """[(card_uid, user_id, name, health)] mirroring the real handler so
        the trigger condition engine can evaluate IsHero / controls-target
        conditions against champions."""
        bstate = getattr(self, "_current_bstate", None) or {}
        return [
            (int(self._player_champ_scid.uid.uid64), 5, "Player",
             int(bstate.get("player_health", 20))),
            (int(self._ai_champ_scid.uid.uid64), 0, "AI", 20),
        ]

    def _max_hand_size(self, session):
        return 7

    def send(self, *a, **k):
        return None

    def _player_draw_card(self, game, session, pl_t):
        """Draw the top card of the player's deck into hand (sweep harness)."""
        row = self._db.execute(
            "SELECT id, card_uid, template_guid FROM game_cards "
            "WHERE session_id=? AND user_id=5 AND location='deck' "
            "ORDER BY position LIMIT 1", (session.session_id,)).fetchone()
        if not row:
            return None
        self._db.execute(
            "UPDATE game_cards SET location='hand', position=100 WHERE id=?",
            (row[0],))
        self._db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(row[1]))
        tpl = row[2]
        _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(game, scid, tpl)
        game.push_card_moved(scid, pl_t, game_engine.ECardCollections.Hand,
                             game_engine.ECardLocations.Top, 1)
        game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand,
                               ct, template_id=tpl)
        return None

    def _prompt_deck_search(self, game, session, pl_t, ai_t, bstate,
                            ability_guid, source_uid, owner_id, candidates,
                            kind="search"):
        """Harness: auto-pick a random candidate and move it to hand (the real
        fallback used when no interactive prompt exists)."""
        import random as _rnd
        from abilities.framework.effects.search import move_deck_card_to_hand
        if not candidates:
            return "deck search: no candidates"
        chosen = _rnd.choice(list(candidates))
        return move_deck_card_to_hand(game, session, self._db, self, pl_t, ai_t,
                                      chosen, owner_id, bstate)

    def _push_discard_prompt(self, *a, **k):
        return None

    def _bom_has_discard(self, ability_guid):
        return False

    def _sacrifice_troop(self, game, session, pl_t, ai_t, card_uid):
        self._db.execute(
            "UPDATE game_cards SET location='discard' "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(card_uid)))
        self._db.commit()
        return None

    def _discard_card_to_owner(self, session, pl_t, ai_t, card_uid):
        self._db.execute(
            "UPDATE game_cards SET location='discard' "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(card_uid)))
        self._db.commit()
        return None, ai_t

    def _card_full_data(self, game, scid, template_guid, instance_id=None):
        row = self._db.execute(
            "SELECT card_type, name, cost, attack, defense, attributes "
            "FROM card_templates WHERE guid=?",
            (template_guid,)).fetchone()
        if not row:
            return (template_guid, game_engine.ECardTypes.Troop,
                    "Card", 0, 0, 0, 0)
        ct = game_engine.card_type_from_db(row[0])
        game.card_defs[scid] = game_engine.CardDef(
            row[1], ct, row[2] or 0, row[3] or 0, row[4] or 0, [], [],
            attributes=row[5] or 0)
        return (template_guid, ct, row[1], row[2] or 0,
                row[3] or 0, row[4] or 0, 0)

    def _resolve_stack_item(self, *a, **k):
        return None

    def _next_resolve_counter(self, session):
        return 1

    def _remove_one_shot_ability(self, session, card_uid, ability_guid,
                                 game, pl_t, ai_t, bstate=None):
        """Consume a one-shot ability on a card instance (mirror of the
        production hconnect handler's method, without the client push).

        ONE-SHOT is represented as uses_per_game=1; only those abilities are
        consumed.  The ability GUID is removed from this card's working
        ability list so the resolved power is not offered again.
        """
        import json as _json
        meta = self._db.execute(
            "SELECT uses_per_game FROM card_abilities_meta "
            "WHERE ability_guid=?", (str(ability_guid).lower(),)).fetchone()
        if not meta or int(meta[0] or 0) != 1:
            return False
        try:
            abilities = _json.loads(self._db.execute(
                "SELECT card_abilities FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, int(card_uid))).fetchone()[0] or "[]")
        except (TypeError, ValueError):
            return False
        ag = str(ability_guid).lower()
        if ag not in abilities:
            return False
        abilities.remove(ag)
        self._db.execute(
            "UPDATE game_cards SET card_abilities=? "
            "WHERE session_id=? AND card_uid=?",
            (_json.dumps(abilities), session.session_id, int(card_uid)))
        self._db.commit()
        return True

    @staticmethod
    def _thresholds_met(thresh_json, player_threshold):
        return True

    def _sync_instance_card_data(self, session, card_uid, new_template_guid):
        pass


def run_combat(db, player_deck_has_enforcer=False):
    """FRA combat: AI Shamed Gladiator (2/2) attacks, the player's Darkspire
    Priestess (2/1) blocks.  Returns (locations, states, game, bstate)."""
    add_card(db, 101, 0, TPL_GLADIATOR)          # AI attacker
    add_card(db, 102, 5, TPL_PRIESTESS)          # player blocker
    if player_deck_has_enforcer:
        add_card(db, 103, 5, TPL_ENFORCER, loc="deck")
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    bstate = {"player_health": 20, "ai_health": 20,
              "player_max_health": 20, "ai_max_health": 20,
              "turn_number": 1}
    attackers = {101: 0}
    blockers = {101: [102]}
    handler = HandlerStub(db)
    captured = {}

    def _capture(game, pl_t2, ai_t2, bstate2):
        captured["game"] = game

    ai._db = db
    ai.resolve_combat(handler, SessionStub(), pl_t, ai_t, bstate, attackers,
                      blockers, ai_t, pl_t, "ai_attackers",
                      send_events=_capture)
    loc_a = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=101").fetchone()[0]
    loc_b = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=102").fetchone()[0]
    st_a = db.execute(
        "SELECT card_state FROM game_cards WHERE card_uid=101").fetchone()[0]
    st_b = db.execute(
        "SELECT card_state FROM game_cards WHERE card_uid=102").fetchone()[0]
    return loc_a, loc_b, st_a, st_b, captured.get("game"), bstate


def test_gladiator_priestess_both_die(db):
    """2/2 Shamed Gladiator vs 2/1 Darkspire Priestess: both die and the
    client receives a CardUpdated + CardMoved to the discard for each."""
    with mock.patch("random.randint", return_value=1):
        loc_a, loc_b, st_a, st_b, game, bstate = run_combat(
            db, player_deck_has_enforcer=True)
    assert loc_a == "discard" and loc_b == "discard", (loc_a, loc_b)
    assert st_a & game_engine.ECardStates.Dead, st_a
    assert st_b & game_engine.ECardStates.Dead, st_b
    assert game is not None
    discards = [ev for ev in game.events
                if ev.__class__.__name__ == "CardUpdatedSessionEventArgs"
                and ev.collection == game_engine.ECardCollections.Discard]
    moves = [ev for ev in game.events
             if ev.__class__.__name__ == "CardMovedSessionEventArgs"
             and ev.collection == game_engine.ECardCollections.Discard]
    assert len(discards) == 2, [int(e.session_card_id.uid.uid64)
                                for e in discards]
    assert len(moves) == 2, [int(e.session_card_id.uid.uid64) for e in moves]
    # The priestess's Deathcry resolved AFTER the combat: roll=1 deals 3 to
    # the opposing (AI) champion.
    assert bstate["ai_health"] == 17, bstate


def test_priestess_deathcry_search_branch(db):
    """Roll=2 branch: the priestess's Deathcry searches the owner's deck for a
    Darkspire troop and puts it into hand (data-driven MoveCardToZone Hand)."""
    with mock.patch("random.randint", return_value=2):
        loc_a, loc_b, _s1, _s2, game, bstate = run_combat(
            db, player_deck_has_enforcer=True)
    hand = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=103").fetchone()[0]
    assert hand == "hand", hand
    assert bstate["ai_health"] == 20, bstate


def test_deploy_never_fires_as_deathcry(db):
    """A Shamed Gladiator dying must NOT fire its Deploy (CardEnteredZone ->
    Warzone) trigger.  Only the death CardUpdated/CardMoved events are pushed —
    no extra CardUpdated from a phantom +0/+0 modifier."""
    from abilities.framework.kill_troop import kill_troop
    add_card(db, 101, 0, TPL_GLADIATOR)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    kill_troop(game, SessionStub(), db, HandlerStub(db), pl_t, ai_t, 101,
               bstate, cause="damage")
    updates = [ev for ev in game.events
               if ev.__class__.__name__ == "CardUpdatedSessionEventArgs"
               and int(ev.session_card_id.uid.uid64) == 101]
    moves = [ev for ev in game.events
             if ev.__class__.__name__ == "CardMovedSessionEventArgs"
             and int(ev.session_card_id.uid.uid64) == 101]
    assert len(updates) == 1, len(updates)
    assert len(moves) == 1, len(moves)
    assert updates[0].collection == game_engine.ECardCollections.Discard
    assert bstate["ai_health"] == 20 and bstate["player_health"] == 20


def test_threshold_count_two_ruby(db):
    """A [2,2] threshold is TWO Ruby — Burn to the Ground / Ragefire must not
    be playable with a single Ruby shard."""
    import hconnect_server as hcs
    assert not hcs.HCPHandler._thresholds_met('{"list": [2, 2]}', {8: 1})
    assert hcs.HCPHandler._thresholds_met('{"list": [2, 2]}', {8: 2})
    assert not hcs.HCPHandler._thresholds_met('{"list": [2, 2]}', {"8": 1})
    assert hcs.HCPHandler._thresholds_met('{"list": [1, 2]}', {4: 1, 8: 1})


def test_push_card_updated_gem_fallback(db):
    """push_card_updated renders the socketed gem from the CardDef when the
    caller omits gems= (the warzone-entry re-push paths), so Shamed Gladiator
    keeps its gem on the board."""
    from domain import game as gmod
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    g = gmod.Game(1, pl_t, ai_t)
    cid = game_engine.SessionCardId(game_engine.UID(101))
    g.card_defs[cid] = gmod.CardDef("Shamed Gladiator",
                                    game_engine.ECardTypes.Troop,
                                    2, 2, 2, [], [])
    g.card_defs[cid].gems = 5
    g.push_card_updated(cid, pl_t, game_engine.ECardCollections.Warzone,
                        game_engine.ECardTypes.Troop,
                        template_id=TPL_GLADIATOR)
    ev = g.events[-1]
    assert ev.gems == 5, ev.gems
    g.push_card_updated(cid, pl_t, game_engine.ECardCollections.Warzone,
                        game_engine.ECardTypes.Troop, gems=3)
    assert g.events[-1].gems == 3, g.events[-1].gems


def test_card_updated_carries_rage(db):
    """CardUpdated.rage drives the client's Rage icon and was never populated
    (always 0).  It must come from the CardDef (set by _card_full_data from
    effective_stats) so template Rage and gem-granted Rage both display."""
    from domain import game as gmod
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    g = gmod.Game(1, pl_t, ai_t)
    cid = game_engine.SessionCardId(game_engine.UID(101))
    g.card_defs[cid] = gmod.CardDef("Shamed Gladiator",
                                    game_engine.ECardTypes.Troop,
                                    2, 2, 2, [], [])
    g.card_defs[cid].rage = 2
    g.push_card_updated(cid, pl_t, game_engine.ECardCollections.Warzone,
                        game_engine.ECardTypes.Troop)
    assert g.events[-1].rage == 2, g.events[-1].rage
    # An explicit kwargs value wins over the CardDef.
    g.push_card_updated(cid, pl_t, game_engine.ECardCollections.Warzone,
                        game_engine.ECardTypes.Troop, rage=1)
    assert g.events[-1].rage == 1, g.events[-1].rage


class PromptHandlerStub(HandlerStub):
    """HandlerStub that records a deck-search picker instead of auto-picking."""

    def __init__(self, db):
        super().__init__(db)
        self.prompt_calls = []

    def _prompt_deck_search(self, game, session, pl_t, ai_t, bstate,
                            ability_guid, source_uid, owner_id, candidates):
        self.prompt_calls.append((ability_guid, int(source_uid), int(owner_id),
                                  [int(c) for c in candidates]))
        return "deck search: awaiting candidates"


def test_priestess_deathcry_human_picker(db):
    """A HUMAN-controlled Darkspire Priestess's Deathcry search branch must
    present every matching deck card for the player to choose — not auto-pick.
    The AI auto-picks (covered by test_priestess_deathcry_search_branch with a
    non-interactive stub).  Champions (e.g. Poca) are never candidates."""
    from abilities.framework.kill_troop import kill_troop
    add_card(db, 101, 5, TPL_PRIESTESS)
    add_card(db, 102, 5, TPL_ENFORCER, loc="deck")
    add_card(db, 103, 5, TPL_ENFORCER, loc="deck")
    db.execute(
        "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("4dc8bff7-17b3-4497-8bec-3d8537a30260", "Poca, The Conflagrater",
         "Troop", 3, 3, 3, 4, "[]", "[]", ""))
    db.execute(
        "INSERT INTO game_cards (session_id, user_id, card_uid, template_guid, "
        "card_template_id, location, position, card_state, card_abilities, "
        "card_type, card_attributes, is_champion, card_attack_mod, "
        "card_defense_mod, card_cost_mod, card_damage, permanent_buffs, "
        "temporary_buffs, card_uses, resolved_at, original_template_guid) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, 5, 104, "4dc8bff7-17b3-4497-8bec-3d8537a30260",
         "4dc8bff7-17b3-4497-8bec-3d8537a30260", "deck", 5, 0, "[]",
         "Troop", 4, 1, 0, 0, 0, 0, "{}", "{}", "{}", 0,
         "4dc8bff7-17b3-4497-8bec-3d8537a30260"))
    db.commit()
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = PromptHandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    with mock.patch("random.randint", return_value=2):
        kill_troop(game, SessionStub(), db, handler, pl_t, ai_t, 101,
                   bstate, cause="damage")
    assert handler.prompt_calls, "human picker was not presented"
    ag, src, owner, candidates = handler.prompt_calls[0]
    assert ag == AG_DEATHCRY and src == 101 and owner == 5
    assert sorted(candidates) == [102, 103], candidates
    assert 104 not in candidates, "champion leaked into card options"
    # Nothing auto-moved — the player still has both Darkspire troops in deck.
    for uid in (102, 103):
        loc = db.execute(
            "SELECT location FROM game_cards WHERE card_uid=?",
            (uid,)).fetchone()[0]
        assert loc == "deck", (uid, loc)


def test_poca_summons_blaze_elemental(db):
    """Poca's charge power ("Summon a Blaze Elemental") creates the token troop
    in the warzone — the SummonTokenTroop leaf reads the ability's game_text,
    so resolving_ability must reach it."""
    import abilities
    db.execute(
        "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("4bc00b6f-1baf-4962-85e8-e2de9e204037", "Blaze Elemental",
         "Troop", 3, 3, 1, 4, "[]", "[]", ""))
    db.execute(
        "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("3687a2ea-baf1-2dfe-25ca-b51cb1a95c28", 0, "", "[RUBY][RUBY]: [BASIC] "
         "[4] [ARROWR] Summon a <b>Blaze Elemental</b>.", "", 0, 0, 0, 0, 0,
         "[]", 0))
    db.execute(
        "INSERT INTO ability_effects (ability_guid, effect_guid, effect_order, effect_type, param) VALUES (?,?,?,?,?)",
        ("3687a2ea-baf1-2dfe-25ca-b51cb1a95c28",
         "1cc228e3-9222-7d6b-2334-6eb8366030f3", 0,
         "SummonTokenTroopAbilityEffectTemplate", ""))
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"resolving_ability": "3687a2ea-baf1-2dfe-25ca-b51cb1a95c28",
              "player_health": 20, "ai_health": 20, "turn_number": 1}
    fn = abilities.resolve_effect("3687a2ea-baf1-2dfe-25ca-b51cb1a95c28")
    out = fn(game, SessionStub(), db, HandlerStub(db), pl_t, ai_t, bstate,
             "3687a2ea-baf1-2dfe-25ca-b51cb1a95c28", None)
    assert "blaze elemental" in out.lower(), out
    row = db.execute(
        "SELECT template_guid, location FROM game_cards "
        "WHERE template_guid=? LIMIT 1",
        ("4bc00b6f-1baf-4962-85e8-e2de9e204037",)).fetchone()
    assert row and row[1] == "warzone", row
    uid64 = db.execute(
        "SELECT card_uid FROM game_cards WHERE template_guid=? LIMIT 1",
        ("4bc00b6f-1baf-4962-85e8-e2de9e204037",)).fetchone()[0]
    # The token must be a Card-type UID (low byte 1) — the old arithmetic
    # produced 12 (Champion), which made the client treat the Blaze Elemental
    # as a champion: it couldn't attack or leave the field visually.
    assert uid64 & 0xFF == 1, hex(uid64)


def test_speed_troop_can_attack_same_turn(db):
    """A troop with Speed may attack the turn it comes into play (client's
    HasSummoningSickness exempts Speed) — e.g. Poca's Blaze Elemental must be
    offered as an attacker right after it is summoned."""
    import hconnect_server as hcs
    import db as dbmod
    old_hcs_db = hcs._db
    old_db = dbmod._db
    hcs._db = db
    dbmod._db = db
    captured = []
    try:
        db.execute(
            "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (TPL_SPEED := "77777777-7777-7777-7777-777777777777",
             "Blaze Elemental", "Troop", 3, 3, 1,
             int(game_engine.ECardAttributes.Speed), "[]", "[]", ""))
        db.execute(
            "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (TPL_SICK := "66666666-6666-6666-6666-666666666666",
             "Summoning Sick", "Troop", 2, 2, 2, 0, "[]", "[]", ""))
        # Speed token that just entered play (CameOutThisTurn, no
        # StartedATurnOnYourSide) — must be offered as an attacker.
        add_card(db, 201, 5, TPL_SPEED,
                 state=game_engine.ECardStates.CameOutThisTurn)
        # Non-Speed troop that just entered play — summoning sick, NOT offered.
        add_card(db, 202, 5, TPL_SICK,
                 state=game_engine.ECardStates.CameOutThisTurn)
        # A ready troop that survived a turn — always offered.
        add_card(db, 203, 5, TPL_SICK,
                 state=game_engine.ECardStates.StartedATurnOnYourSide)
        h = object.__new__(hcs.HCPHandler)
        h.user_profile = {"id": 5}
        h._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        h._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))
        h._send_battle_events = lambda s, g, pl_t: captured.append(g)
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        h._push_attack_options(SessionStub(), pl_t, ai_t)
        offered = set()
        for ev in captured[-1].events:
            if ev.__class__.__name__ == "PlayerOptionListSessionEventArgs":
                for opt in ev.options:
                    offered.add(int(opt.card.uid.uid64))
        assert 201 in offered, offered   # Speed token attacks immediately
        assert 203 in offered, offered   # ready troop
        assert 202 not in offered, offered  # summoning-sick non-Speed
    finally:
        hcs._db = old_hcs_db
        dbmod._db = old_db


def test_ragefire_escalation_damage(db):
    """Ragefire ("Deal ESC:2 damage", Escalation) deals 2 to a targeted
    champion on the first cast, escalates the counter, then deals 4, and moves
    itself into the deck."""
    import abilities
    src = sqlite3.connect(SRC)
    for ag in ("3e29a0c9-f636-0d1f-a829-2c5bf2e4101b",
               "5e434cd5-22ca-93ad-3cc0-43c2ead48949"):
        meta = src.execute(
            "SELECT ability_guid, is_triggered, trigger_event_type, game_text, "
            "raw_json, casting_behavior, is_manual, activation_cost, "
            "uses_per_game, uses_per_turn, target_template_ids, exhausts_on_use "
            "FROM card_abilities_meta WHERE ability_guid=?", (ag,)).fetchone()
        db.execute(
            "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            meta)
        for eff in src.execute(
                "SELECT ability_guid, effect_guid, effect_order, effect_type, "
                "param FROM ability_effects WHERE ability_guid=?", (ag,)).fetchall():
            db.execute(
                "INSERT INTO ability_effects (ability_guid, effect_guid, effect_order, effect_type, param) VALUES (?,?,?,?,?)", eff)
    src.close()
    add_card(db, 101, 5, TPL_ENFORCER, loc="CastSpells")
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    handler = HandlerStub(db)
    ai_champ = game_engine.SessionCardId(game_engine.UID.make(3, 1000))
    handler._ai_champ_scid = ai_champ
    bstate = {"player_spell_target": int(ai_champ.uid.uid64),
              "resolving_source_uid": 101,
              "resolving_owner_id": 5,
              "player_health": 20, "ai_health": 20,
              "player_escalation_uses": 0, "turn_number": 1}
    game = game_engine.Game(1, pl_t, ai_t)
    abilities.resolve_played_spell(
        game, SessionStub(), db, handler, pl_t, ai_t, bstate,
        ["3e29a0c9-f636-0d1f-a829-2c5bf2e4101b",
         "5e434cd5-22ca-93ad-3cc0-43c2ead48949"])
    assert bstate["ai_health"] == 18, bstate
    assert bstate["player_escalation_uses"] == 1, bstate
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=101").fetchone()[0]
    assert loc == "deck", loc
    # Second cast escalates to 4 damage.
    add_card(db, 102, 5, TPL_ENFORCER, loc="CastSpells")
    bstate["resolving_source_uid"] = 102
    bstate["player_spell_target"] = int(ai_champ.uid.uid64)
    bstate["ai_health"] = 20
    abilities.resolve_played_spell(
        game, SessionStub(), db, handler, pl_t, ai_t, bstate,
        ["3e29a0c9-f636-0d1f-a829-2c5bf2e4101b",
         "5e434cd5-22ca-93ad-3cc0-43c2ead48949"])
    assert bstate["ai_health"] == 16, bstate
    assert bstate["player_escalation_uses"] == 2, bstate


def test_ai_discard_down_to_seven(db):
    """At end of turn the AI discards down to the max hand size (7)."""
    import ai as ai_mod
    old_db = ai_mod._db
    ai_mod._db = db
    try:
        for i in range(8):
            add_card(db, 300 + i, 0, TPL_ENFORCER, loc="hand")
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        game = game_engine.Game(1, pl_t, ai_t)
        handler = HandlerStub(db)
        count = db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE user_id=0 AND location='hand'"
        ).fetchone()[0]
        guard = 0
        while count > 7 and guard < 30:
            ai_mod.ai_discard_card(handler, game, SessionStub(), pl_t, ai_t)
            count = db.execute(
                "SELECT COUNT(*) FROM game_cards WHERE user_id=0 "
                "AND location='hand'").fetchone()[0]
            guard += 1
        assert count == 7, count
        assert guard <= 1, guard
    finally:
        ai_mod._db = old_db


def test_x_cost_detection_and_damage(db):
    """Burn to the Ground ("Deal X damage") is detected as a variable-X card,
    and the chosen X is paid and dealt as damage to the target."""
    import hconnect_server as hcs
    import db as dbmod
    old_hcs_db = hcs._db
    old_db = dbmod._db
    dbmod._db = db
    hcs._db = db
    try:
        src = sqlite3.connect(SRC)
        meta = src.execute(
            "SELECT ability_guid, is_triggered, trigger_event_type, game_text, "
            "raw_json, casting_behavior, is_manual, activation_cost, "
            "uses_per_game, uses_per_turn, target_template_ids, exhausts_on_use "
            "FROM card_abilities_meta WHERE ability_guid=?",
            ("58f59723-1a20-b17c-a5db-013e249d4970",)).fetchone()
        src.close()
        db.execute(
            "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            meta)
        db.execute(
            "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype, variable_cost, variable_cost_minimum) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("0fe0fcf5-1054-463c-ab01-a1b5743d66b9", "Burn to the Ground",
             "BasicAction", 1, 0, 0, 0,
             json.dumps(["58f59723-1a20-b17c-a5db-013e249d4970"]), "[]", "", 1, 0))
        db.execute(
            "INSERT INTO ability_effects (ability_guid, effect_guid, effect_order, effect_type, param) VALUES (?,?,?,?,?)",
            ("58f59723-1a20-b17c-a5db-013e249d4970",
             "b7767275-3b18-b063-b6e8-4c29628ea377", 0,
             "CardModifierAbilityEffectTemplate",
             '{"text": "Deal X damage to target champion or troop.", '
             '"property": "damage", "amount": 0}'))
        assert hcs.HCPHandler._template_has_x_cost(
            "0fe0fcf5-1054-463c-ab01-a1b5743d66b9")
        assert not hcs.HCPHandler._template_has_x_cost(TPL_ENFORCER)
        # X=4 against the AI champion: deals 4.
        add_card(db, 101, 5, "0fe0fcf5-1054-463c-ab01-a1b5743d66b9",
                 loc="CastSpells")
        import abilities
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        handler = HandlerStub(db)
        ai_champ = game_engine.SessionCardId(game_engine.UID.make(3, 1000))
        handler._ai_champ_scid = ai_champ
        bstate = {"player_spell_target": int(ai_champ.uid.uid64),
                  "resolving_source_uid": 101, "resolving_owner_id": 5,
                  "x_cost": 4, "player_health": 20, "ai_health": 20,
                  "turn_number": 1}
        game = game_engine.Game(1, pl_t, ai_t)
        abilities.resolve_played_spell(
            game, SessionStub(), db, handler, pl_t, ai_t, bstate,
            ["58f59723-1a20-b17c-a5db-013e249d4970"])
        assert bstate["ai_health"] == 16, bstate
        # The transient Game must reflect the new health too, or the
        # PlayerUpdated pushed in the same resolution would flicker the
        # champion display back to the pre-damage value.
        assert game.ai_health == 16, game.ai_health
    finally:
        dbmod._db = old_db
        hcs._db = old_hcs_db


def test_deck_search_prompt_target_id(db):
    """The deck-search class-39 prompt's TargetInstance must carry a real
    AbilityTargetTemplate id (the deathcry's target_template_ids), not the
    ability GUID — otherwise the client can't render the candidate list and the
    answer submits the source card instead of a Darkspire deck card."""
    import hconnect_server as hcs
    old = hcs._db
    hcs._db = db
    try:
        src = sqlite3.connect(SRC)
        for ag in ("7361162f-ed16-fc00-16dd-22517295282b",
                   "cb21843f-2eae-6bd0-e47d-d13b6006e4af",
                   "37955055-a070-b24e-ada8-ad94e04c1e39"):
            meta = src.execute(
                "SELECT ability_guid, is_triggered, trigger_event_type, "
                "game_text, raw_json, casting_behavior, is_manual, "
                "activation_cost, uses_per_game, uses_per_turn, "
                "target_template_ids, exhausts_on_use "
                "FROM card_abilities_meta WHERE ability_guid=?", (ag,)).fetchone()
            db.execute(
                "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                meta)
        src.close()
        add_card(db, 102, 5, TPL_ENFORCER, loc="deck")
        add_card(db, 103, 5, TPL_ENFORCER, loc="deck")
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        h = object.__new__(hcs.HCPHandler)
        h.user_profile = {"id": 5}
        def _full_data(game, scid, template_guid, instance_id=None):
            game.card_defs[scid] = game_engine.CardDef(
                "Darkspire Enforcer", game_engine.ECardTypes.Troop,
                2, 3, 2, [], [])
            return (template_guid, "Troop", "Darkspire Enforcer", 2, 3, 2, 0)
        h._card_full_data = _full_data
        bstate = {"_next_instance_id": 1, "turn_number": 1}
        game = game_engine.Game(1, pl_t, ai_t)
        h._prompt_deck_search(game, SessionStub(), pl_t, ai_t, bstate,
                              AG_DEATHCRY, 101, 5, [102, 103])
        # The prompt must reference the nested SEARCH ability (37955055), not
        # the top deathcry — the client resolves targets against the prompt
        # ability's own target template.
        prompt_abilities = [str(ev.ability_template_ids[0].guid)
                            for ev in game.events
                            if ev.__class__.__name__ ==
                            "TriggeredAbilityActivationDataRequiredSessionEventArgs"]
        assert "37955055-a070-b24e-ada8-ad94e04c1e39" in prompt_abilities, prompt_abilities
        assert AG_DEATHCRY not in prompt_abilities, prompt_abilities
        target_ids = []
        for ev in game.events:
            if ev.__class__.__name__ == "PlayerOptionListSessionEventArgs":
                for opt in ev.options:
                    for inst in opt.instances:
                        for tgt in inst.target_instances:
                            target_ids.append(str(tgt.target_id.guid))
        assert target_ids, "no TargetInstance in the prompt"
        # The search's own target template (Choosing collection + Darkspire
        # filter), NOT the deathcry's "You" template.
        assert "0ad94887-419c-9e99-7946-74c4f72cdd2e" in target_ids, target_ids
        assert "eb7e48cd-1c85-813f-6635-d43f50cf7809" not in target_ids, target_ids
        option_target_ids = [str(tid.guid)
                             for ev in game.events
                             if ev.__class__.__name__ ==
                             "PlayerOptionListSessionEventArgs"
                             for opt in ev.options
                             for inst in opt.instances
                             for tid in inst.target_ids]
        assert "0ad94887-419c-9e99-7946-74c4f72cdd2e" in option_target_ids, \
            option_target_ids
        # Candidates are presented temporarily in the Choosing zone, matching
        # the client's normal deck-search presentation.
        presented = [ev for ev in game.events
                     if ev.__class__.__name__ in (
                         "CardUpdatedSessionEventArgs",
                         "CardMovedSessionEventArgs")
                     and ev.collection == game_engine.ECardCollections.Choosing]
        assert len(presented) == 4, len(presented)  # 2 candidates x move+update
        moved_uids = {int(ev.session_card_id.uid.uid64)
                      for ev in game.events
                      if ev.__class__.__name__ == "CardMovedSessionEventArgs"
                      and ev.collection == game_engine.ECardCollections.Choosing}
        assert moved_uids == {102, 103}, moved_uids
    finally:
        hcs._db = old


def test_gem_abilities_resolved(db):
    """Socketed gems resolve to granted abilities data-driven from the
    gem_templates table at deck save time — gem 5 (Blood Minor 1, "Minor Blood
    Orb of Hatred") grants Rage 1 in all zones (ddb205d9)."""
    import hconnect_server as hcs
    old = hcs._db
    hcs._db = db
    try:
        db.execute(
            "INSERT INTO gem_templates VALUES (?,?,?,?)",
            (5, "Blood_Minor_1", "Minor Blood Orb of Hatred",
             json.dumps(["ddb205d9-03e0-0a63-adee-d52035ab5b0c"])))
        db.execute(
            "INSERT INTO gem_templates VALUES (?,?,?,?)",
            (34, "Herofall_Blood_Minor_2", "Minor Blood Orb of Frenzy",
             json.dumps(["a0806fbd-b6d8-e49f-145f-89833e2d7c49"])))
        h = object.__new__(hcs.HCPHandler)
        out = h._resolve_gem_abilities(
            {"6515": 5, "6516": 0, "6517": "5", "6518": 34})
        assert out == {"6515": ["ddb205d9-03e0-0a63-adee-d52035ab5b0c"],
                       "6517": ["ddb205d9-03e0-0a63-adee-d52035ab5b0c"],
                       "6518": ["a0806fbd-b6d8-e49f-145f-89833e2d7c49"]}, out
    finally:
        hcs._db = old


def test_gem_rage_applies_as_static(db):
    """A socketed Shamed Gladiator (gem 5 = Minor Blood Orb of Hatred) gets
    Rage 1 — the gem ability's IntAttrModifier gamedata fields drive the
    statics layer (no game-text parsing)."""
    import hconnect_server as hcs
    import db as dbmod
    from abilities.framework.statics import effective_stats
    old_db = dbmod._db
    old_hcs_db = hcs._db
    dbmod._db = db
    hcs._db = db
    try:
        src = sqlite3.connect(SRC)
        meta = src.execute(
            "SELECT ability_guid, is_triggered, trigger_event_type, game_text, "
            "raw_json, casting_behavior, is_manual, activation_cost, "
            "uses_per_game, uses_per_turn, target_template_ids, exhausts_on_use "
            "FROM card_abilities_meta WHERE ability_guid=?",
            ("ddb205d9-03e0-0a63-adee-d52035ab5b0c",)).fetchone()
        eff = src.execute(
            "SELECT ability_guid, effect_guid, effect_order, effect_type, "
            "param FROM ability_effects WHERE ability_guid=?",
            ("ddb205d9-03e0-0a63-adee-d52035ab5b0c",)).fetchone()
        src.close()
        db.execute(
            "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            meta)
        db.execute(
            "INSERT INTO ability_effects (ability_guid, effect_guid, effect_order, effect_type, param) VALUES (?,?,?,?,?)", eff)
        # Shamed Gladiator with the gem ability in its ability list (the
        # deck-save bake) + the corrected base RageValue 0 from the template.
        # make_db already seeds TPL_GLADIATOR — add the gem ability and keep
        # the corrected base RageValue 0 on that row.
        db.execute(
            "UPDATE card_templates SET abilities_json=?, rage_value=0 "
            "WHERE guid=?",
            (json.dumps([AG_DEPLOY,
                         "ddb205d9-03e0-0a63-adee-d52035ab5b0c"]),
             TPL_GLADIATOR))
        add_card(db, 101, 5, TPL_GLADIATOR,
                 state=game_engine.ECardStates.StartedATurnOnYourSide)
        db.execute(
            "UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
            (json.dumps([AG_DEPLOY,
                         "ddb205d9-03e0-0a63-adee-d52035ab5b0c"]),))
        db.commit()
        bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
        _a, _d, _attrs, _flags, rage = effective_stats(db, 1, bstate, 101)
        assert rage == 1, rage  # gem Rage 1 + base RageValue 0
        assert _attrs & game_engine.ECardAttributes.Rage, hex(_attrs)
    finally:
        dbmod._db = old_db
        hcs._db = old_hcs_db


def test_champion_zero_health_state_check(db):
    """A champion at 0 health ends the game at ANY phase — the AI damaging
    itself on its own turn must not leave a dead champion alive."""
    import hconnect_server as hcs
    import db as dbmod
    from unittest import mock
    old_db = dbmod._db
    old_hcs_db = hcs._db
    dbmod._db = db
    hcs._db = db
    try:
        h = object.__new__(hcs.HCPHandler)
        h.user_profile = {"id": 5}
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        with mock.patch("commands.push_battle_game_end") as pbe:
            ended = h._check_champion_health(
                SessionStub(), pl_t, ai_t,
                {"player_health": 20, "ai_health": 0})
            assert ended and pbe.called
            ended2 = h._check_champion_health(
                SessionStub(), pl_t, ai_t,
                {"player_health": 0, "ai_health": 5})
            assert ended2 and pbe.call_count == 2
            ended3 = h._check_champion_health(
                SessionStub(), pl_t, ai_t,
                {"player_health": 5, "ai_health": 5})
            assert not ended3 and pbe.call_count == 2
    finally:
        dbmod._db = old_db
        hcs._db = old_hcs_db


def test_authoritative_resolution_deathcry(db):
    """The authoritative resolver walks the Darkspire Priestess deathcry BOM
    data-driven: the RandomizeVariable leaf sets RandomNumber, and the
    conditioned ActivateAbility branches gate on it — roll 1 damages the
    opposing champion, roll 2 resolves the deck search with the mapped target."""
    from abilities.framework.resolution import resolve_ability
    import db as dbmod
    old_db = dbmod._db
    dbmod._db = db
    try:
        # The fixture's make_db() already seeds the full deathcry BOM chain
        # (meta, effect rows with group/condition/target metadata, and the
        # RandomNumber conditions) straight from the live gamedata.
        add_card(db, 101, 5, TPL_ENFORCER, loc="deck")
        add_card(db, 102, 5, TPL_ENFORCER, loc="deck")
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        handler = HandlerStub(db)
        ai_champ = game_engine.SessionCardId(game_engine.UID.make(3, 1000))
        handler._ai_champ_scid = ai_champ
        # Roll 1 -> the damage branch: 3 to each opposing champion.
        bstate = {"player_health": 20, "ai_health": 20,
                  "turn_number": 1, "resolving_owner_id": 5,
                  "resolving_source_uid": 200}
        game = game_engine.Game(1, pl_t, ai_t)
        with mock.patch("random.randint", return_value=1):
            resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t,
                            bstate, AG_DEATHCRY, 200, 5, {})
        assert bstate["ai_health"] == 17, bstate
        # Roll 2 -> the search branch: move the mapped deck card to hand.
        bstate = {"player_health": 20, "ai_health": 20,
                  "turn_number": 1, "resolving_owner_id": 5,
                  "resolving_source_uid": 200}
        game = game_engine.Game(1, pl_t, ai_t)
        with mock.patch("random.randint", return_value=2):
            resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t,
                            bstate, AG_DEATHCRY, 200, 5, {0: 102})
        loc = db.execute(
            "SELECT location FROM game_cards WHERE card_uid=102").fetchone()[0]
        assert loc == "hand", loc
    finally:
        dbmod._db = old_db


def test_ai_x_kills_target(db):
    """The AI's variable-X spell pays the LARGEST X that kills the chosen
    target (capped by resources): a 3-defense player troop with 5 resources
    and a 1-cost Burn to the Ground -> X=3 kills it."""
    import ai as ai_mod
    import hconnect_server as hcs
    import db as dbmod
    old_db = dbmod._db
    old_ai_db = ai_mod._db
    old_hcs_db = hcs._db
    dbmod._db = db
    ai_mod._db = db
    hcs._db = db
    try:
        db.execute(
            "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("58f59723-1a20-b17c-a5db-013e249d4970", 0, "",
             "Deal X damage to target champion or troop.", "", 0, 0, 0, 0, 0,
             "[]", 0))
        db.execute(
            "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype, variable_cost, variable_cost_minimum) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("0fe0fcf5-1054-463c-ab01-a1b5743d66b9", "Burn to the Ground",
             "BasicAction", 1, 0, 0, 0,
             json.dumps(["58f59723-1a20-b17c-a5db-013e249d4970"]), "[]", "", 1, 0))
        db.execute(
            "INSERT INTO ability_effects (ability_guid, effect_guid, effect_order, effect_type, param) VALUES (?,?,?,?,?)",
            ("58f59723-1a20-b17c-a5db-013e249d4970",
             "b7767275-3b18-b063-b6e8-4c29628ea377", 0,
             "CardModifierAbilityEffectTemplate",
             '{"text": "Deal X damage to target champion or troop.", '
             '"property": "damage", "amount": 0}'))
        add_card(db, 101, 0, "0fe0fcf5-1054-463c-ab01-a1b5743d66b9",
                 loc="hand")
        add_card(db, 102, 5, TPL_ENFORCER, state=16384)
        db.execute(
            "UPDATE game_cards SET card_attack_mod=1, card_defense_mod=1 "
            "WHERE card_uid=102")
        db.commit()
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        handler = HandlerStub(db)
        handler.client_reck_id = 5
        handler._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        battle_state = {"ai_resources": 5, "ai_threshold": {},
                        "player_health": 20, "player_threshold": {},
                        "turn_number": 1}
        game = game_engine.Game(1, pl_t, ai_t)
        ai_mod.ai_play_spell(handler, game, SessionStub(), ai_t, battle_state)
        # The spell now sits on the chain (CastSpells) until the player passes;
        # drain it the way the server's stack resolution does (the AI auto-passes,
        # the human passed, so both sides count as passed).
        import battle_engine as _be
        _be.stack_set_pass(battle_state, _be.PLAYER, True)
        _be.stack_set_pass(battle_state, _be.AI, True)
        while not _be.stack_empty(battle_state):
            item = _be.stack_pop(battle_state)
            _be.stack_reset_passes(battle_state)
            bstate = battle_state
            bstate["player_spell_target"] = item.get("target_uid")
            bstate["resolving_source_uid"] = item.get("source_uid")
            bstate["resolving_owner_id"] = 0
            bstate["x_cost"] = int(item.get("x_cost") or 0)
            from abilities import resolve_played_spell
            resolve_played_spell(game, SessionStub(), db, handler, pl_t, ai_t,
                                 bstate, item.get("ability_guids", []))
            bstate.pop("player_spell_target", None)
            bstate.pop("resolving_source_uid", None)
            bstate.pop("resolving_owner_id", None)
            bstate.pop("x_cost", None)
        # X=3 kills the 3-defense player troop (base 2/2 +1/+1); 1 + X paid.
        row = db.execute(
            "SELECT location, card_state FROM game_cards WHERE card_uid=102"
        ).fetchone()
        assert row[0] == "discard" and (row[1] & game_engine.ECardStates.Dead), row
        assert battle_state["ai_resources"] == 1, battle_state
    finally:
        dbmod._db = old_db
        ai_mod._db = old_ai_db
        hcs._db = old_hcs_db


def test_swiftstrike_kills_before_normal_damage(db):
    """Emberspire Witch (FirstStrike 2/2) vs a 3/2: the Swiftstrike step kills
    the 3/2, so the normal step has no blocker left to deal its 3 — Emberspire
    survives undamaged instead of trading."""
    db.execute(
        "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TPL_WITCH := "ecc1fc8b-a86c-4330-908a-e15ba445f2f0",
         "Emberspire Witch", "Troop", 2, 2, 2,
         int(game_engine.ECardAttributes.FirstStrike), "[]", "[]", ""))
    db.execute(
        "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TPL_BIG := "55555555-5555-5555-5555-555555555555",
         "Big Troop", "Troop", 3, 3, 2, 0, "[]", "[]", ""))
    add_card(db, 101, 0, TPL_WITCH)
    add_card(db, 102, 5, TPL_BIG)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    bstate = {"player_health": 20, "ai_health": 20,
              "player_max_health": 20, "ai_max_health": 20,
              "turn_number": 1}
    attackers = {101: 0}
    blockers = {101: [102]}
    bstate["ai_attackers"] = {"101": "0"}
    bstate["ai_blockers"] = {"101": ["102"]}
    handler = HandlerStub(db)
    handler._send_battle_events = lambda *args, **kwargs: None
    ai._db = db
    def _noop(game, p, a, bstate2):
        pass
    # Swiftstrike step: Emberspire (FirstStrike) kills the 3/2.
    # Exercise the same wrapper used by the AI turn driver; it must preserve
    # the first_strike flag when delegating to the shared resolver.
    ai.resolve_ai_combat_damage(
        handler, SessionStub(), pl_t, ai_t, bstate, first_strike=True)
    b_loc = db.execute(
        "SELECT location, card_state FROM game_cards WHERE card_uid=102"
    ).fetchone()
    assert b_loc[0] == "discard" and (b_loc[1] & game_engine.ECardStates.Dead), b_loc
    # Normal step: the dead blocker deals nothing; Emberspire survives clean.
    ai.resolve_ai_combat_damage(
        handler, SessionStub(), pl_t, ai_t, bstate, first_strike=False)
    a_loc = db.execute(
        "SELECT location, card_damage FROM game_cards WHERE card_uid=101"
    ).fetchone()
    assert a_loc[0] == "warzone" and a_loc[1] == 0, a_loc


def test_ai_attacks_zero_attack_rage_troop_when_unblocked(db):
    """A ready 0-attack troop with printed Rage must still attack into an
    empty opposing warzone so its Rage trigger can apply."""
    tpl = "88888888-8888-8888-8888-888888888888"
    db.execute(
        "INSERT INTO card_templates (guid, name, card_type, cost, attack, "
        "defense, attributes, abilities_json, threshold_json, subtype, "
        "rage_value) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tpl, "Mazat Spearman", "Troop", 1, 0, 1, 0, "[]", "[]",
         "Orc Ranger", 1))
    add_card(db, 101, 0, tpl, state=game_engine.ECardStates.StartedATurnOnYourSide)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    handler = HandlerStub(db)
    game = game_engine.Game(1, pl_t, ai_t)
    ai._db = db

    ai.ai_declare_attackers(handler, game, SessionStub(), ai_t, pl_t, bstate)

    assert bstate["ai_attackers"] == {"101": str(int(handler._player_champ_scid.uid.uid64))}, bstate
    row = db.execute(
        "SELECT card_state, permanent_buffs FROM game_cards WHERE card_uid=101"
    ).fetchone()
    assert row[0] & game_engine.ECardStates.Attacking, row
    assert json.loads(row[1]).get("atk") == 1, row


def test_player_can_block_excludes_cantblock(db):
    """Pile of Bones (CantAttack|CantBlock, and any "can't block" granted via
    card_attributes / temporary_attributes) must never be offered as a blocker
    — the client only lets troops block when the server grants them a Defend
    usage, and _player_can_block gates the whole DeclareDefense stop."""
    import hconnect_server as hcs
    import db as dbmod
    old_db = dbmod._db
    old_hcs_db = hcs._db
    dbmod._db = db
    hcs._db = db
    try:
        db.execute(
            "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("99999999-9999-9999-9999-999999999999", "Pile of Bones",
             "Troop", 2, 0, 1,
             int(game_engine.ECardAttributes.CantAttack |
                 game_engine.ECardAttributes.CantBlock), "[]", "[]", ""))
        db.execute(
            "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("88888888-8888-8888-8888-888888888888", "Bone Warrior",
             "Troop", 2, 1, 2, 0, "[]", "[]", ""))
        h = object.__new__(hcs.HCPHandler)
        h.user_profile = {"id": 5}
        # Only a CantBlock troop -> no legal blockers.
        add_card(db, 201, 5, "99999999-9999-9999-9999-999999999999",
                 state=game_engine.ECardStates.StartedATurnOnYourSide)
        assert not h._player_can_block(SessionStub())
        # A legal blocker appears -> the stop opens (and the CantBlock troop
        # stays out of the option list via the same combined-attributes mask).
        add_card(db, 202, 5, "88888888-8888-8888-8888-888888888888",
                 state=game_engine.ECardStates.StartedATurnOnYourSide)
        assert h._player_can_block(SessionStub())
        rows = db.execute(
            "SELECT gc.card_uid FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.user_id=5 AND gc.location='warzone' "
            "AND gc.card_type='Troop' AND (gc.card_state & ?) = 0 "
            "AND (ct.attributes | gc.card_attributes | "
            "COALESCE(gc.temporary_attributes, 0)) & ? = 0",
            (1, game_engine.ECardStates.Tapped,
             game_engine.ECardAttributes.CantBlock)).fetchall()
        assert [r[0] for r in rows] == [202], rows
    finally:
        dbmod._db = old_db
        hcs._db = old_hcs_db


def test_transform_bom_returns_string(db):
    """Pile of Bones's manual "transform into a Bone Warrior" ability resolves
    without the 'sequence item 0: expected str instance, int found' crash —
    every BOM leaf logs a string even when transform_card returns a card_uid."""
    import db as dbmod
    from abilities.framework.bom import _LEAFS
    import abilities
    db.execute(
        "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TPL_PILE := "c72e6441-6717-4bbf-91f1-3fd6707d165d", "Pile of Bones",
         "Troop", 2, 0, 1,
         int(game_engine.ECardAttributes.CantAttack |
             game_engine.ECardAttributes.CantBlock),
         json.dumps(["4d7b43dd-0a42-be5b-b998-ce8030501e6c"]), "[]", ""))
    db.execute(
        "INSERT INTO card_templates (guid, name, card_type, cost, attack, defense, attributes, abilities_json, threshold_json, subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TPL_BONE := "f4822b71-b1f5-448d-a7e6-b7caf5f11374", "Bone Warrior",
         "Troop", 2, 1, 2, 0, "[]", "[]", ""))
    src = sqlite3.connect(SRC)
    meta = src.execute(
        "SELECT ability_guid, is_triggered, trigger_event_type, game_text, "
        "raw_json, casting_behavior, is_manual, activation_cost, "
        "uses_per_game, uses_per_turn, target_template_ids, exhausts_on_use "
        "FROM card_abilities_meta WHERE ability_guid=?",
        ("4d7b43dd-0a42-be5b-b998-ce8030501e6c",)).fetchone()
    src.close()
    db.execute(
        "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        meta)
    db.execute(
        "INSERT INTO ability_effects (ability_guid, effect_guid, effect_order, effect_type, param) VALUES (?,?,?,?,?)",
        ("4d7b43dd-0a42-be5b-b998-ce8030501e6c",
         "4003a91f-ef1c-fc54-b93c-e359f7199be9", 0,
         "TransformCardAbilityEffectTemplate", ""))
    add_card(db, 101, 5, TPL_PILE,
             state=game_engine.ECardStates.StartedATurnOnYourSide)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"resolving_ability": "4d7b43dd-0a42-be5b-b998-ce8030501e6c",
              "resolving_source_uid": 101,
              "player_health": 20, "ai_health": 20, "turn_number": 1}
    old_db = dbmod._db
    dbmod._db = db
    try:
        fn = abilities.resolve_effect("4d7b43dd-0a42-be5b-b998-ce8030501e6c")
        out = fn(game, SessionStub(), db, HandlerStub(db), pl_t, ai_t,
                 bstate, "4d7b43dd-0a42-be5b-b998-ce8030501e6c", None)
        assert isinstance(out, str), out
        assert "transformed" in out.lower(), out
    finally:
        dbmod._db = old_db
    row = db.execute(
        "SELECT template_guid, location FROM game_cards WHERE card_uid=101"
    ).fetchone()
    assert row[0] == TPL_BONE and row[1] == "warzone", row
    # The leaf itself also returns a string now.
    assert isinstance(_LEAFS["TransformCardAbilityEffectTemplate"](
        game, SessionStub(), db, HandlerStub(db), pl_t, ai_t,
        bstate, "e", None), str)


def main():
    tests = [
        ("Gladiator vs Priestess both die", test_gladiator_priestess_both_die),
        ("Priestess Deathcry search branch", test_priestess_deathcry_search_branch),
        ("Deploy never fires as Deathcry", test_deploy_never_fires_as_deathcry),
        ("Two-Ruby threshold counts", test_threshold_count_two_ruby),
        ("CardUpdated gem fallback", test_push_card_updated_gem_fallback),
        ("CardUpdated carries Rage", test_card_updated_carries_rage),
        ("Priestess Deathcry human picker", test_priestess_deathcry_human_picker),
        ("Blocker options exclude CantBlock", test_player_can_block_excludes_cantblock),
        ("Transform BOM returns string", test_transform_bom_returns_string),
        ("Poca summons Blaze Elemental", test_poca_summons_blaze_elemental),
        ("Speed troop attacks same turn", test_speed_troop_can_attack_same_turn),
        ("Ragefire escalation damage", test_ragefire_escalation_damage),
        ("AI discards to 7", test_ai_discard_down_to_seven),
        ("X-cost detection + damage", test_x_cost_detection_and_damage),
        ("Deck-search prompt target id", test_deck_search_prompt_target_id),
        ("Gem abilities resolved at save", test_gem_abilities_resolved),
        ("Gem Rage applies as static", test_gem_rage_applies_as_static),
        ("Champion zero-health state check", test_champion_zero_health_state_check),
        ("Authoritative deathcry resolution", test_authoritative_resolution_deathcry),
        ("AI X kills target", test_ai_x_kills_target),
        ("Swiftstrike kills before normal damage", test_swiftstrike_kills_before_normal_damage),
        ("AI attacks unblocked zero-attack Rage troop", test_ai_attacks_zero_attack_rage_troop_when_unblocked),
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
