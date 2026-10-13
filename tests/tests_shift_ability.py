#!/usr/bin/env python3
"""Shift moves a power from its card onto the chosen troop.

A chain-resolved Shift (the RulesPort transaction path) carries only the
ability source and its typed target; it never sets the legacy
``player_shift_*`` aliases.  Gemsoul Feeder's "Shift [1]: Lifedrain" onto a
Deepgaze Acolyte must move the Lifedrain power, project SpiritDrain on the
Acolyte (not the Feeder), and queue the Acolyte's PowerShifted trigger.

    python3 tests/tests_shift_ability.py
"""

import contextlib
import io
import json
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import tests_card_play_matrix as matrix  # noqa: E402
import game_engine  # noqa: E402
from rules_port.resolution import resolve_port_ability  # noqa: E402
from rules_port.static_rules import effective_attributes  # noqa: E402

GEMSOUL_FEEDER = "7071ddd7-b81b-46b2-8682-9f391ab3f12e"
DEEPGAZE_ACOLYTE = "669eb23c-868f-4df7-b6b3-328f6dc3eb32"
SHIFT_LIFEDRAIN = "2af60616-96ee-743d-33b8-cdb4c3d9f0a5"
LIFEDRAIN_POWER = "44605164-fbfc-d2f7-433a-ebf79d35adff"
POWER_SHIFTED_TRIGGER = "33a8da86-7d0f-1305-9587-66dd0f9b4ed0"
SPIRIT_DRAIN = int(game_engine.ECardAttributes.SpiritDrain)


def _abilities(db, uid):
    row = db.execute("SELECT card_abilities FROM game_cards WHERE card_uid=?",
                     (uid,)).fetchone()
    return [str(guid).lower() for guid in json.loads(row[0] or "[]")]


def test_chain_shift_moves_lifedrain_onto_target():
    db = sqlite3.connect(matrix.SRC)
    matrix._seed_board(db, GEMSOUL_FEEDER)
    source = matrix._raw_uid(matrix.CARD_UID_BASE + 40)
    target = matrix._raw_uid(matrix.CARD_UID_BASE + 41)
    matrix._insert_card(db, source, matrix.PLAYER_OWNER, "warzone", GEMSOUL_FEEDER, 5)
    matrix._insert_card(db, target, matrix.PLAYER_OWNER, "warzone", DEEPGAZE_ACOLYTE, 6)
    db.commit()
    handler = matrix.HandlerStub(db)
    session = matrix.SessionStub()
    state = matrix._new_state(handler)
    assert effective_attributes(db, session.session_id, state, source) & SPIRIT_DRAIN

    with contextlib.redirect_stdout(io.StringIO()):
        game = handler._fresh_game(session, matrix.PLAYER_UID, matrix.AI_UID, state)
        resolve_port_ability(
            handler, game, session, db, matrix.PLAYER_UID, matrix.AI_UID, state,
            SHIFT_LIFEDRAIN, source_uid=source, owner_id=matrix.PLAYER_OWNER,
            target_map={0: (int(target),)})

    assert LIFEDRAIN_POWER not in _abilities(db, source), "the Feeder kept the power"
    assert LIFEDRAIN_POWER in _abilities(db, target), "the Acolyte did not get the power"
    assert not effective_attributes(db, session.session_id, state, source) & SPIRIT_DRAIN
    assert effective_attributes(db, session.session_id, state, target) & SPIRIT_DRAIN
    queued = [item.get("ability_guid") for item in state.get("stack", [])]
    assert POWER_SHIFTED_TRIGGER in queued, f"PowerShifted trigger not queued: {queued}"


if __name__ == "__main__":
    test_chain_shift_moves_lifedrain_onto_target()
    print("PASS test_chain_shift_moves_lifedrain_onto_target")
