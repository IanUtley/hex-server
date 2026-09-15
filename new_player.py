"""Per-new-player initialization.

Runs once when a brand-new player account is created (not on login of an
existing account).  Grants a starting collection of basic shards and a few
booster packs so a fresh account has something to play with.

Granting is idempotent: callers only invoke this for newly-created users.
"""

from profile_db import (
    db_grant_collection_cards,
    db_grant_inventory_item,
    db_initialize_new_player,
    db_insert_collection_card_instances,
    db_next_card_instance_id,
)

# The five basic threshold shards (Resource / Land).
BASIC_SHARDS = [
    "b253393b-fdde-47c4-9288-4b8efb0698b1",  # Blood Shard
    "6865d8d5-bd2e-43c6-8a68-53d1bde6bc28",  # Diamond Shard
    "1f897193-72a1-487e-a6bd-f3f6e7897c47",  # Ruby Shard
    "8554b2c8-cf48-467d-bf55-ab45e306ce43",  # Sapphire Shard
    "cd41bd00-7585-4762-a721-6163bdaee3c3",  # Wild Shard
]

# Starting quantity of each basic shard in the player's collection.
SHARD_QUANTITY = 100

# A single "Lixil, Heartsworn" legendary troop from the Frostheart set
# (Frostheart set GUID 326602fa; this is the PvP-legal printing).
HEARTSWORN_GUID = "ed564600-b44d-47bd-8b74-e0fe5100171a"
HEARTSWORN_QUANTITY = 1

# Set 1 "Shards of Fate" booster pack granted to new players (3 packs).
PACK_GUID = "a8b78207-686a-4994-b6cd-4548d1349841"
PACK_QUANTITY = 3

# Starting currency.
STARTING_GOLD = 10000
STARTING_PLATINUM = 10000


def grant_new_player(db, user_id):
    """Grant a fresh player their starting currency, shards and booster packs.

    db: open sqlite3 connection.
    user_id: the numeric player ID (hash of the full identity).

    Cards are written BOTH to collections (template + quantity, used by
    GetPlayerCardIDList) AND to card_instances
    (one row per physical card, which is what the client's collection is
    actually populated from via push_cards_to_client at login).
    """
    # Starting gold + platinum.
    db_initialize_new_player(user_id, STARTING_GOLD, STARTING_PLATINUM, conn=db)

    # Allocate instance IDs for the granted cards.
    cid = db_next_card_instance_id(user_id, conn=db)

    def grant_cards(guid, quantity):
        nonlocal cid
        # collections: template + quantity
        db_grant_collection_cards(user_id, guid, quantity, conn=db)
        cid = db_insert_collection_card_instances(
            user_id, cid, guid, quantity, conn=db)

    # 100 of each basic shard in the collection.
    for shard_guid in BASIC_SHARDS:
        grant_cards(shard_guid, SHARD_QUANTITY)

    # 1 "Lixil, Heartsworn" legendary in the collection.
    grant_cards(HEARTSWORN_GUID, HEARTSWORN_QUANTITY)

    # 3 Shards of Fate booster packs in the inventory.
    db_grant_inventory_item(user_id, PACK_GUID, PACK_QUANTITY, conn=db)

    db.commit()
