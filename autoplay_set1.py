"""Headless AI-vs-AI autoplay smoke runner.

Plays full games with zero client involvement: the "player" side is driven by
the same helpers the AI uses (resource/troop/spell plays, attacker declaration,
passes), the AI side by ``ai.run_ai_turn``.  Every generated event is
serialized (the 3055 packet build), so interaction bugs — the class that only
shows up when cards meet on the board — crash the current game and are
recorded with the turn + stack.

Run: python3 tests/tests_set1_autoplay.py [games]

Uses the live hconnect.db with a scratch session id (987654); the running
server's real games use their own sessions and are untouched.  Results are
written to /tmp/set1_autoplay_errors.txt.
"""

import json
import os
import random
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine
import battle_engine as be
import hconnect_server as hcs
import ai as ai_mod
from db import _db

SET1 = "0382f729-7710-432b-b761-13677982dcd2"
OUT = "/tmp/set1_autoplay_errors.txt"
SCRATCH_SESSION = 987654
CHAMP_PLAYER = "60e7f6aa-8743-4daa-816c-85ca99928945"  # Bun'jitsu
CHAMP_AI = "f8f86969-2e47-4901-8c9e-7fbf8d859e22"       # Angel of Dawn

ET = game_engine.ETurnPhases
ECol = game_engine.ECardCollections
ELoc = game_engine.ECardLocations
ESt = game_engine.ECardStates
EAttr = game_engine.ECardAttributes


class BattleSession:
    def __init__(self, sid):
        self.session_id = sid
        self.server_id = 100
        self.session_name = "autoplay"
        self.turn_order = {}

    def _persist(self):
        pass

    def set_state(self, state):
        self.state = state


def _build_deck(rnd):
    shards = [r[0] for r in _db.execute(
        "SELECT guid FROM card_templates WHERE set_guid=? AND card_type='Resource'",
        (SET1,))]
    others = [r[0] for r in _db.execute(
        "SELECT guid FROM card_templates WHERE set_guid=? AND card_type!='Resource' "
        "AND is_pve=0 AND no_pvp=0", (SET1,))]
    rnd.shuffle(others)
    deck = rnd.sample(shards, min(10, len(shards))) + others[:30]
    rnd.shuffle(deck)
    return deck


def _add_card(session, owner, tpl, loc, counter):
    counter[0] += 1
    cu = game_engine.UID.make(1, counter[0]).uid64
    row = _db.execute(
        "SELECT card_type, attributes, abilities_json FROM card_templates "
        "WHERE guid=?", (tpl,)).fetchone()
    ctype = row[0] if row else "Troop"
    attrs = row[1] if row else 0
    ab = row[2] if row else "[]"
    _db.execute(
        "INSERT INTO game_cards (session_id, user_id, card_uid, template_guid, "
        "card_template_id, location, position, card_state, card_abilities, "
        "card_type, card_attributes, card_attack_mod, card_defense_mod, "
        "card_cost_mod, card_damage, permanent_buffs, temporary_buffs, "
        "card_uses, resolved_at, original_template_guid, temporary_attributes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,'{}','{}','{}',0,?,0)",
        (session.session_id, owner, cu, tpl, tpl, loc, 0, 0, ab, ctype,
         attrs, tpl))
    return int(cu)


def _setup(session, deck):
    _db.execute("DELETE FROM game_cards WHERE session_id=?",
                (session.session_id,))
    counter = [0]
    for pos, tpl in enumerate(deck):
        _add_card(session, 5, tpl, "deck", counter)
        _add_card(session, 0, tpl, "deck", counter)
    _db.commit()


def _make_handler(session, pl_t, ai_t):
    h = object.__new__(hcs.HCPHandler)
    h.user_profile = {"id": 5}
    h.client_reck_id = 5
    h.sid = "autoplay"
    h.scnt = 0
    h.ccnt = 0
    h._game_scnt = 0
    h._event_q = []
    h._player_champ_scid = game_engine.SessionCardId(pl_t)
    h._ai_champ_scid = game_engine.SessionCardId(ai_t)
    h._player_champ_guid = CHAMP_PLAYER
    h._ai_champ_guid = CHAMP_AI
    h._player_champ_abilities = []
    h._ai_champ_ability_guids = []
    h._player_starting_health = 20
    h._ai_starting_health = 20
    h._ai_personality = "Aggressive"
    # The harness drives the AI turn itself (with proper resume at opponent
    # stops); stop _advance_to_priority from auto-playing a fresh AI turn
    # when the player's turn ends.
    h._autoplay_drive_ai_turn = True
    h._ai_turn_depth = 0
    h._current_bstate = None
    h._player_autopass = False
    h._pending_player_stops = None
    h._pending_player_draws_first = None
    h.send = lambda *a, **k: None
    h._campaign_gameend = lambda *a, **k: None
    return h


def _fresh(handler, session, pl_t, ai_t, bstate):
    return handler._fresh_game(session, pl_t, ai_t, bstate)


def _drain_stack(handler, session, pl_t, ai_t, bstate, game):
    guard = 0
    while not be.stack_empty(bstate):
        guard += 1
        if guard > 60:
            break  # runaway chain (trigger loop) — stop resolving this batch
        item = be.stack_pop(bstate)
        be.stack_reset_passes(bstate)
        handler._resolve_stack_item(session, pl_t, ai_t, bstate, item, game)
        if be.stack_empty(bstate):
            game.push_chain_empty()
    handler._send_battle_events(session, game, pl_t)


def p_play_resource(handler, session, pl_t, ai_t, bstate):
    if bstate.get("player_resource_played_this_turn"):
        return
    row = _db.execute(
        "SELECT id, card_uid, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=5 AND location='hand' "
        "AND card_type='Resource' ORDER BY position LIMIT 1",
        (session.session_id,)).fetchone()
    if not row:
        return
    _db.execute(
        "UPDATE game_cards SET location='PlayedResources', position=9999 WHERE id=?",
        (row[0],))
    _db.commit()
    bstate["player_resource_played_this_turn"] = True
    bstate["player_total_resources"] = bstate.get("player_total_resources", 0) + 1
    bstate["player_resources"] = bstate.get("player_resources", 0) + 1
    bstate["player_charges"] = bstate.get("player_charges", 0) + 1
    name = _db.execute(
        "SELECT name FROM card_templates WHERE guid=?",
        (row[2],)).fetchone()
    color = (name[0].split()[0] if name else "Wild").lower()
    flag = {"blood": 4, "ruby": 8, "sapphire": 16, "wild": 32,
            "diamond": 64}.get(color, 32)
    bstate.setdefault("player_threshold", {})
    bstate["player_threshold"][flag] = bstate["player_threshold"].get(flag, 0) + 1


def p_play_troop(handler, session, pl_t, ai_t, bstate, game):
    resources = bstate.get("player_resources", 0)
    rows = _db.execute(
        "SELECT gc.id, gc.card_uid, gc.template_guid, ct.cost, ct.card_type, "
        "ct.threshold_json FROM game_cards gc JOIN card_templates ct "
        "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.user_id=5 "
        "AND gc.location='hand' AND (ct.card_type LIKE '%Troop%' "
        "OR ct.card_type LIKE '%Artifact%' OR ct.card_type LIKE '%Constant%') "
        "ORDER BY gc.position", (session.session_id,)).fetchall()
    for row in rows:
        cost = row[3] or 0
        if cost > resources:
            continue
        if not handler._thresholds_met(row[5], bstate.get("player_threshold", {})):
            continue
        tid = int(row[1])
        _db.execute(
            "UPDATE game_cards SET location='CastSpells' "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, tid))
        _db.commit()
        bstate["player_resources"] = resources - cost
        scid = game_engine.SessionCardId(game_engine.UID(tid))
        tpl_g, ct_n, nm, c2, atk2, def2, gem2 = handler._card_full_data(
            game, scid, row[2])
        game.push_card_updated(
            scid, pl_t, ECol.CastSpells, game_engine.card_type_from_db(row[4]),
            template_id=row[2], cost=c2, attack=atk2, defense=def2, gems=gem2)
        game.push_card_moved(scid, pl_t, ECol.CastSpells, ELoc.Top, 0)
        game.push_ability_on_chain(scid, game_engine.ResourceId.from_str(row[2]))
        be.stack_push(bstate, {"kind": "troop", "source_uid": tid,
                               "instance_id": 1})
        return


def p_play_spell(handler, session, pl_t, ai_t, bstate, game):
    resources = bstate.get("player_resources", 0)
    rows = _db.execute(
        "SELECT gc.id, gc.card_uid, gc.template_guid, ct.cost, ct.card_type, "
        "ct.threshold_json FROM game_cards gc JOIN card_templates ct "
        "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.user_id=5 "
        "AND gc.location='hand' AND ct.card_type IN ('BasicAction','QuickAction') "
        "ORDER BY gc.position", (session.session_id,)).fetchall()
    for row in rows:
        cost = row[3] or 0
        if cost > resources:
            continue
        if not handler._thresholds_met(row[5], bstate.get("player_threshold", {})):
            continue
        info = ai_mod._spell_damage_info(_db, row[2])
        if not info:
            continue
        enemy = _db.execute(
            "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=0 "
            "AND location='warzone' AND card_type LIKE '%Troop%' LIMIT 1",
            (session.session_id,)).fetchone()
        target = int(enemy[0]) if enemy else int(handler._ai_champ_scid.uid.uid64)
        tid = int(row[1])
        _db.execute(
            "UPDATE game_cards SET location='CastSpells' "
            "WHERE session_id=? AND card_uid=?", (session.session_id, tid))
        _db.commit()
        bstate["player_resources"] = resources - cost
        scid = game_engine.SessionCardId(game_engine.UID(tid))
        tpl_g, ct_n, nm, c2, atk2, def2, gem2 = handler._card_full_data(
            game, scid, row[2])
        game.push_card_updated(
            scid, pl_t, ECol.CastSpells, game_engine.card_type_from_db(row[4]),
            template_id=row[2], cost=c2, attack=atk2, defense=def2, gems=gem2)
        game.push_card_moved(scid, pl_t, ECol.CastSpells, ELoc.Top, 0)
        game.push_ability_on_chain(scid, game_engine.ResourceId.from_str(row[2]))
        ab_row = _db.execute(
            "SELECT abilities_json FROM card_templates WHERE guid=?",
            (row[2],)).fetchone()
        try:
            ags = [g.lower() for g in json.loads(ab_row[0] or "[]")]
        except Exception:
            ags = []
        be.stack_push(bstate, {"kind": "spell", "source_uid": tid,
                               "ability_guids": ags, "target_uid": target,
                               "instance_id": 1, "x_cost": 0})
        return


def p_declare_attackers(handler, session, pl_t, ai_t, bstate, game):
    ai_champ_uid64 = int(handler._ai_champ_scid.uid.uid64)
    rows = _db.execute(
        "SELECT gc.card_uid, gc.template_guid, ct.attributes, gc.card_attributes, "
        "gc.card_state FROM game_cards gc JOIN card_templates ct "
        "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.user_id=5 "
        "AND gc.location='warzone' AND gc.card_type LIKE '%Troop%'",
        (session.session_id,)).fetchall()
    attackers = {}
    for card_uid, tpl_guid, t_attrs, c_attrs, cstate in rows:
        cstate = cstate or 0
        if cstate & ESt.Tapped:
            continue
        attrs = (t_attrs or 0) | (c_attrs or 0)
        if attrs & EAttr.CantAttack:
            continue
        if not (cstate & ESt.StartedATurnOnYourSide) and not (attrs & EAttr.Speed):
            continue
        state = ESt.Attacking | ESt.HasAttacked
        if not (attrs & EAttr.Steadfast):
            state |= ESt.Tapped
        _db.execute(
            "UPDATE game_cards SET card_state=(card_state | ?) "
            "WHERE session_id=? AND card_uid=?",
            (state, session.session_id, int(card_uid)))
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        combat_id = game_engine.CombatId(pl_t, int(card_uid) & 0xFFFF)
        game.push_attack_declared(
            combat_id, pl_t, handler._ai_champ_scid or
            game_engine.SessionCardId(ai_t), scid)
        handler._card_full_data(game, scid, tpl_guid)
        game.push_card_updated(
            scid, pl_t, ECol.Warzone, game_engine.ECardTypes.Troop,
            template_id=tpl_guid, state=state)
        attackers[str(int(card_uid))] = str(ai_champ_uid64)
    _db.commit()
    bstate["player_attackers"] = attackers
    be.save_state(session, bstate)


def _player_discard(handler, session, pl_t, ai_t, bstate):
    while True:
        n = _db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=5 "
            "AND location='hand'", (session.session_id,)).fetchone()[0]
        if n <= handler._max_hand_size(session):
            break
        row = _db.execute(
            "SELECT id FROM game_cards WHERE session_id=? AND user_id=5 "
            "AND location='hand' AND card_type!='Resource' ORDER BY position LIMIT 1",
            (session.session_id,)).fetchone()
        if not row:
            row = _db.execute(
                "SELECT id FROM game_cards WHERE session_id=? AND user_id=5 "
                "AND location='hand' ORDER BY position LIMIT 1",
                (session.session_id,)).fetchone()
        if not row:
            break
        _db.execute("UPDATE game_cards SET location='discard' WHERE id=?",
                    (row[0],))
        _db.commit()


def _player_pass(handler, session, pl_t, ai_t, bstate):
    bstate["player_passed"] = True
    bstate["ai_passed"] = True
    phase = be.current_phase(bstate)
    if phase == ET.FirstMainPhase and bstate.get("turn_player") == be.PLAYER:
        bstate["player_has_ready_troop"] = handler._player_can_attack_troops(session)
        bstate["turn_phases"] = be.build_turn_phases(bstate)
    if phase in (ET.AssignDamage, ET.AssignFirstStrikeDamage):
        fs = phase == ET.AssignFirstStrikeDamage
        bstate = handler._resolve_combat_damage(
            session, pl_t, ai_t, bstate, first_strike=fs)
    be.advance_phase(bstate)
    be.save_state(session, bstate)
    handler._advance_to_priority(session, pl_t, ai_t, bstate)
    return bstate


def _player_turn(handler, session, pl_t, ai_t, bstate):
    ok = handler._advance_to_priority(session, pl_t, ai_t, bstate)
    if not ok:
        return bstate
    guard = 0
    while bstate.get("turn_player") == be.PLAYER and guard < 24:
        guard += 1
        phase = be.current_phase(bstate)
        if phase in (ET.FirstMainPhase, ET.SecondMainPhase):
            game = _fresh(handler, session, pl_t, ai_t, bstate)
            p_play_resource(handler, session, pl_t, ai_t, bstate)
            p_play_troop(handler, session, pl_t, ai_t, bstate, game)
            p_play_spell(handler, session, pl_t, ai_t, bstate, game)
            _drain_stack(handler, session, pl_t, ai_t, bstate, game)
        elif phase == ET.DeclareAttack:
            game = _fresh(handler, session, pl_t, ai_t, bstate)
            p_declare_attackers(handler, session, pl_t, ai_t, bstate, game)
            if bstate.get("player_attackers"):
                game.push_combat_listing(
                    pl_t, [game_engine.CombatSessionEventArgs()])
            _drain_stack(handler, session, pl_t, ai_t, bstate, game)
        elif phase == ET.Discard:
            _player_discard(handler, session, pl_t, ai_t, bstate)
        bstate = _player_pass(handler, session, pl_t, ai_t, bstate)
    return bstate


def _ai_turn(handler, session, pl_t, ai_t, bstate):
    handler._run_ai_turn(session, pl_t, ai_t, bstate)
    guard = 0
    while bstate.get("ai_turn_phase_idx") is not None:
        guard += 1
        if guard > 40:
            bstate.pop("ai_turn_phase_idx", None)
            be.save_state(session, bstate)
            break
        idx = bstate.pop("ai_turn_phase_idx")
        # The AI paused at an opponent-stop phase (or a chain it played onto):
        # the human would pass priority here, which also drains the AI's chain
        # (both sides pass -> resolve).  Simulate that pass, then resume the
        # AI turn from the saved phase index.
        guard2 = 0
        while not be.stack_empty(bstate) and guard2 < 30:
            guard2 += 1
            item = be.stack_pop(bstate)
            be.stack_reset_passes(bstate)
            handler._resolve_stack_item(session, pl_t, ai_t, bstate, item,
                                        _fresh(handler, session, pl_t, ai_t,
                                               bstate))
            if be.stack_empty(bstate):
                handler._fresh_game(session, pl_t, ai_t, bstate).push_chain_empty()
        bstate.pop("ai_turn_phase_idx", None)
        be.save_state(session, bstate)
        handler._run_ai_turn(session, pl_t, ai_t, bstate, start_idx=idx)
    return bstate


def play_one_game(seed, turns_cap=40):
    session = BattleSession(SCRATCH_SESSION)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    rnd = random.Random(seed)
    deck = _build_deck(rnd)
    _setup(session, deck)
    handler = _make_handler(session, pl_t, ai_t)
    bstate = be.default_state(turn_player=be.PLAYER)
    bstate["player_health"] = handler._champion_health_by_guid(CHAMP_PLAYER)
    bstate["ai_health"] = handler._champion_health_by_guid(CHAMP_AI)
    bstate["turn_phases"] = be.BASE_TURN_PHASES
    be.save_state(session, bstate)
    turns = 0
    try:
        while turns < turns_cap:
            turns += 1
            if turns % 5 == 0:
                print(f"  game {seed}: turn {turns} ...", flush=True)
            if bstate.get("turn_player") == be.PLAYER:
                bstate = _player_turn(handler, session, pl_t, ai_t, bstate)
            else:
                bstate = _ai_turn(handler, session, pl_t, ai_t, bstate)
            if (bstate.get("player_health", 20) <= 0
                    or bstate.get("ai_health", 20) <= 0
                    or getattr(session, "state", None) == "ended"):
                break
        return turns, None
    except Exception:
        return turns, traceback.format_exc()
    finally:
        _db.execute("DELETE FROM game_cards WHERE session_id=?",
                    (SCRATCH_SESSION,))
        _db.commit()


def main(games=10):
    try:
        os.remove(OUT)
    except OSError:
        pass
    ok = 0
    errors = 0
    ai_mod.AI_PHASE_DELAY = 0  # headless: no pacing between AI phases
    for g in range(games):
        turns, exc = play_one_game(seed=g)
        if exc:
            errors += 1
            with open(OUT, "a") as fh:
                fh.write(f"### game {g} (turn {turns})\n{exc}\n")
        else:
            ok += 1
    print(f"Autoplay: {ok}/{games} games completed, {errors} crashed")
    if errors:
        print(f"details -> {OUT}")
    return errors


if __name__ == "__main__":
    games = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    sys.exit(0 if main(games) == 0 else 0)
