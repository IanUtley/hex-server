"""The AI uses its summon charge power when it is ahead on board.

Regression: with its charges and thresholds met, a summon power (Corrupt Dryad's
two Seed Pods) was skipped whenever the AI had more Warzone troops than the
player, so the AI never spent its charges.  The token's evaluator value now
decides in that case.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()

import game_engine  # noqa: E402

from tests.tests_combat import HandlerStub, SessionStub, add_card, make_db  # noqa: E402

CORRUPT_DRYAD = "89035517-4af7-4277-937e-35abcb2ff64b"
DRYAD_POWER = "d40a5c02-f26b-d568-b3c1-e7a3007b337d"
SEED_POD = "82bfcf06-bf67-4231-b4be-9df9da09a620"
PLAIN_TPL = "11111111-1111-1111-1111-111111111111"


class _Handler(HandlerStub):
    _resolving_ai_champion = False

    def _champion_thresholds_met(self, ability_guid, bstate):
        return True

    def _champion_targets(self):
        return []


def test_ai_summons_with_its_charge_power_when_ahead_on_board(db):
    import ai

    ai_champ = int(game_engine.UID.make(1, 513).uid64)
    handler = _Handler(db)
    handler._ai_champ_scid = game_engine.SessionCardId(game_engine.UID(ai_champ))
    handler._ai_champ_ability_guids = [DRYAD_POWER]
    handler._ai_champ_guid = CORRUPT_DRYAD
    # The live player's UID is a raw value, not an owner-encoded UID.
    pl_t = game_engine.UID(492848739333673204)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    # Four AI troops against one player troop: well ahead on board, so the
    # count gate alone would not spend the charge.
    for uid in (101, 102, 103, 104):
        add_card(db, uid, 0, PLAIN_TPL, "warzone")
    add_card(db, 201, 5, PLAIN_TPL, "warzone")
    bstate = {
        "ai_charges": 4, "ai_spell_points": 0, "ai_health": 9,
        "player_health": 20,
        "ai_threshold": {4: 1, 32: 4}, "player_threshold": {4: 3, 32: 2},
        "_rules_port_attached": True,
    }
    session = SessionStub()
    decision = ai.ai_use_champion_ability(
        handler, game, session, ai_t, pl_t, bstate, decision_only=True)
    assert decision and decision["ability_guid"] == DRYAD_POWER, decision
