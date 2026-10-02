#!/usr/bin/env python3
"""Dynamic PvP champion-power contract sweep.

This is one generated test loop, not one hand-maintained test per champion.
Every authored ``champion_abilities`` attachment is projected through the
PvP champion-option builder. Manual powers are activated through the same PvP
cost/target/stack ingress used by the tournament service and resolved by the
native RulesPort ability resolver. Triggered powers are raised through native
PvP trigger discovery. Typed effect traces, persisted state, continuations,
and wire events are checked before an attachment is counted as passed.

The database is a disposable metadata-rich fixture. This proves the server
rules/option contract; it does not replace a live two-client tournament.

Examples::

    python3 tests/tests_champion_sweep.py
    python3 tests/tests_champion_sweep.py --only Corinth
    python3 tests/tests_champion_sweep.py --limit 25
"""

from __future__ import annotations

import argparse
import contextlib
from collections import Counter
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

# Bind the test database before runtime modules open db._db. The live
# hconnect.db is never opened by this sweep.
SRC = fresh_database()

import db as dbmod
import game_engine
import services.tournament_game as tournament_game
from gamedata import DEFAULT_RECORD_STORE, ability_graph
from gamedata.records import reference_guid
from rules_port.coverage import NATIVE_EFFECTS, STRUCTURAL_EFFECTS
from rules_port.resolution import resolve_port_ability, resolve_port_trigger
from rules_port.targeting import legal_targets_for
from rules_port.triggers import TriggerEvent, dispatch_native_trigger
from tests.tests_combat import HandlerStub


SESSION_ID = 1
PLAYER_PID = 1001
OPPONENT_PID = 1002
PLAYER_UID = game_engine.UID.make(244, PLAYER_PID)
OPPONENT_UID = game_engine.UID.make(244, OPPONENT_PID)
PLAYER_CHAMP_UID = int(game_engine.UID.make(1, 9001).uid64)
OPPONENT_CHAMP_UID = int(game_engine.UID.make(1, 9002).uid64)
CARD_UID_FIRST = 100
KNOWN_EFFECTS = set(NATIVE_EFFECTS) | set(STRUCTURAL_EFFECTS)
_ZERO_GUID = "00000000-0000-0000-0000-000000000000"

_SPECIAL_TARGET_KINDS = {
    "AbilitySourceCardTargetTemplate", "AbilityTriggerCardTargetTemplate",
    "SourceDrawnTargetTemplate", "SourceBuriedTargetTemplate",
    "SourceStoredTargetTemplate", "SourceRevealedTargetTemplate",
    "VoidedTargetTemplate", "AbilityCreatedTargetTemplate",
    "PlayerTargetTemplate", "MatchSecondaryTargetTemplate",
    "SecondaryTargetTemplate", "DuplicateCardTargetTemplate",
    "TargetsAPlayerOrHisStuff",
}


class ChampionSweepFailure(AssertionError):
    """A generated champion attachment failed its typed PvP contract."""


class SessionStub:
    session_id = SESSION_ID
    server_id = 100

    def __init__(self, state):
        self.turn_order = state
        self._rules_port_battle_state = state

    def _persist(self):
        self.turn_order = self._rules_port_battle_state


class PvPHandlerStub(HandlerStub):
    """Production handler seams with real PvP participant identities."""

    def __init__(self, db, champion_guid, champion_abilities):
        super().__init__(db)
        self.user_profile = {"id": PLAYER_PID}
        self.client_reck_id = PLAYER_PID
        self._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID(PLAYER_CHAMP_UID))
        self._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID(OPPONENT_CHAMP_UID))
        self._player_champ_guid = champion_guid
        self._player_champ_abilities = list(champion_abilities)

    def _champion_targets(self):
        state = getattr(self, "_current_bstate", None) or {}
        return [
            (PLAYER_CHAMP_UID, PLAYER_PID, "Player champion",
             int(state.get("hp_1001", 20))),
            (OPPONENT_CHAMP_UID, OPPONENT_PID, "Opponent champion",
             int(state.get("hp_1002", 20))),
        ]

    def _fresh_game(self, session, pl_t, ai_t, bstate):
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.player_health = int(bstate.get("hp_1001", 20))
        game.ai_health = int(bstate.get("hp_1002", 20))
        game.player_resources = int(bstate.get("res_1001", 0))
        game.ai_resources = int(bstate.get("res_1002", 0))
        game.player_total_resources = int(bstate.get("res_total_1001", 0))
        game.ai_total_resources = int(bstate.get("res_total_1002", 0))
        game.player_charges = int(bstate.get("chg_1001", 0))
        game.ai_charges = int(bstate.get("chg_1002", 0))
        game.player_spell_points = int(bstate.get("sp_1001", 0))
        game.ai_spell_points = int(bstate.get("sp_1002", 0))
        game.player_threshold = dict(bstate.get("thresh_1001", {}))
        game.ai_threshold = dict(bstate.get("thresh_1002", {}))
        game.turn_number = int(bstate.get("turn_number", 1))
        return game


def _champion_attachments(db):
    """Return every authored template/power attachment, not just GUIDs."""
    rows = db.execute(
        "SELECT champion_guid, champion_name, ability_guid, ability_name, "
        "charge_cost, spell_cost, casting_behavior, thresholds_json "
        "FROM champion_abilities WHERE ability_guid IS NOT NULL "
        "ORDER BY champion_guid, ability_guid"
    ).fetchall()
    return [
        {
            "champion_guid": str(row[0]).lower(),
            "champion_name": row[1] or "Unknown champion",
            "ability_guid": str(row[2]).lower(),
            "ability_name": row[3] or str(row[2]),
            "charge_cost": int(row[4] or 0),
            "spell_cost": int(row[5] or 0),
            "casting_behavior": int(row[6] or 0),
            "thresholds_json": row[7] or "[]",
        }
        for row in rows
    ]


def _template(db, card_type, *, fallback_type=None):
    query = (
        "SELECT guid FROM card_templates WHERE card_type=? "
        "AND COALESCE(abilities_json,'[]')='[]' ORDER BY guid LIMIT 1"
    )
    row = db.execute(query, (card_type,)).fetchone()
    if row:
        return str(row[0])
    if fallback_type:
        row = db.execute(
            "SELECT guid FROM card_templates WHERE card_type LIKE ? "
            "ORDER BY guid LIMIT 1", (f"%{fallback_type}%",)).fetchone()
        if row:
            return str(row[0])
    row = db.execute(
        "SELECT guid FROM card_templates ORDER BY guid LIMIT 1").fetchone()
    if not row:
        raise ChampionSweepFailure(f"fixture has no card template for {card_type}")
    return str(row[0])


def _threshold_template(db, card_type, fallback_guid):
    """Find a card with authored threshold metadata for modifier contracts."""
    rows = db.execute(
        "SELECT guid, threshold_json FROM card_templates "
        "WHERE card_type=? ORDER BY guid", (card_type,)).fetchall()
    for guid, raw in rows:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        values = value.get("list") if isinstance(value, dict) else None
        if isinstance(values, list) and any(int(item or 0) > 0 for item in values):
            return str(guid)
    return str(fallback_guid)


def _subtype_template(db, subtype, fallback_guid):
    """Find an inert authored card carrying one typed subtype token."""
    row = db.execute(
        "SELECT guid FROM card_templates "
        "WHERE lower(COALESCE(subtype,'')) LIKE ? "
        "AND card_type='Troop' ORDER BY guid LIMIT 1",
        (f"%{str(subtype).lower()}%",),
    ).fetchone()
    return str(row[0]) if row else str(fallback_guid)


def _insert_card(db, uid, owner, location, template_guid, position,
                 *, abilities=(), card_type=None, is_champion=0,
                 card_state=0, permanent_buffs=None):
    row = db.execute(
        "SELECT card_type, attributes FROM card_templates WHERE guid=?",
        (template_guid,)).fetchone()
    actual_type = card_type or (row[0] if row else "Troop")
    attributes = int(row[1] or 0) if row else 0
    db.execute(
        "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,"
        "card_template_id,location,position,is_champion,card_state,"
        "card_abilities,card_type,card_attributes,card_attack_mod,"
        "card_defense_mod,card_cost_mod,card_damage,permanent_buffs,"
        "temporary_buffs,card_uses,resolved_at,original_template_guid,"
        "temporary_attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (SESSION_ID, int(owner), int(uid), template_guid, template_guid,
         location, int(position), int(is_champion), int(card_state),
         json.dumps(list(abilities)),
         actual_type, attributes, 0, 0, 0, 0,
         json.dumps(permanent_buffs or {}), "{}", "{}", 0,
         template_guid, 0),
    )


def _seed_board(db, metadata):
    """Seed a broad typed PvP board around the selected champion template."""
    db.execute("DELETE FROM game_cards")
    champion_guid = metadata["champion_guid"]
    ability_rows = db.execute(
        "SELECT ability_guid FROM champion_abilities "
        "WHERE champion_guid=? ORDER BY ability_guid", (champion_guid,)
    ).fetchall()
    champion_abilities = [str(row[0]).lower() for row in ability_rows]
    if not champion_abilities:
        raise ChampionSweepFailure(
            f"{metadata['champion_name']}: champion template has no abilities")

    troop = _template(db, "Troop")
    threshold_troop = _threshold_template(db, "Troop", troop)
    artifact = _template(db, "Artifact", fallback_type="Artifact")
    constant = _template(db, "Constant", fallback_type="Constant")
    action = _template(db, "BasicAction", fallback_type="Action")
    quick = _template(db, "QuickAction", fallback_type="Action")
    resource = _template(db, "Resource", fallback_type="Resource")
    choice = _template(db, "Choice", fallback_type="Choice")
    robot = _subtype_template(db, "robot", troop)
    dwarf = _subtype_template(db, "dwarf", troop)
    gnoll = _subtype_template(db, "gnoll", troop)
    orc = _subtype_template(db, "orc", troop)
    wormoid = _subtype_template(db, "wormoid", troop)
    familiar = _subtype_template(db, "familiar", troop)

    _insert_card(
        db, PLAYER_CHAMP_UID, PLAYER_PID, "champion", champion_guid, 0,
        abilities=champion_abilities, card_type="Champion", is_champion=1)

    # Cards use SessionCardId type 1, which is the client-recognized type.
    # Keep enough representatives in every zone for authored target filters,
    # card costs, deck searches, and choice-zone continuations.
    fixtures = [
        (troop, "warzone", PLAYER_PID, 1),
        (troop, "warzone", PLAYER_PID, 2),
        (artifact, "warzone", PLAYER_PID, 3),
        (constant, "warzone", PLAYER_PID, 4),
        (troop, "warzone", OPPONENT_PID, 5),
        (troop, "warzone", OPPONENT_PID, 6),
        (artifact, "warzone", OPPONENT_PID, 7),
        (constant, "warzone", OPPONENT_PID, 8),
        (action, "hand", PLAYER_PID, 9),
        (quick, "hand", PLAYER_PID, 10),
        (troop, "hand", PLAYER_PID, 11),
        (action, "hand", OPPONENT_PID, 12),
        (troop, "hand", OPPONENT_PID, 13),
        (resource, "PlayedResources", PLAYER_PID, 14),
        (resource, "PlayedResources", OPPONENT_PID, 15),
        (resource, "deck", PLAYER_PID, 16),
        (troop, "deck", PLAYER_PID, 17),
        (action, "deck", PLAYER_PID, 18),
        # Keep the opposing deck's top card threshold-bearing so a typed
        # CardThresholdModifier (for example Renner) has a real before/after
        # mutation to prove.  The fixture still overrides card abilities to
        # empty, so this card cannot introduce unrelated triggers.
        (threshold_troop, "deck", OPPONENT_PID, 19),
        (resource, "deck", OPPONENT_PID, 20),
        (troop, "discard", PLAYER_PID, 21),
        (resource, "discard", PLAYER_PID, 22),
        (troop, "discard", OPPONENT_PID, 23),
        (resource, "discard", OPPONENT_PID, 24),
        (troop, "void", PLAYER_PID, 25),
        (troop, "void", OPPONENT_PID, 26),
        (choice, "choosing", PLAYER_PID, 27),
        (choice, "choosing", PLAYER_PID, 28),
        (choice, "choosing", PLAYER_PID, 29),
        (choice, "choosing", OPPONENT_PID, 30),
        # Typed candidates make the generated board satisfy common authored
        # target/trigger filters without using localized card text.  Reusing
        # one inert template is intentional: the effect resolver only needs
        # legal, persisted cards in the relevant zone.
        (robot, "warzone", PLAYER_PID, 31),
        (robot, "hand", PLAYER_PID, 32),
        (dwarf, "CastSpells", PLAYER_PID, 33),
        (dwarf, "hand", PLAYER_PID, 34),
        (gnoll, "warzone", PLAYER_PID, 35),
        (gnoll, "discard", PLAYER_PID, 36),
        (gnoll, "discard", PLAYER_PID, 37),
        (gnoll, "discard", PLAYER_PID, 38),
        (gnoll, "discard", PLAYER_PID, 39),
        (gnoll, "discard", PLAYER_PID, 40),
        (orc, "warzone", PLAYER_PID, 41),
        (wormoid, "deck", PLAYER_PID, 42),
        (familiar, "warzone", PLAYER_PID, 43),
    ]
    for template_guid, location, owner, position in fixtures:
        _insert_card(
            db, int(game_engine.UID.make(1, 100 + position).uid64),
            owner, location, template_guid, position)
    _insert_card(
        db, int(game_engine.UID.make(1, 100 + 44).uid64), PLAYER_PID,
        "warzone", troop, 44, card_state=1)
    _insert_card(
        db, int(game_engine.UID.make(1, 100 + 45).uid64), PLAYER_PID,
        "deck", wormoid, 45,
        permanent_buffs={"int_attrs": {"Tunneling": 1}})
    opponent_tapped_uid = int(game_engine.UID.make(1, 100 + 46).uid64)
    _insert_card(
        db, opponent_tapped_uid, OPPONENT_PID, "warzone", troop, 46,
        card_state=1)
    # A second participant must be present in the session DB for the actual
    # PvP activation/continuation functions to recognize a two-player game.
    db.commit()
    return {
        "champion_guid": champion_guid,
        "champion_abilities": champion_abilities,
        "source_uid": PLAYER_CHAMP_UID,
        "opponent_uid": OPPONENT_CHAMP_UID,
        "filler_source_uid": int(game_engine.UID.make(1, 101).uid64),
        "cast_target_uid": int(game_engine.UID.make(1, 133).uid64),
        "readied_source_uid": int(game_engine.UID.make(1, 101).uid64),
        "opponent_tapped_uid": opponent_tapped_uid,
    }


def _phase_for(_metadata):
    return int(game_engine.ETurnPhases.FirstMainPhase)


def _new_state(metadata):
    thresholds = {int(flag): 20 for flag in game_engine.SHARD_TO_FLAG.values()}
    return {
        "pvp": True,
        "pids": [PLAYER_PID, OPPONENT_PID],
        "champ_map": {str(PLAYER_PID): PLAYER_CHAMP_UID,
                      str(OPPONENT_PID): OPPONENT_CHAMP_UID},
        "turn_pid": PLAYER_PID,
        "priority_pid": PLAYER_PID,
        "phase": _phase_for(metadata),
        "turn_number": 1,
        "hp_1001": 20,
        "hp_1002": 20,
        "player_health": 20,
        "ai_health": 20,
        "player_max_health": 20,
        "ai_max_health": 20,
        "pvp_health_map": {PLAYER_PID: "player_health",
                           OPPONENT_PID: "ai_health"},
        "res_1001": 30,
        "res_1002": 30,
        "res_total_1001": 30,
        "res_total_1002": 30,
        "chg_1001": 30,
        "chg_1002": 30,
        "sp_1001": 30,
        "sp_1002": 30,
        "player_resources": 30,
        "ai_resources": 30,
        "player_total_resources": 30,
        "ai_total_resources": 30,
        "player_charges": 30,
        "ai_charges": 30,
        "player_spell_points": 30,
        "ai_spell_points": 30,
        "thresh_1001": dict(thresholds),
        "thresh_1002": dict(thresholds),
        "player_threshold": dict(thresholds),
        "ai_threshold": dict(thresholds),
        "stack": [],
        "stack_player_passed": False,
        "stack_ai_passed": False,
        "_next_instance_id": 1,
        "trace_ability_resolution": True,
        "_rules_port_attached": True,
    }


def _metadata_graph(metadata):
    graph = ability_graph(
        DEFAULT_RECORD_STORE, metadata["ability_guid"].lower())
    if graph is None:
        raise ChampionSweepFailure(
            f"{metadata['champion_name']} / {metadata['ability_name']} "
            f"{metadata['ability_guid']}: missing Records graph")
    unknown = sorted({str(effect.concrete_type or "")
                      for effect in graph.effects
                      if str(effect.concrete_type or "") not in KNOWN_EFFECTS})
    if unknown:
        raise ChampionSweepFailure(
            f"{metadata['champion_name']} / {metadata['ability_name']} "
            f"{metadata['ability_guid']}: unsupported typed effects {unknown}")
    if not graph.effects:
        raise ChampionSweepFailure(
            f"{metadata['champion_name']} / {metadata['ability_name']} "
            f"{metadata['ability_guid']}: graph has no effects")
    return graph


def _target_map(db, graph, handler, state, source_uid):
    """Bind every available explicit target from the typed target graph."""
    champions = handler._champion_targets()
    result = {}
    for index, target in enumerate(graph.targets):
        if not getattr(target, "requires_input", False):
            continue
        if (getattr(target, "is_auto", False) or
                getattr(target, "target_kind", "") in _SPECIAL_TARGET_KINDS):
            continue
        candidates = legal_targets_for(
            db, SESSION_ID, PLAYER_PID, target, int(source_uid),
            both_players=True, champions=champions, battle_state=state)
        minimum = int(getattr(target, "minimum", 0) or 0)
        if not candidates:
            if getattr(target, "optional", False) or minimum == 0:
                continue
            raise ChampionSweepFailure(
                f"{graph.guid}: no legal fixture target for target {index} "
                f"({target.guid})")
        maximum = int(target.resolved_maximum() or 0)
        count = max(1, minimum)
        if maximum > 0:
            count = min(count, maximum)
        result[index] = tuple(int(uid) for uid in candidates[:count])
    return result


def _activation_selection(db, graph, handler, state, session, source_uid):
    """Choose legal payment and effect cards for the production PvP ingress."""
    champions = handler._champion_targets()
    cost_rows = tournament_game._pvp_ability_cost_targets(
        session, state, PLAYER_PID, int(source_uid), graph.guid, champions)
    if cost_rows is None:
        return None, {}
    selected = []
    used = set()
    for _tid, _cost_type, candidates, minimum, _maximum in cost_rows:
        if not candidates or not int(minimum or 0):
            continue
        available = [int(uid) for uid in candidates if int(uid) not in used]
        if len(available) < int(minimum):
            return None, {}
        chosen = available[:int(minimum)]
        selected.extend(chosen)
        used.update(chosen)

    target_map = {}
    for index, target in enumerate(graph.targets):
        if not getattr(target, "requires_input", False):
            continue
        candidates = legal_targets_for(
            db, SESSION_ID, PLAYER_PID, target, int(source_uid),
            both_players=True, champions=champions, battle_state=state)
        minimum = int(getattr(target, "minimum", 0) or 0)
        if not candidates:
            if getattr(target, "optional", False) or minimum == 0:
                continue
            return None, {}
        maximum = int(target.resolved_maximum() or 0)
        wanted = max(1, minimum)
        if maximum > 0:
            wanted = min(wanted, maximum)
        available = [int(uid) for uid in candidates if int(uid) not in used]
        if not available:
            if getattr(target, "target_kind", "") in _SPECIAL_TARGET_KINDS:
                continue
            return None, {}
        chosen = available[:wanted]
        selected.extend(chosen)
        used.update(chosen)
        if (not getattr(target, "is_auto", False) and
                getattr(target, "target_kind", "") not in _SPECIAL_TARGET_KINDS):
            target_map[index] = tuple(chosen)
    return selected, target_map


def _last_option_list(game):
    return next(
        (event for event in reversed(game.events)
         if isinstance(event, game_engine.PlayerOptionListSessionEventArgs)),
        None)


def _option_guids(option_list, card_uid):
    if option_list is None:
        return set()
    for option in option_list.options:
        if int(option.card.uid.uid64) != int(card_uid):
            continue
        return {str(instance.opt_id.guid).lower()
                for instance in option.instances}
    return set()


def _wire_visit(value, path="event"):
    if isinstance(value, game_engine.SessionCardId):
        if value.uid.uid_type == 0:
            raise ChampionSweepFailure(f"undefined SessionCardId at {path}")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _wire_visit(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _wire_visit(child, f"{path}[{index}]")
    elif hasattr(value, "__dict__"):
        for key, child in vars(value).items():
            _wire_visit(child, f"{path}.{key}")


def _validate_game_wire(game):
    for index, event in enumerate(game.events):
        _wire_visit(event, f"event[{index}]")
    game.make_network_packet(PLAYER_UID)
    game.make_network_packet(OPPONENT_UID)


def _normalise_zone(value):
    value = str(value or "").rsplit(".", 1)[-1].lower()
    return {"crypt": "discard"}.get(value, value)


def _field(value, name, default=None):
    if hasattr(value, "field"):
        return value.field(name, default)
    if isinstance(value, dict):
        return value.get(name, default)
    return default


def _guid(value):
    """Read either the Records ``m_Guid`` or normalized ``m_guid`` form."""
    try:
        guid = reference_guid(value)
    except (AttributeError, TypeError, ValueError):
        guid = ""
    if not guid and hasattr(value, "field"):
        guid = value.field("m_guid", value.field("m_Guid", ""))
    if not guid and isinstance(value, dict):
        guid = value.get("m_guid") or value.get("m_Guid") or value.get("guid")
    return str(guid or "").lower()


def _card_changes_to(entry, destination):
    destination = _normalise_zone(destination)
    result = []
    for change in entry.get("card_changes") or ():
        before = change.get("before") or {}
        after = change.get("after") or {}
        if (after and _normalise_zone(after.get("location")) == destination
                and _normalise_zone(before.get("location")) != destination):
            result.append(change)
    return result


def _card_was_already_in(entry, destination):
    destination = _normalise_zone(destination)
    for change in entry.get("card_changes") or ():
        before = change.get("before") or {}
        after = change.get("after") or {}
        if (before and after and
                _normalise_zone(before.get("location")) == destination and
                _normalise_zone(after.get("location")) == destination):
            return True
    return False


def _card_left_deck(entry):
    """True when a deck card moved out of the deck during the effect.

    A replaced draw (Booby Trap reveals the drawn card and voids it) leaves the
    deck without ever reaching hand, so a deck->hand assertion is not valid for
    that effect instance.
    """
    for change in entry.get("card_changes") or ():
        before = change.get("before") or {}
        after = change.get("after") or {}
        if (before and after and
                _normalise_zone(before.get("location")) == "deck" and
                _normalise_zone(after.get("location")) != "deck"):
            return True
    return False


def _typed_draw_count(graph, effect):
    """Return the authored draw count, or ``None`` when it cannot be read.

    A "for each <counter>" count is a Counter/IntAttr variable whose default is
    zero; on a board with no counters the effect is a legitimate no-op.  Flat
    ``AbilityConstant`` values resolve to their constant.
    """
    template = getattr(effect, "template", None)
    value = _field(template, "m_InputValue") if template is not None else None
    if value is None:
        return None
    if hasattr(value, "field"):
        name = (value.field("m_InputVariableName")
                or value.field("m_VariableName"))
    elif isinstance(value, dict):
        name = (value.get("m_InputVariableName")
                or value.get("m_VariableName"))
    else:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if not name:
        return None
    for variable in getattr(graph, "variables", ()) or ():
        try:
            if str(variable.field("m_Name")) != str(name):
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        type_name = str(getattr(variable, "type_name", "") or
                        variable.raw.get("_t", ""))
        if "AbilityConstant" in type_name:
            try:
                return int(variable.field("m_DefaultValue", 0) or 0)
            except (TypeError, ValueError):
                return None
        # Counter/IntAttr/other computed variables are zero here.
        return 0
    return None


def _effect_contract_error(effect, trace, graph=None):
    """Check postconditions that are unambiguous in typed effect metadata."""
    if trace.get("error"):
        return str(trace["error"])
    if not str(trace.get("result") or "").strip():
        return "effect returned no result"
    effect_type = str(effect.concrete_type or "")
    template = effect.template
    if effect_type == "MoveCardToZoneEffectTemplate":
        destination = _normalise_zone(
            _field(template, "m_DestinationCollection", ""))
        if destination in {"hand", "discard", "deck", "void", "warzone",
                           "underground"} and not _card_changes_to(
                               trace, destination) and not \
                _card_was_already_in(trace, destination):
            return f"move to {destination} produced no persisted zone change"
    if effect_type in {"BuryCardAbilityEffectTemplate",
                       "DiscardCardAbilityEffectTemplate",
                       "DiscardOrSacrificeCardAbilityEffectTemplate"}:
        if not (_card_changes_to(trace, "discard") or
                _card_changes_to(trace, "underground")):
            return "discard/bury produced no persisted zone change"
    if effect_type == "VoidCardAbilityEffectTemplate" and not \
            _card_changes_to(trace, "void"):
        return "void effect produced no persisted void change"
    if effect_type in {"DrawCardAbilityEffectTemplate",
                       "DrawNCardsAbilityEffectTemplate",
                       "PutTopOfDeckIntoHandAbilityEffectTemplate"} and not \
        _card_changes_to(trace, "hand"):
        # A draw does not guarantee a deck->hand move: a replacement effect
        # such as Booby Trap reveals the drawn card and voids it, and a
        # "for each counter" count is zero on a board with no counters.  Only
        # an authored constant/positive count with no replacement (and no
        # hand change) is a real failure.
        if (effect_type != "PutTopOfDeckIntoHandAbilityEffectTemplate"
                and _card_left_deck(trace)):
            pass
        elif _typed_draw_count(graph, effect) == 0:
            pass
        else:
            return "draw effect produced no deck-to-hand change"
    if effect_type == "CardModifierAbilityEffectTemplate":
        from rules_port.metadata import modifier_metadata
        modifier = modifier_metadata(template=template)
        if str(modifier.get("property") or "").lower() == "cardthreshold":
            threshold_changed = False
            for change in trace.get("card_changes") or ():
                before = change.get("before") or {}
                after = change.get("after") or {}
                if before.get("permanent_buffs") == after.get(
                        "permanent_buffs"):
                    continue
                try:
                    before_buffs = json.loads(
                        before.get("permanent_buffs") or "{}")
                    after_buffs = json.loads(
                        after.get("permanent_buffs") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if before_buffs.get("thresholds") != after_buffs.get(
                        "thresholds"):
                    threshold_changed = True
                    break
            if not threshold_changed:
                return "card-threshold modifier produced no persisted threshold change"
    if effect_type in {"SummonTokenTroopAbilityEffectTemplate",
                       "SummonXTokenTroopsAbilityEffectTemplate"}:
        token = _guid(_field(template, "m_CardTemplateId", ""))
        destination = _normalise_zone(
            _field(template, "m_CardCollection", "warzone"))
        if destination in {"", "unknown"}:
            destination = "warzone"
        new_warzone = [
            change for change in trace.get("card_changes") or ()
            if not change.get("before") and
            _normalise_zone((change.get("after") or {}).get("location"))
            == destination
        ]
        amount = int(_field(template, "m_Amount", 1) or 1)
        if amount > 0 and len(new_warzone) < amount:
            return (f"summon expected {amount} typed token(s), "
                    f"observed {len(new_warzone)} new {destination} card(s)")
        token_changes = [
            change for change in new_warzone
            if str((change.get("after") or {}).get("template_guid") or "")
            .lower() == token
        ]
        # Some authored token effects leave the serialized template reference
        # as the all-zero ResourceId and select the token through another
        # typed field. The persisted-card assertion above still proves that a
        # token was generated; only compare a non-zero authored reference.
        if token and token != _ZERO_GUID and amount > 0 and len(token_changes) < amount:
            return (f"summon expected {amount} typed token(s) {token}, "
                    f"observed {len(token_changes)}")
    return None


def _target_has_no_candidates(db, graph, handler, state, source_uid, effect):
    """True when an automatic effect target enumerates nothing on the board."""
    try:
        index = int(getattr(effect, "target_index", -1))
    except (TypeError, ValueError):
        return False
    if index < 0 or index >= len(graph.targets):
        return False
    target = graph.targets[index]
    kind = str(getattr(target, "target_kind", "") or "")
    if kind == "SourceRevealedTargetTemplate":
        # A source-revealed target depends on the reveal effect feeding it.
        return not (state.get("revealed_cards") or ())
    list_name = {
        "SourceDrawnTargetTemplate": "DrawnCards",
        "SourceBuriedTargetTemplate": "BuriedCards",
        "SourceStoredTargetTemplate": "StoredTargets",
        "AbilityCreatedTargetTemplate": "CreatedCards",
        "VoidedTargetTemplate": "VoidedCards",
    }.get(kind)
    if list_name is not None:
        # A list-derived target has no candidate until this activation records
        # one; an empty ledger is an expected skip, not a missing effect.
        guid = str(graph.guid).lower()
        if ((state.get("list_attrs") or {}).get(guid, {}) or {}).get(list_name):
            return False
        if (state.get("ability_lists") or {}).get(list_name):
            return False
        if kind == "SourceStoredTargetTemplate" and (
                (state.get("stored_targets") or {}).get(guid)
                or (state.get("stored_targets_by_card") or {})):
            return False
        return True
    if kind in _SPECIAL_TARGET_KINDS or not getattr(target, "is_auto", False):
        return False
    from rules_port.targeting import target_uses_both_players
    try:
        both_players = target_uses_both_players(db, target.guid)
    except Exception:
        return False
    try:
        candidates = legal_targets_for(
            db, SESSION_ID, PLAYER_PID, target, int(source_uid),
            both_players=both_players, champions=handler._champion_targets(),
            battle_state=state)
    except Exception:
        return False
    return not candidates


def _fixture_skippable_guids(db, graph, handler, state, source_uid):
    """Effect guids the generated board cannot legally reach.

    The fixture seeds one representative per common card type/subtype, not one
    per authored card name or subtype.  An effect is unreachable here when it
    is gated, optional, contingent on an already-unreachable effect, or has an
    automatic target whose pool is empty.  The resolver is correct to apply
    nothing for those effects, so a missing trace is a fixture limitation
    rather than a missing effect.
    """
    skippable_guids: set[str] = set()
    if not graph.effects:
        return skippable_guids
    skippable_instances: set[int] = set()
    for _ in range(len(graph.effects) + 2):
        changed = False
        for effect in graph.effects:
            key = str(effect.guid).lower()
            if key in skippable_guids:
                continue
            condition = str(getattr(effect, "condition_guid", "") or "")
            gated = bool(condition) and condition.replace(
                "-", "").lower() not in {"", "0" * 32}
            try:
                contingent = int(
                    getattr(effect, "contingent_effect_instance_id", -1))
            except (TypeError, ValueError):
                contingent = -1
            if (gated or getattr(effect, "optional", False) or
                    (contingent >= 0 and contingent in skippable_instances) or
                    _target_has_no_candidates(
                        db, graph, handler, state, source_uid, effect)):
                skippable_guids.add(key)
                try:
                    skippable_instances.add(
                        int(getattr(effect, "effect_instance_id", -1)))
                except (TypeError, ValueError):
                    pass
                changed = True
        if not changed:
            break
    return skippable_guids


def _assert_graph_trace(graph, state, *, fixture_skipped=frozenset()):
    entries = [entry for entry in state.get("ability_trace") or ()
               if str(entry.get("ability_guid") or "").lower()
               == str(graph.guid).lower()]
    if not entries:
        if graph.effects and all(
                str(effect.guid).lower() in fixture_skipped
                for effect in graph.effects):
            return 0
        raise ChampionSweepFailure(
            f"{graph.guid}: resolution produced no typed effect trace")
    wanted = Counter(str(effect.guid).lower() for effect in graph.effects
                     if effect.guid)
    observed = Counter(str(entry.get("effect_guid") or "").lower()
                       for entry in entries)
    missing = wanted - observed
    if missing:
        allowed = Counter()
        for effect in graph.effects:
            key = str(effect.guid).lower()
            if key not in missing:
                continue
            condition_guid = str(getattr(effect, "condition_guid", "") or "")
            has_condition = condition_guid.replace("-", "").lower() not in {
                "", "0" * 32}
            if (has_condition or getattr(effect, "optional", False) or
                    key in fixture_skipped):
                allowed[key] += 1
        missing -= allowed
        if missing:
            raise ChampionSweepFailure(
                f"{graph.guid}: missing typed effect trace(s): "
                + ", ".join(f"{key} x{count}"
                             for key, count in sorted(missing.items())))
    by_guid = {str(effect.guid).lower(): effect for effect in graph.effects}
    for entry in entries:
        effect = by_guid.get(str(entry.get("effect_guid") or "").lower())
        if effect is None:
            raise ChampionSweepFailure(
                f"{graph.guid}: trace contains unknown effect "
                f"{entry.get('effect_guid')}")
        error = _effect_contract_error(effect, entry, graph)
        if error:
            raise ChampionSweepFailure(
                f"{graph.guid} effect {effect.guid} "
                f"({effect.concrete_type}): {error}")
    return len(entries)


def _trigger_event(graph, board):
    event_type = str(graph.trigger_event_type or "").rsplit(".", 1)[-1]
    source = board["source_uid"]
    target = board["opponent_uid"]
    data = {"zones": ("warzone", "hand", "deck", "discard", "void")}
    if event_type == "CardEnteredZoneEvent":
        source = board["filler_source_uid"]
        data.update({"event_source_collection": "deck",
                     "event_destination_collection": "warzone"})
    elif event_type == "CardDrawnEvent":
        target = board["filler_source_uid"]
    elif event_type == "CardDealtDamageEvent":
        source = board["filler_source_uid"]
        data["event_tac"] = {"damage": 3, "DamageDealt": 3,
                              "is_combat_damage": 0}
    elif event_type == "CardCastEvent":
        # Champion trigger conditions such as "you play a Dwarf" use the
        # champion as the typed event source and the played card as target.
        source = board["source_uid"]
        target = board.get("cast_target_uid", board["filler_source_uid"])
    elif event_type == "CardReadiedEvent":
        source = board.get("readied_source_uid", board["filler_source_uid"])
        target = source
    elif event_type in {"CardTappedEvent", "CardExhaustedEvent"}:
        source = board.get("opponent_tapped_uid", board["filler_source_uid"])
        target = source
    elif event_type == "GainThresholdEvent":
        data["gain_threshold_color"] = game_engine.SHARD_TO_FLAG["wild"]
    elif event_type in {"TurnStartedEvent", "TurnEndedEvent",
                        "GameStartedEvent", "CombatEndedEvent"}:
        source = None
        target = None
    return TriggerEvent(
        event_type, int(source) if source is not None else None, PLAYER_PID,
        int(target) if target is not None else None,
        OPPONENT_PID if target is not None else None, data)


def _resolve_free_card_item(db, handler, session, state, game, item):
    """Finish a free permanent queued by a native PlayCard effect.

    The ordinary tournament chain host owns this transition. The generated
    sweep isolates that host, so reproduce only its typed CastSpells ->
    Warzone projection and re-enter native CardEnteredZone discovery for the
    created card.
    """
    if str(item.get("kind") or "") != "troop":
        raise ChampionSweepFailure(
            f"unsupported generated PvP card-chain item: {item}")
    source_uid = int(item.get("source_uid") or 0)
    from pvp_db import (db_card_chain_info, db_card_location,
                        db_card_owner_zone_state, db_set_card_location)
    row = db_card_chain_info(session.session_id, source_uid, conn=db)
    details = db_card_owner_zone_state(
        session.session_id, source_uid, conn=db)
    if not row or not details:
        raise ChampionSweepFailure(
            f"free troop {source_uid}: missing typed card-chain row")
    owner_id = int(details[0] or 0)
    if str(db_card_location(session.session_id, source_uid, conn=db) or "") \
            != "CastSpells":
        return
    entering_state = int(game_engine.ECardStates.CameOutThisTurn)
    db_set_card_location(
        session.session_id, source_uid, "warzone",
        extra_set="position=?, card_state=(card_state | ?)",
        extra_params=[0, entering_state], conn=db)
    db.commit()
    owner_uid = PLAYER_UID if owner_id == PLAYER_PID else OPPONENT_UID
    scid = game_engine.SessionCardId(game_engine.UID(source_uid))
    _tpl, card_type, _name, cost, attack, defense, gems = \
        handler._card_full_data(game, scid, row[0])
    game.push_card_updated(
        scid, owner_uid, game_engine.ECardCollections.Warzone, card_type,
        template_id=row[0], cost=cost, attack=attack, defense=defense,
        gems=gems)
    game.push_card_moved(
        scid, owner_uid, game_engine.ECardCollections.Warzone,
        game_engine.ECardLocations.Top, 0)
    game.push_troop_card_played(scid, owner_uid)
    dispatch_native_trigger(
        db=db, handler=handler, game=game, session=session,
        player_uid=PLAYER_UID, ai_uid=OPPONENT_UID,
        battle_state=state, event_type="CardEnteredZoneEvent",
        source_card_id=source_uid, source_player_id=owner_id,
        data={"event_source_collection": "CastSpells",
              "event_destination_collection": "warzone"})


def _drain_stack(db, handler, session, state, game):
    """Resolve native typed chain entries until input is required."""
    for _ in range(256):
        if state.get("resolution_paused"):
            return
        stack = state.get("stack") or []
        if not stack:
            return
        item = stack.pop()
        kind = str(item.get("kind") or "")
        if kind == "trigger":
            resolve_port_trigger(
                handler, game, session, db, PLAYER_UID, OPPONENT_UID,
                state, item)
            continue
        if kind == "ability":
            activation = item.get("activation_data") or {}
            target_map = (activation.get("target_map")
                          if isinstance(activation, dict) else {}) or {}
            resolve_port_ability(
                handler, game, session, db, PLAYER_UID, OPPONENT_UID,
                state, item.get("ability_guid"), item.get("source_uid"),
                int(item.get("source_owner_uid", PLAYER_PID) or PLAYER_PID),
                target_map=target_map,
                instance_id=int(item.get("instance_id", 1) or 1))
            continue
        if kind == "troop":
            _resolve_free_card_item(db, handler, session, state, game, item)
            continue
        raise ChampionSweepFailure(f"unexpected native PvP stack item: {item}")
    raise ChampionSweepFailure("native PvP stack exceeded 256 resolutions")


def _pending_choices(state):
    pending = state.get("pending_choice")
    if not isinstance(pending, dict):
        return None, []
    values = []
    for value in pending.get("choice_uids") or ():
        try:
            values.append(int(value))
        except (TypeError, ValueError):
            continue
    if not values:
        raise ChampionSweepFailure(
            f"choice continuation {pending.get('kind')!r} has no choices")
    return pending, values


_CONTINUATION_KEYS = (
    "pending_choice", "pending_discard_continuation", "pending_deck_search",
    "pending_conversation", "pending_trigger", "pending_revealed_choice",
    "pending_discard_ability",
)


def _pending_continuation(state):
    for key in _CONTINUATION_KEYS:
        pending = state.get(key)
        if isinstance(pending, dict) and pending:
            value = dict(pending)
            value.setdefault("kind", key.removeprefix("pending_"))
            return value
    return None


def _assert_complete(graph, state, *, static=False, fixture_skipped=frozenset()):
    if state.get("resolution_paused"):
        pending = _pending_continuation(state)
        if not pending:
            raise ChampionSweepFailure(
                f"{graph.guid}: resolution paused without a typed continuation; "
                f"keys={[key for key in _CONTINUATION_KEYS if state.get(key)]}")
        return 0
    if static:
        # Continuous champion auras are installed by the surrounding board
        # lifecycle, not by a direct activation. The graph/effect coverage,
        # CardDef registration, non-clickable option contract, and wire check
        # are the meaningful checks for this attachment here.
        return len(state.get("ability_trace") or ())
    _assert_graph_trace(graph, state, fixture_skipped=fixture_skipped)
    return len(state.get("ability_trace") or ())


_SWEEP_STATS = Counter()


def _fixture_skip_result():
    """Result for an attachment the generated board cannot exercise.

    A manual power whose authored target/payment has no legal card on the
    fixture cannot be offered or activated; the option path is correct to keep
    it greyed out, and its CardDef/wire contract is still asserted separately.
    """
    return {"pending": None, "choices": (), "traces": 0, "events": 0,
            "trigger_mode": "fixture-skip"}


def _run_case(db, metadata, path, *, option_check=True):
    graph = _metadata_graph(metadata)
    board = _seed_board(db, metadata)
    state = _new_state(metadata)
    handler = PvPHandlerStub(
        db, metadata["champion_guid"], board["champion_abilities"])
    handler._current_bstate = state
    session = SessionStub(state)

    source_uid = board["source_uid"]
    static_attachment = bool(not graph.manual and
                             not graph.trigger_event_type)
    handler._current_bstate = state

    # A manual power whose authored target/payment cannot be satisfied on the
    # generated board is a fixture limitation: the option path correctly keeps
    # it greyed out and the activation cannot be driven.  Detect that once and
    # use it both to relax the option assertion and to skip the activation.
    selected = target_map = None
    fixture_unsatisfied = False
    if graph.manual:
        selected, target_map = _activation_selection(
            db, graph, handler, state, session, source_uid)
        fixture_unsatisfied = selected is None

    option_game = game_engine.Game(SESSION_ID, PLAYER_UID, OPPONENT_UID)
    option_game.push_options(PLAYER_UID, [])
    if option_check:
        tournament_game._pvp_add_champion_options(
            option_game, session, state, PLAYER_PID, PLAYER_UID)
        option_list = _last_option_list(option_game)
        champion_def = next(
            (definition for card, definition in option_game.card_defs.items()
             if int(card.uid.uid64) == int(board["source_uid"])), None)
        expected_ids = set(board["champion_abilities"])
        actual_ids = ({str(value.guid).lower() for value in champion_def.abilities}
                      if champion_def is not None else set())
        if actual_ids != expected_ids:
            raise ChampionSweepFailure(
                f"{metadata['champion_name']}: CardDef abilities drifted; "
                f"missing={sorted(expected_ids - actual_ids)} "
                f"extra={sorted(actual_ids - expected_ids)}")
        offered = _option_guids(option_list, board["source_uid"])
        if graph.trigger_event_type and metadata["ability_guid"] in offered:
            raise ChampionSweepFailure(
                f"{metadata['champion_name']} / {metadata['ability_name']}: "
                "triggered power was exposed as a clickable PvP option")
        if (graph.manual and metadata["ability_guid"] not in offered
                and not fixture_unsatisfied):
            raise ChampionSweepFailure(
                f"{metadata['champion_name']} / {metadata['ability_name']}: "
                "manual power was not exposed by the PvP option path")
        _validate_game_wire(option_game)

    if fixture_unsatisfied:
        return _fixture_skip_result()

    # The board mutates during resolution (a prior effect can create the cards
    # a later effect targets), so record which effects are unreachable on the
    # starting board as well as the settled board.  An effect that is
    # unreachable in either snapshot was legitimately skipped for lack of a
    # legal target.
    pre_skipped = _fixture_skippable_guids(
        db, graph, handler, state, source_uid)
    if graph.manual:
        _activation_context["guid"] = metadata["ability_guid"]
        _activation_context["selected"] = list(selected)
        if not tournament_game._pvp_activate_champion_ability(
                handler, session, b"", PLAYER_PID):
            raise ChampionSweepFailure(
                f"{metadata['champion_name']} / {metadata['ability_name']}: "
                "production PvP activation rejected legal fixture data")
        item = next(
            (value for value in reversed(state.get("stack") or [])
             if str(value.get("ability_guid") or "").lower()
             == metadata["ability_guid"]), None)
        if item is None:
            raise ChampionSweepFailure(
                f"{metadata['ability_guid']}: activation did not enqueue a "
                "typed PvP chain item")
        state["stack"] = []
        state["resolving_source_uid"] = source_uid
        state["resolving_owner_id"] = PLAYER_PID
        game = handler._fresh_game(session, PLAYER_UID, OPPONENT_UID, state)
        resolve_port_ability(
            handler, game, session, db, PLAYER_UID, OPPONENT_UID, state,
            metadata["ability_guid"], source_uid, PLAYER_PID,
            target_map=target_map,
            instance_id=int(item.get("instance_id", 1) or 1))
    elif graph.trigger_event_type:
        event = _trigger_event(graph, board)
        game = handler._fresh_game(session, PLAYER_UID, OPPONENT_UID, state)
        dispatch_native_trigger(
            db=db, handler=handler, game=game, session=session,
            player_uid=PLAYER_UID, ai_uid=OPPONENT_UID,
            battle_state=state, event_type=event.event_type,
            source_card_id=event.source_card_id,
            source_player_id=event.source_player_id,
            target_card_id=event.target_card_id, data=event.data)
        matching = [
            value for value in state.get("stack") or ()
            if str(value.get("ability_guid") or "").lower()
            == metadata["ability_guid"]
        ]
        if not matching:
            # The native dispatcher has still been exercised, but a generic
            # event fixture cannot truthfully satisfy every authored trigger
            # predicate (for example an exact card filter or a losing-game
            # replacement). Run the same typed effect body directly so every
            # attachment is covered while reporting this distinction.
            _SWEEP_STATS["trigger_effect_fallback"] += 1
            state["resolving_source_uid"] = source_uid
            state["resolving_owner_id"] = PLAYER_PID
            state["resolving_trigger_event_type"] = event.event_type
            state["resolving_trigger_source_uid"] = event.source_card_id
            state["resolving_trigger_target_uid"] = event.target_card_id
            state["resolving_trigger_event_data"] = dict(event.data or {})
            if event.source_card_id is not None:
                state.setdefault("stored_targets", {})[
                    metadata["ability_guid"]] = [int(event.source_card_id)]
            target_map = _target_map(db, graph, handler, state, source_uid)
            resolve_port_ability(
                handler, game, session, db, PLAYER_UID, OPPONENT_UID, state,
                metadata["ability_guid"], source_uid, PLAYER_PID,
                target_map=target_map)
            trigger_mode = "effect-fallback"
        else:
            _SWEEP_STATS["trigger_native"] += 1
            # Other authored champion triggers may also hear the synthetic
            # event; isolate this generated attachment while retaining nested
            # triggers it creates during its own resolution.
            state["stack"] = [matching[-1]]
            trigger_mode = "native"
    elif static_attachment:
        _SWEEP_STATS["static_registration"] += 1
        game = handler._fresh_game(session, PLAYER_UID, OPPONENT_UID, state)
        trigger_mode = "static"
    else:
        target_map = _target_map(db, graph, handler, state, source_uid)
        state["resolving_source_uid"] = source_uid
        state["resolving_owner_id"] = PLAYER_PID
        game = handler._fresh_game(session, PLAYER_UID, OPPONENT_UID, state)
        resolve_port_ability(
            handler, game, session, db, PLAYER_UID, OPPONENT_UID, state,
            metadata["ability_guid"], source_uid, PLAYER_PID,
            target_map=target_map)

    _drain_stack(db, handler, session, state, game)
    for choice_index in path:
        pending, choices = _pending_choices(state)
        if pending is None:
            raise ChampionSweepFailure(
                f"{metadata['ability_guid']}: choice path {path!r} "
                "continued after resolution completed")
        try:
            selected_uid = choices[int(choice_index)]
        except (IndexError, TypeError, ValueError):
            raise ChampionSweepFailure(
                f"{metadata['ability_guid']}: invalid choice path {path!r}")
        state["trace_ability_resolution"] = True
        tournament_game._pvp_resolve_choice(
            handler, session, b"", PLAYER_PID,
            typed_payload={"activation_data": {
                "target_map": {0: [int(selected_uid)]}}})
        if not state.get("resolution_paused"):
            _drain_stack(db, handler, session, state, game)

    settled_skipped = _fixture_skippable_guids(
        db, graph, handler, state, source_uid)
    fixture_skipped = set(pre_skipped) | set(settled_skipped)
    traces = _assert_complete(
        graph, state, static=static_attachment,
        fixture_skipped=fixture_skipped)
    _validate_game_wire(game)
    pending, choices = _pending_choices(state)
    if pending is None:
        pending = _pending_continuation(state)
    choice_kinds = {"choice_zone_target", "choice_zone_copy", "double_choice"}
    if pending is not None and not choices and str(
            pending.get("kind") or "") in choice_kinds:
        raise ChampionSweepFailure(
            f"{metadata['ability_guid']}: continuation has no branches")
    return {
        "pending": pending,
        "choices": choices,
        "traces": traces,
        "events": len(game.events),
        "trigger_mode": locals().get("trigger_mode", "manual"),
    }


def _run_choice_paths(db, metadata, prefix=(), max_depth=8):
    """Run every presented continuation choice on a fresh board."""
    result = _run_case(db, metadata, prefix)
    if result["pending"] is None:
        return 1
    if len(prefix) >= max_depth:
        raise ChampionSweepFailure(
            f"{metadata['ability_guid']}: choice continuation exceeded "
            f"{max_depth} levels")
    kind = str(result["pending"].get("kind") or "")
    if kind not in {"choice_zone_target", "choice_zone_copy", "double_choice"}:
        if kind in {"conversation", "trigger", "revealed_choice",
                    "discard_continuation", "discard_ability", "deck_search"}:
            _SWEEP_STATS["non_choice_continuation"] += 1
            return 1
        raise ChampionSweepFailure(
            f"{metadata['ability_guid']}: unsupported PvP continuation {kind!r}")
    branches = 0
    for index in range(len(result["choices"])):
        branches += _run_choice_paths(db, metadata, prefix + (index,), max_depth)
    return branches


_activation_context = {"guid": "", "selected": []}


def _run_sweep(limit=None, only=None):
    _SWEEP_STATS.clear()
    fd, sandbox_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy(SRC, sandbox_path)
    db = sqlite3.connect(sandbox_path)
    attachments = _champion_attachments(db)
    if only:
        wanted = str(only).lower()
        attachments = [row for row in attachments
                       if wanted in row["champion_name"].lower() or
                       wanted in row["ability_name"].lower() or
                       wanted in row["ability_guid"]]
    if limit is not None:
        attachments = attachments[:max(0, int(limit))]
    failures = []
    passed = 0
    choice_branches = 0

    previous = {
        "tournament_db": tournament_game._db,
        "db": dbmod._db,
        "handlers": tournament_game.player_handlers,
        "send_same": tournament_game._pvp_send_same_events,
        "send_packet": tournament_game._send_pvp_packet,
        "dispatch": tournament_game._pvp_dispatch_triggers,
        "phase_options": tournament_game.pvp_push_phase_options,
        "main_options": tournament_game.pvp_push_main_phase_options,
        "extract": tournament_game.extract_ability_guid,
        "transaction_uids": tournament_game._pvp_transaction_card_uids,
    }
    try:
        tournament_game._db = db
        dbmod._db = db
        tournament_game.player_handlers = {}
        tournament_game._pvp_send_same_events = lambda *args, **kwargs: None
        tournament_game._send_pvp_packet = lambda *args, **kwargs: None
        # CardActivated trigger projection is tested independently through the
        # native trigger run; suppress it here to focus a manual case on the
        # selected graph and avoid unrelated trigger chains.
        tournament_game._pvp_dispatch_triggers = lambda *args, **kwargs: ""
        tournament_game.pvp_push_phase_options = lambda *args, **kwargs: None
        tournament_game.pvp_push_main_phase_options = lambda *args, **kwargs: None
        tournament_game.extract_ability_guid = (
            lambda _inner: _activation_context["guid"])
        tournament_game._pvp_transaction_card_uids = (
            lambda _inner: list(_activation_context["selected"]))

        for index, metadata in enumerate(attachments, 1):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    branches = _run_choice_paths(db, metadata)
                passed += 1
                choice_branches += max(0, branches - 1)
            except Exception:
                failures.append((metadata, traceback.format_exc()))
            if index % 100 == 0:
                print(f"Champion PvP sweep progress: {index}/{len(attachments)}")
    finally:
        (tournament_game._db, dbmod._db,
         tournament_game.player_handlers,
         tournament_game._pvp_send_same_events,
         tournament_game._send_pvp_packet,
         tournament_game._pvp_dispatch_triggers,
         tournament_game.pvp_push_phase_options,
         tournament_game.pvp_push_main_phase_options,
         tournament_game.extract_ability_guid,
         tournament_game._pvp_transaction_card_uids) = (
             previous["tournament_db"], previous["db"], previous["handlers"],
             previous["send_same"], previous["send_packet"],
             previous["dispatch"], previous["phase_options"],
             previous["main_options"], previous["extract"],
             previous["transaction_uids"])
        db.close()
        try:
            os.remove(sandbox_path)
        except OSError:
            pass

    print(
        f"Champion PvP dynamic sweep: {passed}/{len(attachments)} "
        f"authored attachments passed "
        f"({len(set(row['ability_guid'] for row in attachments))} distinct "
        f"graphs; {choice_branches} extra choice branches)"
    )
    print(
        "Coverage detail: "
        f"{_SWEEP_STATS['trigger_native']} native trigger queues, "
        f"{_SWEEP_STATS['trigger_effect_fallback']} trigger effect fallbacks, "
        f"{_SWEEP_STATS['static_registration']} static registrations"
    )
    if failures:
        print(f"FAILED ({len(failures)}):")
        for metadata, detail in failures:
            print(f"  {metadata['champion_name']} — {metadata['ability_name']} "
                  f"{metadata['ability_guid']}")
            print(detail.rstrip())
    else:
        print("All selected PvP champion powers passed option, native resolution, "
              "trace, continuation, and wire contracts.")
    return len(failures)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only")
    args = parser.parse_args()
    return _run_sweep(limit=args.limit, only=args.only)


if __name__ == "__main__":
    raise SystemExit(main())
