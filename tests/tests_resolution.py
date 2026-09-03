"""Authoritative resolution engine tests (port of the client's
AbilityInstance.ApplyEffectGroup / ResolveAutoTarget / contingencies).

The fixtures reuse tests_combat.make_db() for the common schema and then add
synthetic ability chains so each engine mechanic is exercised in isolation:
effect groups, gamedata conditions, ability variables, ActivateAbility
recursion, auto "You" targets, and contingent effect instances.
"""

import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine

from tests.tests_combat import (make_db, add_card, HandlerStub, SessionStub,
                                PromptHandlerStub, TPL_GLADIATOR)


def _ag(seed):
    """Deterministic pseudo-GUID from a seed string (test-only)."""
    import hashlib
    h = hashlib.md5(seed.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _insert_ability(db, ag, tids, effects):
    """Insert an ability's meta + effect rows.  effects is a list of dicts:
    order, type, param, group, condition, target_index, instance_id,
    contingent_instance_id."""
    db.execute(
        "INSERT INTO card_abilities_meta (ability_guid, is_triggered, "
        "trigger_event_type, game_text, raw_json, casting_behavior, is_manual, "
        "activation_cost, uses_per_game, uses_per_turn, target_template_ids, "
        "exhausts_on_use) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ag, 0, "", "", "", 0, 0, 0, 0, 0, json.dumps(tids), 0))
    for e in effects:
        db.execute(
            "INSERT INTO ability_effects (ability_guid, effect_guid, "
            "effect_order, effect_type, param, effect_group_id, condition_id, "
            "target_index, effect_instance_id, contingent_effect_instance_id, "
            "secondary_target_index, recalculate_targets, is_optional, "
            "effect_duration, output_variables) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ag, _ag(f"{ag}:{e['order']}"), e["order"], e["type"],
             e.get("param", ""), e.get("group", 1), e.get("condition", ""),
             e.get("target_index", -1), e.get("instance_id", e["order"]),
             e.get("contingent", -1), e.get("secondary", -1), 1, 0,
             e.get("duration", "Instant"), "{}"))
    db.commit()


def _condition(db, cid, lhs, rhs):
    db.execute(
        "INSERT INTO ability_effect_conditions (condition_id, name, condition_json) "
        "VALUES (?,?,?)",
        (cid, f"{lhs}Equals{rhs}",
         json.dumps({"_t": "Game.Shared.Mechanics.Abilities.Conditions."
                            "AbilityVariableCondition",
                     "m_Lhs": lhs, "m_Rhs": str(rhs),
                     "m_ComparisonOp": "Equals"})))
    db.commit()


def test_random_variable_conditions_and_recursion(db):
    """A -> B: B rolls RandomNumber (ability variable), then only the matching
    conditioned ActivateAbility branch runs — roll 1 heals 1, roll 2 heals 2.
    This exercises groups, conditions, ability variables, ActivateAbility
    recursion and the auto 'You' (controller champion) target template."""
    from abilities.framework.resolution import resolve_ability
    A = _ag("top")
    B = _ag("roll")
    D = _ag("heal1")
    H = _ag("heal2")
    YOU = "eb7e48cd-1c85-813f-6635-d43f50cf7809"
    C1 = _ag("cond1")
    C2 = _ag("cond2")
    _condition(db, C1, "RandomNumber", 1)
    _condition(db, C2, "RandomNumber", 2)
    _insert_ability(db, A, [YOU], [
        {"order": 0, "type": "ActivateAbilityEffectTemplate",
         "param": B, "target_index": 0}])
    _insert_ability(db, B, [YOU, YOU, YOU], [
        {"order": 0, "type": "RandomizeVariableEffectTemplate",
         "param": '{"variable": "RandomNumber", "min": 1, "max": 2}'},
        {"order": 1, "type": "ActivateAbilityEffectTemplate", "param": D,
         "group": 2, "condition": C1, "target_index": 1},
        {"order": 2, "type": "ActivateAbilityEffectTemplate", "param": H,
         "group": 3, "condition": C2, "target_index": 2}])
    _insert_ability(db, D, [YOU], [
        {"order": 0, "type": "CardModifierAbilityEffectTemplate",
         "param": '{"text": "gain 1 health.", "property": "healhero", '
                  '"amount": 1, "duration": "Instant"}',
         "target_index": 0}])
    _insert_ability(db, H, [YOU], [
        {"order": 0, "type": "CardModifierAbilityEffectTemplate",
         "param": '{"text": "gain 2 health.", "property": "healhero", '
                  '"amount": 2, "duration": "Instant"}',
         "target_index": 0}])

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    handler = HandlerStub(db)

    def run(roll):
        bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
        game = game_engine.Game(1, pl_t, ai_t)
        with mock.patch("random.randint", return_value=roll):
            resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t,
                            bstate, A, 200, 5, {})
        return bstate

    with mock.patch("random.randint", return_value=1):
        assert run(1)["player_health"] == 21, run(1)
    with mock.patch("random.randint", return_value=2):
        assert run(2)["player_health"] == 22, run(2)


def test_contingent_effect_applies_only_when_prerequisite_did(db):
    """Ability X: effect 1 (counter +1 on 'this') always applies; effect 2
    (void 'this') is contingent on effect 1's instance having applied; effect 3
    is contingent on a missing instance — the void runs once, the missing
    contingency never does."""
    from abilities.framework.resolution import resolve_ability
    X = _ag("contingency")
    THIS = "190a4d8c-7c2c-10d0-6429-99c5aeb0791f"
    _insert_ability(db, X, [THIS], [
        {"order": 0, "type": "CardModifierAbilityEffectTemplate",
         "param": '{"text": "add a test counter to this.", '
                  '"property": "counter", "amount": 1, "duration": "Instant"}',
         "target_index": 0, "instance_id": 0},
        {"order": 1, "type": "VoidCardAbilityEffectTemplate",
         "param": "", "group": 2, "target_index": 0,
         "instance_id": 1, "contingent": 0},
        {"order": 2, "type": "VoidCardAbilityEffectTemplate",
         "param": "", "group": 3, "target_index": 0,
         "instance_id": 2, "contingent": 99},
    ])
    add_card(db, 300, 5, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd")  # Shamed Gladiator
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    game = game_engine.Game(1, pl_t, ai_t)
    resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t, bstate,
                    X, 300, 5, {})
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=300").fetchone()[0]
    assert loc == "void", loc


def test_shared_activation_map_feeds_single_explicit_leaf(db):
    """An explicit (non-auto) leaf deep in an ActivateAbility chain uses the
    activation TargetMap entry — the Darkspire search MoveCardToZone gets the
    chosen deck card even though the leaf lives under a nested ability."""
    from abilities.framework.resolution import resolve_ability
    TOP = _ag("search-top")
    MID = _ag("search-mid")
    LEAF = _ag("search-leaf")
    YOU = "eb7e48cd-1c85-813f-6635-d43f50cf7809"
    DECK = "0ad94887-419c-9e99-7946-74c4f72cdd2e"
    _insert_ability(db, TOP, [YOU], [
        {"order": 0, "type": "ActivateAbilityEffectTemplate", "param": MID,
         "target_index": 0}])
    _insert_ability(db, MID, [YOU], [
        {"order": 0, "type": "ActivateAbilityEffectTemplate", "param": LEAF,
         "target_index": 0}])
    _insert_ability(db, LEAF, [DECK], [
        {"order": 0, "type": "MoveCardToZoneEffectTemplate",
         "param": '{"destination": "Hand", "location": "Unknown", '
                  '"name": "PutItIntoYourHand", "text": "put it into your hand."}',
         "target_index": 0}])
    add_card(db, 101, 5, "14909185-1070-48df-9508-61d5a9650bd2", loc="deck")
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    game = game_engine.Game(1, pl_t, ai_t)
    resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t, bstate,
                    TOP, 200, 5, {0: 101})
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=101").fetchone()[0]
    assert loc == "hand", loc


def test_empty_revealed_troop_target_does_not_move_stale_card(db):
    """Oakhenge's no-troop reveal skips the hand move and returns every
    revealed non-troop to the deck instead of resolving the leaf with None.
    """
    from abilities.framework.resolution import resolve_ability

    TOP = _ag("oakhenge-no-troop")
    TROOP = _ag("oakhenge-revealed-troop-target")
    REMAINING = _ag("oakhenge-revealed-remaining-target")
    _insert_ability(db, TOP, [TROOP, REMAINING], [
        {"order": 0, "type": "MoveCardToZoneEffectTemplate",
         "param": '{"destination": "Hand", "location": "Unknown"}',
         "group": 1, "target_index": 0, "instance_id": 0},
        {"order": 1, "type": "MoveCardToZoneEffectTemplate",
         "param": '{"destination": "Deck", "location": "Unknown"}',
         "group": 2, "target_index": 1, "instance_id": 1,
         "secondary": 0},
    ])
    db.executemany(
        "INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
            (TROOP, "a revealed troop", 0, 0, 0, 1, "", "", 1, 1,
             '{"_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"}',
             "SourceRevealedTargetTemplate"),
            (REMAINING, "the remaining cards", 1, 0, 0, 0, "", "", 1, 1,
             "{}", "SourceRevealedTargetTemplate"),
        ])
    shard_tpl = "b7172b6a-ef85-4fef-91e1-81975b4ce7cd"
    add_card(db, 301, 5, shard_tpl, loc="deck")
    add_card(db, 302, 5, shard_tpl, loc="deck")
    db.execute(
        "UPDATE game_cards SET card_type='Resource', position=? "
        "WHERE card_uid=?", (1, 301))
    db.execute(
        "UPDATE game_cards SET card_type='Resource', position=? "
        "WHERE card_uid=?", (2, 302))
    db.commit()

    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"player_health": 20, "ai_health": 20,
              "revealed_cards": [301, 302],
              # Simulate the stale target that previously caused the null
              # hand move to select a shard.
              "player_spell_target": 301}
    resolve_ability(HandlerStub(db), game, SessionStub(), db, pl_t, ai_t,
                    bstate, TOP, 999, 5, {})
    rows = db.execute(
        "SELECT card_uid, location FROM game_cards "
        "WHERE card_uid IN (301,302) ORDER BY card_uid").fetchall()
    assert rows == [(301, "deck"), (302, "deck")], rows


def test_deck_search_prompt_pauses_before_second_effect(db):
    """A nested ability with two effects sharing a deck target opens one
    picker and pauses; the second effect must not issue a duplicate prompt.

    Adaptable Infusion Device has this metadata shape (StoreTargets followed
    by TAC), so this protects the client picker from being rebuilt underneath
    the first selection.
    """
    from abilities.framework.resolution import resolve_ability

    ability = _ag("duplicate-deck-prompt")
    target = _ag("deck-prompt-target")
    _insert_ability(db, ability, [target], [
        {"order": 0, "type": "TACAbilityEffectTemplate",
         "param": "first", "target_index": 0},
        {"order": 1, "type": "TACAbilityEffectTemplate",
         "param": "second", "target_index": 0},
    ])
    db.execute(
        "INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (target, "choose from deck", 0, 0, 0, 1, "", "Deck", 1, 1,
         json.dumps({
             "_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
             "m_Collection": "Deck",
         }), "AbilityTargetTemplate"))
    add_card(db, 401, 5, TPL_GLADIATOR, loc="deck")
    db.commit()
    handler = PromptHandlerStub(db)
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    bstate = {"player_health": 20, "ai_health": 20}
    resolve_ability(handler, game_engine.Game(1, pl_t, ai_t), SessionStub(),
                    db, pl_t, ai_t, bstate, ability, 401, 5, {})
    assert len(handler.prompt_calls) == 1, handler.prompt_calls
    assert bstate.get("resolution_paused") is True, bstate


def test_deck_search_detection_uses_filter_not_collection_flags(db):
    """A broad visibility mask must not turn a hand target into a deck search.

    Stargazer's DiscardACard target advertises all player-owned collections,
    including Deck, but its authoritative filter is InZone: Hand. Only an
    actual InZone: Deck filter should enter the class-39 deck-search path.
    """
    from abilities.framework.resolution import _is_deck_search_target

    broad_flags = "Deck|Hand|Champions|Warzone|Discard|Void|CastSpells|Underground|Choosing"
    hand_filter = {
        "_t": "Game.Shared.Mechanics.Cards.Filters.AndCardFilter",
        "m_TargetFilters": [{
            "_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
            "m_Collection": "Hand",
        }],
    }
    deck_filter = {
        "_t": "Game.Shared.Mechanics.Cards.Filters.AndCardFilter",
        "m_TargetFilters": [{
            "_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
            "m_Collection": "Deck",
        }],
    }

    assert not _is_deck_search_target({
        "collection_flags": broad_flags,
        "filter_json": hand_filter,
    })
    assert _is_deck_search_target({
        "collection_flags": broad_flags,
        "filter_json": deck_filter,
    })
    assert not _is_deck_search_target({
        "collection_flags": broad_flags,
        "filter_json": {
            "_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
            "m_Collection": "Deck|Hand",
        },
    })


def test_empty_sacrifice_target_does_not_sacrifice_source(db):
    """An optional target with no legal card must not fall back to the source."""
    from abilities.framework.resolution import resolve_ability

    ability = _ag("empty-sacrifice")
    target = _ag("optional-troop")
    db.execute(
        "INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (target, "a troop you control", 0, 0, 0, 0, "Self", "Warzone",
         1, 1, '{"_t":"Game.Shared.Mechanics.Cards.Filters.IsTroop"}',
         "AbilityTargetTemplate"))
    _insert_ability(db, ability, [target], [{
        "order": 0, "type": "SacrificeCardAbilityEffectTemplate",
        "target_index": 0,
    }])
    add_card(db, 200, 0, TPL_GLADIATOR, loc="warzone")
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    handler = HandlerStub(db)
    game = game_engine.Game(1, pl_t, ai_t)
    resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t,
                    {"turn_number": 1}, ability, 200, 0, {})
    location = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=200").fetchone()[0]
    assert location == "warzone", location


def test_ai_sacrifice_target_excludes_source(db):
    """AI deploy targeting chooses another troop, or no target if absent."""
    from abilities.framework.triggers import _ai_trigger_target

    ability = _ag("ai-sacrifice-target")
    target = _ag("ai-optional-troop")
    db.execute(
        "INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (target, "a troop you control", 0, 0, 0, 0, "Self", "Warzone",
         1, 1, '{"_t":"Game.Shared.Mechanics.Cards.Filters.IsTroop"}',
         "AbilityTargetTemplate"))
    _insert_ability(db, ability, [target], [{
        "order": 0, "type": "SacrificeCardAbilityEffectTemplate",
        "target_index": 0,
    }])
    add_card(db, 210, 0, TPL_GLADIATOR, loc="warzone")
    session = SessionStub()
    assert _ai_trigger_target(db, session, ability, 210, 0, {}, []) is None
    add_card(db, 211, 0, TPL_GLADIATOR, loc="warzone")
    assert _ai_trigger_target(db, session, ability, 210, 0, {}, []) == 211


def test_double_choice_creates_random_choices_and_clears_before_second(db):
    """DoubleChoice follows the client sequence without using card text."""
    from abilities.framework.effects import choices

    choice_guids = [_ag(f"choice-{i}") for i in range(6)]
    for guid in choice_guids:
        db.execute(
            "INSERT INTO card_templates "
            "(guid,name,card_type,cost,attack,defense,attributes,"
            "abilities_json,threshold_json,subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (guid, f"Choice {guid[-4:]}", "Choice", 0, 0, 0, 0, "[]",
             "[]", ""))
    db.commit()
    add_card(db, 400, 5, TPL_GLADIATOR, loc="warzone")
    session = SessionStub()
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {
        "resolving_ability": "choice-ability",
        "resolving_effect_order": 0,
        "resolving_source_uid": 400,
        "resolving_owner_id": 5,
        "ability_target_map": {},
        "ability_variables": {},
    }
    first_guid = "effect-first"
    second_guid = "effect-second"

    def typed(effect_guid):
        return {"m_SecondChoice": effect_guid == second_guid}

    def value(_db, _state, effect_guid, field_name, default=None):
        if field_name == "m_Choices" and effect_guid == first_guid:
            return [{"m_Guid": guid} for guid in choice_guids]
        return default

    with mock.patch.object(choices, "effect_template", side_effect=typed), \
            mock.patch.object(choices, "effect_template_value",
                              side_effect=value), \
            mock.patch.object(choices, "effect_field", return_value=3), \
            mock.patch("random.randrange", side_effect=[0, 0, 0]):
        result = choices.double_choice(
            game, session, db, handler, pl_t, ai_t, bstate, first_guid, "")
    assert "awaiting 3" in result, result
    assert len(bstate["pending_choice"]["choice_uids"]) == 3
    assert db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE location='choosing'").fetchone()[0] == 3
    bstate.pop("pending_choice")
    bstate.pop("resolution_paused")
    bstate["resolving_effect_order"] = 1
    with mock.patch.object(choices, "effect_template", side_effect=typed), \
            mock.patch.object(choices, "effect_template_value",
                              side_effect=value), \
            mock.patch.object(choices, "effect_field", return_value=3):
        result = choices.double_choice(
            game, session, db, handler, pl_t, ai_t, bstate, second_guid, "")
    assert "awaiting 3" in result, result
    assert db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE location='choosing'").fetchone()[0] == 3
    assert db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE location='PlayedResources'").fetchone()[0] == 3


def test_summon_choosing_collection_stays_out_of_warzone(db):
    """A typed Choosing summon creates option cards, not permanents."""
    from abilities.framework.effects.tokens import summon_token

    choice = _ag("choosing-token")
    db.execute(
        "INSERT INTO card_templates "
        "(guid,name,card_type,cost,attack,defense,attributes,"
        "abilities_json,threshold_json,subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (choice, "A Choice", "Choice", 0, 0, 0, 0, "[]", "[]", ""))
    db.commit()
    session = SessionStub()
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"resolving_owner_id": 5, "resolving_ability": ""}
    result = summon_token(
        game, session, db, HandlerStub(db), pl_t, ai_t, bstate,
        _ag("choosing-effect"), json.dumps({
            "token_guid": choice, "amount": 1, "collection": "Choosing"}))
    assert "to choosing" in result, result
    uid = bstate["created_token_uids"][0]
    assert db.execute(
        "SELECT location, card_state FROM game_cards WHERE card_uid=?",
        (uid,)).fetchone() == ("choosing", 0)
    assert any(
        ev.__class__.__name__ == "CardMovedSessionEventArgs" and
        ev.collection == game_engine.ECardCollections.Choosing
        for ev in game.events)
    assert not any(
        ev.__class__.__name__ == "CardMovedSessionEventArgs" and
        ev.collection == game_engine.ECardCollections.Warzone
        for ev in game.events)


def test_choice_ability_transforms_real_parent(db):
    """Playing a Choice token applies its automatic ability to its parent."""
    import db as db_module
    from abilities.framework.effects.choices import (
        play_choice_card, resolve_choice_card_abilities)
    from abilities.framework.effects.tokens import summon_token

    source_uid = 401
    source_tpl = TPL_GLADIATOR
    output_tpl = _ag("choice-output")
    choice_tpl = _ag("choice-card")
    choice_ability = _ag("choice-transform")
    effect_guid = _ag("choice-transform-effect")
    db.execute(
        "INSERT INTO card_templates "
        "(guid,name,card_type,cost,attack,defense,attributes,"
        "abilities_json,threshold_json,subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (output_tpl, "Choice Output", "Troop", 3, 4, 4, 0, "[]", "[]", ""))
    db.execute(
        "INSERT INTO card_templates "
        "(guid,name,card_type,cost,attack,defense,attributes,"
        "abilities_json,threshold_json,subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (choice_tpl, "Choose Output", "Choice", 0, 0, 0, 0,
         json.dumps([choice_ability]), "[]", ""))
    _insert_ability(db, choice_ability, [
        "190a4d8c-7c2c-10d0-6429-99c5aeb0791f"], [{
            "order": 0, "type": "TransformCardAbilityEffectTemplate",
            "effect_guid": effect_guid, "target_index": 0}])
    db.execute(
        "UPDATE card_abilities_meta SET game_text=? WHERE ability_guid=?",
        (f"Transform this into <a data={output_tpl}>Choice Output</a>.",
         choice_ability))
    # _insert_ability generates the effect GUID; use the requested one so the
    # test can assert the same metadata fallback path as a real transform.
    db.execute(
        "UPDATE ability_effects SET effect_guid=? WHERE ability_guid=?",
        (effect_guid, choice_ability))
    db.commit()
    add_card(db, source_uid, 5, source_tpl)
    session = SessionStub()
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"resolving_source_uid": source_uid,
              "resolving_owner_id": 5}
    summon_token(
        game, session, db, handler, pl_t, ai_t, bstate, _ag("choice-summon"),
        json.dumps({"token_guid": choice_tpl, "amount": 1,
                    "collection": "Choosing"}))
    choice_uid = bstate["created_token_uids"][0]
    old_db = db_module._db
    db_module._db = db
    try:
        assert play_choice_card(
            game, session, db, handler, pl_t, ai_t, bstate,
            choice_uid, 5)
        resolve_choice_card_abilities(
            game, session, db, handler, pl_t, ai_t, bstate,
            choice_uid, source_uid, 5)
    finally:
        db_module._db = old_db
    assert db.execute(
        "SELECT template_guid FROM game_cards WHERE card_uid=?",
        (source_uid,)).fetchone()[0] == output_tpl
    assert db.execute(
        "SELECT location FROM game_cards WHERE card_uid=?",
        (choice_uid,)).fetchone()[0] == "PlayedResources"


def main():
    tests = [
        ("Random variable + conditions + recursion",
         test_random_variable_conditions_and_recursion),
        ("Contingent effects gate on prerequisite",
         test_contingent_effect_applies_only_when_prerequisite_did),
        ("Shared activation map feeds nested explicit leaf",
         test_shared_activation_map_feeds_single_explicit_leaf),
        ("Empty revealed troop target is a no-op",
         test_empty_revealed_troop_target_does_not_move_stale_card),
        ("Deck search pauses after one prompt",
         test_deck_search_prompt_pauses_before_second_effect),
        ("Deck search detection uses the zone filter",
         test_deck_search_detection_uses_filter_not_collection_flags),
        ("Empty sacrifice target does not sacrifice source",
         test_empty_sacrifice_target_does_not_sacrifice_source),
        ("AI sacrifice target excludes source",
         test_ai_sacrifice_target_excludes_source),
        ("DoubleChoice creates and refreshes choices",
         test_double_choice_creates_random_choices_and_clears_before_second),
        ("Choosing summon creates option cards",
         test_summon_choosing_collection_stays_out_of_warzone),
        ("Choice transforms its real parent",
         test_choice_ability_transforms_real_parent),
    ]
    failed = 0
    for name, fn in tests:
        db = make_db()
        try:
            fn(db)
            print(f"PASS {name}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            db.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
