#!/usr/bin/env python3
"""Evaluate and persist strategy profiles for the authored FRA decks.

Card classification comes from typed Records in ``ai_deck_strategy``.  The
result is saved on ``fra_encounters`` and copied to each user's
``fra_challengers`` roster so encounter startup only reads persisted data.

Usage::

    python3 AssetExtraction/evaluate_fra_deck_personalities.py
    python3 AssetExtraction/evaluate_fra_deck_personalities.py --db /path/to/hconnect.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "hconnect.db"
_EVALUATION_LOCK = threading.RLock()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_deck_strategy import evaluate_deck_strategy


def update_fra_deck_personalities(
    db: sqlite3.Connection,
    *,
    force: bool = False,
    output: Callable[[str], None] | None = None,
) -> int:
    """Evaluate missing or all FRA deck profiles and sync saved rosters.

    A deck without a strong strategy signal is stored as ``Default``.  The
    caller owns transaction commit/rollback.  Returns the number of encounter
    rows evaluated.
    """
    lock = getattr(db, "_retry_lock", _EVALUATION_LOCK)
    guard = lock if hasattr(lock, "__enter__") else nullcontext()
    evaluated = 0
    with guard:
        rows = db.execute(
            "SELECT deck_guid, deck_name, ai_deck_personality "
            "FROM fra_encounters ORDER BY deck_name, deck_guid"
        ).fetchall()
        profiles = {}
        for deck_guid, deck_name, current in rows:
            if not force and current and str(current).strip():
                profiles[str(deck_guid).lower()] = str(current).strip()
                continue
            cards = db.execute(
                "SELECT card_guid, quantity FROM encounter_deck_cards "
                "WHERE deck_guid=? ORDER BY card_guid",
                (deck_guid,),
            ).fetchall()
            result = evaluate_deck_strategy(cards)
            personality = result.personality or "Default"
            profiles[str(deck_guid).lower()] = personality
            db.execute(
                "UPDATE fra_encounters SET ai_deck_personality=? "
                "WHERE deck_guid=?",
                (personality, deck_guid),
            )
            evaluated += 1
            if output is not None:
                score_summary = ", ".join(
                    f"{name}={score:.1f}/10"
                    for name, score in result.scores.items())
                output(f"{deck_name}: {personality} — {score_summary}; "
                       f"gap={result.features.get('strategy_score_gap', 0):.1f}")

        scene_decks = db.execute(
            "SELECT ai_deck_guid, "
            "MAX(NULLIF(TRIM(ai_deck_personality), '')), MIN(name) "
            "FROM encounter_scenes WHERE ai_deck_guid IS NOT NULL "
            "AND TRIM(ai_deck_guid)<>'' GROUP BY ai_deck_guid "
            "ORDER BY MIN(name), ai_deck_guid"
        ).fetchall()
        for deck_guid, current, scene_name in scene_decks:
            key = str(deck_guid).lower()
            personality = profiles.get(key)
            if personality is None and not force and current:
                personality = str(current).strip()
                profiles[key] = personality
                db.execute(
                    "UPDATE encounter_scenes SET ai_deck_personality=? "
                    "WHERE ai_deck_guid=? AND "
                    "(ai_deck_personality IS NULL OR TRIM(ai_deck_personality)='' "
                    "OR TRIM(ai_deck_personality)<>?)",
                    (personality, deck_guid, personality),
                )
                continue
            if personality is None:
                cards = db.execute(
                    "SELECT card_guid, quantity FROM encounter_deck_cards "
                    "WHERE deck_guid=? ORDER BY card_guid",
                    (deck_guid,),
                ).fetchall()
                result = evaluate_deck_strategy(cards)
                personality = result.personality or "Default"
                profiles[key] = personality
                evaluated += 1
                if output is not None:
                    score_summary = ", ".join(
                        f"{name}={score:.1f}/10"
                        for name, score in result.scores.items())
                    output(f"{scene_name} [{deck_guid}]: {personality} — "
                           f"{score_summary}; "
                           f"gap={result.features.get('strategy_score_gap', 0):.1f}")
            db.execute(
                "UPDATE encounter_scenes SET ai_deck_personality=? "
                "WHERE ai_deck_guid=? AND "
                "(ai_deck_personality IS NULL OR TRIM(ai_deck_personality)='' "
                "OR TRIM(ai_deck_personality)<>?)",
                (personality, deck_guid, personality),
            )

        db.execute(
            "UPDATE fra_challengers "
            "SET ai_deck_personality=COALESCE(("
            "SELECT NULLIF(TRIM(e.ai_deck_personality), '') "
            "FROM fra_encounters AS e "
            "WHERE e.deck_guid=fra_challengers.encounter_deck_guid"
            "), 'Default') "
            "WHERE COALESCE(NULLIF(TRIM(ai_deck_personality), ''), 'Default') "
            "<> COALESCE((SELECT NULLIF(TRIM(e.ai_deck_personality), '') "
            "FROM fra_encounters AS e "
            "WHERE e.deck_guid=fra_challengers.encounter_deck_guid), 'Default')"
            " OR ai_deck_personality IS NULL OR TRIM(ai_deck_personality)=''"
        )
    return evaluated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--missing-only", action="store_true",
        help="leave existing personality assignments unchanged",
    )
    args = parser.parse_args()

    if not args.db.exists():
        parser.error(f"database does not exist: {args.db}")

    import static

    db = sqlite3.connect(args.db)
    try:
        static.ensure_schema(db)
        count = update_fra_deck_personalities(
            db, force=not args.missing_only, output=print)
        db.commit()
        print(f"Evaluated {count} FRA and encounter deck profile(s)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
