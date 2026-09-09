"""Synthetic campaign identities for headless campaign tests.

These fixtures deliberately contain no copied player progress.  The campaign
tests use the same authored AZ1 metadata as the server, then create fresh
users, champions, and area campaigns for the race branches they exercise.
"""

import campaign


CAMPAIGN_CHAMPIONS = (
    # IDs match the focused test scenarios, while names and races are fixed
    # fixture data rather than values read from the runtime database.
    (4, "test-vennen", 7, 2, 1),
    (6, "test-coyotle", 3, 6, 1),
    (7, "test-shinhare", 6, 3, 2),
)


def seed_campaign_fixtures(db):
    """Seed clean player identities and AZ1 area state into *db*."""
    db.execute(
        "INSERT OR IGNORE INTO users "
        "(id,name,auth_id,reck_id) VALUES (1,?,?,?)",
        ("campaign-test-user", "campaign-test-auth", "campaign-test-reck"),
    )
    champion_ids = tuple(item[0] for item in CAMPAIGN_CHAMPIONS)
    placeholders = ",".join("?" for _ in champion_ids)
    db.execute(
        f"DELETE FROM campaigns WHERE champion_id IN ({placeholders})",
        champion_ids,
    )
    db.execute(
        f"DELETE FROM champions WHERE id IN ({placeholders})",
        champion_ids,
    )
    for champion_id, name, race, champion_class, gender in CAMPAIGN_CHAMPIONS:
        db.execute(
            "INSERT INTO champions "
            "(id,user_id,champion_name,race,champion_class,gender,level) "
            "VALUES (?,?,?,?,?,?,1)",
            (champion_id, 1, name, race, champion_class, gender),
        )
    db.commit()

    for champion_id, _name, _race, _champion_class, _gender in CAMPAIGN_CHAMPIONS:
        campaign._activate_az1_area(db, champion_id)

    # The Shin'hare panorama test models the player after the opening Tamed
    # quest has been accepted.  Seed that quest state explicitly rather than
    # inheriting it from a live character.
    campaign._ensure_quest_campaign(db, 7, "AREA", "az01_tamed")
