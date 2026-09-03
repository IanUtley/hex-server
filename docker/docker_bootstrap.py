"""Prepare the database before the container starts the network services.

The image intentionally does not contain a database snapshot. A Docker
deployment can point ``HEX_DB_PATH`` at persistent storage. If that file does
not exist, this module creates it from the server schema and the mounted
gamedata blob or a mounted ``Records/`` snapshot. Tests run only after a new
database is created.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


# This file is under docker/ in the source tree, while the server sources,
# tests, and bundled database remain at the repository/image root.
ROOT = Path(__file__).resolve().parents[1]
# The entrypoint invokes this file by absolute path, which makes Python place
# /hex/docker (rather than /hex) first on sys.path.  Add the application root
# explicitly so the bootstrap uses the same imports as the server process.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# These tables are the minimum non-empty reference catalog required by the
# server.  A fresh database with an empty or partially parsed gamedata source
# is not usable, even if schema creation itself succeeded.
REQUIRED_STATIC_TABLES = (
    "card_templates",
    "card_abilities_meta",
    "ability_effects",
    "target_templates",
    "talent_data",
    "talent_abilities",
    "ability_effect_conditions",
    "card_counter_templates",
    "gem_templates",
    "champion_abilities",
    "champion_templates_extended",
    "champion_template_data",
    "champion_templates",
    "champion_class_data",
    "encounter_scenes",
    "encounter_deck_cards",
    "campaign_node_conversations",
    "quest_templates",
    "quest_conversations",
    "chest_templates",
    "pack_set_map",
    "store_items",
    "tournament_types",
    "chest_probabilities",
    "redeem_codes",
    "conversation_rewards",
)


def database_path() -> Path:
    value = os.environ.get("HEX_DB_PATH", str(ROOT / "hconnect.db"))
    path = Path(os.path.expanduser(value))
    return path if path.is_absolute() else ROOT / path


def gamedata_path() -> Path | None:
    value = os.environ.get("HEX_GAMEDATA") or os.environ.get("GAMEDATA")
    if not value:
        return None
    return Path(os.path.abspath(os.path.expanduser(value)))


def records_path() -> Path:
    value = os.environ.get("HEX_RECORDS")
    if value:
        return Path(os.path.abspath(os.path.expanduser(value)))
    return ROOT / "Records"


def validate_data_sources(path: Path | None, records: Path, *, database_missing: bool) -> None:
    if path is not None and not path.is_file():
        raise RuntimeError(
            f"Configured gamedata file does not exist: {path}. "
            "Mount the client's Data/gamedata file and set HEX_GAMEDATA."
        )
    if database_missing and path is None:
        from AssetExtraction.gamedata_seed import records_available

        if records_available(records):
            return
        raise RuntimeError(
            f"Database does not exist and neither gamedata nor a complete "
            f"Records source was found for {database_path()}. Set HEX_GAMEDATA "
            "or mount a complete Records/ directory before the first container "
            "start."
        )


def create_records(path: Path, records: Path) -> None:
    """Materialize the JSONL source used by offline/database seed helpers."""
    records.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["GAMEDATA"] = str(path)
    environment["RECORDS_DIR"] = str(records)
    print(f"[docker] creating Records source {records}", flush=True)
    subprocess.run(
        [sys.executable, str(ROOT / "AssetExtraction" / "extract_records.py")],
        cwd=str(ROOT),
        env=environment,
        check=True,
    )


def create_starter_decks(gamedata: Path | None, records: Path) -> None:
    """Generate the race starter-deck catalog outside the image build."""
    output = ROOT / "generated" / "starter_decks.json"
    command = [
        sys.executable,
        str(ROOT / "AssetExtraction" / "generate_starter_decks.py"),
        "--output",
        str(output),
    ]
    if gamedata is not None:
        command.extend(("--gamedata", str(gamedata)))
    else:
        from AssetExtraction.gamedata_seed import records_available

        if not records_available(records):
            print(
                "[docker] starter-deck generation skipped: no gamedata or "
                "complete Records source",
                flush=True,
            )
            return
        command.extend(("--records-dir", str(records)))

    print(f"[docker] generating starter decks at {output}", flush=True)
    subprocess.run(command, cwd=str(ROOT), check=True)


def _ensure_database_schema(connection: sqlite3.Connection) -> None:
    """Apply the current DDL and idempotent static seeds to *connection*."""
    # Keep Docker databases consistent with the runtime database connection.
    # WAL permits readers (including replay) to continue while HConnect or the
    # tournament scheduler is writing, while the busy timeout lets short
    # writer collisions resolve instead of failing.
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    import static

    static.ensure_schema(connection)


def create_database(path: Path) -> None:
    """Create a new database atomically from the current schema and seeds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".bootstrap", dir=path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    connection = None
    try:
        connection = sqlite3.connect(str(temporary), timeout=30.0)
        _ensure_database_schema(connection)
        connection.close()
        connection = None
        os.replace(temporary, path)
    except Exception:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
        raise


def upgrade_database(path: Path) -> None:
    """Apply current schema/data changes to an existing persistent database."""
    connection = sqlite3.connect(str(path), timeout=30.0)
    try:
        _ensure_database_schema(connection)
    finally:
        connection.close()


def validate_static_data(path: Path) -> None:
    """Fail startup if fresh database reference data is missing or empty."""
    connection = sqlite3.connect(str(path), timeout=30.0)
    try:
        missing = []
        for table in REQUIRED_STATIC_TABLES:
            try:
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.Error:
                missing.append(f"{table}=missing")
                continue
            if not count:
                missing.append(f"{table}=empty")
        if missing:
            raise RuntimeError(
                "fresh database static data validation failed: "
                + ", ".join(missing)
            )
        print(
            "[docker] static data validated: "
            + ", ".join(
                f"{table}={connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}"
                for table in REQUIRED_STATIC_TABLES
            ),
            flush=True,
        )
    finally:
        connection.close()


def run_tests(path: Path) -> None:
    if os.environ.get("HEX_RUN_TESTS_ON_BOOT", "1").lower() in {
        "0", "false", "no", "off"
    }:
        print("[docker] HEX_RUN_TESTS_ON_BOOT disables startup tests", flush=True)
        return

    test_env = os.environ.copy()
    test_env["HEX_TEST_SOURCE_DB"] = str(path)
    runner = ROOT / "tests" / "run_all.py"
    if not runner.is_file():
        raise RuntimeError(f"Test runner is missing: {runner}")

    print("[docker] running the supported test suite", flush=True)
    result = subprocess.run(
        [sys.executable, str(runner)],
        cwd=str(ROOT),
        env=test_env,
        check=False,
    )
    failures = [runner.relative_to(ROOT)] if result.returncode else []

    if failures:
        print(
            "[docker] test failures: "
            + ", ".join(str(test_file) for test_file in failures),
            flush=True,
        )
        if os.environ.get("HEX_FAIL_ON_TEST_FAILURE", "0").lower() in {
            "1", "true", "yes", "on"
        }:
            raise RuntimeError("startup tests failed")
    else:
        print("[docker] all startup tests passed", flush=True)


def main() -> int:
    path = database_path()
    configured_gamedata = gamedata_path()
    configured_records = records_path()
    exists = path.exists()
    if exists and not path.is_file():
        raise RuntimeError(f"Database path is not a file: {path}")

    validate_data_sources(
        configured_gamedata, configured_records, database_missing=not exists
    )
    if not exists and configured_gamedata is not None:
        from AssetExtraction.gamedata_seed import records_available

        if not records_available(configured_records):
            create_records(configured_gamedata, configured_records)
    create_starter_decks(configured_gamedata, configured_records)
    if exists:
        print(f"[docker] using existing database {path}", flush=True)
        # Persistent deployments must receive new columns, indexes, and
        # idempotent server-owned seeds before any service opens the DB. This
        # updates the database in place and never replaces player state.
        os.environ["HEX_DB_PATH"] = str(path)
        print(f"[docker] applying current schema and seeds to {path}", flush=True)
        upgrade_database(path)
        validate_static_data(path)
        return 0

    if configured_gamedata is None:
        print(f"[docker] using Records source {configured_records}", flush=True)
    else:
        print(f"[docker] using gamedata {configured_gamedata}", flush=True)

    print(f"[docker] creating database {path}", flush=True)
    os.environ["HEX_DB_PATH"] = str(path)
    create_database(path)
    print(f"[docker] database created at {path}", flush=True)
    validate_static_data(path)
    run_tests(path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"[docker] bootstrap failed: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
