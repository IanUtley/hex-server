"""Metadata-driven card pools used by store and reward responses."""

import random

from db import log
from pvp_db import db_card_catalog, db_pvp_set_guids
from static import CRAYBURN_PACK_CARD_SEEDS


_CARD_CACHE = {}
_PVP_SET_GUIDS = None


def load_card_templates():
    global _CARD_CACHE
    if _CARD_CACHE:
        return _CARD_CACHE
    rows = db_card_catalog()
    if not rows:
        log("No card_templates in DB — run the normal database bootstrap")
        return _CARD_CACHE
    for row in rows:
        guid, sid, name, rarity, cost, attack, defense, is_pve, no_pvp, card_type = row
        _CARD_CACHE.setdefault(sid, []).append(
            (guid, name, rarity, cost, attack, defense, is_pve, no_pvp, card_type))
    log(f"Loaded cards from DB: {sum(map(len, _CARD_CACHE.values()))} cards across {len(_CARD_CACHE)} sets")
    return _CARD_CACHE


def get_pvp_sets():
    global _PVP_SET_GUIDS
    if _PVP_SET_GUIDS is None:
        _PVP_SET_GUIDS = {row[0] for row in db_pvp_set_guids()}
    return _PVP_SET_GUIDS


def generate_booster(card_data, set_id):
    pool = [card for card in card_data.get(set_id, ())
            if card[2] in ("Common", "Uncommon", "Rare", "Legendary")
            and not card[6] and not card[7]]
    if len(pool) < 17:
        chosen = pool
    else:
        commons = [card for card in pool if card[2] == "Common"] or list(pool)
        uncommons = [card for card in pool if card[2] == "Uncommon"] or list(pool)
        rares = [card for card in pool if card[2] == "Rare"] or list(pool)
        legendaries = [card for card in pool if card[2] == "Legendary"]
        chosen = random.sample(commons, min(12, len(commons)))
        chosen += random.sample(uncommons, min(4, len(uncommons)))
        chosen.append(random.choice(legendaries if legendaries and random.random() < 0.11 else rares))
        random.shuffle(chosen)
    return [(g, n, cost, atk, defense)
            for g, n, _rarity, cost, atk, defense, _pve, _nopvp, _type in chosen]


def generate_crayburn_chest(card_data, chest_template_guid):
    card_guids = CRAYBURN_PACK_CARD_SEEDS.get(chest_template_guid)
    if not card_guids:
        return None
    by_guid = {card[0]: card for cards in card_data.values() for card in cards}
    missing = [guid for guid in card_guids if guid not in by_guid]
    if missing:
        log(f"Crayburn chest {chest_template_guid} has missing card templates: {missing}")
    return [(card[0], card[1], card[3], card[4], card[5])
            for guid in card_guids if (card := by_guid.get(guid)) is not None]


def full_set_pool(pool):
    return [card for card in pool
            if card[2] in ("Common", "Uncommon", "Rare", "Legendary")
            and not card[6] and not card[7]]


def roll_primal_upgrade(quantity, rng=None):
    if quantity <= 0:
        return 0, 0
    rand = rng or random.random
    upgraded = sum(1 for _ in range(int(quantity)) if rand() < 0.02)
    return int(quantity) - upgraded, upgraded
