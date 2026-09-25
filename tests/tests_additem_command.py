"""Developer test commands: !additem, !partycap, and !addchest."""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
import commands
from profile_db import db_inventory_item

FAILURES = []


def run(name, fn):
    try:
        fn()
        print(f"PASS {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"FAIL {name}: {e}")
    except Exception as e:
        import traceback
        FAILURES.append(name)
        print(f"ERROR {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


def _handler(user_id):
    db._db.execute("INSERT INTO users (id, name) VALUES (?,?)",
                   (user_id, f"AddItemTester{user_id}"))
    db._db.commit()
    pushes = []
    return SimpleNamespace(
        user_profile={"id": user_id}, pushes=pushes,
        push_inventory_to_client=lambda qty, template_guid, item_id:
            pushes.append((template_guid, item_id, qty)))


def _command(handler, text):
    old = commands.hconnect_server.PROFILE_FEATURE_FLAGS
    commands.hconnect_server.PROFILE_FEATURE_FLAGS = ("allowcon",)
    try:
        return commands.handle_command(handler, text, "", "")
    finally:
        commands.hconnect_server.PROFILE_FEATURE_FLAGS = old


def test_adds_mercenary_by_exact_name():
    handler = _handler(9301)
    result = _command(handler, "!additem Scabtongue")
    assert result.startswith("Added 1x Scabtongue (Mercenaries)"), result
    guid, uid, qty = handler.pushes[-1]
    row = db_inventory_item(9301, guid)
    assert row is not None and int(row[1]) == 1 and qty == 1 and uid > 0


def test_quantity_and_partial_name():
    handler = _handler(9302)
    result = _command(handler, "!additem voltaic handguards x3")
    assert "you now have 3" in result, result
    assert "items match" in _command(handler, "!additem Kismet Merc")


def test_unknown_item():
    assert "No item named" in _command(_handler(9303), "!additem Not A Real Thing")


def test_forgiving_names_from_real_attempts():
    handler = _handler(9304)
    result = _command(handler, "!additem Bebo")
    assert result.startswith("Added 1x B.E.B.O."), result
    assert "Did you mean: Mooof" in _command(handler, "!additem Moof")
    assert "Glorfenblort" in _command(handler, "!additem Glorfenbort")
    assert "is a card, not an inventory item" in _command(handler, "!additem extinction")


def test_whispered_command_runs():
    import json
    from services import chat
    handler = _handler(9305)
    sent = []
    handler.scnt = 0
    handler.sid = "test"
    handler.send = lambda headers, body=b"": sent.append(json.loads(body))
    handler._handle_chat_command = lambda text, room, user: _command(handler, "!" + text)
    chat.handle_whisper_message(handler, json.dumps(
        {"target": None, "flags": "", "msg": "!additem Scabtongue"}).encode())
    assert sent and sent[-1]["room"] == "general", sent
    assert sent[-1]["msg"].startswith("Added 1x Scabtongue"), sent[-1]
    chat.handle_whisper_message(handler, b'{"target": "Bob", "msg": "hello"}')
    assert len(sent) == 1


def test_partycap_sets_flag():
    from services import mercenaries
    handler = _handler(9306)
    result = _command(handler, "!partycap 3")
    assert "set to 3" in result, result
    assert mercenaries.get_flags(db._db, 9306) == [("CAMP_PARTYCAP", 3, 4, 0)]
    assert "Usage" in _command(handler, "!partycap")


def test_addchest_creates_set_chests():
    from profile_db import db_get_unopened_chests_full
    handler = _handler(9307)
    result = _command(handler, "!addchest primal 2 x3")
    assert result.startswith("Added 3x Primal Shattered Destiny chest"), result
    assert "Added 1x Rare Shards of Fate" in _command(handler, "!addchest Rare")
    assert "Herofall" in _command(handler, "!addchest legendary herofall")
    rows = db_get_unopened_chests_full(9307, conn=db._db)
    chests = sorted((row[1], row[2]) for row in rows)
    assert chests == sorted(
        [("b05e69d2-299a-4eed-ac31-3f1b4fa36470", "Primal")] * 3
        + [("0382f729-7710-432b-b761-13677982dcd2", "Rare"),
           ("ecdbc188-5750-48ef-acac-05e2bcbcc46f", "Legendary")]), chests
    assert "Usage" in _command(handler, "!addchest shiny")
    assert "Unknown set" in _command(handler, "!addchest rare 42")


if __name__ == "__main__":
    run("adds a mercenary by exact name", test_adds_mercenary_by_exact_name)
    run("quantity and partial name", test_quantity_and_partial_name)
    run("unknown item", test_unknown_item)
    run("forgiving names from real attempts", test_forgiving_names_from_real_attempts)
    run("whispered command runs", test_whispered_command_runs)
    run("!partycap sets flag", test_partycap_sets_flag)
    run("!addchest creates set chests", test_addchest_creates_set_chests)
    if FAILURES:
        sys.exit(1)
