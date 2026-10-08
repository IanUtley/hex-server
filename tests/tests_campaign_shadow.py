"""Shadow-mode diff between legacy VisLocs and the campaign_rules layout."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import campaign_shadow as cs


def test_diff_flags_only_real_disagreements():
    layout = {"nodes": {
        "A": {"visible": True, "completed": True, "retryable": False,
              "blocked": False, "hidden": False},
        "B": {"visible": True, "completed": False, "retryable": False,
              "blocked": False, "hidden": True},
    }}
    state = {"PublicState": {"Data": {"blocked_nodes": ["B"]}}, "VisLocs": [
        {"Data": {"node": "A", "visible": True, "completed": False}},
        {"Data": {"node": "B", "visible": True}},       # hidden == legacy blocked
        {"Data": {"node": "Fork1", "visible": False}},   # forks are skipped
    ]}
    diffs = cs._diff(None, layout, state)
    assert diffs == [{"node": "A", "field": "completed",
                      "legacy": False, "engine": True}], diffs


def test_flag_env_roundtrip():
    cs.set_enabled(True)
    assert cs.enabled()
    cs.set_enabled(False)
    assert not cs.enabled()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
