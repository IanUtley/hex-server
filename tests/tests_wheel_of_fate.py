"""Wheels of Fate: spinning booster treasure chests."""

import os
import random
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
import hconnect_server
from profile_db import (db_create_treasure_chest, db_get_unopened_chests_full,
                        db_get_user_currency)
from services import wheel_of_fate as wof
from services.chest_loot import SET1, pve_card_pool

SET3 = "fce480eb-15f9-4096-8d12-6beee9118652"
SET4 = "2d05262c-d7a0-408f-a280-36d206a29344"
DOOMBRINGER = "54f14f51-2afe-4a26-be28-d251b06a9cc4"
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


def _new_user(user_id, gold):
    db._db.execute("INSERT INTO users (id, name, gold, platinum) VALUES (?,?,?,0)",
                   (user_id, f"WheelTester{user_id}", gold))
    db._db.commit()
    return user_id


def _templates():
    return hconnect_server._load_card_templates()


def _item_name(guid):
    from gamedata import DEFAULT_RECORD_STORE
    return DEFAULT_RECORD_STORE.get("InventoryItemData", guid).field("m_Name")


def _equipment_name(guid):
    return db._db.execute("SELECT name FROM equipment_templates WHERE guid=?",
                          (guid,)).fetchone()[0]


def _only(outcome):
    """Patch the outcome table so every spin lands on one outcome, no colors."""
    table = {name: (1 if name == outcome else 0,) + row[1:]
             for name, row in wof.OUTCOMES.items()}
    return [mock.patch.dict(wof.OUTCOMES, table),
            mock.patch.object(wof, "RED_CHANCE", 0.0),
            mock.patch.object(wof, "GOLD_CHANCE", 0.0)]


class _Patched:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for patch in self.patches:
            patch.__enter__()

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.__exit__(*exc)


def _client_payout(symbols, colors):
    """The payout rows the client lights (UIPackListViewModel.SetPayoutHighlights)."""
    rows = set()
    if all(c == 1 for c in colors):
        rows.add("triple_red")
    if any(c == 2 for c in colors):
        rows.add("gold")
    eyes = symbols.count(4)
    rows.add({3: "triple_eye", 2: "double_eye", 1: "single_eye"}.get(eyes, ""))
    if eyes <= 0 and len(set(symbols)) == 1:
        rows.add({0: "star", 1: "crown", 2: "hand", 3: "moon", 5: "mushroom",
                  6: "skull", 7: "heart", 9: "spider"}[symbols[0]])
    rows.discard("")
    return rows


EXPECTED_ROW = {
    "fail": None, "paid_spin": "single_eye", "free_spin": "double_eye",
    "upgrade": "triple_eye", "upgrade_paid": "hand", "upgrade_twice": "heart",
    "mercenary": "star", "equipment": "crown", "pve_card": "moon",
    "sleeve": "mushroom", "aa_card": "skull", "pvp_card": "spider",
}


def test_set1_wheel_prizes_match_the_wiki():
    pools = wof.prize_pools(db._db, _templates(), SET1)
    assert [[_item_name(g) for g in tier] for tier in pools["mercenary"]] == [
        ["Ashahsa"], ["Xorak, the Flamehand"], ["Puck, the Dreambringer"]], pools["mercenary"]
    assert [[c[1] for c in tier] for tier in pools["pve_card"]] == [
        ["Lightning Elemental"], ["Water Elemental"], ["Air Elemental"]]
    assert [sorted(_equipment_name(g) for g in tier) for tier in pools["equipment"]] == [
        ["Aqua Mask", "North Wind Chimes", "Sky Walkers"], ["Electric Flail"],
        ["Bubble Mail", "Spark Mitts"]], pools["equipment"]
    assert [[c[1] for c in tier] for tier in pools["aa_card"]] == [
        ["Windbourne Acolyte"], ["Veteran Gladiator"], ["Wrathwood Colossus"]]
    assert all(c[2] == "Epic" for tier in pools["aa_card"] for c in tier)
    assert [[_item_name(g) for g in tier] for tier in pools["sleeve"]] == [
        ["Windbourne Acolyte Sleeve"], ["Veteran Gladiator Sleeve"],
        ["Wrathwood Colossus Sleeve"]]
    assert set(pools["pvp_card"]) == {"Rare", "Legendary"}


def test_later_sets_have_wheel_prizes():
    templates = _templates()
    for set_guid, cards in wof.WOF_PVE_CARDS.items():
        pools = wof.prize_pools(db._db, templates, set_guid)
        assert [tier[0][1] for tier in pools["pve_card"]] == list(cards), set_guid
        # One item per Wheel card at least (Set 8's come in two rarities).
        assert sum(map(len, pools["equipment"])) >= 3, (set_guid, pools["equipment"])
        assert len(pools["equipment"]) >= 2, (set_guid, pools["equipment"])
        assert len(pools["mercenary"]) >= 1, set_guid
    doombringer = wof.prize_pools(db._db, templates, DOOMBRINGER)
    assert "pve_card" not in doombringer and doombringer["mercenary"]


def test_wheel_pve_cards_are_not_chest_prizes():
    templates = _templates()
    for set_guid in (SET3, SET4):
        names = {c[1] for c in pve_card_pool(templates, set_guid, "any")}
        assert names.isdisjoint(wof.WOF_PVE_CARD_NAMES), (set_guid, names)
        assert names, set_guid


def test_reels_show_the_rolled_outcome():
    rng = random.Random(5)
    seen = set()
    for _ in range(4000):
        result = wof.roll_spin(db._db, _templates(), SET1, "Common", rng)
        rows = _client_payout(result.symbols, result.colors)
        expected = EXPECTED_ROW[result.outcome]
        symbol_rows = rows - {"gold", "triple_red"}
        assert symbol_rows == ({expected} if expected else set()), (result, rows)
        prizes = len(result.cards) + sum(
            1 for _, kind in result.inventory_rewards if kind != "booster")
        assert prizes == (1 if result.outcome in wof.PRIZE_OUTCOMES else 0), result
        exclusive = result.colors.count(2) == 3 or result.colors.count(1) == 3
        boosters = [g for g, kind in result.inventory_rewards if kind == "booster"]
        assert len(boosters) == (1 if exclusive else 0), result
        assert result.gold == {1: 500, 2: 2500}.get(result.colors.count(2), 0)
        seen.add(result.outcome)
    assert seen == set(wof.OUTCOMES), seen


def test_outcome_odds_follow_the_survey():
    rng = random.Random(6)
    counts = {}
    spins = 20000
    for _ in range(spins):
        outcome = wof.roll_spin(db._db, _templates(), SET1, "Rare", rng).outcome
        counts[outcome] = counts.get(outcome, 0) + 1
    # Survey: 958 of 2,167 spins failed; 280 were a paid re-spin only.
    assert 0.42 < counts["fail"] / spins < 0.46, counts
    assert 0.115 < counts["paid_spin"] / spins < 0.145, counts


def test_upgrades_cap_at_primal():
    rng = random.Random(1)
    with _Patched(_only("upgrade_twice")):
        assert wof.roll_spin(db._db, _templates(), SET1, "Common", rng).rarity == "Rare"
        assert wof.roll_spin(db._db, _templates(), SET1, "Legendary", rng).rarity == "Primal"
    with _Patched(_only("upgrade")):
        result = wof.roll_spin(db._db, _templates(), SET1, "Rare", rng)
        assert (result.rarity, result.spin_status) == ("Legendary", wof.NO_SPIN)


def _handler(user_id):
    from types import SimpleNamespace
    return SimpleNamespace(user_profile={"id": user_id})


def _chest_row(chest_db_id):
    return db._db.execute(
        "SELECT chest_rarity, opened, wof_spun, wof_status FROM treasure_chests "
        "WHERE id=?", (chest_db_id,)).fetchone()


def test_spin_costs_gold_and_keeps_the_chest():
    user_id = _new_user(9401, 5000)
    chest_db_id = db_create_treasure_chest(user_id, SET1, "Common", conn=db._db)
    handler = _handler(user_id)
    with _Patched(_only("paid_spin")):
        spin = hconnect_server._spin_client_chest(handler, 9000 + chest_db_id)
    assert spin["error"] == wof.OK and spin["spin_status"] == wof.PAID_SPIN, spin
    assert db_get_user_currency(user_id, "gold", conn=db._db) == 3800
    assert tuple(_chest_row(chest_db_id)) == ("Common", 0, 1, wof.PAID_SPIN)
    # The re-spin survives a relog: the login chest list carries it.
    row = [r for r in db_get_unopened_chests_full(user_id, conn=db._db)
           if r[0] == chest_db_id][0]
    assert row[4] == wof.PAID_SPIN

    with _Patched(_only("fail")):
        spin = hconnect_server._spin_client_chest(handler, 9000 + chest_db_id)
    assert spin["error"] == wof.OK and spin["spin_status"] == wof.NO_SPIN
    assert db_get_user_currency(user_id, "gold", conn=db._db) == 2600
    spin = hconnect_server._spin_client_chest(handler, 9000 + chest_db_id)
    assert spin["error"] == wof.NO_SPIN_LEFT, spin
    assert db_get_user_currency(user_id, "gold", conn=db._db) == 2600
    # The chest can still be opened afterwards.
    summary = hconnect_server._open_client_chests(handler, [9000 + chest_db_id])
    assert summary["invalid"] == [] and summary["opened"], summary


def test_free_spins_and_primal_chests_cost_nothing():
    user_id = _new_user(9402, 0)
    handler = _handler(user_id)
    primal = db_create_treasure_chest(user_id, SET1, "Primal", conn=db._db)
    with _Patched(_only("free_spin")):
        spin = hconnect_server._spin_client_chest(handler, 9000 + primal)
    assert spin["error"] == wof.OK and spin["spin_status"] == wof.FREE_SPIN
    rare = db_create_treasure_chest(user_id, SET1, "Rare", conn=db._db)
    spin = hconnect_server._spin_client_chest(handler, 9000 + rare)
    assert spin["error"] == wof.NOT_ENOUGH_GOLD, spin
    db._db.execute("UPDATE treasure_chests SET wof_spun=1, wof_status=? WHERE id=?",
                   (wof.FREE_SPIN, rare))
    with _Patched(_only("upgrade_twice")):
        spin = hconnect_server._spin_client_chest(handler, 9000 + rare)
    assert spin["error"] == wof.OK and spin["rarity"] == "Primal", spin
    assert db_get_user_currency(user_id, "gold", conn=db._db) == 0
    assert _chest_row(rare)[0] == "Primal"


def test_spin_prizes_reach_the_collection():
    user_id = _new_user(9403, 100000)
    handler = _handler(user_id)
    for outcome in ("mercenary", "pve_card", "sleeve"):
        chest_db_id = db_create_treasure_chest(user_id, SET1, "Legendary", conn=db._db)
        with _Patched(_only(outcome)):
            spin = hconnect_server._spin_client_chest(handler, 9000 + chest_db_id)
        assert spin["error"] == wof.OK, spin
        if outcome == "pve_card":
            assert len(spin["cards"]) == 1 and spin["inventory_updates"] == []
        else:
            assert len(spin["inventory_updates"]) == 1 and spin["cards"] == []


class _SpinHandler(hconnect_server.HCPHandler):
    """Drives the real SpinWheelOfFate handler with the transport replaced."""

    def __init__(self, user_id):
        self.user_profile = {"id": user_id}
        self.client_uid = 1
        self.scnt = 0
        self.sid = "test"
        self.sent = []
        self.inventory_pushes = []
        self.card_chunks = []

    def send(self, headers, body=b""):
        self.sent.append(body)

    def push_inventory_to_client(self, qty=1, template_guid="", item_id=1001):
        self.inventory_pushes.append((template_guid, item_id, qty))

    def _send_cards_chunk(self, cards):
        self.card_chunks.append(cards)

    def _send_inventory_updated(self, *a, **k):
        pass


def _spin_request(handler, chest_db_id):
    handler._handle_service_request_legacy(
        "t", "i", 2049, 2, 0, "00000000-0000-0000-0000-000000000000", 0,
        {"ChestID": str(9000 + chest_db_id)}, b"")
    assert handler.sent, "no response sent"
    return handler.sent[-1]


def test_spin_request_reports_prizes():
    user_id = _new_user(9404, 100000)
    handler = _SpinHandler(user_id)
    chest_db_id = db_create_treasure_chest(user_id, SET1, "Rare", conn=db._db)
    with _Patched(_only("mercenary")):
        body = _spin_request(handler, chest_db_id)
    pushed = [guid for guid, _, _ in handler.inventory_pushes]
    assert len(pushed) == 1 and b"RewardItems" in body and pushed[0].encode() in body
    assert b"SpinEntrySymbols" in body and not handler.card_chunks

    chest_db_id = db_create_treasure_chest(user_id, SET1, "Rare", conn=db._db)
    with _Patched(_only("aa_card")):
        body = _spin_request(handler, chest_db_id)
    assert len(handler.card_chunks) == 1 and len(handler.card_chunks[0]) == 1
    assert handler.card_chunks[0][0][0].encode() in body


if __name__ == "__main__":
    run("Set 1 Wheel prizes match the wiki", test_set1_wheel_prizes_match_the_wiki)
    run("later sets have Wheel prizes", test_later_sets_have_wheel_prizes)
    run("Wheel PvE cards are not chest prizes", test_wheel_pve_cards_are_not_chest_prizes)
    run("reels show the rolled outcome", test_reels_show_the_rolled_outcome)
    run("outcome odds follow the survey", test_outcome_odds_follow_the_survey)
    run("upgrades cap at Primal", test_upgrades_cap_at_primal)
    run("spin costs gold and keeps the chest", test_spin_costs_gold_and_keeps_the_chest)
    run("free spins and Primal chests cost nothing", test_free_spins_and_primal_chests_cost_nothing)
    run("spin prizes reach the collection", test_spin_prizes_reach_the_collection)
    run("SpinWheelOfFate reports prizes", test_spin_request_reports_prizes)
    if FAILURES:
        sys.exit(1)
