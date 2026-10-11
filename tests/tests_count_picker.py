"""The count picker (Gnomes Come Home): a pick moves gnomes from the lost
counter to the carried one, keeping the total; out-of-range picks fail."""

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CAMPAIGN_ROOT = os.path.join(ROOT, "hex-private", "campaign")
sys.path[:0] = [ROOT, CAMPAIGN_ROOT]

import campaign_rules as cr
from campaign_runtime import projection

RULES = {
    "schema_version": 1,
    "start": {"node": "A", "flags": []},
    "map": {"paths": [{"id": "Path_A_B", "from": "A", "to": "B"}]},
    "nodes": {},
    "count_pickers": {"B": {"from": "gnomes_unsafe", "to": "gnomes_carried"}},
}
GNOMES = ("gnomes_unsafe", "gnomes_carried", "gnomes_safe")


def _state(unsafe=20, carried=0, safe=0):
    state = cr.initial_state(RULES)
    state["counters"] = dict(zip(GNOMES, (unsafe, carried, safe)))
    state["current"] = "B"
    return state


def test_pick_moves_gnomes_keeping_the_total_and_rejects_out_of_range():
    assert not [p for p in cr.validate(RULES) if p["level"] == "error"]
    state, _log = cr.apply(RULES, _state(15, 0, 5),
                           {"type": "count_choice", "node": "B", "count": 6})
    counters = state["counters"]
    assert (counters["gnomes_unsafe"], counters["gnomes_carried"]) == (9, 6)
    assert sum(counters[name] for name in GNOMES) == 20
    for bad in (16, -1, "3", True):
        with pytest.raises(cr.RulesError):
            cr.apply(RULES, _state(15, 0, 5), {"type": "count_choice", "node": "B", "count": bad})
    # Nothing left to move: no picker, and no pick.
    assert cr.count_picker(RULES, _state(0, 20, 0), "B") is None
    with pytest.raises(cr.RulesError):
        cr.apply(RULES, _state(0, 20, 0), {"type": "count_choice", "node": "B", "count": 0})


def test_projection_offers_the_picker_with_top_level_bounds():
    zone = {"id": "T", "kind": "overworld"}
    out = projection.project(RULES, _state(12, 8, 0), zone, {}, children={},
                             active="B", count_picker="B")
    loc = next(l["Data"] for l in out["VisLocs"] if l["Data"]["node"] == "B")
    pdata = out["PublicState"]["Data"]
    assert loc["type"] == "CountPicker" and out["ALoc"] == "B"
    assert (pdata[loc["min_var"]], pdata[loc["max_var"]]) == (0, 12)
    out = projection.project(RULES, _state(0, 20, 0), zone, out, children={},
                             active=None, count_picker="B")
    loc = next(l["Data"] for l in out["VisLocs"] if l["Data"]["node"] == "B")
    assert loc["type"] != "CountPicker" and "gnomes_unsafe_pick_max" not in out["PublicState"]["Data"]
