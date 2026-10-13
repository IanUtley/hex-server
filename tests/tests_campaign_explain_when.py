"""The simulator's blocked-tag explanation (campaign_rules.explain_when)."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CAMPAIGN_ROOT = os.path.join(ROOT, "hex-private", "campaign")
sys.path[:0] = [ROOT, CAMPAIGN_ROOT]

import campaign_rules as cr

STATE = {"faction": "Ardent", "flags": ["seen_gate"], "counters": {"coins": 2},
         "quests": {"q_a": {"status": "active", "step": 1}}, "nodes": {}}


def test_and_list_reports_only_failing_tags():
    when = ["questActive(q_a)", "faction(Underworld)", "!flag(seen_gate)",
            "counter(coins,2)", "completedQuest(q_a,2)"]
    why = cr.explain_when(when, STATE)
    assert why["holds"] is False and why["holds"] == cr.evaluate(when, STATE)
    assert why["blocked_by"] == ["faction(Underworld)", "!flag(seen_gate)",
                                 "completedQuest(q_a,2)"]


def test_or_groups_put_the_closest_group_first():
    when = [["questStarted(q_b)", "faction(Underworld)"],
            ["questActive(q_a)", "visited(Node01)"]]
    why = cr.explain_when(when, STATE)
    assert why["closest"] == 1
    assert why["blocked_by"] == ["visited(Node01)", "questStarted(q_b)",
                                 "faction(Underworld)"]


def test_holding_and_dict_conditions():
    assert cr.explain_when(["questActive(q_a)"], STATE)["blocked_by"] == []
    assert cr.explain_when(None, STATE)["holds"] is True
    when = {"all": [{"quest": "q_a", "is": "active"}, {"faction": "Underworld"}]}
    why = cr.explain_when(when, STATE)
    assert why["blocked_by"] == ["faction(Underworld)"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
