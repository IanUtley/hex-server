"""Wheels of Fate: spinning a booster treasure chest.

A spin costs gold by chest rarity (Primal chests and free re-spins cost
nothing; ``ChestUtils.GetChestSpinCost`` in the client uses the same table).
The chest is not opened: a spin may award prizes, re-spins, or upgrade the
chest's rarity, and the player opens the chest afterwards as usual.

The client draws the outcome from three reel symbols and colors and lights
the matching payout row (``UIPackListViewModel.SetPayoutHighlights`` and the
``PayoutPanel_Tooltip_*`` strings):

* 1 / 2 / 3 Eyes: paid re-spin / free re-spin / upgrade chest;
* three of a kind: Stars mercenary, Crowns PvE card equipment, Hands upgrade,
  Moons PvE card, Mushrooms sleeve, Skulls alternate-art card, Hearts double
  upgrade, Spiders PvP rare card -- each also grants a paid re-spin;
* 1 or 2 gold reels award 500 or 2,500 gold; three gold or three red reels
  award the exclusive prize (a booster pack of the chest's set).

``OUTCOME_WEIGHTS`` and the prize-tier weights are the counts from the
community spin survey of April-May 2014 (2,167 spins,
docs.google.com/spreadsheets/d/1CAfwv-xqUZN-pJXx4r7Kpbz0PCtcmOFuzh0eSFAQ6Gw).
The survey did not tell the two single-upgrade rows apart, so its upgrades
are split evenly between them, and it did not record reel colors, so
``RED_CHANCE`` and ``GOLD_CHANCE`` are estimates.

The client data labels each set's Wheel mercenaries "<set name> WOF" and
tags the Wheel equipment for each set's three Wheel PvE cards.  Only the Set 1
Wheel alternate-art cards and sleeves are documented (Hex wiki), so other
sets skip those two prizes, like any prize whose pool is empty.
"""

import random
import re
from dataclasses import dataclass, field
from functools import lru_cache

from services.chest_loot import SET1, SET2, SET3, booster_pack_pool

RARITIES = ("Common", "Uncommon", "Rare", "Legendary", "Primal")
SPIN_COSTS = {"Common": 1200, "Uncommon": 3100, "Rare": 8500,
              "Legendary": 30000, "Primal": 0}

# EChestSpinStatus
NO_SPIN, PAID_SPIN, FREE_SPIN = 0, 1, 2

# ESpinWheelOfFateError
OK, NOT_PLAYERS_CHEST, NO_SPIN_LEFT, NOT_ENOUGH_GOLD = 0, 1, 2, 3

# Reel symbol values as the client's payout highlights read them.
STAR, CROWN, HAND, MOON, EYE, MUSHROOM, SKULL, HEART, SPIDER = 0, 1, 2, 3, 4, 5, 6, 7, 9
NON_EYE_SYMBOLS = (STAR, CROWN, HAND, MOON, MUSHROOM, SKULL, HEART, SPIDER)
# Reel colors: the client highlights all-1 as triple red and any 2 as gold.
PLAIN, RED, GOLD = 0, 1, 2
RED_CHANCE = 0.20
GOLD_CHANCE = 0.05
GOLD_AWARDS = {1: 500, 2: 2500}

# outcome -> (weight, triple symbol, chest upgrades, spin status afterwards)
OUTCOMES = {
    "fail":         (958, None, 0, NO_SPIN),
    "paid_spin":    (280, None, 0, PAID_SPIN),
    "free_spin":    (129, None, 0, FREE_SPIN),
    "upgrade":      (128, EYE, 1, NO_SPIN),
    "upgrade_paid": (128, HAND, 1, PAID_SPIN),
    "upgrade_twice": (75, HEART, 2, PAID_SPIN),
    "mercenary":     (66, STAR, 0, PAID_SPIN),
    "equipment":     (78, CROWN, 0, PAID_SPIN),
    "pve_card":      (78, MOON, 0, PAID_SPIN),
    "sleeve":        (63, MUSHROOM, 0, PAID_SPIN),
    "aa_card":       (81, SKULL, 0, PAID_SPIN),
    "pvp_card":     (103, SPIDER, 0, PAID_SPIN),
}
PRIZE_OUTCOMES = ("mercenary", "equipment", "pve_card", "sleeve", "aa_card",
                  "pvp_card")

# Survey counts of each prize's common / uncommon / rare version.
PRIZE_TIER_WEIGHTS = {
    "mercenary": (59, 5, 2),
    "equipment": (10, 8, 60),
    "sleeve": (43, 2, 18),
    "pve_card": (64, 9, 5),
    "aa_card": (74, 1, 6),
}
# PvP rare card prize: "Can also be a legendary card" (estimate).
PVP_CARD_RARITY_WEIGHTS = {"Rare": 80, "Legendary": 20}

# Each set's three Wheel PvE cards, common to rare prize tier.
WOF_PVE_CARDS = {
    SET1: ("Lightning Elemental", "Water Elemental", "Air Elemental"),
    SET2: ("Death Cap", "Shiitake Chef", "Fungal Monstrosity"),
    SET3: ("Slaughtergear's Guardians", "Slaughtergear's Reaver",
           "Slaughtergear's Replicator"),
    "2d05262c-d7a0-408f-a280-36d206a29344": ("Bog Walker", "Dread Wight",
                                             "Splinterslam"),
    "ecdbc188-5750-48ef-acac-05e2bcbcc46f": ("Glorious Pegasus",
                                             "Scion of Oberon", "War Spoils"),
    "fbbac856-2264-4d31-97b0-0d8a646b9597": ("Bitter Dread", "Feralroot Tracker",
                                             "Cosmic Flashpaw"),
    "326602fa-e183-4dfe-8300-55cc0c7c4ce8": ("VB213", "Forgotten Triolith",
                                             "Cartomancy"),
    "9a824393-cd11-4273-a05e-41e35eb50dbe": ("Life From Death",
                                             "Feralfuel Infusion Device",
                                             "Vaarician"),
}
WOF_PVE_CARD_NAMES = frozenset(name for names in WOF_PVE_CARDS.values()
                               for name in names)
# Card sets holding the Wheel PvE cards (Set01_PvE_Arena, SetNN_PvE_Promo).
_WOF_CARD_SETS = frozenset({
    "d8ee3b8d-d4b7-4997-bbb3-f00658dbf303",
    "3cc27cc9-b3af-44c7-a5de-4126f78d96ed",
    "e3217d24-bff4-4159-94bc-4653012a14cd",
    "57df329e-d186-4717-aa99-f2c82f0aee73",
    "e96bc76d-9b12-4f0f-90d7-83d48cd5191a",
    "b9d3bfc7-c1ef-4760-b60c-b22b38419ea6",
    "def91e9f-e888-49b7-818c-ba27ba16c9d3",
})
# Set 1 Wheel alternate-art cards and their sleeves, common to rare.
WOF_AA_CARDS = {SET1: ("Windbourne Acolyte", "Veteran Gladiator",
                       "Wrathwood Colossus")}

_WOF_EQUIPMENT_NOTE = re.compile(r"wof|wheel", re.IGNORECASE)
# "Your <b>Air Elementals</b> have" or "Each of your cards named <b>Vaarician</b>"
_EQUIPMENT_TARGET = re.compile(r"(?:Your|your cards named) <b>([^<]+)<")


@dataclass
class SpinResult:
    outcome: str
    symbols: list
    colors: list
    rarity: str
    spin_status: int
    gold: int = 0
    cards: list = field(default_factory=list)
    inventory_rewards: list = field(default_factory=list)


def spin_cost(chest_rarity, spin_status):
    if spin_status == FREE_SPIN:
        return 0
    return SPIN_COSTS.get(chest_rarity, 0)


def can_spin(spun, spin_status):
    """A chest spins once, plus once more for each re-spin it has won."""
    return not spun or spin_status in (PAID_SPIN, FREE_SPIN)


def client_spin_status(spun, spin_status):
    """The chest_bits WOFSpinStatus the client shows for a chest.

    The client only offers a spin for PaidSpin/FreeSpin chests and labels
    NoSpin chests "No Spin", so an unspun chest holds one paid spin.
    """
    return spin_status if spun else PAID_SPIN


def _tiered(items, tier_of):
    """Group items into [common, uncommon, rare] tiers by rank of ``tier_of``."""
    ranks = sorted({tier_of(item) for item in items})
    tiers = [[] for _ in ranks]
    for item in items:
        tiers[ranks.index(tier_of(item))].append(item)
    return tiers


def _card_rank(rarity):
    order = ("Common", "Uncommon", "Rare", "Legendary", "Epic")
    return order.index(rarity) if rarity in order else len(order)


def pve_card_tiers(card_templates, set_guid):
    """The set's Wheel PvE card templates as [common, uncommon, rare] tiers."""
    tiers = []
    for name in WOF_PVE_CARDS.get(set_guid, ()):
        cards = sorted(card for set_id in _WOF_CARD_SETS
                       for card in card_templates.get(set_id, ())
                       if card[1] == name)
        if cards:
            tiers.append([cards[0]])
    return tiers


def aa_card_tiers(card_templates, set_guid):
    tiers = []
    for name in WOF_AA_CARDS.get(set_guid, ()):
        cards = sorted(card for card in card_templates.get(set_guid, ())
                       if card[1] == name and card[2] == "Epic")
        if cards:
            tiers.append([cards[0]])
    return tiers


def _names_card(target, card_name):
    """Whether an equipment's plural target ("Scions of Oberon") is the card."""
    target_words = target.lower().split()
    card_words = card_name.lower().split()
    return len(target_words) == len(card_words) and all(
        t.startswith(c[:max(3, len(c) - 2)]) for t, c in zip(target_words, card_words))


def equipment_tiers(db, set_guid):
    """Wheel equipment for the set's Wheel PvE cards, tiered by rarity."""
    cards = WOF_PVE_CARDS.get(set_guid, ())
    items = []
    for guid, rarity, notes, description in db.execute(
            "SELECT guid, rarity, design_notes, description "
            "FROM equipment_templates ORDER BY guid").fetchall():
        target = _EQUIPMENT_TARGET.search(description or "")
        if (_WOF_EQUIPMENT_NOTE.search(notes or "") and target
                and any(_names_card(target.group(1), card) for card in cards)):
            items.append((guid, rarity))
    return [[guid for guid, _ in tier]
            for tier in _tiered(items, lambda item: _card_rank(item[1]))]


@lru_cache(maxsize=None)
def _inventory_tiers(set_guid):
    """Return ``(mercenary tiers, sleeve tiers)`` of item GUIDs for a set."""
    from gamedata import DEFAULT_RECORD_STORE
    card_set = DEFAULT_RECORD_STORE.get("CardSetTemplate", set_guid)
    set_name = str(card_set.field("m_Name") or "") if card_set else ""
    mercenaries, sleeves = [], []
    sleeve_names = {f"{name} Sleeve": i
                    for i, name in enumerate(WOF_AA_CARDS.get(set_guid, ()))}
    for item in DEFAULT_RECORD_STORE.load("InventoryItemData"):
        if (set_name and item.is_a("InventoryMercenaryData")
                and str(item.field("m_Description") or "").strip() == f"{set_name} WOF"):
            mercenaries.append((item.guid, str(item.field("m_Rarity") or "")))
        elif item.is_a("InventoryDeckSleeve") and item.field("m_Name") in sleeve_names:
            sleeves.append((item.guid, sleeve_names[item.field("m_Name")]))
    mercenary_tiers = [[guid for guid, _ in tier] for tier in
                       _tiered(sorted(mercenaries), lambda m: _card_rank(m[1]))]
    sleeve_tiers = [[guid for guid, _ in tier]
                    for tier in _tiered(sorted(sleeves), lambda s: s[1])]
    return mercenary_tiers, sleeve_tiers


def pvp_card_pool(card_templates, set_guid):
    """{"Rare": [...], "Legendary": [...]} PvP cards of the chest's set."""
    pool = {}
    for card in card_templates.get(set_guid, ()):
        if card[2] in PVP_CARD_RARITY_WEIGHTS and not card[6] and not card[7]:
            pool.setdefault(card[2], []).append(card)
    return pool


def prize_pools(db, card_templates, set_guid):
    """Return {outcome: prize tiers} for the prize outcomes a set can award."""
    mercenaries, sleeves = _inventory_tiers(set_guid)
    pools = {
        "mercenary": mercenaries,
        "equipment": equipment_tiers(db, set_guid),
        "pve_card": pve_card_tiers(card_templates, set_guid),
        "sleeve": sleeves,
        "aa_card": aa_card_tiers(card_templates, set_guid),
    }
    pools = {outcome: tiers for outcome, tiers in pools.items() if tiers}
    pvp = pvp_card_pool(card_templates, set_guid)
    if pvp:
        pools["pvp_card"] = pvp
    return pools


def _pick_tier(tiers, weights, rng):
    weights = (list(weights) + [weights[-1]] * len(tiers))[:len(tiers)]
    return rng.choices(tiers, weights=weights)[0]


def _symbols_for(outcome, rng):
    triple = OUTCOMES[outcome][1]
    if triple is not None:
        return [triple] * 3
    eyes = {"fail": 0, "paid_spin": 1, "free_spin": 2}[outcome]
    while True:
        symbols = [rng.choice(NON_EYE_SYMBOLS) for _ in range(3)]
        for index in rng.sample(range(3), eyes):
            symbols[index] = EYE
        if eyes or len(set(symbols)) > 1:
            return symbols


def _roll_colors(rng):
    colors = []
    for _ in range(3):
        roll = rng.random()
        colors.append(GOLD if roll < GOLD_CHANCE
                      else RED if roll < GOLD_CHANCE + RED_CHANCE else PLAIN)
    return colors


def roll_spin(db, card_templates, set_guid, chest_rarity, rng=random):
    """Roll one spin of a booster chest and return its ``SpinResult``."""
    pools = prize_pools(db, card_templates, set_guid)
    outcomes = [o for o in OUTCOMES if o not in PRIZE_OUTCOMES or o in pools]
    outcome = rng.choices(outcomes, weights=[OUTCOMES[o][0] for o in outcomes])[0]
    _weight, _symbol, upgrades, spin_status = OUTCOMES[outcome]
    rarity_index = RARITIES.index(chest_rarity) if chest_rarity in RARITIES else 0
    result = SpinResult(
        outcome=outcome, symbols=_symbols_for(outcome, rng),
        colors=_roll_colors(rng),
        rarity=RARITIES[min(rarity_index + upgrades, len(RARITIES) - 1)],
        spin_status=spin_status)

    if outcome == "pvp_card":
        pool = pools["pvp_card"]
        rarities = [r for r in PVP_CARD_RARITY_WEIGHTS if pool.get(r)]
        rarity = rng.choices(rarities, weights=[PVP_CARD_RARITY_WEIGHTS[r] for r in rarities])[0]
        card = rng.choice(pool[rarity])
        result.cards.append((card[0], card[1], card[3], card[4], card[5]))
    elif outcome in PRIZE_OUTCOMES:
        prize = rng.choice(_pick_tier(pools[outcome], PRIZE_TIER_WEIGHTS[outcome], rng))
        if outcome in ("pve_card", "aa_card"):
            result.cards.append((prize[0], prize[1], prize[3], prize[4], prize[5]))
        else:
            result.inventory_rewards.append((prize, outcome))

    gold_reels = result.colors.count(GOLD)
    result.gold = GOLD_AWARDS.get(gold_reels, 0)
    if gold_reels == 3 or result.colors.count(RED) == 3:
        boosters = booster_pack_pool(db, set_guid)
        if boosters:
            result.inventory_rewards.append((rng.choice(boosters), "booster"))
    return result


# ── Chest spin state ──────────────────────────────────────────────────

def chest_spin_state(db, chest_db_id):
    """Return ``(spun, spin_status)`` for a booster chest row."""
    row = db.execute("SELECT wof_spun, wof_status FROM treasure_chests WHERE id=?",
                     (chest_db_id,)).fetchone()
    return (int(row[0] or 0), int(row[1] or 0)) if row else (0, NO_SPIN)


def save_spin(db, chest_db_id, result):
    db.execute("UPDATE treasure_chests SET wof_spun=1, wof_status=?, chest_rarity=? "
               "WHERE id=?", (result.spin_status, result.rarity, chest_db_id))
