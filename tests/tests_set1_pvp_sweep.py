"""Set 1 PvP transaction/packet smoke sweep.

This is deliberately separate from ``tests_set1_sweep.py``.  Each Set 1
ability is placed into a temporary two-player tournament session and routed
through ``services.tournament_game.pvp_handle_transaction``.  Permanent plays
and spells are resolved by PvP priority passes; manual troop abilities are
submitted as activation transactions.  Triggered abilities whose event is not
an enter-play event also receive a representative event and resolve through
the PvP chain when applicable.

The sweep proves that the PvP handler can route the action and serialize the
resulting event stream for both players.  It separately reports actions that
were rejected by PvP legality because the generic fixture lacked a required
target or combat phase.  It is still a smoke test: exact card rules and every
possible target/priority branch need focused tests.
"""

import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import db as dbmod
import game_engine
import services.tournament_game as tournament_game
from tests.tests_combat import HandlerStub as BaseHandler
from tests.tests_set1_sweep import (
    SRC, SET1, _clear_and_seed, _plain_troop, _set1_abilities,
)


PIDS = (1001, 1002)
SESSION_ID = 1


class SweepSession:
    session_id = SESSION_ID
    server_id = 100
    session_name = "tourney-set1-pvp-sweep"

    def __init__(self):
        self.turn_order = {}

    def _persist(self):
        pass


class PvPHandler(BaseHandler):
    """Small HCP handler surface used by the tournament service."""

    def __init__(self, db, pid, opponent_pid):
        super().__init__(db)
        self.client_reck_id = pid
        self.user_profile = {"id": pid}
        self._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, pid))
        self._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, opponent_pid))

    def _champion_targets(self):
        state = getattr(self, "_current_bstate", None) or {}
        return [
            (int(self._player_champ_scid.uid.uid64), self.client_reck_id,
             "Player", int(state.get(f"hp_{self.client_reck_id}", 20))),
            (int(self._ai_champ_scid.uid.uid64),
             next(p for p in PIDS if p != self.client_reck_id),
             "Opponent", int(state.get(
                 f"hp_{next(p for p in PIDS if p != self.client_reck_id)}", 20))),
        ]

    def _extract_transaction_targets(self, raw, exclude_uid):
        out = []
        for match in re.finditer(
                rb"m_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});", raw):
            value = struct.unpack("<Q", bytes.fromhex(match.group(1).decode()))[0]
            if (value & 0xFF) == 1 and int(value) != int(exclude_uid):
                out.append(int(value))
        return out

    def _extract_int32_field(self, raw, field):
        return 0

    def _shards_of_fate_template(self, ability_guids):
        return None, None

    def _resolve_shards_of_fate(self, *args, **kwargs):
        return "resolved"

    def _player_draw_card(self, game, session, pl_t, owner_id=None):
        owner_id = self.client_reck_id if owner_id is None else int(owner_id)
        row = self._db.execute(
            "SELECT id, card_uid, template_guid FROM game_cards "
            "WHERE session_id=? AND user_id=? AND location='deck' "
            "ORDER BY position LIMIT 1",
            (session.session_id, owner_id)).fetchone()
        if not row:
            return None
        self._db.execute(
            "UPDATE game_cards SET location='hand', position=100 WHERE id=?",
            (row[0],))
        self._db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(row[1]))
        tpl = row[2]
        _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(game, scid, tpl)
        game.push_card_moved(scid, pl_t, game_engine.ECardCollections.Hand,
                             game_engine.ECardLocations.Top, 1)
        game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand,
                               ct, template_id=tpl)
        return None


def _wire_uid(uid):
    return struct.pack("<Q", int(uid)).hex().encode("ascii")


def _play_transaction(uid):
    return (b"m_SessionCardId;0;1;1;m_UID64;1;2;0;" +
            _wire_uid(uid) + b";")


def _ability_transaction(ability_guid, source_uid, target_uid=None):
    raw = (b"m_AbilityActivationData;AbilityTemplateId;" +
           ability_guid.encode("ascii") + b";"
           b"m_UID64;0;0;0;" + _wire_uid(source_uid) + b";")
    if target_uid is not None:
        raw += b"m_UID64;0;0;0;" + _wire_uid(target_uid) + b";"
    return raw


def _remap_session(db):
    """Give every fixture card a Card UID whose low byte is Card=1."""
    rows = db.execute(
        "SELECT id, card_uid, user_id FROM game_cards WHERE session_id=?",
        (SESSION_ID,)).fetchall()
    for row_id, old_uid, owner in rows:
        new_uid = (int(old_uid) << 8) | 1
        new_owner = PIDS[0] if owner == 5 else PIDS[1]
        db.execute(
            "UPDATE game_cards SET card_uid=?, user_id=? "
            "WHERE id=?", (new_uid, new_owner, row_id))
    db.commit()
    return (101 << 8) | 1


def _state(session):
    return {
        "pvp": True,
        "pids": list(PIDS),
        "turn_pid": PIDS[0],
        "priority_pid": PIDS[0],
        "phase": int(game_engine.ETurnPhases.FirstMainPhase),
        "passes": [],
        "stack_passed": [],
        "_next_instance_id": 1,
        "champ_map": {str(PIDS[0]): (0x1001 << 8) | 244,
                       str(PIDS[1]): (0x1002 << 8) | 244},
        "hp_1001": 20, "hp_1002": 20,
        "res_1001": 99, "res_1002": 99,
        "res_total_1001": 99, "res_total_1002": 99,
        "chg_1001": 99, "chg_1002": 99,
        "thresh_1001": {4: 99, 8: 99, 16: 99, 32: 99, 64: 99},
        "thresh_1002": {4: 99, 8: 99, 16: 99, 32: 99, 64: 99},
    }


def _pass_stack(session, handlers, limit=12):
    """Pass the current PvP priority until the stack is empty."""
    for _ in range(limit):
        state = session.turn_order
        if not state.get("stack"):
            return
        pid = int(state.get("priority_pid", PIDS[0]))
        if not tournament_game.route_pvp_pass(handlers[pid], session):
            raise RuntimeError(f"PvP pass was rejected for pid {pid}")
    if session.turn_order.get("stack"):
        raise RuntimeError("PvP stack did not drain")


def _representative_event(db, handler, session, source_uid, event_type):
    """Fire a representative event for non-enter-play triggers."""
    state = session.turn_order
    handler._current_bstate = state
    pl_t = game_engine.UID.make(244, PIDS[0])
    ai_t = game_engine.UID.make(244, PIDS[1])
    game = game_engine.Game(SESSION_ID, pl_t, ai_t)
    extra = None
    event_source = source_uid
    if event_type == "CardDrawnEvent":
        event_source = state["champ_map"][str(PIDS[0])]
        extra = source_uid
    elif event_type == "CardDealtDamageEvent":
        extra = state["champ_map"][str(PIDS[1])]
    elif event_type in ("TurnStartedEvent", "TurnEndedEvent",
                        "GameStartedEvent", "CardCreatedEvent",
                        "CardWouldBeDrawnEvent"):
        event_source = None
        extra = source_uid
    from abilities.framework.triggers import resolve_triggers
    resolve_triggers(db, handler, game, session, pl_t, ai_t, state,
                     event_type, event_source, PIDS[0], extra_target=extra)
    if state.get("stack"):
        _pass_stack(session, {PIDS[0]: handler,
                               PIDS[1]: handler._opponent_handler})


def _run_one(db, session, handlers, handler, ability_guid, card_name,
             plain_guid):
    row = db.execute(
        "SELECT guid, card_type FROM card_templates WHERE set_guid=? "
        "AND abilities_json LIKE ? LIMIT 1",
        (SET1, f'%"{ability_guid}"%')).fetchone()
    if not row:
        return {"status": "skip", "reason": "template not found"}
    tpl, card_type = row
    _clear_and_seed(db, tpl, ability_guid, plain_guid)
    source_uid = _remap_session(db)
    state = _state(session)
    session.turn_order = state
    # All generic friendly troops are ready and valid targets.
    db.execute(
        "UPDATE game_cards SET card_state=? WHERE session_id=? "
        "AND user_id=? AND location='warzone'",
        (int(game_engine.ECardStates.StartedATurnOnYourSide),
         SESSION_ID, PIDS[0]))
    db.commit()
    meta = db.execute(
        "SELECT is_manual, trigger_event_type FROM card_abilities_meta "
        "WHERE ability_guid=?", (ability_guid,)).fetchone()
    is_manual, event_type = meta if meta else (0, "")
    source_location = "warzone" if (is_manual and card_type == "Troop") \
        else "hand"
    db.execute(
        "UPDATE game_cards SET location=? WHERE session_id=? AND card_uid=?",
        (source_location, SESSION_ID, source_uid))
    db.commit()
    before = db.execute(
        "SELECT location, card_state, card_uses FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (SESSION_ID, source_uid)
    ).fetchone()
    before_resources = int(state.get("res_1001", 0))
    handler._current_bstate = state
    if source_location == "warzone":
        target_uid = (102 << 8) | 1
        raw = _ability_transaction(ability_guid, source_uid, target_uid)
    else:
        raw = _play_transaction(source_uid)
    handled = tournament_game.pvp_handle_transaction(handler, session, raw)
    after = db.execute(
        "SELECT location, card_state, card_uses FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (SESSION_ID, source_uid)
    ).fetchone()
    accepted = bool(handled)
    if source_location == "warzone":
        # Successful manual activations bump card_uses and/or consume a
        # resource/exhaust the source.  A PvP legality rejection returns True
        # as an acknowledged transaction but leaves all of these unchanged.
        accepted = accepted and (
            after[2] != before[2]
            or after[1] != before[1]
            or int(state.get("res_1001", 0)) != before_resources)
    else:
        accepted = accepted and after[0] != source_location
    # Card plays resolve through the actual both-pass PvP chain.
    if accepted and session.turn_order.get("stack"):
        _pass_stack(session, handlers)
    # Exercise non-enter triggers after the source is in the warzone.  The
    # event is synthetic, but its stack resolution and packet delivery are PvP.
    if accepted and event_type and "EntersPlayEvent" not in event_type \
            and event_type != "CardEnteredZoneEvent":
        _representative_event(db, handler, session, source_uid, event_type)
    return {"status": "pass" if accepted else ("rejected" if handled else "skip"),
            "handled": bool(handled), "accepted": bool(accepted),
            "card": card_name,
            "ability": ability_guid, "type": card_type}


def run_sweep():
    fd, temp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy(SRC, temp_path)
    db = sqlite3.connect(temp_path)
    session = SweepSession()
    packets = []
    old_db = dbmod._db
    old_tg_db = tournament_game._db
    old_pids = tournament_game.db_game_session_pids
    old_handlers = tournament_game.player_handlers
    old_send = tournament_game._send_pvp_packet
    try:
        dbmod._db = db
        tournament_game._db = db
        tournament_game.db_game_session_pids = lambda _sid: list(PIDS)
        handlers = {
            PIDS[0]: PvPHandler(db, PIDS[0], PIDS[1]),
            PIDS[1]: PvPHandler(db, PIDS[1], PIDS[0]),
        }
        handlers[PIDS[0]]._opponent_handler = handlers[PIDS[1]]
        handlers[PIDS[1]]._opponent_handler = handlers[PIDS[0]]
        tournament_game.player_handlers = handlers

        def capture(handler, sess, game, recipient, label):
            # Exercise the same packet serializer used by _send_pvp_packet;
            # retaining both recipient streams makes asymmetric failures clear.
            from encoder import encode_sync_event
            packet = game.make_network_packet(recipient)
            payload = encode_sync_event(packet)
            packets.append((int(recipient.uid64), label, len(payload)))

        tournament_game._send_pvp_packet = capture
        abilities = _set1_abilities(db)
        plain = _plain_troop(db)
        results = []
        for ability_guid, card_name in sorted(abilities.items()):
            try:
                results.append(_run_one(
                    db, session, handlers, handlers[PIDS[0]],
                    ability_guid, card_name, plain))
            except Exception as exc:
                results.append({"status": "fail", "card": card_name,
                                "ability": ability_guid,
                                "error": f"{type(exc).__name__}: {exc}"})
    finally:
        tournament_game._send_pvp_packet = old_send
        tournament_game.player_handlers = old_handlers
        tournament_game.db_game_session_pids = old_pids
        tournament_game._db = old_tg_db
        dbmod._db = old_db
        db.close()
        try:
            os.remove(temp_path)
        except OSError:
            pass
    passed = sum(r["status"] == "pass" for r in results)
    rejected = [r for r in results if r["status"] == "rejected"]
    skipped = sum(r["status"] == "skip" for r in results)
    failed = [r for r in results if r["status"] == "fail"]
    print(f"Set 1 PvP handler sweep: {passed}/{len(results)} accepted; "
          f"{len(rejected)} rejected by PvP legality; {skipped} skipped; "
          f"{len(failed)} failed")
    print(f"Serialized PvP packets: {len(packets)} "
          f"({sum(1 for p in packets if p[0] >> 8 == PIDS[0])} player / "
          f"{sum(1 for p in packets if p[0] >> 8 == PIDS[1])} opponent)")
    for result in failed:
        print(f"FAIL {result['card']} {result['ability']}: {result['error']}")
    for result in rejected:
        print(f"REJECTED {result['card']} {result['ability']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_sweep())
