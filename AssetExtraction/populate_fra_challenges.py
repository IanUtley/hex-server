#!/usr/bin/env python3
"""Extract Frost Ring Arena challenge conversations and modifications.

The challenge conversation is only the client-facing part of an Arena
modification.  The original Arena service separately returned an
``EncounterModBase`` list through ``GetArenaBattleMods``.  This extractor
keeps both pieces together in ``fra_challenges`` so the server can select a
challenge and translate ``modifications_json`` into the response objects.

Usage::

    python3 AssetExtraction/populate_fra_challenges.py --dry-run
    python3 AssetExtraction/populate_fra_challenges.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDS_DIR = ROOT / "Records"
DEFAULT_DB = ROOT / "hconnect.db"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
DEFAULT_PROBABILITY_PERCENT = 5

BONUS_CONVERSATION_NAMES = {
    "Health Buff Reward",
    "Health Buff Boss Notification",
    "Charge Buff Reward",
    "Charge Buff Boss Notification",
    "Resource Buff Reward",
    "Resource Buff Boss Notification",
    "Brawler Buff Reward",
    "Brawler Buff Boss Notification",
    "Challenge Win Strike Removal",
    "Perfected Tier Strike Removal",
    "Starting Health 15",
}


def unescape(value: str) -> str:
    """Decode a JSON string value from a raw extracted record."""
    return json.loads('"' + value + '"')


def field(record: str, name: str) -> str:
    match = re.search(
        rf'"{re.escape(name)}"\s*:\s*"((?:\\.|[^"\\])*)"', record
    )
    return unescape(match.group(1)) if match else ""


def guid_field(record: str, name: str) -> str:
    match = re.search(
        rf'"{re.escape(name)}"\s*:\s*\{{\s*"m_Guid"\s*:\s*"([^"]+)"',
        record,
    )
    return match.group(1).lower() if match else ""


def list_field(record: str, name: str) -> list[str]:
    return [unescape(value) for value in re.findall(
        rf'"{re.escape(name)}"\s*:\s*"((?:\\.|[^"\\])*)"', record
    )]


def integer_field(record: str, name: str) -> int:
    match = re.search(rf'"{re.escape(name)}"\s*:\s*(-?\d+)', record)
    return int(match.group(1)) if match else 0


def parse_card_templates(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Index card variants needed to resolve challenge card names.

    ``card_templates`` intentionally stores only the playable card projection,
    so it cannot distinguish a base card from an equipment-modified variant.
    The raw CardTemplate records retain that information and also carry the
    build tag used by the challenge conversations.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith('"$$$'):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"cannot parse {path}:{line_number}: {exc}") from exc
        if not isinstance(raw, str):
            continue
        name = field(raw, "m_Name")
        guid = guid_field(raw, "m_Id")
        if not name or not guid:
            continue
        result.setdefault(name.casefold(), []).append({
            "guid": guid,
            "build_tag": field(raw, "m_BuildTag"),
            "equipment_modified": integer_field(raw, "m_EquipmentModifiedCard"),
            "alternate_art": integer_field(raw, "m_HasAlternateArt"),
        })
    return result


def parse_conversations(path: Path) -> list[dict[str, Any]]:
    """Read raw JSON-string records without requiring strict JSON object keys."""
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith('"$$$'):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"cannot parse {path}:{line_number}: {exc}") from exc
        if not isinstance(raw, str):
            continue
        name = field(raw, "m_Name")
        owner_name = field(raw, "m_OwnerName")
        objective_texts = list_field(raw, "m_ObjectiveText")
        if owner_name != "Hogarth":
            continue
        if not objective_texts and name not in BONUS_CONVERSATION_NAMES:
            continue
        conversation_guid = guid_field(raw, "m_Id")
        if not conversation_guid:
            raise ValueError(f"challenge conversation lacks an ID: {name!r}")
        order_match = re.match(r"Challenge(\d+)", name)
        challenge_order = int(order_match.group(1)) if order_match else 1000
        if name in BONUS_CONVERSATION_NAMES:
            challenge_order = 1000 + sorted(BONUS_CONVERSATION_NAMES).index(name)
        result.append({
            "conversation_guid": conversation_guid,
            "challenge_key": slug(name),
            "challenge_name": name,
            "challenge_order": challenge_order,
            "owner_name": owner_name,
            "champion_guid": guid_field(raw, "m_ChampionId"),
            "build_tag": field(raw, "m_BuildTag"),
            "dialogue_text": (list_field(raw, "m_QuestionText") or [""])[0],
            "answer_text": (list_field(raw, "m_AnswerText") or [""])[0],
            "objective_heading": (list_field(raw, "m_ObjectiveHeading") or [""])[0],
            "objective_text": objective_texts[0] if objective_texts else "",
            "probability_percent": DEFAULT_PROBABILITY_PERCENT,
            "raw_name": name,
        })
    return sorted(result, key=lambda row: (row["challenge_order"], row["challenge_name"]))


def slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return value


def clean_card_name(value: str) -> str:
    """Remove the article included in natural-language challenge objectives."""
    return re.sub(r"^(?:a|an|the)\s+", "", value.strip(), flags=re.IGNORECASE)


def card_guid(
    db: sqlite3.Connection,
    card_index: dict[str, list[dict[str, Any]]],
    name: str,
    build_tag: str,
) -> str:
    name = clean_card_name(name)
    candidates = card_index.get(name.casefold(), [])
    if candidates:
        # Challenge cards use the base variant from their build.  Equipment
        # and alternate-art records share the display name but are different
        # card templates and must not be selected here.
        candidates = sorted(
            candidates,
            key=lambda card: (
                0 if card["build_tag"] == build_tag else 1,
                card["equipment_modified"],
                card["alternate_art"],
                card["guid"],
            ),
        )
        return candidates[0]["guid"]
    rows = db.execute(
        """
        SELECT guid
          FROM card_templates
         WHERE name=?
           AND no_pvp=0
           AND is_pve=0
         ORDER BY guid
        """,
        (name,),
    ).fetchall()
    if not rows:
        rows = db.execute(
            "SELECT guid FROM card_templates WHERE name=? ORDER BY guid", (name,)
        ).fetchall()
    return str(rows[0][0]) if rows else ""


def mod(conversation_id: str, target: str, kind: str, **values: Any) -> dict[str, Any]:
    result = {
        "type": kind,
        "conversation_id": conversation_id,
        "round_to_apply": 0,
        "target_player": target,
    }
    result.update(values)
    return result


def modifications(
    db: sqlite3.Connection,
    card_index: dict[str, list[dict[str, Any]]],
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    """Translate the extracted objective into response-ready mod descriptors."""
    cid = row["conversation_guid"]
    objective = row["objective_text"]
    name = row["challenge_name"]
    mods: list[dict[str, Any]] = []

    # These are the client-side effects attached to the boss notification,
    # not to the preceding reward conversation.  The conversation ID on the
    # mod makes IsModsEnabled/ApplyEncounterModification show the notification
    # before applying the effect.
    notification_mods = {
        "Health Buff Boss Notification": mod(
            cid, "UserPlayer", "EncounterModAddChampionHealth",
            amount=5, absolute=False,
        ),
        "Charge Buff Boss Notification": mod(
            cid, "UserPlayer", "EncounterModAddResource",
            max_resource_value=0, threshold_values=[], charge_value=2,
            absolute=False,
        ),
        "Resource Buff Boss Notification": mod(
            cid, "UserPlayer", "EncounterModAddResource",
            max_resource_value=1, threshold_values=[], charge_value=0,
            absolute=False,
        ),
        "Brawler Buff Boss Notification": mod(
            cid, "UserPlayer", "EncounterModAddCard",
            card_guid=card_guid(db, card_index, "Arena Brawler", row["build_tag"]),
            card_name="Arena Brawler", amount=1, collection="Warzone",
            location="Unknown", shuffle=False,
        ),
    }
    if name in notification_mods:
        return [notification_mods[name]]
    if name == "Starting Health 15":
        # The client has no negative AddChampionHealth operation: its
        # EncounterModAddChampionHealth implementation calls HealChampion,
        # which ignores negative amounts.  Keep this extracted rule in
        # metadata for the later tier-aware opponent-health implementation.
        return []
    if name in {
        "Health Buff Reward", "Charge Buff Reward", "Resource Buff Reward",
        "Brawler Buff Reward", "Challenge Win Strike Removal",
        "Perfected Tier Strike Removal",
    }:
        return []

    if "six Booby Traps" in objective:
        guid = card_guid(db, card_index, "Booby Trap", row["build_tag"])
        mods.append(mod(cid, "All", "EncounterModAddCard", card_guid=guid,
                        card_name="Booby Trap", amount=6, collection="Deck",
                        location="Unknown", shuffle=True))
    elif "gained 7 health and drew two cards" in objective:
        mods.extend([
            mod(cid, "AIPlayer", "EncounterModAddChampionHealth", amount=7, absolute=False),
            mod(cid, "AIPlayer", "EncounterModDrawCards"),
        ])
    elif "secret tunneled card" in objective:
        mods.append(mod(
            cid,
            "AIPlayer",
            "EncounterModAddCard",
            card_guid="",
            card_name="random card from opponent deck",
            amount=1,
            collection="Underground",
            location="Unknown",
            selection={"random": True, "source_collection": "Deck", "tunnel": True},
        ))
    elif "All actions have Runic" in objective:
        name = "Arena Challenge - Runic"
        mods.append(mod(cid, "All", "EncounterModAddCard", card_guid=card_guid(
                            db, card_index, name, row["build_tag"]),
                        card_name=name, amount=1, collection="CastSpells",
                        location="Unknown"))
    elif "random Banner" in objective:
        name = "Arena Challenge - Banners"
        mods.append(mod(cid, "All", "EncounterModAddCard", card_guid=card_guid(
                            db, card_index, name, row["build_tag"]),
                        card_name=name, amount=1, collection="CastSpells",
                        location="Unknown"))
    elif "each start the game with an Incantation" in objective:
        name = "Arena Incantation"
        mods.append(mod(cid, "All", "EncounterModAddCard", card_guid=card_guid(
                            db, card_index, name, row["build_tag"]),
                        card_name=name, amount=1, collection="CastSpells",
                        location="Unknown"))
    else:
        play_match = re.search(r"played (.+?)\. Goal:", objective)
        start_match = re.search(r"starts this game with (.+?) in play", objective)
        both_match = re.search(r"Both players start this game with (.+?) in play", objective)
        if play_match:
            name = play_match.group(1)
            clean_name = clean_card_name(name)
            mods.append(mod(cid, "AIPlayer", "EncounterModAddCard",
                            card_guid=card_guid(db, card_index, clean_name, row["build_tag"]),
                            card_name=clean_name, amount=1,
                            collection="CastSpells", location="Unknown"))
        elif both_match:
            name = both_match.group(1)
            clean_name = clean_card_name(name)
            mods.append(mod(cid, "All", "EncounterModAddCard",
                            card_guid=card_guid(db, card_index, clean_name, row["build_tag"]),
                            card_name=clean_name, amount=1,
                            collection="Warzone", location="Unknown"))
        elif start_match:
            name = start_match.group(1)
            clean_name = clean_card_name(name)
            mods.append(mod(cid, "AIPlayer", "EncounterModAddCard",
                            card_guid=card_guid(db, card_index, clean_name, row["build_tag"]),
                            card_name=clean_name, amount=1,
                            collection="Warzone", location="Unknown"))
        else:
            mods.append(mod(cid, "AIPlayer", "ArenaSpecial", effect="unclassified"))
    return mods


COLUMNS = (
    "conversation_guid", "challenge_key", "challenge_name", "challenge_order",
    "probability_percent",
    "owner_name", "champion_guid", "build_tag", "dialogue_text", "answer_text",
    "objective_heading", "objective_text", "modifications_json", "metadata_json",
    "enabled",
)


def apply(db_path: Path, records_dir: Path, dry_run: bool) -> int:
    sys.path.insert(0, str(ROOT))
    from static import DDL  # pylint: disable=import-outside-toplevel

    db = sqlite3.connect(db_path)
    try:
        for statement in DDL:
            db.execute(statement)
        # This script is also used directly against an existing checkout DB;
        # keep it usable between restarts while migration.py remains the
        # normal server-start schema path.
        for table, column, definition in (
            ("fra_challenges", "probability_percent",
             "INTEGER NOT NULL DEFAULT 5"),
        ):
            columns = {item[1] for item in db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conversations = parse_conversations(records_dir / "ConversationTemplate.jsonl")
        card_index = parse_card_templates(records_dir / "CardTemplate.jsonl")
        rows = []
        for row in conversations:
            mods = modifications(db, card_index, row)
            metadata = {
                "source": "ConversationTemplate",
                "trigger": "challenge",
                "probability_basis": "default_1_in_20",
            }
            name = row["challenge_name"]
            if name.endswith(" Reward"):
                notification_name = name[:-len(" Reward")] + " Boss Notification"
                notification = next(
                    (item for item in conversations
                     if item["challenge_name"] == notification_name),
                    None,
                )
                metadata.update({
                    "trigger": "reward",
                    "notification_conversation_guid": (
                        notification["conversation_guid"] if notification else ""
                    ),
                })
            elif name.endswith(" Boss Notification"):
                reward_name = name[:-len(" Boss Notification")] + " Reward"
                reward = next(
                    (item for item in conversations
                     if item["challenge_name"] == reward_name),
                    None,
                )
                metadata.update({
                    "trigger": "notification",
                    "reward_conversation_guid": (
                        reward["conversation_guid"] if reward else ""
                    ),
                })
            elif name == "Starting Health 15":
                metadata.update({
                    "trigger": "challenge",
                    "opponent_scope": "TierOne",
                    "health_adjustment": -7,
                })
            elif name in {"Challenge Win Strike Removal", "Perfected Tier Strike Removal"}:
                metadata.update({"trigger": "reward", "effect": "remove_strike"})
            rows.append(tuple(row[key] for key in COLUMNS[:12]) + (
                json.dumps(mods, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                1,
            ))
        if dry_run:
            for row in rows:
                print(row[2], "=>", row[12])
            return len(rows)
        db.execute("DELETE FROM fra_challenges")
        db.executemany(
            f"INSERT INTO fra_challenges ({','.join(COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in COLUMNS)})",
            rows,
        )
        db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    count = apply(args.db, args.records_dir, not args.apply)
    print(f"FRA challenges: {count}")
    if not args.apply:
        print("Dry-run only; pass --apply to write the database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
