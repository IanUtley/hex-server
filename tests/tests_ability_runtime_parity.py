"""Focused parity checks for shared Records ability runtime behavior."""

import os
import json
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

SRC = fresh_database()

import game_engine
from gamedata.semantics import EffectSpec
from rules_port.effects import dispatch
from rules_port.effect_lifetimes import _expired, _intattr_expired
from rules_port.resolution import NativeEffectBackend
from tests.tests_combat import (TPL_GLADIATOR, HandlerStub, SessionStub,
                                add_card, make_db)


def test_repeating_template_applies_nested_card_effect_per_target():
    """LoopCount applies to one whole target instance, then each target."""
    child = SimpleNamespace(
        short_type="CapturedCardEffect",
        guid="",
        raw={
            "_t": "Game.Shared.Mechanics.Abilities.CapturedCardEffect",
            "_v": [{"CapturedCardEffect": 1},
                   {"CardAbilityEffectTemplate": 10}],
            "m_TemplateId": {"m_Guid": "0" * 36},
        })
    parent_template = SimpleNamespace(
        raw={"_t": "Game.Shared.Mechanics.Abilities.RepeatingAbilityEffectTemplate",
             "m_RepeatingEffect": child},
        short_type="RepeatingAbilityEffectTemplate")
    effect = EffectSpec(
        guid="repeat-parent", concrete_type="RepeatingAbilityEffectTemplate",
        operation="Repeating", name="", target_index=0,
        effect_instance_id=0, effect_group_id=0, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={},
        template=parent_template)
    target = SimpleNamespace(
        guid="target-template", target_kind="CardTargetTemplate",
        is_auto=False, is_random=False, player_filter="self",
        resolved_maximum=lambda _variables: 0)
    graph = SimpleNamespace(effects=(effect,), targets=(target,))

    class Ability:
        ability_template_id = "repeat-test"
        source_uid = 100
        responsible_player_id = 5
        ordered_effects = (effect,)
        activation = SimpleNamespace(
            target_map={0: (101, 102)}, variables={})
        metadata = graph

        def value(self, _db, _state, name, *, effect=None, default=0):
            return 3 if name == "m_LoopCount" else default

        def template_value(self, _db, _state, name, *, effect=None,
                           default=None):
            return child if name == "m_RepeatingEffect" else default

    db = make_db()
    try:
        game = game_engine.Game(
            1, game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000))
        applied = []

        def execute(effect_type, context, nested):
            if effect_type == "RepeatingAbilityEffectTemplate":
                return dispatch(effect_type, context, nested)
            applied.append(context.target())
            return "captured"

        NativeEffectBackend()(
            handler=HandlerStub(db), game=game, session=SessionStub(), db=db,
            player_uid=game.player_uid, ai_uid=game.ai_uid, battle_state={},
            ability=Ability(), native_effect=execute)
        assert applied == [101, 102, 101, 102, 101, 102]
    finally:
        db.close()


def test_usage_limits_and_cooldowns_keep_independent_persisted_state():
    from pvp_db import (db_card_ability_use_counts,
                        db_card_cooldown_counts,
                        db_decrement_card_cooldowns_for_owner,
                        db_record_card_ability_use, db_set_card_cooldown)

    db = make_db()
    try:
        add_card(db, 120, 5, TPL_GLADIATOR)
        assert db_record_card_ability_use(
            1, 120, "ability-a", 7, conn=db,
            uses_per_game=False, uses_per_turn=True) == (0, 1)
        assert db_record_card_ability_use(
            1, 120, "ability-a", 7, conn=db,
            uses_per_game=True, uses_per_turn=True) == (1, 2)
        db_set_card_cooldown(1, 120, "ability-a", 3, conn=db)
        assert db_card_ability_use_counts(
            1, 120, "ability-a", 7, conn=db) == (1, 2)
        assert db_card_cooldown_counts(1, 120, conn=db) == {"ability-a": 3}
        assert db_decrement_card_cooldowns_for_owner(
            1, 5, conn=db) == [120]
        assert db_card_cooldown_counts(1, 120, conn=db) == {"ability-a": 2}
    finally:
        db.close()


def test_duration_expiry_uses_client_turn_and_target_boundaries():
    db = make_db()
    try:
        assert _expired(
            {"duration": "EndOfTurn", "owner_id": 9}, db, 1,
            {"turn_number": 4}, boundary="end_turn", boundary_owner=5)
        next_turn = {"duration": "EndOfNextTurn", "turn_number": 4}
        assert not _expired(next_turn, db, 1, {"turn_number": 4},
                            boundary="end_turn", boundary_owner=5)
        assert _expired(next_turn, db, 1, {"turn_number": 5},
                        boundary="end_turn", boundary_owner=5)
        ready = {
            "duration": "AfterCardsReadyOnPlayersTurn",
            "turn_number": 4, "target_owner_id": 5,
        }
        assert not _intattr_expired(
            ready, db, 1, {"turn_number": 4}, boundary="prep",
            boundary_owner=5)
        assert _intattr_expired(
            ready, db, 1, {"turn_number": 5}, boundary="prep",
            boundary_owner=5)
        assert not _intattr_expired(
            ready, db, 1, {"turn_number": 5}, boundary="prep",
            boundary_owner=6)
    finally:
        db.close()


def test_records_card_count_aura_uses_self_target_and_controller_filter():
    """The authored Pack Raptor aura counts other Raptors its owner controls."""
    from rules_port.static_rules import effective_deltas

    db = sqlite3.connect(SRC)
    template = "b1c80936-b1a9-4a65-8919-2f89895ec4ac"
    ability = "2c502e7e-48e9-fa9d-e947-8b0bf9ef3703"
    abilities = json.dumps([
        "a74d223d-4884-0b4a-4e7f-20fb00f79190", ability])
    card_uids = (820101, 820102, 820103)
    try:
        for uid, owner in zip(card_uids, (5, 5, 6)):
            add_card(db, uid, owner, template)
            db.execute(
                "UPDATE game_cards SET card_abilities=? "
                "WHERE session_id=1 AND card_uid=?", (abilities, uid))
        db.commit()

        first = effective_deltas(db, 1, {}, card_uids[0])
        second = effective_deltas(db, 1, {}, card_uids[1])
        opponent = effective_deltas(db, 1, {}, card_uids[2])
        assert (first["atk"], first["def"]) == (1, 1), first
        assert (second["atk"], second["def"]) == (1, 1), second
        assert (opponent["atk"], opponent["def"]) == (0, 0), opponent
    finally:
        db.execute(
            "DELETE FROM game_cards WHERE session_id=1 AND card_uid IN "
            "(820101,820102,820103)")
        db.commit()
        db.close()


def test_typed_variables_read_highest_charge_and_trigger_damage():
    """Native fields evaluate the shipped variable subclasses from state."""
    import json
    from rules_port.static_rules import _expression_value

    raw = json.dumps({"m_Variables": [
        {"_t": "Game.Shared.Mechanics.Abilities.SourcePlayerChargeVariable",
         "m_Name": "Charges", "m_DefaultValue": 0},
        {"_t": "Game.Shared.Mechanics.Abilities.TriggerEventDamageProperty",
         "m_Name": "Damage", "m_DefaultValue": 9},
        {"_t": "Game.Shared.Mechanics.Abilities.IntAttrAbilityVariable",
         "m_Name": "AbilityDamage", "m_IntAttrName": "DamageDealt",
         "m_DefaultValue": 0},
    ]})
    state = {
        "player_charges": 3,
        "resolving_trigger_event_type": "CardDealtDamageEvent",
        "resolving_trigger_event_data": {
            "event_tac": {"damage": 4, "is_combat_damage": 1}},
        "_ability_damage_dealt": 5,
        "resolving_ability_instance_id": 77,
        "ability_runtime_state": {"77": {"DamageDealt": 6}},
    }
    assert _expression_value(None, 1, state, None, 5, raw, "Charges") == 3
    assert _expression_value(None, 1, state, None, 5, raw, "Damage") == 4
    assert _expression_value(
        None, 1, state, None, 5, raw, "AbilityDamage") == 6
    state["resolving_trigger_event_type"] = "TurnEndedEvent"
    assert _expression_value(None, 1, state, None, 5, raw, "Damage") == 9


def test_static_ins_and_player_stats_read_champion_tac():
    """INS and You player-stat paths use the same champion TAC as C#."""
    from rules_port.statistics import add_card_stat, set_tac_stat
    from rules_port.static_rules import _expression_value

    state = {"champ_map": {5: 820601}, "tac_statistics": {
        "cards": {"820602": {"CardStatsWithSpecificDuration": {
            "InspireCount": 3}}}}}
    set_tac_stat(state, "cards", 820601, "PlayerGameStats",
                 "StartingDeckSize", 40)
    add_card_stat(state, 820603, 5, "ActionsCast", 1)
    raw = json.dumps({"m_Variables": [
        {"_t": "Game.Shared.Mechanics.Abilities.ExpressionAbilityVariable",
         "m_Name": "Inspired", "m_ExpressionText": "INS",
         "m_DefaultValue": 0},
        {"_t": "Game.Shared.Mechanics.Abilities.IntAttrAbilityVariable",
         "m_Name": "StartingDeck", "m_IntAttrName":
         "You>PlayerGameStats>StartingDeckSize", "m_DefaultValue": 0},
    ]})
    assert _expression_value(None, 1, state, 820602, 5, raw,
                             "Inspired") == 3
    assert _expression_value(None, 1, state, 820603, 5, raw,
                             "StartingDeck") == 40
    assert state["tac_statistics"]["cards"]["820601"][
        "PlayerStatsThisTurn"]["ActionsCast"] == 1


def test_expression_cache_and_escalation_follow_card_instance():
    """C# caches flagged expressions and resolves ESC from SourceCard."""
    from rules_port.statistics import (card_escalation_count,
                                       increment_card_escalation)
    from rules_port.static_rules import _expression_value

    db = make_db()
    try:
        add_card(db, 820701, 5, TPL_GLADIATOR)
        add_card(db, 820702, 5, TPL_GLADIATOR)
        db.execute(
            "UPDATE game_cards SET permanent_buffs=? "
            "WHERE session_id=1 AND card_uid=820701",
            (json.dumps({"kept": 7}),))
        db.commit()
        state = {"resolving_ability_instance_id": 91,
                 "ability_variables": {"Count": 2}}
        raw = json.dumps({"m_Variables": [
            {"_t": "Game.Shared.Mechanics.Abilities.AbilityVariable",
             "m_Name": "Count", "m_DefaultValue": 0},
            {"_t": "Game.Shared.Mechanics.Abilities.ExpressionAbilityVariable",
             "m_Name": "Cached", "m_ExpressionText": "Count + 1",
             "m_DontRecalculate": 1, "m_DefaultValue": 0},
            {"_t": "Game.Shared.Mechanics.Abilities.ExpressionAbilityVariable",
             "m_Name": "Live", "m_ExpressionText": "Count + 1",
             "m_DontRecalculate": 0, "m_DefaultValue": 0},
            {"_t": "Game.Shared.Mechanics.Abilities.ExpressionAbilityVariable",
             "m_Name": "Escalation", "m_ExpressionText": "ESC * 4",
             "m_DontRecalculate": 1, "m_DefaultValue": 0},
        ]})
        assert card_escalation_count(db, 1, state, 820701) == 1
        assert increment_card_escalation(db, 1, state, 820701) == 2
        assert card_escalation_count(db, 1, state, 820702) == 1
        assert _expression_value(None, 1, state, 820701, 5,
                                 raw, "Escalation") == 8
        state["resolving_ability_instance_id"] = 92
        assert _expression_value(None, 1, state, 820702, 5,
                                 raw, "Escalation") == 4
        assert _expression_value(None, 1, state, 820701,
                                 5, raw, "Cached") == 3
        state["ability_variables"]["Count"] = 8
        assert _expression_value(None, 1, state, 820701,
                                 5, raw, "Cached") == 3
        assert _expression_value(None, 1, state, 820701,
                                 5, raw, "Live") == 9
        persisted = json.loads(db.execute(
            "SELECT permanent_buffs FROM game_cards WHERE card_uid=820701"
        ).fetchone()[0])
        assert persisted == {"kept": 7, "escalation_count": 2}, persisted
    finally:
        db.close()


def test_source_card_intattr_variable_reads_persisted_card_context():
    """PullFromSourceCard resolves the card's authored IntAttr value."""
    from rules_port.static_rules import _expression_value

    db = make_db()
    try:
        add_card(db, 820301, 5, TPL_GLADIATOR)
        db.execute(
            "UPDATE game_cards SET permanent_buffs=? "
            "WHERE session_id=1 AND card_uid=?",
            (json.dumps({"int_attrs": {"Rage": 4}}), 820301))
        db.commit()
        raw = json.dumps({"m_Variables": [{
            "_t": "Game.Shared.Mechanics.Abilities.IntAttrAbilityVariable",
            "m_Name": "CurrentRage", "m_DefaultValue": 0,
            "m_IntAttrName": "Rage", "m_PullFromSourceCard": 1,
            "m_HalfRoundedUp": 0,
        }]})
        assert _expression_value(
            db, 1, {}, 820301, 5, raw, "CurrentRage") == 4
    finally:
        db.close()


def test_pvp_source_player_variables_use_raw_owner_ids_and_any_thresholds():
    from rules_port.static_rules import _expression_value

    raw = json.dumps({"m_Variables": [
        {"_t": "Game.Shared.Mechanics.Abilities.SourcePlayerChargeVariable",
         "m_Name": "Charge", "m_DefaultValue": 0},
        {"_t": "Game.Shared.Mechanics.Abilities.SourcePlayerHealthVariable",
         "m_Name": "Health", "m_DefaultValue": 0},
        {"_t": "Game.Shared.Mechanics.Abilities.SourcePlayerResourceAbilityVariable",
         "m_Name": "Resource", "m_LookUpTemporaryResources": 1,
         "m_DefaultValue": 0},
        {"_t": "Game.Shared.Mechanics.Abilities.SourcePlayerThresholdAbilityVariable",
         "m_Name": "AnyThreshold", "m_Threshold": "Any",
         "m_CountUniques": 0, "m_DefaultValue": 0},
        {"_t": "Game.Shared.Mechanics.Abilities.SourcePlayerThresholdAbilityVariable",
         "m_Name": "UniqueThreshold", "m_Threshold": "Any",
         "m_CountUniques": 1, "m_DefaultValue": 0},
    ]})
    state = {
        "pvp": True, "pids": [501, 902],
        "player_health": 18, "ai_health": 9,
        "player_charges": 2, "ai_charges": 7,
        "player_resources": 4, "ai_resources": 6,
        "player_threshold": {4: 1, 8: 2},
        "ai_threshold": {32: 3, 64: 2},
    }
    assert _expression_value(None, 1, state, None, 902, raw, "Charge") == 7
    assert _expression_value(None, 1, state, None, 902, raw, "Health") == 9
    assert _expression_value(None, 1, state, None, 902, raw, "Resource") == 6
    assert _expression_value(
        None, 1, state, None, 902, raw, "AnyThreshold") == 5
    assert _expression_value(
        None, 1, state, None, 902, raw, "UniqueThreshold") == 2


def test_list_count_filters_and_trigger_properties_keep_authored_identity():
    from rules_port.static_rules import _expression_value

    db = make_db()
    try:
        add_card(db, 820401, 5, TPL_GLADIATOR)
        add_card(db, 820402, 6, TPL_GLADIATOR)
        db.execute("UPDATE game_cards SET card_attack_mod=3 WHERE card_uid=820401")
        db.execute("UPDATE game_cards SET card_attack_mod=7 WHERE card_uid=820402")
        db.commit()
        raw = json.dumps({"m_Variables": [
            {"_t": "Game.Shared.Mechanics.CountListAttrAbilityVariable",
             "m_Name": "Troops", "m_ListAttrName": "VoidedCards",
             "m_DefaultValue": 0, "m_CardFilter": {
                 "_t": "Game.Shared.Mechanics.Cards.Filters.IsType",
                 "m_CardType": "Troop"}},
            {"_t": "Game.Shared.Mechanics.Abilities.SumVariableInListAttrCardsAbilityVariable",
             "m_Name": "AttackSum", "m_ListAttrName": "VoidedCards",
             "m_Property": "CurrentAttackValue", "m_DefaultValue": 0},
            {"_t": "Game.Shared.Mechanics.Abilities.TriggerSourcePropertyVariable",
             "m_Name": "SourceAttack", "m_Property": "CurrentAttackValue",
             "m_DefaultValue": 0},
            {"_t": "Game.Shared.Mechanics.Abilities.TriggerTargetPropertyVariable",
             "m_Name": "TargetAttack", "m_Property": "CurrentAttackValue",
             "m_DefaultValue": 0},
        ]})
        state = {
            "ability_lists": {"VoidedCards": [820401, 820402]},
            "resolving_ability": "ability-a",
            "resolving_trigger_source_uid": 820401,
            "resolving_trigger_target_uid": 820402,
        }
        assert _expression_value(
            db, 1, state, 820401, 5, raw, "Troops") == 2
        assert _expression_value(
            db, 1, state, 820401, 5, raw, "AttackSum") == 14
        assert _expression_value(
            db, 1, state, 820401, 5, raw, "SourceAttack") == 5
        assert _expression_value(
            db, 1, state, 820401, 5, raw, "TargetAttack") == 9
    finally:
        db.close()


def test_count_list_reads_you_and_all_champion_tac_scopes():
    """CountListAttr honors the authored TAC scope prefix and list identity."""
    from rules_port.static_rules import _expression_value

    db = make_db()
    try:
        add_card(db, 820411, 5, TPL_GLADIATOR)
        add_card(db, 820412, 6, TPL_GLADIATOR)
        raw = json.dumps({"m_Variables": [
            {"_t": "Game.Shared.Mechanics.Abilities.CountListAttrAbilityVariable",
             "m_Name": "YouSeen", "m_ListAttrName":
             "You>CardGameStats>SeenCards", "m_DefaultValue": 9},
            {"_t": "Game.Shared.Mechanics.Abilities.CountListAttrAbilityVariable",
             "m_Name": "AllSeen", "m_ListAttrName":
             "All>CardGameStats>SeenCards", "m_DefaultValue": 9},
        ]})
        state = {
            "champ_map": {5: 821001, 6: 821002},
            "tac_statistics": {"cards": {
                "821001": {"CardGameStats": {
                    "SeenCards": [{"Id": 820411}]}},
                "821002": {"CardGameStats": {
                    "SeenCards": [{"Id": 820412}]}},
            }},
        }
        assert _expression_value(
            db, 1, state, 820411, 5, raw, "YouSeen") == 1
        assert _expression_value(
            db, 1, state, 820411, 5, raw, "AllSeen") == 2
    finally:
        db.close()


def test_card_cast_and_effect_accounting_writes_readable_tac_stats():
    from rules_port.cast_stats import record_card_cast
    from rules_port.statistics import (add_ability_stat, add_card_stat,
                                       add_champion_card_stat, tac_stat)

    state = {"champ_map": {5: 820501}}
    record_card_cast(
        state, 5, resource=False, card_uid=820502,
        card_type="QuickAction|Troop", resource_cost=4)
    add_ability_stat(state, "CountersRemoved", 2, instance_id=31)
    add_ability_stat(state, "ChargePointsSpent", 1, instance_id=31)
    add_card_stat(state, 820502, 5, "DamageDealt", 3)
    add_champion_card_stat(state, 5, "ChargePointsGained", 2)
    assert tac_stat(state, "cards", 820502, "CardStatsThisTurn", "CardsCast") == 1
    assert tac_stat(state, "cards", 820502, "CardStatsThisTurn", "DamageDealt") == 3
    assert tac_stat(state, "cards", 820501, "CardStatsThisTurn", "ActionsCast") == 1
    assert tac_stat(state, "cards", 820501, "PlayerStatsThisTurn",
                     "ActionsCast") == 1
    assert tac_stat(state, "cards", 820501, "PlayerStatsThisTurn",
                     "HighestCostCardsCast") == 4
    assert tac_stat(state, "cards", 820501, "CardStatsThisTurn", "ChargePointsGained") == 2
    assert state["ability_runtime_state"]["31"] == {
        "CountersRemoved": 2, "ChargePointsSpent": 1}


def test_pvp_effect_view_keeps_ability_statistics_on_champion_tac():
    from rules_port.pvp_view import apply_effect_view, to_effect_view
    from rules_port.statistics import add_card_stat, tac_stat

    state = {"pvp": True, "pids": [501, 902],
             "champ_map": {"501": 820701, "902": 820702}}
    view = to_effect_view(state, 501, 902)
    add_card_stat(view, 820703, 501, "DamageDealt", 2)
    view.setdefault("escalation_counts_by_card", {})["820703"] = 3
    apply_effect_view(state, view, 501, 902)
    assert tac_stat(state, "cards", 820701,
                    "PlayerGameStats", "DamageDealt") == 2
    assert state["escalation_counts_by_card"]["820703"] == 3


def test_typed_count_and_highest_variables_apply_player_scope():
    """MultipleOpponents and MultiplePlayers have distinct card pools."""
    from rules_port.static_rules import _expression_value

    db = sqlite3.connect(SRC)
    card_uids = (820201, 820202)
    try:
        low, high = db.execute(
            "SELECT guid, cost FROM card_templates "
            "WHERE card_type='Troop' AND COALESCE(abilities_json,'[]')='[]' "
            "GROUP BY guid ORDER BY cost, guid").fetchall()[0], db.execute(
            "SELECT guid, cost FROM card_templates "
            "WHERE card_type='Troop' AND COALESCE(abilities_json,'[]')='[]' "
            "GROUP BY guid ORDER BY cost DESC, guid LIMIT 1").fetchone()
        assert low and high and int(low[1]) < int(high[1]), (low, high)
        add_card(db, card_uids[0], 5, low[0])
        add_card(db, card_uids[1], 6, high[0])
        raw = json.dumps({"m_Variables": [
            {"_t": "Game.Shared.Mechanics.Abilities.CardCountAbilityVariable",
             "m_Name": "OpponentTroops", "m_DefaultValue": 7,
             "m_PlayerFilter": "MultipleOpponents",
             "m_CollectionFlags": "Warzone", "m_CardFilter": None},
            {"_t": "Game.Shared.Mechanics.Abilities.HighestCardAbilityVariable",
             "m_Name": "HighestCost", "m_DefaultValue": 0,
             "m_PlayerFilter": "MultiplePlayers",
             "m_CollectionFlags": "Warzone", "m_CardFilter": None,
             "m_Property": "ResourceCostTrue"},
        ]})
        assert _expression_value(
            db, 1, {}, card_uids[0], 5, raw, "OpponentTroops") == 1
        assert _expression_value(
            db, 1, {}, card_uids[0], 5, raw, "HighestCost") == int(high[1])
    finally:
        db.execute(
            "DELETE FROM game_cards WHERE session_id=1 AND card_uid IN "
            "(820201,820202)")
        db.commit()
        db.close()


def test_queued_trigger_retains_its_event_payload_for_ability_resolution():
    """Queued C# trigger instances retain the originating event's data."""
    from unittest import mock
    from rules_port.resolution import resolve_port_trigger

    db = make_db()
    try:
        state = {}
        observed = {}

        def resolve(_handler, _game, _session, _db, _player, _ai,
                    battle_state, _guid, _source, _owner, **_kwargs):
            observed["type"] = battle_state.get(
                "resolving_trigger_event_type")
            observed["data"] = battle_state.get(
                "resolving_trigger_event_data")
            battle_state["resolution_paused"] = True
            return "resolved"

        item = {
            "ability_guid": "2c502e7e-48e9-fa9d-e947-8b0bf9ef3703",
            "source_uid": 101, "source_owner_uid": 5,
            "trigger_event_type": "CardDealtDamageEvent",
            "trigger_event_data": {"event_tac": {"damage": 6}},
        }
        with mock.patch("rules_port.resolution.resolve_port_ability", resolve):
            result = resolve_port_trigger(
                HandlerStub(db), None, SessionStub(), db, None, None,
                state, item)
        assert result == "resolved"
        assert observed == {
            "type": "CardDealtDamageEvent",
            "data": {"event_tac": {"damage": 6}},
        }, observed
        assert "resolving_trigger_event_type" not in state
        assert "resolving_trigger_event_data" not in state
    finally:
        db.close()


if __name__ == "__main__":
    tests = [value for name, value in list(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} ability runtime parity checks passed")
