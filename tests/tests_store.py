"""Regression tests for the store booster -> Primal pack upgrade (2% per pack
at purchase, data-driven via pack_set_map)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import hconnect_server
from db import db_primal_pack_for


def run(name, fn):
    try:
        fn()
        print(f"PASS {name}")
    except AssertionError as e:
        print(f"FAIL {name}: {e}")
    except Exception as e:
        import traceback
        print(f"ERROR {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


def test_roll_always_upgrades():
    assert hconnect_server._roll_primal_upgrade(3, rng=lambda: 0.0) == (0, 3)


def test_roll_never_upgrades():
    assert hconnect_server._roll_primal_upgrade(3, rng=lambda: 0.5) == (3, 0)
    assert hconnect_server._roll_primal_upgrade(0) == (0, 0)


def test_roll_just_under_threshold_upgrades():
    # 0.019 < 0.02 -> every pack upgrades.
    assert hconnect_server._roll_primal_upgrade(4, rng=lambda: 0.019) == (0, 4)


def test_roll_mixed_quantity():
    seq = iter([0.5, 0.01])
    assert hconnect_server._roll_primal_upgrade(2, rng=lambda: next(seq)) == (1, 1)


def test_primal_mapping_core_sets():
    # Set 1 booster -> Primal Pack: Set 1
    assert db_primal_pack_for(
        "a8b78207-686a-4994-b6cd-4548d1349841"
    ) == "8d20082a-4163-4f42-8fce-d4c056f9da04"
    # Set 2 booster -> Primal Pack: Set 2
    assert db_primal_pack_for(
        "f37fc1bc-4d7b-4bab-a2a2-770957d9a7b1"
    ) == "653f153b-8288-4ece-a304-2804c1e2ffb9"


def test_no_primal_for_later_sets_or_primals():
    # Sets 5-9 have no Primal version -> no upgrade.
    assert db_primal_pack_for(
        "84c65d9b-779b-4128-879d-b0779e6e6edc") is None  # Set 5
    assert db_primal_pack_for(
        "3dacd91b-84f1-4ff9-99df-dae3f7740702") is None  # Set 9
    # Purchasing a Primal pack itself never re-upgrades.
    assert db_primal_pack_for(
        "8d20082a-4163-4f42-8fce-d4c056f9da04") is None


def test_full_set_excludes_alternate_art_rarities():
    cards = [
        ("common", "Common card", "Common", 1, 1, 1, 0, 0, "Troop"),
        ("rare", "Rare card", "Rare", 2, 2, 2, 0, 0, "Troop"),
        ("epic", "Alternate art", "Epic", 2, 2, 2, 0, 0, "Troop"),
        ("promo", "Promo card", "Promo", 2, 2, 2, 0, 0, "Troop"),
    ]
    selected = hconnect_server._full_set_pool(cards)
    assert [card[0] for card in selected] == ["common", "rare"]


def test_booster_stays_in_requested_set_and_excludes_generated_lands():
    cards = {
        "set-a": [
            ("a-common", "A common", "Common", 1, 1, 1, 0, 0, "Troop"),
            ("a-uncommon", "A uncommon", "Uncommon", 2, 2, 2, 0, 0, "Troop"),
            ("a-rare", "A rare", "Rare", 3, 3, 3, 0, 0, "Troop"),
            ("a-land", "Generated land", "Land", 0, 0, 0, 0, 0, "Resource"),
        ],
        "set-b": [
            ("b-common", "B common", "Common", 1, 1, 1, 0, 0, "Troop"),
        ],
    }
    opened = hconnect_server._generate_booster(cards, "set-a")
    assert opened
    assert all(card[0].startswith("a-") for card in opened)
    assert all(card[0] != "a-land" for card in opened)


def test_pack_map_uses_canonical_core_set_guids():
    expected = {
        "a8b78207-686a-4994-b6cd-4548d1349841": "0382f729-7710-432b-b761-13677982dcd2",
        "f37fc1bc-4d7b-4bab-a2a2-770957d9a7b1": "b05e69d2-299a-4eed-ac31-3f1b4fa36470",
        "237866c1-aea2-4cb4-89ca-418babda3595": "fce480eb-15f9-4096-8d12-6beee9118652",
        "a8e324e3-b9fb-4bb6-b659-f2773982aed2": "2d05262c-d7a0-408f-a280-36d206a29344",
        "84c65d9b-779b-4128-879d-b0779e6e6edc": "ecdbc188-5750-48ef-acac-05e2bcbcc46f",
        "63273f9b-5f4d-4db7-a418-fc6e2c4c9900": "fbbac856-2264-4d31-97b0-0d8a646b9597",
        "df144885-0fb5-4238-942c-79b35870dabc": "326602fa-e183-4dfe-8300-55cc0c7c4ce8",
        "902193e6-645b-41be-ac51-23196335b788": "9a824393-cd11-4273-a05e-41e35eb50dbe",
        "3dacd91b-84f1-4ff9-99df-dae3f7740702": "54f14f51-2afe-4a26-be28-d251b06a9cc4",
    }
    actual = dict(hconnect_server._db.execute(
        "SELECT pack_guid, set_guid FROM pack_set_map WHERE is_full_set=0 AND is_primal=0"
    ).fetchall())
    assert {guid: actual[guid] for guid in expected} == expected


def test_full_set_pools_use_the_mapped_core_sets():
    expected = {
        "5e338ab1-da47-41ce-b980-4020f1b5b4fc": ("0382f729-7710-432b-b761-13677982dcd2", 341),
        "17e0a0ff-7ec3-4ed7-a261-adb4dcfd6625": ("b05e69d2-299a-4eed-ac31-3f1b4fa36470", 248),
        "3bdc41b5-c265-4616-812e-9f1965787c33": ("fce480eb-15f9-4096-8d12-6beee9118652", 250),
        "5bdb9ea1-9e03-42af-a23c-6968fff55ce5": ("2d05262c-d7a0-408f-a280-36d206a29344", 250),
        "37fb3559-6f2e-4f53-b6d7-05c28c6c075d": ("ecdbc188-5750-48ef-acac-05e2bcbcc46f", 275),
    }
    rows = hconnect_server._db.execute(
        "SELECT pack_guid, set_guid FROM pack_set_map WHERE is_full_set=1"
    ).fetchall()
    actual = dict(rows)
    cards = hconnect_server._load_card_templates()
    assert {
        pack_guid: (actual[pack_guid], len(hconnect_server._full_set_pool(cards[actual[pack_guid]])))
        for pack_guid in expected
    } == expected


if __name__ == "__main__":
    run("roll always upgrades", test_roll_always_upgrades)
    run("roll never upgrades", test_roll_never_upgrades)
    run("roll just under threshold", test_roll_just_under_threshold_upgrades)
    run("roll mixed quantity", test_roll_mixed_quantity)
    run("primal mapping for core sets", test_primal_mapping_core_sets)
    run("no primal for later sets/primals", test_no_primal_for_later_sets_or_primals)
    run("full set excludes alternate art rarities", test_full_set_excludes_alternate_art_rarities)
    run("booster stays in requested set", test_booster_stays_in_requested_set_and_excludes_generated_lands)
    run("pack map uses canonical set GUIDs", test_pack_map_uses_canonical_core_set_guids)
    run("full sets use mapped core sets", test_full_set_pools_use_the_mapped_core_sets)
