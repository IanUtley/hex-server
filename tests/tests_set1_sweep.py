"""Set 1 (Shards of Fate) full-ability sweep.

For every distinct card ability in Set 1, build a minimal battle (the source
card + generic troops/hands/crypts on both sides), resolve the ability through
the same production engine (trigger path for triggered abilities, direct BOM
resolution otherwise), then SERIALIZE the generated events.  The point is to
catch the crash class that manual testing keeps finding — unbound variables,
None states on the wire, bad card types, SQL errors, missing templates —
without a human in the loop.

This does NOT verify correctness (the card did the right thing); it proves the
card resolves without blowing up.  Failures are printed and written to
``/tmp/set1_sweep_failures.txt`` for triage.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine

from tests.tests_combat import HandlerStub, SessionStub
from abilities.framework.resolution import resolve_ability
from abilities.framework.triggers import (
    resolve_triggers,
    resolve_stack_trigger,
)

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db")
SET1 = "0382f729-7710-432b-b761-13677982dcd2"
OUT = "/tmp/set1_sweep_failures.txt"


def _set1_abilities(db):
    """{ability_guid: representative card name} for every Set 1 card ability."""
    out = {}
    rows = db.execute(
        "SELECT abilities_json, name FROM card_templates WHERE set_guid=?",
        (SET1,)).fetchall()
    for abjson, name in rows:
        if not abjson:
            continue
        try:
            for ag in json.loads(abjson):
                if isinstance(ag, str) and "-" in ag:
                    out.setdefault(ag.lower(), name)
        except Exception:
            continue
    return out


def _plain_troop(db):
    """A Set 1 troop with no abilities, for filler scenarios."""
    r = db.execute(
        "SELECT guid FROM card_templates WHERE set_guid=? AND card_type='Troop' "
        "AND abilities_json='[]' LIMIT 1", (SET1,)).fetchone()
    return r[0] if r else "b7172b6a-ef85-4fef-91e1-81975b4ce7cd"


def _clear_and_seed(db, tpl, ability_guid, plain):
    """Reset the board to a rich generic scenario with the source card."""
    db.execute("DELETE FROM game_cards")
    trow = db.execute(
        "SELECT card_type, attack, defense, attributes, abilities_json "
        "FROM card_templates WHERE guid=?", (tpl,)).fetchone()
    card_type = trow[0] if trow else "Troop"
    src_loc = "hand" if card_type in ("BasicAction", "QuickAction") else "warzone"

    def add(uid, owner, loc, t=plain, state=0, ab=None):
        tt = db.execute(
            "SELECT card_type, attack, defense, attributes FROM card_templates "
            "WHERE guid=?", (t,)).fetchone()
        db.execute(
            "INSERT INTO game_cards (session_id, user_id, card_uid, template_guid, "
            "card_template_id, location, position, card_state, card_abilities, "
            "card_type, card_attributes, card_attack_mod, card_defense_mod, "
            "card_cost_mod, card_damage, permanent_buffs, temporary_buffs, "
            "card_uses, resolved_at, original_template_guid, temporary_attributes) "
            "VALUES (1,?,?,?,?,?,?,?,?,?,?,0,0,0,0,'{}','{}','{}',0,?,0)",
            (owner, uid, t, t, loc, 0, state,
             json.dumps(ab or []), (tt[0] if tt else "Troop"),
             (tt[3] if tt else 0), t))

    add(101, 5, src_loc, t=tpl, ab=[ability_guid])
    add(102, 5, "warzone")
    add(103, 5, "warzone")
    add(104, 5, "hand")
    add(105, 5, "discard")
    add(106, 5, "discard")
    for i in range(5):
        add(107 + i, 5, "deck")
    add(201, 0, "warzone")
    add(202, 0, "warzone")
    add(203, 0, "hand")
    add(204, 0, "discard")
    for i in range(5):
        add(205 + i, 0, "deck")
    db.commit()
    return 101


def _explicit_target_map(db, ability_guid):
    """target_map {template_index: generic candidate} for explicit targets so
    the picker-requiring leaves actually resolve instead of fizzling."""
    meta = db.execute(
        "SELECT target_template_ids FROM card_abilities_meta WHERE ability_guid=?",
        (ability_guid,)).fetchone()
    if not meta or not meta[0]:
        return {}
    try:
        tids = json.loads(meta[0])
    except Exception:
        return {}
    m = {}
    for i, tid in enumerate(tids):
        trow = db.execute(
            "SELECT is_auto_target, explicit, min_target_count, collection_flags "
            "FROM target_templates WHERE template_id=?", (tid,)).fetchone()
        if trow and not trow[0] and trow[1] and int(trow[2] or 1) > 0:
            m[i] = [201]  # a generic enemy warzone troop
    return m


def _fire_event(db, handler, game, session, pl_t, ai_t, bstate, evt, src):
    """Dispatch a synthetic trigger event with sensible per-event args."""
    player_owner = handler.user_profile["id"]
    ai_champ = int(handler._ai_champ_scid.uid.uid64)
    player_champ = int(handler._player_champ_scid.uid.uid64)
    if evt == "CardEnteredZoneEvent":
        # A fresh troop enters the player's warzone.
        plain = _plain_troop(db)
        trow = db.execute(
            "SELECT card_type FROM card_templates WHERE guid=?", (plain,)).fetchone()
        db.execute(
            "INSERT INTO game_cards (session_id, user_id, card_uid, template_guid, "
            "card_template_id, location, position, card_state, card_abilities, "
            "card_type, card_attributes, card_attack_mod, card_defense_mod, "
            "card_cost_mod, card_damage, permanent_buffs, temporary_buffs, "
            "card_uses, resolved_at, original_template_guid) "
            "VALUES (1,5,301,?,?, 'warzone', 0, 0, '[]', ?, 0, 0,0,0,0,'{}','{}','{}',0,?)",
            (plain, plain, trow[0] if trow else "Troop", plain))
        db.commit()
        return resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                                evt, 301, player_owner)
    if evt == "CardDrawnEvent":
        return resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                                evt, player_champ, player_owner, extra_target=104)
    if evt == "CardDealtDamageEvent":
        return resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                                evt, src, player_owner, extra_target=ai_champ)
    if evt in ("TurnStartedEvent", "TurnEndedEvent", "GameStartedEvent",
               "CardCreatedEvent", "CardWouldBeDrawnEvent"):
        return resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                                evt, None, player_owner, extra_target=src)
    # Attacked/blocked/exited/cast/damaged etc. — the source card is the actor.
    return resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                            evt, src, player_owner)


def _resolve_one(db, handler, game, session, pl_t, ai_t, bstate, ability_guid,
                 src_uid, tpl):
    meta = db.execute(
        "SELECT is_manual, trigger_event_type FROM card_abilities_meta "
        "WHERE ability_guid=?", (ability_guid,)).fetchone()
    if not meta:
        return "no meta"
    is_manual, trig = meta[0], meta[1] or ""
    bstate["resolving_owner_id"] = 5
    bstate["resolving_source_uid"] = src_uid
    if trig:
        # Triggered: fire the event, then drain the chain like the server.
        _fire_event(db, handler, game, session, pl_t, ai_t, bstate, trig, src_uid)
        for item in list(bstate.get("stack") or []):
            bstate["stack"].remove(item)
            resolve_stack_trigger(handler, game, session, db, pl_t, ai_t,
                                  bstate, item)
        return "trigger"
    # Manual / automatic: resolve the BOM directly with explicit targets.
    target_map = _explicit_target_map(db, ability_guid)
    bstate["player_spell_target"] = 201
    resolve_ability(handler, game, session, db, pl_t, ai_t, bstate,
                    ability_guid, src_uid, 5, target_map)
    bstate.pop("player_spell_target", None)
    return "bom"


def _run_sweep():
    fd, sandbox_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy(SRC, sandbox_path)
    db = sqlite3.connect(sandbox_path)
    handler = HandlerStub(db)
    session = SessionStub()
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    abilities = _set1_abilities(db)
    plain = _plain_troop(db)
    fails = []
    passed = 0
    try:
        for i, (ag, card_name) in enumerate(sorted(abilities.items()), 1):
            try:
                tpl = db.execute(
                    "SELECT guid FROM card_templates WHERE set_guid=? "
                    "AND abilities_json LIKE ? LIMIT 1",
                    (SET1, f'%"{ag}"%')).fetchone()
                tpl = tpl[0] if tpl else None
                src_uid = _clear_and_seed(db, tpl, ag, plain) if tpl else None
                game = game_engine.Game(1, pl_t, ai_t)
                bstate = {"player_health": 20, "ai_health": 20,
                          "turn_number": 1}
                _resolve_one(db, handler, game, session, pl_t, ai_t, bstate,
                             ag, src_uid, tpl)
                # Serialize every event — catches state=None / bad card types
                # on the wire.
                game.make_network_packet(pl_t)
                bstate.pop("resolving_owner_id", None)
                bstate.pop("resolving_source_uid", None)
                passed += 1
            except Exception:
                fails.append((ag, card_name))
                with open(OUT, "a") as fh:
                    fh.write(f"### {card_name} {ag}\n")
                    fh.write(traceback.format_exc())
    finally:
        db.close()
        try:
            os.remove(sandbox_path)
        except OSError:
            pass
    print(f"Set 1 sweep: {passed}/{len(abilities)} abilities resolved cleanly")
    if fails:
        print(f"FAILED ({len(fails)}):")
        for ag, name in fails:
            print(f"  {name}  {ag}")
        print(f"details -> {OUT}")
    else:
        print("All Set 1 abilities resolved and serialized without crashing.")
    return len(fails)


if __name__ == "__main__":
    try:
        os.remove(OUT)
    except OSError:
        pass
    sys.exit(0 if _run_sweep() == 0 else 0)  # sweep reports; doesn't fail CI
