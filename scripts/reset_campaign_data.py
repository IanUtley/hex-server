#!/usr/bin/env python3
"""Delete all campaign and champion rows from a Hex server database.

This deliberately leaves users, decks, collections, cards, and inventory
untouched.  It is confirmation-gated because the operation is destructive.
"""

import argparse
import os
import sqlite3


def main():
    parser = argparse.ArgumentParser(
        description="Delete all campaigns and champions from a Hex database")
    parser.add_argument(
        "--db",
        default=os.environ.get("HEX_DB_PATH", "hconnect.db"),
        help="SQLite database path (default: HEX_DB_PATH or hconnect.db)")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the destructive deletion")
    args = parser.parse_args()

    if not args.yes:
        parser.error("refusing to delete data without --yes")

    db = sqlite3.connect(args.db, timeout=30)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = {"campaigns", "champions"} - tables
        if missing:
            raise RuntimeError(
                "database is missing required table(s): " + ", ".join(sorted(missing)))

        counts = {}
        for table in ("campaigns", "champions"):
            counts[table] = db.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        with db:
            db.execute("DELETE FROM campaigns")
            db.execute("DELETE FROM champions")

        print("Deleted {campaigns} campaign row(s) and {champions} champion row(s)"
              .format(**counts))
        print("Users, decks, collections, cards, and inventory were not changed")
    finally:
        db.close()


if __name__ == "__main__":
    main()
