"""Regression tests for the chain/trigger fixes from the Counter-deck test:

  * Brood Creeper / Spawn of Othuyeg — "When this deals damage to an opposing
    champion" triggers now receive the damaged champion as the trigger TARGET
    and the condition engine resolves champions (IsHero / controls-target).
  * Trigger collection flags — a hand card whose trigger requires
    Champions|Warzone (e.g. Incantation of Ascendance drawn into hand) no
    longer fires from the hand; the same card in the warzone does.
  * Countermagic — "Interrupt target card" is only legal while a card is in
    CastSpells, and the CounterSpell leaf moves the interrupted card to the
    graveyard so its BOM never resolves.
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

# Bind this process's test database before any runtime import
# opens ``db``; the live ``hconnect.db`` is never opened.
SRC = fresh_database()

import game_engine

from tests.tests_cards_fixes import _copy_card, _copy_ability
from tests.tests_combat import (make_db, add_card, HandlerStub, SessionStub,
                                TPL_GLADIATOR)


TPL_BROOD_CREEPER = "5f2c8a4b-5f38-4743-aff8-a1bd5abd9ad5"
TPL_SPIDESPAWN = "a9ebe40e-ef30-4c9e-b4dd-1b414dc35d0c"
TPL_INCANTATION = "3a6c51e8-cf1a-4b76-a774-010003648323"
TPL_COUNTERMAGIC = "16c354dd-50a7-45fb-b4e6-309d27cb6575"
TPL_SPAWN = "100e05a3-9993-4edd-a2fe-66f8565c345e"
TPL_CHRONIC_MADNESS = "b717f238-7488-46fd-82a6-0d7f2efc9623"
TPL_INCUBATE = "ae6ffe36-c358-4ea1-94cc-d4294c1d9b1c"
TPL_SPIDERLING_EGG = "32bf0698-63d0-483f-9fc1-7f0d75808192"

AG_BROOD_DAMAGE = "aa9ca993-d6b0-8eda-b6d1-09cc622dd5d0"
AG_INCANTATION_DRAW = "e6945521-c4bf-9b3c-5bb6-82dc8ae0f82d"
AG_COUNTERMAGIC = "ecd8264c-306a-1d07-f685-0c8b2ef3d3bf"
AG_SPAWN_DAMAGE = "f0d7ccb0-b6d0-ed8c-819c-e58acd8a806c"
AG_CHRONIC_BURY = "b2a0ec2d-f844-2dc4-34d2-3a0c2b94c73d"
AG_CHRONIC_ESCALATE = "0e2a9042-06c2-d0f3-51f2-8c9115601980"
AG_BUNJITSU = "32d0d36a-55fd-2cff-0d3d-341319536a57"
TID_INTERRUPT = "cf070006-3fe9-82d3-5f13-343d1d7ee517"
TID_BUNJITSU_VOID = "becbfb96-fea8-e8ec-234b-b066d1f7184c"
TID_LIGHTNING = "fb84ad94-e6ed-f04b-353d-eda325e0ae43"
TPL_TOMB_LORD = "dc748c9a-9b04-4279-93d6-19b06cbde108"
TPL_INFILTRATOR = "cad6307e-bafc-492f-84f6-3b914071d5d3"
TPL_INCANT_FEAR = "f8103511-772f-40ea-8599-04d520508bac"
AG_INCANT_FEAR = "1026a613-0814-a633-0869-3d35aaa8dd72"
TPL_STRENGTH_REDWOOD = "27e20321-3e24-4802-8ffe-b4579616ff5c"
AG_HARDSHELL_LOSE_LIFE = "3c64eeac-7953-d876-67c1-445b90b8ccbc"
AG_PSYCHOTIC_CHANCE = "0897aeba-a167-e714-8bd1-5b162af7752b"
COND_PSYCHOTIC_CHANCE = "699011c9-5f13-8570-d32e-de114d01207d"
TPL_REESE = "09770f1d-aca6-4c15-a479-7fcbede6384b"
AG_REESE_REPLACEMENT = "cfede135-b890-9aee-0a2c-b3007869c40a"
TPL_GHASTLY_EXCHANGE = "36fd3cb2-ab3d-41f5-9037-fbae95a0e8e9"
AG_GHASTLY_TURN_BURY = "8d0102c4-8d59-5e20-b14d-2a7febdaad47"
TPL_MOONARIU_SENSEI = "884c641e-b76b-4375-a7cc-b09f748840dc"
AG_SENSEI_DEPLOY_DRAW = "dfc60750-4bb5-8218-770e-7d3a37be8da7"
AG_SENSEI_ONESHOT_DEATHCRY = "89285cf9-97ba-5b40-3a91-ba14ecfccd2a"


def _pl_ai():
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    return pl_t, ai_t


def test_cards_attacked_dispatch_uses_group_count_once(db):
    """The attack-group event carries NumAttackers and is not replayed."""
    from unittest import mock
    from abilities.framework import triggers
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"turn_number": 4}
    source_uid = int(handler._player_champ_scid.uid.uid64)
    with mock.patch.object(triggers, "resolve_triggers",
                           return_value="fired") as dispatch:
        assert triggers.resolve_cards_attacked(
            db, handler, game, SessionStub(), pl_t, ai_t, bstate,
            source_uid, 5, [103, 101, 102]) == "fired"
        assert triggers.resolve_cards_attacked(
            db, handler, game, SessionStub(), pl_t, ai_t, bstate,
            source_uid, 5, [101, 102, 103]) == ""
    assert dispatch.call_count == 1
    assert dispatch.call_args.args[7:9] == (
        "CardsAttackedEvent", source_uid)
    assert dispatch.call_args.kwargs["event_tac"]
    from abilities.framework.tac import _tac_attr_hash
    assert dispatch.call_args.kwargs["event_tac"][_tac_attr_hash(
        "NumAttackers")] == 3


def test_card_battled_dispatch_is_directional(db):
    """A card battle gives each participant its own trigger perspective."""
    from unittest import mock
    from abilities.framework import triggers
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    with mock.patch.object(triggers, "resolve_triggers",
                           return_value="fired") as dispatch:
        assert triggers.resolve_card_battled(
            db, handler, game, SessionStub(), pl_t, ai_t, {},
            101, 5, 202, 0) == "fired"
    assert dispatch.call_count == 1
    assert dispatch.call_args.args[7:9] == ("CardBattledEvent", 101)
    assert dispatch.call_args.kwargs["extra_target"] == 202


def test_lose_life_modifier_is_not_damage(db):
    """LoseLifeModifier must not recursively fire damage replacement hooks."""
    from tests.tests_cards_fixes import _copy_ability
    from abilities.framework.resolution import resolve_ability

    _copy_ability(db, AG_HARDSHELL_LOSE_LIFE)
    pl_t, ai_t = _pl_ai()
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    source_uid = int(handler._player_champ_scid.uid.uid64)
    resolve_ability(handler, game_engine.Game(1, pl_t, ai_t), SessionStub(),
                    db, pl_t, ai_t, bstate, AG_HARDSHELL_LOSE_LIFE,
                    source_uid, 5, {0: source_uid})
    assert bstate["player_health"] == 19, bstate


def test_generated_card_uid_is_independent_of_row_id(db):
    """Generated tokens must not reuse a SessionCardId from another card.

    A card's SQLite row id and its UID instance are separate sequences.  This
    reproduces the live collision where a newly summoned Worker Bot was
    serialized with an existing Pack Raptor's UID.
    """
    from abilities.framework._shared import next_game_card_uid

    existing_uid = int(game_engine.UID.make(1, 45193).uid64)
    add_card(db, existing_uid, 0, TPL_SPAWN)
    generated_uid = next_game_card_uid(db, 1)
    assert generated_uid != existing_uid
    assert game_engine.UID(generated_uid).instance_id == 45194

    add_card(db, generated_uid, 0, TPL_SPAWN)
    next_uid = next_game_card_uid(db, 1)
    assert next_uid != generated_uid
    assert game_engine.UID(next_uid).instance_id == 45195


def test_brood_creeper_damage_to_opposing_champion_summons(db):
    """Brood Creeper deals combat damage to the player's champion -> its
    CardDealtDamageEvent trigger fires (source card == ability source, target
    is an opposing hero) and summons a Spiderspawn under the AI."""
    from abilities.framework.triggers import (
        resolve_triggers, resolve_stack_trigger)
    _copy_card(db, TPL_BROOD_CREEPER)
    _copy_card(db, TPL_SPIDESPAWN)
    add_card(db, 101, 0, TPL_BROOD_CREEPER)
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
        (json.dumps([AG_BROOD_DAMAGE]),))
    db.commit()
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    player_champ_uid = int(handler._player_champ_scid.uid.uid64)
    # The AI's Brood Creeper damaged the player's champion.
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardDealtDamageEvent", 101, 0,
                     extra_target=player_champ_uid)
    # The trigger does not ignore the chain (m_IgnoresChain=0): resolve the
    # pushed item the way the server's stack drain would.
    items = list(bstate.get("stack") or [])
    assert items, "Brood Creeper trigger should have fired"
    for item in items:
        bstate["stack"].remove(item)
        resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                              bstate, item)
    spiders = db.execute(
        "SELECT user_id, location FROM game_cards "
        "WHERE template_guid=? AND card_uid != 101",
        (TPL_SPIDESPAWN,)).fetchall()
    assert spiders and spiders[0] == (0, "warzone"), spiders
    # Serializing the pushed events must not crash: a token CardUpdated with
    # state=None used to blow up the wire encoder mid-combat (the AI Brood
    # Creeper crash) — the summoned token always carries CameOutThisTurn.
    game.make_network_packet(pl_t)


def test_brood_creeper_does_not_fire_on_own_champion(db):
    """The same trigger must NOT fire when the ability source's controller
    controls the damaged champion (the Not(TriggerPlayerControlsTarget) gate).
    A troop can't hit its own champion in combat, so simulate the event where
    the AI's Brood Creeper's owner controls the target."""
    from abilities.framework.triggers import resolve_triggers
    _copy_card(db, TPL_BROOD_CREEPER)
    _copy_card(db, TPL_SPIDESPAWN)
    add_card(db, 101, 0, TPL_BROOD_CREEPER)
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
        (json.dumps([AG_BROOD_DAMAGE]),))
    db.commit()
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    ai_champ_uid = int(handler._ai_champ_scid.uid.uid64)
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardDealtDamageEvent", 101, 0,
                     extra_target=ai_champ_uid)
    assert not (bstate.get("stack") or []), "own-champion hit must not fire"


def test_spawn_of_othuyeg_buries_one_or_five(db):
    """Spawn of Othuyeg deals damage to an opposing champion: with fewer than
    ten cards in opposing crypts it buries one top card of their deck; with ten
    or more it buries five (data-driven gated branches — the effect list has
    two StoreTargets leaves, so the backfill must not collapse them)."""
    from abilities.framework.triggers import (
        resolve_triggers, resolve_stack_trigger)
    _copy_card(db, TPL_SPAWN)
    for i, uid in enumerate((301, 302, 303, 304, 305)):
        add_card(db, uid, 5, "14909185-1070-48df-9508-61d5a9650bd2",
                 loc="deck")  # player deck cards to bury
    add_card(db, 101, 0, TPL_SPAWN)
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
        (json.dumps([AG_SPAWN_DAMAGE]),))
    db.commit()
    pl_t, ai_t = _pl_ai()
    handler = HandlerStub(db)

    def fire(crypt_cards, deck_uids, crypt_uids):
        # Reset the player's deck to exactly deck_uids so each call buries a
        # fresh top set (the earlier call already buried its own cards).
        db.execute(
            "DELETE FROM game_cards WHERE session_id=1 AND user_id=5 "
            "AND location='deck'")
        db.commit()
        for uid in deck_uids:
            add_card(db, uid, 5, "14909185-1070-48df-9508-61d5a9650bd2",
                     loc="deck")
        game = game_engine.Game(1, pl_t, ai_t)
        bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
        for i in range(crypt_cards):
            add_card(db, crypt_uids[i], 5,
                     "14909185-1070-48df-9508-61d5a9650bd2",
                     loc="discard")  # player crypt filler
        player_champ_uid = int(handler._player_champ_scid.uid.uid64)
        resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                         "CardDealtDamageEvent", 101, 0,
                         extra_target=player_champ_uid)
        for item in list(bstate.get("stack") or []):
            bstate["stack"].remove(item)
            resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                                  bstate, item)
        return db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE session_id=1 "
            "AND card_uid IN (%s) AND location='discard'"
            % ",".join("?" * len(deck_uids)), deck_uids
        ).fetchone()[0]

    assert fire(0, [301, 302, 303, 304, 305],
                list(range(401, 401))) == 1, "fewer than ten crypt cards buries one"
    assert fire(10, [311, 312, 313, 314, 315],
                list(range(501, 511))) == 5, "ten or more crypt cards buries five"


def test_hand_incantation_trigger_does_not_fire(db):
    """Incantation of Ascendance drawn into the hand must NOT fire its
    CardDrawnEvent trigger from the hand (m_TriggerCollectionFlags =
    Champions|Warzone) — this was the "AI played it without spending mana"
    symptom: the draw trigger put the hand card on the chain."""
    from abilities.framework.triggers import resolve_triggers
    _copy_card(db, TPL_INCANTATION)
    add_card(db, 101, 0, TPL_INCANTATION, loc="hand")
    add_card(db, 102, 0, TPL_INCANTATION, loc="warzone")
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid IN (101,102)",
        (json.dumps([AG_INCANTATION_DRAW]),))
    db.commit()
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    ai_champ_uid = int(handler._ai_champ_scid.uid.uid64)
    # AI draws a card (its hand + warzone cards react; the gate keeps hand
    # Incantations off the chain).
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardDrawnEvent", ai_champ_uid, 0, extra_target=103)
    items = bstate.get("stack") or []
    assert len(items) == 1, items  # only the warzone Incantation fires
    assert items[0]["source_uid"] == 102


def test_countermagic_requires_castspells_target(db):
    """Countermagic's only legal target template requires a card in CastSpells:
    with an empty chain the card must not be playable; with a spell on the
    chain it is.  Resolving the CounterSpell leaf moves the target to discard."""
    from abilities.framework.targeting import legal_targets
    from abilities.framework.resolution import resolve_ability
    _copy_card(db, TPL_COUNTERMAGIC)
    _copy_card(db, TPL_INCANTATION)
    _copy_card(db, TPL_SPAWN)
    add_card(db, 101, 5, TPL_COUNTERMAGIC, loc="hand")
    add_card(db, 203, 5, TPL_INCANTATION, loc="hand")
    pl_t, ai_t = _pl_ai()
    # Empty chain (no CastSpells cards): no legal interrupt targets, so the
    # card must not be playable.
    assert legal_targets(db, 1, 5, TID_INTERRUPT, 0, both_players=True,
                         champions=[]) == []
    # A TROOP on the chain (e.g. the AI's Spawn of Othuyeg) is a legal
    # interrupt target too — the CastSpells filter accepts any card type.
    add_card(db, 204, 0, TPL_SPAWN, loc="CastSpells")
    troop_cands = legal_targets(db, 1, 5, TID_INTERRUPT, 0, both_players=True,
                                champions=[])
    assert 204 in troop_cands, troop_cands
    db.execute("DELETE FROM game_cards WHERE card_uid=204")
    db.commit()
    # The AI's Incantation sits on the chain: the interrupt has one target.
    add_card(db, 202, 0, TPL_INCANTATION, loc="CastSpells")
    cands = legal_targets(db, 1, 5, TID_INTERRUPT, 0, both_players=True,
                          champions=[])
    assert 202 in cands and 203 not in cands, cands
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1,
              "stack": [{"kind": "troop", "source_uid": 202,
                         "target_uid": None, "instance_id": 1}]}
    out = resolve_ability(handler, game, SessionStub(), db, pl_t, ai_t,
                          bstate, AG_COUNTERMAGIC, 101, 5, {0: 202})
    assert "countered" in (out or "").lower(), out
    assert not bstate["stack"], bstate["stack"]
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=202").fetchone()[0]
    assert loc == "discard", loc
    discard_updates = [e for e in game.events
                       if isinstance(e, game_engine.CardUpdatedSessionEventArgs)
                       and int(e.session_card_id.uid.uid64) == 202
                       and e.collection == game_engine.ECardCollections.Discard]
    assert discard_updates, "countered card needs a full discard update"


def test_countermagic_offered_in_ai_chain_window(db):
    """The exact state the user hit: the AI's troop sits in CastSpells (on the
    chain) during the response window; the player has Countermagic in hand with
    3 resources + 2 sapphire.  The chain-window options push must mark the card
    playable AND attach the CastSpells TargetInstance so the client's
    CanUseAbility passes and the target picker opens."""
    import hconnect_server as hcs
    import db as dbmod
    _copy_card(db, TPL_COUNTERMAGIC)
    _copy_card(db, TPL_SPAWN)
    db.execute("ALTER TABLE card_templates ADD COLUMN sacrifice_target TEXT DEFAULT ''")
    db.commit()
    add_card(db, 101, 5, TPL_COUNTERMAGIC, loc="hand")
    add_card(db, 202, 0, TPL_SPAWN, loc="CastSpells")  # AI troop on the chain
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
               (json.dumps([AG_COUNTERMAGIC]),))
    db.commit()
    old_db, old_hcs = dbmod._db, hcs._db
    dbmod._db, hcs._db = db, db
    try:
        h = object.__new__(hcs.HCPHandler)
        h._db = db
        h.user_profile = {"id": 5}
        h._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        h._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))
        h._current_bstate = {"player_health": 20, "ai_health": 20}
        # The exact checks _push_phase_options_empty runs for each QuickAction.
        playable = h._hand_card_playable(
            SessionStub(), 101, "QuickAction", 3,
            '{"values": [0, 0, 0, 2, 0, 0], "list": [3, 3]}',
            [AG_COUNTERMAGIC], 3, {16: 2}, True, 0, 0)
        assert playable is True, "Countermagic must be playable with 3 mana/2 sapphire"
        # And the target picker candidates for the interrupt template.
        plan = h._card_play_plan(TPL_COUNTERMAGIC, 101, 5)
        targets = h._play_ability_targets(
            SessionStub(), plan)
        assert any(t[:8] == TID_INTERRUPT[:8] and
                   any(int(x.uid.uid64) == 202 for x in ts)
                   for _, _, t, ts in targets), targets
    finally:
        dbmod._db, hcs._db = old_db, old_hcs


def test_strength_of_redwood_targets_combat_troop(db):
    """A combat troop remains a legal Redwood target in a priority window.

    This is the state after Howling Brave has generated a resource while the
    player is paused after blockers: the QuickAction must be offered and its
    TargetInstance must include the attacking troop.  The battle state is
    passed through so transient combat filters remain available to the same
    target evaluator used by the live option builder.
    """
    import hconnect_server as hcs
    import db as dbmod
    _copy_card(db, TPL_STRENGTH_REDWOOD)
    _copy_card(db, TPL_SPAWN)
    # UID 101 is the attacking troop; it is deliberately not the Redwood card.
    add_card(db, 101, 5, TPL_STRENGTH_REDWOOD, loc="hand")
    add_card(db, 102, 5, TPL_SPAWN, loc="warzone",
             state=game_engine.ECardStates.Attacking |
                   game_engine.ECardStates.HasAttacked)
    pl_t, ai_t = _pl_ai()
    old_db, old_hcs = dbmod._db, hcs._db
    dbmod._db, hcs._db = db, db
    try:
        h = object.__new__(hcs.HCPHandler)
        h._db = db
        h.user_profile = {"id": 5}
        h._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        h._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))
        h._current_bstate = {
            "turn_player": "player",
            "turn_number": 1,
            "player_attackers": {"102": "0"},
            "ai_blockers": {"102": ["103"]},
        }
        # The card's Wild threshold is met and Howling Brave has left two
        # resources available.  This exercises the same predicate used by
        # _push_phase_options_empty after a troop ability resolves.
        assert h._hand_card_playable(
            SessionStub(), 101, "QuickAction", 1,
            '{"list": [4]}',
            ["90f5fcfe-aeff-13e1-0f8c-60d0f7b3b972"],
            2, {32: 2}, True, 1, 0)
        plan = h._card_play_plan(TPL_STRENGTH_REDWOOD, 101, 5)
        targets = h._play_ability_targets(
            SessionStub(), plan, battle_state=h._current_bstate)
        assert targets, "Strength of the Redwood needs a troop target"
        assert any(102 in [int(x.uid.uid64) for x in candidate_uids]
                   for _, _, _, candidate_uids in targets), targets
    finally:
        dbmod._db, hcs._db = old_db, old_hcs


def test_chronic_madness_buries_escalates_and_returns_to_deck(db):
    """Chronic Madness: "Bury the top ESC:4 cards of target champion's deck.
    Escalation." — the first cast buries 4 (ESC starts at 1), each later cast
    escalates by 4, the buried cards render face-up in the discard (CardUpdated
    events), and the spell itself is put back into its owner's deck at a
    random index on resolution."""
    from abilities import resolve_played_spell
    _copy_card(db, TPL_CHRONIC_MADNESS)
    ai_deck = list(range(401, 421))  # 20 AI deck cards to bury
    for uid in ai_deck:
        add_card(db, uid, 0, "14909185-1070-48df-9508-61d5a9650bd2",
                 loc="deck")
    pl_t, ai_t = _pl_ai()
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    ai_champ = int(handler._ai_champ_scid.uid.uid64)

    def cast(uid):
        add_card(db, uid, 5, TPL_CHRONIC_MADNESS, loc="hand")
        db.execute(
            "UPDATE game_cards SET card_abilities=? WHERE card_uid=?",
            (json.dumps([AG_CHRONIC_BURY, AG_CHRONIC_ESCALATE]), uid))
        db.commit()
        game = game_engine.Game(1, pl_t, ai_t)
        bstate["player_spell_target"] = ai_champ
        bstate["resolving_source_uid"] = uid
        bstate["resolving_owner_id"] = 5
        out = resolve_played_spell(
            game, SessionStub(), db, handler, pl_t, ai_t, bstate,
            [AG_CHRONIC_BURY, AG_CHRONIC_ESCALATE])
        bstate.pop("player_spell_target", None)
        return game, out

    game1, out1 = cast(101)
    assert "bury 4 cards" in out1, out1
    assert "escalate player" in out1, out1
    buried1 = db.execute(
        "SELECT card_uid FROM game_cards WHERE session_id=1 AND card_uid IN (%s) "
        "AND location='discard'" % ",".join("?" * len(ai_deck)),
        ai_deck).fetchall()
    assert len(buried1) == 4, buried1
    # The buried cards each got a face-up CardUpdated into the Discard.
    buried_uids = {int(r[0]) for r in buried1}
    upd = [e for e in game1.events
           if isinstance(e, game_engine.CardUpdatedSessionEventArgs)
           and int(e.session_card_id.uid.uid64) in buried_uids]
    assert len(upd) == 4, len(upd)
    assert all(e.collection == game_engine.ECardCollections.Discard
               for e in upd), [e.collection for e in upd]
    # The spell itself returned to a uniformly selected deck-relative slot;
    # position zero is valid when it lands on top.
    loc, pos = db.execute(
        "SELECT location, position FROM game_cards WHERE card_uid=101"
    ).fetchone()
    deck_count = db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=1 AND user_id=5 "
        "AND location='deck'"
    ).fetchone()[0]
    assert loc == "deck" and 0 <= int(pos or 0) < int(deck_count), \
        (loc, pos, deck_count)
    assert bstate.get("player_escalation_uses") == 1, bstate

    # Second cast escalates: ESC*4 with count 2 buries 8.
    for uid in range(421, 441):
        add_card(db, uid, 0, "14909185-1070-48df-9508-61d5a9650bd2",
                 loc="deck")
    game2, out2 = cast(102)
    assert "bury 8 cards" in out2, out2
    assert bstate.get("player_escalation_uses") == 2, bstate
    loc2 = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=102").fetchone()[0]
    assert loc2 == "deck", loc2


def test_bunjitsu_void_cost_is_a_cost_instance(db):
    """Bun'jitsu's champion power ("Void two ready troops you control") must
    be delivered to the client as a CostInstance (EAbilityCostType.Void) with
    the two-troop picker — not as a plain effect TargetInstance.  The client's
    BattleStateAssignXCost reads GetCostsFor(); without the CostInstance it
    loops forever with an empty X-cost dialog and never asks for the troops."""
    import hconnect_server as hcs
    import db as dbmod
    from domain.events import CostInstanceSessionEventArgs
    _copy_ability(db, AG_BUNJITSU)
    add_card(db, 101, 5, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd")
    add_card(db, 102, 5, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd")
    db.commit()
    old_db, old_hcs = dbmod._db, hcs._db
    dbmod._db, hcs._db = db, db
    try:
        h = object.__new__(hcs.HCPHandler)
        h._db = db
        h.user_profile = {"id": 5}
        h._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        h._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))
        h._current_bstate = {"player_health": 20}
        rid = game_engine.ResourceId.from_str(AG_BUNJITSU)
        champ_uid = int(h._player_champ_scid.uid.uid64)
        targets = h._champion_ability_targets(
            SessionStub(), [rid], champ_uid)
        assert TID_BUNJITSU_VOID not in [
            e[0] for v in targets.values() for e in v], targets
        costs = h._champion_ability_costs(
            SessionStub(), [rid], champ_uid)
        entry = costs.get(AG_BUNJITSU)
        assert entry and entry[0][0] == TID_BUNJITSU_VOID, entry
        tid, ctype, cands, mn, mx = entry[0]
        assert ctype == 16 and mn == 2 and mx == 2, entry[0]  # Void, 2 troops
        assert set(cands) == {101, 102}, cands
        # The CostInstance event serializes on the wire (class 66) without
        # crashing — the empty-XCost client loop is what the user saw.
        ev = CostInstanceSessionEventArgs()
        ev.min_target_count = mn
        ev.max_target_count = mx
        ev.cost_type = ctype
        ev.targets = [game_engine.SessionCardId(game_engine.UID(int(u)))
                      for u in cands]
        ev.target_template_id = game_engine.ResourceId.from_str(tid)
        assert ev.to_byte_array() and ev.CLASS_ID == 66
    finally:
        dbmod._db, hcs._db = old_db, old_hcs


def test_practice_ability_chain_window_reaches_the_client(db):
    """An ability that leaves a chain item must offer the client its resolve
    window.

    This projection raised ``NameError: name 'pl_t' is not defined`` in live
    play, so the client never received the chain-only options and the
    ResolveTopOfChain green light.  The chain item then stayed pending (the
    client refuses any Basic Action while the chain is not empty), which is
    what made a hand like Scheme unplayable after a resource was played.
    """
    import hconnect_server as hcs
    from types import SimpleNamespace

    h = object.__new__(hcs.HCPHandler)
    h.client_reck_id = 5
    pushed = {}
    h._push_phase_options_empty = lambda session, pl, ai: pushed.update(
        options=(pl, ai))
    h._fresh_game = lambda session, pl, ai, state: game_engine.Game(
        1, pl, ai)
    sent = []
    h._send_battle_events = lambda session, game, pl: sent.append((game, pl))
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(1, pl_t, ai_t)
    port = SimpleNamespace(
        current_turn_phase=game_engine.ETurnPhases.SecondMainPhase,
        active_player_id=pl_t,
        action_stack=SimpleNamespace(priority_player_id=pl_t))
    session = SessionStub()

    assert h._push_practice_ability_chain_window(session, port, game) is True
    assert pushed.get("options") == (game.player_uid, game.ai_uid), pushed
    assert len(sent) == 1, sent
    chain_game, recipient = sent[0]
    lights = [event for event in chain_game.events
              if event.__class__.__name__ == "GreenLightSessionEventArgs"]
    assert lights and lights[-1].context == \
        game_engine.EPriorityContext.ResolveTopOfChain, lights
    assert str(lights[-1].player_id) == str(pl_t), lights[-1].player_id
    phases = [event for event in chain_game.events
              if event.__class__.__name__ == "TurnPhaseUpdatedSessionEventArgs"]
    assert phases, chain_game.events

    # The AI holding priority is not a client window.
    port.action_stack.priority_player_id = ai_t
    pushed.clear()
    sent.clear()
    assert h._push_practice_ability_chain_window(session, port, game) is False
    assert not pushed and not sent, (pushed, sent)

    # The original defect only existed at runtime inside a nested closure, so
    # also assert the whole dispatch closure has no unbound global name.
    import builtins
    import dis
    import types
    codes = []

    def walk(code):
        codes.append(code)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                walk(const)

    walk(hcs.HCPHandler._dispatch_rules_port_transaction_locked.__code__)
    missing = set()
    for code in codes:
        for instruction in dis.get_instructions(code):
            if (instruction.opname == "LOAD_GLOBAL" and
                    instruction.argval not in vars(hcs) and
                    not hasattr(builtins, instruction.argval)):
                missing.add(instruction.argval)
    assert not missing, missing
    # ...and that the check actually detects such a name.
    def _probe():
        def _inner():
            return undefined_practice_identity + 1
        return _inner
    assert "undefined_practice_identity" in {
        instruction.argval
        for code in (_probe.__code__, _probe().__code__)
        for instruction in dis.get_instructions(code)
        if (instruction.opname == "LOAD_GLOBAL" and
            instruction.argval not in vars(hcs) and
            not hasattr(builtins, instruction.argval))}


def test_champion_power_offer_requires_its_authored_cost_and_target(db):
    """Bunoshi's charge power is offered only when it can actually be paid.

    The power costs three charges AND "sacrifice a troop you control", and it
    needs "another target troop".  The offer gate used to check only charges
    and thresholds, so the champion card lit up with nothing to sacrifice.
    Underground troops never satisfy that payment: the authored cost template
    is Warzone-only.
    """
    import hconnect_server as hcs
    import db as dbmod

    ability_guid = "eac96648-be59-4f36-0ba3-59117efc8138"
    tid_sacrifice = "38e37324-0d8f-69ec-60f1-f4695087e5c4"
    src = sqlite3.connect(SRC)
    try:
        for tid in (tid_sacrifice, "8431ab14-20d5-cb04-bd4b-664c698dbd42"):
            row = src.execute(
                "SELECT template_id, game_text, is_auto_target, "
                "is_random_target, optional, explicit, player_filter, "
                "collection_flags, min_target_count, max_target_count, "
                "filter_json, target_kind FROM target_templates "
                "WHERE template_id=?", (tid,)).fetchone()
            assert row, tid
            db.execute("INSERT INTO target_templates VALUES "
                       "(?,?,?,?,?,?,?,?,?,?,?,?)", row)
    finally:
        src.close()
    db.execute("CREATE TABLE talent_abilities (ability_guid TEXT, "
               "charge_cost INTEGER, spell_cost INTEGER, "
               "activatable_phases INTEGER, casting_behavior INTEGER)")
    db.execute("CREATE TABLE champion_abilities (ability_guid TEXT, "
               "charge_cost INTEGER, spell_cost INTEGER, "
               "casting_behavior INTEGER, thresholds_json TEXT)")
    db.execute("INSERT INTO champion_abilities VALUES (?,?,?,?,?)",
               (ability_guid, 3, 0, game_engine.ECardTypes.BasicAction,
                json.dumps([{"color": "Blood", "quantity": 1}])))
    add_card(db, 501, 5, TPL_GLADIATOR, loc="underground")
    add_card(db, 601, 0, TPL_GLADIATOR, loc="warzone")
    db.commit()
    old_db, old_hcs_db = dbmod._db, hcs._db
    dbmod._db, hcs._db = db, db
    try:
        h = object.__new__(hcs.HCPHandler)
        h.user_profile = {"id": 5}
        h._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        h._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))
        rid = game_engine.ResourceId.from_str(ability_guid)
        champ_uid = int(h._player_champ_scid.uid.uid64)
        session = SessionStub()
        bstate = {
            "player_charges": 3,
            "player_spell_points": 0,
            "player_threshold": {game_engine.SHARD_TO_FLAG["blood"]: 1},
            "turn_player": "player",
            "stack": [],
        }

        def offer():
            targets = h._champion_ability_targets(session, [rid], champ_uid)
            costs = h._champion_ability_costs(session, [rid], champ_uid)
            affordable = h._filter_affordable_abilities(
                [rid], bstate, game_engine.ETurnPhases.FirstMainPhase,
                target_data=targets, cost_data=costs)
            return targets, costs, affordable

        # Only an underground troop is available for the sacrifice, so the
        # power must not be offered even though the charges and threshold are
        # both there and the buff target exists.
        targets, costs, affordable = offer()
        sacrifice = costs[ability_guid][0]
        assert sacrifice[0] == tid_sacrifice, sacrifice
        assert tuple(sacrifice[2]) == (), sacrifice
        assert targets[ability_guid], targets
        assert affordable == [], affordable

        # A warzone troop can be sacrificed: the power is offered again.
        db.execute("UPDATE game_cards SET location='warzone' WHERE card_uid=501")
        db.commit()
        targets, costs, affordable = offer()
        assert set(costs[ability_guid][0][2]) == {501}, costs
        assert [str(a.guid) for a in affordable] == [ability_guid], affordable

        # With no troop anywhere the explicit buff target has no legal card
        # either, so the power stays unoffered.
        db.execute("UPDATE game_cards SET location='discard' "
                   "WHERE card_uid IN (501, 601)")
        db.commit()
        _targets, _costs, affordable = offer()
        assert affordable == [], affordable
    finally:
        dbmod._db, hcs._db = old_db, old_hcs_db


def test_bunjitsu_voided_stats_sum_both_troops(db):
    """Bun'jitsu's Abomination buff is "+[ATK] equal to the VOIDED TROOPS'
    [ATK] plus 3": with two voided 2/1 troops the remembered stats must be
    4/2 (sum), so the token becomes 7/5, not the first troop's 2/1 + 3 = 5/4."""
    import hconnect_server as hcs
    import db as dbmod
    from tests.tests_cards_fixes import _copy_ability
    _copy_ability(db, AG_BUNJITSU)
    add_card(db, 101, 5, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd")  # 2/2
    db.execute(
        "UPDATE game_cards SET card_defense_mod=-1 "
        "WHERE card_uid=101")
    add_card(db, 102, 5, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd")  # 2/2
    db.execute(
        "UPDATE game_cards SET card_defense_mod=-1 "
        "WHERE card_uid=102")
    db.commit()
    old_db, old_hcs = dbmod._db, hcs._db
    dbmod._db, hcs._db = db, db
    try:
        h = object.__new__(hcs.HCPHandler)
        h._db = db
        h.user_profile = {"id": 5}
        h._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        h._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))
        h._current_bstate = {"player_health": 20, "ai_health": 20}
        game = game_engine.Game(1, game_engine.UID.make(244, 5),
                                game_engine.UID.make(3, 1000))
        bstate = {"player_health": 20, "ai_health": 20}
        bstate["champion_void_uids"] = [101, 102]
        h._resolve_champion_void_targets(
            game, SessionStub(), game_engine.UID.make(244, 5),
            game_engine.UID.make(3, 1000), bstate, AG_BUNJITSU)
        stats = bstate.get("champion_voided_stats") or {}
        assert stats.get("atk") == 4 and stats.get("def") == 2, stats
    finally:
        dbmod._db, hcs._db = old_db, old_hcs


def test_lightning_armada_counts_only_your_hand(db):
    """Lightning Armada's "+2/+2 for each card in your hand" must count ONLY
    the controller's hand — IsControlledBy was a tautology in the statics
    layer, so it summed both players' hands (22/22 instead of 2 + 2N)."""
    import db as dbmod
    from tests.tests_cards_fixes import _copy_ability
    from abilities.framework.statics import _variable_value
    _copy_ability(db, TID_LIGHTNING)
    for uid in (101, 102, 103):
        add_card(db, uid, 5, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd",
                 loc="hand")  # player's hand: 3 cards
    for uid in (201, 202, 203, 204):
        add_card(db, uid, 0, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd",
                 loc="hand")  # AI's hand: 4 cards
    db.commit()
    old_db = dbmod._db
    dbmod._db = db
    try:
        raw = db.execute(
            "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
            (TID_LIGHTNING,)).fetchone()[0]
        n = _variable_value(db, 1, {"player_health": 20, "ai_health": 20},
                            raw, "CardInYourHand", 5, 999)
        assert n == 3, f"player hand count should be 3, got {n}"
    finally:
        dbmod._db = old_db


def test_summon_zero_count_does_not_crash(db):
    """SummonToken with a count that resolves to 0 must not crash on an
    unbound `cname` (Xarlox the Brood Lord's trigger) — the return string
    uses a safe default name when no token was created."""
    from abilities.framework.bom import _leaf_summon
    import db as dbmod
    from tests.tests_cards_fixes import _copy_card
    _copy_card(db, "a9ebe40e-ef30-4c9e-b4dd-1b414dc35d0c")  # Spiderspawn
    db.execute(
        "INSERT INTO card_abilities_meta (ability_guid, is_triggered, "
        "trigger_event_type, game_text, raw_json, casting_behavior, is_manual, "
        "activation_cost, uses_per_game, uses_per_turn, target_template_ids, "
        "exhausts_on_use) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("00000000-0000-0000-0000-0000000000aa", 0, "", "Summon a token.",
         json.dumps({"m_Variables": [
             {"m_Name": "amount", "m_DefaultValue": 0,
              "_t": "Game.Shared.Mechanics.Abilities.AbilityConstant"}]}),
         64, 0, 0, 0, 0, "[]", 0))
    db.commit()
    old_db = dbmod._db
    dbmod._db = db
    try:
        bstate = {"player_health": 20, "ai_health": 20,
                  "resolving_ability": "00000000-0000-0000-0000-0000000000aa"}
        class _H:
            user_profile = {"id": 5}
        out = _leaf_summon(
            game_engine.Game(1, game_engine.UID.make(244, 5),
                             game_engine.UID.make(3, 1000)),
            SessionStub(), db, _H(), game_engine.UID.make(244, 5),
            game_engine.UID.make(3, 1000), bstate,
            "x", '{"token_guid": "a9ebe40e-ef30-4c9e-b4dd-1b414dc35d0c", '
                 '"amount_variable": "amount"}')
        assert isinstance(out, str), out
        assert "0x" not in out or "summon" in out, out
    finally:
        dbmod._db = old_db


def test_worker_bot_creation_replacement_is_authored(db):
    """Reese's Surface grant replaces a created Worker Bot with a random Robot.

    The native token boundary created the Worker Bot itself (the live client
    showed a Worker Bot in play while the card carried the replacement
    IntAttr), because only the legacy BOM summon consulted the authored
    replacement.  The attribute and the replaced template both come from the
    card's Records graph, so the native summon must honour them too.
    """
    from unittest import mock
    from rules_port.context import EffectContext
    from rules_port.token_effects import summon_token

    worker_bot = "ce57cae9-c573-4098-97a6-8637711aef26"
    robot = "02051dbf-43d5-4b51-a36f-0f7af91a3298"   # Mimeobot, subtype Robot
    marker = "CreateRandomRobotInsteadOfWorkerBotIfThisIsInPlay"
    _copy_card(db, TPL_REESE)
    _copy_card(db, worker_bot)
    _copy_card(db, robot)
    add_card(db, 101, 5, TPL_REESE)
    db.execute(
        "UPDATE game_cards SET card_abilities=?, permanent_buffs=? "
        "WHERE card_uid=101",
        (json.dumps([AG_REESE_REPLACEMENT]),
         json.dumps({"int_attrs": {marker: 1}})))
    db.commit()

    def summon(token_guid):
        before = {int(row[0]) for row in db.execute(
            "SELECT card_uid FROM game_cards")}
        pl_t, ai_t = _pl_ai()
        game = game_engine.Game(1, pl_t, ai_t)
        bstate = {"player_health": 20, "ai_health": 20,
                  "resolving_owner_id": 5, "resolving_source_uid": 101}
        context = EffectContext.from_rules_port(
            game, SessionStub(), db, HandlerStub(db), pl_t, ai_t, bstate,
            "effect", ability=None)
        with mock.patch("rules_port.token_effects.random.choice",
                        return_value=robot):
            summon_token(context, {"token_guid": token_guid, "amount": 1,
                                   "collection": "Warzone"})
        return [row[1] for row in db.execute(
            "SELECT card_uid, template_guid FROM game_cards")
            if int(row[0]) not in before]

    assert summon(worker_bot) == [robot]
    # Only the authored Worker Bot is replaced; another token is untouched.
    assert summon(TPL_GLADIATOR) == [TPL_GLADIATOR]
    # Without the active grant the Worker Bot is created as printed.
    db.execute("UPDATE game_cards SET permanent_buffs='{}' WHERE card_uid=101")
    db.commit()
    assert summon(worker_bot) == [worker_bot]


def test_incubate_puts_eggs_in_opposing_deck(db):
    """Incubate's opposing-champion target controls the generated eggs.

    The AI casts Incubate, so the three Spiderling Eggs must be inserted into
    the player's deck, not the AI's deck.  The previous token leaf always used
    resolving_owner_id (the caster) for deck-bound tokens.
    """
    from abilities.framework.triggers import resolve_stack_trigger
    from tests.tests_cards_fixes import _copy_card

    _copy_card(db, TPL_INCUBATE)
    _copy_card(db, TPL_SPIDERLING_EGG)
    add_card(db, 101, 0, TPL_INCUBATE, loc="CastSpells")
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
        (json.dumps(["edc225ee-3d7c-74e5-6fe1-01a85c974dcf"]),))
    # Give both sides a few existing deck cards so insertion is tested against
    # real deck owners rather than an empty-deck edge case.
    add_card(db, 201, 5, TPL_INCUBATE, loc="deck")
    add_card(db, 202, 0, TPL_INCUBATE, loc="deck")
    db.commit()
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    resolve_stack_trigger(
        handler, game, SessionStub(), db, pl_t, ai_t, bstate,
        {"kind": "spell", "ability_guid":
         "edc225ee-3d7c-74e5-6fe1-01a85c974dcf", "source_uid": 101})
    eggs = db.execute(
        "SELECT user_id, location, COUNT(*) FROM game_cards "
        "WHERE template_guid=? GROUP BY user_id, location",
        (TPL_SPIDERLING_EGG,)).fetchall()
    assert eggs == [(5, "deck", 3)], eggs


def test_ai_incubate_uses_play_card_ability_on_chain(db):
    """AI card plays must use the client's built-in Play Card ability ID.

    UIBattle ignores AbilityPushedOnChain when its AbilityTemplateId is a card
    template GUID, which made an opposing Incubate resolve from an apparently
    empty chain even though the server had moved it to CastSpells.
    """
    import ai as ai_mod
    from ai_eval import CardInfo
    from domain.events import AbilityPushedOnChainSessionEventArgs

    _copy_card(db, TPL_INCUBATE)
    add_card(db, 101, 0, TPL_INCUBATE, loc="hand")
    db.execute(
        "UPDATE game_cards SET card_type='BasicAction' WHERE card_uid=101")
    db.commit()
    card = CardInfo((
        101, TPL_INCUBATE, "hand", "BasicAction", "Incubate", "Common",
        1, 0, 0, "[]",
        json.dumps(["edc225ee-3d7c-74e5-6fe1-01a85c974dcf"]),
        0, "", 0, 0, 0, 0, 0, "{}", "{}", 0))
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    # The first chain id is not necessarily 1: setup triggers and previous
    # actions consume ids.  The client must receive the same id as the stack
    # item so its chain view can remove the item when it resolves.
    bstate = {"ai_resources": 1, "ai_threshold": {}, "turn_number": 1,
              "_next_instance_id": 9}
    old_ai_db = ai_mod._db
    ai_mod._db = db
    try:
        ai_mod.ai_play_hand_card(
            handler, game, SessionStub(), ai_t, bstate, card)
    finally:
        ai_mod._db = old_ai_db
    chain_events = [
        ev for ev in game.events
        if isinstance(ev, AbilityPushedOnChainSessionEventArgs)]
    assert len(chain_events) == 1, chain_events
    assert chain_events[0].ability_instance_id == 9, chain_events[0]
    assert bstate["stack"][-1]["instance_id"] == 9, bstate["stack"]
    assert (str(chain_events[0].ability_template_id.guid) ==
            game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID), chain_events[0]


def test_spiderling_egg_summons_under_random_opponent(db):
    """A Spiderling Egg's Bane trigger uses the selected champion's controller.

    The player drew the Egg, so the trigger source is player-owned, but its
    metadata target is a random opposing champion.  The Spiderling must
    therefore enter the AI's warzone rather than the player's.
    """
    from abilities.framework.triggers import resolve_stack_trigger

    _copy_card(db, TPL_SPIDERLING_EGG)
    _copy_card(db, TPL_INCUBATE)
    _copy_card(db, "ca5c02c6-023e-42b6-b02a-12724dfd6920")
    add_card(db, 301, 5, TPL_SPIDERLING_EGG, loc="hand")
    add_card(db, 302, 5, TPL_INCUBATE, loc="deck")
    db.commit()
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}

    result = resolve_stack_trigger(
        handler, game, SessionStub(), db, pl_t, ai_t, bstate,
        {"kind": "trigger",
         "ability_guid": "9c2e45ce-4ec3-90b5-6165-fa742e50dc95",
         "source_uid": 301, "target_uid": 301})

    spiders = db.execute(
        "SELECT user_id, location, COUNT(*) FROM game_cards "
        "WHERE template_guid=? GROUP BY user_id, location",
        ("ca5c02c6-023e-42b6-b02a-12724dfd6920",)).fetchall()
    assert spiders == [(0, "warzone", 1)], (result, spiders, bstate)
    zones = dict(db.execute(
        "SELECT card_uid, location FROM game_cards WHERE card_uid IN (301,302)"
    ).fetchall())
    assert zones == {301: "void", 302: "hand"}, (result, zones, bstate)


def test_spiderling_egg_bane_copies_discard_destination(db):
    """A Bane entering the crypt moves the top deck card to that crypt."""
    from abilities.framework.triggers import resolve_stack_trigger

    _copy_card(db, TPL_SPIDERLING_EGG)
    _copy_card(db, TPL_INCUBATE)
    _copy_card(db, "ca5c02c6-023e-42b6-b02a-12724dfd6920")
    add_card(db, 401, 5, TPL_SPIDERLING_EGG, loc="discard")
    add_card(db, 402, 5, TPL_INCUBATE, loc="deck")
    db.commit()
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}

    resolve_stack_trigger(
        handler, game, SessionStub(), db, pl_t, ai_t, bstate,
        {"kind": "trigger",
         "ability_guid": "9c2e45ce-4ec3-90b5-6165-fa742e50dc95",
         "source_uid": 401, "target_uid": 401})

    zones = dict(db.execute(
        "SELECT card_uid, location FROM game_cards WHERE card_uid IN (401,402)"
    ).fetchall())
    assert zones == {401: "void", 402: "discard"}, (zones, bstate)


def test_state_based_death_includes_static_defense(db):
    """High Tomb Lord ("+1/+1 for each card in all crypts") at 9/9 that took 4
    combat damage must NOT die to the state check — the continuous static
    defense (not stored in permanent/temporary buffs) counts toward survival."""
    from abilities.framework.kill_troop import state_based_deaths
    from tests.tests_cards_fixes import _copy_card, _copy_ability
    _copy_card(db, TPL_TOMB_LORD)
    for i, uid in enumerate(range(501, 510)):
        add_card(db, uid, 5 if i < 5 else 0,
                 "14909185-1070-48df-9508-61d5a9650bd2", loc="discard")
    add_card(db, 101, 5, TPL_TOMB_LORD)
    db.execute("UPDATE game_cards SET card_damage=4 WHERE card_uid=101")
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
        (json.dumps(["6ac287a1-da4a-0d14-5ff0-de0329393fbb"]),))
    db.commit()
    pl_t, ai_t = _pl_ai()
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20}
    state_based_deaths(game_engine.Game(1, pl_t, ai_t), SessionStub(), db,
                       handler, pl_t, ai_t, bstate)
    loc = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=101").fetchone()[0]
    assert loc == "warzone", loc  # 9 def - 4 damage = 5, survives


def test_troop_artifact_can_attack(db):
    """Infiltrator Bot is a Troop|Artifact — the attack-eligibility checks
    used exact card_type='Troop' and silently excluded it from attacking."""
    from ai import player_can_attack_troops
    import ai as ai_mod
    import db as dbmod
    from tests.tests_cards_fixes import _copy_card, _copy_ability
    _copy_card(db, TPL_INFILTRATOR)
    add_card(db, 101, 5, TPL_INFILTRATOR,
             state=game_engine.ECardStates.StartedATurnOnYourSide)
    db.commit()
    old_db, old_ai = dbmod._db, ai_mod._db
    dbmod._db, ai_mod._db = db, db
    try:
        handler = HandlerStub(db)
        assert player_can_attack_troops(handler, SessionStub(), user_id=5)
    finally:
        dbmod._db, ai_mod._db = old_db, old_ai


def test_unblockable_attacker_cannot_be_blocked(db):
    """Infiltrator Bot's activated "Unblockable" (CantBeBlocked) must stop
    blockers — can_block previously ignored the attribute."""
    from abilities.framework.statics import can_block
    from tests.tests_cards_fixes import _copy_card, _copy_ability
    _copy_card(db, TPL_INFILTRATOR)
    _copy_card(db, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd")
    add_card(db, 101, 5, TPL_INFILTRATOR,
             state=game_engine.ECardStates.StartedATurnOnYourSide)
    add_card(db, 102, 5, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd")
    db.execute(
        "UPDATE game_cards SET temporary_attributes=? WHERE card_uid=101",
        (game_engine.ECardAttributes.CantBeBlocked,))
    db.commit()
    bstate = {"player_health": 20, "ai_health": 20}
    assert not can_block(db, 1, bstate, 101, 102)
    # Without the attribute, the same blocker may block.
    db.execute("UPDATE game_cards SET temporary_attributes=0 WHERE card_uid=101")
    db.commit()
    assert can_block(db, 1, bstate, 101, 102)


def test_void_leaf_publishes_the_voided_troops_stats(db):
    """The typed void leaf records the voided card for follow-up operands.

    Mentor of the Grave's charge power ("Void target troop in a crypt. Then,
    gain health equal to the voided troop's [DEF]") reads the voided card back
    through the ability's ``VoidedCards`` list attr
    (``SumVariableInListAttrCardsAbilityVariable``).  Nothing recorded it, so
    the follow-up operand resolved to 0 and the power healed nothing even
    though the troop was voided.
    """
    from tests.tests_cards_fixes import _copy_card, _copy_ability
    from rules_port.resolution import resolve_port_ability
    charge_power = "8cc3e276-04c2-2da9-57e1-38a71d6b1d09"
    hopper = "fe2472ed-4ff8-455b-8b18-b7e0033cd896"      # Battle Hopper 0/1
    # The focused fixture predates the combat columns the static card reader
    # needs (same workaround as the Lethal test in tests_combat).
    db.execute("ALTER TABLE card_templates ADD COLUMN lethal INTEGER DEFAULT 0")
    _copy_card(db, hopper)
    _copy_ability(db, charge_power)
    add_card(db, 901, 5, hopper, loc="discard")          # a troop in a crypt
    db.commit()
    pl_t, ai_t = _pl_ai()
    handler = HandlerStub(db)
    champion = game_engine.SessionCardId(game_engine.UID.make(3, 7777))
    handler._ai_champ_scid = champion
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"player_health": 20, "ai_health": 15, "turn_number": 3,
              "stack": [], "resolving_owner_id": 0}
    resolve_port_ability(
        handler, game, SessionStub(), db, pl_t, ai_t, bstate, charge_power,
        source_uid=int(champion.uid.uid64), owner_id=0, target_map={0: 901})
    location = db.execute(
        "SELECT location FROM game_cards WHERE card_uid=901").fetchone()[0]
    assert location == "void", location
    # The heal equals the voided troop's defense (0/1 -> +1), and the list attr
    # does not leak into the next ability resolution.
    assert bstate["ai_health"] == 16, bstate
    assert not (bstate.get("ability_lists") or {}).get("VoidedCards")


def test_human_block_dispatches_the_attackers_blocked_trigger(db):
    """A declared blocker queues CardBlockedEvent with the attacker as TARGET.

    ``Session.EmitBlockerEvents`` names the blocker as the event source and the
    blocked attacker as its target, so the attacker's "When this becomes
    blocked" ability matches (Nameless Citizen buries the top four cards of
    each opposing champion's deck).  Only the AI's own blocking dispatched
    these events, so a human block never fired the attacker's abilities.
    """
    import hconnect_server as hcs
    import db as dbmod
    from tests.tests_cards_fixes import _copy_card
    tpl_citizen = "a0ed3464-ea1e-4f79-b206-d2675a965ceb"
    ag_citizen = "6c48c325-2fa0-11e3-d47e-c229d2b35c72"
    tpl_hopper = "fe2472ed-4ff8-455b-8b18-b7e0033cd896"
    _copy_card(db, tpl_citizen)
    _copy_card(db, tpl_hopper)
    # The AI's Nameless Citizen attacks; the human blocks it with a troop.
    add_card(db, 701, 0, tpl_citizen, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=701",
               (json.dumps([ag_citizen]),))
    add_card(db, 801, 5, tpl_hopper, loc="warzone")
    for index, uid in enumerate(range(900, 905)):
        add_card(db, uid, 5, tpl_hopper, loc="deck")
        db.execute("UPDATE game_cards SET position=? WHERE card_uid=?",
                   (index, uid))
    db.commit()
    pl_t, ai_t = _pl_ai()
    session = SessionStub()
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"player_health": 20, "ai_health": 15, "turn_number": 3,
              "stack": []}
    old_db, old_hcs = dbmod._db, hcs._db
    dbmod._db, hcs._db = db, db
    try:
        handler = object.__new__(hcs.HCPHandler)
        handler._db = db
        handler.user_profile = {"id": 5}
        handler._player_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(244, 5))
        handler._ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID.make(3, 1000))
        handler._current_bstate = bstate
        handler._dispatch_blocker_events(game, session, bstate, [(701, [801])])
        items = bstate.get("stack") or []
        assert items, "the blocked attacker's trigger must go on the chain"
        assert items[0]["ability_guid"] == ag_citizen
        assert int(items[0]["target_uid"]) == 701
        from rules_port.resolution import resolve_port_trigger
        for item in list(items):
            bstate["stack"].remove(item)
            resolve_port_trigger(handler, game, session, db, pl_t, ai_t,
                                 bstate, item)
        buried = [row[0] for row in db.execute(
            "SELECT card_uid FROM game_cards WHERE user_id=5 "
            "AND location='discard' ORDER BY card_uid").fetchall()]
        assert buried == [900, 901, 902, 903], buried
    finally:
        dbmod._db, hcs._db = old_db, old_hcs


def test_friendly_zone_trigger_ignores_cards_taken_from_an_opponent(db):
    """``TriggerCardEnteredZone``'s "your" flag needs the card's current AND
    previous controller to be the ability source's controller.

    Checking only the current controller let an opposing champion react to a
    card that came from the other player's zone: the AI's Mentor of the Grave
    ("when a troop enters your hand from your crypt, it gets +1[ATK]/+1[DEF]")
    buffed a troop its Call the Grave pulled out of the human's crypt, because
    that move had already transferred control.
    """
    from rules_port.triggers import dispatch_native_trigger

    tpl_hopper = "fe2472ed-4ff8-455b-8b18-b7e0033cd896"
    ag_mentor = "3776cad6-1f13-9068-6124-bf5c0152f181"
    from tests.tests_cards_fixes import _copy_card
    _copy_card(db, tpl_hopper)
    pl_t, ai_t = _pl_ai()
    session = SessionStub()
    handler = HandlerStub(db)
    ai_champ = game_engine.SessionCardId(game_engine.UID.make(3, 7777))
    player_champ = game_engine.SessionCardId(game_engine.UID.make(244, 5))
    handler._player_champ_scid = player_champ
    handler._ai_champ_scid = ai_champ
    handler._champion_targets = lambda: [
        (int(player_champ.uid.uid64), 5, "Player", 20),
        (int(ai_champ.uid.uid64), 0, "AI", 15)]
    handler._ai_champ_ability_guids = [ag_mentor]
    handler._player_champ_abilities = []

    def chain_for(card_owner, previous_owner):
        game = game_engine.Game(1, pl_t, ai_t)
        bstate = {"player_health": 20, "ai_health": 15, "turn_number": 3,
                  "stack": []}
        uid = 7000 + card_owner
        add_card(db, uid, card_owner, tpl_hopper, loc="hand")
        db.commit()
        dispatch_native_trigger(
            db=db, handler=handler, game=game, session=session,
            player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
            event_type="CardEnteredZoneEvent", source_card_id=uid,
            source_player_id=card_owner,
            data={"event_source_collection": "discard",
                  "event_destination_collection": "hand",
                  "event_previous_owner_id": previous_owner})
        return [item.get("ability_guid")
                for item in bstate.get("stack") or []]

    # The champion's own crypt entry still lights up ...
    assert chain_for(0, 0) == [ag_mentor]
    # ... a troop taken from the human's crypt does not ...
    assert chain_for(0, 5) == []
    # ... and neither does the human's own crypt entry.
    assert chain_for(5, 5) == []


def test_incantation_of_fear_counter_on_opposing_crypt_entry(db):
    """Incantation of Fear: "When a card enters an opposing crypt, add an
    incantation counter to this."  The server never fired CardEnteredZoneEvent
    for cards entering the discard — the trigger must now fire and add the
    counter to the player's Incantation."""
    from abilities.framework.triggers import (
        resolve_triggers, resolve_stack_trigger)
    from tests.tests_cards_fixes import _copy_card
    _copy_card(db, TPL_INCANT_FEAR)
    add_card(db, 101, 5, TPL_INCANT_FEAR)  # player's Incantation in warzone
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
        (json.dumps([AG_INCANT_FEAR]),))
    add_card(db, 202, 0, "b7172b6a-ef85-4fef-91e1-81975b4ce7cd",
             loc="discard")  # AI card already in the crypt
    db.commit()
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    resolve_triggers(db, handler, game, SessionStub(), pl_t, ai_t, bstate,
                     "CardEnteredZoneEvent", 202, 0)
    items = bstate.get("stack") or []
    assert items, "Incantation of Fear trigger should fire on opposing crypt entry"
    for item in list(items):
        bstate["stack"].remove(item)
        resolve_stack_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                              bstate, item)
    buffs = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE card_uid=101"
    ).fetchone()[0]
    counters = (json.loads(buffs or "{}").get("counters") or {})
    assert counters.get("incantation", 0) >= 1, counters


def test_pvp_champion_trigger_discovery_uses_raw_participant_id(db):
    """PvP champion triggers must resolve from the raw participant id.

    ``champion_holders`` compared the PvP owner against the local
    ``user_profile["id"]``, so Corinth's end-of-turn ability (daf1ed04) was
    never discovered and ``Shifted Paradigm`` never fired.  In PvP the owner
    is the raw participant id, matching the C# ``EndPhaseState.OnEntry``
    champion source.
    """
    from rules_port.trigger_discovery import RecordsTriggerDiscovery
    champion_guid = "93d8a5ca-d999-461d-84d8-30975ef4dfc1"
    ability_guid = "daf1ed04-6035-b4dd-a11b-48f93e4bfdb2"
    db.execute(
        "CREATE TABLE IF NOT EXISTS champion_abilities ("
        "champion_guid TEXT, champion_name TEXT, ability_guid TEXT, "
        "ability_name TEXT DEFAULT '', charge_cost INTEGER DEFAULT 0, "
        "spell_cost INTEGER DEFAULT 0, threshold_colors TEXT DEFAULT '', "
        "game_text TEXT DEFAULT '', casting_behavior INTEGER DEFAULT 0, "
        "thresholds_json TEXT DEFAULT '[]', "
        "target_template_ids TEXT DEFAULT '[]')")
    db.execute(
        "INSERT INTO champion_abilities (champion_guid, champion_name, "
        "ability_guid) VALUES (?,?,?)",
        (champion_guid, "Corinth the Iconoclast", ability_guid))
    db.execute(
        "INSERT INTO card_abilities_meta (ability_guid, trigger_event_type) "
        "VALUES (?,?)",
        (ability_guid, "Game.Shared.Mechanics.TurnEndedEvent"))
    add_card(db, 9001, 1001, champion_guid, loc="warzone")
    db.execute("UPDATE game_cards SET is_champion=1 WHERE card_uid=9001")
    db.commit()
    session = SessionStub()
    handler = HandlerStub(db)
    # The local DB id deliberately differs from the raw participant id.
    handler.user_profile = {"id": 999999}
    bstate = {"pvp": True, "pids": [1001, 1002],
              "champ_map": {"1001": 9001, "1002": 9002}}
    candidates = RecordsTriggerDiscovery(
        db, handler, session, game_engine.UID.make(244, 1001),
        game_engine.UID.make(244, 1002), bstate).discover(
            "TurnEndedEvent", 9001, 1001)
    found = {int(c.source_uid): list(c.ability_guids) for c in candidates}
    assert ability_guid in found.get(9001, []), found


def test_pvp_champion_trigger_condition_uses_raw_participant_owner(db):
    """The champion trigger condition must see the raw PvP owner.

    ``handler._champion_targets`` reports the local ``user_profile["id"]`` for
    the player's champion and ``0`` for the AI.  ``ConditionContext.card``
    handed that compatibility identity to
    ``TriggerPlayerControlsAbilitySource``, which compares it against the raw
    PvP participant id, so the condition failed and Corinth's ``Shifted
    Paradigm`` was dropped even though discovery found it.
    """
    from rules_port.triggers import dispatch_native_trigger
    champion_guid = "93d8a5ca-d999-461d-84d8-30975ef4dfc1"
    ability_guid = "daf1ed04-6035-b4dd-a11b-48f93e4bfdb2"
    db.execute(
        "CREATE TABLE IF NOT EXISTS champion_abilities ("
        "champion_guid TEXT, champion_name TEXT, ability_guid TEXT, "
        "ability_name TEXT DEFAULT '', charge_cost INTEGER DEFAULT 0, "
        "spell_cost INTEGER DEFAULT 0, threshold_colors TEXT DEFAULT '', "
        "game_text TEXT DEFAULT '', casting_behavior INTEGER DEFAULT 0, "
        "thresholds_json TEXT DEFAULT '[]', "
        "target_template_ids TEXT DEFAULT '[]')")
    db.execute(
        "INSERT INTO champion_abilities (champion_guid, champion_name, "
        "ability_guid) VALUES (?,?,?)",
        (champion_guid, "Corinth the Iconoclast", ability_guid))
    db.execute(
        "INSERT INTO card_abilities_meta (ability_guid, trigger_event_type) "
        "VALUES (?,?)",
        (ability_guid, "Game.Shared.Mechanics.TurnEndedEvent"))
    add_card(db, 9001, 1001, champion_guid, loc="champion")
    db.execute("UPDATE game_cards SET is_champion=1 WHERE card_uid=9001")
    db.commit()

    class ChampionTargetHandler(HandlerStub):
        def _champion_targets(self):
            # The compatibility identity the real handler reports: local
            # profile id for the human, 0 for the AI — never the raw pid.
            return [(9001, 999999, "Player", 28), (9002, 0, "AI", 28)]

    handler = ChampionTargetHandler(db)
    handler.user_profile = {"id": 999999}
    bstate = {"pvp": True, "pids": [1001, 1002],
              "champ_map": {"1001": 9001, "1002": 9002},
              "stack": [], "turn_pid": 1001}
    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    result = dispatch_native_trigger(
        db=db, handler=handler, game=game,
        session=SessionStub(), player_uid=pl_t, ai_uid=ai_t,
        battle_state=bstate, event_type="TurnEndedEvent",
        source_card_id=9001, source_player_id=1001)
    assert "daf1ed04" in result, result
    assert any(item.get("ability_guid") == ability_guid
               for item in (bstate.get("stack") or [])), bstate
    # The trigger's source is a champion; it must NOT be re-projected as a
    # warzone card.  ``push_source`` passed the zone ("champion") as the
    # template GUID and ``card_collection_for_location`` defaulted unknown
    # zones to Warzone, moving Corinth onto the board client-side.
    warzone_champion = [
        ev for ev in game.events
        if isinstance(ev, game_engine.CardUpdatedSessionEventArgs)
        and ev.collection == game_engine.ECardCollections.Warzone
        and int(ev.session_card_id.uid.uid64) == 9001]
    assert not warzone_champion, warzone_champion


def test_shifted_paradigm_never_moves_champion_when_crypt_empty(db):
    """Shifted Paradigm moves hand/crypt, never the champion.

    Both MoveCardToZone effects use auto-targets ("your hand"/"your crypt").
    When the crypt is empty the auto-target resolved to no cards, the resolver
    substituted ``(None,)``, and ``move_card_to_zone`` fell back to
    ``resolving_source_uid`` — moving Corinth from the champion zone into the
    deck.  An empty auto-target must skip the effect.
    """
    from rules_port.triggers import dispatch_native_trigger
    from rules_port.resolution import resolve_port_trigger
    champion_guid = "93d8a5ca-d999-461d-84d8-30975ef4dfc1"
    ability_guid = "daf1ed04-6035-b4dd-a11b-48f93e4bfdb2"
    db.execute(
        "CREATE TABLE IF NOT EXISTS champion_abilities ("
        "champion_guid TEXT, champion_name TEXT, ability_guid TEXT, "
        "ability_name TEXT DEFAULT '', charge_cost INTEGER DEFAULT 0, "
        "spell_cost INTEGER DEFAULT 0, threshold_colors TEXT DEFAULT '', "
        "game_text TEXT DEFAULT '', casting_behavior INTEGER DEFAULT 0, "
        "thresholds_json TEXT DEFAULT '[]', "
        "target_template_ids TEXT DEFAULT '[]')")
    db.execute(
        "INSERT INTO champion_abilities (champion_guid, champion_name, "
        "ability_guid) VALUES (?,?,?)",
        (champion_guid, "Corinth the Iconoclast", ability_guid))
    db.execute(
        "INSERT INTO card_abilities_meta (ability_guid, trigger_event_type) "
        "VALUES (?,?)",
        (ability_guid, "Game.Shared.Mechanics.TurnEndedEvent"))
    add_card(db, 9001, 1001, champion_guid, loc="champion")
    db.execute("UPDATE game_cards SET is_champion=1 WHERE card_uid=9001")
    # A hand card to move, an intentionally EMPTY discard, and enough deck
    # cards that the trailing "draw four" has a deterministic count.
    add_card(db, 9003, 1001, TPL_GLADIATOR, loc="hand")
    for uid in range(9100, 9106):
        add_card(db, uid, 1001, TPL_GLADIATOR, loc="deck")
    db.commit()

    handler = HandlerStub(db)
    handler.user_profile = {"id": 1001}
    # The live HCPHandler supplies this deck-out projection; the focused
    # fixture only needs the draw to stop cleanly once the deck empties.
    handler._rules_port_deck_out = lambda *args, **kwargs: None
    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    bstate = {"pvp": True, "pids": [1001, 1002],
              "champ_map": {"1001": 9001, "1002": 9002},
              "stack": [], "turn_pid": 1001}
    game = game_engine.Game(1, pl_t, ai_t)
    dispatch_native_trigger(
        db=db, handler=handler, game=game, session=SessionStub(),
        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
        event_type="TurnEndedEvent", source_card_id=9001,
        source_player_id=1001)
    item = next(item for item in (bstate.get("stack") or [])
                if item.get("ability_guid") == ability_guid)
    resolve_port_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                         bstate, item)
    locations = dict(db.execute(
        "SELECT card_uid, location FROM game_cards").fetchall())
    assert locations[9001] == "champion", locations
    # The ability resolves: hand -> deck, (empty) crypt skipped, draw 4.
    moves = [(int(ev.session_card_id.uid.uid64), ev.collection)
             for ev in game.events
             if isinstance(ev, game_engine.CardMovedSessionEventArgs)]
    assert (9003, game_engine.ECardCollections.Deck) in moves, moves
    drawn = [ev for ev in game.events
             if isinstance(ev, game_engine.CardDrawnSessionEventArgs)]
    assert len(drawn) == 4, drawn


def test_corinth_end_of_turn_ability_resolves_inline(db):
    """Merry-Melee-Corinth resolves the end-of-turn ability without a chain.

    ``force_ignores_chain`` is the format rule: the ability must not be
    pushed onto the chain, so no priority window is created and the effects
    run immediately (hand shuffled into the deck, four drawn).
    """
    from rules_port.triggers import dispatch_native_trigger
    champion_guid = "93d8a5ca-d999-461d-84d8-30975ef4dfc1"
    ability_guid = "daf1ed04-6035-b4dd-a11b-48f93e4bfdb2"
    db.execute(
        "CREATE TABLE IF NOT EXISTS champion_abilities ("
        "champion_guid TEXT, champion_name TEXT, ability_guid TEXT, "
        "ability_name TEXT DEFAULT '', charge_cost INTEGER DEFAULT 0, "
        "spell_cost INTEGER DEFAULT 0, threshold_colors TEXT DEFAULT '', "
        "game_text TEXT DEFAULT '', casting_behavior INTEGER DEFAULT 0, "
        "thresholds_json TEXT DEFAULT '[]', "
        "target_template_ids TEXT DEFAULT '[]')")
    db.execute(
        "INSERT INTO champion_abilities (champion_guid, champion_name, "
        "ability_guid) VALUES (?,?,?)",
        (champion_guid, "Corinth the Iconoclast", ability_guid))
    db.execute(
        "INSERT INTO card_abilities_meta (ability_guid, trigger_event_type) "
        "VALUES (?,?)",
        (ability_guid, "Game.Shared.Mechanics.TurnEndedEvent"))
    add_card(db, 9001, 1001, champion_guid, loc="champion")
    db.execute("UPDATE game_cards SET is_champion=1 WHERE card_uid=9001")
    add_card(db, 9003, 1001, TPL_GLADIATOR, loc="hand")
    for uid in range(9100, 9106):
        add_card(db, uid, 1001, TPL_GLADIATOR, loc="deck")
    db.commit()

    class ChampionTargetHandler(HandlerStub):
        def _champion_targets(self):
            return [(9001, 1001, "Player", 28), (9002, 0, "AI", 28)]

    handler = ChampionTargetHandler(db)
    handler.user_profile = {"id": 1001}
    handler._rules_port_deck_out = lambda *args, **kwargs: None
    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    bstate = {"pvp": True, "pids": [1001, 1002],
              "champ_map": {"1001": 9001, "1002": 9002},
              "stack": [], "turn_pid": 1001}
    game = game_engine.Game(1, pl_t, ai_t)
    # Force the shuffled slot past the top of the deck so the following draw
    # cannot return the hand card.  Without the shuffle it lands at position 0
    # and the draw hands it straight back.
    from unittest import mock
    with mock.patch("random.randrange", return_value=5):
        result = dispatch_native_trigger(
            db=db, handler=handler, game=game, session=SessionStub(),
            player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
            event_type="TurnEndedEvent", source_card_id=9001,
            source_player_id=1001, force_ignores_chain=True)
    # Resolved inline: no chain item, and the hand already moved to the deck.
    assert not (bstate.get("stack") or []), bstate.get("stack")
    assert "daf1ed04" in result, result
    moves = [(int(ev.session_card_id.uid.uid64), ev.collection)
             for ev in game.events
             if isinstance(ev, game_engine.CardMovedSessionEventArgs)]
    assert (9003, game_engine.ECardCollections.Deck) in moves, moves
    # It was shuffled into the deck, not left on top where the draw would
    # immediately recover it.
    locations = dict(db.execute(
        "SELECT card_uid, location FROM game_cards").fetchall())
    assert locations[9003] == "deck", locations


def test_native_chain_resolves_trigger_without_legacy_fallback(db):
    """A chain-queued trigger must resolve through the native chain resolver.

    Regression: ``resolve_port_chain_item`` handled only ability/troop/spell
    descriptors and raised ``RuntimeError`` for ``kind='trigger'``.  A
    GameStarted/first-turn trigger queued by ``queue_projected_chain`` then
    aborted the mulligan-keep transaction, so PvE games failed to start.
    """
    from rules_port.triggers import dispatch_native_trigger
    champion_guid = "93d8a5ca-d999-461d-84d8-30975ef4dfc1"
    ability_guid = "daf1ed04-6035-b4dd-a11b-48f93e4bfdb2"
    db.execute(
        "CREATE TABLE IF NOT EXISTS champion_abilities ("
        "champion_guid TEXT, champion_name TEXT, ability_guid TEXT, "
        "ability_name TEXT DEFAULT '', charge_cost INTEGER DEFAULT 0, "
        "spell_cost INTEGER DEFAULT 0, threshold_colors TEXT DEFAULT '', "
        "game_text TEXT DEFAULT '', casting_behavior INTEGER DEFAULT 0, "
        "thresholds_json TEXT DEFAULT '[]', "
        "target_template_ids TEXT DEFAULT '[]')")
    db.execute(
        "INSERT INTO champion_abilities (champion_guid, champion_name, "
        "ability_guid) VALUES (?,?,?)",
        (champion_guid, "Corinth the Iconoclast", ability_guid))
    db.execute(
        "INSERT INTO card_abilities_meta (ability_guid, trigger_event_type) "
        "VALUES (?,?)",
        (ability_guid, "Game.Shared.Mechanics.TurnEndedEvent"))
    add_card(db, 9001, 1001, champion_guid, loc="champion")
    db.execute("UPDATE game_cards SET is_champion=1 WHERE card_uid=9001")
    add_card(db, 9003, 1001, TPL_GLADIATOR, loc="hand")
    for uid in range(9100, 9106):
        add_card(db, uid, 1001, TPL_GLADIATOR, loc="deck")
    db.commit()

    handler = HandlerStub(db)
    handler.user_profile = {"id": 1001}
    handler._rules_port_deck_out = lambda *args, **kwargs: None
    pl_t = game_engine.UID.make(244, 1001)
    ai_t = game_engine.UID.make(244, 1002)
    game = game_engine.Game(1, pl_t, ai_t)
    bstate = {"pvp": True, "pids": [1001, 1002],
              "champ_map": {"1001": 9001, "1002": 9002},
              "stack": [], "turn_pid": 1001}
    # Discovery queues the chain descriptor; the native chain resolver must
    # then handle kind='trigger' instead of refusing it.
    dispatch_native_trigger(
        db=db, handler=handler, game=game, session=SessionStub(),
        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
        event_type="TurnEndedEvent", source_card_id=9001,
        source_player_id=1001)
    item = next(item for item in (bstate.get("stack") or [])
                if item.get("ability_guid") == ability_guid)
    import hconnect_server as hcs
    import db as dbmod
    old_db, old_hcs = dbmod._db, hcs._db
    dbmod._db, hcs._db = db, db
    try:
        hcs.HCPHandler._resolve_native_trigger_chain_item(
            handler, SessionStub(), pl_t, ai_t, bstate, item, game)
    finally:
        dbmod._db, hcs._db = old_db, old_hcs
    moves = [(int(ev.session_card_id.uid.uid64), ev.collection)
             for ev in game.events
             if isinstance(ev, game_engine.CardMovedSessionEventArgs)]
    assert (9003, game_engine.ECardCollections.Deck) in moves, moves
    resolved = [e for e in game.events
                if isinstance(e, game_engine.TopOfChainResolvedSessionEventArgs)]
    removed = [e for e in game.events
               if isinstance(e, game_engine.RemovedTopOfChainSessionEventArgs)]
    assert resolved and removed, game.events


def test_triggered_chance_branch_stores_the_entering_troop(db):
    """Psychotic Anarchist's authored "25% chance" chain, faithful to C#.

    Records: RandomizeVariable(RandomNumber 1..100) -> StoreTargets of the
    trigger event's card (AbilityTriggerCardTargetTemplate) gated by
    ``RandomNumber <= 25`` -> ActivateAbility of a child whose only target is
    ``SourceStoredTargetTemplate``.

    The client keeps the trigger event on the ability instance, appends the
    parent instance into the invoked child, and disables (never redirects to
    the source) an effect whose authored list target enumerates nothing.  The
    entering troop therefore receives the modifiers on a successful roll and
    nothing at all on a failed one.
    """
    from types import SimpleNamespace
    from unittest import mock
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port import effects as effects_module
    from rules_port.resolution import resolve_port_trigger

    troop = 9200
    add_card(db, troop, 5, TPL_GLADIATOR)
    condition = DEFAULT_RECORD_STORE.get(
        "AbilityEffectConditionTemplate", COND_PSYCHOTIC_CHANCE)
    db.execute("INSERT INTO ability_effect_conditions VALUES (?,?,?)",
               (COND_PSYCHOTIC_CHANCE, condition.field("m_Name"),
                json.dumps(condition.field("m_Condition").to_dict())))
    db.commit()

    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    item = {"kind": "trigger", "ability_guid": AG_PSYCHOTIC_CHANCE,
            "source_uid": int(handler._player_champ_scid.uid.uid64),
            "target_uid": troop, "trigger_target_uid": troop,
            "source_owner_uid": 5, "instance_id": 1}
    bstate = {"pvp": False, "turn_pid": 5, "stack": [],
              "_rules_rng": SimpleNamespace(next=lambda span: 0)}
    seen = []
    real_dispatch = effects_module.dispatch

    def recording_dispatch(effect_type, context, effect=None):
        if effect_type == "CardModifierAbilityEffectTemplate":
            seen.append(context.resolved_target())
            return "recorded"
        return real_dispatch(effect_type, context, effect)

    with mock.patch.object(effects_module, "dispatch", recording_dispatch):
        resolve_port_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                             bstate, item)
    assert bstate["stored_targets"][AG_PSYCHOTIC_CHANCE] == [troop], bstate
    # Speed and +1[ATK] both land on the stored troop.
    assert seen == [troop, troop], seen

    # A failed roll skips the store; the invoked child then has no stored
    # target and must not fall back to the ability source (the champion).
    bstate["stored_targets"] = {}
    bstate["_rules_rng"] = SimpleNamespace(next=lambda span: 99)
    seen.clear()
    with mock.patch.object(effects_module, "dispatch", recording_dispatch):
        resolve_port_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                             bstate, item)
    assert not bstate["stored_targets"].get(AG_PSYCHOTIC_CHANCE), bstate
    assert seen == [], seen


def test_ai_start_of_turn_buries_each_champion_deck(db):
    """Ghastly Exchange (constant): "At the start of your turn, bury the top
    card of each champion's deck."  The authored target template is an
    auto-target for every champion, so an AI-owned copy must bury both
    champions' decks.  The AI-activation picker used to truncate the resolved
    pool to one card, so only the human's deck was buried.
    """
    from rules_port.triggers import dispatch_native_trigger
    filler = "14909185-1070-48df-9508-61d5a9650bd2"
    _copy_card(db, TPL_GHASTLY_EXCHANGE)
    add_card(db, 101, 0, TPL_GHASTLY_EXCHANGE, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
               (json.dumps([AG_GHASTLY_TURN_BURY]),))
    for uid in (301, 302):
        add_card(db, uid, 5, filler, loc="deck")   # human deck
    for uid in (401, 402):
        add_card(db, uid, 0, filler, loc="deck")   # AI deck
    db.commit()

    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 2}
    handler._current_bstate = bstate
    result = dispatch_native_trigger(
        db=db, handler=handler, game=game, session=SessionStub(),
        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
        event_type="TurnStartedEvent", source_card_id=None,
        source_player_id=0)
    assert AG_GHASTLY_TURN_BURY[:8] in result, result
    buried = db.execute(
        "SELECT user_id, COUNT(*) FROM game_cards WHERE session_id=1 "
        "AND location='discard' AND card_uid IN (301, 302, 401, 402) "
        "GROUP BY user_id").fetchall()
    assert sorted(buried) == [(0, 1), (5, 1)], buried


def test_one_shot_deathcry_consumes_and_keeps_its_deploy_draw(db):
    """Moon'ariu Sensei's granted "1-SHOT: Deathcry - Put this into play".

    The native death path queued the Deathcry for the chain *and* resolved it
    inline, so the queued copy stranded the chain and the enters-play trigger
    created by the return (Deploy - Draw a card) never resolved.  One authored
    resolution must return the card, consume the ONE-SHOT
    (``uses_per_game=1`` drops the ability from the instance), and let the
    Deploy draw resolve afterwards.
    """
    from rules_port.context import EffectContext
    from rules_port.death_effects import kill_troop
    from rules_port.resolution import resolve_port_trigger
    _copy_card(db, TPL_MOONARIU_SENSEI)
    _copy_ability(db, AG_SENSEI_DEPLOY_DRAW)
    _copy_ability(db, AG_SENSEI_ONESHOT_DEATHCRY)
    add_card(db, 601, 5, TPL_MOONARIU_SENSEI, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=601",
               (json.dumps([AG_SENSEI_DEPLOY_DRAW,
                            AG_SENSEI_ONESHOT_DEATHCRY]),))
    for uid in (301, 302):
        add_card(db, uid, 5, "14909185-1070-48df-9508-61d5a9650bd2",
                 loc="deck")
    db.commit()
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 3,
              "_rules_port_attached": True, "stack": [],
              "_next_instance_id": 1}
    handler._current_bstate = bstate

    def drain():
        resolved = []
        for item in list(bstate.get("stack") or []):
            bstate["stack"].remove(item)
            resolve_port_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                                 bstate, item)
            resolved.append(item["ability_guid"])
        return resolved

    context = EffectContext.from_rules_port(
        game, SessionStub(), db, handler, pl_t, ai_t, bstate, "kill", None)
    assert kill_troop(context, 601, cause="damage").startswith("killed")
    # Exactly one authored resolution: the Deathcry, not a duplicate.
    assert drain() == [AG_SENSEI_ONESHOT_DEATHCRY], bstate.get("stack")
    location, abilities = db.execute(
        "SELECT location, card_abilities FROM game_cards "
        "WHERE card_uid=601").fetchone()
    assert location == "warzone", location
    assert AG_SENSEI_ONESHOT_DEATHCRY not in json.loads(abilities), abilities
    # The return queued the card's own enters-play trigger, and its draw
    # resolves because no stale chain item is left behind.
    assert drain() == [AG_SENSEI_DEPLOY_DRAW], bstate.get("stack")
    hand = db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=1 AND user_id=5 "
        "AND location='hand'").fetchone()[0]
    assert hand == 1, hand
    assert not (bstate.get("stack") or []), bstate.get("stack")


def _main():
    tests = (test_brood_creeper_damage_to_opposing_champion_summons,
             test_cards_attacked_dispatch_uses_group_count_once,
             test_card_battled_dispatch_is_directional,
             test_lose_life_modifier_is_not_damage,
             test_brood_creeper_does_not_fire_on_own_champion,
             test_generated_card_uid_is_independent_of_row_id,
             test_spawn_of_othuyeg_buries_one_or_five,
             test_hand_incantation_trigger_does_not_fire,
             test_countermagic_requires_castspells_target,
             test_countermagic_offered_in_ai_chain_window,
             test_strength_of_redwood_targets_combat_troop,
             test_chronic_madness_buries_escalates_and_returns_to_deck,
             test_bunjitsu_void_cost_is_a_cost_instance,
             test_champion_power_offer_requires_its_authored_cost_and_target,
             test_practice_ability_chain_window_reaches_the_client,
             test_bunjitsu_voided_stats_sum_both_troops,
             test_lightning_armada_counts_only_your_hand,
             test_summon_zero_count_does_not_crash,
             test_worker_bot_creation_replacement_is_authored,
             test_incubate_puts_eggs_in_opposing_deck,
             test_ai_incubate_uses_play_card_ability_on_chain,
             test_spiderling_egg_summons_under_random_opponent,
             test_spiderling_egg_bane_copies_discard_destination,
             test_state_based_death_includes_static_defense,
             test_troop_artifact_can_attack,
             test_unblockable_attacker_cannot_be_blocked,
             test_void_leaf_publishes_the_voided_troops_stats,
             test_human_block_dispatches_the_attackers_blocked_trigger,
             test_friendly_zone_trigger_ignores_cards_taken_from_an_opponent,
             test_incantation_of_fear_counter_on_opposing_crypt_entry,
             test_pvp_champion_trigger_discovery_uses_raw_participant_id,
             test_pvp_champion_trigger_condition_uses_raw_participant_owner,
             test_shifted_paradigm_never_moves_champion_when_crypt_empty,
             test_corinth_end_of_turn_ability_resolves_inline,
             test_native_chain_resolves_trigger_without_legacy_fallback,
             test_triggered_chance_branch_stores_the_entering_troop,
             test_ai_start_of_turn_buries_each_champion_deck,
             test_one_shot_deathcry_consumes_and_keeps_its_deploy_draw)
    failed = 0
    for fn in tests:
        db = make_db()
        try:
            fn(db)
            print("PASS", fn.__name__)
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _main()
