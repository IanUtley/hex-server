"""Focused checks for the metadata-driven card coverage layer.

Run serially with the other repository card tests:

    python3 tests/tests_card_coverage.py
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

import db as dbmod

from rules_port.coverage import NATIVE_EFFECTS
from abilities.framework.effects.tokens import _random_template_guids, load_player_deck
from abilities.framework.fields import effect_template
from abilities.framework.targeting import template_faction
from abilities.framework.triggers import ability_matches_keyword


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Session:
    session_id = 987654


def test_typed_conscript_filter():
    db = sqlite3.connect(SRC)
    try:
        effect_guid, = db.execute(
            "SELECT effect_guid FROM ability_effects "
            "WHERE effect_type='ConscriptAbilityEffectTemplate' LIMIT 1"
        ).fetchone()
        template = effect_template(effect_guid)
        assert template and template.get("m_CardFilter"), effect_guid
        candidates = _random_template_guids(
            db, template["m_CardFilter"], 0, 5)
        assert candidates, "typed Conscript filter returned no cards"
        for guid in candidates[:25]:
            row = db.execute(
                "SELECT card_type, cost FROM card_templates WHERE guid=?",
                (guid,)).fetchone()
            assert row and "Troop" in (row[0] or "") and row[1] == 1, row
        faction = template.get("m_Faction")
        assert faction
        assert all(template_faction(guid) == faction for guid in candidates)
    finally:
        db.close()


def test_random_artifact_pool_respects_game_mode():
    db = sqlite3.connect(SRC)
    try:
        artifact_filter = {
            "_t": "Game.Shared.Mechanics.Cards.Filters.IsArtifact"
        }
        pvp_candidates = _random_template_guids(
            db, artifact_filter, 0, 5, {"pvp": True})
        assert pvp_candidates, "PvP artifact filter returned no cards"
        assert all(db.execute(
            "SELECT is_pve=0 AND no_pvp=0 FROM card_templates WHERE guid=?",
            (guid,)).fetchone()[0] for guid in pvp_candidates)

        pve_candidates = _random_template_guids(
            db, artifact_filter, 0, 5, {"pvp": False})
        assert len(pve_candidates) >= len(pvp_candidates)
        assert set(pvp_candidates).issubset(set(pve_candidates))
    finally:
        db.close()


def test_keyword_matching_uses_ability_metadata():
    db = sqlite3.connect(SRC)
    try:
        # These are real Set 1 abilities whose serialized TAC/event metadata
        # identifies the keyword; neither assertion depends on display text
        # being parsed as the primary source.
        deathcry = "8c11891b-8667-a479-63d8-4aac733c8adc"
        momentum = "13357fde-a793-e300-a589-bd102244109b"
        assert ability_matches_keyword(db, deathcry, "Deathcry")
        assert ability_matches_keyword(db, momentum, "Momentum")
    finally:
        db.close()


def test_load_player_deck_instantiates_typed_resources():
    source = sqlite3.connect(SRC)
    db = sqlite3.connect(":memory:")
    source.backup(db)
    source.close()
    try:
        effect_guid, = db.execute(
            "SELECT effect_guid FROM ability_effects "
            "WHERE effect_type='LoadPlayerDeckAbilityEffectTemplate' LIMIT 1"
        ).fetchone()
        template = effect_template(effect_guid)
        deck_guid = template["m_DeckTemplateId"]["m_Guid"]
        result = load_player_deck(
            None, Session(), db, None, None, None,
            {"resolving_owner_id": 5}, effect_guid, None)
        assert result.startswith("loaded "), result
        count = db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE session_id=? "
            "AND user_id=? AND location='deck'",
            (Session.session_id, 5)).fetchone()[0]
        assert count > 0, (deck_guid, result)
        assert db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE session_id=? "
            "AND is_champion=1", (Session.session_id,)).fetchone()[0] == 0
    finally:
        db.close()


def test_matching_target_filter_pool_uses_target_as_source():
    """CreateTokenMatchingTarget draws from its typed filter pool.

    C# evaluates ``m_CardFilter`` against the resolved target card for the
    responsible player (``GetPotentialRandomCardTemplates``).  The native
    pool previously ignored the filter entirely and copied the target
    template, and the shared-shard filter could not read the projected shard
    lists at all.
    """
    import json
    from types import SimpleNamespace

    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.token_effects import _matching_template_candidates

    db = sqlite3.connect(SRC)
    try:
        # Source: a card that requires exactly one Diamond (Records shard
        # index 5 -> ECardShards.Diamond bit 64).
        source_guid = None
        for guid, threshold in db.execute(
                "SELECT guid, threshold_json FROM card_templates"):
            try:
                data = json.loads(threshold or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            values = data.get("values") or []
            if (len(values) == 6 and values[5] == 1 and
                    sum(int(count or 0) for count in values) == 1):
                source_guid = guid
                break
        assert source_guid, "no single-Diamond template in the seed"
        db.execute(
            "INSERT INTO game_cards (session_id,user_id,card_uid,"
            "template_guid,card_template_id,location,position,card_state,"
            "card_type,card_abilities,card_attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (987654, 5, 4242, source_guid, source_guid, "warzone", 0, 0,
             "Troop", "[]", 0))
        db.commit()
        context = SimpleNamespace(
            db=db, session=SimpleNamespace(session_id=987654),
            bstate={"resolving_owner_id": 5})

        # Iconoclasm: two random cards that share every shard with the
        # discarded card (ExactMatch).
        iconoclasm = DEFAULT_RECORD_STORE.get(
            "AbilityEffectTemplate", "5b5c3110-f2a7-c6b7-8154-6c79e280c13e")
        pool = _matching_template_candidates(
            context, iconoclasm.raw["m_CardFilter"], (),
            source_uid=4242, player=5)
        assert pool, "exact-shard filter produced no candidates"
        allowed = {"Troop", "BasicAction", "QuickAction", "Artifact",
                   "Constant", "Bane", "Choice", "Resource"}
        for guid in pool:
            values = json.loads(db.execute(
                "SELECT threshold_json FROM card_templates WHERE guid=?",
                (guid,)).fetchone()[0] or "{}").get("values") or []
            nonzero = [index for index, count in enumerate(values)
                       if int(count or 0) > 0]
            assert nonzero == [5], (guid, values)
            card_type = db.execute(
                "SELECT card_type FROM card_templates WHERE guid=?",
                (guid,)).fetchone()[0] or ""
            assert set(card_type.split("|")) & allowed, (guid, card_type)

        # Daarmak: a random card that shares no shard with the played card.
        daarmak = DEFAULT_RECORD_STORE.get(
            "AbilityEffectTemplate", "b2b79ac9-353d-fba8-c1e0-143498e3cb62")
        disjoint_pool = _matching_template_candidates(
            context, daarmak.raw["m_CardFilter"], (),
            source_uid=4242, player=5)
        assert disjoint_pool, "disjoint-shard filter produced no candidates"
        for guid in disjoint_pool:
            values = json.loads(db.execute(
                "SELECT threshold_json FROM card_templates WHERE guid=?",
                (guid,)).fetchone()[0] or "{}").get("values") or []
            if len(values) > 5:
                assert not int(values[5] or 0), (guid, values)
    finally:
        db.close()


def test_effect_inventory_has_no_unexpected_unregistered_types():
    db = sqlite3.connect(SRC)
    try:
        special = {
            "RandomizeVariableEffectTemplate",
            "RepeatingAbilityEffectTemplate",
            "SetCardIntegerVariableEffectTemplate",
        }
        unknown = {
            effect_type for effect_type, in db.execute(
                "SELECT DISTINCT effect_type FROM ability_effects")
            if effect_type not in NATIVE_EFFECTS and effect_type not in special
        }
        # Narrative conversations have an explicit continuation path; they
        # must not be silently mistaken for an ordinary card effect or fall
        # out of the registry without coverage.
        assert unknown == set(), sorted(unknown)
    finally:
        db.close()


def main():
    tests = [
        test_typed_conscript_filter,
        test_keyword_matching_uses_ability_metadata,
        test_load_player_deck_instantiates_typed_resources,
        test_matching_target_filter_pool_uses_target_as_source,
        test_effect_inventory_has_no_unexpected_unregistered_types,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
