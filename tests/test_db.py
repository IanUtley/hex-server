"""Test database bootstrap.

No test reads or seeds the developer's live ``hconnect.db``.  Unless a caller
explicitly points ``HEX_TEST_SOURCE_DB``/``HEX_TEST_DB_TEMPLATE`` at a
snapshot (``tests/run_all.py`` does this once for the whole run, and it is
available for local debugging), each test process instantiates its own
database with ``static.ensure_schema`` — the schema plus the Records-derived
metadata, with no players, decks, or games — and points ``HEX_DB_PATH`` at it
before ``db`` is imported.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_database_path: str | None = None


def fresh_database() -> str:
    """Return this process's static-data database path.

    Call it before importing ``db`` so the runtime connection and the rows a
    test copies from ``SRC`` come from the same freshly seeded file.
    """
    global _database_path
    override = (os.environ.get("HEX_TEST_SOURCE_DB")
                or os.environ.get("HEX_TEST_DB_TEMPLATE"))
    if override:
        path = str(Path(override).resolve())
        _verify_runtime_database(path)
        return path
    if _database_path is None:
        directory = tempfile.mkdtemp(prefix="hex-test-db-")
        atexit.register(shutil.rmtree, directory, True)
        path = os.path.join(directory, "fresh.db")
        _verify_runtime_database(path)
        build_baseline(path)
        # ``db._db`` must open this same file, never the live database.
        os.environ["HEX_DB_PATH"] = path
        _database_path = path
    _verify_runtime_database(_database_path)
    return _database_path


def _verify_runtime_database(path: str) -> None:
    """Fail loudly when ``db`` already opened a different database.

    A test module that imports runtime code before calling
    :func:`fresh_database` has already opened the developer's live
    ``hconnect.db``; nothing can rebind that connection afterwards, so the
    run must stop instead of silently testing against live state.
    """
    module = sys.modules.get("db")
    active = getattr(module, "DB_PATH", None) if module is not None else None
    if active is None:
        return
    candidates = [str(path)]
    intended = os.environ.get("HEX_DB_PATH")
    if intended:
        candidates.append(str(intended))
    for candidate in candidates:
        if str(active) == candidate or os.path.abspath(
                str(active)) == os.path.abspath(candidate):
            return
    raise RuntimeError(
        "tests.test_db.fresh_database() must run before any module imports "
        f"db; the runtime connection already opened {active}")


def build_baseline(path: str) -> str:
    """Instantiate ``path`` as the metadata-only test baseline.

    ``static.ensure_schema`` owns the schema and the Records-derived
    catalogue.  The single synthetic deck row exists because a protocol test
    inspects an authored deck by id and must never read the developer's.
    """
    path = str(path)
    previous = os.environ.get("HEX_DB_PATH")
    os.environ["HEX_DB_PATH"] = path
    try:
        import static

        connection = sqlite3.connect(path, timeout=30.0)
        try:
            static.ensure_schema(connection)
            connection.execute(
                "INSERT OR IGNORE INTO decks "
                "(id,user_id,deck_name,cards,pvp_champion_guid) "
                "VALUES (4,0,?,?,?)",
                ("test-lifesteal", "[]",
                 "0c0ba840-cba0-4e33-a379-4d16aeaf9a73"))
            connection.commit()
        finally:
            connection.close()
    finally:
        if previous is None:
            os.environ.pop("HEX_DB_PATH", None)
        else:
            os.environ["HEX_DB_PATH"] = previous
    return path
