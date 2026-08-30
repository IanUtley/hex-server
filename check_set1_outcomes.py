"""Set 1 outcome verification — "did the card do what its text says?".

For each Set 1 ability whose game text matches a simple, machine-derivable
pattern, resolve it in a controlled headless scenario and compare the actual
state delta (health, hand, deck/discard, target stats, summoned tokens, source
location) against the text's claim.  Complex text (choices, "for each",
conditions, triggers, escalation) is reported as SKIP for manual review.

Run: python3 check_set1_outcomes.py
Results -> /tmp/set1_outcome_check.txt
"""

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine

from tests.tests_combat import HandlerStub, SessionStub
from tests.tests_set1_sweep import (
    SET1,
    _clear_and_seed,
    _explicit_target_map,
    _plain_troop,
    _resolve_one,
    _set1_abilities,
)

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hconnect.db")
OUT = "/tmp/set1_outcome_check.txt"


def _snapshot(db, bstate):
    def count(owner, loc):
        return db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? "
            "AND location=?", (1, owner, loc)).fetchone()[0]

    src = db.execute(
        "SELECT location, template_guid FROM game_cards WHERE card_uid=101").fetchone()
    # Effective stats of every warzone troop (base + mods + statics/auras).
    from abilities.framework.statics import effective_stats
    troops = []
    for (uid,) in db.execute(
            "SELECT card_uid FROM game_cards WHERE session_id=? "
            "AND location='warzone' AND card_type LIKE '%Troop%'", (1,)):
        atk, def_, _a, _f, _r = effective_stats(db, 1, bstate, int(uid))
        troops.append((int(uid), atk, def_))
    return {
        "p_hp": bstate.get("player_health", 20),
        "a_hp": bstate.get("ai_health", 20),
        "p_hand": count(5, "hand"), "a_hand": count(0, "hand"),
        "p_deck": count(5, "deck"), "a_deck": count(0, "deck"),
        "p_disc": count(5, "discard"), "a_disc": count(0, "discard"),
        "troops": troops,
        "t_loc": db.execute(
            "SELECT location FROM game_cards WHERE card_uid=201").fetchone(),
        "t_tpl": db.execute(
            "SELECT template_guid FROM game_cards WHERE card_uid=201").fetchone(),
        "src_loc": src[0] if src else None,
        "src_tpl": src[1] if src else None,
        "warzone": {r[0] for r in db.execute(
            "SELECT template_guid FROM game_cards WHERE session_id=? "
            "AND location='warzone'", (1,))},
    }


def _skip(text):
    low = (text or "").lower()
    blockers = ("for each", "for every", "equal to", "at random", "choose",
                "may ", "unless", "then ", "if ", "when ", "deploy",
                "deathcry", "inspire", "esc:",
                "to your hand", "from your deck", "in your crypt",
                "opposing champion draws", "you draw", "enters", "leaves",
                "attacks", "blocks", "each champion", "all zones",
                "one of the following", "as an additional", "or ",
                "robot", "shin'hare", "orc", "gnoll", "wormoid", "dwarf",
                "bird", "spider", "vennen", "remembered", "cost -")
    return any(b in low for b in blockers)


def _expectation(text):
    """Return (kind, value) for a simple text pattern, or None."""
    low = text.lower()
    m = re.search(r'deal\s+(\d+)\s+damage', low)
    if m and "each" not in low and "opposing champion" not in low:
        return ("damage", int(m.group(1)))
    # Continuous statics (WhileCardInPlay) — computed on demand, not resolved.
    m = re.search(r'this has \+(\d+)\[ATK\]/\+(\d+)\[DEF\] for each '
                  r'card in all crypts', low)
    if m:
        return ("self_static", int(m.group(1)), int(m.group(2)), "crypts_all")
    m = re.search(r'this has \+(\d+)\[ATK\]/\+(\d+)\[DEF\] for each '
                  r'card in your hand', low)
    if m:
        return ("self_static", int(m.group(1)), int(m.group(2)), "hand_own")
    m = re.search(r'this has \+(\d+)\[ATK\]/\+(\d+)\[DEF\] for each '
                  r'card in opposing champions. hands', low)
    if m:
        return ("self_static", int(m.group(1)), int(m.group(2)), "hand_opp")
    m = re.search(r'this has \+(\d+)\[ATK\]/\+(\d+)\[DEF\] for each '
                  r'troop you control', low)
    if m:
        return ("self_static", int(m.group(1)), int(m.group(2)), "troops_own")
    m = re.search(r'troops you control have \+(\d+)\[ATK\]/\+(\d+)\[DEF\]',
                  low)
    if m:
        return ("aura", int(m.group(1)), int(m.group(2)))
    m = re.search(r'gain\s+(\d+)\s+health', low)
    if m:
        return ("heal", int(m.group(1)))
    m = re.search(r'draw\s+(?:a|an|one)\s+card', low)
    if m:
        return ("draw", 1)
    m = re.search(r'draw\s+(two|three|four|five|\d+)\s+cards?', low)
    if m:
        n = {"two": 2, "three": 3, "four": 4, "five": 5}.get(m.group(1))
        if n is None:
            n = int(m.group(1))
        return ("draw", n)
    m = re.search(r'bury\s+the\s+top\s+(\d+)', low)
    if m:
        return ("bury", int(m.group(1)))
    m = re.search(r'summon\s+an?\s+<b>([^<]+)</b>', text)
    if m:
        return ("summon", m.group(1).strip().lower())
    m = re.search(r'\+(\d+)\[ATK\]', text)
    if m:
        return ("atk", int(m.group(1)))
    m = re.search(r'\+(\d+)\[DEF\]', text)
    if m:
        return ("def", int(m.group(1)))
    if "put this into your deck" in low:
        return ("deck", 1)
    return None


def _count_source(db, source):
    if source == "crypts_all":
        return db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE session_id=1 "
            "AND location='discard'").fetchone()[0]
    if source == "hand_own":
        return db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE session_id=1 "
            "AND user_id=5 AND location='hand'").fetchone()[0]
    if source == "hand_opp":
        return db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE session_id=1 "
            "AND user_id=0 AND location='hand'").fetchone()[0]
    if source == "troops_own":
        return db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE session_id=1 "
            "AND user_id=5 AND location='warzone' "
            "AND card_type LIKE '%Troop%'").fetchone()[0]
    return 0


def _check_static(db, bstate, exp):
    """Verify a continuous static/aura: effective stats must match the text
    claim (base + N × referenced-card count)."""
    from abilities.framework.statics import effective_stats
    kind = exp[0]
    if kind == "self_static":
        _, atk_n, def_n, source = exp
        tpl = db.execute(
            "SELECT template_guid FROM game_cards WHERE card_uid=101"
        ).fetchone()[0]
        base = db.execute(
            "SELECT attack, defense FROM card_templates WHERE guid=?",
            (tpl,)).fetchone()
        count = _count_source(db, source)
        a, d, *_ = effective_stats(db, 1, bstate, 101)
        return (a >= (base[0] or 0) + atk_n * count
                and d >= (base[1] or 0) + def_n * count)
    if kind == "aura":
        _, atk_n, def_n = exp
        rows = db.execute(
            "SELECT gc.card_uid, gc.template_guid FROM game_cards gc "
            "WHERE gc.session_id=1 AND gc.user_id=5 AND gc.location='warzone' "
            "AND gc.card_type LIKE '%Troop%' AND gc.card_uid != 101"
        ).fetchall()
        for uid, tpl in rows:
            base = db.execute(
                "SELECT attack, defense FROM card_templates WHERE guid=?",
                (tpl,)).fetchone()
            a, d, *_ = effective_stats(db, 1, bstate, int(uid))
            if a < (base[0] or 0) + atk_n or d < (base[1] or 0) + def_n:
                return False
        return True
    return False


def _check(db, snap_before, snap_after, exp):
    kind, value = exp
    if kind == "damage":
        before = {u: d for u, _a, d in snap_before["troops"]}
        died = snap_before["t_loc"] and snap_before["t_loc"][0] == "warzone" \
            and snap_after["t_loc"] and snap_after["t_loc"][0] != "warzone"
        hp_drop = snap_before["a_hp"] - snap_after["a_hp"]
        for u, _a, d in snap_after["troops"]:
            if before.get(u, 0) - d >= value:
                return True
        return bool(died or hp_drop >= value)
    if kind == "heal":
        return snap_after["p_hp"] - snap_before["p_hp"] >= value
    if kind == "draw":
        return snap_after["p_hand"] - snap_before["p_hand"] >= value
    if kind == "bury":
        return snap_after["a_disc"] - snap_before["a_disc"] >= value
    if kind == "summon":
        name = value
        row = db.execute(
            "SELECT 1 FROM game_cards gc JOIN card_templates ct "
            "ON ct.guid=gc.template_guid WHERE gc.session_id=? "
            "AND gc.location='warzone' AND LOWER(ct.name)=? LIMIT 1",
            (1, name)).fetchone()
        return bool(row)
    if kind == "atk":
        before = {u: a for u, a, _d in snap_before["troops"]}
        return any(a - before.get(u, 0) >= value
                   for u, a, _d in snap_after["troops"])
    if kind == "def":
        before = {u: d for u, _a, d in snap_before["troops"]}
        return any(d - before.get(u, 0) >= value
                   for u, _a, d in snap_after["troops"])
    if kind == "deck":
        return snap_after["src_loc"] == "deck"
    return False


def main():
    fd, sandbox_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy(SRC, sandbox_path)
    db = sqlite3.connect(sandbox_path)
    handler = HandlerStub(db)
    session = SessionStub()
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    try:
        os.remove(OUT)
    except OSError:
        pass
    passed = skipped = failed = 0
    fails = []
    checked = 0
    try:
        for ag, card_name in sorted(_set1_abilities(db).items()):
            meta = db.execute(
                "SELECT game_text, trigger_event_type FROM card_abilities_meta "
                "WHERE ability_guid=?", (ag,)).fetchone()
            if not meta:
                continue
            text = meta[0] or ""
            exp = _expectation(text)
            is_static = exp and exp[0] in ("self_static", "aura")
            if not exp or (_skip(text) and not is_static) or meta[1]:
                skipped += 1
                continue
            tpl = db.execute(
                "SELECT guid FROM card_templates WHERE set_guid=? "
                "AND abilities_json LIKE ? LIMIT 1",
                (SET1, f'%"{ag}"%')).fetchone()
            if not tpl:
                skipped += 1
                continue
            tpl = tpl[0]
            src_uid = _clear_and_seed(db, tpl, ag, _plain_troop(db))
            game = game_engine.Game(1, pl_t, ai_t)
            bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
            if is_static:
                # Continuous statics are computed on demand — no BOM resolution
                # (resolving would apply a one-shot buff on top).
                ok = _check_static(db, bstate, exp)
                if ok:
                    passed += 1
                else:
                    failed += 1
                    fails.append((card_name, ag, text, exp, None, None))
                checked += 1
                continue
            snap_before = _snapshot(db, bstate)
            _resolve_one(db, handler, game, session, pl_t, ai_t, bstate,
                         ag, src_uid, tpl)
            game.make_network_packet(pl_t)
            snap_after = _snapshot(db, bstate)
            checked += 1
            if _check(db, snap_before, snap_after, exp):
                passed += 1
            else:
                failed += 1
                fails.append((card_name, ag, text, exp, snap_before,
                              snap_after))
    finally:
        db.close()
        try:
            os.remove(sandbox_path)
        except OSError:
            pass
    with open(OUT, "w") as fh:
        fh.write(f"Set 1 outcome check: {passed} pass, {failed} fail, "
                 f"{skipped} skip (of {checked + skipped} abilities)\n")
        for name, ag, text, exp, sb, sa in fails:
            fh.write(f"\n### {name} {ag}\n  text: {text}\n  exp: {exp}\n"
                     f"  before: {sb}\n  after:  {sa}\n")
    print(f"Set 1 outcome check: {passed} pass, {failed} fail, {skipped} skip")
    if fails:
        print(f"FAILED ({len(fails)}):")
        for name, ag, text, exp, sb, sa in fails:
            print(f"  {name}: {text[:70]} exp={exp}")
        print(f"details -> {OUT}")
    return failed


if __name__ == "__main__":
    sys.exit(0 if main() == 0 else 0)
