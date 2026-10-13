"""Client chest opening: resolving chest rows, rolling rewards and granting pack inventory."""

import random
import threading
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
    import hconnect_server as _hc
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
    row = _hc._db.execute(
        "SELECT i.id, i.template_guid, i.quantity, i.client_item_uid, "
        "c.set_guid, c.chest_type FROM player_inventory i "
        "JOIN chest_templates c ON lower(c.guid)=lower(i.template_guid) "
        "WHERE i.user_id=? AND i.client_item_uid=? AND i.quantity>0 "
        "ORDER BY i.id LIMIT 1", (user_id, chest_uid)).fetchone()
    if row:
        return ClientChest(chest_uid, row[1] or "", row[4] or "",
                           row[5] or "Promo", None, row[0])
    if chest_uid >= CLIENT_CHEST_UID_BASE:
        row = _hc.db_get_chest_by_id(
            chest_uid - CLIENT_CHEST_UID_BASE, user_id, conn=_hc._db)
        if row:
            return ClientChest(chest_uid, row[4] or "", row[1] or "",
                               row[2] or "Common", row[0], None)
    return None


def _generate_chest_rewards(chest, card_templates, rng=None):
    """Return ``(cards, inventory_rewards)`` for one resolved client chest.

    ``cards`` are ``(guid, name, cost, attack, defense)`` rows and
    ``inventory_rewards`` are ``(template_guid, kind)`` pairs.  Campaign packs
    and Crayburn promo chests are authored by template; booster treasure chests
    award their set's chest loot (``services/chest_loot.py``; the equipment
    pool comes from the equipment design notes), and chests from sets without
    chest loot fall back to the set booster trimmed to the chest rarity.
    """
    import hconnect_server as _hc
    rng = rng or random
    pack_config = _hc.CAMPAIGN_PACK_CONFIGS.get((chest.template_guid or "").lower())
    if pack_config:
        from gamedata import DEFAULT_RECORD_STORE
        reward = _hc.generate_campaign_pack(
            card_templates, DEFAULT_RECORD_STORE, rng,
            pack_config=pack_config)
        inventory_rewards = [(guid, "equipment")
                             for guid in reward.equipment_guids]
        inventory_rewards.extend(
            (_hc.STARDUST_TEMPLATES[rarity.lower()], "stardust")
            for rarity in reward.stardust_rarities)
        return list(reward.cards), inventory_rewards
    cards = _hc._generate_crayburn_chest(card_templates, chest.template_guid)
    if cards is None:
        from services.chest_loot import roll_chest_loot
        loot_cards, inventory_rewards = roll_chest_loot(
            _hc._db, card_templates, chest.set_guid, chest.chest_type, rng)
        if loot_cards or inventory_rewards:
            return loot_cards, inventory_rewards
        cards = _hc._generate_booster(card_templates, chest.set_guid)
        keep_count = _CHEST_KEEP_COUNTS.get(chest.chest_type, 3)
        if len(cards) > keep_count:
            cards = rng.sample(cards, keep_count)
    return list(cards), []


def _grant_pack_inventory_rewards(handler, inventory_rewards):
    """Persist and coalesce equipment/Stardust pack rewards.

    Returns ``(template_guid, client_uid, quantity)`` rows to push to the
    client.  Stardust is additionally recorded in its own ledger.
    """
    import hconnect_server as _hc
    user_id = handler.user_profile["id"]
    next_uid = _hc.db_next_inventory_client_uid(user_id, conn=_hc._db)
    updates = {}
    for template_guid, _kind in inventory_rewards:
        row = _hc.db_inventory_item(user_id, template_guid, conn=_hc._db)
        candidate_uid = next_uid if not row or not row[2] else 0
        client_uid = _hc.db_upsert_inventory_item(
            user_id, template_guid, 1, client_item_uid=candidate_uid, conn=_hc._db)
        if candidate_uid:
            next_uid += 1
        if template_guid in _hc.STARDUST_TEMPLATES.values():
            rarity = next(key for key, value in _hc.STARDUST_TEMPLATES.items()
                          if value == template_guid)
            _hc.db_add_stardust(user_id, rarity, 1, conn=_hc._db)
        updates[template_guid] = (client_uid, int(row[1] or 0) + 1 if row else 1)
    return [(guid, client_uid, quantity)
            for guid, (client_uid, quantity) in updates.items()]


def _roll_pack_chest_rarities(pack_count, is_primal=False, rng=None):
    """Return the rarity of the treasure chest in each opened pack.

    Every booster pack contains one treasure chest
    (BoosterPack_Content_Combined); booster packs roll
    ``chest_probabilities`` and Primal Packs always contain a Legendary chest
    (DisplayRewards_ItemDescription_PrimalPack).
    """
    import hconnect_server as _hc
    if is_primal:
        return ["Legendary"] * pack_count
    probs = _hc.db_chest_probabilities(conn=_hc._db)
    if not probs:
        return []
    rng = rng or random
    return rng.choices([row[0] for row in probs],
                       weights=[row[1] for row in probs], k=pack_count)


def _grant_reward_cards(user_id, cards):
    """Add reward cards to a player's collection.

    ``cards`` are ``(guid, name, cost, attack, defense)`` rows; returns the
    ``card_instance_bits`` tuples to show and push to the client.
    """
    import hconnect_server as _hc
    granted = []
    next_id = _hc.db_next_card_instance_id(user_id, conn=_hc._db)
    for offset, (guid, name, cost, atk, def_) in enumerate(cards):
        instance_id = next_id + offset
        _hc.db_add_card(user_id, guid, conn=_hc._db)
        _hc.db_create_card_instance(user_id, instance_id, guid, conn=_hc._db)
        granted.append((guid, name, cost, atk, def_, instance_id, 0))
    return granted


def _spin_client_chest(handler, chest_uid, rng=None):
    """Spin a booster chest on the Wheels of Fate; nothing is sent.

    Returns a summary with ``error`` (ESpinWheelOfFateError), the chest's
    ``rarity``/``spin_status``/``set_guid`` after the spin (a spin can
    upgrade the chest), reward ``cards`` (``card_instance_bits`` tuples),
    ``inventory_updates``, ``gold`` and the reel ``colors``/``symbols``.
    The chest stays unopened; the player opens it afterwards as usual.
    """
    import hconnect_server as _hc
    from profile_db import db_adjust_user_currency, db_get_user_currency
    from services import wheel_of_fate as wof
    user_id = handler.user_profile["id"]
    summary = {"error": wof.OK, "rarity": "Common", "spin_status": wof.NO_SPIN,
               "set_guid": "00000000-0000-0000-0000-000000000000",
               "cards": [], "inventory_updates": [], "gold": 0,
               "colors": [0, 0, 0], "symbols": [0, 1, 2], "outcome": ""}
    # One player's spins are serialized so a double click cannot spend the
    # same re-spin twice.
    with _spin_lock(user_id):
        chest = _resolve_client_chest(user_id, chest_uid)
        # Only booster chests (not promo/campaign packs) spin.
        if chest is None or chest.db_id is None or chest.template_guid:
            summary["error"] = (wof.NOT_PLAYERS_CHEST if chest is None
                                else wof.NO_SPIN_LEFT)
            return summary
        spun, spin_status = wof.chest_spin_state(_hc._db, chest.db_id)
        summary.update(rarity=chest.chest_type, spin_status=spin_status,
                       set_guid=chest.set_guid)
        if not wof.can_spin(spun, spin_status):
            summary["error"] = wof.NO_SPIN_LEFT
            return summary
        cost = wof.spin_cost(chest.chest_type, spin_status)
        if db_get_user_currency(user_id, "gold", conn=_hc._db) < cost:
            summary["error"] = wof.NOT_ENOUGH_GOLD
            return summary

        result = wof.roll_spin(_hc._db, _hc._load_card_templates(),
                               chest.set_guid, chest.chest_type, rng or random)
        db_adjust_user_currency(user_id, gold_delta=result.gold - cost,
                                conn=_hc._db)
        wof.save_spin(_hc._db, chest.db_id, result)
        summary.update(
            rarity=result.rarity, spin_status=result.spin_status,
            gold=result.gold, colors=result.colors, symbols=result.symbols,
            outcome=result.outcome,
            cards=_grant_reward_cards(user_id, result.cards),
            inventory_updates=_grant_pack_inventory_rewards(
                handler, result.inventory_rewards))
        _hc._db.commit()
    _hc.log_req(f"    Spun chest {chest_uid} ({chest.chest_type}, cost {cost}): "
                f"{result.outcome} symbols={result.symbols} colors={result.colors} "
                f"gold={result.gold} -> {result.rarity} status={result.spin_status}")
    return summary


_SPIN_LOCKS = {}
_SPIN_LOCKS_GUARD = threading.Lock()


def _spin_lock(user_id):
    with _SPIN_LOCKS_GUARD:
        return _SPIN_LOCKS.setdefault(user_id, threading.Lock())


def _encode_spin_response(spin, chest_uid):
    """Encode a SpinWheelOfFateResponse for a ``_spin_client_chest`` summary.

    Member names and shapes follow the client's SpinWheelOfFateResponse:
    ``Chest`` is a chest_bits object and the item prizes are
    ``RewardedItems``.
    """
    from objfmt_builder import ObjFmtBuilder
    rarities = {"Common": 0, "Uncommon": 1, "Rare": 2, "Legendary": 3,
                "Primal": 4, "Promo": 5}
    b = ObjFmtBuilder("Game.Client.Network.Profile.SpinWheelOfFateResponse")
    b.begin_element("Chest", "Game.Shared.Domain.chest_bits", b.CHEST_PROPS)
    b.chest_fields(rarities.get(spin["rarity"], 0), spin["spin_status"],
                   spin["set_guid"], chest_uid)
    b.field_int("GoldAward", spin["gold"])
    items = spin["inventory_updates"]
    b.begin_list("RewardedItems",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
                 len(items))
    for index, (template_guid, item_uid, _total) in enumerate(items):
        b.begin_element(index, "Game.Shared.Domain.inventory_bits", b.INVENTORY_PROPS)
        b.inventory_fields(template_guid, item_uid, quantity=1)
    cards = spin["cards"]
    b.begin_list("RewardCards",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                 len(cards))
    for index, card in enumerate(cards):
        # card_fields writes all eight card_instance_bits members.
        b.begin_element(index, "Game.Shared.Domain.card_instance_bits", 8)
        b.card_fields(card[0], card[5], card[6])
    b.int_list("SpinEntryColors", spin["colors"])
    b.int_list("SpinEntrySymbols", spin["symbols"])
    b.field_enum("Error", "Game.Shared.Network.Profile.ESpinWheelOfFateError",
                 spin["error"])
    b.field_str("ErrorMessage", "")
    return b.finish(8)


def _open_client_chests(handler, chest_uids):
    """Award and consume client-addressed chests without sending packets.

    Returns ``None`` when there is no profile to award.  Otherwise a summary
    carrying ``cards`` (``card_instance_bits`` tuples), ``card_template_ids``,
    ``inventory_template_ids``, ``inventory_updates`` as
    ``(template_guid, client_uid, quantity)``, ``opened`` as
    ``(template_guid, client_uid, remaining_quantity)`` and ``invalid`` (the
    UIDs that are unknown, already opened, or not owned by the player).
    """
    import hconnect_server as _hc
    if not handler.user_profile:
        return None
    user_id = handler.user_profile["id"]
    card_templates = _hc._load_card_templates()
    summary = {"cards": [], "card_template_ids": [], "inventory_template_ids": [],
               "inventory_updates": [], "opened": [], "invalid": []}
    for chest_uid in chest_uids:
        chest = _resolve_client_chest(user_id, chest_uid)
        if chest is None:
            summary["invalid"].append(chest_uid)
            continue
        cards, inventory_rewards = _generate_chest_rewards(chest, card_templates)
        for card in _grant_reward_cards(user_id, cards):
            summary["card_template_ids"].append(card[0])
            summary["cards"].append(card)
        for guid, item_uid, quantity in _grant_pack_inventory_rewards(
                handler, inventory_rewards):
            summary["inventory_template_ids"].append(guid)
            summary["inventory_updates"].append((guid, item_uid, quantity))
        if chest.db_id is not None:
            _hc.db_open_chest(chest.db_id, conn=_hc._db)
            remaining = 0
        else:
            remaining = _hc.db_consume_inventory_row(
                chest.inventory_row_id, 1, conn=_hc._db)
        summary["opened"].append((chest.template_guid, chest_uid, remaining))
        _hc.log_req(f"    Opened chest {chest_uid} "
                f"({chest.template_guid or chest.set_guid}): {len(cards)} cards, "
                f"{len(inventory_rewards)} items, remaining={remaining}")
    return summary
