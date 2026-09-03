"""Regression tests for the ported trigger/ability condition layer
(abilities/framework/condition_engine.py) — data-driven evaluation of the
gamedata m_TriggerCondition / m_AbilityCondition / effect-condition trees."""

import os
import json
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import game_engine

from abilities.framework.condition_engine import (
    ConditionContext,
    evaluate_effect_condition,
    trigger_condition_met,
)

SRC = os.environ.get(
    "HEX_TEST_SOURCE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db"),
)

SCRIVENER = "e6e77180-238a-a5db-08da-16f07cb67836"
ANGEL = "0d22faf5-a934-0983-ca9d-9d0a11636891"
INCANTATION = "3cf80f54-bb4b-a285-6bfe-f65bd75f0b76"
INC_COND = "d35818e3-7209-9c38-241f-6b5e2322d1c9"
INC_COUNTER = "12a1bb1f-6308-650c-4d75-35a12cb4c5cd"
DROO = "6a095431-820f-5d7c-dd9f-2eef65ce4e7c"
VILEFANG = "0ead517d-9926-d1ff-becf-fada9afc6f31"
RIDGE_RAIDER_DEATH = "3b79c597-7b6e-0896-7128-fd6b1df48f03"


class SessionStub:
    session_id = 1
    server_id = 100


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = sqlite3.connect(path)
    src = sqlite3.connect(SRC)
    db.execute("""CREATE TABLE game_cards (
        session_id INTEGER, user_id INTEGER, card_uid INTEGER,
        template_guid TEXT, card_template_id TEXT, location TEXT,
        position INTEGER, card_state INTEGER, card_abilities TEXT,
        card_type TEXT, card_attributes INTEGER, temporary_attributes INTEGER DEFAULT 0, card_attack_mod INTEGER,
        card_defense_mod INTEGER, card_damage INTEGER,
        permanent_buffs TEXT, temporary_buffs TEXT, card_uses TEXT,
        resolved_at INTEGER)""")
    db.execute("""CREATE TABLE card_abilities_meta (
        ability_guid TEXT, is_triggered INTEGER, trigger_event_type TEXT,
        game_text TEXT, raw_json TEXT, casting_behavior INTEGER,
        is_manual INTEGER, activation_cost INTEGER, uses_per_game INTEGER,
        uses_per_turn INTEGER, target_template_ids TEXT)""")
    db.execute("""CREATE TABLE card_templates (
        guid TEXT, name TEXT, card_type TEXT, cost INTEGER, attack INTEGER,
        defense INTEGER, attributes INTEGER, abilities_json TEXT,
        threshold_json TEXT, subtype TEXT)""")
    db.execute("""CREATE TABLE ability_effect_conditions (
        condition_id TEXT PRIMARY KEY, name TEXT, condition_json TEXT)""")
    db.execute("""CREATE TABLE card_counter_templates (
        template_id TEXT PRIMARY KEY, name TEXT, description TEXT)""")
    for ag in (SCRIVENER, ANGEL, INCANTATION, DROO, VILEFANG,
               RIDGE_RAIDER_DEATH):
        for row in src.execute(
                "SELECT ability_guid, is_triggered, trigger_event_type, game_text, "
                "raw_json, casting_behavior, is_manual, activation_cost, "
                "uses_per_game, uses_per_turn, target_template_ids "
                "FROM card_abilities_meta WHERE ability_guid=?", (ag,)):
            db.execute(
                "INSERT INTO card_abilities_meta VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
    for row in src.execute(
            "SELECT * FROM ability_effect_conditions WHERE condition_id=?", (INC_COND,)):
        db.execute("INSERT INTO ability_effect_conditions VALUES (?,?,?)", row)
    for row in src.execute(
            "SELECT * FROM card_counter_templates WHERE template_id=?", (INC_COUNTER,)):
        db.execute("INSERT INTO card_counter_templates VALUES (?,?,?)", row)
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("11111111-1111-1111-1111-111111111111", "Troop", "Troop", 1, 1, 1, 0,
         "[]", "[]", ""))
    db.execute(
        "INSERT INTO card_templates VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("22222222-2222-2222-2222-222222222222", "Artifact", "Artifact", 1, 1, 1,
         0, "[]", "[]", ""))
    src.close()
    db.commit()
    return db


def add_card(db, uid, owner, ctype, counters=None, loc="warzone", state=0):
    import json
    tpl = ("11111111-1111-1111-1111-111111111111" if ctype == "Troop"
           else "22222222-2222-2222-2222-222222222222")
    pb = json.dumps({"counters": counters or {},
                     "counter_guids": {"incantation": INC_COUNTER}
                     if counters and "incantation" in counters else {}})
    db.execute(
        "INSERT INTO game_cards VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,?,?,?,0)",
        (1, owner, uid, tpl, tpl, loc, 0, state, "[]", ctype, 0, pb, "{}", "{}"))
    db.commit()


def raw(db, ag):
    row = db.execute(
        "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?", (ag,)).fetchone()
    if row:
        return row[0]
    if ag != RIDGE_RAIDER_DEATH:
        return None
    # Champion-granted abilities are seeded in champion_abilities rather than
    # card_abilities_meta. The runtime field loader has the same Records
    # fallback used by live trigger resolution.
    from abilities.framework.fields import ability_record
    import json
    return json.dumps(ability_record(db, ag))


def run(name, fn):
    db = make_db()
    try:
        fn(db)
        print(f"PASS {name}")
    except AssertionError as e:
        print(f"FAIL {name}: {e}")
    except Exception as e:
        import traceback
        print(f"ERROR {name}: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        db.close()


def ctx(db, **kw):
    return ConditionContext(db, SessionStub(), kw.pop("bstate", {}), **kw)


def test_scrivener_troop_vs_artifact(db):
    add_card(db, 100, 5, "Troop")
    c = ctx(db, event_type="CardEnteredZoneEvent",
            ability_source_uid=100, ability_source_owner_id=5,
            trigger_uid=100)
    assert trigger_condition_met(raw(db, SCRIVENER), c)


def test_scrivener_artifact_blocked(db):
    add_card(db, 100, 5, "Artifact")
    c = ctx(db, event_type="CardEnteredZoneEvent",
            ability_source_uid=100, ability_source_owner_id=5,
            trigger_uid=100)
    assert not trigger_condition_met(raw(db, SCRIVENER), c)


def test_angel_first_draw_threshold(db):
    add_card(db, 100, 5, "Troop")
    bstate = {"player_draws_this_turn": 1,
              "player_threshold": {game_engine.ECardShards.Diamond: 1}}
    c = ctx(db, event_type="CardDrawnEvent",
            ability_source_uid=100, ability_source_owner_id=5,
            trigger_uid=100, bstate=bstate)
    assert trigger_condition_met(raw(db, ANGEL), c)


def test_angel_second_draw_blocked(db):
    add_card(db, 100, 5, "Troop")
    bstate = {"player_draws_this_turn": 2,
              "player_threshold": {game_engine.ECardShards.Diamond: 1}}
    c = ctx(db, event_type="CardDrawnEvent",
            ability_source_uid=100, ability_source_owner_id=5,
            trigger_uid=100, bstate=bstate)
    assert not trigger_condition_met(raw(db, ANGEL), c)


def test_incantation_counter_condition(db):
    add_card(db, 200, 5, "Constant", counters={"incantation": 4})
    c4 = ctx(db, ability_source_uid=200, ability_source_owner_id=5)
    assert not evaluate_effect_condition(db, INC_COND, c4)
    db.execute(
        "UPDATE game_cards SET permanent_buffs='{\"counters\": {\"incantation\": 5}, "
        "\"counter_guids\": {\"incantation\": \"%s\"}}' WHERE card_uid=200" % INC_COUNTER)
    db.commit()
    c5 = ctx(db, ability_source_uid=200, ability_source_owner_id=5)
    assert evaluate_effect_condition(db, INC_COND, c5)


def test_source_passes_filter_condition(db):
    """Droo's Colossal Walker: 'While this is exhausted: [BASIC] Pay 8 health
    — Ready this.' — RequiresSourcePassesFilterCondition(IsTapped) gates the
    manual activation, so it is only activatable while tapped."""
    add_card(db, 300, 5, "Troop")
    db.execute("UPDATE game_cards SET card_state=1 WHERE card_uid=300")  # Tapped
    db.commit()
    c = ctx(db, ability_source_uid=300, ability_source_owner_id=5)
    assert trigger_condition_met(raw(db, DROO), c)
    db.execute("UPDATE game_cards SET card_state=0 WHERE card_uid=300")
    db.commit()
    c2 = ctx(db, ability_source_uid=300, ability_source_owner_id=5)
    assert not trigger_condition_met(raw(db, DROO), c2)


def test_ability_variable_condition_accepts_variable_rhs(db):
    """AbilityVariableCondition can compare two ability variables."""
    condition_id = "condition-variable-rhs"
    db.execute(
        "INSERT INTO ability_effect_conditions VALUES (?,?,?)",
        (condition_id, "SacrificeWasMade", json.dumps({
            "_t": "Game.Shared.Mechanics.Abilities.Conditions."
                   "AbilityVariableCondition",
            "m_Lhs": "a", "m_Rhs": "SacrificedCards",
            "m_ComparisonOp": "LessThanOrEqual"})))
    db.commit()
    no_sacrifice = ctx(db)
    no_sacrifice.ability_variables = {"a": 1, "SacrificedCards": 0}
    assert not evaluate_effect_condition(db, condition_id, no_sacrifice)
    sacrifice = ctx(db)
    sacrifice.ability_variables = {"a": 1, "SacrificedCards": 1}
    assert evaluate_effect_condition(db, condition_id, sacrifice)


def test_vilefang_spider_trigger_fails_closed_for_unknown_hand_card(db):
    """A template-less legacy/fallback card entering Hand is not a Spider
    entering Warzone.  Missing card-template metadata must not turn the
    metadata condition into an unconditional Vilefang damage trigger."""
    db.execute(
        "INSERT INTO game_cards VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,?,?,?,0)",
        (1, 0, 400, None, None, "hand", 0, 0, "[]", "Troop", 0,
         "{}", "{}", "{}"))
    db.commit()
    c = ctx(db, event_type="CardEnteredZoneEvent",
            ability_source_uid=999, ability_source_owner_id=0,
            trigger_uid=400)
    assert not trigger_condition_met(raw(db, VILEFANG), c)


def test_ridge_raider_requires_dead_warzone_troop(db):
    """Ridge Raider's authored previous-state trigger only accepts a real
    Warzone -> Discard death, not a hand/deck card buried into the crypt."""
    bstate = {"turn_player": "player"}
    add_card(db, 500, 5, "Troop", loc="discard",
             state=game_engine.ECardStates.Dead)
    dead = ctx(db, event_type="CardEnteredZoneEvent",
               ability_source_uid=500, ability_source_owner_id=5,
               trigger_uid=500, bstate=bstate,
               event_source_collection="warzone",
               event_destination_collection="discard",
               event_previous_state=game_engine.ECardStates.Dead,
               uses_previous_state=True)
    assert trigger_condition_met(raw(db, RIDGE_RAIDER_DEATH), dead)

    not_dead = ctx(db, event_type="CardEnteredZoneEvent",
                   ability_source_uid=500, ability_source_owner_id=5,
                   trigger_uid=500, bstate=bstate,
                   event_source_collection="warzone",
                   event_destination_collection="discard",
                   event_previous_state=0,
                   uses_previous_state=True)
    assert not trigger_condition_met(raw(db, RIDGE_RAIDER_DEATH), not_dead)

    for source in ("deck", "hand"):
        buried = ctx(db, event_type="CardEnteredZoneEvent",
                     ability_source_uid=500, ability_source_owner_id=5,
                     trigger_uid=500, bstate=bstate,
                     event_source_collection=source,
                     event_destination_collection="discard",
                     event_previous_state=0,
                     uses_previous_state=True)
        assert not trigger_condition_met(raw(db, RIDGE_RAIDER_DEATH), buried)


if __name__ == "__main__":
    run("Scrivener fires for troop entry", test_scrivener_troop_vs_artifact)
    run("Scrivener blocked for artifact entry", test_scrivener_artifact_blocked)
    run("Angel fires on first draw w/ Diamond", test_angel_first_draw_threshold)
    run("Angel blocked on second draw", test_angel_second_draw_blocked)
    run("Incantation SourceCardHasCounters gate", test_incantation_counter_condition)
    run("Source passes filter gates activation", test_source_passes_filter_condition)
    run("Ability variable condition accepts variable RHS",
        test_ability_variable_condition_accepts_variable_rhs)
    run("Vilefang trigger fails closed for unknown hand card", test_vilefang_spider_trigger_fails_closed_for_unknown_hand_card)
    run("Ridge Raider only triggers for dead warzone troops", test_ridge_raider_requires_dead_warzone_troop)
