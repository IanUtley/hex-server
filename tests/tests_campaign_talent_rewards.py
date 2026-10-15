"""Champion talents change campaign XP, gold and dungeon rewards.

Reads the talent scripts from the gamedata store, so no database is needed.
"""

import os
import random
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

# Bind this process's disposable database before ``campaign`` imports ``db``.
from tests.test_db import fresh_database  # noqa: E402
SRC = fresh_database()

import campaign  # noqa: E402

ARTISTIC = "ca7ea39e-c692-4581-b02d-6169b4e5eb60"
INTELLECTUAL = "45d87b7c-24f8-418e-b776-c5b168e99420"
EFFICIENT = "10970620-eb94-4aad-b497-64f34d85c403"


def test_talent_bonuses_stack_and_gold_counts_only_at_the_cap():
    xp, gold = campaign._talent_reward_multipliers([INTELLECTUAL, EFFICIENT], 1)
    assert round(xp, 4) == 1.15 and gold == 1.0
    assert campaign._scale_reward(100, xp) == 115
    xp, gold = campaign._talent_reward_multipliers(
        [INTELLECTUAL, EFFICIENT], len(campaign.CHAMPION_XP_THRESHOLDS))
    assert round(gold, 4) == 1.05
    assert campaign._scale_reward(100, gold) == 105


def test_artistic_rolls_a_stardust_pick_on_dungeon_wins():
    random.seed(7)
    rolled = [campaign._talent_dungeon_reward_specs([ARTISTIC])
              for _ in range(200)]
    picks = [spec for specs in rolled for spec in specs]
    # 25% of rolls grant something; every grant here is a stardust item.
    assert 20 < len(picks) < 80, len(picks)
    assert all(spec["stardust"] in ("common", "uncommon", "rare", "legendary")
               for spec in picks)


if __name__ == "__main__":
    test_talent_bonuses_stack_and_gold_counts_only_at_the_cap()
    print("PASS test_talent_bonuses_stack_and_gold_counts_only_at_the_cap")
    test_artistic_rolls_a_stardust_pick_on_dungeon_wins()
    print("PASS test_artistic_rolls_a_stardust_pick_on_dungeon_wins")
