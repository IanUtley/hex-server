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
TPL_RUNEWEB_INFILTRATOR = "e50468fe-6e6f-4319-80e9-c138748e18b4"
AG_RUNEWEB_INFILTRATOR_DAMAGE = "2b4d0103-4513-a194-8dfa-f48c0587ab49"
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
AG_ARMITRON_DEPLOY = "5e4ac297-ba9e-6d55-5a62-aab1a056b34a"
TPL_DAYBREAK = "22e5df67-93cb-4942-ad8e-ad3d0551fb96"
AG_DAYBREAK_HEAL = "d62cfc79-f069-4435-abfe-7a98b5f74989"
TPL_EMBERSPIRE_WITCH = "ecc1fc8b-a86c-4330-908a-e15ba445f2f0"
AG_CHAMPIONS_CANT_GAIN_HEALTH = "ab91b642-b63f-484c-5d49-782c96e06e22"
# Dragon Guard Stalwart's charge power: "[BASIC][DIAMOND]: [1][ARROWR]
# Gain 1 health."  A champion ability is not a ``game_cards`` row, so its
# source is the champion's synthetic SessionCardId.
AG_STALWART_GAIN_HEALTH = "45d470a3-e314-1797-d5d3-d5fef5b53507"
TPL_CEREBRAL_FULMINATION = "8bf3184f-b2b4-4646-b2e9-ac39079978c9"
AG_CEREBRAL_FULMINATION = "87a0cbaf-85f8-2555-3261-1c373bea1e77"
TPL_BOOBY_TRAP = "9c1acda8-778b-4dd0-b278-7fee21e203af"
AG_BOOBY_TRAP = "1a5c43ec-2c65-85f0-0310-8395ec405acf"


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
    from rules_port.resolution import resolve_port_ability

    _copy_ability(db, AG_HARDSHELL_LOSE_LIFE)
    pl_t, ai_t = _pl_ai()
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1}
    source_uid = int(handler._player_champ_scid.uid.uid64)
    resolve_port_ability(
        handler, game_engine.Game(1, pl_t, ai_t), SessionStub(), db, pl_t, ai_t,
        bstate, AG_HARDSHELL_LOSE_LIFE, source_uid, 5,
        target_map={0: source_uid})
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


def test_runeweb_infiltrator_puts_two_spiderling_eggs_in_opponent_deck(db):
    """Runeweb Infiltrator's native damage trigger creates two eggs for the
    opposing champion's deck, using the authored ``Two`` variable and target
    controller rather than the trigger source's owner."""
    from rules_port.resolution import resolve_port_trigger
    from rules_port.triggers import dispatch_native_trigger

    _copy_card(db, TPL_RUNEWEB_INFILTRATOR)
    _copy_card(db, TPL_SPIDERLING_EGG)
    add_card(db, 101, 0, TPL_RUNEWEB_INFILTRATOR, loc="warzone")
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
        (json.dumps([AG_RUNEWEB_INFILTRATOR_DAMAGE]),))
    # Existing cards make the destination an ordinary populated deck, not an
    # empty-deck special case.
    add_card(db, 201, 5, TPL_GLADIATOR, loc="deck")
    add_card(db, 202, 0, TPL_GLADIATOR, loc="deck")
    db.commit()

    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1,
              "stack": [], "_rules_port_attached": True}
    handler._current_bstate = bstate
    player_champ_uid = int(handler._player_champ_scid.uid.uid64)
    result = dispatch_native_trigger(
        db=db, handler=handler, game=game, session=SessionStub(),
        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
        event_type="CardDealtDamageEvent", source_card_id=101,
        source_player_id=0, target_card_id=player_champ_uid)
    assert AG_RUNEWEB_INFILTRATOR_DAMAGE[:8] in result, result

    item = next(item for item in (bstate.get("stack") or [])
                if item.get("ability_guid") == AG_RUNEWEB_INFILTRATOR_DAMAGE)
    bstate["stack"].remove(item)
    resolve_port_trigger(
        handler, game, SessionStub(), db, pl_t, ai_t, bstate, item)

    eggs = db.execute(
        "SELECT user_id, location, COUNT(*) FROM game_cards "
        "WHERE template_guid=? GROUP BY user_id, location",
        (TPL_SPIDERLING_EGG,)).fetchall()
    assert eggs == [(5, "deck", 2)], (result, eggs, bstate)


def test_queued_trigger_source_projection_preserves_combat_state(db):
    """Queueing a trigger must not make its attacking source look ready.

    NativeTriggerBackend republishes the trigger source so the client can
    display it on the chain. That CardUpdated must carry the persisted state;
    the wire builder defaults an omitted state to None, which visually readies
    an attacking source until a later refresh.
    """
    from rules_port.triggers import dispatch_native_trigger

    _copy_card(db, TPL_BROOD_CREEPER)
    _copy_card(db, TPL_SPIDESPAWN)
    source_uid = 101
    add_card(db, source_uid, 0, TPL_BROOD_CREEPER)
    expected_state = int(
        game_engine.ECardStates.Tapped |
        game_engine.ECardStates.Attacking |
        game_engine.ECardStates.HasAttacked |
        game_engine.ECardStates.StartedATurnOnYourSide)
    db.execute(
        "UPDATE game_cards SET card_abilities=?, card_state=? "
        "WHERE card_uid=?",
        (json.dumps([AG_BROOD_DAMAGE]), expected_state, source_uid))
    db.commit()

    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1,
              "stack": []}
    player_champ_uid = int(handler._player_champ_scid.uid.uid64)
    dispatch_native_trigger(
        db=db, handler=handler, game=game, session=SessionStub(),
        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
        event_type="CardDealtDamageEvent", source_card_id=source_uid,
        source_player_id=0, target_card_id=player_champ_uid)

    source_updates = [
        event for event in game.events
        if isinstance(event, game_engine.CardUpdatedSessionEventArgs)
        and int(event.session_card_id.uid.uid64) == source_uid]
    assert source_updates, "queued trigger should project its source card"
    assert source_updates[-1].state == expected_state, source_updates[-1].state
    assert bstate.get("stack"), "the trigger should be queued on the chain"


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
    from rules_port.targeting import legal_targets
    from rules_port.resolution import resolve_port_ability
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
    resolve_port_ability(
        handler, game, SessionStub(), db, pl_t, ai_t, bstate,
        AG_COUNTERMAGIC, 101, 5, target_map={0: 202})
    # The native port owns the live chain; the durable compatibility ``stack``
    # is no longer the resolver's state, so only the card outcome is asserted.
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

    for uid in (101, 102):
        add_card(db, uid, 5, TPL_CHRONIC_MADNESS, loc="deck")
        db.execute(
            "UPDATE game_cards SET card_abilities=? WHERE card_uid=?",
            (json.dumps([AG_CHRONIC_BURY, AG_CHRONIC_ESCALATE]), uid))
    db.commit()

    def cast(uid):
        db.execute(
            "UPDATE game_cards SET location='hand' WHERE session_id=1 "
            "AND card_uid=?", (uid,))
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
    assert "escalate " in out1, out1
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
    for uid in (101, 102):
        buffs = json.loads(db.execute(
            "SELECT permanent_buffs FROM game_cards WHERE card_uid=?",
            (uid,)).fetchone()[0])
        assert buffs["escalation_count"] == 2, (uid, buffs)
    escalated_events = {
        int(event.session_card_id.uid.uid64): event.escalation
        for event in game1.events
        if isinstance(event, game_engine.CardUpdatedSessionEventArgs)
        and int(event.session_card_id.uid.uid64) in {101, 102}
        and event.escalation == 2
    }
    assert escalated_events == {101: 2, 102: 2}, escalated_events

    # Second cast escalates: ESC*4 with count 2 buries 8.
    for uid in range(421, 441):
        add_card(db, uid, 0, "14909185-1070-48df-9508-61d5a9650bd2",
                 loc="deck")
    game2, out2 = cast(102)
    assert "bury 8 cards" in out2, out2
    for uid in (101, 102):
        buffs = json.loads(db.execute(
            "SELECT permanent_buffs FROM game_cards WHERE card_uid=?",
            (uid,)).fetchone()[0])
        assert buffs["escalation_count"] == 3, (uid, buffs)
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
            return undefined_practice_identity + 1  # pyright: ignore[reportUndefinedVariable] -- bytecode probe
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
    from rules_port.resolution import resolve_port_trigger

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

    bstate["_rules_port_native_effect"] = True
    result = resolve_port_trigger(
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
    from rules_port.resolution import resolve_port_trigger

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

    bstate["_rules_port_native_effect"] = True
    resolve_port_trigger(
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
    from rules_port.resolution import resolve_port_trigger
    from rules_port.triggers import dispatch_native_trigger
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
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1,
              "_rules_port_attached": True}
    dispatch_native_trigger(
        db=db, handler=handler, game=game, session=SessionStub(),
        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
        event_type="CardEnteredZoneEvent", source_card_id=202,
        source_player_id=0,
        data={"event_destination_collection": "discard"})
    items = bstate.get("stack") or []
    assert items, "Incantation of Fear trigger should fire on opposing crypt entry"
    for item in list(items):
        bstate["stack"].remove(item)
        resolve_port_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
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

    from rules_port.chain_items import resolve_trigger_item

    old_db, old_hcs = dbmod._db, hcs._db
    dbmod._db, hcs._db = db, db
    try:
        resolve_trigger_item(
            handler, SessionStub(), db, game, bstate, item, pl_t, ai_t)
    finally:
        dbmod._db, hcs._db = old_db, old_hcs
    moves = [(int(ev.session_card_id.uid.uid64), ev.collection)
             for ev in game.events
             if isinstance(ev, game_engine.CardMovedSessionEventArgs)]
    assert (9003, game_engine.ECardCollections.Deck) in moves, moves


def test_trigger_chain_item_does_not_rerun_its_bom_after_a_picker(db):
    """Darkspire Priestess's Deathcry asks for a deck troop once, not per card.

    Regression: the deck-search picker's continuation resolved the trigger's
    BOM and marked the chain item complete, but the chain-item seam re-entered
    ``resolve_port_trigger`` on the next pass.  Every entry re-ran the authored
    ``ActivateAbility`` chain and re-opened the picker with one fewer
    candidate — the live log asked 6, 5, 4, 3 times for a single death.
    """
    from types import SimpleNamespace
    from unittest import mock

    from rules_port import chain_items
    from rules_port.actions import AbilityResolutionState

    ability_guid = "9853659b-89f4-1e16-f940-67bdb37f5729"
    item = {"kind": "trigger", "ability_guid": ability_guid,
            "source_uid": 9217, "source_owner_uid": 1001,
            "trigger_target_uid": 9217, "instance_id": 17}
    state = {"resolving_source_uid": 9217, "resolving_owner_id": 1001}
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    handler.user_profile = {"id": 1001}
    handler._remove_one_shot_ability = lambda *args, **kwargs: False
    calls = []

    class Host:
        """Practice/PvE projection double for the shared chain-item seam."""

        def chain_load(self, session):
            return state

        def chain_save(self, session, value):
            pass

        def chain_new_game(self, session, value, player_uid, ai_uid):
            return game

        def chain_send(self, session, value, player_uid, ai_uid):
            pass

        def chain_push_empty(self, port, value, item, pending):
            return not pending and not value.get("stack")

    ability = SimpleNamespace(descriptor=dict(item), instance_id=17,
                             ignores_chain=False)

    def fake_trigger(*_args, **_kwargs):
        calls.append("bom")
        # The authored BOM parks on its deck-search picker.
        state["resolution_paused"] = True

    with mock.patch("rules_port.resolution.resolve_port_trigger",
                    fake_trigger):
        first = chain_items.resolve_chain_item(
            Host(), None, SessionStub(), db, ability, pl_t, ai_t)
        assert first is AbilityResolutionState.WAITING_FOR_INPUT, first
        assert calls == ["bom"], calls
        assert state["paused_chain_instance_id"] == 17, state
        assert state.get("stack"), state
        # The picker answer resolves that BOM and marks the chain item.
        state.pop("resolution_paused")
        state["completed_chain_instance_id"] = 17
        game.events.clear()
        second = chain_items.resolve_chain_item(
            Host(), None, SessionStub(), db, ability, pl_t, ai_t)
    assert second is AbilityResolutionState.COMPLETED, second
    assert calls == ["bom"], calls
    assert not state.get("resolution_paused"), state
    assert "completed_chain_instance_id" not in state, state
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


def test_triggered_attribute_grant_persists_the_speed_bit(db):
    """Psychotic Anarchist's "25% chance" grant must persist authored Speed.

    The child effect's typed ``AttributeModifier`` carries
    ``m_AttributeFlags = "Speed"``, whose runtime param key is
    ``attribute_flags``.  While the projection emitted ``attributeflags`` the
    attribute leaf resolved zero bits, so the client saw the companion
    +1[ATK] without any Speed.
    """
    from types import SimpleNamespace
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.resolution import resolve_port_trigger

    troop = 9201
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
    resolve_port_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                         bstate, item)
    speed = int(game_engine.ECardAttributes.Speed)
    row = db.execute(
        "SELECT card_attributes FROM game_cards WHERE card_uid=?",
        (troop,)).fetchone()
    assert int(row[0] or 0) & speed, row
    pushed = [event.attributes for event in game.events
              if isinstance(event, game_engine.CardUpdatedSessionEventArgs)]
    assert pushed and all(int(value or 0) & speed for value in pushed), pushed


TPL_KRAKEN_GUARD_MARINER = "e3a0ca9c-c21c-45d3-a36d-c4d5c03e445d"
AG_KRAKEN_GUARD_INSPIRE = "759e8464-7980-279a-1935-626e00c13f99"
TPL_NECROPHAGE_SENSEI = "01286365-bb10-4c8a-a539-2c61a8f76d95"
AG_NECROPHAGE_ENTERS_PLAY = "138319d8-9e41-6388-7090-2884d73accbb"


TPL_BATTLE_HOPPER = "fe2472ed-4ff8-455b-8b18-b7e0033cd896"
TPL_SHINHARE_MILITIA = "d4fdd87f-0f95-4ce5-9a88-7e1f48e3e9b8"
TPL_COTTONTAIL_RECRUITER = "b507438b-d5f9-4cbe-9d82-188427171fd6"
AG_COTTONTAIL_REPLACEMENT = "b393a0c8-d152-1224-f4c8-7645d7c07f35"
TPL_SPRING_LITTER_DISCIPLE = "3198d12c-3c31-4f75-90ad-1362069d18c2"
AG_SPRING_LITTER_BONUS = "474bff59-cf88-97a1-2e06-a790620207e4"


def test_creation_replacement_and_bonus(db):
    """Authored creation replacements substitute and add to creation counts.

    Cottontail Recruiter's "would create Battle Hopper, create Shin'hare
    Militia instead" must substitute the linked template, and Spring Litter
    Disciple's cost-1 creation bonus must add one to the batch.
    """
    from rules_port.context import EffectContext
    from rules_port.creation_effects import activate_creation_replacements
    from rules_port.token_effects import (_creation_count_bonus,
                                          _creation_replacement_guid)

    _copy_card(db, TPL_BATTLE_HOPPER)
    _copy_card(db, TPL_SHINHARE_MILITIA)
    _copy_card(db, TPL_COTTONTAIL_RECRUITER)
    _copy_ability(db, AG_COTTONTAIL_REPLACEMENT)
    _copy_card(db, TPL_SPRING_LITTER_DISCIPLE)
    _copy_ability(db, AG_SPRING_LITTER_BONUS)
    add_card(db, 701, 5, TPL_COTTONTAIL_RECRUITER, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=701",
               (json.dumps([AG_COTTONTAIL_REPLACEMENT]),))
    add_card(db, 702, 5, TPL_SPRING_LITTER_DISCIPLE, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=702",
               (json.dumps([AG_SPRING_LITTER_BONUS]),))
    db.commit()
    assert activate_creation_replacements(db, 1, 701)
    assert activate_creation_replacements(db, 1, 702)

    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1,
              "stack": [], "_rules_port_attached": True,
              "_next_instance_id": 1, "resolving_owner_id": 5}
    context = EffectContext.from_rules_port(
        game, SessionStub(), db, handler, pl_t, ai_t, bstate, "e", None)
    assert _creation_replacement_guid(
        context, TPL_BATTLE_HOPPER) == TPL_SHINHARE_MILITIA
    assert _creation_count_bonus(context, TPL_BATTLE_HOPPER) == 1


def test_native_enters_play_and_inspire_fire(db):
    """A CardEnteredZoneEvent must chain the events C# raises after it.

    The native dispatcher only ran the CardEnteredZone triggers, so a card's
    own "as this enters play" ability (Necrophage Sensei) and every Inspire
    ability (Kraken Guard Mariner) never fired in attached sessions.  C#
    raises AsEntersPlayEvent afterwards, then CardInspiredEvent per inspirer.
    """
    from rules_port.triggers import dispatch_native_trigger

    _copy_card(db, TPL_KRAKEN_GUARD_MARINER)
    _copy_ability(db, AG_KRAKEN_GUARD_INSPIRE)
    _copy_card(db, TPL_NECROPHAGE_SENSEI)
    _copy_ability(db, AG_NECROPHAGE_ENTERS_PLAY)
    # Two troops in crypts: the entering Sensei gets +1/+1 for each.
    add_card(db, 201, 5, TPL_NECROPHAGE_SENSEI, loc="discard")
    add_card(db, 202, 0, TPL_NECROPHAGE_SENSEI, loc="discard")
    add_card(db, 101, 5, TPL_NECROPHAGE_SENSEI, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
               (json.dumps([AG_NECROPHAGE_ENTERS_PLAY]),))
    # The second Mariner enters with cost >= the first, so the first inspires.
    add_card(db, 301, 5, TPL_KRAKEN_GUARD_MARINER, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=301",
               (json.dumps([AG_KRAKEN_GUARD_INSPIRE]),))
    add_card(db, 302, 5, TPL_KRAKEN_GUARD_MARINER, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=302",
               (json.dumps([AG_KRAKEN_GUARD_INSPIRE]),))
    db.commit()

    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 1,
              "stack": [], "_rules_port_attached": True,
              "_next_instance_id": 1}

    def enter(uid):
        return dispatch_native_trigger(
            db=db, handler=handler, game=game, session=SessionStub(),
            player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
            event_type="CardEnteredZoneEvent", source_card_id=uid,
            source_player_id=5,
            data={"event_source_collection": "hand",
                  "event_destination_collection": "warzone"})

    assert "AsEntersPlayEvent" in enter(101)
    buffs = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE card_uid=101"
    ).fetchone()[0]
    assert '"atk": 2' in buffs or '"atk":2' in buffs, buffs

    assert "AsEntersPlayEvent" in enter(302)
    assert bstate["tac_statistics"]["cards"]["302"][
        "CardStatsWithSpecificDuration"]["InspireCount"] == 1
    attrs = db.execute(
        "SELECT card_attributes FROM game_cards WHERE card_uid=302"
    ).fetchone()[0]
    assert int(attrs or 0) & int(game_engine.ECardAttributes.Steadfast), attrs


def test_troop_entry_discovers_the_opposing_champions_trigger(db):
    """The AI champion's "when a troop enters play" trait is owner-agnostic.

    Psychotic Anarchist's champion ability (0897aeba) has no m_Your/m_Opposing
    restriction, so the client fires it for a troop entering play on EITHER
    side.  Discovery only scanned the entering card's own champion, so the
    trait never applied Speed/+1[ATK] to the human's troops.
    """
    from rules_port.trigger_discovery import RecordsTriggerDiscovery

    pl_t, ai_t = _pl_ai()
    handler = HandlerStub(db)
    # No champion_guid: the test DB has no champion_abilities seed tables, so
    # the configured ability list is the metadata seam under test.
    handler._ai_champ_guid = None
    handler._ai_champ_ability_guids = [AG_PSYCHOTIC_CHANCE]
    ai_champ_uid = int(handler._ai_champ_scid.uid.uid64)
    discovery = RecordsTriggerDiscovery(
        db, handler, SessionStub(), pl_t, ai_t, {"turn_number": 1})

    player_troop = 9210
    ai_troop = 9211
    add_card(db, player_troop, 5, TPL_GLADIATOR)
    add_card(db, ai_troop, 0, TPL_GLADIATOR)
    for source, owner in ((player_troop, 5), (ai_troop, 0)):
        candidates = discovery.discover(
            "CardEnteredZoneEvent", source_uid=source,
            source_owner_uid=owner)
        by_source = {int(c.source_uid): set(c.ability_guids)
                     for c in candidates}
        assert AG_PSYCHOTIC_CHANCE in by_source.get(ai_champ_uid, set()), (
            source, owner, by_source)


def test_generated_card_pool_offers_only_ownable_castable_cards(db):
    """Corinth's "create three random non-resource cards" pool is filtered.

    The client settles ownability with ``CardRarity > ERarity.Land ||
    IsBasicResource()``: a Land-rarity non-shard card only ever exists because
    another card creates it (Valor is created by seventeen summon/transform
    effects, Vine Goliath is a transform token).  The pool also has to treat
    repeated shards as counts — "Wild Wild Wild" is three Wild, so comparing
    the flattened entries one at a time offered cards a player cannot cast.
    """
    import db as dbm
    from gamedata import DEFAULT_RECORD_STORE
    from pvp_db import db_transform_candidate_templates
    from rules_port.filters import records_filter_matches
    from rules_port.token_effects import _candidate_thresholds

    rows = db_transform_candidate_templates(conn=dbm._db)
    names = {row[1] for row in rows}
    assert "Valor" not in names and "Vine Goliath" not in names
    assert "Blood Shard" in names, "basic shards stay ownable"

    card_filter = DEFAULT_RECORD_STORE.get(
        "AbilityEffectTemplate",
        "f3f1f5d1-a196-c4c9-ac10-51b93ff8bd3e").to_dict()["m_CardFilter"]
    source = {"user_id": 5, "owner_id": 5, "controller_id": 5}
    checked = 0
    for row in rows:
        try:
            threshold_data = json.loads(row[5] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        requirements = _candidate_thresholds(threshold_data)
        repeat = next((entry for entry in requirements
                       if int(entry["quantity"]) >= 2), None)
        if repeat is None:
            continue
        candidate = {"card_uid": 0, "template_guid": row[0],
                     "name": row[1] or "", "card_type": row[2] or "",
                     "cost": int(row[3] or 0), "rarity": row[4] or "",
                     "shards": [], "thresholds": requirements,
                     "subtype": row[6] or "", "attributes": int(row[7] or 0),
                     "user_id": 5}
        color = str(repeat["color_flags"])
        needed = int(repeat["quantity"])
        assert not records_filter_matches(
            candidate, card_filter, source=source, context=None,
            player={"resource_thresholds": {color: needed - 1}}), row[1]
        assert records_filter_matches(
            candidate, card_filter, source=source, context=None,
            player={"resource_thresholds": {color: needed}}), row[1]
        checked += 1
        if checked >= 3:
            break
    assert checked, "no multi-shard candidate found in the generated pool"


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


def test_emberspire_witch_blocks_every_champion_health_gain(db):
    """Emberspire Witch's CantGainHealth must reach both live heal paths.

    "Champions can't gain health." is a WhileCardInPlay CantGainHealth intattr
    on an AllChampions target.  The per-card static projection only reaches
    ``game_cards`` rows, so RulesPort healed anyway: Dragon Guard Stalwart's
    "Gain 1 health." charge power and Daybreak's "At the start of your turn,
    gain 1 health." both ignored her.  The client funnels every gain through
    ``Session.HealChampion``, which refuses while the champion carries the
    attribute, so RulesPort refuses it in one shared operation.
    """
    from rules_port.resolution import resolve_port_ability
    from rules_port.triggers import dispatch_native_trigger

    _copy_card(db, TPL_DAYBREAK)
    _copy_ability(db, AG_DAYBREAK_HEAL)
    _copy_card(db, TPL_EMBERSPIRE_WITCH)
    _copy_ability(db, AG_CHAMPIONS_CANT_GAIN_HEALTH)
    add_card(db, 101, 5, TPL_DAYBREAK, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
               (json.dumps([AG_DAYBREAK_HEAL]),))
    pl_t, ai_t = _pl_ai()
    handler = HandlerStub(db)
    game = game_engine.Game(1, pl_t, ai_t)

    def start_turn_heal(bstate):
        handler._current_bstate = bstate
        return dispatch_native_trigger(
            db=db, handler=handler, game=game, session=SessionStub(),
            player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
            event_type="TurnStartedEvent", source_card_id=None,
            source_player_id=5)

    def charge_power_heal(bstate):
        handler._current_bstate = bstate
        bstate["resolving_owner_id"] = 0
        bstate["resolving_source_uid"] = 0
        resolve_port_ability(
            handler, game, SessionStub(), db, pl_t, ai_t, bstate,
            AG_STALWART_GAIN_HEALTH, 0, 0,
            target_map={0: int(ai_t.uid64)}, instance_id=7)

    add_card(db, 102, 5, TPL_EMBERSPIRE_WITCH, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=102",
               (json.dumps([AG_CHAMPIONS_CANT_GAIN_HEALTH]),))
    db.commit()
    blocked = {"player_health": 12, "ai_health": 12, "turn_number": 2}
    assert AG_DAYBREAK_HEAL[:8] in start_turn_heal(blocked)
    assert blocked["player_health"] == 12, blocked
    charge_power_heal(blocked)
    assert blocked["ai_health"] == 12, blocked

    # Both gains land once the constraint has left play.
    db.execute("DELETE FROM game_cards WHERE card_uid=102")
    db.commit()
    healed = {"player_health": 12, "ai_health": 12, "turn_number": 3}
    assert AG_DAYBREAK_HEAL[:8] in start_turn_heal(healed)
    assert healed["player_health"] == 13, healed
    charge_power_heal(healed)
    assert healed["ai_health"] == 13, healed


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


def test_deploy_effect_targets_the_chosen_card_not_the_trigger_source(db):
    """A triggered ability's input target outranks the triggering card.

    Armitron's Deploy is "Another target Robot you control gets +1[ATK]/+1[DEF]".
    Its chain item carries the chosen Robot in ``target_uid`` and the entering
    Armitron itself in ``trigger_target_uid``; the native resolver preferred
    the trigger card, so the buff landed on Armitron instead of the selected
    Robot.  ``trigger_target_uid`` must stay available for the templates that
    name the event's card (``AbilityTriggerCardTargetTemplate``).
    """
    from unittest import mock
    from rules_port import effects as effects_module
    from rules_port.resolution import resolve_port_trigger

    source = 0x4601
    chosen = 0x4001
    fallback = 0x4002
    add_card(db, source, 0, TPL_GLADIATOR)
    add_card(db, chosen, 0, TPL_GLADIATOR)
    add_card(db, fallback, 0, TPL_GLADIATOR)
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    graph = ability_graph(DEFAULT_RECORD_STORE, AG_ARMITRON_DEPLOY)
    target_index = next(i for i, target in enumerate(graph.targets)
                        if target.requires_input)
    item = {"kind": "trigger", "ability_guid": AG_ARMITRON_DEPLOY,
            "source_uid": source, "target_uid": fallback,
            "trigger_target_uid": source, "source_owner_uid": 0,
            "activation_data": {"target_map": {
                str(target_index): [chosen]}},
            "instance_id": 1}
    bstate = {"pvp": False, "turn_pid": 0, "stack": [], "player_health": 20,
              "ai_health": 20}
    resolved = []
    real_dispatch = effects_module.dispatch

    def recording_dispatch(effect_type, context, effect=None):
        if effect_type == "CardModifierAbilityEffectTemplate":
            resolved.append(context.modifier_target())
            return "recorded"
        return real_dispatch(effect_type, context, effect)

    with mock.patch.object(effects_module, "dispatch", recording_dispatch):
        resolve_port_trigger(handler, game, SessionStub(), db, pl_t, ai_t,
                             bstate, item)
    # Both the +1[ATK] and +1[DEF] modifiers land on the selected Robot.
    assert resolved == [chosen, chosen], [hex(value) for value in resolved
                                         if value is not None]


def test_replaced_projection_drains_into_the_next_packet_once(db):
    """A projection the port moved off must reach the next packet, once.

    Practice builds a fresh Game per packet, so a native phase entry can
    publish (an AI attack declaration, for example) after the previous packet
    was sent and before the host builds the next one.  Pointing the sink at the
    new projection queues the outgoing one; the session-level serializer drains
    it.  The carried events are older than the new packet's own, so they must
    arrive first, and the queue must clear so nothing is delivered twice.
    """
    from rules_port.session import GameEngineEventSink

    pl_t, ai_t = _pl_ai()
    declared = game_engine.AttackDeclaredSessionEventArgs.CLASS_ID
    phase_game = game_engine.Game(1, pl_t, ai_t)
    phase_game.push_attack_declared(
        game_engine.CombatId(ai_t, 0x4001 & 0xFFFF), ai_t,
        game_engine.SessionCardId(game_engine.UID(0x101)),
        game_engine.SessionCardId(game_engine.UID(0x4001)))
    sink = GameEngineEventSink(phase_game)
    packet_game = game_engine.Game(1, pl_t, ai_t)
    packet_game.push_turn_phase(game_engine.ETurnPhases.DeclareDefense, ai_t,
                                pl_t)
    sink.game = packet_game
    assert sink.drain_into(packet_game) == 1
    assert not phase_game.events, phase_game.events
    packet = packet_game.make_network_packet(pl_t)
    assert packet.event_ids[0] == declared, packet.event_ids
    # Drained once: the queue is empty and a later packet has no declaration.
    assert sink.drain_into(packet_game) == 0
    assert declared not in packet_game.make_network_packet(pl_t).event_ids


def test_pvp_same_events_serializer_drains_and_consumes_its_projection(db):
    """The PvP session-level sender owns the same drain, and consumes it.

    Tournament PvP clones one Game into a packet per recipient.  It must drain
    the sink's unpublished queue into that Game (never into a per-recipient
    clone, which would deliver the events to one player only) and then clear
    the source, or a later re-point would replay them to both clients.
    """
    import services.tournament_game as tournament_game
    from rules_port.session import GameEngineEventSink

    pl_t, ai_t = _pl_ai()
    sink_game = game_engine.Game(1, pl_t, ai_t)
    sink = GameEngineEventSink(sink_game)
    session = SessionStub()
    session.session_id = 1
    session._rules_port_session = type("Port", (), {"event_sink": sink})()
    # A projection the port moved off still holds an unpublished declaration.
    stale = game_engine.Game(1, pl_t, ai_t)
    stale.push_attack_declared(
        game_engine.CombatId(ai_t, 0x4001 & 0xFFFF), ai_t,
        game_engine.SessionCardId(game_engine.UID(0x101)),
        game_engine.SessionCardId(game_engine.UID(0x4001)))
    sink._game = stale
    sink.game = sink_game
    handlers = dict(tournament_game.player_handlers)
    tournament_game.player_handlers.clear()
    try:
        tournament_game._pvp_send_same_events(session, sink_game, pl_t, ai_t)
    finally:
        tournament_game.player_handlers.clear()
        tournament_game.player_handlers.update(handlers)
    # Drained into the session-level Game, then consumed by the clone send.
    assert not stale.events, stale.events
    assert not sink_game.events, sink_game.events
    assert sink.drain_into(sink_game) == 0


def test_opposing_card_triggers_at_the_start_of_the_active_champions_turn(db):
    """Cerebral Fulmination: "At the start of each champion's turn, they draw
    a card."  The turn boundary is broadcast to both sides, so an AI-owned copy
    must resolve on the human's turn and let the human draw.  Scanning only the
    active side's cards silently dropped it.
    """
    from rules_port.triggers import dispatch_native_trigger
    _copy_card(db, TPL_CEREBRAL_FULMINATION)
    add_card(db, 101, 0, TPL_CEREBRAL_FULMINATION, loc="warzone")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
               (json.dumps([AG_CEREBRAL_FULMINATION]),))
    for uid in (301, 302):
        add_card(db, uid, 5, "14909185-1070-48df-9508-61d5a9650bd2",
                 loc="deck")
    db.commit()
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 2}
    handler._current_bstate = bstate
    result = dispatch_native_trigger(
        db=db, handler=handler, game=game, session=SessionStub(),
        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
        event_type="TurnStartedEvent",
        source_card_id=int(handler._player_champ_scid.uid.uid64),
        source_player_id=5)
    assert AG_CEREBRAL_FULMINATION[:8] in result, result
    drawn = db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=1 AND user_id=5 "
        "AND location='hand'").fetchone()[0]
    assert drawn == 1, drawn


def test_booby_trap_damages_its_owners_champion(db):
    """Booby Trap's damage effect targets target-index 1 ("You").

    The implicit champion fallback derived its target from the ability's
    *first* authored template (index 0, a "Self" AbilitySourceCard target), so
    the damage leaf reported "damage: no target" and traps never damaged the
    champion that drew them.
    """
    from rules_port.triggers import dispatch_native_trigger
    _copy_card(db, TPL_BOOBY_TRAP)
    add_card(db, 101, 0, TPL_BOOBY_TRAP, loc="deck")
    db.execute("UPDATE game_cards SET card_abilities=? WHERE card_uid=101",
               (json.dumps([AG_BOOBY_TRAP]),))
    db.commit()
    pl_t, ai_t = _pl_ai()
    game = game_engine.Game(1, pl_t, ai_t)
    handler = HandlerStub(db)
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 2,
              "stack": []}
    handler._current_bstate = bstate
    result = dispatch_native_trigger(
        db=db, handler=handler, game=game, session=SessionStub(),
        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
        event_type="CardWouldEnterZoneEvent", source_card_id=101,
        source_player_id=0, target_card_id=101,
        data={"event_source_collection": "deck",
              "event_destination_collection": "hand"})
    assert AG_BOOBY_TRAP[:8] in result, result
    assert bstate["ai_health"] == 16, bstate
    assert bstate["player_health"] == 20, bstate


def test_construction_plans_count_the_exhausted_troops(db):
    """"Exhaust one or more Dwarves and/or Robots you control: add a
    construction counter to this for each troop exhausted this way."  The
    count comes from the ability's ExhaustedCards list, and the counter goes
    on the Plans, not on the exhausted troop the client flattened into
    TargetMap[0]."""
    from rules_port.resolution import resolve_port_ability
    plan, bot, hornet = "aa325145-6d3d-474e-b990-608619620fe8", "02ed9695-207a-4c23-a3f8-13c7b001203d", "93cf512d-8b01-4e30-bf22-f02bf81cf12b"
    db.execute("DELETE FROM game_cards")
    db.execute("CREATE TABLE IF NOT EXISTS card_counter_templates "
               "(template_id TEXT PRIMARY KEY, name TEXT, description TEXT)")
    db.execute("INSERT OR IGNORE INTO card_counter_templates VALUES "
               "('c277b077-04ae-5020-09fb-d5831d3358b8', 'Construction', '')")
    for tpl in (plan, bot, hornet):
        _copy_card(db, tpl)
    add_card(db, 0x401, 5, plan, loc="warzone")
    add_card(db, 0x301, 5, bot, loc="warzone")
    add_card(db, 0x501, 5, bot, loc="warzone")
    db.commit()
    pl_t, ai_t = _pl_ai()
    bstate = {"player_health": 20, "ai_health": 20, "turn_number": 3,
              "stack": [], "_rules_port_attached": True}

    def activate(*exhausted):
        # The live chain resolves the saved descriptor: an empty TargetMap and
        # the exhaust selections in its cost_target_map (string keys, as
        # persisted).
        resolve_port_ability(
            HandlerStub(db), game_engine.Game(1, pl_t, ai_t), SessionStub(), db,
            pl_t, ai_t, bstate, "257418ed-24ec-98cd-d6f7-faeb02de4d50", 0x401, 5,
            target_map={}, cost_target_map={"0": list(exhausted)},
            instance_id=1)

    def plan_row():
        return db.execute("SELECT template_guid, permanent_buffs FROM game_cards "
                          "WHERE card_uid=?", (0x401,)).fetchone()

    activate(0x301)
    assert json.loads(plan_row()[1])["counters"] == {"construction": 1}, plan_row()
    assert json.loads(db.execute("SELECT permanent_buffs FROM game_cards "
                                 "WHERE card_uid=?", (0x301,)).fetchone()[0]
                      or "{}").get("counters", {}) == {}
    activate(0x501)   # two counters: remove them and become a Hornet Bot
    assert plan_row()[0] == hornet, plan_row()
    # Exhausting two troops at once adds two counters in one activation.
    db.execute("UPDATE game_cards SET template_guid=?, card_template_id=?, "
               "permanent_buffs='{}', card_state=0 WHERE card_uid=?",
               (plan, plan, 0x401))
    db.commit()
    activate(0x301, 0x501)
    assert plan_row()[0] == hornet, plan_row()


def test_dictionary_with_struct_keys_decodes(db):
    """A Dictionary whose keys are structs decoded to dict keys and failed
    the whole transaction with "unhashable type: 'dict'"."""
    from application.objfmt_wire import _hashable_key
    assert _hashable_key({"m_UID64": 0x301}) == 0x301
    assert _hashable_key({"a": 1, "b": 2}) == '{"a": 1, "b": 2}'
    assert _hashable_key(3) == 3
    assert hash(_hashable_key([{"m_UID64": 1}, 2])) is not None


# A real ActivateAbilityTransaction from the Mono client: Construction Plans:
# Crank Rocket with two Robots selected in XCostData.CardsToExhaust.
_TWO_TROOP_EXHAUST_ACTIVATION = bytes.fromhex(
    "3b303b303b323b506c6179657249643b313b313b313b6d5f55494436343b323b323b303b4634"
    "36303732304546433144343530363b5472616e73616374696f6e3b333b333b333b6d5f416269"
    "6c69747941637469766174696f6e446174613b343b343b383b536f757263654361726449643b"
    "353b353b313b76616c75653b363b313b313b6d5f55494436343b373b323b303b303130393030"
    "303030303030303030303b4162696c69747954656d706c61746549643b383b363b313b6d5f47"
    "7569643b393b373b303b33363b63316262636563662d656236622d326436392d333436662d31"
    "65393231323265333236364162696c697479496e7374616e636549643b31303b383b303b3030"
    "30303030303030303030303030303b44697361626c653b31313b393b303b304f70746564496e"
    "3b31323b393b303b314f70746564496e5365743b31333b393b303b3078436f7374446174613b"
    "31343b31303b373b6d5f53657456616c7565733b31353b31313b313b76616c75655f5f3b3136"
    "3b31323b303b31303030303030303b6d5f5265736f7572636558436f73743b31373b31323b30"
    "3b30303030303030303b6d5f436861726765506f696e747358436f73743b31383b31323b303b"
    "30303030303030303b6d5f5370656c6c506f696e747358436f73743b31393b31323b303b3030"
    "3030303030303b6d5f4c69666558436f73743b32303b31323b303b30303030303030303b6d5f"
    "4361726473546f457868617573743b32313b31333b303b313b303b32323b31343b323b6b6579"
    "3b32333b363b313b6d5f477569643b32343b373b303b33363b38373638613737622d64396134"
    "2d376466372d656537612d34326364396562643137653976616c75653b32353b31353b303b32"
    "3b303b32363b353b313b76616c75653b32373b313b313b6d5f55494436343b32383b323b303b"
    "303131313237303030303030303030303b313b32393b353b313b76616c75653b33303b313b31"
    "3b6d5f55494436343b33313b323b303b303130383030303030303030303030303b6d5f436f75"
    "6e74657258436f73743b33323b31323b303b30303030303030303b496e6465783b33333b3132"
    "3b303b46464646464646463b6d5f506c6179657249643b33343b313b313b6d5f55494436343b"
    "33353b323b303b463436303732304546433144343530363b6d5f5472616e73616374696f6e49"
    "643b33363b31323b303b31353030303030303b47616d652e5368617265642e4e6574776f726b"
    "2e47616d6553657373696f6e2e506c617965725472616e73616374696f6e5265717565737441"
    "7267733b47616d652e5368617265642e5549443b53797374656d2e55496e7436343b47616d65"
    "2e5368617265642e4d656368616e6963732e5472616e73616374696f6e732e41637469766174"
    "654162696c6974795472616e73616374696f6e3b47616d652e5368617265642e4d656368616e"
    "6963732e4162696c69746965732e4162696c69747941637469766174696f6e446174613b4761"
    "6d652e5368617265642e53657373696f6e4361726449643b47616d652e5368617265642e5265"
    "736f7572636549643b53797374656d2e477569643b53797374656d2e496e7436343b53797374"
    "656d2e426f6f6c65616e3b47616d652e5368617265642e4d656368616e6963732e4162696c69"
    "746965732e58436f7374446174613b47616d652e5368617265642e4d656368616e6963732e41"
    "62696c69746965732e58436f7374446174612b4553657456616c7565733b53797374656d2e49"
    "6e7433323b53797374656d2e436f6c6c656374696f6e732e47656e657269632e44696374696f"
    "6e61727960322347616d652e5368617265642e5265736f7572636549642153797374656d2e43"
    "6f6c6c656374696f6e732e47656e657269632e4c69737460312347616d652e5368617265642e"
    "53657373696f6e4361726449643b53797374656d2e436f6c6c656374696f6e732e47656e6572"
    "69632e4b657956616c75655061697260322347616d652e5368617265642e5265736f75726365"
    "49642153797374656d2e436f6c6c656374696f6e732e47656e657269632e4c69737460312347"
    "616d652e5368617265642e53657373696f6e4361726449643b53797374656d2e436f6c6c6563"
    "74696f6e732e47656e657269632e4c69737460312347616d652e5368617265642e5365737369"
    "6f6e4361726449640a3839333b34363b33313b3834303b3733393b36323b34333b33313b3736"
    "3b35323b34323b31363b31363b31393b3435353b34353b32353b33333b33373b33363b32393b"
    "3232353b3139383b36343b35333b3132343b35343b34353b33323b35343b34353b33323b3332"
    "3b32333b35303b33323b3333")


def test_two_troop_exhaust_activation_keeps_both_troops(db):
    """The client sends exhaust costs in XCostData.m_CardsToExhaust.  The
    ingress normalizer knew only CardsToSacrifice, so a raw fallback kept one
    card: exhausting two troops exhausted one and added one counter."""
    from application.objfmt_wire import parse_datawrapper
    from application.player_transactions import (
        classify_player_transaction, typed_payload_from_decoded)
    from rules_port.resolution import build_port_ability, _exhausted_cost_cards
    raw = _TWO_TROOP_EXHAUST_ACTIVATION
    decoded = parse_datawrapper(raw, preserve_complex=True)
    decoded.setdefault("__raw__", raw)
    payload = typed_payload_from_decoded(classify_player_transaction(raw), decoded)
    activation = payload["activation_data"]
    cost = {int(k): list(v) for k, v in activation["cost_target_map"].items()}
    assert cost == {0: [0x271101, 0x801]}, activation
    ability = build_port_ability(
        "c1bbcecf-eb6b-2d69-346f-1e92122e3266", 0x901, 5,
        target_map=activation.get("target_map"), cost_target_map=cost)
    # Both troops are paid; neither is left as the automatic "this" target.
    assert _exhausted_cost_cards(ability) == [0x271101, 0x801]
    assert dict(ability.activation.target_map) == {}, ability.activation.target_map


def _main():
    tests = (test_construction_plans_count_the_exhausted_troops,
             test_dictionary_with_struct_keys_decodes,
             test_two_troop_exhaust_activation_keeps_both_troops,
             test_brood_creeper_damage_to_opposing_champion_summons,
             test_cards_attacked_dispatch_uses_group_count_once,
             test_card_battled_dispatch_is_directional,
             test_lose_life_modifier_is_not_damage,
             test_brood_creeper_does_not_fire_on_own_champion,
             test_runeweb_infiltrator_puts_two_spiderling_eggs_in_opponent_deck,
             test_queued_trigger_source_projection_preserves_combat_state,
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
             test_trigger_chain_item_does_not_rerun_its_bom_after_a_picker,
             test_triggered_chance_branch_stores_the_entering_troop,
             test_triggered_attribute_grant_persists_the_speed_bit,
             test_creation_replacement_and_bonus,
             test_native_enters_play_and_inspire_fire,
             test_troop_entry_discovers_the_opposing_champions_trigger,
             test_ai_start_of_turn_buries_each_champion_deck,
             test_opposing_card_triggers_at_the_start_of_the_active_champions_turn,
             test_booby_trap_damages_its_owners_champion,
             test_generated_card_pool_offers_only_ownable_castable_cards,
             test_one_shot_deathcry_consumes_and_keeps_its_deploy_draw,
             test_emberspire_witch_blocks_every_champion_health_gain,
             test_deploy_effect_targets_the_chosen_card_not_the_trigger_source,
             test_replaced_projection_drains_into_the_next_packet_once,
             test_pvp_same_events_serializer_drains_and_consumes_its_projection)
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
