#!/usr/bin/env python3
"""Populate the Frost Ring Arena encounter catalogue from extracted records.

The Arena records are DeckTemplate records whose ``m_DeckName`` starts with
``Arena_``.  The deck template is the identifier used by the current server
when it starts an Arena match, so ``fra_encounters.deck_guid`` and the
corresponding ``encounter_deck_cards.deck_guid`` both use that GUID.

DeckTemplate does not contain the Arena roster's tier or boss classification.
Those fields are therefore nullable and can be supplied with a JSON
classification file when that information is recovered from server traffic or
other source data.  Example classification file::

    {
      "by_deck_guid": {
        "238dab27-acde-47e2-8310-30313b98ca52": {"is_boss": true, "tier": 1},
        "c85d8a03-ba8b-48d4-80fa-e34fa444aaa9": {"is_boss": true, "tier": null}
      }
    }

Usage::

    python3 AssetExtraction/populate_fra_encounters.py --dry-run
    python3 AssetExtraction/populate_fra_encounters.py --apply
    python3 AssetExtraction/populate_fra_encounters.py --apply \
        --classification fra_classifications.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDS_DIR = ROOT / "Records"
DEFAULT_DB = ROOT / "hconnect.db"
ARENA_PREFIX = "Arena_"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
FRA_DEFAULT_RANK_RANGE = (6, 19)

# One-based positions in a Frost Ring Arena run.  These are keyed by the
# DeckTemplate name because the rank range is roster metadata, not a field on
# DeckTemplate itself.  A supplied classification file may override them.
FRA_RANK_RANGES = {
    "Arena_Dragon_Guard_Stalwart": (1, 4),
    "Arena_Eternal_Guardian": (5, 5),
    "Arena_Mentor_of_the_Grave": (1, 4),
    "Arena_Oakhenge_Druid": (1, 4),
    "Arena_Psychotic_Anarchist": (1, 4),
    "Arena_Spam_Bot": (1, 4),
    "Arena_Storm_Cloud": (1, 4),
    "Arena_Uruunaaz": (17, 19),
    "Arena_Zakiir": (17, 19),
    "Arena_Hogarth": (20, 20),
}


def parse_jsonl(records_path: Path) -> list[dict[str, Any]]:
    """Read the JSON-string JSONL format emitted by extract_records.py."""
    rows: list[dict[str, Any]] = []
    with records_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip() or line.startswith('"$$$'):
                continue
            try:
                value: Any = json.loads(line)
                if isinstance(value, str):
                    value = json.loads(
                        re.sub(r",\s*([}\]])", r"\1", value),
                        strict=False,
                    )
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"cannot parse {records_path}:{line_number}: {exc}") from exc
            if isinstance(value, dict):
                rows.append(value)
    return rows


def guid(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("m_Guid") or "").lower()
    return ""


def integer(value: Any, default: int = 0) -> int:
    if isinstance(value, dict):
        value = value.get("m_Value", default)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def base_deck_name(deck_name: str) -> str:
    return deck_name[:-6] if deck_name.endswith("_Elite") else deck_name


def fallback_champion_name(deck_name: str) -> str:
    value = base_deck_name(deck_name)
    if value.startswith(ARENA_PREFIX):
        value = value[len(ARENA_PREFIX):]
    return value.replace("_", " ")


def load_classification(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    for key in ("by_deck_guid", "by_deck_name"):
        if key in value:
            value = value[key]
            break
    if not isinstance(value, dict):
        raise ValueError("classification JSON must be an object or contain by_deck_guid/by_deck_name")
    result = {}
    for key, classification in value.items():
        if not isinstance(classification, dict):
            raise ValueError(f"classification for {key!r} must be an object")
        row: dict[str, Any] = {}
        if "is_boss" in classification and classification["is_boss"] is not None:
            row["is_boss"] = 1 if bool(classification["is_boss"]) else 0
        if "tier" in classification and classification["tier"] is not None:
            row["tier"] = integer(classification["tier"], 0)
        if "min_rank" in classification and classification["min_rank"] is not None:
            row["min_rank"] = integer(classification["min_rank"], 0)
        if "max_rank" in classification and classification["max_rank"] is not None:
            row["max_rank"] = integer(classification["max_rank"], 0)
        result[str(key)] = row
    return result


def extract_rows(records_dir: Path, classifications: dict[str, dict[str, Any]]) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], list[str]]:
    deck_records = parse_jsonl(records_dir / "DeckTemplate.jsonl")
    champion_records = parse_jsonl(records_dir / "ChampionTemplate.jsonl")
    champion_names = {
        guid(record.get("m_Id")): str(record.get("m_Name") or "")
        for record in champion_records
        if guid(record.get("m_Id"))
    }

    encounter_rows: list[tuple[Any, ...]] = []
    card_counts: dict[tuple[str, str], list[Any]] = {}
    unresolved: list[str] = []

    for record in deck_records:
        deck_name = str(record.get("m_DeckName") or "")
        if not deck_name.startswith(ARENA_PREFIX):
            continue

        deck_guid = guid(record.get("m_Id"))
        champion_guid = guid(record.get("m_ChampionId"))
        if not deck_guid or not champion_guid:
            raise ValueError(f"Arena deck lacks deck/champion GUID: {deck_name!r}")

        champion_name = champion_names.get(champion_guid) or fallback_champion_name(deck_name)
        classification = {
            "min_rank": FRA_DEFAULT_RANK_RANGE[0],
            "max_rank": FRA_DEFAULT_RANK_RANGE[1],
        }
        default_rank = FRA_RANK_RANGES.get(deck_name)
        if default_rank:
            classification.update({"min_rank": default_rank[0], "max_rank": default_rank[1]})
        classification.update(
            classifications.get(deck_guid)
            or classifications.get(deck_name)
            or classifications.get(base_deck_name(deck_name))
            or {}
        )
        is_boss = classification.get("is_boss") if classification else None
        tier = classification.get("tier") if classification else None
        if is_boss is None or tier is None:
            unresolved.append(deck_name)

        equipment_ids = [
            value
            for value in (guid(item) for item in record.get("m_EquipmentIDs") or [])
            if value and value != ZERO_GUID
        ]
        sleeve_guid = guid(record.get("m_DeckSleeve"))
        resources = record.get("m_DeckResources") or []
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            card_guid = guid(resource.get("m_idTemplate"))
            quantity = integer(resource.get("m_Count"), 0)
            if card_guid and quantity > 0:
                key = (deck_guid, card_guid)
                entry = card_counts.setdefault(key, [0, [], False])
                gem_types = resource.get("m_GemTypesNewList") or []
                if gem_types:
                    if not entry[2]:
                        entry[1] = [[] for _ in range(entry[0])]
                    entry[1].extend(gem_types)
                    entry[2] = True
                elif entry[2]:
                    entry[1].extend([[] for _ in range(quantity)])
                entry[0] += quantity

        extra = {
            "m_DeckResources": resources,
            "m_EquipmentIDs": record.get("m_EquipmentIDs") or [],
            "m_DeckFlavor": record.get("m_DeckFlavor") or "",
            "m_DeckSleeve": record.get("m_DeckSleeve") or {"m_Guid": ZERO_GUID},
            "m_DontShuffleFirstNCards": integer(record.get("m_DontShuffleFirstNCards")),
            "m_MaximumDuplicates": integer(record.get("m_MaximumDuplicates")),
            "m_MaximumTotalCards": integer(record.get("m_MaximumTotalCards")),
            "m_SetId": record.get("m_SetId") or {"m_Guid": ZERO_GUID},
        }
        encounter_rows.append(
            (
                deck_guid,
                deck_name,
                champion_name,
                champion_guid,
                is_boss,
                tier,
                classification.get("min_rank"),
                classification.get("max_rank"),
                1 if deck_name.endswith("_Elite") or "Elite" in champion_name else 0,
                base_deck_name(deck_name),
                guid(record.get("m_SetId")),
                str(record.get("m_DeckFlavor") or ""),
                sleeve_guid,
                json_compact(equipment_ids),
                integer(record.get("m_DontShuffleFirstNCards")),
                integer(record.get("m_MaximumDuplicates")),
                integer(record.get("m_MaximumTotalCards")),
                None,
                None,
                None,
                None,
                json_compact(extra),
            )
        )

    encounter_rows.sort(key=lambda row: (row[1], row[0]))
    card_rows = sorted(
        (deck, card, values[0], json_compact(values[1]))
        for (deck, card), values in card_counts.items()
    )
    return encounter_rows, card_rows, sorted(set(unresolved))


ENCOUNTER_COLUMNS = (
    "deck_guid", "deck_name", "name", "champion_guid", "is_boss", "tier",
    "min_rank", "max_rank", "is_elite", "base_deck_name", "set_guid", "deck_flavor",
    "deck_sleeve_guid", "equipment_ids_json", "dont_shuffle_first_n_cards",
    "maximum_duplicates", "maximum_total_cards", "opening_hand_size",
    "encounter_deck_guid", "gameboard", "deck_texture", "metadata_json",
)


def apply_rows(db_path: Path, encounter_rows: list[tuple[Any, ...]], card_rows: list[tuple[Any, ...]]) -> tuple[int, int]:
    # DDL remains owned by static.py.  Executing its list here makes this
    # one-off extractor safe to run before the next server restart without
    # creating a second schema definition in the extraction package.
    sys.path.insert(0, str(ROOT))
    from static import DDL  # pylint: disable=import-outside-toplevel

    db = sqlite3.connect(db_path)
    try:
        for statement in DDL:
            db.execute(statement)
        columns = {item[1] for item in db.execute("PRAGMA table_info(fra_encounters)")}
        for column in ("min_rank", "max_rank"):
            if column not in columns:
                db.execute(
                    f"ALTER TABLE fra_encounters ADD COLUMN {column} INTEGER DEFAULT NULL"
                )
        db.execute("DELETE FROM fra_encounters")
        db.executemany(
            f"INSERT INTO fra_encounters ({','.join(ENCOUNTER_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in ENCOUNTER_COLUMNS)})",
            encounter_rows,
        )

        arena_decks = [row[0] for row in encounter_rows]
        if arena_decks:
            db.executemany(
                "DELETE FROM encounter_deck_cards WHERE deck_guid=?",
                ((deck_guid,) for deck_guid in arena_decks),
            )
        db.executemany(
            "INSERT INTO encounter_deck_cards "
            "(deck_guid, card_guid, quantity, gem_types_new_list_json) VALUES (?,?,?,?)",
            card_rows,
        )
        db.commit()
        return len(encounter_rows), len(card_rows)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def write_classification_template(path: Path, encounter_rows: list[tuple[Any, ...]]) -> None:
    template = {
        "by_deck_guid": {
            row[0]: {
                "deck_name": row[1],
                "name": row[2],
                "is_boss": None,
                "tier": None,
            }
            for row in encounter_rows
        }
    }
    path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--classification", type=Path)
    parser.add_argument("--write-classification-template", type=Path)
    parser.add_argument("--apply", action="store_true", help="write rows to --db")
    parser.add_argument("--dry-run", action="store_true", help="only report what would be written")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    classification = load_classification(args.classification)
    encounter_rows, card_rows, unresolved = extract_rows(args.records_dir, classification)
    if args.write_classification_template:
        write_classification_template(args.write_classification_template, encounter_rows)

    print(f"FRA encounters: {len(encounter_rows)}")
    print(f"FRA encounter card rows: {len(card_rows)}")
    print(f"FRA cards represented: {sum(row[2] for row in card_rows)}")
    if unresolved:
        print(f"Unclassified deck rows: {len(unresolved)} (is_boss/tier remain NULL)")
    if args.apply:
        encounters, cards = apply_rows(args.db, encounter_rows, card_rows)
        print(f"Applied to {args.db}: {encounters} encounters, {cards} card rows")
    elif not args.dry_run:
        print("Dry-run only; pass --apply to write the database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
