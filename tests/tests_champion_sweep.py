"""Resolve every distinct champion ability through the production path.

This is a catalog smoke test, not a card-by-card rules oracle.  It catches
metadata abilities that cannot be built, targeted, resolved, or serialized in
the same disposable database used by the normal test runner.  Triggered
abilities are dispatched through ``resolve_triggers``; the remainder are
activated through ``resolve_ability`` with metadata-defined explicit targets.
"""

import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine

from abilities.framework.resolution import resolve_ability
from abilities.framework.triggers import resolve_stack_trigger, resolve_triggers
from tests.tests_combat import HandlerStub, SessionStub
from tests.tests_set1_sweep import (
    _clear_and_seed,
    _explicit_target_map,
    _plain_troop,
)


SRC = os.environ.get(
    "HEX_TEST_SOURCE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db"),
)


def _champion_abilities(db):
    rows = db.execute(
        "SELECT ca.ability_guid, ca.champion_guid, ca.champion_name, "
        "cam.trigger_event_type "
        "FROM champion_abilities ca LEFT JOIN card_abilities_meta cam "
        "ON cam.ability_guid=ca.ability_guid "
        "WHERE ca.ability_guid IS NOT NULL "
        "GROUP BY ca.ability_guid "
        "ORDER BY ca.ability_guid"
    ).fetchall()
    return {
        str(guid).lower(): {
            "champion_guid": champion_guid,
            "name": champion_name or "Unknown champion",
            "trigger": trigger or "",
        }
        for guid, champion_guid, champion_name, trigger in rows
    }


def _drain_stack(db, handler, game, session, pl_t, ai_t, bstate):
    stack = bstate.get("stack") or []
    while stack:
        item = stack.pop()
        resolve_stack_trigger(handler, game, session, db, pl_t, ai_t,
                              bstate, item)


def _run_one(db, handler, game, session, pl_t, ai_t, bstate, ability_guid,
             metadata, source_uid):
    trigger = metadata["trigger"]
    if trigger:
        handler._player_champ_guid = metadata["champion_guid"]
        handler._player_champ_abilities = [ability_guid]
        resolve_triggers(
            db, handler, game, session, pl_t, ai_t, bstate, trigger,
            source_uid, source_owner_uid=5,
            extra_target=int(handler._ai_champ_scid.uid.uid64))
        _drain_stack(db, handler, game, session, pl_t, ai_t, bstate)
        return

    target_map = _explicit_target_map(db, ability_guid)
    resolve_ability(
        handler, game, session, db, pl_t, ai_t, bstate, ability_guid,
        source_uid, 5, target_map)


def _run_sweep():
    fd, sandbox_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy(SRC, sandbox_path)
    db = sqlite3.connect(sandbox_path)
    handler = HandlerStub(db)
    session = SessionStub()
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    source_uid = int(handler._player_champ_scid.uid.uid64)
    abilities = _champion_abilities(db)
    plain = _plain_troop(db)
    failures = []
    passed = 0
    try:
        for ability_guid, metadata in abilities.items():
            try:
                # Rebuild a rich, isolated board for every ability.  The
                # champion itself is represented by its real SessionCardId;
                # ordinary cards provide legal targets and deck/hand zones.
                _clear_and_seed(db, plain, ability_guid, plain)
                bstate = {
                    "player_health": 20,
                    "ai_health": 20,
                    "player_max_health": 20,
                    "ai_max_health": 20,
                    "player_resources": 10,
                    "player_total_resources": 10,
                    "player_spell_points": 10,
                    "player_charges": 10,
                    "ai_charges": 10,
                    "turn_number": 1,
                    "turn_player": 1,
                    "player_threshold": {1: 5, 2: 5, 4: 5, 8: 5, 16: 5},
                    "ai_threshold": {1: 5, 2: 5, 4: 5, 8: 5, 16: 5},
                }
                handler._current_bstate = bstate
                game = game_engine.Game(1, pl_t, ai_t)
                with contextlib.redirect_stdout(io.StringIO()):
                    _run_one(db, handler, game, session, pl_t, ai_t, bstate,
                             ability_guid, metadata, source_uid)
                game.make_network_packet(pl_t)
                passed += 1
            except Exception:
                failures.append((ability_guid, metadata["name"], metadata["trigger"],
                                 traceback.format_exc()))
    finally:
        db.close()
        try:
            os.remove(sandbox_path)
        except OSError:
            pass

    print(f"Champion catalog sweep: {passed}/{len(abilities)} abilities resolved")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for guid, name, trigger, detail in failures:
            print(f"  {name} {guid} trigger={trigger}")
            print(detail.rstrip())
    else:
        print("All champion abilities resolved and serialized without crashing.")
    return len(failures)


if __name__ == "__main__":
    raise SystemExit(_run_sweep())
