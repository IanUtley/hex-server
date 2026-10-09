"""Default talents and champion/race mappings used when a champion is created."""

import json
import sqlite3

from db import _db
from pve_db import db_default_talents, db_talent_data_ability_guid


def _talent_ability_guid(talent_guid: str) -> str | None:
    """Resolve a champion talent GUID to its actual ability GUID via DB."""
    return db_talent_data_ability_guid(talent_guid, conn=_db)


# ERace enum value -> deck name key
_RACE_DECK_MAP = {
    1: "Human", 2: "Elf", 3: "Coyotle", 4: "Orc",
    5: "Dwarf", 6: "ShinHare", 7: "Vennen", 8: "Necrotic",
}


_CHAMPION_CLASS_MAP = {
    1: "Mage", 2: "Warrior", 3: "Cleric", 4: "Rogue",
    5: "Warlock", 6: "Ranger", 7: "Boat",
}


_CHAMPION_GENDER_MAP = {1: "Male", 2: "Female", 3: "Other"}


def _default_talents_for_champion(race_val, class_val, gender_val):
    """Return the authored default talent list for a player template."""
    race_name = _RACE_DECK_MAP.get(int(race_val))
    class_name = _CHAMPION_CLASS_MAP.get(int(class_val))
    gender_name = _CHAMPION_GENDER_MAP.get(int(gender_val))
    if not race_name or not class_name or not gender_name:
        return []
    try:
        row = db_default_talents(
            race_name, class_name, gender_name, conn=_db)
    except sqlite3.Error:
        return []
    if not row:
        return []
    try:
        values = json.loads(row[0] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(value).lower() for value in values] if isinstance(values, list) else []
