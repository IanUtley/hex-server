"""Booster treasure-chest loot.

Booster-pack treasure chests award one item per roll (Common, Uncommon, and
Rare chests roll once, Legendary chests twice, Primal chests three times).
Each roll first picks a kind of prize from ``CHEST_CONTENT_WEIGHTS`` and then
a prize from the chest's set:

* equipment from the set's chest-loot pool
  (``equipment_templates.is_chest_loot``), by equipment rarity;
* Stardust;
* an alternate-art (AA) PvP card of the set;
* a PvE card from the set's PvE promo cards;
* one of the set's three chest mercenaries (Rare chests and up);
* one of those mercenaries' deck sleeves (Primal chests only).

The weights are the observed frequencies (per mille, per chest rarity) from
the community chest-drop survey of July 2015, which logged ~1,700 typed drops
from Set 1-3 chests ("Aggregated Chest distribution",
docs.google.com/spreadsheets/d/193MgoYZ5OJc5F7UMXq39-GBmzBUhCT7s9ltcKGWtIVs).
Rare one-off reports (e.g. booster packs from Legendary chests) are left out.

Mercenaries and sleeves come from the client data, which labels them
"<set name> Chest".  The client data does not mark which AA or PvE cards were
chest prizes, so the survey's lists are used where it has them; any prize
kind whose pool is empty for a set is skipped and the roll goes to the other
kinds instead.
"""

import random
from functools import lru_cache

ITEMS_PER_CHEST = {
    "Common": 1, "Uncommon": 1, "Rare": 1, "Legendary": 2, "Primal": 3,
}

# chest rarity -> {(prize kind, tier): relative weight}
CHEST_CONTENT_WEIGHTS = {
    "Common": {
        ("equipment", "Common"): 674, ("equipment", "Uncommon"): 136,
        ("equipment", "Rare"): 48,
        ("stardust", "common"): 45, ("stardust", "uncommon"): 25,
        ("aa_card", "low"): 67,
    },
    "Uncommon": {
        ("equipment", "Common"): 214, ("equipment", "Uncommon"): 580,
        ("equipment", "Rare"): 76,
        ("stardust", "uncommon"): 22, ("stardust", "rare"): 14,
        ("aa_card", "low"): 69,
        ("pve_card", "rare"): 25,
    },
    "Rare": {
        ("equipment", "Common"): 103, ("equipment", "Uncommon"): 194,
        ("equipment", "Rare"): 455,
        ("stardust", "rare"): 20, ("stardust", "legendary"): 12,
        ("aa_card", "low"): 59,
        ("pve_card", "rare"): 55,
        ("mercenary", ""): 59,
    },
    "Legendary": {
        ("equipment", "Uncommon"): 172, ("equipment", "Rare"): 178,
        ("equipment", "Legendary"): 249,
        ("stardust", "promo"): 18,
        ("aa_card", "any"): 65,
        ("pve_card", "any"): 154,
        ("mercenary", ""): 136,
    },
    "Primal": {
        ("equipment", "Rare"): 85, ("equipment", "Legendary"): 237,
        ("stardust", "promo"): 34,
        ("aa_card", "high"): 212,
        ("pve_card", "any"): 204,
        ("mercenary", ""): 110,
        ("sleeve", ""): 102,
    },
}

SET1 = "0382f729-7710-432b-b761-13677982dcd2"   # Shards of Fate
SET2 = "b05e69d2-299a-4eed-ac31-3f1b4fa36470"   # Shattered Destiny
SET3 = "fce480eb-15f9-4096-8d12-6beee9118652"   # Armies of Myth

# AA cards reported from chests.  "low" dropped from Common-Rare chests,
# "high" from Legendary and Primal chests.
CHEST_AA_CARDS = {
    SET1: {"low": ("Savage Raider", "Hex Engine", "Wall of Corpses",
                   "Rot Caster"),
           "high": ("Mastery of Time", "Crash of Beasts")},
    SET2: {"low": ("Constantina", "Royal Valkyr", "Paladin of the Necropolis",
                   "Psychotic Anarchist"),
           "high": ("Darkspire Tyrant", "Arborean Rootfather")},
}

# Each set's PvE chest cards live in a SetNN_PvE_Promo card set.  Sets 1-3
# share Set03_PvE_Promo, so Sets 1 and 2 list the cards the survey reported
# and Set 3 takes the rest of that set.
PVE_PROMO_SETS = {
    SET1: "3cc27cc9-b3af-44c7-a5de-4126f78d96ed",
    SET2: "3cc27cc9-b3af-44c7-a5de-4126f78d96ed",
    SET3: "3cc27cc9-b3af-44c7-a5de-4126f78d96ed",
    "2d05262c-d7a0-408f-a280-36d206a29344": "e3217d24-bff4-4159-94bc-4653012a14cd",
    "ecdbc188-5750-48ef-acac-05e2bcbcc46f": "57df329e-d186-4717-aa99-f2c82f0aee73",
    "fbbac856-2264-4d31-97b0-0d8a646b9597": "e96bc76d-9b12-4f0f-90d7-83d48cd5191a",
    "326602fa-e183-4dfe-8300-55cc0c7c4ce8": "b9d3bfc7-c1ef-4760-b60c-b22b38419ea6",
    "9a824393-cd11-4273-a05e-41e35eb50dbe": "def91e9f-e888-49b7-818c-ba27ba16c9d3",
    "54f14f51-2afe-4a26-be28-d251b06a9cc4": "522d7e3b-ba61-491c-801c-7c3beb1a8f1b",
}
CHEST_PVE_CARDS = {
    SET1: ("Alchemy Lab", "Cerulean Sky Mage", "Horrific Poltergeist",
           "Radiant Salvation", "Scourgecrag Witch", "Volley"),
    SET2: ("Angel of Foresight", "Optimatron", "Rain of Meteors",
           "Soul Devour", "Vengeance of the Ancient Kings",
           "Wildwood Beastcaller"),
}

_PVE_CARD_RARITIES = {"rare": ("Uncommon", "Rare"),
                      "any": ("Uncommon", "Rare", "Legendary")}


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


def _first_per_name(cards):
    by_name = {}
    for card in sorted(cards, key=lambda card: card[0]):
        by_name.setdefault(card[1], card)
    return list(by_name.values())


def aa_card_pool(card_templates, set_guid, tier):
    """AA (Epic) templates of the set's chest AA cards for a tier."""
    names = CHEST_AA_CARDS.get(set_guid, {})
    wanted = set(names.get("low", ()) + names.get("high", ())) if tier == "any" \
        else set(names.get(tier, ()))
    return _first_per_name(
        card for card in card_templates.get(set_guid, ())
        if card[2] == "Epic" and card[1] in wanted)


def pve_card_pool(card_templates, set_guid, tier):
    """The set's PvE chest cards allowed for a tier ("rare" or "any")."""
    promo_set = PVE_PROMO_SETS.get(set_guid)
    if not promo_set:
        return []
    cards = [card for card in card_templates.get(promo_set, ())
             if card[2] in _PVE_CARD_RARITIES[tier]]
    if set_guid in CHEST_PVE_CARDS:
        cards = [card for card in cards if card[1] in CHEST_PVE_CARDS[set_guid]]
    elif set_guid == SET3:
        taken = set(CHEST_PVE_CARDS[SET1] + CHEST_PVE_CARDS[SET2])
        cards = [card for card in cards if card[1] not in taken]
    return _first_per_name(cards)


@lru_cache(maxsize=None)
def chest_inventory_items(set_guid):
    """Return ``(mercenary guids, sleeve guids)`` labeled as the set's chest prizes."""
    from gamedata import DEFAULT_RECORD_STORE
    card_set = DEFAULT_RECORD_STORE.get("CardSetTemplate", set_guid)
    set_name = str(card_set.field("m_Name") or "") if card_set else ""
    if not set_name:
        return (), ()
    label = f"{set_name} Chest"
    mercenaries, sleeves = [], []
    for item in DEFAULT_RECORD_STORE.load("InventoryItemData"):
        description = str(item.field("m_Description") or "").strip()
        if item.is_a("InventoryMercenaryData") and description == label:
            mercenaries.append(item.guid)
        elif (item.is_a("InventoryDeckSleeve")
              and description.startswith(label + ":")):
            sleeves.append(item.guid)
    return tuple(sorted(mercenaries)), tuple(sorted(sleeves))


def roll_chest_loot(db, card_templates, set_guid, chest_rarity, rng=random):
    """Return ``(cards, inventory_rewards)`` awarded by one booster chest.

    ``cards`` are ``(guid, name, cost, attack, defense)`` rows and
    ``inventory_rewards`` are ``(template_guid, kind)`` pairs.  Both are empty
    when the set has no chest-loot equipment, so the caller should keep its
    non-chest behavior.
    """
    from db import STARDUST_TEMPLATES
    equipment = chest_loot_pool(db, set_guid)
    if not equipment:
        return [], []
    mercenaries, sleeves = chest_inventory_items(set_guid)
    pools = {}
    weights = CHEST_CONTENT_WEIGHTS.get(chest_rarity, CHEST_CONTENT_WEIGHTS["Common"])
    for kind, tier in weights:
        if kind == "equipment":
            pool = equipment.get(tier, [])
        elif kind == "stardust":
            pool = [STARDUST_TEMPLATES[tier]]
        elif kind == "aa_card":
            pool = aa_card_pool(card_templates, set_guid, tier)
        elif kind == "pve_card":
            pool = pve_card_pool(card_templates, set_guid, tier)
        elif kind == "mercenary":
            pool = list(mercenaries)
        else:
            pool = list(sleeves)
        if pool:
            pools[(kind, tier)] = pool
    prizes = list(pools)
    cards, inventory_rewards = [], []
    for _ in range(ITEMS_PER_CHEST.get(chest_rarity, 1)):
        kind, tier = rng.choices(prizes, weights=[weights[p] for p in prizes])[0]
        prize = rng.choice(pools[(kind, tier)])
        if kind in ("aa_card", "pve_card"):
            guid, name, _rarity, cost, attack, defense = prize[:6]
            cards.append((guid, name, cost, attack, defense))
        else:
            inventory_rewards.append((prize, kind))
    return cards, inventory_rewards
