"""campaign_data deletes data-file rows removed from their file, never seed rows."""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "campaign")]

import campaign_data


def _rows(db):
    return sorted(db.execute(
        "SELECT conversation_guid, origin FROM campaign_node_conversations"))


def test_removed_data_rows_are_deleted_and_seed_rows_kept():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE campaign_node_conversations (campaign_template TEXT, "
               "node_id TEXT, conversation_guid TEXT, conversation_name TEXT, "
               "trigger_json TEXT DEFAULT '{}', priority INTEGER DEFAULT 0, "
               "enabled INTEGER DEFAULT 1, origin TEXT NOT NULL DEFAULT 'seed')")
    # Seed rows: one overridden by the file, one the file never lists.
    db.execute("INSERT INTO campaign_node_conversations (campaign_template, "
               "node_id, conversation_guid) VALUES ('AZ1', 'N1', 'seed-a'), "
               "('AZ1', 'N1', 'seed-b')")
    row = lambda guid: {"campaign_template": "AZ1", "conversation_guid": guid,
                        "node_id": "N2", "priority": 1}
    first = {"schema_version": 1, "tables": {"campaign_node_conversations": [
        {"campaign_template": "AZ1", "conversation_guid": "seed-a", "priority": 9},
        row("data-kept"), row("data-removed")]}}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder, "AZ1.json")
        path.write_text(json.dumps(first))
        assert campaign_data.apply_all(db, Path(folder))["deleted"] == {}
        assert _rows(db) == [("data-kept", "data"), ("data-removed", "data"),
                             ("seed-a", "seed"), ("seed-b", "seed")]

        # A skipped (invalid) file blocks deleting: its rows look removed.
        Path(folder, "bad.json").write_text("{")
        path.write_text(json.dumps({"schema_version": 1, "tables": {}}))
        assert campaign_data.apply_all(db, Path(folder))["deleted"] == {}
        Path(folder, "bad.json").unlink()

        # Drop the override of a seed row and one data row from the file.
        first["tables"]["campaign_node_conversations"] = [row("data-kept")]
        path.write_text(json.dumps(first))
        result = campaign_data.apply_all(db, Path(folder))
    assert result["deleted"] == {
        "campaign_node_conversations": [("AZ1", "data-removed")]}
    assert _rows(db) == [("data-kept", "data"), ("seed-a", "seed"),
                         ("seed-b", "seed")]
