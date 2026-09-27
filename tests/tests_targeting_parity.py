"""Shared RulesPort filter and target selection parity fixtures."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gamedata import DEFAULT_RECORD_STORE
import game_engine
from rules_port.filters import (records_filter_from_metadata,
                                records_filter_matches)
from rules_port.targeting import legal_targets, validate_target_selection
from tests.tests_targeting import (
    EXILE_TPL, PLAIN_TPL, SRC, add_card, make_db,
)


NONE_TARGET = "d1000000-0000-0000-0000-000000000001"
PLAYER_TARGET = "d1000000-0000-0000-0000-000000000002"
DUPLICATE_TARGET = "d1000000-0000-0000-0000-000000000003"
SHARED_NAME_TARGET = "5619c81c-e0ca-6a2d-bb22-11d501aee52a"


def _insert_target(db, guid, kind, player_filter, collections, minimum,
                   maximum, filter_json="{}"):
    db.execute(
        "INSERT OR REPLACE INTO target_templates VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        (guid, "target", 0, 0, 0, 1, player_filter, collections,
         minimum, maximum, filter_json, kind))


def _run(name, action):
    db = make_db()
    try:
        action(db)
        print(f"PASS {name}")
    except AssertionError as exc:
        print(f"FAIL {name}: {exc}")
        raise
    finally:
        db.close()


def _comparison_scope_is_mode_independent(db):
    comparison = {
        "_t": "Game.Shared.Mechanics.Cards.Filters.CompareAttackToLowestFilter",
        "m_ComparisonOp": "GreaterThan",
        "m_CollectionFlags": "Warzone",
        "m_PlayerFilter": "SingleOpponent",
        "m_CardFilter": {
            "_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop",
        },
    }
    for controller, opponent in ((0, 5), (51, 52)):
        state = {"all_cards": [
            {"card_uid": 1, "card_type": "Troop", "location": "warzone",
             "user_id": controller, "attack": 1},
            {"card_uid": 2, "card_type": "Troop", "location": "warzone",
             "user_id": opponent, "attack": 2},
            {"card_uid": 3, "card_type": "Troop", "location": "warzone",
             "user_id": opponent, "attack": 4},
            {"card_uid": 4, "card_type": "Troop", "location": "hand",
             "user_id": opponent, "attack": 99},
        ]}
        target = {"card_uid": 3, "card_type": "Troop", "location": "hand",
                  "user_id": opponent, "attack": 4}
        assert records_filter_matches(
            target, comparison, context=state, player=controller)
        # Self-scoped comparison sees the controller's warzone troop (ATK 1)
        # and excludes the opponent's lower-cost collection entry.
        self_comparison = dict(comparison, m_PlayerFilter="Self")
        assert records_filter_matches(
            target, self_comparison, context=state, player=controller)


def _name_topn_and_combat_filters_read_authored_fields(db):
    name_filter = {
        "_t": "Game.Shared.Mechanics.Cards.Filters.NameContainsFilter",
        "m_ContainsString": "elf$",
        "m_IncludeSubType": 1,
    }
    assert records_filter_matches(
        {"name": "Plain Troop", "card_type": "Troop", "subtype": "Elf"},
        name_filter)
    assert not records_filter_matches(
        {"name": "Plain Troop", "card_type": "Troop", "subtype": "Human"},
        name_filter)

    deck = [
        {"card_uid": 10, "location": "deck", "user_id": 5,
         "position": 0, "card_type": "Resource"},
        {"card_uid": 11, "location": "deck", "user_id": 5,
         "position": 1, "card_type": "Troop"},
        {"card_uid": 12, "location": "deck", "user_id": 5,
         "position": 2, "card_type": "Troop"},
        {"card_uid": 13, "location": "deck", "user_id": 0,
         "position": 2, "card_type": "Troop"},
        {"card_uid": 14, "location": "deck", "user_id": 0,
         "position": 0, "card_type": "Troop"},
        {"card_uid": 15, "location": "deck", "user_id": 0,
         "position": 1, "card_type": "Troop"},
    ]
    top_n = {
        "_t": "Game.Shared.Mechanics.Cards.Filters.TopNOfDeck",
        "m_Amount": 1,
        "m_AddX": 1,
        "m_Filter": {"_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"},
    }
    context = {"all_cards": deck}
    source = {"resource_x_cost_paid": 1}
    assert records_filter_matches(
        deck[1], top_n, source=source, context=context, player=5)
    assert records_filter_matches(
        deck[2], top_n, source=source, context=context, player=5)
    assert not records_filter_matches(
        deck[3], top_n, source=source, context=context, player=5)

    combat_filter = {
        "_t": "Game.Shared.Mechanics.Cards.Filters.BlockingFilter",
        "m_Filter": {
            "_t": "Game.Shared.Mechanics.Cards.Filters.IsCardName",
            "m_CardName": "Attacker",
        },
    }
    combat = {
        "all_cards": [
            {"card_uid": 20, "name": "Attacker", "card_type": "Troop"},
            {"card_uid": 21, "name": "Blocker", "card_type": "Troop"},
        ],
        "ai_blockers": {"20": [21]},
    }
    assert records_filter_matches(
        combat["all_cards"][1], combat_filter, context=combat)


def _none_and_player_target_contracts_match_in_practice_and_pvp(db):
    _insert_target(db, NONE_TARGET, "AbilityTargetTemplate",
                   "SingleOpponent", "None", 0, 1)
    _insert_target(db, PLAYER_TARGET, "PlayerTargetTemplate",
                   "SingleOpponent", "None", 1, 1)
    for controller, opponent, champ_controller, champ_opponent in (
            (5, 0, 0xA01, 0xA02), (51, 52, 0xB01, 0xB02)):
        add_card(db, controller + 1000, controller, EXILE_TPL, "warzone")
        add_card(db, opponent + 1000, opponent, PLAIN_TPL, "hand")
        state = {"_rules_port_suppress_card_properties": True,
                 "champ_map": {str(controller): champ_controller,
                               str(opponent): champ_opponent}}
        candidates = legal_targets(
            db, 1, controller, NONE_TARGET, controller + 1000,
            both_players=True, battle_state=state)
        assert candidates == [], candidates
        # C# base target enumeration with CollectionFlags.None offers no
        # candidates, while direct IsTargetValid treats None as unrestricted.
        assert validate_target_selection(
            db, 1, controller, NONE_TARGET, controller + 1000,
            [opponent + 1000], both_players=True,
            battle_state=state) == [opponent + 1000]

        champions = [(champ_controller, controller, "Controller", 20),
                     (champ_opponent, opponent, "Opponent", 20)]
        assert legal_targets(
            db, 1, controller, PLAYER_TARGET, controller + 1000,
            champions=champions, battle_state=state) == [champ_opponent]
        assert validate_target_selection(
            db, 1, controller, PLAYER_TARGET, controller + 1000,
            [champ_opponent], champions=champions,
            battle_state=state) == [champ_opponent]


def _duplicate_is_collection_local(db):
    _insert_target(db, DUPLICATE_TARGET, "DuplicateCardTargetTemplate",
                   "Self", "Warzone", 1, 1)
    add_card(db, 101, 51, PLAIN_TPL, "warzone", position=1)
    add_card(db, 102, 51, PLAIN_TPL, "warzone", position=2)
    # A same-name copy in another collection cannot make a Warzone duplicate.
    add_card(db, 103, 51, PLAIN_TPL, "hand", position=1)
    add_card(db, 201, 52, PLAIN_TPL, "warzone", position=1)
    state = {"_rules_port_suppress_card_properties": True}
    candidates = legal_targets(
        db, 1, 51, DUPLICATE_TARGET, 101, both_players=True,
        battle_state=state)
    assert candidates == [101, 102], candidates
    assert validate_target_selection(
        db, 1, 51, DUPLICATE_TARGET, 101, [102], both_players=True,
        battle_state=state) == [102]


def _shared_name_uses_custom_card_validation(db):
    source_db = __import__("sqlite3").connect(
        __import__("tests.tests_targeting", fromlist=["SRC"]).SRC)
    try:
        row = source_db.execute(
            "SELECT * FROM target_templates WHERE template_id=?",
            (SHARED_NAME_TARGET,)).fetchone()
    finally:
        source_db.close()
    assert row is not None
    db.execute("INSERT OR REPLACE INTO target_templates VALUES "
               "(?,?,?,?,?,?,?,?,?,?,?,?)", row)
    for uid in range(301, 305):
        add_card(db, uid, 51, PLAIN_TPL, "hand", position=uid - 301)
    # Enumeration honors CardFilter, but SharedNameTargetTemplate's C# direct
    # IsCardValidTarget only checks the same-name count in the selected zones.
    impossible_filter = {
        "_t": "Game.Shared.Mechanics.Cards.Filters.IsResource",
    }
    db.execute("UPDATE target_templates SET filter_json=? WHERE template_id=?",
               (json.dumps(impossible_filter), SHARED_NAME_TARGET))
    db.commit()
    assert legal_targets(
        db, 1, 51, SHARED_NAME_TARGET, 999,
        battle_state={"_rules_port_suppress_card_properties": True}) == []
    assert validate_target_selection(
        db, 1, 51, SHARED_NAME_TARGET, 999,
        [301, 302, 303, 304],
        battle_state={"_rules_port_suppress_card_properties": True}) == [
            301, 302, 303, 304]


def _records_target_catalog_compiles_all_filters_and_modes(db):
    records = DEFAULT_RECORD_STORE.load("AbilityTargetTemplate")
    target_kinds = set()
    filter_kinds = set()
    auxiliary_field_kinds = set()
    mode_flags = set()
    compiled_filter_types = {}

    def collect(node):
        if isinstance(node, dict):
            serialized_type = str(node.get("_t") or "")
            kind = serialized_type.rsplit(".", 1)[-1]
            if serialized_type.startswith(
                    "Game.Shared.Mechanics.Cards.Filters."):
                filter_kinds.add(kind)
                compiled = records_filter_from_metadata(node)
                assert type(compiled).__name__ != "AnyCard", kind
                compiled_filter_types[kind] = type(compiled).__name__
            elif kind == "EffectInputVariable":
                # EffectFields are operands nested in filters, not predicates.
                auxiliary_field_kinds.add(kind)
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    for record in records:
        target = record.target_spec
        target_kinds.add(target.target_kind)
        mode_flags.add((target.is_auto, target.is_random, target.explicit,
                        target.optional, target.allow_best_effort_minimum,
                        bool(target.min_variable or target.max_variable)))
        card_filter = record.to_dict().get("m_CardFilter")
        if card_filter:
            collect(card_filter)
            records_filter_from_metadata(card_filter)

    assert len(records) == 1773, len(records)
    assert len(target_kinds) == 15, target_kinds
    assert len(filter_kinds) == 54, filter_kinds
    assert auxiliary_field_kinds == {"EffectInputVariable"}
    assert len(compiled_filter_types) == len(filter_kinds)
    assert any(flags[0] and flags[1] for flags in mode_flags)
    assert any(flags[2] for flags in mode_flags)
    assert any(flags[4] for flags in mode_flags)
    assert any(flags[5] for flags in mode_flags)


def _nested_filter_edges_are_shared_across_modes(db):
    composite = {
        "_t": "Game.Shared.Mechanics.Cards.Filters.AndCardFilter",
        "m_TargetFilters": [
            {"_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"},
            {"_t": "Game.Shared.Mechanics.Cards.Filters.NotCardFilter",
             "m_TargetFilter": {
                 "_t": "Game.Shared.Mechanics.Cards.Filters.IsTapped"}},
            {"_t": "Game.Shared.Mechanics.Cards.Filters.HasResourceCost",
             "m_ResourceCost": 3, "m_ComparisonOp": "Equals",
             "m_AddX": 1,
             "m_AddCardIntegerVariable": "CostBonus"},
        ],
    }
    for player, opponent in ((5, 0), (51, 52)):
        source = {"card_uid": 900, "resource_x_cost_paid": 1,
                  "int_attrs": {"CostBonus": 1}}
        candidate = {"card_uid": 901,
                     "card_type": int(game_engine.ECardTypes.Troop),
                     "resource_cost": 5, "user_id": opponent,
                     "location": "warzone", "is_tapped": False}
        assert records_filter_matches(
            candidate, composite, source=source, context={}, player=player)
        assert not records_filter_matches(
            dict(candidate, is_tapped=True), composite, source=source,
            context={}, player=player)
        assert not records_filter_matches(
            dict(candidate, card_type=int(game_engine.ECardTypes.Resource)), composite,
            source=source, context={}, player=player)


def _effect_input_filter_values_follow_active_ability_in_both_modes(db):
    """EffectInputVariable operands read activation data during target scans."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from gamedata.semantics import EffectSpec
    from rules_port.resolution import NativeEffectBackend
    from tests.tests_combat import HandlerStub, SessionStub

    input_filter = {
        "_t": "Game.Shared.Mechanics.Cards.Filters.HasResourceCost",
        "m_ResourceCost": 0,
        "m_ComparisonOp": "Equals",
        "m_AddVariable": {
            "_t": "Game.Shared.Mechanics.Abilities.EffectInputVariable",
            "m_InputVariableName": "TheChosenNumber",
        },
    }
    for owner, opponent in ((5, 0), (51, 52)):
        for activation_value in (5, None):
            expected = 2 if activation_value is None else activation_value
            target = SimpleNamespace(
                guid="variable-cost-target",
                target_kind="AbilityTargetTemplate", is_auto=True,
                is_random=False, player_filter="multipleplayers",
                resolved_maximum=lambda _variables: 1)
            effect = EffectSpec(
                guid="variable-filter-probe", concrete_type="ProbeEffect",
                operation="Probe", name="", target_index=0,
                effect_instance_id=0, effect_group_id=0, duration="Instant",
                condition_guid="", optional=False,
                recalculate_targets="True", secondary_target_index=-1,
                output_variables={})
            graph = SimpleNamespace(variables=(SimpleNamespace(
                m_Name="TheChosenNumber", m_DefaultValue=2),))
            activation_variables = ({} if activation_value is None else
                                    {"TheChosenNumber": activation_value})
            ability = SimpleNamespace(
                ability_template_id="variable-filter-test", source_uid=800,
                responsible_player_id=owner, ordered_effects=(effect,),
                metadata=SimpleNamespace(targets=(target,), graph=graph),
                activation=SimpleNamespace(
                    target_map={}, variables=activation_variables))
            game = game_engine.Game(
                1, game_engine.UID.make(244, owner),
                game_engine.UID.make(244, opponent))
            state = {"ability_variables": {"outer": 9}}
            observed = []

            def candidates(*_args, **kwargs):
                active = kwargs["battle_state"]["ability_variables"]
                assert active == {"TheChosenNumber": expected}, active
                return (901,)

            def execute(_effect_type, context, _effect):
                matching = {"card_uid": 901, "resource_cost": expected,
                            "location": "warzone", "user_id": opponent}
                nonmatching = dict(matching, resource_cost=expected + 1)
                observed.append((
                    records_filter_matches(
                        matching, input_filter, source={"card_uid": 800},
                        context=context.bstate, player=owner),
                    records_filter_matches(
                        nonmatching, input_filter, source={"card_uid": 800},
                        context=context.bstate, player=owner)))
                return "probed"

            with patch("rules_port.targeting.legal_targets", candidates):
                NativeEffectBackend()(
                    handler=HandlerStub(db), game=game,
                    session=SessionStub(), db=db,
                    player_uid=game.player_uid, ai_uid=game.ai_uid,
                    battle_state=state, ability=ability,
                    native_effect=execute)
            assert observed == [(True, False)], observed
            assert state["ability_variables"] == {"outer": 9}, state


def _confirmed_filter_divergences_match_client_semantics(db):
    from rules_port.filters import _records_filter_card, filter_from_metadata

    champion = _records_filter_card({"card_type": "Champion"})
    choice = _records_filter_card({"card_type": "Choice"})
    assert filter_from_metadata({"type": "IsChampion"}).matches(champion)
    assert not filter_from_metadata({"type": "IsChampion"}).matches(choice)
    assert not filter_from_metadata({"type": "IsTroop"}).matches(choice)
    assert filter_from_metadata({"type": "IsType",
                                 "m_CardType": "Choice"}).matches(choice)
    assert not filter_from_metadata({"type": "IsType",
                                     "m_CardType": "Unknown"}).matches(champion)
    assert filter_from_metadata({"type": "IsArtifact"}).matches({"card_type": 32})
    assert not filter_from_metadata({"type": "IsArtifact"}).matches({"card_type": 4})

    quick = {"card_type": "BasicAction", "attributes": 268435456}
    plain = {"card_type": "BasicAction"}
    assert filter_from_metadata({"type": "IsQuick"}).matches(quick)
    assert filter_from_metadata({"type": "IsBasic"}).matches(plain)
    assert not filter_from_metadata({"type": "IsBasic"}).matches(quick)
    assert filter_from_metadata({"type": "IsToken"}).matches({"card_type": "Token"})

    assert filter_from_metadata({"type": "IsSubType",
                                 "subtype": "Robot"}).matches(
        {"subtype": "Dwarf Robot"})
    assert not filter_from_metadata({"type": "IsSubType",
                                     "subtype": "Orc"}).matches(
        {"subtype": "Orcish Cleric"})

    assert filter_from_metadata({"type": "HasCountersValue", "m_Amount": 2,
                                 "m_ComparisonOp": "GreaterThanOrEqual",
                                 "m_CounterType": "charge"}).matches(
        {"counters": {"charge": 3}})
    assert filter_from_metadata({"type": "HasCountersValue", "m_Amount": 2,
                                 "m_ComparisonOp": "GreaterThanOrEqual",
                                 "m_CounterType": "abc"}).matches(
        {"counters": {"charge": 3}, "counter_guids": {"charge": "abc"}})

    assert filter_from_metadata({"type": "SetNumberFilter",
                                 "m_SetNumber": 7}).matches(
        {"set_id": "set-a", "set_number": 7})
    assert not filter_from_metadata({"type": "SetNumberFilter",
                                     "m_SetNumber": 3}).matches(
        {"set_id": "set-a", "set_number": 7})
    assert filter_from_metadata({"type": "SetIdFilter",
                                 "m_SetId": "set-a"}).matches(
        {"set_id": "set-a"})

    assert filter_from_metadata(
        {"type": "HasASharedShardWithTopOfChainFilter"}).matches(
            {"shards": 4}, session={"top_of_chain": {"shards": 4}})

    assert filter_from_metadata({"type": "HasAttackValue",
                                 "value": 5}).matches({"attack": 3})
    assert not filter_from_metadata({"type": "HasAttackValue",
                                     "value": 3}).matches({"attack": 3})

    assert filter_from_metadata({"type": "IsPromo"}).matches(
        {"is_promo": False})
    assert not filter_from_metadata({"type": "IsPromo",
                                     "m_HasAlternateArt": 1}).matches({})
    assert filter_from_metadata(
        {"type": "IsAlternateArt", "m_AlternateArtPref": "NoAA"}).matches(
            {"alternate_art": False})
    assert not filter_from_metadata(
        {"type": "IsAlternateArt", "m_AlternateArtPref": "OnlyAA"}).matches(
            {"alternate_art": False})

    assert filter_from_metadata({"type": "IsSocketed"}).matches({"gems": 0})
    assert filter_from_metadata(
        {"type": "IsSocketed", "m_SocketedValue": 1,
         "m_MustBeMinor": True}).matches({"gems": 1})
    assert not filter_from_metadata(
        {"type": "IsSocketed", "m_SocketedValue": 1,
         "m_MustBeMinor": True}).matches({"gems": 4})
    assert filter_from_metadata(
        {"type": "IsSocketed", "m_CompareToAbilitySource": True}).matches(
            {"gems": 1}, source={"gems": 5})

    player = type("P", (), {"resource_thresholds": {"ruby": 2}})()
    assert filter_from_metadata(
        {"type": "PlayerMeetsThresholdRequirementsToCast"}).matches(
            {"thresholds": ({"color": "ruby", "quantity": 2},)}, player=player)
    assert not filter_from_metadata(
        {"type": "PlayerMeetsThresholdRequirementsToCast"}).matches(
            {"thresholds": ({"color": "ruby", "quantity": 3},)}, player=player)


def _template_and_cost_value_support(db):
    from rules_port import (AndCardFilter, filter_cost_value,
                            filter_from_metadata, filter_matches_template)
    from rules_port.filters import (HasResourceCost,
                                    CompareCastingCostToSourceCountersFilter)

    picked = {}
    import sqlite3 as _sqlite3
    source = _sqlite3.connect(SRC)
    try:
        rows = source.execute(
            "SELECT guid, card_type FROM card_templates WHERE card_type IN "
            "('Troop','Choice','QuickAction') GROUP BY card_type").fetchall()
    finally:
        source.close()
    for guid, card_type in rows:
        record = DEFAULT_RECORD_STORE.get("CardTemplate", str(guid).lower())
        if record is not None and card_type not in picked:
            picked[card_type] = record
    troop = picked["Troop"]
    choice = picked["Choice"]
    quick = picked["QuickAction"]

    assert filter_matches_template(
        filter_from_metadata({"type": "IsTroop"}), troop)
    assert not filter_matches_template(
        filter_from_metadata({"type": "IsTroop"}), choice)
    assert filter_matches_template(
        filter_from_metadata({"type": "IsType", "m_CardType": "Choice"}),
        choice)
    assert filter_matches_template(
        filter_from_metadata({"type": "IsQuick"}), quick)
    assert not filter_matches_template(
        filter_from_metadata({"type": "IsBasic"}), quick)
    assert filter_matches_template(
        filter_from_metadata({"type": "IsExtendedArt"}), troop)
    assert filter_matches_template(
        filter_from_metadata({"type": "AnyCard"}), troop)
    assert not filter_matches_template(
        filter_from_metadata({"type": "HasTag", "tag": "construct"}), troop)

    cost = int(troop.field("m_ResourceCost", 0) or 0)
    assert filter_matches_template(
        filter_from_metadata({"type": "HasCastingCost", "cost": cost,
                              "comparison": "Equals"}), troop)
    assert filter_matches_template(
        filter_from_metadata({"type": "HasResourceCost",
                              "m_ResourceCost": cost,
                              "comparison": "Equals"}), troop)

    assert filter_cost_value(filter_from_metadata(
        {"type": "HasCastingCost", "cost": 3,
         "comparison": "OneMoreThan"})) == 4
    assert filter_cost_value(filter_from_metadata(
        {"type": "HasCastingCost", "cost": 3,
         "comparison": "TwoMoreThan"})) == 5
    assert filter_cost_value(AndCardFilter((
        filter_from_metadata({"type": "HasCastingCost", "cost": 3}),
        filter_from_metadata({"type": "HasCastingCost", "cost": 5})))) == 5
    assert filter_cost_value(CompareCastingCostToSourceCountersFilter(
        comparison="OneLessThan", counter_type="charge"),
        source_card={"counters": {"charge": 3}}) == 2
    assert filter_cost_value(HasResourceCost(
        cost=4, comparison="OneMoreThan")) == 5

    assert records_filter_matches(
        {"tags": {"construct": 0}},
        {"_t": "Game.Shared.Mechanics.Cards.Filters.HasTag",
         "m_Tag": "Construct"})
    assert not records_filter_matches(
        {}, {"_t": "Game.Shared.Mechanics.Cards.Filters.HasTag",
             "m_Tag": "Construct"})

    from rules_port.filters import OtherTroops
    card = {"card_uid": 7, "card_type": "Troop"}
    effect = {"target_map": {0: ({"card_uid": 7},),
                             1: ({"card_uid": 8},)}}
    assert OtherTroops().matches(card, effect=effect)


if __name__ == "__main__":
    _run("target comparator scopes are shared across PVE/PVP ids",
         _comparison_scope_is_mode_independent)
    _run("regex, TopN amount and nested blocker filters",
         _name_topn_and_combat_filters_read_authored_fields)
    _run("None-mask and PlayerTarget mode contracts",
         _none_and_player_target_contracts_match_in_practice_and_pvp)
    _run("DuplicateCardTargetTemplate stays collection-local",
         _duplicate_is_collection_local)
    _run("SharedName direct target validation follows C# override",
         _shared_name_uses_custom_card_validation)
    _run("all Records target filters and selection modes compile",
         _records_target_catalog_compiles_all_filters_and_modes)
    _run("nested filter edge cases agree for PVE and PVP owners",
         _nested_filter_edges_are_shared_across_modes)
    _run("EffectInputVariable target filters use active ability values",
         _effect_input_filter_values_follow_active_ability_in_both_modes)
    _run("confirmed filter divergences match client semantics",
         _confirmed_filter_divergences_match_client_semantics)
    _run("template, cost value, tag and multi-target filter support",
         _template_and_cost_value_support)
