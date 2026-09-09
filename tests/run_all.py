#!/usr/bin/env python3
"""Run the supported direct test scripts against a disposable test database.

By default the baseline is created with ``static.ensure_schema``.  It contains
server and client-derived metadata, but never copies the live ``hconnect.db``
or its users, champions, campaigns, and matches.  An explicit
``HEX_TEST_SOURCE_DB`` remains available for specialized local debugging.
Each test process receives its own in-memory copy of that baseline.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
REMOVED_SWEEPS = {
    "tests_set1_pvp_sweep.py",
    "tests_core_sets_sweep.py",
}


def _snapshot(source_path, target_path):
    source = sqlite3.connect(str(source_path), timeout=30.0)
    target = sqlite3.connect(str(target_path), timeout=30.0)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _create_baseline(target_path):
    """Create the metadata-only baseline used by every test subprocess."""
    source_override = os.environ.get("HEX_TEST_SOURCE_DB")
    if source_override:
        source_path = Path(source_override).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        _snapshot(source_path, target_path)
        return f"snapshot={source_path}"

    previous_db_path = os.environ.get("HEX_DB_PATH")
    os.environ["HEX_DB_PATH"] = str(target_path)
    import static

    db = sqlite3.connect(str(target_path), timeout=30.0)
    try:
        static.ensure_schema(db)
        # A small number of protocol tests inspect an authored deck row by
        # ID. Keep that identity synthetic and metadata-only; never copy the
        # developer's decks or collection into the test baseline.
        db.execute(
            "INSERT OR IGNORE INTO decks "
            "(id,user_id,deck_name,cards,pvp_champion_guid) "
            "VALUES (4,0,?,?,?)",
            ("test-lifesteal", "[]",
             "0c0ba840-cba0-4e33-a379-4d16aeaf9a73"),
        )
        db.commit()
    finally:
        db.close()
        if previous_db_path is None:
            os.environ.pop("HEX_DB_PATH", None)
        else:
            os.environ["HEX_DB_PATH"] = previous_db_path
    return "fresh static database"


def _test_files():
    return [
        path for path in sorted(TESTS.glob("tests_*.py"))
        if path.name not in REMOVED_SWEEPS
    ]


def main():
    with tempfile.TemporaryDirectory(prefix="hex-test-suite-") as work_dir:
        template_path = Path(work_dir) / "baseline.db"
        try:
            baseline_description = _create_baseline(template_path)
        except (OSError, sqlite3.Error) as exc:
            print(f"Could not create test database: {exc}",
                  file=sys.stderr)
            return 2

        test_env = os.environ.copy()
        test_env["HEX_TEST_SOURCE_DB"] = str(template_path)
        test_env["HEX_DB_PATH"] = ":memory:"
        test_env["HEX_TEST_DB_TEMPLATE"] = str(template_path)
        test_env["HEX_TEST_DB_READY"] = "1"

        test_files = _test_files()
        all_files = test_files + [TESTS / "verify_goldens.py"]
        print(
            f"Running {len(all_files)} tests using {baseline_description}; "
            f"excluded sweeps: {', '.join(sorted(REMOVED_SWEEPS))}",
            flush=True,
        )
        failures = []
        timings = []
        for test_file in all_files:
            relative = test_file.relative_to(ROOT)
            print(f"START {relative}", flush=True)
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, str(test_file)],
                cwd=str(ROOT),
                env=test_env,
                check=False,
            )
            elapsed = time.monotonic() - started
            timings.append((relative, elapsed, result.returncode))
            print(
                f"DONE  {relative} {elapsed:.3f}s exit={result.returncode}",
                flush=True,
            )
            if result.returncode:
                failures.append(relative)

        print("Test timings:", flush=True)
        for relative, elapsed, status in timings:
            print(f"  {relative}: {elapsed:.3f}s (exit {status})",
                  flush=True)
        if failures:
            print(
                "Test failures: "
                + ", ".join(str(path) for path in failures),
                file=sys.stderr,
            )
            return 1
        print("All tests passed", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
