"""Regression: an AI Deploy with an optional (min 0) crypt target must not
target its own source when no legal target exists.

Moldmass: "Deploy - Void up to three troops in your crypt; this gets +1/+1
for each troop voided this way."  With an empty crypt the AI's Moldmass voided
itself because the native trigger dispatcher defaulted the input target to the
event source (the entering card) when the target template is not explicit.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

SRC = fresh_database()

import game_engine
from tests.tests_combat import make_db, add_card, HandlerStub, SessionStub  # noqa: F401

TPL_MOLDMASS = "49a2b136-6ff9-4d2e-9a0d-ea8bfc566410"
AG_MOLDMASS_DEPLOY = "9c5d3811-2b38-974f-7e1d-d930a261c4ec"


def test_ai_moldmass_deploy_with_empty_crypt_does_not_void_itself(db):
    from rules_port.resolution import resolve_port_trigger
    from rules_port.triggers import dispatch_native_trigger

    add_card(db, 500, 0, TPL_MOLDMASS, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=500",
               (json.dumps([AG_MOLDMASS_DEPLOY]),))
    db.commit()
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 3,
              "_rules_port_attached": True, "stack": [],
              "_next_instance_id": 1}
    handler._current_bstate = bstate

    dispatch_native_trigger(
        db=db, handler=handler, game=game, session=SessionStub(),
        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
        event_type="CardEnteredZoneEvent", source_card_id=500,
        source_player_id=0,
        data={"event_source_collection": "CastSpells",
              "event_destination_collection": "warzone"})
    items = [item for item in (bstate.get("stack") or [])
             if item.get("ability_guid") == AG_MOLDMASS_DEPLOY]
    for item in items:
        bstate["stack"].remove(item)
        assert item.get("target_uid") != 500, item
        resolve_port_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                             bstate, item)

    location = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=500").fetchone()[0]
    assert location == "warzone", location


if __name__ == "__main__":
    import db as dbmod
    connection = make_db()
    dbmod._db = connection
    test_ai_moldmass_deploy_with_empty_crypt_does_not_void_itself(connection)
    print("PASS")
