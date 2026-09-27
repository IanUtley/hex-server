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
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

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


def _fixture_effect_list(db, ability_guid):
    """Build the resolver ABI for an intentionally synthetic test ability."""
    rows = db.execute(
        "SELECT effect_guid, effect_order, effect_type, param, "
        "effect_group_id, condition_id, target_index, effect_instance_id, "
        "contingent_effect_instance_id, secondary_target_index, "
        "recalculate_targets, is_optional, effect_duration, output_variables "
        "FROM ability_effects WHERE ability_guid=? ORDER BY effect_order",
        (ability_guid,)).fetchall()
    return [{
        "effect_guid": row[0], "effect_order": row[1],
        "effect_type": row[2] or "", "param": row[3] or "",
        "effect_group_id": int(row[4] or 0), "condition_id": row[5] or "",
        "target_index": int(row[6] if row[6] is not None else -1),
        "effect_instance_id": int(row[7] if row[7] is not None else -1),
        "contingent_effect_instance_id": int(
            row[8] if row[8] is not None else -1),
        "secondary_target_index": int(row[9] if row[9] is not None else -1),
        "recalculate_targets": int(row[10] if row[10] is not None else -1),
        "is_optional": int(row[11] or 0),
        "effect_duration": row[12] or "Instant",
        "output_variables": row[13] or "{}",
    } for row in rows]


def _fixture_target_template_ids(db, ability_guid):
    row = db.execute(
        "SELECT target_template_ids FROM card_abilities_meta "
        "WHERE ability_guid=? LIMIT 1", (ability_guid,)).fetchone()
    return [str(value).lower() for value in (
        json.loads(row[0] or "[]") if row and row[0] else []) if value]


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
    from rules_port.resolution import resolve_port_ability
    from tests.native_records import synthetic_records

    YOU = "eb7e48cd-1c85-813f-6635-d43f50cf7809"
    C1 = _ag("cond1")
    C2 = _ag("cond2")
    _condition(db, C1, "RandomNumber", 1)
    _condition(db, C2, "RandomNumber", 2)
    A, B, D, H = (_ag(name) for name in
                  ("rv-top", "rv-roll", "rv-heal1", "rv-heal2"))
    with synthetic_records() as rec:
        heal1 = rec.effect(
            _ag("rv-e1"), "CardModifierAbilityEffectTemplate",
            text="gain 1 health.",
            m_Modifier=rec.modifier("HealHeroModifier", input_variable="1"))
        heal2 = rec.effect(
            _ag("rv-e2"), "CardModifierAbilityEffectTemplate",
            text="gain 2 health.",
            m_Modifier=rec.modifier("HealHeroModifier", input_variable="2"))
        rec.ability(D, effects=[rec.mapping(heal1)], targets=[YOU],
                    variables=[rec.constant("1", 1)])
        rec.ability(H, effects=[rec.mapping(heal2)], targets=[YOU],
                    variables=[rec.constant("2", 2)])
        randomize = rec.effect(
            _ag("rv-rand"), "RandomizeVariableEffectTemplate",
            m_VariableName="RandomNumber", m_MinValue=1, m_MaxValue=2)
        act1 = rec.effect(_ag("rv-act1"), "ActivateAbilityEffectTemplate",
                          m_AbilityToInvoke={"m_Guid": D})
        act2 = rec.effect(_ag("rv-act2"), "ActivateAbilityEffectTemplate",
                          m_AbilityToInvoke={"m_Guid": H})
        rec.ability(B, effects=[
            rec.mapping(randomize, instance=0, group=1),
            rec.mapping(act1, target_index=1, instance=1, group=2,
                        condition=C1),
            rec.mapping(act2, target_index=2, instance=2, group=3,
                        condition=C2),
        ], targets=[YOU, YOU, YOU])
        top = rec.effect(_ag("rv-topact"), "ActivateAbilityEffectTemplate",
                         m_AbilityToInvoke={"m_Guid": B})
        rec.ability(A, effects=[rec.mapping(top)], targets=[YOU])

        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        handler = HandlerStub(db)

        def run(roll):
            bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
            game = game_engine.Game(1, pl_t, ai_t)
            with mock.patch("random.randint", return_value=roll):
                resolve_port_ability(handler, game, SessionStub(), db,
                                     pl_t, ai_t, bstate, A, 200, 5)
            return bstate

        assert run(1)["player_health"] == 21
        assert run(2)["player_health"] == 22


def test_contingent_effect_applies_only_when_prerequisite_did(db):
    """Ability X: effect 1 (heal) always applies; effect 2 (void 'this') is
    contingent on effect 1's instance having applied; effect 3 is contingent
    on a missing instance — the void runs once, the missing contingency never
    does."""
    from rules_port.resolution import resolve_port_ability
    from tests.native_records import synthetic_records

    X = _ag("contingency")
    THIS = "190a4d8c-7c2c-10d0-6429-99c5aeb0791f"
    with synthetic_records() as rec:
        heal = rec.effect(
            _ag("cont-heal"), "CardModifierAbilityEffectTemplate",
            text="gain 1 health.",
            m_Modifier=rec.modifier("HealHeroModifier", input_variable="1"))
        void = rec.effect(_ag("cont-void"), "VoidCardAbilityEffectTemplate",
                          text="void this.")
        rec.ability(X, effects=[
            rec.mapping(heal, instance=0, group=1),
            rec.mapping(void, instance=1, group=2, contingent=0),
            rec.mapping(void, instance=2, group=3, contingent=99),
        ], targets=[THIS], variables=[rec.constant("1", 1)])
        add_card(db, 300, 5, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd")
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        handler = HandlerStub(db)
        bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
        game = game_engine.Game(1, pl_t, ai_t)
        resolve_port_ability(handler, game, SessionStub(), db, pl_t, ai_t,
                             bstate, X, 300, 5)
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=300").fetchone()[0]
    assert loc == "void", loc


def test_empty_revealed_troop_target_does_not_move_stale_card(db):
    """Oakhenge's no-troop reveal skips the hand move and returns every
    revealed non-troop to the deck instead of resolving the leaf with None.
    """
    from rules_port.resolution import resolve_port_ability
    from tests.native_records import synthetic_records

    TOP = _ag("oakhenge-no-troop")
    TROOP = _ag("oakhenge-revealed-troop-target")
    REMAINING = _ag("oakhenge-revealed-remaining-target")
    db.executemany(
        "INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
            (TROOP, "a revealed troop", 0, 0, 0, 1, "", "", 1, 1,
             '{"_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"}',
             "SourceRevealedTargetTemplate"),
            (REMAINING, "the remaining cards", 1, 0, 0, 0, "", "", 1, 1,
             "{}", "SourceRevealedTargetTemplate"),
        ])
    with synthetic_records() as rec:
        rec.target(
            TROOP, "SourceRevealedTargetTemplate", is_auto=0,
            explicit=1, minimum=1, maximum=1,
            card_filter={
                "_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"})
        rec.target(
            REMAINING, "SourceRevealedTargetTemplate", is_auto=1,
            explicit=0, minimum=1, maximum=1)
        move_hand = rec.effect(
            _ag("oak-move-hand"), "MoveCardToZoneEffectTemplate",
            m_DestinationCollection="Hand")
        move_deck = rec.effect(
            _ag("oak-move-deck"), "MoveCardToZoneEffectTemplate",
            m_DestinationCollection="Deck")
        rec.ability(TOP, effects=[
            rec.mapping(move_hand, target_index=0, instance=0, group=1),
            rec.mapping(move_deck, target_index=1, instance=1, group=2,
                        secondary=0),
        ], targets=[TROOP, REMAINING])

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
        resolve_port_ability(HandlerStub(db), game, SessionStub(), db,
                             pl_t, ai_t, bstate, TOP, 999, 5)
    rows = db.execute(
        "SELECT card_uid, location FROM game_cards "
        "WHERE card_uid IN (301,302) ORDER BY card_uid").fetchall()
    assert rows == [(301, "deck"), (302, "deck")], rows


def test_secondary_target_ignores_missing_source_uid(db):
    """A source-less nested activation must not expose ``None`` as a target."""
    from rules_port.resolution import resolve_port_ability
    from tests.native_records import synthetic_records

    ability = _ag("source-less-secondary")
    with synthetic_records() as rec:
        reveal = rec.effect(
            _ag("sl-reveal"), "RevealCardsAbilityEffectTemplate")
        store = rec.effect(
            _ag("sl-store"), "StoreTargetsAbilityEffectTemplate")
        rec.ability(ability, effects=[
            rec.mapping(reveal, instance=0, group=1),
            rec.mapping(store, instance=1, group=2, secondary=0),
        ])
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        resolve_port_ability(
            HandlerStub(db), game_engine.Game(1, pl_t, ai_t), SessionStub(),
            db, pl_t, ai_t, {"player_health": 20, "ai_health": 20},
            ability, None, 5)


def test_deck_search_detection_uses_filter_not_collection_flags(db):
    """A broad visibility mask must not turn a hand target into a deck search.

    Stargazer's DiscardACard target advertises all player-owned collections,
    including Deck, but its authoritative filter is InZone: Hand. Only an
    actual InZone: Deck filter should enter the class-39 deck-search path.
    """
    from rules_port.targeting import filter_restricts_to_zone

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

    assert not filter_restricts_to_zone(hand_filter, "Deck")
    assert filter_restricts_to_zone(deck_filter, "Deck")
    assert not filter_restricts_to_zone({
        "_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
        "m_Collection": "Deck|Hand",
    }, "Deck")


def test_empty_sacrifice_target_does_not_sacrifice_source(db):
    """An optional target with no legal card must not fall back to the source."""
    from rules_port.resolution import resolve_port_ability
    from tests.native_records import synthetic_records

    ability = _ag("empty-sacrifice")
    target = _ag("optional-troop")
    db.execute(
        "INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (target, "a troop you control", 0, 0, 0, 0, "Self", "Warzone",
         1, 1, '{"_t":"Game.Shared.Mechanics.Cards.Filters.IsTroop"}',
         "AbilityTargetTemplate"))
    with synthetic_records() as rec:
        rec.target(
            target, "AbilityTargetTemplate", is_auto=0, explicit=0,
            player_filter="Self", collection_flags="Warzone", minimum=1,
            maximum=1,
            card_filter={
                "_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"})
        sacrifice = rec.effect(
            _ag("empty-sacrifice-effect"),
            "SacrificeCardAbilityEffectTemplate")
        rec.ability(ability, effects=[rec.mapping(sacrifice)],
                    targets=[target])
        add_card(db, 200, 0, TPL_GLADIATOR, loc="warzone")
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        handler = HandlerStub(db)
        game = game_engine.Game(1, pl_t, ai_t)
        resolve_port_ability(handler, game, SessionStub(), db, pl_t, ai_t,
                             {"turn_number": 1}, ability, 200, 0)
    location = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=200").fetchone()[0]
    assert location == "warzone", location


def test_ai_sacrifice_target_excludes_source(db):
    """AI deploy targeting chooses another troop, or no target if absent."""
    from rules_port import targeting
    from tests.native_records import synthetic_records

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
    with synthetic_records() as rec:
        rec.target(
            target, "AbilityTargetTemplate", is_auto=0, explicit=0,
            player_filter="Self", collection_flags="Warzone", minimum=1,
            maximum=1,
            card_filter={
                "_t": "Game.Shared.Mechanics.Cards.Filters.IsTroop"})
        sacrifice = rec.effect(_ag("ai-sacrifice-effect"),
                               "SacrificeCardAbilityEffectTemplate")
        rec.ability(ability, effects=[rec.mapping(sacrifice)],
                    targets=[target])
        add_card(db, 210, 0, TPL_GLADIATOR, loc="warzone")
        session = SessionStub()
        assert targeting.ai_trigger_target(
            db, session, ability, 210, 0, {}, []) is None
        add_card(db, 211, 0, TPL_GLADIATOR, loc="warzone")
        assert targeting.ai_trigger_target(
            db, session, ability, 210, 0, {}, []) == 211

def test_double_choice_creates_random_choices_and_clears_before_second(db):
    """DoubleChoice follows the client sequence without using card text."""
    from rules_port.choice_effects import double_choice
    from rules_port.context import EffectContext

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
    context = EffectContext.from_rules_port(
        game, session, db, handler, pl_t, ai_t, bstate, first_guid, "")

    def template_value(name, default=None):
        if name == "m_SecondChoice":
            return context.effect_guid == second_guid
        if name == "m_Choices" and context.effect_guid == first_guid:
            return [{"m_Guid": guid} for guid in choice_guids]
        return default

    with mock.patch.object(context, "template_value",
                           side_effect=template_value), \
            mock.patch.object(context, "value", return_value=3), \
            mock.patch("rules_port.choice_effects.random.randrange",
                       side_effect=[0, 0, 0]):
        result = double_choice(context)
    assert "awaiting 3" in result, result
    assert len(bstate["pending_choice"]["choice_uids"]) == 3
    assert db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE location='choosing'").fetchone()[0] == 3
    bstate.pop("pending_choice", None)
    bstate.pop("resolution_paused", None)
    bstate["resolving_effect_order"] = 1
    context.effect_guid = second_guid
    with mock.patch.object(context, "template_value",
                           side_effect=template_value), \
            mock.patch.object(context, "value", return_value=3):
        result = double_choice(context)
    assert "awaiting 3" in result, result
    assert db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE location='choosing'").fetchone()[0] == 3
    assert db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE location='PlayedResources'").fetchone()[0] == 3


def test_summon_choosing_collection_stays_out_of_warzone(db):
    """A typed Choosing summon creates option cards, not permanents."""
    from rules_port.context import EffectContext
    from rules_port.token_effects import summon_token

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
    context = EffectContext.from_rules_port(
        game, session, db, HandlerStub(db), pl_t, ai_t, bstate, "", "")
    result = summon_token(context, {
        "token_guid": choice, "amount": 1, "collection": "Choosing"})
    assert "summoned 1" in result, result
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


def test_native_choice_target_opens_one_picker_for_all_options(db):
    """Native Choosing summons defer to their authored child target."""
    from rules_port.context import EffectContext

    target = SimpleNamespace(
        requires_input=True,
        target_kind="AbilityTargetTemplate",
        collection_flags="Deck|Choosing",
        player_filter="MultiplePlayers",
        guid=_ag("choice-target"),
        # Every authored choice-zone target restricts to Choosing through its
        # InZone filter; the visibility mask alone is not the zone contract.
        card_filter={
            "_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
            "m_Collection": "Choosing",
        })
    child = SimpleNamespace(targets=(target,))
    ability = SimpleNamespace(
        instance_id=9,
        continuation=lambda **kwargs: {
            "ability_guid": "parent",
            "source_uid": 77,
            "owner_id": 5,
            "target_map": {},
            "variables": {},
            "resume_effect_order": kwargs["resume_effect_order"],
        })
    prompts = []
    handler = SimpleNamespace(
        _prompt_choice_cards=lambda *args: prompts.append(args[-1]))
    context = EffectContext.from_rules_port(
        object(), SimpleNamespace(session_id=1), db, handler,
        game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000),
        {"resolving_source_uid": 77, "resolving_owner_id": 5,
         "resolving_effect_order": 3}, "effect", _ag("child"),
        ability=ability)
    with mock.patch("gamedata.ability_graph", return_value=child), \
            mock.patch("rules_port.targeting.legal_targets",
                       return_value=[101, 102]):
        result = context.activate_ability()
    assert "awaiting choice of 2" in result, result
    assert len(prompts) == 1
    assert prompts[0]["kind"] == "choice_zone_target"
    assert prompts[0]["choice_uids"] == [101, 102]


def test_hand_discard_child_asks_the_controller_and_discards_on_resume(db):
    """Bloatcap's Deathcry (and Giant Corpse Fly's Deploy) must ask to discard.

    Their shared "Discard a card" child targets a card in the chosen
    champion's hand, but the template advertises Choosing in its visibility
    mask.  The ActivateAbility picker branch read that mask, turned the child
    into a choice-zone picker holding the wrong cards, and the controller was
    never asked to discard.  Answering the class-23 picker must also re-enter
    the paused effect: resuming after it discarded nothing at all.
    """
    from rules_port.actions import AbilityResolutionState
    from rules_port.resolution import resolve_port_ability

    deathcry = "50a8dcb6-5733-c2d0-824b-943199bef45f"      # Bloatcap's Deathcry
    discard_child = "06570445-27e3-fc87-2e17-a7b5e1de693d"  # "Discard a card"
    hand_card = 401
    db.execute(
        "INSERT INTO target_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("84e4acf1-1f2e-abac-069d-8c6eb18b2b12", "a card from your hand",
         0, 0, 0, 0, "MultiplePlayers",
         # The authored template advertises every collection in its
         # visibility mask while its filter is the authoritative zone.
         "Deck|Hand|Champions|Warzone|Discard|Void|CastSpells|Underground|"
         "Choosing", 1, 1, json.dumps({
             "_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
             "m_Collection": "Hand",
         }), "AbilityTargetTemplate"))
    add_card(db, hand_card, 5, TPL_GLADIATOR, loc="hand")
    db.commit()

    class Handler(HandlerStub):
        def __init__(self, db):
            super().__init__(db)
            self.discard_prompts = []
            self.choice_prompts = []

        def _push_discard_prompt(self, _game, _session, _pl_t, _ai_t,
                                 _bstate, ability_guid=None):
            self.discard_prompts.append(ability_guid)
            return "prompted"

        def _prompt_choice_cards(self, *args):
            self.choice_prompts.append(args)

    def location():
        return db.execute("SELECT location FROM game_cards WHERE card_uid=?",
                          (hand_card,)).fetchone()[0]

    handler = Handler(db)
    pl_t, ai_t = game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"resolving_source_uid": 900, "resolving_owner_id": 0,
              "resolving_effect_order": 0,
              "_rules_port_native_effect": True}
    # The AI's Bloatcap died; its Deathcry auto-targets the opposing champion,
    # who then chooses a card from that champion's hand.
    assert resolve_port_ability(
        handler, game, SessionStub(), db, pl_t, ai_t, bstate,
        deathcry, 900, 0, target_map={}) is AbilityResolutionState.WAITING_FOR_INPUT
    assert handler.discard_prompts == [discard_child], handler.discard_prompts
    assert handler.choice_prompts == [], handler.choice_prompts
    assert bstate.get("resolution_paused") is True
    assert location() == "hand"

    continuation = bstate["pending_discard_continuation"]
    assert continuation["resume_effect_order"] == 0, continuation
    result = resolve_port_ability(
        handler, game, SessionStub(), db, pl_t, ai_t, bstate,
        discard_child, 900, 5, target_map={0: (hand_card,)},
        resume_from_order=int(continuation["resume_effect_order"]),
        instance_id=int(continuation["instance_id"]))
    assert result is AbilityResolutionState.COMPLETED, result
    assert location() == "discard"
    assert any(ev.__class__.__name__ == "CardDiscardedSessionEventArgs"
               for ev in game.events)


def test_native_deck_target_opens_the_deck_search_picker(db):
    """Scheme's "choose an action in your deck" must offer real candidates.

    The child target's authoritative zone is the deck, which the ChooseAndPlay
    picker cannot show: the client only lists cards it already holds in
    Choosing, so that prompt arrived with nothing to select.  The deck-search
    picker projects the candidates into Choosing and leaves them in the deck.
    """
    from rules_port.context import EffectContext

    target = SimpleNamespace(
        requires_input=True,
        target_kind="AbilityTargetTemplate",
        collection_flags="Deck|Hand|Warzone|Choosing",
        player_filter="MultiplePlayers",
        guid=_ag("deck-action-target"),
        card_filter={
            "_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
            "m_Collection": "Deck",
        })
    child = SimpleNamespace(targets=(target,))
    ability = SimpleNamespace(
        instance_id=9,
        continuation=lambda **kwargs: {
            "ability_guid": "parent",
            "source_uid": 77,
            "owner_id": 5,
            "target_map": {},
            "variables": {},
            "resume_effect_order": kwargs["resume_effect_order"],
        })
    prompts = []
    handler = SimpleNamespace(
        _prompt_deck_search=lambda *args, **kwargs: prompts.append(
            (args, kwargs)))
    context = EffectContext.from_rules_port(
        object(), SimpleNamespace(session_id=1), db, handler,
        game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000),
        {"resolving_source_uid": 77, "resolving_owner_id": 5,
         "resolving_effect_order": 0}, "effect", _ag("child"),
        ability=ability)
    with mock.patch("gamedata.ability_graph", return_value=child), \
            mock.patch("rules_port.targeting.legal_targets",
                       return_value=[101, 102]):
        result = context.activate_ability()
    assert "awaiting choice of 2" in result, result
    assert context.bstate.get("resolution_paused") is True
    assert len(prompts) == 1
    args, kwargs = prompts[0]
    assert kwargs["kind"] == "matching_target"
    assert args[5] == _ag("child"), args
    assert sorted(args[8]) == [101, 102], args
    continuation = kwargs["continuation"]
    assert continuation["ability_guid"] == _ag("child")
    assert continuation["target_index"] == 0
    assert continuation["parent"]["ability_guid"] == "parent"


def test_native_deck_target_ai_auto_selects_without_a_picker(db):
    """The AI resolves the same deck-restricted child with no client picker."""
    from rules_port.context import EffectContext
    from rules_port import resolution as port_resolution

    target = SimpleNamespace(
        requires_input=True,
        target_kind="AbilityTargetTemplate",
        collection_flags="Deck|Choosing",
        player_filter="MultiplePlayers",
        guid=_ag("ai-deck-target"),
        card_filter={
            "_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
            "m_Collection": "Deck",
        })
    child = SimpleNamespace(targets=(target,))
    ability = SimpleNamespace(instance_id=1, continuation=lambda **kwargs: {})
    context = EffectContext.from_rules_port(
        object(), SimpleNamespace(session_id=1), db, SimpleNamespace(),
        game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000),
        {"resolving_source_uid": 77, "resolving_owner_id": 0},
        "effect", _ag("child"), ability=ability)
    calls = {}

    def fake_resolve(_handler, _game, _session, _db, _pl_t, _ai_t, _bstate,
                     guid, _source, owner, **kwargs):
        calls.update(guid=guid, owner=owner, target_map=kwargs.get("target_map"))
        return "resolved"

    with mock.patch("gamedata.ability_graph", return_value=child), \
            mock.patch("rules_port.targeting.legal_targets",
                       return_value=[101, 102]), \
            mock.patch.object(port_resolution, "resolve_port_ability",
                              fake_resolve):
        assert context.activate_ability() == "resolved"
    assert calls == {"guid": _ag("child"), "owner": 0,
                     "target_map": {0: (101,)}}, calls


def test_choice_ability_transforms_real_parent(db):
    """Playing a Choice token applies its automatic ability to its parent."""
    from rules_port.choice_effects import (
        _play_choice_card, _resolve_choice_card_abilities)
    from rules_port.context import EffectContext

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
    from tests.native_records import synthetic_records
    with synthetic_records() as rec:
        transform = rec.effect(
            effect_guid, "TransformCardAbilityEffectTemplate",
            text=f"Transform this into <a data={output_tpl}>Choice Output</a>.",
            m_CardTemplateId={"m_Guid": output_tpl})
        rec.ability(choice_ability, effects=[rec.mapping(transform)],
                    targets=["190a4d8c-7c2c-10d0-6429-99c5aeb0791f"])
        db.commit()
        add_card(db, source_uid, 5, source_tpl)
        session = SessionStub()
        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        game = game_engine.Game(1, pl_t, ai_t)
        handler = HandlerStub(db)
        bstate = {"resolving_source_uid": source_uid,
                  "resolving_owner_id": 5}
        from rules_port.token_effects import summon_token
        summon_token(
            EffectContext.from_rules_port(
                game, session, db, handler, pl_t, ai_t, bstate, "", ""),
            {"token_guid": choice_tpl, "amount": 1,
             "collection": "Choosing"})
        choice_uid = bstate["created_token_uids"][0]
        context = EffectContext.from_rules_port(
            game, session, db, handler, pl_t, ai_t, bstate, "", "")
        assert _play_choice_card(context, choice_uid, 5)
        _resolve_choice_card_abilities(context, choice_uid, source_uid, 5)
    assert db.execute(
        "SELECT template_guid FROM game_cards WHERE card_uid=?",
        (source_uid,)).fetchone()[0] == output_tpl
    assert db.execute(
        "SELECT location FROM game_cards WHERE card_uid=?",
        (choice_uid,)).fetchone()[0] == "PlayedResources"


def test_records_target_filter_dict_is_parsed_for_choice_prompt(db):
    """Records TargetSpecs are typed dicts, not only legacy JSON strings."""
    from abilities.framework import resolution

    filter_data = {
        "_t": "Game.Shared.Mechanics.Cards.Filters.InZone",
        "m_Collection": "Choosing",
    }
    assert resolution._filter_has_exact_zone(
        resolution._parse_param(filter_data), "Choosing")


def test_attached_session_rejects_legacy_walker_even_with_native_dispatch(_db):
    """Passing a native leaf callback must not re-enable the legacy walker."""
    from abilities.framework.resolution import resolve_ability

    class AttachedSession:
        session_id = 9
        _rules_port_session = object()

    try:
        resolve_ability(
            object(), object(), AttachedSession(), object(), 1, 2,
            {"_rules_port_attached": True}, "not-a-live-ability", 101, 5,
            {}, native_effect=lambda *_args: "native")
    except RuntimeError as exc:
        assert "bypassed RulesPort" in str(exc)
    else:
        raise AssertionError("attached session entered legacy resolver")


def main():
    from abilities.framework import resolution

    tests = [
        ("Random variable + conditions + recursion",
         test_random_variable_conditions_and_recursion),
        ("Contingent effects gate on prerequisite",
         test_contingent_effect_applies_only_when_prerequisite_did),
        ("Empty revealed troop target is a no-op",
         test_empty_revealed_troop_target_does_not_move_stale_card),
        ("Source-less secondary target is empty",
         test_secondary_target_ignores_missing_source_uid),
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
        ("Native choice target opens one picker",
         test_native_choice_target_opens_one_picker_for_all_options),
        ("Hand discard asks then discards on resume",
         test_hand_discard_child_asks_the_controller_and_discards_on_resume),
        ("Native deck target opens the deck-search picker",
         test_native_deck_target_opens_the_deck_search_picker),
        ("Native deck target AI auto-selects",
         test_native_deck_target_ai_auto_selects_without_a_picker),
        ("Choice transforms its real parent",
         test_choice_ability_transforms_real_parent),
        ("Records choice filter preserves typed target data",
         test_records_target_filter_dict_is_parsed_for_choice_prompt),
        ("Attached session rejects legacy walker",
         test_attached_session_rejects_legacy_walker_even_with_native_dispatch),
    ]
    failed = 0
    for name, fn in tests:
        db = make_db()
        try:
            # These tests intentionally use tiny SQLite-only abilities. Keep
            # their source adapter in the test module so production resolution
            # has exactly one supported Records path.
            with mock.patch.object(resolution, "_effect_list",
                                   _fixture_effect_list), \
                    mock.patch.object(resolution, "_target_template_ids",
                                      _fixture_target_template_ids):
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
