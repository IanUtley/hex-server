"""Saved ProfileDeckTemplates are stored relationally and regenerated.

pdecktsave decodes the client's ProfileDeckTemplate bytes into
player_deck_templates / player_deck_template_cards; the login list rebuilds
the bytes from those columns.
"""

import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
from services import deck_templates as dt

CARD_A = "11111111-2222-3333-4444-555555555555"
CARD_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHAMPION = "8a336fec-d970-4613-9308-c0afd41b62eb"
SLEEVE = "0a8400f1-ebd6-458c-8770-872ac49513e4"
GAMEBOARD = "10b0e374-d304-4dad-b493-7ba4493c76be"


def _template(name, cards):
    return {"name": name, "champion_guid": CHAMPION, "sleeve_guid": SLEEVE,
            "equipment": {"Gloves": CARD_B}, "cards": cards,
            "extended": {"gboard": GAMEBOARD, "personality": "Aggro"}}


def _card(guid, count, gems=(), reserves=False, foil=False):
    return {"guid": guid, "count": count, "reserves": reserves,
            "extended": False, "foil": foil, "gems": list(gems)}


def test_template_round_trips_through_columns():
    user_id = 9701
    db._db.execute("INSERT INTO users (id, name, gold, platinum) VALUES (?,?,0,0)",
                   (user_id, "DeckTemplateTester"))
    db._db.commit()
    assert db._db.execute(
        "SELECT COUNT(*) FROM inventory_item_templates "
        "WHERE item_type IN ('DeckSleeve','Gameboard')").fetchone()[0] > 0

    template = _template("Gems and Foil", [
        _card(CARD_A, 3, gems=[5]),
        _card(CARD_A, 1, reserves=True, foil=True),
        _card(CARD_B, 2, foil=True)])
    data = dt.encode_template(template)
    dt.handle_profile_action(db._db, user_id, {
        "action": "pdecktsave", "DeckTemplateID": 0,
        "Template": base64.b64encode(data).decode()})
    template_id = db._db.execute(
        "SELECT id FROM player_deck_templates WHERE user_id=?",
        (user_id,)).fetchone()[0]

    columns = db._db.execute(
        "SELECT name, champion_guid, sleeve_guid, gameboard_guid, personality, "
        "equipment_json FROM player_deck_templates WHERE id=?",
        (template_id,)).fetchone()
    assert tuple(columns) == ("Gems and Foil", CHAMPION, SLEEVE, GAMEBOARD,
                              "Aggro", json.dumps({"Gloves": CARD_B}))
    cards = {row[0]: row[1:] for row in db._db.execute(
        "SELECT card_guid, quantity, gem_types_list_json, flags_json "
        "FROM player_deck_template_cards WHERE template_id=?", (template_id,))}
    assert cards[CARD_A][:2] == (4, "[[5], [5], [5], []]")
    assert json.loads(cards[CARD_A][2])["copies"][3] == {
        "extended": False, "reserves": True, "foil": True}
    assert cards[CARD_B] == (2, "[]", json.dumps(
        {"extended": False, "foil": True, "reserves": False}))

    # The login list regenerates identical bytes; the merc deck skips reserves.
    assert dt.list_templates(db._db, user_id) == [(template_id, "Gems and Foil", data)]
    stored = dt.get_template(db._db, user_id, template_id)
    assert dt.main_deck_cards(stored) == [CARD_A] * 3 + [CARD_B] * 2

    # Saving over the id replaces the card rows; deleting removes both tables.
    dt.save_template(db._db, user_id, template_id,
                     dt.encode_template(_template("Renamed", [_card(CARD_B, 1)])))
    assert [row["card_guid"] for row in
            dt.get_template(db._db, user_id, template_id)["cards"]] == [CARD_B]
    dt.handle_profile_action(db._db, user_id, {
        "action": "pdeckdel", "DeckTemplateID": template_id})
    assert dt.get_template(db._db, user_id, template_id) is None
    assert not db._db.execute(
        "SELECT 1 FROM player_deck_template_cards WHERE template_id=?",
        (template_id,)).fetchone()
