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

import game_engine

from tests.tests_cards_fixes import _copy_card, _copy_ability
from tests.tests_combat import make_db, add_card, HandlerStub, SessionStub

SRC = os.environ.get(
    "HEX_TEST_SOURCE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hconnect.db"),
)

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


def _pl_ai():
    pl_t = game_engine.UID.make(244, 5)
    ai_t = game_engine.UID.make(3, 1000)
    return pl_t, ai_t


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
    bstate = {"ai_resources": 1, "ai_threshold": {}, "turn_number": 1}
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


def _main():
    tests = (test_brood_creeper_damage_to_opposing_champion_summons,
             test_brood_creeper_does_not_fire_on_own_champion,
             test_generated_card_uid_is_independent_of_row_id,
             test_spawn_of_othuyeg_buries_one_or_five,
             test_hand_incantation_trigger_does_not_fire,
             test_countermagic_requires_castspells_target,
             test_countermagic_offered_in_ai_chain_window,
             test_chronic_madness_buries_escalates_and_returns_to_deck,
             test_bunjitsu_void_cost_is_a_cost_instance,
             test_bunjitsu_voided_stats_sum_both_troops,
             test_lightning_armada_counts_only_your_hand,
             test_summon_zero_count_does_not_crash,
             test_incubate_puts_eggs_in_opposing_deck,
             test_ai_incubate_uses_play_card_ability_on_chain,
             test_spiderling_egg_summons_under_random_opponent,
             test_spiderling_egg_bane_copies_discard_destination,
             test_state_based_death_includes_static_defense,
             test_troop_artifact_can_attack,
             test_unblockable_attacker_cannot_be_blocked,
             test_incantation_of_fear_counter_on_opposing_crypt_entry)
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
