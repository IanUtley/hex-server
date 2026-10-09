"""Client chest opening: resolving chest rows, rolling rewards and granting pack inventory."""

import random
from collections import namedtuple

from az1_pack import CAMPAIGN_PACK_CONFIGS, generate_campaign_pack
from db import _db, log_req, STARDUST_TEMPLATES
from profile_db import (db_add_card, db_add_stardust,
                        db_consume_inventory_row, db_create_card_instance,
                        db_get_chest_by_id, db_inventory_item,
                        db_next_card_instance_id, db_next_inventory_client_uid,
                        db_open_chest, db_upsert_inventory_item)
from services.card_pools import (
    generate_booster as _generate_booster,
    generate_crayburn_chest as _generate_crayburn_chest,
    load_card_templates as _load_card_templates,
)


# The fixed client addresses every treasure chest by the InventoryItem UID the
# server handed it, which comes from two spaces: ``9000 + treasure_chests.id``
# for chests delivered as chest_bits / Applied chest rewards, and the
# ``player_inventory.client_item_uid`` of an InventoryTreasureChest template
# granted as an item (a store purchase such as the AZ1/Howling Plains campaign
# booster).  Chest opening must resolve both or the client sees
# "Invalid Chest ID" for the items it legitimately owns.
CLIENT_CHEST_UID_BASE = 9000


_CHEST_KEEP_COUNTS = {"Common": 3, "Uncommon": 2, "Rare": 1,
                      "Legendary": 0, "Primal": 0}


ClientChest = namedtuple(
    "ClientChest",
    "client_uid template_guid set_guid chest_type db_id inventory_row_id")


def _resolve_client_chest(user_id, chest_uid):
    """Resolve a client chest-instance UID to the chest it addresses.

    Returns ``ClientChest`` or ``None`` when the UID is not an unopened chest
    owned by the player.
    """
    try:
        chest_uid = int(chest_uid)
    except (TypeError, ValueError):
        return None
    if not user_id or chest_uid <= 0:
        return None
    # Older store grants derived InventoryItem UIDs from store item IDs.  That
    # can collide with another inventory instance, so prefer an owned chest
    # row when resolving a legacy duplicated UID rather than whichever
    # unrelated row happens to sort first.
    row = _db.execute(
        "SELECT i.id, i.template_guid, i.quantity, i.client_item_uid, "
        "c.set_guid, c.chest_type FROM player_inventory i "
        "JOIN chest_templates c ON lower(c.guid)=lower(i.template_guid) "
        "WHERE i.user_id=? AND i.client_item_uid=? AND i.quantity>0 "
        "ORDER BY i.id LIMIT 1", (user_id, chest_uid)).fetchone()
    if row:
        return ClientChest(chest_uid, row[1] or "", row[4] or "",
                           row[5] or "Promo", None, row[0])
    if chest_uid >= CLIENT_CHEST_UID_BASE:
        row = db_get_chest_by_id(
            chest_uid - CLIENT_CHEST_UID_BASE, user_id, conn=_db)
        if row:
            return ClientChest(chest_uid, row[4] or "", row[1] or "",
                               row[2] or "Common", row[0], None)
    return None


def _generate_chest_rewards(chest, card_templates, rng=None):
    """Return ``(cards, inventory_rewards)`` for one resolved client chest.

    ``cards`` are ``(guid, name, cost, attack, defense)`` rows and
    ``inventory_rewards`` are ``(template_guid, kind)`` pairs.  Campaign packs
    and Crayburn promo chests are authored by template; every other chest falls
    back to its set booster trimmed to the chest rarity.
    """
    rng = rng or random
    pack_config = CAMPAIGN_PACK_CONFIGS.get((chest.template_guid or "").lower())
    if pack_config:
        from gamedata import DEFAULT_RECORD_STORE
        reward = generate_campaign_pack(
            card_templates, DEFAULT_RECORD_STORE, rng,
            pack_config=pack_config)
        inventory_rewards = [(guid, "equipment")
                             for guid in reward.equipment_guids]
        inventory_rewards.extend(
            (STARDUST_TEMPLATES[rarity.lower()], "stardust")
            for rarity in reward.stardust_rarities)
        return list(reward.cards), inventory_rewards
    cards = _generate_crayburn_chest(card_templates, chest.template_guid)
    if cards is None:
        cards = _generate_booster(card_templates, chest.set_guid)
        keep_count = _CHEST_KEEP_COUNTS.get(chest.chest_type, 3)
        if len(cards) > keep_count:
            cards = rng.sample(cards, keep_count)
    return list(cards), []


def _grant_pack_inventory_rewards(handler, inventory_rewards):
    """Persist and coalesce equipment/Stardust pack rewards.

    Returns ``(template_guid, client_uid, quantity)`` rows to push to the
    client.  Stardust is additionally recorded in its own ledger.
    """
    user_id = handler.user_profile["id"]
    next_uid = db_next_inventory_client_uid(user_id, conn=_db)
    updates = {}
    for template_guid, _kind in inventory_rewards:
        row = db_inventory_item(user_id, template_guid, conn=_db)
        candidate_uid = next_uid if not row or not row[2] else 0
        client_uid = db_upsert_inventory_item(
            user_id, template_guid, 1, client_item_uid=candidate_uid, conn=_db)
        if candidate_uid:
            next_uid += 1
        if template_guid in STARDUST_TEMPLATES.values():
            rarity = next(key for key, value in STARDUST_TEMPLATES.items()
                          if value == template_guid)
            db_add_stardust(user_id, rarity, 1, conn=_db)
        updates[template_guid] = (client_uid, int(row[1] or 0) + 1 if row else 1)
    return [(guid, client_uid, quantity)
            for guid, (client_uid, quantity) in updates.items()]


def _open_client_chests(handler, chest_uids):
    """Award and consume client-addressed chests without sending packets.

    Returns ``None`` when there is no profile to award.  Otherwise a summary
    carrying ``cards`` (``card_instance_bits`` tuples), ``card_template_ids``,
    ``inventory_template_ids``, ``inventory_updates`` as
    ``(template_guid, client_uid, quantity)``, ``opened`` as
    ``(template_guid, client_uid, remaining_quantity)`` and ``invalid`` (the
    UIDs that are unknown, already opened, or not owned by the player).
    """
    if not handler.user_profile:
        return None
    user_id = handler.user_profile["id"]
    card_templates = _load_card_templates()
    summary = {"cards": [], "card_template_ids": [], "inventory_template_ids": [],
               "inventory_updates": [], "opened": [], "invalid": []}
    for chest_uid in chest_uids:
        chest = _resolve_client_chest(user_id, chest_uid)
        if chest is None:
            summary["invalid"].append(chest_uid)
            continue
        cards, inventory_rewards = _generate_chest_rewards(chest, card_templates)
        next_id = db_next_card_instance_id(user_id, conn=_db)
        for offset, (guid, name, cost, atk, def_) in enumerate(cards):
            instance_id = next_id + offset
            db_add_card(user_id, guid, conn=_db)
            db_create_card_instance(user_id, instance_id, guid, conn=_db)
            summary["card_template_ids"].append(guid)
            summary["cards"].append((guid, name, cost, atk, def_, instance_id, 0))
        for guid, item_uid, quantity in _grant_pack_inventory_rewards(
                handler, inventory_rewards):
            summary["inventory_template_ids"].append(guid)
            summary["inventory_updates"].append((guid, item_uid, quantity))
        if chest.db_id is not None:
            db_open_chest(chest.db_id, conn=_db)
            remaining = 0
        else:
            remaining = db_consume_inventory_row(
                chest.inventory_row_id, 1, conn=_db)
        summary["opened"].append((chest.template_guid, chest_uid, remaining))
        log_req(f"    Opened chest {chest_uid} "
                f"({chest.template_guid or chest.set_guid}): {len(cards)} cards, "
                f"{len(inventory_rewards)} items, remaining={remaining}")
    return summary
