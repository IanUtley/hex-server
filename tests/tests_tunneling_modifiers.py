#!/usr/bin/env python3
"""Tunneling modifiers start from the printed value (Tectonic Megahulk).

Excavation Bot's power tunnels a random Robot and gives it Tunneling +4 only
if it does not already have Tunneling.  Tectonic Megahulk prints Tunneling 15
in its CardTemplate TAC and gets "Tunneling -1 for each Dwarf and/or Robot you
control" at the start of its controller's turn.  It used to surface on the
next turn: the filter missed the printed value (so +4 applied, from 0), and
the -1 then started from that 4.

    python3 tests/tests_tunneling_modifiers.py
"""

import contextlib
import io
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests"),
                os.path.join(ROOT, "campaign")]

import sqlite3  # noqa: E402

import tests_card_play_matrix as matrix  # noqa: E402
from rules_port.context import EffectContext  # noqa: E402
from rules_port.resolution import resolve_port_ability  # noqa: E402
from rules_port.triggers import dispatch_native_trigger  # noqa: E402
from rules_port.tunneling import advance, queue_surfaces, tunneling_value  # noqa: E402

EXCAVATION_BOT = "25ce3c67-3b47-4f64-a3c0-66eb30486b6f"
TUNNEL_A_ROBOT = "5ff7edb6-454d-cb1a-287f-087b5b430f98"
CHARGE_BOT = "7325706e-6bf1-4ca4-8d6b-5da13ac069f4"
# The alternate-art template's TAC starts with BaseVersionGuid.
MEGAHULK = "0c7a4e38-25ab-45e4-9d6b-faefea971b89"
HULK = 20001


def _seat(db, robots):
    db.execute("DELETE FROM game_cards")
    abilities = [row[0] for row in db.execute(
        "SELECT ability_guid FROM champion_abilities WHERE champion_guid=?",
        (EXCAVATION_BOT,))]
    db.execute(
        "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,card_template_id,"
        "location,position,card_state,card_abilities,card_type,card_attributes,card_attack_mod,"
        "card_defense_mod,card_cost_mod,card_damage,permanent_buffs,temporary_buffs,card_uses,"
        "resolved_at,original_template_guid,is_champion) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (matrix.SESSION_ID, matrix.AI_OWNER, matrix.AI_CHAMP, EXCAVATION_BOT, EXCAVATION_BOT,
         "warzone", 0, 0, json.dumps(abilities), "Champion", 0, 0, 0, 0, 0, "{}", "{}", "{}",
         0, EXCAVATION_BOT, 1))
    matrix._insert_card(db, HULK, matrix.AI_OWNER, "hand", MEGAHULK, 0)
    for index in range(robots):
        matrix._insert_card(db, 20100 + index, matrix.AI_OWNER, "warzone", CHARGE_BOT, index)
    db.commit()


def _hulk(db):
    buffs = json.loads(db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE card_uid=?", (HULK,)).fetchone()[0] or "{}")
    attrs = buffs.get("int_attrs", {})
    return (int(buffs.get("counters", {}).get("tunneling", 0)),
            tunneling_value(db, MEGAHULK, attrs), attrs)


def _run(robots, max_turns=20):
    """Return [(turn, counter, threshold)] up to and including the surface."""
    db = sqlite3.connect(matrix.SRC)
    db.row_factory = sqlite3.Row
    _seat(db, robots)
    handler = matrix.HandlerStub(db)
    session = matrix.SessionStub()
    state = matrix._new_state(handler)
    state["turn_player"] = "ai"
    state["resolving_owner_id"] = matrix.AI_OWNER
    state["resolving_source_uid"] = matrix.AI_CHAMP
    with contextlib.redirect_stdout(io.StringIO()):
        game = handler._fresh_game(session, matrix.PLAYER_UID, matrix.AI_UID, state)
        resolve_port_ability(handler, game, session, db, matrix.PLAYER_UID, matrix.AI_UID,
                             state, TUNNEL_A_ROBOT, matrix.AI_CHAMP, matrix.AI_OWNER,
                             target_map={})
        matrix._drain_triggers(handler, game, session, db, state)
    counter, threshold, attrs = _hulk(db)
    assert threshold == 15 and "Tunneling" not in attrs, \
        f"Megahulk already has Tunneling 15; the +4 must not apply: {attrs}"
    assert counter == 2, f"Excavation Bot adds two counters, got {counter}"
    history = []
    for turn in range(1, max_turns + 1):
        with contextlib.redirect_stdout(io.StringIO()):
            game = handler._fresh_game(session, matrix.PLAYER_UID, matrix.AI_UID, state)
            context = EffectContext.from_rules_port(
                game, session, db, handler, matrix.PLAYER_UID, matrix.AI_UID, state, "",
                ability=None)
            advance(context, matrix.AI_OWNER)
            dispatch_native_trigger(
                db=db, handler=handler, game=game, session=session,
                player_uid=matrix.PLAYER_UID, ai_uid=matrix.AI_UID, battle_state=state,
                event_type="TurnStartedEvent", source_card_id=matrix.AI_CHAMP,
                source_player_id=matrix.AI_OWNER)
            matrix._drain_triggers(handler, game, session, db, state)
            counter, threshold, _attrs = _hulk(db)
            surfaced = queue_surfaces(context, matrix.AI_OWNER)
        history.append((turn, counter, threshold))
        if surfaced:
            return history
    return history


def test_megahulk_tunneling_counts_down_from_printed_value():
    # One Robot: counter 2+t meets 15-t at turn 7 (9 >= 8), not turn 1.
    history = _run(robots=1)
    assert history[0] == (1, 3, 14), history[0]
    assert history[-1] == (7, 9, 8), history
    assert all(counter < threshold for _turn, counter, threshold in history[:-1]), history


def test_megahulk_reduced_below_zero_surfaces():
    # C# surfaces once counters >= Tunneling, including a value pushed <= 0.
    history = _run(robots=20)
    assert history == [(1, 3, 0)], history


if __name__ == "__main__":
    test_megahulk_tunneling_counts_down_from_printed_value()
    test_megahulk_reduced_below_zero_surfaces()
    print("ok")
