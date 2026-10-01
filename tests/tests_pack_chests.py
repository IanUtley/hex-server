"""Opening booster packs awards one treasure chest per pack."""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
# The OpenCardPack log line prints "→", which a Windows console can't encode.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
import hconnect_server
from profile_db import db_add_inventory, db_get_unopened_chests_full

SET1 = "0382f729-7710-432b-b761-13677982dcd2"
SET1_BOOSTER = "a8b78207-686a-4994-b6cd-4548d1349841"
SET1_PRIMAL_PACK = "8d20082a-4163-4f42-8fce-d4c056f9da04"
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


class _PackHandler(hconnect_server.HCPHandler):
    """Drives the real OpenCardPack handler with the transport replaced."""

    def __init__(self, user_id):
        self.user_profile = {"id": user_id}
        self.client_uid = 1
        self.scnt = 0
        self.sid = "test"
        self.sent = []

    def send(self, headers, body=b""):
        self.sent.append(body)

    def push_inventory_to_client(self, qty=1, template_guid="", item_id=1001):
        pass


def _open_packs(user_id, pack_guid, amount):
    db._db.execute("INSERT INTO users (id, name) VALUES (?,?)",
                   (user_id, f"PackTester{user_id}"))
    db_add_inventory(user_id, pack_guid, amount, conn=db._db)
    db._db.commit()
    handler = _PackHandler(user_id)
    item_id = f"ItemId;1;1;1;m_Guid;2;2;0;36;{pack_guid}".encode()
    handler._handle_service_request_legacy(
        "t", "i", 2127, 2, 0, "00000000-0000-0000-0000-000000000000", 0,
        {"ItemId": "{}", "OpenAmount": amount}, item_id)
    assert handler.sent, "no response sent"
    body = handler.sent[-1]
    listed = re.search(rb"NewChestInstances;\d+;\d+;0;(\d+);", body)
    assert listed, "NewChestInstances missing from the response"
    rows = db_get_unopened_chests_full(user_id, conn=db._db)
    return int(listed.group(1)), [(row[1], row[2]) for row in rows]


def test_each_booster_pack_awards_a_chest():
    listed, chests = _open_packs(9501, SET1_BOOSTER, 5)
    assert listed == 5, listed
    assert len(chests) == 5, chests
    assert all(set_guid == SET1 for set_guid, _ in chests), chests


def test_primal_packs_award_legendary_chests():
    listed, chests = _open_packs(9502, SET1_PRIMAL_PACK, 2)
    assert listed == 2, listed
    assert [rarity for _, rarity in chests] == ["Legendary", "Legendary"], chests


def test_chest_rarities_follow_the_table():
    import random
    rarities = hconnect_server._roll_pack_chest_rarities(4000, rng=random.Random(3))
    assert len(rarities) == 4000
    assert 0.77 < rarities.count("Common") / 4000 < 0.83, rarities.count("Common")
    assert hconnect_server._roll_pack_chest_rarities(3, is_primal=True) == ["Legendary"] * 3


if __name__ == "__main__":
    run("each booster pack awards a chest", test_each_booster_pack_awards_a_chest)
    run("Primal Packs award Legendary chests", test_primal_packs_award_legendary_chests)
    run("chest rarities follow the table", test_chest_rarities_follow_the_table)
    if FAILURES:
        sys.exit(1)
