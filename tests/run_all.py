#!/usr/bin/env python3
"""Run the supported direct test scripts against a disposable test database.

By default the baseline is created with ``static.ensure_schema``.  It contains
server and client-derived metadata, but never copies the live ``hconnect.db``
or its users, champions, campaigns, and matches.  An explicit
``HEX_TEST_SOURCE_DB`` remains available for specialized local debugging.
Each test process receives its own in-memory copy of that baseline.
"""

import argparse
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
QUICK_TESTS = {
    "tests_application.py",
    "tests_chain_abilities.py",
    "tests_conditions.py",
    "tests_effect_context.py",
    "tests_encoding.py",
    "tests_events.py",
    "tests_leaves.py",
    "tests_resolution.py",
    "tests_statics.py",
    "tests_targeting.py",
    "tests_rules_port_kernel.py",
    "tests_rules_port_csharp_parity.py",
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

    # One owner for what a test baseline contains: the same builder serves
    # direct test runs (tests/test_db.py) and this suite.
    from tests.test_db import build_baseline

    build_baseline(target_path)
    return "fresh static database"


def _test_files(quick=False, only=None):
    paths = [
        path for path in sorted(TESTS.glob("tests_*.py"))
        if path.name not in REMOVED_SWEEPS
    ]
    if quick:
        paths = [path for path in paths if path.name in QUICK_TESTS]
    if only:
        wanted = {str(value).strip().lower() for value in only if str(value).strip()}
        aliases = {
            "rules_port": {
                "tests_rules_port_kernel.py",
                "tests_rules_port_csharp_parity.py",
                "tests_chain_abilities.py",
                "tests_combat.py",
            },
            "rules-port": {
                "tests_rules_port_kernel.py",
                "tests_rules_port_csharp_parity.py",
                "tests_chain_abilities.py",
                "tests_combat.py",
            },
        }
        expanded = set()
        for value in wanted:
            expanded.update(aliases.get(value, {value}))
        paths = [path for path in paths if path.name.lower() in expanded or
                 path.stem.lower() in expanded]
    return paths


def main(quick=False, only=None):
    # Windows cannot delete the baseline while a SQLite handle is still open.
    with tempfile.TemporaryDirectory(prefix="hex-test-suite-",
                                     ignore_cleanup_errors=True) as work_dir:
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

        test_files = _test_files(quick=quick, only=only)
        all_files = (
            test_files
            if quick or only
            else test_files + [TESTS / "verify_goldens.py"]
        )
        print(
            f"Running {len(all_files)} {'quick ' if quick else ''}tests using "
            f"{baseline_description}; "
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="run the core pre-commit tests without the broader suite",
    )
    parser.add_argument(
        "--only",
        help=("run selected tests serially; accepts comma-separated filenames, "
              "stems, or the rules_port alias"),
    )
    args = parser.parse_args()
    only = args.only.split(",") if args.only else None
    raise SystemExit(main(quick=args.quick, only=only))
