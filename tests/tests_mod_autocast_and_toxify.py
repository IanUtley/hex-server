#!/usr/bin/env python3
"""Champion IntAttr HUD projection and encounter-mod auto-cast.

1. A champion IntAttr set by an effect (Killarvae's "that champion becomes
   Toxified") is published as a champion ``CardUpdated`` carrying the
   attribute: Unity's champion HUD (``GoChampionEffects``) reads its state
   icons from the cached champion card, which only a CardUpdated stores.
2. A card an encounter mod's granted ability creates for a hand (Canyon
   Heat Wave's "create a Heat Wave and put it into your hand") keeps its
   authored Hand destination, so the player can play it.

    python3 tests/tests_mod_autocast_and_toxify.py
"""

import contextlib
import io
import os
import sqlite3
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests"),
                os.path.join(ROOT, "campaign")]

import tests_card_play_matrix as matrix  # noqa: E402
import game_engine  # noqa: E402
from application.turn_flow import TurnFlowMixin  # noqa: E402
from rules_port.resolution import resolve_port_ability  # noqa: E402

# Ability/card GUIDs are fixture data (the authored Records rows under test).
TOXIFY_ON_DAMAGE = "02ec2ec0-ecae-7260-2f9b-8449379a82db"
GRANTED_HEAT_WAVE = "eedf7920-c753-5ab9-22a6-a9cb5e83bade"
HEAT_WAVE = "4e449c8a-15ab-4de9-b877-06827491eb21"
PLAYER_CHAMP_GUID = "ba5b0a6f-e6d1-4d0b-9d0c-cd0b1c9bd9b2"


class _FixedRng:
    """Session RNG double: always rolls the minimum (RandomNumber = 1)."""

    def next(self, _span):
        return 0


def _fixture():
    db = sqlite3.connect(matrix.SRC)
    db.row_factory = sqlite3.Row
    db.execute("DELETE FROM game_cards")
    db.commit()
    handler = matrix.HandlerStub(db)
    handler._push_champions_warm = types.MethodType(
        TurnFlowMixin._push_champions_warm, handler)
    handler._player_champ_guid = PLAYER_CHAMP_GUID
    session = matrix.SessionStub()
    state = matrix._new_state(handler)
    return db, handler, session, state


def _resolve(handler, session, db, state, guid, source, owner, target_map):
    game = handler._fresh_game(session, matrix.PLAYER_UID, matrix.AI_UID, state)
    with contextlib.redirect_stdout(io.StringIO()):
        resolve_port_ability(
            handler, game, session, db, matrix.PLAYER_UID, matrix.AI_UID,
            state, guid, source, owner, target_map=target_map)
    return game


def test_toxified_champion_is_published_on_the_champion_card():
    db, handler, session, state = _fixture()
    state["resolving_owner_id"] = matrix.AI_OWNER
    game = _resolve(handler, session, db, state, TOXIFY_ON_DAMAGE, None,
                    matrix.AI_OWNER, {0: [matrix.PLAYER_CHAMP]})
    assert state["champion_int_attrs"][str(matrix.PLAYER_CHAMP)] == {"Toxified": 1}
    updates = [event for event in game.events
               if isinstance(event, game_engine.CardUpdatedSessionEventArgs) and
               int(event.session_card_id.uid.uid64) == matrix.PLAYER_CHAMP]
    assert updates, "no champion CardUpdated carried the Toxified IntAttr"
    event = updates[-1]
    assert event.int_attrs.get("Toxified") == 1, event.int_attrs
    # Same collection the champion already has: not a move, not suppressed.
    assert event.collection == game_engine.ECardCollections.None_
    assert event.card_type == game_engine.ECardTypes.Champion
    # The CardUpdated replaces the whole cached card, so it must keep the
    # champion's template and abilities.
    assert PLAYER_CHAMP_GUID in str(event.card_id).lower(), event.card_id
    game.make_network_packet(matrix.PLAYER_UID)  # serializes


def _troop(db):
    guid = matrix._template_guid(db, "Troop", require_empty_abilities=True)
    uid = matrix.CARD_UID_BASE + 1
    matrix._insert_card(db, uid, matrix.AI_OWNER, "warzone", guid, 0)
    db.commit()
    return uid


def _created_heat_waves(db):
    return db.execute(
        "SELECT card_uid, user_id, location FROM game_cards "
        "WHERE lower(template_guid)=?", (HEAT_WAVE,)).fetchall()


def _run_granted_heat_wave(db, handler, session, state):
    state["_rules_rng"] = _FixedRng()
    state["resolving_owner_id"] = matrix.PLAYER_OWNER
    return _resolve(handler, session, db, state, GRANTED_HEAT_WAVE,
                    matrix.PLAYER_CHAMP, matrix.PLAYER_OWNER,
                    {0: [matrix.PLAYER_CHAMP]})


def test_granted_heat_wave_is_created_in_hand():
    db, handler, session, state = _fixture()
    troop = _troop(db)
    _run_granted_heat_wave(db, handler, session, state)
    created = _created_heat_waves(db)
    assert len(created) == 1, created
    assert created[0]["location"] == "hand", dict(created[0])
    row = db.execute("SELECT card_damage FROM game_cards WHERE card_uid=?",
                     (troop,)).fetchone()
    assert int(row["card_damage"] or 0) == 0


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
