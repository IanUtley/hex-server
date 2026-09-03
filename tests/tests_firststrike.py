"""Oracle-derived regression tests for First/Swiftstrike combat timing.

Correct behavior (HexTCG gameforge_runtime, test_phase36_second_frost.py
::test_first_strike_damage_happens_before_normal_retaliation):

A blocker that dies to the Swiftstrike (first-strike) damage step is removed
before the normal step and therefore deals NO retaliation damage to the
attacker.  Conversely a regular attacker with no Swiftstrike deals no damage
in the Swiftstrike step even if its blocker has Swiftstrike.

There are two combat passes through resolve_combat:
  - first_strike=True  : only FirstStrike/DualStrike combatants deal damage
  - first_strike=False : all surviving combatants deal damage

This file drives resolve_combat twice (like the engine's combat phase cycle)
and checks the damage/health outcomes across both steps.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine
import ai

from tests.tests_combat import make_db as _combat_make_db, add_card, HandlerStub, SessionStub


def make_db():
    return _combat_make_db()


def _insert_troop(db, tpl, name, atk, deff, attributes=0):
    db.execute(
        "INSERT INTO card_templates (guid, name, card_type, cost, attack, "
        "defense, attributes, abilities_json, threshold_json, subtype) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tpl, name, "Troop", 2, atk, deff, attributes, "[]", "[]", ""))


def _run_two_steps(db, atk_uid, def_uid, atk_tpl, def_tpl, ai_start=20):
    """AI 5/3 FirstStrike attacker at atk_tpl attacks; player blocker at
    def_tpl blocks.  Resolves both the Swiftstrike (first) and normal steps.
    Returns a dict with bstate plus each combatant's post-combat location and
    damage."""
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    bstate = {"player_health": 20, "ai_health": ai_start,
              "player_max_health": 20, "ai_max_health": 20, "turn_number": 1}
    add_card(db, atk_uid, 0, atk_tpl)
    add_card(db, def_uid, 5, def_tpl)
    attackers = {atk_uid: 0}
    blockers = {atk_uid: [def_uid]}
    handler = HandlerStub(db)
    ai._db = db
    send = lambda *a: None
    ai.resolve_combat(handler, SessionStub(), pl_t, ai_t, bstate, attackers,
                      blockers, ai_t, pl_t, "ai_attackers", first_strike=True,
                      send_events=send)
    ai.resolve_combat(handler, SessionStub(), pl_t, ai_t, bstate, attackers,
                      blockers, ai_t, pl_t, "ai_attackers", first_strike=False,
                      send_events=send)

    def _state(uid):
        row = db.execute(
            "SELECT location, card_damage FROM game_cards "
            "WHERE session_id=? AND card_uid=?", (1, uid)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    return {"bstate": bstate, "atk": _state(atk_uid), "blk": _state(def_uid)}


FS_ATK = "ffffffff-0000-0000-0000-00000000f001"  # 5/3 FirstStrike
NOR_ATK = "ffffffff-0000-0000-0000-00000000f002"  # 3/3 normal (no FS)
DIE_BLK = "ffffffff-0000-0000-0000-00000000f003"  # 3/1 blocker (dies to FS)
SURV_BLK = "ffffffff-0000-0000-0000-00000000f004"  # 1/6 blocker (survives FS 5)
FS_BLK = "ffffffff-0000-0000-0000-00000000f005"  # 3/3 FirstStrike blocker


def test_fs_attacker_kills_blocker_before_retaliation(db):
    """5/3 FirstStrike attacker vs a 3/1 blocker: the blocker dies in the
    Swiftstrike step and deals NO retaliation.  Attacker survives unharmed,
    player champion untouched.  Blocker ends in discard with 0 damage on the
    attacker (never touched)."""
    _insert_troop(db, FS_ATK, "FS Attacker", 5, 3,
                  attributes=game_engine.ECardAttributes.FirstStrike)
    _insert_troop(db, DIE_BLK, "Frail Blocker", 3, 1)
    r = _run_two_steps(db, 101, 102, FS_ATK, DIE_BLK)
    assert r["blk"][0] == "discard", r          # blocker died to Swiftstrike
    assert r["atk"][1] == 0, r                  # attacker took no damage
    assert r["bstate"]["ai_health"] == 20, r
    assert r["bstate"]["player_health"] == 20, r


def test_fs_attacker_survives_big_blocker_takes_its_damage(db):
    """5/3 FirstStrike attacker vs a 1/6 blocker (survives FS by 1): the
    blocker deals its 1 in the normal step, attacker survives at 2 damaged."""
    _insert_troop(db, FS_ATK, "FS Attacker", 5, 3,
                  attributes=game_engine.ECardAttributes.FirstStrike)
    _insert_troop(db, SURV_BLK, "1/6 Blocker", 1, 6)
    r = _run_two_steps(db, 101, 102, FS_ATK, SURV_BLK)
    assert r["blk"][0] == "warzone", r          # blocker survives (1/6)
    assert r["atk"][0] == "warzone", r          # attacker survives
    assert r["atk"][1] == 1, r                  # took 1 from the surviving blocker
    assert r["blk"][1] == 5, r                  # took 5 (survives at 1)
    assert r["bstate"]["player_health"] == 20, r


def test_normal_attacker_dies_to_fs_blocker(db):
    """A NON-FirstStrike 3/3 attacker blocked by a 3/3 FirstStrike blocker:
    the blocker strikes first and kills it before it can retaliate."""
    _insert_troop(db, NOR_ATK, "Normal Attacker", 3, 3)
    _insert_troop(db, FS_BLK, "FS Blocker", 3, 3,
                  attributes=game_engine.ECardAttributes.FirstStrike)
    r = _run_two_steps(db, 101, 102, NOR_ATK, FS_BLK)
    assert r["atk"][0] == "discard", r          # killed by FS before retaliation
    assert r["blk"][0] == "warzone", r          # blocker survives
    assert r["blk"][1] == 0, r                  # never took damage
    assert r["bstate"]["player_health"] == 20, r


def main():
    tests = [
        ("FS attacker kills blocker before retaliation", test_fs_attacker_kills_blocker_before_retaliation),
        ("FS attacker survives big blocker", test_fs_attacker_survives_big_blocker_takes_its_damage),
        ("Normal attacker dies to FS blocker", test_normal_attacker_dies_to_fs_blocker),
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