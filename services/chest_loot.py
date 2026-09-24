"""Treasure-chest equipment loot.

Booster-pack treasure chests award equipment from their set's chest-loot pool
(``equipment_templates.is_chest_loot``).  The retail item counts and the
chest/equipment rarity eligibility are documented by the community:

* Common, Uncommon, and Rare chests award one item, Legendary chests two, and
  Primal chests three.
* Common equipment drops from Common/Uncommon/Rare chests, Uncommon from every
  chest below Primal, Rare from every chest, and Legendary from Legendary and
  Primal chests.

The official per-rarity odds were never published.  ``EQUIPMENT_RARITY_WEIGHTS``
is an estimate that respects the eligibility rules above and can be tuned
without touching the handlers.
"""

import random

ITEMS_PER_CHEST = {
    "Common": 1, "Uncommon": 1, "Rare": 1, "Legendary": 2, "Primal": 3,
}

# chest rarity -> {equipment rarity: relative weight}
EQUIPMENT_RARITY_WEIGHTS = {
    "Common":    {"Common": 70, "Uncommon": 25, "Rare": 5},
    "Uncommon":  {"Common": 40, "Uncommon": 45, "Rare": 15},
    "Rare":      {"Common": 15, "Uncommon": 45, "Rare": 40},
    "Legendary": {"Uncommon": 30, "Rare": 45, "Legendary": 25},
    "Primal":    {"Rare": 50, "Legendary": 50},
}


def chest_loot_pool(db, set_guid):
    """Return {equipment rarity: [template guid, ...]} for a set's chests."""
    pool = {}
    rows = db.execute(
        "SELECT guid, rarity FROM equipment_templates "
        "WHERE set_guid=? AND is_chest_loot=1 AND is_live=1 ORDER BY guid",
        (set_guid,)).fetchall()
    for guid, rarity in rows:
        pool.setdefault(rarity, []).append(guid)
    return pool


def roll_chest_equipment(db, set_guid, chest_rarity, rng=random):
    """Return the equipment template GUIDs awarded by one chest.

    An empty list means the set has no chest-loot equipment, so the caller
    should keep its non-equipment behavior.
    """
    pool = chest_loot_pool(db, set_guid)
    weights = {
        rarity: weight
        for rarity, weight in EQUIPMENT_RARITY_WEIGHTS.get(
            chest_rarity, EQUIPMENT_RARITY_WEIGHTS["Common"]).items()
        if pool.get(rarity)
    }
    if not weights:
        return []
    rarities = list(weights)
    awarded = []
    for _ in range(ITEMS_PER_CHEST.get(chest_rarity, 1)):
        rarity = rng.choices(rarities, weights=[weights[r] for r in rarities])[0]
        awarded.append(rng.choice(pool[rarity]))
    return awarded
