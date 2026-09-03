#!/usr/bin/env python3
"""Run the supported direct test scripts with an isolated database snapshot.

The server database is initialized once by the caller (or copied from the
existing runtime database), then each test process receives its own in-memory
copy.  This avoids running the expensive schema/seed migration path once per
Python process while preserving process-level test isolation.
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
DEFAULT_SOURCE = ROOT / "hconnect.db"
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


def _test_files():
    return [
        path for path in sorted(TESTS.glob("tests_*.py"))
        if path.name not in REMOVED_SWEEPS
    ]


def main():
    source_path = Path(
        os.environ.get("HEX_TEST_SOURCE_DB", str(DEFAULT_SOURCE))
    ).resolve()
    if not source_path.is_file():
        print(f"Test database source does not exist: {source_path}",
              file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="hex-test-suite-") as work_dir:
        template_path = Path(work_dir) / "baseline.db"
        _snapshot(source_path, template_path)

        test_env = os.environ.copy()
        test_env["HEX_TEST_SOURCE_DB"] = str(template_path)
        test_env["HEX_DB_PATH"] = ":memory:"
        test_env["HEX_TEST_DB_TEMPLATE"] = str(template_path)
        test_env["HEX_TEST_DB_READY"] = "1"

        test_files = _test_files()
        all_files = test_files + [TESTS / "verify_goldens.py"]
        print(
            f"Running {len(all_files)} tests with one SQLite snapshot; "
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
