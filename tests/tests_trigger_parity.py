"""Focused shared trigger publication checks for RulesPort effects/phases."""

import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_db import fresh_database

# Keep the runtime DB binding on a disposable static snapshot.
fresh_database()

import game_engine
from rules_port.effects import _int_attribute
from rules_port.session import AuthoritativeSession
from rules_port.token_effects import summon_token
from tests.tests_combat import (HandlerStub, SessionStub, TPL_GLADIATOR,
                                add_card, make_db)


class _SummonContext:
    def __init__(self, db, *, pvp, source_uid, source_owner, target_owner):
        self.db = db
        self.session = SessionStub()
        self.handler = HandlerStub(db)
        self.player_uid = game_engine.UID.make(244, source_owner)
        self.ai_uid = (game_engine.UID.make(244, target_owner) if pvp else
                       game_engine.UID.make(3, 1000))
        self.game = game_engine.Game(
            self.session.session_id, self.player_uid, self.ai_uid)
        self.bstate = {
            "pvp": bool(pvp), "resolving_owner_id": source_owner,
            "resolving_source_uid": source_uid,
        }
        self._modifier_updates = []

    def template_value(self, name, default=None):
        return {
            "m_CardCollection": "Hand",
            "m_CardFilter": {
                "_t": "Game.Shared.Mechanics.Cards.Filters.AndCardFilter",
                "m_TargetFilters": [
                    {"_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"},
                    {"_t": "Game.Shared.Mechanics.Cards.Filters.HasResourceCost",
                     "m_ResourceCost": 1,
                     "m_ComparisonOp": "Equals"},
                ],
            },
            "m_Amount": 2,
            "m_Faction": "Aria",
        }.get(name, default)

    def value(self, name, default=0):
        return 2 if name == "m_Amount" else default

    def resolved_target(self):
        return 2

    def target_owner(self, _target=None, default=None):
        return (self.bstate["target_owner_id"]
                if "target_owner_id" in self.bstate else default)

    def active_talent_guids(self):
        return ()

    def _card_thresholds(self, uid):
        return [8] if int(uid) == int(self.bstate["resolving_source_uid"]) else []

    def _push_modifier_card(self, uid, **kwargs):
        self._modifier_updates.append((int(uid), kwargs))

    def emit_int_attribute_gained(self, uid, attribute, previous, current):
        if int(previous or 0) == 0 and int(current or 0) > 0:
            return self._emit_trigger(
                "CardGainedIntAttrEvent", int(uid),
                self.bstate["target_owner_id"],
                event_int_attribute=str(attribute))
        return None

    def _emit_trigger(self, event_type, source, owner, *,
                      event_int_attribute=None):
        self._published_events.append((
            event_type, int(source), int(owner or 0), None,
            {"event_int_attribute": event_int_attribute}))
        return None


def _conscript_is_filtered_repeatable_and_publishes_faction_in_both_modes(db):
    candidate_guid = "f0000000-0000-0000-0000-000000000001"
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (candidate_guid, "Aria Troop", "Troop", 1, 1, 1, 0, "[]", "[]",
         "", 0, 0, 0))
    for pvp, source_owner, target_owner in ((False, 5, 0), (True, 51, 52)):
        session_id = 1 if not pvp else 2
        source_uid = 101 if not pvp else 201
        target_uid = 102 if not pvp else 202
        add_card(db, source_uid, source_owner, TPL_GLADIATOR)
        add_card(db, target_uid, target_owner, TPL_GLADIATOR, loc="warzone")
        if session_id != 1:
            db.execute(
                "UPDATE game_cards SET session_id=? WHERE card_uid IN (?,?)",
                (session_id, source_uid, target_uid))
        db.execute(
            "UPDATE game_cards SET permanent_buffs=? "
            "WHERE session_id=? AND card_uid=?",
            (json.dumps({"thresholds": [8]}), session_id, source_uid))
        db.commit()
        context = _SummonContext(
            db, pvp=pvp, source_uid=source_uid,
            source_owner=source_owner, target_owner=target_owner)
        context.session.session_id = session_id
        context.bstate["target_owner_id"] = target_owner
        published = []
        context._published_events = published

        def record(_context, event_type, source, owner=None, target=None,
                   *, data=None):
            published.append((event_type, int(source), int(owner or 0),
                              target, dict(data or {})))
            return None

        with mock.patch("rules_port.triggers.dispatch_trigger", side_effect=record):
            result = summon_token(context, {"conscript_event": True,
                                             "conscript_faction": "Aria",
                                             "random_with_replacement": True})
        assert result == "summoned 2 token(s)", result
        created = [int(row[0]) for row in db.execute(
            "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
            "AND template_guid=? AND location='hand' ORDER BY id",
            (session_id, target_owner, candidate_guid)).fetchall()]
        assert len(created) == 2, created
        assert all(json.loads(db.execute(
            "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
            "AND card_uid=?", (session_id, uid)).fetchone()[0])["thresholds"]
                   == [8] for uid in created)
        assert [event[0] for event in published] == [
            "OtherCardCreatedEvent", "CardCreatedEvent",
            "CardEnteredZoneEvent", "CardGainedIntAttrEvent",
            "OtherCardCreatedEvent", "CardCreatedEvent",
            "CardEnteredZoneEvent", "CardGainedIntAttrEvent",
            "ConscriptEvent", "ConscriptEvent"]
        gains = [event for event in published
                 if event[0] == "CardGainedIntAttrEvent"]
        assert [event[1] for event in gains] == created
        assert all(event[4]["event_int_attribute"] == "Ruby"
                   for event in gains)
        conscripts = [event for event in published
                      if event[0] == "ConscriptEvent"]
        assert [event[1] for event in conscripts] == created
        assert all(event[2] == target_owner and event[3] is None and
                   event[4]["event_faction"] == "Aria"
                   for event in conscripts)
        assert [event[0] for event in context._modifier_updates] == created


def _int_attribute_gained_is_a_zero_to_positive_edge(db):
    from rules_port.context import EffectContext

    for pvp, owner, opponent in ((False, 5, 0), (True, 51, 52)):
        uid = 301 if not pvp else 401
        add_card(db, uid, owner, TPL_GLADIATOR)
        player_uid = game_engine.UID.make(244, owner)
        ai_uid = (game_engine.UID.make(244, opponent) if pvp else
                  game_engine.UID.make(3, 1000))
        game = game_engine.Game(1, player_uid, ai_uid)
        state = {"pvp": pvp, "resolving_owner_id": owner}
        context = EffectContext.from_rules_port(
            game, SessionStub(), db, HandlerStub(db), player_uid, ai_uid,
            state, "int-attribute")
        context.target_owner = lambda _target, default=None: owner
        context._push_modifier_card = lambda *_args, **_kwargs: None
        events = []
        context._emit_trigger = lambda *args, **kwargs: (
            events.append((args, kwargs)) or "published")

        assert _int_attribute(context, uid, {
            "attribute": "Valorous", "amount": 1, "operation": "add"})
        assert len(events) == 1, events
        args, kwargs = events[0]
        assert args[:3] == ("CardGainedIntAttrEvent", uid, owner), args
        assert kwargs["event_int_attribute"] == "Valorous", kwargs
        assert _int_attribute(context, uid, {
            "attribute": "Valorous", "amount": 1, "operation": "add"})
        assert len(events) == 1, events


def _phase_exit_hook_is_owned_by_native_phase_states():
    player = game_engine.UID.make(244, 11)
    session = AuthoritativeSession(11, (player,), seed_z=1, seed_w=2)
    seen = []
    session.set_turn_phase_exit_resolver(seen.append)
    phase_names = ("FirstStrikePriorityWindow", "AssignDamage")
    phases = tuple(getattr(game_engine.ETurnPhases, name)
                   for name in phase_names)
    for name in phase_names:
        session.phase_states[name].on_exit(session)
    assert tuple(seen) == phases, seen


def _records_trigger_inventory_is_45_authored_event_types():
    from gamedata import DEFAULT_RECORD_STORE

    names = set()
    for ability in DEFAULT_RECORD_STORE.load("AbilityTemplate"):
        value = ability.field("m_TriggerEventType")
        raw = getattr(value, "raw", None)
        if isinstance(raw, dict):
            value = raw
        if isinstance(value, dict):
            value = (value.get("m_InternalType") or
                     value.get("m_Type") or value.get("type"))
        if value:
            names.add(str(value).rsplit(".", 1)[-1])
    assert len(names) == 45, sorted(names)
    assert {"CardDestroyedEvent", "ConscriptEvent", "CombatEndedEvent",
            "VerdictEvent"}.issubset(names), sorted(names)


def _chain_mutation_events_precede_client_completion_in_both_owner_models(db):
    """C# applies an ability before TopOfChainResolved/RemovedTopOfChain."""
    from rules_port.actions import AbilityResolutionState
    from rules_port.chain_items import resolve_chain_item

    for pvp, owner, opponent, session_id, instance in (
            (False, 5, 0, 1, 71), (True, 51, 52, 2, 72)):
        uid = int(game_engine.UID.make(1, 91000 + instance).uid64)
        add_card(db, uid, owner, TPL_GLADIATOR, loc="CastSpells")
        if session_id != 1:
            db.execute(
                "UPDATE game_cards SET session_id=? WHERE card_uid=?",
                (session_id, uid))
        db.commit()
        game = game_engine.Game(
            session_id, game_engine.UID.make(244, owner),
            game_engine.UID.make(244, opponent) if pvp else
            game_engine.UID.make(3, 1000))
        session = SessionStub()
        session.session_id = session_id
        state = {"pvp": pvp, "stack": []}

        class Host:
            def chain_load(self, _session):
                return state

            def chain_save(self, _session, _state):
                return None

            def chain_new_game(self, _session, _state, _player, _ai):
                return game

            def chain_send(self, *_args):
                return None

            def chain_card_data(self, _game, _scid, template_guid):
                return (template_guid, game_engine.ECardTypes.Troop,
                        "Shamed Gladiator", 2, 2, 2, 0)

            def chain_dispatch(self, *_args, **_kwargs):
                return None

        descriptor = {"kind": "troop", "source_uid": uid,
                      "instance_id": instance}
        ability = SimpleNamespace(
            descriptor=descriptor, instance_id=instance,
            ignores_chain=False)
        result = resolve_chain_item(
            Host(), None, session, db, ability, game.player_uid,
            game.ai_uid)
        assert result is AbilityResolutionState.COMPLETED, result
        names = [event.__class__.__name__ for event in game.events]
        moved = names.index("CardMovedSessionEventArgs")
        played = names.index("TroopCardPlayedSessionEventArgs")
        resolved = names.index("TopOfChainResolvedSessionEventArgs")
        removed = names.index("RemovedTopOfChainSessionEventArgs")
        assert moved < resolved and played < resolved < removed, names
        location = db.execute(
            "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
            (session_id, uid)).fetchone()
        assert location and str(location[0]).lower() == "warzone", location


if __name__ == "__main__":
    db = make_db()
    try:
        _conscript_is_filtered_repeatable_and_publishes_faction_in_both_modes(db)
        print("PASS Conscript filters, repeat selection, thresholds and events in PVE/PVP")
        _int_attribute_gained_is_a_zero_to_positive_edge(db)
        print("PASS CardGainedIntAttrEvent mutation edge in PVE/PVP")
    finally:
        db.close()
    _phase_exit_hook_is_owned_by_native_phase_states()
    print("PASS native phase exit trigger hook")
    _records_trigger_inventory_is_45_authored_event_types()
    print("PASS 45 authored trigger event types inventory")
    _db = make_db()
    try:
        _chain_mutation_events_precede_client_completion_in_both_owner_models(
            _db)
        print("PASS chain mutation/completion ordering in PVE/PVP owner models")
    finally:
        _db.close()
