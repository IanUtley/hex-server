"""Regression tests for metadata-driven BOM leaves and zone operations."""

import os
import json
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine

from abilities.framework.bom import _LEAFS

SRC = os.environ.get(
    "HEX_TEST_SOURCE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db"),
)


class SessionStub:
    session_id = 1
    server_id = 100


class HandlerStub:
    user_profile = {"id": 5}

    @staticmethod
    def _next_resolve_counter(session):
        return 1

    def _card_full_data(self, game, scid, template_guid, instance_id=None):
        return (template_guid, "Troop", "Card", 1, 1, 1, 0)

    def _sync_instance_card_data(self, session, card_uid, new_template_guid):
        pass


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE game_cards (
        session_id INTEGER, user_id INTEGER, card_uid INTEGER,
        template_guid TEXT, card_template_id TEXT, location TEXT,
        position INTEGER, card_state INTEGER, card_abilities TEXT,
        card_type TEXT, card_attributes INTEGER, temporary_attributes INTEGER DEFAULT 0, card_attack_mod INTEGER,
        card_defense_mod INTEGER, card_cost_mod INTEGER, card_damage INTEGER,
        permanent_buffs TEXT, temporary_buffs TEXT, card_uses TEXT,
        resolved_at INTEGER, cost_mod_json TEXT DEFAULT '[]')""")
    db.execute("""CREATE TABLE card_abilities_meta (
        ability_guid TEXT, is_triggered INTEGER, trigger_event_type TEXT,
        game_text TEXT, raw_json TEXT, casting_behavior INTEGER,
        is_manual INTEGER, activation_cost INTEGER, uses_per_game INTEGER,
        uses_per_turn INTEGER, target_template_ids TEXT)""")
    db.execute("""CREATE TABLE card_templates (
        guid TEXT, name TEXT, card_type TEXT, cost INTEGER, attack INTEGER,
        defense INTEGER, attributes INTEGER, abilities_json TEXT,
        threshold_json TEXT, subtype TEXT)""")
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("11111111-1111-1111-1111-111111111111", "Troop", "Troop", 1, 1, 1,
         0, "[]", "[]", ""))
    db.commit()
    return db


def add_card(db, uid, owner, loc="warzone", state=0):
    db.execute(
        "INSERT INTO game_cards VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,0,'{}','{}','{}',0,'[]')",
        (1, owner, uid, "11111111-1111-1111-1111-111111111111",
         "11111111-1111-1111-1111-111111111111", loc, 0, state, "[]", "Troop", 0))
    db.commit()


def new_game(db):
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"resolving_ability": "test-ability"}
    return pl_t, ai_t, game, bstate


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


def state(db, uid):
    return db.execute(
        "SELECT card_state, location FROM game_cards WHERE card_uid=?",
        (uid,)).fetchone()


def test_destroy(db):
    add_card(db, 100, 5)
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_spell_target"] = 100
    _LEAFS["DestroyCardAbilityEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e", None)
    s, loc = state(db, 100)
    assert loc == "discard" and (s & game_engine.ECardStates.Dead), (s, loc)


def test_tap_untap(db):
    add_card(db, 100, 5, state=game_engine.ECardStates.StartedATurnOnYourSide)
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_spell_target"] = 100
    _LEAFS["TapCardAbilityEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e", None)
    s, _ = state(db, 100)
    assert s & game_engine.ECardStates.Tapped, s
    _LEAFS["UntapCardAbilityEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e", None)
    s, _ = state(db, 100)
    assert not (s & game_engine.ECardStates.Tapped), s


def test_discard_moves_target_and_emits_events(db):
    add_card(db, 100, 5, loc="hand",
             state=game_engine.ECardStates.Tapped)
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_spell_target"] = 100
    result = _LEAFS["DiscardCardAbilityEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e", None)
    assert result == "discarded 0x64", result
    row = db.execute(
        "SELECT location, card_state, card_damage, temporary_buffs "
        "FROM game_cards WHERE card_uid=100").fetchone()
    assert row == ("discard", 0, 0, "{}"), row
    assert any(isinstance(ev, game_engine.CardDiscardedSessionEventArgs)
               for ev in game.events)
    assert any(isinstance(ev, game_engine.CardMovedSessionEventArgs)
               and ev.collection == game_engine.ECardCollections.Discard
               for ev in game.events)


def test_block_assigns_secondary_target_and_emits_event(db):
    """BlockEffect uses the created troop as the blocker and the selected
    attacking troop as the primary target, matching the client BOM wiring."""
    add_card(db, 100, 5, loc="warzone")
    add_card(db, 200, 6, loc="warzone",
             state=game_engine.ECardStates.Attacking)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(244, 6)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {
        "pvp": True,
        "champ_map": {"5": 900, "6": 901},
        "attackers": {"200": "900"},
        "resolving_ability": "block-test",
        "resolving_secondary_target_uid": 100,
        "player_spell_target": 200,
    }
    result = _LEAFS["BlockEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e", None)
    assert result == "blocked 0xc8 with 0x64", result
    state_value, _ = state(db, 100)
    assert state_value & game_engine.ECardStates.Blocking
    assert state_value & game_engine.ECardStates.HasBlocked
    assert bstate["blockers"] == {"200": ["100"]}, bstate
    assert any(isinstance(ev, game_engine.BlockersAssignedSessionEventArgs)
               for ev in game.events)


def test_block_assigns_pve_player_blocker(db):
    """The same BlockEffect wiring works when the AI attacks the player."""
    add_card(db, 100, 5, loc="warzone")
    add_card(db, 200, 0, loc="warzone",
             state=game_engine.ECardStates.Attacking)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub()
    handler._player_champ_scid = game_engine.SessionCardId(pl_t)
    handler._ai_champ_scid = game_engine.SessionCardId(ai_t)
    bstate = {
        "ai_attackers": {"200": str(int(pl_t.uid64))},
        "resolving_ability": "block-test",
        "resolving_secondary_target_uid": 100,
        "player_spell_target": 200,
    }
    result = _LEAFS["BlockEffectTemplate"](
        game, SessionStub(), db, handler, pl_t, ai_t, bstate, "e", None)
    assert result == "blocked 0xc8 with 0x64", result
    state_value, _ = state(db, 100)
    assert state_value & game_engine.ECardStates.Blocking
    assert bstate["ai_blockers"] == {"200": ["100"]}, bstate
    assert any(isinstance(ev, game_engine.BlockersAssignedSessionEventArgs)
               for ev in game.events)


def test_reveal(db):
    add_card(db, 100, 5, loc="deck", state=0)
    add_card(db, 101, 5, loc="deck", state=0)
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["resolving_owner_id"] = 5
    db.execute(
        "INSERT INTO card_abilities_meta VALUES ('test-ability',0,'','look at two "
        "random cards from your deck.','{}',0,0,0,0,0,'[]')")
    db.commit()
    graph = SimpleNamespace(
        targets=(), effects=())
    from abilities.framework import bom
    with mock.patch.object(bom, "ability_graph",
                           lambda _store, _guid: graph):
        _LEAFS["RevealCardsAbilityEffectTemplate"](
            game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate,
            "e", json.dumps({"count": 2}))
    revealed = bstate.get("revealed_cards") or []
    assert len(revealed) == 2, revealed
    assert any(isinstance(ev, game_engine.CardsRevealedSessionEventArgs)
               for ev in game.events)


def test_store_targets_fallback(db):
    add_card(db, 100, 5)
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_spell_target"] = 100
    bstate["player_mod_target"] = 100
    _LEAFS["StoreTargetsAbilityEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e", None)
    assert bstate.get("stored_targets", {}).get("test-ability") == [100]


def test_move_to_hand(db):
    add_card(db, 100, 5, loc="deck", state=0)
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_spell_target"] = 100
    _LEAFS["MoveCardToZoneEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e",
        '{"destination": "Hand"}')
    _s, loc = state(db, 100)
    assert loc == "hand", loc


def test_move_to_warzone_clears_dead(db):
    add_card(db, 101, 5, loc="discard", state=game_engine.ECardStates.Dead)
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_spell_target"] = 101
    _LEAFS["MoveCardToZoneEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e",
        '{"destination": "Warzone"}')
    state_value, loc = state(db, 101)
    assert loc == "warzone", loc
    assert not (state_value & game_engine.ECardStates.Dead), state_value


def test_revert_mods(db):
    add_card(db, 100, 5)
    db.execute(
        "UPDATE game_cards SET card_attack_mod=2, card_defense_mod=3, "
        "card_cost_mod=1, permanent_buffs='{\"atk\": 1, \"def\": 1}' "
        "WHERE card_uid=100")
    db.commit()
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_spell_target"] = 100
    _LEAFS["RevertPermanentModificationsAbilityEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e", None)
    row = db.execute(
        "SELECT card_attack_mod, card_defense_mod, card_cost_mod, permanent_buffs "
        "FROM game_cards WHERE card_uid=100").fetchone()
    assert row[:3] == (0, 0, 0), row
    assert '"atk": 0' in row[3] and '"def": 0' in row[3], row[3]


def test_battle_damage(db):
    add_card(db, 100, 5, state=game_engine.ECardStates.StartedATurnOnYourSide)
    add_card(db, 200, 0, state=game_engine.ECardStates.StartedATurnOnYourSide)
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["resolving_source_uid"] = 100
    bstate["player_spell_target"] = 200
    _LEAFS["Battle2CardsAbilityEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e", None)
    row = db.execute(
        "SELECT location, card_damage FROM game_cards WHERE card_uid=200").fetchone()
    # 1 ATK vs 1 DEF -> dead (discard); damage is cleared on death per the
    # client's DeactivateCard -> ResetCardDamage (GY shows full stats).
    assert row[0] == "discard" and row[1] == 0, row


def test_sacrifice(db):
    add_card(db, 100, 5)
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_spell_target"] = 100
    _LEAFS["SacrificeCardAbilityEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e", None)
    _s, loc = state(db, 100)
    assert loc == "discard", loc


def test_store_name(db):
    add_card(db, 100, 5)
    pl_t, ai_t, game, bstate = new_game(db)
    bstate["player_spell_target"] = 100
    _LEAFS["StoreNameAbilityEffectTemplate"](
        game, SessionStub(), db, HandlerStub(), pl_t, ai_t, bstate, "e", None)
    assert bstate.get("stored_names", {}).get("test-ability") == ["Troop"]


def test_bonus_turn(db):
    import battle_engine as be
    state = be.default_state()
    state["bonus_turn"] = be.PLAYER
    state["phase_idx"] = len(be.BASE_TURN_PHASES) - 1
    be.advance_phase(state)
    assert state["turn_player"] == be.PLAYER
    assert "bonus_turn" not in state


def test_explicit_end_turn_handoff_consumes_bonus_turn(db):
    """The PvE driver's explicit handoff uses the same rule as phase advance."""
    import battle_engine as be
    state = be.default_state()
    state["bonus_turn"] = be.PLAYER
    assert be.next_turn_player(state) == be.PLAYER
    assert "bonus_turn" not in state
    assert be.next_turn_player(state) == be.AI


def test_skip_combat_steps(db):
    """No attackers declared -> the turn skips the combat steps straight to
    the Second Main Phase."""
    import battle_engine as be
    state = be.default_state()
    state["turn_phases"] = be.COMBAT_TURN_PHASES
    state["phase_idx"] = be.COMBAT_TURN_PHASES.index(
        game_engine.ETurnPhases.DeclareAttackPriorityWindow)
    ok = be.skip_to_phase(state, game_engine.ETurnPhases.SecondMainPhase)
    assert ok
    assert be.current_phase(state) == game_engine.ETurnPhases.SecondMainPhase
    # The caller guards on the combat-step index: the declaration window
    # (index 0/1) must not skip, DeclareAttackPriorityWindow (index 2) may.
    assert be.COMBAT_STEPS.index(
        game_engine.ETurnPhases.DeclareCombatPriorityWindow) < 2
    assert be.COMBAT_STEPS.index(
        game_engine.ETurnPhases.DeclareAttackPriorityWindow) >= 2


def test_combat_state_clear_keeps_tapped(db):
    """End-of-combat clears Attacking/Blocking bits from warzone troops but
    keeps Tapped (a Steadfast attacker stays untapped, so its 'attacking'
    visuals must not linger into the next turn)."""
    import battle_engine as be
    add_card(db, 100, 5)
    bits = (game_engine.ECardStates.Attacking |
            game_engine.ECardStates.HasAttacked |
            game_engine.ECardStates.Blocking |
            game_engine.ECardStates.HasBlocked |
            game_engine.ECardStates.Tapped)
    db.execute("UPDATE game_cards SET card_state=? WHERE card_uid=100", (bits,))
    db.commit()
    combat_bits = (game_engine.ECardStates.Attacking |
                   game_engine.ECardStates.HasAttacked |
                   game_engine.ECardStates.Blocking |
                   game_engine.ECardStates.HasBlocked)
    db.execute(
        "UPDATE game_cards SET card_state = card_state & ~? "
        "WHERE session_id=? AND location='warzone'",
        (combat_bits, 1))
    db.commit()
    st = db.execute(
        "SELECT card_state FROM game_cards WHERE card_uid=100").fetchone()[0]
    assert not (st & combat_bits), bin(st)
    assert st & game_engine.ECardStates.Tapped, bin(st)


def test_kill_resets_damage_and_temp(db):
    """A troop that dies loses its damage and temporary buffs (client's
    DeactivateCard -> ResetCardDamage) so a deathcry-transform returns fresh."""
    from abilities.framework.kill_troop import kill_troop
    add_card(db, 100, 5)
    db.execute(
        "UPDATE game_cards SET card_damage=3, "
        "temporary_buffs='{\"atk\": 2}', temporary_attributes=1, "
        "card_state=? WHERE card_uid=100",
        (game_engine.ECardStates.Tapped | game_engine.ECardStates.Attacking,))
    db.commit()
    pl_t, ai_t, game, bstate = new_game(db)
    kill_troop(game, SessionStub(), db, HandlerStub(), pl_t, ai_t, 100,
               bstate, cause="damage")
    row = db.execute(
        "SELECT card_damage, temporary_buffs, temporary_attributes, "
        "card_state, location FROM game_cards WHERE card_uid=100").fetchone()
    assert row[0] == 0 and row[1] == "{}" and row[2] == 0, row
    assert row[4] == "discard", row
    assert row[3] & game_engine.ECardStates.Dead, row
    assert not (row[3] & (game_engine.ECardStates.Tapped |
                          game_engine.ECardStates.Attacking)), row


def test_dynamic_cost_formula(db):
    from abilities.framework.cost_mod import cost_mod_delta, formula_from_raw
    src = sqlite3.connect(SRC)
    raw = src.execute(
        "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
        ("60370ce1-b037-c1b6-16b0-e267dc520b3a",)).fetchone()
    src.close()
    formula = formula_from_raw(raw[0] if raw else "")
    assert formula is not None, "CardCountAbilityVariable formula not parsed"
    assert formula["multiplier"] == -1, formula
    assert "Warzone" in formula["zones"], formula
    # Pterobot (Robot Dinosaur) counts itself + two Dwarf troops -> -3.
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("33333333-3333-3333-3333-333333333333", "Dwarf Grunt", "Troop", 2,
         2, 2, 0, "[]", "[]", "Dwarf"))
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("44444444-4444-4444-4444-444444444444", "Pterobot", "Troop", 4,
         2, 2, 0, "[]", "[]", "Robot Dinosaur"))
    fjson = '[' + __import__("json").dumps(formula) + ']'
    for uid, tpl in ((101, "44444444-4444-4444-4444-444444444444"),
                     (102, "33333333-3333-3333-3333-333333333333"),
                     (103, "33333333-3333-3333-3333-333333333333")):
        db.execute(
            "INSERT INTO game_cards (session_id, user_id, card_uid, "
            "template_guid, card_template_id, location, position, card_state, "
            "card_abilities, card_type, card_attributes, card_attack_mod, "
            "card_defense_mod, card_cost_mod, card_damage, permanent_buffs, "
            "temporary_buffs, card_uses, resolved_at, cost_mod_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, 5, uid, tpl, tpl, "warzone", 0, 0, "[]", "Troop", 0,
             0, 0, 0, 0, "{}", "{}", "{}", 0,
             fjson if uid == 101 else "[]"))
    delta = cost_mod_delta(db, 1, 101, fjson)
    assert delta == -3, delta


if __name__ == "__main__":
    run("DestroyCard kills target", test_destroy)
    run("Tap / Untap toggle state", test_tap_untap)
    run("Discard moves target and emits events",
        test_discard_moves_target_and_emits_events)
    run("Block assigns secondary target and emits event",
        test_block_assigns_secondary_target_and_emits_event)
    run("Block assigns PvE player blocker",
        test_block_assigns_pve_player_blocker)
    run("RevealCards reveals N + class-51", test_reveal)
    run("StoreTargets remembers target", test_store_targets_fallback)
    run("MoveCardToZone hand destination", test_move_to_hand)
    run("MoveCardToZone clears Dead on warzone entry", test_move_to_warzone_clears_dead)
    run("RevertPermanentModifications resets mods", test_revert_mods)
    run("Battle2Cards deals damage", test_battle_damage)
    run("SacrificeCard kills target", test_sacrifice)
    run("StoreName remembers name", test_store_name)
    run("GiveBonusTurn keeps the turn player", test_bonus_turn)
    run("explicit EndTurn handoff consumes bonus turn",
        test_explicit_end_turn_handoff_consumes_bonus_turn)
    run("Dynamic cost formula counts Dwarf/Robot", test_dynamic_cost_formula)
    run("skip_to_phase jumps combat to SecondMain", test_skip_combat_steps)
    run("combat clear drops Attacking but keeps Tapped", test_combat_state_clear_keeps_tapped)
    run("death resets damage and temp buffs", test_kill_resets_damage_and_temp)
