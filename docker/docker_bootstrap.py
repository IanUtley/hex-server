"""Prepare the database before the container starts the network services.

The image intentionally does not contain a database snapshot. A Docker
deployment can point ``HEX_DB_PATH`` at persistent storage. If that file does
not exist, this module creates it from the server schema and the mounted
gamedata blob or a mounted ``hex-private/Records/`` snapshot. Tests run only after a new
database is created.

An existing database is upgraded in place through ``static.ensure_schema``,
which also re-syncs its client-derived projection (card, ability, champion and
encounter rows) from the mounted gamedata or ``hex-private/Records/`` source whenever that
snapshot has moved on — player progress is never part of that projection.

The Frost Ring Arena encounter and challenge catalogues are maintained by
standalone extractors, so this bootstrap also fills them when they are absent
or incomplete.  Existing usable FRA data is left untouched.
"""

from __future__ import annotations

import json
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
    "fra_encounters",
    "fra_challenges",
    "campaign_node_conversations",
    "quest_templates",
    "quest_conversations",
    "chest_templates",
    "inventory_item_templates",
    "pack_set_map",
    "store_items",
    "tournament_types",
    "chest_probabilities",
    "redeem_codes",
    "conversation_rewards",
)


FRA_ENCOUNTER_RECORD_FILES = (
    "DeckTemplate.jsonl",
    "ChampionTemplate.jsonl",
)
FRA_CHALLENGE_RECORD_FILES = (
    "ConversationTemplate.jsonl",
    "CardTemplate.jsonl",
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
    return ROOT / "hex-private" / "Records"


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
            "or mount a complete hex-private/Records/ directory before the first container "
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


# The campaign editor's art comes from the client install, not the gamedata.
# Each extractor writes one index; a missing index means that art is missing.
EDITOR_GENERATED = ROOT / "campaign_editor" / "generated"


def _zone_files_missing() -> bool:
    """A zone's map (or panorama) file is missing, or the zone list is.

    Those files hold the zone's background tiles, particle emitters and
    parallax layers, so each one is checked, not just the zone list.
    """
    zones_path = EDITOR_GENERATED / "zones.json"
    if not zones_path.is_file():
        return True
    zones = json.loads(zones_path.read_text(encoding="utf-8")).get("zones") or []
    return any(not (EDITOR_GENERATED / zone["id"] / (
        "panorama.json" if zone.get("kind") == "panorama" else "map.json")).is_file()
        for zone in zones)


EDITOR_ART_EXTRACTORS = (
    ("cards/index.json", lambda: not (EDITOR_GENERATED / "cards" / "index.json").is_file(),
     "extract_card_art.py", ()),
    ("items/index.json", lambda: not (EDITOR_GENERATED / "items" / "index.json").is_file(),
     "extract_item_icons.py", ()),
    ("zones and zone files", _zone_files_missing,
     "extract_campaign_editor_assets.py", ("--records",)),
)


# The container's client mount is the Hex install root (the folder that holds
# ``Hex_Data`` and ``AssetBundles``), always at this path.
CLIENT_ROOT = Path("/client-data")


def client_install() -> Path | None:
    """The Hex install root mounted at ``/client-data``, if it is there."""
    return CLIENT_ROOT if (CLIENT_ROOT / "Hex_Data" / "resources.assets").is_file() else None


def ensure_editor_art(records: Path) -> None:
    """Generate the campaign editor's missing art from the mounted client.

    The image never bundles this art. A missing index is generated when the
    client install is mounted; without it the editor runs with less art. A
    failed extraction is logged and does not stop startup.
    """
    client = client_install()
    scripts = ROOT / "campaign" / "AssetExtraction"
    for label, is_missing, script, extra in EDITOR_ART_EXTRACTORS:
        if not is_missing():
            continue
        index = label
        if client is None:
            print(f"[docker] editor art {index} skipped: no Hex install mounted "
                  "at /client-data", flush=True)
            continue
        command = [sys.executable, str(scripts / script), "--client-root", str(client)]
        if "--records" in extra:
            command.extend(("--records", str(records)))
        print(f"[docker] generating editor art {index} from {client}", flush=True)
        try:
            subprocess.run(command, cwd=str(ROOT), check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"[docker] editor art {index} failed: {exc}", flush=True)


def _ensure_database_schema(connection: sqlite3.Connection) -> None:
    """Apply the current DDL and idempotent static seeds to *connection*."""
    # Keep Docker databases consistent with the runtime database connection.
    # WAL permits readers (including replay) to continue while HConnect or the
    # tournament scheduler is writing, while the five-second busy timeout lets
    # short writer collisions resolve instead of failing.
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    # static.ensure_schema imports a couple of reusable DML helpers from db.py.
    # Importing db.py normally initializes its own connection and calls
    # ensure_schema again. Mark this scoped bootstrap pass so that import only
    # provides the helpers, without recursively running another seed pass.
    database_path = next(
        (row[2] for row in connection.execute("PRAGMA database_list")
         if row[1] == "main" and row[2]),
        None,
    )
    previous_db_path = os.environ.get("HEX_DB_PATH")
    previous_bootstrap_marker = os.environ.get("HEX_DB_BOOTSTRAPPED")
    if database_path:
        os.environ["HEX_DB_PATH"] = database_path
    os.environ["HEX_DB_BOOTSTRAPPED"] = "1"
    try:
        import static

        static.ensure_schema(connection)
    finally:
        # The helper import's connection points at this same file. Close it
        # before create_database renames a temporary bootstrap database.
        database_module = sys.modules.get("db")
        shared_connection = getattr(database_module, "_db", None)
        if shared_connection is not None and shared_connection is not connection:
            try:
                shared_connection.close()
            except sqlite3.Error:
                pass
        if previous_db_path is None:
            os.environ.pop("HEX_DB_PATH", None)
        else:
            os.environ["HEX_DB_PATH"] = previous_db_path
        if previous_bootstrap_marker is None:
            os.environ.pop("HEX_DB_BOOTSTRAPPED", None)
        else:
            os.environ["HEX_DB_BOOTSTRAPPED"] = previous_bootstrap_marker


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
        connection = sqlite3.connect(str(temporary), timeout=5.0)
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
    """Apply current schema/data changes to an existing persistent database.

    This is the container's client-data refresh point: ``ensure_schema`` owns
    both the schema and the Records-derived projection, so a release that
    changes game data brings the persistent database with it instead of
    requiring a fresh database.
    """
    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        _ensure_database_schema(connection)
    finally:
        connection.close()


def _scalar_count(connection: sqlite3.Connection, query: str) -> int | None:
    """Return a count, treating a missing table/column as unavailable."""
    try:
        row = connection.execute(query).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0] or 0) if row else 0


def _fra_catalog_needs_population(path: Path) -> tuple[bool, bool]:
    """Return ``(encounters_needed, challenges_needed)`` for *path*.

    A non-empty encounter table is not sufficient: roster selection starts at
    rank one and every selected deck must have card rows.  Likewise, disabled
    challenge rows cannot satisfy the Arena challenge lookup.
    """
    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        encounter_count = _scalar_count(
            connection, "SELECT COUNT(*) FROM fra_encounters")
        rank_one_count = _scalar_count(
            connection,
            "SELECT COUNT(*) FROM fra_encounters "
            "WHERE COALESCE(min_rank, 6) <= 1 "
            "AND COALESCE(max_rank, 19) >= 1 "
            "AND COALESCE(is_elite, 0) = 0",
        )
        encounters_without_cards = _scalar_count(
            connection,
            "SELECT COUNT(*) FROM fra_encounters AS e "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM encounter_deck_cards AS c "
            "WHERE c.deck_guid=e.deck_guid)",
        )
        challenge_count = _scalar_count(
            connection,
            "SELECT COUNT(*) FROM fra_challenges WHERE enabled=1",
        )
    finally:
        connection.close()

    encounters_needed = (
        encounter_count is None
        or encounter_count == 0
        or rank_one_count is None
        or rank_one_count == 0
        or encounters_without_cards is None
        or encounters_without_cards > 0
    )
    challenges_needed = challenge_count is None or challenge_count == 0
    return encounters_needed, challenges_needed


def _require_records_files(records: Path, files: tuple[str, ...], catalog: str) -> None:
    missing = [name for name in files if not (records / name).is_file()]
    if not missing:
        return
    joined = ", ".join(missing)
    raise RuntimeError(
        f"FRA {catalog} data is missing ({joined}) under {records}. "
        "Mount a complete hex-private/Records/ directory or set HEX_GAMEDATA so the "
        "bootstrap can regenerate it."
    )


def populate_missing_fra_data(path: Path, records: Path) -> None:
    """Populate standalone FRA catalogues without replacing usable data."""
    encounters_needed, challenges_needed = _fra_catalog_needs_population(path)
    if not encounters_needed and not challenges_needed:
        print("[docker] FRA catalogues already populated", flush=True)
        return

    if encounters_needed:
        _require_records_files(records, FRA_ENCOUNTER_RECORD_FILES, "encounter")
        from AssetExtraction.populate_fra_encounters import (
            apply_rows,
            extract_rows,
        )

        print("[docker] populating FRA encounters", flush=True)
        encounter_rows, card_rows, unresolved = extract_rows(records, {})
        if not encounter_rows or not card_rows:
            raise RuntimeError(
                "FRA encounter extraction produced no encounter or deck-card "
                f"rows from {records}"
            )
        encounter_count, card_count = apply_rows(
            path, encounter_rows, card_rows)
        print(
            f"[docker] populated FRA encounters: encounters={encounter_count} "
            f"deck_cards={card_count} unresolved={len(unresolved)}",
            flush=True,
        )

    if challenges_needed:
        _require_records_files(records, FRA_CHALLENGE_RECORD_FILES, "challenge")
        from AssetExtraction.populate_fra_challenges import apply

        print("[docker] populating FRA challenges", flush=True)
        challenge_count = apply(path, records, dry_run=False)
        if not challenge_count:
            raise RuntimeError(
                f"FRA challenge extraction produced no rows from {records}"
            )
        print(
            f"[docker] populated FRA challenges: rows={challenge_count}",
            flush=True,
        )


def validate_static_data(path: Path) -> None:
    """Fail startup if required database reference data is missing or empty."""
    connection = sqlite3.connect(str(path), timeout=5.0)
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
                "database static data validation failed: "
                + ", ".join(missing)
            )
        encounters_needed, challenges_needed = _fra_catalog_needs_population(path)
        if encounters_needed:
            raise RuntimeError(
                "database static data validation failed: "
                "fra_encounters=incomplete"
            )
        if challenges_needed:
            raise RuntimeError(
                "database static data validation failed: "
                "fra_challenges=empty"
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
    # Records are a runtime dependency as well as a source for fresh database
    # creation.  Materialize them whenever gamedata is configured, including
    # deployments reusing an existing persistent database.
    if configured_gamedata is not None:
        from AssetExtraction.gamedata_seed import records_available

        if not records_available(configured_records):
            create_records(configured_gamedata, configured_records)
    create_starter_decks(configured_gamedata, configured_records)
    ensure_editor_art(configured_records)
    if exists:
        print(f"[docker] using existing database {path}", flush=True)
        # Persistent deployments must receive new columns, indexes, and
        # idempotent server-owned seeds before any service opens the DB. This
        # updates the database in place and never replaces player state.
        os.environ["HEX_DB_PATH"] = str(path)
        print(f"[docker] applying current schema and seeds to {path}", flush=True)
        upgrade_database(path)
        populate_missing_fra_data(path, configured_records)
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
    populate_missing_fra_data(path, configured_records)
    validate_static_data(path)
    run_tests(path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"[docker] bootstrap failed: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
