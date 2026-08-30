"""Per-new-player initialization.

Runs once when a brand-new player account is created (not on login of an
existing account).  Grants a starting collection of basic shards and a few
booster packs so a fresh account has something to play with.

Granting is idempotent: callers only invoke this for newly-created users.
"""

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
    db.execute(
        "UPDATE users SET gold=?, platinum=? WHERE id=?",
        (STARTING_GOLD, STARTING_PLATINUM, user_id))

    # Allocate instance IDs for the granted cards.
    max_row = db.execute(
        "SELECT MAX(instance_id) FROM card_instances WHERE user_id=?",
        (user_id,)).fetchone()
    cid = max(max_row[0] + 1, 5000) if max_row and max_row[0] else 5000

    def grant_cards(guid, quantity):
        nonlocal cid
        # collections: template + quantity
        existing = db.execute(
            "SELECT id, quantity FROM collections WHERE user_id=? AND card_template_id=?",
            (user_id, guid)).fetchone()
        if existing:
            db.execute(
                "UPDATE collections SET quantity=? WHERE id=?",
                (existing[1] + quantity, existing[0]))
        else:
            db.execute(
                "INSERT INTO collections (user_id, card_template_id, quantity) VALUES (?,?,?)",
                (user_id, guid, quantity))
        # card_instances: one row per physical card
        for _ in range(quantity):
            db.execute(
                "INSERT OR IGNORE INTO card_instances (user_id, instance_id, template_guid) VALUES (?,?,?)",
                (user_id, cid, guid))
            cid += 1

    # 100 of each basic shard in the collection.
    for shard_guid in BASIC_SHARDS:
        grant_cards(shard_guid, SHARD_QUANTITY)

    # 1 "Lixil, Heartsworn" legendary in the collection.
    grant_cards(HEARTSWORN_GUID, HEARTSWORN_QUANTITY)

    # 3 Shards of Fate booster packs in the inventory.
    existing_pack = db.execute(
        "SELECT id, quantity FROM player_inventory WHERE user_id=? AND template_guid=?",
        (user_id, PACK_GUID)).fetchone()
    if existing_pack:
        db.execute(
            "UPDATE player_inventory SET quantity=? WHERE id=?",
            (existing_pack[1] + PACK_QUANTITY, existing_pack[0]))
    else:
        db.execute(
            "INSERT INTO player_inventory (user_id, template_guid, quantity) VALUES (?,?,?)",
            (user_id, PACK_GUID, PACK_QUANTITY))

    db.commit()
