"""Headless PvP runner for engine smoke checks and FRA AI duels.

Drives a real tournament PvP session (tourney-N) through the production
paths: 3029 PassPriority / ChoosePlay / AcceptStartingHand transactions,
resource + troop plays, attacker/blocker declarations and combat — with two
fake HCPHandlers pushing events to no client.  The goal is to catch crashes
and stuck phases in the PvP state machine (GreenLight sync, phase wrapping,
combat).

Run the original Set 1 smoke runner with ``python3 pvp_autoplay.py [games]``.
Run two FRA opponents with ``python3 pvp_autoplay.py --fra [GUID GUID]
--turn-limit 30 --profile``. If no GUIDs are given, the first two authored
encounters with complete decks are used.
"""

import os
import argparse
import json
import random
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter

# A command-line simulation should never clean up rows in the server's live
# database.  SQLite's online backup includes committed WAL state and gives the
# runner a disposable snapshot with the authored metadata it needs.
_SIMULATION_DB_TEMP = None
_SIMULATION_DB_COPY = None
if __name__ == "__main__" and "--fra" in sys.argv:
    _source_db = os.environ.get(
        "HEX_DB_PATH", os.path.join(os.path.dirname(__file__), "hconnect.db"))
    if _source_db != ":memory:" and os.path.isfile(_source_db):
        _SIMULATION_DB_TEMP = tempfile.TemporaryDirectory(
            prefix="hex-pvp-simulation-")
        _SIMULATION_DB_COPY = os.path.join(
            _SIMULATION_DB_TEMP.name, "simulation.db")
        _source_connection = sqlite3.connect(_source_db, timeout=30)
        _copy_connection = sqlite3.connect(_SIMULATION_DB_COPY, timeout=30)
        try:
            _source_connection.backup(_copy_connection)
        finally:
            _copy_connection.close()
            _source_connection.close()
        os.environ["HEX_DB_PATH"] = _SIMULATION_DB_COPY

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import game_engine
import game_session as gs
import hconnect_server as hcs
import encoder
from db import _db, log_req
from pvp_db import (
    db_ai_hand_playables, db_clear_session_cards,
    db_copy_template_payload, db_delete_game_session,
    db_hand_resources_with_template, db_insert_generated_card,
    db_next_game_card_row_id, db_set_constructed_guids, db_set_resource_guids,
    db_gem_templates, db_static_card_rows, db_warzone_troop_attributes,
    db_zone_card_count, db_encounter_deck_cards, db_is_champion_template,
    db_warzone_attack_candidates,
)
from domain.constants import (CARD_UID_TYPE, PLAYER_UID_TYPE,
                              PLAYER_TRANSACTION_DATA_TYPE,
                              SESSION_CARD_UID_FIELD_TYPE)
from services.tournament_game import (
    pvp_default_state, pvp_load_state, pvp_save_state,
    handle_ready_for_game_setup, handle_ready_for_game_events,
    player_handlers,
)

SET1 = "0382f729-7710-432b-b761-13677982dcd2"
SCRATCH_SESSION_BASE = 987700
AUTOPLAY_PLAYER_IDS = (5, 6)
AUTOPLAY_SERVER_ID_MULTIPLIER = 7
AUTOPLAY_DECK_RESOURCE_COUNT = 12
AUTOPLAY_DECK_SPELL_COUNT = 28
AUTOPLAY_CHAMPION_UID_OFFSET = 9000
AUTOPLAY_PLAYER_UID_STRIDE = 10000
AUTOPLAY_TURN_CAP = 60
AUTOPLAY_GUARD_CAP = 1200
AUTOPLAY_MULLIGAN_ROUNDS = 6
AUTOPLAY_REDRAW_ROUNDS = (0, 1)
OPENING_SETUP_PHASES = (game_engine.ETurnPhases.PickGoesFirst,
                         game_engine.ETurnPhases.Mulligan)
CARD_POSITION_CHAMPION = 0
DEFAULT_AUTOPLAY_GAMES = 5
FIRST_AUTOPLAY_SEED = 1


def _cleanup(session_id, pids):
    db_clear_session_cards(session_id)
    db_delete_game_session(session_id)
    for pid in pids:
        player_handlers.pop(pid, None)


def _make_handler(pid, session):
    h = object.__new__(hcs.HCPHandler)
    h.user_profile = {"id": pid, "name": f"P{pid}"}
    h.client_reck_id = pid
    h.sid = f"pvp-{pid}"
    h.scnt = 0
    h.ccnt = 0
    h._game_scnt = 0
    h._event_q = []
    h._svc_scnt = {}
    h.client_uid = f"pvp-{pid}"
    h._ai_turn_depth = 0
    h._current_bstate = None
    setattr(h, "_player_autopass", False)
    h._pending_player_stops = None
    h._pending_player_draws_first = None
    h._player_champ_scid = None
    h._ai_champ_scid = None
    h._player_champ_guid = None
    h._ai_champ_guid = None
    h._player_starting_health = game_engine.DEFAULT_STARTING_HEALTH
    h._ai_starting_health = game_engine.DEFAULT_STARTING_HEALTH
    setattr(h, "_autoplay_drive_ai_turn", True)
    setattr(h, "_campaign_gameend", lambda *a, **k: None)
    # Native mode exercises the same PvP RulesPort attachment used by HConnect.
    h._application = hcs.ApplicationCommandDispatcher(
        event_publisher=h._publish_application_events)
    h.send = lambda *a, **k: None
    h.send_and_cache = lambda *a, **k: None
    h._push_transaction_ack = lambda *a, **k: None
    return h


def _seed_deck(session_id, pid, deck, uid_offset=0):
    ctr = [uid_offset]
    for pos, card_spec in enumerate(deck):
        if isinstance(card_spec, (tuple, list)):
            tpl, gem_type, gem_abilities = card_spec
        else:
            tpl, gem_type, gem_abilities = card_spec, 0, ()
        ctr[0] += 1
        cu = game_engine.UID.make(CARD_UID_TYPE, ctr[0]).uid64
        card = db_copy_template_payload(tpl)
        if not card:
            raise ValueError(f"unknown autoplay template {tpl}")
        try:
            abilities = [str(value).lower()
                         for value in json.loads(card[1] or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            abilities = []
        for ability_guid in gem_abilities or ():
            value = str(ability_guid).lower()
            if value not in abilities:
                abilities.append(value)
        db_insert_generated_card(
            session_id, pid, cu, tpl, "deck", card[0],
            json.dumps(abilities), card[2],
            db_next_game_card_row_id(session_id), position=pos,
            gems=int(gem_type or 0))


def _seed_champion(session_id, pid, champ_guid, uid_offset=0):
    ctr = [uid_offset]
    ctr[0] += 1
    cu = game_engine.UID.make(CARD_UID_TYPE, ctr[0]).uid64
    card = db_copy_template_payload(champ_guid)
    is_catalog_champion = db_is_champion_template(champ_guid)
    if not card and not is_catalog_champion:
        raise ValueError(f"unknown autoplay champion {champ_guid}")
    # Authored FRA champions are stored in champion_templates(_extended),
    # not necessarily in card_templates.  The production PvP projection uses
    # this same catalog check and obtains signature abilities from
    # champion_abilities when it builds the champion card.
    card_type, abilities, attributes = (
        ("Champion", "[]", 0) if is_catalog_champion else card)
    db_insert_generated_card(
        session_id, pid, cu, champ_guid, "champions", card_type, abilities,
        attributes, db_next_game_card_row_id(session_id),
        position=CARD_POSITION_CHAMPION)
    _db.execute(
        "UPDATE game_cards SET is_champion=1 "
        "WHERE session_id=? AND card_uid=?", (session_id, int(cu)))
    return int(cu)


def _transaction(handler, session, inner_bytes, typed_payload=None,
                 profiler=None):
    """Push a 3029 PlayerTransaction through the production handler.

    ``inner_obj`` carries the labelled raw envelope so the handler recovers
    the same typed payload it recovers when ObjFmt decoding of a live request
    fails; without it the harness would only exercise the untyped ingress.
    """
    import game_engine as _ge
    # The 3029 handler reloads the session via find_session_by_player; use the
    # same lookup so our view of the state stays in sync with the DB.
    pid = int(handler.client_reck_id)
    if profiler is None:
        profiler = getattr(handler, "_autoplay_profiler", None)
    session = gs.find_session_by_player(_ge.UID.make(PLAYER_UID_TYPE, pid).to_uint64()) \
        or session
    if os.environ.get("PVP_TRACE"):
        to = session.turn_order
        log_req(f"    [pvp-trace] pre-tx session={session.session_id} "
                f"name={session.session_name} turn_order_type="
                f"{type(to).__name__} keys="
                f"{list(to.keys())[:6] if isinstance(to, dict) else 'n/a'}")
    inner_obj = {"__raw__": inner_bytes}
    if isinstance(typed_payload, dict):
        # Add the same labeled decoder fields that the client request parser
        # supplies. This supports target selections without bypassing the
        # production application dispatcher or PvP RulesPort adapter.
        inner_obj.update(typed_payload)
    started = time.perf_counter()
    try:
        handler.handle_service_request(
            "ServiceGameSession", str(session.server_id),
            PLAYER_TRANSACTION_DATA_TYPE, 1, 1,
            session.session_id, 0, inner_obj, inner_bytes)
    finally:
        if profiler is not None:
            _profile_add(profiler, "rules_transactions", 1)
            _profile_add(profiler, "rules_transaction_seconds",
                         time.perf_counter() - started)
    cur = gs.find_session_by_player(
        _ge.UID.make(PLAYER_UID_TYPE, int(handler.client_reck_id)).to_uint64()) or session
    port = getattr(cur, "_rules_port_session", None)
    if port is not None and profiler is not None:
        port._transaction_profile_callback = (
            lambda kind, elapsed: _record_rules_port_dispatch(
                profiler, kind, elapsed))
    if os.environ.get("PVP_TRACE"):
        if isinstance(cur.turn_order, dict) and "turn_player" in cur.turn_order:
            log_req(f"    [pvp-trace] after {inner_bytes[:36]!r} — turn_order "
                    f"CORRUPTED keys={list(cur.turn_order.keys())[:6]}")
        else:
            log_req(f"    [pvp-trace] after {inner_bytes[:36]!r} — ok "
                    f"phase={cur.turn_order.get('phase') if isinstance(cur.turn_order, dict) else '?'}")
    return cur


def _mk_uid_bytes(uid):
    import struct
    from binascii import hexlify
    # ObjFmt field: m_UID64;<idx>;<type>;0;<little-endian-hex>;
    return (b"m_UID64;0;" + str(SESSION_CARD_UID_FIELD_TYPE).encode()
            + b";0;" + hexlify(struct.pack("<Q", int(uid))) + b";")


# The stock client submits one Play<CardType>Transaction per card (there is no
# generic play envelope), so the harness must name the same class the client
# would or its plays fall outside the typed RulesPort ingress.  Keyed by
# ``card_templates.card_type``; a combined type uses its first listed token.
_PLAY_TRANSACTION_BY_DB_TYPE = {
    "Champion": b"PlayChampionTransaction",
    "BasicAction": b"PlaySpellTransaction",
    "QuickAction": b"PlaySpellTransaction",
    "Troop": b"PlayTroopTransaction",
    "Artifact": b"PlayArtifactTransaction",
    "Constant": b"PlayArtifactTransaction",
    "Resource": b"PlayResourceTransaction",
}


def _card_play_bytes(card_uid, card_type):
    """Client-shaped play transaction for one hand card of ``card_type``."""
    for token in str(card_type or "").split("|"):
        transaction = _PLAY_TRANSACTION_BY_DB_TYPE.get(token.strip())
        if transaction is not None:
            return transaction + b";m_SessionCardId;" + _mk_uid_bytes(card_uid)
    raise ValueError(f"autoplay cannot play card type {card_type!r}")


def _attack_bytes(attacker_uids, champ_uid):
    out = b"CommitTroopsToAttackTransaction;"
    for u in attacker_uids:
        out += _mk_uid_bytes(u)
    out += _mk_uid_bytes(champ_uid)
    return out


def _defense_bytes(attacker_uids, blocker_map, champ_uid):
    out = b"CommitTroopsToDefenseTransaction;"
    for a in attacker_uids:
        out += _mk_uid_bytes(a)
        for b in blocker_map.get(int(a), ()):
            out += _mk_uid_bytes(b)
    out += _mk_uid_bytes(champ_uid)
    return out


def _fra_encounter_specs(deck_guids=None):
    """Load two authored FRA lists as disposable PvP decks."""
    rows = _db.execute(
        "SELECT deck_guid, deck_name, champion_guid FROM fra_encounters "
        "ORDER BY deck_name, deck_guid").fetchall()
    by_guid = {str(row[0]).lower(): row for row in rows}
    if deck_guids:
        chosen = []
        for guid in deck_guids:
            row = by_guid.get(str(guid).lower())
            if row is None:
                raise ValueError(f"unknown FRA encounter deck {guid!r}")
            chosen.append(row)
    else:
        chosen = rows

    specs = []
    gem_templates = {
        str(name): (int(gem_type or 0),
                    [str(value).lower() for value in
                     json.loads(abilities_json or "[]")])
        for name, gem_type, abilities_json in db_gem_templates(conn=_db)
    }
    for deck_guid, name, champion_guid in chosen:
        cards = []
        for card_guid, quantity, gem_json in db_encounter_deck_cards(
                deck_guid, conn=_db):
            try:
                gem_slots = json.loads(gem_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                gem_slots = []
            for copy_index in range(max(0, int(quantity or 0))):
                socket_names = (gem_slots[copy_index]
                                if copy_index < len(gem_slots) else [])
                if not isinstance(socket_names, list):
                    socket_names = [socket_names]
                gem_type = 0
                gem_abilities = []
                for socket_name in socket_names:
                    gem_data = gem_templates.get(str(socket_name))
                    if not gem_data:
                        continue
                    if not gem_type:
                        gem_type = gem_data[0]
                    for ability_guid in gem_data[1]:
                        if ability_guid not in gem_abilities:
                            gem_abilities.append(ability_guid)
                cards.append((str(card_guid), gem_type, gem_abilities))
        if not cards or not champion_guid:
            continue
        specs.append({"deck_guid": str(deck_guid), "name": str(name),
                      "champion_guid": str(champion_guid), "cards": cards})
        if len(specs) == 2:
            break
    if len(specs) != 2:
        raise ValueError("need two FRA encounter decks with champions and cards")
    return specs


def _ai_evaluator_for_seat(handler, session, state, owner_id, opponent_id,
                           profiler, report):
    """Build the existing hand evaluator with this PvP seat as its AI side."""
    from ai_eval import build_evaluator

    champ_map = state.get("champ_map") or {}
    own_champ = int(champ_map.get(str(owner_id), 0) or 0)
    opp_champ = int(champ_map.get(str(opponent_id), 0) or 0)
    handler.user_profile = {"id": int(owner_id), "name": f"FRA-{owner_id}"}
    handler._ai_champ_scid = (game_engine.SessionCardId(
        game_engine.UID(own_champ)) if own_champ else None)
    handler._player_champ_scid = (game_engine.SessionCardId(
        game_engine.UID(opp_champ)) if opp_champ else None)

    view = dict(state)
    view.update({
        "ai_resources": int(state.get(f"res_{owner_id}", 0) or 0),
        "ai_total_resources": int(
            state.get(f"res_total_{owner_id}", 0) or 0),
        "ai_threshold": state.get(f"thresh_{owner_id}") or {},
        "ai_health": int(state.get(f"hp_{owner_id}", 20) or 0),
        "player_health": int(state.get(f"hp_{opponent_id}", 20) or 0),
    })
    handler._current_bstate = view
    evaluator = build_evaluator(
        handler, session, view,
        game_engine.UID.make(PLAYER_UID_TYPE, owner_id),
        game_engine.UID.make(PLAYER_UID_TYPE, opponent_id),
        ai_owner_id=owner_id, player_owner_id=opponent_id)
    return evaluator


def _record_card_decision(report, evaluator, chosen, choice_reason,
                          only_quick_actions=False):
    if report is None:
        return
    owner_id = int(evaluator.ai_owner_id)
    decisions = report.setdefault("card_decisions", {}).setdefault(
        str(owner_id), {})
    for card in evaluator.hand:
        if only_quick_actions and not card.is_quick_action():
            continue
        key = f"{card.template_guid}:{card.name}"
        entry = decisions.setdefault(key, {
            "template_guid": card.template_guid,
            "name": card.name,
            "seen": 0,
            "selected": 0,
            "played": 0,
            "reasons": Counter(),
        })
        entry["seen"] += 1
        playability = evaluator.is_playable(card)
        if playability == "False":
            if not evaluator.handler._thresholds_met(
                    card.threshold_json, evaluator.threshold):
                reason = "threshold_unavailable"
            elif (card.is_action()
                  and evaluator.has_required_explicit_target(card)
                  and evaluator.choose_action_target(card) is None):
                reason = "required_target_unavailable"
            else:
                reason = "other_legality_or_target_unavailable"
        elif playability == "NeedsResources":
            reason = "needs_resources"
        elif card is chosen:
            reason = choice_reason or "selected"
            entry["selected"] += 1
        elif card.is_resource():
            reason = "resource_slot_used_or_not_selected"
        elif card.is_quick_action():
            reason = (choice_reason if only_quick_actions else
                      "quick_action_not_selected_in_main_phase")
        elif chosen is None:
            reason = "no_preferred_action"
        else:
            reason = "lower_priority_than_selected"
        entry["reasons"][reason] += 1


def _record_resource_decision(report, owner_id, row, played):
    """Account for a resource choice made before the hand evaluator runs."""
    if report is None:
        return
    template_guid = str(row[2])
    card_uid = int(row[1])
    name_row = _db.execute(
        "SELECT name FROM card_templates WHERE guid=?", (template_guid,)
    ).fetchone()
    name = str(name_row[0] if name_row else template_guid)
    key = f"{template_guid}:{name}"
    entry = report.setdefault("card_decisions", {}).setdefault(
        str(int(owner_id)), {}).setdefault(key, {
            "template_guid": template_guid,
            "name": name,
            "seen": 0,
            "selected": 0,
            "played": 0,
            "reasons": Counter(),
        })
    entry["seen"] += 1
    entry["selected"] += 1
    entry["reasons"]["resource_play"] += 1
    if played:
        entry["played"] += 1
    else:
        entry["reasons"]["selected_but_not_played"] += 1


def _record_failed_card_play(report, owner_id, chosen):
    if report is None or chosen is None:
        return
    key = f"{chosen.template_guid}:{chosen.name}"
    entry = report.get("card_decisions", {}).get(str(int(owner_id)), {}).get(key)
    if entry is not None:
        entry["reasons"]["selected_but_not_played"] += 1


def _effective_combat_attributes(session_id, owner_id, battle_state, cards):
    """Refresh evaluator cards with dynamic card attributes for combat."""
    from rules_port.static_rules import effective_stats

    by_uid = {card.card_uid: card for card in cards}
    for row in db_warzone_troop_attributes(session_id, owner_id):
        card_uid = int(row[0])
        card = by_uid.get(card_uid)
        if card is None:
            continue
        card.attributes = int(effective_stats(
            _db, session_id, battle_state, card_uid)[2] or 0)
        card.card_state = int(row[1] or 0)
    return by_uid


def _ai_attack_decision(handler, session, state, owner_id, opponent_id,
                        profiler=None, report=None):
    """Use the shared AI combat valuation for a FRA seat's attack choice."""
    started = time.perf_counter()
    evaluator = _ai_evaluator_for_seat(
        handler, session, state, owner_id, opponent_id, profiler, report)
    import ai as ai_policy

    own_cards = _effective_combat_attributes(
        session.session_id, owner_id, state, evaluator.ai_warzone)
    opposing_cards = _effective_combat_attributes(
        session.session_id, opponent_id, state, evaluator.player_warzone)
    blockers = [card for card in opposing_cards.values()
                if card.is_troop()
                and not (int(card.card_state or 0)
                         & game_engine.ECardStates.Tapped)
                and not card.has_attribute(game_engine.ECardAttributes.CantBlock)]

    eligible = []
    for card_uid, _template, attrs, _card_attrs, card_state, _attack in \
            db_warzone_attack_candidates(session.session_id, owner_id):
        card_uid = int(card_uid)
        card = own_cards.get(card_uid)
        if card is None:
            continue
        card_state = int(card_state or 0)
        attrs = int(attrs or 0) | int(_card_attrs or 0) | int(
            card.attributes or 0)
        if card_state & game_engine.ECardStates.Tapped:
            continue
        if attrs & (game_engine.ECardAttributes.CantAttack |
                    game_engine.ECardAttributes.Defensive):
            continue
        if not (card_state & game_engine.ECardStates.StartedATurnOnYourSide) \
                and not attrs & game_engine.ECardAttributes.Speed:
            continue
        card.attributes = attrs
        eligible.append(card)

    chosen = []
    reason = "no_eligible_attackers"
    personality = ai_policy.personality(handler)
    min_value = int(personality.get("min_x_value", 3) or 3)
    alpha = bool(personality.get("alpha_strike", True))
    if eligible and not blockers:
        chosen = eligible
        reason = "open_attack"
    elif eligible and evaluator.alpha_strike_wins(
            int(state.get(f"hp_{opponent_id}", 20) or 0), eligible, blockers):
        chosen = eligible
        reason = "alpha_strike_lethal"
    elif eligible and alpha and ai_policy._aieval_attack_set_value(
            evaluator, eligible, blockers) > 0:
        chosen = eligible
        reason = "profitable_group_attack"
    elif eligible:
        for card in eligible:
            if card.has_attribute(game_engine.ECardAttributes.ForceAttack):
                chosen.append(card)
                continue
            attack = card.effective_attack(in_play=True)
            if attack <= 0:
                continue
            _damage, value = ai_policy._aieval_best_attack_value(
                evaluator, card, blockers)
            if value > 0 and (attack >= min_value or alpha):
                chosen.append(card)
        reason = "positive_individual_combat_value" if chosen \
            else "combat_value_below_threshold"

    if profiler is not None:
        _profile_add(profiler, "combat_evaluations", 1)
        _profile_add(profiler, "combat_evaluation_seconds",
                     time.perf_counter() - started)
    if report is not None:
        report.setdefault("combat_decisions", []).append({
            "turn": int(state.get("turn", 0) or 0),
            "player_id": int(owner_id),
            "kind": "attack",
            "reason": reason,
            "attackers": [int(card.card_uid) for card in chosen],
            "eligible": [int(card.card_uid) for card in eligible],
            "blockers": [int(card.card_uid) for card in blockers],
        })
    return [int(card.card_uid) for card in chosen], reason


def _ai_block_decision(handler, session, state, owner_id, opponent_id,
                       attacker_uids, profiler=None, report=None):
    """Select legal, value-positive one-to-one blocks with AICombat values."""
    started = time.perf_counter()
    evaluator = _ai_evaluator_for_seat(
        handler, session, state, owner_id, opponent_id, profiler, report)
    from ai_eval import ECardAttributes

    own_cards = _effective_combat_attributes(
        session.session_id, owner_id, state, evaluator.ai_warzone)
    opposing_cards = _effective_combat_attributes(
        session.session_id, opponent_id, state, evaluator.player_warzone)
    blockers = [card for card in own_cards.values()
                if card.is_troop()
                and not (int(card.card_state or 0)
                         & game_engine.ECardStates.Tapped)
                and not card.has_attribute(ECardAttributes.CantBlock)]
    attackers = [opposing_cards[int(uid)] for uid in attacker_uids
                 if int(uid) in opposing_cards]
    attackers.sort(key=lambda card: -card.effective_attack(in_play=True))
    available = list(blockers)
    blocker_map = {}
    details = []
    for attacker in attackers:
        best = None
        for blocker in available:
            if not evaluator._can_block(blocker, attacker):
                continue
            damage, blocker_value, attacker_dies, attacker_value = \
                evaluator._damage_through(attacker, [blocker])
            prevented = max(0, attacker.effective_attack(in_play=True) - damage)
            blocker_dies = attacker.effective_attack(in_play=True) >= \
                blocker.effective_defense(in_play=True) and not \
                blocker.has_attribute(ECardAttributes.Immortal)
            score = (attacker_value if attacker_dies else 0.0) - \
                (blocker_value if blocker_dies else 0.0) + \
                prevented * evaluator.personality.values["DamageParityValue"] / 20.0
            if best is None or score > best[0]:
                best = (score, blocker, damage, attacker_dies, blocker_dies)
        if best is not None and best[0] > 0:
            score, blocker, damage, attacker_dies, blocker_dies = best
            blocker_map[int(attacker.card_uid)] = [int(blocker.card_uid)]
            available.remove(blocker)
            details.append({
                "attacker": int(attacker.card_uid),
                "blocker": int(blocker.card_uid),
                "score": round(float(score), 4),
                "attacker_dies": bool(attacker_dies),
                "blocker_dies": bool(blocker_dies),
                "damage_through": int(damage),
            })

    if profiler is not None:
        _profile_add(profiler, "combat_evaluations", 1)
        _profile_add(profiler, "combat_evaluation_seconds",
                     time.perf_counter() - started)
    if report is not None:
        report.setdefault("combat_decisions", []).append({
            "turn": int(state.get("turn", 0) or 0),
            "player_id": int(owner_id),
            "kind": "block",
            "attackers": [int(uid) for uid in attacker_uids],
            "blocks": details,
        })
    return blocker_map


def _profile_add(profiler, key, amount):
    lock = profiler.get("_lock")
    if lock is None:
        profiler[key] = profiler.get(key, 0) + amount
    else:
        with lock:
            profiler[key] = profiler.get(key, 0) + amount


def _record_rules_port_dispatch(profiler, kind, elapsed):
    lock = profiler.get("_lock")

    def record():
        profiler["rules_port_dispatches"] += 1
        profiler["rules_port_dispatch_seconds"] += float(elapsed)
        by_kind = profiler["rules_port_by_kind"]
        item = by_kind.setdefault(str(kind), {"count": 0, "seconds": 0.0})
        item["count"] += 1
        item["seconds"] += float(elapsed)

    if lock is None:
        record()
    else:
        with lock:
            record()


def _ai_main_decision(handler, session, state, owner_id, opponent_id,
                      profiler=None, report=None, pre_combat=True):
    """Apply the production evaluator's removal and board-building order."""
    started = time.perf_counter()
    evaluator = _ai_evaluator_for_seat(
        handler, session, state, owner_id, opponent_id, profiler, report)
    chosen = target_uid = None
    x_cost = 0
    reason = "pass_no_play"

    chosen = evaluator.burn_to_win()
    if chosen is not None:
        reason = "burn_to_win"
    if chosen is None:
        sweep = evaluator.best_sweeper()
        if sweep is not None:
            chosen, x_cost = sweep
            reason = "sweeper"
    if chosen is None:
        lockdown = evaluator.lockdown_removal()
        if lockdown is not None:
            chosen, target_uid = lockdown
            reason = "lockdown_removal"
    if chosen is None:
        for threat in evaluator.threatening_targets():
            removal, removal_x, removal_target = evaluator.find_removal_for(
                threat)
            if (removal is not None
                    and evaluator.is_playable(removal) == "True"):
                chosen, x_cost, target_uid = (
                    removal, removal_x, removal_target)
                reason = "threat_removal"
                break
    if chosen is None:
        chosen = evaluator.get_best_board_builder(
            pre_combat=pre_combat, include_resources=False)
        if chosen is not None:
            reason = "best_board_builder"
            target_uid = evaluator.choose_action_target(chosen)

    _record_card_decision(report, evaluator, chosen, reason)
    if profiler is not None:
        _profile_add(profiler, "ai_evaluations", 1)
        _profile_add(profiler, "ai_evaluation_seconds",
                     time.perf_counter() - started)
    return chosen, target_uid, x_cost, reason


def _ai_quick_decision(handler, session, state, owner_id, opponent_id,
                       profiler=None, report=None):
    """Choose a legal quick removal after the opponent makes a play."""
    started = time.perf_counter()
    evaluator = _ai_evaluator_for_seat(
        handler, session, state, owner_id, opponent_id, profiler, report)
    chosen = target_uid = None
    x_cost = 0
    reason = "no_preferred_quick_action"
    for threat in evaluator.threatening_targets():
        candidate, variable_cost, target = evaluator.find_removal_for(
            threat, quick=True)
        if (candidate is not None
                and evaluator.is_playable(candidate) == "True"):
            chosen, x_cost, target_uid = (
                candidate, variable_cost, target)
            reason = "quick_threat_removal"
            break

    _record_card_decision(
        report, evaluator, chosen, reason, only_quick_actions=True)
    if profiler is not None:
        _profile_add(profiler, "ai_evaluations", 1)
        _profile_add(profiler, "ai_evaluation_seconds",
                     time.perf_counter() - started)
    return chosen, target_uid, x_cost, reason


def _play_quick_response(handler, session, session_id, state,
                         owner_id, opponent_id, player_name, turn_number,
                         profiler=None, report=None):
    """Play one threat-removal quick action in the current response window."""
    quick, target_uid, x_cost, reason = _ai_quick_decision(
        handler, session, state, owner_id, opponent_id,
        profiler=profiler, report=report)
    if quick is None:
        return False
    payload = {"ability_data": [{
        "target_map": ({0: [int(target_uid)]}
                       if target_uid is not None else {}),
        "x_cost": int(x_cost or 0),
    }]}
    _transaction(
        handler, session,
        _card_play_bytes(quick.card_uid, quick.card_type),
        typed_payload=payload, profiler=profiler)
    location = _db.execute(
        "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(quick.card_uid))).fetchone()
    played = bool(location and location[0] != "hand")
    if report is not None:
        key = f"{quick.template_guid}:{quick.name}"
        entry = report["card_decisions"].get(
            str(int(owner_id)), {}).get(key)
        if played and entry is not None:
            entry["played"] += 1
        elif not played:
            _record_failed_card_play(report, owner_id, quick)
        report["actions"].append({
            "turn": int(turn_number),
            "player_id": int(owner_id),
            "player": player_name,
            "card": quick.name,
            "action": "quick_response",
            "reason": reason,
            "target_uid": target_uid,
            "played": played,
        })
    return played


def _play_one_game(seed, turns_cap=AUTOPLAY_TURN_CAP, deck_specs=None,
                   profiler=None, report=None):
    rnd = random.Random(seed)
    session_id = SCRATCH_SESSION_BASE + seed
    pids = list(AUTOPLAY_PLAYER_IDS)
    _cleanup(session_id, pids)

    session = gs.GameSession(session_id, session_id * AUTOPLAY_SERVER_ID_MULTIPLIER,
                             f"tourney-{session_id}", pids[0])
    for pid in pids:
        session.add_player(encoder.make_uid(PLAYER_UID_TYPE, pid), 0)

    # Seed FRA encounter decks when requested; otherwise retain the Set 1
    # smoke fixture used by the original runner.
    shards = db_set_resource_guids(SET1)
    others = db_set_constructed_guids(SET1)
    for i, pid in enumerate(pids):
        if deck_specs:
            spec = deck_specs[i]
            deck = list(spec["cards"])
            champ_guid = spec["champion_guid"]
            rnd.shuffle(deck)
        else:
            rnd.shuffle(others)
            deck = ((shards * AUTOPLAY_DECK_RESOURCE_COUNT)
                    [:AUTOPLAY_DECK_RESOURCE_COUNT]
                    + others[:AUTOPLAY_DECK_SPELL_COUNT])
            champ_guid = "1ae73dcf-e96e-4536-aec3-f53efb5e1c96"
            rnd.shuffle(deck)
        _seed_deck(session_id, pid, deck,
                   uid_offset=i * AUTOPLAY_PLAYER_UID_STRIDE)
        champ_uid = _seed_champion(
            session_id, pid, champ_guid,
            uid_offset=i * AUTOPLAY_PLAYER_UID_STRIDE +
            AUTOPLAY_CHAMPION_UID_OFFSET)
        if report is not None:
            report["champion_uids"][str(pid)] = champ_uid
    _db.commit()

    h1 = _make_handler(pids[0], session)
    h2 = _make_handler(pids[1], session)
    if profiler is not None:
        h1._autoplay_profiler = profiler
        h2._autoplay_profiler = profiler
    player_handlers[pids[0]] = h1
    player_handlers[pids[1]] = h2
    if report is not None:
        report["decks"] = {
            str(pid): {
                "name": (deck_specs[index]["name"] if deck_specs
                         else "Set 1 smoke deck"),
                "deck_guid": (deck_specs[index]["deck_guid"]
                              if deck_specs else None),
                "champion_guid": (deck_specs[index]["champion_guid"]
                                  if deck_specs else None),
            }
            for index, pid in enumerate(pids)
        }

    # Ready setup (both players) — persists the coin flip.
    handle_ready_for_game_setup(h1, session, {}, player_handlers)
    handle_ready_for_game_setup(h2, session, {}, player_handlers)
    state = pvp_load_state(session) or {}
    if not state:
        state = pvp_default_state(pids[0], pids[0])
        state["goes_first_pid"] = pids[0]
        pvp_save_state(session, state)
    handle_ready_for_game_events(h1, session, {}, log_req)
    handle_ready_for_game_events(h2, session, {}, log_req)

    # Play/Draw pick: winner chooses to play.
    _transaction(h1, session, b"ChoosePlayTransaction;")
    # The user's theory: the OTHER player must also send their pick for the
    # client to leave PickGoesFirst.  Test by having the loser send the
    # complementary pick (Draw) too.
    _transaction(h2, session, b"ChooseDrawTransaction;")

    # Sequential mulligan: ask 1 redraws (player A), ask 2 redraws (player B),
    # ask 3 keeps, ask 4 keeps — exercises the alternating redraw path the
    # real client uses.
    for round_i in range(AUTOPLAY_MULLIGAN_ROUNDS):
        cur = gs.find_session_by_player(
            game_engine.UID.make(PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
        st = pvp_load_state(cur)
        if not st or len(st.get("kept") or []) >= len(pids) \
                or st.get("phase") != game_engine.ETurnPhases.Mulligan:
            break
        ask = st.get("mulligan_pid")
        if ask in pids:
            if round_i in AUTOPLAY_REDRAW_ROUNDS:
                # Ask 1 (A) redraws, ask 2 (B) redraws — alternating redraw.
                _transaction(player_handlers[ask], session,
                             b"MulliganTransaction;")
            else:
                _transaction(player_handlers[ask], session,
                             b"AcceptStartingHand;")
        else:
            break

    state = pvp_load_state(session)
    if state is None:
        state = {}
    log_req(f"    PvP game {seed}: phase={state.get('phase')} "
            f"turn={state.get('turn_pid')}")

    turns = 0
    observed_turn_number = int(state.get("turn_number", 1) or 1)
    guard = 0
    try:
        while turns < turns_cap and guard < AUTOPLAY_GUARD_CAP:
            guard += 1
            cur_session = gs.find_session_by_player(
                game_engine.UID.make(PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
            state = pvp_load_state(cur_session)
            if state is None:
                log_req(f"    [pvp-autoplay] STATE LOST at guard {guard} — "
                        f"turn_order keys="
                        f"{list(cur_session.turn_order.keys()) if isinstance(cur_session.turn_order, dict) else type(cur_session.turn_order)}")
                break
            # The native scheduler can enter EndTurn and the next StartTurn
            # inside one priority pass, so sampling phase 22 misses completed
            # turns. The persisted turn number advances exactly once at that
            # boundary and remains observable after the scheduler settles.
            current_turn_number = int(
                state.get("turn_number", observed_turn_number) or
                observed_turn_number)
            if current_turn_number > observed_turn_number:
                turns += current_turn_number - observed_turn_number
                observed_turn_number = current_turn_number
                if turns >= turns_cap:
                    break
            phase = state.get("phase")
            turn_pid = state.get("turn_pid")
            opp_pid = pids[1] if turn_pid == pids[0] else pids[0]
            if any(int(state.get(f"hp_{pid}", 20) or 0) <= 0
                   for pid in pids):
                break
            # A triggered ability is awaiting a target: answer it (choose the
            # first legal candidate) before doing anything else.
            pend = state.get("pending_trigger")
            if pend:
                chooser_pid = int(pend.get("owner_id", turn_pid))
                candidates = []
                if pend.get("source_uid"):
                    src = int(pend["source_uid"])
                    candidates = [r[0] for r in db_static_card_rows(
                        session_id, ("warzone", "CastSpells"))
                                  if r[0] != src]
                if not candidates:
                    candidates = [r[0] for r in db_static_card_rows(
                        session_id, ("warzone",))[:1]]
                if candidates:
                    _transaction(player_handlers[chooser_pid], session,
                                 b"SetAbilityActivationDataTransaction;" +
                                 _mk_uid_bytes(candidates[0]),
                                 typed_payload={
                                     "AbilityInstanceId": int(
                                         pend.get("instance_id", 1) or 1),
                                     "AbilityActivationData": {
                                         "TargetMap": {
                                             str(int(pend.get(
                                                 "target_index", 0) or 0)):
                                             [int(candidates[0])]
                                         }
                                     }
                                 })
                    continue
            if phase in OPENING_SETUP_PHASES:
                # Setup phases are driven above; a stray pass should not loop.
                break
            # Main phases: the turn player plays a resource then an affordable
            # troop; the opponent passes immediately.
            if phase in (game_engine.ETurnPhases.FirstMainPhase,
                         game_engine.ETurnPhases.SecondMainPhase):
                if deck_specs:
                    _transaction(player_handlers[opp_pid], session,
                                 b"PassPriorityTransaction;")
                    h = player_handlers[turn_pid]
                    res_rows = db_hand_resources_with_template(
                        session_id, turn_pid)
                    if res_rows and not state.get(
                            f"res_played_{turn_pid}", 0):
                        resource_row = res_rows[0]
                        _transaction(h, session,
                                     _card_play_bytes(
                                         resource_row[1], "Resource"),
                                     profiler=profiler)
                        _transaction(player_handlers[opp_pid], session,
                                     b"PassPriorityTransaction;",
                                     profiler=profiler)
                        resource_location = _db.execute(
                            "SELECT location FROM game_cards WHERE session_id=? "
                            "AND card_uid=?", (session_id,
                                                 int(resource_row[1]))).fetchone()
                        resource_played = bool(
                            resource_location
                            and resource_location[0] != "hand")
                        _record_resource_decision(
                            report, turn_pid, resource_row,
                            resource_played)
                        if report is not None:
                            report["actions"].append({
                                "turn": turns + 1,
                                "player_id": int(turn_pid),
                                "player": deck_specs[pids.index(turn_pid)]["name"],
                                "card": _db.execute(
                                    "SELECT name FROM card_templates WHERE guid=?",
                                    (str(resource_row[2]),)).fetchone()[0],
                                "reason": "resource_play",
                                "played": resource_played,
                            })

                    latest = gs.find_session_by_player(
                        game_engine.UID.make(
                            PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                    state = pvp_load_state(latest) or state

                    chosen, target_uid, x_cost, reason = _ai_main_decision(
                        h, session, state, turn_pid, opp_pid,
                        profiler=profiler, report=report,
                        pre_combat=(phase ==
                                    game_engine.ETurnPhases.FirstMainPhase))
                    if chosen is not None:
                        typed_payload = {"ability_data": [{
                            "target_map": ({0: [int(target_uid)]}
                                           if target_uid is not None else {}),
                            "x_cost": int(x_cost or 0),
                        }]}
                        _transaction(
                            h, session,
                            _card_play_bytes(chosen.card_uid, chosen.card_type),
                            typed_payload=typed_payload,
                            profiler=profiler)
                        location = _db.execute(
                            "SELECT location FROM game_cards WHERE session_id=? "
                            "AND card_uid=?", (session_id,
                                                 int(chosen.card_uid))).fetchone()
                        played = bool(location and location[0] != "hand")
                        if report is not None:
                            key = f"{chosen.template_guid}:{chosen.name}"
                            entry = report["card_decisions"].get(
                                str(int(turn_pid)), {}).get(key)
                            if entry is not None and played:
                                entry["played"] += 1
                            elif not played:
                                _record_failed_card_play(
                                    report, turn_pid, chosen)
                            report["actions"].append({
                                "turn": turns + 1,
                                "player_id": int(turn_pid),
                                "player": deck_specs[pids.index(turn_pid)]["name"],
                                "card": chosen.name,
                                "reason": reason,
                                "target_uid": target_uid,
                                "played": played,
                            })
                    if chosen is not None:
                        latest = gs.find_session_by_player(
                            game_engine.UID.make(
                                PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                        response_state = pvp_load_state(latest) or state
                        deck_name = deck_specs[pids.index(opp_pid)]["name"]
                        quick_played = _play_quick_response(
                            player_handlers[opp_pid], session, session_id,
                            response_state, opp_pid, turn_pid, deck_name,
                            turns + 1, profiler=profiler, report=report)
                    else:
                        quick_played = False
                    if not quick_played:
                        _transaction(player_handlers[opp_pid], session,
                                     b"PassPriorityTransaction;",
                                     profiler=profiler)
                    _transaction(player_handlers[turn_pid], session,
                                 b"PassPriorityTransaction;",
                                 profiler=profiler)
                    continue

                _transaction(player_handlers[opp_pid], session,
                             b"PassPriorityTransaction;")
                h = player_handlers[turn_pid]
                res_rows = db_hand_resources_with_template(session_id, turn_pid)
                if res_rows and not db_zone_card_count(
                        session_id, turn_pid, "PlayedResources"):
                    _transaction(h, session,
                                 _card_play_bytes(res_rows[0][1], "Resource"))
                    _transaction(player_handlers[opp_pid], session,
                                 b"PassPriorityTransaction;")
                troop = next((row for row in db_ai_hand_playables(
                    session_id, turn_pid, "permanent")
                    if "Troop" in str(row[4])), None)
                if troop:
                    _transaction(h, session,
                                 _card_play_bytes(troop[1], troop[4]))
                    _transaction(player_handlers[opp_pid], session,
                                 b"PassPriorityTransaction;")
                _transaction(h, session, b"PassPriorityTransaction;")
                continue
            # DeclareAttack: the turn player swings with all eligible troops.
            if phase == game_engine.ETurnPhases.DeclareAttack:
                attackers, attack_reason = _ai_attack_decision(
                    player_handlers[turn_pid], session, state, turn_pid,
                    opp_pid, profiler=profiler, report=report)
                champ_map = state.get("champ_map") or {}
                opp_champ = int(champ_map.get(str(opp_pid), 0))
                if attackers:
                    _transaction(player_handlers[turn_pid], session,
                                 _attack_bytes(attackers, opp_champ),
                                 typed_payload={"m_Attacks": [{
                                     "DefendingCardId": opp_champ,
                                     "AttackingCardIds": attackers,
                                 }]},
                                 profiler=profiler)
                if report is not None:
                    report["actions"].append({
                        "turn": turns + 1,
                        "player_id": int(turn_pid),
                        "player": deck_specs[pids.index(turn_pid)]["name"],
                        "action": "attack",
                        "reason": attack_reason,
                        "attacker_uids": attackers,
                    })
                quick_played = False
                if attackers:
                    latest = gs.find_session_by_player(
                        game_engine.UID.make(
                            PLAYER_UID_TYPE, pids[0]).to_uint64()) or session
                    response_state = pvp_load_state(latest) or state
                    deck_name = deck_specs[pids.index(opp_pid)]["name"]
                    quick_played = _play_quick_response(
                        player_handlers[opp_pid], session, session_id,
                        response_state, opp_pid, turn_pid, deck_name,
                        turns + 1, profiler=profiler, report=report)
                if not quick_played:
                    _transaction(player_handlers[opp_pid], session,
                                 b"PassPriorityTransaction;",
                                 profiler=profiler)
                _transaction(player_handlers[turn_pid], session,
                             b"PassPriorityTransaction;",
                             profiler=profiler)
                continue
            # DeclareDefense: the defender blocks with every eligible troop.
            if phase == game_engine.ETurnPhases.DeclareDefense:
                att_state = state
                attacker_uids = [int(k) for k in
                                 (att_state.get("attackers") or {})]
                blockers = _ai_block_decision(
                    player_handlers[opp_pid], session, state, opp_pid,
                    turn_pid, attacker_uids, profiler=profiler,
                    report=report)
                champ_map = state.get("champ_map") or {}
                opp_champ = int(champ_map.get(str(opp_pid), 0))
                if attacker_uids:
                    _transaction(player_handlers[opp_pid], session,
                                 _defense_bytes(attacker_uids, blockers,
                                                opp_champ),
                                 typed_payload={"m_DefenseDeclarations": [
                                     {"AttackerId": int(attacker),
                                      "DefendingCardIds": [
                                          int(uid) for uid in blocker_list]}
                                     for attacker, blocker_list in
                                     blockers.items()
                                 ]},
                                 profiler=profiler)
                _transaction(player_handlers[opp_pid], session,
                             b"PassPriorityTransaction;",
                             profiler=profiler)
                _transaction(player_handlers[turn_pid], session,
                             b"PassPriorityTransaction;",
                             profiler=profiler)
                continue
            # Both players pass the phase (the non-turn player passes first so
            # the turn player's pass completes the pair).
            _transaction(player_handlers[opp_pid], session,
                         b"PassPriorityTransaction;")
            _transaction(player_handlers[turn_pid], session,
                         b"PassPriorityTransaction;")
    except Exception:
        return turns, traceback.format_exc()
    finally:
        if report is not None:
            last_session = gs.find_session_by_player(
                game_engine.UID.make(PLAYER_UID_TYPE, pids[0]).to_uint64())
            last_state = pvp_load_state(last_session or session) or {}
            report["turns"] = turns
            report["last_phase"] = str(last_state.get("phase"))
            report["health"] = {
                str(pid): int(last_state.get(f"hp_{pid}", 20) or 0)
                for pid in pids
            }
        _cleanup(session_id, pids)
    return turns, None


def main(games=DEFAULT_AUTOPLAY_GAMES):
    ok = 0
    for seed in range(FIRST_AUTOPLAY_SEED, games + FIRST_AUTOPLAY_SEED):
        turns, err = _play_one_game(seed)
        if err:
            print(f"game {seed}: CRASH after {turns} turns")
            print(err[:3000])
        else:
            ok += 1
            print(f"game {seed}: {turns} turns, no crash")
    print(f"PvP autoplay: {ok}/{games} games completed")


def _json_report(report):
    copied = dict(report)
    copied["card_decisions"] = {
        owner: {
            key: {**entry, "reasons": dict(entry["reasons"])}
            for key, entry in decisions.items()
        }
        for owner, decisions in report.get("card_decisions", {}).items()
    }
    copied["profile"] = {
        key: value for key, value in report.get("profile", {}).items()
        if not key.startswith("_")
    }
    return copied


def main_fra(deck_guids=None, turns_cap=30, show_profile=False,
             report_path=None, runs=1, seed=900):
    specs = _fra_encounter_specs(deck_guids)
    profiler = {
        "_lock": threading.Lock(),
        "rules_transactions": 0,
        "rules_transaction_seconds": 0.0,
        "rules_port_dispatches": 0,
        "rules_port_dispatch_seconds": 0.0,
        "rules_port_by_kind": {},
        "ai_evaluations": 0,
        "ai_evaluation_seconds": 0.0,
        "combat_evaluations": 0,
        "combat_evaluation_seconds": 0.0,
    }
    turn_limit = max(0, int(turns_cap))
    run_count = max(1, int(runs))
    first_seed = int(seed)
    report = {"decks": {}, "runs": [], "actions": [],
              "combat_decisions": [], "card_decisions": {}}
    any_error = False
    for run_index in range(run_count):
        run_seed = first_seed + run_index
        run_report = {"champion_uids": {}, "actions": [],
                      "combat_decisions": [], "card_decisions": {}}
        turns, err = _play_one_game(
            run_seed, turns_cap=turn_limit, deck_specs=specs,
            profiler=profiler, report=run_report)
        if not report["decks"]:
            report["decks"] = dict(run_report.get("decks") or {})
        run_summary = {
            "seed": run_seed,
            "turns": turns,
            "health": dict(run_report.get("health") or {}),
            "last_phase": run_report.get("last_phase"),
        }
        if err:
            any_error = True
            run_summary["error"] = err
            print(f"FRA run seed {run_seed}: stopped after {turns} turns")
            print(err)
        else:
            print(f"FRA run seed {run_seed}: {turns}/{turn_limit} turns; "
                  f"health={run_summary['health']}")
        report["runs"].append(run_summary)
        for action in run_report.get("actions", ()):
            report["actions"].append({
                "run": run_index + 1, "seed": run_seed, **action})
        for decision in run_report.get("combat_decisions", ()):
            report["combat_decisions"].append({
                "run": run_index + 1, "seed": run_seed, **decision})
        for owner_id, decisions in run_report.get(
                "card_decisions", {}).items():
            destination = report["card_decisions"].setdefault(owner_id, {})
            for key, entry in decisions.items():
                total = destination.setdefault(key, {
                    "template_guid": entry["template_guid"],
                    "name": entry["name"],
                    "seen": 0,
                    "selected": 0,
                    "played": 0,
                    "reasons": Counter(),
                })
                total["seen"] += entry["seen"]
                total["selected"] += entry["selected"]
                total["played"] += entry["played"]
                total["reasons"].update(entry["reasons"])

    print(f"FRA duel: {specs[0]['name']} vs {specs[1]['name']} — "
          f"{run_count} run(s), max {turn_limit} turns each")

    print("AI card decisions by seat (played / selected / seen):")
    for owner_id, deck in report["decks"].items():
        print(f"  {deck['name']} (seat {owner_id})")
        entries = sorted(report["card_decisions"].get(owner_id, {}).values(),
                         key=lambda item: (item["played"], -item["seen"],
                                           item["name"].lower()))
        for entry in entries:
            if entry["played"] >= entry["seen"]:
                continue
            reasons = ", ".join(
                f"{reason}={count}" for reason, count in
                sorted(entry["reasons"].items()))
            print(f"    {entry['name']}: {entry['played']}/"
                  f"{entry['selected']} / {entry['seen']} — {reasons}")

    if show_profile:
        evaluations = int(profiler["ai_evaluations"])
        combat_evaluations = int(profiler["combat_evaluations"])
        port_dispatches = int(profiler["rules_port_dispatches"])
        print("Profile:")
        if evaluations:
            average = profiler["ai_evaluation_seconds"] * 1000 / evaluations
            print(f"  AI evaluator: "
                  f"{profiler['ai_evaluation_seconds'] * 1000:.2f} ms "
                  f"across {evaluations} decisions ({average:.2f} ms/decision)")
        else:
            print("  AI evaluator: 0 decisions")
        if combat_evaluations:
            average = profiler["combat_evaluation_seconds"] * 1000 / \
                combat_evaluations
            print(f"  AI combat evaluator: "
                  f"{profiler['combat_evaluation_seconds'] * 1000:.2f} ms "
                  f"across {combat_evaluations} decisions "
                  f"({average:.2f} ms/decision)")
        else:
            print("  AI combat evaluator: 0 decisions")
        if port_dispatches:
            print(f"  RulesPort dispatch: "
                  f"{profiler['rules_port_dispatch_seconds'] * 1000:.2f} ms "
                  f"across {port_dispatches} queued transaction dispatches "
                  f"(includes PvP projection callbacks)")
        else:
            print("  RulesPort dispatch: no queued transactions")
        for kind, item in sorted(profiler["rules_port_by_kind"].items()):
            print(f"    {kind}: {item['seconds'] * 1000:.2f} ms "
                  f"across {item['count']}")
        print(f"  End-to-end transaction path: "
              f"{profiler['rules_transaction_seconds'] * 1000:.2f} ms "
              f"across {profiler['rules_transactions']} transactions")
    report["profile"] = {
        key: value for key, value in profiler.items()
        if not key.startswith("_")
    }
    report["max_turns"] = turn_limit
    report["seed"] = first_seed
    if report_path:
        with open(report_path, "w", encoding="utf-8") as output:
            json.dump(_json_report(report), output, indent=2, sort_keys=True)
            output.write("\n")
        print(f"Wrote report: {report_path}")
    return 1 if any_error else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("games", nargs="?", type=int,
                        default=DEFAULT_AUTOPLAY_GAMES)
    parser.add_argument("--fra", nargs="*", metavar="DECK_GUID",
                        help="run a duel between two FRA encounter decks")
    parser.add_argument("--turn-limit", type=int, default=30,
                        help="maximum completed turns in FRA mode (default: 30)")
    parser.add_argument("--runs", type=int, default=1,
                        help="number of seeded FRA duels to aggregate")
    parser.add_argument("--seed", type=int, default=900,
                        help="first shuffle seed for FRA mode (default: 900)")
    parser.add_argument("--profile", action="store_true",
                        help="print AI, RulesPort, and transaction timings")
    parser.add_argument("--json-report", metavar="PATH",
                        help="write card decisions and actions as JSON")
    args = parser.parse_args()
    if args.fra is not None:
        if len(args.fra) not in (0, 2):
            parser.error("--fra accepts either no GUIDs or exactly two")
        if args.runs < 1:
            parser.error("--runs must be at least 1")
        if _SIMULATION_DB_COPY:
            print("Simulation database: disposable snapshot")
        raise SystemExit(main_fra(
            args.fra or None, turns_cap=args.turn_limit,
            show_profile=args.profile, report_path=args.json_report,
            runs=args.runs, seed=args.seed))
    main(args.games)
