#!/usr/bin/env python3
"""Generate the server's race starter decks from client gamedata.

The source DeckTemplate records contain the actual card lists.  A starter
deck is selected when it has exactly 60 cards, contains no Rare or Legendary
cards, and belongs to one of the eight playable races.  Clearly marked AI,
Arena, tutorial, and campaign decks are excluded; if more than one ordinary
deck qualifies for a race, the first one in the source data is selected.

Run from the repository root::

    python3 AssetExtraction/generate_starter_decks.py

The generated file is intentionally written below ``generated/``.  That
directory is ignored and is created by Docker at startup when the gamedata
source is available.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

# When this file is invoked by path, Python puts AssetExtraction (rather than
# the repository root) first on sys.path.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from AssetExtraction.gamedata_seed import (
    configured_path,
    load_records_text,
    load_text,
    nested_guid,
    records,
)


DEFAULT_OUTPUT = ROOT / "generated" / "starter_decks.json"
RACES = ("Human", "Elf", "Coyotle", "Orc", "Dwarf", "ShinHare", "Vennen", "Necrotic")

# These names are valid DeckTemplate records but are not player starter decks.
# Keep the exclusion based on deck metadata rather than card names.
EXCLUDED_PREFIXES = (
    "AI_",
    "Arena_",
    "AZ",
    "Crayburn Castle",
    "Demo_",
    "Starter",
    "Tutorial",
    "zPvE",
)


def _load_source(records_dir: Path | None, gamedata: Path | None) -> str:
    if gamedata is not None:
        return load_text(str(gamedata))
    if records_dir is not None:
        return load_records_text(records_dir)
    if configured_path():
        return load_text()
    return load_records_text()


def _card_rarities(data: str) -> dict[str, str]:
    return {
        nested_guid(record, "m_Id"): str(record.get("m_CardRarity") or "")
        for _, record in records(data, "CardTemplate")[0]
        if nested_guid(record, "m_Id")
    }


def _champion_races(data: str) -> dict[str, str]:
    return {
        nested_guid(record, "m_Id"): str(record.get("m_Race") or "")
        for _, record in records(data, "ChampionTemplate")[0]
        if nested_guid(record, "m_Id")
    }


def generate(data: str) -> dict[str, dict[str, Any]]:
    rarities = _card_rarities(data)
    champion_races = _champion_races(data)
    selected: dict[str, dict[str, Any]] = {}
    candidate_counts = {race: 0 for race in RACES}

    for _, deck in records(data, "DeckTemplate")[0]:
        name = str(deck.get("m_DeckName") or "")
        if not name or name.startswith(EXCLUDED_PREFIXES):
            continue

        race = champion_races.get(nested_guid(deck, "m_ChampionId"))
        if race not in RACES:
            continue

        resources = deck.get("m_DeckResources") or []
        cards: list[list[Any]] = []
        total = 0
        invalid = False
        for resource in resources:
            if not isinstance(resource, dict):
                invalid = True
                break
            card_guid = nested_guid(resource, "m_idTemplate")
            count = int(resource.get("m_Count") or 0)
            rarity = rarities.get(card_guid, "")
            if not card_guid or count <= 0 or not rarity:
                invalid = True
                break
            if rarity.lower() in {"rare", "legendary"}:
                invalid = True
                break
            cards.append([card_guid, count])
            total += count

        if invalid or total != 60:
            continue

        candidate_counts[race] += 1
        if race in selected:
            continue
        selected[race] = {"deck_name": name, "cards": cards}

    missing = [race for race in RACES if race not in selected]
    if missing:
        raise RuntimeError("No qualifying 60-card starter deck found for: " + ", ".join(missing))

    # Preserve the stable race order used by the server and JSON output.
    result = {race: selected[race] for race in RACES}
    for race in RACES:
        print(f"{race}: {result[race]['deck_name']} ({candidate_counts[race]} eligible)")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-dir", type=Path, help="Records directory to read")
    parser.add_argument("--gamedata", type=Path, help="Compressed client gamedata file to read")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"Generated JSON path (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args()

    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    result = generate(_load_source(args.records_dir, args.gamedata))
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(result)} starter decks to {output}")


if __name__ == "__main__":
    main()
