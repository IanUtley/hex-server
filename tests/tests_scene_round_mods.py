"""Round-scheduled campaign scene mods: deferral at setup and the Nth-turn grant."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

# Bind this process's test database before any runtime import.
SRC = fresh_database()

from rules_port import scene_round_mods as srm

EARLY_SPROUTS = "1bbcc90a-0046-4d64-911b-b1ea69ee5b1c"


def test_targeted_round_entry_is_deferred_and_granted_on_its_turn(monkeypatch):
    """A targeted entry with round 3 is held back at setup, grants on the
    target side's third turn only, and is then retired."""
    scene_mods = [
        {"title": "Resource Rich", "target": "Player", "round": 3,
         "mods": [{"guid": EARLY_SPROUTS.upper()}]},
        {"title": "Scene-wide", "mods": [{"guid": "c647b336-2909-48b2-8a4c-a932222b68f7"}]},
    ]
    immediate, pending = srm.split_round_mods(scene_mods)
    assert [m["title"] for m in immediate] == ["Scene-wide"]
    assert pending == [{"round": 3, "side": "player", "card": EARLY_SPROUTS}]

    state = {"pending_scene_round_mods": pending}
    applied_calls = []
    monkeypatch.setattr(srm, "card_resource_grants",
                        lambda db, card: [("currentresource", 1),
                                          ("totalresource", 1)])
    monkeypatch.setattr(srm, "project_resource_change",
                        lambda *args, **kw: applied_calls.append(args[5:8]))

    for turn in (1, 2):
        assert srm.apply_due_round_mods(None, None, None, state, "player",
                                        turn, 1, 2) == []
    assert applied_calls == []
    # The AI's third turn does not fire a Player-targeted entry.
    assert srm.apply_due_round_mods(None, None, None, state, "ai",
                                    3, 1, 2) == []

    fired = srm.apply_due_round_mods(None, None, None, state, "player",
                                     3, 1, 2)
    assert fired == [(EARLY_SPROUTS, "currentresource", 1),
                     (EARLY_SPROUTS, "totalresource", 1)]
    assert applied_calls == [("player", "currentresource", 1),
                             ("player", "totalresource", 1)]
    assert state["pending_scene_round_mods"] == []
