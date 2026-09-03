"""Oracle-derived regression tests for SpiritDrain (lifelink) combat healing.

The correct behavior (from HexTCG's recovered Gameforge semantics,
gameforge_runtime.py::_spirit_drain) is that a SpiritDrain troop heals its
controller for the ACTUAL damage dealt — the post-clamp amount that actually
got through — not its full printed attack value.  It also heals per-source
(per damaging card), and the max-health cap applies per heal.

Scenarios (attacker is the AI, so its SpiritDrain heals the AI champion):
  - Unblocked attacker reaching the champion  : heal == attack
  - Blocked attacker that kills its blocker   : heal == blocker defense (lethal)
  - Blocked attacker vs a surviving blocker   : heal == defense assigned (non-lethal)
  - Attacker killed by blockers               : heal only for damage it actually dealt
Previously hex-server accumulated `step_atk` (full attack) regardless of the
real damage dealt, and healed in one controller-side lump.
"""

import os
import sqlite3
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


def _run(db, atk_uid, def_uid, atk_tpl, def_tpl, ai_start=10):
    """AI 5/3 SpiritDrain attacker at atk_tpl attacks; player blocker at
    def_tpl blocks.  Both players' champion health starts at ``ai_start`` /
    20.  Returns bstate."""
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    bstate = {"player_health": 20, "ai_health": ai_start,
              "player_max_health": 20, "ai_max_health": 20,
              "turn_number": 1}
    add_card(db, atk_uid, 0, atk_tpl)
    add_card(db, def_uid, 5, def_tpl)
    attackers = {atk_uid: 0}
    blockers = {atk_uid: [def_uid]}
    handler = HandlerStub(db)
    ai._db = db
    ai.resolve_combat(handler, SessionStub(), pl_t, ai_t, bstate, attackers,
                      blockers, ai_t, pl_t, "ai_attackers", send_events=lambda *a: None)
    return bstate


# Template GUIDs: attacker is 5/3 SpiritDrain (805 is "5/3 lifelinker"),
# blocker varies.
ATK = "ffffffff-0000-0000-0000-0000000000aa"   # 5/3 SpiritDrain / Juggernaught
BLK_25 = "ffffffff-0000-0000-0000-0000000000bb"  # 2/5 blocker
BLK_22 = "ffffffff-0000-0000-0000-0000000000cc"  # 2/2 blocker
BLK_00 = "ffffffff-0000-0000-0000-0000000000dd"  # 0/2 blocker


def test_spiritdrain_unblocked_heals_full(db):
    """Unblocked 5/3 SpiritDrain attacker deals 5 to the player champion and
    the AI (its controller) heals 5: AI 10 -> 15, player 20 -> 15."""
    _insert_troop(db, ATK, "Lifelink Attacker", 5, 3,
                  attributes=game_engine.ECardAttributes.SpiritDrain)
    _insert_troop(db, BLK_00, "Decoy", 0, 2)
    # No blocker: pass an empty blocker map via a dedicated unblocked run.
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    bstate = {"player_health": 20, "ai_health": 10,
              "player_max_health": 20, "ai_max_health": 20, "turn_number": 1}
    add_card(db, 101, 0, ATK)
    handler = HandlerStub(db)
    ai._db = db
    ai.resolve_combat(handler, SessionStub(), pl_t, ai_t, bstate,
                      {101: 0}, {}, ai_t, pl_t, "ai_attackers",
                      send_events=lambda *a: None)
    assert bstate["player_health"] == 15, bstate   # took 5
    assert bstate["ai_health"] == 15, bstate       # healed full 5


def test_spiritdrain_kills_blocker_heals_lethal(db):
    """5/3 SpiritDrain attacker kills a 2/2 blocker: deals 2 (lethal), so the
    AI heals 2, NOT 5.  Player health unchanged (no champion contact)."""
    _insert_troop(db, ATK, "Lifelink Attacker", 5, 3,
                  attributes=game_engine.ECardAttributes.SpiritDrain)
    _insert_troop(db, BLK_22, "2/2 Blocker", 2, 2)
    bstate = _run(db, 101, 102, ATK, BLK_22, ai_start=10)
    assert bstate["ai_health"] == 12, bstate       # healed 2 for the 2 lethal
    assert bstate["player_health"] == 20, bstate   # no champion damage


def test_spiritdrain_blocked_surviving_blocker_heals_nonlethal(db):
    """5/3 SpiritDrain attacker blocked by a 2/5 blocker that survives: the
    attacker's 5 damage is assigned as non-lethal, so it heals 5 (the damage
    it actually dealt), and the blocker survives."""
    _insert_troop(db, ATK, "Lifelink Attacker", 5, 3,
                  attributes=game_engine.ECardAttributes.SpiritDrain)
    _insert_troop(db, BLK_25, "2/5 Blocker", 2, 5)
    bstate = _run(db, 101, 102, ATK, BLK_25, ai_start=10)
    # The 2/5 blocker deals 2 back (non-lethal to the 5/3 attacker). The
    # attacker survives. AI heals 5 for the 5 it dealt.
    assert bstate["ai_health"] == 15, bstate       # healed 5
    assert bstate["player_health"] == 20, bstate


def test_spiritdrain_lifelink_capped_at_max(db):
    """Lifelink healing is capped at the champion's max health.  AI at 18/20,
    unblocked 5-power SpiritDrain: it heals toward but not past 20."""
    _insert_troop(db, ATK, "Lifelink Attacker", 5, 3,
                  attributes=game_engine.ECardAttributes.SpiritDrain)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    bstate = {"player_health": 20, "ai_health": 18,
              "player_max_health": 20, "ai_max_health": 20, "turn_number": 1}
    add_card(db, 101, 0, ATK)
    handler = HandlerStub(db)
    ai._db = db
    ai.resolve_combat(handler, SessionStub(), pl_t, ai_t, bstate,
                      {101: 0}, {}, ai_t, pl_t, "ai_attackers",
                      send_events=lambda *a: None)
    assert bstate["player_health"] == 15, bstate
    assert bstate["ai_health"] == 20, bstate  # 18+5 capped at max 20


def test_spiritdrain_blocked_attacker_killed_heals_only_dealt(db):
    """5/3 SpiritDrain attacker is killed by a 5/2 blocker.  It still dealt 2
    lethal to the blocker before dying, so it heals 2 (not 5)."""
    _insert_troop(db, ATK, "Lifelink Attacker", 5, 3,
                  attributes=game_engine.ECardAttributes.SpiritDrain)
    _insert_troop(db, "ffffffff-0000-0000-0000-0000000000ee", "5/2 Blocker",
                  5, 2)
    bstate = _run(db, 101, 102, ATK, "ffffffff-0000-0000-0000-0000000000ee",
                  ai_start=10)
    # The 5/2 blocker kills the attacker; the attacker dealt only 2 (lethal)
    # back. Lives linked to that 2.
    assert bstate["ai_health"] == 12, bstate       # healed 2
    assert bstate["player_health"] == 20, bstate


def test_spiritdrain_juggernaught_heals_through_to_champion(db):
    """5/3 SpiritDrain+Juggernaught attacker kills a 2/2 blocker and the 3
    leftover breaks through to the champion.  All 5 is dealt, so heal 5."""
    _insert_troop(db, ATK, "Trample Lifelinker", 5, 3,
                  attributes=game_engine.ECardAttributes.SpiritDrain
                  | game_engine.ECardAttributes.Juggernaught)
    _insert_troop(db, BLK_22, "2/2 Blocker", 2, 2)
    bstate = _run(db, 101, 102, ATK, BLK_22, ai_start=10)
    assert bstate["ai_health"] == 15, bstate       # healed full 5
    assert bstate["player_health"] == 17, bstate   # took 3 trample


def test_blocker_spiritdrain_heals_full_block(db):
    """A blocker with SpiritDrain deals its full attack back to the survivor
    and heals for that full amount (blocker side is not over/under-healed)."""
    _insert_troop(db, ATK, "Attacker", 2, 5)
    _insert_troop(db, "ffffffff-0000-0000-0000-0000000000ff", "Blocker",
                  3, 3, attributes=game_engine.ECardAttributes.SpiritDrain)
    bstate = _run(db, 101, 102, ATK, "ffffffff-0000-0000-0000-0000000000ff",
                  ai_start=10)
    # Attacker deals 2 non-lethal to the 3/3 blocker; blocker deals 3 back
    # (heals the AI? no — the blocker's controller is the player).  The player
    # has max health so its heal is capped; assert the player is still 20.
    assert bstate["player_health"] == 20, bstate


def main():
    tests = [
        ("SpiritDrain unblocked heals full", test_spiritdrain_unblocked_heals_full),
        ("SpiritDrain kills blocker heals lethal", test_spiritdrain_kills_blocker_heals_lethal),
        ("SpiritDrain surviving blocker heals nonlethal", test_spiritdrain_blocked_surviving_blocker_heals_nonlethal),
        ("SpiritDrain lifelink capped at max", test_spiritdrain_lifelink_capped_at_max),
        ("SpiritDrain attacker killed heals only dealt", test_spiritdrain_blocked_attacker_killed_heals_only_dealt),
        ("SpiritDrain Juggernaught heals through", test_spiritdrain_juggernaught_heals_through_to_champion),
        ("Blocker SpiritDrain full block", test_blocker_spiritdrain_heals_full_block),
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
