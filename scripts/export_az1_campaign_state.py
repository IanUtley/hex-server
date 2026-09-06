#!/usr/bin/env python3
"""Export one persisted AZ1 area campaign in the viewer's state format.

The exporter is read-only.  It converts the server's GameplayState snapshot
into the small runtime overlay consumed by ``web/az1``; campaign.py remains
the authoritative state machine.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _campaign_row(db: sqlite3.Connection, campaign_id: int | None,
                  champion_id: int | None):
    if campaign_id is not None:
        return db.execute(
            "SELECT id, champion_id, champion_name, state_json "
            "FROM campaigns WHERE id=? AND template_name='AZ1' "
            "AND campaign_type='AREA'",
            (campaign_id,),
        ).fetchone()
    if champion_id is not None:
        return db.execute(
            "SELECT id, champion_id, champion_name, state_json "
            "FROM campaigns WHERE champion_id=? AND template_name='AZ1' "
            "AND campaign_type='AREA' ORDER BY id DESC LIMIT 1",
            (champion_id,),
        ).fetchone()
    return db.execute(
        "SELECT id, champion_id, champion_name, state_json "
        "FROM campaigns WHERE template_name='AZ1' AND campaign_type='AREA' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


def export_state(db: sqlite3.Connection, row) -> dict:
    if not row:
        raise RuntimeError("No AZ1 area campaign matched the requested selector")
    campaign_id, champion_id, champion_name, raw_state = row
    state = json.loads(raw_state or "{}")
    if not isinstance(state, dict):
        raise RuntimeError(f"Campaign {campaign_id} has invalid state_json")
    public = state.get("PublicState") or {}
    data = public.get("Data") or {}
    locations = state.get("VisLocs") or []
    current_node = state.get("LastNode")
    if not current_node and state.get("ALoc"):
        current_node = next(
            ((location.get("Data") or {}).get("node")
             for location in locations
             if (location.get("Data") or {}).get("name") == state["ALoc"]),
            None,
        )
    return {
        "schema_version": 1,
        "label": f"{champion_name or champion_id} / AZ1 area #{campaign_id}",
        "campaign_id": campaign_id,
        "champion_id": champion_id,
        "current_node": current_node,
        "visited_nodes": list(data.get("visited_nodes") or []),
        "visited_paths": list(data.get("visited_paths") or []),
        "quest_nodes": list(data.get("quest_nodes") or []),
        "blocked_nodes": list(data.get("blocked_nodes") or []),
        "locations": locations,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("hconnect.db"))
    parser.add_argument("--campaign-id", type=int)
    parser.add_argument("--champion-id", type=int)
    parser.add_argument("--output", type=Path,
                        help="Write JSON here instead of stdout")
    args = parser.parse_args()
    if args.campaign_id is not None and args.champion_id is not None:
        parser.error("use --campaign-id or --champion-id, not both")
    with sqlite3.connect(args.database) as db:
        payload = export_state(
            db, _campaign_row(db, args.campaign_id, args.champion_id))
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(args.output)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
