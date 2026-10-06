"""Focused C# parity checks for typed RulesPort effect conditions."""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# Several focused assertions import the tournament boundary, which imports
# ``db`` lazily. Bind it to a disposable metadata database before that can
# otherwise open the developer's live HConnect file.
from tests.test_db import fresh_database

fresh_database()

from rules_port.condition_context import ConditionContext
from rules_port.conditions import evaluate_condition


class SessionStub:
    session_id = 1


def _context(cards, *, secondary_targets=(), source_owner=5):
    card_by_uid = {int(card["card_uid"]): card for card in cards}
    context = ConditionContext(
        None, SessionStub(), {}, ability_source_uid=1,
        ability_source_owner_id=source_owner,
        effect_secondary_target_uids=secondary_targets)
    context.card = lambda uid: card_by_uid.get(int(uid))
    context._cards_in_zones = lambda zones: [
        card for card in cards
        if str(card.get("location", "")).lower() in zones]
    return context


def test_requires_cards_controlled_same_owner_uses_contingent_target():
    target_champion = {
        "card_uid": 200, "card_type": "Champion", "location": "champions",
        "user_id": 0, "controller_id": 0,
    }
    cards = [target_champion]
    cards.extend({
        "card_uid": 1000 + index, "card_type": "Troop", "subtype": "Egg",
        "location": "deck", "user_id": 0, "controller_id": 0,
    } for index in range(249))
    cards.append({
        "card_uid": 3000, "card_type": "Troop", "subtype": "Egg",
        "location": "deck", "user_id": 5, "controller_id": 5,
    })
    condition = {
        "_t": "Game.Shared.Mechanics.Abilities.Conditions.RequiresCardsControlled",
        "m_CardFilter": {
            "_t": "Game.Shared.Mechanics.Cards.Filters.IsSubType",
            "m_SubType": "Egg",
        },
        "m_CardCollection": "Deck",
        "m_RequiredQuantity": 250,
        "m_ComparisonOp": "GreaterThanOrEqual",
        "m_PlayerFilter": "MultiplePlayers",
        "m_SameOwner": 1,
    }

    # Total Eggs across both owners is 250, but the referenced opposing
    # champion's deck has only 249, so the C# SameOwner gate rejects it.
    context = _context(cards, secondary_targets=(target_champion["card_uid"],))
    assert not evaluate_condition(condition, context)

    cards.append({
        "card_uid": 3001, "card_type": "Troop", "subtype": "Egg",
        "location": "deck", "user_id": 0, "controller_id": 0,
    })
    assert evaluate_condition(condition, context)

    # C# also rejects SameOwner when the effect mapping has no referenced
    # target, even if enough matching cards exist globally.
    context_without_target = _context(cards)
    assert not evaluate_condition(condition, context_without_target)

    # The distinct SingleOpponent enum counts the opponent's cards, while
    # SinglePlayer follows C#'s explicit no-player branch.
    opponent_only = dict(condition, m_SameOwner=0,
                         m_PlayerFilter="SingleOpponent")
    assert evaluate_condition(opponent_only, context)
    single_player = dict(opponent_only, m_PlayerFilter="SinglePlayer")
    assert not evaluate_condition(single_player, context)


def test_requires_cards_controlled_distinct_facts_and_quantity_filter():
    source = {
        "card_uid": 1, "card_type": "Constant", "location": "warzone",
        "user_id": 5, "controller_id": 5, "cost": 0,
        "name": "Source", "subtype": "",
    }
    troops = [
        {"card_uid": 2, "card_type": "Troop", "location": "warzone",
         "user_id": 5, "controller_id": 5, "cost": 1, "name": "Wolf",
         "subtype": "Dwarf Soldier"},
        {"card_uid": 3, "card_type": "Troop", "location": "warzone",
         "user_id": 5, "controller_id": 5, "cost": 1, "name": "Wolf",
         "subtype": "Shin'hare Ranger"},
        {"card_uid": 4, "card_type": "Troop", "location": "warzone",
         "user_id": 5, "controller_id": 5, "cost": 2, "name": "Fox",
         "subtype": "Orc Scout"},
    ]
    context = _context([source, *troops])
    base = {
        "_t": "Game.Shared.Mechanics.Abilities.Conditions.RequiresCardsControlled",
        "m_CardFilter": {
            "_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop",
        },
        "m_CardCollection": "Warzone",
        "m_RequiredQuantity": 2,
        "m_ComparisonOp": "Equals",
        "m_PlayerFilter": "Self",
    }

    # The source has three troops, but only two distinct resource costs.
    distinct_costs = dict(base, m_OnlyIncludeDifferentCosts=1)
    assert evaluate_condition(distinct_costs, context)
    assert not evaluate_condition(base, context)

    # C# counts each distinct printed card name once.
    distinct_names = dict(
        base, m_OnlyIncludeDifferentNames=1, m_RequiredQuantity=2)
    assert evaluate_condition(distinct_names, context)

    # C# counts each race in the selected faction once; Orc is Aria and does
    # not contribute to the two distinct Underworld races.
    distinct_underworld_races = dict(
        base, m_OnlyIncludeDifferentRacesForFaction="Underworld")
    assert evaluate_condition(distinct_underworld_races, context)

    # QuantityCardFilter supplies the comparison target through the same
    # collection and player filters.
    quantity_filter = dict(
        base, m_RequiredQuantity=99,
        m_QuantityCardFilter={
            "_t": "Game.Shared.Mechanics.Cards.Filters.IsType",
            "m_CardType": "Constant",
        })
    one_troop = _context([source, troops[0]])
    assert evaluate_condition(quantity_filter, one_troop)

    # The opposing match is the count on the largest opposing player board,
    # even when those cards are spread across collections.
    opposing = [
        {"card_uid": 10 + index, "card_type": "Troop",
         "location": location, "user_id": 0, "controller_id": 0,
         "cost": index, "name": f"Enemy {index}", "subtype": "Human"}
        for index, location in enumerate(("hand", "deck", "warzone"))
    ]
    highest_match = dict(
        base, m_ComparisonOp="LessThan",
        m_QuantityIsHighestOpposingChampionsMatch=1)
    assert evaluate_condition(
        highest_match, _context([source, *troops[:2], *opposing]))

    # C# iterates every opposing Player, even when one has no cards in any
    # collection. The lowest opposing match is therefore zero in this state.
    empty_opponent_context = _context([source])
    empty_opponent_context.bstate = {"pvp": True, "pids": [5, 0]}
    lowest_match = dict(
        base, m_RequiredQuantity=99, m_ComparisonOp="Equals",
        m_QuantityIsLowestOpposingChampionsMatch=1)
    assert evaluate_condition(lowest_match, empty_opponent_context)


def test_requires_cards_controlled_preserves_zero_required_quantity():
    source = {
        "card_uid": 1, "card_type": "BasicAction", "location": "hand",
        "user_id": 5, "controller_id": 5, "name": "Source",
    }
    candlekin = {
        "card_uid": 2, "card_type": "Troop", "location": "warzone",
        "user_id": 5, "controller_id": 5, "name": "Candlekin",
    }
    condition = {
        "_t": "Game.Shared.Mechanics.Abilities.Conditions.RequiresCardsControlled",
        "m_CardFilter": {
            "_t": "Game.Shared.Mechanics.Cards.Filters.IsCardName",
            "m_CardName": "Candlekin",
        },
        "m_CardCollection": "Warzone",
        "m_RequiredQuantity": 0,
        "m_ComparisonOp": "Equals",
        "m_PlayerFilter": "Self",
    }

    assert evaluate_condition(condition, _context([source]))
    assert not evaluate_condition(condition, _context([source, candlekin]))


def _pvp_champion_destroy_fixture():
    from rules_port.context import EffectContext

    class EmptyCursor:
        def fetchone(self):
            return None

    class EmptyDB:
        def execute(self, _sql, _params=()):
            return EmptyCursor()

    class GameSession:
        session_id = 1

    class Handler:
        user_profile = {"id": 101}
        _player_champ_scid = type("CardId", (), {
            "uid": type("UID", (), {"uid64": 700})()})()
        _ai_champ_scid = type("CardId", (), {
            "uid": type("UID", (), {"uid64": 900})()})()

        def _champion_targets(self):
            # This mirrors the PvE-shaped handler projection; PvP ownership
            # must instead come from the persisted participant map.
            return [(700, 101, "Player", 20), (900, 0, "Opponent", 20)]

    bstate = {
        "pvp": True,
        "pids": [101, 202],
        "champ_map": {"101": 700, "202": 900},
    }
    session = GameSession()
    context = EffectContext.from_rules_port(
        object(), session, EmptyDB(), Handler(), 101, 202, bstate,
        "destroy-effect")
    return context, session, bstate


def test_native_pvp_champion_destroy_uses_persisted_participant_owner():
    context, session, bstate = _pvp_champion_destroy_fixture()
    with mock.patch("pvp_db.db_card_location", return_value=None), \
            mock.patch("pvp_db.db_card_owner_id", return_value=None), \
            mock.patch("rules_port.death_effects.champion_would_lose",
                       return_value=""), \
            mock.patch("services.tournament_game._pvp_end_game") as end_game:
        result = context.destroy(900)

    assert result == f"destroyed champion {hex(900)}"
    end_game.assert_called_once_with(
        session, bstate, 101, 202, "champion destroyed by card effect")


def test_native_pvp_champion_destroy_honors_loss_replacement():
    context, _session, _bstate = _pvp_champion_destroy_fixture()
    with mock.patch("pvp_db.db_card_location", return_value=None), \
            mock.patch("pvp_db.db_card_owner_id", return_value=None), \
            mock.patch("rules_port.death_effects.champion_would_lose",
                       return_value="replacement resolved") as replacement, \
            mock.patch("services.tournament_game._pvp_end_game") as end_game:
        result = context.destroy(900)

    assert result == f"champion defeat replaced {hex(900)}"
    replacement.assert_called_once_with(context, 202, deduplicate=False)
    end_game.assert_not_called()


def test_champion_loss_only_reports_an_accepted_replacement():
    from rules_port.death_effects import champion_would_lose

    class Context:
        bstate = {
            "pvp": True,
            "champ_map": {"202": 900},
            "_champion_would_lose_seen": [202],
        }
        handler = None

        def __init__(self, accepted):
            self.accepted = accepted

        def _emit_trigger(self, *_args):
            if self.accepted:
                self.bstate[
                    "_champion_would_lose_replacement_accepted"] = True
            # A diagnostic string by itself must not count as a replacement.
            return "trigger diagnostic"

    accepted = Context(True)
    assert champion_would_lose(accepted, 202, deduplicate=False)
    assert accepted.bstate["_champion_would_lose_seen"] == [202]
    assert "_champion_would_lose_replacement_accepted" not in accepted.bstate

    diagnostic_only = Context(False)
    assert champion_would_lose(
        diagnostic_only, 202, deduplicate=False) == ""


_CONDITIONS = "Game.Shared.Mechanics.Abilities.Conditions."
_TRIGGERS = "Game.Shared.Mechanics.Triggers.Conditions."


def test_pvp_player_conditions_read_the_conditions_own_participant():
    """Player-scoped conditions read their controller's values in PvP.

    Both PvP participants have nonzero raw IDs, so the PvE ``bool(owner)``
    side mapping sent both to ``player``. The effect view also puts its
    *resolving* participant on ``player`` (often ``pids[1]``), so a
    ``pids[0]`` mapping read the opponent. Checked against the raw
    checkpoint and both view orientations; Practice/PvE keeps its side keys.
    """
    from rules_port.pvp_view import to_effect_view

    first, second = 1001, 1002
    raw = {
        "pvp": True, "pids": [first, second], "turn_pid": second,
        "champ_map": {},
        "chg_1001": 0, "chg_1002": 3, "hp_1001": 20, "hp_1002": 5,
        "res_1001": 1, "res_1002": 1,
        "res_total_1001": 2, "res_total_1002": 7,
        "thresh_1001": {}, "thresh_1002": {"4": 2},
    }
    conditions = {
        "charges": {"_t": _CONDITIONS + "RequiresChampionCharges",
                    "m_RequiredQuantity": 3,
                    "m_ComparisonOp": "GreaterThanOrEqual"},
        "health": {"_t": _CONDITIONS + "RequiresChampionHealth",
                   "m_RequiredQuantity": 5,
                   "m_ComparisonOp": "LessThanOrEqual"},
        "trigger health": {"_t": _TRIGGERS + "TriggerPlayerHealth",
                           "m_Health": 5,
                           "m_ComparisonOp": "LessThanOrEqual"},
        "total resources": {"_t": _CONDITIONS + "RequiresTotalResources",
                            "m_RequiredQuantity": 7,
                            "m_ComparisonOp": "GreaterThanOrEqual"},
        "threshold": {"_t": _CONDITIONS + "RequiresResourceThreshold",
                      "m_ColorFlags": "Blood", "m_RequiredQuantity": 2},
        "controller threshold": {
            "_t": _CONDITIONS +
            "AbilityControllerHasThresholdAbilityCondition",
            "m_ColorFlags": "Blood", "m_RequiredQuantity": 2},
        "active": {"_t": _CONDITIONS +
                   "AbilityControllerIsActiveAbilityCondition"},
        "trigger active": {"_t": _TRIGGERS + "TriggerPlayerIsActivePlayer",
                           "m_TriggerTest": "TriggerSource"},
    }
    states = {"checkpoint": raw,
              "view resolving second": to_effect_view(raw, second, first),
              "view resolving first": to_effect_view(raw, first, second)}
    for state_name, state in states.items():
        for name, condition in conditions.items():
            for owner, expected in ((second, True), (first, False)):
                context = ConditionContext(
                    None, SessionStub(), state, ability_source_uid=1,
                    ability_source_owner_id=owner)
                assert evaluate_condition(condition, context) is expected, (
                    state_name, name, owner)

    pve = {"player_charges": 3, "ai_charges": 0, "turn_player": "player"}
    for owner, expected in ((5, True), (0, False)):
        context = ConditionContext(
            None, SessionStub(), pve, ability_source_uid=1,
            ability_source_owner_id=owner)
        assert evaluate_condition(conditions["charges"], context) is expected
        assert evaluate_condition(conditions["active"], context) is expected


def test_pvp_zone_entry_your_and_opposing_compare_raw_participants():
    """``m_Your``/``m_Opposing`` zone triggers distinguish PvP players."""
    entering = {"card_uid": 50, "card_type": "Troop", "location": "warzone",
                "user_id": 1001, "controller_id": 1001}
    state = {"pvp": True, "pids": [1001, 1002], "champ_map": {}}
    for flag, listener, expected in (
            ("m_Opposing", 1002, True), ("m_Opposing", 1001, False),
            ("m_Your", 1002, False), ("m_Your", 1001, True)):
        context = _context([entering], source_owner=listener)
        context.bstate = state
        context.trigger_uid = 50
        context.event_destination_collection = "warzone"
        condition = {"_t": _TRIGGERS + "TriggerCardEnteredZone",
                     "m_TriggerTest": "TriggerSource",
                     "m_DestinationCollection": "Warzone", flag: 1}
        assert evaluate_condition(condition, context) is expected, (
            flag, listener)


def test_actions_cast_this_turn_ignores_an_earlier_turns_count():
    """``ChampionActionsCastThisTurn`` reads only this turn's count.

    C# reads ``PlayerStatsThisTurn.ActionsCast``. The port's count kept its
    write turn but the reader ignored it, so one action left Runeblast's
    "while you played an action this turn" discount on for later turns.
    PvP keeps the count per raw participant.
    """
    condition = {"_t": _TRIGGERS + "ChampionActionsCastThisTurn",
                 "m_RequiredQuantity": 1,
                 "m_ComparisonOp": "GreaterThanOrEqual"}
    cases = (
        ({"player_actions_cast_this_turn": 1, "player_actions_cast_turn": 3},
         5),
        ({"pvp": True, "pids": [1001, 1002],
          "actions_cast_this_turn_1002": 1, "actions_cast_turn_1002": 3},
         1002),
    )
    for counters, owner in cases:
        for turn, expected in ((3, True), (4, False)):
            state = dict(counters, turn_number=turn)
            context = ConditionContext(
                None, SessionStub(), state, ability_source_uid=1,
                ability_source_owner_id=owner)
            assert evaluate_condition(condition, context) is expected, (
                counters, turn)


if __name__ == "__main__":
    test_requires_cards_controlled_same_owner_uses_contingent_target()
    test_requires_cards_controlled_distinct_facts_and_quantity_filter()
    test_native_pvp_champion_destroy_uses_persisted_participant_owner()
    test_native_pvp_champion_destroy_honors_loss_replacement()
    test_champion_loss_only_reports_an_accepted_replacement()
    test_pvp_player_conditions_read_the_conditions_own_participant()
    test_pvp_zone_entry_your_and_opposing_compare_raw_participants()
    test_actions_cast_this_turn_ignores_an_earlier_turns_count()
    print("PASS condition ownership and champion loss replacement")
