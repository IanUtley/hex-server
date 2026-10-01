"""Focused tests for Docker bootstrap reference-data checks."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker"))

import docker_bootstrap


def _create_fra_catalog(path: Path, *, complete: bool) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE fra_encounters (
                deck_guid TEXT PRIMARY KEY,
                min_rank INTEGER,
                max_rank INTEGER,
                is_elite INTEGER
            );
            CREATE TABLE encounter_deck_cards (
                deck_guid TEXT,
                card_guid TEXT
            );
            CREATE TABLE fra_challenges (
                enabled INTEGER
            );
            """
        )
        if complete:
            connection.execute(
                "INSERT INTO fra_encounters VALUES (?, ?, ?, ?)",
                ("deck", 1, 4, 0),
            )
            connection.execute(
                "INSERT INTO encounter_deck_cards VALUES (?, ?)",
                ("deck", "card"),
            )
            connection.execute("INSERT INTO fra_challenges VALUES (1)")
        connection.commit()
    finally:
        connection.close()


def test_required_static_tables_include_fra_catalogues():
    assert "fra_encounters" in docker_bootstrap.REQUIRED_STATIC_TABLES
    assert "fra_challenges" in docker_bootstrap.REQUIRED_STATIC_TABLES


def test_fra_catalog_check_requires_rank_one_cards_and_enabled_challenges(tmp_path):
    incomplete = tmp_path / "incomplete.db"
    _create_fra_catalog(incomplete, complete=False)
    assert docker_bootstrap._fra_catalog_needs_population(incomplete) == (True, True)

    complete = tmp_path / "complete.db"
    _create_fra_catalog(complete, complete=True)
    assert docker_bootstrap._fra_catalog_needs_population(complete) == (False, False)


if __name__ == "__main__":
    import tempfile

    test_required_static_tables_include_fra_catalogues()
    with tempfile.TemporaryDirectory() as directory:
        test_fra_catalog_check_requires_rank_one_cards_and_enabled_challenges(
            Path(directory)
        )
    print("Docker bootstrap tests passed")
