"""Stealth counter keyword: champion counters, attack gate, removal.

Regression cover for the reported "Good at Hiding" defect (Shin'hare Ranger
racial talent): the ONE-SHOT talent adds a stealth counter to your champion,
the counter keeps opposing troops from attacking that champion and grants it
Spellshield, and one counter is removed at the start of your turn.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

# Bind this process's test database before any runtime import opens ``db``.
SRC = fresh_database()

import game_engine

from tests.tests_combat import HandlerStub, SessionStub

# ChampionTalentData "Good at Hiding" — "[BASIC] [ONE-SHOT]: [(0)] [ARROW R]
# Add a stealth counter to yourself."
GOOD_AT_HIDING_ABILITY = "04e2805b-5507-520f-1006-71819d68c21e"
STEALTH_COUNTER_GUID = "78db859d-02b9-fc97-e2fb-8aca1dfeed77"
# "deal damage to target opposing champion or troop" — champion + troop pool.
CHAMPION_OR_TROOP_TARGET = "98d8e8e9-63bf-ec79-b137-b2ae7a6db6cd"


def _champion_battle(handler):
    """Battle state for a PvE session with both champion identities present."""
    player_champ = int(handler._player_champ_scid.uid.uid64)
    ai_champ = int(handler._ai_champ_scid.uid.uid64)
    state = {
        "_rules_port_attached": True,
        "pids": [5, 0],
        "champ_map": {5: player_champ},
        "player_health": 20,
        "ai_health": 20,
    }
    return state, player_champ, ai_champ


def test_one_shot_talent_adds_a_stealth_counter_to_the_champion():
    """Resolving the talent adds exactly one Stealth counter to the champion.

    The authored CounterModifier (operation "Add", input variable A, stealth
    counter template) must reach the champion's persisted counter map.  Live
    champions have no ``game_cards`` row, so an owner lookup that could not
    read the participant UID silently dropped the counter.
    """
    from db import _db as con
    from rules_port.resolution import resolve_port_ability
    from rules_port.stealth import stealth_counters

    handler = HandlerStub(con)
    session = SessionStub()
    state, player_champ, _ai_champ = _champion_battle(handler)
    state["resolving_ability"] = GOOD_AT_HIDING_ABILITY
    state["resolving_owner_id"] = 5
    game = handler._fresh_game(session, state["player_health"],
                               state["ai_health"], state)
    pl_t, ai_t = handler._player_champ_scid.uid, handler._ai_champ_scid.uid

    resolve_port_ability(handler, game, session, con, pl_t, ai_t, state,
                         GOOD_AT_HIDING_ABILITY, player_champ, 5)

    assert stealth_counters(state, player_champ) == 1
    assert state["champion_counters"][str(player_champ)] == {
        STEALTH_COUNTER_GUID: 1}
    # The champion HUD derives its Stealth icon from the cached
    # CardRepresentation counters, which the client only stores from a
    # CardUpdated — so the counter change has to be published that way too,
    # with the collection the champion already sits in (None).
    updates = [event for event in game.events
               if type(event).__name__ == "CardUpdatedSessionEventArgs"
               and int(event.session_card_id.uid.uid64) == player_champ]
    assert updates, [type(event).__name__ for event in game.events]
    published = updates[-1]
    assert published.collection == game_engine.ECardCollections.None_
    assert [str(template.guid) for template in published.counter_templates] == [
        STEALTH_COUNTER_GUID]
    assert [int(count) for count in published.counter_counts] == [1]


def test_stealth_champion_cannot_be_attacked_by_troops():
    '''Client built-in: "While you are Stealth, opposing troops can't attack."'''
    from rules_port.runtime_adapter import PvpRuntimeFacts, RuntimeCard

    profile_id, reck_id = 6175190558117173535, 1925190388022160
    player = game_engine.UID.make(244, reck_id)
    opponent = game_engine.UID.make(3, 1000)
    player_champion = game_engine.SessionCardId(game_engine.UID.make(244, reck_id))
    ai_champion = game_engine.SessionCardId(game_engine.UID.make(3, 4242))
    ai_champ = int(ai_champion.uid.uid64)
    state = {"champion_counters": {str(ai_champ): {STEALTH_COUNTER_GUID: 1}}}

    facts = PvpRuntimeFacts(1, state, player_uid=player, ai_uid=opponent)
    facts.player_owner_id = profile_id
    facts.ai_owner_id = 0
    facts.client_player_uid = player
    facts.player_champion_card_id = player_champion
    facts.ai_champion_card_id = ai_champion
    ready = int(game_engine.ECardStates.StartedATurnOnYourSide)
    attacker = RuntimeCard(
        1793, "template", profile_id, "warzone",
        game_engine.ECardCollections.Warzone,
        int(game_engine.ECardTypes.Troop), ready, 0, 2, (), ())

    # Attacking the stealthed champion face is illegal...
    assert not facts.can_attack(attacker, None, player)
    # ...and so is naming the champion as an explicit defender.
    champion_defender = RuntimeCard(
        ai_champ, "template", 0, "champions",
        game_engine.ECardCollections.Champions,
        int(game_engine.ECardTypes.Champion), 0, 0, 20, (), ())
    assert not facts.can_attack(attacker, champion_defender, player)
    # Troops stay attackable while the champion is Stealth.
    troop = RuntimeCard(
        55, "template", 0, "warzone", game_engine.ECardCollections.Warzone,
        int(game_engine.ECardTypes.Troop), 0, 0, 3, (), ())
    assert facts.can_attack(attacker, troop, player)

    # With the counter gone the champion can be attacked again.
    state["champion_counters"] = {}
    assert facts.can_attack(attacker, None, player)


def test_stealth_champion_is_spellshielded_against_opposing_targets():
    '''Client built-in: "While you are Stealth, you have Spellshield."'''
    from db import _db as con
    from rules_port.targeting import legal_targets

    handler = HandlerStub(con)
    state, player_champ, ai_champ = _champion_battle(handler)
    champions = [(ai_champ, 0, "AI", 20)]

    def targets():
        return legal_targets(
            con, SessionStub.session_id, 5, CHAMPION_OR_TROOP_TARGET,
            source_uid=player_champ, both_players=True, champions=champions,
            battle_state=state)

    assert ai_champ in targets()
    state["champion_counters"] = {str(ai_champ): {STEALTH_COUNTER_GUID: 1}}
    assert ai_champ not in targets()


def test_stealth_counter_removed_at_start_of_owners_turn():
    '''Client built-in: "At the start of your turn ... remove a counter."'''
    from db import _db as con
    from rules_port.context import EffectContext
    from rules_port.stealth import advance, stealth_counters

    handler = HandlerStub(con)
    session = SessionStub()
    state, player_champ, _ai_champ = _champion_battle(handler)
    state["champion_counters"] = {str(player_champ): {STEALTH_COUNTER_GUID: 2}}
    pl_t, ai_t = handler._player_champ_scid.uid, handler._ai_champ_scid.uid
    game = handler._fresh_game(session, state["player_health"],
                               state["ai_health"], state)
    context = EffectContext.from_rules_port(
        game, session, con, handler, pl_t, ai_t, state, "", ability=None)

    assert advance(context, 5) == [(player_champ, 2, 1)]
    assert stealth_counters(state, player_champ) == 1
    # The AI champion is not the active side's champion.
    assert advance(context, 0) == []
    assert stealth_counters(state, player_champ) == 1


def test_ai_declares_no_attack_against_a_stealthed_champion():
    '''The AI's DeclareAttackers must honour "opposing troops can't attack".

    The AI always commits against the opposing champion face, so a stealth
    counter there leaves it with no legal attack this turn.
    '''
    import ai
    from db import _db as con
    from tests.tests_combat import add_card

    tpl = "88888888-8888-8888-8888-888888888888"
    con.execute(
        "INSERT OR REPLACE INTO card_templates (guid, name, card_type, cost, "
        "attack, defense, attributes, abilities_json, threshold_json, "
        "subtype, rage_value) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tpl, "Stealth Test Troop", "Troop", 1, 2, 2, 0, "[]", "[]",
         "Orc Ranger", 0))
    add_card(con, 101, 0, tpl,
             state=int(game_engine.ECardStates.StartedATurnOnYourSide))
    handler = HandlerStub(con)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    player_champ = int(handler._player_champ_scid.uid.uid64)
    state = {"player_health": 20, "ai_health": 20, "turn_number": 2}
    game = game_engine.Game(1, pl_t, ai_t)
    ai._db = con

    ai.ai_declare_attackers(handler, game, SessionStub(), ai_t, pl_t, state)
    assert state.get("ai_attackers") == {
        "101": str(player_champ)}, state

    state.pop("ai_attackers", None)
    state["champion_counters"] = {str(player_champ): {STEALTH_COUNTER_GUID: 1}}
    ai.ai_declare_attackers(handler, game, SessionStub(), ai_t, pl_t, state)
    assert not state.get("ai_attackers"), state


def test_one_shot_champion_ability_is_consumed_on_activation():
    """ONE-SHOT champion powers spend themselves, then leave the offer list."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from hconnect_server import HCPHandler
    from types import SimpleNamespace

    handler = HCPHandler.__new__(HCPHandler)
    handler.user_profile = {"id": 5}
    state = {"turn_player": "player", "player_charges": 0,
             "player_spell_points": 0, "_rules_port_attached": True}
    graph = ability_graph(DEFAULT_RECORD_STORE, GOOD_AT_HIDING_ABILITY)
    assert int(graph.costs.uses_per_game) == 1
    power = SimpleNamespace(guid=GOOD_AT_HIDING_ABILITY)

    assert handler._champion_ability_available(
        state, GOOD_AT_HIDING_ABILITY, graph)
    assert handler._filter_affordable_abilities(
        [power], state,
        phase=game_engine.ETurnPhases.FirstMainPhase) == [power]

    handler._consume_champion_ability_use(state, GOOD_AT_HIDING_ABILITY)
    assert not handler._champion_ability_available(
        state, GOOD_AT_HIDING_ABILITY, graph)
    # The spent ONE-SHOT no longer lights up the champion card.
    assert handler._filter_affordable_abilities(
        [power], state,
        phase=game_engine.ETurnPhases.FirstMainPhase) == []
    # Powers without an authored per-game limit are never gated.
    assert handler._champion_ability_available(state, "unlimited-power", None)


def test_port_spends_a_one_shot_champion_power_once():
    """The live RulesPort rejects the second activation of a ONE-SHOT power.

    Manual champion powers are consumed by the port
    (``MetadataCardTransactionExecutor``), not by the legacy HConnect handler:
    the port validates, pays, and queues the projected chain item.  A spent
    ONE-SHOT must therefore be refused there or the client can re-activate it
    every turn (which also keeps regenerating the stealth counter).
    """
    from types import SimpleNamespace

    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from rules_port.card_transactions import MetadataCardTransactionExecutor
    from rules_port.runtime_adapter import PvpRuntimeFacts

    profile_id, reck_id = 6175190558117173535, 1925190388022160
    player = game_engine.UID.make(244, reck_id)
    opponent = game_engine.UID.make(3, 1000)
    champion = game_engine.SessionCardId(game_engine.UID.make(1, 1))
    champ_uid = int(champion.uid.uid64)
    state = {}
    facts = PvpRuntimeFacts(1, state, player_uid=player, ai_uid=opponent)
    facts.player_owner_id = profile_id
    facts.ai_owner_id = 0
    facts.player_champion_card_id = champion

    class _Port:
        event_sink = None

        def __init__(self, facts):
            self.runtime_facts = facts
            self.ability_manager = SimpleNamespace(_instances={})
            self.action_stack = SimpleNamespace(priority_player_id=player)
            self.queued = []

        def can_pay_ability_cost(self, metadata):
            return True

        def pay_ability_cost(self, ability):
            return True

        def queue_projected_chain(self, descriptor, player_id,
                                  first_player_id=None):
            self.queued.append(descriptor)
            return True

    port = _Port(facts)
    executor = MetadataCardTransactionExecutor(
        port,
        graph_loader=lambda guid: ability_graph(DEFAULT_RECORD_STORE, guid),
        owner_id=profile_id, store=DEFAULT_RECORD_STORE)
    transaction = SimpleNamespace(
        payload={"source_card_id": champ_uid,
                 "ability_template_id": GOOD_AT_HIDING_ABILITY,
                 "activation_data": {"target_map": {}, "variables": {}},
                 "ability_instance_id": 1},
        player_id=player)

    assert executor("activate_ability", transaction) is True
    assert state["champion_ability_uses"] == {
        GOOD_AT_HIDING_ABILITY: 1}
    assert len(port.queued) == 1
    # The ONE-SHOT is spent: the replay is refused and nothing is queued.
    assert executor("activate_ability", transaction) is False
    assert state["champion_ability_uses"] == {
        GOOD_AT_HIDING_ABILITY: 1}
    assert len(port.queued) == 1


def main():
    tests = [
        test_one_shot_talent_adds_a_stealth_counter_to_the_champion,
        test_stealth_champion_cannot_be_attacked_by_troops,
        test_stealth_champion_is_spellshielded_against_opposing_targets,
        test_stealth_counter_removed_at_start_of_owners_turn,
        test_ai_declares_no_attack_against_a_stealthed_champion,
        test_one_shot_champion_ability_is_consumed_on_activation,
        test_port_spends_a_one_shot_champion_power_once,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS {len(tests)} stealth counter tests")


if __name__ == "__main__":
    main()
