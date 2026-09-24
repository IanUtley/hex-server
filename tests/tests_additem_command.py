"""The !additem developer command grants inventory items by name."""

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


if __name__ == "__main__":
    run("adds a mercenary by exact name", test_adds_mercenary_by_exact_name)
    run("quantity and partial name", test_quantity_and_partial_name)
    run("unknown item", test_unknown_item)
    if FAILURES:
        sys.exit(1)
