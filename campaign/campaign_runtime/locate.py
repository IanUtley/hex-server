"""Which campaign zone a champion's campaign row is showing.

A rules-driven row records its zone in ``state["RulesZone"]``. Rows written by
the legacy code are recognised by their type and markers: the race starter
panorama (AZ0), Castle Crayburn, the AZ1 overworld and the AZ1 panoramas.
"""

from __future__ import annotations

from typing import Any

from campaign_runtime import catalog_data

RACE_NAMES = {1: "Human", 2: "Elf", 3: "Coyotle", 4: "Orc", 5: "Dwarf",
              6: "Shin'hare", 7: "Vennen", 8: "Necrotic"}
FACTIONS = {1: "Ardent", 2: "Ardent", 3: "Ardent", 4: "Ardent",
            5: "Underworld", 6: "Underworld", 7: "Underworld", 8: "Underworld"}
ROW_TYPES = {"overworld": "AREA", "dungeon": "DUNGEON", "panorama": "PANORAMA"}


def starter_zone(race_id: Any) -> str | None:
    """The race's AZ0 starter panorama zone (``az0-shinhare`` ...)."""
    race = RACE_NAMES.get(int(race_id or 0)) if str(race_id or "").isdigit() else race_id
    return next((z["id"] for z in catalog_data.zones()
                 if z.get("server_template") == "AZ0" and z.get("race") == race), None)


def child_zones(zone_id: str) -> dict[str, dict[str, Any]]:
    """``{parent node: child zone}`` for the dungeons/panoramas a zone leads to."""
    return {z["parent_node"]: z for z in catalog_data.zones()
            if z.get("parent") == zone_id and z.get("parent_node")}


def zone_for_row(ctype: str, template_name: str, state: dict[str, Any],
                 race_id: Any) -> str | None:
    """The zone a campaign row shows (None: not a rules-modelled zone)."""
    if state.get("RulesZone"):
        return str(state["RulesZone"])
    ctype = str(ctype or "").upper()
    template = str(template_name or "")
    if ctype == "PANORAMA":
        node = state.get("PanoramaNode")
        if node:
            child = child_zones("AZ1").get(str(node))
            return child["id"] if child else None
        return starter_zone(race_id)
    for entry in catalog_data.zones():
        if (ROW_TYPES.get(entry.get("kind")) == ctype and
                entry.get("server_template") == template):
            return entry["id"]
    return None
