#!/usr/bin/env python3
"""Generated card play/trigger matrix for the Records card catalogue.

This is an opt-in audit because it exercises thousands of authored templates.
It is deliberately one generated test loop, not one hand-maintained function
per card:

    python3 tests/tests_card_play_matrix.py --all --limit 25
    python3 tests/tests_card_play_matrix.py --all
    python3 tests/tests_card_play_matrix.py --rarity Rare --rarity Legendary --contracts-only
    python3 tests/tests_card_play_matrix.py --only 'card name' --contracts
    python3 tests/tests_card_play_matrix.py --ability-text 'ability phrase' --contracts

The fixture is intentionally broad rather than card-specific.  It gives the
native resolver a player and opposing champion, troops, an artifact, cards in
hand/deck/discard, resources, and a spell on CastSpells.  Target choices come
from the authored target templates and the native legal-target evaluator.

The assertions are a resolution contract: every selected card must enter the
real typed play/trigger path, leave a valid persisted state, and serialize its
events.  The separate card-text audit checks unambiguous printed claims
against the typed Records graph.  Neither layer treats localized text as the
rules source, and neither claims that one board proves every conditional branch
of a card; ``--contracts`` adds an authoritative per-effect trace and typed
postconditions for every authored graph on a fresh fixture.  Complex choices
are resumed through their typed continuation: every presented choice branch is
run on a fresh fixture, and a branch failure is reported instead of silently
counting the paused card as a pass.
"""

from __future__ import annotations

import argparse
import contextlib
from collections import Counter
import hashlib
import io
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

# Bind this process's test database before importing runtime modules.
SRC = fresh_database()

import game_engine
from gamedata import ActivationData, DEFAULT_RECORD_STORE, ability_graph
from gamedata.records import reference_guid
from rules_port.kernel import MultiplyWithCarryRng
from rules_port.resolution import (resolve_port_ability,
                                   resolve_port_played_spell,
                                   resolve_port_trigger)
from rules_port.targeting import legal_targets_for
import rules_port.triggers as trigger_module
from rules_port.triggers import (NativeTriggerBackend, TriggerEvent,
                                 dispatch_native_trigger)
from rules_port.coverage import NATIVE_EFFECTS, STRUCTURAL_EFFECTS
from tests.tests_combat import HandlerStub, SessionStub


SESSION_ID = 1
PLAYER_OWNER = 5
AI_OWNER = 0
PLAYER_UID = game_engine.UID.make(244, PLAYER_OWNER)
AI_UID = game_engine.UID.make(3, 1000)
PLAYER_CHAMP = int(PLAYER_UID.uid64)
AI_CHAMP = int(AI_UID.uid64)
CARD_UID_BASE = 10000

_PERMANENT_BITS = (game_engine.ECardTypes.Troop |
                   game_engine.ECardTypes.Artifact |
                   game_engine.ECardTypes.Constant)
_SPECIAL_TARGET_KINDS = {
    "AbilitySourceCardTargetTemplate", "AbilityTriggerCardTargetTemplate",
    "SourceDrawnTargetTemplate", "SourceBuriedTargetTemplate",
    "SourceStoredTargetTemplate", "SourceRevealedTargetTemplate",
    "VoidedTargetTemplate", "AbilityCreatedTargetTemplate",
    "PlayerTargetTemplate", "MatchSecondaryTargetTemplate",
    "SecondaryTargetTemplate", "DuplicateCardTargetTemplate",
    "TargetsAPlayerOrHisStuff",
}
_KNOWN_EFFECTS = set(NATIVE_EFFECTS) | set(STRUCTURAL_EFFECTS)
_CONTRACT_RANDOM_LOCK = threading.RLock()


class CardMatrixTimeout(RuntimeError):
    """A malformed/generated scenario must not stall the whole catalogue."""


class CardMatrixTriggerRecursion(RuntimeError):
    """A generated fixture recursively re-entered native trigger dispatch."""


class _MatrixTriggerBackend(NativeTriggerBackend):
    """Bound native trigger recursion for one generated card scenario.

    The production dispatcher is intentionally untouched.  This guard only
    protects the opt-in catalogue runner from a malformed fixture or a real
    recursive authored interaction consuming the entire audit timeout.
    """

    MAX_DEPTH = 64
    # Some authored contracts stage hundreds of matching permanents (for
    # example, one witness per Egg for Brood Baron).  Keep a generous bound so
    # those legitimate trigger checks complete while malformed trigger loops
    # remain bounded by this fixture-only guard and the per-card timeout.
    MAX_CALLS = 4096

    def __call__(self, *, battle_state, **kwargs):
        depth = int(battle_state.get("_card_matrix_trigger_depth", 0) or 0)
        if depth >= self.MAX_DEPTH:
            raise CardMatrixTriggerRecursion(
                f"native trigger recursion exceeded {self.MAX_DEPTH} levels")
        calls = int(battle_state.get("_card_matrix_trigger_calls", 0) or 0)
        if calls >= self.MAX_CALLS:
            raise CardMatrixTriggerRecursion(
                f"native trigger dispatch exceeded {self.MAX_CALLS} calls")
        battle_state["_card_matrix_trigger_calls"] = calls + 1
        battle_state["_card_matrix_trigger_depth"] = depth + 1
        try:
            return super().__call__(battle_state=battle_state, **kwargs)
        finally:
            if depth:
                battle_state["_card_matrix_trigger_depth"] = depth
            else:
                battle_state.pop("_card_matrix_trigger_depth", None)


class _AbilityContractHandlerStub(HandlerStub):
    """Resolve metadata-defined deck-target choices in the contract fixture."""

    def _prompt_deck_search(self, game, session, pl_t, ai_t, bstate,
                            ability_guid, source_uid, owner_id, candidates,
                            kind="search", continuation=None, **kwargs):
        if kind != "matching_target":
            return super()._prompt_deck_search(
                game, session, pl_t, ai_t, bstate, ability_guid, source_uid,
                owner_id, candidates, kind=kind, **kwargs)
        if not candidates or not continuation:
            return "deck target: no candidates"
        continuation = dict(continuation)
        target_map = _numeric_target_map(continuation.get("target_map"))
        target_map[int(continuation.get("target_index", 0) or 0)] = (
            int(candidates[0]),)
        bstate.pop("resolution_paused", None)
        self._current_bstate = bstate
        return resolve_port_ability(
            self, game, session, self._db, pl_t, ai_t, bstate,
            str(continuation.get("ability_guid") or ability_guid).lower(),
            int(continuation.get("source_uid", source_uid) or 0),
            int(continuation.get("owner_id", owner_id) or owner_id),
            target_map=target_map,
            variables=continuation.get("variables") or {},
            resume_from_order=int(
                continuation.get("resume_effect_order", 0) or 0))


def _alarm_handler(_signum, _frame):
    raise CardMatrixTimeout("card scenario exceeded its time limit")


def _run_ability_contracts_with_timeout(db, guid, card_timeout):
    """Bound one card run and isolate its Python-random choices by GUID."""
    seed = int.from_bytes(
        hashlib.sha256(str(guid).lower().encode("ascii")).digest()[:8],
        "big")
    with _CONTRACT_RANDOM_LOCK:
        previous_random_state = random.getstate()
        try:
            random.seed(seed)
            previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
            signal.setitimer(signal.ITIMER_REAL, max(1, int(card_timeout)))
            try:
                from unittest import mock
                from rules_port.context import EffectContext
                original_child_resolver = \
                    EffectContext._resolve_child_ability

                def resolve_child_with_fixture_choice(
                        context, child_guid, source_uid, owner_id,
                        target_map):
                    return _resolve_contract_child_choice(
                        context, child_guid, source_uid, owner_id,
                        target_map,
                        original_resolver=original_child_resolver)

                with mock.patch.object(
                        EffectContext, "_resolve_child_ability",
                        resolve_child_with_fixture_choice):
                    return _run_ability_contracts(db, guid)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
        finally:
            random.setstate(previous_random_state)


def _json_list(value):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _raw_uid(instance):
    return int(game_engine.UID.make(1, instance).uid64)


def _template_guid(db, card_type, *, require_empty_abilities=False):
    query = ("SELECT guid FROM card_templates WHERE card_type=? "
             "ORDER BY guid")
    for (guid,) in db.execute(query, (card_type,)):
        if not require_empty_abilities:
            return guid
        abilities = db.execute(
            "SELECT abilities_json FROM card_templates WHERE guid=?", (guid,)
        ).fetchone()[0]
        if not _json_list(abilities):
            return guid
    return db.execute(
        "SELECT guid FROM card_templates ORDER BY guid LIMIT 1"
    ).fetchone()[0]


def _insert_card(db, uid, owner, location, template_guid, position,
                 *, abilities=None, state=0):
    row = db.execute(
        "SELECT card_type, attributes, abilities_json FROM card_templates "
        "WHERE guid=?", (template_guid,)).fetchone()
    if row is None:
        raise AssertionError(f"fixture template missing: {template_guid}")
    card_type, attributes, authored_abilities = row
    if abilities is None:
        abilities = _json_list(authored_abilities)
    db.execute(
        "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,"
        "card_template_id,location,position,card_state,card_abilities,"
        "card_type,card_attributes,card_attack_mod,card_defense_mod,"
        "card_cost_mod,card_damage,permanent_buffs,temporary_buffs,"
        "card_uses,resolved_at,original_template_guid) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (SESSION_ID, int(owner), int(uid), template_guid, template_guid,
         location, int(position), int(state), json.dumps(abilities), card_type,
         int(attributes or 0), 0, 0, 0, 0, "{}", "{}", "{}", 0,
         template_guid),
    )


def _effect_tac_function(effect):
    if str(effect.concrete_type or "") != "TACAbilityEffectTemplate":
        return ""
    serialized = _field(effect.template, "m_SerializedTAC", None)
    serialized = _field(serialized, "data", "")
    from rules_port.tac import tac_function
    return tac_function(serialized)


def _needs_standard_deck_threshold_provider(ability_guids):
    """Whether this authored graph closure needs a threshold-bearing target."""
    pending = list(ability_guids or ())
    visited = set()
    while pending:
        guid = str(pending.pop()).lower()
        if guid in visited:
            continue
        visited.add(guid)
        graph = ability_graph(DEFAULT_RECORD_STORE, guid)
        if graph is None:
            continue
        for effect in graph.effects:
            child_guid = _invoked_ability_guid(effect)
            if child_guid:
                pending.append(child_guid)
            if _effect_tac_function(effect) != "GainTargetsProvidedThresholds":
                continue
            try:
                target_index = int(effect.target_index)
            except (TypeError, ValueError):
                continue
            if target_index < 0 or target_index >= len(graph.targets):
                continue
            target = graph.targets[target_index]
            if int(target.minimum or 0) <= 0 or target.optional:
                continue
            card_filter = target.card_filter
            if hasattr(card_filter, "to_dict"):
                card_filter = card_filter.to_dict()
            resources = list(_filter_nodes(card_filter, "IsResource"))
            standard = any(
                str(node.get("m_SubType") or "").rsplit(".", 1)[-1]
                .lower() == "standard"
                for node in _filter_nodes(card_filter, "IsSubType"))
            deck = any(
                str(node.get("m_Collection") or "").rsplit(".", 1)[-1]
                .lower() == "deck"
                for node in _filter_nodes(card_filter, "InZone"))
            if resources and standard and deck:
                return True
    return False


def _seed_standard_threshold_provider(db, uid, position):
    """Seed one legal Standard resource that supplies authored thresholds."""
    from rules_port.resources import card_template_threshold_flags
    rows = db.execute(
        "SELECT guid FROM card_templates WHERE card_type='Resource' "
        "AND LOWER(COALESCE(subtype,'')) LIKE '%standard%' ORDER BY guid"
    ).fetchall()
    for (guid,) in rows:
        _insert_card(db, uid, PLAYER_OWNER, "deck", guid, position)
        if card_template_threshold_flags(db, SESSION_ID, uid):
            return guid
        db.execute(
            "DELETE FROM game_cards WHERE session_id=? AND card_uid=?",
            (SESSION_ID, int(uid)))
    raise AssertionError(
        "no authored Standard resource provides a threshold in Records")


def _is_permanent(card_type):
    return bool(game_engine.card_type_from_db(card_type) & _PERMANENT_BITS)


def _is_resource(card_type):
    return bool(game_engine.card_type_from_db(card_type) &
                game_engine.ECardTypes.Resource)


def _card_graphs(ability_guids):
    result = []
    for guid in ability_guids:
        graph = ability_graph(DEFAULT_RECORD_STORE, guid)
        if graph is None:
            raise AssertionError(f"ability is missing from Records: {guid}")
        result.append(graph)
    return tuple(result)


def _field(value, name, default=None):
    if hasattr(value, "field"):
        return value.field(name, default)
    if isinstance(value, dict):
        return value.get(name, default)
    return default


def _guid(value):
    guid = str(reference_guid(value) or "").lower()
    return ("" if guid == "00000000-0000-0000-0000-000000000000"
            else guid)


def _normalise_zone(value):
    zone = str(value or "").rsplit(".", 1)[-1].lower()
    # Most persisted card locations are lowercase, but these two C# enum
    # names are also the canonical database values. Preserve their spelling
    # so zone-event fixtures can find the same rows the runtime resolver does.
    return {
        "crypt": "discard",
        "castspells": "CastSpells",
        "playedresources": "PlayedResources",
    }.get(zone, zone)


def _seed_board(db, card_guid, *, contract_support=False):
    """Create one broad, typed board and return its source UID and metadata."""
    db.execute("DELETE FROM game_cards")
    row = db.execute(
        "SELECT guid, name, card_type, abilities_json, variable_cost, "
        "variable_cost_double, variable_cost_minimum "
        "FROM card_templates WHERE guid=?", (card_guid,)).fetchone()
    if row is None:
        raise AssertionError(f"card template missing: {card_guid}")
    guid, name, card_type, raw_abilities, variable, variable_double, variable_min = row
    abilities = _json_list(raw_abilities)
    source_uid = _raw_uid(CARD_UID_BASE)

    plain_troop = _template_guid(db, "Troop", require_empty_abilities=True)
    plain_artifact = _template_guid(db, "Artifact", require_empty_abilities=True)
    plain_action = _template_guid(db, "BasicAction")
    resource = _template_guid(db, "Resource")

    if _is_resource(card_type):
        source_location = "PlayedResources"
    else:
        source_location = "CastSpells"
    _insert_card(db, source_uid, PLAYER_OWNER, source_location, guid, 0)

    # The filler cards have authored templates but an empty instance ability
    # list, so the trigger backend cannot accidentally run an unrelated card.
    filler = [
        (_raw_uid(CARD_UID_BASE + 1), PLAYER_OWNER, "warzone", plain_troop, 0),
        (_raw_uid(CARD_UID_BASE + 2), AI_OWNER, "warzone", plain_troop, 0),
        (_raw_uid(CARD_UID_BASE + 3), PLAYER_OWNER, "warzone", plain_artifact, 1),
        (_raw_uid(CARD_UID_BASE + 4), AI_OWNER, "warzone", plain_artifact, 1),
        (_raw_uid(CARD_UID_BASE + 5), PLAYER_OWNER, "hand", plain_action, 0),
        (_raw_uid(CARD_UID_BASE + 6), AI_OWNER, "hand", plain_troop, 0),
        (_raw_uid(CARD_UID_BASE + 7), PLAYER_OWNER, "discard", plain_troop, 0),
        (_raw_uid(CARD_UID_BASE + 8), AI_OWNER, "discard", plain_troop, 0),
        (_raw_uid(CARD_UID_BASE + 9), PLAYER_OWNER, "deck", plain_action, 0),
        (_raw_uid(CARD_UID_BASE + 10), AI_OWNER, "deck", plain_troop, 0),
        (_raw_uid(CARD_UID_BASE + 11), PLAYER_OWNER, "PlayedResources", resource, 0),
        (_raw_uid(CARD_UID_BASE + 12), AI_OWNER, "PlayedResources", resource, 0),
        (_raw_uid(CARD_UID_BASE + 13), PLAYER_OWNER, "CastSpells", plain_action, 0),
        (_raw_uid(CARD_UID_BASE + 14), AI_OWNER, "CastSpells", plain_action, 0),
    ]
    for uid, owner, location, template, position in filler:
        _insert_card(db, uid, owner, location, template, position, abilities=[])
    if contract_support:
        # A real typed resource is needed by random resource/deck contracts.
        # When an authored child explicitly reads its target's provided
        # thresholds, seed a Standard shard with its printed abilities so the
        # nested picker resolves the same target contract as the client.
        # Other graphs retain the inert resource filler to avoid introducing
        # unrelated threshold triggers into their generated board.
        support_resource_uid = _raw_uid(CARD_UID_BASE + 30)
        if _needs_standard_deck_threshold_provider(abilities):
            _seed_standard_threshold_provider(
                db, support_resource_uid, 30)
        else:
            _insert_card(db, support_resource_uid, PLAYER_OWNER, "deck",
                         resource, 30, abilities=[])
        flight = db.execute(
            "SELECT guid FROM card_templates WHERE card_type='Troop' "
            "AND (attributes & ?) != 0 ORDER BY guid LIMIT 1",
            (int(game_engine.ECardAttributes.Flight),)).fetchone()
        if flight is not None:
            _insert_card(db, _raw_uid(CARD_UID_BASE + 31), PLAYER_OWNER,
                         "warzone", flight[0], 31, abilities=[])
        constant = _template_guid(
            db, "Constant", require_empty_abilities=True)
        _insert_card(db, _raw_uid(CARD_UID_BASE + 32), PLAYER_OWNER,
                     "warzone", constant, 32, abilities=[])
        # Deck effects need a real typed witness pool. Three cards per side
        # lets mill/reveal contracts exercise their authored count, and a
        # low-defense troop exposes follow-up filters after stat changes.
        troop_rows = db.execute(
            "SELECT guid,COALESCE(attack,0),COALESCE(defense,0) "
            "FROM card_templates WHERE lower(card_type)='troop' "
            "ORDER BY COALESCE(defense,0),COALESCE(attack,0) DESC,guid"
        ).fetchall()
        if troop_rows:
            low_troop = str(troop_rows[0][0])
            adjusted_troop = next((str(row[0]) for row in troop_rows
                                   if int(row[2] or 0) > 0), low_troop)
            sturdy_troop = next((str(row[0]) for row in reversed(troop_rows)
                                 if int(row[2] or 0) > 1), adjusted_troop)
            deck_templates = (low_troop, adjusted_troop, sturdy_troop,
                              adjusted_troop, sturdy_troop, low_troop)
            for owner, base in ((PLAYER_OWNER, 100), (AI_OWNER, 200)):
                for ordinal, template_guid in enumerate(deck_templates):
                    _insert_card(
                        db, _raw_uid(CARD_UID_BASE + base + ordinal),
                        owner, "deck", template_guid, ordinal + 1,
                        abilities=[])
    db.commit()
    return {
        "guid": guid,
        "name": name or guid,
        "card_type": card_type or "",
        "ability_guids": tuple(str(value).lower() for value in abilities),
        "source_uid": source_uid,
        "variable": bool(variable or variable_double),
        "variable_minimum": int(variable_min or 0),
        "filler_player": _raw_uid(CARD_UID_BASE + 1),
        "filler_ai": _raw_uid(CARD_UID_BASE + 2),
        "filler_action": _raw_uid(CARD_UID_BASE + 13),
    }


def _new_state(handler, *, trace=False):
    state = {
        "player_health": 17,
        "ai_health": 17,
        "player_max_health": 20,
        "ai_max_health": 20,
        "player_resources": 30,
        "player_total_resources": 30,
        "ai_resources": 30,
        "ai_total_resources": 30,
        "player_charges": 10,
        "ai_charges": 10,
        "player_spell_points": 10,
        "ai_spell_points": 10,
        "player_threshold": {1: 10, 2: 10, 4: 10, 8: 10, 16: 10, 32: 10, 64: 10},
        "ai_threshold": {1: 10, 2: 10, 4: 10, 8: 10, 16: 10, 32: 10, 64: 10},
        "turn_number": 1,
        "turn_player": "player",
        "starting_player": "player",
        "champ_map": {str(PLAYER_OWNER): PLAYER_CHAMP, str(AI_OWNER): AI_CHAMP},
        "stack": [],
        "_next_instance_id": 1,
        "_rules_port_attached": True,
        "resolving_owner_id": PLAYER_OWNER,
        # Keep generated choice branches reproducible.  This is the same
        # client-side RNG implementation used by the RulesPort session, and
        # it prevents a second branch run from selecting a different authored
        # option merely because Python's process RNG advanced.
        "_rules_rng": MultiplyWithCarryRng(12345, 67890),
    }
    if trace:
        state["trace_ability_resolution"] = True
    return state


def _announce_card(game, handler, db, uid, owner, location):
    row = db.execute(
        "SELECT template_guid, card_type, card_state FROM game_cards "
        "WHERE session_id=? AND card_uid=?", (SESSION_ID, int(uid))
    ).fetchone()
    if row is None:
        return
    template_guid, card_type, state = row
    scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
    _tpl, ctype, _name, cost, attack, defense, gems = handler._card_full_data(
        game, scid, template_guid)
    from rules_port.runtime_helpers import card_collection_for_location, owner_uid
    player = owner_uid(owner, PLAYER_UID, AI_UID)
    collection = card_collection_for_location(location)
    # The C# cache derives its internal move for a newly cached card from this
    # update. Keep the fixture stream aligned and avoid a duplicate wire event.
    game.push_card_updated(
        scid, player, collection, ctype, state=int(state or 0),
        template_id=template_guid, cost=cost, attack=attack,
        defense=defense, gems=gems)


def _contract_filter_is_template_only(filter_spec):
    """Keep only target conditions the fixture can test from card metadata."""
    allowed = {
        "AndCardFilter", "OrCardFilter", "NotCardFilter", "IsSubType",
        "IsTroop", "IsType", "IsResource", "IsArtifact", "IsConstant",
        "IsColor", "IsBasic", "IsQuick", "NameContainsFilter",
        "IsCardName", "HasCastingCost", "HasResourceCost",
    }

    def visit(value):
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        if isinstance(value, dict):
            kind = str(value.get("_t", "")).rsplit(".", 1)[-1]
            if kind and kind not in allowed:
                return False
            return all(visit(child) for key, child in value.items()
                       if key != "_t")
        if isinstance(value, (list, tuple)):
            return all(visit(child) for child in value)
        return True

    return visit(filter_spec)


def _contract_target_condition_filters(graph, target_index):
    """Return positive target filters required by this target's effect gates."""
    filters = []
    for effect in graph.effects:
        try:
            effect_target = int(effect.target_index)
        except (TypeError, ValueError):
            continue
        if effect_target != int(target_index):
            continue
        condition = _condition_for_effect(effect)
        filters.extend(
            node.get("m_Filter") for node in _typed_nodes(
                condition, "RequiresTargetPassesFilterCondition")
            if node.get("m_Filter") and
            _contract_filter_is_template_only(node.get("m_Filter")))
    return tuple(filters)


def _stage_filter_count_witnesses(db, graph, source_uid, owner, state,
                                  filter_specs):
    """Stage card-count operands referenced by target CardFilters.

    C# HasAttackValue can compare a troop's attack to the number of matching
    cards its source controller has in the warzone. The base board deliberately
    has inert filler, so typed filters such as "attack less than the number of
    Scarless Raiders you control" otherwise have no legal target. Seed a small
    authored witness pool for that comparison and let the production filter
    evaluator count it.
    """
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import (filter_matches_template,
                                    records_filter_from_metadata)

    from rules_port.targeting import _source_card

    desired_count = 16
    source = _source_card(db, SESSION_ID, int(source_uid), int(owner))
    for filter_ordinal, filter_spec in enumerate(filter_specs):
        for node in _filter_nodes(filter_spec, "HasAttackValue"):
            card_filter = node.get(
                "m_CompareToSourceControlledCardFilterCount")
            if not card_filter:
                continue
            try:
                compiled = records_filter_from_metadata(card_filter)
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            template_rows = db.execute(
                "SELECT guid FROM card_templates WHERE "
                "lower(card_type) LIKE '%troop%' ORDER BY guid"
            ).fetchall()
            matching_guids = []
            for (template_guid,) in template_rows:
                template = DEFAULT_RECORD_STORE.get(
                    "CardTemplate", str(template_guid).lower())
                if template is not None and filter_matches_template(
                        compiled, template, source=source):
                    matching_guids.append(str(template_guid).lower())
            if not matching_guids:
                continue
            marks = ",".join("?" for _ in matching_guids)
            existing = int(db.execute(
                "SELECT COUNT(*) FROM game_cards WHERE session_id=? "
                "AND user_id=? AND lower(location)='warzone' "
                "AND lower(template_guid) IN (" + marks + ")",
                [SESSION_ID, int(owner), *matching_guids]).fetchone()[0] or 0)
            template_guid = matching_guids[0]
            for ordinal in range(existing, desired_count):
                uid = _raw_uid(
                    CARD_UID_BASE + 12000 +
                    filter_ordinal * desired_count + ordinal)
                if db.execute(
                        "SELECT 1 FROM game_cards WHERE session_id=? "
                        "AND card_uid=?", (SESSION_ID, uid)).fetchone():
                    continue
                _insert_card(
                    db, uid, int(owner), "warzone", template_guid,
                    12000 + filter_ordinal * desired_count + ordinal,
                    abilities=[])
                _stage_card_states_for_filters(
                    db, uid, [card_filter])
            db.commit()


def _condition_filter_matches_template(db, filter_spec, target_uid):
    """Evaluate template-only parts of an effect's target condition."""
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import (filter_matches_template,
                                    records_filter_from_metadata)

    row = db.execute(
        "SELECT template_guid FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (SESSION_ID, int(target_uid))).fetchone()
    template = (DEFAULT_RECORD_STORE.get("CardTemplate", row[0])
                if row else None)
    if template is None:
        return False
    try:
        compiled = records_filter_from_metadata(filter_spec)
        return bool(filter_matches_template(compiled, template))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _stage_stored_name_target_filters(db, graph, source_uid, owner, state,
                                     handler):
    """Seed the remembered name used by authored HasName target filters."""
    from rules_port.targeting import legal_targets_for

    state.setdefault("stored_names", {})
    cards = state.setdefault("cards", {})
    source_key = str(int(source_uid))
    source_data = cards.setdefault(source_key, {})
    if not isinstance(source_data, dict):
        source_data = {}
        cards[source_key] = source_data
    permanent = source_data.setdefault("PermanentData", {})
    for index in sorted({int(effect.target_index) for effect in graph.effects
                         if int(effect.target_index) >= 0}):
        if index >= len(graph.targets):
            continue
        target = graph.targets[index]
        uses_stored_name = any(bool(node.get("m_UseStoredName"))
                               for node in _filter_nodes(
                                   target.card_filter, "HasName"))
        if not uses_stored_name:
            continue
        zones = tuple(str(zone).lower() for zone in _target_zones(target))
        if not zones:
            continue
        placeholders = ",".join("?" for _ in zones)
        rows = db.execute(
            "SELECT gc.card_uid,ct.name FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid=gc.template_guid "
            "WHERE gc.session_id=? AND lower(gc.location) IN (" +
            placeholders + ") ORDER BY gc.position,gc.card_uid",
            (SESSION_ID, *zones)).fetchall()
        for target_uid, name in rows:
            if not name:
                continue
            remembered = str(name)
            state["stored_names"][source_key] = [remembered]
            state["stored_names"][str(graph.guid).lower()] = [remembered]
            permanent["StoredName"] = remembered
            candidates = legal_targets_for(
                db, SESSION_ID, owner, target, source_uid,
                both_players=True, champions=handler._champion_targets(),
                battle_state=state)
            if int(target_uid) in candidates:
                break


def _stage_stored_card_target_filters(db, graph, source_uid, owner, state,
                                     handler):
    """Seed a Records-legal stored target for authored stored-card filters."""
    from rules_port.targeting import legal_targets_for

    source_key = str(int(source_uid))
    stored_by_card = state.setdefault("stored_targets_by_card", {})
    previous = stored_by_card.get(source_key)
    staged = list(previous or ())
    for index in sorted({int(effect.target_index) for effect in graph.effects
                         if int(effect.target_index) >= 0}):
        if index >= len(graph.targets):
            continue
        target = graph.targets[index]
        if not list(_filter_nodes(target.card_filter,
                                  "IsStoredCardFilter")):
            continue
        zones = tuple(str(zone).lower() for zone in _target_zones(target))
        if not zones:
            continue
        placeholders = ",".join("?" for _ in zones)
        candidate_uids = [int(row[0]) for row in db.execute(
            "SELECT card_uid FROM game_cards WHERE session_id=? "
            "AND lower(location) IN (" + placeholders + ") "
            "ORDER BY position,card_uid",
            (SESSION_ID, *zones)).fetchall()]
        candidate_uids.extend(
            int(row[0]) for row in handler._champion_targets()
            if len(row) > 0)
        for candidate_uid in candidate_uids:
            stored_by_card[source_key] = list(dict.fromkeys(
                [*staged, candidate_uid]))
            legal = legal_targets_for(
                db, SESSION_ID, owner, target, source_uid,
                both_players=True, champions=handler._champion_targets(),
                battle_state=state)
            if candidate_uid in legal:
                if candidate_uid not in staged:
                    staged.append(candidate_uid)
                break
    if staged:
        stored_by_card[source_key] = list(dict.fromkeys(staged))
    elif previous is None:
        stored_by_card.pop(source_key, None)
    else:
        stored_by_card[source_key] = previous


def _target_map(db, graph, source_uid, owner, state, handler):
    """Choose legal explicit targets from the authored target templates."""
    result = {}
    champions = handler._champion_targets()
    indexes = sorted({int(effect.target_index) for effect in graph.effects
                      if int(effect.target_index) >= 0})
    for index in indexes:
        if index >= len(graph.targets):
            continue
        target = graph.targets[index]
        if not target.requires_input:
            continue
        if target.target_kind == "SecondaryTargetTemplate":
            # A SecondaryTarget is an input choice from an earlier effect's
            # resolved target instance.  It is not an ordinary board target,
            # so enumerate the referenced target's authored candidate set
            # here and let the generated client choose within this template's
            # min/max.  Without this choice, the resolver's fallback treated
            # every revealed card as selected (Academy of Cerulea then moved
            # both cards to the top and had no remaining card for its second
            # effect).
            linked_effect = next((effect for effect in graph.effects
                                  if int(effect.target_index) == index and
                                  int(effect.secondary_target_index) >= 0),
                                 None)
            if linked_effect is None:
                continue
            referenced_effect = next((effect for effect in graph.effects
                                      if int(effect.effect_instance_id) ==
                                      int(linked_effect.secondary_target_index)),
                                     None)
            if (referenced_effect is None or
                    int(referenced_effect.target_index) < 0 or
                    int(referenced_effect.target_index) >= len(graph.targets)):
                continue
            referenced_index = int(referenced_effect.target_index)
            referenced_target = graph.targets[referenced_index]
            candidates = result.get(referenced_index)
            if not candidates:
                if referenced_target.target_kind == \
                        "SourceRevealedTargetTemplate":
                    from rules_port.targeting import revealed_target_uids
                    candidates = revealed_target_uids(
                        db, SESSION_ID, owner, source_uid,
                        referenced_target.guid,
                        state.get("revealed_cards") or [],
                        battle_state=state)
                else:
                    candidates = legal_targets_for(
                        db, SESSION_ID, owner, referenced_target, source_uid,
                        both_players=True, champions=champions,
                        battle_state=state)
            from rules_port.targeting import filter_resolved_targets
            candidates = filter_resolved_targets(
                db, SESSION_ID, owner, target.guid, source_uid,
                candidates, battle_state=state)
            condition_filters = _contract_target_condition_filters(
                graph, index)
            if candidates and condition_filters:
                constrained = [
                    uid for uid in candidates
                    if all(_condition_filter_matches_template(
                        db, filter_spec, uid)
                           for filter_spec in condition_filters)]
                if constrained:
                    candidates = constrained
            if not candidates:
                continue
            maximum = int(target.resolved_maximum() or 0)
            selected = list(candidates if maximum <= 0 else
                            candidates[:maximum])
            minimum = int(target.resolved_minimum() or 0)
            if len(selected) >= minimum:
                result[index] = tuple(int(uid) for uid in selected)
            continue
        if target.target_kind in _SPECIAL_TARGET_KINDS:
            continue
        candidates = legal_targets_for(
            db, SESSION_ID, owner, target, source_uid,
            both_players=True, champions=champions, battle_state=state)
        condition_filters = _contract_target_condition_filters(graph, index)
        if candidates and condition_filters:
            constrained = [
                uid for uid in candidates
                if all(_condition_filter_matches_template(
                    db, filter_spec, uid) for filter_spec in condition_filters)]
            if constrained:
                candidates = constrained
        if not candidates:
            # Optional/zero-minimum targets are allowed to remain unbound; the
            # native resolver will apply the authored best-effort semantics.
            if target.optional or int(target.minimum or 0) == 0:
                continue
            continue
        maximum = int(target.resolved_maximum() or 0)
        selected = list(candidates if maximum <= 0 else candidates[:maximum])
        if selected:
            result[index] = tuple(selected)
    return result


def _contract_child_target_map(db, graph, source_uid, owner, state, handler):
    """Simulate one legal client choice for an invoked child ability.

    Child abilities are entered while their parent is already resolving, so
    the ordinary fixture's pre-activation target map does not cover them.
    Generate witnesses from the child's own Records target templates, then
    select the authored minimum (or one legal card for an up-to target).
    """
    _stage_stored_name_target_filters(
        db, graph, source_uid, owner, state, handler)
    _stage_stored_card_target_filters(
        db, graph, source_uid, owner, state, handler)
    _seed_target_filter_candidates(
        db, graph, source_uid, owner, state, handler)
    selected = _target_map(db, graph, source_uid, owner, state, handler)
    from rules_port.targeting import revealed_target_uids
    for index, target in enumerate(graph.targets):
        if (not target.requires_input or
                target.target_kind != "SourceRevealedTargetTemplate" or
                index in selected):
            continue
        candidates = revealed_target_uids(
            db, SESSION_ID, owner, source_uid, target.guid,
            state.get("revealed_cards") or [], battle_state=state)
        if not candidates:
            continue
        count = max(1, int(target.resolved_minimum() or 0))
        maximum = int(target.resolved_maximum() or 0)
        if maximum > 0:
            count = min(count, maximum)
        selected[index] = tuple(int(uid) for uid in candidates[:count])
    for index, values in tuple(selected.items()):
        if index < 0 or index >= len(graph.targets):
            continue
        target = graph.targets[index]
        count = max(1, int(target.resolved_minimum() or 0))
        maximum = int(target.resolved_maximum() or 0)
        if maximum > 0:
            count = min(count, maximum)
        selected[index] = tuple(values[:count])
    return selected


def _stage_child_revealed_target_witnesses(db, graph, state):
    """Put a Records-valid card in a parent reveal scope before resolution.

    A SourceRevealedTargetTemplate can only select cards the parent ability
    actually revealed. The generic board's opponent hand may have no card of
    the child's authored type, so stage one legal template in the parent's
    MatchSecondary collection before the reveal effect runs.
    """
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import (filter_matches_template,
                                    records_filter_from_metadata)

    pending = [graph]
    visited = set()
    next_uid = CARD_UID_BASE + 45
    while pending:
        parent = pending.pop()
        parent_guid = str(parent.guid).lower()
        if parent_guid in visited:
            continue
        visited.add(parent_guid)
        reveal_scopes = [target for target in parent.targets
                         if target.target_kind ==
                         "MatchSecondaryTargetTemplate"]
        for effect in parent.effects:
            child_guid = _invoked_ability_guid(effect)
            if not child_guid or child_guid in visited:
                continue
            child = ability_graph(DEFAULT_RECORD_STORE, child_guid)
            if child is None:
                continue
            pending.append(child)
            revealed_targets = [
                target for target in child.targets
                if target.target_kind == "SourceRevealedTargetTemplate" and
                target.requires_input]
            for target in revealed_targets:
                try:
                    compiled = records_filter_from_metadata(
                        target.card_filter)
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
                card_types = set(_card_count_candidate_types(
                    target.card_filter))
                for node in _filter_nodes(target.card_filter, "IsType"):
                    card_types.update(
                        value.strip().rsplit(".", 1)[-1]
                        for value in str(node.get("m_CardType") or "")
                        .split("|") if value.strip())
                template_rows = []
                for card_type in sorted(card_types):
                    template_rows.extend(db.execute(
                        "SELECT guid FROM card_templates WHERE "
                        "lower(card_type) LIKE ? ORDER BY guid LIMIT 512",
                        (f"%{card_type.lower()}%",)).fetchall())
                if not template_rows:
                    template_rows = db.execute(
                        "SELECT guid FROM card_templates ORDER BY guid "
                        "LIMIT 2048").fetchall()
                template_guid = None
                seen = set()
                for (candidate_guid,) in template_rows:
                    candidate_guid = str(candidate_guid).lower()
                    if candidate_guid in seen:
                        continue
                    seen.add(candidate_guid)
                    candidate = DEFAULT_RECORD_STORE.get(
                        "CardTemplate", candidate_guid)
                    if candidate is None:
                        continue
                    try:
                        if filter_matches_template(compiled, candidate):
                            template_guid = candidate_guid
                            break
                    except (AttributeError, KeyError, TypeError, ValueError):
                        continue
                if not template_guid:
                    continue
                for scope in reveal_scopes:
                    zones = _target_zones(scope)
                    if not zones:
                        continue
                    for owner in (AI_OWNER, PLAYER_OWNER):
                        staged_uid = None
                        for zone in zones:
                            rows = db.execute(
                                "SELECT card_uid,template_guid FROM game_cards "
                                "WHERE session_id=? AND user_id=? AND "
                                "lower(location)=? ORDER BY position,card_uid",
                                (SESSION_ID, owner, zone)).fetchall()
                            for uid, existing_guid in rows:
                                existing_template = DEFAULT_RECORD_STORE.get(
                                    "CardTemplate", existing_guid)
                                if existing_template is None:
                                    continue
                                try:
                                    matches = filter_matches_template(
                                        compiled, existing_template)
                                except (AttributeError, KeyError, TypeError,
                                        ValueError):
                                    matches = False
                                if matches:
                                    staged_uid = int(uid)
                                    break
                            if staged_uid is not None:
                                break
                        if staged_uid is None:
                            zone = zones[0]
                            while db.execute(
                                    "SELECT 1 FROM game_cards WHERE "
                                    "session_id=? AND card_uid=?",
                                    (SESSION_ID, _raw_uid(next_uid)))\
                                    .fetchone():
                                next_uid += 1
                            staged_uid = _raw_uid(next_uid)
                            next_uid += 1
                            _insert_card(
                                db, staged_uid, owner, zone, template_guid,
                                -100)
                        # MatchSecondary preserves its C# collection order.
                        # Make the legal witness the first candidate when the
                        # parent authored a single-card reveal scope.
                        db.execute(
                            "UPDATE game_cards SET position=-100 WHERE "
                            "session_id=? AND card_uid=?",
                            (SESSION_ID, staged_uid))
                db.commit()


def _resolve_contract_child_choice(context, child_guid, source_uid, owner_id,
                                   target_map, *, original_resolver):
    """Resolve a nested graph with legal target choices from the fixture."""
    if not context.bstate.get("_card_matrix_contract_fixture"):
        return original_resolver(
            context, child_guid, source_uid, owner_id, target_map)
    child_guid = str(child_guid or "").lower()
    child = ability_graph(DEFAULT_RECORD_STORE, child_guid)
    if child is None:
        return original_resolver(
            context, child_guid, source_uid, owner_id, target_map)
    context.inherit_child_lists(child_guid)
    selected = _contract_child_target_map(
        context.db, child, source_uid, int(owner_id or 0),
        context.bstate, context.handler)
    for index, value in (target_map or {}).items():
        try:
            selected[int(index)] = value
        except (TypeError, ValueError):
            continue
    previous = context.bstate.get("_choice_parent")
    context.bstate["_choice_parent"] = context.continuation()
    try:
        return resolve_port_ability(
            context.handler, context.game, context.session, context.db,
            context.player_uid, context.ai_uid, context.bstate, child_guid,
            source_uid, int(owner_id or 0), target_map=selected,
            variables=context.bstate.get("ability_variables") or {})
    finally:
        if previous is None:
            context.bstate.pop("_choice_parent", None)
        else:
            context.bstate["_choice_parent"] = previous


def _additional_cost_target_specs(graph):
    """Return typed authored target templates for additional ability costs."""
    specs = {}
    for index, (_kind, target_guid) in enumerate(
            graph.additional_cost_targets or ()):
        record = DEFAULT_RECORD_STORE.get(
            "AbilityTargetTemplate", str(target_guid).lower())
        if record is not None:
            specs[len(graph.targets) + index] = record.target_spec
    return specs


def _contract_x_cost(graph, metadata):
    """Choose a small positive X that can exercise follow-up target sets."""
    if graph.costs.variable_activation:
        return max(2, int(graph.costs.variable_minimum or 0))
    if metadata.get("variable"):
        return max(2, int(metadata.get("variable_minimum") or 0))
    return None


def _activation_map(db, graphs, source_uid, owner, state, handler, metadata):
    activations = {}
    for graph in graphs:
        variables = {}
        for variable in graph.variables:
            name = str(variable.field("m_Name", "") or "")
            if not name:
                continue
            requires = bool(variable.field("m_RequiresPlayerInput", 0)) or bool(
                variable.field("m_RequiresExplicitSet", 0))
            if requires:
                variables[name] = variable.field(
                    "m_DefaultValue", variable.field("m_Value", 0))
        options = {index: 0 for index, _option in enumerate(graph.options)}
        x_cost = _contract_x_cost(graph, metadata)
        previous_x_cost = state.get("x_cost")
        had_x_cost = "x_cost" in state
        # Target filters and additional-cost counts can read EffectInputVariable
        # values backed by AbilityResourceXCost, so stage the generated X
        # before asking the native target evaluator for candidates.
        state["x_cost"] = int(x_cost or 0)
        cost_target_map = {}
        try:
            for cost_index, (_kind, target_guid) in enumerate(
                    graph.additional_cost_targets or ()):
                target_record = DEFAULT_RECORD_STORE.get(
                    "AbilityTargetTemplate", str(target_guid).lower())
                if target_record is None:
                    continue
                target = target_record.target_spec
                required = max(0, int(target.minimum or 0))
                if required <= 0:
                    continue
                candidates = legal_targets_for(
                    db, SESSION_ID, owner, target, source_uid,
                    both_players=True,
                    champions=handler._champion_targets(),
                    battle_state=state)
                maximum = int(target.maximum or 0)
                count = max(1, required)
                if maximum > 0:
                    count = min(count, maximum)
                if len(candidates) >= count:
                    cost_target_map[cost_index] = tuple(candidates[:count])
            target_map = _target_map(
                db, graph, source_uid, owner, state, handler)
        finally:
            if had_x_cost:
                state["x_cost"] = previous_x_cost
            else:
                state.pop("x_cost", None)
        activations[graph.guid.lower()] = ActivationData.from_values(
            target_map=target_map, option_map=options, variables=variables,
            cost_target_map=cost_target_map, x_cost=x_cost)
    return activations


def _stage_contract_cost_payment(db, graph, source_uid, activation, state):
    """Apply selected Records additional costs before resolving a graph."""
    selected_costs = activation.cost_target_map or {}
    if not selected_costs:
        return
    ability_lists = state.setdefault("ability_lists", {})
    for index, (kind, _target_guid) in enumerate(
            graph.additional_cost_targets or ()):
        values = selected_costs.get(index, selected_costs.get(str(index), ()))
        if not isinstance(values, (tuple, list, set)):
            values = (values,) if values is not None else ()
        cost_kind = str(kind or "").lower()
        for value in values:
            try:
                uid = int(value)
            except (TypeError, ValueError):
                continue
            if cost_kind == "exhaust":
                db.execute(
                    "UPDATE game_cards SET card_state="
                    "(COALESCE(card_state,0) | ?) WHERE session_id=? "
                    "AND card_uid=?",
                    (int(game_engine.ECardStates.Tapped), SESSION_ID, uid))
                ability_lists.setdefault("ExhaustedCards", []).append(uid)
            elif cost_kind in {"sacrifice", "discard"}:
                db.execute(
                    "UPDATE game_cards SET location='discard' "
                    "WHERE session_id=? AND card_uid=?",
                    (SESSION_ID, uid))
                # C# tracks additional sacrifice and discard costs in their
                # own ListAttrs. SourceBuriedTargetTemplate reads the authored
                # m_ListAttrName (Professor Sporzin uses SacrificedCards), so
                # folding both costs into BuriedCards makes follow-up effects
                # incorrectly resolve with no target.
                list_name = ("SacrificedCards" if cost_kind == "sacrifice"
                             else "DiscardedCards")
                ability_lists.setdefault(list_name, []).append(uid)
            elif cost_kind == "void":
                db.execute(
                    "UPDATE game_cards SET location='void' "
                    "WHERE session_id=? AND card_uid=?",
                    (SESSION_ID, uid))
                ability_lists.setdefault("VoidedCards", []).append(uid)
                state.setdefault("voided_by", {}).setdefault(
                    str(int(source_uid)), []).append(uid)
            elif cost_kind in {"put_into_deck", "shuffle_into_deck"}:
                db.execute(
                    "UPDATE game_cards SET location='deck' "
                    "WHERE session_id=? AND card_uid=?",
                    (SESSION_ID, uid))
            elif cost_kind == "put_into_hand":
                db.execute(
                    "UPDATE game_cards SET location='hand' "
                    "WHERE session_id=? AND card_uid=?",
                    (SESSION_ID, uid))
            elif cost_kind == "reveal":
                state.setdefault("revealed_cards", []).append(uid)
    db.commit()


def _seed_contract_counter_costs(db, graph, source_uid):
    """Give generated fixtures the minimum witness for authored counter costs."""
    counter_costs = tuple(getattr(graph.costs, "counter_costs", ()) or ())
    if not counter_costs:
        return
    from pvp_db import db_card_mutation_field, db_counter_template_name
    from pvp_db import db_set_card_mutation_field
    raw = db_card_mutation_field(
        SESSION_ID, source_uid, "permanent_buffs", conn=db)
    try:
        permanent = json.loads(raw or "{}") if raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        permanent = {}
    if not isinstance(permanent, dict):
        permanent = {}
    counters = permanent.setdefault("counters", {})
    counter_guids = permanent.setdefault("counter_guids", {})
    if not isinstance(counters, dict):
        counters = {}
        permanent["counters"] = counters
    if not isinstance(counter_guids, dict):
        counter_guids = {}
        permanent["counter_guids"] = counter_guids
    for cost in counter_costs:
        guid = str(cost.counter_guid).lower()
        name = db_counter_template_name(guid, conn=db)
        if not name:
            continue
        key = next((value for value in counters
                    if str(value).lower() == str(name).lower() or
                    str(counter_guids.get(value, "")).lower() == guid),
                   name)
        required = (2 if int(cost.amount) == -10000 else
                    max(0, int(cost.amount)))
        counters[key] = max(int(counters.get(key, 0) or 0), required)
        counter_guids[key] = guid
    db_set_card_mutation_field(
        SESSION_ID, source_uid, "permanent_buffs", json.dumps(permanent),
        conn=db)
    db.commit()


def _pay_contract_counter_costs(db, graph, source_uid, state):
    """Use the shared authored counter-cost plan for a generated activation."""
    if not getattr(graph.costs, "counter_costs", ()):
        return ()
    from rules_port.costs import (
        apply_source_counter_cost_plan,
        plan_source_counter_costs,
    )
    plan = plan_source_counter_costs(
        db, SESSION_ID, source_uid, graph.costs, state)
    if plan is None:
        raise AssertionError(
            f"ability {graph.guid}: generated counter-cost witness is "
            "not affordable")
    instance_id = int(state.get("resolving_ability_instance_id", 1) or 1)
    return apply_source_counter_cost_plan(
        db, SESSION_ID, source_uid, plan, state,
        ability_instance_id=instance_id)


def _drain_triggers(handler, game, session, db, state):
    while state.get("stack"):
        item = state["stack"].pop()
        kind = str(item.get("kind") or "")
        if kind == "trigger":
            resolve_port_trigger(handler, game, session, db, PLAYER_UID, AI_UID,
                                 state, item)
            continue

        uid = int(item.get("source_uid") or 0)
        owner = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (SESSION_ID, uid)).fetchone()
        owner = int(owner[0]) if owner else PLAYER_OWNER
        if kind == "troop":
            # This is the same CastSpells -> Warzone boundary used by the
            # host when a token or free-play effect creates a permanent.
            db.execute(
                "UPDATE game_cards SET location='warzone' WHERE session_id=? "
                "AND card_uid=?", (SESSION_ID, uid))
            db.commit()
            _announce_card(game, handler, db, uid, owner, "warzone")
            event = TriggerEvent(
                "CardEnteredZoneEvent", uid, owner, None, None,
                {"event_source_collection": "CastSpells",
                 "event_destination_collection": "warzone",
                 "zones": ("warzone", "underground")})
            trigger_module.NativeTriggerBackend()(
                db=db, handler=handler, game=game, session=session,
                player_uid=PLAYER_UID, ai_uid=AI_UID, battle_state=state,
                event=event)
            continue
        if kind == "spell":
            ability_guids = [str(value).lower() for value in
                             (item.get("ability_guids") or ())]
            graphs = _card_graphs(ability_guids)
            state["resolving_source_uid"] = uid
            state["resolving_owner_id"] = owner
            activations = {
                graph.guid.lower(): ActivationData.from_values(
                    target_map=_target_map(
                        db, graph, uid, owner, state, handler),
                    x_cost=int(item.get("x_cost") or 0))
                for graph in graphs
            }
            resolve_port_played_spell(
                game, session, db, handler, PLAYER_UID, AI_UID, state,
                ability_guids, activations=activations,
                played_from_hand=bool(item.get("played_from_hand")))
            if not state.get("resolution_paused"):
                db.execute(
                    "UPDATE game_cards SET location='discard' WHERE "
                    "session_id=? AND card_uid=?", (SESSION_ID, uid))
                db.commit()
                _announce_card(game, handler, db, uid, owner, "discard")
            continue
        if kind == "ability":
            state["resolving_source_uid"] = uid
            state["resolving_owner_id"] = owner
            resolve_port_ability(
                handler, game, session, db, PLAYER_UID, AI_UID, state,
                str(item.get("ability_guid") or "").lower(), uid, owner,
                target_map={})
            continue
        raise AssertionError(f"unexpected fixture stack item: {item}")


def _trigger_zone(flags):
    values = {str(value).strip().lower() for value in str(flags or "").split("|")}
    for wanted in ("warzone", "hand", "deck", "crypt", "discard", "underground"):
        if wanted in values:
            return "discard" if wanted in {"crypt", "discard"} else wanted
    return "warzone"


def _record_values(value):
    if hasattr(value, "fields"):
        return value.fields.values()
    if isinstance(value, dict):
        return value.values()
    if isinstance(value, (list, tuple)):
        return value
    return ()


def _gain_threshold_color(graph):
    """Choose the shard required by a typed GainThreshold TAC condition."""
    from abilities.framework.tac import _tac_attr_hash, decode_tac_tree

    def walk(value):
        if hasattr(value, "short_type"):
            if value.short_type == "TACTriggerCondition":
                serialized = value.field("m_Conditions", {})
                data = _field(serialized, "data", "")
                decoded = decode_tac_tree(data)
                names = {
                    _tac_attr_hash(name.title()): flag
                    for name, flag in game_engine.SHARD_TO_FLAG.items()
                }

                def find(mapping):
                    if isinstance(mapping, dict):
                        for key, item in mapping.items():
                            if key in names and isinstance(item, (int, float)):
                                return names[key]
                            found = find(item)
                            if found is not None:
                                return found
                    elif isinstance(mapping, (list, tuple)):
                        for item in mapping:
                            found = find(item)
                            if found is not None:
                                return found
                    return None

                found = find(decoded)
                if found is not None:
                    return found
            for item in _record_values(value):
                found = walk(item)
                if found is not None:
                    return found
        elif isinstance(value, (list, tuple, dict)):
            for item in _record_values(value):
                found = walk(item)
                if found is not None:
                    return found
        return None

    # Wild is a valid typed fallback when the authored TAC condition is opaque.
    return int(walk(graph.trigger_condition) or game_engine.SHARD_TO_FLAG["wild"])


def _trigger_ability_source_side(graph):
    """Find whether typed trigger conditions bind this card to source/target."""
    for kind in ("TriggerCardIsAbilitySource",
                 "TriggerCardMatchesFilter"):
        for node in _typed_nodes(graph.trigger_condition, kind):
            side = str(node.get("m_TriggerTest") or "").rsplit(".", 1)[-1]
            if kind == "TriggerCardIsAbilitySource":
                if side.lower() in {"triggersource", "triggertarget"}:
                    return side.lower().replace("trigger", "")
                continue
            if not list(_filter_nodes(node.get("m_CardFilter"),
                                      "IsAbilitySource")):
                continue
            if side.lower() in {"triggersource", "triggertarget"}:
                return side.lower().replace("trigger", "")
    # No authored condition binds the ability's source card to either event
    # participant.  In that case, both event cards must be generated from the
    # trigger's own filters; assuming the ability source is TriggerTarget
    # silently built the wrong CardBlockedEvent for abilities such as
    # "when a troop you control blocks" whose source card remains in a deck.
    return None


def _stage_trigger_target(db, graph, metadata, *, location):
    """Stage an event target that matches the authored TriggerTarget filter."""
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import (filter_matches_template,
                                    records_filter_from_metadata)

    target_filter = next((node.get("m_CardFilter")
                          for node in _typed_nodes(
                              graph.trigger_condition,
                              "TriggerCardMatchesFilter")
                          if str(node.get("m_TriggerTest") or "")
                          .rsplit(".", 1)[-1].lower() == "triggertarget"),
                         None)
    if not target_filter:
        return
    try:
        compiled_filter = records_filter_from_metadata(target_filter)
    except (AttributeError, KeyError, TypeError, ValueError):
        return
    for (template_guid,) in db.execute(
            "SELECT guid FROM card_templates ORDER BY guid"):
        template = DEFAULT_RECORD_STORE.get("CardTemplate", template_guid)
        if (template is None or
                not _template_filter_matches_with_runtime_witnesses(
                    target_filter, template)):
            continue
        target_uid = _raw_uid(CARD_UID_BASE + 1800)
        target_owner = int(metadata.get("event_target_owner", PLAYER_OWNER))
        _insert_card(db, target_uid, target_owner, location,
                     str(template_guid), 19)
        _stage_socketed_gem_witness(
            db, target_uid, (target_filter,),
            source_uid=metadata.get("source_uid"))
        db.commit()
        if list(_filter_nodes(target_filter, "IsSocketed")):
            from rules_port.filters import records_filter_matches
            from rules_port.targeting import _source_card
            candidate = _source_card(
                db, SESSION_ID, target_uid, PLAYER_OWNER)
            source_uid = metadata.get("source_uid")
            source = (_source_card(db, SESSION_ID, int(source_uid),
                                   PLAYER_OWNER)
                      if source_uid is not None else None)
            if not records_filter_matches(
                    candidate, compiled_filter, source=source,
                    context={}, player=PLAYER_OWNER):
                db.execute(
                    "DELETE FROM game_cards WHERE session_id=? "
                    "AND card_uid=?", (SESSION_ID, target_uid))
                db.commit()
                continue
        metadata["event_target_uid"] = target_uid
        return


def _event_for_graph(graph, metadata, state):
    event = str(graph.trigger_event_type or "").rsplit(".", 1)[-1]
    source_uid = metadata["source_uid"]
    filler = metadata["filler_player"]
    filler_action = metadata["filler_action"]
    if event == "CardEnteredZoneEvent":
        zone = _trigger_zone(graph.trigger_collection_flags)
        destination = "discard" if zone == "discard" else "warzone"
        source_collection = "warzone" if destination == "discard" else "CastSpells"
        return TriggerEvent(
            event, source_uid, PLAYER_OWNER, None, None,
            {"zones": ("warzone", "discard", "hand", "underground"),
             "event_source_collection": source_collection,
             "event_destination_collection": destination})
    if event == "CardDrawnEvent":
        draw_owner = int(metadata.get("event_target_owner", PLAYER_OWNER))
        target_uid = metadata.get("event_target_uid")
        if target_uid is None:
            target_uid = (metadata["filler_ai"] if draw_owner == AI_OWNER
                          else metadata["filler_player"])
        draw_champion = (AI_CHAMP if draw_owner == AI_OWNER else PLAYER_CHAMP)
        nth_draw = next((int(node.get("m_Nth", 1) or 1)
                         for node in _typed_nodes(
                             graph.trigger_condition,
                             "TriggerCardIsNthCardDrawnThisTurnByThisPlayer")),
                        1)
        return TriggerEvent(event, draw_champion, draw_owner, target_uid,
                            draw_owner, {
                                "zones": ("hand", "deck"),
                                "nth_card_drawn_this_turn_by_this_player":
                                    nth_draw,
                            })
    if event == "CardDealtDamageEvent":
        # Session emits the damage source as the event source and the damaged
        # card/champion as its target. Derive the fixture direction and target
        # kind from typed trigger conditions so both "you are dealt damage"
        # and "this damages an opposing champion" graphs get a valid event.
        target_is_opposing = []

        def visit(value, negated=False):
            if hasattr(value, "to_dict"):
                value = value.to_dict()
            if isinstance(value, dict):
                kind = str(value.get("_t", "")).rsplit(".", 1)[-1]
                next_negated = (not negated if kind == "NotTriggerCondition"
                                else negated)
                if (kind == "TriggerPlayerControlsTarget" and
                        str(value.get("m_TriggerTest") or
                            "TriggerSource").rsplit(".", 1)[-1].lower()
                        == "triggersource"):
                    target_is_opposing.append(next_negated)
                for child in value.values():
                    visit(child, next_negated)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    visit(child, negated)

        visit(graph.trigger_condition)
        target_owner = (AI_OWNER if target_is_opposing and
                        target_is_opposing[0] else PLAYER_OWNER)
        target_is_hero = any(
            str(node.get("m_TriggerTest") or "").rsplit(".", 1)[-1]
            .lower() == "triggertarget" and
            bool(list(_filter_nodes(node.get("m_CardFilter"), "IsHero")))
            for node in _typed_nodes(
                graph.trigger_condition, "TriggerCardMatchesFilter"))
        if target_is_hero:
            target_card = AI_CHAMP if target_owner == AI_OWNER else PLAYER_CHAMP
        else:
            target_card = (metadata["filler_ai"] if target_owner == AI_OWNER
                           else metadata["filler_player"])
        ability_is_damage_source = (
            _trigger_ability_source_side(graph) == "source")
        source_card = (metadata["source_uid"] if ability_is_damage_source
                       else metadata["filler_ai"])
        source_owner = (PLAYER_OWNER if ability_is_damage_source else AI_OWNER)
        return TriggerEvent(event, source_card, source_owner,
                            target_card, target_owner,
                            {"event_tac": {"damage": 3,
                                           "DamageDealt": 3,
                                           "is_combat_damage": 0},
                             "zones": ("warzone", "hand")})
    if event == "CardCastEvent":
        return TriggerEvent(
            event, PLAYER_CHAMP, PLAYER_OWNER,
            metadata.get("event_target_uid", filler_action), PLAYER_OWNER,
            {"zones": ("warzone", "champions")})
    if event == "CardBlockedEvent":
        # Session.EmitBlockerEvents uses the blocker as source and the blocked
        # attacker as target. Bind the ability's own card to whichever event
        # side its typed trigger condition names; unrelated filler troops are
        # only used for the other participant.
        ability_side = _trigger_ability_source_side(graph)
        if ability_side == "source":
            source_owner, target_owner = PLAYER_OWNER, AI_OWNER
        elif ability_side == "target":
            source_owner, target_owner = AI_OWNER, PLAYER_OWNER
        else:
            source_owner, target_owner = _blocked_event_owners(
                graph, int(PLAYER_OWNER))
        source_card = (
            metadata["source_uid"] if ability_side == "source" else
            metadata["filler_ai"] if source_owner == AI_OWNER else
            metadata["filler_player"])
        target_card = (
            metadata["source_uid"] if ability_side == "target" else
            metadata["filler_ai"] if target_owner == AI_OWNER else
            metadata["filler_player"])
        return TriggerEvent(
            event, source_card, source_owner, target_card, target_owner,
            {"zones": ("warzone",)})
    if event == "GainThresholdEvent":
        color = _gain_threshold_color(graph)
        # Match the host resource projections.  They publish the typed shard
        # colour; the native condition layer reconstructs the client's event
        # TAC for authored TACTriggerCondition leaves.
        return TriggerEvent(
            event, PLAYER_CHAMP, PLAYER_OWNER, None, None,
            {"gain_threshold_color": color})
    if event == "CardDiscardedEvent":
        target_uid = int(metadata.get("event_target_uid") or filler)
        owner = int(metadata.get("event_target_owner", AI_OWNER))
        champion = AI_CHAMP if owner == AI_OWNER else PLAYER_CHAMP
        from rules_port.tac import _tac_attr_hash
        event_tac = {
            _tac_attr_hash("DiscardedCards"): [{
                _tac_attr_hash("Id"): target_uid >> 8,
            }],
        }
        return TriggerEvent(
            event, champion, owner, target_uid, owner,
            {"event_source_collection": "hand",
             "event_destination_collection": "discard",
             "event_previous_state": 0,
             "event_previous_owner_id": owner,
             "event_tac": event_tac,
             "zones": ("hand", "discard", "warzone")})
    if event in {"TurnStartedEvent", "TurnEndedEvent", "GameStartedEvent",
                 "CombatEndedEvent"}:
        event_owner = (AI_OWNER if event == "TurnStartedEvent" and
                       _requires_opposing_turn(graph) else PLAYER_OWNER)
        return TriggerEvent(event, None, event_owner, None, None,
                            {"zones": ("warzone", "hand", "deck", "discard")})
    if event == "CardCreatedEvent":
        # CreateNTokensFromResource calls ActivateCardCreationAbilities on
        # the new card itself before it moves from ECardCollections.None.
        return TriggerEvent(event, source_uid, PLAYER_OWNER, None, None,
                            {"zones": ("none", "warzone", "hand")})
    if event in {"CardWouldEnterZoneEvent", "OtherCardCreatedEvent"}:
        return TriggerEvent(event, filler, PLAYER_OWNER, source_uid,
                            PLAYER_OWNER, {"zones": ("warzone", "hand")})
    if event in {"CardExitedZoneEvent", "CardDiscardedEvent",
                 "CardVoidedEvent", "CardDestroyedEvent"}:
        return TriggerEvent(
            event, filler, PLAYER_OWNER, None, None,
            {"event_source_collection": "warzone",
             "event_destination_collection": "discard",
             "zones": ("warzone", "discard")})
    return TriggerEvent(
        event, filler, PLAYER_OWNER, AI_CHAMP, AI_OWNER,
        {"event_tac": {"damage": 3}, "zones": ("warzone", "hand")})


def _requires_opposing_turn(graph):
    """Whether Records negates the source owner's turn-start condition."""
    def walk(value, negated=False):
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        if isinstance(value, dict):
            kind = str(value.get("_t", "")).rsplit(".", 1)[-1]
            if kind == "TriggerPlayerControlsAbilitySource" and negated:
                return True
            nested_negation = not negated if kind == "NotTriggerCondition" else negated
            return any(walk(child, nested_negation)
                       for child in value.values())
        if isinstance(value, (list, tuple)):
            return any(walk(child, negated) for child in value)
        return False

    return walk(graph.trigger_condition)


def _control_condition_result(node, candidate_owner, source_owner, *,
                             trigger_source_owner=None,
                             trigger_target_owner=None):
    """Evaluate only player-control leaves for a synthetic draw event.

    Other trigger leaves are unknown here and therefore do not choose the
    event's player. And/Or/Not structure is retained so an authored
    alternative can select the owner that makes one complete control branch
    true.
    """
    if hasattr(node, "to_dict"):
        node = node.to_dict()
    if isinstance(node, (list, tuple)):
        values = [_control_condition_result(
            child, candidate_owner, source_owner,
            trigger_source_owner=trigger_source_owner,
            trigger_target_owner=trigger_target_owner) for child in node]
        return _combine_control_results(values, is_and=True)
    if not isinstance(node, dict):
        return None
    kind = str(node.get("_t", "")).rsplit(".", 1)[-1]
    if kind == "NotTriggerCondition":
        child = node.get("m_Condition")
        if child is None:
            children = node.get("m_Conditions") or ()
            child = children[0] if children else None
        result = _control_condition_result(
            child, candidate_owner, source_owner,
            trigger_source_owner=trigger_source_owner,
            trigger_target_owner=trigger_target_owner)
        return None if result is None else not result
    if kind in {"AndTriggerCondition", "OrTriggerCondition"}:
        values = [_control_condition_result(
            child, candidate_owner, source_owner,
            trigger_source_owner=trigger_source_owner,
            trigger_target_owner=trigger_target_owner)
                  for child in node.get("m_Conditions", ()) or ()]
        return _combine_control_results(values, is_and=kind.startswith("And"))
    selector = str(node.get("m_TriggerTest") or "TriggerSource") \
        .rsplit(".", 1)[-1].lower()
    controlled_player = None
    if kind == "TriggerPlayerControlsAbilitySource":
        if selector == "triggersource":
            controlled_player = (candidate_owner if trigger_source_owner is None
                                 else trigger_source_owner)
        elif selector == "triggertarget":
            controlled_player = (candidate_owner if trigger_target_owner is None
                                 else trigger_target_owner)
        else:
            return False
    elif kind == "TriggerPlayerControlsTarget":
        # The C# condition uses TargetPlayerId directly; its serialized
        # m_TriggerTest field does not change that selection.
        controlled_player = (candidate_owner if trigger_target_owner is None
                             else trigger_target_owner)
    else:
        return None
    same = int(controlled_player) == int(source_owner)
    return same


def _combine_control_results(values, *, is_and):
    known = [value for value in values if value is not None]
    if is_and:
        if False in known:
            return False
        return True if len(known) == len(values) else None
    if True in known:
        return True
    return False if len(known) == len(values) else None


def _draw_event_owner(graph, source_owner=PLAYER_OWNER):
    """Choose the typed event owner required by authored control conditions."""
    condition = graph.trigger_condition
    player_result = _control_condition_result(
        condition, PLAYER_OWNER, source_owner)
    opposing_result = _control_condition_result(
        condition, AI_OWNER, source_owner)
    if (opposing_result is not False and player_result is False) or (
            opposing_result is True and player_result is not True):
        return AI_OWNER
    return PLAYER_OWNER


def _blocked_event_owners(graph, source_owner=PLAYER_OWNER):
    """Choose blocker/attacker controllers satisfying typed control gates."""
    owners = (int(PLAYER_OWNER), int(AI_OWNER))
    for blocker_owner in owners:
        for attacker_owner in owners:
            if blocker_owner == attacker_owner:
                continue
            matched = _control_condition_result(
                graph.trigger_condition, blocker_owner, source_owner,
                trigger_source_owner=blocker_owner,
                trigger_target_owner=attacker_owner)
            # Non-control trigger leaves are intentionally unknown to this
            # participant chooser. Keep a candidate when no authored control
            # gate rejects it; the native dispatcher evaluates the full tree
            # after the typed event cards and owners have been staged.
            if matched is not False:
                return blocker_owner, attacker_owner
    # Most fixture graphs do not constrain either participant. Preserve the
    # ordinary opponent-blocking relationship in that case.
    return AI_OWNER, PLAYER_OWNER


def _dispatch_graph_event(handler, game, session, db, state, graph, metadata):
    zone = _trigger_zone(graph.trigger_collection_flags)
    db.execute(
        "UPDATE game_cards SET location=? WHERE session_id=? AND card_uid=?",
        (zone, SESSION_ID, metadata["source_uid"]))
    db.commit()
    if str(graph.trigger_event_type or "").rsplit(".", 1)[-1] == \
            "CardDrawnEvent":
        metadata["event_target_owner"] = _draw_event_owner(
            graph, int(PLAYER_OWNER))
    event = _event_for_graph(graph, metadata, state)
    if event.event_type == "CardDealtDamageEvent":
        instance_id = int(state.get("_next_instance_id", 1) or 1)
        state.setdefault("ability_runtime_state", {}).setdefault(
            str(instance_id), {})["DamageDealt"] = int(
                (event.data.get("event_tac") or {}).get("DamageDealt", 3)
                or 3)
    dispatch_native_trigger(
        db=db, handler=handler, game=game, session=session,
        player_uid=PLAYER_UID, ai_uid=AI_UID, battle_state=state,
        event_type=event.event_type,
        source_card_id=event.source_card_id,
        source_player_id=event.source_player_id,
        target_card_id=event.target_card_id,
        target_player_id=event.target_player_id,
        data=event.data)
    _drain_triggers(handler, game, session, db, state)


def _validate_wire_events(game):
    """Catch the undefined SessionCardId class that crashes Mono clients."""
    def visit(value, path="event"):
        if isinstance(value, game_engine.SessionCardId):
            if value.uid.uid_type == 0:
                raise AssertionError(f"undefined SessionCardId at {path}")
            return
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
        elif hasattr(value, "__dict__"):
            for key, child in vars(value).items():
                visit(child, f"{path}.{key}")

    for index, event in enumerate(game.events):
        visit(event, f"event[{index}]")
    return game.make_network_packet(PLAYER_UID)


def _card_changes_to(entry, destination):
    destination = _normalise_zone(destination)
    changes = []
    for change in entry.get("card_changes") or ():
        before = change.get("before") or {}
        after = change.get("after") or {}
        if (after and _normalise_zone(after.get("location")) == destination
                and _normalise_zone(before.get("location")) != destination):
            changes.append(change)
    return changes


def _card_control_changed_to(entry, destination):
    destination = _normalise_zone(destination)
    return any(
        change.get("before") and
        _normalise_zone((change.get("after") or {}).get("location"))
        == destination and
        change["before"].get("user_id") !=
        (change.get("after") or {}).get("user_id")
        for change in entry.get("card_changes") or ())


def _target_is_in_zone(db, entry, destination):
    """Whether an effect's resolved target is already in its destination."""
    if db is None or entry.get("target_uid") is None:
        return False
    try:
        uid = int(entry["target_uid"])
    except (TypeError, ValueError):
        return False
    row = db.execute(
        "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
        (SESSION_ID, uid)).fetchone()
    return bool(row and _normalise_zone(row[0]) ==
                _normalise_zone(destination))


def _target_projected_to_zone(entry, destination):
    """Whether an effect emitted its target in a zone before later triggers.

    One leaf can move a card and synchronously dispatch an enter-zone ability
    that moves it again. The trace's final SQLite snapshot then shows only the
    later zone, while CardUpdated/CardMoved events still preserve the
    intermediate client-visible move performed by this effect.
    """
    try:
        target_uid = int(entry.get("target_uid"))
        from rules_port.runtime_helpers import card_collection_for_location
        expected = int(card_collection_for_location(
            _normalise_zone(destination)))
    except (TypeError, ValueError):
        return False
    if not expected:
        return False
    for event in entry.get("events") or ():
        if str(event.get("type") or "") not in {
                "CardUpdatedSessionEventArgs", "CardMovedSessionEventArgs"}:
            continue
        fields = event.get("fields") or {}
        identity = re.search(
            r"UID\((\d+)\)", str(fields.get("session_card_id") or ""))
        if identity is None or int(identity.group(1)) != target_uid:
            continue
        try:
            collection = int(fields.get("collection") or 0)
        except (TypeError, ValueError):
            continue
        if collection & expected:
            return True
    return False


def _effect_contract_error(effect, trace, *, dynamic_amount=None, db=None,
                           graph=None):
    """Validate postconditions that are unambiguous in typed metadata."""
    if trace.get("error"):
        return str(trace["error"])
    result = str(trace.get("result") or "").strip()
    if not result:
        return "effect returned no result"

    effect_type = str(effect.concrete_type or "")
    template = effect.template
    if (effect_type == "DestroyCardAbilityEffectTemplate" and
            result.lower() == "death: no card"):
        return "destroy target had no persisted card or champion outcome"

    if _effect_tac_function(effect) == "GainTargetsProvidedThresholds":
        target_uid = trace.get("target_uid")
        target_spec = None
        try:
            target_index = int(effect.target_index)
        except (TypeError, ValueError):
            target_index = -1
        if (graph is not None and 0 <= target_index < len(graph.targets)):
            target_spec = graph.targets[target_index]
        required = bool(target_spec and int(target_spec.minimum or 0) > 0
                        and not target_spec.optional)
        if target_uid is None and required:
            return "provided-threshold effect did not resolve its required target"
        from rules_port.resources import card_template_threshold_flags
        from domain.enums import SHARD_TO_FLAG
        provided = (card_template_threshold_flags(
            db, SESSION_ID, int(target_uid))
                    if db is not None and target_uid is not None else 0)
        expected = Counter({int(flag): 1 for flag in SHARD_TO_FLAG.values()
                            if provided & int(flag)})
        actual = Counter()
        for event in trace.get("events") or ():
            if event.get("type") != \
                    "PlayerResourceThresholdChangedSessionEventArgs":
                continue
            fields = event.get("fields") or {}
            try:
                actual[int(fields.get("color"))] += int(fields.get("delta"))
            except (TypeError, ValueError):
                return "threshold event omitted a numeric color or delta"
        if actual != expected:
            return ("provided-threshold effect events did not match its "
                    f"target: expected {dict(expected)}, got {dict(actual)}")
        if expected:
            changes = trace.get("champion_changes") or {}
            scopes = [changes.get(name) for name in
                      ("player_threshold", "ai_threshold")]
            scopes.extend(value for key, value in changes.items()
                          if str(key).startswith("thresh_"))
            before_after = next((value for value in scopes
                                 if isinstance(value, dict) and
                                 isinstance(value.get("before"), dict) and
                                 isinstance(value.get("after"), dict)), None)
            if before_after is None:
                return "provided-threshold effect emitted no threshold state delta"
            before = before_after["before"]
            after = before_after["after"]
            for color, amount in expected.items():
                old = int(before.get(str(color), before.get(color, 0)) or 0)
                new = int(after.get(str(color), after.get(color, 0)) or 0)
                if new != old + amount:
                    return ("provided-threshold state did not increase by the "
                            f"target amount for color {color}: {old} -> {new}")

    if effect_type == "MoveCardToZoneEffectTemplate":
        destination = _normalise_zone(
            _field(template, "m_DestinationCollection", ""))
        if destination in {"hand", "discard", "deck", "void", "warzone",
                           "underground"} and not _card_changes_to(
                               trace, destination):
            # Some authored MoveCardToZone effects change the position inside
            # the destination collection rather than changing collections (for
            # example, Seek Fortune puts a resource on top of its deck).
            repositioned = any(
                change.get("before") and change.get("after") and
                _normalise_zone(change["before"].get("location")) ==
                destination and
                _normalise_zone(change["after"].get("location")) ==
                destination and
                change["before"].get("position") !=
                change["after"].get("position")
                for change in trace.get("card_changes") or ()
            )
            control_given = any(bool(_field(template, name, 0)) for name in (
                "m_AbilityOwnerTakesControl",
                "m_AbilityOpponentTakesControl",
                "m_ArenaChampionTakesControl",
                "m_PreviousControllerTakesControl"))
            try:
                control_given = control_given or int(_field(
                    template, "m_ControlGivenToTargetIndex", -1)) >= 0
            except (TypeError, ValueError):
                pass
            controller_changed = (
                control_given and
                _card_control_changed_to(trace, destination))
            already_in_destination = _target_is_in_zone(
                db, trace, destination)
            projected_to_destination = _target_projected_to_zone(
                trace, destination)
            if (not repositioned and not controller_changed and
                    not already_in_destination and
                    not projected_to_destination):
                return (f"typed MoveCardToZone destination={destination} "
                        "produced no matching persisted zone change")

    if effect_type in {"BuryCardAbilityEffectTemplate",
                       "DiscardCardAbilityEffectTemplate",
                       "DiscardOrSacrificeCardAbilityEffectTemplate"}:
        if not (_card_changes_to(trace, "discard") or
                _card_changes_to(trace, "underground") or
                _target_is_in_zone(db, trace, "discard") or
                _target_is_in_zone(db, trace, "underground") or
                _target_projected_to_zone(trace, "discard") or
                _target_projected_to_zone(trace, "underground")):
            return "discard/bury effect produced no discard-zone change"

    if effect_type == "VoidCardAbilityEffectTemplate":
        # A later mapping can intentionally re-enumerate a card that an
        # earlier mapping just voided.  MatchSecondaryTargetTemplate, for
        # example, may include the previous target when its authored filter
        # spans Void and does not set CantBePreviousTarget.  The effect is
        # already satisfied when that selected card is persisted in Void.
        if (not _card_changes_to(trace, "void") and
                not _target_is_in_zone(db, trace, "void")):
            return "void effect produced no void-zone change"

    if effect_type in {"DrawCardAbilityEffectTemplate",
                       "DrawNCardsAbilityEffectTemplate"}:
        drawn = _card_changes_to(trace, "hand")
        match = re.fullmatch(r"draw (\d+) for owner \d+", result.lower())
        if match:
            expected_draws = int(match.group(1))
            if len(drawn) != expected_draws:
                return ("draw effect persisted " + str(len(drawn)) +
                        f" deck-to-hand changes, expected {expected_draws}")
        elif not drawn:
            return "draw effect produced no deck-to-hand change"

    if effect_type == "PutTopOfDeckIntoHandAbilityEffectTemplate":
        if not _card_changes_to(trace, "hand"):
            return "draw effect produced no deck-to-hand change"

    if effect_type in {"SummonTokenTroopAbilityEffectTemplate",
                       "SummonXTokenTroopsAbilityEffectTemplate"}:
        token_guid = _guid(_field(template, "m_CardTemplateId", ""))
        authored_collection = _normalise_zone(
            _field(template, "m_CardCollection", "warzone"))
        # Records uses None/Unknown when a following MoveCardToZone effect
        # supplies the created token's destination. None is not a game zone.
        collection = (authored_collection if authored_collection not in
                      {"", "none", "unknown"} else None)
        summoned = []
        for change in trace.get("card_changes") or ():
            before = change.get("before")
            after = change.get("after") or {}
            if (before is None and token_guid and
                    str(after.get("template_guid") or "").lower() == token_guid
                    and (collection is None or
                         _normalise_zone(after.get("location")) == collection)):
                summoned.append(change)
        amount_field = _field(template, "m_AmountField")
        if hasattr(amount_field, "to_dict"):
            amount_field = amount_field.to_dict()
        if dynamic_amount is not None:
            amount = max(0, int(dynamic_amount))
        elif (isinstance(amount_field, dict) and
              str(amount_field.get("_t", "")).rsplit(".", 1)[-1] in {
                  "EffectInputVariable", "EffectAbilityVariable",
                  "AbilityVariable", "EffectVariable"}):
            return "dynamic summon amount was not resolved from Records"
        else:
            try:
                amount = int(_field(template, "m_Amount", 1) or 1)
            except (TypeError, ValueError):
                amount = 1
        if token_guid and amount > 0 and len(summoned) < amount:
            return (f"summon effect expected {amount} typed token(s) "
                    f"{token_guid}, observed {len(summoned)}")

    return None


def _assert_champion_destroy_closed(scenario):
    """Require the native DestroyCard champion branch to end its session."""
    destroyed_champion = any(
        str(entry.get("result") or "").lower().startswith(
            "destroyed champion ")
        for entry in scenario["state"].get("ability_trace") or ())
    if (destroyed_champion and
            str(getattr(scenario["session"], "state", "")).lower()
            != "ended"):
        raise AssertionError(
            "DestroyCard on a champion did not end the generated session")


def _filter_nodes(value, wanted_type, *, negated=False):
    """Yield authored Records filter nodes with one concrete type."""
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, dict):
        kind = str(value.get("_t", "")).rsplit(".", 1)[-1]
        if kind == "NotCardFilter":
            negated = not negated
        if kind == wanted_type and not negated:
            yield value
        for child in value.values():
            yield from _filter_nodes(child, wanted_type, negated=negated)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _filter_nodes(child, wanted_type, negated=negated)


_TEMPLATE_WITNESS_CACHE = {}


def _template_filter_has_runtime_nodes(filter_obj):
    """Whether a Records filter contains predicates absent from templates."""
    from rules_port.filters import (AndCardFilter, NotCardFilter,
                                    OrCardFilter, _TEMPLATE_FALSE)

    if isinstance(filter_obj, _TEMPLATE_FALSE):
        return True
    if isinstance(filter_obj, (AndCardFilter, OrCardFilter)):
        return any(_template_filter_has_runtime_nodes(child)
                   for child in filter_obj.filters)
    if isinstance(filter_obj, NotCardFilter):
        return _template_filter_has_runtime_nodes(filter_obj.filter)
    return False


def _candidate_filter_matches_template(filter_obj, template):
    """Match the template-known part of a Records filter for fixture setup.

    Runtime predicates such as zone, controller, state, counters, and deck
    position are staged on generated instances after their template is chosen.
    Ignoring those predicates here keeps candidate selection from falling back
    to an unrelated first row when a mixed authored filter contains them.
    """
    from rules_port.filters import (AndCardFilter, NotCardFilter,
                                    OrCardFilter, _TEMPLATE_FALSE,
                                    filter_matches_template)

    if filter_obj is None or isinstance(filter_obj, _TEMPLATE_FALSE):
        return True
    if isinstance(filter_obj, AndCardFilter):
        return all(_candidate_filter_matches_template(child, template)
                   for child in filter_obj.filters)
    if isinstance(filter_obj, OrCardFilter):
        return any(_candidate_filter_matches_template(child, template)
                   for child in filter_obj.filters)
    if isinstance(filter_obj, NotCardFilter):
        # A runtime child can make the negation true for a card regardless of
        # its template, so retain it as a possible witness and let the runtime
        # filter check the staged instance.
        if _template_filter_has_runtime_nodes(filter_obj.filter):
            return True
        return not _candidate_filter_matches_template(
            filter_obj.filter, template)
    return filter_matches_template(filter_obj, template)


def _filter_witness_template_rows(db, filter_specs, fallback_rows=()):
    """Return Records templates matching every template-known filter clause."""
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import records_filter_from_metadata

    normalized_specs = []
    for spec in filter_specs:
        if hasattr(spec, "to_dict"):
            spec = spec.to_dict()
        if not spec:
            continue
        try:
            compiled = records_filter_from_metadata(spec)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if compiled is not None:
            normalized_specs.append((spec, compiled))
    if not normalized_specs:
        return list(fallback_rows)

    cache_key = json.dumps(
        [spec for spec, _compiled in normalized_specs],
        sort_keys=True, separators=(",", ":"), default=str)
    cached = _TEMPLATE_WITNESS_CACHE.get(cache_key)
    if cached is None:
        templates = DEFAULT_RECORD_STORE.load("CardTemplate")
        matches = [
            str(template.guid) for template in templates
            if template.guid and all(
                _candidate_filter_matches_template(compiled, template)
                for _spec, compiled in normalized_specs)]
        cached = tuple(matches)
        _TEMPLATE_WITNESS_CACHE[cache_key] = cached
    if cached:
        return [(guid,) for guid in cached]
    return list(fallback_rows)


def _card_count_candidate_types(card_filter):
    """Return simple positive card types that can seed a count variable."""
    names = set()
    for kind in ("IsArtifact", "IsConstant", "IsTroop", "IsResource"):
        if list(_filter_nodes(card_filter, kind)):
            names.add(kind[2:])
    for node in _filter_nodes(card_filter, "IsType"):
        names.update(
            value.strip().rsplit(".", 1)[-1]
            for value in str(node.get("m_CardType") or "").split("|")
            if value.strip())
    return tuple(sorted(names))


def _stage_card_states_for_filters(db, card_uid, filter_specs):
    """Set authored combat-state predicates on a generated witness card."""
    from domain.enums import ECardStates

    state_filters = (
        ("IsTapped", ECardStates.Tapped, "m_Tapped"),
        ("IsAttacking", ECardStates.Attacking, None),
        ("IsBlocking", ECardStates.Blocking, None),
        # Turn-history predicates read the same authoritative state bits.
        ("IsDamagedThisTurn", ECardStates.Damaged, None),
        ("IsPlayedThisTurn", ECardStates.CameOutThisTurn, None),
    )
    mask = 0
    value = 0
    for kind, bit, field in state_filters:
        nodes = [node for spec in filter_specs
                 for node in _filter_nodes(spec, kind)]
        if not nodes:
            continue
        mask |= int(bit)
        enabled = any(bool(node.get(
            field, node.get("m_Value", 1)) if field else
                          node.get("m_Value", 1)) for node in nodes)
        if enabled:
            value |= int(bit)
    if mask:
        db.execute(
            "UPDATE game_cards SET card_state=(COALESCE(card_state,0) & ?) | "
            "? WHERE session_id=? AND card_uid=?",
            (~mask, value, SESSION_ID, int(card_uid)))
        db.commit()


def _stage_filter_counter_witnesses(db, card_uid, filter_specs):
    """Give generated cards the counters required by typed target filters."""
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.conditions import _compare

    requirements = {}
    for filter_spec in filter_specs:
        for node in _filter_nodes(filter_spec, "HasCountersValue"):
            counter_guid = _guid(node.get("m_CounterType"))
            if not counter_guid:
                continue
            requirements.setdefault(counter_guid, []).append(node)
    if not requirements:
        return

    row = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (SESSION_ID, int(card_uid))).fetchone()
    if row is None:
        return
    try:
        buffs = json.loads(row[0] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if not isinstance(buffs, dict):
        buffs = {}
    counters = buffs.setdefault("counters", {})
    counter_guids = buffs.setdefault("counter_guids", {})
    changed = False
    for counter_guid, nodes in requirements.items():
        counter = DEFAULT_RECORD_STORE.get(
            "CardCounterTemplate", counter_guid)
        name = str(_field(counter, "m_Name", "") or "").strip().lower()
        key = name or counter_guid
        value = next((candidate for candidate in range(0, 1025)
                      if all(_compare(
                          candidate, node.get("m_ComparisonOp"),
                          int(node.get("m_Amount", 0) or 0))
                             for node in nodes)), None)
        if value is None:
            continue
        counters[key] = int(value)
        counter_guids[key] = counter_guid
        changed = True
    if changed:
        db.execute(
            "UPDATE game_cards SET permanent_buffs=? WHERE session_id=? "
            "AND card_uid=?",
            (json.dumps(buffs), SESSION_ID, int(card_uid)))
        db.commit()


def _stage_filter_relationship_witnesses(db, card_uid, filter_specs, *,
                                         source_uid=None):
    """Persist typed transform and parent relationships for fixture cards."""
    transformed = any(
        list(_filter_nodes(spec, "IsTranformed")) for spec in filter_specs)
    child_of_source = (source_uid is not None and any(
        list(_filter_nodes(spec, "IsChildOfAbilitySource"))
        for spec in filter_specs))
    if transformed:
        row = db.execute(
            "SELECT template_guid,original_template_guid FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (SESSION_ID, int(card_uid))).fetchone()
        if row and (not row[1] or
                    str(row[0] or "").lower()
                    == str(row[1] or "").lower()):
            alternate = db.execute(
                "SELECT guid FROM card_templates WHERE lower(guid)<>? "
                "ORDER BY guid LIMIT 1", (str(row[0] or "").lower(),)
            ).fetchone()
            if alternate:
                db.execute(
                    "UPDATE game_cards SET original_template_guid=? "
                    "WHERE session_id=? AND card_uid=?",
                    (str(alternate[0]), SESSION_ID, int(card_uid)))
    if child_of_source:
        row = db.execute(
            "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
            "AND card_uid=?", (SESSION_ID, int(card_uid))).fetchone()
        try:
            buffs = json.loads(row[0] or "{}") if row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            buffs = {}
        buffs["parent_uid"] = int(source_uid)
        db.execute(
            "UPDATE game_cards SET permanent_buffs=? WHERE session_id=? "
            "AND card_uid=?", (json.dumps(buffs), SESSION_ID, int(card_uid)))
    if transformed or child_of_source:
        db.commit()


def _stage_socketed_gem_witness(db, card_uid, filter_specs, *,
                                source_uid=None):
    """Stage valid ActiveGem bits for authored IsSocketed witnesses."""
    nodes = [node for spec in filter_specs
             for node in _filter_nodes(spec, "IsSocketed")]
    if not nodes:
        return
    source_gems = 0
    if source_uid is not None:
        row = db.execute(
            "SELECT gems FROM game_cards WHERE session_id=? AND card_uid=?",
            (SESSION_ID, int(source_uid))).fetchone()
        source_gems = int(row[0] or 0) if row else 0
        if any(bool(node.get("m_CompareToAbilitySource")) for node in nodes) \
                and source_gems == 0:
            source_gems = 1  # Wild Minor 1, a valid client gem enum value.
            db.execute(
                "UPDATE game_cards SET gems=? WHERE session_id=? "
                "AND card_uid=?", (source_gems, SESSION_ID, int(source_uid)))
    source = {"gems": source_gems}
    from rules_port.filters import records_filter_matches
    selected = None
    for count in range(7):
        # EGemTypesNew stores one valid gem enum value in each ten-bit slot.
        gems = sum(1 << (10 * index) for index in range(count))
        card = {"gems": gems}
        if all(records_filter_matches(card, node, source=source)
               for node in nodes):
            selected = gems
            break
    if selected is not None:
        db.execute(
            "UPDATE game_cards SET gems=? WHERE session_id=? AND card_uid=?",
            (int(selected), SESSION_ID, int(card_uid)))
        db.commit()


def _template_filter_matches_with_runtime_witnesses(spec, template):
    """Check static template predicates, leaving IsSocketed for card state."""
    from rules_port.filters import (filter_matches_template,
                                    records_filter_from_metadata)
    if hasattr(spec, "to_dict"):
        spec = spec.to_dict()
    if not isinstance(spec, dict):
        return True
    kind = str(spec.get("_t", "")).rsplit(".", 1)[-1]
    if kind == "IsSocketed":
        return True
    if kind == "AndCardFilter":
        children = spec.get("m_TargetFilters") or ()
        return all(_template_filter_matches_with_runtime_witnesses(
            child, template) for child in children)
    if kind == "OrCardFilter":
        children = spec.get("m_TargetFilters") or ()
        return any(_template_filter_matches_with_runtime_witnesses(
            child, template) for child in children)
    if kind == "NotCardFilter" and list(_filter_nodes(spec, "IsSocketed")):
        # The template-only view cannot decide a runtime gem predicate under
        # negation; defer it to the completed card candidate below.
        return True
    if list(_filter_nodes(spec, "IsSocketed")):
        # Other composite filter shapes are uncommon. Treat the runtime part
        # as possible here; the full typed filter is checked after staging.
        return True
    try:
        return filter_matches_template(
            records_filter_from_metadata(spec), template)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _ability_variable_condition_goals(db, graph, source_uid, source_owner,
                                     state, raw_text):
    """Resolve authored AbilityVariableCondition thresholds for fixtures."""
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.static_rules import _expression_value

    conditions = [graph.ability_condition]
    for effect in graph.effects:
        condition_guid = str(effect.condition_guid or "").lower()
        if not condition_guid or condition_guid == "0" * 36:
            continue
        record = DEFAULT_RECORD_STORE.get(
            "AbilityEffectConditionTemplate", condition_guid)
        conditions.append(getattr(record, "condition", None) if record else None)

    goals = {}
    for condition in conditions:
        for comparison in _typed_nodes(
                condition, "AbilityVariableCondition"):
            name = str(comparison.get("m_Lhs") or "").strip()
            rhs_name = str(comparison.get("m_Rhs") or "").strip()
            if not name or not rhs_name:
                continue
            try:
                rhs = _expression_value(
                    db, SESSION_ID, state, int(source_uid),
                    int(source_owner), raw_text, rhs_name)
            except (TypeError, ValueError, RuntimeError):
                rhs = None
            if rhs is None:
                try:
                    rhs = int(rhs_name)
                except (TypeError, ValueError):
                    continue
            try:
                current = _expression_value(
                    db, SESSION_ID, state, int(source_uid),
                    int(source_owner), raw_text, name)
                current = int(current or 0)
            except (TypeError, ValueError, RuntimeError):
                current = 0
            rhs = int(rhs)
            operation = str(comparison.get("m_ComparisonOp") or "")
            if operation == "GreaterThanOrEqual":
                goal = max(current, rhs)
            elif operation == "GreaterThan":
                goal = max(current, rhs + 1)
            elif operation == "Equals":
                if current > rhs:
                    continue
                goal = rhs
            elif operation == "NotEqual":
                goal = current if current != rhs else rhs + 1
            elif operation == "LessThan":
                if current >= rhs:
                    continue
                goal = current
            elif operation == "LessThanOrEqual":
                if current > rhs:
                    continue
                goal = current
            elif operation == "OneMoreThan":
                goal = max(current, rhs + 1)
            elif operation == "TwoMoreThan":
                goal = max(current, rhs + 2)
            elif operation == "OneLessThan":
                if current != rhs - 1:
                    continue
                goal = current
            else:
                continue
            goals[name] = max(int(goals.get(name, 0) or 0), int(goal))
    return goals


def _stage_card_count_variable_inputs(db, graph, source_uid, source_owner,
                                      state):
    """Seed typed filter witnesses for resolvable CardCount variables.

    The matrix uses the variable's authored player, collection, and filter
    fields to select a candidate; the ordinary Records variable evaluator
    confirms that the persisted candidate actually satisfies the graph.
    Unsupported/ambiguous filters remain at their authored board value.
    """
    from rules_port.filters import records_filter_matches
    from rules_port.static_rules import _expression_value
    from rules_port.targeting import _shards, _source_card

    raw = graph.source.to_dict()
    raw_text = json.dumps(raw)
    source = _source_card(db, SESSION_ID, int(source_uid), int(source_owner))
    variable_goals = _ability_variable_condition_goals(
        db, graph, source_uid, source_owner, state, raw_text)
    zone_names = {
        "Deck": "deck", "Hand": "hand", "Warzone": "warzone",
        "Discard": "discard", "Crypt": "discard", "Void": "void",
        "CastSpells": "CastSpells", "Underground": "underground",
        "Choosing": "choosing", "PlayedResources": "PlayedResources",
    }
    inserted = 0
    for variable in graph.variables:
        variable = variable.to_dict() if hasattr(variable, "to_dict") else variable
        variable_kind = str(variable.get("_t", "")).rsplit(".", 1)[-1]
        variable_name = str(variable.get("m_Name") or "")
        if variable_kind == "CountListAttrAbilityVariable":
            goal = int(variable_goals.get(variable_name, 0) or 0)
            if goal > 0:
                _stage_count_list_variable(
                    db, graph, variable, goal, source_uid, source_owner,
                    state, raw_text, inserted)
            continue
        if variable_kind not in {
                "CardCountAbilityVariable", "CardSumAbilityVariable"}:
            continue
        card_filter = variable.get("m_CardFilter") or {}
        player_filter = str(variable.get("m_PlayerFilter") or "").rsplit(
            ".", 1)[-1].lower()
        if player_filter == "self":
            owners = (int(source_owner),)
        elif player_filter == "multipleopponents":
            owners = (AI_OWNER if int(source_owner) != AI_OWNER else
                      PLAYER_OWNER,)
        elif player_filter == "multipleplayers":
            other = (AI_OWNER if int(source_owner) != AI_OWNER else
                     PLAYER_OWNER)
            owners = (other, int(source_owner))
        else:
            continue

        flags = str(variable.get("m_CollectionFlags") or "").split("|")
        zones = tuple(dict.fromkeys(
            zone_names.get(str(flag).rsplit(".", 1)[-1].strip())
            for flag in flags
            if zone_names.get(str(flag).rsplit(".", 1)[-1].strip())))
        candidate_types = _card_count_candidate_types(card_filter)
        subtypes = [
            str(node.get("m_SubType", "") or "").strip()
            for node in _filter_nodes(card_filter, "IsSubType")
            if str(node.get("m_SubType", "") or "").strip() not in {"", "*"}]
        name_terms = [
            str(node.get("m_ContainsString", "") or "").strip()
            for node in _filter_nodes(card_filter, "NameContainsFilter")
            if str(node.get("m_ContainsString", "") or "").strip()]
        exact_names = [
            str(node.get("m_CardName", "") or "").strip()
            for node in _filter_nodes(card_filter, "IsCardName")
            if str(node.get("m_CardName", "") or "").strip() and
            not any(bool(node.get(flag)) for flag in (
                "m_CompareToAbilitySource", "m_CompareToTriggerSource",
                "m_CompareToTriggerTarget"))]
        if not zones or not (candidate_types or subtypes or name_terms or
                             exact_names):
            continue
        template_rows = []
        if candidate_types:
            type_predicates = " OR ".join(
                "lower(card_type) LIKE ?" for _ in candidate_types)
            template_rows.extend(db.execute(
                "SELECT guid,name,card_type,subtype,cost,attack,defense,"
                "attributes,rarity,threshold_json FROM card_templates "
                f"WHERE {type_predicates} ORDER BY guid LIMIT 512",
                tuple(f"%{card_type.lower()}%"
                      for card_type in candidate_types)).fetchall())
        for subtype in subtypes:
            template_rows.extend(db.execute(
                "SELECT guid,name,card_type,subtype,cost,attack,defense,"
                "attributes,rarity,threshold_json FROM card_templates "
                "WHERE lower(subtype) LIKE ? ORDER BY guid LIMIT 256",
                (f"%{subtype.lower()}%",)).fetchall())
        for term in name_terms:
            template_rows.extend(db.execute(
                "SELECT guid,name,card_type,subtype,cost,attack,defense,"
                "attributes,rarity,threshold_json FROM card_templates "
                "WHERE lower(name) LIKE ? OR lower(subtype) LIKE ? "
                "ORDER BY guid LIMIT 256",
                (f"%{term.lower()}%", f"%{term.lower()}%")).fetchall())
        for name in exact_names:
            template_rows.extend(db.execute(
                "SELECT guid,name,card_type,subtype,cost,attack,defense,"
                "attributes,rarity,threshold_json FROM card_templates "
                "WHERE lower(name)=? ORDER BY guid LIMIT 64",
                (name.lower(),)).fetchall())
        if not template_rows:
            template_rows = db.execute(
                "SELECT guid,name,card_type,subtype,cost,attack,defense,"
                "attributes,rarity,threshold_json FROM card_templates "
                "ORDER BY guid LIMIT 256").fetchall()
        if not template_rows:
            continue
        template_rows = _filter_witness_template_rows(
            db, [card_filter], template_rows)
        template_guids = list(dict.fromkeys(
            str(row[0]).lower() for row in template_rows if row and row[0]))
        if template_guids:
            placeholders = ",".join("?" for _ in template_guids)
            template_rows = db.execute(
                "SELECT guid,name,card_type,subtype,cost,attack,defense,"
                "attributes,rarity,threshold_json FROM card_templates "
                f"WHERE lower(guid) IN ({placeholders}) ORDER BY guid",
                tuple(template_guids)).fetchall()
        else:
            template_rows = []
        if not template_rows:
            continue

        current = _expression_value(
            db, SESSION_ID, state, int(source_uid), int(source_owner),
            raw_text, variable_name)
        goal = max(1, int(variable_goals.get(variable_name, 1) or 1))
        if current is not None and int(current) >= goal:
            state.setdefault("_card_matrix_witnessed_count_variables", set()).add(
                variable_name)
            continue

        found = False
        for zone in zones:
            for owner in owners:
                for row in template_rows:
                    (guid, name, card_type, subtype, cost, attack, defense,
                     attributes, rarity, threshold_json) = row
                    candidate = {
                        "card_uid": _raw_uid(CARD_UID_BASE + 1000 + inserted),
                        "template_guid": str(guid).lower(), "name": name or "",
                        "card_type": card_type or "", "subtype": subtype or "",
                        "cost": int(cost or 0), "attack": int(attack or 0),
                        "defense": int(defense or 0),
                        "attributes": int(attributes or 0),
                        "rarity": rarity or "", "location": zone,
                        "user_id": int(owner), "owner_id": int(owner),
                        "controller_id": int(owner),
                        "shards": _shards(threshold_json),
                        "int_attrs": {},
                    }
                    # Filter matching runs before the candidate is persisted,
                    # so expose combat-state predicates to that first check
                    # as well as storing the corresponding card_state later.
                    from domain.enums import ECardStates
                    for filter_kind, key, bit, field in (
                            ("IsTapped", "is_tapped", ECardStates.Tapped,
                             "m_Tapped"),
                            ("IsAttacking", "is_attacking",
                             ECardStates.Attacking, None),
                            ("IsBlocking", "is_blocking",
                             ECardStates.Blocking, None)):
                        matches = list(_filter_nodes(
                            card_filter, filter_kind))
                        if matches:
                            enabled = any(bool(node.get(
                                field, node.get("m_Value", 1))
                                if field else node.get("m_Value", 1))
                                          for node in matches)
                            candidate[key] = enabled
                            if enabled:
                                candidate["state"] = (
                                    int(candidate.get("state", 0)) |
                                    int(bit))
                    for node in _filter_nodes(card_filter, "IntAttrFilter"):
                        attr_name = str(node.get("m_Attribute") or "").strip()
                        if (not attr_name or ">" in attr_name or
                                attr_name.lower().startswith(
                                    ("you", "abilitytac"))):
                            continue
                        try:
                            attr_value = int(node.get("m_Value", 0) or 0)
                        except (TypeError, ValueError):
                            attr_value = 0
                        operation = str(
                            node.get("m_ComparisonOp", "") or "")
                        if operation == "GreaterThan":
                            attr_value += 1
                        elif operation == "LessThan":
                            attr_value = max(0, attr_value - 1)
                        elif operation == "NotEqualTo":
                            attr_value += 1
                        candidate["int_attrs"][attr_name] = attr_value
                    if not records_filter_matches(
                            candidate, card_filter, source=source,
                            context=state, player=int(source_owner)):
                        continue
                    uid = int(candidate["card_uid"])
                    _insert_card(db, uid, owner, zone, guid, 200 + inserted)
                    _seed_filter_int_attributes(db, uid, card_filter)
                    _stage_card_states_for_filters(
                        db, uid, [card_filter])
                    _stage_filter_relationship_witnesses(
                        db, uid, [card_filter], source_uid=source_uid)
                    _stage_filter_tac_statistics(
                        state, uid, [card_filter])
                    db.commit()
                    actual = _expression_value(
                        db, SESSION_ID, state, int(source_uid),
                        int(source_owner), raw_text, variable_name)
                    if actual is not None and int(actual) > 0:
                        inserted += 1
                        if int(actual) >= goal:
                            state.setdefault(
                                "_card_matrix_witnessed_count_variables", set()).add(
                                    variable_name)
                            found = True
                            break
                        if inserted >= 64:
                            found = True
                            break
                        continue
                    db.execute(
                        "DELETE FROM game_cards WHERE session_id=? "
                        "AND card_uid=?", (SESSION_ID, uid))
                    db.commit()
                if found:
                    break

    # AbilityVariableCondition evaluates this per-instance map, while the
    # fixture assertions and dynamic effect inputs resolve directly from
    # Records. Keep both views of each staged variable synchronized.
    ability_values = state.setdefault("ability_variables", {})
    for variable in graph.variables:
        variable = variable.to_dict() if hasattr(variable, "to_dict") else variable
        name = str(variable.get("m_Name") or "")
        if not name:
            continue
        value = _expression_value(
            db, SESSION_ID, state, int(source_uid), int(source_owner),
            raw_text, name)
        if value is not None:
            ability_values[name] = int(value)


def _stage_count_list_variable(db, graph, variable, goal, source_uid,
                               source_owner, state, raw_text, uid_offset):
    """Stage typed TAC-list members for authored list-count variables."""
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.static_rules import _expression_value
    from rules_port.targeting import _source_card

    variable_name = str(variable.get("m_Name") or "")
    current = _expression_value(
        db, SESSION_ID, state, int(source_uid), int(source_owner),
        raw_text, variable_name)
    current = int(current or 0)
    if current >= int(goal):
        return
    list_name = str(variable.get("m_ListAttrName") or variable_name)
    path = [part.strip() for part in list_name.split(">") if part.strip()]
    if not path:
        return
    leaf = path[-1]
    scope = path[-2] if len(path) > 1 else "CardStatsThisTurn"
    champion_map = state.get("champ_map") or {}
    if path[0].lower().startswith("you"):
        champion_uid = champion_map.get(
            int(source_owner), champion_map.get(str(int(source_owner))))
        champions = [champion_uid] if champion_uid is not None else []
    elif path[0].lower().startswith("all"):
        champions = list(dict.fromkeys(
            int(uid) for uid in champion_map.values() if uid is not None))
    else:
        champions = []
    if not champions:
        return

    card_filter = variable.get("m_CardFilter") or {}
    from_rules = _filter_witness_template_rows(
        db, [card_filter], db.execute(
            "SELECT guid FROM card_templates ORDER BY guid LIMIT 128"
        ).fetchall())
    template_guids = [str(row[0]) for row in from_rules if row and row[0]]
    if not template_guids:
        return

    # The authored All/You list is stored on champion TAC, while its values
    # are card instance IDs. Put generated died-card witnesses in the crypt
    # and append them to that exact typed list.
    champion_uid = int(champions[0])
    stats = state.setdefault("tac_statistics", {}).setdefault(
        "cards", {}).setdefault(str(champion_uid), {})
    values = stats.setdefault(scope, {}).setdefault(leaf, [])
    if not isinstance(values, list):
        values = list(values or ())
        stats[scope][leaf] = values
    for index in range(max(0, int(goal) - current)):
        uid = _raw_uid(CARD_UID_BASE + 2400 + int(uid_offset) + index)
        while db.execute(
                "SELECT 1 FROM game_cards WHERE session_id=? AND card_uid=?",
                (SESSION_ID, uid)).fetchone():
            uid = _raw_uid(int(uid) + 1)
        template_guid = template_guids[index % len(template_guids)]
        _insert_card(db, uid, int(source_owner), "discard", template_guid,
                     2400 + index)
        _stage_card_states_for_filters(
            db, uid, [variable.get("m_CardFilter") or {}])
        _stage_filter_relationship_witnesses(
            db, uid, [variable.get("m_CardFilter") or {}],
            source_uid=source_uid)
        values.append(int(uid))
        db.commit()
        actual = _expression_value(
            db, SESSION_ID, state, int(source_uid), int(source_owner),
            raw_text, variable_name)
        if actual is not None and int(actual) >= int(goal):
            state.setdefault("_card_matrix_witnessed_count_variables", set()).add(
                variable_name)
            break
            if found:
                break


def _capture_dynamic_summon_amounts(db, graph, source_uid, source_owner,
                                    state):
    """Record the typed amount field expected by generated effect contracts."""
    from rules_port.static_rules import _expression_value

    raw_text = json.dumps(graph.source.to_dict())
    expected = state.setdefault("_card_matrix_expected_summon_amounts", {})
    variables = {}
    for variable in graph.variables:
        variable = variable.to_dict() if hasattr(variable, "to_dict") else variable
        name = str(variable.get("m_Name") or "")
        if name:
            variables[name] = variable
    witnessed = set(state.get("_card_matrix_witnessed_count_variables") or ())

    def card_count_dependencies(name, seen=None):
        seen = set(seen or ())
        if name in seen:
            return set()
        seen.add(name)
        variable = variables.get(name) or {}
        kind = str(variable.get("_t", "")).rsplit(".", 1)[-1]
        if kind == "CardCountAbilityVariable":
            return {name}
        if kind != "ExpressionAbilityVariable":
            return set()
        expression = str(variable.get("m_ExpressionText") or "")
        dependencies = set()
        for child in re.findall(r"[A-Za-z_]\w*", expression):
            dependencies.update(card_count_dependencies(child, seen))
        return dependencies

    for effect in graph.effects:
        if effect.concrete_type not in {
                "SummonTokenTroopAbilityEffectTemplate",
                "SummonXTokenTroopsAbilityEffectTemplate"}:
            continue
        amount_field = _field(effect.template, "m_AmountField")
        if hasattr(amount_field, "to_dict"):
            amount_field = amount_field.to_dict()
        if not isinstance(amount_field, dict):
            continue
        if str(amount_field.get("_t", "")).rsplit(".", 1)[-1] not in {
                "EffectInputVariable", "EffectAbilityVariable",
                "AbilityVariable", "EffectVariable"}:
            continue
        name = str(amount_field.get("m_InputVariableName") or
                   amount_field.get("m_VariableName") or "")
        if not name:
            continue
        dependencies = card_count_dependencies(name)
        if dependencies - witnessed:
            continue
        value = _expression_value(
            db, SESSION_ID, state, int(source_uid), int(source_owner),
            raw_text, name)
        if value is not None:
            expected[f"{str(graph.guid).lower()}:{str(effect.guid).lower()}"] = int(value)


def _target_zones(target):
    """Return authored candidate zones, preferring a filter's InZone leaf."""
    zone_names = {
        "Warzone": "warzone", "Hand": "hand", "Deck": "deck",
        "Crypt": "discard", "Discard": "discard", "Void": "void",
        "Champions": "champions", "CastSpells": "CastSpells",
        "PlayedResources": "PlayedResources", "Underground": "underground",
        "Choosing": "choosing", "Mod": "mod", "Simulacrum": "simulacrum",
    }
    filter_zones = [
        zone_names.get(str(node.get("m_Collection", "")).rsplit(".", 1)[-1], "")
        for node in _filter_nodes(target.card_filter, "InZone")]
    filter_zones = [zone for zone in filter_zones if zone]
    if filter_zones:
        return tuple(dict.fromkeys(filter_zones))
    flags = (str(target.collection_flags or "").split("|")
             if target.collection_flags else ())
    return tuple(dict.fromkeys(
        zone_names[name.strip()] for name in flags
        if name.strip() in zone_names and name.strip() != "Champions"))


def _condition_zones(card_filter, collection_flags):
    """Return condition witness zones, honoring explicit InZone predicates."""
    zone_names = {
        "warzone": "warzone", "hand": "hand", "deck": "deck",
        "crypt": "discard", "discard": "discard", "void": "void",
        "champions": "champions", "castspells": "CastSpells",
        "playedresources": "PlayedResources", "underground": "underground",
        "choosing": "choosing", "mod": "mod", "simulacrum": "simulacrum",
    }
    filter_zones = [
        zone_names.get(str(node.get("m_Collection", "")).rsplit(
            ".", 1)[-1].lower(), "")
        for node in _filter_nodes(card_filter, "InZone")]
    collection_zones = [
        zone_names.get(name.strip().rsplit(".", 1)[-1].lower(), "")
        for name in str(collection_flags or "").split("|")]
    return tuple(dict.fromkeys(
        zone for zone in (filter_zones or collection_zones)
        if zone and zone != "champions"))


def _typed_nodes(value, wanted_type, *, negated=False):
    """Yield nested typed Records nodes with the requested short type."""
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, dict):
        kind = str(value.get("_t", "")).rsplit(".", 1)[-1]
        if kind in {"NotEffectCondition", "NotAbilityCondition",
                    "NotTriggerCondition"}:
            negated = not negated
        if kind == wanted_type and not negated:
            yield value
        for child in value.values():
            yield from _typed_nodes(child, wanted_type, negated=negated)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _typed_nodes(child, wanted_type, negated=negated)


def _seed_source_counter_conditions(db, graph, source_uid):
    """Satisfy typed source-counter gates so their positive branch is tested."""
    from gamedata import DEFAULT_RECORD_STORE

    desired = {}
    conditions = [graph.trigger_condition]
    for effect in graph.effects:
        condition_guid = str(effect.condition_guid or "").lower()
        if not condition_guid or condition_guid == "0" * 36:
            continue
        record = DEFAULT_RECORD_STORE.get(
            "AbilityEffectConditionTemplate", condition_guid)
        condition = (getattr(record, "condition", None) if record else None)
        conditions.append(condition)
    for condition in conditions:
        for node in _typed_nodes(condition, "SourceCardHasCounters"):
            counter_guid = _guid(node.get("m_CardCounterTemplateId", ""))
            counter = DEFAULT_RECORD_STORE.get(
                "CardCounterTemplate", counter_guid) if counter_guid else None
            counter_name = str(_field(counter, "m_Name", "") or "").lower()
            if not counter_name:
                continue
            try:
                required = int(node.get("m_RequiredCounters", 1) or 0)
            except (TypeError, ValueError):
                required = 0
            operation = str(node.get("m_ComparisonOp", "") or "")
            if operation == "GreaterThan":
                value = required + 1
            elif operation == "LessThan":
                value = max(0, required - 1)
            elif operation in {"LessThanOrEqual", "Equals"}:
                value = required
            else:
                value = max(0, required)
            desired[counter_name] = (counter_guid, value)
    if not desired:
        return
    row = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (SESSION_ID, int(source_uid))).fetchone()
    if row is None:
        return
    try:
        buffs = json.loads(row[0] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if not isinstance(buffs, dict):
        buffs = {}
    counters = buffs.setdefault("counters", {})
    counter_guids = buffs.setdefault("counter_guids", {})
    for name, (guid, value) in desired.items():
        counters[name] = int(value)
        counter_guids[name] = guid
    db.execute(
        "UPDATE game_cards SET permanent_buffs=? WHERE session_id=? "
        "AND card_uid=?", (json.dumps(buffs), SESSION_ID, int(source_uid)))
    db.commit()


def _stage_counter_variable_witnesses(db, graph, source_uid, source_owner,
                                     state):
    """Give resolvable CounterVariables a metadata-valid positive witness."""
    from rules_port.static_rules import _variable_card_candidates

    for raw_variable in graph.variables:
        variable = (raw_variable.to_dict()
                    if hasattr(raw_variable, "to_dict") else raw_variable)
        if not isinstance(variable, dict) or str(
                variable.get("_t", "")).rsplit(".", 1)[-1] != \
                "CounterVariable":
            continue
        counter_guid = _guid(
            variable.get("m_CardCounterTemplateId", ""))
        if not counter_guid:
            continue
        candidates = _variable_card_candidates(
            db, SESSION_ID, state, int(source_uid), int(source_owner),
            variable)
        if not candidates:
            continue
        card = candidates[0]
        card_uid = int(card.get("card_uid") or 0)
        if not card_uid:
            continue
        value = max(1, int(variable.get("m_DefaultValue", 0) or 0))
        if str(card.get("location") or "").lower() == "champions":
            all_counters = state.setdefault("champion_counters", {})
            counters = all_counters.setdefault(str(card_uid), {})
            counters[counter_guid] = max(
                int(counters.get(counter_guid, 0) or 0), value)
            continue
        row = db.execute(
            "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
            "AND card_uid=?", (SESSION_ID, card_uid)).fetchone()
        if row is None:
            continue
        try:
            buffs = json.loads(row[0] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            buffs = {}
        counters = buffs.setdefault("counters", {})
        counter_guids = buffs.setdefault("counter_guids", {})
        counters[counter_guid] = max(
            int(counters.get(counter_guid, 0) or 0), value)
        counter_guids[counter_guid] = counter_guid
        db.execute(
            "UPDATE game_cards SET permanent_buffs=? WHERE session_id=? "
            "AND card_uid=?",
            (json.dumps(buffs), SESSION_ID, card_uid))
    db.commit()


def _condition_witness_template_guids(rows, requirement, count):
    """Choose card templates that satisfy a typed count requirement.

    Ordinary quantity conditions count card instances, so one matching
    template may supply any number of witnesses. Only authored distinct-cost,
    distinct-name, or distinct-race conditions require different templates.
    """
    from gamedata import DEFAULT_RECORD_STORE

    if count <= 0:
        return []
    different_costs = bool(requirement.get("m_OnlyIncludeDifferentCosts", 0))
    different_names = bool(requirement.get("m_OnlyIncludeDifferentNames", 0))
    faction = str(requirement.get(
        "m_OnlyIncludeDifferentRacesForFaction", "") or "").rsplit(
            ".", 1)[-1].lower()
    faction_races = {
        "aria": ("Human", "Elf", "Coyotle", "Orc"),
        "underworld": ("Dwarf", "Shin'hare", "Vennen", "Necrotic"),
    }.get(faction, ())
    if not (different_costs or different_names or faction_races):
        return [str(rows[0][0])] * count if rows else []

    selected = []
    seen = set()
    for row in rows:
        guid = str(row[0])
        template = DEFAULT_RECORD_STORE.get("CardTemplate", guid)
        if template is None:
            continue
        if different_costs:
            value = int(template.resource_cost)
        elif different_names:
            value = str(template.name)
        elif faction_races:
            subtype_tokens = set(str(template.subtype or "").lower().split())
            races = tuple(race for race in faction_races
                          if race.lower() in subtype_tokens)
            if not races:
                continue
            value = races[0]
        else:
            value = None
        if value is not None and value in seen:
            continue
        if value is not None:
            seen.add(value)
        selected.append(guid)
        if len(selected) >= count:
            break
    return selected


def _stage_relative_static_count_condition(db, requirement, source_owner,
                                           source_uid, next_uid):
    """Satisfy a simple authored comparison with the opposing card count."""
    if not (requirement.get("m_QuantityIsHighestOpposingChampionsMatch") or
            requirement.get("m_QuantityIsLowestOpposingChampionsMatch")):
        return False
    player_filter = str(requirement.get(
        "m_PlayerFilter", "Self") or "Self").rsplit(".", 1)[-1].lower()
    if player_filter not in {"self", "you", "controller"}:
        return False

    zone_names = {
        "deck": "deck", "hand": "hand", "champions": "champions",
        "warzone": "warzone", "discard": "discard", "crypt": "discard",
        "void": "void", "castspells": "CastSpells",
        "playedresources": "PlayedResources", "underground": "underground",
        "choosing": "choosing", "mod": "mod", "simulacrum": "simulacrum",
    }
    card_filter = requirement.get("m_CardFilter") or {}
    filter_zones = [zone_names.get(str(node.get("m_Collection") or "")
                                    .rsplit(".", 1)[-1].lower())
                    for node in _filter_nodes(card_filter, "InZone")]
    collection_zones = [zone_names.get(value.strip().rsplit(".", 1)[-1].lower())
                        for value in str(requirement.get(
                            "m_CardCollection") or "").split("|")]
    zones = tuple(dict.fromkeys(zone for zone in
                                (filter_zones or collection_zones) if zone))
    if not zones:
        return False

    from gamedata import DEFAULT_RECORD_STORE
    from pvp_db import db_target_candidate_rows
    from rules_port.filters import (filter_matches_template,
                                    records_filter_from_metadata,
                                    records_filter_matches)
    from rules_port.targeting import _card, _source_card

    source = _source_card(db, SESSION_ID, int(source_uid), int(source_owner))
    try:
        compiled = records_filter_from_metadata(card_filter)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    candidate_types = _card_count_candidate_types(card_filter)
    template_rows = []
    for card_type in candidate_types:
        template_rows.extend(db.execute(
            "SELECT guid FROM card_templates WHERE card_type=? ORDER BY guid "
            "LIMIT 128", (card_type,)).fetchall())
    if not template_rows:
        return False
    matching_templates = [
        row for row in template_rows
        if filter_matches_template(
            compiled, DEFAULT_RECORD_STORE.get("CardTemplate", row[0]))]
    witnesses = _condition_witness_template_guids(
        matching_templates or template_rows, requirement, 1)
    if not witnesses:
        return False

    opposing_owner = (AI_OWNER if int(source_owner) == PLAYER_OWNER
                      else PLAYER_OWNER)
    highest = bool(requirement.get(
        "m_QuantityIsHighestOpposingChampionsMatch"))
    comparison = str(requirement.get("m_ComparisonOp") or "")
    rows = db_target_candidate_rows(
        SESSION_ID, zones, both_players=True, conn=db)
    cards = [_card(row) for row in rows]

    def filtered_count(owner_id):
        context = {"cards": cards}
        return sum(
            int(card.get("controller_id", card.get("user_id", 0)) or 0)
            == int(owner_id) and records_filter_matches(
                card, card_filter, source=source, context=context,
                player=int(source_owner))
            for card in cards)

    primary_count = filtered_count(source_owner)
    opposing_counts = [filtered_count(opposing_owner)]
    reference_count = (max(opposing_counts) if highest
                       else min(opposing_counts))
    if comparison in {"LessThan", "LessThanOrEqual"}:
        witness_owner = opposing_owner
        desired_count = primary_count + (comparison == "LessThan")
        needed = max(0, desired_count - reference_count)
    elif comparison in {"GreaterThan", "GreaterThanOrEqual"}:
        witness_owner = int(source_owner)
        desired_count = reference_count + (comparison == "GreaterThan")
        needed = max(0, desired_count - primary_count)
    elif comparison == "Equals":
        witness_owner = (int(source_owner) if primary_count < reference_count
                         else opposing_owner)
        needed = abs(primary_count - reference_count)
    elif comparison == "NotEqual":
        if primary_count != reference_count:
            return True
        witness_owner = opposing_owner
        needed = 1
    else:
        return False
    if needed <= 0:
        return True
    if needed > 64:
        return False
    collection = zones[0]
    for index in range(needed):
        _insert_card(
            db, _raw_uid(next_uid + index), witness_owner, collection,
            witnesses[0], 3000 + index)
        _stage_card_states_for_filters(
            db, _raw_uid(next_uid + index), [card_filter])
        _stage_filter_relationship_witnesses(
            db, _raw_uid(next_uid + index), [card_filter],
            source_uid=source_uid)
    return True


def _stage_relative_static_resource_condition(requirement, source_owner,
                                              state):
    """Make a simple opposing-resource comparison true from Records fields."""
    if not requirement.get("m_QuantityIsHighestOpposingChampionsMatch"):
        return False
    player_filter = str(requirement.get(
        "m_PlayerFilter", "Self") or "Self").rsplit(".", 1)[-1].lower()
    if player_filter not in {"self", "you", "controller"}:
        return False
    from rules_port.conditions import _player_resources
    from rules_port.runtime_helpers import participant_side

    state = state if isinstance(state, dict) else {}
    source_owner = int(source_owner)
    participants = list(state.get("pids") or ())
    if not participants:
        participants = list((state.get("champ_map") or {}).keys())
    try:
        participants = list(dict.fromkeys(int(pid) for pid in participants))
    except (TypeError, ValueError):
        participants = []
    opposing_owners = [pid for pid in participants if pid != source_owner]
    if not opposing_owners:
        opposing_owners = [AI_OWNER if source_owner == PLAYER_OWNER
                           else PLAYER_OWNER]
    temporary = bool(requirement.get("m_UseTemporaryResources"))
    reference = max(_player_resources(
        state, owner, temporary=temporary) for owner in opposing_owners)
    current = _player_resources(state, source_owner, temporary=temporary)
    operation = str(requirement.get("m_ComparisonOp") or "")
    if operation == "LessThan":
        desired = min(current, max(0, reference - 1))
    elif operation == "LessThanOrEqual":
        desired = min(current, reference)
    elif operation == "GreaterThan":
        desired = max(current, reference + 1)
    elif operation == "GreaterThanOrEqual":
        desired = max(current, reference)
    elif operation == "Equals":
        desired = reference
    elif operation == "NotEqual":
        desired = current if current != reference else reference + 1
    elif operation == "OneMoreThan":
        desired = reference + 1
    elif operation == "TwoMoreThan":
        desired = reference + 2
    elif operation == "OneLessThan":
        desired = max(0, reference - 1)
    else:
        return False
    side = (participant_side(state, source_owner) or "ai")
    raw_key = (f"res_{source_owner}" if temporary else
               f"res_total_{source_owner}")
    key = (raw_key if state.get("pvp") and raw_key in state else
           f"{side}_{'resources' if temporary else 'total_resources'}")
    state[key] = int(desired)
    return True


def _stage_actions_cast_condition(requirement, source_owner, state):
    """Record this turn's actions cast for a ChampionActionsCastThisTurn gate."""
    if state is None:
        return
    from rules_port.runtime_helpers import participant_turn_counter_key
    try:
        required = int(requirement.get("m_RequiredQuantity", 1) or 0)
    except (TypeError, ValueError):
        return
    operation = str(requirement.get("m_ComparisonOp") or "GreaterThanOrEqual")
    if operation not in {"GreaterThanOrEqual", "GreaterThan", "Equals"}:
        return
    count = required + (1 if operation == "GreaterThan" else 0)
    state[participant_turn_counter_key(
        state, source_owner, "actions_cast_this_turn")] = count
    state[participant_turn_counter_key(
        state, source_owner, "actions_cast_turn")] = int(
            state.get("turn_number", 1) or 1)


def _stage_resource_condition_witnesses(db, graph, source_owner, state):
    """Set fixture resources to satisfy authored RequiresTotalResources gates."""
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.runtime_helpers import participant_side

    conditions = [graph.trigger_condition]
    for effect in graph.effects:
        condition_guid = str(effect.condition_guid or "").lower()
        if not condition_guid or condition_guid == "0" * 36:
            continue
        record = DEFAULT_RECORD_STORE.get(
            "AbilityEffectConditionTemplate", condition_guid)
        conditions.append(getattr(record, "condition", None) if record else None)

    participants = list((state or {}).get("pids") or ())
    if not participants:
        participants = list(((state or {}).get("champ_map") or {}).keys())
    try:
        participants = list(dict.fromkeys(int(pid) for pid in participants))
    except (TypeError, ValueError):
        participants = []
    source_owner = int(source_owner)
    if not participants:
        participants = [source_owner,
                        AI_OWNER if source_owner == PLAYER_OWNER else
                        PLAYER_OWNER]
    opponents = [owner for owner in participants if owner != source_owner]

    def owners_for(player_filter):
        if player_filter in {"self", "you", "controller"}:
            return [source_owner]
        if player_filter in {"singleopponent", "singleplayer"}:
            return opponents[:1]
        if player_filter in {"multipleopponents", "multipleplayers"}:
            return opponents
        return []

    def satisfying_value(current, operation, required):
        if operation == "LessThan":
            return min(current, required - 1) if required > 0 else None
        if operation == "LessThanOrEqual":
            return min(current, required)
        if operation == "GreaterThan":
            return max(current, required + 1)
        if operation == "GreaterThanOrEqual":
            return max(current, required)
        if operation == "Equals":
            return required
        if operation == "NotEqual":
            return current if current != required else required + 1
        if operation == "OneMoreThan":
            return required + 1
        if operation == "TwoMoreThan":
            return required + 2
        if operation == "OneLessThan":
            return required - 1 if required > 0 else None
        return None

    for condition in conditions:
        for requirement in _typed_nodes(
                condition, "ChampionActionsCastThisTurn"):
            _stage_actions_cast_condition(requirement, source_owner, state)
        for requirement in _typed_nodes(
                condition, "RequiresTotalResources"):
            if (requirement.get(
                    "m_QuantityIsHighestOpposingChampionsMatch") and
                    _stage_relative_static_resource_condition(
                        requirement, source_owner, state)):
                continue
            player_filter = str(requirement.get(
                "m_PlayerFilter", "Self") or "Self").rsplit(
                    ".", 1)[-1].lower()
            owners = owners_for(player_filter)
            if not owners:
                continue
            try:
                required = int(requirement.get(
                    "m_RequiredQuantity", requirement.get("m_Value", 0)) or 0)
            except (TypeError, ValueError):
                continue
            operation = str(requirement.get("m_ComparisonOp") or "")
            temporary = bool(requirement.get("m_UseTemporaryResources"))
            for owner in owners:
                side = (participant_side(state, owner) or "ai")
                raw_key = (f"res_{owner}" if temporary else
                           f"res_total_{owner}")
                key = (raw_key if state.get("pvp") and raw_key in state else
                       f"{side}_{'resources' if temporary else 'total_resources'}")
                try:
                    current = int(state.get(key, 0) or 0)
                except (TypeError, ValueError):
                    current = 0
                desired = satisfying_value(current, operation, required)
                if desired is not None:
                    state[key] = int(max(0, desired))


def _stage_static_effect_condition_cards(db, graph, source_owner,
                                         source_uid=None, state=None):
    """Seed RequiresCardsControlled gates from typed effect metadata."""
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import (filter_matches_template,
                                    records_filter_from_metadata)

    next_uid = CARD_UID_BASE + 80
    for effect in graph.effects:
        condition_guid = str(effect.condition_guid or "").lower()
        if not condition_guid or condition_guid == "0" * 36:
            continue
        record = DEFAULT_RECORD_STORE.get(
            "AbilityEffectConditionTemplate", condition_guid)
        condition = (getattr(record, "condition", None) if record else None)
        for requirement in _typed_nodes(
                condition, "ChampionActionsCastThisTurn"):
            _stage_actions_cast_condition(requirement, source_owner, state)
        for requirement in _typed_nodes(
                condition, "RequiresTotalResources"):
            _stage_relative_static_resource_condition(
                requirement, source_owner, state)
        for requirement in _typed_nodes(
                condition, "RequiresCardsControlled"):
            if source_uid is not None and \
                    _stage_relative_static_count_condition(
                        db, requirement, source_owner, source_uid, next_uid):
                next_uid += 64
                continue
            card_filter = requirement.get("m_CardFilter") or {}
            zones = _condition_zones(
                card_filter, requirement.get("m_CardCollection", "Warzone"))
            if not zones:
                continue
            collection = zones[0]
            player_filter = str(
                requirement.get("m_PlayerFilter", "Self") or "Self"
            ).rsplit(".", 1)[-1].lower()
            candidate_owner = int(source_owner)
            if player_filter in {
                    "opposing", "opponent", "opponents",
                    "singleopponent", "multipleopponents"}:
                candidate_owner = AI_OWNER if candidate_owner else PLAYER_OWNER
            try:
                required = max(0, int(
                    requirement.get("m_RequiredQuantity", 1) or 0))
            except (TypeError, ValueError):
                required = 0
            operation = str(requirement.get("m_ComparisonOp") or "")
            if ((required == 0 and operation == "Equals") or
                    operation in {"LessThan", "LessThanOrEqual"}):
                # Generated base fixtures contain ordinary hand/board cards.
                # Remove the positive witness pool from the authored
                # collection so a negative quantity gate is actually tested.
                # This is deliberately zone-scoped and leaves other players'
                # state intact.
                for zone in zones:
                    db.execute(
                        "UPDATE game_cards SET location='deck',"
                        "position=COALESCE(position,0)+1000000 "
                        "WHERE session_id=? AND user_id=? AND lower(location)=?"
                        " AND card_uid<>COALESCE(?, -1)",
                        (SESSION_ID, candidate_owner, str(zone).lower(),
                         int(source_uid) if source_uid is not None else None))
                db.commit()
                continue
            if required > 16:
                continue

            subtype_nodes = list(_filter_nodes(card_filter, "IsSubType"))
            subtypes = [str(node.get("m_SubType", "") or "").strip()
                        for node in subtype_nodes]
            template_rows = []
            for subtype in subtypes:
                if subtype:
                    template_rows.extend(db.execute(
                        "SELECT guid FROM card_templates WHERE "
                        "lower(subtype) LIKE ? ORDER BY guid LIMIT 64",
                        (f"%{subtype.lower()}%",)).fetchall())
            for node in _filter_nodes(card_filter, "IsCardName"):
                if any(bool(node.get(flag)) for flag in (
                        "m_CompareToTriggerSource",
                        "m_CompareToTriggerTarget")):
                    continue
                if node.get("m_CompareToAbilitySource") and source_uid is not None:
                    template = db.execute(
                        "SELECT template_guid FROM game_cards WHERE "
                        "session_id=? AND card_uid=?",
                        (SESSION_ID, int(source_uid))).fetchone()
                    if template:
                        template_rows.extend([(template[0],)])
                    continue
                card_name = str(node.get("m_CardName") or "").strip()
                if card_name:
                    template_rows.extend(db.execute(
                        "SELECT guid FROM card_templates WHERE "
                        "lower(name)=? ORDER BY guid LIMIT 64",
                        (card_name.lower(),)).fetchall())
            for node in _filter_nodes(card_filter, "NameContainsFilter"):
                term = str(node.get("m_ContainsString") or "").strip()
                if term:
                    template_rows.extend(db.execute(
                        "SELECT guid FROM card_templates WHERE "
                        "lower(COALESCE(name,'')) LIKE ? OR "
                        "lower(COALESCE(subtype,'')) LIKE ? "
                        "ORDER BY guid LIMIT 64",
                        (f"%{term.lower()}%", f"%{term.lower()}%")
                    ).fetchall())
            if not template_rows and (
                    list(_filter_nodes(card_filter, "IsTroop")) or
                    list(_filter_nodes(card_filter, "IsType"))):
                template_rows = db.execute(
                    "SELECT guid FROM card_templates WHERE card_type='Troop' "
                    "ORDER BY guid LIMIT 64").fetchall()
            if not template_rows:
                continue
            witnesses = _condition_witness_template_guids(
                _filter_witness_template_rows(
                    db, [card_filter], template_rows), requirement, required)
            if not witnesses:
                continue
            for index, template_guid in enumerate(witnesses):
                uid = _raw_uid(next_uid)
                next_uid += 1
                position = _fixture_position_for_filter(
                    db, candidate_owner, collection, card_filter,
                    200 + index, ordinal=index)
                _insert_card(
                    db, uid, candidate_owner, collection, template_guid,
                    position)
                _seed_filter_int_attributes(db, uid, card_filter)
                _stage_card_states_for_filters(db, uid, [card_filter])
                _stage_filter_relationship_witnesses(
                    db, uid, [card_filter], source_uid=source_uid)
                _stage_socketed_gem_witness(
                    db, uid, [card_filter], source_uid=source_uid)


def _stage_effect_condition_cards(db, graph, source_owner, *, source_uid=None,
                                  target_map=None, handler=None):
    """Stage typed witnesses for positive RequiresCardsControlled gates.

    A SameOwner condition counts cards controlled by the target of its
    authored ``m_SecondaryTargetIndex`` effect mapping. Resolve that owner from
    the generated activation map so an opposing-player gate gets its witness
    in the opposing collection, rather than passing accidentally from cards
    on the source side.
    """
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import (filter_matches_template,
                                    records_filter_from_metadata)

    target_map = target_map or {}
    champions = tuple(handler._champion_targets()
                      if handler is not None else ())
    champion_owners = {}
    for entry in champions:
        try:
            uid, owner = int(entry[0]), int(entry[1])
        except (IndexError, TypeError, ValueError):
            continue
        champion_owners[uid] = owner

    next_uid = CARD_UID_BASE + 2000
    # The ability-level condition gates triggered abilities too (Allegiance:
    # "if you control another Elf" is m_AbilityCondition on the Deploy).
    condition_items = [(graph.ability_condition, None),
                       (graph.trigger_condition, None)]
    for effect in graph.effects:
        condition_guid = str(effect.condition_guid or "").lower()
        if not condition_guid or condition_guid == "0" * 36:
            continue
        record = DEFAULT_RECORD_STORE.get(
            "AbilityEffectConditionTemplate", condition_guid)
        condition = getattr(record, "condition", None) if record else None
        condition_items.append((condition, effect))
    for condition, effect in condition_items:
        for requirement in _typed_nodes(condition, "RequiresCardsControlled"):
            card_filter = requirement.get("m_CardFilter") or {}
            if (source_uid is not None and
                    (requirement.get(
                        "m_QuantityIsHighestOpposingChampionsMatch") or
                     requirement.get(
                        "m_QuantityIsLowestOpposingChampionsMatch")) and
                    _stage_relative_static_count_condition(
                        db, requirement, source_owner, source_uid, next_uid)):
                next_uid += 100
                continue
            try:
                required = max(0, int(
                    requirement.get("m_RequiredQuantity", 1) or 0))
            except (TypeError, ValueError):
                continue
            operation = str(requirement.get("m_ComparisonOp", "") or "")
            if operation == "GreaterThan":
                witness_count = required + 1
            elif operation in {"LessThan", "LessThanOrEqual"}:
                # These negative gates need an empty/minimal witness set; the
                # broad fixture already supplies that for the authored cases.
                witness_count = 0
            else:
                witness_count = required
            if witness_count > 1024:
                continue

            same_owner = bool(requirement.get("m_SameOwner", 0))
            candidate_owners = []
            if same_owner:
                secondary_index = (int(effect.secondary_target_index)
                                   if effect is not None else -1)
                referenced = next((candidate for candidate in graph.effects
                                   if candidate.effect_instance_id ==
                                   secondary_index), None)
                raw_targets = (target_map.get(referenced.target_index, ())
                               if referenced is not None else ())
                if not isinstance(raw_targets, (tuple, list, set)):
                    raw_targets = (raw_targets,) if raw_targets is not None else ()
                for raw_uid in raw_targets:
                    if raw_uid is None:
                        continue
                    uid = int(raw_uid)
                    owner = champion_owners.get(uid)
                    if owner is None:
                        row = db.execute(
                            "SELECT user_id FROM game_cards WHERE session_id=? "
                            "AND card_uid=?", (SESSION_ID, uid)).fetchone()
                        owner = int(row[0]) if row else None
                    if owner is not None and owner not in candidate_owners:
                        candidate_owners.append(owner)
            else:
                player_filter = str(
                    requirement.get("m_PlayerFilter", "Self") or "Self"
                ).rsplit(".", 1)[-1].lower()
                if player_filter == "singleplayer":
                    continue
                if player_filter == "multipleplayers":
                    candidate_owners.extend((int(source_owner),
                        AI_OWNER if int(source_owner) == PLAYER_OWNER
                        else PLAYER_OWNER))
                elif player_filter == "multipleopponents":
                    candidate_owners.append(
                        AI_OWNER if int(source_owner) == PLAYER_OWNER
                        else PLAYER_OWNER)
                elif player_filter in {
                        "opposing", "opponent", "opponents",
                        "singleopponent"}:
                    candidate_owners.append(
                        AI_OWNER if int(source_owner) else PLAYER_OWNER)
                else:
                    candidate_owners.append(int(source_owner))
            zones = _condition_zones(
                card_filter, requirement.get(
                    "m_CardCollection", "Warzone"))
            if not zones:
                continue
            collection = zones[0]

            quantity_filter = requirement.get("m_QuantityCardFilter")
            if (quantity_filter is not None and not same_owner and
                    operation in {"GreaterThan", "GreaterThanOrEqual"} and
                    candidate_owners):
                # Relative RequiresCardsControlled gates compare the count
                # selected by m_CardFilter with the count selected by
                # m_QuantityCardFilter. A fixed RequiredQuantity witness is
                # insufficient for clauses such as "more cards in your
                # discard than in your deck".
                from rules_port.filters import records_filter_matches
                from rules_port.targeting import _source_card

                source_card = _source_card(
                    db, SESSION_ID, int(source_uid), int(source_owner))

                def filtered_count(filter_spec, owner):
                    rows = db.execute(
                        "SELECT card_uid FROM game_cards WHERE session_id=? "
                        "AND user_id=? ORDER BY position,card_uid",
                        (SESSION_ID, int(owner))).fetchall()
                    total = 0
                    for (uid,) in rows:
                        candidate = _source_card(
                            db, SESSION_ID, int(uid), int(owner))
                        if candidate and records_filter_matches(
                                candidate, filter_spec, source=source_card,
                                context={}, player=int(source_owner)):
                            total += 1
                    return total

                quantity_count = sum(
                    filtered_count(quantity_filter, candidate_owner)
                    for candidate_owner in candidate_owners)
                current_count = sum(
                    filtered_count(card_filter, candidate_owner)
                    for candidate_owner in candidate_owners)
                minimum_count = quantity_count + int(
                    operation == "GreaterThan")
                if current_count >= minimum_count:
                    witness_count = 0
                    candidate_owners = []
                else:
                    witness_count = minimum_count - current_count
                    # The filter's total is over all selected owners. Add the
                    # missing witnesses on one valid owner instead of
                    # multiplying the required count by the player count.
                    candidate_owners = [candidate_owners[0]]
            if quantity_filter and operation == "Equals" and not same_owner:
                # Some typed count conditions compare every card in a zone
                # against a quantity sub-filter (for example, "all cards in
                # your hand have this name"). Remove only generated cards
                # that fail the sub-filter, leaving authored matching cards
                # and the ability source in place.
                from rules_port.filters import records_filter_matches
                from rules_port.targeting import _source_card
                source_card = (_source_card(
                    db, SESSION_ID, int(source_uid), int(source_owner))
                    if source_uid is not None else None)
                for owner in dict.fromkeys(candidate_owners):
                    placeholders = ",".join("?" for _ in zones)
                    rows = db.execute(
                        "SELECT card_uid FROM game_cards WHERE session_id=? "
                        "AND user_id=? AND lower(location) IN (" +
                        placeholders + ")",
                        (SESSION_ID, int(owner),
                         *(str(zone).lower() for zone in zones))).fetchall()
                    for (uid,) in rows:
                        candidate = _source_card(
                            db, SESSION_ID, int(uid), int(owner))
                        if candidate and records_filter_matches(
                                candidate, quantity_filter,
                                source=source_card, context={"cards": [
                                    _source_card(db, SESSION_ID, int(row[0]),
                                                 int(owner))
                                    for row in rows]},
                                player=int(source_owner)):
                            continue
                        if source_uid is not None and int(uid) == int(source_uid):
                            continue
                        db.execute(
                            "UPDATE game_cards SET location='deck',"
                            "position=COALESCE(position,0)+1000000 "
                            "WHERE session_id=? AND card_uid=?",
                            (SESSION_ID, int(uid)))
                db.commit()
                continue

            if not candidate_owners or witness_count == 0:
                continue

            template_rows = []
            for node in _filter_nodes(card_filter, "IsCardName"):
                if any(bool(node.get(flag)) for flag in (
                        "m_CompareToTriggerSource",
                        "m_CompareToTriggerTarget")):
                    continue
                if node.get("m_CompareToAbilitySource"):
                    source_row = db.execute(
                        "SELECT template_guid FROM game_cards WHERE "
                        "session_id=? AND card_uid=?",
                        (SESSION_ID, source_uid)).fetchone()
                    if source_row:
                        template_rows.append((source_row[0],))
                    continue
                card_name = str(node.get("m_CardName") or "").strip()
                if card_name:
                    template_rows.extend(db.execute(
                        "SELECT guid FROM card_templates WHERE "
                        "lower(name)=? ORDER BY guid LIMIT 64",
                        (card_name.lower(),)).fetchall())
            for node in _filter_nodes(card_filter, "IsSubType"):
                subtype = str(node.get("m_SubType", "") or "").strip()
                if subtype:
                    template_rows.extend(db.execute(
                        "SELECT guid FROM card_templates WHERE "
                        "lower(subtype) LIKE ? ORDER BY guid LIMIT 128",
                        (f"%{subtype.lower()}%",)).fetchall())
            if not template_rows:
                card_types = _card_count_candidate_types(card_filter)
                for card_type in card_types:
                    template_rows.extend(db.execute(
                        "SELECT guid FROM card_templates WHERE card_type=? "
                        "ORDER BY guid LIMIT 128", (card_type,)).fetchall())
            if not template_rows:
                template_rows = db.execute(
                    "SELECT guid FROM card_templates ORDER BY guid LIMIT 128"
                ).fetchall()
            if not template_rows:
                continue
            witnesses = _condition_witness_template_guids(
                _filter_witness_template_rows(
                    db, [card_filter], template_rows), requirement,
                witness_count)
            if not witnesses:
                continue
            for owner in candidate_owners:
                for index, template_guid in enumerate(witnesses):
                    position = _fixture_position_for_filter(
                        db, owner, collection, card_filter,
                        1000 + index, ordinal=index)
                    _insert_card(
                        db, _raw_uid(next_uid), owner, collection,
                        template_guid, position)
                    _seed_filter_int_attributes(db, _raw_uid(next_uid),
                                                card_filter)
                    _stage_card_states_for_filters(
                        db, _raw_uid(next_uid), [card_filter])
                    _stage_filter_relationship_witnesses(
                        db, _raw_uid(next_uid), [card_filter],
                        source_uid=source_uid)
                    next_uid += 1
    db.commit()


def _fixture_position_for_filter(db, owner, collection, filter_spec,
                                 fallback, *, ordinal=0):
    """Place generated deck witnesses where typed positional filters see them.

    ``TopNOfDeck`` evaluates the live ordered deck, so a metadata-matching
    witness at the fixture's ordinary append position is not enough. Stage
    one-sided top/bottom filters at the corresponding end of the owner's
    deck; leave unqualified cards at their requested fixture position.
    """
    if str(collection or "").lower() != "deck":
        return int(fallback)
    positional = list(_filter_nodes(filter_spec, "TopNOfDeck"))
    if not positional:
        return int(fallback)
    bottom_only = all(bool(node.get("m_CountFromBottom"))
                      for node in positional)
    row = db.execute(
        "SELECT COALESCE(MAX(position),0), COALESCE(MIN(position),0) "
        "FROM game_cards WHERE session_id=? AND user_id=? "
        "AND LOWER(location)='deck'",
        (SESSION_ID, int(owner))).fetchone()
    maximum, minimum = (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)
    if bottom_only:
        return max(maximum, int(fallback)) + 100000 + int(ordinal)
    return min(minimum, int(fallback)) - 100000 + int(ordinal)


def _seed_target_filter_candidates(db, graph, source_uid, owner, state,
                                   handler, extra_targets=None):
    """Add matching typed cards where the broad fixture lacks a target.

    Candidate templates come from the target's Records CardFilter and card
    template metadata. Positive integer-attribute predicates are staged on
    the generated card instance, leaving the resolver to validate the full
    authored filter.
    """
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import (_champion_class,
                                    filter_matches_template,
                                    records_filter_from_metadata)
    from rules_port.targeting import legal_targets_for

    extra_targets = dict(extra_targets or {})
    target_indexes = sorted({
        int(effect.target_index) for effect in graph.effects
        if int(effect.target_index) >= 0} | set(extra_targets))
    count_filter_specs = []
    for index in target_indexes:
        target = extra_targets.get(index)
        if target is None and index < len(graph.targets):
            target = graph.targets[index]
        if target is not None:
            count_filter_specs.extend(
                spec for spec in (
                    target.card_filter,
                    *_contract_target_condition_filters(graph, index))
                if spec)
    _stage_filter_count_witnesses(
        db, graph, source_uid, owner, state, count_filter_specs)
    next_fixture_uid = CARD_UID_BASE + 50
    for index in target_indexes:
        target = extra_targets.get(index)
        if target is None:
            if index >= len(graph.targets):
                continue
            target = graph.targets[index]
        minimum = int(target.resolved_minimum() or 0)
        maximum = int(target.resolved_maximum() or 0)
        required_candidates = max(1, minimum)
        if maximum > 0:
            required_candidates = min(required_candidates, maximum)
        champions = handler._champion_targets()
        existing = legal_targets_for(
            db, SESSION_ID, owner, target, source_uid,
            both_players=True, champions=champions, battle_state=state)
        condition_filters = _contract_target_condition_filters(graph, index)
        existing = [uid for uid in existing
                    if (not condition_filters or all(
                        _condition_filter_matches_template(
                            db, filter_spec, uid)
                        for filter_spec in condition_filters))]
        if len(existing) >= required_candidates:
            continue
        if list(_filter_nodes(target.card_filter or {}, "IsHero")):
            # Champion targets come from the champion fixture. No card
            # template can become a champion, so searching ~256 templates
            # here only exhausts the scenario's time budget.
            continue
        if target.target_kind == "DuplicateCardTargetTemplate":
            # C# enumerates a card only after seeing another card with the
            # same name in that player's same collection. This target has no
            # ordinary positive CardFilter hint, so add one matching pair to
            # a legal authored zone before asking the production target
            # enumerator to prove the result.
            duplicate_filter = target.card_filter or {}
            template_rows = _filter_witness_template_rows(
                db, [duplicate_filter], db.execute(
                    "SELECT guid FROM card_templates ORDER BY guid LIMIT 256"
                ).fetchall())
            zones = _target_zones(target)
            if not zones:
                continue
            candidate_owner = (AI_OWNER if str(
                target.player_filter or "").lower() in {
                    "singleopponent", "multipleopponents"} else int(owner))
            for template_guid, in template_rows:
                staged_uids = []
                for ordinal in range(2):
                    uid = _raw_uid(next_fixture_uid)
                    next_fixture_uid += 1
                    if db.execute(
                            "SELECT 1 FROM game_cards WHERE session_id=? "
                            "AND card_uid=?", (SESSION_ID, uid)).fetchone():
                        continue
                    _insert_card(
                        db, uid, candidate_owner, zones[0], template_guid,
                        _fixture_position_for_filter(
                            db, candidate_owner, zones[0], [duplicate_filter],
                            100 + index, ordinal=next_fixture_uid))
                    staged_uids.append(uid)
                if len(staged_uids) != 2:
                    for uid in staged_uids:
                        db.execute(
                            "DELETE FROM game_cards WHERE session_id=? "
                            "AND card_uid=?", (SESSION_ID, int(uid)))
                    db.commit()
                    continue
                db.commit()
                for uid in staged_uids:
                    _seed_filter_int_attributes(
                        db, uid, duplicate_filter)
                    _stage_card_states_for_filters(
                        db, uid, [duplicate_filter])
                    _stage_filter_tac_statistics(
                        state, uid, [duplicate_filter])
                matching = legal_targets_for(
                    db, SESSION_ID, owner, target, source_uid,
                    both_players=True, champions=champions,
                    battle_state=state)
                matching = [uid for uid in matching
                            if not condition_filters or all(
                                _condition_filter_matches_template(
                                    db, filter_spec, uid)
                                for filter_spec in condition_filters)]
                if len(matching) >= required_candidates:
                    break
                for uid in staged_uids:
                    db.execute(
                        "DELETE FROM game_cards WHERE session_id=? "
                        "AND card_uid=?", (SESSION_ID, int(uid)))
                db.commit()
            continue
        filter_specs = [target.card_filter, *condition_filters]
        subtypes = []
        name_terms = []
        exact_names = []
        card_types = set()
        for filter_spec in filter_specs:
            for node in _filter_nodes(filter_spec, "IsSubType"):
                subtype = str(node.get("m_SubType", "") or "").strip()
                if subtype and subtype != "*" and subtype.lower() not in {
                        value.lower() for value in subtypes}:
                    subtypes.append(subtype)
            if list(_filter_nodes(
                    filter_spec,
                    "HasASharedClassWithSourceChampionFilter")):
                raw_champion_guids = state.get("champ_guid_map") or {}
                champion_guid = (raw_champion_guids.get(str(owner)) or
                                 raw_champion_guids.get(int(owner)))
                champion_class = _champion_class(
                    {"template_guid": champion_guid})
                if (champion_class and champion_class.lower() not in {
                        value.lower() for value in subtypes}):
                    subtypes.append(champion_class)
            for node in _filter_nodes(filter_spec, "NameContainsFilter"):
                term = str(node.get("m_ContainsString", "") or "").strip()
                if term and term.lower() not in {
                        value.lower() for value in name_terms}:
                    name_terms.append(term)
            for node in _filter_nodes(filter_spec, "IsCardName"):
                if any(bool(node.get(flag)) for flag in (
                        "m_CompareToAbilitySource",
                        "m_CompareToTriggerSource",
                        "m_CompareToTriggerTarget")):
                    continue
                name = str(node.get("m_CardName", "") or "").strip()
                if name and name.lower() not in {
                        value.lower() for value in exact_names}:
                    exact_names.append(name)
            for node in _filter_nodes(filter_spec, "IsTroop"):
                card_types.add("Troop")
            for node in _filter_nodes(filter_spec, "IsType"):
                card_types.update(
                    value.strip().rsplit(".", 1)[-1]
                    for value in str(node.get("m_CardType", "") or "")
                    .split("|") if value.strip())
            card_types.update(_card_count_candidate_types(filter_spec))
        int_attr_nodes = [
            node for filter_spec in filter_specs
            for node in _filter_nodes(filter_spec, "IntAttrFilter")]
        zones = _target_zones(target)
        has_parent_relation = any(
            list(_filter_nodes(filter_spec, "IsChildOfAbilitySource"))
            for filter_spec in filter_specs)
        if has_parent_relation and not card_types:
            # A parent relationship does not constrain a template type. When
            # the authored passive modifies combat stats, use the matching
            # combat-card family as its generated child witness.
            from rules_port.metadata import modifier_metadata
            for effect in graph.effects:
                if int(effect.target_index) != index:
                    continue
                prop = str(modifier_metadata(effect.guid).get(
                    "property") or "").lower()
                if prop in {"attack", "defense"}:
                    card_types.add("Troop")
                    break
        if has_parent_relation and "warzone" in zones:
            zones = ("warzone", *tuple(
                zone for zone in zones if zone != "warzone"))
        if not zones or not (subtypes or name_terms or exact_names or
                             card_types):
            continue
        template_rows = []
        if subtypes:
            for subtype in subtypes:
                template_rows.extend(db.execute(
                    "SELECT guid FROM card_templates WHERE "
                    "lower(subtype) LIKE ? ORDER BY guid LIMIT 128",
                    (f"%{subtype.lower()}%",)).fetchall())
        if name_terms:
            for term in name_terms:
                template_rows.extend(db.execute(
                    "SELECT guid FROM card_templates WHERE "
                    "lower(COALESCE(name,'')) LIKE ? OR "
                    "lower(COALESCE(subtype,'')) LIKE ? "
                    "ORDER BY guid LIMIT 256",
                    (f"%{term.lower()}%", f"%{term.lower()}%")).fetchall())
        if exact_names:
            for name in exact_names:
                template_rows.extend(db.execute(
                    "SELECT guid FROM card_templates WHERE "
                    "lower(name)=? ORDER BY guid LIMIT 64",
                    (name.lower(),)).fetchall())
        for card_type in sorted(card_types):
            template_rows.extend(db.execute(
                "SELECT guid FROM card_templates WHERE "
                "lower(card_type) LIKE ? ORDER BY guid LIMIT 256",
                (f"%{card_type.lower()}%",)).fetchall())
        if not template_rows:
            template_rows = db.execute(
                "SELECT guid FROM card_templates ORDER BY guid LIMIT 256"
            ).fetchall()
        template_rows = _filter_witness_template_rows(
            db, filter_specs, template_rows)
        from rules_port.metadata import modifier_metadata
        if any(
                int(effect.target_index) == int(index) and
                effect.concrete_type == "CardModifierAbilityEffectTemplate" and
                str(modifier_metadata(effect.guid).get("property") or "")
                .lower() == "attack"
                for effect in graph.effects):
            # Negative attack modifiers clamp at zero. Prefer a legal troop
            # with printed attack above zero so the contract can observe the
            # authored modifier instead of selecting a zero-attack witness
            # whose projected value is unchanged.
            candidate_guids = list(dict.fromkeys(
                str(row[0]).lower() for row in template_rows))
            attacks = {}
            for offset in range(0, len(candidate_guids), 800):
                batch = candidate_guids[offset:offset + 800]
                if not batch:
                    continue
                marks = ",".join("?" for _ in batch)
                attacks.update({
                    str(guid).lower(): int(attack or 0)
                    for guid, attack in db.execute(
                        "SELECT guid,COALESCE(attack,0) "
                        "FROM card_templates WHERE guid IN (" + marks + ")",
                        batch).fetchall()})
            template_rows.sort(
                key=lambda row: (attacks.get(str(row[0]).lower(), 0) != 0,
                                 abs(attacks.get(str(row[0]).lower(), 0))),
                reverse=True)
        seen_templates = set()
        for (template_guid,) in template_rows:
            normalized_guid = str(template_guid).lower()
            if normalized_guid in seen_templates:
                continue
            seen_templates.add(normalized_guid)
            template = DEFAULT_RECORD_STORE.get("CardTemplate", template_guid)
            if template is None:
                continue
            seeded = len(existing)
            candidate_owners = ((int(owner),) if has_parent_relation else
                                (AI_OWNER, int(owner)))
            for candidate_owner in candidate_owners:
                if seeded >= required_candidates:
                    break
                for zone in zones:
                    if zone == "champions":
                        continue
                    while seeded < required_candidates:
                        uid = _raw_uid(next_fixture_uid)
                        next_fixture_uid += 1
                        if db.execute(
                                "SELECT 1 FROM game_cards WHERE session_id=? "
                                "AND card_uid=?", (SESSION_ID, uid)).fetchone():
                            continue
                        position = _fixture_position_for_filter(
                            db, candidate_owner, zone, filter_specs,
                            100 + index, ordinal=next_fixture_uid)
                        _insert_card(
                            db, uid, candidate_owner, zone, template_guid,
                            position)
                        db.commit()
                        if int_attr_nodes:
                            _seed_filter_int_attributes(
                                db, uid, target.card_filter)
                        _stage_filter_counter_witnesses(
                            db, uid, filter_specs)
                        _stage_card_states_for_filters(
                            db, uid, filter_specs)
                        _stage_filter_relationship_witnesses(
                            db, uid, filter_specs, source_uid=source_uid)
                        _stage_filter_tac_statistics(state, uid, filter_specs)
                        _stage_socketed_gem_witness(
                            db, uid, filter_specs, source_uid=source_uid)
                        matching = legal_targets_for(
                            db, SESSION_ID, owner, target, source_uid,
                            both_players=True, champions=champions,
                            battle_state=state)
                        if (int(uid) in matching and
                                (not condition_filters or all(
                                    _condition_filter_matches_template(
                                        db, filter_spec, uid)
                                    for filter_spec in condition_filters))):
                            seeded += 1
                            continue
                        db.execute(
                            "DELETE FROM game_cards WHERE session_id=? "
                            "AND card_uid=?", (SESSION_ID, uid))
                        db.commit()
                        break
                    if seeded >= required_candidates:
                        break
            if seeded >= required_candidates:
                break

    # A MatchSecondaryTargetTemplate can resolve an authored set such as
    # "each troop with the same name as the previous target".  A broad board
    # may contain the first target but no second card with that name, leaving
    # the second effect with a valid empty set.  Duplicate each possible first
    # target into the secondary target's authored zone so the contract exercises
    # the set movement and its persisted zone delta.
    for index in target_indexes:
        target = extra_targets.get(index)
        if target is None:
            if index >= len(graph.targets):
                continue
            target = graph.targets[index]
        if target.target_kind != "MatchSecondaryTargetTemplate":
            continue
        effect_links = [effect for effect in graph.effects
                        if int(effect.target_index) == index and
                        int(effect.secondary_target_index) >= 0]
        zones = _target_zones(target)
        for effect in effect_links:
            referenced = next((candidate for candidate in graph.effects
                               if int(candidate.effect_instance_id) ==
                               int(effect.secondary_target_index)), None)
            if (referenced is None or int(referenced.target_index) < 0 or
                    int(referenced.target_index) >= len(graph.targets)):
                continue
            reference_target = graph.targets[int(referenced.target_index)]
            reference_uids = legal_targets_for(
                db, SESSION_ID, owner, reference_target, source_uid,
                both_players=True, champions=handler._champion_targets(),
                battle_state=state)
            condition_filters = _contract_target_condition_filters(
                graph, index)
            filter_specs = [target.card_filter, *condition_filters]
            for reference_uid in reference_uids:
                row = db.execute(
                    "SELECT template_guid,user_id FROM game_cards WHERE "
                    "session_id=? AND card_uid=?",
                    (SESSION_ID, int(reference_uid))).fetchone()
                if row is None:
                    continue
                template_guid, candidate_owner = row
                template = DEFAULT_RECORD_STORE.get(
                    "CardTemplate", template_guid)
                if template is None:
                    continue
                name = str(getattr(template, "name", "") or "").lower()
                if not name:
                    continue
                current = legal_targets_for(
                    db, SESSION_ID, owner, target, source_uid,
                    both_players=True, champions=handler._champion_targets(),
                    battle_state=state)
                same_name = 0
                for candidate_uid in current:
                    candidate_row = db.execute(
                        "SELECT ct.name,gc.user_id FROM game_cards gc JOIN "
                        "card_templates ct ON ct.guid=gc.template_guid "
                        "WHERE gc.session_id=? AND gc.card_uid=?",
                        (SESSION_ID, int(candidate_uid))).fetchone()
                    if (candidate_row and
                            str(candidate_row[0] or "").lower() == name and
                            int(candidate_row[1] or 0) ==
                            int(candidate_owner or 0) and
                            all(_condition_filter_matches_template(
                                db, spec, int(candidate_uid))
                                for spec in condition_filters)):
                        same_name += 1
                if same_name >= 2:
                    continue
                for zone in zones:
                    if zone == "champions":
                        continue
                    uid = _raw_uid(next_fixture_uid)
                    next_fixture_uid += 1
                    position = _fixture_position_for_filter(
                        db, int(candidate_owner or 0), zone, filter_specs,
                        200 + index, ordinal=next_fixture_uid)
                    _insert_card(
                        db, uid, int(candidate_owner or 0), zone,
                        template_guid, position)
                    db.commit()
                    _seed_filter_int_attributes(
                        db, uid, filter_specs)
                    _stage_card_states_for_filters(db, uid, filter_specs)
                    _stage_filter_relationship_witnesses(
                        db, uid, filter_specs, source_uid=source_uid)
                    _stage_socketed_gem_witness(
                        db, uid, filter_specs, source_uid=source_uid)
                    matching = legal_targets_for(
                        db, SESSION_ID, owner, target, source_uid,
                        both_players=True,
                        champions=handler._champion_targets(),
                        battle_state=state)
                    if (int(uid) in matching and all(
                            _condition_filter_matches_template(
                                db, spec, int(uid))
                            for spec in condition_filters)):
                        break
                    db.execute(
                        "DELETE FROM game_cards WHERE session_id=? "
                        "AND card_uid=?", (SESSION_ID, int(uid)))
                    db.commit()

    # List-backed targets are not enumerated from arbitrary broad-board cards.
    # They use the per-ability ListAttrs that C# records as cards are drawn,
    # buried, created, revealed, or voided. Seed a typed witness in the
    # corresponding zone and list so the contract can exercise the authored
    # follow-up effect instead of treating an empty list as a card failure.
    from rules_port.filters import records_filter_matches
    from rules_port.targeting import _source_card
    for index in target_indexes:
        target = extra_targets.get(index)
        if target is None:
            if index >= len(graph.targets):
                continue
            target = graph.targets[index]
        kind = target.target_kind
        if kind not in {
                "SourceDrawnTargetTemplate", "SourceBuriedTargetTemplate",
                "SourceRevealedTargetTemplate", "VoidedTargetTemplate"}:
            continue
        # An unfiltered SourceRevealed target represents cards revealed by an
        # earlier effect, such as "the remaining cards". It does not need a
        # made-up deck witness; seeding one can displace the filtered witness
        # from the top-N set that the preceding reveal actually selects.
        if (kind == "SourceRevealedTargetTemplate" and
                target.card_filter is None):
            continue
        card_filter = (target.card_filter
                       if target.card_filter is not None else {})
        template_guid = _zone_trigger_witness_template(
            db, card_filter)
        if not template_guid:
            continue
        explicit_filter_zones = _condition_zones(
            card_filter, "")
        if kind == "SourceRevealedTargetTemplate":
            zones = explicit_filter_zones or ("deck",)
        else:
            preferred = {
                "SourceDrawnTargetTemplate": "hand",
                "SourceBuriedTargetTemplate": "discard",
                "VoidedTargetTemplate": "void",
            }[kind]
            zones = ((explicit_filter_zones[0],)
                     if explicit_filter_zones else
                     (preferred,))
        target_record = DEFAULT_RECORD_STORE.get(
            "AbilityTargetTemplate", str(target.guid).lower())
        buried_list_name = (str(target_record.field(
            "m_ListAttrName", "BuriedCards") or "BuriedCards").strip()
                            if target_record is not None else "BuriedCards")
        list_name = {
            "SourceDrawnTargetTemplate": "DrawnCards",
            "SourceBuriedTargetTemplate": buried_list_name,
            "VoidedTargetTemplate": "VoidedCards",
        }.get(kind, "")
        filter_specs = ([card_filter]
                        if target.card_filter is not None else [])
        for candidate_owner in (int(owner), AI_OWNER):
            if kind == "SourceRevealedTargetTemplate" and \
                    candidate_owner != int(owner):
                continue
            seeded = False
            for zone in zones:
                if zone == "champions":
                    continue
                uid = _raw_uid(next_fixture_uid)
                next_fixture_uid += 1
                placement_filter = filter_specs
                if kind == "SourceRevealedTargetTemplate":
                    placement_filter = [
                        *filter_specs,
                        {"_t": "TopNOfDeck", "m_Amount": 5,
                         "m_CountFromBottom": 0}]
                position = _fixture_position_for_filter(
                    db, candidate_owner, zone, placement_filter,
                    200 + index, ordinal=next_fixture_uid)
                _insert_card(
                    db, uid, candidate_owner, zone, template_guid,
                    position)
                db.commit()
                _seed_filter_int_attributes(db, uid, filter_specs)
                _stage_card_states_for_filters(db, uid, filter_specs)
                _stage_filter_relationship_witnesses(
                    db, uid, filter_specs, source_uid=source_uid)
                _stage_socketed_gem_witness(
                    db, uid, filter_specs, source_uid=source_uid)
                candidate = _source_card(
                    db, SESSION_ID, int(uid), candidate_owner)
                source = _source_card(
                    db, SESSION_ID, int(source_uid), int(owner))
                matches_filter = (
                    target.card_filter is None or
                    records_filter_matches(
                        candidate, card_filter, source=source,
                        context=state, player=int(owner)))
                matches_conditions = all(
                    _condition_filter_matches_template(db, spec, int(uid))
                    for spec in _contract_target_condition_filters(
                        graph, index))
                if not (matches_filter and matches_conditions):
                    db.execute(
                        "DELETE FROM game_cards WHERE session_id=? "
                        "AND card_uid=?", (SESSION_ID, int(uid)))
                    db.commit()
                    continue
                if kind != "SourceRevealedTargetTemplate":
                    per_ability = state.setdefault(
                        "list_attrs", {}).setdefault(
                            str(graph.guid).lower(), {})
                    per_ability.setdefault(list_name, []).append(int(uid))
                    if kind == "VoidedTargetTemplate":
                        state.setdefault("voided_by", {}).setdefault(
                            str(int(source_uid)), []).append(int(uid))
                seeded = True
                break
            if seeded:
                break


def _ordered_contract_effects(graph):
    """Return typed mappings in the order used by the native resolver."""
    return tuple(effect for index, effect in sorted(
        enumerate(graph.effects),
        key=lambda item: (
            int(item[1].effect_group_id),
            int(item[1].effect_instance_id),
            item[0])))


def _merge_effect_status_snapshots(states):
    """Union per-ability application outcomes from independent branches."""
    merged = {}
    for state in states:
        for ability_guid, outcomes in (
                state.get("_ability_effect_status_by_guid") or {}).items():
            destination = merged.setdefault(str(ability_guid).lower(), {})
            for instance_id, applied in outcomes.items():
                key = str(instance_id)
                destination[key] = bool(
                    destination.get(key, False) or applied)
    return merged


def _assert_graph_trace(graph, state, *, db=None, require_complete=True):
    """Validate traced mappings and, by default, require graph completeness."""
    effects = _ordered_contract_effects(graph)
    traces = [entry for entry in state.get("ability_trace") or ()
              if str(entry.get("ability_guid") or "").lower() ==
              str(graph.guid).lower()]
    traced_instance_ids_by_activation = {}
    observed_orders = set()
    for entry in traces:
        try:
            order = int(entry.get("effect_order"))
        except (TypeError, ValueError):
            raise AssertionError(
                f"ability {graph.guid}: trace has no valid effect order")
        if order < 0 or order >= len(effects):
            raise AssertionError(
                f"ability {graph.guid}: trace references unmapped effect "
                f"order {order}")
        effect = effects[order]
        expected_guid = str(effect.guid or "").lower()
        observed_guid = str(entry.get("effect_guid") or "").lower()
        if observed_guid != expected_guid:
            raise AssertionError(
                f"ability {graph.guid}: effect order {order} traced "
                f"{observed_guid}, expected {expected_guid}")
        observed_orders.add(order)
        try:
            activation_id = entry.get("ability_instance_id")
            traced_instance_ids_by_activation.setdefault(
                activation_id, set()).add(
                    int(effects[order].effect_instance_id))
        except (TypeError, ValueError):
            pass
        expected_amount = (state.get(
            "_card_matrix_expected_summon_amounts") or {}).get(
                f"{str(graph.guid).lower()}:{expected_guid}")
        error = _effect_contract_error(
            effect, entry, dynamic_amount=expected_amount,
            db=db, graph=graph)
        if error:
            raise AssertionError(
                f"ability {graph.guid} effect {effect.guid} "
                f"({effect.concrete_type}): {error}")
    applied_effects = state.get("applied_effects") or {}
    status_by_ability = state.get("_ability_effect_status_by_guid") or {}
    ability_status = status_by_ability.get(str(graph.guid).lower())
    condition_skipped_orders = set()
    for order, effect in enumerate(effects):
        try:
            instance_id = int(effect.effect_instance_id)
        except (TypeError, ValueError):
            continue
        status_key = str(instance_id)
        has_ability_status = (isinstance(ability_status, dict) and
                              status_key in ability_status)
        applied = (ability_status.get(status_key)
                   if has_ability_status else applied_effects.get(
                       instance_id, applied_effects.get(status_key)))
        if applied is not None and not bool(applied):
            # The native resolver records m_WasApplied=false when an authored
            # effect condition or contingency rejects this mapping. Prefer the
            # per-ability snapshot because the live map is scoped to the last
            # resolver call. C# does not call Apply for a skipped leaf.
            condition_skipped_orders.add(order)
            continue
        condition_guid = str(effect.condition_guid or "").lower()
        if db is None or not condition_guid or condition_guid == "0" * 36:
            continue
        try:
            from pvp_db import db_effect_condition_json
            raw_condition = db_effect_condition_json(condition_guid, conn=db)
            condition = json.loads(raw_condition) if raw_condition else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            condition = {}
        condition_type = str(condition.get("_t") or "").rsplit(".", 1)[-1]
        if not has_ability_status and condition_type in {
                "NotContingentAbilityCondition",
                "NotContingentEffectCondition"}:
            try:
                referenced_instance_id = int(
                    condition.get("m_EffectIndex"))
            except (TypeError, ValueError):
                continue
            activation_instance_ids = tuple(
                traced_instance_ids_by_activation.values())
            if (activation_instance_ids and all(
                    referenced_instance_id in instance_ids
                    for instance_ids in activation_instance_ids)):
                # Nested trigger resolution restores its local m_WasApplied
                # map after returning. The typed trace still proves the
                # referenced mapping applied, so C#'s NotContingent condition
                # rejects this leaf.
                condition_skipped_orders.add(order)
    unexpected_orders = observed_orders & condition_skipped_orders
    if unexpected_orders:
        raise AssertionError(
            f"ability {graph.guid}: resolver traced effects marked not "
            f"applied: {sorted(unexpected_orders)}")
    expected_orders = set(range(len(effects))) - condition_skipped_orders
    missing_orders = expected_orders - observed_orders
    if require_complete and missing_orders:
        missing_text = ", ".join(
            f"{order}:{effects[order].guid}"
            for order in sorted(missing_orders))
        target_map = state.get("_card_matrix_activation_target_map") or {}
        target_conditions = state.get(
            "_card_matrix_target_condition_filters") or {}
        target_details = {}
        if db is not None:
            for order in sorted(missing_orders):
                index = int(effects[order].target_index)
                values = target_map.get(str(index), ())
                if values and index not in target_details:
                    placeholders = ",".join("?" for _ in values)
                    target_details[index] = [dict(zip(
                        ("uid", "name", "location", "user_id"), row))
                        for row in db.execute(
                            "SELECT gc.card_uid,ct.name,gc.location,gc.user_id "
                            "FROM game_cards gc JOIN card_templates ct "
                            "ON ct.guid=gc.template_guid WHERE "
                            "gc.session_id=? AND gc.card_uid IN (" +
                            placeholders + ")",
                            (SESSION_ID, *values)).fetchall()]
        status = ("paused before completing the effect graph; continuation "
                  "requires a focused choice fixture"
                  if state.get("resolution_paused") else
                  "effect trace missing")
        runtime_target_map = state.get("ability_target_map") or {}
        revealed_uids = state.get("revealed_cards") or ()
        revealed_cards = []
        if db is not None and revealed_uids:
            placeholders = ",".join("?" for _ in revealed_uids)
            revealed_cards = [dict(zip(
                ("uid", "name", "location", "owner"), row))
                for row in db.execute(
                    "SELECT gc.card_uid,ct.name,gc.location,gc.user_id "
                    "FROM game_cards gc JOIN card_templates ct "
                    "ON ct.guid=gc.template_guid WHERE gc.session_id=? "
                    "AND gc.card_uid IN (" + placeholders + ")",
                    (SESSION_ID, *[int(uid) for uid in revealed_uids]))]
        trace_summary = [{
            key: entry.get(key) for key in (
                "effect_order", "effect_guid", "target_uids", "targets",
                "result", "error", "ability_instance_id")
        } for entry in traces]
        raise AssertionError(
            f"ability {graph.guid}: {status}: {missing_text}; "
            f"selected_targets={target_details!r}; "
            f"target_condition_filters={target_conditions!r}; "
            f"runtime_target_map={runtime_target_map!r}; "
            f"revealed_cards={revealed_cards!r}; "
            f"traces={trace_summary!r}")
    return len(traces)


def _numeric_target_map(value):
    """Normalize a persisted continuation target map for the resolver."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, selected in value.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        values = selected if isinstance(selected, (list, tuple, set)) else (
            selected,)
        targets = []
        for target in values:
            try:
                targets.append(int(target))
            except (TypeError, ValueError):
                continue
        result[index] = tuple(targets)
    return result


def _invoked_ability_guid(effect):
    """Return the typed child of an authored ActivateAbility effect."""
    if effect.concrete_type not in {
            "ActivateAbilityEffectTemplate",
            "ActivatePowerAbilityEffectTemplate"}:
        return ""
    return _guid(_field(effect.template, "m_AbilityToInvoke", ""))


def _effect_condition_requires_played_from_hand(graph):
    """Whether a leaf condition reads the parent cast's PlayedFromHand TAC."""
    from gamedata import DEFAULT_RECORD_STORE

    for effect in graph.effects:
        condition_guid = str(effect.condition_guid or "").lower()
        if not condition_guid or condition_guid == "0" * 36:
            continue
        record = DEFAULT_RECORD_STORE.get(
            "AbilityEffectConditionTemplate", condition_guid)
        condition = getattr(record, "condition", None) if record else None
        for node in _typed_nodes(condition, "IntAttrFilter"):
            parts = [part.strip().lower() for part in
                     str(node.get("m_Attribute") or "").split(">")]
            if (len(parts) > 1 and parts[0] == "abilitytac" and
                    parts[-1] == "playedfromhand"):
                return True
    return False


def _condition_for_effect(effect):
    from gamedata import DEFAULT_RECORD_STORE

    condition_guid = str(effect.condition_guid or "").lower()
    if not condition_guid or condition_guid == "0" * 36:
        return None
    record = DEFAULT_RECORD_STORE.get(
        "AbilityEffectConditionTemplate", condition_guid)
    return getattr(record, "condition", None) if record else None


def _record_dict(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


def _random_condition_outcome(value, variable_name, variables):
    """Evaluate typed AbilityVariableCondition logic for one random value."""
    value = _record_dict(value)
    if not isinstance(value, dict):
        return False, None
    kind = str(value.get("_t", "")).rsplit(".", 1)[-1]
    if kind == "AbilityVariableCondition":
        lhs_name = str(value.get("m_Lhs", "") or "")
        rhs_name = str(value.get("m_Rhs", "") or "")
        depends = variable_name in {lhs_name, rhs_name}
        if not depends:
            return False, None

        def resolve(operand):
            if operand in variables:
                return int(variables[operand])
            try:
                return int(operand)
            except (TypeError, ValueError):
                return None

        lhs = resolve(lhs_name)
        rhs = resolve(rhs_name)
        if lhs is None or rhs is None:
            return True, None
        operation = str(value.get("m_ComparisonOp", "") or "")
        comparisons = {
            "LessThan": lhs < rhs,
            "LessThanOrEqual": lhs <= rhs,
            "GreaterThan": lhs > rhs,
            "GreaterThanOrEqual": lhs >= rhs,
            "Equals": lhs == rhs,
            "NotEqual": lhs != rhs,
            "OneMoreThan": lhs == rhs + 1,
            "TwoMoreThan": lhs == rhs + 2,
            "OneLessThan": lhs == rhs - 1,
        }
        if operation not in comparisons:
            return True, None
        return True, comparisons[operation]

    if kind == "NotEffectCondition":
        depends, outcome = _random_condition_outcome(
            value.get("m_Condition"), variable_name, variables)
        return depends, None if outcome is None else not outcome

    if kind in {"AndEffectCondition", "OrEffectCondition"}:
        children = value.get("m_Conditions") or ()
        results = [_random_condition_outcome(
            child, variable_name, variables) for child in children]
        depends = any(item[0] for item in results)
        outcomes = [item[1] for item in results]
        if kind == "AndEffectCondition":
            if False in outcomes:
                return depends, False
            if outcomes and all(outcome is True for outcome in outcomes):
                return depends, True
        else:
            if True in outcomes:
                return depends, True
            if outcomes and all(outcome is False for outcome in outcomes):
                return depends, False
        return depends, None

    return False, None


def _condition_random_thresholds(value, variable_name, variables):
    """Read literal comparison pivots from typed variable-condition leaves."""
    value = _record_dict(value)
    if isinstance(value, dict):
        kind = str(value.get("_t", "")).rsplit(".", 1)[-1]
        if kind == "AbilityVariableCondition":
            lhs = str(value.get("m_Lhs", "") or "")
            rhs = str(value.get("m_Rhs", "") or "")
            if variable_name in {lhs, rhs}:
                other = rhs if lhs == variable_name else lhs
                if other in variables:
                    return {int(variables[other])}
                try:
                    return {int(other)}
                except (TypeError, ValueError):
                    return set()
        thresholds = set()
        for child in value.values():
            thresholds.update(_condition_random_thresholds(
                child, variable_name, variables))
        return thresholds
    if isinstance(value, (list, tuple)):
        thresholds = set()
        for child in value:
            thresholds.update(_condition_random_thresholds(
                child, variable_name, variables))
        return thresholds
    return set()


def _graph_ability_constants(graph):
    values = {}
    for variable in graph.variables:
        data = _record_dict(variable) or {}
        if (str(data.get("_t", "")).rsplit(".", 1)[-1]
                != "AbilityConstant"):
            continue
        name = str(data.get("m_Name", "") or "")
        try:
            values[name] = int(data.get("m_DefaultValue"))
        except (TypeError, ValueError):
            continue
    return values


def _random_variable_condition_outcomes(graph, variable_name, value):
    """Return each effect mapping's branch state for one random value."""
    variables = _graph_ability_constants(graph)
    variables[str(variable_name)] = int(value)
    outcomes = []
    for effect in _ordered_contract_effects(graph):
        condition = _condition_for_effect(effect)
        if condition is None:
            outcomes.append(True)
            continue
        depends, outcome = _random_condition_outcome(
            condition, str(variable_name), variables)
        if depends and outcome is None:
            raise AssertionError(
                f"ability {graph.guid}: cannot evaluate typed random "
                f"condition at {variable_name}={value}")
        outcomes.append(outcome if depends else None)
    return tuple(outcomes)


def _randomized_variable_contract_cases(graph):
    """Derive representative RNG outcomes for every typed condition branch."""
    effects = _ordered_contract_effects(graph)
    randomizers = [effect for effect in effects
                   if str(effect.concrete_type).startswith(
                       "RandomizeVariable")]
    if not randomizers:
        return None
    random_names = {
        str(_field(effect.template, "m_VariableName", "RandomNumber") or
            "RandomNumber") for effect in randomizers}
    conditions = [_condition_for_effect(effect) for effect in effects]
    referenced = set()
    for condition in conditions:
        for name in random_names:
            depends, _ = _random_condition_outcome(condition, name, {})
            if depends:
                referenced.add(name)
    if not referenced:
        return None
    if len(randomizers) != 1 or len(referenced) != 1:
        raise AssertionError(
            f"ability {graph.guid}: random branch contract needs one "
            "randomized variable; typed graph has multiple randomizers")

    randomizer = randomizers[0]
    variable_name = next(iter(referenced))
    template = _record_dict(randomizer.template) or {}
    if template.get("m_MaxValueField"):
        raise AssertionError(
            f"ability {graph.guid}: random branch contract needs a staged "
            "dynamic MaxValueField")
    try:
        minimum = int(template.get("m_MinValue", 1))
        maximum = int(template.get("m_MaxValue", minimum))
    except (TypeError, ValueError):
        raise AssertionError(
            f"ability {graph.guid}: random bounds are not fixed integers")
    if minimum > maximum:
        minimum, maximum = maximum, minimum

    variables = _graph_ability_constants(graph)
    variables[variable_name] = minimum

    if (bool(template.get("m_SecondValue")) and
            any(_random_condition_outcome(condition, variable_name + "2",
                                         variables)[0]
                for condition in conditions)):
        raise AssertionError(
            f"ability {graph.guid}: random branch contract needs paired "
            "SecondValue outcomes")

    thresholds = set()
    for condition in conditions:
        thresholds.update(_condition_random_thresholds(
            condition, variable_name, variables))
    candidates = {minimum, maximum}
    for threshold in thresholds:
        candidates.update(threshold + delta for delta in range(-2, 3))
    candidates = sorted(value for value in candidates
                        if minimum <= value <= maximum)

    cases = {}
    for desired in candidates:
        condition_outcomes = _random_variable_condition_outcomes(
            graph, variable_name, desired)
        signature = condition_outcomes
        if signature in cases:
            continue
        offset = desired - minimum
        cases[signature] = {
            "value": desired,
            "rng_seed": (0, offset << 16),
            "condition_outcomes": tuple(condition_outcomes),
        }

    if len(cases) < 2:
        return None
    return tuple(cases.values())


def _static_effect_source_zone(graph):
    """Choose an authored source zone required by a static effect gate."""
    for effect in graph.effects:
        condition = _condition_for_effect(effect)
        for requirement in _typed_nodes(
                condition, "RequiresSourcePassesFilterCondition"):
            zones = list(_filter_nodes(
                requirement.get("m_Filter"), "InZone"))
            if zones:
                zone = str(zones[0].get("m_Collection", "") or "").lower()
                return {"crypt": "discard"}.get(zone, zone)
    return ""


def _stage_champion_health_conditions(state, graph, owner):
    """Set fixture health to satisfy typed champion-health conditions."""
    conditions = [graph.trigger_condition]
    conditions.extend(_condition_for_effect(effect) for effect in graph.effects)
    side = "ai" if int(owner) == AI_OWNER else "player"
    for condition in conditions:
        for node in _typed_nodes(condition, "RequiresChampionHealth"):
            try:
                required = int(node.get("m_RequiredQuantity", 0) or 0)
            except (TypeError, ValueError):
                required = 0
            operation = str(node.get("m_ComparisonOp") or
                            "GreaterThanOrEqual")
            if operation == "GreaterThanOrEqual":
                value = required
            elif operation == "GreaterThan":
                value = required + 1
            elif operation == "LessThanOrEqual":
                value = min(required, 10)
            elif operation == "LessThan":
                value = required - 1
            elif operation == "NotEqual":
                value = required + 1
            elif operation == "OneMoreThan":
                value = required + 1
            elif operation == "TwoMoreThan":
                value = required + 2
            elif operation == "OneLessThan":
                value = required - 1
            else:
                value = required
            state[f"{side}_health"] = int(value)


def _stage_source_keyword_conditions(db, graph, source_uid):
    """Seed valid attribute flags for authored source-keyword count gates."""
    row = db.execute(
        "SELECT COALESCE(gc.card_attributes,0), "
        "COALESCE(ct.attributes,0), gc.permanent_buffs, gc.temporary_buffs "
        "FROM game_cards gc JOIN card_templates ct "
        "ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (SESSION_ID, int(source_uid))).fetchone()
    if row is None:
        return
    int_attrs = {}
    for payload in row[2:]:
        try:
            data = json.loads(payload or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and isinstance(data.get("int_attrs"), dict):
            int_attrs.update(data["int_attrs"])
    bits = int(row[0] or 0) | int(row[1] or 0)
    keyword_count = bits.bit_count()
    keyword_count += int(int_attrs.get("Rage", int_attrs.get("rage", 0)) or 0) > 0
    keyword_count += bool(int_attrs.get("Lethal", int_attrs.get("lethal", 0)))
    requested = max((int(node.get("m_RequiredQuantity", 0) or 0)
                     for effect in graph.effects
                     for node in _typed_nodes(
                         _condition_for_effect(effect),
                         "SourceCardHasKeywords")), default=0)
    missing = max(0, requested - keyword_count)
    if not missing:
        return
    available = []
    for name, raw_value in vars(game_engine.ECardAttributes).items():
        if name.startswith("_"):
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value & (value - 1) == 0 and not bits & value:
            available.append(value)
    for value in available[:missing]:
        bits |= value
    db.execute(
        "UPDATE game_cards SET card_attributes=? WHERE session_id=? "
        "AND card_uid=?", (bits, SESSION_ID, int(source_uid)))
    db.commit()


def _stage_source_filter_conditions(db, graph, source_uid):
    """Seed positive typed filters that C# evaluates on the ability source."""
    conditions = [graph.trigger_condition]
    conditions.extend(_condition_for_effect(effect)
                      for effect in graph.effects)
    filters = []
    for condition in conditions:
        for node in _typed_nodes(condition, "CardFilterAbilityCondition"):
            filters.append(node.get("m_CardFilter") or {})
        for node in _typed_nodes(
                condition, "RequiresSourcePassesFilterCondition"):
            filters.append(node.get("m_Filter") or {})
    for filter_spec in filters:
        _seed_filter_int_attributes(db, source_uid, filter_spec)
        _stage_filter_numeric_stats(db, source_uid, filter_spec)
        _stage_card_states_for_filters(db, source_uid, [filter_spec])
        _stage_socketed_gem_witness(
            db, source_uid, [filter_spec], source_uid=source_uid)
        cost_nodes = list(_filter_nodes(filter_spec, "HasResourceCost"))
        if not cost_nodes:
            continue
        from rules_port.static_rules import effective_cost
        for node in cost_nodes:
            if any(node.get(key) for key in (
                    "m_AddX", "m_AddCardIntegerVariable",
                    "m_AddSumListAttrName", "m_AddVariable",
                    "m_AddAttack", "m_AddDefense")):
                continue
            try:
                required = int(node.get("m_ResourceCost", 0) or 0)
                current = int(effective_cost(
                    db, SESSION_ID, {}, int(source_uid)))
            except (TypeError, ValueError, RuntimeError):
                continue
            operation = str(node.get("m_ComparisonOp") or "")
            if operation == "LessThan":
                desired = max(0, min(current, required - 1))
            elif operation == "LessThanOrEqual":
                desired = max(0, min(current, required))
            elif operation == "GreaterThan":
                desired = max(current, required + 1)
            elif operation == "GreaterThanOrEqual":
                desired = max(current, required)
            elif operation == "Equals":
                desired = required
            elif operation == "NotEqual":
                desired = current if current != required else required + 1
            else:
                continue
            delta = int(desired) - int(current)
            if delta:
                db.execute(
                    "UPDATE game_cards SET card_cost_mod="
                    "COALESCE(card_cost_mod,0)+? WHERE session_id=? "
                    "AND card_uid=?", (delta, SESSION_ID, int(source_uid)))
                db.commit()


def _stage_filter_numeric_stats(db, card_uid, filter_spec):
    """Set fixture stats to satisfy authored attack/defense predicates."""
    row = db.execute(
        "SELECT ct.attack,ct.defense,COALESCE(gc.card_attack_mod,0),"
        "COALESCE(gc.card_defense_mod,0) FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (SESSION_ID, int(card_uid))).fetchone()
    if row is None:
        return
    bases = {"attack": int(row[0] or 0), "defense": int(row[1] or 0)}
    mods = {"attack": int(row[2] or 0), "defense": int(row[3] or 0)}
    for filter_name, stat in (("HasAttackValue", "attack"),
                              ("HasDefenseValue", "defense")):
        for node in _filter_nodes(filter_spec, filter_name):
            # Dynamic comparisons are resolved by the live evaluator from
            # their source/variable inputs. Only stage literal comparisons.
            if any(node.get(key) for key in (
                    "m_CompareToSource", "m_CompareToTriggerSource",
                    "m_CompareToStoredTarget",
                    "m_CompareToCardIntegerVariable",
                    "m_CompareToSourceControlledCardFilterCount")):
                continue
            try:
                required = int(node.get("m_RequiredQuantity",
                                        node.get(
                                            "m_AttackValue" if stat ==
                                            "attack" else "m_DefenseValue",
                                            node.get("m_Value", 0))) or 0)
            except (TypeError, ValueError):
                continue
            operation = str(node.get("m_ComparisonOp") or "Equals")
            current = bases[stat] + mods[stat]
            if operation == "LessThan":
                desired = min(current, required - 1)
            elif operation == "LessThanOrEqual":
                desired = min(current, required)
            elif operation == "GreaterThan":
                desired = max(current, required + 1)
            elif operation == "GreaterThanOrEqual":
                desired = max(current, required)
            elif operation == "Equals":
                desired = required
            elif operation == "NotEqual":
                desired = current if current != required else required + 1
            else:
                continue
            mods[stat] += desired - current
    db.execute(
        "UPDATE game_cards SET card_attack_mod=?,card_defense_mod=? "
        "WHERE session_id=? AND card_uid=?",
        (mods["attack"], mods["defense"], SESSION_ID, int(card_uid)))
    db.commit()


def _cardblocked_resource_branches(graph):
    """Derive mutually exclusive target/contingency paths from Records."""
    if str(graph.trigger_event_type or "").rsplit(".", 1)[-1] != \
            "CardBlockedEvent":
        return None
    resource_stores = []
    noncontingent_by_instance = {}
    for effect in graph.effects:
        condition = _condition_for_effect(effect)
        if effect.concrete_type == "StoreTargetsAbilityEffectTemplate":
            for node in _typed_nodes(
                    condition, "RequiresTargetPassesFilterCondition"):
                if list(_filter_nodes(node.get("m_Filter"), "IsResource")):
                    resource_stores.append(effect)
                    break
        for node in _typed_nodes(
                condition, "NotContingentAbilityCondition"):
            try:
                referenced = int(node.get("m_EffectIndex"))
            except (TypeError, ValueError):
                continue
            noncontingent_by_instance.setdefault(referenced, set()).add(
                str(effect.guid).lower())
    resource_effects = set()
    fallback_effects = set()
    for store in resource_stores:
        try:
            instance_id = int(store.effect_instance_id)
        except (TypeError, ValueError):
            continue
        resource_effects.add(str(store.guid).lower())
        for effect in graph.effects:
            try:
                contingent_id = int(effect.contingent_effect_instance_id)
            except (TypeError, ValueError):
                continue
            if contingent_id == instance_id:
                resource_effects.add(str(effect.guid).lower())
        fallback_effects.update(noncontingent_by_instance.get(
            instance_id, ()))
    if not resource_effects or not fallback_effects:
        return None
    all_effects = {str(effect.guid).lower() for effect in graph.effects}
    always_effects = all_effects - resource_effects - fallback_effects
    return always_effects, resource_effects, fallback_effects


def _start_ability_contract(db, card_guid, graph, *,
                            cardblocked_resource_branch=None,
                            rng_seed=None, revealed_target_branch=None):
    """Build and resolve the initial leg of one typed graph contract."""
    metadata = _seed_board(db, card_guid, contract_support=True)
    if cardblocked_resource_branch is not None:
        # Exercise both typed outcomes of a reveal/filter/contingency graph.
        # Position zero is the client-visible top of this generated deck.
        top_uid = _raw_uid(
            CARD_UID_BASE + (30 if cardblocked_resource_branch else 9))
        db.execute(
            "UPDATE game_cards SET position=CASE WHEN card_uid=? THEN 0 "
            "ELSE position+100 END WHERE session_id=? AND location='deck'",
            (top_uid, SESSION_ID))
        db.commit()
    # Choice abilities can begin by moving stale generated options out of
    # Choosing. A completely empty fixture makes that authored
    # cleanup a silent no-op, so the effect cannot leave a typed trace.  Seed
    # one inert Choice card only for graphs that explicitly contain that
    # metadata-named cleanup effect; this tests the real zone transition and
    # does not invent a card rule for unrelated graphs.
    if any("choicezone" in str(effect.name or "").lower()
           for effect in graph.effects):
        choice_template = _template_guid(
            db, "Choice", require_empty_abilities=True)
        _insert_card(
            db, _raw_uid(CARD_UID_BASE + 40), PLAYER_OWNER, "choosing",
            choice_template, 40, abilities=[])
        # Leave a real non-resource/resource candidate below the top of the
        # deck so Seek Adventure/Seek Fortune prove a position change rather
        # than accidentally selecting an already-top card.
        db.execute(
            "UPDATE game_cards SET position=10 WHERE session_id=? "
            "AND card_uid=?", (SESSION_ID, _raw_uid(CARD_UID_BASE + 9)))
        db.commit()
    handler = _AbilityContractHandlerStub(db)
    session = SessionStub()
    state = _new_state(handler, trace=True)
    state["_card_matrix_contract_fixture"] = True
    fixture_champion = db.execute(
        "SELECT champion.guid FROM champion_templates_extended champion "
        "WHERE COALESCE(TRIM(champion.champion_class),'')<>'' AND "
        "EXISTS (SELECT 1 FROM card_templates troop WHERE "
        "lower(troop.card_type)='troop' AND instr(' ' || "
        "lower(replace(COALESCE(troop.subtype,''),'-',' ')) || ' ', "
        "' ' || lower(TRIM(champion.champion_class)) || ' ')>0) "
        "ORDER BY champion.guid LIMIT 1").fetchone()
    if fixture_champion is None:
        fixture_champion = db.execute(
            "SELECT guid FROM champion_templates_extended WHERE "
            "COALESCE(TRIM(champion_class),'')<>'' ORDER BY guid LIMIT 1"
        ).fetchone()
    if fixture_champion is not None:
        state["champ_guid_map"] = {
            str(PLAYER_OWNER): str(fixture_champion[0]).lower(),
            str(AI_OWNER): str(fixture_champion[0]).lower(),
        }
    _stage_child_revealed_target_witnesses(db, graph, state)
    contract_x_cost = _contract_x_cost(graph, metadata)
    if contract_x_cost is not None:
        # Candidate seeding and target calculation both evaluate authored
        # HasResourceCost(... + X) filters. Stage the selected activation X
        # before either asks the typed target evaluator for legal cards.
        state["x_cost"] = int(contract_x_cost)
    if rng_seed is not None:
        state["_rules_rng"] = MultiplyWithCarryRng(*rng_seed)
    _stage_resource_condition_witnesses(
        db, graph, PLAYER_OWNER, state)
    _stage_champion_health_conditions(state, graph, PLAYER_OWNER)
    handler._current_bstate = state
    game = handler._fresh_game(session, PLAYER_UID, AI_UID, state)
    source_uid = metadata["source_uid"]
    source_location = ("PlayedResources" if _is_resource(metadata["card_type"])
                       else "CastSpells")
    if str(graph.trigger_event_type or "").rsplit(".", 1)[-1] == \
            "CardDrawnEvent":
        metadata["event_target_owner"] = _draw_event_owner(
            graph, int(PLAYER_OWNER))
    event = _event_for_graph(graph, metadata, state)
    if event.event_type == "CardCastEvent":
        _stage_trigger_target(db, graph, metadata, location="CastSpells")
        event = _event_for_graph(graph, metadata, state)
    elif event.event_type == "CardDrawnEvent":
        target_filter = next((node.get("m_CardFilter")
                              for node in _typed_nodes(
                                  graph.trigger_condition,
                                  "TriggerCardMatchesFilter")
                              if str(node.get("m_TriggerTest") or "")
                              .rsplit(".", 1)[-1].lower() == "triggertarget"),
                             None)
        # "When you draw this" graphs identify the target by source identity;
        # other draw triggers need a real matching card in Hand and an actual
        # CardDrawnEvent so SourceDrawnTargetTemplate resolves as C# does.
        if (target_filter and
                "IsAbilitySource" not in _record_type_names(target_filter)):
            _stage_trigger_target(db, graph, metadata, location="hand")
        elif target_filter:
            metadata["event_target_uid"] = int(source_uid)
            metadata["event_target_owner"] = int(PLAYER_OWNER)
        elif not target_filter:
            # CardDrawnEvent always carries both player IDs even when the
            # ability has no drawn-card filter. Use a hand card controlled by
            # the same participant as the source/target player fields.
            target_owner = int(metadata["event_target_owner"])
            target_uid = (metadata["filler_ai"] if target_owner == AI_OWNER
                          else metadata["filler_player"])
            db.execute(
                "UPDATE game_cards SET location='hand' WHERE session_id=? "
                "AND card_uid=?", (SESSION_ID, int(target_uid)))
            db.commit()
            metadata["event_target_uid"] = int(target_uid)
        event = _event_for_graph(graph, metadata, state)
    elif event.event_type == "CardDiscardedEvent":
        controls_source = bool(list(_typed_nodes(
            graph.trigger_condition, "TriggerPlayerControlsAbilitySource")))
        has_source_control_test = (
            "TriggerPlayerControlsAbilitySource" in
            _record_type_names(graph.trigger_condition))
        discarded_owner = (int(PLAYER_OWNER) if
                           controls_source or not has_source_control_test else
                           int(AI_OWNER))
        metadata["event_target_owner"] = discarded_owner
        target_filter = next((node.get("m_CardFilter")
                              for node in _typed_nodes(
                                  graph.trigger_condition,
                                  "TriggerCardMatchesFilter")
                              if str(node.get("m_TriggerTest") or "")
                              .rsplit(".", 1)[-1].lower() == "triggertarget"),
                             None)
        target_is_source = bool(list(_filter_nodes(
            target_filter, "IsAbilitySource")))
        if target_is_source:
            # "When you discard this" uses the discarded source itself as
            # TriggerTarget. The event player is therefore its controller,
            # and both the hand-to-discard move and trigger lookup must use
            # that same generated instance.
            metadata["event_target_uid"] = int(source_uid)
            metadata["event_target_owner"] = int(PLAYER_OWNER)
        else:
            _stage_trigger_target(db, graph, metadata, location="hand")
        target_uid = metadata.get("event_target_uid")
        if target_uid is None:
            target_uid = int(metadata["filler_ai"])
            db.execute(
                "UPDATE game_cards SET location='hand' WHERE session_id=? "
                "AND card_uid=?", (SESSION_ID, target_uid))
            metadata["event_target_uid"] = target_uid
        # Give list-sum contracts a non-zero event-card value while retaining
        # every authored template predicate on the discarded card.
        type_nodes = list(_filter_nodes(target_filter, "IsType"))
        if target_is_source:
            matching_templates = []
        elif (type_nodes and
                _record_type_names(target_filter) <= {"IsType"}):
            accepted_types = sorted({
                kind.strip().rsplit(".", 1)[-1].lower()
                for node in type_nodes
                for kind in str(node.get("m_CardType") or "").split("|")
                if kind.strip()})
            marks = ",".join("?" for _ in accepted_types)
            matching_templates = (db.execute(
                "SELECT guid FROM card_templates WHERE "
                "COALESCE(cost,0)>0 AND lower(card_type) IN (" + marks + ") "
                "ORDER BY cost DESC,guid", accepted_types).fetchall()
                if accepted_types else [])
        else:
            matching_templates = _filter_witness_template_rows(
                db, [target_filter], db.execute(
                    "SELECT guid FROM card_templates WHERE "
                    "COALESCE(cost,0)>0 ORDER BY cost DESC,guid").fetchall())
        for candidate_guid, in matching_templates:
            candidate = DEFAULT_RECORD_STORE.get(
                "CardTemplate", str(candidate_guid).lower())
            type_only = bool(type_nodes and target_filter and
                             _record_type_names(target_filter) <= {"IsType"})
            if candidate is None or (target_filter and not type_only and
                    not _template_filter_matches_with_runtime_witnesses(
                        target_filter, candidate)):
                continue
            existing = db.execute(
                "SELECT user_id,position FROM game_cards WHERE "
                "session_id=? AND card_uid=?",
                (SESSION_ID, int(target_uid))).fetchone()
            if existing:
                db.execute(
                    "DELETE FROM game_cards WHERE session_id=? AND card_uid=?",
                    (SESSION_ID, int(target_uid)))
                _insert_card(
                    db, int(target_uid), int(existing[0]), "hand",
                    str(candidate_guid), int(existing[1]), abilities=[])
                db.commit()
            break
        event = _event_for_graph(graph, metadata, state)
    stage_blocked_trigger = event.event_type == "CardBlockedEvent"
    if (event.event_type != "CardCreatedEvent" and
            not stage_blocked_trigger):
        _announce_card(game, handler, db, source_uid, PLAYER_OWNER,
                       source_location)

    stage_lifecycle_trigger = event.event_type in {
        "GameStartedEvent", "CardCreatedEvent"}
    stage_card_draw_trigger = (
        event.event_type == "CardDrawnEvent" and
        metadata.get("event_target_uid") is not None)
    zone_trigger_shape = (
        _authored_zone_trigger_shape(graph)
        if event.event_type == "CardEnteredZoneEvent" else None)
    stage_turn_start_trigger = (
        event.event_type == "TurnStartedEvent" and
        not any(target.requires_input for target in graph.targets))
    stage_combat_trigger = event.event_type == "CardBlockedEvent"
    stage_card_discarded_trigger = (
        event.event_type == "CardDiscardedEvent" and
        metadata.get("event_target_uid") is not None)
    if event.event_type == "CardEnteredZoneEvent":
        destination = (zone_trigger_shape["source_zone"]
                       if zone_trigger_shape else
                       _trigger_zone(graph.trigger_collection_flags))
        db.execute(
            "UPDATE game_cards SET location=? WHERE session_id=? "
            "AND card_uid=?", (destination, SESSION_ID, source_uid))
        db.commit()
        _announce_card(game, handler, db, source_uid, PLAYER_OWNER,
                       destination)
    elif event.event_type == "TurnStartedEvent":
        # Start-turn abilities can be registered from Void or another
        # non-warzone collection. Stage the source at the positive
        # CardFilterAbilityCondition zone, then dispatch the actual native
        # event instead of resolving the ability as a direct action.
        trigger_source_zone = (
            _source_zone_from_condition(graph) or
            _trigger_zone(graph.trigger_collection_flags))
        db.execute(
            "UPDATE game_cards SET location=? WHERE session_id=? "
            "AND card_uid=?", (trigger_source_zone, SESSION_ID, source_uid))
        db.commit()
        _announce_card(game, handler, db, source_uid, PLAYER_OWNER,
                       trigger_source_zone)
    elif stage_blocked_trigger:
        # The C# event is emitted after the block relation and Blocking state
        # have been recorded. BlockingFilter reads this combat map when it
        # builds the authored "troops blocking this" target set.
        blocker_uid = int(event.source_card_id)
        attacker_uid = int(event.target_card_id)
        trigger_source_zone = (
            _source_zone_from_condition(graph) or
            _trigger_zone(graph.trigger_collection_flags))
        db.execute(
            "UPDATE game_cards SET location=? WHERE session_id=? "
            "AND card_uid=?",
            (trigger_source_zone, SESSION_ID, int(source_uid)))
        db.execute(
            "UPDATE game_cards SET location=?, card_state="
            "(COALESCE(card_state,0) | ?) WHERE session_id=? AND card_uid=?",
            ("warzone", int(game_engine.ECardStates.Attacking),
             SESSION_ID, attacker_uid))
        db.execute(
            "UPDATE game_cards SET location=?, "
            "card_state=(COALESCE(card_state,0) | ?) "
            "WHERE session_id=? AND card_uid=?",
            ("warzone", int(game_engine.ECardStates.Blocking |
                             game_engine.ECardStates.HasBlocked),
             SESSION_ID, blocker_uid))
        db.commit()
        state["blockers"] = {str(attacker_uid): [blocker_uid]}
        _announce_card(game, handler, db, source_uid, PLAYER_OWNER,
                       trigger_source_zone)
        _announce_card(game, handler, db, blocker_uid,
                       int(event.source_player_id), "warzone")
        _announce_card(game, handler, db, attacker_uid,
                       int(event.target_player_id), "warzone")
    elif stage_card_discarded_trigger:
        trigger_source_zone = (
            _source_zone_from_condition(graph) or
            _trigger_zone(graph.trigger_collection_flags) or "warzone")
        db.execute(
            "UPDATE game_cards SET location=? WHERE session_id=? "
            "AND card_uid=?", (trigger_source_zone, SESSION_ID, source_uid))
        db.execute(
            "UPDATE game_cards SET location='discard' WHERE session_id=? "
            "AND card_uid=?", (SESSION_ID, int(event.target_card_id)))
        db.commit()
        _announce_card(game, handler, db, source_uid, PLAYER_OWNER,
                       trigger_source_zone)
        _announce_card(game, handler, db, int(event.target_card_id),
                       int(event.target_player_id or AI_OWNER), "discard")
    elif stage_lifecycle_trigger:
        trigger_source_zone = (
            _source_zone_from_condition(graph) or
            ("hand" if event.event_type == "GameStartedEvent" and
             "hand" in str(graph.trigger_collection_flags or "").lower().split("|")
             else "none" if event.event_type == "CardCreatedEvent" else
             _trigger_zone(graph.trigger_collection_flags)))
        db.execute(
            "UPDATE game_cards SET location=? WHERE session_id=? "
            "AND card_uid=?", (trigger_source_zone, SESSION_ID, source_uid))
        db.commit()
        if trigger_source_zone != "none":
            _announce_card(game, handler, db, source_uid, PLAYER_OWNER,
                           trigger_source_zone)
    elif graph.trigger_event_type:
        # TriggerCollectionFlags determines where the client has registered
        # this ability. A positive CardFilterAbilityCondition may narrow that
        # set further; deck abilities such as "when you play a Plant" need to
        # remain in Deck while the event is dispatched.
        trigger_source_zone = (
            _source_zone_from_condition(graph) or
            _trigger_zone(graph.trigger_collection_flags) or "warzone")
        db.execute(
            "UPDATE game_cards SET location=? WHERE session_id=? "
            "AND card_uid=?", (trigger_source_zone, SESSION_ID, source_uid))
        db.commit()
        _announce_card(game, handler, db, source_uid, PLAYER_OWNER,
                       trigger_source_zone)
    state["resolving_source_uid"] = source_uid
    state["resolving_owner_id"] = PLAYER_OWNER
    state["resolving_trigger_event_type"] = event.event_type
    state["resolving_trigger_source_uid"] = event.source_card_id
    state["resolving_trigger_target_uid"] = event.target_card_id
    state["resolving_trigger_event_data"] = dict(event.data or {})
    if event.event_type == "GainThresholdEvent":
        state["gain_threshold_color"] = event.data["gain_threshold_color"]
    if event.event_type == "CardDealtDamageEvent":
        # IntAttrAbilityVariable(DamageDealt) reads the active ability
        # instance, just as the live damage resolver populates it before
        # dispatching CardDealtDamageEvent.
        state["ability_runtime_state"] = {"1": {"DamageDealt": 3}}

    _stage_card_count_variable_inputs(
        db, graph, source_uid, PLAYER_OWNER, state)
    _capture_dynamic_summon_amounts(
        db, graph, source_uid, PLAYER_OWNER, state)
    _seed_source_counter_conditions(db, graph, source_uid)
    _seed_contract_counter_costs(db, graph, source_uid)
    counter_cost_changes = _pay_contract_counter_costs(
        db, graph, source_uid, state)
    if counter_cost_changes:
        from rules_port.context import EffectContext
        from rules_port.counter_effects import project_counter_cost_change
        cost_context = EffectContext.from_rules_port(
            game, session, db, handler, PLAYER_UID, AI_UID, state,
            "counter_cost_payment")
        for change in counter_cost_changes:
            project_counter_cost_change(
                cost_context, source_uid, change["counter_guid"],
                change["old"], change["new"])
    _stage_source_top_of_deck_filters(db, graph, source_uid)
    _stage_source_keyword_conditions(db, graph, source_uid)
    _stage_source_filter_conditions(db, graph, source_uid)
    _stage_stored_name_target_filters(
        db, graph, source_uid, PLAYER_OWNER, state, handler)
    _stage_stored_card_target_filters(
        db, graph, source_uid, PLAYER_OWNER, state, handler)
    cost_target_specs = _additional_cost_target_specs(graph)
    _seed_target_filter_candidates(
        db, graph, source_uid, PLAYER_OWNER, state, handler,
        extra_targets=cost_target_specs)
    if revealed_target_branch is not None:
        _stage_top_revealed_target_branch(
            db, graph, int(revealed_target_branch), PLAYER_OWNER)

    activation = _activation_map(
        db, [graph], source_uid, PLAYER_OWNER, state, handler, metadata
    ).get(graph.guid.lower(), ActivationData())
    # The native resolver reads AbilityResourceXCost from the active
    # ability state.  Carry the value chosen by this generated activation into
    # effect fields and list-target filters, including triggered graphs on a
    # card whose parent spell was played for X.
    state["x_cost"] = int(activation.x_cost or 0)
    _stage_contract_cost_payment(
        db, graph, source_uid, activation, state)
    state["_card_matrix_activation_target_map"] = {
        str(index): tuple(int(uid) for uid in values)
        for index, values in activation.target_map.items()}
    state["_card_matrix_target_condition_filters"] = {
        str(index): _contract_target_condition_filters(graph, index)
        for index in range(len(graph.targets))}
    _stage_effect_condition_cards(
        db, graph, PLAYER_OWNER, source_uid=source_uid,
        target_map=activation.target_map,
        handler=handler)
    if zone_trigger_shape:
        state["resolving_trigger_event_type"] = "CardEnteredZoneEvent"
        _stage_authored_zone_trigger(
            db, graph, zone_trigger_shape, source_uid, PLAYER_OWNER,
            state, handler, game, session)
    elif (stage_turn_start_trigger or stage_lifecycle_trigger or
          stage_combat_trigger or stage_card_draw_trigger or
          stage_card_discarded_trigger):
        from unittest import mock
        if stage_card_draw_trigger and event.target_card_id is not None:
            # Session.CreateAbility appends CardDrawnEvent.tac, which carries
            # ListAttrs.DrawnCards, to the triggered AbilityInstance. Mirror
            # that list so SourceDrawnTargetTemplate sees the actual drawn
            # card during this native resolution.
            state.setdefault("ability_lists", {})["DrawnCards"] = [
                int(event.target_card_id)]
        with mock.patch("rules_port.triggers.random.randrange",
                        return_value=0):
            dispatch_result = dispatch_native_trigger(
                db=db, handler=handler, game=game, session=session,
                player_uid=PLAYER_UID, ai_uid=AI_UID, battle_state=state,
                event_type=event.event_type,
                source_card_id=event.source_card_id,
                source_player_id=event.source_player_id,
                target_card_id=event.target_card_id,
                target_player_id=event.target_player_id, data=event.data)
        pending_before_drain = len(state.get("stack") or ())
        _drain_triggers(handler, game, session, db, state)
        if not any(
                str(entry.get("ability_guid") or "").lower()
                == str(graph.guid).lower()
                for entry in state.get("ability_trace") or ()):
            # A trigger can resolve successfully without applying a leaf.
            # C# records that outcome on the effect instance, but emits no
            # Apply trace when its authored effect condition rejects the
            # mapping. Let the typed graph contract distinguish that valid
            # no-op from an unexplained missing resolution trace.
            try:
                _assert_graph_trace(graph, state, db=db)
            except AssertionError as trace_error:
                condition_diagnostics = {"trace_contract": str(trace_error)}
            else:
                condition_diagnostics = None
            if condition_diagnostics is not None:
                candidate_sources = ()
                try:
                    from rules_port.condition_context import ConditionContext
                    from rules_port.conditions import (
                        evaluate_condition, trigger_condition_met)
                    context = ConditionContext(
                        db, session, state, event_type=event.event_type,
                        ability_source_uid=source_uid,
                        ability_source_owner_id=PLAYER_OWNER,
                        trigger_uid=event.source_card_id,
                        extra_target=event.target_card_id,
                        trigger_owner_id=event.source_player_id,
                        event_target_owner_id=event.target_player_id,
                        event_data=event.data,
                        champions=handler._champion_targets())
                    raw_condition = graph.trigger_condition
                    condition_diagnostics["graph"] = trigger_condition_met(
                        graph.source.to_dict(), context)
                    for kind in ("TriggerPlayerControlsAbilitySource",
                                 "TriggerCardMatchesFilter",
                                 "TriggerCardIsNthCardDrawnThisTurnByThisPlayer"):
                        values = [evaluate_condition(node, context)
                                  for node in _typed_nodes(raw_condition, kind)]
                        if values:
                            condition_diagnostics[kind] = values
                    from rules_port.trigger_discovery import RecordsTriggerDiscovery
                    candidates = RecordsTriggerDiscovery(
                        db, handler, session, PLAYER_UID, AI_UID,
                        state).discover(
                            event.event_type,
                            source_uid=event.source_card_id,
                            source_owner_uid=event.source_player_id,
                            extra_target=event.target_card_id,
                            zones=event.data.get("zones"))
                    candidate_sources = tuple(
                        candidate.source_uid for candidate in candidates
                        if str(graph.guid).lower() in candidate.ability_guids)
                except (RuntimeError, TypeError, ValueError, KeyError) as exc:
                    condition_diagnostics["error"] = str(exc)
                trace_summary = [{
                    key: entry.get(key) for key in (
                        "ability_guid", "effect_guid", "effect_order",
                        "effect_type", "result", "error",
                        "ability_instance_id")
                } for entry in (state.get("ability_trace") or ())[:12]]
                raise AssertionError(
                    f"authored {event.event_type} did not resolve {graph.guid} "
                    f"from its staged source zone "
                    f"{trigger_source_zone!r} "
                    f"(dispatch={dispatch_result!r}, "
                    f"queued={pending_before_drain}, "
                    f"paused={bool(state.get('resolution_paused'))}, "
                    f"pending={state.get('pending_choice')!r}, "
                    f"trace={trace_summary!r}, event={event!r}, "
                    f"condition={condition_diagnostics!r}, "
                    f"candidate_sources={candidate_sources!r})")
    else:
        event_tac = dict(event.data.get("event_tac") or {})
        if (not graph.trigger_event_type and not graph.manual and
                not _is_resource(metadata["card_type"]) and
                _effect_condition_requires_played_from_hand(graph)):
            # This direct per-graph contract represents the ordinary cast
            # path from hand. Production C# sets PlayedFromHand on the parent
            # PlayCard AbilityInstance; its effect conditions read that TAC.
            # Mirror the same authored value used by resolve_port_played_spell
            # without resolving sibling card abilities in this fixture.
            from rules_port.tac import _tac_attr_hash
            event_tac[_tac_attr_hash("PlayedFromHand")] = 1
        resolve_port_ability(
            handler, game, session, db, PLAYER_UID, AI_UID, state,
            graph.guid, source_uid, PLAYER_OWNER,
            target_map=activation.target_map, variables=activation.variables,
            event_tac=event_tac or None,
            cost_target_map=activation.cost_target_map)
    return {"metadata": metadata, "handler": handler, "session": session,
            "state": state, "game": game}


def _pending_choice(state):
    pending = state.get("pending_choice")
    if not isinstance(pending, dict):
        raise AssertionError("choice contract paused without pending_choice")
    raw_uids = pending.get("choice_uids") or ()
    choice_uids = []
    for value in raw_uids:
        try:
            choice_uids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not choice_uids:
        raise AssertionError("choice contract exposed no selectable cards")
    return pending, choice_uids


def _record_type_names(value):
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, dict):
        kind = str(value.get("_t", "")).rsplit(".", 1)[-1]
        return ({kind} if kind else set()).union(*(
            _record_type_names(child) for child in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_record_type_names(child) for child in value))
    return set()


def _source_zone_from_condition(graph):
    filters = []
    for node in _typed_nodes(
            graph.trigger_condition, "CardFilterAbilityCondition"):
        filters.append(node.get("m_CardFilter") or {})
    for node in _typed_nodes(
            graph.trigger_condition,
            "RequiresSourcePassesFilterCondition"):
        filters.append(node.get("m_Filter") or {})
    for node in _typed_nodes(
            graph.trigger_condition, "TriggerCardMatchesFilter"):
        if str(node.get("m_TriggerTest") or "TriggerSource").rsplit(
                ".", 1)[-1].lower() != "triggersource":
            continue
        card_filter = node.get("m_CardFilter") or {}
        source_identity = (
            bool(list(_filter_nodes(card_filter, "IsAbilitySource"))) or
            any(bool(match.get(key)) for match in
                _filter_nodes(card_filter, "IsCardName")
                for key in ("m_CompareToAbilitySource",)))
        if source_identity:
            filters.append(card_filter)
    for filter_spec in filters:
        zones = list(_filter_nodes(filter_spec, "InZone"))
        if zones:
            zone = str(zones[0].get("m_Collection", ""))
            return {"crypt": "discard"}.get(zone.lower(), zone.lower())
    if any(_filter_nodes(filter_spec, "TopNOfDeck")
           for filter_spec in filters):
        return "deck"
    return ""


def _stage_source_top_of_deck_filters(db, graph, source_uid):
    """Place the source at an authored top/bottom deck position."""
    source_filters = []
    for node in _typed_nodes(
            graph.trigger_condition, "CardFilterAbilityCondition"):
        source_filters.append(node.get("m_CardFilter") or {})
    for node in _typed_nodes(
            graph.trigger_condition, "RequiresSourcePassesFilterCondition"):
        source_filters.append(node.get("m_Filter") or {})
    for node in _typed_nodes(
            graph.trigger_condition, "TriggerCardMatchesFilter"):
        if str(node.get("m_TriggerTest") or "TriggerSource").rsplit(
                ".", 1)[-1].lower() == "triggersource":
            source_filters.append(node.get("m_CardFilter") or {})
    positional = [node for filter_spec in source_filters
                  for node in _filter_nodes(filter_spec, "TopNOfDeck")]
    if not positional:
        return
    row = db.execute(
        "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
        (SESSION_ID, int(source_uid))).fetchone()
    if row and str(row[0] or "").lower() == "deck":
        bottom_only = all(bool(node.get("m_CountFromBottom"))
                          for node in positional)
        if bottom_only:
            db.execute(
                "UPDATE game_cards SET position=COALESCE((SELECT "
                "MAX(position)+1000000 FROM game_cards WHERE session_id=? "
                "AND lower(location)='deck'),1000000) WHERE session_id=? "
                "AND card_uid=?",
                (SESSION_ID, SESSION_ID, int(source_uid)))
        else:
            # The fixture orders the deck by ascending position.
            db.execute(
                "UPDATE game_cards SET position=-1000000 WHERE session_id=? "
                "AND card_uid=?", (SESSION_ID, int(source_uid)))
        db.commit()


def _authored_zone_trigger_shape(graph):
    """Recognize a simple Records zone trigger suitable for event staging."""
    zone_nodes = list(_typed_nodes(
        graph.trigger_condition, "TriggerCardEnteredZone"))
    match_nodes = list(_typed_nodes(
        graph.trigger_condition, "TriggerCardMatchesFilter"))
    source_nodes = list(_typed_nodes(
        graph.trigger_condition, "TriggerCardIsAbilitySource"))
    if len(zone_nodes) != 1:
        return None
    event_card_is_source = len(source_nodes) == 1 and not match_nodes
    if event_card_is_source:
        match_filter = {}
        source_zone = ""
    elif (len(match_nodes) == 1 and
          str(match_nodes[0].get("m_TriggerTest") or "TriggerSource")
          == "TriggerSource"):
        match_filter = match_nodes[0].get("m_CardFilter") or {}
        source_zone = _source_zone_from_condition(graph)
    else:
        return None
    supported_filters = {
        "AndCardFilter", "OrCardFilter", "NotCardFilter", "IsTroop",
        "IsControlledBy", "IsNotControlledBy", "InZone", "IntAttrFilter",
    }
    if ((not source_zone and not event_card_is_source) or
            _record_type_names(match_filter) - supported_filters):
        return None
    zone_node = zone_nodes[0]
    source_collection = _normalise_zone(
        zone_node.get("m_SourceCollection", "Warzone"))
    destination = _normalise_zone(
        zone_node.get("m_DestinationCollection", "Discard"))
    if not source_collection or source_collection in {"none", "unknown"}:
        # A None previous-collection value is a C# wildcard. The generated
        # play fixture starts a permanent on CastSpells before it enters the
        # Warzone, which supplies a concrete prior collection for the event.
        source_collection = "CastSpells"
    if not destination or destination in {"none", "unknown"}:
        return None
    if "|" in destination:
        # A flags value ("Hand|Discard") is a set of accepted destinations.
        # Stage one concrete collection the card can actually occupy.
        destination = next((
            _normalise_zone(part) for part in destination.split("|")
            if _normalise_zone(part) not in {
                "", "none", "unknown", "champions", "choosing",
                source_collection.lower()}), "")
        if not destination:
            return None
    if event_card_is_source:
        # The source card remains registered for this transition through the
        # client's previous trigger map, even after it leaves this collection.
        source_zone = source_collection
    return {
        "source_zone": source_zone,
        "event_source_collection": source_collection,
        "event_destination_collection": destination,
        "card_filter": match_filter,
        "event_card_is_source": event_card_is_source,
    }


def _seed_filter_int_attributes(db, card_uid, filter_spec):
    row = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
        "AND card_uid=?", (SESSION_ID, int(card_uid))).fetchone()
    if row is None:
        return
    try:
        buffs = json.loads(row[0] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if not isinstance(buffs, dict):
        buffs = {}
    attrs = buffs.setdefault("int_attrs", {})
    for node in _filter_nodes(filter_spec, "IntAttrFilter"):
        name = str(node.get("m_Attribute", "") or "").strip()
        if (not name or ">" in name or
                name.lower().startswith(("you", "abilitytac"))):
            continue
        try:
            value = int(node.get("m_Value", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        operation = str(node.get("m_ComparisonOp", "") or "")
        if operation == "GreaterThan":
            value += 1
        elif operation == "LessThan":
            value = max(0, value - 1)
        elif operation == "NotEqualTo":
            value += 1
        attrs[name] = value
    db.execute(
        "UPDATE game_cards SET permanent_buffs=? WHERE session_id=? "
        "AND card_uid=?", (json.dumps(buffs), SESSION_ID, int(card_uid)))
    db.commit()


def _stage_filter_tac_statistics(state, card_uid, filter_specs):
    """Seed typed card-scoped TAC counters used by generated witnesses."""
    if not isinstance(state, dict):
        return
    for filter_spec in filter_specs:
        for node in _filter_nodes(filter_spec, "IntAttrFilter"):
            attribute = str(node.get("m_Attribute", "") or "").strip()
            parts = [part.strip() for part in attribute.split(">", 1)]
            if len(parts) != 2:
                continue
            scope, name = parts
            try:
                required = int(node.get("m_Value", 0) or 0)
            except (TypeError, ValueError):
                continue
            operation = str(node.get("m_ComparisonOp") or "Equals")
            if operation in {"GreaterThan", "GreaterThanOrEqual"}:
                required += int(operation == "GreaterThan")
            elif operation == "LessThan":
                required = max(0, required - 1)
            elif operation == "NotEqual":
                required += int(required == 0)
            elif operation not in {"Equals", "LessThanOrEqual"}:
                continue
            values = state.setdefault("tac_statistics", {}).setdefault(
                "cards", {}).setdefault(str(int(card_uid)), {}).setdefault(
                    scope, {})
            values[name] = max(int(values.get(name, 0) or 0), required)


def _zone_trigger_witness_template(db, card_filter):
    """Choose a Records template for a generated zone-trigger event card."""
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import (filter_matches_template,
                                    records_filter_from_metadata)

    rows = []
    for node in _filter_nodes(card_filter, "IsSubType"):
        subtype = str(node.get("m_SubType", "") or "").strip()
        if subtype:
            rows.extend(db.execute(
                "SELECT guid FROM card_templates WHERE "
                "lower(subtype) LIKE ? ORDER BY guid LIMIT 128",
                (f"%{subtype.lower()}%",)).fetchall())
    for node in _filter_nodes(card_filter, "IsCardName"):
        if any(bool(node.get(flag)) for flag in (
                "m_CompareToAbilitySource", "m_CompareToTriggerSource",
                "m_CompareToTriggerTarget")):
            continue
        name = str(node.get("m_CardName", "") or "").strip()
        if name:
            rows.extend(db.execute(
                "SELECT guid FROM card_templates WHERE lower(name)=? "
                "ORDER BY guid LIMIT 64", (name.lower(),)).fetchall())
    for node in _filter_nodes(card_filter, "NameContainsFilter"):
        term = str(node.get("m_ContainsString", "") or "").strip()
        if term:
            rows.extend(db.execute(
                "SELECT guid FROM card_templates WHERE "
                "lower(COALESCE(name,'')) LIKE ? OR "
                "lower(COALESCE(subtype,'')) LIKE ? ORDER BY guid LIMIT 128",
                (f"%{term.lower()}%", f"%{term.lower()}%")).fetchall())
    card_types = _card_count_candidate_types(card_filter)
    for card_type in sorted(card_types):
        rows.extend(db.execute(
            "SELECT guid FROM card_templates WHERE lower(card_type) LIKE ? "
            "ORDER BY guid LIMIT 128", (f"%{card_type.lower()}%",)).fetchall())
    if not rows:
        rows = db.execute(
            "SELECT guid FROM card_templates ORDER BY guid LIMIT 128"
        ).fetchall()

    unique_rows = []
    seen = set()
    for (guid,) in rows:
        normalized = str(guid).lower()
        if normalized not in seen:
            seen.add(normalized)
            unique_rows.append((guid,))
    candidates = _filter_witness_template_rows(
        db, [card_filter], unique_rows)
    return str(candidates[0][0]) if candidates else None


def _stage_authored_zone_trigger(db, graph, shape, source_uid,
                                 source_owner, state, handler, game, session):
    """Create an authored zone event and require the real trigger path to fire."""
    from rules_port.condition_context import ConditionContext
    from rules_port.conditions import trigger_condition_met

    source_zone = shape["event_source_collection"]
    destination = shape["event_destination_collection"]
    if not shape.get("event_card_is_source"):
        witness_guid = _zone_trigger_witness_template(
            db, shape["card_filter"])
        if witness_guid:
            witness_instance = CARD_UID_BASE + 9000
            witness_uid = _raw_uid(witness_instance)
            while db.execute(
                    "SELECT 1 FROM game_cards WHERE session_id=? "
                    "AND card_uid=?", (SESSION_ID, witness_uid)).fetchone():
                witness_instance += 1
                witness_uid = _raw_uid(witness_instance)
            _insert_card(
                db, witness_uid, int(source_owner), source_zone,
                witness_guid, 300)
            _seed_filter_int_attributes(
                db, witness_uid, shape["card_filter"])
            _stage_card_states_for_filters(
                db, witness_uid, [shape["card_filter"]])
            _stage_filter_relationship_witnesses(
                db, witness_uid, [shape["card_filter"]],
                source_uid=source_uid)
            _stage_filter_tac_statistics(
                state, witness_uid, [shape["card_filter"]])
            _stage_socketed_gem_witness(
                db, witness_uid, [shape["card_filter"]],
                source_uid=source_uid)
            db.commit()
    rows = db.execute(
        "SELECT card_uid,user_id,card_state FROM game_cards "
        "WHERE session_id=? AND location=? ORDER BY user_id,position",
        (SESSION_ID, source_zone)).fetchall()
    if shape.get("event_card_is_source"):
        rows = [row for row in rows if int(row[0]) == int(source_uid)]
    for raw_uid, raw_owner, raw_state in rows:
        event_uid = int(raw_uid)
        event_owner = int(raw_owner or 0)
        if (event_uid == int(source_uid) and
                not shape.get("event_card_is_source")):
            continue
        if list(_typed_nodes(
                graph.trigger_condition,
                "TriggerCardIsStoredTargetOfAbilitySource")):
            stored = state.setdefault("stored_targets", {}).setdefault(
                str(graph.guid).lower(), [])
            if int(event_uid) not in {int(uid) for uid in stored}:
                stored.append(int(event_uid))
        _seed_filter_int_attributes(db, event_uid, shape["card_filter"])
        db.execute(
            "UPDATE game_cards SET location=? WHERE session_id=? "
            "AND card_uid=?", (destination, SESSION_ID, event_uid))
        db.commit()
        data = {
            "event_source_collection": source_zone,
            "event_destination_collection": destination,
            "event_previous_state": int(raw_state or 0),
            "event_previous_owner_id": event_owner,
            "zones": tuple(dict.fromkeys((
                source_zone, destination, "warzone", "discard", "hand",
                "underground"))),
        }
        context = ConditionContext(
            db, session, state, event_type="CardEnteredZoneEvent",
            ability_source_uid=int(source_uid),
            ability_source_owner_id=int(source_owner),
            trigger_uid=event_uid, pl_t=PLAYER_UID, ai_t=AI_UID,
            champions=handler._champion_targets(),
            trigger_owner_id=event_owner,
            event_source_collection=source_zone,
            event_destination_collection=destination,
            event_previous_state=int(raw_state or 0),
            uses_previous_state=graph.uses_previous_state,
            event_previous_owner_id=event_owner)
        if trigger_condition_met(graph.source.to_dict(), context):
            # Resolve the trigger against the board *after* this zone move.
            # The general activation setup runs while the source is still in
            # its pre-event collection; it can incorrectly count the source
            # itself as the only legal target (for example, a troop on
            # CastSpells) even though the event has just moved it to Warzone.
            # Re-run metadata-driven target witness staging now so explicit
            # trigger targets have a legal card in the authored collection.
            _seed_target_filter_candidates(
                db, graph, int(source_uid), int(source_owner), state, handler)
            _announce_card(game, handler, db, event_uid, event_owner,
                           destination)
            state["resolving_trigger_event_type"] = "CardEnteredZoneEvent"
            state["resolving_trigger_source_uid"] = event_uid
            state["resolving_trigger_target_uid"] = None
            state["resolving_trigger_event_data"] = dict(data)
            from unittest import mock
            with mock.patch("rules_port.triggers.random.randrange",
                            return_value=0):
                dispatch_result = dispatch_native_trigger(
                    db=db, handler=handler, game=game, session=session,
                    player_uid=PLAYER_UID, ai_uid=AI_UID, battle_state=state,
                    event_type="CardEnteredZoneEvent",
                    source_card_id=event_uid, source_player_id=event_owner,
                    target_card_id=None, data=data)
            pending_before_drain = len(state.get("stack") or ())
            # Generated client activation data can bind several authored
            # target instances at once. The headless trigger adapter normally
            # selects only its first scalar target, so supply legal choices
            # for every target index after the event has established the
            # final board state (Ruinforge Reinventor sacrifices two artifacts
            # and then targets one artifact in the crypt).
            activation_targets = _target_map(
                db, graph, int(source_uid), int(source_owner), state, handler)
            for queued in reversed(state.get("stack") or ()):
                if (str(queued.get("kind") or "") == "trigger" and
                        str(queued.get("ability_guid") or "").lower() ==
                        str(graph.guid).lower()):
                    queued["activation_data"] = {
                        "target_map": activation_targets}
                    break
            _drain_triggers(handler, game, session, db, state)
            if not any(
                    str(entry.get("ability_guid") or "").lower()
                    == str(graph.guid).lower()
                    for entry in state.get("ability_trace") or ()):
                trace_summary = [{
                    key: entry.get(key) for key in (
                        "ability_guid", "effect_guid", "effect_order",
                        "effect_type", "result", "error",
                        "ability_instance_id")
                } for entry in (state.get("ability_trace") or ())[:12]]
                raise AssertionError(
                    f"authored zone trigger {graph.guid} matched its staged "
                    "event but produced no ability resolution trace "
                    f"(dispatch={dispatch_result!r}, "
                    f"queued={pending_before_drain}, "
                    f"paused={bool(state.get('resolution_paused'))}, "
                    f"pending={state.get('pending_choice')!r}, "
                    f"trace={trace_summary!r})")
            return True
        db.execute(
            "UPDATE game_cards SET location=? WHERE session_id=? "
            "AND card_uid=?", (source_zone, SESSION_ID, event_uid))
        db.commit()
    raise AssertionError(
        f"no generated event card satisfied authored zone trigger "
        f"{graph.guid} from {source_zone} to {destination}")


def _resume_choice_selection(scenario, choice_index):
    """Resume one client-visible choice using the production continuation shape."""
    from rules_port.choice_effects import (
        _play_choice_card, _resolve_choice_card_abilities)
    from rules_port.context import EffectContext

    handler = scenario["handler"]
    session = scenario["session"]
    db = scenario["db"]
    state = scenario["state"]
    game = scenario["game"]
    pending, choice_uids = _pending_choice(state)
    try:
        selected_uid = int(choice_uids[int(choice_index)])
    except (IndexError, TypeError, ValueError):
        raise AssertionError(
            f"choice branch index {choice_index} is outside "
            f"{len(choice_uids)} presented choices")
    kind = str(pending.get("kind") or "")
    owner_id = int(pending.get("owner_id", PLAYER_OWNER) or PLAYER_OWNER)
    state.pop("pending_choice", None)
    state.pop("resolution_paused", None)
    handler._current_bstate = state

    if kind == "choice_zone_target":
        continuation = pending.get("continuation") or {}
        child_guid = str(continuation.get("ability_guid") or "").lower()
        if not child_guid:
            raise AssertionError("choice-zone continuation has no child ability")
        child_source = int(continuation.get(
            "source_uid", pending.get("source_uid", 0)) or 0)
        child_owner = int(continuation.get(
            "owner_id", owner_id) or owner_id)
        child_targets = _numeric_target_map(continuation.get("target_map"))
        child_targets[int(continuation.get("target_index", 0) or 0)] = (
            selected_uid,)
        resolve_port_ability(
            handler, game, session, db, PLAYER_UID, AI_UID, state,
            child_guid, child_source, child_owner,
            target_map=child_targets,
            variables=continuation.get("variables") or {},
            resume_from_order=int(
                continuation.get("resume_effect_order", 0) or 0))
        parent = pending.get("parent") or {}
        parent_guid = str(parent.get("ability_guid") or "").lower()
        if parent_guid and not state.get("resolution_paused"):
            resolve_port_ability(
                handler, game, session, db, PLAYER_UID, AI_UID, state,
                parent_guid, parent.get("source_uid"),
                int(parent.get("owner_id", owner_id) or owner_id),
                target_map=_numeric_target_map(parent.get("target_map")),
                variables=parent.get("variables") or {},
                resume_from_order=int(
                    parent.get("resume_effect_order", 0) or 0),
                event_tac=parent.get("event_tac"))
    elif kind == "double_choice":
        context = EffectContext.from_rules_port(
            game, session, db, handler, PLAYER_UID, AI_UID, state,
            "", ability=None)
        state["phase"] = game_engine.ETurnPhases.StartTurn
        if not _play_choice_card(context, selected_uid, owner_id):
            raise AssertionError(
                f"choice card {hex(selected_uid)} was not legal to play")
        location = db.execute(
            "SELECT location FROM game_cards WHERE session_id=? "
            "AND card_uid=?", (SESSION_ID, selected_uid)).fetchone()
        if location is None or str(location[0]) != "PlayedResources":
            raise AssertionError(
                f"played choice {hex(selected_uid)} did not enter "
                f"PlayedResources: {location!r}")
        state.setdefault("_card_matrix_played_choice_uids", []).append(
            selected_uid)
        _resolve_choice_card_abilities(
            context, selected_uid, pending.get("source_uid"), owner_id)
        if not state.get("resolution_paused"):
            ability_guid = str(pending.get("ability_guid") or "").lower()
            if not ability_guid:
                raise AssertionError("double-choice continuation has no ability")
            resolve_port_ability(
                handler, game, session, db, PLAYER_UID, AI_UID, state,
                ability_guid, pending.get("source_uid"), owner_id,
                target_map=_numeric_target_map(pending.get("target_map")),
                variables=pending.get("variables") or {},
                resume_from_order=int(
                    pending.get("resume_effect_order", 0) or 0),
                event_tac=pending.get("event_tac"))
    else:
        raise AssertionError(f"unsupported choice continuation kind: {kind}")

    if not state.get("resolution_paused"):
        _drain_triggers(handler, game, session, db, state)
    # A choice-zone target is not necessarily being played. Its authored child
    # effect may move it to Hand, Discard, or another zone, so validate that
    # mutation through the typed effect contract instead of assuming a final
    # PlayedResources location here.
    return selected_uid


def _choice_paths(db, card_guid, graph, prefix=(), max_depth=8):
    """Enumerate selection-index paths until the authored graph completes."""
    scenario = _start_ability_contract(db, card_guid, graph)
    scenario["db"] = db
    state = scenario["state"]
    for choice_index in prefix:
        _pending, choice_uids = _pending_choice(state)
        if int(choice_index) >= len(choice_uids):
            raise AssertionError(
                f"choice path {prefix!r} is not legal for the presented "
                f"options {len(choice_uids)}")
        _resume_choice_selection(scenario, choice_index)
    if not state.get("resolution_paused"):
        return [tuple(prefix)]
    pending, choice_uids = _pending_choice(state)
    if str(pending.get("kind") or "") not in {
            "choice_zone_target", "double_choice"}:
        raise AssertionError(
            f"choice contract reached unsupported continuation "
            f"{pending.get('kind')!r}")
    if len(prefix) >= int(max_depth):
        raise AssertionError(
            f"choice continuation exceeded {max_depth} nested selections")
    paths = []
    for choice_index in range(len(choice_uids)):
        paths.extend(_choice_paths(
            db, card_guid, graph, prefix + (choice_index,), max_depth))
    return paths


def _assert_invoked_ability_graphs(db, graph, state, visited=None):
    """Verify and count every typed child graph reached in one resolution."""
    visited = set(visited or ())
    visited.add(str(graph.guid).lower())
    descendants = set()
    resolved_orders = set()
    for entry in state.get("ability_trace") or ():
        if str(entry.get("ability_guid") or "").lower() != \
                str(graph.guid).lower():
            continue
        try:
            resolved_orders.add(int(entry.get("effect_order")))
        except (TypeError, ValueError):
            continue
    for order, effect in enumerate(_ordered_contract_effects(graph)):
        child_guid = _invoked_ability_guid(effect)
        # A typed child graph is only reached if its ActivateAbility mapping
        # actually resolved. Conditional and random sibling branches are
        # intentionally absent from a single activation's trace.
        if order not in resolved_orders or not child_guid or \
                child_guid in visited:
            continue
        child = ability_graph(DEFAULT_RECORD_STORE, child_guid)
        if child is None:
            raise AssertionError(
                f"ability {graph.guid}: missing invoked child {child_guid}")
        if not any(str(entry.get("ability_guid") or "").lower() ==
                   child_guid for entry in state.get("ability_trace") or ()):
            raise AssertionError(
                f"ability {graph.guid}: nested activation never resolved child "
                f"ability {child_guid}")
        child_sources = {
            int(entry["source_uid"])
            for entry in state.get("ability_trace") or ()
            if str(entry.get("ability_guid") or "").lower() == child_guid
            and entry.get("source_uid") is not None}
        for source_uid in child_sources:
            owner_row = db.execute(
                "SELECT user_id FROM game_cards WHERE session_id=? "
                "AND card_uid=?", (SESSION_ID, source_uid)).fetchone()
            if owner_row is not None:
                _capture_dynamic_summon_amounts(
                    db, child, source_uid, int(owner_row[0]), state)
        # A child may have conditional/contingent mappings whose branch value
        # comes from the invoking parent ability instance. Validate every
        # child effect that actually traced and its postcondition, while the
        # presence check above guarantees the child itself did resolve.
        _assert_graph_trace(child, state, db=db, require_complete=False)
        descendants.add(child_guid)
        descendants.update(_assert_invoked_ability_graphs(
            db, child, state, visited))
    return descendants


def _assert_selected_choice_abilities(db, selected_uids, state):
    """Require selected cards' C# automatic abilities to resolve."""
    for selected_uid in selected_uids:
        row = db.execute(
            "SELECT card_abilities FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (SESSION_ID, int(selected_uid))).fetchone()
        for ability_guid in _json_list(row[0] if row else "[]"):
            guid = str(ability_guid).lower()
            graph = ability_graph(DEFAULT_RECORD_STORE, guid)
            if graph is None:
                raise AssertionError(
                    f"choice card {hex(int(selected_uid))}: missing "
                    f"ability graph {guid}")
            ability_index = int(
                graph.source.field("m_AbilityIndex", -1) or -1)
            if (graph.manual or graph.trigger_event_type or
                    ability_index >= 0 or not graph.effects):
                # C# Card.GetAutomaticAbilities excludes manual, triggered,
                # indexed, and effect-free abilities from PlayChoiceCard.
                continue
            traces = [
                entry for entry in state.get("ability_trace") or ()
                if str(entry.get("ability_guid") or "").lower() == guid
            ]
            if not traces:
                raise AssertionError(
                    f"choice card {hex(int(selected_uid))}: ability "
                    f"{guid} did not resolve")
            _assert_graph_trace(graph, state, db=db)


def _is_static_modifier_contract(graph):
    """Recognize passive CardModifier graphs evaluated by static_rules."""
    from rules_port.static_rules import is_continuous_self_static
    if is_continuous_self_static(graph):
        return True
    return bool(
        graph and not graph.trigger_event_type and not graph.manual and
        graph.effects and all(
            effect.concrete_type == "CardModifierAbilityEffectTemplate" and
            str(effect.duration).lower() == "whilecardinplay"
            for effect in graph.effects))


def _run_static_modifier_contract(db, card_guid, graph):
    """Assert passive Records modifiers through the shared static projector."""
    metadata = _seed_board(db, card_guid, contract_support=True)
    source_uid = int(metadata["source_uid"])
    source_zone = _static_effect_source_zone(graph) or "warzone"
    db.execute(
        "UPDATE game_cards SET location=? WHERE session_id=? "
        "AND card_uid=?", (source_zone, SESSION_ID, source_uid))
    db.commit()
    from gamedata import DEFAULT_RECORD_STORE
    source_filters = []
    for effect in graph.effects:
        condition_guid = str(effect.condition_guid or "").lower()
        if not condition_guid or condition_guid == "0" * 36:
            continue
        condition_record = DEFAULT_RECORD_STORE.get(
            "AbilityEffectConditionTemplate", condition_guid)
        condition = (getattr(condition_record, "condition", None)
                     if condition_record else None)
        source_filters.extend(
            node.get("m_Filter") or {}
            for node in _typed_nodes(
                condition, "RequiresSourcePassesFilterCondition"))
    _stage_card_states_for_filters(db, source_uid, source_filters)
    _stage_socketed_gem_witness(
        db, source_uid, source_filters, source_uid=source_uid)
    _stage_source_keyword_conditions(db, graph, source_uid)
    _stage_source_filter_conditions(db, graph, source_uid)
    handler = HandlerStub(db)
    state = _new_state(handler)
    _stage_resource_condition_witnesses(
        db, graph, PLAYER_OWNER, state)
    _stage_champion_health_conditions(state, graph, PLAYER_OWNER)
    _stage_source_keyword_conditions(db, graph, source_uid)
    _seed_source_counter_conditions(db, graph, source_uid)
    _stage_card_count_variable_inputs(
        db, graph, source_uid, PLAYER_OWNER, state)
    _stage_static_effect_condition_cards(
        db, graph, int(PLAYER_OWNER), source_uid=source_uid, state=state)
    _stage_stored_name_target_filters(
        db, graph, source_uid, PLAYER_OWNER, state, handler)
    _stage_stored_card_target_filters(
        db, graph, source_uid, PLAYER_OWNER, state, handler)
    _seed_target_filter_candidates(
        db, graph, source_uid, PLAYER_OWNER, state, handler)
    condition_targets = _target_map(
        db, graph, source_uid, PLAYER_OWNER, state, handler)
    _stage_effect_condition_cards(
        db, graph, PLAYER_OWNER, source_uid=source_uid,
        target_map=condition_targets,
        handler=handler)

    from rules_port.attribute_effects import attribute_bits_from_flags
    from rules_port.metadata import modifier_metadata
    from rules_port.static_rules import (effective_cost, effective_stats,
                                         _native_leaf_value, rule_modifiers)
    from rules_port.targeting import legal_targets_for
    from pvp_db import db_ability_raw_json

    source_owner = int(db.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (SESSION_ID, source_uid)).fetchone()[0])
    ability_raw = db_ability_raw_json(graph.guid, conn=db)
    if not ability_raw:
        ability_raw = json.dumps(graph.source.to_dict())
    _stage_counter_variable_witnesses(
        db, graph, source_uid, source_owner, state)

    champion_owner_by_uid = {}
    for owner_value, champion_value in (state.get("champ_map") or {}).items():
        try:
            champion_owner_by_uid[int(champion_value)] = int(owner_value)
        except (TypeError, ValueError):
            continue

    for effect in graph.effects:
        index = int(effect.target_index)
        if index >= len(graph.targets):
            raise AssertionError(
                f"static effect {effect.guid} has no authored target")
        target = graph.targets[index]
        player_target = target.target_kind == "PlayerTargetTemplate"
        candidates = ([PLAYER_OWNER, AI_OWNER] if player_target else
                      [source_uid]
                      if target.target_kind ==
                      "AbilitySourceCardTargetTemplate" else
                      legal_targets_for(
                          db, SESSION_ID, PLAYER_OWNER, target, source_uid,
                          both_players=True,
                          champions=handler._champion_targets(),
                          battle_state=state))
        if not candidates:
            raise AssertionError(
                f"static effect {effect.guid} has no generated legal target")
        modifier = modifier_metadata(effect.guid)
        prop = str(modifier.get("property") or "").lower()
        resolved_value = _native_leaf_value(
            db, SESSION_ID, state, source_uid, source_owner,
            modifier, ability_raw)
        if resolved_value is not None and resolved_value[0] == prop:
            modifier["_contract_input_value"] = int(resolved_value[1])
        projected = False
        for target_uid in candidates:
            if player_target:
                if prop != "intattr":
                    raise AssertionError(
                        f"static player effect {effect.guid} has unsupported "
                        f"contract property {prop!r}")
                from rules_port.static_rules import player_int_attributes
                from rules_port.resources import resource_play_limit
                source_owner = int(db.execute(
                    "SELECT user_id FROM game_cards WHERE session_id=? "
                    "AND card_uid=?",
                    (SESSION_ID, source_uid)).fetchone()[0])
                player_filter = str(
                    target.player_filter or "Self").rsplit(".", 1)[-1].lower()
                player_is_targeted = (
                    source_owner == int(target_uid)
                    if player_filter in {"self", "you", "controller",
                                         "activeplayer"} else
                    source_owner != int(target_uid)
                    if player_filter in {"opposing", "opponents",
                                         "multipleopponents"} else
                    player_filter in {"multipleplayers", "allplayers"})
                attrs = player_int_attributes(
                    db, SESSION_ID, state, int(target_uid))
                actual = next((int(value or 0) for name, value in attrs.items()
                               if str(name).lower() ==
                               str(modifier.get("attribute") or "").lower()), 0)
                champion_uid = (state.get("champ_map") or {}).get(
                    int(target_uid), (state.get("champ_map") or {}).get(
                        str(int(target_uid))))
                runtime_attrs = ((state.get("champion_int_attrs") or {}).get(
                    str(int(champion_uid)), {}) if champion_uid is not None
                                 else {})
                stored = next((int(value or 0) for name, value in
                               runtime_attrs.items()
                               if str(name).lower() ==
                               str(modifier.get("attribute") or "").lower()), 0)
                operation = str(modifier.get("operation") or "set").lower()
                amount = int(modifier.get(
                    "_contract_input_value", modifier.get(
                        "input_value", modifier.get("value", 0))) or 0)
                if modifier.get("double"):
                    expected = stored * 2
                elif operation in {"add", "increment"}:
                    expected = stored + amount
                elif operation in {"remove", "removeall", "clear"}:
                    expected = 0
                elif operation == "subtract":
                    expected = stored - amount
                else:
                    expected = amount
                projected = (actual == expected if player_is_targeted
                             else actual == stored)
                if (player_is_targeted and
                        str(modifier.get("attribute") or "").lower() ==
                        "additionalresourcesplayableonyourturn"):
                    limit = resource_play_limit(
                        db, SESSION_ID, state, int(target_uid))
                    projected = projected and limit == max(0, 1 + expected)
                if projected:
                    break
                continue
            champion_owner = champion_owner_by_uid.get(int(target_uid))
            if champion_owner is not None:
                if prop != "intattr":
                    continue
                from rules_port.static_rules import player_int_attributes
                attrs = player_int_attributes(
                    db, SESSION_ID, state, champion_owner,
                    target_uid=int(target_uid))
                attribute = str(modifier.get("attribute") or "").lower()
                actual = next((int(value or 0) for name, value in attrs.items()
                               if str(name).lower() == attribute), 0)
                runtime_attrs = (state.get("champion_int_attrs") or {}).get(
                    str(int(target_uid)), {})
                stored = next((int(value or 0) for name, value in
                               runtime_attrs.items()
                               if str(name).lower() == attribute), 0)
                operation = str(modifier.get("operation") or "set").lower()
                amount = int(modifier.get(
                    "_contract_input_value", modifier.get(
                        "input_value", modifier.get("value", 0))) or 0)
                if modifier.get("double"):
                    expected = stored * 2
                elif operation in {"add", "increment"}:
                    expected = stored + amount
                elif operation in {"remove", "removeall", "clear"}:
                    expected = 0
                elif operation == "subtract":
                    expected = stored - amount
                else:
                    expected = amount
                projected = actual == expected
                if projected:
                    break
                continue
            current = effective_stats(db, SESSION_ID, state, int(target_uid))
            base = db.execute(
                "SELECT COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
                "gc.card_attributes, COALESCE(ct.attributes,0), "
                "COALESCE(ct.cost,0), COALESCE(ct.rage_value,0) "
                ", COALESCE(ct.lethal,0) "
                "FROM game_cards gc "
                "JOIN card_templates ct ON ct.guid=gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (SESSION_ID, int(target_uid))).fetchone()
            if base is None:
                continue
            base_attributes = int(base[2] or 0) | int(base[3] or 0)
            if prop == "attribute":
                wanted = attribute_bits_from_flags(
                    modifier.get("attribute_flags", modifier.get("value", 0)))
                projected = bool(
                    wanted and (int(current[2]) & int(wanted)) == int(wanted))
            elif prop == "attack":
                projected = int(current[0]) != int(base[0])
                if not projected and "_contract_input_value" in modifier:
                    amount = int(modifier["_contract_input_value"])
                    expected = (amount if modifier.get("replaceexistingvalue")
                                else int(base[0]) + amount)
                    projected = expected == int(base[0])
            elif prop == "defense":
                projected = int(current[1]) != int(base[1])
                if not projected and "_contract_input_value" in modifier:
                    amount = int(modifier["_contract_input_value"])
                    expected = (amount if modifier.get("replaceexistingvalue")
                                else int(base[1]) + amount)
                    projected = expected == int(base[1])
            elif prop == "cardcost":
                actual = int(effective_cost(
                    db, SESSION_ID, state, int(target_uid)))
                projected = actual != int(base[4])
                if not projected and "_contract_input_value" in modifier:
                    amount = int(modifier["_contract_input_value"])
                    expected = (amount if modifier.get("replaceexistingvalue")
                                else int(base[4]) + amount)
                    projected = max(0, expected) == int(base[4])
            elif prop == "damagemultiplier":
                from rules_port.damage_effects import card_damage_multiplier
                value = max(0, int(modifier.get(
                    "_contract_input_value", modifier.get(
                        "input_value", modifier.get("value", 1))) or 0))
                rules = rule_modifiers(
                    db, SESSION_ID, state, int(target_uid))
                authored_rule = any(
                    rule.get("property") == "damagemultiplier" and
                    int(rule.get("value", rule.get("amount", 1)) or 0) == value
                    for rule in rules)
                combat_only = bool(modifier.get("combatdamageonly"))
                noncombat_only = bool(modifier.get("noncombatdamageonly"))
                expected_combat = value if not noncombat_only else 1
                expected_noncombat = value if not combat_only else 1
                projected = (
                    authored_rule and
                    card_damage_multiplier(
                        db, SESSION_ID, state, int(target_uid), True) ==
                    expected_combat and
                    card_damage_multiplier(
                        db, SESSION_ID, state, int(target_uid), False) ==
                    expected_noncombat)
            elif prop == "intattr":
                attribute = str(modifier.get("attribute") or "").lower()
                if attribute == "rage":
                    rage_bit = int(game_engine.ECardAttributes.Rage)
                    projected = (
                        int(current[4]) != int(base[5]) and
                        bool(int(current[2]) & rage_bit))
                elif attribute in {"lethal", "crush"}:
                    attr_bit = int(getattr(
                        game_engine.ECardAttributes,
                        attribute.title(), 0) or 0)
                    already_printed = bool(int(base_attributes) & attr_bit)
                    if attribute == "lethal":
                        already_printed = already_printed or bool(base[6])
                    projected = (attribute in current[3] and
                                 not already_printed)
                else:
                    from rules_port.combat_rules import card_int_attr
                    mutation_row = db.execute(
                        "SELECT user_id, permanent_buffs, temporary_buffs "
                        "FROM game_cards WHERE session_id=? AND card_uid=?",
                        (SESSION_ID, int(target_uid))).fetchone()
                    stored = 0
                    if mutation_row:
                        for payload in mutation_row[1:]:
                            try:
                                buffs = json.loads(payload or "{}")
                            except (TypeError, ValueError, json.JSONDecodeError):
                                buffs = {}
                            attrs = buffs.get("int_attrs", {}) \
                                if isinstance(buffs, dict) else {}
                            if isinstance(attrs, dict):
                                stored += next((int(value or 0)
                                                for key, value in attrs.items()
                                                if str(key).lower() == attribute), 0)
                    effective = card_int_attr(
                        db, SESSION_ID, int(target_uid), attribute,
                        battle_state=state)
                    projected = effective != stored
                    if (not projected and
                            "_contract_input_value" in modifier):
                        amount = int(modifier["_contract_input_value"])
                        operation = str(
                            modifier.get("operation") or "set").lower()
                        projected = (
                            amount == 0 and operation in {"add", "increment"}
                            or operation == "set" and amount == stored
                            or operation in {"remove", "removeall", "clear"}
                            and stored == 0)
                    if (projected and
                            attribute == "cantbeexhaustedbyopponent"):
                        from types import SimpleNamespace
                        from rules_port.effects import _tap

                        class TapContext:
                            def __init__(self, responsible_owner):
                                self.db = db
                                self.session = SimpleNamespace(
                                    session_id=SESSION_ID)
                                self.bstate = {
                                    "resolving_owner_id": responsible_owner}
                                self.events = []

                            def resolved_target(self):
                                return int(target_uid)

                            def update_card_state(self, card_uid, **kwargs):
                                self.events.append((int(card_uid), kwargs))

                        target_owner = int(mutation_row[0])
                        opposing_owner = (AI_OWNER if target_owner == PLAYER_OWNER
                                          else PLAYER_OWNER)
                        blocked = TapContext(opposing_owner)
                        if _tap(blocked) != "tapped 0" or blocked.events:
                            raise AssertionError(
                                "opposing exhaust was not blocked by the "
                                "projected IntAttr")
                        allowed = TapContext(target_owner)
                        if _tap(allowed) != "tapped 1" or len(allowed.events) != 1:
                            raise AssertionError(
                                "controller exhaust was blocked by the "
                                "projected IntAttr")
            elif prop in {
                    "attackimmunity", "blockimmunity",
                    "blockimmunityexception", "blockrestriction",
                    "damageimmunity", "targetingimmunity"}:
                authored_filter = modifier.get("cardfilter")
                rules = rule_modifiers(
                    db, SESSION_ID, state, int(target_uid))
                projected = any(
                    str(rule.get("property") or "").lower() == prop and
                    (rule.get("cardfilter") or rule.get("filter")) ==
                    authored_filter and
                    (prop != "damageimmunity" or
                     bool(rule.get("iscombatdamage")) ==
                     bool(modifier.get("iscombatdamage")))
                    for rule in rules)
            else:
                raise AssertionError(
                    f"static effect {effect.guid} has unsupported contract "
                    f"property {prop!r}")
            if projected:
                break
        if not projected:
            raise AssertionError(
                f"static effect {effect.guid} did not project its authored "
                f"{prop or 'modifier'} onto a legal target")
    return {"effects": len(graph.effects), "traces": len(graph.effects),
            "paused": False, "choice_branches": 0}


def _revealed_target_branch_cases(graph):
    """Find one-card reveal graphs with multiple typed result branches."""
    source_indexes = {
        int(effect.target_index) for effect in graph.effects
        if 0 <= int(effect.target_index) < len(graph.targets) and
        graph.targets[int(effect.target_index)].target_kind ==
        "SourceRevealedTargetTemplate"}
    branches = [(index, tuple(
        order for order, effect in enumerate(_ordered_contract_effects(graph))
        if int(effect.target_index) == index)) for index in
        sorted(source_indexes)]
    branches = [(index, orders) for index, orders in branches if orders]
    if len(branches) < 2:
        return ()
    reveal_effects = [effect for effect in graph.effects
                      if effect.concrete_type ==
                      "RevealCardsAbilityEffectTemplate" and
                      0 <= int(effect.target_index) < len(graph.targets)]
    for reveal in reveal_effects:
        scope = graph.targets[int(reveal.target_index)]
        top_nodes = list(_filter_nodes(scope.card_filter, "TopNOfDeck"))
        if not top_nodes:
            continue
        counts = [int(node.get("m_Amount", 1) or 1)
                  for node in top_nodes]
        if counts and max(counts) == 1:
            return tuple(branches)
    return ()


def _stage_top_revealed_target_branch(db, graph, target_index,
                                      source_owner):
    """Place one authored matching witness at the reveal scope's top."""
    if target_index < 0 or target_index >= len(graph.targets):
        raise AssertionError(
            f"ability {graph.guid}: invalid revealed target {target_index}")
    target = graph.targets[target_index]
    reveal = next((effect for effect in graph.effects
                   if effect.concrete_type ==
                   "RevealCardsAbilityEffectTemplate" and
                   0 <= int(effect.target_index) < len(graph.targets) and
                   list(_filter_nodes(
                       graph.targets[int(effect.target_index)].card_filter,
                       "TopNOfDeck"))), None)
    if reveal is None:
        raise AssertionError(
            f"ability {graph.guid}: no top-card reveal scope for branch")
    scope = graph.targets[int(reveal.target_index)]
    zones = _target_zones(scope)
    zone = next((value for value in zones if value == "deck"),
                zones[0] if zones else "deck")
    player_filter = str(scope.player_filter or "Self").rsplit(".", 1)[-1]
    candidate_owner = (AI_OWNER if player_filter.lower() in {
        "singleopponent", "multipleopponents", "opposing"} else
        int(source_owner))
    filter_specs = [target.card_filter,
                    *_contract_target_condition_filters(graph, target_index)]
    candidates = _filter_witness_template_rows(
        db, filter_specs, db.execute(
            "SELECT guid FROM card_templates ORDER BY guid").fetchall())
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import (filter_matches_template,
                                    records_filter_from_metadata)
    compiled = None
    try:
        compiled = records_filter_from_metadata(target.card_filter)
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    # A filterless "remaining revealed cards" target is a cleanup branch,
    # not a competing match for every typed branch. Pick a witness that does
    # not satisfy any of the other filtered revealed targets so the contract
    # can exercise each authored path independently.
    branch_indexes = sorted({
        int(effect.target_index) for effect in graph.effects
        if 0 <= int(effect.target_index) < len(graph.targets) and
        graph.targets[int(effect.target_index)].target_kind ==
        "SourceRevealedTargetTemplate"})
    exclusive_filters = []
    for other_index in branch_indexes:
        if other_index == int(target_index):
            continue
        other_filter = graph.targets[other_index].card_filter
        if not other_filter:
            continue
        try:
            exclusive_filters.append(
                records_filter_from_metadata(other_filter))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    template_guid = None
    for (candidate_guid,) in candidates:
        candidate = DEFAULT_RECORD_STORE.get(
            "CardTemplate", str(candidate_guid).lower())
        if candidate is None:
            continue
        try:
            if compiled is not None and not filter_matches_template(
                    compiled, candidate):
                continue
            if any(filter_matches_template(other_filter, candidate)
                   for other_filter in exclusive_filters):
                continue
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        template_guid = str(candidate_guid).lower()
        break
    if template_guid is None:
        raise AssertionError(
            f"ability {graph.guid}: no template satisfies revealed target "
            f"{target_index} filter")
    uid_instance = CARD_UID_BASE + 20000 + target_index
    uid = _raw_uid(uid_instance)
    while db.execute(
            "SELECT 1 FROM game_cards WHERE session_id=? AND card_uid=?",
            (SESSION_ID, uid)).fetchone():
        uid_instance += 100
        uid = _raw_uid(uid_instance)
    _insert_card(db, uid, candidate_owner, zone, template_guid,
                 -1000000 + target_index, abilities=[])
    db.commit()


def _run_ability_contract(db, card_guid, graph):
    """Resolve one typed graph and every presented choice continuation."""
    if _is_static_modifier_contract(graph):
        return _run_static_modifier_contract(db, card_guid, graph)

    branch_contract = _cardblocked_resource_branches(graph)
    random_branch_cases = _randomized_variable_contract_cases(graph)
    if branch_contract and random_branch_cases:
        raise AssertionError(
            f"ability {graph.guid}: combined CardBlocked and random "
            "branch contract needs a product fixture")
    if branch_contract:
        always_effects, resource_effects, fallback_effects = branch_contract
        branch_traces = []
        branch_states = []
        expected_summon_amounts = {}
        nested_graphs = set()
        final_state = None
        for resource_top in (False, True):
            scenario = _start_ability_contract(
                db, card_guid, graph,
                cardblocked_resource_branch=resource_top)
            scenario["db"] = db
            state = scenario["state"]
            if state.get("resolution_paused"):
                pending = state.get("pending_choice") or {}
                raise AssertionError(
                    f"ability {graph.guid}: CardBlocked branch left an "
                    f"unsupported continuation ({pending.get('kind')!r})")
            _drain_triggers(
                scenario["handler"], scenario["game"], scenario["session"],
                db, state)
            _assert_champion_destroy_closed(scenario)
            _validate_wire_events(scenario["game"])
            observed = Counter(
                str(entry.get("effect_guid") or "").lower()
                for entry in state.get("ability_trace") or ()
                if str(entry.get("ability_guid") or "").lower() ==
                str(graph.guid).lower())
            expected = (always_effects | resource_effects if resource_top
                        else always_effects | fallback_effects)
            forbidden = (fallback_effects if resource_top
                         else resource_effects)
            missing = expected - set(observed)
            unexpected = forbidden & set(observed)
            branch = "resource" if resource_top else "nonresource"
            if missing or unexpected:
                raise AssertionError(
                    f"ability {graph.guid}: {branch} reveal branch "
                    f"expected effects missing={sorted(missing)} "
                    f"opposite-branch effects={sorted(unexpected)}")
            nested_graphs.update(
                _assert_invoked_ability_graphs(db, graph, state))
            branch_traces.extend(state.get("ability_trace") or ())
            branch_states.append(state)
            expected_summon_amounts.update(
                state.get("_card_matrix_expected_summon_amounts") or {})
            final_state = state

        combined_state = dict(final_state or {})
        combined_state["ability_trace"] = branch_traces
        combined_state["_ability_effect_status_by_guid"] = \
            _merge_effect_status_snapshots(branch_states)
        combined_state["_card_matrix_expected_summon_amounts"] = \
            expected_summon_amounts
        traces = _assert_graph_trace(graph, combined_state, db=db)
        return {"effects": len(graph.effects), "traces": traces,
                "nested_abilities": len(nested_graphs),
                "nested_effects": sum(
                    len(ability_graph(DEFAULT_RECORD_STORE, guid).effects)
                    for guid in nested_graphs),
                "paused": False, "choice_branches": 0}

    if random_branch_cases:
        effects = _ordered_contract_effects(graph)
        randomizer_order = next(
            order for order, effect in enumerate(effects)
            if str(effect.concrete_type).startswith("RandomizeVariable"))
        randomizer_name = str(_field(
            effects[randomizer_order].template, "m_VariableName",
            "RandomNumber") or "RandomNumber")
        branch_traces = []
        branch_states = []
        expected_summon_amounts = {}
        observed_random_values = []
        final_state = None
        for case in random_branch_cases:
            scenario = _start_ability_contract(
                db, card_guid, graph, rng_seed=case["rng_seed"])
            scenario["db"] = db
            state = scenario["state"]
            if state.get("resolution_paused"):
                pending = state.get("pending_choice") or {}
                raise AssertionError(
                    f"ability {graph.guid}: random branch paused on an "
                    f"unsupported continuation ({pending.get('kind')!r})")
            _drain_triggers(
                scenario["handler"], scenario["game"], scenario["session"],
                db, state)
            _assert_champion_destroy_closed(scenario)
            _validate_wire_events(scenario["game"])
            prefix = f"randomized {randomizer_name}="
            graph_traces = [entry for entry in
                            state.get("ability_trace") or ()
                            if str(entry.get("ability_guid") or "").lower()
                            == str(graph.guid).lower()]
            random_traces = [entry for entry in graph_traces
                             if int(entry.get("effect_order", -1))
                             == randomizer_order]
            if not random_traces:
                raise AssertionError(
                    f"ability {graph.guid}: randomizer mapping left no trace")
            requested_seen = False
            for random_trace in random_traces:
                result = str(random_trace.get("result") or "")
                actual = None
                try:
                    if result.startswith(prefix):
                        actual = int(result[len(prefix):])
                except (TypeError, ValueError):
                    actual = None
                if actual is None:
                    raise AssertionError(
                        f"ability {graph.guid}: randomizer trace has "
                        f"unexpected result {result!r}")
                if actual == int(case["value"]):
                    requested_seen = True
                instance_id = random_trace.get("ability_instance_id")
                if instance_id is None:
                    raise AssertionError(
                        f"ability {graph.guid}: random branch trace omitted "
                        "its ability instance id")
                instance_traces = [entry for entry in graph_traces
                                   if entry.get("ability_instance_id") ==
                                   instance_id]
                branch_outcomes = _random_variable_condition_outcomes(
                    graph, randomizer_name, actual)
                observed_orders = {
                    int(entry["effect_order"]) for entry in instance_traces}
                expected_orders = {
                    order for order, outcome in enumerate(branch_outcomes)
                    if outcome is True}
                forbidden_orders = {
                    order for order, outcome in enumerate(branch_outcomes)
                    if outcome is False}
                missing = expected_orders - observed_orders
                unexpected = forbidden_orders & observed_orders
                if missing or unexpected:
                    missing_text = [
                        f"{order}:{effects[order].guid}"
                        for order in sorted(missing)]
                    unexpected_text = [
                        f"{order}:{effects[order].guid}"
                        for order in sorted(unexpected)]
                    raise AssertionError(
                        f"ability {graph.guid}: random outcome {actual} "
                        f"instance {instance_id} has missing effects="
                        f"{missing_text}, opposite-branch effects="
                        f"{unexpected_text}")
            if not requested_seen:
                raise AssertionError(
                    f"ability {graph.guid}: RNG fixture requested "
                    f"{case['value']} but no activation resolved it")

            _assert_graph_trace(
                graph, state, db=db, require_complete=False)
            observed_random_values.append(int(case["value"]))
            branch_traces.extend(state.get("ability_trace") or ())
            branch_states.append(state)
            expected_summon_amounts.update(
                state.get("_card_matrix_expected_summon_amounts") or {})
            final_state = state

        combined_state = dict(final_state or {})
        combined_state["ability_trace"] = branch_traces
        combined_state["_ability_effect_status_by_guid"] = \
            _merge_effect_status_snapshots(branch_states)
        combined_state["_card_matrix_expected_summon_amounts"] = \
            expected_summon_amounts
        traces = _assert_graph_trace(graph, combined_state, db=db)
        nested_graphs = _assert_invoked_ability_graphs(
            db, graph, combined_state)
        return {"effects": len(graph.effects), "traces": traces,
                "nested_abilities": len(nested_graphs),
                "nested_effects": sum(
                    len(ability_graph(DEFAULT_RECORD_STORE, guid).effects)
                    for guid in nested_graphs),
                "paused": False, "choice_branches": 0,
                "random_branches": len(observed_random_values)}

    revealed_branches = _revealed_target_branch_cases(graph)
    if revealed_branches:
        branch_traces = []
        branch_states = []
        expected_orders = set()
        nested_graphs = set()
        final_state = None
        # Filterless SourceRevealed targets commonly represent a cleanup
        # effect such as returning the remaining revealed cards to the deck.
        # That effect also runs when a filtered branch has matched, so it
        # cannot be treated as an exclusive branch for trace assertions.
        exclusive_branch_orders = {
            order for index, orders in revealed_branches
            if graph.targets[index].card_filter
            for order in orders}
        for target_index, orders in revealed_branches:
            scenario = _start_ability_contract(
                db, card_guid, graph,
                revealed_target_branch=target_index)
            scenario["db"] = db
            state = scenario["state"]
            if state.get("resolution_paused"):
                pending = state.get("pending_choice") or {}
                raise AssertionError(
                    f"ability {graph.guid}: revealed-target branch paused "
                    f"on {pending.get('kind')!r}")
            _drain_triggers(
                scenario["handler"], scenario["game"], scenario["session"],
                db, state)
            _assert_champion_destroy_closed(scenario)
            _validate_wire_events(scenario["game"])
            observed_orders = {
                int(entry.get("effect_order"))
                for entry in state.get("ability_trace") or ()
                if str(entry.get("ability_guid") or "").lower() ==
                str(graph.guid).lower() and entry.get("effect_order") is not None}
            missing = set(orders) - observed_orders
            expected_exclusive = (set(orders)
                                  if graph.targets[target_index].card_filter
                                  else set())
            unexpected = (exclusive_branch_orders - expected_exclusive) & \
                observed_orders
            if missing or unexpected:
                raise AssertionError(
                    f"ability {graph.guid}: revealed target {target_index} "
                    f"expected effects missing={sorted(missing)} "
                    f"opposite branches={sorted(unexpected)}")
            _assert_graph_trace(
                graph, state, db=db, require_complete=False)
            nested_graphs.update(
                _assert_invoked_ability_graphs(db, graph, state))
            expected_orders.update(orders)
            branch_traces.extend(state.get("ability_trace") or ())
            branch_states.append(state)
            final_state = state
        combined_state = dict(final_state or {})
        combined_state["ability_trace"] = branch_traces
        combined_state["_ability_effect_status_by_guid"] = \
            _merge_effect_status_snapshots(branch_states)
        traces = _assert_graph_trace(graph, combined_state, db=db)
        return {"effects": len(graph.effects), "traces": traces,
                "nested_abilities": len(nested_graphs),
                "nested_effects": sum(
                    len(ability_graph(DEFAULT_RECORD_STORE, guid).effects)
                    for guid in nested_graphs),
                "paused": False, "choice_branches": 0,
                "revealed_branches": len(revealed_branches)}

    initial = _start_ability_contract(db, card_guid, graph)
    initial["db"] = db
    initial_state = initial["state"]
    if initial_state.get("pending_choice"):
        paths = _choice_paths(db, card_guid, graph)
        nested_graphs = set()
        for path in paths:
            scenario = _start_ability_contract(db, card_guid, graph)
            scenario["db"] = db
            for choice_index in path:
                _resume_choice_selection(scenario, choice_index)
            state = scenario["state"]
            if state.get("resolution_paused"):
                pending = state.get("pending_choice") or {}
                raise AssertionError(
                    f"ability {graph.guid}: choice path {path!r} left a "
                    f"continuation ({pending.get('kind')!r}) unresolved")
            _drain_triggers(
                scenario["handler"], scenario["game"], scenario["session"],
                db, state)
            _assert_champion_destroy_closed(scenario)
            _validate_wire_events(scenario["game"])
            _assert_graph_trace(graph, state, db=db)
            nested_graphs.update(
                _assert_invoked_ability_graphs(db, graph, state))
            _assert_selected_choice_abilities(
                db, state.get("_card_matrix_played_choice_uids", ()), state)
        return {"effects": len(graph.effects), "traces": 0,
                "nested_abilities": len(nested_graphs),
                "nested_effects": sum(
                    len(ability_graph(DEFAULT_RECORD_STORE, guid).effects)
                    for guid in nested_graphs),
                "paused": False, "choice_branches": len(paths)}

    if not initial_state.get("resolution_paused"):
        _drain_triggers(
            initial["handler"], initial["game"], initial["session"], db,
            initial_state)
    _assert_champion_destroy_closed(initial)
    _validate_wire_events(initial["game"])
    traces = _assert_graph_trace(graph, initial_state, db=db)
    nested_graphs = _assert_invoked_ability_graphs(db, graph, initial_state)
    return {"effects": len(graph.effects), "traces": traces,
            "nested_abilities": len(nested_graphs),
            "nested_effects": sum(
                len(ability_graph(DEFAULT_RECORD_STORE, guid).effects)
                for guid in nested_graphs),
            "paused": bool(initial_state.get("resolution_paused")),
            "choice_branches": 0}


def _run_ability_contracts(db, card_guid):
    """Run one generated effect contract for every authored card ability."""
    row = db.execute(
        "SELECT abilities_json FROM card_templates WHERE guid=?",
        (card_guid,)).fetchone()
    ability_guids = [str(value).lower() for value in
                     _json_list(row[0] if row else "[]")]
    abilities = effects = choice_branches = nested_abilities = 0
    for ability_guid in ability_guids:
        graph = ability_graph(DEFAULT_RECORD_STORE, ability_guid)
        if graph is None:
            raise AssertionError(
                f"card {card_guid}: missing ability graph {ability_guid}")
        result = _run_ability_contract(db, card_guid, graph)
        abilities += 1
        effects += (len(graph.effects) + int(result.get("nested_effects", 0)))
        nested_abilities += int(result.get("nested_abilities", 0))
        choice_branches += int(result.get("choice_branches", 0))
    return abilities, effects, choice_branches, nested_abilities


def _run_card(db, metadata, *, trace=False):
    handler = HandlerStub(db)
    session = SessionStub()
    state = _new_state(handler, trace=trace)
    handler._current_bstate = state
    graphs = _card_graphs(metadata["ability_guids"])
    for graph in graphs:
        for effect in graph.effects:
            if effect.concrete_type not in _KNOWN_EFFECTS:
                raise AssertionError(
                    f"{metadata['name']}: unregistered {effect.concrete_type}")

    game = handler._fresh_game(session, PLAYER_UID, AI_UID, state)
    source_uid = metadata["source_uid"]
    source_location = "PlayedResources" if _is_resource(metadata["card_type"]) \
        else "CastSpells"
    _announce_card(game, handler, db, source_uid, PLAYER_OWNER, source_location)
    state["resolving_source_uid"] = source_uid
    state["resolving_owner_id"] = PLAYER_OWNER

    trigger_graphs = [graph for graph in graphs if graph.trigger_event_type]
    cast_graphs = [graph for graph in graphs
                   if not graph.trigger_event_type and not graph.manual]
    paused = False

    if _is_permanent(metadata["card_type"]):
        db.execute(
            "UPDATE game_cards SET location='warzone' WHERE session_id=? "
            "AND card_uid=?", (SESSION_ID, source_uid))
        db.commit()
        _announce_card(game, handler, db, source_uid, PLAYER_OWNER, "warzone")
        entry_graphs = [graph for graph in trigger_graphs
                        if str(graph.trigger_event_type).rsplit(".", 1)[-1]
                        == "CardEnteredZoneEvent"]
        for graph in entry_graphs:
            _dispatch_graph_event(handler, game, session, db, state, graph,
                                   metadata)
    else:
        activations = _activation_map(
            db, cast_graphs, source_uid, PLAYER_OWNER, state, handler, metadata)
        if cast_graphs:
            with contextlib.redirect_stdout(io.StringIO()):
                resolve_port_played_spell(
                    game, session, db, handler, PLAYER_UID, AI_UID, state,
                    [graph.guid for graph in cast_graphs],
                    activations=activations, played_from_hand=True)
            paused = bool(state.get("resolution_paused"))
        if not paused and not _is_resource(metadata["card_type"]):
            # A cast event observes the card while it is still on CastSpells;
            # only then does the ordinary spell move to the discard.
            cast_event = TriggerEvent(
                "CardCastEvent", PLAYER_CHAMP, PLAYER_OWNER, source_uid,
                PLAYER_OWNER, {"zones": ("warzone", "champions")})
            trigger_module.NativeTriggerBackend()(
                db=db, handler=handler, game=game, session=session,
                player_uid=PLAYER_UID, ai_uid=AI_UID, battle_state=state,
                event=cast_event)
            _drain_triggers(handler, game, session, db, state)
            current_location = db.execute(
                "SELECT location FROM game_cards WHERE session_id=? "
                "AND card_uid=?", (SESSION_ID, source_uid)).fetchone()
            # C# FinishResolvingCard only moves a spell out of CastSpells.
            # Effects that already moved the source (for example, a
            # transform followed by FinishMovingCardToWarzone) keep their
            # authored destination. Mirror the production RulesPort
            # finalizer, which also checks the current zone after resolution.
            if current_location and str(current_location[0]).lower() == "castspells":
                db.execute(
                    "UPDATE game_cards SET location='discard' WHERE session_id=? "
                    "AND card_uid=?", (SESSION_ID, source_uid))
                db.commit()
                _announce_card(
                    game, handler, db, source_uid, PLAYER_OWNER, "discard")
                expected_location = "discard"
            else:
                expected_location = (str(current_location[0]).lower()
                                     if current_location else "")
            finalized_location = db.execute(
                "SELECT location FROM game_cards WHERE session_id=? "
                "AND card_uid=?", (SESSION_ID, source_uid)).fetchone()
            if (not finalized_location or
                    str(finalized_location[0]).lower() != expected_location):
                raise AssertionError(
                    f"{metadata['name']}: spell finalizer changed an "
                    f"effect-selected zone: expected={expected_location!r} "
                    f"actual={finalized_location[0] if finalized_location else None!r}")

    if not paused:
        # Exercise every authored trigger family for this card using a fresh
        # event shape.  Entry triggers were already raised by the play path.
        seen = set()
        for graph in trigger_graphs:
            event_name = str(graph.trigger_event_type).rsplit(".", 1)[-1]
            if event_name == "CardEnteredZoneEvent":
                continue
            key = (event_name, _trigger_zone(graph.trigger_collection_flags))
            if key in seen:
                continue
            seen.add(key)
            _dispatch_graph_event(handler, game, session, db, state, graph,
                                   metadata)

    source_row = db.execute(
        "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
        (SESSION_ID, source_uid)).fetchone()
    if source_row is None:
        raise AssertionError(f"{metadata['name']}: source card disappeared")
    if not paused and str(source_row[0]) == "CastSpells":
        raise AssertionError(
            f"{metadata['name']}: completed card is stranded on CastSpells")
    if paused and not any(
            state.get(key) for key in (
                "pending_choice", "pending_trigger", "pending_deck_search",
                "pending_conversation", "pending_discard_continuation",
                "pending_transform_cards")):
        raise AssertionError(
            f"{metadata['name']}: resolution paused without a continuation")

    _validate_wire_events(game)
    return {
        "events": len(game.events),
        "paused": paused,
        "triggered": len(trigger_graphs),
        "cast": len(cast_graphs),
        "ability_trace": list(state.get("ability_trace") or ())
        if trace else [],
    }


def _card_rows(db, *, set_guid=None, only=None, ability_text=None,
               rarities=None):
    query = ("SELECT guid, abilities_json FROM card_templates "
             "WHERE (? IS NULL OR set_guid=?) "
             "AND (?='' OR LOWER(name) LIKE LOWER(?)) "
             "ORDER BY guid")
    needle = str(only or "")
    params = [set_guid, set_guid, needle, f"%{needle}%"]
    rarity_values = tuple(sorted({str(value).strip().lower()
                                  for value in (rarities or ())
                                  if str(value).strip()}))
    if rarity_values:
        query = query.replace(
            "ORDER BY guid",
            "AND LOWER(COALESCE(rarity,'')) IN (" +
            ",".join("?" for _ in rarity_values) + ") ORDER BY guid")
        params.extend(rarity_values)
    rows = list(db.execute(query, params))
    if not ability_text:
        return [row[0] for row in rows]
    text_needle = f"%{str(ability_text).lower()}%"
    matching_abilities = {
        str(row[0]).lower()
        for row in db.execute(
            "SELECT ability_guid FROM card_abilities_meta "
            "WHERE LOWER(game_text) LIKE LOWER(?)", (text_needle,))
    }
    selected = []
    for guid, raw_abilities in rows:
        abilities = {
            str(value).lower() for value in _json_list(raw_abilities)
        }
        if abilities & matching_abilities:
            selected.append(guid)
    return selected


def run_matrix(*, all_cards=False, set_guid=None, only=None, ability_text=None,
               rarities=None, limit=0, offset=0, card_timeout=30,
               contracts=False, contracts_only=False):
    if (not all_cards and not set_guid and not only and not ability_text and
            not rarities):
        raise ValueError(
            "pass --all, --set-guid, --only, --ability-text, or --rarity")
    _src = sqlite3.connect(SRC)
    db = sqlite3.connect(":memory:")
    _src.backup(db)
    _src.close()
    failures = []
    passed = 0
    contract_cards = 0
    contract_abilities = 0
    contract_nested_abilities = 0
    contract_effects = 0
    contract_choice_branches = 0
    try:
        guids = _card_rows(
            db, set_guid=set_guid, only=only, ability_text=ability_text,
            rarities=rarities)
        if offset:
            guids = guids[int(offset):]
        if limit:
            guids = guids[:int(limit)]
        completed = 0
        for guid in guids:
            try:
                if not contracts_only:
                    # Keep the ordinary card-play pass on its broad inert
                    # board. Per-effect contracts below use a separate support
                    # fixture with ability-specific authored targets staged.
                    metadata = _seed_board(db, guid)
                    # The production resolver's diagnostic logger is useful
                    # for one card, but makes a catalogue run unreadable.
                    previous_handler = signal.signal(signal.SIGALRM,
                                                     _alarm_handler)
                    signal.setitimer(signal.ITIMER_REAL,
                                     max(1, card_timeout))
                    try:
                        with contextlib.redirect_stdout(io.StringIO()):
                            previous_backend = \
                                trigger_module.NativeTriggerBackend
                            trigger_module.NativeTriggerBackend = \
                                _MatrixTriggerBackend
                            try:
                                _run_card(db, metadata, trace=contracts)
                            finally:
                                trigger_module.NativeTriggerBackend = \
                                    previous_backend
                    finally:
                        signal.setitimer(signal.ITIMER_REAL, 0)
                        signal.signal(signal.SIGALRM, previous_handler)
                if contracts:
                    with contextlib.redirect_stdout(io.StringIO()):
                        previous_backend = \
                            trigger_module.NativeTriggerBackend
                        trigger_module.NativeTriggerBackend = \
                            _MatrixTriggerBackend
                        try:
                            abilities, effects, choice_branches, \
                                nested_abilities = \
                                _run_ability_contracts_with_timeout(
                                    db, guid, card_timeout)
                        finally:
                            trigger_module.NativeTriggerBackend = \
                                previous_backend
                    contract_cards += 1
                    contract_abilities += abilities
                    contract_nested_abilities += nested_abilities
                    contract_effects += effects
                    contract_choice_branches += choice_branches
                passed += 1
            except Exception:
                row = db.execute(
                    "SELECT name FROM card_templates WHERE guid=?", (guid,)
                ).fetchone()
                failures.append((guid, row[0] if row else guid,
                                 traceback.format_exc()))
            completed += 1
            if completed % 10 == 0:
                print(f"  matrix progress: {completed}/{len(guids)} "
                      f"(last={guid})",
                      flush=True)
    finally:
        db.close()
        try:
            os.remove(sandbox_path)
        except OSError:
            pass
    if not contracts_only:
        print(f"card play matrix: {passed}/{len(guids)} cards passed")
    if contracts:
        print("card ability contract matrix: "
              f"{contract_cards}/{len(guids)} cards passed, "
              f"{contract_abilities} card abilities, "
              f"{contract_nested_abilities} invoked child abilities, "
              f"{contract_effects} typed effects, "
              f"{contract_choice_branches} choice branches")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for guid, name, detail in failures:
            print(f"  {name} [{guid}]\n{detail}")
    return len(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true",
                        help="run every card template in the current seed")
    parser.add_argument("--set-guid", default="",
                        help="run one authored set GUID")
    parser.add_argument("--only", default="",
                        help="run cards whose name contains this text")
    parser.add_argument("--ability-text", default="",
                        help="run cards with authored ability text containing "
                             "this text (selection only; rules stay typed)")
    parser.add_argument("--rarity", action="append", default=[],
                        help="select a card rarity; may be repeated")
    parser.add_argument("--limit", type=int, default=0,
                        help="run only the first N selected cards")
    parser.add_argument("--offset", type=int, default=0,
                        help="skip this many selected cards before running")
    parser.add_argument("--card-timeout", type=int, default=30,
                        help="abort one pathological scenario after N seconds")
    parser.add_argument("--contracts", action="store_true",
                        help="assert typed effect postconditions for every "
                             "authored ability; opt-in and slower")
    parser.add_argument("--contracts-only", action="store_true",
                        help="run per-card ability contracts without the "
                             "ordinary card-play pass")
    args = parser.parse_args()
    failures = run_matrix(
        all_cards=args.all, set_guid=args.set_guid or None,
        only=args.only or None, ability_text=args.ability_text or None,
        rarities=args.rarity,
        limit=args.limit, offset=args.offset,
        card_timeout=args.card_timeout,
        contracts=args.contracts or args.contracts_only,
        contracts_only=args.contracts_only)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
