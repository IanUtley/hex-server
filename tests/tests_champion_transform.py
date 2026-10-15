#!/usr/bin/env python3
"""The J-1 gnoll chain: a champion that loses transforms into the next one.

Starting from the Rabid Gnoll (Brutecrown J-1), each lose replacement sets the
champion's health, plays its encounter conversation, summons, and transforms
it into the next champion (Ravenous Gnolls, Raving Gnoll Cackle, King
Kazraga).  The conversation is finished the way the client finishes it: the
pending conversation is cleared and the ability resumes from its saved effect
offset (PendingChoicesMixin._resolve_pending_conversation).

    python3 tests/tests_champion_transform.py
"""

import io
import contextlib
import json
import os
import sqlite3
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests"),
                os.path.join(ROOT, "campaign")]

import tests_card_play_matrix as matrix  # noqa: E402
from rules_port.context import EffectContext  # noqa: E402
from rules_port.death_effects import champion_would_lose  # noqa: E402
from rules_port.resolution import resolve_port_ability  # noqa: E402

RABID_GNOLL = "02125548-e929-41eb-ae88-93f526519ed3"
RAVENOUS_GNOLLS = "0f356809-6c45-43ae-909c-f8c5ee25e2ef"
RAVING_CACKLE = "b3ac7c53-260f-4df1-9da1-b457e5181443"
KING_KAZRAGA = "6688c66f-9dcb-4139-bdda-398831b02019"
KAZRAGA_DECK_CARDS = 60  # DeckTemplate dbe04b5d: its stacks sum to 60


def _champion_abilities(guid):
    src = sqlite3.connect(os.path.join(ROOT, "hconnect.db"))
    try:
        return [row[0] for row in src.execute(
            "SELECT ability_guid FROM champion_abilities WHERE champion_guid=? "
            "ORDER BY ability_guid", (guid,))]
    finally:
        src.close()


def _seat_rabid_gnoll(db):
    db.execute("DELETE FROM game_cards")
    db.execute(
        "INSERT INTO game_cards (session_id,user_id,card_uid,template_guid,card_template_id,"
        "location,position,card_state,card_abilities,card_type,card_attributes,card_attack_mod,"
        "card_defense_mod,card_cost_mod,card_damage,permanent_buffs,temporary_buffs,card_uses,"
        "resolved_at,original_template_guid,is_champion) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (matrix.SESSION_ID, matrix.AI_OWNER, matrix.AI_CHAMP, RABID_GNOLL, RABID_GNOLL,
         "warzone", 0, 0, json.dumps(_champion_abilities(RABID_GNOLL)), "Champion",
         0, 0, 0, 0, 0, "{}", "{}", 0, 0, RABID_GNOLL, 1))
    db.commit()


def _champion_row(db):
    return db.execute(
        "SELECT template_guid, card_type, card_abilities FROM game_cards WHERE card_uid=?",
        (matrix.AI_CHAMP,)).fetchone()


def _lose_and_convert(handler, session, db, state):
    """One lose: the replacement runs, then the conversation is finished."""
    state["ai_health"] = 0
    state["_champion_would_lose_seen"] = []
    state.pop("_champion_would_lose_replacement_accepted", None)
    with contextlib.redirect_stdout(io.StringIO()):
        game = handler._fresh_game(session, matrix.PLAYER_UID, matrix.AI_UID, state)
        champion_would_lose(EffectContext.from_rules_port(
            game, session, db, handler, matrix.PLAYER_UID, matrix.AI_UID, state, "",
            ability=None), 0)
    pending = state.pop("pending_conversation", None)
    state.pop("resolution_paused", None)
    if pending is None:
        return None
    with contextlib.redirect_stdout(io.StringIO()):
        game = handler._fresh_game(session, matrix.PLAYER_UID, matrix.AI_UID, state)
        resolve_port_ability(
            handler, game, session, db, matrix.PLAYER_UID, matrix.AI_UID, state,
            pending.get("ability_guid", ""), pending.get("source_uid"),
            int(pending.get("ability_owner_id", 0) or 0),
            target_map={int(k): v for k, v in (pending.get("target_map") or {}).items()},
            variables=pending.get("variables") or {},
            resume_from_order=int(pending.get("resume_effect_order", 0)))
    return pending


def test_j1_gnoll_chain_transforms_through_four_champions():
    db = sqlite3.connect(matrix.SRC)
    db.row_factory = sqlite3.Row
    _seat_rabid_gnoll(db)
    handler = matrix.HandlerStub(db)
    session = matrix.SessionStub()
    state = matrix._new_state(handler)
    handler._ai_champ_scid = types.SimpleNamespace(uid=types.SimpleNamespace(uid64=matrix.AI_CHAMP))
    handler._player_champ_scid = types.SimpleNamespace(uid=types.SimpleNamespace(uid64=matrix.PLAYER_CHAMP))
    state["resolving_owner_id"] = matrix.AI_OWNER
    player_health = state["player_health"]

    expected = [
        (RAVENOUS_GNOLLS, 15),   # Rabid Gnoll -> Ravenous Gnolls
        (RAVING_CACKLE, 30),     # Ravenous Gnolls -> Raving Gnoll Cackle
        (KING_KAZRAGA, 60),      # Raving Gnoll Cackle -> King Kazraga
    ]
    for stage, (guid, health) in enumerate(expected, start=1):
        pending = _lose_and_convert(handler, session, db, state)
        assert pending is not None, f"stage {stage}: the lose replacement did not start a conversation"
        template, card_type, abilities = _champion_row(db)
        assert template.lower() == guid, f"stage {stage}: champion is {template}, expected {guid}"
        assert card_type == "Champion", f"stage {stage}: card type {card_type}"
        assert state["ai_health"] == health, f"stage {stage}: health {state['ai_health']}, expected {health}"
        assert state["player_health"] == player_health, "the player's health changed"
    src = sqlite3.connect(os.path.join(ROOT, "hconnect.db"))
    card_names = {row[0].lower(): row[1] for row in src.execute("SELECT guid, name FROM card_templates")}
    src.close()
    on_board = {card_names.get(row[0].lower()) for row in db.execute(
        "SELECT template_guid FROM game_cards WHERE session_id=? AND location='warzone'",
        (matrix.SESSION_ID,))}
    assert {"Burgg", "Zagg"} <= on_board, f"the Cackle's princesses are not on the board: {sorted(n for n in on_board if n)}"

    # King Kazraga has no lose replacement: the next lose ends the battle.
    state["ai_health"] = 0
    state["_champion_would_lose_seen"] = []
    with contextlib.redirect_stdout(io.StringIO()):
        game = handler._fresh_game(session, matrix.PLAYER_UID, matrix.AI_UID, state)
        champion_would_lose(EffectContext.from_rules_port(
            game, session, db, handler, matrix.PLAYER_UID, matrix.AI_UID, state, "",
            ability=None), 0)
    assert state.get("pending_conversation") is None, "King Kazraga should not convert again"

    deck_total = db.execute(
        "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND location IN ('deck','hand')",
        (matrix.SESSION_ID,)).fetchone()[0]
    assert deck_total == KAZRAGA_DECK_CARDS, f"deck and hand hold {deck_total} cards, expected {KAZRAGA_DECK_CARDS}"


if __name__ == "__main__":
    test_j1_gnoll_chain_transforms_through_four_champions()
    print("PASS test_j1_gnoll_chain_transforms_through_four_champions")
