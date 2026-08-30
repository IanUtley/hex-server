"""Focused regression tests for Frost Ring Arena roster selection."""

import os
import random
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gamemodes.arena import (
    FIXED_ELITE_RANKS,
    encounter_family,
    is_boss_encounter,
    select_fra_roster,
)


def encounter(base, *, elite=False, min_rank=6, max_rank=19):
    return {
        "base": base,
        "is_boss": False,
        "is_elite": elite,
        "min_rank": min_rank,
        "max_rank": max_rank,
        "name": base,
    }


def test_fixed_elite_positions_and_known_bosses():
    encounters = [
        *[
            encounter(f"Arena_Starter_{index}", min_rank=1, max_rank=4)
            for index in range(6)
        ],
        encounter("Arena_Eternal_Guardian", min_rank=5, max_rank=5),
    ]
    for base in ("Arena_Phenteo", "Arena_Eurig", "Arena_Princess_Cory"):
        encounters.extend((encounter(base), encounter(base, elite=True)))
    # There must be enough ordinary families both for the fixed elite slots
    # and for the ordinary slots that occur before/after them.
    for index in range(20):
        base = f"Arena_Generic_{index}"
        encounters.extend((encounter(base), encounter(base, elite=True)))
    encounters.extend((
        encounter("Arena_Hogarth", min_rank=20, max_rank=20),
        encounter("Arena_Hogarth", elite=True, min_rank=20, max_rank=20),
    ))

    selected = dict(select_fra_roster(encounters, rng=random.Random(7)))

    assert all(selected[position]["is_elite"] for position in FIXED_ELITE_RANKS)
    assert all(
        not selected[position]["is_elite"]
        for position in range(1, 21)
        if position not in FIXED_ELITE_RANKS and position not in (10, 15, 20)
    )
    assert all(is_boss_encounter(selected[position]) for position in (10, 15, 20))
    assert selected[20]["base"] == "Arena_Hogarth"
    assert all(
        not is_boss_encounter(selected[position])
        for position in FIXED_ELITE_RANKS
    )
    assert len({encounter_family(item) for item in selected.values()}) == 20


def test_cashout_sends_empty_roster_refresh():
    """Cash-out must clear ArenaClient's persistent fighter-list cache."""
    import services.arena as arena_service

    class Handler:
        user_profile = {"id": 7}

    events = []
    with mock.patch.object(
            arena_service, "db_get_arena_state",
            return_value={"gold_earned": 2, "chests_earned": 1,
                          "sacks_earned": 0}), \
            mock.patch.object(arena_service, "db_update_arena_state"), \
            mock.patch.object(arena_service, "db_clear_fra_challengers"), \
            mock.patch.object(
                arena_service, "_send_response",
                side_effect=lambda *args, **kwargs: events.append("response")), \
            mock.patch.object(
                arena_service, "_send_challenger_list",
                side_effect=lambda *args, **kwargs: events.append((args, kwargs))):
        arena_service._cash_out(Handler(), "ServiceCampaign", "Shared", 9,
                                1, "session", 0, "mail")

    assert events[0] == "response", events
    assert events[1][1]["log_prefix"] == "clear", events
    assert events[1][0][3] == 0, events  # unsolicited cache-clearing refresh


def test_destroy_arena_responds_and_clears_roster():
    """The client's post-cashout DestroyArenaData request is acknowledged."""
    import services.arena as arena_service

    class Handler:
        user_profile = {"id": 7}

    events = []
    with mock.patch.object(arena_service, "db_update_arena_state"), \
            mock.patch.object(arena_service, "db_clear_fra_challengers"), \
            mock.patch.object(
                arena_service, "_send_response",
                side_effect=lambda *args, **kwargs: events.append("response")), \
            mock.patch.object(
                arena_service, "_send_challenger_list",
                side_effect=lambda *args, **kwargs: events.append("clear")):
        arena_service._destroy_arena(Handler(), "ServiceCampaign", "Shared",
                                     11, 1, "session", 0, "mail")

    assert events == ["response", "clear"], events


if __name__ == "__main__":
    test_fixed_elite_positions_and_known_bosses()
    test_cashout_sends_empty_roster_refresh()
    test_destroy_arena_responds_and_clears_roster()
    print("Arena roster tests passed")
