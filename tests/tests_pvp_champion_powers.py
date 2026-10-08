#!/usr/bin/env python3
"""Focused regressions from the PvP champion-power audit.

Covers the shared layers behind the audit fixes rather than one test per
champion: granted EntersPlayExhausted, exhausted summons fed by a paid void
cost, "until the start of your next turn" stat lifetimes, and the PvP effect
view keeping duration ledgers.

    python3 tests/tests_pvp_champion_powers.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

# Bind this process's test database before any runtime import opens ``db``.
SRC = fresh_database()

import game_engine

from tests.tests_cards_fixes import _copy_ability, _copy_card
from tests.tests_combat import make_db, add_card, HandlerStub, SessionStub
from tests.tests_chain_abilities import _pl_ai


TPL_BOT = "02ed9695-207a-4c23-a3f8-13c7b001203d"          # S.P.A.M. Bot 1/1
TPL_ABOMINATION = "c776499e-53c1-4526-9be4-acba62050d06"
AG_BUNJITSU = "32d0d36a-55fd-2cff-0d3d-341319536a57"
AG_FIONA = "084fc899-740d-5ef1-e1e3-c44104b82c4f"


def _bstate():
    return {"player_health": 20, "ai_health": 20, "turn_number": 3,
            "stack": [], "_rules_port_attached": True}


def test_granted_enters_play_exhausted_taps_the_entering_card(db):
    """Storm Cloud grants "This enters play exhausted" to deck troops; the
    instance attribute (not only the printed one) must tap it on entry."""
    from rules_port.static_rules import card_enters_play_exhausted
    _copy_card(db, TPL_BOT)
    add_card(db, 0x301, 5, TPL_BOT, loc="hand")
    db.commit()
    assert not card_enters_play_exhausted(db, 1, _bstate(), 0x301, 5, TPL_BOT)
    db.execute("UPDATE game_cards SET card_attributes=card_attributes | ? "
               "WHERE card_uid=?",
               (int(game_engine.ECardAttributes.EntersPlayExhausted), 0x301))
    db.commit()
    assert card_enters_play_exhausted(db, 1, _bstate(), 0x301, 5, TPL_BOT)


def test_bunjitsu_summons_an_exhausted_abomination_from_the_voided_stats(db):
    """"Void two ready troops ... Summon an exhausted Abomination. It gets
    +[ATK]/+[DEF] equal to the voided troop's plus 3."  The summon ignored
    m_EntersPlayExhausted, and the VoidedCards list was lost between the
    PvP cost payment and resolution, leaving a ready 3/3."""
    from rules_port.resolution import resolve_port_ability
    for tpl in (TPL_ABOMINATION, TPL_BOT):
        _copy_card(db, tpl)
    add_card(db, 0x301, 5, TPL_BOT, loc="void")
    add_card(db, 0x501, 5, TPL_BOT, loc="void")
    db.commit()
    pl_t, ai_t = _pl_ai()
    resolve_port_ability(
        HandlerStub(db), game_engine.Game(1, pl_t, ai_t), SessionStub(), db,
        pl_t, ai_t, _bstate(), AG_BUNJITSU, 0x244, 5, target_map={},
        cost_target_map={"0": [0x301, 0x501]}, instance_id=1)
    state, buffs = db.execute(
        "SELECT card_state, permanent_buffs FROM game_cards "
        "WHERE template_guid=?", (TPL_ABOMINATION,)).fetchone()
    assert state & int(game_engine.ECardStates.Tapped), state
    assert json.loads(buffs) == {"atk": 5, "def": 5}, buffs


def test_until_your_next_turn_debuff_outlives_the_end_of_turn(db):
    """Fiona Honeyfinch: "-1[ATK] until the start of your next turn" was a
    this-turn buff wiped at end of turn, before the opponent attacked."""
    from rules_port.resolution import resolve_port_ability
    from rules_port.lifecycle import clear_expired_temporary_attributes
    _copy_card(db, TPL_BOT)
    _copy_ability(db, AG_FIONA)
    add_card(db, 0x301, 0, TPL_BOT)          # opposing troop without Flight
    db.commit()
    pl_t, ai_t = _pl_ai()
    bstate = _bstate()
    resolve_port_ability(
        HandlerStub(db), game_engine.Game(1, pl_t, ai_t), SessionStub(), db,
        pl_t, ai_t, bstate, AG_FIONA, 0x244, 5, target_map={}, instance_id=1)

    def attack_buff():
        raw = db.execute("SELECT permanent_buffs, temporary_buffs FROM "
                         "game_cards WHERE card_uid=?", (0x301,)).fetchone()
        return sum(int(json.loads(value or "{}").get("atk", 0) or 0)
                   for value in raw)

    assert attack_buff() == -1
    clear_expired_temporary_attributes(db, 1, 5, "end_turn",
                                       clear_stat_buffs=True,
                                       battle_state=bstate)
    clear_expired_temporary_attributes(db, 1, 0, "start_turn",
                                       clear_stat_buffs=True,
                                       battle_state=bstate)
    assert attack_buff() == -1, "expired before the opponent's turn ended"
    clear_expired_temporary_attributes(db, 1, 5, "start_turn",
                                       clear_stat_buffs=True,
                                       battle_state=bstate)
    assert attack_buff() == 0


def test_pvp_effect_view_keeps_duration_ledgers(_db):
    """A grant or champion IntAttr recorded while resolving in the PvP view
    must reach the checkpoint (Raven Talon's "this turn" grant; Succubus
    crashed appending to a dict-aliased champion ledger)."""
    from rules_port.pvp_view import apply_effect_view, to_effect_view
    from rules_port.effect_lifetimes import (champion_grants, record_grant,
                                             record_temporary_intattr)
    state = {"pvp": True, "pids": [1001, 1002],
             "temporary_champion_int_attrs": {}}
    view = to_effect_view(state, 1001, 1002)
    record_grant(view, source_uid=9, target_uid=9, ability_guid="ABC",
                 duration="EndOfTurn", owner_id=1001)
    record_temporary_intattr(view, 9, "Stealth", 0, 1, champion=True,
                             duration="EndOfTurn", owner_id=1001)
    apply_effect_view(state, view, 1001, 1002)
    assert champion_grants(state, 9) == ("abc",)
    assert state["temporary_champion_int_attrs"][0]["attribute"] == "Stealth"


def _main():
    tests = (test_granted_enters_play_exhausted_taps_the_entering_card,
             test_bunjitsu_summons_an_exhausted_abomination_from_the_voided_stats,
             test_until_your_next_turn_debuff_outlives_the_end_of_turn,
             test_pvp_effect_view_keeps_duration_ledgers)
    failed = 0
    for fn in tests:
        db = make_db()
        try:
            fn(db)
            print("PASS", fn.__name__)
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _main()
