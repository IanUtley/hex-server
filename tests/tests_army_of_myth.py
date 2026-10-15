"""The Armies of Myth battle starts the AI with the campaign's army level."""

import os
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database  # noqa: E402

fresh_database()

import hconnect_server as hc  # noqa: E402
from campaign_runtime import progress  # noqa: E402
from services import legacy_loadbalancer as lb  # noqa: E402


def _ai_resources(scene, counters):
    session = SimpleNamespace(session_name="camp_7")
    with mock.patch.object(hc.campaign, "_last_encounter_scene",
                           {"camp_7": scene}), \
         mock.patch.object(hc, "db_campaign_identity_state",
                           return_value=(42, "{}")), \
         mock.patch.object(progress, "load",
                           return_value=({"counters": counters}, {})):
        return lb._army_of_myth_ai_resources(session)


def test_army_of_myth_starts_the_ai_at_the_army_level():
    assert _ai_resources(lb.ARMY_OF_MYTH_SCENE, {"fight_army_at_level": 4}) == 4
    assert _ai_resources(lb.ARMY_OF_MYTH_SCENE, {}) == 0
    assert _ai_resources("some-other-scene", {"fight_army_at_level": 4}) is None
